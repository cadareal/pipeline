"""Mart "recibo": de cada R$ 1,00, quantos centavos foram para cada linha.

Tudo nasce de config/linhas.yaml: o SQL de cada linha, a ficha técnica exportada e as reconciliações.
Motor local: DuckDB sobre o parquet de staging. (Em nuvem, o mesmo SQL roda no BigQuery com uma troca
de dialeto: regexp_matches -> REGEXP_CONTAINS.)

Além do período pedido, compute() calcula o mesmo período do exercício anterior (quando está no staging) para
publicar a variação anual de cada linha e acusar saltos (reconciliação informativa `variacao_anual`).
"""

from __future__ import annotations

import csv
import json
import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import duckdb

log = logging.getLogger(__name__)


def lit(s: str) -> str:
    return "'" + str(s).replace("'", "''") + "'"


def base_where_static(base: dict, metrica: dict) -> str:
    """Parte do WHERE que não depende do período: anexo, coluna da métrica, escopo e nível."""
    parts = [
        f"regexp_matches(anexo_norm, {lit(base['anexo_regex'])})",
        f"regexp_matches(coluna_norm, {lit(metrica['coluna_regex'])})",
    ]
    if base.get("usa_escopo", True):
        parts.append(f"escopo = {lit(metrica['escopo'])}")
    if base.get("nivel"):
        parts.append(f"nivel = {lit(base['nivel'])}")
    return " AND ".join(parts)


def periodo_where(exercicio: int, periodo: int) -> str:
    return f"exercicio = {int(exercicio)} AND periodo = {int(periodo)}"


def base_where(base: dict, exercicio: int, periodo: int, metrica: dict) -> str:
    return periodo_where(exercicio, periodo) + " AND " + base_where_static(base, metrica)


def sql_sum(where: str, filtro: str | None = None, table: str = "stg") -> str:
    w = where + (f" AND ({filtro})" if filtro else "")
    return f"SELECT COALESCE(SUM(valor), 0) AS valor FROM {table} WHERE {w}"


def largest_remainder(values: list[float], total: int = 100) -> list[int]:
    """Arredonda proporções para inteiros que somam exatamente `total`."""
    if not values or sum(values) <= 0:
        return [0 for _ in values]
    s = sum(values)
    exact = [v / s * total for v in values]
    floors = [math.floor(x) for x in exact]
    rest = total - sum(floors)
    order = sorted(range(len(values)), key=lambda i: exact[i] - floors[i], reverse=True)
    for i in order[:rest]:
        floors[i] += 1
    return floors


@dataclass
class Item:
    id: str
    nome: str
    valor: float
    sql: str
    cfg: dict
    share: float = 0.0
    centavos: int = 0
    anterior: float | None = None  # mesma linha, mesmo período, exercício anterior (None se não há dados)
    variacao: float | None = None  # valor / anterior - 1 (None se não há anterior ou anterior = 0)
    fonte_valor: str = "rreo"  # rreo | siop: de onde veio o número desta linha
    piso: bool = False  # True quando a linha usou o fallback do RREO (ano em andamento ou SIOP ausente)
    notas: list[str] = field(default_factory=list)  # o que o leitor precisa saber sobre este número (vai para o hover)
    transferencias: list[dict] = field(default_factory=list)  # valores movidos entre linhas com base no SIOP


@dataclass
class Reconciliacao:
    id: str
    ok: bool
    critica: bool
    esperado: float
    obtido: float
    diff: float
    descricao: str = ""
    detalhe: str = ""  # texto livre com o que foi encontrado (ex.: quais linhas saltaram)


@dataclass
class Valores:
    """O que depende só de (exercicio, periodo). compute() calcula para o período pedido e para o anterior."""

    exercicio: int
    periodo: int
    total_base: float
    anexo01: dict[str, float]
    linhas: list[Item]
    excluidos: list[Item]
    fora_843_844: float
    ajuste: float
    total_recibo: float


@dataclass
class Resultado:
    exercicio: int
    periodo: int
    linhas: list[Item]
    excluidos: list[Item]
    total_base_anexo02: float
    total_recibo: float
    reconciliacoes: list[Reconciliacao]
    publicavel: bool
    anexo01: dict = field(default_factory=dict)
    snapshots: list[dict] = field(default_factory=list)
    comparacao: dict = field(default_factory=dict)  # {"exercicio", "periodo", "disponivel"}
    siop: dict = field(default_factory=dict)  # conferência cruzada com o SIOP (S8), quando o staging existe


def _tol(total: float) -> float:
    """Tolerância para igualdades contábeis: arredondamento da fonte é de centavos; erro estrutural é de bilhões."""
    return max(1.0, abs(total) * 1e-9)


