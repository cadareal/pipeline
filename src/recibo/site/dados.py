"""Do export (data/export/recibo_{ano}.json, divida_{ano}.json) para o modelo das páginas, já formatado.

Nenhum número é digitado: tudo vem dos exports. Nenhum número é formatado fora daqui: o HTML e o fluxo.js recebem
strings prontas ("R$ 990,2 bi", "31 centavos", "30,7%", "R$ 4.640 por habitante"), com o valor bruto ao lado para o
desenho medir as larguras.

Um "nó" do desenho (entrada, saída, linha, parte de linha) é um dicionário com: id, nome, valor (reais), txt, exato,
pct e cent (em relação ao R$ 1,00 da visão), pct_pai e cent_pai (em relação ao pai), definicao, nota, fonte, hab
(por habitante), pagina (quando a linha tem página própria), filhos, nao_alocado (ajustes nomeados que não têm
subfunção), proximo (o que será o próximo nível quando ele ainda não existe) e negativo (parte que ficou negativa
depois dos ajustes: fica na ficha, não no desenho).
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from ..textnorm import normalizar_familia

ACOES_NO_DESENHO = None  # sem limite: o desenho cresce para baixo para caber tudo; None = todas as ações
MINIMO_NO_DESENHO = 0.0  # partes com valor <= 0 não são desenhadas (ficam na ficha)


# --------------------------------------------------------------------------------------------------------------------
# formatação (pt-BR)
# --------------------------------------------------------------------------------------------------------------------


def _br(v: float, casas: int) -> str:
    s = f"{v:,.{casas}f}"
    return s.replace(",", "\x00").replace(".", ",").replace("\x00", ".")


def fmt_exato(v: float | None) -> str:
    if v is None:
        return "indisponível"
    return ("-" if v < 0 else "") + "R$ " + _br(abs(v), 2)


def fmt_bi(v: float | None) -> str:
    """R$ 3,22 tri · R$ 990,2 bi · R$ 282,9 mi · R$ 12 mil · R$ 350,00."""
    if v is None:
        return "indisponível"
    sinal = "-" if v < 0 else ""
    a = abs(v)
    if a >= 1e12:
        return f"{sinal}R$ {_br(a / 1e12, 2)} tri"
    if a >= 1e9:
        return f"{sinal}R$ {_br(a / 1e9, 1)} bi"
    if a >= 1e6:
        return f"{sinal}R$ {_br(a / 1e6, 1)} mi"
    if a >= 1e3:
        return f"{sinal}R$ {_br(a / 1e3, 0)} mil"
    return f"{sinal}R$ {_br(a, 2)}"


def fmt_pct(v: float | None, total: float | None, casas: int = 1) -> str:
    if v is None or not total:
        return ""
    return _br(v / total * 100, casas) + "%"


def fmt_cent(v: float | None, total: float | None, oficial: int | None = None) -> str:
    """Centavos de cada R$ 1,00. `oficial` é o inteiro publicado no recibo (largest remainder), usado quando existe."""
    if oficial is not None:
        return f"{oficial} centavo" + ("" if oficial == 1 else "s")
    if v is None or not total:
        return ""
    c = v / total * 100
    if c < 0:
        return _br(c, 1) + " centavos"
    if c < 0.05:
        return "menos de 0,1 centavo"
    if c >= 10:
        s = f"{round(c):d}"
        return s + (" centavo" if s == "1" else " centavos")
    s = _br(c, 1)
    return s + (" centavo" if s == "1,0" else " centavos")


def fmt_hab(v: float | None, pop: float | None) -> str:
    if v is None or not pop:
        return ""
    x = v / pop
    return "R$ " + (_br(x, 0) if abs(x) >= 100 else _br(x, 2)) + " por habitante"


def fmt_int(n: float | int | None) -> str:
    return "" if n is None else _br(float(n), 0)


def fmt_var(v: float | None) -> str:
    if v is None:
        return ""
    return ("+" if v >= 0 else "-") + _br(abs(v) * 100, 1) + "%"


def slug(texto: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", texto.lower().replace("_", "-")).strip("-")


# --------------------------------------------------------------------------------------------------------------------
# nós
# --------------------------------------------------------------------------------------------------------------------


def no(nid: str, nome: str, valor: float | None, total: float, pop: float | None, *, pai_valor: float | None = None, oficial: int | None = None, **extra) -> dict:
    v = float(valor or 0.0)
    d = {
        "id": nid,
        "nome": nome,
        "valor": v,
        "txt": fmt_bi(v),
        "exato": fmt_exato(valor),
        "pct": fmt_pct(v, total),
        "cent": fmt_cent(v, total, oficial),
        "pct_pai": fmt_pct(v, pai_valor) if pai_valor else "",
        "cent_pai": fmt_cent(v, pai_valor) if pai_valor else "",
        "hab": fmt_hab(v, pop),
        "base_exato": fmt_exato(total),
        "definicao": "",
        "nota": "",
        "fonte": "",
        "pagina": None,
        "filhos": [],
        "nao_alocado": [],
        "proximo": "",
        "negativo": v < 0,
    }
    d.update({k: val for k, val in extra.items() if val is not None})
    return d


def _texto(conteudo: dict, grupo: str, nid: str, campo: str, padrao: str = "") -> str:
    return " ".join(str(((conteudo.get(grupo) or {}).get(nid) or {}).get(campo) or padrao).split())


def _memo(exp: dict, mid: str) -> dict | None:
    return next((m for m in exp.get("memos", []) if m["id"] == mid), None)


def _comp(memo: dict | None, cid: str) -> float | None:
    if not memo:
        return None
    c = next((c for c in memo.get("componentes", []) if c["id"] == cid), None)
    return None if c is None else c.get("valor")


def _ordenar(filhos: list[dict]) -> list[dict]:
    return sorted(filhos, key=lambda f: -f["valor"])


# --------------------------------------------------------------------------------------------------------------------
# receitas (lado esquerdo das duas visões)
# --------------------------------------------------------------------------------------------------------------------


def _tributos(exp: dict, total: float, pop: float | None, conteudo: dict) -> dict:
    m_trib = _memo(exp, "tributos")
    a06 = exp.get("anexo06_referencias") or {}
    valor = (a06.get("tributarias:realizadas") or 0.0) + (a06.get("contribuicoes:realizadas") or 0.0) if a06 else (m_trib or {}).get("valor")
    n = no("tributos", _texto(conteudo, "nos", "tributos", "nome", "Tributos"), valor, total, pop,
           definicao=_texto(conteudo, "nos", "tributos", "definicao"), nota=_texto(conteudo, "nos", "tributos", "nota"), cor="#0B6B3A",
           fonte="RREO, Anexo 06 (receita tributária e de contribuições realizadas no período)")
    if m_trib and m_trib.get("disponivel"):
        for cid in ("impostos", "contribuicoes_sociais", "contribuicoes_economicas", "taxas"):
            v = _comp(m_trib, cid)
            if v is None:
                continue
            n["filhos"].append(no(cid, _texto(conteudo, "nos", cid, "nome", cid), v, total, pop, pai_valor=n["valor"],
                                  definicao=_texto(conteudo, "nos", cid, "definicao"), nota=_texto(conteudo, "nos", cid, "nota"), fonte="RREO, Anexo 01 (receitas realizadas)",
                                  proximo="Fim do que o RREO abre. A abertura imposto por imposto (IR, IPI, IOF...) é da Receita Federal, em fase futura."))
        n["filhos"] = _ordenar(n["filhos"])
    return n


def _outras_primarias(exp: dict, total: float, pop: float | None, conteudo: dict, tributos: float) -> dict:
    m = _memo(exp, "receita_primaria")
    a06 = exp.get("anexo06_referencias") or {}
    rp = a06.get("receita_primaria_total:realizadas") if a06 else (m or {}).get("valor")
    valor = (rp or 0.0) - tributos
    n = no("outras_primarias", _texto(conteudo, "nos", "outras_primarias", "nome", "Outras receitas"), valor, total, pop, cor="#2F9E62",
           definicao=_texto(conteudo, "nos", "outras_primarias", "definicao"), nota=_texto(conteudo, "nos", "outras_primarias", "nota"),
           fonte="RREO, Anexo 06: receita primária total menos tributos")
    if m and m.get("disponivel"):
        for cid in ("patrimonial", "demais_correntes", "transferencias_correntes", "capital"):
            v = _comp(m, cid)
            if v is None:
                continue
            n["filhos"].append(no(cid, _texto(conteudo, "nos", cid, "nome", cid), v, total, pop, pai_valor=n["valor"],
                                  definicao=_texto(conteudo, "nos", cid, "definicao"), fonte="RREO, Anexo 06", proximo="Fim do que o RREO abre neste anexo."))
        n["filhos"] = _ordenar(n["filhos"])
    return n


def _financeiras(exp: dict, total: float, pop: float | None, conteudo: dict) -> dict:
    """Receitas financeiras sem os títulos vendidos: rendimento do caixa, resultado do BC, empréstimos devolvidos, outras."""
    m = _memo(exp, "receitas_financeiras")
    partes = []
    for cid in ("remuneracao_caixa", "resultado_bc", "amortizacoes_emprestimos", "outras_financeiras"):
        v = _comp(m, cid)
        if v is not None:
            partes.append((cid, v))
    valor = sum(v for _, v in partes)
    n = no("financeiras", _texto(conteudo, "nos", "financeiras", "nome"), valor, total, pop, cor="#6AA889",
           definicao=_texto(conteudo, "nos", "financeiras", "definicao"), nota=_texto(conteudo, "nos", "financeiras", "nota"), fonte="RREO, Anexo 01 (receitas realizadas)")
    for cid, v in partes:
        n["filhos"].append(no(cid, _texto(conteudo, "nos", cid, "nome", cid), v, total, pop, pai_valor=n["valor"], definicao=_texto(conteudo, "nos", cid, "definicao"), fonte="RREO, Anexo 01"))
    n["filhos"] = _ordenar(n["filhos"])
    return n


# --------------------------------------------------------------------------------------------------------------------
# linhas do recibo e seus níveis
# --------------------------------------------------------------------------------------------------------------------


def _acoes_para_filhos(item: dict, total: float, pop: float | None, pai_valor: float, limite: int | None, prefixo: str) -> tuple[list[dict], str]:
    """As ações do SIOP dentro de uma subfunção. Os ids levam o prefixo da subfunção: a mesma ação ("2000 Administração
    da Unidade") existe em várias linhas e subfunções, e cada nó do desenho precisa de um id só seu."""
    bloqueio = item.get("acoes_bloqueadas") or None
    if bloqueio:
        # parte desta subfunção saiu para outra linha por elemento de despesa, e a agregação do SIOP por ação não tem
        # elemento: não dá para dizer de qual ação saiu. Passou do limite, não abre por ação, e a ficha diz por quê.
        para = " e ".join(bloqueio.get("para") or []) or "outra linha"
        fr = bloqueio.get("fracao")
        return [], (f"Esta subfunção não abre por ação: as ações do SIOP dentro dela somam {fmt_bi(bloqueio.get('total_siop'))}, mas {fmt_bi(bloqueio.get('saiu'))}"
                    + (f" ({fmt_pct(fr, 1.0)} disso)" if fr is not None else "")
                    + f" saíram daqui para a linha {para} (por elemento de despesa, que a lista de ações do SIOP não traz), e não dá para dizer de qual ação saiu o quê. "
                    f"Mostrar as ações inteiras seria mostrar dinheiro que já está em outra faixa. Débito registrado para a V2 (agregação ação por elemento).")
    acoes = item.get("acoes") or []
    if not acoes:
        return [], ""
    lista = acoes if limite is None else acoes[:limite]
    resto = acoes[len(lista):]
    filhos = [no(f"{prefixo}-acao-{a['cod']}", f"{a['cod']} {a['nome']}".strip(), a["valor"], total, None, pai_valor=pai_valor, tipo="acao") for a in lista]
    demais = item.get("acoes_demais") or None
    n_resto = len(resto) + (demais["n"] if demais else 0)
    v_resto = sum(a["valor"] for a in resto) + (demais["valor"] if demais else 0.0)
    if n_resto:
        filhos.append(no(f"{prefixo}-acoes-demais", f"Demais {n_resto} ações", v_resto, total, None, pai_valor=pai_valor, tipo="acao"))
    notas = []
    saida = item.get("acoes_saida") or None
    saiu = float(saida.get("saiu") or 0.0) if saida else 0.0
    if saida:
        # uma parte pequena desta subfunção está em outra faixa (por elemento de despesa); as ações aparecem inteiras
        para = " e ".join(saida.get("para") or []) or "outra linha"
        fr = saida.get("fracao")
        notas.append(f"{fmt_bi(saiu)} desta subfunção" + (f" ({fmt_pct(fr, 1.0)} do que as ações somam)" if fr is not None else "")
                     + f" estão na linha {para}, por elemento de despesa; a lista de ações do SIOP não traz o elemento, então as ações aparecem inteiras, "
                     f"sem esse desconto, e o desenho reparte a faixa na proporção delas. Débito registrado para a V2 (agregação ação por elemento).")
    tot_siop = item.get("acoes_total_siop")
    if tot_siop is not None and abs(tot_siop - saiu - pai_valor) > max(1.0, abs(pai_valor) * 1e-6):
        # o que sobra de diferença depois de descontar o que saiu é a dos dois sistemas (fecham por função, não por subfunção)
        notas.append(f"As ações somam {fmt_bi(tot_siop)} pelo SIOP" + (f" ({fmt_bi(tot_siop - saiu)} sem o que saiu)" if saiu else "")
                     + f"; a subfunção vale {fmt_bi(pai_valor)} pelo RREO (os dois sistemas fecham por função, não por subfunção). O desenho reparte a faixa na proporção das ações.")
    return filhos, " ".join(notas)


def _subfuncao_no(item: dict, total: float, pop: float | None, pai_valor: float, limite_acoes: int | None, proximo_sem_acoes: str, prefixo: str) -> dict:
    nid = f"{prefixo}-{item['id']}"
    n = no(nid, item["nome"], item["valor"], total, pop, pai_valor=pai_valor, cod=item.get("subfuncao_cod"), funcao=item.get("funcao"),
           fonte="RREO, Anexo 02 (despesa liquidada por subfunção)" if item.get("subfuncao_cod") else "SIOP e Anexo 06 (lei contra Tesouro)")
    ajustes = item.get("ajustes") or []
    if ajustes:
        n["ajustes"] = [{"nome": a["nome"], "valor": a["valor"], "txt": fmt_bi(a["valor"])} for a in ajustes]
        n["valor_rreo_txt"] = fmt_bi(item.get("valor_rreo"))
        n["nota"] = f"No RREO esta subfunção tem {fmt_bi(item.get('valor_rreo'))}; " + "; ".join(f"{a['nome']}: {fmt_bi(a['valor'])}" for a in ajustes) + "."
    if item.get("detalhe"):
        n["nota"] = (n["nota"] + " " if n["nota"] else "") + " ".join(str(item["detalhe"].get("nota") or "").split())
    filhos, nota_acoes = _acoes_para_filhos(item, total, pop, n["valor"], limite_acoes, nid)
    n["filhos"] = filhos
    if item.get("acoes_bloqueadas"):
        # não abre por ação: o motivo vai no "próximo nível" da ficha, uma vez só
        n["proximo"] = nota_acoes
        return n
    if nota_acoes:
        n["nota"] = (n["nota"] + " " if n["nota"] else "") + nota_acoes
    if not filhos:
        n["proximo"] = proximo_sem_acoes
    return n


def _filhos_da_linha(linha: dict, total: float, pop: float | None, conteudo: dict, limite_acoes: int | None, divida: dict | None) -> tuple[list[dict], list[dict], str, str]:
    """Devolve (filhos, nao_alocado, nota, proximo) a partir de linhas[].niveis (ou do mart da dívida, para juros)."""
    prox = conteudo.get("proximo_nivel") or {}
    niv = linha.get("niveis") or {}
    tipo = niv.get("tipo")
    nao_alocado = [{"nome": a["nome"], "valor": a["valor"], "txt": fmt_bi(a["valor"]), "para": a.get("para")} for a in (niv.get("nao_alocado") or [])]
    nota = ""
    if tipo is None and linha["id"] == "juros":
        exe = (divida or {}).get("execucao") or {}
        if exe.get("disponivel") and exe.get("por_carteira"):
            filhos = [
                no(f"{linha['id']}-carteira-{c['id']}", c["nome"], c["juros"], total, pop, pai_valor=linha["valor"], definicao=c.get("definicao", ""), fonte=exe.get("fonte", "Tesouro, execução da DPF"),
                   proximo="Fim do que o Tesouro publica por carteira. Quem são os donos dos títulos no mercado (bancos, fundos, previdência, pessoas, estrangeiros) só sai em PDF, no Relatório Mensal da Dívida.")
                for c in exe["por_carteira"] if c.get("juros")
            ]
            soma = sum(f["valor"] for f in filhos)
            if abs(soma - linha["valor"]) > max(1.0, abs(linha["valor"]) * 1e-6):
                nota = f"As carteiras somam {fmt_bi(soma)} pelo CSV de execução do Tesouro; a linha vale {fmt_bi(linha['valor'])} pelo RREO (Anexo 01). A diferença está nomeada na conferência da dívida."
            return _ordenar(filhos), nao_alocado, nota, ""
        return [], nao_alocado, "", "As partes dos juros (títulos no mercado, Banco Central, externa, cobertura do BC) vêm do produto da dívida; rode o mart da dívida deste ano para abri-las."
    lid = linha["id"]
    if tipo == "subfuncoes":
        filhos = [_subfuncao_no(it, total, pop, linha["valor"], limite_acoes, prox.get("acoes_sem_siop", ""), lid) for it in niv.get("itens", [])]
        return _ordenar(filhos), nao_alocado, "", ""
    if tipo == "funcoes":
        filhos = []
        for f in niv.get("itens", []):
            fid = f"{lid}-funcao-{f['cod']}"
            fn = no(fid, f["nome"], f["valor"], total, pop, pai_valor=linha["valor"], cod=f["cod"], fonte="RREO, Anexo 02 (despesa liquidada por função, no que sobra depois das linhas nomeadas)")
            fn["filhos"] = _ordenar([_subfuncao_no(it, total, pop, fn["valor"], limite_acoes, prox.get("acoes_sem_siop", ""), fid) for it in f.get("subfuncoes", [])])
            filhos.append(fn)
        return _ordenar(filhos), nao_alocado, "", ""
    if tipo == "elementos":
        filhos = []
        for e in niv.get("itens", []):
            eid = f"{lid}-elemento-{e['cod']}"
            en = no(eid, e["nome"], e["valor"], total, pop, pai_valor=linha["valor"], cod=e["cod"], fonte="SIOP, elemento de despesa (liquidado)")
            orgs = [no(f"{eid}-orgao-{o['cod']}", o["nome"], o["valor"], total, None, pai_valor=en["valor"], tipo="orgao") for o in e.get("orgaos", [])]
            if e.get("orgaos_demais"):
                orgs.append(no(f"{eid}-orgaos-demais", f"Demais {e['orgaos_demais']['n']} órgãos", e["orgaos_demais"]["valor"], total, None, pai_valor=en["valor"], tipo="orgao"))
            en["filhos"] = _ordenar(orgs)
            filhos.append(en)
        return _ordenar(filhos), nao_alocado, "", ""
    return [], nao_alocado, "", prox.get("subfuncoes", "")


def _linha_no(linha: dict, total: float, pop: float | None, conteudo: dict, limite_acoes: int | None, divida: dict | None, cfg_linha: dict | None) -> dict:
    lid = linha["id"]
    ct = (conteudo.get("linhas") or {}).get(lid) or {}
    n = no(lid, ct.get("curto") or linha["nome"], linha["valor"], total, pop, oficial=linha.get("centavos"), cor=ct.get("cor") or "#64748B",
           nome_completo=linha["nome"], definicao=" ".join(str(ct.get("explica") or "").split()), nao_inclui=" ".join(str(ct.get("nao_inclui") or "").split()),
           pagina=f"linha/{slug(lid)}/", var=fmt_var(linha.get("variacao_anual")), variacao=linha.get("variacao_anual"), anterior_txt=fmt_bi(linha.get("anterior")),
           fonte=_fonte_linha(linha), definicao_tecnica=" ".join(str((cfg_linha or {}).get("definicao") or linha.get("ficha", {}).get("definicao") or "").split()),
           notas=list(linha.get("notas") or []), piso=bool(linha.get("piso")), fonte_valor=linha.get("fonte_valor"))
    n["pct_pai"], n["cent_pai"] = n["pct"], n["cent"]  # dentro de "O gasto de verdade" a linha vale o mesmo que no R$ 1,00
    filhos, nao_alocado, nota, proximo = _filhos_da_linha(linha, total, pop, conteudo, limite_acoes, divida)
    n["filhos"], n["nao_alocado"], n["proximo"] = filhos, nao_alocado, proximo
    niv = linha.get("niveis") or {}
    n["niveis_fonte"] = niv.get("fonte") or ""
    n["niveis_fecham"] = bool(niv.get("fecha", True))
    if nota:
        n["nota"] = nota
    soma = sum(f["valor"] for f in filhos) + sum(a["valor"] for a in nao_alocado)
    if filhos and abs(soma - n["valor"]) > max(1.0, abs(n["valor"]) * 1e-6) and not nota:
        n["nota"] = f"As partes somam {fmt_bi(soma)}; a linha vale {fmt_bi(n['valor'])}. A diferença está na conferência niveis_fecham do recibo."
    return n


def _fonte_linha(linha: dict) -> str:
    f = linha.get("ficha") or {}
    fonte = f.get("fonte") or {}
    partes = [fonte.get("nome") or fonte.get("id") or ""]
    if f.get("anexo_regex"):
        partes.append("Anexo " + re.sub(r"[^0-9]", "", f["anexo_regex"]).lstrip("0").zfill(2))
    if linha.get("fonte_valor") == "siop":
        partes = ["SIOP (orçamento federal), elementos de despesa; conferido com o RREO"]
    return ", ".join(p for p in partes if p)


# --------------------------------------------------------------------------------------------------------------------
# visões
# --------------------------------------------------------------------------------------------------------------------


def _visao_gasto(exp: dict, pop: float | None, conteudo: dict, linhas_nos: list[dict]) -> dict:
    total = float(exp["total_recibo"])
    trib = _tributos(exp, total, pop, conteudo)
    outras = _outras_primarias(exp, total, pop, conteudo, trib["valor"])
    m_orig = _memo(exp, "origem_do_real_gasto")
    cents = None
    if m_orig and m_orig.get("disponivel"):
        # centavos oficiais do memo (largest remainder sobre tributos, outras, dívida ou caixa)
        vals = [_comp(m_orig, "tributos") or 0.0, _comp(m_orig, "outras_receitas_primarias") or 0.0, _comp(m_orig, "divida_ou_caixa") or 0.0]
        from ..marts.recibo import largest_remainder

        cents = largest_remainder([max(v, 0.0) for v in vals])
    divida_ou_caixa = total - (trib["valor"] + outras["valor"])
    dc = no("divida_ou_caixa", _texto(conteudo, "nos", "divida_ou_caixa", "nome", "Dívida nova ou caixa"), divida_ou_caixa, total, pop, cor="#C9CCC8",
            definicao=_texto(conteudo, "nos", "divida_ou_caixa", "definicao"), nota=_texto(conteudo, "nos", "divida_ou_caixa", "nota"), proximo=_texto(conteudo, "nos", "divida_ou_caixa", "proximo"),
            fonte="Calculado: total do gasto menos a receita primária do Anexo 06")
    if cents:
        trib["cent"], outras["cent"], dc["cent"] = (fmt_cent(None, None, cents[0]), fmt_cent(None, None, cents[1]), fmt_cent(None, None, cents[2]))
    v = conteudo.get("visoes", {}).get("gasto", {})
    return {
        "id": "gasto",
        "rotulo": v.get("rotulo", "O gasto de verdade"),
        "titulo": v.get("titulo", "").format(ano=exp["exercicio"]),
        "explica": " ".join(str(v.get("explica", "")).split()),
        "legenda_esq": v.get("legenda_esq", ""),
        "legenda_dir": v.get("legenda_dir", ""),
        "centro_txt": fmt_bi(total),
        "centro_nome": v.get("centro", "").format(ano=exp["exercicio"]),
        "total": total,
        "total_txt": fmt_bi(total),
        "total_exato": fmt_exato(total),
        "entradas": [trib, outras, dc] if divida_ou_caixa >= 0 else [trib, outras],
        "saidas": linhas_nos,
        "sobra_receita": None if divida_ou_caixa >= 0 else no("sobra_receita", "Receita que sobrou (não foi gasta no período)", -divida_ou_caixa, total, pop, cor="#C9CCC8",
                                                                definicao="Neste período entrou mais receita primária do que foi gasto; a sobra não faz parte do R$ 1,00."),
    }


def _por_fonte(exp: dict) -> dict | None:
    f = exp.get("fontes_de_recursos") or {}
    return f if f.get("disponivel") and f.get("grupos") else None


def _soma_fontes(f: dict, grupos: tuple, origens: tuple, destinos: tuple) -> float:
    tot = 0.0
    for g in f["grupos"]:
        if g["id"] not in grupos:
            continue
        for o in origens:
            for d in destinos:
                tot += float(((g.get("por_origem_destino") or {}).get(o) or {}).get(d) or 0.0)
    return tot


FIM_CARIMBO = "Esta faixa não abre: é o carimbo da fonte, que termina aqui (o SIOP diz com que fonte cada despesa foi paga, e só)."
FIM_CRUZAMENTO = "Esta faixa não abre: é o cruzamento título a título, que termina aqui."
DESTINOS_ORDEM = ("rolagem", "principal", "juros", "primaria", "inversoes")
DESTINO_COR = {"rolagem": "#1E3A8A", "principal": "#2563EB", "juros": "#7C2D12", "primaria": "#15201F", "inversoes": "#64748B"}
ORIGENS_ROTULO = (
    ("receita", "corrente", "Receita do ano (tributos, contribuições e demais fontes)", "#0B6B3A"),
    ("receitas_para_divida", "corrente", "Receitas do ano carimbadas para a dívida (rendimento do caixa, dividendos)", "#2F9E62"),
    ("titulos_refinanciamento", "corrente", "Títulos vendidos no ano para a rolagem", "#1E3A8A"),
    ("titulos_novos", "corrente", "Títulos novos vendidos no ano", "#2563EB"),
    ("receita", "anteriores", "Receita de anos anteriores (sobra guardada)", "#6AA889"),
    ("receitas_para_divida", "anteriores", "Receitas de anos anteriores carimbadas para a dívida", "#9FD6B8"),
    ("titulos_refinanciamento", "anteriores", "Títulos de anos anteriores vendidos para a rolagem (o colchão)", "#7C93D6"),
    ("titulos_novos", "anteriores", "Títulos novos de anos anteriores (o colchão)", "#93B4F2"),
)


def _destinos_de(f: dict, grupos: tuple, origens: tuple, pai: dict, total: float, pop: float | None, sobra_nome: str | None, sobra_def: str, reserva: dict | None = None, alvos: dict | None = None) -> list[dict]:
    """O outro lado do desenho dentro de uma entrada: para onde foi aquele dinheiro, pelo carimbo da fonte (SIOP).
    `reserva` (só para títulos): textos de conteudo/site.yaml para a faixa "reserva de liquidez", com a conta inteira:
    o que entrou, o que foi pago com título do ano, a sobra, o que o Tesouro usou de títulos de anos anteriores e o saldo."""
    nomes = f.get("destinos_nomes") or {}
    fonte = "SIOP: despesa liquidada paga com fontes " + ", ".join(sorted({x["cod"] for g in f["grupos"] if g["id"] in grupos for x in g.get("fontes", []) if x["origem"] in origens})) + " (carimbo da fonte na lei orçamentária)"
    out = []
    for d in DESTINOS_ORDEM:
        v = _soma_fontes(f, grupos, origens, (d,))
        if v <= 0:
            continue
        alvo = (alvos or {}).get(d)
        out.append(no(f"{pai['id']}-dest-{d}", nomes.get(d, d), v, total, pop, pai_valor=pai["valor"], cor=DESTINO_COR[d], fonte=fonte, tipo="destino",
                      definicao=f"Parte do dinheiro de “{pai['nome']}” que pagou: {nomes.get(d, d).lower()}.",
                      proximo=FIM_CARIMBO + " " + (f"O botão “Ir para {alvo['nome']}” leva à faixa de verdade, inteira, não só a parte paga com este dinheiro." if alvo else "A página de dados tem o detalhe por fonte."),
                      ir_para=dict(alvo) if alvo else None))
    gasto_ano = sum(x["valor"] for x in out)
    sobra = pai["valor"] - gasto_ano
    if sobra_nome and sobra > max(1.0, pai["valor"] * 1e-6):
        motivo = "É uma diferença calculada por este site entre dois relatórios (o que entrou pelo RREO menos o que foi pago com este dinheiro pelo carimbo da fonte no SIOP), não uma linha lida de um relatório."
        if reserva:
            anteriores = _soma_fontes(f, grupos, ("anteriores",), DESTINOS_ORDEM)
            numeros = {"ano": reserva.get("ano", ""), "entrou": fmt_bi(pai["valor"]), "pago": fmt_bi(gasto_ano), "sobra": fmt_bi(sobra), "anteriores": fmt_bi(anteriores), "saldo": fmt_bi(sobra - anteriores),
                       "saldo_sinal": "cresceu" if sobra - anteriores >= 0 else "diminuiu"}
            sobra_nome = str(reserva.get("nome") or sobra_nome)
            sobra_def = " ".join(str(reserva.get("definicao") or sobra_def).format(**numeros).split())
            nota = " ".join(str(reserva.get("nota") or "").format(**numeros).split())
            motivo = " ".join(str(reserva.get("tracejado_motivo") or motivo).format(**numeros).split())
            out.append(no(f"{pai['id']}-sobra", sobra_nome, sobra, total, pop, pai_valor=pai["valor"], cor="#C9CCC8", tracejado=True, tipo="sobra", tracejado_motivo=motivo, nota=nota,
                          fonte="Calculado: títulos vendidos no ano (RREO, Anexo 01) menos o que foi pago no ano com fontes de título do próprio ano (SIOP)", definicao=sobra_def,
                          proximo="O tamanho da reserva em si o Tesouro publica no Relatório Mensal da Dívida, em PDF; por isso ele não aparece aqui. A diferença entre os títulos do RREO e as emissões do Tesouro está em Perguntas abertas."))
        else:
            out.append(no(f"{pai['id']}-sobra", sobra_nome, sobra, total, pop, pai_valor=pai["valor"], cor="#C9CCC8", tracejado=True, tipo="sobra", tracejado_motivo=motivo,
                          fonte="Calculado: o que entrou menos o que foi pago no ano com fontes deste dinheiro (SIOP)", definicao=sobra_def))
    elif sobra < -max(1.0, pai["valor"] * 1e-6):
        pai["nota"] = (pai.get("nota", "") + " " if pai.get("nota") else "") + f"Pelo carimbo da fonte, foi pago no ano {fmt_bi(gasto_ano)} com este dinheiro, mais do que entrou ({fmt_bi(pai['valor'])}): a diferença veio do caixa (restos e saldos), e o desenho reparte a faixa na proporção dos destinos."
    return _ordenar(out)


def _origens_de(f: dict, destinos: tuple, pai: dict, total: float, pop: float | None, alvos: dict | None = None) -> list[dict]:
    """O outro lado do desenho dentro de uma saída: com que dinheiro ela foi paga, pelo carimbo da fonte (SIOP)."""
    out = []
    for gid, origem, nome, cor in ORIGENS_ROTULO:
        v = _soma_fontes(f, (gid,), (origem,), destinos)
        if v <= 0:
            continue
        alvo = (alvos or {}).get(gid) if origem == "corrente" else None
        if alvo:
            fim = f"O botão “Ir para {alvo['nome']}” leva à faixa de verdade, inteira, não só a parte que pagou esta despesa."
        elif origem == "anteriores":
            fim = "Este dinheiro entrou no caixa em anos anteriores, por isso não tem faixa de entrada no desenho deste ano."
        else:
            fim = "A página de dados tem o detalhe por fonte."
        out.append(no(f"{pai['id']}-orig-{gid}-{origem}", nome, v, total, pop, pai_valor=pai["valor"], cor=cor, tipo="origem",
                      fonte="SIOP: despesa liquidada por fonte de recursos (carimbo da fonte na lei orçamentária)",
                      definicao=f"Parte de “{pai['nome']}” paga com: {nome[0].lower() + nome[1:]}.",
                      proximo=FIM_CARIMBO + " " + fim, ir_para=dict(alvo) if alvo else None))
    outros = _soma_fontes(f, tuple(g["id"] for g in f["grupos"]), ("outros",), destinos)
    if outros > 0:
        out.append(no(f"{pai['id']}-orig-outros", "Outras fontes (condicionadas e afins)", outros, total, pop, pai_valor=pai["valor"], cor="#A8ADAA", tipo="origem", fonte="SIOP",
                      proximo=FIM_CARIMBO + " Fontes que não são receita do ano nem título: recursos condicionados, convênios e afins. A página de dados tem o detalhe por fonte."))
    soma = sum(x["valor"] for x in out)
    if out and abs(soma - pai["valor"]) > max(1.0, pai["valor"] * 1e-6):
        pai["nota"] = (pai.get("nota", "") + " " if pai.get("nota") else "") + f"Pelo SIOP, as fontes somam {fmt_bi(soma)} para esta faixa; o RREO dá {fmt_bi(pai['valor'])} (medidas parecidas, não iguais: o desenho reparte a faixa na proporção das fontes)."
    return _ordenar(out)


def _linhas_tabela(nos: list[dict], total: float, filhos: bool = True) -> list[dict]:
    """Linhas da tabela "linha por linha" de uma visão: cada nó e, logo abaixo, as partes de dentro, todos com os
    centavos de cada R$ 1,00 DESTA visão (uma linha do gasto dentro do caixa vale menos centavos do que na visão do gasto)."""
    out = []
    for n in nos:
        out.append({"no": n, "dentro": False, "cent": fmt_cent(n["valor"], total), "pct": fmt_pct(n["valor"], total)})
        if filhos:
            for f in n.get("filhos") or []:
                if f["valor"] > 0:
                    out.append({"no": f, "dentro": True, "cent": fmt_cent(f["valor"], total), "pct": fmt_pct(f["valor"], total)})
    return out


def _herdar_outro_lado(no_: dict, topo: dict) -> None:
    """As partes de dentro de um nó que tem carimbo próprio apontam para ele: o desenho mostra, tracejado, o outro lado
    do conjunto (o SIOP não separa tributos de outras receitas, nem um imposto do outro)."""
    for f in no_.get("filhos") or []:
        if not f.get("outro_lado"):
            f["outro_lado_de"] = topo["id"]
            f["outro_lado_nome"] = topo["nome"]
            _herdar_outro_lado(f, topo)
        else:
            _herdar_outro_lado(f, f)


def _visao_caixa(exp: dict, pop: float | None, conteudo: dict, gasto_no: dict, divida: dict | None) -> dict | None:
    r01 = exp.get("anexo01_receitas") or {}
    a01 = exp.get("anexo01_referencias") or {}
    fora = exp.get("fora_do_real") or {}
    if not r01 or "receitas_exceto_intra" not in r01 or "amortizacao" not in a01:
        return None
    ano = exp["exercicio"]
    recibo = float(exp["total_recibo"])
    op_cred = float(r01.get("operacoes_credito") or 0.0)
    refin_rec = float(r01.get("refinanciamento") or 0.0)
    receita = float(r01["receitas_exceto_intra"]) - op_cred
    titulos = op_cred + refin_rec
    amort = float(a01.get("amortizacao") or 0.0)
    refin_desp = float(a01.get("refinanciamento") or 0.0)
    emprest = float(((fora.get("emprestimos_financeiros") or {}).get("valor")) or 0.0)
    entradas_total = receita + titulos
    saidas_total = recibo + amort + refin_desp + emprest
    dif = entradas_total - saidas_total
    total = max(entradas_total, saidas_total)  # o R$ 1,00 desta visão: tudo o que passou pelo caixa
    f = _por_fonte(exp)

    def T(nid, campo, padrao=""):
        return _texto(conteudo, "nos", nid, campo, padrao)

    # ---- entradas
    n_rec = no("receita", T("receita", "nome", "Receita"), receita, total, pop, cor="#0B6B3A", definicao=T("receita", "definicao"), nota=T("receita", "nota"), fonte="RREO, Anexo 01, receitas realizadas (I) menos operações de crédito")
    trib = _tributos(exp, total, pop, conteudo)
    outras = _outras_primarias(exp, total, pop, conteudo, trib["valor"])
    fin = _financeiras(exp, total, pop, conteudo)
    for x in (trib, outras, fin):
        x["pct_pai"], x["cent_pai"] = fmt_pct(x["valor"], receita), fmt_cent(x["valor"], receita)
        for g in x["filhos"]:
            g["pct_pai"], g["cent_pai"] = fmt_pct(g["valor"], x["valor"]), fmt_cent(g["valor"], x["valor"])
    n_rec["filhos"] = [trib, outras, fin]
    soma_rec = trib["valor"] + outras["valor"] + fin["valor"]
    if abs(soma_rec - receita) > max(1.0, receita * 1e-6):
        n_rec["nao_alocado"].append({"nome": "diferença entre o Anexo 01 (receita total) e as partes dos Anexos 01 e 06", "valor": receita - soma_rec, "txt": fmt_bi(receita - soma_rec), "para": None})
    n_tit = no("titulos", T("titulos", "nome"), titulos, total, pop, cor="#012169", definicao=T("titulos", "definicao"), nota=T("titulos", "nota"), fonte="RREO, Anexo 01 (operações de crédito e refinanciamento, receitas realizadas)")
    n_rol = no("rolagem_entrada", T("rolagem_entrada", "nome"), refin_rec, total, pop, pai_valor=titulos, definicao=T("rolagem_entrada", "definicao"), nota=T("rolagem_entrada", "nota"), fonte="RREO, Anexo 01 (IV)", proximo="Fim do que o RREO abre.")
    n_nov = no("divida_nova", T("divida_nova", "nome"), op_cred, total, pop, pai_valor=titulos, definicao=T("divida_nova", "definicao"), nota=T("divida_nova", "nota"), fonte="RREO, Anexo 01", proximo="Fim do que o RREO abre.")
    n_tit["filhos"] = _ordenar([n_rol, n_nov])
    entradas = [n_rec, n_tit]
    if dif < -1.0:
        entradas.append(no("caixa_usado", T("caixa_usado", "nome"), -dif, total, pop, cor="#C9CCC8", tracejado=True, grupo="ficou", definicao=T("caixa_usado", "definicao"), fonte="Calculado: saídas menos entradas",
                           tracejado_motivo=T("caixa_usado", "tracejado_motivo", "É a diferença entre o que saiu e o que entrou, calculada por este site a partir do Anexo 01 do RREO; não é uma linha de relatório."),
                           proximo=" ".join(str(conteudo.get("visoes", {}).get("caixa", {}).get("nota_diferenca", "")).split())))

    # ---- saídas
    g = dict(gasto_no)
    g.update({"pct": fmt_pct(recibo, total), "cent": fmt_cent(recibo, total), "pct_pai": "", "cent_pai": ""})
    n_dp = no("divida_paga", T("divida_paga", "nome"), amort + refin_desp, total, pop, cor="#1E3A8A", definicao=T("divida_paga", "definicao"), nota=T("divida_paga", "nota"), fonte="RREO, Anexo 01 (amortização da dívida e amortização por refinanciamento)")
    exe = (divida or {}).get("execucao") or {}
    if exe.get("disponivel") and exe.get("por_carteira"):
        n_dp["filhos"] = _ordenar([
            no(f"divida_paga-carteira-{c['id']}", c["nome"], c["principal"], total, pop, pai_valor=n_dp["valor"], definicao=c.get("definicao", ""), fonte=exe.get("fonte", ""),
               proximo="Fim do que o Tesouro publica por carteira.") for c in exe["por_carteira"] if c.get("principal")
        ])
        soma = sum(x["valor"] for x in n_dp["filhos"])
        if abs(soma - n_dp["valor"]) > max(1.0, n_dp["valor"] * 1e-6):
            n_dp["nota"] = (n_dp["nota"] + " " if n_dp["nota"] else "") + f"As carteiras somam {fmt_bi(soma)} pelo CSV do Tesouro; o Anexo 01 dá {fmt_bi(n_dp['valor'])}. O desenho reparte a faixa na proporção das carteiras."
    else:
        n_dp["filhos"] = _ordenar([
            no("rolagem_saida", T("rolagem_saida", "nome"), refin_desp, total, pop, pai_valor=n_dp["valor"], definicao=T("rolagem_saida", "definicao"), fonte="RREO, Anexo 01 (XI)", proximo="As carteiras (mercado, Banco Central, externa) entram quando o mart da dívida do ano roda."),
            no("principal", T("principal", "nome"), amort, total, pop, pai_valor=n_dp["valor"], definicao=T("principal", "definicao"), fonte="RREO, Anexo 01 (amortização da dívida)", proximo="As carteiras entram quando o mart da dívida do ano roda."),
        ])
    n_emp = no("emprestimos_feitos", T("emprestimos_feitos", "nome"), emprest, total, pop, cor="#64748B", definicao=T("emprestimos_feitos", "definicao"), nota=T("emprestimos_feitos", "nota"), fonte="RREO (Anexos 01, 02 e 06): a conta do Tesouro")
    acoes = (fora.get("emprestimos_financeiros") or {}).get("acoes") or []
    if acoes:
        n_emp["proximo"] = "As maiores, pelo carimbo da lei orçamentária (SIOP): " + "; ".join(f"{a['acao']} {a['nome']} ({fmt_bi(a['liquidado'])})" for a in acoes[:5]) + ". A lista soma mais que a faixa porque o Tesouro conta parte dela como gasto (fica dentro do R$ 1,00, nomeada)."
    saidas = [g, n_dp]
    if emprest > 0:
        saidas.append(n_emp)
    ficou = None
    if dif > 1.0:
        ficou = no("caixa_guardado", T("caixa_guardado", "nome"), dif, total, pop, cor="#C9CCC8", tracejado=True, grupo="ficou", definicao=T("caixa_guardado", "definicao"), fonte="Calculado: entradas menos saídas",
                   tracejado_motivo=T("caixa_guardado", "tracejado_motivo", "É a diferença entre o que entrou e o que saiu, calculada por este site a partir do Anexo 01 do RREO; não é uma linha de relatório."),
                   proximo=" ".join(str(conteudo.get("visoes", {}).get("caixa", {}).get("nota_diferenca", "")).split()))
        saidas.append(ficou)
    # a conta da diferença, aberta, com os números da própria página (não é lucro)
    rp = (exp.get("anexo06_referencias") or {}).get("receita_primaria_total:realizadas")
    m_fin = _memo(exp, "receitas_financeiras")
    fin_sem_titulos = sum(float(_comp(m_fin, c) or 0.0) for c in ("remuneracao_caixa", "resultado_bc", "amortizacoes_emprestimos", "outras_financeiras")) if m_fin else None
    partes = []
    if rp is not None:
        partes.append(f"receita primária menos o gasto de verdade: {fmt_bi(float(rp) - recibo)}" + (" (o déficit)" if float(rp) < recibo else ""))
    if fin_sem_titulos is not None:
        partes.append(f"receitas financeiras sem títulos: {fmt_bi(fin_sem_titulos)}")
    partes.append(f"títulos novos: {fmt_bi(op_cred)}")
    partes.append(f"títulos de rolagem vendidos além do principal rolado: {fmt_bi(refin_rec - refin_desp)}")
    partes.append(f"principal pago com caixa: {fmt_bi(-amort)}")
    partes.append(f"empréstimos e aplicações: {fmt_bi(-emprest)}")
    conta = "A conta: " + "; ".join(partes) + f". Resultado: {fmt_bi(dif)}."
    alvo_nota = ficou if ficou is not None else (entradas[-1] if dif < -1.0 else None)
    if alvo_nota is not None:
        alvo_nota["nota"] = conta

    # ---- o outro lado dos zooms: carimbo da fonte (SIOP), nunca rateio nesta visão
    if f:
        titulos_grupos = ("titulos_refinanciamento", "titulos_novos")
        reserva = dict((conteudo.get("nos") or {}).get("reserva_liquidez") or {})
        reserva["ano"] = ano
        # para onde o botão "Ir para" de cada faixa de carimbo leva: a faixa de verdade correspondente nesta visão
        juros_no = next((x for x in g.get("filhos") or [] if x["id"] == "juros"), None)
        alvos_dest = {"primaria": {"id": g["id"], "nome": g["nome"]}, "rolagem": {"id": n_dp["id"], "nome": n_dp["nome"]}, "principal": {"id": n_dp["id"], "nome": n_dp["nome"]}}
        if juros_no:
            alvos_dest["juros"] = {"id": juros_no["id"], "nome": juros_no["nome"]}
        if emprest > 0:
            alvos_dest["inversoes"] = {"id": n_emp["id"], "nome": n_emp["nome"]}
        alvos_orig = {"receita": {"id": n_rec["id"], "nome": n_rec["nome"]}, "receitas_para_divida": {"id": n_rec["id"], "nome": n_rec["nome"]},
                      "titulos_refinanciamento": {"id": n_tit["id"], "nome": n_tit["nome"]}, "titulos_novos": {"id": n_tit["id"], "nome": n_tit["nome"]}}
        n_tit["outro_lado"] = _destinos_de(f, titulos_grupos, ("corrente",), n_tit, total, pop, T("reserva_liquidez", "nome", "Reserva de liquidez (não usado no ano)"), "", reserva=reserva, alvos=alvos_dest)
        n_rol["outro_lado"] = _destinos_de(f, ("titulos_refinanciamento",), ("corrente",), n_rol, total, pop, T("reserva_liquidez", "nome", "Reserva de liquidez (não usado no ano)"), "", reserva=reserva, alvos=alvos_dest)
        n_nov["outro_lado"] = _destinos_de(f, ("titulos_novos",), ("corrente",), n_nov, total, pop, T("reserva_liquidez", "nome", "Reserva de liquidez (não usado no ano)"), "", reserva=reserva, alvos=alvos_dest)
        n_rec["outro_lado"] = _destinos_de(f, ("receita", "receitas_para_divida"), ("corrente",), n_rec, total, pop, T("receita_sobra", "nome", "Ficou no caixa ou pagou contas de anos anteriores"),
                                           T("receita_sobra", "definicao", "Receita do ano que não pagou despesa do ano: pagou restos a pagar de anos anteriores ou ficou no caixa."), alvos=alvos_dest)
        g["outro_lado"] = _origens_de(f, ("juros", "primaria"), g, total, pop, alvos=alvos_orig)
        n_dp["outro_lado"] = _origens_de(f, ("rolagem", "principal"), n_dp, total, pop, alvos=alvos_orig)
        if emprest > 0:
            n_emp["outro_lado"] = _origens_de(f, ("inversoes",), n_emp, total, pop, alvos=alvos_orig)
        anteriores = _soma_fontes(f, titulos_grupos, ("anteriores",), DESTINOS_ORDEM)
        if anteriores > 0:
            n_tit["nota"] = (n_tit.get("nota", "") + " " if n_tit.get("nota") else "") + f"Além dos títulos vendidos em {ano}, o Tesouro gastou em {ano} {fmt_bi(anteriores)} de títulos vendidos em anos anteriores (o colchão); isso aparece dentro de “Dívida antiga paga” e de “O gasto de verdade”."
        # o carimbo da fonte não separa as partes de dentro (tributos, outras receitas, cada imposto): dentro delas, o
        # desenho mostra o outro lado do conjunto de onde vieram, tracejado e dito como tal, em vez de ficar em branco
        for topo in (n_rec, n_tit, g, n_dp, n_emp):
            if topo.get("outro_lado"):
                _herdar_outro_lado(topo, topo)
    for lado in (entradas, saidas):
        for x in lado:
            x["pct"], x["cent"] = fmt_pct(x["valor"], total), fmt_cent(x["valor"], total)
    # ---- as três frases e a tabela "linha por linha" desta visão (a visão inteira, com a rolagem)
    frases_caixa = [{"numero": fmt_bi(total), "texto": f"passaram pelo caixa da União em {ano}: de cada real que entrou, {fmt_cent(receita, entradas_total)} vieram de receita (tributos, contribuições e demais fontes) e {fmt_cent(titulos, entradas_total)} de títulos vendidos (rolagem e dívida nova)."}]
    if f and titulos:
        pago_div_f = _soma_fontes(f, ("titulos_refinanciamento", "titulos_novos"), ("corrente",), ("rolagem", "principal"))
        frases_caixa.append({"numero": fmt_cent(pago_div_f, titulos), "texto": f"de cada R$ 1,00 de título vendido em {ano} pagaram principal de dívida antiga, pelo carimbo da fonte no orçamento (SIOP). Vender título para pagar título é a rolagem: dinheiro que entra e sai no mesmo ano, e por isso fica fora do gasto de verdade."})
    elif titulos:
        frases_caixa.append({"numero": fmt_cent(refin_desp + amort, titulos), "texto": f"de cada R$ 1,00 de título vendido em {ano} foram pagar dívida antiga (rolagem e amortização do Anexo 01). Vender título para pagar título fica fora do gasto de verdade."})
    if dif > 1.0 and ficou is not None:
        frases_caixa.append({"numero": fmt_bi(dif), "texto": f"ficaram no caixa (e em contas de anos anteriores): {fmt_cent(dif, total)} de cada real que passou pelo caixa. Não é lucro: é o que o Tesouro levantou além do que o ano consumiu, principalmente título vendido adiantado para vencimentos futuros."})
    elif dif < -1.0:
        frases_caixa.append({"numero": fmt_bi(-dif), "texto": f"vieram do caixa de anos anteriores: o ano consumiu mais do que entrou e o Tesouro usou o colchão."})
    tabela_caixa = [
        {"id": "entrou", "grupo": "O que entrou", "nota": "receita realizada e títulos vendidos (Anexo 01 do RREO)", "linhas": _linhas_tabela([x for x in entradas if x.get("grupo") != "ficou"], total)},
        {"id": "saiu", "grupo": "O que saiu", "nota": "despesa liquidada: gasto de verdade, dívida antiga paga, empréstimos feitos", "linhas": _linhas_tabela([x for x in saidas if x.get("grupo") != "ficou"], total)},
    ]
    if ficou is not None or dif < -1.0:
        tabela_caixa.append({"id": "ficou", "grupo": "O que ficou", "nota": "a diferença entre entradas e saídas, tracejada no desenho", "linhas": _linhas_tabela([ficou] if ficou is not None else [entradas[-1]], total, filhos=False)})
    v = conteudo.get("visoes", {}).get("caixa", {})
    rolagem_share = (refin_desp + amort) / titulos if titulos else None
    frase = ""
    if f:
        pago_div = _soma_fontes(f, ("titulos_refinanciamento", "titulos_novos"), ("corrente",), ("rolagem", "principal"))
        pago_jur = _soma_fontes(f, ("titulos_refinanciamento", "titulos_novos"), ("corrente",), ("juros",))
        if titulos:
            frase = f"Pelo carimbo da fonte: de cada R$ 1,00 de título vendido em {ano}, {fmt_cent(pago_div, titulos)} pagaram principal de dívida antiga e {fmt_cent(pago_jur, titulos)} pagaram juros no próprio ano; o resto cobriu outras despesas ou ficou no caixa para os anos seguintes."
    elif rolagem_share:
        frase = f"De cada R$ 1,00 de título vendido em {ano}, {fmt_cent(refin_desp + amort, titulos)} foram pagar dívida antiga; o resto cobriu o que a receita não cobriu."
    return {
        "id": "caixa",
        "frases": frases_caixa,
        "tabela": tabela_caixa,
        "rotulo": v.get("rotulo", "Tudo o que passou pelo caixa"),
        "titulo": v.get("titulo", "").format(ano=ano),
        "explica": " ".join(str(v.get("explica", "")).split()),
        "legenda_esq": v.get("legenda_esq", ""),
        "legenda_dir": v.get("legenda_dir", ""),
        "centro_txt": fmt_bi(total),
        "centro_nome": v.get("centro", "").format(ano=ano),
        "total": total,
        "total_txt": fmt_bi(total),
        "total_exato": fmt_exato(total),
        "entradas": entradas,
        "saidas": saidas,
        "ficou": ficou if ficou is not None else (entradas[-1] if dif < -1.0 else None),
        "diferenca": dif,
        "diferenca_txt": fmt_bi(dif),
        "nota_diferenca": " ".join(str(v.get("nota_diferenca", "")).split()),
        "conta_diferenca": conta,
        "frase_rolagem": frase,
        "por_fonte": bool(f),
        "sem_fonte_motivo": None if f else " ".join(str((exp.get("fontes_de_recursos") or {}).get("motivo") or "").split()),
        "titulo_esq": "De onde veio",
        "titulo_dir": "Para onde foi",
        "outro": {
            "modo": "carimbo",
            "rotulo": "carimbo da fonte",
            "termina": "o carimbo da fonte",
            "esq": "com que dinheiro foi pago, pelo carimbo da fonte no orçamento (SIOP)",
            "dir": "o que esse dinheiro pagou no ano, pelo carimbo da fonte no orçamento (SIOP)",
            "estado": "O outro lado não é rateio: é o carimbo da fonte na lei orçamentária (SIOP), que diz com que dinheiro cada despesa foi paga.",
            "ficha_esq": "O que esse dinheiro pagou no ano",
            "ficha_dir": "Com que dinheiro foi pago",
            "vazio": "sem detalhe por fonte neste período: " + (" ".join(str((exp.get("fontes_de_recursos") or {}).get("motivo") or "").split()) if not f else ""),
            "vazio_nome": "Sem carimbo da fonte até o ano fechar",
            "vazio_sub": "o SIOP só diz com que dinheiro cada despesa foi paga quando o ano fecha; aqui não entra aproximação",
            "vazio_estado": "O outro lado é só um aviso, sem números, porque o carimbo da fonte (SIOP) só é comparável no ano fechado; nada de aproximação. No ano fechado, esse lado mostra com que dinheiro cada faixa foi paga.",
            "herdado_esq": "com que dinheiro foi pago o conjunto {nome}, pelo carimbo da fonte (SIOP): o carimbo não separa esta parte, por isso o lado está tracejado",
            "herdado_dir": "o que o conjunto {nome} pagou no ano, pelo carimbo da fonte (SIOP): o carimbo não separa esta parte, por isso o lado está tracejado",
            "herdado_estado": "O outro lado está tracejado porque o carimbo da fonte não separa esta parte: o SIOP diz com que fonte cada despesa foi paga, e os tributos, por exemplo, entram juntos na fonte “recursos livres”. O que se vê é o outro lado do conjunto {nome}, com as porcentagens dele.",
            "herdado_ficha": "O outro lado do conjunto {nome} (o carimbo da fonte não separa esta parte)",
        },
        "numeros": {"receita": fmt_bi(receita), "titulos": fmt_bi(titulos), "rolagem": fmt_bi(refin_rec), "divida_nova": fmt_bi(op_cred), "gasto": fmt_bi(recibo), "divida_paga": fmt_bi(amort + refin_desp), "emprestimos": fmt_bi(emprest)},
    }


# --------------------------------------------------------------------------------------------------------------------
# visão "A dívida" (v0.10.0): o que a União devia, do que é feito (índice) e quando vence, título a título
# --------------------------------------------------------------------------------------------------------------------

IDX_COR = {"selic": "#B45309", "prefixado": "#1E3A8A", "precos": "#7C2D12", "cambio": "#0E7490", "demais": "#64748B"}
FAIXA_COR = {"ate_12m": "#15201F", "de_1_a_2_anos": "#1E3A8A", "de_2_a_5_anos": "#2563EB", "mais_de_5_anos": "#7C93D6", "sem_data": "#A8ADAA"}
MESES_NOME = ("janeiro", "fevereiro", "março", "abril", "maio", "junho", "julho", "agosto", "setembro", "outubro", "novembro", "dezembro")


def _data_referencia(ano: int, mes: int) -> tuple[str, str]:
    """("31 de dezembro de 2025", "dezembro de 2025")."""
    import calendar

    dia = calendar.monthrange(ano, mes)[1]
    return f"{dia} de {MESES_NOME[mes - 1]} de {ano}", f"{MESES_NOME[mes - 1]} de {ano}"


def _medida(divida: dict | None, grupo: str, mid: str) -> dict | None:
    for g in (divida or {}).get("grupos", []):
        if g["id"] == grupo:
            return next((m for m in g.get("medidas", []) if m["id"] == mid), None)
    return None


def motivo_leigo(motivo: str | None) -> str:
    """O motivo técnico do mart (com nome de staging e comando de terminal) vira uma frase para a página."""
    t = " ".join(str(motivo or "").split())
    if not t:
        return "sem fonte aberta para este número."
    if "COFOG" in t:
        return "o Tesouro publica a base COFOG com cerca de um ano de atraso; a deste ano ainda não saiu."
    if "sem fonte aberta" in t or "Relatório Mensal da Dívida" in t:
        return "sem fonte aberta: o Tesouro publica esse número só no Relatório Mensal da Dívida, em PDF."
    if "IBGE" in t or "população" in t:
        return "a população do IBGE não estava disponível nesta rodada."
    if "staging" in t or "rode `" in t:
        return "a fonte não foi baixada nesta rodada do pipeline."
    if "não tem" in t or "não cobre" in t or "não está" in t or "ausente" in t:
        return "a fonte não cobre este período."
    return t[0].lower() + t[1:] if t else t


def fmt_medida(m: dict | None) -> str:
    """O valor de uma medida da dívida na unidade dela; "indisponível" quando não existe (o motivo vai à parte)."""
    if not m:
        return "indisponível"
    v = m.get("valor")
    if v is None:
        return "indisponível"
    u = m.get("unidade")
    if u == "reais":
        return fmt_bi(v)
    if u == "% do PIB":
        return _br(v, 1) + "% do PIB"
    if u == "%":
        return _br(v, 1) + "%"
    if u == "meses":
        return _br(v, 0) + " meses"
    if u == "pessoas":
        return fmt_int(v) + " pessoas"
    if u == "reais por habitante":
        return "R$ " + (_br(v, 0) if abs(v) >= 100 else _br(v, 2)) + " por habitante"
    return _br(v, 2)


def _normalizar_familias(celulas: list[dict]) -> None:
    """Exports gerados antes da v1.0.0 podem ter a mesma família grafada de dois jeitos ("NTN-I" e "ntn-i"); o site
    junta as duas, como o mart passou a fazer."""
    for c in celulas:
        fam = str(c.get("familia") or "")
        if fam:
            c["familia"] = normalizar_familia(fam)


def _visao_divida(divida: dict | None, conteudo: dict, pop_recibo: float | None) -> dict | None:
    det = (divida or {}).get("estoque_detalhe") or {}
    if not det.get("disponivel") or not det.get("celulas"):
        return None
    cfg = conteudo.get("visoes", {}).get("divida", {})
    ano, mes, fechado = int(divida["exercicio"]), int(det["mes"]), bool(det.get("fechado"))
    data_longa, data_curta = _data_referencia(ano, mes)
    total = float(det["total"])
    celulas = det["celulas"]
    _normalizar_familias(celulas)
    fonte = det.get("fonte", "")
    m_pop = _medida(divida, "por_habitante", "populacao")
    pop = float(m_pop["valor"]) if m_pop and m_pop.get("valor") else pop_recibo
    idx_nomes = {i["id"]: i["nome"] for i in det.get("indexadores", [])}
    fam_nomes = cfg.get("familias") or {}
    idx_txt = cfg.get("indexadores") or {}
    faixa_txt = cfg.get("faixas") or {}
    faixas = [f for f in det.get("faixas", []) if f["valor"] > 0]
    fim_csv = "Fim do que o CSV do Tesouro abre neste desenho."

    def soma(pred) -> float:
        return sum(c["valor"] for c in celulas if pred(c))

    def por_faixa(pred, pai: dict) -> list[dict]:
        out = []
        for fx in faixas:
            v = soma(lambda c: pred(c) and c["faixa"] == fx["id"])
            if v > 0:
                out.append(no(f"{pai['id']}-venc-{fx['id']}", fx["nome"], v, total, pop, pai_valor=pai["valor"], cor=FAIXA_COR.get(fx["id"], "#A8ADAA"), tipo="prazo", fonte=fonte,
                              definicao=f"Parte de “{pai['nome']}” que {fx['nome'][0].lower() + fx['nome'][1:]}.",
                              proximo=FIM_CRUZAMENTO + f" O botão “Ir para {fx['nome']}” leva à faixa inteira, com todos os índices, aberta por mês ou por ano.",
                              ir_para={"id": f"venc-{fx['id']}", "nome": fx["nome"]}))
        return out

    def por_indice(pred, pai: dict) -> list[dict]:
        out = []
        for iid, inome in idx_nomes.items():
            v = soma(lambda c: pred(c) and c["indexador"] == iid)
            if v > 0:
                out.append(no(f"{pai['id']}-idx-{iid}", inome, v, total, pop, pai_valor=pai["valor"], cor=IDX_COR.get(iid, "#64748B"), tipo="indice", fonte=fonte,
                              definicao=f"Parte de “{pai['nome']}” corrigida por: {inome[0].lower() + inome[1:]}.",
                              proximo=FIM_CRUZAMENTO + f" O botão “Ir para {inome}” leva à faixa inteira, com todos os prazos, aberta por família de título.",
                              ir_para={"id": f"idx-{iid}", "nome": inome}))
        return _ordenar(out)

    # ---- esquerda: do que a dívida é feita (índice), dentro de cada índice as famílias de título
    entradas = []
    for iid, inome in idx_nomes.items():
        v = soma(lambda c: c["indexador"] == iid)
        if v <= 0:
            continue
        n = no(f"idx-{iid}", inome, v, total, pop, cor=IDX_COR.get(iid, "#64748B"), tipo="indice", fonte=fonte, definicao=" ".join(str((idx_txt.get(iid) or {}).get("explica") or "").split()))
        fams: dict[str, float] = {}
        for c in celulas:
            if c["indexador"] == iid:
                fams[c["familia"]] = fams.get(c["familia"], 0.0) + c["valor"]   # a família já passou por _normalizar_familias
        filhos = []
        for fam, fv in fams.items():
            f = no(f"idx-{iid}-fam-{slug(fam)}", fam_nomes.get(fam, fam), fv, total, pop, pai_valor=v, tipo="familia", fonte=fonte, proximo=fim_csv + " A família reúne os títulos de um mesmo tipo, com vencimentos diferentes.")
            f["outro_lado"] = por_faixa(lambda c, _i=iid, _f=fam: c["indexador"] == _i and c["familia"] == _f, f)
            filhos.append(f)
        n["filhos"] = _ordenar(filhos)
        n["outro_lado"] = por_faixa(lambda c, _i=iid: c["indexador"] == _i, n)
        entradas.append(n)
    entradas = _ordenar(entradas)

    # ---- direita: quando vence (faixas na ordem do calendário), dentro de cada faixa os meses ou anos
    saidas = []
    for fx in faixas:
        n = no(f"venc-{fx['id']}", fx["nome"], fx["valor"], total, pop, cor=FAIXA_COR.get(fx["id"], "#A8ADAA"), tipo="prazo", fonte=fonte, definicao=" ".join(str(faixa_txt.get(fx["id"]) or "").split()))
        pers: dict[str, list] = {}
        for c in celulas:
            if c["faixa"] == fx["id"]:
                p = pers.setdefault(c["periodo"], [c["periodo_nome"], 0.0])
                p[1] += c["valor"]
        filhos = []
        for pid in sorted(pers, key=lambda k: ("0" if k == "vencido" else "1" + k)):
            pnome, pv = pers[pid]
            f = no(f"venc-{fx['id']}-{slug(pid)}", pnome, pv, total, pop, pai_valor=fx["valor"], tipo="periodo", fonte=fonte, proximo=fim_csv + (" O mês é o do vencimento final de cada título." if fx["id"] == "ate_12m" else " O ano é o do vencimento final de cada título."))
            f["outro_lado"] = por_indice(lambda c, _fx=fx["id"], _p=pid: c["faixa"] == _fx and c["periodo"] == _p, f)
            filhos.append(f)
        n["filhos"] = filhos
        n["outro_lado"] = por_indice(lambda c, _fx=fx["id"]: c["faixa"] == _fx, n)
        saidas.append(n)

    # ---- a conta do ano (fatores do Tesouro) e os números por habitante
    conta_src = (divida or {}).get("conta_do_ano") or {}
    tira = cfg.get("tira") or {}
    conta = None
    if conta_src.get("disponivel"):
        var = conta_src.get("variacao")
        conta = {
            "itens": [
                {"id": "comecou", "nome": str(tira.get("comecou", "Começou {ano} devendo")).format(ano=ano), "txt": fmt_bi(conta_src.get("estoque_anterior")), "exato": fmt_exato(conta_src.get("estoque_anterior"))},
                {"id": "vendeu", "nome": tira.get("vendeu", "Vendeu títulos (emissões)"), "txt": "+ " + fmt_bi(conta_src.get("emissoes")), "exato": fmt_exato(conta_src.get("emissoes"))},
                {"id": "pagou", "nome": tira.get("pagou", "Pagou (principal e juros, em dinheiro)"), "txt": "- " + fmt_bi(abs(conta_src.get("resgates") or 0.0)), "exato": fmt_exato(abs(conta_src.get("resgates") or 0.0))},
                {"id": "juros", "nome": tira.get("juros", "Juros que a dívida gerou no ano, somados ao saldo (apropriados)"), "txt": "+ " + fmt_bi(conta_src.get("juros_apropriados")), "exato": fmt_exato(conta_src.get("juros_apropriados"))},
                {"id": "bc", "nome": tira.get("bc", "Operações do mercado com o Banco Central"), "txt": ("+ " if (conta_src.get("operacoes_bc") or 0.0) >= 0 else "- ") + fmt_bi(abs(conta_src.get("operacoes_bc") or 0.0)), "exato": fmt_exato(conta_src.get("operacoes_bc"))},
                {"id": "terminou", "nome": str(tira.get("terminou", "Terminou {data_curta} devendo")).format(data_curta=data_curta, ano=ano), "txt": fmt_bi(conta_src.get("estoque_fim")), "exato": fmt_exato(conta_src.get("estoque_fim"))},
            ],
            "variacao": var,
            "variacao_txt": fmt_bi(abs(var)) if var is not None else "",
            "variacao_rotulo": (tira.get("cresceu", "A dívida cresceu") if (var or 0.0) >= 0 else tira.get("caiu", "A dívida caiu")),
            "variacao_hab": fmt_hab(abs(var), pop) if var is not None and pop else "",
            "fecha": bool(conta_src.get("fecha")),
            "meses": conta_src.get("meses") or [],
            "fonte": conta_src.get("fonte", ""),
            "nota": " ".join(str(tira.get("nota_conta") or conta_src.get("nota") or "").split()),
        }
    juros_hab = _medida(divida, "por_habitante", "juros_pagos_por_habitante")
    por_hab = {
        "disponivel": bool(pop),
        "populacao_txt": fmt_int(pop) if pop else "",
        "divida": fmt_hab(total, pop) if pop else "",
        "divida_motivo": "" if pop else "a população do IBGE não estava disponível nesta rodada do mart da dívida",
        "juros_pagos": fmt_medida(juros_hab) if juros_hab and juros_hab.get("valor") is not None else "",
        "fonte": (m_pop or {}).get("fonte", "") if pop else "",
    }

    # ---- as réguas de juros por pergunta, outras medidas do estoque e o risco de rolagem
    perg = cfg.get("perguntas_juros") or {}

    def item_medida(m: dict, nome: str, explica: str, **extra) -> dict:
        disp = m.get("valor") is not None
        return {"id": m["id"], "nome": nome, "explica": " ".join(str(explica or "").split()), "valor_txt": fmt_medida(m), "disponivel": disp,
                "motivo": "" if disp else motivo_leigo(m.get("motivo")), "fonte": m.get("fonte", ""),
                "hab": fmt_hab(m["valor"], pop) if disp and pop and m.get("unidade") == "reais" else "", **extra}

    reguas = []
    for g in (divida or {}).get("grupos", []):
        if g["id"] != "juros":
            continue
        for m in g.get("medidas", []):
            p = perg.get(m["id"]) or {}
            reguas.append(item_medida(m, p.get("pergunta") or m["nome"], p.get("explica") or m.get("definicao"), pergunta=p.get("pergunta") or m["nome"]))
    med_cfg = cfg.get("medidas_estoque") or {}
    outras = []
    for mid, t in med_cfg.items():
        m = _medida(divida, "estoque", mid)
        if m:
            outras.append(item_medida(m, t.get("nome") or m["nome"], t.get("explica") or m.get("definicao")))
    rol_cfg = cfg.get("medidas_rolagem") or {}
    rolagem = []
    for mid, t in rol_cfg.items():
        m = _medida(divida, "rolagem", mid)
        if m:
            rolagem.append(item_medida(m, t.get("nome") or m["nome"], m.get("definicao")))
    prazo = next((r for r in rolagem if r["id"] == "prazo_medio_bcb" and r["disponivel"]), None)
    recs = (divida or {}).get("reconciliacoes") or []
    conf = {"n": len(recs), "ok": sum(1 for r in recs if r.get("ok")), "criticas": sum(1 for r in recs if r.get("critica")), "criticas_ok": sum(1 for r in recs if r.get("critica") and r.get("ok")),
            "lista": [{"id": r["id"], "ok": bool(r.get("ok")), "critica": bool(r.get("critica")), "calculada": bool(r.get("calculada", True)), "descricao": " ".join(str(r.get("descricao") or "").split()), "detalhe": " ".join(str(r.get("detalhe") or "").split())} for r in recs]}
    fontes_div = []
    for k, rot in (("estoque_csv", "Estoque da DPF título a título (CSV)"), ("estoque_xlsx", "Estoque da DPF (planilha, Anexo 2.1)"), ("fatores_xlsx", "Fatores de variação da DPF (planilha, Anexo 2.9)"), ("execucao_csv", "Execução orçamentária e financeira da DPF (CSV)"), ("tesouro_direto_csv", "Estoque do Tesouro Direto (CSV)")):
        f = ((divida or {}).get("fontes") or {}).get(k)
        if f:
            fontes_div.append({"id": k, "nome": rot, "arquivo": f.get("arquivo"), "snapshot": f.get("snapshot_id"), "recurso_data": (f.get("recurso_data") or "")[:10], "sha256": f.get("sha256_canonical")})
    bcb = ((divida or {}).get("fontes") or {}).get("bcb")
    if bcb:
        fontes_div.append({"id": "bcb", "nome": "Banco Central, séries do SGS", "arquivo": f"{bcb.get('series')} séries, {bcb.get('pontos')} pontos", "snapshot": "", "recurso_data": (bcb.get("lido_em") or "")[:10], "sha256": ""})
    ate12 = next((s for s in saidas if s["id"] == "venc-ate_12m"), None)
    # ---- as três frases e a tabela "linha por linha" desta visão
    frases = []
    if pop:
        frases.append({"numero": fmt_hab(total, pop).replace(" por habitante", ""), "texto": f"por habitante é a dívida da União fora do Banco Central em {data_longa} ({fmt_bi(total)}) dividida pelos {fmt_int(pop)} habitantes. É a parte média de cada um de nós: dívida se paga com tributo, hoje ou depois."})
    else:
        frases.append({"numero": fmt_bi(total), "texto": f"é a dívida da União fora do Banco Central em {data_longa}: títulos vendidos a bancos, fundos, previdência privada, pessoas e estrangeiros, mais os contratos de empréstimo."})
    if conta and conta["variacao"] is not None:
        var = conta["variacao"]
        quando = f"em {ano}" if fechado else f"de janeiro a {data_curta}"
        frases.append({"numero": fmt_bi(abs(var)), "texto": f"foi quanto a dívida {'cresceu' if var >= 0 else 'caiu'} {quando}" + (f" ({fmt_hab(abs(var), pop)})" if pop else "") + ". Entrou mais título vendido e mais juros apropriados do que saiu em pagamento. A conta inteira está na tira acima e fecha ao centavo com o Tesouro."})
    if ate12:
        frases.append({"numero": ate12["cent"], "texto": f"de cada real devido vencem em até 12 meses ({ate12['txt']}): principal que o Tesouro vai pagar ou rolar no próximo ano" + (f". Prazo médio da dívida em títulos, pelo Banco Central: {prazo['valor_txt']}" if prazo else "") + "."})
    tabela = [
        {"id": "indices", "grupo": "Do que é feita", "nota": "o índice que corrige cada título; dentro, os tipos de título", "linhas": _linhas_tabela(entradas, total)},
        {"id": "prazos", "grupo": "Quando vence", "nota": "pelo vencimento final de cada título ou contrato; o mês a mês e o ano a ano estão no desenho", "linhas": _linhas_tabela(saidas, total, filhos=False)},
    ]
    # ---- os três números grandes no alto da aba
    fim_12m = f"{MESES_NOME[mes - 1]} de {ano + 1}"
    destaques = []
    if pop:
        destaques.append({"rotulo": "Cada habitante deve", "numero": fmt_hab(total, pop).replace(" por habitante", ""), "texto": f"a dívida da União fora do Banco Central em {data_longa} ({fmt_bi(total)}) dividida pelos {fmt_int(pop)} habitantes do IBGE. É a parte média de cada um de nós."})
    else:
        destaques.append({"rotulo": "Quanto devemos", "numero": fmt_bi(total), "texto": f"a dívida da União fora do Banco Central em {data_longa}; por habitante indisponível ({por_hab['divida_motivo']})."})
    if conta and conta["variacao"] is not None:
        var = conta["variacao"]
        destaques.append({"rotulo": f"A dívida {'cresceu' if var >= 0 else 'caiu'} {'em ' + str(ano) if fechado else 'até ' + data_curta}", "numero": fmt_bi(abs(var)),
                          "texto": (f"{fmt_hab(abs(var), pop)}. " if pop else "") + "Entrou mais título vendido e mais juros apropriados do que saiu em pagamento; a conta inteira está logo abaixo e fecha ao centavo com o Tesouro."})
    if ate12:
        destaques.append({"rotulo": f"Vence até {fim_12m}", "numero": ate12["txt"], "texto": f"{ate12['cent']} de cada real devido: o principal que o Tesouro vai pagar ou rolar nos próximos 12 meses, mês a mês no desenho e no calendário abaixo" + (f". Prazo médio da dívida em títulos, pelo Banco Central: {prazo['valor_txt']}" if prazo else "") + "."})
    return {
        "id": "divida",
        "frases": frases,
        "tabela": tabela,
        "destaques": destaques,
        "rotulo": cfg.get("rotulo", "A dívida"),
        "titulo": str(cfg.get("titulo", "")).format(data=data_longa, ano=ano),
        "explica": " ".join(str(cfg.get("explica", "")).format(data=data_longa, ano=ano).split()),
        "legenda_esq": cfg.get("legenda_esq", ""),
        "legenda_dir": cfg.get("legenda_dir", ""),
        "titulo_esq": cfg.get("titulo_esq", "Do que é feita"),
        "titulo_dir": cfg.get("titulo_dir", "Quando vence"),
        "hab_nota": cfg.get("hab_nota", ""),
        "outro": cfg.get("outro") or {"modo": "cruzamento"},
        "centro_txt": fmt_bi(total),
        "centro_nome": str(cfg.get("centro", "")).format(data=data_longa, ano=ano),
        "total": total,
        "total_txt": fmt_bi(total),
        "total_exato": fmt_exato(total),
        "entradas": entradas,
        "saidas": saidas,
        "por_fonte": True,
        "ano": ano, "mes": mes, "fechado": fechado, "data": data_longa, "data_curta": data_curta,
        "fonte": fonte,
        "nota_detalhe": " ".join(str(det.get("nota") or "").split()),
        "conta": conta,
        "conta_motivo": "" if conta else " ".join(str(conta_src.get("motivo") or "planilha de fatores indisponível").split()),
        "por_habitante": por_hab,
        "reguas_juros": reguas,
        "medidas_estoque": outras,
        "rolagem": rolagem,
        "vencimentos_12m": [{"nome": f["nome"], "txt": f["txt"], "pct_pai": f["pct_pai"], "cent_pai": f["cent_pai"]} for f in (ate12["filhos"] if ate12 else [])],
        "notas": {k: " ".join(str(tira.get(k) or "").split()) for k in ("nota_publico", "nota_vencimento")},
        "conferencias": conf,
        "fontes": fontes_div,
        "avisos": [" ".join(str(a).split()) for a in ((divida or {}).get("avisos") or [])],
        "selo": (divida or {}).get("selo", ""),
        "celulas": len(celulas),
    }


# --------------------------------------------------------------------------------------------------------------------
# modelo do ano
# --------------------------------------------------------------------------------------------------------------------


def _conferencias(exp: dict, linhas_cfg: dict | None) -> dict:
    cfg = {r["id"]: r for r in (linhas_cfg or {}).get("reconciliacoes", [])}
    lista = []
    for r in exp.get("reconciliacoes", []):
        lista.append({
            "id": r["id"], "ok": bool(r.get("ok")), "critica": bool(r.get("critica")), "explicada": bool(r.get("explicada")),
            "descricao": " ".join(str(cfg.get(r["id"], {}).get("descricao") or r.get("descricao") or "").split()),
            "esperado": fmt_exato(r.get("esperado")) if isinstance(r.get("esperado"), (int, float)) else str(r.get("esperado")),
            "obtido": fmt_exato(r.get("obtido")) if isinstance(r.get("obtido"), (int, float)) else str(r.get("obtido")),
            "diff": fmt_exato(r.get("diff")) if isinstance(r.get("diff"), (int, float)) else "",
            "detalhe": " ".join(str(r.get("detalhe") or "").split()),
        })
    n = len(lista)
    falhas = [r for r in lista if not r["ok"]]
    criticas_falhas = [r for r in falhas if r["critica"]]
    return {"lista": lista, "n": n, "ok": n - len(falhas), "falhas": len(falhas), "criticas_falhas": len(criticas_falhas), "todas_ok": not falhas}


def _lacres(exp: dict) -> list[dict]:
    snaps = []
    vistos = set()
    for l in exp.get("linhas", []):
        for s in (l.get("ficha") or {}).get("snapshots", []):
            chave = (s.get("raw_key"), s.get("snapshot_id"))
            if chave in vistos:
                continue
            vistos.add(chave)
            snaps.append({"arquivo": s.get("raw_key"), "snapshot_id": s.get("snapshot_id"), "sha256": s.get("sha256_canonical"), "baixado_em": (s.get("fetched_at") or "")[:19].replace("T", " ")})
    pop = exp.get("populacao") or {}
    if pop.get("disponivel") and pop.get("fonte"):
        f = pop["fonte"]
        snaps.append({"arquivo": f"IBGE: {f.get('arquivo')}", "snapshot_id": f.get("snapshot_id"), "sha256": f.get("sha256_canonical"), "baixado_em": (f.get("fetched_at") or "")[:19].replace("T", " ")})
    return snaps


def _tres_frases(exp: dict, pop: float | None, gasto: dict, linhas_nos: list[dict], divida: dict | None) -> list[dict]:
    total = float(exp["total_recibo"])
    ent = {e["id"]: e for e in gasto["entradas"]}
    trib = ent.get("tributos")
    frases = []
    if trib:
        outras = ent.get("outras_primarias")
        dc = ent.get("divida_ou_caixa")
        m = _memo(exp, "gasto_por_real_de_tributo")
        por_real = fmt_exato(m["valor"]) if m and m.get("disponivel") and m.get("valor") is not None else ""
        texto = f"de cada real gasto vieram de tributos."
        if outras and dc:
            texto += f" Outros {outras['cent'].replace(' centavos', '').replace(' centavo', '')} vieram de royalties, dividendos e serviços, e {dc['cent'].replace(' centavos', '').replace(' centavo', '')} de dívida nova ou do caixa."
        if por_real:
            texto += f" Para cada R$ 1,00 de tributo, a União gastou {por_real}."
        frases.append({"numero": trib["cent"], "texto": texto})
    if pop:
        inss = next((x for x in linhas_nos if x["id"] == "previdencia_inss"), None)
        texto = f"é o gasto do ano dividido pelos {fmt_int(pop)} habitantes. Não é o que cada pessoa pagou nem recebeu: é só um jeito de ver o tamanho."
        if trib:
            texto += f" Na mesma conta, os tributos federais dão {fmt_hab(trib['valor'], pop)}" + (f" e o INSS, {fmt_hab(inss['valor'], pop)}." if inss else ".")
        frases.append({"numero": fmt_hab(total, pop).replace(" por habitante", ""), "texto": texto})
    # a maior linha e os juros (as duas são gasto; a dívida em si tem a própria visão)
    maior = linhas_nos[0] if linhas_nos else None
    juros = next((x for x in linhas_nos if x["id"] == "juros"), None)
    if maior:
        texto = f"de cada real gasto foram para a maior linha, {maior['nome']} ({maior['txt']})."
        if juros and juros["id"] != maior["id"]:
            texto += f" Os juros da dívida, pagos em dinheiro, foram {juros['cent']} ({juros['txt']}): o custo da dívida está dentro do R$ 1,00; o pagamento da dívida em si, não."
        frases.append({"numero": maior["cent"], "texto": texto})
    return frases


def cobertura_do_ano(exp: dict, divida: dict | None, caixa: dict | None, div_v: dict | None) -> dict:
    """Uma linha, montada dos dados, dizendo o que este ano tem e o que ainda falta: RREO até que mês, se o SIOP entrou
    (detalhe por ação e carimbo da fonte), até que mês a dívida vai. `completa` é True só no ano fechado com tudo."""
    periodo = int(exp.get("periodo") or 6)
    fechado = (exp.get("estado") or {}).get("selo") == "fechado" or periodo >= 6
    mes_rreo = MESES_NOME[min(periodo * 2, 12) - 1]
    siop = bool((exp.get("siop") or {}).get("usado_nas_linhas"))
    fonte = bool(caixa and caixa.get("por_fonte"))
    partes = [f"RREO de janeiro a {mes_rreo} ({periodo}º bimestre)"]
    faltas = []
    if siop and fonte:
        partes.append("SIOP do ano inteiro (detalhe por ação e carimbo da fonte)")
    else:
        tem = [t for t, ok in (("detalhe por ação", siop), ("carimbo da fonte", fonte)) if ok]
        falta = [t for t, ok in (("detalhe por ação", siop), ("carimbo da fonte", fonte)) if not ok]
        if tem:
            partes.append("SIOP: " + " e ".join(tem))
        if falta:
            faltas.append("sem " + " nem ".join(falta) + (", que só entram com o ano fechado" if not fechado else ""))
    if div_v:
        mes_div = int(div_v.get("mes") or 0)
        exe = (divida or {}).get("execucao") or {}
        ult = int(exe.get("ultimo_mes_com_dados") or 0) if exe.get("disponivel") else 0
        txt = f"dívida até {MESES_NOME[mes_div - 1]}" if 1 <= mes_div <= 12 else "dívida"
        if ult and mes_div and ult < mes_div:
            txt += f" (juros e principal pagos até {MESES_NOME[ult - 1]})"
        partes.append(txt)
        if not div_v.get("fechado"):
            faltas.append("a dívida é a posição do último mês publicado, não a de dezembro")
    else:
        faltas.append("sem a aba da dívida")
    completa = fechado and siop and fonte and bool(div_v and div_v.get("fechado"))
    texto = ("Ano fechado: " if fechado else "Ano em andamento: ") + "; ".join(partes) + "."
    if faltas:
        f_txt = "; ".join(faltas)
        texto += " " + f_txt[0].upper() + f_txt[1:] + "."
    if not fechado:
        texto += " Os números mudam a cada bimestre."
    return {"texto": texto, "completa": completa, "fechado": fechado, "rreo_ate": mes_rreo, "siop": siop, "carimbo": fonte, "divida_ate": MESES_NOME[int(div_v.get("mes")) - 1] if div_v and div_v.get("mes") else None}


def modelo_ano(exp: dict, divida: dict | None, conteudo: dict, linhas_cfg: dict | None, anos: list[dict], limite_acoes: int | None = ACOES_NO_DESENHO) -> dict:
    ano, periodo = int(exp["exercicio"]), int(exp.get("periodo") or 6)
    fechado = (exp.get("estado") or {}).get("selo") == "fechado" or periodo >= 6
    pop_d = exp.get("populacao") or {}
    pop = float(pop_d["valor"]) if pop_d.get("disponivel") and pop_d.get("valor") else None
    total = float(exp["total_recibo"])
    cfg_linhas = {l["id"]: l for l in (linhas_cfg or {}).get("linhas", [])}
    linhas_nos = [_linha_no(l, total, pop, conteudo, limite_acoes, divida, cfg_linhas.get(l["id"])) for l in exp["linhas"]]
    linhas_nos = sorted(linhas_nos, key=lambda n: -n["valor"])
    gasto = _visao_gasto(exp, pop, conteudo, linhas_nos)
    g_no = no("gasto", _texto(conteudo, "nos", "gasto", "nome", "O gasto de verdade"), total, total, pop, cor="#15201F", definicao=_texto(conteudo, "nos", "gasto", "definicao"), nota=_texto(conteudo, "nos", "gasto", "nota"),
              fonte="RREO, Anexo 06 (despesa primária liquidada) + Anexo 01 (juros e encargos da dívida)", entra_na_visao="gasto")
    g_no["filhos"] = linhas_nos
    caixa = _visao_caixa(exp, pop, conteudo, g_no, divida)
    div_v = _visao_divida(divida, conteudo, pop)
    gasto["frases"] = _tres_frases(exp, pop, gasto, linhas_nos, divida)
    conf = _conferencias(exp, linhas_cfg)
    frases = conteudo.get("frases", {})
    if conf["todas_ok"]:
        selo = (frases.get("selo_fechado", "") if fechado else frases.get("selo_aberto", "")).format(n=conf["n"], periodo=periodo)
    else:
        selo = frases.get("selo_problema", "").format(n=conf["n"], falhas=conf["falhas"])
    titulo = (frases.get("titulo_ano_fechado", "") if fechado else frases.get("titulo_ano_aberto", "")).format(ano=ano, periodo=periodo)
    fora = exp.get("fora_do_real") or {}
    a01 = exp.get("anexo01_referencias") or {}
    fora_lista = [
        {"nome": "Rolagem da dívida (título novo pagando título velho)", "valor": fmt_bi(a01.get("refinanciamento")), "explica": "Entra de um lado e sai do outro no mesmo ano; não é gasto."},
        {"nome": "Pagamento de principal da dívida", "valor": fmt_bi(a01.get("amortizacao")), "explica": "Devolver o que foi emprestado não é gasto; o custo da dívida (juros) está dentro."},
        {"nome": "Empréstimos e aplicações financeiras", "valor": fmt_bi((fora.get("emprestimos_financeiros") or {}).get("valor")), "explica": "Dinheiro que deve voltar (FIES, crédito via bancos públicos, capital em empresas); a conta do Tesouro."},
    ]
    return {
        "ano": ano,
        "periodo": periodo,
        "fechado": fechado,
        "publicavel": bool(exp.get("publicavel")),
        "titulo": titulo,
        "selo": selo,
        "gerado_em": (exp.get("gerado_em") or "")[:10],
        "versao_spec": exp.get("versao_spec"),
        "total": total,
        "total_txt": fmt_bi(total),
        "total_exato": fmt_exato(total),
        "populacao": pop,
        "populacao_txt": fmt_int(pop) if pop else "",
        "populacao_fonte": (pop_d.get("fonte") or {}).get("arquivo") if pop else None,
        "populacao_data": pop_d.get("data_referencia") if pop else None,
        "hab_total": fmt_hab(total, pop) if pop else "",
        "linhas": linhas_nos,
        "visoes": [v for v in (caixa, gasto, div_v) if v],
        "visao_inicial": "caixa" if caixa else "gasto",
        "divida_visao": div_v,
        "divida_motivo": "" if div_v else (" ".join(str(((divida or {}).get("estoque_detalhe") or {}).get("motivo") or "").split()) if divida else "o mart da dívida deste ano ainda não rodou (data/export/divida_AAAA.json ausente)"),
        "conferencias": conf,
        "lacres": _lacres(exp),
        "fora": fora_lista,
        "tres_frases": gasto["frases"],
        "anos": anos,
        "nota_estado": (exp.get("estado") or {}).get("nota", ""),
        "regra_rateio": " ".join(str(exp.get("regra_rateio") or "").split()),
        "siop_usado": bool((exp.get("siop") or {}).get("usado_nas_linhas")),
        "divida_disponivel": bool((divida or {}).get("execucao", {}).get("disponivel")),
        "cobertura": cobertura_do_ano(exp, divida, caixa, div_v),
    }


# --------------------------------------------------------------------------------------------------------------------
# perguntas abertas: os números que a página cita vêm dos exports do último ano fechado, nunca digitados
# --------------------------------------------------------------------------------------------------------------------


def numeros_para_perguntas(recibo: dict | None, divida: dict | None, ano: int | None) -> dict:
    """Valores formatados para os marcadores de conteudo/perguntas.yaml; o que falta sai como "(indisponível)"."""
    falta = "(indisponível)"
    out = {"ano": ano if ano else falta}
    r01 = (recibo or {}).get("anexo01_receitas") or {}
    tit = (float(r01["operacoes_credito"]) + float(r01["refinanciamento"])) if r01.get("operacoes_credito") is not None and r01.get("refinanciamento") is not None else None
    emis = ((divida or {}).get("conta_do_ano") or {}).get("emissoes")
    out["titulos_rreo"] = fmt_bi(tit) if tit is not None else falta
    out["emissoes"] = fmt_bi(emis) if emis is not None else falta
    out["diferenca"] = fmt_bi(tit - emis) if tit is not None and emis is not None else falta
    vinc = _medida(divida, "rolagem", "vincendo_12m")
    out["vincendo_site"] = fmt_medida(vinc) if vinc and vinc.get("valor") is not None else falta
    comp_v = next((c for c in (vinc or {}).get("componentes") or [] if c.get("id") == "vincendo_valor"), None)
    out["vincendo_valor"] = fmt_bi(comp_v["valor"]) if comp_v and comp_v.get("valor") is not None else falta
    for c in (divida or {}).get("composicao_indexador") or []:
        out[c["id"]] = (_br(c["share_dpf"] * 100, 1) + "%") if c.get("share_dpf") is not None else falta
    for k in ("selic", "prefixado", "precos", "cambio", "demais"):
        out.setdefault(k, falta)
    return out


def perguntas_abertas(conteudo: dict, recibo: dict | None, divida: dict | None, ano: int | None) -> dict:
    """O bloco de conteudo/perguntas.yaml com os marcadores preenchidos."""
    src = conteudo.get("perguntas") or {}
    nums = numeros_para_perguntas(recibo, divida, ano)

    def f(t: str | None) -> str:
        try:
            return " ".join(str(t or "").format(**nums).split())
        except (KeyError, IndexError, ValueError):
            return " ".join(str(t or "").split())

    itens = []
    for q in src.get("perguntas") or []:
        item = {k: (f(v) if isinstance(v, str) else v) for k, v in q.items()}
        item["status"] = item.get("status") or ("respondida" if (item.get("resposta") or item.get("respondida_em")) else ("enviada" if item.get("protocolo") else "aberta"))
        itens.append(item)
    td = src.get("tem_duvida") or {}
    return {"titulo": src.get("titulo", "Perguntas abertas"), "intro": f(src.get("intro")), "tem_duvida": {"titulo": td.get("titulo", "Tem uma dúvida que não está aqui?"), "texto": f(td.get("texto"))}, "perguntas": itens, "ano": nums["ano"],
            "n_abertas": sum(1 for q in itens if q.get("status") == "aberta"), "n_enviadas": sum(1 for q in itens if q.get("status") == "enviada"), "n_respondidas": sum(1 for q in itens if q.get("status") == "respondida")}


# --------------------------------------------------------------------------------------------------------------------
# leitura dos exports
# --------------------------------------------------------------------------------------------------------------------


def carregar_exports(export_dir: Path) -> dict[str, dict[int, dict]]:
    """{"recibo": {ano: export}, "divida": {ano: export}, "cofog": {ano: export}}, só os publicáveis do recibo."""
    out: dict[str, dict[int, dict]] = {"recibo": {}, "divida": {}, "cofog": {}}
    for p in sorted(Path(export_dir).glob("*.json")):
        m = re.match(r"^(recibo|divida|cofog)_(\d{4})\.json$", p.name)
        if not m:
            continue
        try:
            out[m.group(1)][int(m.group(2))] = json.loads(p.read_text(encoding="utf-8"))
        except Exception as e:  # um export corrompido não derruba os outros
            out[m.group(1)][int(m.group(2))] = {"_erro": str(e)}
    return out


def dados_do_desenho(modelo: dict) -> dict:
    """O JSON embutido na página do ano: só o que o desenho precisa (nós com strings prontas)."""
    total_exato_visao = ""

    def enxuto(n: dict) -> dict:
        nonlocal total_exato_visao
        chaves = ("id", "nome", "valor", "txt", "exato", "pct", "cent", "pct_pai", "cent_pai", "hab", "definicao", "nota", "fonte", "pagina", "proximo", "negativo", "cor", "nao_inclui", "var", "tipo", "tracejado", "tracejado_motivo", "grupo", "outro_lado_de", "outro_lado_nome", "ir_para")
        d = {k: n.get(k) for k in chaves if n.get(k) not in (None, "", [], False)}
        d["valor"] = n["valor"]
        if n.get("base_exato") and n.get("base_exato") != total_exato_visao:
            d["base_exato"] = n["base_exato"]
        if n.get("filhos"):
            d["filhos"] = [enxuto(f) for f in n["filhos"]]
        if n.get("outro_lado"):
            d["outro_lado"] = [enxuto(f) for f in n["outro_lado"]]
        if n.get("nao_alocado"):
            d["nao_alocado"] = [{"nome": a["nome"], "txt": a["txt"], "valor": a["valor"]} for a in n["nao_alocado"]]
        return d

    visoes = []
    for v in modelo["visoes"]:
        total_exato_visao = v["total_exato"]
        visoes.append({k: v.get(k) for k in ("id", "rotulo", "titulo", "explica", "legenda_esq", "legenda_dir", "centro_txt", "centro_nome", "total", "total_txt", "total_exato", "por_fonte", "sem_fonte_motivo", "titulo_esq", "titulo_dir", "hab_nota", "outro") if v.get(k) is not None}
                      | {"entradas": [enxuto(n) for n in v["entradas"]], "saidas": [enxuto(n) for n in v["saidas"]]})
        ids_repetidos(visoes[-1], modelo["ano"])
    return {"ano": modelo["ano"], "visao_inicial": modelo["visao_inicial"], "visoes": visoes}


def ids_repetidos(visao: dict, ano: int | str = "") -> None:
    """Trava: cada nó de uma visão tem um id só seu. O desenho acha o nó pelo id (clique, ficha, "Ir para"); com id
    repetido, a ficha mostra o irmão. Falhou, não publica."""
    vistos: dict[str, str] = {}
    repetidos: list[str] = []

    def andar(n: dict, caminho: list[str]) -> None:
        onde = " > ".join(caminho + [n["nome"]])
        if n["id"] in vistos:
            repetidos.append(f"{n['id']}: {vistos[n['id']]} e {onde}")
        else:
            vistos[n["id"]] = onde
        for f in n.get("filhos") or []:
            andar(f, caminho + [n["nome"]])
        for o in n.get("outro_lado") or []:
            andar(o, caminho + [n["nome"], "(outro lado)"])

    for n in visao["entradas"] + visao["saidas"]:
        andar(n, [])
    if repetidos:
        raise ValueError(f"{ano} / visão {visao['id']}: {len(repetidos)} id(s) de nó repetido(s); o desenho não saberia qual abrir. " + " | ".join(repetidos[:5]))
