"""Mart dívida: quatro medidas, cada uma com várias réguas nomeadas e nunca misturadas. Produto separado do recibo.

  1. Juros do ano: pagos em dinheiro (Anexo 01, a linha do recibo; conferidos com a execução da DPF do Tesouro);
     apropriados na DPF (Tesouro, fatores de variação); nominais pelo Banco Central (NFSP, líquidos; Governo Federal e
     Governo Central); por competência no RREO (Anexo 06, brutos e líquidos); e o número do COFOG.
  2. Dívida nova no ano: variação da DPF decomposta como o Tesouro faz (emissões, resgates, juros apropriados, operações
     com o BC); variação da dívida consolidada líquida do RREO; variação da dívida líquida pelo BC; resultado nominal
     pelo BC.
  3. Estoque: DPF em poder do público (interna e externa), carteira do BC, dívida consolidada e líquida do RREO, dívida
     líquida pelo BC (reais e % do PIB), dívida bruta do governo geral (% do PIB), Tesouro Direto.
  4. Risco de rolagem: quanto da DPF em mercado vence em 12 meses (título a título, do CSV do Tesouro), prazo médio
     (BC), composição por indexador (CSV do Tesouro, conferida com o BC). Colchão de liquidez e detentores por classe:
     sem fonte aberta, dito com todas as letras.
  5. Por habitante (v0.8.0): DPF em poder do público, dívida nova no ano, dívida líquida do Governo Federal (BC),
     dívida consolidada líquida (RREO) e juros pagos, divididos pela população do ano (IBGE, S14, projeção revisão
     2024). Só sai quando a projeção e a estimativa oficial do IBGE batem; nenhuma outra medida depende dela.

Fontes: staging da dívida (S1: planilhas e CSVs do Tesouro, layout fixado em linhas.yaml `divida`), staging do BCB
(S16), o cálculo do recibo para o ano (Anexos 01 e 06) e, quando existe, o staging do COFOG. Unidades convertidas para
reais na leitura (bilhões e milhões das planilhas, milhões do BC), com a origem anotada em cada medida.
"""

from __future__ import annotations

import calendar
import csv
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path

import polars as pl

from ..grade import GradeFormato, Planilha
from ..textnorm import norm
from ..transform.bcb import STAGING_FILE as BCB_FILE
from ..transform.divida import INDICE_FILE
from .recibo import SemDados, tolerancia

log = logging.getLogger(__name__)

UNIDADES = {"reais": 1.0, "milhares": 1e3, "milhoes": 1e6, "bilhoes": 1e9}
GRUPOS = ("juros", "divida_nova", "estoque", "rolagem", "por_habitante")
NOMES_GRUPOS = {
    "juros": "Juros do ano: o mesmo ano, cinco réguas",
    "divida_nova": "Dívida nova no ano",
    "estoque": "Estoque: medidas nomeadas",
    "rolagem": "Risco de rolagem",
    "por_habitante": "Por habitante (população do IBGE, S14)",
}


@dataclass
class Medida:
    id: str
    nome: str
    valor: float | None
    unidade: str  # reais | % do PIB | % | meses
    fonte: str
    definicao: str
    disponivel: bool = True
    motivo: str | None = None
    componentes: list[dict] = field(default_factory=list)


@dataclass
class ReconciliacaoDivida:
    id: str
    ok: bool
    critica: bool
    esperado: float
    obtido: float
    diff: float
    descricao: str = ""
    detalhe: str = ""
    calculada: bool = True  # False quando um dos lados não existe (fonte indisponível): não é uma falha de número


@dataclass
class ResultadoDivida:
    exercicio: int
    mes_referencia: int
    fechado: bool
    medidas: dict[str, list[Medida]]
    composicao: list[dict]
    fontes: dict
    reconciliacoes: list[ReconciliacaoDivida] = field(default_factory=list)
    publicavel: bool = False
    selo: str = ""
    avisos: list[str] = field(default_factory=list)
    execucao: dict = field(default_factory=dict)  # juros e principal do período por carteira (CSV de execução do Tesouro)
    estoque_detalhe: dict = field(default_factory=dict)  # a DPF em mercado em células índice x família x prazo (v0.10.0)
    conta_do_ano: dict = field(default_factory=dict)  # começou devendo, vendeu, pagou, juros, terminou devendo (fatores do Tesouro)
    estoque_mensal: list = field(default_factory=list)  # DPF em poder do público mês a mês no ano (planilha 2.1)


# --------------------------------------------------------------------------------------------------------------------
# leitura das fontes
# --------------------------------------------------------------------------------------------------------------------


def _indice(staging_dir: Path) -> pl.DataFrame | None:
    p = Path(staging_dir) / INDICE_FILE
    return pl.read_parquet(p) if p.exists() else None


def localizar(indice: pl.DataFrame | None, cfg: dict) -> dict | None:
    """Registro do índice do staging para (dataset, arquivo_regex); None se não há."""
    if indice is None:
        return None
    rx = re.compile(cfg.get("arquivo_regex", ".*"), re.I)
    cand = indice.filter((pl.col("dataset") == cfg["dataset"]) & (pl.col("tipo") != "erro"))
    for r in cand.iter_rows(named=True):
        if rx.search(str(r["arquivo"])):
            return r
    return None


def _fator(cfg: dict) -> float:
    u = str(cfg.get("unidade", "reais")).lower()
    if u not in UNIDADES:
        raise GradeFormato(f"unidade {u!r} em linhas.yaml (divida) não é uma de {sorted(UNIDADES)}")
    return UNIDADES[u]


class Fontes:
    """Carrega o que existe no staging; o que falta vira motivo nas medidas, não exceção."""

    def __init__(self, staging_dir: Path, cfg: dict):
        self.cfg = cfg
        self.staging_dir = Path(staging_dir)
        self.indice = _indice(staging_dir)
        self.registros: dict[str, dict] = {}
        self.planilhas: dict[str, Planilha] = {}
        self.tabelas: dict[str, pl.DataFrame] = {}
        self.erros: dict[str, str] = {}
        for nome in ("estoque_xlsx", "fatores_xlsx"):
            self._carregar_planilha(nome)
        for nome in ("estoque_csv", "execucao_csv", "tesouro_direto_csv"):
            self._carregar_tabela(nome)
        p = self.staging_dir / BCB_FILE
        self.bcb = pl.read_parquet(p) if p.exists() else None
        if self.bcb is None:
            self.erros["bcb"] = f"staging do BCB não existe ({p}); rode `recibo fetch bcb` e `recibo transform bcb`"

    def _carregar_planilha(self, nome: str) -> None:
        c = self.cfg.get(nome)
        if not c:
            self.erros[nome] = f"linhas.yaml não tem divida.{nome}"
            return
        r = localizar(self.indice, c)
        if r is None:
            self.erros[nome] = f"arquivo do dataset {c['dataset']!r} ({c.get('arquivo_regex')}) não está no staging da dívida; rode `recibo fetch divida` e `recibo transform divida`"
            return
        try:
            grade = pl.read_parquet(self.staging_dir / r["staging"])
            self.planilhas[nome] = Planilha(grade, c.get("planilha"))
            self.registros[nome] = r
        except (GradeFormato, Exception) as e:  # noqa: BLE001
            self.erros[nome] = f"{r['arquivo']}: {e}"

    def _carregar_tabela(self, nome: str) -> None:
        c = self.cfg.get(nome)
        if not c:
            self.erros[nome] = f"linhas.yaml não tem divida.{nome}"
            return
        r = localizar(self.indice, c)
        if r is None:
            self.erros[nome] = f"arquivo do dataset {c['dataset']!r} ({c.get('arquivo_regex')}) não está no staging da dívida; rode `recibo fetch divida` e `recibo transform divida`"
            return
        df = pl.read_parquet(self.staging_dir / r["staging"])
        faltam = []
        for chave, col in c["colunas"].items():
            cols = col if isinstance(col, list) else [col]
            for cc in cols:
                if cc not in df.columns:
                    faltam.append(cc)
        if faltam:
            self.erros[nome] = f"{r['arquivo']}: colunas de linhas.yaml ausentes: {faltam}; colunas do arquivo: {[x for x in df.columns if not x.startswith('_') and not x.endswith('__num') and not x.endswith('__data')]}"
            return
        self.tabelas[nome] = df
        self.registros[nome] = r

    def ficha(self) -> dict:
        out = {}
        for nome, r in self.registros.items():
            out[nome] = {k: r.get(k) for k in ("dataset", "arquivo", "recurso_nome", "recurso_data", "snapshot_id", "sha256_canonical", "fetched_at", "linhas")}
            out[nome]["unidade_arquivo"] = self.cfg.get(nome, {}).get("unidade")
        if self.bcb is not None:
            out["bcb"] = {"series": int(self.bcb["codigo"].n_unique()), "pontos": self.bcb.height, "lido_em": str(self.bcb["fetched_at"].max())}
        out["indisponiveis"] = dict(self.erros)
        return out


# --------------------------------------------------------------------------------------------------------------------
# cálculos por fonte
# --------------------------------------------------------------------------------------------------------------------


def _num(df: pl.DataFrame, col: str) -> pl.Expr:
    n = f"{col}__num"
    if n not in df.columns:
        if col in df.columns and df.select((_txt(col) == "").all().alias("v")).item():
            return pl.lit(None, dtype=pl.Float64)  # coluna inteira vazia (um mês sem lançamentos): não há número a ler
        raise GradeFormato(f"coluna {col!r} não foi lida como número no transform (falta {n!r})")
    return pl.col(n)


def _data(df: pl.DataFrame, col: str) -> pl.Expr:
    n = f"{col}__data"
    if n not in df.columns:
        raise GradeFormato(f"coluna {col!r} não foi lida como data no transform (falta {n!r})")
    return pl.col(n)


def _txt(col: str) -> pl.Expr:
    return pl.col(col).cast(pl.Utf8).fill_null("").str.strip_chars()


def meses_disponiveis_csv(df: pl.DataFrame, col_mes: str, ano: int) -> list[int]:
    d = df.select(_data(df, col_mes).alias("d")).drop_nulls()
    return sorted(d.filter(pl.col("d").dt.year() == ano)["d"].dt.month().unique().to_list())