def tem_dados(q: Callable[[str], float], linhas_cfg: dict, exercicio: int, periodo: int) -> bool:
    """Há linhas do Anexo 02 na métrica para este período?"""
    b02 = linhas_cfg["bases"]["anexo02_subfuncao"]
    w = base_where(b02, exercicio, periodo, linhas_cfg["metrica"])
    return q(f"SELECT COUNT(*) FROM stg WHERE {w}") > 0


def universo_siop(base_cfg: dict, exercicio: int) -> str:
    """WHERE de uma base do SIOP sobre a view `siop_lin`: exercício, agregação e universo comparável ao RREO."""
    partes = [f"exercicio = {int(exercicio)}", f"agregacao = {lit(base_cfg.get('agregacao', 'elemento_orgao'))}"]
    if base_cfg.get("universo"):
        partes.append(f"({base_cfg['universo']})")
    return " AND ".join(partes)


def calcular(q: Callable[[str], float], linhas_cfg: dict, exercicio: int, periodo: int, siop: bool = False) -> Valores:
    """Linhas, exclusões, referências do Anexo 01 e ajuste para um (exercicio, periodo). Sem reconciliações.

    `siop=True` quando a view `siop_lin` existe, tem o exercício e o ano está fechado: as linhas com base no SIOP
    (fonte_valor: siop) usam o SIOP e tiram das linhas do RREO o que o SIOP encontra dentro delas (transferências
    declaradas). Caso contrário essas linhas usam o `fallback` do RREO e saem marcadas como piso.
    """
    metrica = linhas_cfg["metrica"]
    bases = linhas_cfg["bases"]
    refs = linhas_cfg.get("referencias_anexo01", {})
    b02 = bases["anexo02_subfuncao"]
    b01 = bases.get("anexo01_conta")

    w02 = base_where(b02, exercicio, periodo, metrica)
    total_base = q(sql_sum(w02))

    # Valores de referência do Anexo 01 (por cod_conta)
    anexo01: dict[str, float] = {}
    if b01:
        w01 = base_where(b01, exercicio, periodo, metrica)
        for nome, cod in refs.items():
            anexo01[nome] = q(sql_sum(w01, f"cod_conta = {lit(cod)}"))

    linhas: list[Item] = []
    excluidos: list[Item] = []
    via_siop: list[Item] = []
    for cfg in linhas_cfg["linhas"]:
        if cfg.get("residual"):
            continue
        base_cfg = bases[cfg["base"]]
        if base_cfg.get("fonte_valor") == "siop":
            if siop:
                sql = sql_sum(universo_siop(base_cfg, exercicio), cfg.get("filtro"), table="siop_lin")
                item = Item(cfg["id"], cfg["nome"], q(sql), sql, cfg, fonte_valor="siop")
                via_siop.append(item)
            else:
                fb = cfg["fallback"]
                w = base_where(bases[fb["base"]], exercicio, periodo, metrica)
                sql = sql_sum(w, fb.get("filtro"))
                item = Item(cfg["id"], cfg["nome"], q(sql), sql, cfg, piso=True)
                item.notas.append(fb.get("nota", "valor pelo RREO (piso): o SIOP não cobre este período"))
            linhas.append(item)
            continue
        w = base_where(base_cfg, exercicio, periodo, metrica)
        sql = sql_sum(w, cfg.get("filtro"))
        linhas.append(Item(cfg["id"], cfg["nome"], q(sql), sql, cfg))
    for cfg in linhas_cfg.get("excluidos", []):
        w = base_where(bases[cfg["base"]], exercicio, periodo, metrica)
        sql = sql_sum(w, cfg.get("filtro"))
        excluidos.append(Item(cfg["id"], cfg["nome"], q(sql), sql, cfg))

    # Transferências: o que o SIOP encontra dentro de uma linha do RREO (ex.: militares da reserva dentro de Defesa)
    # sai dela e entra na linha do SIOP. Soma zero: o total do recibo não muda, a partição fica certa.
    for item in via_siop:
        if not item.cfg.get("transfere_de_linhas"):
            continue
        base_cfg = bases[item.cfg["base"]]
        for alvo in linhas:
            if alvo is item or alvo.fonte_valor == "siop" or alvo.cfg["base"] != "anexo02_subfuncao" or not alvo.cfg.get("filtro"):
                continue
            sql = sql_sum(universo_siop(base_cfg, exercicio), f"({alvo.cfg['filtro']}) AND ({item.cfg.get('filtro')})", table="siop_lin")
            v = q(sql)
            if v:
                alvo.valor -= v
                alvo.transferencias.append({"para": item.id, "valor": -v, "sql": sql})
                item.transferencias.append({"de": alvo.id, "valor": v, "sql": sql})
                alvo.notas.append(f"não inclui {v:,.2f} de '{item.nome}' que o RREO classifica aqui (SIOP)")

    # Serviço da dívida fora das subfunções 843/844: juros e amortização (Anexo 01) que excedem o que está em
    # 843/844. Removido do residual quando o ajuste está ligado em linhas.yaml (ver justificativa lá).
    juros = next((x.valor for x in linhas if x.id == "juros"), 0.0)
    servico = next((x.valor for x in excluidos if x.id == "servico_divida_bruto"), 0.0)
    fora_843_844 = (juros + anexo01.get("amortizacao", 0.0)) - servico if "amortizacao" in anexo01 else 0.0
    ajuste_ligado = bool(linhas_cfg.get("ajustes", {}).get("servico_divida_fora_843_844", False))
    ajuste = fora_843_844 if (ajuste_ligado and fora_843_844 > 0) else 0.0

    residual_cfg = next((c for c in linhas_cfg["linhas"] if c.get("residual")), None)
    if residual_cfg:
        # linhas que partem o mesmo bolo do Anexo 02: as do RREO (base anexo02) e as do SIOP (que descrevem o mesmo gasto
        # por outro classificador). A linha de juros (Anexo 01) substitui as subfunções 843/844, que estão nas exclusões.
        atribuido = sum(x.valor for x in linhas if x.cfg["base"] == "anexo02_subfuncao" or x.fonte_valor == "siop" or x.piso)
        excl = sum(x.valor for x in excluidos if x.cfg["base"] == "anexo02_subfuncao")
        residual = total_base - atribuido - excl - ajuste
        sql = (
            f"-- residual = total_base ({total_base:.2f}) - atribuido ({atribuido:.2f}) - excluido ({excl:.2f})"
            f" - servico_divida_fora_843_844 ({ajuste:.2f})"
        )
        linhas.append(Item(residual_cfg["id"], residual_cfg["nome"], residual, sql, residual_cfg))

    total_recibo = sum(x.valor for x in linhas)
    return Valores(exercicio, periodo, total_base, anexo01, linhas, excluidos, fora_843_844, ajuste, total_recibo)


