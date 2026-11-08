"""Transform S4: raw (pages.jsonl) -> staging (stg_siconfi_rreo.parquet).

Mantém todas as colunas da API e acrescenta derivações:
  anexo_norm, coluna_norm, conta_norm  : texto normalizado (sem acento, maiúsculas)
  escopo                               : exceto_intra | intra | desconhecido (forward-fill a partir dos cabeçalhos)
  nivel                                : total | funcao | subfuncao | conta
  funcao_cod                           : código da função (na linha de função, o próprio; na de subfunção, o da função-mãe)
  subfuncao_cod                        : código da subfunção quando reconhecida
  is_header                            : True nas linhas "DESPESAS (EXCETO INTRAORÇAMENTÁRIAS)" / "(INTRAORÇAMENTÁRIAS)"
  ordem                                : posição da linha dentro de (chave raw, coluna), como a API devolveu

A hierarquia função -> subfunção é derivada da ORDEM das linhas (uma subfunção pertence à última função vista).
Isso é validado pela reconciliação "hierarquia" (soma das subfunções = soma das funções). Se falhar, o
cod_conta da API provavelmente carrega a hierarquia; `recibo inspect` mostra os valores para calibrar.
"""

from __future__ import annotations

import csv
import json
import logging
import re
from pathlib import Path

import polars as pl

from ..store import Store
from ..textnorm import norm, split_leading_code

log = logging.getLogger(__name__)

SOURCE = "siconfi"
DATASET = "rreo"
STAGING_FILE = "stg_siconfi_rreo.parquet"

RE_EXCETO_INTRA = re.compile(r"EXCETO\s+INTRA[- ]?ORCAMENTARIAS")
RE_INTRA = re.compile(r"\bINTRA[- ]?ORCAMENTARIAS")
RE_TOTAL = re.compile(r"^(TOTAL|SUBTOTAL|SUPERAVIT|DEFICIT)\b")
RE_ANEXO02 = re.compile(r"^RREO-ANEXO 0?2$")

API_COLS = [
    "exercicio", "demonstrativo", "periodo", "periodicidade", "instituicao", "cod_ibge", "uf",
    "populacao", "anexo", "esfera", "rotulo", "coluna", "cod_conta", "conta", "valor",
]