def fatores_do_ano(pl_: Planilha, cfg: dict, ano: int, mes_ref: int) -> dict:
    """Fluxos somados de janeiro até mes_ref (R$ da planilha, já em reais), estoque anterior de janeiro e estoque de
    mes_ref; a coluna anual do Tesouro, quando existe, fica em `coluna_anual` para conferência."""
    f = _fator(cfg)
    rot = cfg["linhas"]
    meses = [m for m in pl_.meses_do_ano(ano) if m <= mes_ref]
    if not meses:
        raise GradeFormato(f"fatores: o ano {ano} não está na planilha (anos: {sorted({a for a, _ in pl_.col_mes})})")
    out: dict = {"meses": meses}

    def soma(chave: str) -> float:
        vals = [pl_.valor_mes(rot[chave], ano, m) for m in meses]
        return float(sum(v for v in vals if v is not None)) * f

    for chave in ("emissoes", "resgates", "juros", "juros_dpmfi", "juros_dpfe", "operacoes_bc", "total", "variacao"):
        out[chave] = soma(chave)
    ea = pl_.valor_mes(rot["estoque_anterior"], ano, meses[0])
    ef = pl_.valor_mes(rot["estoque_fim"], ano, meses[-1])
    out["estoque_anterior"] = float(ea) * f if ea is not None else None
    out["estoque_fim"] = float(ef) * f if ef is not None else None
    out["coluna_anual"] = {}
    if ano in pl_.col_ano:
        for chave in ("emissoes", "resgates", "juros", "operacoes_bc", "total", "variacao"):
            v, origem = pl_.valor_anual(rot[chave], ano)
            if origem == "coluna_anual" and v is not None:
                out["coluna_anual"][chave] = float(v) * f
    return out


def estoque_xlsx_em(pl_: Planilha, cfg: dict, ano: int, mes: int) -> dict[str, float | None]:
    f = _fator(cfg)
    out = {}
    for chave, rotulo in cfg["linhas"].items():
        v = pl_.valor_mes(rotulo, ano, mes)
        out[chave] = float(v) * f if v is not None else None
    return out


def classificar_indexador(titulo: str, externa: bool, cfg_idx: dict) -> str:
    """Primeiro os regex, na ordem de linhas.yaml (assim "Global BRL" cai em prefixado antes de "Global" cair em câmbio);
    depois, o que for dívida externa vai para a classe marcada `externa: true`; o resto é "demais"."""
    t = (titulo or "").strip().upper()
    for chave, c in cfg_idx.items():
        rx = c.get("regex")
        if rx and re.match(rx, t, re.I):
            return chave
    if externa:
        for chave, c in cfg_idx.items():
            if c.get("externa"):
                return chave
    return "demais"


def estoque_csv_em(df: pl.DataFrame, cfg: dict, ano: int, mes: int) -> dict:
    """Soma do estoque em (ano, mes) por carteira e tipo; vincendo em N meses; composição por indexador (em reais)."""
    f = _fator(cfg)
    c = cfg["colunas"]
    v = cfg["valores"]
    d = df.with_columns(_data(df, c["mes"]).alias("_mes"), _num(df, c["valor"]).alias("_valor"), _data(df, c["vencimento"]).alias("_venc"),
                        _txt(c["classe"]).alias("_classe"), _txt(c["tipo"]).alias("_tipo"), _txt(c["titulo"]).alias("_titulo"))
    d = d.filter((pl.col("_mes").dt.year() == ano) & (pl.col("_mes").dt.month() == mes))
    if d.height == 0:
        return {"disponivel": False, "motivo": f"o CSV do estoque não tem {mes:02d}/{ano}"}
    merc = d.filter(pl.col("_classe") == v["mercado"])
    bc = d.filter(pl.col("_classe") == v["banco_central"])
    total = lambda x: float(x["_valor"].sum()) * f  # noqa: E731
    out = {
        "disponivel": True,
        "linhas": d.height,
        "mercado": total(merc),
        "banco_central": total(bc),
        "mercado_interna": total(merc.filter(pl.col("_tipo") == v["interna"])),
        "mercado_externa": total(merc.filter(pl.col("_tipo") == v["externa"])),
        "interna_total": total(d.filter(pl.col("_tipo") == v["interna"])),
    }
    # vincendo em N meses: vencimento até o último dia do mesmo mês, N meses à frente
    n = int(cfg.get("vincendo_meses", 12))
    a2, m2 = ano + (mes + n - 1) // 12, (mes + n - 1) % 12 + 1
    limite = date(a2, m2, calendar.monthrange(a2, m2)[1])
    venc = merc.filter(pl.col("_venc").is_not_null() & (pl.col("_venc") <= limite))
    out["vincendo"] = {"meses": n, "ate": limite.isoformat(), "valor": total(venc), "share_dpf": (total(venc) / out["mercado"]) if out["mercado"] else None, "linhas": venc.height}
    out["vincendo_interna"] = total(venc.filter(pl.col("_tipo") == v["interna"]))
    # composição por indexador (regex no nome do título; externa vira câmbio)
    cfg_idx = cfg.get("indexadores") or {}
    ext = v["externa"]
    merc = merc.with_columns(pl.struct(["_titulo", "_tipo"]).map_elements(lambda s: classificar_indexador(s["_titulo"], s["_tipo"] == ext, cfg_idx), return_dtype=pl.Utf8).alias("_idx"))
    comp = []
    for chave in list(cfg_idx):
        parte = merc.filter(pl.col("_idx") == chave)
        val = total(parte)
        val_int = total(parte.filter(pl.col("_tipo") == v["interna"]))
        comp.append({"id": chave, "nome": cfg_idx[chave].get("nome", chave), "valor": val, "share_dpf": (val / out["mercado"]) if out["mercado"] else None,
                     "valor_interna": val_int, "share_dpmfi": (val_int / out["mercado_interna"]) if out["mercado_interna"] else None, "linhas": parte.height})
    out["composicao"] = comp
    out["detalhe"] = detalhe_titulo_a_titulo(merc, cfg, cfg_idx, ano, mes, f, v)
    return out


MESES_NOME = ("janeiro", "fevereiro", "março", "abril", "maio", "junho", "julho", "agosto", "setembro", "outubro", "novembro", "dezembro")
FAIXAS_PADRAO = [
    {"id": "ate_12m", "nome": "Vence em até 12 meses", "ate_meses": 12},
    {"id": "de_1_a_2_anos", "nome": "Vence de 1 a 2 anos", "ate_meses": 24},
    {"id": "de_2_a_5_anos", "nome": "Vence de 2 a 5 anos", "ate_meses": 60},
    {"id": "mais_de_5_anos", "nome": "Vence em mais de 5 anos", "ate_meses": None},
]


def familia_do_titulo(titulo: str) -> str:
    """"LFT 010325" -> "LFT"; "NTN-B 150535" -> "NTN-B"; "Global BRL 2028" -> "Global BRL"; "BIRD9172-BR" fica como está.
    A família é o nome do título sem o código de vencimento (os pedaços finais que começam com dígito)."""
    partes = (titulo or "").strip().split()
    while len(partes) > 1 and partes[-1][:1].isdigit():
        partes.pop()
    return " ".join(partes) or (titulo or "").strip() or "(sem nome)"


def faixa_de(meses_ate_vencer: int | None, faixas: list[dict], sem_venc: dict) -> dict:
    if meses_ate_vencer is None:
        return sem_venc
    for fx in faixas:
        if fx.get("ate_meses") is None or meses_ate_vencer <= int(fx["ate_meses"]):
            return fx
    return faixas[-1]


def detalhe_titulo_a_titulo(merc: pl.DataFrame, cfg: dict, cfg_idx: dict, ano: int, mes: int, f: float, v: dict) -> dict:
    """A DPF em poder do público (carteira Mercado) em células: índice x família x faixa de vencimento x período
    (mês dentro da primeira faixa, ano nas outras). O site monta os dois lados do desenho da dívida a partir daqui;
    a soma das células é o total da carteira, ao centavo."""
    faixas = [dict(x) for x in (cfg.get("faixas_vencimento") or FAIXAS_PADRAO)]
    sem_venc = dict(cfg.get("sem_vencimento") or {"id": "sem_data", "nome": "Sem data de vencimento no arquivo"})
    celulas: dict[tuple, dict] = {}
    for r in merc.select(["_idx", "_titulo", "_tipo", "_venc", "_valor"]).iter_rows(named=True):
        valor = float(r["_valor"] or 0.0) * f
        if valor == 0.0:
            continue
        venc = r["_venc"]
        meses = None if venc is None else (venc.year - ano) * 12 + (venc.month - mes)
        fx = faixa_de(meses, faixas, sem_venc)
        if venc is None:
            periodo, periodo_nome = "sem_data", sem_venc["nome"]
        elif meses <= 0:
            periodo, periodo_nome = "vencido", "já vencido ou à vista"
        elif fx is faixas[0]:
            periodo, periodo_nome = f"{venc.year}-{venc.month:02d}", f"{MESES_NOME[venc.month - 1]} de {venc.year}"
        else:
            periodo, periodo_nome = str(venc.year), str(venc.year)
        fam = familia_do_titulo(r["_titulo"])
        chave = (r["_idx"], fam, fx["id"], periodo)
        c = celulas.get(chave)
        if c is None:
            c = celulas[chave] = {"indexador": r["_idx"], "familia": fam, "faixa": fx["id"], "periodo": periodo, "periodo_nome": periodo_nome,
                                  "externa": r["_tipo"] == v["externa"], "valor": 0.0, "titulos": 0}
        c["valor"] += valor
        c["titulos"] += 1
    lista = sorted(celulas.values(), key=lambda c: (c["faixa"], c["periodo"], c["indexador"], c["familia"]))
    total = sum(c["valor"] for c in lista)
    por_faixa = []
    for fx in faixas + [sem_venc]:
        val = sum(c["valor"] for c in lista if c["faixa"] == fx["id"])
        if fx is sem_venc and val == 0.0:
            continue
        por_faixa.append({"id": fx["id"], "nome": fx["nome"], "ate_meses": fx.get("ate_meses"), "valor": val, "share": (val / total) if total else None})
    return {
        "disponivel": True,
        "mes": mes,
        "total": total,
        "indexadores": [{"id": k, "nome": c.get("nome", k)} for k, c in cfg_idx.items()],
        "faixas": por_faixa,
        "celulas": lista,
        "nota": "Cada célula soma os títulos e contratos em mercado com o mesmo índice, a mesma família (nome sem o código de vencimento), "
                "a mesma faixa de vencimento e o mesmo período (mês na primeira faixa, ano nas outras). A soma das células é a DPF em poder do público do CSV.",
    }