def cruzar_siop(con, q: Callable[[str], float], linhas_cfg: dict, exercicio: int, periodo: int, anexo01: dict, fora_843_844: float, add, w02_fun: str) -> dict:
    """Conferências cruzadas com o SIOP (S8), fonte independente do RREO, sobre a view `siop` já criada.

    Universo comparável: orçamentos fiscal e da seguridade (ofss), sem intraorçamentárias (modalidade 91) e, para
    comparar com o Anexo 02, sem refinanciamento (fonte), que o RREO deixa fora do total (I). Só em ano fechado: o
    SIOP é acumulado até a última atualização, o RREO até o bimestre. O detalhe do serviço da dívida fora de
    841-844 por ação sai sempre (é o que vira o ajuste nomeado).
    """
    cfg = linhas_cfg.get("siop", {})
    ag_gnd = cfg.get("agregacao_gnd", "fonte_gnd_subfuncao")
    ag_acao = cfg.get("agregacao_acao", "acao_subfuncao")
    divida = [str(s) for s in cfg.get("subfuncoes_servico_divida", ["841", "842", "843", "844"])]
    lista_divida = ", ".join(lit(s) for s in divida)
    base = f"exercicio = {int(exercicio)} AND ofss AND NOT intra"
    info: dict = {"disponivel": False, "data_atualizacao": None, "agregacao_gnd": ag_gnd, "agregacao_acao": ag_acao}

    n = q(f"SELECT COUNT(*) FROM siop WHERE exercicio = {int(exercicio)} AND agregacao = {lit(ag_gnd)}")
    if n == 0:
        return info
    info["disponivel"] = True
    info["data_atualizacao"] = con.execute(f"SELECT MAX(data_atualizacao) FROM siop WHERE exercicio = {int(exercicio)}").fetchone()[0]

    # Serviço da dívida (GND 2 e 6) fora das subfunções da dívida, por ação: o ajuste deixa de ser hipótese.
    rows = con.execute(
        f"SELECT cod_acao, MAX(nome_acao), cod_gnd, SUM(liquidado) FROM siop WHERE {base} AND agregacao = {lit(ag_acao)} "
        f"AND cod_gnd IN ('2', '6') AND COALESCE(cod_subfuncao, '') NOT IN ({lista_divida}) GROUP BY 1, 3 HAVING SUM(liquidado) <> 0 ORDER BY 4 DESC"
    ).fetchall()
    info["servico_divida_fora_843_844"] = [
        {"acao": a, "nome": nm, "gnd": g, "liquidado": float(v or 0.0)} for a, nm, g, v in rows
    ]
    detalhe_acoes = "; ".join(f"{a} {nm or ''} (GND {g}): {float(v or 0.0):,.2f}" for a, nm, g, v in rows[:6])

    # A 846 "Outros Encargos Especiais" aberta por ação e grupo: é a maior parte do resíduo e mistura naturezas
    # (cobertura do BC, sentenças judiciais, financiamentos, pensões militares). Vai para o hover do resíduo.
    sub846 = lit(str(cfg.get("subfuncao_outros_encargos", "846")))
    rows846 = con.execute(
        f"SELECT cod_acao, MAX(nome_acao), cod_gnd, MAX(nome_gnd), SUM(liquidado) FROM siop WHERE {base} AND agregacao = {lit(ag_acao)} "
        f"AND cod_subfuncao = {sub846} GROUP BY 1, 3 HAVING SUM(liquidado) <> 0 ORDER BY 5 DESC LIMIT 60"
    ).fetchall()
    info["detalhe_846"] = [{"acao": a, "nome": nm, "gnd": g, "gnd_nome": gn, "liquidado": float(v or 0.0)} for a, nm, g, gn, v in rows846]

    if periodo < 6:
        add(
            "siop_comparavel", 0.0, 0.0, ok=True, critica_padrao=False,
            detalhe=f"ano em andamento: SIOP acumulado até {info['data_atualizacao']}, RREO até o {periodo}º bimestre; conferências cruzadas só em ano fechado. Fora de 841-844 até agora: {detalhe_acoes or 'nada'}",
        )
        return info

    cfg_rec = {r["id"]: r for r in linhas_cfg.get("reconciliacoes", [])}
    lim_fun = float(cfg_rec.get("siop_vs_rreo_funcao", {}).get("tolerancia_relativa", 0.005))

    # 1) Por função: SIOP (ofss, sem intra, sem refinanciamento) contra Anexo 02 (exceto intra)
    siop_fun = {str(k): float(v or 0.0) for k, v in con.execute(
        f"SELECT cod_funcao, SUM(liquidado) FROM siop WHERE {base} AND agregacao = {lit(ag_gnd)} AND NOT COALESCE(refinanciamento, false) GROUP BY 1"
    ).fetchall()}
    rreo_fun = {str(k): float(v or 0.0) for k, v in con.execute(f"SELECT funcao_cod, SUM(valor) FROM stg WHERE {w02_fun} GROUP BY 1").fetchall()}
    desvios = sorted(
        ((f, siop_fun.get(f, 0.0) - rreo_fun.get(f, 0.0), rreo_fun.get(f, 0.0)) for f in set(siop_fun) | set(rreo_fun)),
        key=lambda t: -abs(t[1]),
    )
    fora_tol = [(f, d, r) for f, d, r in desvios if abs(d) > max(1.0, abs(r) * lim_fun)]
    detalhe = "; ".join(f"função {f}: SIOP {d:+,.2f} vs RREO" + (f" ({d / r * 100:+.2f}%)" if r else "") for f, d, r in fora_tol[:8])
    add("siop_vs_rreo_funcao", sum(rreo_fun.values()), sum(siop_fun.values()), critica_padrao=False,
        detalhe=detalhe or f"todas as {len(rreo_fun)} funções dentro de ±{lim_fun * 100:.5g}%")

    # 2) Por grupo de despesa, contra o Anexo 01
    gnd = {str(k): float(v or 0.0) for k, v in con.execute(
        f"SELECT cod_gnd, SUM(liquidado) FROM siop WHERE {base} AND agregacao = {lit(ag_gnd)} GROUP BY 1"
    ).fetchall()}
    if "juros" in anexo01:
        add("siop_juros_vs_anexo01", anexo01["juros"], gnd.get("2", 0.0), critica_padrao=False)
    if "amortizacao" in anexo01 and "refinanciamento" in anexo01:
        add("siop_gnd6_vs_anexo01", anexo01["amortizacao"] + anexo01["refinanciamento"], gnd.get("6", 0.0), critica_padrao=False)
        refin = q(f"SELECT COALESCE(SUM(liquidado), 0) FROM siop WHERE {base} AND agregacao = {lit(ag_gnd)} AND cod_gnd = '6' AND COALESCE(refinanciamento, false)")
        add("siop_refinanciamento_vs_anexo01", anexo01["refinanciamento"], refin, critica_padrao=False,
            detalhe="mesma definição dos dois lados: amortização (GND 6) paga com fonte de refinanciamento (código 443); diferença só de arredondamento")

    # 3) Serviço da dívida fora de 841-844 (sem refinanciamento): o que o RREO estima por diferença, o SIOP mede
    fora_siop = q(
        f"SELECT COALESCE(SUM(liquidado), 0) FROM siop WHERE {base} AND agregacao = {lit(ag_gnd)} AND cod_gnd IN ('2', '6') "
        f"AND COALESCE(cod_subfuncao, '') NOT IN ({lista_divida}) AND NOT COALESCE(refinanciamento, false)"
    )
    add("servico_divida_fora_843_844_siop", fora_843_844, fora_siop, critica_padrao=False, detalhe=detalhe_acoes or "nenhuma ação com GND 2 ou 6 fora de 841-844")
    return info


