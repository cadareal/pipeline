"""Transform S8: raw do SIOP (result.json por chave) -> staging (stg_siop_execucao.parquet).

Uma linha por combinação de códigos de uma agregação, com os cinco valores somados e o número de itens.
Todas as agregações vão para a mesma tabela; a coluna `agregacao` diz qual. Dimensões ausentes numa agregação
ficam nulas. Rótulos vêm das chaves {ex}/membros/{dimensao} (código -> rótulo), nunca da própria agregação.

Derivações:
  intra          : modalidade de aplicação 91 (operação entre órgãos do próprio orçamento). É o que o RREO chama de
                   "intraorçamentária" e deixa fora do total (I).
  esfera_tipo    : fiscal | seguridade | investimento | desconhecida, pelo rótulo da esfera. O RREO cobre só os
                   orçamentos fiscal e da seguridade (OFSS); o orçamento de investimento das estatais fica fora.
  ofss           : esfera_tipo em (fiscal, seguridade)
  refinanciamento: GND 6 (amortização) pago com fonte de refinanciamento (código da fonte terminado em 443, lista em
                   sources.yaml). É exatamente como o RREO monta a linha XII do Anexo 01 (confirmado em 2025: R$ 1.417,6 bi
                   nas duas fontes); o Anexo 02 não a inclui. A fonte 444 ("demais aplicações" dos títulos) NÃO é rolagem:
                   paga juros, a cobertura do resultado do Banco Central e benefícios do INSS (regra de ouro).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import polars as pl

from ..fetchers.siop import DATASET, DIMENSOES, SOURCE, VALORES, bindings
from ..store import Store
from ..textnorm import norm

log = logging.getLogger(__name__)

STAGING_FILE = "stg_siop_execucao.parquet"
FONTES_REFINANCIAMENTO_PADRAO = ["443"]
COLS_COD = [f"cod_{d}" for d in DIMENSOES]
COLS_NOME = [f"nome_{d}" for d in DIMENSOES]


def _carregar(store: Store) -> tuple[dict[int, str | None], dict[tuple[int, str], dict[str, str]], list[tuple[dict, list[dict]]]]:
    """Lê todas as chaves do raw: datas por exercício, rótulos por (exercício, dimensão) e agregações (manifest, linhas)."""
    datas: dict[int, str | None] = {}
    rotulos: dict[tuple[int, str], dict[str, str]] = {}
    agregacoes: list[tuple[dict, list[dict]]] = []
    for key in store.list_keys(SOURCE, DATASET):
        got = store.get_latest(SOURCE, DATASET, key)
        if not got:
            continue
        raw, manifest = got
        rows = bindings(json.loads(raw.decode("utf-8")))
        partes = key.split("/")
        ex = int(partes[0])
        if len(partes) == 2 and partes[1] == "exercicio":
            datas[ex] = manifest.get("data_atualizacao") or (max((r.get("data") or "" for r in rows), default=None) or None)
        elif len(partes) == 3 and partes[1] == "membros":
            rotulos[(ex, partes[2])] = {r["codigo"]: (r.get("rotulo") or "") for r in rows if r.get("codigo") is not None}
        elif manifest.get("agregacao"):
            agregacoes.append((manifest, rows))
        else:
            log.warning("chave raw não reconhecida, ignorando: %s", key)
    return datas, rotulos, agregacoes


def esfera_tipo(rotulo: str | None) -> str:
    r = norm(rotulo or "")
    if "SEGURIDADE" in r:
        return "seguridade"
    if "FISCAL" in r:
        return "fiscal"
    if "INVESTIMENTO" in r:
        return "investimento"
    return "desconhecida"


def eh_refinanciamento(cod_gnd: str | None, cod_fonte: str | None, fontes: list[str]) -> bool:
    """Rolagem como o RREO define: amortização (GND 6) com fonte cujo código (sem o dígito de exercício) está na lista."""
    if cod_gnd != "6" or not cod_fonte:
        return False
    return any(str(cod_fonte).endswith(str(f)) for f in fontes)


def linhas_staging(datas: dict, rotulos: dict, agregacoes: list[tuple[dict, list[dict]]], fontes_refinanciamento: list[str] | None = None) -> list[dict]:
    fontes = [str(f) for f in (fontes_refinanciamento or FONTES_REFINANCIAMENTO_PADRAO)]
    out: list[dict] = []
    for manifest, rows in agregacoes:
        ex = int(str(manifest["key"]).split("/")[0])
        ag = manifest["agregacao"]
        dims = list(manifest.get("dimensoes") or [])
        for r in rows:
            linha: dict = {"exercicio": ex, "agregacao": ag, "fatia_funcao": manifest.get("funcao")}
            for d in DIMENSOES:
                cod = r.get(f"cod_{d}") if d in dims else None
                linha[f"cod_{d}"] = cod
                linha[f"nome_{d}"] = rotulos.get((ex, d), {}).get(cod) if cod is not None else None
            for m in VALORES:
                try:
                    linha[m] = float(r.get(m) or 0.0)
                except ValueError:
                    linha[m] = 0.0
            try:
                linha["n_itens"] = int(float(r.get("n_itens") or 0))
            except ValueError:
                linha["n_itens"] = 0
            linha["intra"] = (linha["cod_modalidade_aplicacao"] == "91") if "modalidade_aplicacao" in dims else None
            tipo = esfera_tipo(linha["nome_esfera"]) if "esfera" in dims else "desconhecida"
            linha["esfera_tipo"] = tipo
            linha["ofss"] = tipo in ("fiscal", "seguridade") if "esfera" in dims else None
            linha["refinanciamento"] = eh_refinanciamento(linha["cod_gnd"], linha["cod_fonte"], fontes) if ("fonte" in dims and "gnd" in dims) else None
            linha["data_atualizacao"] = datas.get(ex)
            linha["raw_key"] = manifest["key"]
            linha["snapshot_id"] = manifest["snapshot_id"]
            linha["fetched_at"] = manifest["fetched_at"]
            linha["sha256_canonical"] = manifest["sha256_canonical"]
            out.append(linha)
    return out


def to_frame(rows: list[dict]) -> pl.DataFrame:
    df = pl.from_dicts(rows, infer_schema_length=None)
    casts = [pl.col(m).cast(pl.Float64, strict=False) for m in VALORES if m in df.columns]
    casts += [pl.col(c).cast(pl.Utf8, strict=False) for c in COLS_COD + COLS_NOME + ["fatia_funcao", "data_atualizacao"] if c in df.columns]
    casts += [pl.col(c).cast(pl.Boolean, strict=False) for c in ("intra", "ofss", "refinanciamento") if c in df.columns]
    if "exercicio" in df.columns:
        casts.append(pl.col("exercicio").cast(pl.Int64, strict=False))
    if "n_itens" in df.columns:
        casts.append(pl.col("n_itens").cast(pl.Int64, strict=False))
    return df.with_columns(casts) if casts else df


def build_staging(store: Store, staging_dir: Path, src_cfg: dict | None = None) -> Path:
    fontes = list((src_cfg or {}).get("fontes_refinanciamento") or FONTES_REFINANCIAMENTO_PADRAO)
    datas, rotulos, agregacoes = _carregar(store)
    rows = linhas_staging(datas, rotulos, agregacoes, fontes)
    if not rows:
        raise SystemExit("nenhum raw do SIOP encontrado; rode `recibo fetch siop` antes")
    df = to_frame(rows)
    staging_dir.mkdir(parents=True, exist_ok=True)
    path = staging_dir / STAGING_FILE
    df.write_parquet(path)
    log.info("staging siop: %d agregações/fatias, %d linhas -> %s", len(agregacoes), df.height, path)
    return path