def execucao_do_ano(df: pl.DataFrame, cfg: dict, ano: int, mes_ref: int) -> dict:
    """Juros e principal pagos de janeiro a mes_ref. Linhas iguais em todas as colunas originais menos as de
    `duplicatas_ignorando` são contadas uma vez (o arquivo de 2025 repete a cobertura do BC sob dois "Tipo de Dívida").
    Política monetária (cobertura do resultado negativo do BC) é reconhecida pelo tipo, pela modalidade ou pelo nome
    da natureza, para não depender de qual das linhas repetidas sobrou."""
    f = _fator(cfg)
    c = cfg["colunas"]
    v = cfg["valores"]
    meses = list(c["meses"])[:mes_ref]
    originais = [x for x in df.columns if not x.startswith("_") and not x.endswith("__num") and not x.endswith("__data")]
    d = df.with_columns(_num(df, c["exercicio"]).alias("_ex"), _txt(c["movimentacao"]).alias("_mov"), _txt(c["tipo"]).alias("_tipo"))
    d = d.filter(pl.col("_ex") == ano)
    if d.height == 0:
        return {"disponivel": False, "motivo": f"a execução da DPF não tem o exercício {ano}"}
    soma_meses = pl.sum_horizontal([_num(df, m).fill_null(0.0) for m in meses]).alias("_total")
    d = d.with_columns(soma_meses)
    # até que mês o arquivo do Tesouro tem dados neste exercício (o CKAN pode estar atrasado em relação ao RREO)
    ultimo_mes_com_dados = 0
    for i, m in enumerate(c["meses"], start=1):
        if m in d.columns and d.select(_num(d, m).is_not_null().any()).item():
            ultimo_mes_com_dados = i
    ignorar = [x for x in (cfg.get("duplicatas_ignorando") or []) if x in originais]
    chave = [x for x in originais if x not in ignorar and x.strip() != ""]
    antes = d.height
    d = d.with_columns(pl.struct(chave).is_first_distinct().alias("_primeira"))
    repetidas = d.filter(~pl.col("_primeira"))
    d = d.filter(pl.col("_primeira")).drop("_primeira")
    tot = lambda x: float(x["_total"].sum()) * f  # noqa: E731
    pm_mask = pl.col("_tipo") == v["politica_monetaria"]
    if c.get("modalidade") and c["modalidade"] in d.columns:
        pm_mask = pm_mask | (_txt(c["modalidade"]) == v["politica_monetaria"])
    if v.get("cobertura_bc_regex") and c.get("natureza") and c["natureza"] in d.columns:
        pm_mask = pm_mask | _txt(c["natureza"]).str.contains(v["cobertura_bc_regex"])
    d = d.with_columns(pm_mask.alias("_pm"))
    pm = d.filter(pl.col("_pm"))
    out = {
        "disponivel": True,
        "linhas": d.height,
        "meses": len(meses),
        "ultimo_mes_com_dados": ultimo_mes_com_dados,
        "juros": tot(d.filter(pl.col("_mov") == v["juros"])),
        "principal": tot(d.filter(pl.col("_mov") == v["principal"])),
        "politica_monetaria_juros": tot(pm.filter(pl.col("_mov") == v["juros"])),
        "politica_monetaria_principal": tot(pm.filter(pl.col("_mov") == v["principal"])),
        "juros_sem_politica_monetaria": tot(d.filter((pl.col("_mov") == v["juros"]) & ~pl.col("_pm"))),
        "linhas_repetidas": int(antes - d.height),
        "valor_repetido": tot(repetidas) if repetidas.height else 0.0,
        "valor_repetido_juros": tot(repetidas.filter(pl.col("_mov") == v["juros"])) if repetidas.height else 0.0,
        "valor_repetido_principal": tot(repetidas.filter(pl.col("_mov") == v["principal"])) if repetidas.height else 0.0,
        "repetidas_detalhe": [],
    }
    if repetidas.height:
        for r in repetidas.iter_rows(named=True):
            out["repetidas_detalhe"].append({"tipo": r["_tipo"], "movimentacao": r["_mov"], "natureza": str(r.get(c.get("natureza"), "") or "")[:60], "valor": float(r["_total"]) * f})
    out["por_carteira"] = por_carteira(d, c, v, f)
    return out


CARTEIRAS = (
    ("mercado", "Títulos no mercado (dívida interna)", "Juros e principal pagos a quem tem título do Tesouro no mercado: bancos, fundos, previdência privada, pessoas (Tesouro Direto) e estrangeiros."),
    ("banco_central", "Títulos na carteira do Banco Central", "Pagos ao Banco Central pelos títulos do Tesouro que ele carrega para fazer política monetária."),
    ("externa", "Títulos e contratos no exterior", "Dívida externa: títulos em dólar e euro e contratos com organismos internacionais e bancos de fora."),
    ("contratos", "Contratos (dívida interna)", "Dívida interna que não é título: contratos com bancos e fundos."),
    ("cobertura_bc", "Cobertura do resultado negativo do Banco Central", "Quando o Banco Central fecha o semestre com prejuízo, o Tesouro cobre a diferença; o orçamento registra como juros e amortização de dívida (ação 0669)."),
    ("outros", "Outras", "Linhas do arquivo do Tesouro que não casam com nenhuma das carteiras acima."),
)


def classificar_carteira(tipo: str, modalidade: str, carteira: str, pm: bool) -> str:
    """Em qual das carteiras publicadas pelo Tesouro cai uma linha do CSV de execução. Texto normalizado (maiúsculas,
    sem acento); a cobertura do BC vem antes porque o arquivo a repete sob tipos diferentes."""
    if pm:
        return "cobertura_bc"
    t, m, k = norm(tipo or ""), norm(modalidade or ""), norm(carteira or "")
    if "EXTERNA" in t:
        return "externa"
    if "CONTRAT" in m:
        return "contratos"
    if "BANCO CENTRAL" in k or "BACEN" in k or k == "BC":
        return "banco_central"
    if "MERCADO" in k or "MOBILI" in m or "INTERNA" in t:
        return "mercado"
    return "outros"


def por_carteira(d: pl.DataFrame, c: dict, v: dict, f: float) -> list[dict]:
    """Juros e principal do período por carteira (mercado, Banco Central, externa, contratos, cobertura do BC), com os
    grupos originais do arquivo (tipo, modalidade, carteira) dentro de cada uma, para o leitor ver como o Tesouro nomeia.
    Soma por carteira = total do período: nenhuma linha fica de fora (as que não casam vão para "outros")."""
    cols = {"tipo": c["tipo"], "modalidade": c.get("modalidade"), "carteira": c.get("carteira")}
    presentes = {k: col for k, col in cols.items() if col and col in d.columns}
    exprs = [(_txt(col) if k != "tipo" else pl.col("_tipo")).alias(f"_{k}") for k, col in presentes.items()]
    dd = d.with_columns(exprs) if exprs else d
    for k in cols:
        if f"_{k}" not in dd.columns:
            dd = dd.with_columns(pl.lit("").alias(f"_{k}"))
    g = dd.group_by(["_tipo", "_modalidade", "_carteira", "_pm", "_mov"]).agg(pl.col("_total").sum().alias("v"))
    acum: dict[str, dict] = {}
    for r in g.iter_rows(named=True):
        cid = classificar_carteira(r["_tipo"], r["_modalidade"], r["_carteira"], bool(r["_pm"]))
        item = acum.setdefault(cid, {"juros": 0.0, "principal": 0.0, "grupos": {}})
        campo = "juros" if r["_mov"] == v["juros"] else ("principal" if r["_mov"] == v["principal"] else None)
        if campo is None:
            continue
        val = float(r["v"] or 0.0) * f
        item[campo] += val
        chave = (r["_tipo"], r["_modalidade"], r["_carteira"])
        grp = item["grupos"].setdefault(chave, {"tipo": r["_tipo"], "modalidade": r["_modalidade"], "carteira": r["_carteira"], "juros": 0.0, "principal": 0.0})
        grp[campo] += val
    out = []
    for cid, nome, definicao in CARTEIRAS:
        if cid not in acum:
            continue
        it = acum[cid]
        grupos = sorted(it["grupos"].values(), key=lambda x: -(x["juros"] + x["principal"]))
        out.append({"id": cid, "nome": nome, "definicao": definicao, "juros": it["juros"], "principal": it["principal"], "grupos_originais": grupos})
    return out


def tesouro_direto_em(df: pl.DataFrame, cfg: dict, ano: int, mes: int) -> dict:
    f = _fator(cfg)
    c = cfg["colunas"]
    d = df.with_columns(_data(df, c["mes"]).alias("_mes"), _num(df, c["valor"]).alias("_valor"), _txt(c["tipo"]).alias("_tipo"))
    d = d.filter((pl.col("_mes").dt.year() == ano) & (pl.col("_mes").dt.month() == mes))
    if d.height == 0:
        return {"disponivel": False, "motivo": f"o CSV do Tesouro Direto não tem {mes:02d}/{ano}"}
    g = d.group_by("_tipo").agg(pl.col("_valor").sum().alias("v")).sort("v", descending=True)
    return {"disponivel": True, "total": float(d["_valor"].sum()) * f, "por_tipo": [{"tipo": t, "valor": float(x) * f} for t, x in zip(g["_tipo"].to_list(), g["v"].to_list())]}


def bcb_valores(bcb: pl.DataFrame | None, papeis: dict, ano: int, mes_ref: int) -> dict:
    """Por papel: fluxos somados de janeiro a mes_ref; saldos e taxas no último mês disponível até mes_ref (e o saldo de
    dezembro do ano anterior, para variações). Valores em reais quando a unidade é R$ milhões."""
    out: dict = {}
    if bcb is None:
        return out
    for papel, codigo in (papeis or {}).items():
        d = bcb.filter(pl.col("codigo") == int(codigo)).sort("data")
        if d.height == 0:
            out[papel] = {"disponivel": False, "motivo": f"série {codigo} não está no staging do BCB"}
            continue
        tipo = d["tipo"][0]
        unidade = d["unidade"][0] or ""
        f = 1e6 if "milh" in unidade.lower() else 1.0
        ano_d = d.filter(pl.col("data").dt.year() == ano).filter(pl.col("data").dt.month() <= mes_ref)
        ant = d.filter((pl.col("data").dt.year() == ano - 1) & (pl.col("data").dt.month() == 12))
        item = {"disponivel": ano_d.height > 0, "codigo": int(codigo), "nome": d["nome"][0], "titulo_bc": d["titulo_bc"][0], "tipo": tipo, "unidade": "reais" if f != 1.0 else unidade,
                "meses": ano_d.height, "ultimo_mes": int(ano_d["data"].max().month) if ano_d.height else None}
        if ano_d.height == 0:
            item["motivo"] = f"série {codigo} não tem {ano}"
        elif tipo == "fluxo":
            item["valor"] = float(ano_d["valor"].sum()) * f
        else:
            item["valor"] = float(ano_d.sort("data")["valor"][-1]) * f
            item["anterior"] = float(ant["valor"][0]) * f if ant.height else None
        out[papel] = item
    return out