def siop_cobre(q: Callable[[str], float], linhas_cfg: dict, exercicio: int, periodo: int) -> bool:
    """O SIOP pode dar valor a linhas deste período? Só em ano fechado (o SIOP é acumulado até a última atualização,
    o RREO até o bimestre) e quando a agregação por elemento existe no staging para o exercício."""
    if periodo < 6:
        return False
    ag = linhas_cfg.get("siop", {}).get("agregacao_elemento", "elemento_orgao")
    return q(f"SELECT COUNT(*) FROM siop WHERE exercicio = {int(exercicio)} AND agregacao = {lit(ag)}") > 0


def compute(stg_path: Path, linhas_cfg: dict, exercicio: int, periodo: int, siop_path: Path | None = None) -> Resultado:
    con = duckdb.connect()
    con.execute(f"CREATE VIEW stg AS SELECT * FROM read_parquet({lit(str(stg_path))})")
    tem_siop = siop_path is not None and Path(siop_path).exists()
    if tem_siop:
        con.execute(f"CREATE VIEW siop AS SELECT * FROM read_parquet({lit(str(siop_path))})")
        # mesma tabela com os nomes de coluna do RREO (funcao_cod, subfuncao_cod, valor), para os filtros e o SQL de
        # linhas.yaml valerem nas duas fontes. A medida que vira `valor` segue a métrica do recibo (liquidado).
        medida = next((b.get("medida", "liquidado") for b in linhas_cfg["bases"].values() if b.get("fonte_valor") == "siop"), "liquidado")
        con.execute(f"CREATE VIEW siop_lin AS SELECT *, cod_funcao AS funcao_cod, cod_subfuncao AS subfuncao_cod, {medida} AS valor FROM siop")

    def q(sql: str) -> float:
        v = con.execute(sql).fetchone()[0]
        return float(v or 0.0)

    metrica = linhas_cfg["metrica"]
    b02 = linhas_cfg["bases"]["anexo02_subfuncao"]
    bases = linhas_cfg["bases"]

    usa_siop = tem_siop and siop_cobre(q, linhas_cfg, exercicio, periodo)
    v = calcular(q, linhas_cfg, exercicio, periodo, siop=usa_siop)
    linhas, excluidos, anexo01 = v.linhas, v.excluidos, v.anexo01
    total_base, total_recibo, fora_843_844 = v.total_base, v.total_recibo, v.fora_843_844

    shares = [(x.valor / total_recibo) if total_recibo else 0.0 for x in linhas]
    cents = largest_remainder([max(s, 0.0) for s in shares])
    for x, s, c in zip(linhas, shares, cents):
        x.share, x.centavos = s, c

    # Mesmo período do exercício anterior: variação anual por linha (quando há dados no staging)
    ex_ant = exercicio - 1
    anterior: Valores | None = None
    if tem_dados(q, linhas_cfg, ex_ant, periodo):
        anterior = calcular(q, linhas_cfg, ex_ant, periodo, siop=tem_siop and siop_cobre(q, linhas_cfg, ex_ant, periodo))
        prev = {x.id: x for x in anterior.linhas}
        for x in linhas:
            xa = prev.get(x.id)
            x.anterior = xa.valor if xa else None
            x.variacao = (x.valor / x.anterior - 1.0) if x.anterior else None
            if xa is not None and xa.fonte_valor != x.fonte_valor:
                x.notas.append(f"variação anual compara fontes diferentes ({x.fonte_valor} em {exercicio}, {xa.fonte_valor} em {ex_ant})")
    comparacao = {"exercicio": ex_ant, "periodo": periodo, "disponivel": anterior is not None}

    # Reconciliações
    recs: list[Reconciliacao] = []
    cfg_rec = {r["id"]: r for r in linhas_cfg.get("reconciliacoes", [])}

    def add(rid: str, esperado: float, obtido: float, ok: bool | None = None, detalhe: str = "", critica_padrao: bool = True):
        c = cfg_rec.get(rid, {})
        diff = obtido - esperado
        if ok is None:
            rel = c.get("tolerancia_relativa")
            tol = abs(esperado) * float(rel) if rel else _tol(esperado)
            ok = abs(diff) <= tol
        recs.append(Reconciliacao(rid, ok, bool(c.get("critica", critica_padrao)), esperado, obtido, diff, c.get("descricao", ""), detalhe))

    w02_fun = base_where({**b02, "nivel": "funcao"}, exercicio, periodo, metrica)
    soma_funcoes = q(sql_sum(w02_fun))
    add("hierarquia", soma_funcoes, total_base)

    # Cabeçalho (I) identificado pelo próprio conta, não pelo bloco: o bloco I também contém a linha-resumo (II).
    w02_hdr = base_where({**b02, "nivel": "total"}, exercicio, periodo, metrica) + " AND is_header AND header_escopo = 'exceto_intra'"
    total_header = q(sql_sum(w02_hdr))
    add("total_cabecalho", total_header, soma_funcoes)

    if "total_exceto_intra" in anexo01:
        add("total_anexos", anexo01["total_exceto_intra"], total_header)

    w02_fu = base_where({**b02, "nivel": "subfuncao"}, exercicio, periodo, metrica) + " AND fu_inconsistente"
    n_inconsistentes = q(f"SELECT COUNT(*) FROM stg WHERE {w02_fu}")
    add("fu_prefixo_consistente", 0.0, n_inconsistentes, ok=n_inconsistentes == 0)

    residual_val = next((x.valor for x in linhas if x.cfg.get("residual")), 0.0)
    add("residual_nao_negativo", 0.0, residual_val, ok=residual_val >= -_tol(total_base))

    add("centavos_somam_100", 100.0, float(sum(cents)), ok=sum(cents) == 100)

    # Identidade do recibo: total = despesa exceto intra - amortização (Anexo 01). Vale por construção quando o
    # ajuste está ligado; se alguém desligar o ajuste ou a fonte mudar, acusa.
    if "amortizacao" in anexo01:
        add("identidade_total", total_base - anexo01["amortizacao"], total_recibo)

    # Informativa: quanto de juros + amortização está fora de 843/844 (ideal 0; tolerado até a tolerância relativa do total)
    if "amortizacao" in anexo01:
        c = cfg_rec.get("servico_divida_fora_843_844", {})
        rel = float(c.get("tolerancia_relativa", 0.02))
        add("servico_divida_fora_843_844", 0.0, fora_843_844, ok=(0.0 <= fora_843_844 <= rel * total_base))

    inss = next((x.valor for x in linhas if x.id == "previdencia_inss"), 0.0)
    if "beneficios_previdenciarios" in anexo01:
        add("inss_vs_beneficios_anexo01", anexo01["beneficios_previdenciarios"], inss)

    # Informativa: variação anual por linha contra o mesmo período do exercício anterior. Acima do limite é aviso:
    # pode ser economia real, calendário de pagamento ou mudança de classificação na fonte (investigar com `recibo diff`).
    c = cfg_rec.get("variacao_anual", {})
    limite = float(c.get("limite_relativo", 0.15))
    if anterior is None:
        add("variacao_anual", 0.0, 0.0, ok=True, critica_padrao=False, detalhe=f"sem dados de {ex_ant}/p{periodo} no staging; comparação não feita")
    else:
        saltos: list[str] = []
        for x in linhas:
            if x.anterior is None or (x.anterior == 0 and x.valor == 0):
                continue
            if x.anterior == 0:
                saltos.append(f"{x.id}: novo ({x.valor:,.2f})")
            elif abs(x.variacao or 0.0) > limite:
                saltos.append(f"{x.id}: {x.variacao * 100:+.1f}% ({x.anterior:,.2f} -> {x.valor:,.2f})")
        detalhe = "; ".join(saltos) if saltos else f"todas as linhas dentro de ±{limite * 100:.0f}% contra {ex_ant}/p{periodo}"
        add("variacao_anual", 0.0, float(len(saltos)), ok=not saltos, critica_padrao=False, detalhe=detalhe)

    # Linhas com valor do SIOP: o que o SIOP vê dentro do recorte do RREO (fallback) tem que ser quase o que o RREO vê
    if usa_siop:
        for x in linhas:
            if x.fonte_valor != "siop" or not x.cfg.get("fallback"):
                continue
            fb = x.cfg["fallback"]
            rreo_val = q(sql_sum(base_where(bases[fb["base"]], exercicio, periodo, metrica), fb.get("filtro")))
            siop_val = q(sql_sum(universo_siop(bases[x.cfg["base"]], exercicio), f"({fb.get('filtro')}) AND ({x.cfg.get('filtro')})", table="siop_lin"))
            add(f"{x.id}_siop_vs_rreo", rreo_val, siop_val, critica_padrao=False,
                detalhe=f"no recorte do RREO ({fb.get('filtro')}): RREO {rreo_val:,.2f} vs SIOP (elementos da linha) {siop_val:,.2f}; a diferença é o que está nessas subfunções sem ser {x.nome.lower()}")
            fora_recorte = x.valor - siop_val
            x.notas.append(f"{fora_recorte:,.2f} estavam fora do recorte do RREO ({fb.get('filtro')}), em outras funções: " + ", ".join(f"{t['de']} {t['valor']:,.2f}" for t in x.transferencias) + (f"; o resto saiu do resíduo" if fora_recorte - sum(t['valor'] for t in x.transferencias) > 1.0 else ""))

    # Conferência cruzada com o SIOP (S8), quando o staging dele existe
    siop_info: dict = {"disponivel": False, "usado_nas_linhas": usa_siop}
    if tem_siop:
        siop_info = {**cruzar_siop(con, q, linhas_cfg, exercicio, periodo, anexo01, fora_843_844, add, w02_fun), "usado_nas_linhas": usa_siop}
        # juros do Anexo 01 incluem GND 2 fora de 843/844: nomeie no hover da linha de juros
        for a in siop_info.get("servico_divida_fora_843_844", []):
            if a["gnd"] == "2":
                for x in linhas:
                    if x.id == "juros":
                        x.notas.append(f"inclui {a['liquidado']:,.2f} de juros classificados fora de 843/844: {a['acao']} {a['nome']}")

    publicavel = all(r.ok for r in recs if r.critica)

    snaps = [
        dict(zip(("raw_key", "snapshot_id", "sha256_canonical", "fetched_at"), row))
        for row in con.execute(
            f"SELECT DISTINCT raw_key, snapshot_id, sha256_canonical, fetched_at FROM stg "
            f"WHERE {periodo_where(exercicio, periodo)} ORDER BY raw_key"
        ).fetchall()
    ]
    con.close()
    return Resultado(exercicio, periodo, linhas, excluidos, total_base, total_recibo, recs, publicavel, anexo01, snaps, comparacao, siop_info)


