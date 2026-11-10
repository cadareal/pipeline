"""Transform S1: raw da dívida (CSV e XLSX do CKAN do Tesouro) -> staging, um parquet por arquivo, sem adivinhar layout.

v0.7.0 é reconhecimento: ainda não sabemos as colunas reais de cada arquivo, então o transform grava o que veio, do jeito
que veio, e só acrescenta leituras "ao lado" para o inspect poder mostrar:
  CSV  -> todas as colunas como texto (nomes originais) + `{coluna}__num` quando pelo menos 90% das células não vazias são
          números (vírgula decimal e ponto de milhar, ou ponto decimal, decidido por coluna) + `{coluna}__data` quando
          pelo menos 90% são datas em um dos formatos conhecidos (dd/mm/aaaa, aaaa-mm-dd, mm/aaaa, aaaa-mm, aaaamm)
  XLSX -> grade longa: planilha, linha, coluna, valor_texto, valor_num, eh_data (uma linha por célula preenchida), porque
          as planilhas do Tesouro são relatórios formatados (títulos, cabeçalhos em várias linhas) e o layout de cada uma
          será fixado em config na v0.7.1, depois de visto
Um índice `stg_divida_arquivos.parquet` lista os arquivos do staging com procedência (dataset, chave, arquivo, snapshot,
hash, data do recurso) e o resumo (linhas, colunas ou planilhas).
"""

from __future__ import annotations

import io
import logging
import re
from datetime import date, datetime
from pathlib import Path

import polars as pl

from ..fetchers.cofog import KEY_PACKAGE
from ..fetchers.divida import SOURCE
from ..store import Store
from .cofog import CofogFormato, decodificar, separador

log = logging.getLogger(__name__)

INDICE_FILE = "stg_divida_arquivos.parquet"
FORMATOS_DATA = ("%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S")
LIMIAR = 0.9


def nome_staging(dataset: str, chave: str) -> str:
    return f"stg_divida__{dataset}__{chave.replace('.', '_')}.parquet"


def _texto(col: str) -> pl.Expr:
    return pl.col(col).cast(pl.Utf8).fill_null("").str.strip_chars()


def coluna_numerica(df: pl.DataFrame, col: str) -> pl.Series | None:
    """Leitura numérica da coluna quando pelo menos 90% das células não vazias são números. Vírgula na coluna = padrão
    brasileiro (ponto de milhar, vírgula decimal); senão, ponto decimal (vírgula de milhar removida)."""
    t = _texto(col)
    n_nao_vazias = df.select((t != "").sum().alias("n")).item()
    if not n_nao_vazias:
        return None
    limpo = t.str.replace_all(r"[R$\s%]", "")
    tem_virgula = df.select(limpo.str.contains(",").any().alias("v")).item()
    if tem_virgula:
        num = limpo.str.replace_all(r"\.", "").str.replace(",", ".").cast(pl.Float64, strict=False)
    else:
        num = limpo.cast(pl.Float64, strict=False)
    out = df.select(num.alias("n"), (t != "").alias("v"))
    ok = out.filter(pl.col("v"))["n"].is_not_null().sum()
    if ok / n_nao_vazias < LIMIAR:
        return None
    return out["n"].alias(f"{col}__num")


def coluna_data(df: pl.DataFrame, col: str) -> pl.Series | None:
    """Leitura como data quando pelo menos 90% das células não vazias casam com um formato conhecido (o primeiro que
    atingir o limiar). mm/aaaa, aaaa-mm e aaaamm viram o dia 1 do mês."""
    t = _texto(col)
    n_nao_vazias = df.select((t != "").sum().alias("n")).item()
    if not n_nao_vazias:
        return None
    mes_ano = pl.when(t.str.contains(r"^\d{1,2}/\d{4}$")).then(pl.lit("01/") + t).otherwise(t)
    ano_mes = pl.when(t.str.contains(r"^\d{4}-\d{1,2}$")).then(t + pl.lit("-01")).otherwise(t)
    aaaamm = pl.when(t.str.contains(r"^\d{6}$")).then(t + pl.lit("01")).otherwise(t)
    candidatos: list[tuple[str, pl.Expr]] = [(f, t) for f in FORMATOS_DATA] + [("%d/%m/%Y", mes_ano), ("%Y-%m-%d", ano_mes), ("%Y%m%d", aaaamm)]
    for fmt, expr in candidatos:
        if "%H" in fmt:
            parsed = expr.str.strptime(pl.Datetime, fmt, strict=False).cast(pl.Date)
        else:
            parsed = expr.str.strptime(pl.Date, fmt, strict=False)
        out = df.select(parsed.alias("d"), (t != "").alias("v"))
        ok = out.filter(pl.col("v"))["d"].is_not_null().sum()
        if ok / n_nao_vazias >= LIMIAR:
            return out["d"].alias(f"{col}__data")
    return None


def ler_csv_generico(raw: bytes) -> pl.DataFrame:
    texto = decodificar(raw)
    primeira = texto.split("\n", 1)[0]
    sep = separador(primeira)
    df = pl.read_csv(io.BytesIO(texto.encode("utf-8")), separator=sep, infer_schema_length=0, quote_char='"', truncate_ragged_lines=True, encoding="utf8")
    df = df.rename({c: c.strip() for c in df.columns})
    extras: list[pl.Series] = []
    for c in df.columns:
        n = coluna_numerica(df, c)
        if n is not None:
            extras.append(n)
        d = coluna_data(df, c)
        if d is not None:
            extras.append(d)
    return df.with_columns(extras) if extras else df