def numeros_do_recibo(stg_siconfi: Path | None, linhas_cfg: dict, exercicio: int, periodo: int | None, siop_path: Path | None, config_dir: Path | None) -> dict:
    from .recibo import compute

    if stg_siconfi is None or not Path(stg_siconfi).exists():
        return {"disponivel": False, "motivo": "staging do RREO ausente: rode `recibo fetch siconfi` e `recibo transform siconfi`"}
    if not periodo:
        return {"disponivel": False, "motivo": f"nenhum período do RREO de {exercicio} no índice"}
    try:
        res = compute(Path(stg_siconfi), linhas_cfg, exercicio, periodo, siop_path=siop_path, config_dir=config_dir)
    except SemDados as e:
        return {"disponivel": False, "motivo": str(e)}
    a06 = res.anexo06
    r01 = getattr(res, "anexo01_receitas", None) or {}
    servico = next((x.valor for x in res.excluidos if x.id == "servico_divida_bruto"), 0.0)
    juros = next((x.valor for x in res.linhas if x.id == "juros"), 0.0)
    return {
        "disponivel": True,
        "periodo": periodo,
        "fechado": periodo >= 6,
        "titulos_vendidos": (float(r01.get("operacoes_credito") or 0.0) + float(r01.get("refinanciamento") or 0.0)) if r01 else None,
        "receita_operacoes_credito": r01.get("operacoes_credito"),
        "receita_refinanciamento": r01.get("refinanciamento"),
        "juros_pagos": juros,
        "amortizacao": res.anexo01.get("amortizacao", 0.0),
        "refinanciamento": res.anexo01.get("refinanciamento", 0.0),
        "servico_843_844": servico,
        "fora_843_844": (juros + res.anexo01.get("amortizacao", 0.0)) - servico,
        "juros_brutos_xi": a06.get("juros_passivos:valor_incorrido"),
        "juros_recebidos_x": a06.get("juros_ativos:valor_incorrido"),
        "dc_inicial": a06.get("divida_consolidada:saldo_inicial"),
        "dc_final": a06.get("divida_consolidada:saldo_final"),
        "dcl_inicial": a06.get("divida_liquida:saldo_inicial"),
        "dcl_final": a06.get("divida_liquida:saldo_final"),
        "nominal_acima": a06.get("nominal_acima:valor"),
        "nominal_abaixo": a06.get("nominal_abaixo:valor"),
        "primario_acima": a06.get("primario_acima:valor"),
        "primario_abaixo": a06.get("primario_abaixo:valor"),
        "total_recibo": res.total_recibo,
        "publicavel": res.publicavel,
    }


def juros_do_cofog(staging_dir: Path, linhas_cfg: dict, exercicio: int) -> dict:
    from ..transform.cofog import STAGING_FILE as COFOG_FILE
    from .cofog import classificar_ajustes, ler_ano

    p = Path(staging_dir) / COFOG_FILE
    if not p.exists():
        return {"disponivel": False, "motivo": "staging do COFOG ausente"}
    try:
        df = ler_ano(p, exercicio)
    except SemDados as e:
        return {"disponivel": False, "motivo": str(e)}
    somam = ((linhas_cfg.get("cofog") or {}).get("ponte") or {}).get("somam") or {}
    aj = classificar_ajustes(df, somam)
    juros = sum(a.valor for a in aj if a.item == "juros")
    return {"disponivel": True, "valor": juros, "snapshot_id": df["snapshot_id"][0], "arquivo": df["arquivo"][0]}


# --------------------------------------------------------------------------------------------------------------------
# o mart
# --------------------------------------------------------------------------------------------------------------------


def _mes_referencia(fontes: Fontes, cfg: dict, ano: int) -> tuple[int, dict[str, int | None]]:
    """O último mês do ano presente em todas as fontes mensais (12 em ano fechado)."""
    ultimos: dict[str, int | None] = {}
    for nome in ("estoque_xlsx", "fatores_xlsx"):
        pl_ = fontes.planilhas.get(nome)
        ultimos[nome] = pl_.ultimo_mes(ano) if pl_ else None
    for nome in ("estoque_csv", "tesouro_direto_csv"):
        df = fontes.tabelas.get(nome)
        if df is not None:
            ms = meses_disponiveis_csv(df, cfg[nome]["colunas"]["mes"], ano)
            ultimos[nome] = ms[-1] if ms else None
        else:
            ultimos[nome] = None
    if fontes.bcb is not None:
        d = fontes.bcb.filter((pl.col("data").dt.year() == ano) & (pl.col("tipo") == "saldo"))
        ultimos["bcb"] = int(d["data"].max().month) if d.height else None
    presentes = [m for m in ultimos.values() if m]
    return (min(presentes) if presentes else 0), ultimos