def ficha(item: Item, linhas_cfg: dict, fontes: dict[str, dict], res: Resultado) -> dict:
    base = linhas_cfg["bases"].get(item.cfg.get("base", ""), {})
    fonte = fontes.get(base.get("fonte", ""), {})
    return {
        "definicao": item.cfg.get("definicao") or item.cfg.get("motivo"),
        "numerador": f"{item.nome}: soma de 'valor' nas linhas selecionadas pelo filtro",
        "denominador": "Soma de todas as linhas do recibo (despesa executada, ex-refinanciamento e ex-amortização)",
        "fonte": {"id": base.get("fonte"), "nome": fonte.get("nome"), "url": fonte.get("base_url")},
        "dataset": base.get("dataset"),
        "anexo_regex": base.get("anexo_regex"),
        "metrica": linhas_cfg["metrica"]["descricao"],
        "coluna_regex": linhas_cfg["metrica"]["coluna_regex"],
        "escopo": linhas_cfg["metrica"]["escopo"] if base.get("usa_escopo", True) else "n/a (filtro por cod_conta)",
        "filtro": item.cfg.get("filtro"),
        "residual": bool(item.cfg.get("residual", False)),
        "fonte_valor": item.fonte_valor,
        "piso": item.piso,
        "fallback": item.cfg.get("fallback"),
        "transferencias": item.transferencias,
        "sql": item.sql,
        "snapshots": res.snapshots,
    }


