"""Transform S16: raw das séries do SGS (JSON do Banco Central) -> staging/stg_bcb_sgs.parquet.

Uma linha por ponto: codigo, data (Date), valor (Float64), valor_texto (como veio), mais a ficha de sources.yaml (nome,
tipo, unidade, uso) e, quando o fetch achou, o título do dataset no portal de dados abertos do BC. Procedência por
linha: snapshot_id, sha256_canonical, fetched_at, primeira e última data da série.

Guardas: data fora de dd/mm/aaaa ou valor não numérico param o transform com a lista do que não leu (o BC manda ponto
decimal no JSON; se um dia vier vírgula, é aqui que aparece). Série em sources.yaml sem raw vira aviso, não erro.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import polars as pl

from ..fetchers.bcb import DATASET, DATASET_META, SOURCE
from ..store import Store
from .cofog import CofogFormato

log = logging.getLogger(__name__)

STAGING_FILE = "stg_bcb_sgs.parquet"


def pontos_para_df(codigo: int, pontos: list[dict]) -> pl.DataFrame:
    df = pl.DataFrame({"codigo": [codigo] * len(pontos), "data_texto": [str(p.get("data")) for p in pontos], "valor_texto": [str(p.get("valor")) for p in pontos]},
                      schema={"codigo": pl.Int64, "data_texto": pl.Utf8, "valor_texto": pl.Utf8})
    df = df.with_columns(
        pl.col("data_texto").str.strptime(pl.Date, "%d/%m/%Y", strict=False).alias("data"),
        pl.col("valor_texto").str.strip_chars().str.replace(",", ".").cast(pl.Float64, strict=False).alias("valor"),
    )
    ruins_data = df.filter(pl.col("data").is_null())
    ruins_valor = df.filter(pl.col("valor").is_null() & (pl.col("valor_texto").str.strip_chars() != "") & (pl.col("valor_texto") != "None"))
    if ruins_data.height or ruins_valor.height:
        raise CofogFormato(
            f"SGS {codigo}: {ruins_data.height} datas fora de dd/mm/aaaa (ex.: {ruins_data['data_texto'].head(3).to_list()}) e "
            f"{ruins_valor.height} valores não numéricos (ex.: {ruins_valor['valor_texto'].head(3).to_list()})"
        )
    return df.sort("data")


def build_staging(store: Store, staging_dir: Path, src_cfg: dict | None = None) -> Path:
    cfg = src_cfg or {}
    fichas = {int(s["codigo"]): s for s in (cfg.get("series") or [])}
    frames: list[pl.DataFrame] = []
    erros: list[str] = []
    chaves = store.list_keys(SOURCE, DATASET)
    for key in chaves:
        got = store.get_latest(SOURCE, DATASET, key)
        if not got:
            continue
        raw, manifest = got
        codigo = int(key)
        try:
            pontos = json.loads(raw.decode("utf-8"))
            df = pontos_para_df(codigo, pontos)
        except (CofogFormato, ValueError) as e:
            erros.append(str(e))
            log.error("%s", e)
            continue
        ficha = fichas.get(codigo, {})
        titulo_bc = None
        meta = store.get_latest(SOURCE, DATASET_META, key)
        if meta:
            try:
                titulo_bc = json.loads(meta[0].decode("utf-8")).get("title")
            except ValueError:
                titulo_bc = None
        df = df.with_columns(
            pl.lit(ficha.get("nome")).alias("nome"),
            pl.lit(ficha.get("tipo")).alias("tipo"),
            pl.lit(ficha.get("unidade")).alias("unidade"),
            pl.lit(ficha.get("uso")).alias("uso"),
            pl.lit(titulo_bc).alias("titulo_bc"),
            pl.lit(manifest.get("snapshot_id")).alias("snapshot_id"),
            pl.lit(manifest.get("sha256_canonical")).alias("sha256_canonical"),
            pl.lit(manifest.get("fetched_at")).alias("fetched_at"),
            pl.col("data").min().alias("primeira_data"),
            pl.col("data").max().alias("ultima_data"),
        )
        frames.append(df)
        log.info("SGS %s (%s): %d pontos, %s a %s", codigo, ficha.get("nome") or titulo_bc or "?", df.height, df["data"].min(), df["data"].max())
    faltando = sorted(set(fichas) - {int(k) for k in chaves})
    if faltando:
        log.warning("séries em sources.yaml sem raw (rode `recibo fetch bcb`): %s", faltando)
    if erros:
        raise CofogFormato(f"{len(erros)} série(s) com formato inesperado; nada foi gravado:\n  - " + "\n  - ".join(erros))
    if not frames:
        raise SystemExit("nenhum raw do BCB encontrado; rode `recibo fetch bcb` antes")
    out = pl.concat(frames, how="vertical_relaxed")
    staging_dir.mkdir(parents=True, exist_ok=True)
    path = staging_dir / STAGING_FILE
    out.write_parquet(path)
    log.info("staging bcb: %d séries, %d pontos -> %s", len(frames), out.height, path)
    return path
