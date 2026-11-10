"""Mart "recibo": de cada R$ 1,00, quantos centavos foram para cada linha.

Tudo nasce de config/linhas.yaml: o SQL de cada linha, a ficha técnica exportada e as reconciliações.
Motor local: DuckDB sobre o parquet de staging. (Em nuvem, o mesmo SQL roda no BigQuery com uma troca
de dialeto: regexp_matches -> REGEXP_CONTAINS.)

Além do período pedido, compute() calcula o mesmo período do exercício anterior (quando está no staging) para
publicar a variação anual de cada linha e acusar saltos (reconciliação informativa `variacao_anual`).

Identidade do recibo (v0.5.0): soma das linhas = despesa primária liquidada do Anexo 06 (a conta oficial do Tesouro)
+ juros e encargos da dívida do Anexo 01. Fica fora: rolagem, amortização e o que o Tesouro trata como financeiro
(empréstimos e aplicações), medido por diferença: despesa exceto intra (Anexo 02) - amortização (Anexo 01) - total.
Por linha, o que sai de cada linha nomeada é o GND 5 com RP 0 do SIOP (ano fechado); o resto sai do resíduo.
Memos (receitas, resultado primário e nominal, juros por competência, dívida, restos a pagar) saem do Anexo 06 e das
receitas do Anexo 01, fora do R$ 1,00.
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

from ..textnorm import norm

log = logging.getLogger(__name__)


class SemDados(RuntimeError):
    """O staging não tem o período pedido: o mart recusa rodar (antes, rodava com zeros e imprimia lixo)."""


class ConfigInvalida(ValueError):
    """linhas.yaml pede algo que o código não sabe calcular (memo sem implementação, referência ausente)."""


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


def conta_where(base: dict, exercicio: int, periodo: int, coluna_regex: str) -> str:
    """WHERE de uma base por cod_conta (Anexo 01 ou 06) numa coluna dada: sem escopo e sem nível."""
    return (
        periodo_where(exercicio, periodo)
        + f" AND regexp_matches(anexo_norm, {lit(base['anexo_regex'])}) AND regexp_matches(coluna_norm, {lit(coluna_regex)})"
    )


def filtro_conta(ref) -> str:
    """Uma referência de conta é um cod_conta exato ou {prefixo: ...} (para códigos longos que só conhecemos pelo
    começo). O prefixo nunca casa a variante intraorçamentária (sufixo Intra)."""
    if isinstance(ref, dict) and ref.get("prefixo"):
        return f"cod_conta LIKE {lit(str(ref['prefixo']) + '%')} AND cod_conta NOT LIKE '%Intra'"
    return f"cod_conta = {lit(str(ref))}"


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
    fonte_valor: str = "rreo"  # rreo | siop | anexo06: de onde veio o número
    piso: bool = False  # True quando a linha usou o fallback do RREO (ano em andamento ou SIOP ausente)
    por_linha: bool = False  # exclusão: True quando o SIOP disse de qual linha saiu cada parte (ano fechado)
    notas: list[str] = field(default_factory=list)  # o que o leitor precisa saber sobre este número (vai para o hover)
    transferencias: list[dict] = field(default_factory=list)  # valores movidos entre linhas (ou para fora do R$ 1,00) com base no SIOP
    por_grupo: dict = field(default_factory=dict)  # exclusão: {grupo: {anexo01, anexo06_primaria, financeira}}


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
    explicada: bool = False  # acima da tolerância, mas explicada ao real por outra conferência (explicada_por em linhas.yaml)


@dataclass
class Chip:
    """Medida transversal às linhas (não é linha: o mesmo real já está em alguma linha). Vem do SIOP em ano fechado."""

    id: str
    nome: str
    cfg: dict
    disponivel: bool
    total: float = 0.0
    share: float = 0.0
    por_linha: list[dict] = field(default_factory=list)  # [{"linha": id, "valor": v, "empenhado": e}], residual incluído
    excluidos: float = 0.0  # parte do chip que cai nas subfunções excluídas (841-844)
    sql: str = ""
    nota: str = ""
    estagios: dict = field(default_factory=dict)  # dotacao_atual, empenhado, liquidado, pago do chip (SIOP)


@dataclass
class Memo:
    """Número publicado ao lado do recibo, fora do R$ 1,00 (receitas, déficit, juros, dívida, restos a pagar)."""

    id: str
    nome: str
    cfg: dict
    disponivel: bool = False
    valor: float | None = None
    unidade: str = "reais"  # reais | reais_por_real | centavos
    componentes: list[dict] = field(default_factory=list)  # [{"id", "nome", "valor", "fonte", ...}]
    notas: list[str] = field(default_factory=list)
    fontes: list[str] = field(default_factory=list)  # chaves do Anexo 06 ("conta:coluna") e do Anexo 01 usadas
    fonte_id: str = "S4"  # "S4+S14" nos memos por habitante (população do IBGE)


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
    exclusao_financeira: float = 0.0  # empréstimos e aplicações financeiras retirados do total (conta do Tesouro)
    anexo06: dict[str, float] = field(default_factory=dict)  # "conta:coluna" -> valor (ler_anexo06)
    anexo01_receitas: dict[str, float] = field(default_factory=dict)
    memos: list[Memo] = field(default_factory=list)


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
    chips: list[Chip] = field(default_factory=list)
    anexo06: dict = field(default_factory=dict)
    anexo01_receitas: dict = field(default_factory=dict)
    memos: list[Memo] = field(default_factory=list)
    populacao: dict = field(default_factory=dict)  # S14: população do ano usada nos memos por habitante (ou o motivo de não haver)
    niveis: dict = field(default_factory=dict)  # por linha: o que há dentro dela (subfunções, funções, elementos), com ajustes nomeados (marts/niveis.py)
    fontes: dict = field(default_factory=dict)  # com que dinheiro cada despesa foi paga, pelo carimbo da fonte no SIOP (marts/fontes.py); só ano fechado


def _tol(total: float) -> float:
    """Tolerância para igualdades contábeis: arredondamento da fonte é de centavos; erro estrutural é de bilhões."""
    return max(1.0, abs(total) * 1e-9)


def tolerancia(esperado: float, rel: float | None, total: float) -> float:
    """Quanto de diferença uma conferência aceita. Relativa ao esperado quando `rel` existe, com um piso absoluto igual
    ao das identidades (R$ 1 ou um bilionésimo do total do universo): sem o piso, um esperado perto de zero vira
    tolerância zero e R$ 2,26 reprova um recibo de R$ 2,7 tri (2023: o RREO publica em dezenas de reais e a estimativa
    "juros + amortização - 843/844" deu -2,26 contra 0,00 no SIOP). Sem `rel`, é a tolerância de identidade."""
    if rel is None:
        return _tol(esperado)
    return max(abs(esperado) * float(rel), _tol(total))


def tem_dados(q: Callable[[str], float], linhas_cfg: dict, exercicio: int, periodo: int) -> bool:
    """Há linhas do Anexo 02 na métrica e a despesa primária do Anexo 06 para este período?"""
    b02 = linhas_cfg["bases"]["anexo02_subfuncao"]
    w = base_where(b02, exercicio, periodo, linhas_cfg["metrica"])
    if q(f"SELECT COUNT(*) FROM stg WHERE {w}") == 0:
        return False
    return tem_anexo06(q, linhas_cfg, exercicio, periodo)


def tem_anexo06(q: Callable[[str], float], linhas_cfg: dict, exercicio: int, periodo: int) -> bool:
    cfg = linhas_cfg.get("anexo06") or {}
    base = linhas_cfg["bases"].get(cfg.get("base", "anexo06_conta"))
    if not base or not cfg.get("grupos"):
        return True  # configuração sem Anexo 06: nada a exigir
    conta = cfg["grupos"]["despesas"]["contas"]["despesa_primaria_total"]
    w = conta_where(base, exercicio, periodo, cfg["colunas"]["liquidadas"])
    return q(f"SELECT COUNT(*) FROM stg WHERE {w} AND ({filtro_conta(conta)})") > 0


def ler_anexo06(q: Callable[[str], float], linhas_cfg: dict, exercicio: int, periodo: int) -> dict[str, float]:
    """Lê do Anexo 06 todas as contas de linhas.yaml (`anexo06.grupos`), nas colunas de cada grupo.
    Chave: "conta:coluna" (ex.: "pessoal:liquidadas", "primario_acima:valor", "divida_liquida:saldo_final")."""
    cfg = linhas_cfg.get("anexo06") or {}
    base = linhas_cfg["bases"].get(cfg.get("base", "anexo06_conta"))
    out: dict[str, float] = {}
    if not base or not cfg.get("grupos"):
        return out
    colunas = cfg.get("colunas", {})
    for grupo in cfg["grupos"].values():
        for col in grupo.get("colunas", []):
            w = conta_where(base, exercicio, periodo, colunas[col])
            for nome, conta in grupo.get("contas", {}).items():
                out[f"{nome}:{col}"] = q(sql_sum(w, filtro_conta(conta)))
    return out


def ler_anexo01_receitas(q: Callable[[str], float], linhas_cfg: dict, exercicio: int, periodo: int) -> dict[str, float]:
    """Receitas realizadas do Anexo 01 (coluna "ATE O BIMESTRE (C)"), por cod_conta de referencias_anexo01_receitas."""
    cfg = linhas_cfg.get("referencias_anexo01_receitas") or {}
    base = linhas_cfg["bases"].get("anexo01_conta")
    out: dict[str, float] = {}
    if not base or not cfg.get("contas"):
        return out
    w = conta_where(base, exercicio, periodo, cfg.get("coluna_regex", "^ATE O BIMESTRE"))
    for nome, conta in cfg["contas"].items():
        out[nome] = q(sql_sum(w, filtro_conta(conta)))
    return out


def universo_siop(base_cfg: dict, exercicio: int) -> str:
    """WHERE de uma base do SIOP sobre a view `siop_lin`: exercício, agregação e universo comparável ao RREO."""
    partes = [f"exercicio = {int(exercicio)}", f"agregacao = {lit(base_cfg.get('agregacao', 'elemento_orgao'))}"]
    if base_cfg.get("universo"):
        partes.append(f"({base_cfg['universo']})")
    return " AND ".join(partes)


def siop_tem_agregacao(q: Callable[[str], float], exercicio: int, agregacao: str) -> bool:
    return q(f"SELECT COUNT(*) FROM siop WHERE exercicio = {int(exercicio)} AND agregacao = {lit(agregacao)}") > 0


def _acoes_dentro(rows: Callable[[str], list] | None, linhas_cfg: dict, exercicio: int, universo: str | None, filtro: str, top: int = 5) -> list[dict]:
    """As maiores ações do SIOP que satisfazem `filtro` (agregação ação x RP), para nomear no hover o que saiu de uma linha."""
    if rows is None:
        return []
    ag = linhas_cfg.get("siop", {}).get("agregacao_acao_rp", "acao_rp_subfuncao")
    uni = f"exercicio = {int(exercicio)} AND agregacao = {lit(ag)}" + (f" AND ({universo})" if universo else "")
    try:
        out = rows(
            f"SELECT cod_acao, MAX(nome_acao), SUM(valor) FROM siop_lin WHERE {uni} AND ({filtro}) "
            f"GROUP BY 1 HAVING SUM(valor) <> 0 ORDER BY 3 DESC LIMIT {int(top)}"
        )
    except Exception as e:  # agregação ausente ou coluna inexistente: o hover fica sem a lista, o número não muda
        log.debug("ações dentro do filtro indisponíveis: %s", e)
        return []
    return [{"acao": a, "nome": (nm or "").strip(), "liquidado": float(v or 0.0)} for a, nm, v in out]


def _fmt_acoes(acoes: list[dict], n: int = 3) -> str:
    return ", ".join(f"{a['acao']} {a['nome']} {a['liquidado']:,.2f}" for a in acoes[:n])


def _nota(texto) -> str:
    return " ".join(str(texto or "").split())


def calcular(
    q: Callable[[str], float], linhas_cfg: dict, exercicio: int, periodo: int, siop: bool = False, rows: Callable[[str], list] | None = None, populacao: dict | None = None
) -> Valores:
    """Linhas, exclusões, referências dos Anexos 01 e 06, ajuste e memos para um (exercicio, periodo). Sem reconciliações.

    `siop=True` quando a view `siop_lin` existe, tem o exercício e o ano está fechado: as linhas com base no SIOP
    (fonte_valor: siop) usam o SIOP e tiram das linhas do RREO o que o SIOP encontra dentro delas (transferências
    declaradas), e a exclusão de empréstimos diz de qual linha saiu cada parte. Caso contrário as linhas usam o
    `fallback` do RREO (marcadas como piso) e a exclusão, exata pelo Anexo 06, sai inteira do resíduo. `rows`
    (consulta que devolve linhas) serve para nomear as ações no hover; é opcional.
    """
    metrica = linhas_cfg["metrica"]
    bases = linhas_cfg["bases"]
    refs = linhas_cfg.get("referencias_anexo01", {})
    b02 = bases["anexo02_subfuncao"]
    b01 = bases.get("anexo01_conta")

    w02 = base_where(b02, exercicio, periodo, metrica)
    total_base = q(sql_sum(w02))

    # Valores de referência do Anexo 01 (por cod_conta, coluna da métrica), receitas do Anexo 01 e Anexo 06
    anexo01: dict[str, float] = {}
    if b01:
        w01 = base_where(b01, exercicio, periodo, metrica)
        for nome, cod in refs.items():
            anexo01[nome] = q(sql_sum(w01, filtro_conta(cod)))
    anexo06 = ler_anexo06(q, linhas_cfg, exercicio, periodo)
    r01 = ler_anexo01_receitas(q, linhas_cfg, exercicio, periodo)

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
                item.notas.append(_nota(fb.get("nota", "valor pelo RREO (piso): o SIOP não cobre este período")))
            linhas.append(item)
            continue
        w = base_where(base_cfg, exercicio, periodo, metrica)
        sql = sql_sum(w, cfg.get("filtro"))
        linhas.append(Item(cfg["id"], cfg["nome"], q(sql), sql, cfg))
    excl_tesouro_cfg: dict | None = None
    for cfg in linhas_cfg.get("excluidos", []):
        if cfg.get("calculo") == "tesouro":
            excl_tesouro_cfg = cfg  # calculada depois das transferências, sobre as linhas já montadas
            continue
        base_cfg = bases[cfg["base"]]
        w = base_where(base_cfg, exercicio, periodo, metrica)
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

    juros = next((x.valor for x in linhas if x.id == "juros"), 0.0)

    # Exclusão pela conta do Tesouro: o que fica fora do R$ 1,00 além da amortização é a diferença entre a despesa do
    # RREO (exceto intra, sem amortização) e a despesa primária oficial (Anexo 06, liquidada) mais os juros (Anexo 01).
    # Por grupo, é Anexo 01 menos a parte primária do Anexo 06. Por linha, o que o SIOP marca como GND 5 com RP 0
    # dentro de uma linha nomeada sai dela (transferência para fora, declarada); o resto sai do resíduo.
    exclusao_financeira = 0.0
    if excl_tesouro_cfg is not None:
        cfg = excl_tesouro_cfg
        a06_prim = anexo06.get("despesa_primaria_total:liquidadas", 0.0)
        amort = anexo01.get("amortizacao", 0.0)
        valor = total_base - amort - a06_prim - juros
        sql = (
            f"-- {valor:.2f} = despesa exceto intra, Anexo 02 ({total_base:.2f}) - AmortizacaoDaDivida, Anexo 01 ({amort:.2f})"
            f" - despesa primária liquidada, Anexo 06 ({a06_prim:.2f}) - JurosEEncargosDaDivida, Anexo 01 ({juros:.2f})"
        )
        item = Item(cfg["id"], cfg["nome"], valor, sql, cfg, fonte_valor="anexo06")
        for gid, g in (cfg.get("por_grupo") or {}).items():
            k01, k06 = g.get("anexo01"), f"{g.get('anexo06')}:liquidadas"
            if k01 in anexo01 and k06 in anexo06:
                item.por_grupo[gid] = {"anexo01": anexo01[k01], "anexo06_primaria": anexo06[k06], "financeira": anexo01[k01] - anexo06[k06]}
        pl_cfg = cfg.get("por_linha") or {}
        base_pl = bases.get(pl_cfg.get("base", "")) if pl_cfg else None
        if siop and base_pl is not None and siop_tem_agregacao(q, exercicio, base_pl.get("agregacao", "")):
            item.por_linha = True
            uni = universo_siop(base_pl, exercicio)
            for alvo in linhas:
                if alvo.fonte_valor == "siop" or alvo.piso or alvo.cfg.get("base") != "anexo02_subfuncao" or not alvo.cfg.get("filtro"):
                    continue
                filtro = f"({alvo.cfg['filtro']}) AND ({pl_cfg.get('filtro')})"
                sql_l = sql_sum(uni, filtro, table="siop_lin")
                v = q(sql_l)
                if v:
                    acoes = _acoes_dentro(rows, linhas_cfg, exercicio, base_pl.get("universo"), filtro)
                    alvo.valor -= v
                    alvo.transferencias.append({"para": item.id, "valor": -v, "tipo": "exclusao", "acoes": acoes, "sql": sql_l})
                    item.transferencias.append({"de": alvo.id, "valor": v, "tipo": "exclusao", "acoes": acoes, "sql": sql_l})
                    alvo.notas.append(
                        f"não inclui {v:,.2f} de '{item.nome}' que o RREO classifica aqui (fora do R$ 1,00; SIOP, GND 5 com RP 0)"
                        + (f": {_fmt_acoes(acoes)}" if acoes else "")
                    )
        else:
            item.notas.append(_nota(cfg.get("nota_sem_siop", "total pelo Anexo 06; sem detalhe por linha neste período")))
            for aviso in cfg.get("avisos_sem_siop", []) or []:
                alvo = next((x for x in linhas if x.id == aviso.get("linha")), None)
                if alvo is not None:
                    alvo.notas.append(_nota(aviso.get("nota", "")))
        excluidos.append(item)
        exclusao_financeira = item.valor

    # Serviço da dívida fora das subfunções 843/844: juros e amortização (Anexo 01) que excedem o que está em
    # 843/844. Removido do residual quando o ajuste está ligado em linhas.yaml (ver justificativa lá).
    servico = next((x.valor for x in excluidos if x.id == "servico_divida_bruto"), 0.0)
    fora_843_844 = (juros + anexo01.get("amortizacao", 0.0)) - servico if "amortizacao" in anexo01 else 0.0
    ajuste_ligado = bool(linhas_cfg.get("ajustes", {}).get("servico_divida_fora_843_844", False))
    ajuste = fora_843_844 if (ajuste_ligado and fora_843_844 > 0) else 0.0

    residual_cfg = next((c for c in linhas_cfg["linhas"] if c.get("residual")), None)
    if residual_cfg:
        # linhas que partem o mesmo bolo do Anexo 02: as do RREO (base anexo02) e as do SIOP (que descrevem o mesmo gasto
        # por outro classificador). A linha de juros (Anexo 01) substitui as subfunções 843/844, que estão nas exclusões.
        # A exclusão financeira sai inteira do total: a parte atribuída a linhas já saiu delas (atribuido é medido
        # depois), então o resíduo perde só o que não estava em linha nomeada.
        atribuido = sum(x.valor for x in linhas if x.cfg["base"] == "anexo02_subfuncao" or x.fonte_valor == "siop" or x.piso)
        excl = sum(x.valor for x in excluidos if x.cfg["base"] == "anexo02_subfuncao")
        residual = total_base - atribuido - excl - ajuste - exclusao_financeira
        sql = (
            f"-- residual = total_base ({total_base:.2f}) - atribuido ({atribuido:.2f}) - excluido ({excl:.2f})"
            f" - servico_divida_fora_843_844 ({ajuste:.2f}) - emprestimos_e_aplicacoes_financeiras ({exclusao_financeira:.2f})"
        )
        linhas.append(Item(residual_cfg["id"], residual_cfg["nome"], residual, sql, residual_cfg))

    total_recibo = sum(x.valor for x in linhas)
    v = Valores(exercicio, periodo, total_base, anexo01, linhas, excluidos, fora_843_844, ajuste, total_recibo, exclusao_financeira, anexo06, r01)
    v.memos = calcular_memos(linhas_cfg, v, populacao)
    return v


def _comp(cid: str, nome: str, valor: float, fonte: str, **extra) -> dict:
    return {"id": cid, "nome": nome, "valor": valor, "fonte": fonte, **extra}


def calcular_memos(linhas_cfg: dict, v: Valores, populacao: dict | None = None) -> list[Memo]:
    """Memos (fora do R$ 1,00) a partir do Anexo 06, das receitas do Anexo 01, do total do recibo e, nos memos por
    habitante, da população do IBGE (`populacao`, de marts.populacao.carregar_populacao). O id em linhas.yaml escolhe
    o cálculo; um memo "implementado" sem cálculo aqui é erro de configuração."""
    a06, r01, anexo01 = v.anexo06, v.anexo01_receitas, v.anexo01
    pop = populacao or {"disponivel": False, "motivo": "população indisponível: staging do IBGE ausente (rode `recibo fetch ibge` e `recibo transform ibge`)"}
    tem06 = bool(a06) and a06.get("despesa_primaria_total:liquidadas", 0.0) != 0.0
    tem01 = bool(r01) and r01.get("receitas_exceto_intra", 0.0) != 0.0

    def g(k: str) -> float:
        return float(a06.get(k, 0.0))

    def r(k: str) -> float:
        return float(r01.get(k, 0.0))

    exclusao = next((e for e in v.excluidos if e.cfg.get("calculo") == "tesouro"), None)
    tributos = g("tributarias:realizadas") + g("contribuicoes:realizadas")
    receita_primaria = g("receita_primaria_total:realizadas")
    out: list[Memo] = []
    for cfg in linhas_cfg.get("memos", []):
        m = Memo(cfg["id"], cfg["nome"], cfg, unidade=cfg.get("unidade", "reais"))
        if cfg.get("status") != "implementado":
            m.notas.append("planejado: ainda sem cálculo")
            out.append(m)
            continue
        mid = cfg["id"]
        if mid == "tributos":
            m.disponivel = tem06
            m.valor = tributos
            m.componentes = [
                _comp("impostos", "Impostos", r("impostos"), "Anexo 01: Impostos"),
                _comp("taxas", "Taxas", r("taxas"), "Anexo 01: Taxas"),
                _comp("contribuicoes_sociais", "Contribuições sociais (INSS, COFINS, CSLL, PIS/PASEP)", r("contribuicoes_sociais"), "Anexo 01: ContribuicoesSociais"),
                _comp("contribuicoes_economicas", "Contribuições econômicas (CIDE e outras)", r("contribuicoes") - r("contribuicoes_sociais"), "Anexo 01: ReceitaDeContribuicoes - ContribuicoesSociais"),
            ]
            m.fontes = ["anexo06: tributarias:realizadas + contribuicoes:realizadas", "anexo01 (componentes): Impostos, Taxas, ContribuicoesSociais, ReceitaDeContribuicoes"]
        elif mid == "receita_primaria":
            m.disponivel = tem06
            m.valor = receita_primaria
            m.componentes = [
                _comp("tributos", "Tributos (impostos, taxas e contribuições)", tributos, "Anexo 06"),
                _comp("patrimonial", "Receita patrimonial (royalties, dividendos, concessões)", g("patrimonial:realizadas"), "Anexo 06: patrimonial"),
                _comp("demais_correntes", "Demais receitas correntes (serviços, multas, restituições)", g("demais_correntes:realizadas"), "Anexo 06: demais_correntes"),
                _comp("transferencias_correntes", "Transferências correntes recebidas", g("transferencias_correntes:realizadas"), "Anexo 06: transferencias_correntes"),
                _comp("capital", "Receitas de capital (venda de bens e outras)", g("receitas_capital:realizadas"), "Anexo 06: receitas_capital"),
            ]
            m.fontes = ["anexo06: receita_primaria_total:realizadas (III) e componentes"]
        elif mid == "receitas_financeiras":
            m.disponivel = tem06 and tem01
            total01 = r("receitas_exceto_intra")
            m.valor = total01 - receita_primaria
            nomeadas = [
                _comp("operacoes_credito", "Títulos vendidos além da rolagem (dívida nova)", r("operacoes_credito"), "Anexo 01: ReceitasDeOperacoesDeCredito"),
                _comp("remuneracao_caixa", "Rendimento do caixa do Tesouro no Banco Central", r("remuneracao_caixa"), "Anexo 01: RemuneracaoDasDisponibilidadesDoTesouro (prefixo)"),
                _comp("resultado_bc", "Resultado do Banco Central repassado ao Tesouro", r("resultado_bc"), "Anexo 01: ResultadoDoBancoCentralDoBrasil (prefixo)"),
                _comp("amortizacoes_emprestimos", "Empréstimos que tomadores devolveram", r("amortizacoes_emprestimos"), "Anexo 01: AmortizacoesDeEmprestimos"),
            ]
            outras = m.valor - sum(c["valor"] for c in nomeadas)
            m.componentes = nomeadas + [
                _comp("outras_financeiras", "Outras receitas que o Tesouro classifica como financeiras (parte da patrimonial e dos serviços)", outras, "diferença: Anexo 01 (I) - Anexo 06 (III) - as quatro acima"),
                _comp("refinanciamento", "Rolagem da dívida (título novo pagando título velho; fora deste total)", r("refinanciamento"), "Anexo 01: OperacoesDeCreditoRefinanciamento (IV)", no_total=False),
            ]
            m.fontes = ["anexo01: ReceitasExcetoIntraOrcamentarias (I) e componentes", "anexo06: receita_primaria_total:realizadas (III)"]
            m.notas.append(f"receita total do Anexo 01 (I) {total01:,.2f} = receita primária (Anexo 06) {receita_primaria:,.2f} + este memo {m.valor:,.2f}")
        elif mid == "gasto_por_real_de_tributo":
            m.disponivel = tem06 and tributos > 0
            m.valor = (v.total_recibo / tributos) if tributos else None
            m.componentes = [
                _comp("total_recibo", "Total do recibo (despesa primária + juros pagos)", v.total_recibo, "recibo"),
                _comp("tributos", "Tributos (impostos, taxas e contribuições)", tributos, "Anexo 06"),
            ]
            m.fontes = ["recibo: total", "anexo06: tributarias:realizadas + contribuicoes:realizadas"]
        elif mid == "origem_do_real_gasto":
            m.disponivel = tem06 and v.total_recibo > 0
            outras_prim = receita_primaria - tributos
            divida = v.total_recibo - receita_primaria
            partes = [
                ("tributos", "Tributos", tributos),
                ("outras_receitas_primarias", "Outras receitas (royalties, dividendos, serviços, venda de bens)", outras_prim),
                ("divida_ou_caixa", "Dívida nova ou caixa", divida),
            ]
            shares = [(p[2] / v.total_recibo) if v.total_recibo else 0.0 for p in partes]
            cents = largest_remainder([max(s, 0.0) for s in shares])
            m.valor = divida
            m.componentes = [_comp(pid, nome, val, "Anexo 06 e recibo", share=s, centavos=c) for (pid, nome, val), s, c in zip(partes, shares, cents)]
            if divida < 0:
                m.notas.append(f"a receita primária superou o gasto do recibo em {-divida:,.2f}: nada foi coberto com dívida; os centavos das outras duas partes foram normalizados para somar 100")
            m.fontes = ["anexo06: receita_primaria_total:realizadas, tributarias:realizadas, contribuicoes:realizadas", "recibo: total"]
        elif mid == "resultado_primario":
            m.disponivel = tem06
            pagas, rpp, rpnp = g("despesa_primaria_total:pagas"), g("despesa_primaria_total:rp_processados_pagos"), g("despesa_primaria_total:rpnp_pagos")
            ajuste, acima, abaixo = g("ajuste_metodologico:valor"), g("primario_acima:valor"), g("primario_abaixo:valor")
            antes = receita_primaria - pagas - rpp - rpnp
            m.valor = acima
            m.componentes = [
                _comp("receita_primaria", "Receita primária realizada (III)", receita_primaria, "Anexo 06"),
                _comp("despesa_paga", "Despesa primária paga no ano", pagas, "Anexo 06: despesa_primaria_total:pagas"),
                _comp("rp_processados_pagos", "Restos a pagar processados pagos (contas de anos anteriores)", rpp, "Anexo 06: despesa_primaria_total:rp_processados_pagos"),
                _comp("rp_nao_processados_pagos", "Restos a pagar não processados pagos", rpnp, "Anexo 06: despesa_primaria_total:rpnp_pagos"),
                _comp("antes_do_ajuste", "Receita menos tudo o que foi pago (calculado)", antes, "calculado"),
                _comp("ajuste_metodologico", "Ajuste metodológico do Tesouro (não detalhado no anexo)", ajuste, "Anexo 06: ajuste_metodologico:valor"),
                _comp("acima_da_linha", "Resultado primário acima da linha (IX), a conta do Tesouro", acima, "Anexo 06: primario_acima:valor"),
                _comp("abaixo_da_linha", "Resultado primário abaixo da linha (XXVII), pela variação da dívida consolidada líquida do RREO", abaixo, "Anexo 06: primario_abaixo:valor"),
                _comp("despesa_liquidada", "Despesa primária liquidada (a base do recibo)", g("despesa_primaria_total:liquidadas"), "Anexo 06: despesa_primaria_total:liquidadas"),
            ]
            m.fontes = ["anexo06: receita_primaria_total, despesa_primaria_total (pagas, rp_processados_pagos, rpnp_pagos), ajuste_metodologico, primario_acima, primario_abaixo"]
            if ajuste:
                m.notas.append(
                    f"em {v.exercicio} o Tesouro aplica um ajuste metodológico de {ajuste:,.2f} sobre a conta de caixa. Segundo a nota 1 do Anexo 06 do RREO da União, "
                    "é a diferença para o resultado publicado no Relatório do Tesouro Nacional (RTN), que segue o Manual de Estatísticas de Finanças Públicas (MEFP); "
                    "o anexo não abre os itens"
                )
        elif mid == "juros_nominais":
            m.disponivel = tem06
            ativos, passivos = g("juros_ativos:valor_incorrido"), g("juros_passivos:valor_incorrido")
            m.valor = passivos - ativos
            m.componentes = [
                _comp("passivos", "Juros que a dívida gerou (XI)", passivos, "Anexo 06: juros_passivos:valor_incorrido"),
                _comp("ativos", "Juros que o governo recebeu (X)", ativos, "Anexo 06: juros_ativos:valor_incorrido"),
                _comp("liquidos", "Juros líquidos (XI - X)", passivos - ativos, "calculado"),
                _comp("juros_pagos", "Juros pagos em dinheiro (linha do recibo)", anexo01.get("juros", 0.0), "Anexo 01: JurosEEncargosDaDivida"),
            ]
            m.fontes = ["anexo06: juros_passivos:valor_incorrido, juros_ativos:valor_incorrido", "anexo01: JurosEEncargosDaDivida"]
        elif mid == "resultado_nominal":
            m.disponivel = tem06
            m.valor = g("nominal_acima:valor")
            m.componentes = [
                _comp("acima_da_linha", "Resultado nominal acima da linha (XII), Tesouro", g("nominal_acima:valor"), "Anexo 06: nominal_acima:valor"),
                _comp("abaixo_da_linha", "Resultado nominal abaixo da linha (XVII), a variação da dívida consolidada líquida do RREO", g("nominal_abaixo:valor"), "Anexo 06: nominal_abaixo:valor"),
                _comp("ajustado", "Resultado nominal ajustado (XXVI)", g("nominal_ajustado:valor"), "Anexo 06: nominal_ajustado:valor"),
                _comp("variacao_saldo_rpp", "Ajuste: variação do saldo de restos a pagar processados (XVIII)", g("variacao_saldo_rpp:valor"), "Anexo 06"),
                _comp("passivos_reconhecidos", "Ajuste: passivos reconhecidos na dívida (XX)", g("passivos_reconhecidos:valor"), "Anexo 06"),
                _comp("outros_ajustes", "Ajuste: outros (XXV)", g("outros_ajustes:valor"), "Anexo 06"),
            ]
            m.fontes = ["anexo06: nominal_acima, nominal_abaixo, nominal_ajustado, variacao_saldo_rpp, passivos_reconhecidos, outros_ajustes (valor)"]
        elif mid == "divida":
            m.disponivel = tem06
            dcl_i, dcl_f = g("divida_liquida:saldo_inicial"), g("divida_liquida:saldo_final")
            m.valor = dcl_f - dcl_i
            m.componentes = [
                _comp("consolidada_inicial", "Dívida consolidada em 31/12 do ano anterior (XIII)", g("divida_consolidada:saldo_inicial"), "Anexo 06"),
                _comp("consolidada_final", "Dívida consolidada no fim do período (XIII)", g("divida_consolidada:saldo_final"), "Anexo 06"),
                _comp("deducoes_final", "Deduções no fim do período (caixa e haveres, XIV)", g("deducoes:saldo_final"), "Anexo 06"),
                _comp("caixa_bruta_final", "Disponibilidade de caixa bruta no fim do período", g("caixa_bruta:saldo_final"), "Anexo 06"),
                _comp("haveres_final", "Demais haveres financeiros no fim do período", g("demais_haveres:saldo_final"), "Anexo 06"),
                _comp("liquida_inicial", "Dívida consolidada líquida em 31/12 do ano anterior (XVI)", dcl_i, "Anexo 06"),
                _comp("liquida_final", "Dívida consolidada líquida no fim do período (XVI)", dcl_f, "Anexo 06"),
                _comp("variacao_liquida", "Dívida nova no período (variação da líquida)", dcl_f - dcl_i, "calculado"),
            ]
            m.fontes = ["anexo06: divida_consolidada, deducoes, caixa_bruta, demais_haveres, divida_liquida (saldo_inicial, saldo_final)"]
            m.notas.append("a dívida nova é o resultado nominal abaixo da linha com o sinal trocado (conferência nominal_abaixo_vs_divida_liquida)")
        elif mid == "restos_a_pagar":
            m.disponivel = tem06
            rpp, rpnp = g("despesa_primaria_total:rp_processados_pagos"), g("despesa_primaria_total:rpnp_pagos")
            m.valor = rpp + rpnp
            inv_total = g("investimentos:pagas") + g("investimentos:rp_processados_pagos") + g("investimentos:rpnp_pagos")
            m.componentes = [
                _comp("processados_pagos", "Restos a pagar processados pagos (já liquidados em anos anteriores)", rpp, "Anexo 06"),
                _comp("nao_processados_pagos", "Restos a pagar não processados pagos (empenhados em anos anteriores)", rpnp, "Anexo 06"),
            ] + [
                _comp(f"{gid}_restos", f"Restos pagos: {nome}", g(f"{gid}:rp_processados_pagos") + g(f"{gid}:rpnp_pagos"), "Anexo 06")
                for gid, nome in (("pessoal", "pessoal"), ("outras_correntes", "outras despesas correntes"), ("investimentos", "investimentos"), ("inversoes", "inversões financeiras"))
            ] + [
                _comp("investimentos_liquidados", "Investimento liquidado no ano (métrica do recibo)", g("investimentos:liquidadas"), "Anexo 06"),
                _comp("investimentos_pagos_no_ano", "Investimento pago no ano (pagas do ano + restos pagos)", inv_total, "calculado"),
            ]
            m.fontes = ["anexo06: despesa_primaria_total e grupos (pagas, rp_processados_pagos, rpnp_pagos, liquidadas)"]
        elif mid == "emprestimos_retorno":
            m.disponivel = tem01
            m.valor = r("amortizacoes_emprestimos")
            m.componentes = [
                _comp("devolvido", "Devolvido ao Tesouro no período", r("amortizacoes_emprestimos"), "Anexo 01: AmortizacoesDeEmprestimos"),
                _comp("emprestado", "Emprestado ou aplicado no período (fora do R$ 1,00)", exclusao.valor if exclusao else 0.0, "exclusão emprestimos_financeiros"),
            ]
            m.fontes = ["anexo01: AmortizacoesDeEmprestimos", "recibo: exclusão emprestimos_financeiros"]
        elif mid == "por_habitante":
            m.fonte_id = "S4+S14"
            m.disponivel = bool(pop.get("disponivel")) and v.total_recibo > 0
            if not pop.get("disponivel"):
                m.notas.append(pop.get("motivo") or "população indisponível")
            else:
                n = float(pop["valor"])
                m.valor = v.total_recibo / n
                m.componentes = [
                    _comp("populacao", f"População do Brasil em {pop.get('data_referencia')} (IBGE, projeção revisão {pop.get('revisao')})", float(pop["valor"]), "S14: IBGE, projeção revisão 2024", unidade="pessoas"),
                    _comp("total_recibo", "Gasto do recibo por habitante (despesa primária + juros pagos)", v.total_recibo / n, "recibo: total / população", unidade="reais_por_habitante"),
                    _comp("tributos", "Tributos federais por habitante (impostos, taxas e contribuições)", (tributos / n) if tem06 else None, "Anexo 06: tributarias + contribuicoes / população", unidade="reais_por_habitante"),
                ] + [
                    _comp(x.id, f"{x.nome} por habitante", x.valor / n, "recibo: linha / população", unidade="reais_por_habitante") for x in v.linhas
                ]
                m.fontes = ["recibo: total e linhas", "anexo06: tributarias:realizadas + contribuicoes:realizadas", f"S14 IBGE: {pop['fonte']['arquivo']} (snapshot {pop['fonte']['snapshot_id']})"]
                if pop.get("estimativa"):
                    m.notas.append(f"a estimativa oficial do IBGE para {v.exercicio} (SIDRA {pop['estimativa']['tabela']}) é igual à projeção: {pop['estimativa']['valor']:,} pessoas")
                if pop.get("censo"):
                    c = pop["censo"]
                    m.notas.append(f"o Censo {c['ano']} contou {c['valor']:,} pessoas; a projeção corrige a cobertura do Censo e dá {c['projecao_no_mesmo_ano']:,} para o mesmo ano: per capita pelo Censo sairia maior na mesma proporção")
        elif mid == "imposto_futuro":
            m.fonte_id = "S4+S14"
            dcl_i, dcl_f = g("divida_liquida:saldo_inicial"), g("divida_liquida:saldo_final")
            m.disponivel = tem06 and bool(pop.get("disponivel"))
            if not pop.get("disponivel"):
                m.notas.append(pop.get("motivo") or "população indisponível")
            elif tem06:
                n = float(pop["valor"])
                m.valor = (dcl_f - dcl_i) / n
                m.componentes = [
                    _comp("variacao_liquida", "Dívida nova no período (variação da dívida consolidada líquida, Anexo 06)", dcl_f - dcl_i, "Anexo 06: divida_liquida (saldo_final - saldo_inicial)"),
                    _comp("populacao", f"População do Brasil em {pop.get('data_referencia')} (IBGE)", float(pop["valor"]), "S14: IBGE, projeção revisão 2024", unidade="pessoas"),
                    _comp("divida_liquida_por_habitante", "Dívida consolidada líquida no fim do período, por habitante", dcl_f / n, "Anexo 06: divida_liquida:saldo_final / população", unidade="reais_por_habitante"),
                ]
                m.fontes = ["anexo06: divida_liquida (saldo_inicial, saldo_final)", f"S14 IBGE: {pop['fonte']['arquivo']} (snapshot {pop['fonte']['snapshot_id']})"]
        else:
            raise ConfigInvalida(f"memo '{mid}' marcado como implementado em linhas.yaml, mas o mart não sabe calculá-lo")
        out.append(m)
    return out


def cruzar_siop(con, q: Callable[[str], float], linhas_cfg: dict, exercicio: int, periodo: int, anexo01: dict, fora_843_844: float, add, w02_fun: str, excl: Item | None = None) -> dict:
    """Conferências cruzadas com o SIOP (S8), fonte independente do RREO, sobre a view `siop` já criada.

    Universo comparável: orçamentos fiscal e da seguridade (ofss), sem intraorçamentárias (modalidade 91) e, para
    comparar com o Anexo 02, sem refinanciamento (fonte), que o RREO deixa fora do total (I). Só em ano fechado: o
    SIOP é acumulado até a última atualização, o RREO até o bimestre. O detalhe do serviço da dívida fora de
    841-844 por ação sai sempre (é o que vira o ajuste nomeado). `excl` é a exclusão pela conta do Tesouro, para a
    comparação lei x Tesouro.
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
        {"acao": a, "nome": (nm or "").strip(), "gnd": g, "liquidado": float(v or 0.0)} for a, nm, g, v in rows
    ]
    detalhe_acoes = "; ".join(f"{a} {(nm or '').strip()} (GND {g}): {float(v or 0.0):,.2f}" for a, nm, g, v in rows[:6])
    # a lista por ação não enxerga a fonte: parte dela pode ser refinanciamento (fonte 443), que o RREO trata como rolagem
    refin_fora = q(
        f"SELECT COALESCE(SUM(liquidado), 0) FROM siop WHERE {base} AND agregacao = {lit(ag_gnd)} AND cod_gnd = '6' "
        f"AND COALESCE(refinanciamento, false) AND COALESCE(cod_subfuncao, '') NOT IN ({lista_divida})"
    )
    info["refinanciamento_fora_841_844"] = refin_fora
    if refin_fora:
        info["nota_lista_acoes"] = (
            f"a lista por ação não distingue fonte: {refin_fora:,.2f} dela têm fonte de refinanciamento (ex.: novação do FCVS, ação 00Q3) "
            "e não entram na medida, porque o RREO os trata como rolagem"
        )
        detalhe_acoes += f" [{info['nota_lista_acoes']}]"

    # A 846 "Outros Encargos Especiais" aberta por ação e grupo: é a maior parte do resíduo e mistura naturezas
    # (cobertura do BC, sentenças judiciais, financiamentos, pensões militares). Vai para o hover do resíduo.
    sub846 = lit(str(cfg.get("subfuncao_outros_encargos", "846")))
    rows846 = con.execute(
        f"SELECT cod_acao, MAX(nome_acao), cod_gnd, MAX(nome_gnd), SUM(liquidado) FROM siop WHERE {base} AND agregacao = {lit(ag_acao)} "
        f"AND cod_subfuncao = {sub846} GROUP BY 1, 3 HAVING SUM(liquidado) <> 0 ORDER BY 5 DESC LIMIT 60"
    ).fetchall()
    info["detalhe_846"] = [{"acao": a, "nome": nm, "gnd": g, "gnd_nome": gn, "liquidado": float(v or 0.0)} for a, nm, g, gn, v in rows846]

    # Empréstimos e aplicações financeiras pelo carimbo da lei (GND 5 com RP 0) por ação: a lista do "Fora do R$ 1,00"
    # e dos hovers. Precisa da agregação ação x RP; sem ela, a lista fica com um aviso. O GND 5 por RP (financeiro
    # contra primário) sai sempre, para a conferência.
    ag_acao_rp = cfg.get("agregacao_acao_rp", "acao_rp_subfuncao")
    ag_rp = cfg.get("agregacao_rp", "rp_subfuncao")
    n_acoes = int(cfg.get("acoes_listadas", 40))
    info["agregacao_acao_rp"] = ag_acao_rp
    tem_acao_rp = q(f"SELECT COUNT(*) FROM siop WHERE exercicio = {int(exercicio)} AND agregacao = {lit(ag_acao_rp)}") > 0
    if tem_acao_rp:
        rows_emp = con.execute(
            f"SELECT cod_acao, MAX(nome_acao), cod_funcao, cod_subfuncao, SUM(liquidado) FROM siop WHERE {base} AND agregacao = {lit(ag_acao_rp)} "
            f"AND cod_gnd = '5' AND cod_resultado_primario = '0' GROUP BY 1, 3, 4 HAVING SUM(liquidado) <> 0 ORDER BY 5 DESC LIMIT {n_acoes}"
        ).fetchall()
        info["emprestimos_financeiros_acoes"] = [
            {"acao": a, "nome": (nm or "").strip(), "funcao": f, "subfuncao": s, "liquidado": float(v or 0.0)} for a, nm, f, s, v in rows_emp
        ]
    else:
        info["emprestimos_financeiros_acoes"] = []
        info["emprestimos_financeiros_nota"] = (
            f"lista por ação indisponível: a agregação {ag_acao_rp} não está no staging "
            f"(rode `recibo fetch siop --agregacoes {ag_acao_rp}` e `recibo transform siop`)"
        )
    rows_rp = con.execute(
        f"SELECT cod_resultado_primario, MAX(nome_resultado_primario), SUM(liquidado) FROM siop WHERE {base} AND agregacao = {lit(ag_rp)} "
        f"AND cod_gnd = '5' GROUP BY 1 ORDER BY 1"
    ).fetchall()
    info["inversoes_por_rp"] = [{"rp": r, "nome": (nm or "").strip(), "liquidado": float(v or 0.0)} for r, nm, v in rows_rp]
    rp0 = sum(d["liquidado"] for d in info["inversoes_por_rp"] if d["rp"] == "0")
    primarias = [d for d in info["inversoes_por_rp"] if d["rp"] != "0"]
    detalhe_rp = (
        f"RP 0 (financeira pela lei orçamentária): {rp0:,.2f}; primária pela lei (RP 1, 2, 3 e emendas): {sum(d['liquidado'] for d in primarias):,.2f}"
        + (" = " + "; ".join(f"RP {d['rp']} {d['liquidado']:,.2f}" for d in primarias) if primarias else "")
    )
    info["inversoes_detalhe"] = detalhe_rp

    if periodo < 6:
        add(
            "siop_comparavel", 0.0, 0.0, ok=True, critica_padrao=False,
            detalhe=f"ano em andamento: SIOP acumulado até {info['data_atualizacao']}, RREO até o {periodo}º bimestre; conferências cruzadas só em ano fechado. Fora de 841-844 até agora: {detalhe_acoes or 'nada'}",
        )
        return info

    # Lei x Tesouro (só em ano fechado, quando SIOP e Anexo 06 cobrem o mesmo período): o que a lei carimba como
    # financeiro (RP 0) contra o que o Tesouro trata como financeiro no mesmo grupo (Anexo 01 - Anexo 06 primária).
    # A diferença fica dentro do R$ 1,00; os fundos constitucionais são a maior parte.
    lt_cfg = (excl.cfg.get("lei_vs_tesouro") or {}) if excl is not None else {}
    grupo = lt_cfg.get("grupo", "inversoes_financeiras")
    if excl is not None and grupo in excl.por_grupo:
        fundos_cfg = lt_cfg.get("fundos_constitucionais") or {}
        acoes_fundos = [str(a) for a in fundos_cfg.get("acoes", [])]
        fundos_val = 0.0
        if tem_acao_rp and acoes_fundos:
            fundos_val = q(
                f"SELECT COALESCE(SUM(liquidado), 0) FROM siop WHERE {base} AND agregacao = {lit(ag_acao_rp)} AND cod_gnd = '5' "
                f"AND cod_resultado_primario = '0' AND cod_acao IN ({', '.join(lit(a) for a in acoes_fundos)})"
            )
        tesouro = excl.por_grupo[grupo]["financeira"]
        fica = rp0 - tesouro
        info["lei_vs_tesouro"] = {
            "grupo": grupo,
            "siop_gnd5_rp0": rp0,
            "tesouro_gnd5_financeira": tesouro,
            "fica_no_real": fica,
            "fundos_constitucionais": {"nome": fundos_cfg.get("nome"), "acoes": acoes_fundos, "valor": fundos_val, "nota": _nota(fundos_cfg.get("nota"))},
            "sem_acao_identificada": fica - fundos_val,
            "nota": (
                f"a lei orçamentária carimba {rp0:,.2f} de inversões financeiras como financeiras (RP 0); o Tesouro trata {tesouro:,.2f} como financeiras. "
                f"A diferença, {fica:,.2f}, fica dentro do R$ 1,00 (resíduo): {fundos_val:,.2f} são os fundos constitucionais FNE/FNO/FCO e {fica - fundos_val:,.2f} não têm ação identificada"
            ),
        }

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
    if "inversoes_financeiras" in anexo01:
        add("siop_inversoes_vs_anexo01", anexo01["inversoes_financeiras"], gnd.get("5", 0.0), critica_padrao=False, detalhe=detalhe_rp)
    if info.get("lei_vs_tesouro"):
        lt = info["lei_vs_tesouro"]
        # acusa só se o Tesouro tratar como financeiro mais do que a lei carimba (aí a atribuição por linha ficaria curta)
        add("lei_vs_tesouro_inversoes", lt["tesouro_gnd5_financeira"], lt["siop_gnd5_rp0"], ok=lt["fica_no_real"] >= -_tol(lt["siop_gnd5_rp0"]),
            critica_padrao=False, detalhe=lt["nota"])

    # 3) Serviço da dívida fora de 841-844 (sem refinanciamento): o que o RREO estima por diferença, o SIOP mede
    fora_siop = q(
        f"SELECT COALESCE(SUM(liquidado), 0) FROM siop WHERE {base} AND agregacao = {lit(ag_gnd)} AND cod_gnd IN ('2', '6') "
        f"AND COALESCE(cod_subfuncao, '') NOT IN ({lista_divida}) AND NOT COALESCE(refinanciamento, false)"
    )
    add("servico_divida_fora_843_844_siop", fora_843_844, fora_siop, critica_padrao=False, detalhe=detalhe_acoes or "nenhuma ação com GND 2 ou 6 fora de 841-844")
    return info