def to_export(res: Resultado, linhas_cfg: dict, fontes_cfg: dict, estado: dict) -> dict:
    fontes = {f["id"]: f for f in fontes_cfg["fontes"]}
    juros = next((x.valor for x in res.linhas if x.id == "juros"), 0.0)
    servico = next((x.valor for x in res.excluidos if x.id == "servico_divida_bruto"), 0.0)
    refin02 = next((x.valor for x in res.excluidos if x.id == "refinanciamento"), 0.0)
    return {
        "versao_spec": linhas_cfg.get("versao"),
        "gerado_em": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "exercicio": res.exercicio,
        "periodo": res.periodo,
        "estado": estado,
        "publicavel": res.publicavel,
        "unidade": "R$ (como publicado pela fonte)",
        "regra_rateio": linhas_cfg.get("regra_rateio"),
        "total_recibo": res.total_recibo,
        "total_base_anexo02": res.total_base_anexo02,
        "comparacao": res.comparacao,
        "siop": res.siop,
        "linhas": [
            {
                "id": x.id,
                "nome": x.nome,
                "valor": x.valor,
                "share": x.share,
                "centavos": x.centavos,
                "anterior": x.anterior,
                "variacao_anual": x.variacao,
                "fonte_valor": x.fonte_valor,
                "piso": x.piso,
                "notas": x.notas,
                "transferencias": [{k: v for k, v in t.items() if k != "sql"} for t in x.transferencias],
                "ficha": ficha(x, linhas_cfg, fontes, res),
            }
            for x in res.linhas
        ],
        "fora_do_real": {
            "refinanciamento": {
                "valor": res.anexo01.get("refinanciamento", 0.0),
                "fonte": "Anexo 01, cod_conta " + str(linhas_cfg.get("referencias_anexo01", {}).get("refinanciamento")),
                "excluido_do_anexo02": refin02,
                "motivo": next((e.cfg["motivo"] for e in res.excluidos if e.id == "refinanciamento"), None),
            },
            "amortizacao": {
                "valor": res.anexo01.get("amortizacao", 0.0),
                "fonte": "Anexo 01, cod_conta " + str(linhas_cfg.get("referencias_anexo01", {}).get("amortizacao")),
                "em_843_844": servico - juros,
                "fora_843_844_estimado": (juros + res.anexo01.get("amortizacao", 0.0)) - servico,
                "nota": "Amortização = operação de estoque, fora do R$ 1,00. A parte fora de 843/844 é removida do residual (ajuste declarado em linhas.yaml).",
            },
            "servico_divida_bruto_anexo02": servico,
            "servico_divida_fora_843_844": {
                "valor_rreo": (juros + res.anexo01.get("amortizacao", 0.0)) - servico,
                "acoes": res.siop.get("servico_divida_fora_843_844", []),
                "nota": "Juros e amortização classificados fora das subfunções da dívida. Em 2024 e 2025 é quase só a ação 0669, cobertura do resultado negativo do Banco Central (Lei 13.820/2019): a parte de amortização fica fora do R$ 1,00; a parte de juros está na linha de juros.",
            },
        },
        "anexo01_referencias": res.anexo01,
        "reconciliacoes": [r.__dict__ for r in res.reconciliacoes],
        "chips_planejados": linhas_cfg.get("chips", []),
        "memos_planejados": linhas_cfg.get("memos", []),
    }


