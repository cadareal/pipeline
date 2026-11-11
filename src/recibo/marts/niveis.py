"""Níveis de dentro de cada linha do recibo: o que o site abre quando a pessoa clica numa faixa.

Para cada linha, uma lista de partes que somam o valor da linha, com a fonte de cada parte:

  linhas com filtro no Anexo 02 (saúde, educação...)  -> subfunções do RREO, cada uma com o valor bruto do RREO e os
                                                          ajustes (o que saiu dela para outra linha ou para fora do
                                                          R$ 1,00, medido no SIOP), e as ações do SIOP dentro dela
  o resíduo (encargos especiais e demais funções)     -> funções do RREO, cada uma com suas subfunções, menos o que
                                                          o SIOP mostra que saiu do resíduo (aposentados, empréstimos,
                                                          serviço da dívida fora de 843/844), mais o que a lei chama
                                                          de empréstimo e o Tesouro conta como gasto (fica no resíduo)
  linhas com valor do SIOP (aposentados e pensionistas) -> elementos de despesa (aposentadorias, pensões), cada um
                                                          aberto por órgão
  juros                                               -> nenhum nível aqui: as partes vêm do mart da dívida
                                                          (execução por carteira), que o site junta

Regra: as partes de uma linha somam o valor da linha. Quando um ajuste não tem detalhe por subfunção (ano em
andamento, sem SIOP), ele fica em `nao_alocado`, com nome e valor negativo, e a conta fecha com ele; o site mostra
isso em vez de esconder. A conferência informativa `niveis_fecham` acusa qualquer linha em que a conta não feche.
"""

from __future__ import annotations

import logging
import re
from typing import Callable

from .recibo import Item, Valores, _tol, base_where, lit, universo_siop

log = logging.getLogger(__name__)

RE_PREFIXO_FU = re.compile(r"^FU\s*\d+\s*-\s*", re.I)
ACOES_PADRAO = 40


def nome_subfuncao(conta: str | None, subfuncao_cod: str | None) -> str:
    """'FU10 - Administração Geral' -> 'Administração Geral'; o balde 'demais' vira 'Demais subfunções'."""
    s = RE_PREFIXO_FU.sub("", str(conta or "").strip())
    if subfuncao_cod == "demais" or not s:
        return "Demais subfunções" if subfuncao_cod == "demais" else (str(subfuncao_cod or "") or "sem nome")
    return s


def _fmt(v: float) -> str:
    return f"{v:,.2f}"