def load_ref(config_dir: Path) -> tuple[dict[str, str], dict[str, str]]:
    """Retorna (nome_norm -> código) para funções e subfunções. Vários nomes podem apontar para o mesmo código."""
    funcoes: dict[str, str] = {}
    with open(config_dir / "ref" / "funcoes.csv", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            funcoes[norm(row["nome"])] = row["codigo"]
    subfuncoes: dict[str, str] = {}
    with open(config_dir / "ref" / "subfuncoes.csv", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            subfuncoes[norm(row["nome"])] = row["codigo"]
    return funcoes, subfuncoes


def parse_pages(raw: bytes) -> list[dict]:
    items: list[dict] = []
    for line in raw.decode("utf-8").splitlines():
        if not line.strip():
            continue
        body = json.loads(line)
        items.extend(body.get("items", []) or [])
    return items


def normalize_items(items: list[dict], manifest: dict) -> list[dict]:
    out = []
    for it in items:
        row = {str(k).lower(): v for k, v in it.items()}
        for c in API_COLS:
            row.setdefault(c, None)
        row["raw_key"] = manifest["key"]
        row["snapshot_id"] = manifest["snapshot_id"]
        row["fetched_at"] = manifest["fetched_at"]
        row["sha256_canonical"] = manifest["sha256_canonical"]
        out.append(row)
    return out


def derive(rows: list[dict], funcoes: dict[str, str], subfuncoes: dict[str, str]) -> list[dict]:
    func_codes = set(funcoes.values())
    sub_codes = set(subfuncoes.values())

    groups: dict[tuple[str, str], list[dict]] = {}
    for r in rows:
        groups.setdefault((r["raw_key"], norm(r.get("coluna"))), []).append(r)

    out: list[dict] = []
    for (_key, coluna_norm), grp in groups.items():
        anexo_norm = norm(grp[0].get("anexo"))
        is_anexo02 = bool(RE_ANEXO02.match(anexo_norm))
        escopo = "desconhecido"
        funcao_atual: str | None = None

        for i, r in enumerate(grp):
            conta_norm = norm(r.get("conta"))
            code, label = split_leading_code(conta_norm)
            hint = " | ".join([norm(r.get("rotulo")), norm(r.get("cod_conta")), conta_norm])

            is_header = False
            if RE_EXCETO_INTRA.search(hint):
                escopo, is_header = "exceto_intra", True
            elif RE_INTRA.search(hint):
                escopo, is_header = "intra", True

            nivel = "conta"
            funcao_cod: str | None = None
            subfuncao_cod: str | None = None
            if is_header or RE_TOTAL.match(conta_norm):
                nivel = "total"
                funcao_atual = None
            elif is_anexo02:
                fcode = code if (code in func_codes and len(code) == 2) else funcoes.get(label)
                if fcode:
                    nivel, funcao_cod, funcao_atual = "funcao", fcode, fcode
                else:
                    nivel, funcao_cod = "subfuncao", funcao_atual
                    subfuncao_cod = code if (code in sub_codes and len(code) == 3) else subfuncoes.get(label)

            r2 = dict(r)
            r2.update(
                {
                    "anexo_norm": anexo_norm,
                    "coluna_norm": coluna_norm,
                    "conta_norm": conta_norm,
                    "conta_cod_prefixo": code,
                    "escopo": escopo,
                    "nivel": nivel,
                    "funcao_cod": funcao_cod,
                    "subfuncao_cod": subfuncao_cod,
                    "is_header": is_header,
                    "ordem": i,
                }
            )
            out.append(r2)
    return out


def rows_for_key(store: Store, config_dir: Path, key: str) -> list[dict]:
    got = store.get_latest(SOURCE, DATASET, key)
    if not got:
        raise SystemExit(f"sem raw para a chave {key}; rode `recibo fetch siconfi` antes")
    raw, manifest = got
    funcoes, subfuncoes = load_ref(config_dir)
    return derive(normalize_items(parse_pages(raw), manifest), funcoes, subfuncoes)


def to_frame(rows: list[dict]) -> pl.DataFrame:
    df = pl.from_dicts(rows, infer_schema_length=None)
    casts = []
    if "valor" in df.columns:
        casts.append(pl.col("valor").cast(pl.Float64, strict=False))
    for c in ("exercicio", "periodo"):
        if c in df.columns:
            casts.append(pl.col(c).cast(pl.Int64, strict=False))
    for c in ("cod_ibge", "populacao", "cod_conta", "rotulo", "uf", "instituicao", "demonstrativo", "periodicidade", "esfera"):
        if c in df.columns:
            casts.append(pl.col(c).cast(pl.Utf8, strict=False))
    return df.with_columns(casts) if casts else df


def build_staging(store: Store, config_dir: Path, staging_dir: Path) -> Path:
    funcoes, subfuncoes = load_ref(config_dir)
    all_rows: list[dict] = []
    keys = store.list_keys(SOURCE, DATASET)
    for key in keys:
        got = store.get_latest(SOURCE, DATASET, key)
        if not got:
            continue
        raw, manifest = got
        all_rows.extend(derive(normalize_items(parse_pages(raw), manifest), funcoes, subfuncoes))
    if not all_rows:
        raise SystemExit("nenhum raw encontrado; rode `recibo fetch siconfi` antes")
    df = to_frame(all_rows)
    staging_dir.mkdir(parents=True, exist_ok=True)
    path = staging_dir / STAGING_FILE
    df.write_parquet(path)
    log.info("staging: %d chaves, %d linhas -> %s", len(keys), df.height, path)
    return path