def write_outputs(export: dict, marts_dir: Path, export_dir: Path) -> tuple[Path, Path, Path]:
    marts_dir.mkdir(parents=True, exist_ok=True)
    export_dir.mkdir(parents=True, exist_ok=True)
    ex, per = export["exercicio"], export["periodo"]
    p_json = marts_dir / f"recibo_{ex}_p{per:02d}.json"
    p_json.write_text(json.dumps(export, ensure_ascii=False, indent=2), encoding="utf-8")
    p_csv = marts_dir / f"recibo_{ex}_p{per:02d}.csv"
    with open(p_csv, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["id", "nome", "valor", "share", "centavos", "anterior", "variacao_anual"])
        for x in export["linhas"]:
            ant = "" if x["anterior"] is None else f"{x['anterior']:.2f}"
            var = "" if x["variacao_anual"] is None else f"{x['variacao_anual']:.6f}"
            w.writerow([x["id"], x["nome"], f"{x['valor']:.2f}", f"{x['share']:.6f}", x["centavos"], ant, var])
    p_exp = export_dir / f"recibo_{ex}.json"
    p_exp.write_text(json.dumps(export, ensure_ascii=False, indent=2), encoding="utf-8")
    return p_json, p_csv, p_exp


def _pct(v: float | None) -> str:
    return "-" if v is None else f"{v * 100:+.1f}%"