def compute_divida(
    staging_dir: Path,
    linhas_cfg: dict,
    fontes_cfg: dict,
    exercicio: int,
    stg_siconfi: Path | None = None,
    periodo_recibo: int | None = None,
    siop_path: Path | None = None,
    config_dir: Path | None = None,
    stg_ibge: Path | None = None,
) -> ResultadoDivida:
    from .populacao import carregar_populacao, por_habitante

    cfg = linhas_cfg.get("divida") or {}
    if not cfg:
        raise SemDados("linhas.yaml não tem a seção `divida`")
    fontes = Fontes(staging_dir, cfg)
    if not fontes.planilhas and not fontes.tabelas:
        raise SemDados("staging da dívida vazio ou ausente: rode `recibo fetch divida` e `recibo transform divida`")
    mes_ref, ultimos = _mes_referencia(fontes, cfg, exercicio)
    if mes_ref == 0:
        raise SemDados(f"nenhuma fonte da dívida tem o ano {exercicio} (últimos meses por fonte: {ultimos})")
    fechado = mes_ref == 12
    avisos: list[str] = []
    for nome, m in ultimos.items():
        if m is not None and m != mes_ref:
            avisos.append(f"{nome} vai até o mês {m}; o mart usa o mês {mes_ref}, o último presente em todas as fontes")
    for nome, erro in fontes.erros.items():
        avisos.append(f"fonte indisponível: {nome}: {erro}")

    recs: list[ReconciliacaoDivida] = []
    cfg_rec = {r["id"]: r for r in cfg.get("reconciliacoes", [])}

    def add(rid: str, esperado: float | None, obtido: float | None, detalhe: str = "", escala: float | None = None, nao_comparavel: str | None = None):
        c = cfg_rec.get(rid, {})
        critica = bool(c.get("critica", False))
        if nao_comparavel:
            # os dois lados existem, mas não cobrem os mesmos meses: não é falha de número, é falta de base comum
            recs.append(ReconciliacaoDivida(rid, False, critica, esperado or 0.0, obtido or 0.0, 0.0, c.get("descricao", ""), f"não comparável: {nao_comparavel}", calculada=False))
            return
        if esperado is None or obtido is None:
            lado = "esperado" if esperado is None else "obtido"
            recs.append(ReconciliacaoDivida(rid, False, critica, esperado or 0.0, obtido or 0.0, 0.0, c.get("descricao", ""), f"não calculada: fonte indisponível (lado {lado})" + (f"; {detalhe}" if detalhe else ""), calculada=False))
            return
        diff = obtido - esperado
        if c.get("tolerancia_absoluta") is not None:
            ok = abs(diff) <= float(c["tolerancia_absoluta"])
        else:
            tol = tolerancia(esperado, float(c.get("tolerancia_relativa", 0.0)) if c.get("tolerancia_relativa") is not None else None, escala or abs(esperado) or 1.0)
            ok = abs(diff) <= tol
        recs.append(ReconciliacaoDivida(rid, ok, critica, esperado, obtido, diff, c.get("descricao", ""), detalhe))

    # ---- fontes por ano
    fat = None
    if "fatores_xlsx" in fontes.planilhas:
        try:
            fat = fatores_do_ano(fontes.planilhas["fatores_xlsx"], cfg["fatores_xlsx"], exercicio, mes_ref)
        except GradeFormato as e:
            avisos.append(f"fatores: {e}")
    est = est_ant = None
    if "estoque_xlsx" in fontes.planilhas:
        try:
            est = estoque_xlsx_em(fontes.planilhas["estoque_xlsx"], cfg["estoque_xlsx"], exercicio, mes_ref)
            est_ant = estoque_xlsx_em(fontes.planilhas["estoque_xlsx"], cfg["estoque_xlsx"], exercicio - 1, 12)
        except GradeFormato as e:
            avisos.append(f"estoque (planilha): {e}")
    def tabela(nome: str, fn, *args) -> dict:
        """Roda o cálculo de um CSV; coluna fora do esperado vira motivo (e aviso), não exceção."""
        if nome not in fontes.tabelas:
            return {"disponivel": False, "motivo": fontes.erros.get(nome)}
        try:
            return fn(fontes.tabelas[nome], cfg[nome], *args)
        except GradeFormato as e:
            avisos.append(f"{nome}: {e}")
            return {"disponivel": False, "motivo": str(e)}

    csv_est = tabela("estoque_csv", estoque_csv_em, exercicio, mes_ref)
    exe = tabela("execucao_csv", execucao_do_ano, exercicio, mes_ref)
    td = tabela("tesouro_direto_csv", tesouro_direto_em, exercicio, mes_ref)
    bcb = bcb_valores(fontes.bcb, cfg.get("bcb") or {}, exercicio, mes_ref)
    rec = numeros_do_recibo(stg_siconfi, linhas_cfg, exercicio, periodo_recibo, siop_path, config_dir)
    cof = juros_do_cofog(staging_dir, linhas_cfg, exercicio)
    # a execução da DPF comparada com o RREO precisa cobrir os mesmos meses: o Anexo 01 vai até o fim do bimestre p
    # (mês 2p); num ano em andamento o CSV do Tesouro pode ir além (ou aquém) disso
    exe_rec = exe
    meses_rec = min(mes_ref, 2 * int(rec["periodo"])) if rec.get("disponivel") and rec.get("periodo") else mes_ref
    if meses_rec != mes_ref and exe.get("disponivel"):
        exe_rec = tabela("execucao_csv", execucao_do_ano, exercicio, meses_rec)

    def b(papel: str) -> float | None:
        it = bcb.get(papel) or {}
        return it.get("valor") if it.get("disponivel") else None

    def b_ant(papel: str) -> float | None:
        it = bcb.get(papel) or {}
        return it.get("anterior") if it.get("disponivel") else None

    def b_motivo(papel: str) -> str:
        it = bcb.get(papel) or {}
        return it.get("motivo") or fontes.erros.get("bcb") or f"série do papel {papel!r} indisponível"

    def b_motivo_variacao(papel: str) -> str:
        it = bcb.get(papel) or {}
        if it.get("disponivel") and it.get("anterior") is None:
            return f"série {it.get('codigo')} sem o saldo de dezembro de {exercicio - 1}; a variação precisa do ponto de partida"
        return b_motivo(papel)

    def b_fonte(papel: str, oque: str = "") -> str:
        it = bcb.get(papel) or {}
        base = f"Banco Central, SGS {it.get('codigo', cfg.get('bcb', {}).get(papel))}"
        if it.get("titulo_bc"):
            base += f" ({it['titulo_bc']})"
        if it.get("disponivel") and it.get("ultimo_mes") and it["ultimo_mes"] != mes_ref:
            base += f"; a série vai só até o mês {it['ultimo_mes']:02d}/{exercicio} (o mart usa o mês {mes_ref:02d})"
            aviso = f"BCB {it.get('codigo')}: último mês {it['ultimo_mes']:02d}/{exercicio}, antes do mês de referência {mes_ref:02d}"
            if aviso not in avisos:
                avisos.append(aviso)
        return base + (f"; {oque}" if oque else "")

    periodo_txt = f"até o mês {mes_ref:02d}/{exercicio}" if not fechado else f"{exercicio}"
    fonte_fat = f"Tesouro, Fatores de Variação da DPF (Anexo 2.9), meses 1 a {mes_ref} de {exercicio}, R$ milhões"
    fonte_est = f"Tesouro, Estoque da DPF (Anexo 2.1), posição de {mes_ref:02d}/{exercicio}, R$ bilhões"
    fonte_csv = f"Tesouro, Estoque da DPF título a título (CSV), posição de {mes_ref:02d}/{exercicio}, reais"
    fonte_exe = f"Tesouro, Execução Orçamentária e Financeira da DPF por natureza (CSV), {periodo_txt}, reais"
    fonte_rec = f"RREO da União via recibo (Anexos 01 e 06, {exercicio}/p{rec.get('periodo') or '-'})"

    medidas: dict[str, list[Medida]] = {g: [] for g in GRUPOS}

    def med(grupo: str, id_: str, nome: str, valor: float | None, unidade: str, fonte: str, definicao: str, motivo: str | None = None, componentes: list[dict] | None = None):
        medidas[grupo].append(Medida(id_, nome, valor, unidade, fonte, definicao, valor is not None, None if valor is not None else (motivo or "fonte indisponível"), componentes or []))

    # ---- 1. juros do ano
    med("juros", "juros_pagos", "Pagos em dinheiro pelo orçamento (a linha do recibo)", rec.get("juros_pagos") if rec.get("disponivel") else None, "reais", fonte_rec,
        "O que saiu do caixa em juros e encargos da dívida no ano (Anexo 01, liquidado). É a régua do recibo: dinheiro que saiu.", rec.get("motivo"))
    med("juros", "juros_execucao_dpf", "Pagos em dinheiro pela execução da DPF (Tesouro)", exe.get("juros") if exe.get("disponivel") else None, "reais", fonte_exe,
        "Juros e encargos pagos da Dívida Pública Federal, grupo 2, inclusive a cobertura do resultado negativo do Banco Central. Deve ser igual à linha do recibo, a menos de juros fora da DPF.", exe.get("motivo"),
        ([{"id": "politica_monetaria_juros", "nome": "dos quais cobertura do resultado negativo do BC (ação 0669)", "valor": exe.get("politica_monetaria_juros")}]
         + ([{"id": "linhas_repetidas", "nome": f"juros em linhas repetidas no arquivo do Tesouro, contados uma vez ({exe.get('linhas_repetidas')} linhas; principal repetido {exe.get('valor_repetido_principal', 0.0):,.2f})", "valor": exe.get("valor_repetido_juros")}] if exe.get("linhas_repetidas") else [])) if exe.get("disponivel") else None)
    med("juros", "juros_apropriados_dpf", "Apropriados no estoque da DPF (Tesouro)", fat["juros"] if fat else None, "reais", fonte_fat,
        "Os juros que a dívida do Tesouro gerou no período, pagos ou não; na externa, inclui a variação do câmbio. É quanto a dívida cresceu só por juros.", fontes.erros.get("fatores_xlsx"),
        [{"id": "juros_dpmfi", "nome": "dívida interna (DPMFi)", "valor": fat["juros_dpmfi"]}, {"id": "juros_dpfe", "nome": "dívida externa (DPFe), com câmbio (negativo quando o real se valoriza)", "valor": fat["juros_dpfe"]}] if fat else None)
    med("juros", "juros_bcb_gf", "Nominais pelo Banco Central, Governo Federal (NFSP, líquidos)", b("juros_gf"), "reais", b_fonte("juros_gf", f"soma dos meses 1 a {mes_ref}"),
        "Juros devidos menos juros recebidos pelo Governo Federal (Tesouro e INSS), pela conta de necessidades de financiamento do BC. Líquidos, por isso menores que os brutos.", b_motivo("juros_gf"))
    med("juros", "juros_bcb_gc", "Nominais pelo Banco Central, Governo Central (Governo Federal e BC)", b("juros_gc"), "reais", b_fonte("juros_gc", f"soma dos meses 1 a {mes_ref}"),
        "A mesma conta incluindo o próprio Banco Central (por exemplo, o custo das operações de câmbio).", b_motivo("juros_gc"))
    med("juros", "juros_rreo_brutos", "Por competência no RREO, brutos (Anexo 06, XI)", rec.get("juros_brutos_xi") if rec.get("disponivel") else None, "reais", fonte_rec,
        "Juros, encargos e variações monetárias que a dívida gerou no ano, pela competência, sem descontar o que o governo recebeu.", rec.get("motivo"))
    jl = (rec.get("juros_brutos_xi") or 0.0) - (rec.get("juros_recebidos_x") or 0.0) if rec.get("disponivel") and rec.get("juros_brutos_xi") is not None else None
    med("juros", "juros_rreo_liquidos", "Por competência no RREO, líquidos (XI menos X)", jl, "reais", fonte_rec,
        "Os brutos menos os juros que o governo recebeu de quem lhe deve e do caixa aplicado (Anexo 06, X).", rec.get("motivo"))
    med("juros", "juros_cofog", "No COFOG (número informado pelo BC ao Tesouro)", cof.get("valor") if cof.get("disponivel") else None, "reais",
        f"Tesouro, base COFOG {exercicio}, linha de ajuste JUROS" + (f" (snapshot {cof.get('snapshot_id')})" if cof.get("disponivel") else ""),
        "O que a base COFOG usa como juros. Não é reconstruível pelas séries abertas do BC nem pelas planilhas do Tesouro; fica nomeado como está.", cof.get("motivo"))

    # ---- 2. dívida nova
    comp_fat = None
    if fat:
        comp_fat = [
            {"id": "estoque_anterior", "nome": f"estoque no fim de {exercicio - 1}", "valor": fat["estoque_anterior"]},
            {"id": "emissoes", "nome": "mais emissões (dívida nova vendida)", "valor": fat["emissoes"]},
            {"id": "resgates", "nome": "menos resgates (dívida paga)", "valor": fat["resgates"]},
            {"id": "juros", "nome": "mais juros apropriados", "valor": fat["juros"]},
            {"id": "operacoes_bc", "nome": "mais operações de mercado com o BC", "valor": fat["operacoes_bc"]},
            {"id": "estoque_fim", "nome": f"estoque em {mes_ref:02d}/{exercicio}", "valor": fat["estoque_fim"]},
        ]
    var_dpf = (fat["estoque_fim"] - fat["estoque_anterior"]) if fat and fat["estoque_fim"] is not None and fat["estoque_anterior"] is not None else None
    med("divida_nova", "variacao_dpf", "Variação da DPF em poder do público (Tesouro)", var_dpf, "reais", fonte_fat,
        "Quanto a dívida do Tesouro cresceu no período, aberta como o Tesouro abre: emissões menos resgates, mais juros apropriados, mais operações com o BC.", fontes.erros.get("fatores_xlsx"), comp_fat)
    var_dcl = (rec["dcl_final"] - rec["dcl_inicial"]) if rec.get("disponivel") and rec.get("dcl_final") is not None and rec.get("dcl_inicial") is not None else None
    med("divida_nova", "variacao_dcl_rreo", "Variação da dívida consolidada líquida (RREO)", var_dcl, "reais", fonte_rec,
        "A medida da Lei de Responsabilidade Fiscal: dívida consolidada menos caixa e haveres, no fim do ano menos no início. É o que o RREO chama de resultado nominal abaixo da linha, com o sinal trocado.", rec.get("motivo"))
    var_dlsp = (b("dlsp_gf") - b_ant("dlsp_gf")) if b("dlsp_gf") is not None and b_ant("dlsp_gf") is not None else None
    med("divida_nova", "variacao_dlsp_gf", "Variação da dívida líquida do Governo Federal (BC)", var_dlsp, "reais", b_fonte("dlsp_gf", f"saldo de {mes_ref:02d}/{exercicio} menos dezembro de {exercicio - 1}"),
        "Pela conta do Banco Central: o que o Governo Federal deve menos o que tem a receber, no fim do período menos no fim do ano anterior.", b_motivo_variacao("dlsp_gf"))
    med("divida_nova", "nominal_bcb_gc", "Resultado nominal do Governo Central pelo BC (NFSP)", b("nominal_gc"), "reais", b_fonte("nominal_gc", f"soma dos meses 1 a {mes_ref}"),
        "O déficit completo, com juros, medido pelo Banco Central pela variação da dívida líquida (positivo = déficit).", b_motivo("nominal_gc"))
    med("divida_nova", "primario_bcb_gc", "Resultado primário do Governo Central pelo BC (NFSP)", b("primario_gc"), "reais", b_fonte("primario_gc", f"soma dos meses 1 a {mes_ref}"),
        "O déficit antes dos juros, pela conta do Banco Central (positivo = déficit). Compare com o acima da linha do Tesouro no recibo.", b_motivo("primario_gc"))

    # ---- 3. estoque
    med("estoque", "dpf_publico", "DPF em poder do público (Tesouro)", est.get("dpf_publico") if est else None, "reais", fonte_est,
        "Tudo o que o Tesouro deve fora do Banco Central: títulos no mercado e contratos. A medida de estoque do próprio Tesouro.", fontes.erros.get("estoque_xlsx"),
        [{"id": "dpmfi", "nome": "interna em títulos (DPMFi)", "valor": est.get("dpmfi")}, {"id": "dpfe", "nome": "externa (DPFe)", "valor": est.get("dpfe")}] if est else None)
    med("estoque", "dpf_publico_csv", "DPF em poder do público, título a título (CSV do Tesouro)", csv_est.get("mercado") if csv_est.get("disponivel") else None, "reais", fonte_csv,
        "A mesma dívida somada título por título e contrato por contrato (carteira Mercado). Tem que dar o mesmo número da planilha.", csv_est.get("motivo"),
        [{"id": "mercado_interna", "nome": "interna", "valor": csv_est.get("mercado_interna")}, {"id": "mercado_externa", "nome": "externa", "valor": csv_est.get("mercado_externa")}] if csv_est.get("disponivel") else None)
    med("estoque", "carteira_bc", "Títulos do Tesouro na carteira do Banco Central", est.get("dpmfi_bc") if est else None, "reais", fonte_est,
        "Dívida do Tesouro na mão do BC, usada na política monetária. Não entra na DPF em poder do público.", fontes.erros.get("estoque_xlsx"),
        [{"id": "carteira_bc_csv", "nome": "no CSV título a título", "valor": csv_est.get("banco_central")}, {"id": "carteira_bc_bcb", "nome": "na série 4152 do BC", "valor": b("mobiliaria_bc")}])
    med("estoque", "dc_rreo", "Dívida consolidada (RREO, Anexo 06, XIII)", rec.get("dc_final") if rec.get("disponivel") else None, "reais", fonte_rec,
        "A dívida bruta na régua da Lei de Responsabilidade Fiscal, no fim do período.", rec.get("motivo"))
    med("estoque", "dcl_rreo", "Dívida consolidada líquida (RREO, Anexo 06, XVI)", rec.get("dcl_final") if rec.get("disponivel") else None, "reais", fonte_rec,
        "A consolidada menos caixa e haveres financeiros, no fim do período.", rec.get("motivo"))
    med("estoque", "dlsp_gf", "Dívida líquida do Governo Federal (BC)", b("dlsp_gf"), "reais", b_fonte("dlsp_gf", f"saldo de {mes_ref:02d}/{exercicio}"),
        "O que o Governo Federal deve menos o que tem a receber e em caixa, pela conta do Banco Central.", b_motivo("dlsp_gf"))
    med("estoque", "dlsp_gf_pib", "Dívida líquida do Governo Federal, em % do PIB (BC)", b("dlsp_gf_pib"), "% do PIB", b_fonte("dlsp_gf_pib"), "A mesma medida dividida pelo tamanho da economia.", b_motivo("dlsp_gf_pib"))
    med("estoque", "dlsp_consolidado", "Dívida líquida do setor público (BC)", b("dlsp_consolidado"), "reais", b_fonte("dlsp_consolidado", f"saldo de {mes_ref:02d}/{exercicio}"),
        "União, estados, municípios e estatais, líquida, pela conta do Banco Central.", b_motivo("dlsp_consolidado"))
    med("estoque", "dlsp_consolidado_pib", "Dívida líquida do setor público, em % do PIB (BC)", b("dlsp_consolidado_pib"), "% do PIB", b_fonte("dlsp_consolidado_pib"), "", b_motivo("dlsp_consolidado_pib"))
    med("estoque", "dbgg_pib", "Dívida bruta do governo geral, em % do PIB (BC)", b("dbgg_pib"), "% do PIB", b_fonte("dbgg_pib"),
        "União, estados e municípios, bruta (sem descontar caixa e haveres), em % do PIB. A medida mais citada no noticiário.", b_motivo("dbgg_pib"))
    med("estoque", "tesouro_direto", "Tesouro Direto (pessoas físicas)", td.get("total") if td.get("disponivel") else None, "reais", f"Tesouro, Estoque do Tesouro Direto (CSV), posição de {mes_ref:02d}/{exercicio}",
        "A parte da dívida interna comprada diretamente por pessoas físicas. É a única fatia de detentores com fonte aberta; o restante (bancos, fundos, previdência, estrangeiros) está só no Relatório Mensal da Dívida, em PDF.", td.get("motivo"),
        [{"id": t["tipo"], "nome": t["tipo"], "valor": t["valor"]} for t in td.get("por_tipo", [])] if td.get("disponivel") else None)

    # ---- 4. rolagem
    vinc = csv_est.get("vincendo") if csv_est.get("disponivel") else None
    n_vinc = cfg['estoque_csv'].get('vincendo_meses', 12)
    med("rolagem", "vincendo_12m", f"Fatia da DPF em poder do público cujo principal vence em {n_vinc} meses", (vinc["share_dpf"] * 100) if vinc and vinc.get("share_dpf") is not None else None, "%",
        fonte_csv + (f"; vencimentos até {vinc['ate']}" if vinc else ""),
        f"Valor do estoque dos títulos e contratos em mercado cujo vencimento final cai nos próximos {n_vinc} meses, dividido pela DPF em poder do público: o principal que precisa ser pago ou rolado logo. "
        "Não é o 'percentual vincendo em 12 meses' do Relatório Mensal da Dívida: lá o Tesouro divide pelo estoque o fluxo de principal e juros (cupons) dos 12 meses seguintes, trazido a valor presente, e o número sai maior.", csv_est.get("motivo"),
        [{"id": "vincendo_valor", "nome": "valor que vence", "valor": vinc["valor"]}, {"id": "vincendo_linhas", "nome": "títulos e contratos", "valor": vinc["linhas"]}] if vinc else None)
    med("rolagem", "prazo_medio_bcb", "Prazo médio da dívida mobiliária federal (BC)", b("prazo_medio"), "meses", b_fonte("prazo_medio"),
        "Quanto tempo, em média, falta para os títulos do Tesouro em mercado vencerem, pela série do Banco Central (dívida mobiliária interna). O Tesouro publica a própria medida, para a DPF inteira e ponderando fluxos de principal e juros a valor presente, só no Relatório Mensal da Dívida (PDF); por isso os dois números diferem.", b_motivo("prazo_medio"))
    composicao = csv_est.get("composicao", []) if csv_est.get("disponivel") else []
    for c in composicao:
        med("rolagem", f"indexador_{c['id']}", f"Fatia da DPF em poder do público: {c['nome']}", (c["share_dpf"] * 100) if c.get("share_dpf") is not None else None, "%", fonte_csv,
            "Classificação pelo começo do nome do título (regras em linhas.yaml); a dívida externa que não casa com nenhuma regra conta como câmbio (o Global BRL, emitido em reais, é prefixado). "
            "O Tesouro classifica cada papel pelo indexador do contrato (TDA pela TR, securitizadas pelo IGP-DI ou em dólar), que o nome não revela: a composição do Relatório Mensal difere daqui em até 0,1 ponto percentual.", None,
            [{"id": "valor", "nome": "valor", "valor": c["valor"]}, {"id": "share_dpmfi", "nome": "fatia da dívida interna em mercado (DPMFi), em %", "valor": (c["share_dpmfi"] * 100) if c.get("share_dpmfi") is not None else None}])
    med("rolagem", "indexador_selic_bcb", "Fatia da dívida mobiliária atrelada à Selic (BC)", b("indexador_selic"), "%", b_fonte("indexador_selic"), "Participação por indexador na posição em carteira, pelo Banco Central (dívida interna em mercado).", b_motivo("indexador_selic"))
    med("rolagem", "indexador_prefixado_bcb", "Fatia prefixada da dívida mobiliária (BC)", b("indexador_prefixado"), "%", b_fonte("indexador_prefixado"), "", b_motivo("indexador_prefixado"))
    med("rolagem", "indexador_cambio_bcb", "Fatia cambial da dívida mobiliária interna (BC)", b("indexador_cambio"), "%", b_fonte("indexador_cambio"),
        "Só a dívida interna em mercado (a externa fica fora desta série). Compare com a fatia da DPMFi em câmbio acima: a diferença são papéis internos em dólar que o nome não revela.", b_motivo("indexador_cambio"))
    med("rolagem", "colchao_liquidez", "Colchão de liquidez do Tesouro", None, "reais", "Relatório Mensal da Dívida (PDF); sem fonte aberta em planilha ou API",
        "A reserva em dinheiro que o Tesouro mantém para pagar vencimentos sem depender do mercado por alguns meses.", "sem fonte aberta: o Tesouro publica só no Relatório Mensal da Dívida, em PDF")
    med("rolagem", "detentores", "Detentores da dívida por classe (bancos, fundos, previdência, estrangeiros)", None, "reais", "Relatório Mensal da Dívida (PDF); sem fonte aberta em planilha ou API",
        "Quem são os credores, por tipo. A única fatia aberta é o Tesouro Direto (pessoas físicas), no grupo de estoque.", "sem fonte aberta: só no Relatório Mensal da Dívida, em PDF")

    # ---- 5. por habitante (S14)
    pop = carregar_populacao(stg_ibge, linhas_cfg, exercicio)
    fonte_pop = (f"IBGE, projeção revisão {pop.get('revisao')}, população em {pop.get('data_referencia')} ({pop['valor']:,} pessoas; snapshot {pop['fonte']['snapshot_id']})" if pop.get("disponivel") else "IBGE (S14)")
    motivo_pop = None if pop.get("disponivel") else pop.get("motivo")
    med("por_habitante", "populacao", f"População do Brasil em {pop.get('data_referencia') or exercicio} (IBGE)", float(pop["valor"]) if pop.get("disponivel") else None, "pessoas", fonte_pop,
        "A régua do per capita: a projeção revisão 2024 do IBGE, população em 1º de julho. As estimativas oficiais (TCU) saem dela de 2024 em diante; o Censo 2022 contou menos porque a projeção corrige a cobertura.", motivo_pop,
        ([{"id": "estimativa", "nome": f"estimativa oficial (SIDRA {pop['estimativa']['tabela']})", "valor": int(pop["estimativa"]["valor"])}] if pop.get("estimativa") else [])
        + ([{"id": "censo", "nome": f"Censo {pop['censo']['ano']} (contagem, sem correção de cobertura)", "valor": int(pop["censo"]["valor"])}] if pop.get("censo") else []) if pop.get("disponivel") else None)
    med("por_habitante", "dpf_publico_por_habitante", "DPF em poder do público por habitante (Tesouro)", por_habitante(est.get("dpf_publico") if est else None, pop), "reais por habitante", f"{fonte_est}; {fonte_pop}",
        "O que o Tesouro deve fora do Banco Central, dividido pela população. Média, não o que cada um deve.", motivo_pop or fontes.erros.get("estoque_xlsx"))
    med("por_habitante", "variacao_dpf_por_habitante", "Dívida nova no ano por habitante (variação da DPF, Tesouro)", por_habitante(var_dpf, pop), "reais por habitante", f"{fonte_fat}; {fonte_pop}",
        "Quanto a DPF em poder do público cresceu no período, por pessoa.", motivo_pop or fontes.erros.get("fatores_xlsx"))
    med("por_habitante", "dlsp_gf_por_habitante", "Dívida líquida do Governo Federal por habitante (BC)", por_habitante(b("dlsp_gf"), pop), "reais por habitante", f"{b_fonte('dlsp_gf')}; {fonte_pop}",
        "O que o Governo Federal deve menos o que tem a receber, pela conta do Banco Central, por pessoa.", motivo_pop or b_motivo("dlsp_gf"))
    med("por_habitante", "dcl_rreo_por_habitante", "Dívida consolidada líquida por habitante (RREO)", por_habitante(rec.get("dcl_final") if rec.get("disponivel") else None, pop), "reais por habitante", f"{fonte_rec}; {fonte_pop}",
        "A medida da Lei de Responsabilidade Fiscal, por pessoa. É a mesma conta do memo 'imposto futuro' do recibo.", motivo_pop or rec.get("motivo"))
    med("por_habitante", "juros_pagos_por_habitante", "Juros pagos no ano por habitante (a linha do recibo)", por_habitante(rec.get("juros_pagos") if rec.get("disponivel") else None, pop), "reais por habitante", f"{fonte_rec}; {fonte_pop}",
        "O que saiu do caixa em juros no período, por pessoa.", motivo_pop or rec.get("motivo"))

    # ---- reconciliações
    if fat:
        esperado = (fat["estoque_anterior"] or 0.0) + fat["emissoes"] + fat["resgates"] + fat["juros"] + fat["operacoes_bc"]
        add("dpf_fatores_identidade", esperado, fat["estoque_fim"], f"meses {fat['meses'][0]} a {fat['meses'][-1]}; variação nominal da planilha {fat['variacao']:,.2f}", escala=fat["estoque_fim"])
        if fat["coluna_anual"].get("juros") is not None and fechado:
            add("fatores_coluna_anual_vs_soma_meses", fat["coluna_anual"]["juros"], fat["juros"], "juros apropriados: coluna anual do Tesouro contra a soma dos doze meses", escala=fat["juros"])
    else:
        add("dpf_fatores_identidade", None, None)
    # títulos vendidos pelo RREO (receitas de operações de crédito + refinanciamento, Anexo 01) contra as emissões da DPF
    # em mercado (fatores do Tesouro): medem coisas parecidas, não iguais (títulos emitidos direto ao BC, trocas, prazos
    # de liquidação); a diferença fica à vista, em investigação (página Perguntas abertas do site)
    tit_rreo = rec.get("titulos_vendidos") if rec.get("disponivel") else None
    meses_fat = (fat or {}).get("meses") or []
    if tit_rreo is not None and fat and rec.get("periodo") and len(meses_fat) != min(mes_ref, 2 * int(rec["periodo"])):
        add("titulos_rreo_vs_emissoes_dpf", tit_rreo, fat["emissoes"], nao_comparavel=f"o RREO vai até o mês {2 * int(rec['periodo']):02d} e os fatores do Tesouro até o mês {meses_fat[-1] if meses_fat else 0:02d}")
    else:
        add("titulos_rreo_vs_emissoes_dpf", tit_rreo, fat["emissoes"] if fat else None,
            f"receitas de operações de crédito ({rec.get('receita_operacoes_credito') or 0.0:,.2f}) + refinanciamento ({rec.get('receita_refinanciamento') or 0.0:,.2f}) do Anexo 01 contra emissões da DPF em poder do público (Anexo 2.9); em investigação: ver Perguntas abertas" if tit_rreo is not None else "")
    add("dpf_estoque_xlsx_vs_csv", est.get("dpf_publico") if est else None, csv_est.get("mercado") if csv_est.get("disponivel") else None, f"posição de {mes_ref:02d}/{exercicio}")
    add("dpf_estoque_xlsx_vs_fatores", est.get("dpf_publico") if est else None, fat["estoque_fim"] if fat else None, "estoque 2.1 (bilhões) contra fatores 2.9 (milhões)")
    add("carteira_bc_tesouro_vs_bcb", csv_est.get("banco_central") if csv_est.get("disponivel") else None, b("mobiliaria_bc"), "CSV do Tesouro (carteira Banco Central) contra SGS 4152")
    add("dpmfi_mercado_tesouro_vs_bcb", est.get("dpmfi") if est else None, b("mobiliaria_mercado"), "DPMFi da planilha do Tesouro contra SGS 4154")
    bim = f"RREO até o {rec.get('periodo')}º bimestre, execução meses 1 a {exe_rec.get('meses', 0)}" if rec.get("disponivel") else "sem RREO do ano"
    # o arquivo de execução do Tesouro no CKAN pode parar antes do fim do bimestre do RREO (em 2026, publicado em agosto com
    # dados até julho, contra um RREO até agosto): aí não há meses iguais para comparar, e isso é dito, não chamado de falha
    ult_exe = int(exe.get("ultimo_mes_com_dados") or 0) if exe.get("disponivel") else None
    sem_base = None
    if rec.get("disponivel") and exe.get("disponivel") and ult_exe is not None and ult_exe < meses_rec:
        rec_exe = (fontes.ficha().get("execucao_csv") or {}).get("recurso_data")
        sem_base = (f"a execução da DPF do Tesouro tem dados até o mês {ult_exe:02d}/{exercicio}" + (f" (arquivo publicado em {str(rec_exe)[:10]})" if rec_exe else "")
                    + f"; o RREO do {rec.get('periodo')}º bimestre vai até o mês {meses_rec:02d}: sem meses iguais para comparar até o Tesouro atualizar o arquivo")
        avisos.append(f"execução da DPF {exercicio}: {sem_base}")
    if exe.get("disponivel") and ult_exe and ult_exe < mes_ref:
        for m_ in medidas["juros"]:
            if m_.id == "juros_execucao_dpf":
                m_.fonte += f"; o arquivo do Tesouro tem dados até o mês {ult_exe:02d}/{exercicio}, antes do mês de referência {mes_ref:02d}"
    add("juros_anexo01_vs_execucao_dpf", rec.get("juros_pagos") if rec.get("disponivel") else None, exe_rec.get("juros") if exe_rec.get("disponivel") else None,
        f"Anexo 01 (liquidado) contra execução da DPF ({bim}); a execução inclui {exe_rec.get('politica_monetaria_juros', 0.0) or 0.0:,.2f} de cobertura do BC" if exe_rec.get("disponivel") else "", nao_comparavel=sem_base)
    add("principal_anexo01_vs_execucao_dpf", ((rec.get("amortizacao") or 0.0) + (rec.get("refinanciamento") or 0.0)) if rec.get("disponivel") else None, exe_rec.get("principal") if exe_rec.get("disponivel") else None,
        f"amortização + refinanciamento do Anexo 01 contra principal da execução da DPF ({bim})", nao_comparavel=sem_base)
    pm_total = (exe_rec.get("politica_monetaria_juros", 0.0) or 0.0) + (exe_rec.get("politica_monetaria_principal", 0.0) or 0.0) if exe_rec.get("disponivel") else None
    escala_servico = ((rec.get("juros_pagos") or 0.0) + (rec.get("amortizacao") or 0.0)) if rec.get("disponivel") else None
    add("cobertura_bc_execucao_vs_recibo", rec.get("fora_843_844") if rec.get("disponivel") else None, pm_total, f"juros + amortização fora de 843/844 (recibo) contra Política Monetária (execução da DPF, contada uma vez) ({bim})", escala=escala_servico, nao_comparavel=sem_base)
    if exe.get("linhas_repetidas"):
        avisos.append(f"execução da DPF {exercicio}: {exe['linhas_repetidas']} linha(s) repetida(s) no arquivo do Tesouro (iguais em tudo menos {cfg['execucao_csv'].get('duplicatas_ignorando')}), contadas uma vez: {exe['valor_repetido']:,.2f}; detalhe: {exe.get('repetidas_detalhe')}")
    selic = next((c for c in composicao if c["id"] == "selic"), None)
    pref = next((c for c in composicao if c["id"] == "prefixado"), None)
    add("indexador_selic_tesouro_vs_bcb", (selic["share_dpmfi"] * 100) if selic and selic.get("share_dpmfi") is not None else None, b("indexador_selic"), "LFT / DPMFi (CSV do Tesouro) contra SGS 4177, em pontos percentuais")
    add("indexador_prefixado_tesouro_vs_bcb", (pref["share_dpmfi"] * 100) if pref and pref.get("share_dpmfi") is not None else None, b("indexador_prefixado"), "(LTN + NTN-F) / DPMFi contra SGS 4178, em pontos percentuais")

    conf_pop = pop.get("conferencia")
    if conf_pop and conf_pop.get("calculada", True):
        recs.append(ReconciliacaoDivida(conf_pop["id"], bool(conf_pop["ok"]), False, float(conf_pop["esperado"]), float(conf_pop["obtido"]), float(conf_pop["diff"]),
                                        {r["id"]: r for r in (linhas_cfg.get("populacao") or {}).get("reconciliacoes", [])}.get("populacao_projecao_vs_estimativa", {}).get("descricao", ""), conf_pop.get("detalhe", "")))

    publicavel = all(r.ok for r in recs if r.critica) and any(r.critica for r in recs)
    falhas = [r.id for r in recs if r.critica and not r.ok]
    selo = ("ano fechado (dezembro em todas as fontes)" if fechado else f"em andamento: até o mês {mes_ref:02d}/{exercicio}") + ("; conferências críticas OK" if publicavel else f"; NÃO PUBLICÁVEL: {falhas}")
    fontes_ficha = fontes.ficha()
    fontes_ficha["recibo"] = {k: rec.get(k) for k in ("disponivel", "periodo", "fechado", "publicavel", "motivo")}
    fontes_ficha["cofog"] = {k: cof.get(k) for k in ("disponivel", "arquivo", "snapshot_id", "motivo")}
    fontes_ficha["populacao"] = {k: pop.get(k) for k in ("disponivel", "valor", "regua", "revisao", "data_referencia", "motivo", "fonte", "estimativa", "censo")}
    fontes_ficha["ultimo_mes_por_fonte"] = ultimos
    execucao = {
        "disponivel": bool(exe.get("disponivel")),
        "motivo": exe.get("motivo"),
        "fonte": fonte_exe,
        "meses": exe.get("meses"),
        "ultimo_mes_com_dados": exe.get("ultimo_mes_com_dados"),
        "juros": exe.get("juros"),
        "principal": exe.get("principal"),
        "por_carteira": exe.get("por_carteira", []),
        "nota": "Soma por carteira = total do período. Mercado, Banco Central, externa e contratos são as carteiras que o Tesouro publica mês a mês; "
                "quem são os donos dos títulos no mercado (bancos, fundos, previdência, pessoas, estrangeiros) só sai no Relatório Mensal da Dívida, em PDF.",
    } if exe.get("disponivel") else {"disponivel": False, "motivo": exe.get("motivo"), "fonte": fonte_exe, "por_carteira": []}
    # ---- blocos da visão "A dívida" do site (v0.10.0)
    det = csv_est.get("detalhe") if csv_est.get("disponivel") else None
    if det:
        det = dict(det)
        det.update({"fonte": fonte_csv, "exercicio": exercicio, "fechado": fechado})
    else:
        det = {"disponivel": False, "motivo": csv_est.get("motivo") or "CSV do estoque indisponível", "fonte": fonte_csv}
    identidade = next((r for r in recs if r.id == "dpf_fatores_identidade"), None)
    conta = {
        "disponivel": True, "fonte": fonte_fat, "exercicio": exercicio, "fechado": fechado, "meses": fat["meses"],
        "estoque_anterior": fat["estoque_anterior"], "emissoes": fat["emissoes"], "resgates": fat["resgates"],
        "juros_apropriados": fat["juros"], "juros_dpmfi": fat["juros_dpmfi"], "juros_dpfe": fat["juros_dpfe"],
        "operacoes_bc": fat["operacoes_bc"], "estoque_fim": fat["estoque_fim"], "variacao": var_dpf,
        "fecha": bool(identidade and identidade.ok and identidade.calculada),
        "nota": "Como o Tesouro abre a variação da DPF em poder do público (Anexo 2.9): estoque no fim do ano anterior, mais o que vendeu (emissões), "
                "menos o que pagou (resgates: principal e juros pagos em dinheiro), mais os juros que a dívida gerou sem passar pelo caixa (apropriados), "
                "mais as operações do mercado com o Banco Central. A conta tem que fechar com o estoque do fim do período (conferência crítica dpf_fatores_identidade).",
    } if fat else {"disponivel": False, "motivo": fontes.erros.get("fatores_xlsx") or "planilha de fatores indisponível", "fonte": fonte_fat}
    mensal: list[dict] = []
    if "estoque_xlsx" in fontes.planilhas:
        try:
            for m in range(1, mes_ref + 1):
                e_m = estoque_xlsx_em(fontes.planilhas["estoque_xlsx"], cfg["estoque_xlsx"], exercicio, m)
                if e_m.get("dpf_publico") is not None:
                    mensal.append({"mes": m, "dpf_publico": e_m.get("dpf_publico"), "dpmfi": e_m.get("dpmfi"), "dpfe": e_m.get("dpfe"), "dpmfi_bc": e_m.get("dpmfi_bc")})
        except GradeFormato as e:
            avisos.append(f"estoque mensal (planilha): {e}")
    return ResultadoDivida(exercicio, mes_ref, fechado, medidas, composicao, fontes_ficha, recs, publicavel, selo, avisos, execucao, det, conta, mensal)


