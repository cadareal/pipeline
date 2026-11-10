"""Diagnóstico do staging do BCB (S16): por série, ficha, período, últimos valores e o agregado anual (soma dos meses
para fluxos; valor de dezembro para saldos e taxas). `--ano` monta a tabela de todas as séries num ano, que é o que se
compara com o Anexo 06 (abaixo da linha, juros nominais) e com o COFOG (linha JUROS)."""

from __future__ import annotations

from pathlib import Path

import polars as pl


def _fmt(v) -> str:
    return "-" if v is None else f"{v:,.2f}"


def agregado_anual(d: pl.DataFrame, tipo: str | None) -> pl.DataFrame:
    """fluxo: soma dos meses do ano (e quantos meses havia); saldo/taxa: o último valor do ano (e o mês dele)."""
    d = d.with_columns(pl.col("data").dt.year().alias("ano"), pl.col("data").dt.month().alias("mes"))
    if tipo == "fluxo":
        return d.group_by("ano").agg(pl.col("valor").sum().alias("valor"), pl.len().alias("meses"), pl.col("mes").max().alias("ultimo_mes")).sort("ano")
    return d.sort("data").group_by("ano").agg(pl.col("valor").last().alias("valor"), pl.len().alias("meses"), pl.col("mes").max().alias("ultimo_mes")).sort("ano")


def inspect_bcb(staging_path: Path, serie: int | None = None, ano: int | None = None, rows: int = 13) -> str:
    if not Path(staging_path).exists():
        return f"staging do BCB não existe ({staging_path}); rode `recibo fetch bcb` e `recibo transform bcb`"
    df = pl.read_parquet(staging_path)
    codigos = sorted(df["codigo"].unique().to_list())
    out: list[str] = [f"séries no staging do BCB: {len(codigos)}; {df.height} pontos"]
    if serie is not None and serie not in codigos:
        return "\n".join(out + [f"série {serie} não está no staging; códigos: {codigos}"])
    if ano is not None:
        out.append(f"\nagregado de {ano} por série (fluxo = soma dos meses; saldo e taxa = último valor do ano; 'meses' = quantos meses o BC já publicou):")
        out.append(f"  {'código':<7}{'tipo':<7}{'unidade':<12}{'valor':>22}{'meses':>7}  nome")
        for c in codigos:
            d = df.filter(pl.col("codigo") == c)
            tipo = d["tipo"][0]
            g = agregado_anual(d, tipo).filter(pl.col("ano") == ano)
            if g.height == 0:
                out.append(f"  {c:<7}{(tipo or '?'):<7}{(d['unidade'][0] or '?')[:11]:<12}{'(sem dados)':>22}{'':>7}  {d['nome'][0] or d['titulo_bc'][0] or ''}")
                continue
            r = g.row(0, named=True)
            aviso = "" if (tipo != "fluxo" or r["meses"] == 12) and (tipo == "fluxo" or r["ultimo_mes"] == 12) else f" (até o mês {r['ultimo_mes']})"
            out.append(f"  {c:<7}{(tipo or '?'):<7}{(d['unidade'][0] or '?')[:11]:<12}{_fmt(r['valor']):>22}{r['meses']:>7}  {d['nome'][0] or d['titulo_bc'][0] or ''}{aviso}")
        if serie is None:
            return "\n".join(out)
    for c in ([serie] if serie is not None else codigos):
        d = df.filter(pl.col("codigo") == c).sort("data")
        out.append("")
        out.append("=" * 100)
        out.append(f"SGS {c} | {d['nome'][0] or '(sem nome em sources.yaml)'} | tipo {d['tipo'][0]} | unidade {d['unidade'][0]}")
        if d["titulo_bc"][0]:
            out.append(f"  nome no BC: {d['titulo_bc'][0]}")
        if d["uso"][0]:
            out.append(f"  uso: {d['uso'][0]}")
        out.append(f"  {d.height} pontos, de {d['data'].min()} a {d['data'].max()} | snapshot {d['snapshot_id'][0]} | lido em {d['fetched_at'][0]}")
        g = agregado_anual(d, d["tipo"][0])
        ultimos_anos = g.tail(5)
        out.append("  por ano: " + "; ".join(f"{r['ano']}: {_fmt(r['valor'])}" + ("" if r["meses"] == 12 else f" ({r['meses']} meses)") for r in ultimos_anos.iter_rows(named=True)))
        out.append(f"  últimos {min(rows, d.height)} valores:")
        for r in d.tail(rows).iter_rows(named=True):
            out.append(f"    {r['data']}  {_fmt(r['valor']):>20}")
    return "\n".join(out)
