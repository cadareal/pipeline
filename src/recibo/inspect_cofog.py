"""Diagnóstico do staging do COFOG: o que o Tesouro publicou para um ano, somado por divisão, grupo, classificação
econômica, grupo de despesa, ajuste, unidade orçamentária ou ação. É o que se cola de volta para desenhar a ponte com
o recibo (Anexo 06) e o mapa das linhas para as gavetas COFOG."""

from __future__ import annotations

import csv
from pathlib import Path

import polars as pl

POR = ("divisao", "grupo", "classe", "gfs", "gnd", "ajuste", "uo", "acao", "natureza", "anos")


def _fmt(v: float) -> str:
    return f"{v:,.2f}"


def rotulos_cofog(config_dir: Path | None) -> dict[str, str]:
    if config_dir is None:
        return {}
    p = Path(config_dir) / "ref" / "cofog.csv"
    if not p.exists():
        return {}
    with open(p, encoding="utf-8") as f:
        return {r["codigo"]: r["nome"] for r in csv.DictReader(f)}


def _modo(df: pl.DataFrame, chave: str, desc: str) -> dict[str, str]:
    """Para cada código, a descrição mais frequente no próprio arquivo (o Tesouro descreve no nível da classe)."""
    if desc not in df.columns:
        return {}
    m = (
        df.group_by([chave, desc]).agg(pl.len().alias("n"))
        .sort(["n", desc], descending=[True, False])
        .unique(subset=[chave], keep="first", maintain_order=True)
    )
    return {str(k): str(d or "") for k, d in zip(m[chave].to_list(), m[desc].to_list())}


def _do_raw(store, exercicio: int, config_dir: Path | None = None) -> tuple[pl.DataFrame | None, list[str]]:
    """Lê o CSV direto do raw (sem a guarda de escala), para diagnosticar um arquivo que o transform recusou. A unidade
    declarada em sources.yaml é aplicada, para os números saírem em reais como no staging; sem config, fica como veio."""
    from .fetchers.cofog import DATASET, SOURCE
    from .transform.cofog import CofogFormato, aplicar_unidade, decodificar, derivar, ler_csv, unidade_do_exercicio

    got = store.get_latest(SOURCE, DATASET, str(exercicio)) if store is not None else None
    if not got:
        return None, [f"COFOG {exercicio}: também não há raw desse ano (rode `recibo fetch cofog --exercicios {exercicio}`)"]
    raw, manifest = got
    msgs = [f"COFOG {exercicio}: lido direto do raw ({manifest.get('filename')}, {len(raw) / 1e6:.1f} MB, snapshot {manifest.get('snapshot_id')}), sem a guarda de escala"]
    try:
        df = derivar(ler_csv(raw))
        cfg = None
        if config_dir is not None:
            from .config import source_config

            cfg = source_config(Path(config_dir), "S7")
        nome, fator = unidade_do_exercicio(cfg, exercicio)
        df = aplicar_unidade(df, nome, fator)
    except CofogFormato as e:
        primeiras = decodificar(raw).split("\n")[:6]
        return None, msgs + [f"o CSV não tem o formato esperado: {e}", "primeiras linhas do arquivo:"] + [f"  {ln[:300]}" for ln in primeiras]
    return df.with_columns(pl.lit(exercicio).cast(pl.Int64).alias("exercicio"), pl.lit(manifest.get("filename")).alias("arquivo"),
                           pl.lit((manifest.get("recurso") or {}).get("last_modified") or (manifest.get("recurso") or {}).get("created")).alias("recurso_data")), msgs


