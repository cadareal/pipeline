"""Transform S4: raw (pages.jsonl) -> staging (stg_siconfi_rreo.parquet).

Mantém todas as colunas da API e acrescenta derivações:
  anexo_norm, coluna_norm, conta_norm, rotulo_norm : texto normalizado (sem acento, maiúsculas)
  escopo              : exceto_intra | intra | desconhecido. Vem do `rotulo` (a API repete o nome da seção em
                        toda linha); se o rotulo não disser, usa o próprio `conta` quando ele é o cabeçalho, e
                        por último o forward-fill a partir do último cabeçalho visto.
  nivel               : total | funcao | subfuncao | conta
  funcao_cod          : na linha de função, o próprio código; na de subfunção, o da função-mãe
  funcao_cod_prefixo  : código vindo do prefixo "FUxx - " (União), quando existe
  funcao_cod_ordem    : código da última função vista na ordem da API
  subfuncao_cod       : código da subfunção ('122' para "FUxx - Administração Geral"; 'demais' para o balde
                        "FUxx - Demais Subfunções"; None quando o nome não está na referência)
  is_header           : True nas linhas cujo `conta` é "DESPESAS (EXCETO INTRA-ORÇAMENTÁRIAS)" / "(INTRA-...)"
  header_escopo       : nas linhas is_header, o escopo dito pelo próprio `conta` (exceto_intra | intra). Necessário
                        porque o bloco I do Anexo 02 contém também a linha-resumo "DESPESAS (INTRA-ORÇAMENTÁRIAS) (II)",
                        cujo rotulo é o do bloco I; só o `conta` distingue o cabeçalho (I) desse resumo (II).
  fu_inconsistente    : True quando o prefixo FUxx discorda da função vista na ordem (hierarquia suspeita)
  ordem               : posição da linha dentro de (chave raw, coluna), como a API devolveu

Calibrado em 2026-11-08 com o RREO 2024/6º bimestre da União:
  - `cod_conta` do Anexo 02 é constante ("RREO2TotalDespesas"): não serve para hierarquia.
  - A União lista subfunções típicas pelo nome e agrega as atípicas em "FUxx - Demais Subfunções".
  - `cod_conta` do Anexo 01 é um código estável (JurosEEncargosDaDivida, AmortizacaoDaDivida...); as linhas
    intraorçamentárias têm sufixo "Intra". Os filtros do Anexo 01 usam cod_conta, não escopo.
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
RE_FU = re.compile(r"^FU(\d{2})\s*-\s*(.+)$")

SUBFUNCAO_DEMAIS = "demais"

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


def _escopo_de(texto: str) -> str | None:
    if RE_EXCETO_INTRA.search(texto):
        return "exceto_intra"
    if RE_INTRA.search(texto):
        return "intra"
    return None


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
        escopo_ff = "desconhecido"
        funcao_ordem: str | None = None

        for i, r in enumerate(grp):
            conta_norm = norm(r.get("conta"))
            rotulo_norm = norm(r.get("rotulo"))
            code, label = split_leading_code(conta_norm)

            header_escopo = _escopo_de(conta_norm)
            is_header = header_escopo is not None
            if is_header:
                escopo_ff = header_escopo
            escopo = _escopo_de(rotulo_norm) or header_escopo or escopo_ff

            nivel = "conta"
            funcao_cod: str | None = None
            funcao_prefixo: str | None = None
            subfuncao_cod: str | None = None
            fu_inconsistente = False

            if is_header or RE_TOTAL.match(conta_norm):
                nivel = "total"
                funcao_ordem = None
            elif is_anexo02:
                m_fu = RE_FU.match(conta_norm)
                if m_fu:
                    funcao_prefixo, fu_label = m_fu.group(1), m_fu.group(2).strip()
                    nivel = "subfuncao"
                    funcao_cod = funcao_prefixo
                    if fu_label.startswith("DEMAIS SUBFUNC"):
                        subfuncao_cod = SUBFUNCAO_DEMAIS
                    else:
                        subfuncao_cod = subfuncoes.get(fu_label)
                    fu_inconsistente = funcao_ordem is not None and funcao_ordem != funcao_prefixo
                else:
                    fcode = code if (code in func_codes and len(code) == 2) else funcoes.get(label)
                    if fcode:
                        nivel, funcao_cod, funcao_ordem = "funcao", fcode, fcode
                    else:
                        nivel, funcao_cod = "subfuncao", funcao_ordem
                        subfuncao_cod = code if (code in sub_codes and len(code) == 3) else subfuncoes.get(label)

            r2 = dict(r)
            r2.update(
                {
                    "anexo_norm": anexo_norm,
                    "coluna_norm": coluna_norm,
                    "conta_norm": conta_norm,
                    "rotulo_norm": rotulo_norm,
                    "conta_cod_prefixo": code,
                    "escopo": escopo,
                    "nivel": nivel,
                    "funcao_cod": funcao_cod,
                    "funcao_cod_prefixo": funcao_prefixo,
                    "funcao_cod_ordem": funcao_ordem if nivel == "subfuncao" else None,
                    "subfuncao_cod": subfuncao_cod,
                    "is_header": is_header,
                    "header_escopo": header_escopo,
                    "fu_inconsistente": fu_inconsistente,
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