def print_summary(export: dict) -> None:
    comp = export.get("comparacao") or {}
    ant_label = f"vs {comp.get('exercicio')}/p{comp.get('periodo')}" if comp.get("disponivel") else "vs ano-1"
    print(f"\nRecibo {export['exercicio']} (período {export['periodo']}) | estado: {export['estado'].get('selo')} | publicável: {export['publicavel']}")
    print(f"Total do recibo (ex-rolagem, ex-amortização): {export['total_recibo']:,.2f}")
    print(f"{'linha':<36}{'valor':>22}{'share':>10}{'centavos':>10}{ant_label:>16}")
    for x in export["linhas"]:
        print(f"{x['nome'][:35]:<36}{x['valor']:>22,.2f}{x['share']*100:>9.2f}%{x['centavos']:>10}{_pct(x.get('variacao_anual')):>16}")
    if not comp.get("disponivel"):
        print(f"(sem dados de {comp.get('exercicio')}/p{comp.get('periodo')} no staging: variação anual não calculada)")
    notas = [(x["id"], n) for x in export["linhas"] for n in (x.get("notas") or [])]
    if notas:
        print("\nnotas:")
        for lid, n in notas:
            print(f"  [{lid}] {n}")
    fr = export["fora_do_real"]
    am = fr["amortizacao"]
    print(
        f"\nFora do R$ 1,00: refinanciamento (Anexo 01) {fr['refinanciamento']['valor']:,.2f} | "
        f"amortização (Anexo 01) {am['valor']:,.2f} "
        f"[em 843/844: {am['em_843_844']:,.2f}; fora, estimado: {am['fora_843_844_estimado']:,.2f}] | "
        f"serviço da dívida bruto (843+844) {fr['servico_divida_bruto_anexo02']:,.2f}"
    )
    if export.get("anexo01_referencias"):
        print("\nReferências do Anexo 01:")
        for k, v in export["anexo01_referencias"].items():
            print(f"  {k:<28}{v:>22,.2f}")
    print("\nReconciliações:")
    for r in export["reconciliacoes"]:
        flag = "OK " if r["ok"] else "FALHOU"
        crit = "crítica" if r["critica"] else "informativa"
        print(f"  [{flag}] {r['id']} ({crit}): esperado {r['esperado']:,.2f} | obtido {r['obtido']:,.2f} | diff {r['diff']:,.2f}")
        if r.get("detalhe"):
            print(f"           {r['detalhe']}")