def calcular_chips(q: Callable[[str], float], linhas_cfg: dict, exercicio: int, linhas: list[Item], excluidos: list[Item], total_recibo: float, usa_siop: bool) -> list[Chip]:
    """Chips transversais (emendas, investimento, folha de ativos): total e quanto de cada linha é isso.

    O chip é somado no universo do SIOP (ofss, sem intra) com o filtro do chip; por linha, o filtro do chip E o filtro
    da linha (os filtros do RREO valem no SIOP pela view siop_lin). Linhas sem filtro de função/subfunção (a de juros,
    pelo Anexo 01; a de aposentados, pelo SIOP) ficam com zero; o resíduo recebe o que sobra, fora as exclusões.
    """
    bases = linhas_cfg["bases"]
    out: list[Chip] = []
    for cfg in linhas_cfg.get("chips", []):
        chip = Chip(cfg["id"], cfg["nome"], cfg, disponivel=False)
        if cfg.get("status") != "implementado" or not cfg.get("base"):
            chip.nota = "planejado: ainda sem cálculo"
            out.append(chip)
            continue
        if not usa_siop:
            chip.nota = "sem valor neste período: o chip vem do SIOP e só é calculado em ano fechado"
            out.append(chip)
            continue
        base_cfg = bases[cfg["base"]]
        uni = universo_siop(base_cfg, exercicio)
        chip.sql = sql_sum(uni, cfg.get("filtro"), table="siop_lin")
        chip.total = q(chip.sql)
        chip.share = (chip.total / total_recibo) if total_recibo else 0.0
        chip.disponivel = True
        # estágios da despesa (SIOP): o chip é liquidado, como o recibo; empenhado e pago vão para a ficha, porque em
        # emendas e investimento a diferença entre empenhado e liquidado (restos a pagar) é o tamanho da história
        w_chip = uni + f" AND ({cfg.get('filtro')})"
        chip.estagios = {
            est: q(f"SELECT COALESCE(SUM({est}), 0) FROM siop_lin WHERE {w_chip}") for est in ("dotacao_atual", "empenhado", "liquidado", "pago")
        }
        soma = 0.0
        for x in linhas:
            if x.cfg.get("residual"):
                continue
            if x.cfg.get("base") != "anexo02_subfuncao" or x.fonte_valor == "siop" or not x.cfg.get("filtro"):
                chip.por_linha.append({"linha": x.id, "valor": 0.0, "empenhado": 0.0})
                continue
            w_l = uni + f" AND ({x.cfg['filtro']}) AND ({cfg.get('filtro')})"
            v = q(f"SELECT COALESCE(SUM(valor), 0) FROM siop_lin WHERE {w_l}")
            soma += v
            chip.por_linha.append({"linha": x.id, "valor": v, "empenhado": q(f"SELECT COALESCE(SUM(empenhado), 0) FROM siop_lin WHERE {w_l}")})
        for e in excluidos:
            if e.cfg.get("base") == "anexo02_subfuncao" and e.cfg.get("filtro"):
                chip.excluidos += q(sql_sum(uni, f"({e.cfg['filtro']}) AND ({cfg.get('filtro')})", table="siop_lin"))
        residual = next((x for x in linhas if x.cfg.get("residual")), None)
        if residual is not None:
            emp_linhas = sum(p["empenhado"] for p in chip.por_linha)
            chip.por_linha.append({"linha": residual.id, "valor": chip.total - soma - chip.excluidos, "empenhado": chip.estagios["empenhado"] - emp_linhas})
        chip.nota = cfg.get("nota", "")
        out.append(chip)
    return out