class Niveis:
    def __init__(self, con, q: Callable[[str], float], rows: Callable[[str], list], linhas_cfg: dict, exercicio: int, periodo: int, v: Valores, usa_siop: bool, tem_siop: bool, siop_info: dict):
        self.con, self.q, self.rows = con, q, rows
        self.cfg = linhas_cfg
        self.exercicio, self.periodo = exercicio, periodo
        self.v = v
        self.usa_siop, self.tem_siop = usa_siop, tem_siop
        self.siop_info = siop_info or {}
        self.metrica = linhas_cfg["metrica"]
        self.bases = linhas_cfg["bases"]
        self.b02 = self.bases["anexo02_subfuncao"]
        self.w02 = base_where(self.b02, exercicio, periodo, self.metrica)
        self.w02_fun = base_where({**self.b02, "nivel": "funcao"}, exercicio, periodo, self.metrica)
        self.tol = _tol(v.total_base)
        scfg = linhas_cfg.get("siop", {})
        self.ag_acao_rp = scfg.get("agregacao_acao_rp", "acao_rp_subfuncao")
        maximo = int(scfg.get("niveis_acoes_maximo", 0) or 0)
        self.n_acoes = maximo if maximo > 0 else 10**9  # 0 = todas: o site mostra tudo e cresce para baixo
        self.saida_maxima = float(scfg.get("niveis_acoes_saida_maxima", 0.25))  # acima disso, a subfunção não abre por ação
        self.divida_subf = [str(s) for s in scfg.get("subfuncoes_servico_divida", ["841", "842", "843", "844"])]
        self.tem_acoes = bool(tem_siop and usa_siop and q(f"SELECT COUNT(*) FROM siop WHERE exercicio = {int(exercicio)} AND agregacao = {lit(self.ag_acao_rp)}") > 0)
        self.nomes_funcao = {str(c): str(n or "").strip() for c, n in rows(f"SELECT funcao_cod, MAX(conta) FROM stg WHERE {self.w02_fun} GROUP BY 1")}
        self.nomes_subfuncao: dict[tuple[str, str], str] = {}
        for f, s, n in rows(f"SELECT funcao_cod, subfuncao_cod, MAX(conta) FROM stg WHERE {self.w02} GROUP BY 1, 2"):
            self.nomes_subfuncao[(str(f), str(s))] = nome_subfuncao(n, str(s))
        self.uni_acoes = f"exercicio = {int(exercicio)} AND agregacao = {lit(self.ag_acao_rp)} AND ofss AND NOT intra"

    # ------------------------------------------------------------------ utilidades

    def _filtro_linhas_nomeadas(self) -> str | None:
        """Filtros (RREO) das linhas nomeadas e das exclusões por subfunção, negados: o que sobra é o resíduo."""
        partes = []
        for x in self.v.linhas:
            if x.cfg.get("residual"):
                continue
            if x.cfg.get("base") == "anexo02_subfuncao" and x.cfg.get("filtro"):
                partes.append(f"NOT ({x.cfg['filtro']})")
            elif x.piso and x.cfg.get("fallback", {}).get("base") == "anexo02_subfuncao" and x.cfg["fallback"].get("filtro"):
                partes.append(f"NOT ({x.cfg['fallback']['filtro']})")
        for e in self.v.excluidos:
            if e.cfg.get("base") == "anexo02_subfuncao" and e.cfg.get("filtro"):
                partes.append(f"NOT ({e.cfg['filtro']})")
        return " AND ".join(partes) if partes else None

    def _subfuncoes_rreo(self, filtro: str | None) -> list[dict]:
        w = self.w02 + (f" AND ({filtro})" if filtro else "")
        out = []
        for f, s, n, val in self.rows(f"SELECT funcao_cod, subfuncao_cod, MAX(conta), SUM(valor) FROM stg WHERE {w} GROUP BY 1, 2 ORDER BY 4 DESC"):
            f, s = str(f), str(s)
            out.append({
                "id": f"{f}-{s}", "funcao_cod": f, "funcao": self.nomes_funcao.get(f, f), "subfuncao_cod": s,
                "nome": nome_subfuncao(n, s), "valor_rreo": float(val or 0.0), "ajustes": [], "valor": float(val or 0.0),
            })
        return out

    @staticmethod
    def _achar(itens: list[dict], f: str, s: str) -> dict | None:
        """Casa (função, subfunção) do SIOP com uma subfunção do RREO; subfunção atípica cai no balde 'demais' da função."""
        for it in itens:
            if it["funcao_cod"] == f and it["subfuncao_cod"] == s:
                return it
        for it in itens:
            if it["funcao_cod"] == f and it["subfuncao_cod"] == "demais":
                return it
        return None

    def _aplicar(self, itens: list[dict], nao_alocado: list[dict], pares: list[tuple], nome: str, para: str | None, sinal: float = -1.0) -> float:
        """Aplica ajustes por (função, subfunção) às subfunções; o que não casa vai para nao_alocado. Devolve o total aplicado."""
        total = 0.0
        sobra = 0.0
        for f, s, val in pares:
            val = float(val or 0.0) * sinal
            if not val:
                continue
            it = self._achar(itens, str(f), str(s))
            if it is None:
                sobra += val
                continue
            it["ajustes"].append({"nome": nome, "para": para, "valor": val})
            it["valor"] += val
            total += val
        if abs(sobra) > 0:
            nao_alocado.append({"nome": f"{nome} (subfunções que o RREO não lista nesta linha)", "para": para, "valor": sobra})
            total += sobra
        return total

    def _acoes(self, f: str, s: str, codigos_funcao: list[str], excluir: str | None = None) -> dict:
        """Ações do SIOP dentro de (função, subfunção), sem o que não é gasto do R$ 1,00 (GND 5 com RP 0; GND 2 e 6).
        `excluir` é um filtro a mais (no resíduo, as subfunções das linhas nomeadas: o balde "demais" de uma função
        não pode listar a Previdência Básica do INSS como se fosse resíduo)."""
        if not self.tem_acoes:
            return {}
        if s == "demais":
            tipicas = [c for c in codigos_funcao if c != "demais"]
            cond = f"funcao_cod = {lit(f)}" + (f" AND COALESCE(subfuncao_cod, '') NOT IN ({', '.join(lit(c) for c in tipicas)})" if tipicas else "")
        else:
            cond = f"funcao_cod = {lit(f)} AND subfuncao_cod = {lit(s)}"
        if excluir:
            cond += f" AND ({excluir})"
        w = f"{self.uni_acoes} AND {cond} AND NOT (cod_gnd = '5' AND cod_resultado_primario = '0') AND cod_gnd NOT IN ('2', '6')"
        try:
            lista = self.rows(f"SELECT cod_acao, MAX(nome_acao), SUM(valor) FROM siop_lin WHERE {w} GROUP BY 1 HAVING SUM(valor) <> 0 ORDER BY 3 DESC")
        except Exception as e:  # coluna ausente numa agregação antiga: a linha fica sem ações, o número não muda
            log.debug("ações indisponíveis para %s/%s: %s", f, s, e)
            return {}
        acoes = [{"cod": str(a), "nome": str(nm or "").strip(), "valor": float(val or 0.0)} for a, nm, val in lista]
        top, resto = acoes[: self.n_acoes], acoes[self.n_acoes :]
        return {
            "acoes": top,
            "acoes_demais": {"n": len(resto), "valor": sum(a["valor"] for a in resto)} if resto else None,
            "acoes_total_siop": sum(a["valor"] for a in acoes),
        }

    def _pendurar_acoes(self, itens: list[dict], excluir: str | None = None) -> None:
        """Pendura as ações do SIOP em cada subfunção. Quando parte da subfunção saiu para uma linha medida por elemento
        de despesa (aposentados e pensionistas: elementos 01 e 03), a lista de ações não consegue acompanhar, porque a
        agregação do SIOP por ação não traz o elemento, e as ações aparecem inteiras (com dinheiro que o site mostra em
        outra faixa). O que sai registrado: `acoes_saida` (quanto saiu, para onde, que fração do total das ações é) quando
        a parte é pequena; `acoes_bloqueadas` (sem lista de ações, com os mesmos números) quando passa de
        `siop.niveis_acoes_saida_maxima` do total das ações, em vez de listar R$ 104 bi de uma ação dentro de uma faixa
        de R$ 959 mi. Débito da V2 (DEBITOS.md): a agregação ação x elemento, que abre todas sem ressalva."""
        por_funcao: dict[str, list[str]] = {}
        for it in itens:
            por_funcao.setdefault(it["funcao_cod"], []).append(it["subfuncao_cod"])
        por_elemento = {x.id: x.nome for x in self.v.linhas if x.fonte_valor == "siop"}
        for it in itens:
            saidas = [a for a in (it.get("ajustes") or []) if a.get("para") in por_elemento and float(a.get("valor") or 0.0) < 0]
            acoes = self._acoes(it["funcao_cod"], it["subfuncao_cod"], por_funcao[it["funcao_cod"]], excluir)
            if saidas and acoes:
                saiu = -sum(float(a["valor"]) for a in saidas)
                total = float(acoes.get("acoes_total_siop") or 0.0)
                info = {
                    "total_siop": total, "saiu": saiu, "fracao": (saiu / total) if total > 0 else None,
                    "para": sorted({por_elemento[a["para"]] for a in saidas}), "limite": self.saida_maxima,
                    "motivo": "parte desta subfunção saiu para uma linha medida por elemento de despesa, e a lista de ações do SIOP não traz o elemento",
                }
                if total <= 0 or saiu > self.saida_maxima * total:
                    it["acoes_bloqueadas"] = info
                    continue
                it["acoes_saida"] = info
            it.update(acoes)

    # ------------------------------------------------------------------ por tipo de linha

    def linha_rreo(self, x: Item, filtro: str) -> dict:
        itens = self._subfuncoes_rreo(filtro)
        nao_alocado: list[dict] = []
        if self.usa_siop:
            # o que saiu desta linha para uma linha do SIOP (ex.: pensões militares para aposentados), por subfunção
            for s_item in self.v.linhas:
                if s_item.fonte_valor != "siop" or not s_item.cfg.get("transfere_de_linhas") or not s_item.cfg.get("filtro"):
                    continue
                uni = universo_siop(self.bases[s_item.cfg["base"]], self.exercicio)
                pares = self.rows(f"SELECT funcao_cod, subfuncao_cod, SUM(valor) FROM siop_lin WHERE {uni} AND ({filtro}) AND ({s_item.cfg['filtro']}) GROUP BY 1, 2")
                self._aplicar(itens, nao_alocado, pares, f"saiu para a linha {s_item.nome}", s_item.id)
            # o que saiu desta linha para fora do R$ 1,00 (empréstimos e aplicações financeiras pela lei: GND 5 com RP 0)
            excl = next((e for e in self.v.excluidos if e.cfg.get("calculo") == "tesouro"), None)
            if excl is not None and excl.por_linha:
                pl_cfg = excl.cfg.get("por_linha") or {}
                base_pl = self.bases.get(pl_cfg.get("base", ""))
                if base_pl is not None and pl_cfg.get("filtro"):
                    uni = universo_siop(base_pl, self.exercicio)
                    pares = self.rows(f"SELECT funcao_cod, subfuncao_cod, SUM(valor) FROM siop_lin WHERE {uni} AND ({filtro}) AND ({pl_cfg['filtro']}) GROUP BY 1, 2")
                    self._aplicar(itens, nao_alocado, pares, "fora do R$ 1,00: empréstimos e aplicações financeiras", "fora_do_real")
        else:
            # sem SIOP, as transferências da linha não têm detalhe por subfunção: ficam nomeadas, sem alocar
            for t in x.transferencias:
                if t.get("valor", 0.0) < 0:
                    nao_alocado.append({"nome": f"saiu desta linha para {t.get('para')} (sem detalhe por subfunção neste período)", "para": t.get("para"), "valor": float(t["valor"])})
        self._pendurar_acoes(itens)
        return self._fechar(x, "subfuncoes", itens, nao_alocado, "RREO, Anexo 02 (despesa liquidada por subfunção)" + ("; ajustes por subfunção pelo SIOP" if self.usa_siop else ""))

    def linha_residual(self, x: Item) -> dict:
        filtro = self._filtro_linhas_nomeadas()
        itens = self._subfuncoes_rreo(filtro)
        nao_alocado: list[dict] = []
        lt = self.siop_info.get("lei_vs_tesouro") or {}
        if self.usa_siop:
            nomeadas = filtro or "TRUE"
            # aposentados e pensionistas (SIOP) que estavam em subfunções do resíduo (272, 274 e outras)
            for s_item in self.v.linhas:
                if s_item.fonte_valor != "siop" or not s_item.cfg.get("filtro"):
                    continue
                uni = universo_siop(self.bases[s_item.cfg["base"]], self.exercicio)
                pares = self.rows(f"SELECT funcao_cod, subfuncao_cod, SUM(valor) FROM siop_lin WHERE {uni} AND ({s_item.cfg['filtro']}) AND ({nomeadas}) GROUP BY 1, 2")
                self._aplicar(itens, nao_alocado, pares, f"saiu para a linha {s_item.nome}", s_item.id)
            # empréstimos e aplicações financeiras pela lei (GND 5, RP 0) nas subfunções do resíduo...
            excl = next((e for e in self.v.excluidos if e.cfg.get("calculo") == "tesouro"), None)
            if excl is not None and excl.por_linha:
                pl_cfg = excl.cfg.get("por_linha") or {}
                base_pl = self.bases.get(pl_cfg.get("base", ""))
                if base_pl is not None and pl_cfg.get("filtro"):
                    uni = universo_siop(base_pl, self.exercicio)
                    pares = self.rows(f"SELECT funcao_cod, subfuncao_cod, SUM(valor) FROM siop_lin WHERE {uni} AND ({pl_cfg['filtro']}) AND ({nomeadas}) GROUP BY 1, 2")
                    self._aplicar(itens, nao_alocado, pares, "fora do R$ 1,00: empréstimos e aplicações financeiras", "fora_do_real")
                # a exclusão do Tesouro também tem partes em outros grupos de despesa (sem contrapartida no carimbo da
                # lei): saem do resíduo sem detalhe por subfunção, nomeadas
                outros = sum(float(gr.get("financeira") or 0.0) for gid, gr in (excl.por_grupo or {}).items() if gid != (lt.get("grupo") or "inversoes_financeiras"))
                if abs(outros) > 0.005:
                    nao_alocado.append({"nome": "fora do R$ 1,00: despesas que o Tesouro trata como financeiras em outros grupos (sem detalhe por subfunção)", "para": "fora_do_real", "valor": -outros})
            # ...menos o que a lei carimba como empréstimo e o Tesouro conta como gasto: volta para o resíduo, nomeado
            fica = float(lt.get("fica_no_real") or 0.0)
            if fica > 0.005:
                fundos = lt.get("fundos_constitucionais") or {}
                itens.append({
                    "id": "lei_vs_tesouro", "funcao_cod": "28", "funcao": self.nomes_funcao.get("28", "Encargos Especiais"), "subfuncao_cod": None,
                    "nome": "Empréstimos pela lei que o Tesouro conta como gasto (fundos constitucionais FNE/FNO/FCO e outros)",
                    "valor_rreo": 0.0, "ajustes": [{"nome": "o Tesouro trata como gasto; a lei orçamentária, como empréstimo (GND 5, RP 0)", "para": None, "valor": fica}], "valor": fica,
                    "detalhe": {"fundos_constitucionais": fundos.get("valor"), "sem_acao_identificada": lt.get("sem_acao_identificada"), "nota": lt.get("nota")},
                })
            # serviço da dívida fora de 843/844 (juros já na linha de juros; amortização fora do R$ 1,00), por subfunção
            if self.v.ajuste > 0:
                lista_divida = ", ".join(lit(s) for s in self.divida_subf)
                ag_gnd = self.siop_info.get("agregacao_gnd", "fonte_gnd_subfuncao")
                pares = self.rows(
                    f"SELECT cod_funcao, cod_subfuncao, SUM(liquidado) FROM siop WHERE exercicio = {int(self.exercicio)} AND agregacao = {lit(ag_gnd)} AND ofss AND NOT intra "
                    f"AND cod_gnd IN ('2', '6') AND COALESCE(cod_subfuncao, '') NOT IN ({lista_divida}) AND NOT COALESCE(refinanciamento, false) GROUP BY 1, 2"
                )
                aplicado = self._aplicar(itens, nao_alocado, pares, "serviço da dívida fora de 843/844 (juros na linha de juros; amortização fora do R$ 1,00)", "juros")
                dif = -self.v.ajuste - aplicado
                if abs(dif) > 0.005:
                    nao_alocado.append({"nome": "serviço da dívida fora de 843/844: diferença entre a estimativa do RREO e o SIOP", "para": "juros", "valor": dif})
        else:
            excl = next((e for e in self.v.excluidos if e.cfg.get("calculo") == "tesouro"), None)
            if excl is not None and excl.valor:
                do_residual = excl.valor - sum(t.get("valor", 0.0) for t in excl.transferencias)
                if abs(do_residual) > 0.005:
                    nao_alocado.append({"nome": "fora do R$ 1,00: empréstimos e aplicações financeiras (sem detalhe por subfunção neste período)", "para": "fora_do_real", "valor": -do_residual})
            if self.v.ajuste > 0:
                nao_alocado.append({"nome": "serviço da dívida fora de 843/844 (sem detalhe por subfunção neste período)", "para": "juros", "valor": -self.v.ajuste})
            for s_item in self.v.linhas:
                if s_item.fonte_valor == "siop":
                    nao_alocado.append({"nome": f"saiu para a linha {s_item.nome}", "para": s_item.id, "valor": -s_item.valor})
        self._pendurar_acoes([it for it in itens if it.get("subfuncao_cod")], excluir=filtro)
        # agrupa por função, para o desenho abrir em dois passos (função, depois subfunção)
        funcoes: dict[str, dict] = {}
        for it in itens:
            fcod = it["funcao_cod"]
            fn = funcoes.setdefault(fcod, {"id": fcod, "cod": fcod, "nome": it["funcao"], "valor": 0.0, "subfuncoes": []})
            fn["valor"] += it["valor"]
            fn["subfuncoes"].append(it)
        lista = sorted(funcoes.values(), key=lambda d: -d["valor"])
        return self._fechar(x, "funcoes", lista, nao_alocado, "RREO, Anexo 02 (despesa liquidada por função e subfunção, no que sobra depois das linhas nomeadas)" + ("; ajustes pelo SIOP" if self.usa_siop else ""))

    def linha_siop(self, x: Item) -> dict:
        """Linha com valor do SIOP (elementos de despesa): abre por elemento e, dentro, por órgão."""
        base_cfg = self.bases[x.cfg["base"]]
        uni = universo_siop(base_cfg, self.exercicio)
        filtro = x.cfg.get("filtro")
        w = uni + (f" AND ({filtro})" if filtro else "")
        itens = []
        try:
            elementos = self.rows(f"SELECT cod_elemento_despesa, MAX(nome_elemento_despesa), SUM(valor) FROM siop_lin WHERE {w} GROUP BY 1 ORDER BY 3 DESC")
            for cod, nome, val in elementos:
                orgaos = self.rows(f"SELECT cod_orgao, MAX(nome_orgao), SUM(valor) FROM siop_lin WHERE {w} AND cod_elemento_despesa = {lit(str(cod))} GROUP BY 1 HAVING SUM(valor) <> 0 ORDER BY 3 DESC")
                lista = [{"cod": str(o), "nome": str(n or "").strip(), "valor": float(v or 0.0)} for o, n, v in orgaos]
                top, resto = lista[: self.n_acoes], lista[self.n_acoes :]
                itens.append({
                    "id": str(cod), "cod": str(cod), "nome": str(nome or "").strip() or str(cod), "valor": float(val or 0.0),
                    "orgaos": top, "orgaos_demais": {"n": len(resto), "valor": sum(o["valor"] for o in resto)} if resto else None,
                })
        except Exception as e:
            log.debug("níveis da linha %s indisponíveis no SIOP: %s", x.id, e)
            return self._fechar(x, None, [], [], f"SIOP (não foi possível abrir: {e})")
        return self._fechar(x, "elementos", itens, [], "SIOP, execução por elemento de despesa e órgão (liquidado), mesmo universo do RREO")

    def _fechar(self, x: Item, tipo: str | None, itens: list[dict], nao_alocado: list[dict], fonte: str) -> dict:
        soma = sum(it["valor"] for it in itens) + sum(n["valor"] for n in nao_alocado)
        dif = soma - x.valor
        fecha = abs(dif) <= max(self.tol, 1.0)
        if not fecha:
            log.warning("níveis da linha %s não fecham: partes %s vs linha %s (diferença %s)", x.id, _fmt(soma), _fmt(x.valor), _fmt(dif))
        return {"tipo": tipo, "itens": itens, "nao_alocado": nao_alocado, "soma": soma, "fecha": fecha, "diferenca": dif, "fonte": fonte,
                "acoes_fonte": ("SIOP, ações por subfunção (liquidado), sem GND 5 com RP 0 e sem GND 2 e 6" if self.tem_acoes else None)}

    # ------------------------------------------------------------------ entrada

    def todas(self) -> dict[str, dict]:
        out: dict[str, dict] = {}
        for x in self.v.linhas:
            try:
                if x.cfg.get("residual"):
                    out[x.id] = self.linha_residual(x)
                elif x.fonte_valor == "siop":
                    out[x.id] = self.linha_siop(x)
                elif x.cfg.get("base") == "anexo02_subfuncao" and x.cfg.get("filtro"):
                    out[x.id] = self.linha_rreo(x, x.cfg["filtro"])
                elif x.piso and (x.cfg.get("fallback") or {}).get("base") == "anexo02_subfuncao" and x.cfg["fallback"].get("filtro"):
                    out[x.id] = self.linha_rreo(x, x.cfg["fallback"]["filtro"])
                else:
                    out[x.id] = {"tipo": None, "itens": [], "nao_alocado": [], "soma": x.valor, "fecha": True, "diferenca": 0.0,
                                 "fonte": "sem níveis nesta fonte" + (" (as partes dos juros vêm do mart da dívida, execução por carteira)" if x.id == "juros" else ""), "acoes_fonte": None}
            except Exception as e:  # níveis nunca derrubam o recibo: a linha fica sem abertura, com o motivo
                log.warning("níveis da linha %s não calculados: %s", x.id, e)
                out[x.id] = {"tipo": None, "itens": [], "nao_alocado": [], "soma": None, "fecha": False, "diferenca": None, "fonte": f"erro ao abrir: {e}", "acoes_fonte": None}
        return out


def calcular_niveis(con, q, rows, linhas_cfg: dict, exercicio: int, periodo: int, v: Valores, usa_siop: bool, tem_siop: bool, siop_info: dict) -> dict[str, dict]:
    return Niveis(con, q, rows, linhas_cfg, exercicio, periodo, v, usa_siop, tem_siop, siop_info).todas()
