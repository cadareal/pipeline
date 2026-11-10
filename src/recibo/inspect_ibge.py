"""Diagnóstico do staging do IBGE (S14): as réguas de população lado a lado, por ano, para o Brasil (ou outro local),
com a diferença entre projeção e estimativa onde as duas existem. É o que se cola de volta para conferir o per capita."""

from __future__ import annotations

from pathlib import Path

import polars as pl


def inspect_ibge(staging_path: Path, local: str = "Brasil", desde: int | None = None, rows: int = 30) -> str:
    p = Path(staging_path)
    if not p.exists():
        return f"staging do IBGE não existe ({p}); rode `recibo fetch ibge` e `recibo transform ibge`"
    df = pl.read_parquet(p)
    out: list[str] = [f"staging do IBGE: {df.height} linhas; réguas: {sorted(df['regua'].unique().to_list())}; locais: {df['local'].n_unique()}; anos: {df['ano'].min()} a {df['ano'].max()}"]
    for r in df.group_by("regua").agg(pl.col("ano").min().alias("de"), pl.col("ano").max().alias("ate"), pl.len().alias("n"), pl.col("arquivo").first(), pl.col("snapshot_id").first(), pl.col("fetched_at").first()).sort("regua").iter_rows(named=True):
        out.append(f"  {r['regua']:<11} {r['de']} a {r['ate']} ({r['n']} linhas) | {r['arquivo']} | snapshot {r['snapshot_id']} | lido em {r['fetched_at']}")
    d = df.filter(pl.col("local") == local)
    if d.height == 0:
        return "\n".join(out + [f"\nnenhuma linha para {local!r}; locais: {sorted(df['local'].unique().to_list())}"])
    reguas = [x for x in ("projecao", "estimativa", "censo") if x in d["regua"].unique().to_list()] + sorted(set(d["regua"].unique().to_list()) - {"projecao", "estimativa", "censo"})
    wide = d.select("ano", "regua", "populacao").pivot(on="regua", index="ano", values="populacao", aggregate_function="first").sort("ano")
    for rg in reguas:
        if rg not in wide.columns:
            wide = wide.with_columns(pl.lit(None, dtype=pl.Int64).alias(rg))
    if desde is not None:
        wide = wide.filter(pl.col("ano") >= desde)
    else:
        wide = wide.tail(rows)
    out.append(f"\n{local}, população por régua (projeção = 1º de julho, revisão 2024; estimativa = oficial do TCU; censo = contagem):")
    out.append(f"  {'ano':<6}" + "".join(f"{rg:>16}" for rg in reguas) + f"{'est - proj':>14}{'%':>9}")
    for r in wide.iter_rows(named=True):
        vals = "".join(f"{(format(r[rg], ',') if r.get(rg) is not None else '-'):>16}" for rg in reguas)
        diff = ""
        pct = ""
        if r.get("projecao") is not None and r.get("estimativa") is not None:
            dd = r["estimativa"] - r["projecao"]
            diff = f"{dd:+,}"
            pct = f"{dd / r['projecao'] * 100:+.3f}%"
        out.append(f"  {r['ano']:<6}{vals}{diff:>14}{pct:>9}")
    proj = d.filter(pl.col("regua") == "projecao").sort("ano")
    if proj.height and proj["homens"].drop_nulls().len():
        ult = proj.filter(pl.col("ano") <= 2026).tail(4)
        out.append(f"\n{local}, projeção: sexo e 65+ (últimos anos até 2026):")
        out.append(f"  {'ano':<6}{'total':>16}{'homens':>16}{'mulheres':>16}{'65+':>16}{'65+ %':>9}")
        for r in ult.iter_rows(named=True):
            p65 = f"{r['idosos_65_mais'] / r['populacao'] * 100:.1f}%" if r.get("idosos_65_mais") and r.get("populacao") else "-"
            out.append(f"  {r['ano']:<6}{r['populacao']:>16,}{(r['homens'] or 0):>16,}{(r['mulheres'] or 0):>16,}{(r['idosos_65_mais'] or 0):>16,}{p65:>9}")
    return "\n".join(out)