# --------------------------------------------------------------------------------------------------------------------
# export e resumo
# --------------------------------------------------------------------------------------------------------------------


def to_export_divida(res: ResultadoDivida, linhas_cfg: dict) -> dict:
    cfg = linhas_cfg.get("divida") or {}
    return {
        "produto": "divida",
        "versao_spec": linhas_cfg.get("versao"),
        "gerado_em": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "exercicio": res.exercicio,
        "mes_referencia": res.mes_referencia,
        "fechado": res.fechado,
        "publicavel": res.publicavel,
        "selo": res.selo,
        "o_que_e": "A dívida da União em quatro medidas, cada uma com várias réguas oficiais nomeadas (Tesouro, Banco Central, RREO, COFOG) e nunca misturadas, mais o per capita pela população do IBGE. Produto separado do recibo.",
        "grupos": [{"id": g, "nome": NOMES_GRUPOS[g], "medidas": [m.__dict__ for m in res.medidas[g]]} for g in GRUPOS],
        "composicao_indexador": res.composicao,
        "execucao": res.execucao,
        "estoque_detalhe": res.estoque_detalhe,
        "conta_do_ano": res.conta_do_ano,
        "estoque_mensal": res.estoque_mensal,
        "fontes": res.fontes,
        "reconciliacoes": [r.__dict__ for r in res.reconciliacoes],
        "avisos": res.avisos,
        "glossario": cfg.get("glossario", []),
    }