def inspect_cofog(staging_path: Path, exercicio: int, por: str = "divisao", rows: int = 40, cofog: str | None = None, config_dir: Path | None = None, store=None) -> str:
    out: list[str] = []
    if Path(staging_path).exists():
        df = pl.read_parquet(staging_path)
    else:
        out.append(f"staging do COFOG não existe ({staging_path}); rode `recibo transform cofog`")
        df = None
    anos = sorted(df["exercicio"].unique().to_list()) if df is not None else []
    if df is None or (exercicio not in anos and por != "anos"):
        if df is not None:
            out.append(f"COFOG {exercicio}: não está no staging. Anos disponíveis: {anos}")
        df_raw, msgs = _do_raw(store, exercicio, config_dir)
        out += msgs
        if df_raw is None:
            return "\n".join(out)
        df, anos = df_raw, [exercicio]
    if "unidade_fonte" not in df.columns:  # staging de antes da v0.6.2
        df = df.with_columns(pl.lit("reais").alias("unidade_fonte"), pl.lit(1.0).alias("fator_unidade"))
    if por == "anos":
        tot = df.group_by("exercicio").agg(pl.col("valor").sum().alias("total"), pl.len().alias("linhas"), pl.col("ajuste").sum().alias("linhas_com_ajuste"), pl.col("unidade_fonte").first().alias("unidade")).sort("exercicio")
        out.append("total por ano (R$) | linhas | linhas com ajuste metodológico | unidade no arquivo | data do recurso no CKAN")
        datas = df.group_by("exercicio").agg(pl.col("recurso_data").max()).sort("exercicio")
        d_map = dict(zip(datas["exercicio"].to_list(), datas["recurso_data"].to_list()))
        for ex, t, n, na, u in zip(tot["exercicio"].to_list(), tot["total"].to_list(), tot["linhas"].to_list(), tot["linhas_com_ajuste"].to_list(), tot["unidade"].to_list()):
            out.append(f"  {ex}  {_fmt(t):>26}  {n:>9}  {na:>6}  {u:<8}  {d_map.get(ex) or '-'}")
        return "\n".join(out)

    d = df.filter(pl.col("exercicio") == exercicio)
    filtro = ""
    if cofog:
        dig = "".join(ch for ch in cofog if ch.isdigit())
        d = d.filter(pl.col("cofog_digitos").str.starts_with(dig))
        filtro = f" | só COFOG {cofog}"
    total = float(d["valor"].sum())
    out.append(f"COFOG {exercicio} | {d.height} linhas | total R$ {_fmt(total)}{filtro} | arquivo {d['arquivo'][0] if d.height else '-'} | recurso {d['recurso_data'][0] if d.height else '-'}")
    if d.height and d["unidade_fonte"][0] != "reais":
        out.append(f"valores em reais; o arquivo está em {d['unidade_fonte'][0]} (fator {float(d['fator_unidade'][0]):,.0f}, declarado em sources.yaml), com resolução de R$ {float(d['fator_unidade'][0]) * 0.01:,.2f} por linha")
    out.append(f"distintos: {d['uo_cod'].n_unique()} UOs, {d['acao_cod'].n_unique()} ações, {d['natureza_cod'].n_unique()} naturezas, {d['cofog_cod'].n_unique()} códigos COFOG, {d['gfs_cod'].n_unique()} códigos GFS")
    neg = d.filter(pl.col("valor") < 0)
    if neg.height:
        out.append(f"linhas negativas: {neg.height}, somando R$ {_fmt(float(neg['valor'].sum()))} (venda de bens entra negativa, pelos metadados)")
    aj = d.filter(pl.col("ajuste"))
    if aj.height:
        out.append(f"linhas com ajuste metodológico: {aj.height}, somando R$ {_fmt(float(aj['valor'].sum()))}")

    ref = rotulos_cofog(config_dir)
    chave_desc: dict[str, tuple[str, str | None]] = {
        "divisao": ("cofog_divisao", None),
        "grupo": ("cofog_grupo", None),
        "classe": ("cofog_classe", "cofog_desc"),
        "gfs": ("gfs2", "gfs_desc"),
        "gnd": ("gnd", None),
        "ajuste": ("ajuste_metodologico", None),
        "uo": ("uo_cod", "uo_desc"),
        "acao": ("acao_cod", "acao_desc"),
        "natureza": ("natureza_cod", "natureza_desc"),
    }
    if por not in chave_desc:
        return "\n".join(out + [f"--por deve ser um de {POR}"])
    chave, desc = chave_desc[por]
    if por == "ajuste":  # o CSV real escreve "NA" nas linhas sem ajuste; agrupa pelo texto só onde é ajuste de verdade
        d = d.with_columns(pl.when(pl.col("ajuste")).then(pl.col("ajuste_metodologico").str.strip_chars()).otherwise(pl.lit("(sem ajuste)")).alias("ajuste_metodologico"))
    g = d.group_by(chave).agg(pl.col("valor").sum().alias("valor"), pl.len().alias("linhas")).sort("valor", descending=True)
    labels = _modo(d, chave, desc) if desc else {}
    gnd_nomes = {"1": "pessoal e encargos", "2": "juros e encargos da dívida", "3": "outras despesas correntes", "4": "investimentos", "5": "inversões financeiras", "6": "amortização da dívida", "9": "reserva"}
    out.append(f"\npor {por} (R$; share do total do ano):")
    for k, v, n in list(zip(g[chave].to_list(), g["valor"].to_list(), g["linhas"].to_list()))[: rows if rows else None]:
        k = "" if k is None else str(k)
        if por in ("divisao", "grupo"):
            lab = ref.get(k, "") or labels.get(k, "")
            if por == "grupo" and not lab:
                lab = _modo(d, chave, "cofog_desc").get(k, "")
        elif por == "gnd":
            lab = gnd_nomes.get(k, "")
        else:
            lab = labels.get(k, "")
        share = (v / total * 100) if total else 0.0
        out.append(f"  {k:<12} {_fmt(v):>24} {share:>7.2f}%  {n:>7} linhas  {lab[:70]}")
    if por == "divisao":
        out.append("\nreferência: divisões COFOG (config/ref/cofog.csv); o Tesouro descreve os códigos no nível da classe (--por classe)")
    return "\n".join(out)
