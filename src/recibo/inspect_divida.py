"""Diagnóstico do staging da dívida (S1): o que cada arquivo do Tesouro tem de verdade (colunas, tipos lidos, valores
distintos, período, somas por ano e por valor distinto; `--filtro COLUNA=VALOR` recorta as linhas) ou, nas planilhas,
as primeiras linhas de cada aba e, com `--ano`, todas as linhas nas colunas daquele ano (meses e total anual, pelo
cabeçalho). É o que se cola de volta para fixar em config o layout de cada arquivo (v0.7.1)."""

from __future__ import annotations

from pathlib import Path

import polars as pl

from .grade import RX_ANO, RX_MES, linha_cabecalho  # noqa: F401 - reexportado para os testes
from .transform.divida import INDICE_FILE

MAX_DISTINTOS = 25


def _fmt(v) -> str:
    if v is None:
        return "-"
    if isinstance(v, float):
        return f"{v:,.2f}"
    return str(v)


def aplicar_filtros(df: pl.DataFrame, filtros: list[str] | None) -> tuple[pl.DataFrame, list[str]]:
    """`COLUNA=VALOR` (texto exato, depois de tirar espaços das pontas) sobre as colunas originais; vários filtros se somam."""
    msgs: list[str] = []
    for f in filtros or []:
        if "=" not in f:
            msgs.append(f"filtro ignorado (use COLUNA=VALOR): {f!r}")
            continue
        col, val = f.split("=", 1)
        col, val = col.strip(), val.strip()
        if col not in df.columns:
            msgs.append(f"filtro ignorado: coluna {col!r} não existe; colunas: {[c for c in df.columns if not c.startswith('_') and not c.endswith('__num') and not c.endswith('__data')]}")
            continue
        antes = df.height
        df = df.filter(pl.col(col).cast(pl.Utf8).fill_null("").str.strip_chars() == val)
        msgs.append(f"filtro {col!r} = {val!r}: {df.height} de {antes} linhas")
    return df, msgs


def _tabela_csv(df: pl.DataFrame, rows: int, filtros: list[str] | None = None) -> list[str]:
    out: list[str] = []
    df, msgs = aplicar_filtros(df, filtros)
    out += msgs
    originais = [c for c in df.columns if not c.startswith("_") and not c.endswith("__num") and not c.endswith("__data")]
    nums = [c for c in df.columns if c.endswith("__num")]
    datas = [c for c in df.columns if c.endswith("__data")]
    if df.height == 0:
        return out + ["nenhuma linha depois dos filtros"]
    out.append(f"colunas ({len(originais)}):")
    for c in originais:
        tipo = "número" if f"{c}__num" in nums else ("data" if f"{c}__data" in datas else "texto")
        s = df[c].cast(pl.Utf8).fill_null("").str.strip_chars()
        n_vazias = int((s == "").sum())
        nd = int(s.n_unique())
        linha = f"  {c!r}: {tipo}, {nd} distintos, {n_vazias} vazias"
        if tipo == "texto" and nd <= MAX_DISTINTOS:
            vals = [v for v in s.unique().sort().to_list() if v != ""]
            linha += f": {vals}"
        elif tipo == "data":
            d = df[f"{c}__data"]
            linha += f", de {d.min()} a {d.max()}"
        elif tipo == "número":
            n = df[f"{c}__num"]
            linha += f", soma {_fmt(float(n.sum()))}, mín {_fmt(n.min())}, máx {_fmt(n.max())}"
        out.append(linha)
    if datas and nums:
        dcol = datas[0]
        anos = df.select(pl.col(dcol).dt.year().alias("ano")).drop_nulls()["ano"]
        if anos.len():
            ultimos = sorted(anos.unique().to_list())[-5:]
            out.append(f"\nsoma por ano (pela coluna de data {dcol[:-6]!r}; últimos {len(ultimos)} anos):")
            cab = f"  {'ano':<6}" + "".join(f"{c[:-5][:22]:>24}" for c in nums) + f"{'linhas':>9}"
            out.append(cab)
            g = df.with_columns(pl.col(dcol).dt.year().alias("ano")).filter(pl.col("ano").is_in(ultimos)).group_by("ano").agg([pl.col(c).sum() for c in nums] + [pl.len().alias("n")]).sort("ano")
            for r in g.iter_rows(named=True):
                out.append(f"  {r['ano']:<6}" + "".join(f"{_fmt(float(r[c])):>24}" for c in nums) + f"{r['n']:>9}")
    baixa = [c for c in originais if f"{c}__num" not in nums and f"{c}__data" not in datas and df[c].cast(pl.Utf8).fill_null("").n_unique() <= MAX_DISTINTOS]
    if nums and baixa:
        out.append("\nsomas das colunas numéricas por valor distinto (colunas de texto com até 25 valores):")
        for c in baixa:
            g = df.group_by(pl.col(c).cast(pl.Utf8).fill_null("").str.strip_chars().alias("_v")).agg([pl.col(n).sum() for n in nums] + [pl.len().alias("_n")]).sort("_v")
            out.append(f"  por {c!r}:")
            out.append(f"    {'valor':<34}" + "".join(f"{n[:-5][:20]:>22}" for n in nums) + f"{'linhas':>9}")
            for r in g.iter_rows(named=True):
                out.append(f"    {str(r['_v'])[:33]:<34}" + "".join(f"{_fmt(float(r[n])):>22}" for n in nums) + f"{r['_n']:>9}")
    out.append(f"\nprimeiras {min(rows, df.height)} linhas (colunas originais):")
    larg = {c: min(28, max(len(c), *(len(str(v)) for v in df[c].head(rows).to_list())) if df.height else len(c)) for c in originais}
    out.append("  " + " | ".join(c[: larg[c]].ljust(larg[c]) for c in originais))
    for r in df.head(rows).select(originais).iter_rows():
        out.append("  " + " | ".join(("" if v is None else str(v))[: larg[c]].ljust(larg[c]) for c, v in zip(originais, r)))
    return out