def ler_xlsx_grade(raw: bytes) -> pl.DataFrame:
    """Toda célula preenchida de toda planilha, como veio."""
    from openpyxl import load_workbook

    wb = load_workbook(io.BytesIO(raw), read_only=True, data_only=True)
    planilhas: list[str] = []
    linhas: list[int] = []
    colunas: list[int] = []
    textos: list[str] = []
    nums: list[float | None] = []
    datas: list[bool] = []
    for ws in wb.worksheets:
        for i, row in enumerate(ws.iter_rows(values_only=True), start=1):
            for j, v in enumerate(row, start=1):
                if v is None or (isinstance(v, str) and not v.strip()):
                    continue
                planilhas.append(ws.title)
                linhas.append(i)
                colunas.append(j)
                if isinstance(v, (datetime, date)):
                    so_data = not isinstance(v, datetime) or (v.hour == 0 and v.minute == 0 and v.second == 0)
                    textos.append(v.isoformat()[:10] if so_data else v.isoformat())
                    nums.append(None)
                    datas.append(True)
                elif isinstance(v, bool):
                    textos.append(str(v))
                    nums.append(None)
                    datas.append(False)
                elif isinstance(v, (int, float)):
                    textos.append(repr(v) if isinstance(v, float) else str(v))
                    nums.append(float(v))
                    datas.append(False)
                else:
                    textos.append(str(v).strip())
                    nums.append(None)
                    datas.append(False)
    wb.close()
    return pl.DataFrame({"planilha": planilhas, "linha": linhas, "coluna": colunas, "valor_texto": textos, "valor_num": nums, "eh_data": datas},
                        schema={"planilha": pl.Utf8, "linha": pl.Int64, "coluna": pl.Int64, "valor_texto": pl.Utf8, "valor_num": pl.Float64, "eh_data": pl.Boolean})


def _datasets(store: Store) -> list[str]:
    return [d for d in store.b.list_dirs(f"raw/{SOURCE}") if not d.startswith("_")]


def build_staging(store: Store, staging_dir: Path, src_cfg: dict | None = None) -> Path:
    staging_dir.mkdir(parents=True, exist_ok=True)
    registros: list[dict] = []
    for dataset in _datasets(store):
        for key in store.list_keys(SOURCE, dataset):
            if key == KEY_PACKAGE:
                continue
            got = store.get_latest(SOURCE, dataset, key)
            if not got:
                continue
            raw, manifest = got
            rec = manifest.get("recurso") or {}
            formato = str(rec.get("formato") or manifest.get("filename", "")).upper()
            arquivo = str(manifest.get("filename") or "")
            try:
                if arquivo.lower().endswith((".xlsx", ".xlsm", ".xls")) or formato in ("XLSX", "XLS"):
                    df = ler_xlsx_grade(raw)
                    tipo = "xlsx_grade"
                    resumo = {"planilhas": int(df["planilha"].n_unique()) if df.height else 0, "colunas": None}
                else:
                    df = ler_csv_generico(raw)
                    tipo = "csv"
                    resumo = {"planilhas": None, "colunas": len([c for c in df.columns if not c.endswith("__num") and not c.endswith("__data")])}
            except (CofogFormato, Exception) as e:  # noqa: BLE001 - um arquivo ilegível não derruba os outros; fica registrado
                log.error("divida %s/%s: não consegui ler (%s); arquivo fica fora do staging", dataset, key, e)
                registros.append({"dataset": dataset, "chave": key, "arquivo": arquivo, "formato": formato, "tipo": "erro", "staging": None, "linhas": 0,
                                  "colunas": None, "planilhas": None, "erro": str(e)[:500], "snapshot_id": manifest.get("snapshot_id"), "sha256_canonical": manifest.get("sha256_canonical"),
                                  "fetched_at": manifest.get("fetched_at"), "recurso_data": rec.get("last_modified") or rec.get("created"), "recurso_nome": rec.get("nome"), "bytes": manifest.get("bytes")})
                continue
            nome = nome_staging(dataset, key)
            df = df.with_columns(pl.lit(dataset).alias("_dataset"), pl.lit(key).alias("_chave"), pl.lit(manifest.get("snapshot_id")).alias("_snapshot_id"))
            df.write_parquet(staging_dir / nome)
            registros.append({"dataset": dataset, "chave": key, "arquivo": arquivo, "formato": formato, "tipo": tipo, "staging": nome, "linhas": df.height,
                              "colunas": resumo["colunas"], "planilhas": resumo["planilhas"], "erro": None, "snapshot_id": manifest.get("snapshot_id"),
                              "sha256_canonical": manifest.get("sha256_canonical"), "fetched_at": manifest.get("fetched_at"),
                              "recurso_data": rec.get("last_modified") or rec.get("created"), "recurso_nome": rec.get("nome"), "bytes": manifest.get("bytes")})
            log.info("divida %s/%s: %s, %d linhas -> %s", dataset, key, tipo, df.height, nome)
    if not registros:
        raise SystemExit("nenhum raw da dívida encontrado; rode `recibo fetch divida` antes")
    idx = pl.DataFrame(registros, schema={"dataset": pl.Utf8, "chave": pl.Utf8, "arquivo": pl.Utf8, "formato": pl.Utf8, "tipo": pl.Utf8, "staging": pl.Utf8, "linhas": pl.Int64,
                                           "colunas": pl.Int64, "planilhas": pl.Int64, "erro": pl.Utf8, "snapshot_id": pl.Utf8, "sha256_canonical": pl.Utf8, "fetched_at": pl.Utf8,
                                           "recurso_data": pl.Utf8, "recurso_nome": pl.Utf8, "bytes": pl.Int64})
    path = staging_dir / INDICE_FILE
    idx.write_parquet(path)
    erros = idx.filter(pl.col("tipo") == "erro").height
    log.info("staging divida: %d arquivos (%d com erro de leitura) -> %s", idx.height, erros, path)
    return path