def _ref_subfuncoes(config_dir: Path) -> tuple[dict[str, str], dict[str, set[str]]]:
    """config/ref/subfuncoes.csv: nome normalizado -> código, e código -> nomes normalizados."""
    nome_para_cod: dict[str, str] = {}
    cod_para_nomes: dict[str, set[str]] = {}
    with open(Path(config_dir) / "ref" / "subfuncoes.csv", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            n = norm(row["nome"])
            nome_para_cod[n] = row["codigo"]
            cod_para_nomes.setdefault(row["codigo"], set()).add(n)
    return nome_para_cod, cod_para_nomes


def conferir_ref_subfuncoes(siop_rotulos: list[tuple[str, str]], nome_para_cod: dict[str, str], cod_para_nomes: dict[str, set[str]]) -> tuple[list[str], list[str]]:
    """Confere os apelidos do CSV contra os rótulos (código, nome) do SIOP.

    Devolve (erros, novos): erro = um nome que o CSV manda para um código e o SIOP usa com outro código (o `diff` e as
    fichas imprimiriam o código errado); novo = um nome que o SIOP usa para um código e o CSV não conhece (candidato
    a apelido, se o RREO vier a usá-lo).
    """
    erros: list[str] = []
    novos: list[str] = []
    for cod, nome in sorted(set(siop_rotulos)):
        if not cod or not nome:
            continue
        n = norm(nome)
        cod_csv = nome_para_cod.get(n)
        if cod_csv is not None and cod_csv != cod:
            erros.append(f"'{nome}' é {cod_csv} no CSV e {cod} no SIOP")
        elif cod_csv is None and cod in cod_para_nomes:
            novos.append(f"{cod} '{nome}' (CSV conhece {cod} como {', '.join(sorted(cod_para_nomes[cod]))})")
        elif cod_csv is None:
            novos.append(f"{cod} '{nome}' (código ausente do CSV)")
    return erros, novos


def siop_cobre(q: Callable[[str], float], linhas_cfg: dict, exercicio: int, periodo: int) -> bool:
    """O SIOP pode dar valor a linhas deste período? Só em ano fechado (o SIOP é acumulado até a última atualização,
    o RREO até o bimestre) e quando a agregação por elemento existe no staging para o exercício."""
    if periodo < 6:
        return False
    ag = linhas_cfg.get("siop", {}).get("agregacao_elemento", "elemento_orgao")
    return q(f"SELECT COUNT(*) FROM siop WHERE exercicio = {int(exercicio)} AND agregacao = {lit(ag)}") > 0


def compute(
    stg_path: Path, linhas_cfg: dict, exercicio: int, periodo: int, siop_path: Path | None = None, config_dir: Path | None = None, strict: bool = True,
    stg_ibge: Path | None = None,
) -> Resultado:
    """Calcula o recibo de (exercicio, periodo). `strict=True` recusa rodar quando o staging do RREO não tem o
    período (SemDados). `config_dir` liga a conferência dos apelidos de subfunção contra os rótulos do SIOP.
    `stg_ibge` (staging da população, S14) liga os memos por habitante; sem ele, saem como indisponíveis."""
    from .populacao import carregar_populacao

    populacao = carregar_populacao(stg_ibge, linhas_cfg, exercicio)
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

    def rows(sql: str) -> list:
        return con.execute(sql).fetchall()

    metrica = linhas_cfg["metrica"]
    b02 = linhas_cfg["bases"]["anexo02_subfuncao"]
    bases = linhas_cfg["bases"]

    if strict and not tem_dados(q, linhas_cfg, exercicio, periodo):
        con.close()
        raise SemDados(
            f"o staging do RREO ({stg_path}) não tem {exercicio}/p{periodo:02d} na métrica do recibo (Anexo 02) ou não tem o Anexo 06 desse período. "
            f"Se o índice já tem essa chave, rode `recibo transform siconfi`; se não, `recibo fetch siconfi --exercicios {exercicio} --periodos {periodo}` e depois o transform."
        )

    usa_siop = tem_siop and siop_cobre(q, linhas_cfg, exercicio, periodo)
    v = calcular(q, linhas_cfg, exercicio, periodo, siop=usa_siop, rows=rows if tem_siop else None, populacao=populacao)
    linhas, excluidos, anexo01, a06, r01 = v.linhas, v.excluidos, v.anexo01, v.anexo06, v.anexo01_receitas
    total_base, total_recibo, fora_843_844 = v.total_base, v.total_recibo, v.fora_843_844
    excl_tes = next((e for e in excluidos if e.cfg.get("calculo") == "tesouro"), None)

    shares = [(x.valor / total_recibo) if total_recibo else 0.0 for x in linhas]
    cents = largest_remainder([max(s, 0.0) for s in shares])
    for x, s, c in zip(linhas, shares, cents):
        x.share, x.centavos = s, c

    # Mesmo período do exercício anterior: variação anual por linha (quando há dados no staging)
    ex_ant = exercicio - 1
    anterior: Valores | None = None
    if tem_dados(q, linhas_cfg, ex_ant, periodo):
        anterior = calcular(q, linhas_cfg, ex_ant, periodo, siop=tem_siop and siop_cobre(q, linhas_cfg, ex_ant, periodo), rows=rows if tem_siop else None,
                            populacao=carregar_populacao(stg_ibge, linhas_cfg, ex_ant))
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
            tol = tolerancia(esperado, float(rel) if rel is not None else None, total_base)
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

    # Identidade do recibo: soma das linhas = despesa primária liquidada (Anexo 06) + juros (Anexo 01). Vale por
    # construção quando o ajuste está ligado; se alguém desligar o ajuste ou a fonte mudar, acusa.
    if a06:
        add("identidade_total", a06.get("despesa_primaria_total:liquidadas", 0.0) + anexo01.get("juros", 0.0), total_recibo)

    # A exclusão pela conta do Tesouro fecha por grupo de despesa, e o que sai das linhas nomeadas cabe nela
    if excl_tes is not None and a06:
        soma_grupos = sum(gr["financeira"] for gr in excl_tes.por_grupo.values())
        add("exclusao_financeira_por_grupo", excl_tes.valor, soma_grupos,
            detalhe="; ".join(f"{gid}: Anexo 01 {gr['anexo01']:,.2f} - Anexo 06 primária {gr['anexo06_primaria']:,.2f} = {gr['financeira']:,.2f}" for gid, gr in excl_tes.por_grupo.items()))
        por_linha = sum(t["valor"] for t in excl_tes.transferencias)
        add("exclusao_por_linha", excl_tes.valor, por_linha, ok=por_linha <= excl_tes.valor + _tol(total_base),
            detalhe=("saiu das linhas: " + "; ".join(f"{t['de']} {t['valor']:,.2f}" for t in excl_tes.transferencias) + f"; do resíduo {excl_tes.valor - por_linha:,.2f}")
            if excl_tes.transferencias else f"sem detalhe por linha neste período: {excl_tes.valor:,.2f} saem do resíduo")
        if "pessoal" in anexo01 and "pessoal:liquidadas" in a06:
            add("pessoal_anexo06_vs_anexo01", anexo01["pessoal"], a06["pessoal:liquidadas"], critica_padrao=False)
        if "investimentos" in anexo01 and "investimentos:liquidadas" in a06:
            add("investimentos_anexo06_vs_anexo01", anexo01["investimentos"], a06["investimentos:liquidadas"], critica_padrao=False)

    # Receitas: tributos iguais nos dois anexos; hierarquias fecham; fórmulas do Anexo 06 fecham
    if a06 and r01:
        add("tributos_anexo01_vs_anexo06", r01.get("tributaria", 0.0) + r01.get("contribuicoes", 0.0),
            a06.get("tributarias:realizadas", 0.0) + a06.get("contribuicoes:realizadas", 0.0))
        correntes = sum(r01.get(k, 0.0) for k in ("tributaria", "contribuicoes", "patrimonial", "agropecuaria", "industrial", "servicos", "transferencias_correntes", "outras_correntes", "a_classificar"))
        capital = sum(r01.get(k, 0.0) for k in ("operacoes_credito", "alienacao_bens", "amortizacoes_emprestimos", "transferencias_capital", "outras_capital"))
        d_cor, d_cap, d_tot = correntes - r01.get("correntes", 0.0), capital - r01.get("capital", 0.0), (r01.get("correntes", 0.0) + r01.get("capital", 0.0)) - r01.get("receitas_exceto_intra", 0.0)
        add("receitas_anexo01_hierarquia", r01.get("receitas_exceto_intra", 0.0), correntes + capital,
            ok=all(abs(d) <= _tol(total_base) for d in (d_cor, d_cap, d_tot)),
            detalhe=f"correntes: soma das contas - ReceitasCorrentes = {d_cor:,.2f}; capital: {d_cap:,.2f}; correntes + capital - (I) = {d_tot:,.2f}")
    if a06:
        g = lambda k: a06.get(k, 0.0)  # noqa: E731
        cor06 = sum(g(f"{k}:realizadas") for k in ("tributarias", "contribuicoes", "patrimonial", "transferencias_correntes", "demais_correntes"))
        cap06 = sum(g(f"{k}:realizadas") for k in ("alienacao_bens", "transferencias_capital", "outras_capital"))
        d1, d2, d3 = cor06 - g("receitas_correntes:realizadas"), cap06 - g("receitas_capital:realizadas"), (g("receitas_correntes:realizadas") + g("receitas_capital:realizadas")) - g("receita_primaria_total:realizadas")
        add("receitas_anexo06_hierarquia", g("receita_primaria_total:realizadas"), cor06 + cap06,
            ok=all(abs(d) <= _tol(total_base) for d in (d1, d2, d3)),
            detalhe=f"correntes (I) - soma = {-d1:,.2f}; capital (II) - soma = {-d2:,.2f}; I + II - III = {d3:,.2f}")
        e1 = (g("pessoal:liquidadas") + g("outras_correntes:liquidadas")) - g("despesas_correntes:liquidadas")
        e2 = (g("transferencias_constitucionais:liquidadas") + g("demais_despesas_correntes:liquidadas")) - g("outras_correntes:liquidadas")
        e3 = (g("investimentos:liquidadas") + g("inversoes:liquidadas")) - g("despesas_capital:liquidadas")
        e4 = (g("despesas_correntes:liquidadas") + g("despesas_capital:liquidadas") + g("reserva_contingencia:liquidadas")) - g("despesa_primaria_total:liquidadas")
        add("despesas_anexo06_hierarquia", g("despesa_primaria_total:liquidadas"),
            g("pessoal:liquidadas") + g("transferencias_constitucionais:liquidadas") + g("demais_despesas_correntes:liquidadas") + g("investimentos:liquidadas") + g("inversoes:liquidadas") + g("reserva_contingencia:liquidadas"),
            ok=all(abs(d) <= _tol(total_base) for d in (e1, e2, e3, e4)),
            detalhe=f"IV - (pessoal + outras correntes) = {-e1:,.2f}; outras correntes - (transferências + demais) = {-e2:,.2f}; V - (investimentos + inversões) = {-e3:,.2f}; VII - (IV + V + VI) = {-e4:,.2f}")
        antes = g("receita_primaria_total:realizadas") - g("despesa_primaria_total:pagas") - g("despesa_primaria_total:rp_processados_pagos") - g("despesa_primaria_total:rpnp_pagos")
        add("resultado_primario_acima_da_linha", antes + g("ajuste_metodologico:valor"), g("primario_acima:valor"),
            detalhe=f"receita {g('receita_primaria_total:realizadas'):,.2f} - pagas {g('despesa_primaria_total:pagas'):,.2f} - RP processados {g('despesa_primaria_total:rp_processados_pagos'):,.2f} - RP não processados {g('despesa_primaria_total:rpnp_pagos'):,.2f} + ajuste {g('ajuste_metodologico:valor'):,.2f}")
        juros_liq = g("juros_ativos:valor_incorrido") - g("juros_passivos:valor_incorrido")
        add("resultado_nominal_acima_da_linha", g("primario_acima:valor") + juros_liq, g("nominal_acima:valor"), critica_padrao=False)
        add("resultado_primario_abaixo_da_linha", g("nominal_ajustado:valor") - juros_liq, g("primario_abaixo:valor"), critica_padrao=False)
        add("divida_liquida_identidade", g("divida_consolidada:saldo_final") - g("deducoes:saldo_final"), g("divida_liquida:saldo_final"), critica_padrao=False)
        add("nominal_abaixo_vs_divida_liquida", g("divida_liquida:saldo_inicial") - g("divida_liquida:saldo_final"), g("nominal_abaixo:valor"), critica_padrao=False,
            detalhe=f"dívida líquida de {g('divida_liquida:saldo_inicial'):,.2f} para {g('divida_liquida:saldo_final'):,.2f}: dívida nova {g('divida_liquida:saldo_final') - g('divida_liquida:saldo_inicial'):,.2f}")

    # Informativa: quanto de juros + amortização está fora de 843/844 (ideal 0; tolerado até a tolerância relativa do total)
    if "amortizacao" in anexo01:
        c = cfg_rec.get("servico_divida_fora_843_844", {})
        rel = float(c.get("tolerancia_relativa", 0.02))
        # negativo do tamanho do arredondamento da fonte (2023: -R$ 2,26 num RREO publicado em dezenas de reais) é zero
        add("servico_divida_fora_843_844", 0.0, fora_843_844, ok=(-_tol(total_base) <= fora_843_844 <= rel * total_base))

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
        siop_info = {**cruzar_siop(con, q, linhas_cfg, exercicio, periodo, anexo01, fora_843_844, add, w02_fun, excl_tes), "usado_nas_linhas": usa_siop}
        # juros do Anexo 01 incluem GND 2 fora de 843/844: nomeie no hover da linha de juros
        for a in siop_info.get("servico_divida_fora_843_844", []):
            if a["gnd"] == "2":
                for x in linhas:
                    if x.id == "juros":
                        x.notas.append(f"inclui {a['liquidado']:,.2f} de juros classificados fora de 843/844: {a['acao']} {a['nome']}")
        # o que a lei carimba como financeiro e o Tesouro conta como gasto fica no resíduo: nomeie no hover dele
        lt = siop_info.get("lei_vs_tesouro")
        if lt and lt["fica_no_real"] > _tol(total_base):
            for x in linhas:
                if x.cfg.get("residual"):
                    x.notas.append(
                        f"inclui {lt['fica_no_real']:,.2f} que a lei orçamentária carimba como empréstimo (GND 5, RP 0) e o Tesouro conta como gasto: "
                        f"{lt['fundos_constitucionais']['valor']:,.2f} de fundos constitucionais FNE/FNO/FCO e {lt['sem_acao_identificada']:,.2f} sem ação identificada"
                    )
        if excl_tes is not None and lt:
            excl_tes.notas.append(lt["nota"])

    # Níveis de dentro de cada linha (o que o site abre ao clicar): subfunções, funções do resíduo, elementos do SIOP.
    # Informativa: a conta tem que fechar linha a linha; se não fechar, a linha sai sem abertura e o recibo não muda.
    from .niveis import calcular_niveis

    niveis = calcular_niveis(con, q, rows, linhas_cfg, exercicio, periodo, v, usa_siop, tem_siop, siop_info)
    abertas = [x.id for x in linhas if niveis.get(x.id, {}).get("tipo")]
    nao_fecham = [f"{k}: partes {d['soma']:,.2f} vs linha {next(x.valor for x in linhas if x.id == k):,.2f}" for k, d in niveis.items() if d.get("tipo") and not d.get("fecha")]
    add("niveis_fecham", 0.0, float(len(nao_fecham)), ok=not nao_fecham, critica_padrao=False,
        detalhe="; ".join(nao_fecham) if nao_fecham else f"{len(abertas)} linhas abertas em níveis, todas fecham: " + ", ".join(abertas))

    # Fontes de recursos (carimbo da fonte no SIOP): a visão do caixa do site usa isto em vez de rateio. Informativa.
    from .fontes import calcular_fontes

    fontes_rec = calcular_fontes(q, rows, linhas_cfg, exercicio, anexo01, usa_siop, tem_siop)
    if fontes_rec.get("disponivel"):
        cf = fontes_rec.get("conferencia") or {}
        if "total_rreo" in cf:
            add("fontes_total_vs_anexo01", cf["total_rreo"], cf["total_siop"], critica_padrao=False,
                detalhe="SIOP por fonte (todos os GND, com refinanciamento) contra Anexo 01 (total exceto intra + refinanciamento)")

    chips = calcular_chips(q, linhas_cfg, exercicio, linhas, excluidos, total_recibo, usa_siop)
    if usa_siop:
        # os totais por grupo do SIOP têm que bater com o Anexo 01 (mesmo universo): confere o que sustenta os chips
        gnd_cfg = linhas_cfg.get("siop", {})
        uni_gnd = f"exercicio = {int(exercicio)} AND agregacao = {lit(gnd_cfg.get('agregacao_gnd', 'fonte_gnd_subfuncao'))} AND ofss AND NOT intra"
        if "investimentos" in anexo01:
            add("siop_investimentos_vs_anexo01", anexo01["investimentos"], q(f"SELECT COALESCE(SUM(liquidado), 0) FROM siop WHERE {uni_gnd} AND cod_gnd = '4'"), critica_padrao=False)
        if "pessoal" in anexo01:
            add("siop_pessoal_vs_anexo01", anexo01["pessoal"], q(f"SELECT COALESCE(SUM(liquidado), 0) FROM siop WHERE {uni_gnd} AND cod_gnd = '1'"), critica_padrao=False)

    # Apelidos de subfunção do CSV contra os rótulos do SIOP do exercício: um nome com código diferente nas duas
    # pontas é erro de referência (ver subfuncoes_ref_vs_siop em linhas.yaml).
    if tem_siop and config_dir is not None and q(f"SELECT COUNT(*) FROM siop WHERE exercicio = {int(exercicio)}") > 0:
        nome_para_cod, cod_para_nomes = _ref_subfuncoes(config_dir)
        rotulos = [(str(c), str(n)) for c, n in rows(
            f"SELECT DISTINCT cod_subfuncao, nome_subfuncao FROM siop WHERE exercicio = {int(exercicio)} AND cod_subfuncao IS NOT NULL AND nome_subfuncao IS NOT NULL"
        )]
        erros, novos = conferir_ref_subfuncoes(rotulos, nome_para_cod, cod_para_nomes)
        detalhe = "; ".join(erros) if erros else f"{len(rotulos)} rótulos de subfunção do SIOP conferidos com config/ref/subfuncoes.csv"
        if novos:
            detalhe += f" | nomes que o SIOP usa e o CSV não conhece ({len(novos)}): " + "; ".join(novos[:8])
        add("subfuncoes_ref_vs_siop", 0.0, float(len(erros)), ok=not erros, detalhe=detalhe)

    # População (S14): projeção contra estimativa oficial do IBGE, quando o staging existe e o ano tem as duas. Informativa:
    # nenhum centavo do recibo depende dela; se falhar, os memos por habitante saem como indisponíveis.
    conf_pop = populacao.get("conferencia") if populacao else None
    if conf_pop and conf_pop.get("calculada", True):
        c_pop = {r["id"]: r for r in (linhas_cfg.get("populacao") or {}).get("reconciliacoes", [])}.get("populacao_projecao_vs_estimativa", {})
        recs.append(Reconciliacao(conf_pop["id"], bool(conf_pop["ok"]), False, float(conf_pop["esperado"]), float(conf_pop["obtido"]), float(conf_pop["diff"]), c_pop.get("descricao", ""), conf_pop.get("detalhe", "")))

    # Conferências "explicadas": acima da própria tolerância, mas com o valor nomeado ao real por outra conferência
    # (explicada_por em linhas.yaml). Deixam de acusar; a explicação vai para o detalhe.
    for r in recs:
        dep = cfg_rec.get(r.id, {}).get("explicada_por")
        if dep and not r.ok:
            d = next((x for x in recs if x.id == dep), None)
            if d is not None and d.ok:
                r.ok, r.explicada = True, True
                r.detalhe = (r.detalhe + "; " if r.detalhe else "") + f"acima da tolerância, mas explicada ao real por {dep}: {d.detalhe[:200]}"

    publicavel = all(r.ok for r in recs if r.critica)

    snaps = [
        dict(zip(("raw_key", "snapshot_id", "sha256_canonical", "fetched_at"), row))
        for row in con.execute(
            f"SELECT DISTINCT raw_key, snapshot_id, sha256_canonical, fetched_at FROM stg "
            f"WHERE {periodo_where(exercicio, periodo)} ORDER BY raw_key"
        ).fetchall()
    ]
    con.close()
    return Resultado(exercicio, periodo, linhas, excluidos, total_base, total_recibo, recs, publicavel, anexo01, snaps, comparacao, siop_info, chips, a06, r01, v.memos, populacao, niveis, fontes_rec)


def ficha(item: Item, linhas_cfg: dict, fontes: dict[str, dict], res: Resultado) -> dict:
    base = linhas_cfg["bases"].get(item.cfg.get("base", ""), {})
    fonte = fontes.get(base.get("fonte", ""), {})
    return {
        "definicao": item.cfg.get("definicao") or item.cfg.get("motivo"),
        "numerador": f"{item.nome}: soma de 'valor' nas linhas selecionadas pelo filtro",
        "denominador": "Soma de todas as linhas do recibo = despesa primária liquidada (Anexo 06) + juros e encargos da dívida (Anexo 01); fora: rolagem, amortização e empréstimos e aplicações financeiras",
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


def _sem_sql(d: dict) -> dict:
    return {k: v for k, v in d.items() if k != "sql"}


def exportar_exclusao_financeira(res: Resultado, linhas_cfg: dict) -> dict:
    """Bloco "Fora do R$ 1,00" da exclusão pela conta do Tesouro (empréstimos e aplicações financeiras)."""
    out: dict = {}
    for e in res.excluidos:
        if e.cfg.get("calculo") != "tesouro":
            continue
        por_linha = [{"linha": t["de"], "valor": t["valor"], "acoes": t.get("acoes", [])} for t in e.transferencias]
        no_residual = e.valor - sum(t["valor"] for t in e.transferencias)
        out[e.id] = {
            "nome": e.nome,
            "valor": e.valor,
            "fonte_valor": e.fonte_valor,
            "calculo": "despesa exceto intra (Anexo 02) - AmortizacaoDaDivida (Anexo 01) - despesa primária liquidada (Anexo 06) - JurosEEncargosDaDivida (Anexo 01)",
            "por_grupo": e.por_grupo,
            "por_linha_disponivel": e.por_linha,
            "por_linha": por_linha,
            "filtro_por_linha": (e.cfg.get("por_linha") or {}).get("filtro"),
            "do_residual": no_residual,
            "lei_vs_tesouro": res.siop.get("lei_vs_tesouro"),
            "acoes": res.siop.get("emprestimos_financeiros_acoes", []),
            "acoes_nota": "lista pelo carimbo da lei orçamentária (GND 5 com RP 0, SIOP); soma mais do que o valor fora do R$ 1,00 quando o Tesouro conta parte dela como gasto (ver lei_vs_tesouro)",
            "por_rp": res.siop.get("inversoes_por_rp", []),
            "notas": e.notas + ([res.siop["emprestimos_financeiros_nota"]] if res.siop.get("emprestimos_financeiros_nota") else []),
            "motivo": e.cfg.get("motivo"),
            "sql": e.sql,
        }
    return out


def exportar_memo(m: Memo) -> dict:
    return {
        "id": m.id,
        "nome": m.nome,
        "definicao": m.cfg.get("definicao"),
        "status": m.cfg.get("status"),
        "disponivel": m.disponivel,
        "unidade": m.unidade,
        "valor": m.valor,
        "componentes": m.componentes,
        "notas": m.notas,
        "ficha": {"fonte": {"id": m.fonte_id}, "fontes": m.fontes},
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
        "total_recibo_definicao": "despesa primária liquidada (Anexo 06) + juros e encargos da dívida (Anexo 01)",
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
                "transferencias": [_sem_sql(t) for t in x.transferencias],
                "niveis": res.niveis.get(x.id),
                "ficha": ficha(x, linhas_cfg, fontes, res),
            }
            for x in res.linhas
        ],
        "fora_do_real": {
            **exportar_exclusao_financeira(res, linhas_cfg),
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
        "anexo01_receitas": res.anexo01_receitas,
        "anexo06_referencias": res.anexo06,
        "memos": [exportar_memo(m) for m in res.memos],
        "populacao": res.populacao,
        "fontes_de_recursos": res.fontes,
        "reconciliacoes": [r.__dict__ for r in res.reconciliacoes],
        "chips": [
            {
                "id": c.id,
                "nome": c.nome,
                "definicao": c.cfg.get("definicao"),
                "status": c.cfg.get("status"),
                "disponivel": c.disponivel,
                "fonte_valor": "siop" if c.disponivel else None,
                "total": c.total,
                "share": c.share,
                "centavos": round(c.share * 100, 2),
                "estagios": c.estagios,
                "liquidado_sobre_empenhado": (c.estagios.get("liquidado", 0.0) / c.estagios["empenhado"]) if c.estagios.get("empenhado") else None,
                "por_linha": c.por_linha,
                "excluidos": c.excluidos,
                "nota": c.nota,
                "ficha": {"fonte": {"id": "S8"}, "filtro": c.cfg.get("filtro"), "sql": c.sql, "universo": linhas_cfg["bases"].get(c.cfg.get("base", ""), {}).get("universo")},
            }
            for c in res.chips
        ],
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


def _fmt_memo_valor(m: dict) -> str:
    if m.get("valor") is None:
        return "-"
    if m.get("unidade") == "reais_por_real":
        return f"R$ {m['valor']:.2f} por R$ 1,00"
    if m.get("unidade") == "reais_por_habitante":
        return f"R$ {m['valor']:,.2f} por habitante"
    return f"{m['valor']:,.2f}"


def _fmt_comp_valor(c: dict) -> str:
    v = c.get("valor")
    if v is None:
        return f"{'-':>22}"
    if c.get("unidade") == "pessoas":
        return f"{int(round(v)):>22,}"
    return f"{v:>22,.2f}"


def print_summary(export: dict) -> None:
    comp = export.get("comparacao") or {}
    ant_label = f"vs {comp.get('exercicio')}/p{comp.get('periodo')}" if comp.get("disponivel") else "vs ano-1"
    print(f"\nRecibo {export['exercicio']} (período {export['periodo']}) | estado: {export['estado'].get('selo')} | publicável: {export['publicavel']}")
    print(f"Total do recibo (despesa primária liquidada do Anexo 06 + juros pagos do Anexo 01): {export['total_recibo']:,.2f}")
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
    chips = [c for c in export.get("chips", []) if c.get("disponivel")]
    if chips:
        print("\nchips transversais (SIOP; o mesmo real já está nas linhas; centavos de cada real entre parênteses):")
        print(f"  {'linha':<36}" + "".join(f"{c['nome'][:22]:>24}" for c in chips))
        for x in export["linhas"]:
            vals = []
            for c in chips:
                v = next((p["valor"] for p in c["por_linha"] if p["linha"] == x["id"]), 0.0)
                vals.append(f"{v:>24,.2f}")
            print(f"  {x['nome'][:35]:<36}" + "".join(vals))
        print(f"  {'total':<36}" + "".join(f"{c['total']:>24,.2f}" for c in chips))
        print(f"  {'':<36}" + "".join(f"{'(' + format(c['centavos'], '.2f') + ' centavos)':>24}" for c in chips))
        print(f"  {'empenhado':<36}" + "".join(f"{c['estagios'].get('empenhado', 0.0):>24,.2f}" for c in chips))
        print(f"  {'pago':<36}" + "".join(f"{c['estagios'].get('pago', 0.0):>24,.2f}" for c in chips))
        print(f"  {'liquidado / empenhado':<36}" + "".join(
            f"{(format(c['liquidado_sobre_empenhado'] * 100, '.0f') + '%') if c.get('liquidado_sobre_empenhado') is not None else '-':>24}" for c in chips
        ))
    else:
        pend = [c["nome"] for c in export.get("chips", []) if not c.get("disponivel")]
        if pend:
            print(f"\nchips sem valor neste período (vêm do SIOP, só em ano fechado): {', '.join(pend)}")
    fr = export["fora_do_real"]
    am = fr["amortizacao"]
    print(
        f"\nFora do R$ 1,00: refinanciamento (Anexo 01) {fr['refinanciamento']['valor']:,.2f} | "
        f"amortização (Anexo 01) {am['valor']:,.2f} "
        f"[em 843/844: {am['em_843_844']:,.2f}; fora, estimado: {am['fora_843_844_estimado']:,.2f}] | "
        f"serviço da dívida bruto (843+844) {fr['servico_divida_bruto_anexo02']:,.2f}"
    )
    for k, e in fr.items():
        if not isinstance(e, dict) or "por_grupo" not in e:
            continue
        print(f"  {e['nome']} (conta do Tesouro: Anexo 02 - amortização - despesa primária do Anexo 06 - juros): {e['valor']:,.2f}")
        if e["por_grupo"]:
            print("    por grupo (Anexo 01 - Anexo 06 primária): " + "; ".join(f"{g} {d['financeira']:,.2f}" for g, d in e["por_grupo"].items()))
        if e["por_linha"]:
            print("    saiu das linhas (SIOP, GND 5 com RP 0): " + "; ".join(f"{p['linha']} {p['valor']:,.2f}" for p in e["por_linha"]) + f"; do resíduo {e['do_residual']:,.2f}")
        else:
            print(f"    sem detalhe por linha neste período: {e['do_residual']:,.2f} saem do resíduo")
        lt = e.get("lei_vs_tesouro")
        if lt:
            print(
                f"    lei x Tesouro (GND 5): carimbo RP 0 do SIOP {lt['siop_gnd5_rp0']:,.2f} | financeiro pelo Tesouro {lt['tesouro_gnd5_financeira']:,.2f} | "
                f"fica no R$ 1,00 {lt['fica_no_real']:,.2f} (fundos constitucionais FNE/FNO/FCO {lt['fundos_constitucionais']['valor']:,.2f}; sem ação identificada {lt['sem_acao_identificada']:,.2f})"
            )
        for n in e.get("notas", []):
            print(f"    nota: {n}")
        if e.get("acoes"):
            print("    maiores (por ação, carimbo da lei: GND 5 com RP 0, SIOP):")
            for a in e["acoes"][:12]:
                print(f"      {a['acao']} {a['funcao']}/{a['subfuncao']} {a['nome'][:84]:<84} {a['liquidado']:>22,.2f}")
    memos = [m for m in export.get("memos", []) if m.get("disponivel")]
    if memos:
        print("\nMemos (fora do R$ 1,00; Anexo 06 e receitas do Anexo 01):")
        for m in memos:
            if m.get("unidade") == "centavos":
                print(f"  {m['nome']}: " + " | ".join(f"{c['nome'].split(' (')[0]} {c.get('centavos', 0)} centavos" for c in m["componentes"]))
            else:
                print(f"  {m['nome'][:60]:<62}{_fmt_memo_valor(m):>24}")
            for c in m["componentes"]:
                extra = "  (fora do total)" if c.get("no_total") is False else ""
                print(f"      {c['nome'][:70]:<72}{_fmt_comp_valor(c)}{extra}")
            for n in m.get("notas", []):
                print(f"      nota: {n}")
    pend = [m["nome"] for m in export.get("memos", []) if not m.get("disponivel")]
    if pend:
        print(f"  memos sem valor neste período ou planejados: {', '.join(pend)}")
    pop = export.get("populacao") or {}
    if pop and not pop.get("disponivel"):
        print(f"  população (S14): {pop.get('motivo')}")
    if export.get("anexo01_referencias"):
        print("\nReferências do Anexo 01 (despesas):")
        for k, v in export["anexo01_referencias"].items():
            print(f"  {k:<28}{v:>22,.2f}")
    print("\nReconciliações:")
    for r in export["reconciliacoes"]:
        flag = "OK " if r["ok"] else "FALHOU"
        crit = ("crítica" if r["critica"] else "informativa") + (", explicada" if r.get("explicada") else "")
        print(f"  [{flag}] {r['id']} ({crit}): esperado {r['esperado']:,.2f} | obtido {r['obtido']:,.2f} | diff {r['diff']:,.2f}")
        if r.get("detalhe"):
            print(f"           {r['detalhe']}")