_RX_MES = RX_MES
_RX_ANO = RX_ANO


def _grade_ano(d: pl.DataFrame, planilha: str, ano: int) -> list[str]:
    """Todas as linhas da planilha, só nas colunas do ano: os meses (Mmm/aa) e a coluna anual (aaaa), pelo cabeçalho."""
    cab = linha_cabecalho(d)
    if cab is None:
        return [f"\nplanilha {planilha!r}: não achei a linha de cabeçalho com meses (Mmm/aa) ou anos (aaaa)"]
    aa = f"{ano % 100:02d}"
    cabs = d.filter(pl.col("linha") == cab).sort("coluna")
    cols = [(int(c), t.strip()) for c, t in zip(cabs["coluna"].to_list(), cabs["valor_texto"].to_list()) if t.strip() == str(ano) or (_RX_MES.match(t.strip()) and t.strip().endswith("/" + aa))]
    if not cols:
        anos = sorted({t.strip() for t in cabs["valor_texto"].to_list() if _RX_ANO.match(t.strip())})
        return [f"\nplanilha {planilha!r}: nenhuma coluna de {ano} no cabeçalho (linha {cab}); anos no cabeçalho: {anos}"]
    out = [f"\nplanilha {planilha!r}, colunas de {ano} (cabeçalho na linha {cab}; rótulo = coluna 1; valores como no arquivo):"]
    out.append(f"  {'linha':<6}{'rótulo':<44}" + "".join(f"{t:>16}" for _, t in cols))
    for i in sorted(d["linha"].unique().to_list()):
        if i == cab:
            continue
        cels = d.filter(pl.col("linha") == i)
        por_col = dict(zip(cels["coluna"].to_list(), cels["valor_texto"].to_list()))
        nums = dict(zip(cels["coluna"].to_list(), cels["valor_num"].to_list()))
        rotulo = str(por_col.get(1, "")).strip()
        vals = []
        for c, _ in cols:
            v = nums.get(c)
            vals.append(f"{v:>16,.2f}" if v is not None else f"{str(por_col.get(c, ''))[:15]:>16}")
        if not rotulo and all(v.strip() == "" for v in vals):
            continue
        out.append(f"  l{i:<5}{rotulo[:43]:<44}" + "".join(vals))
    return out


def _tabela_grade(df: pl.DataFrame, rows: int, ano: int | None = None) -> list[str]:
    out: list[str] = []
    for planilha in df["planilha"].unique(maintain_order=True).to_list():
        d = df.filter(pl.col("planilha") == planilha)
        if ano is not None:
            out += _grade_ano(d, planilha, ano)
            continue
        n_lin, n_col = int(d["linha"].max()), int(d["coluna"].max())
        out.append(f"\nplanilha {planilha!r}: {n_lin} linhas x {n_col} colunas ({d.height} células preenchidas); primeiras {min(rows, n_lin)} linhas:")
        for i in sorted(d["linha"].unique().to_list())[:rows]:
            cels = d.filter(pl.col("linha") == i).sort("coluna")
            partes = [f"[{c}] {t[:40]}" for c, t in zip(cels["coluna"].to_list(), cels["valor_texto"].to_list())]
            out.append(f"  l{i:<4} " + " | ".join(partes)[:300])
        ultimas = sorted(d["linha"].unique().to_list())[-3:]
        if n_lin > rows:
            out.append(f"  ... últimas linhas:")
            for i in ultimas:
                cels = d.filter(pl.col("linha") == i).sort("coluna")
                partes = [f"[{c}] {t[:40]}" for c, t in zip(cels["coluna"].to_list(), cels["valor_texto"].to_list())]
                out.append(f"  l{i:<4} " + " | ".join(partes)[:300])
    return out


def inspect_divida(staging_dir: Path, dataset: str | None = None, rows: int = 20, ano: int | None = None, filtros: list[str] | None = None) -> str:
    idx_path = Path(staging_dir) / INDICE_FILE
    if not idx_path.exists():
        return f"staging da dívida não existe ({idx_path}); rode `recibo fetch divida` e `recibo transform divida`"
    idx = pl.read_parquet(idx_path)
    out: list[str] = [f"arquivos no staging da dívida: {idx.height}" + (f" (filtrando dataset {dataset!r})" if dataset else "")]
    if dataset:
        idx = idx.filter(pl.col("dataset") == dataset)
        if idx.height == 0:
            return "\n".join(out + [f"nenhum arquivo do dataset {dataset!r}; datasets no staging: {sorted(pl.read_parquet(idx_path)['dataset'].unique().to_list())}"])
    for r in idx.sort(["dataset", "chave"]).iter_rows(named=True):
        out.append("")
        out.append("=" * 100)
        out.append(f"{r['dataset']} / {r['chave']} | {r['arquivo']} | {r['formato']} | {(r['bytes'] or 0) / 1e6:.2f} MB | recurso {r['recurso_data']} | snapshot {r['snapshot_id']}")
        out.append(f"recurso no CKAN: {r['recurso_nome']!r} | tipo de leitura: {r['tipo']} | linhas: {r['linhas']}")
        if r["tipo"] == "erro":
            out.append(f"  ERRO de leitura: {r['erro']}")
            continue
        df = pl.read_parquet(Path(staging_dir) / r["staging"])
        out += _tabela_grade(df, rows, ano) if r["tipo"] == "xlsx_grade" else _tabela_csv(df, rows, filtros)
    return "\n".join(out)