def write_outputs_divida(export: dict, marts_dir: Path, export_dir: Path) -> tuple[Path, Path, Path]:
    marts_dir.mkdir(parents=True, exist_ok=True)
    export_dir.mkdir(parents=True, exist_ok=True)
    ex = export["exercicio"]
    p_json = marts_dir / f"divida_{ex}.json"
    p_json.write_text(json.dumps(export, ensure_ascii=False, indent=2), encoding="utf-8")
    p_csv = marts_dir / f"divida_{ex}.csv"
    with open(p_csv, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["grupo", "id", "nome", "valor", "unidade", "disponivel", "fonte"])
        for g in export["grupos"]:
            for m in g["medidas"]:
                w.writerow([g["id"], m["id"], m["nome"], "" if m["valor"] is None else f"{m['valor']:.2f}", m["unidade"], m["disponivel"], m["fonte"]])
    p_exp = export_dir / f"divida_{ex}.json"
    p_exp.write_text(json.dumps(export, ensure_ascii=False, indent=2), encoding="utf-8")
    return p_json, p_csv, p_exp


def _fmt_valor(m: dict) -> str:
    if m["valor"] is None:
        return f"(indisponível: {m.get('motivo')})"
    if m["unidade"] == "reais":
        return f"R$ {m['valor']:>24,.2f}"
    if m["unidade"] == "% do PIB":
        return f"{m['valor']:>8.2f} % do PIB"
    if m["unidade"] == "%":
        return f"{m['valor']:>8.2f} %"
    if m["unidade"] == "pessoas":
        return f"{int(round(m['valor'])):>27,} pessoas"
    if m["unidade"] == "reais por habitante":
        return f"R$ {m['valor']:>24,.2f} por habitante"
    return f"{m['valor']:>8.2f} {m['unidade']}"


def print_summary_divida(export: dict) -> None:
    print(f"\nDívida {export['exercicio']} | publicável: {export['publicavel']} | {export['selo']}")
    for g in export["grupos"]:
        print(f"\n{g['nome']}")
        for m in g["medidas"]:
            print(f"  {m['nome'][:78]:<79}{_fmt_valor(m)}")
            for c in m.get("componentes") or []:
                v = c.get("valor")
                print(f"      {c['nome'][:70]:<75}{'' if v is None else (f'{v:>24,d}' if isinstance(v, int) else f'{v:>24,.2f}')}")
    det = export.get("estoque_detalhe") or {}
    if det.get("disponivel"):
        print(f"\nQuando vence (DPF em poder do público, título a título, {det.get('mes'):02d}/{export['exercicio']}; total R$ {det['total']:,.2f})")
        for fx in det.get("faixas", []):
            print(f"  {fx['nome'][:60]:<61}R$ {fx['valor']:>24,.2f}   {(fx['share'] or 0) * 100:6.2f} %")
        print(f"  células índice x família x prazo: {len(det.get('celulas', []))}")
    conta = export.get("conta_do_ano") or {}
    if conta.get("disponivel"):
        print(f"\nA conta do ano (fatores do Tesouro, meses {conta['meses'][0]} a {conta['meses'][-1]}; fecha: {conta['fecha']})")
        for k, nome in (("estoque_anterior", "começou devendo"), ("emissoes", "vendeu (emissões)"), ("resgates", "pagou (resgates, principal e juros)"), ("juros_apropriados", "juros que viraram dívida (apropriados)"), ("operacoes_bc", "operações com o BC"), ("estoque_fim", "terminou devendo"), ("variacao", "variação")):
            v = conta.get(k)
            print(f"  {nome:<45}{'' if v is None else f'R$ {v:>24,.2f}'}")
    if export.get("avisos"):
        print("\navisos:")
        for a in export["avisos"]:
            print(f"  - {a}")
    print("\nreconciliações:")
    for r in export["reconciliacoes"]:
        crit = "crítica" if r["critica"] else "informativa"
        if not r.get("calculada", True):
            print(f"  [ -- ] {r['id']} ({crit}): {r.get('detalhe') or 'não calculada'}")
            continue
        flag = "OK " if r["ok"] else "FALHOU"
        print(f"  [{flag}] {r['id']} ({crit}): esperado {r['esperado']:,.2f} | obtido {r['obtido']:,.2f} | diff {r['diff']:,.2f}  {r.get('detalhe') or ''}")
    f = export.get("fontes") or {}
    print("\nfontes (snapshot | recurso):")
    for k in ("estoque_xlsx", "fatores_xlsx", "estoque_csv", "execucao_csv", "tesouro_direto_csv"):
        if k in f:
            print(f"  {k:<20} {f[k].get('arquivo')} | {f[k].get('snapshot_id')} | recurso {f[k].get('recurso_data')} | unidade {f[k].get('unidade_arquivo')}")
    if "bcb" in f:
        print(f"  {'bcb':<20} {f['bcb'].get('series')} séries, {f['bcb'].get('pontos')} pontos, lido em {f['bcb'].get('lido_em')}")
    if f.get("indisponiveis"):
        print(f"  indisponíveis: {f['indisponiveis']}")
