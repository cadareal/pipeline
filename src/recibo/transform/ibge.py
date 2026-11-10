"""Transform S14: raw do IBGE (planilha da projeção e JSONs do SIDRA) -> staging/stg_ibge_populacao.parquet.

Uma linha por (régua, ano, local): regua (projecao | estimativa | censo), ano, data_referencia (1º de julho, ou a
data da tabela), cod, sigla, local, populacao, homens, mulheres, idosos_65_mais (só a projeção traz sexo e idade),
revisao, tabela, variavel, arquivo e procedência (snapshot_id, sha256_canonical, fetched_at).

Guardas (param o transform com a explicação, sem gravar nada):
  - a aba da projeção ou uma coluna de linhas.yaml (bloco `populacao`) não existe;
  - a população do Brasil num ano sai da faixa de linhas.yaml (brasil_minimo a brasil_maximo): unidade ou coluna errada;
  - a projeção não tem o Brasil.
Tabela do SIDRA marcada `opcional` em sources.yaml com formato estranho vira aviso, não erro.
"""

from __future__ import annotations

import io
import json
import logging
from datetime import date
from pathlib import Path

import polars as pl

from ..fetchers.ibge import DATASET_PROJECAO, DATASET_SIDRA, SOURCE, linhas_sidra
from ..store import Store
from ..textnorm import norm

log = logging.getLogger(__name__)

STAGING_FILE = "stg_ibge_populacao.parquet"
SCHEMA = {
    "regua": pl.Utf8, "ano": pl.Int64, "data_referencia": pl.Date, "cod": pl.Utf8, "sigla": pl.Utf8, "local": pl.Utf8,
    "populacao": pl.Int64, "homens": pl.Int64, "mulheres": pl.Int64, "idosos_65_mais": pl.Int64,
    "revisao": pl.Utf8, "tabela": pl.Utf8, "variavel": pl.Utf8, "arquivo": pl.Utf8,
    "snapshot_id": pl.Utf8, "sha256_canonical": pl.Utf8, "fetched_at": pl.Utf8,
}


class IbgeFormato(RuntimeError):
    """O arquivo do IBGE não tem o desenho esperado."""


def _data_ref(ano: int, mes_dia: str | None) -> date:
    mm, dd = (str(mes_dia or "07-01").split("-") + ["01"])[:2]
    return date(int(ano), int(mm), int(dd))


def _inteiro(v) -> int | None:
    if v is None or (isinstance(v, str) and not v.strip()):
        return None
    if isinstance(v, bool):
        raise ValueError(v)
    if isinstance(v, (int, float)):
        if isinstance(v, float) and abs(v - round(v)) > 1e-6:
            raise ValueError(v)
        return int(round(v))
    t = str(v).strip().replace(".", "").replace(" ", "")
    if t in ("-", "...", "X", "x"):
        return None
    return int(t)


def ler_projecao_xlsx(raw: bytes, cfg_xlsx: dict) -> pl.DataFrame:
    """A planilha tab3: cabeçalho na primeira linha cuja coluna 1 é `cabecalho_coluna1`; colunas por nome."""
    from openpyxl import load_workbook

    wb = load_workbook(io.BytesIO(raw), read_only=True, data_only=True)
    nome_aba = str(cfg_xlsx.get("planilha") or "")
    abas = {ws.title: ws for ws in wb.worksheets}
    ws = abas.get(nome_aba)
    if ws is None:
        # tolera diferença de acento/caixa/espaços no nome da aba, mas diz qual usou
        por_norm = {norm(t): w for t, w in abas.items()}
        ws = por_norm.get(norm(nome_aba))
        if ws is None:
            raise IbgeFormato(f"projeção: aba {nome_aba!r} não existe; abas: {list(abas)}")
        log.warning("projeção: aba %r não achada pelo nome exato; usando %r", nome_aba, ws.title)
    marca = norm(str(cfg_xlsx.get("cabecalho_coluna1") or "ANO"))
    cols_cfg: dict[str, str] = dict(cfg_xlsx.get("colunas") or {})
    for obrig in ("ano", "local", "populacao"):
        if obrig not in cols_cfg:
            raise IbgeFormato(f"linhas.yaml populacao.projecao_xlsx.colunas precisa de {obrig!r}")
    cab_idx: dict[str, int] | None = None
    registros: list[dict] = []
    for row in ws.iter_rows(values_only=True):
        if cab_idx is None:
            if row and row[0] is not None and norm(str(row[0])) == marca:
                nomes = {norm(str(v)): j for j, v in enumerate(row) if v is not None and str(v).strip()}
                cab_idx = {}
                faltam = []
                for papel, nome in cols_cfg.items():
                    j = nomes.get(norm(str(nome)))
                    if j is None:
                        faltam.append(nome)
                    else:
                        cab_idx[papel] = j
                if faltam:
                    raise IbgeFormato(f"projeção: colunas de linhas.yaml ausentes no cabeçalho: {faltam}; cabeçalho: {[str(v) for v in row if v is not None]}")
            continue
        ano_v = row[cab_idx["ano"]] if cab_idx["ano"] < len(row) else None
        if ano_v is None or str(ano_v).strip() == "":
            continue
        try:
            ano = int(str(ano_v).strip())
        except ValueError:
            continue  # linhas de nota depois da tabela

        def cel(papel: str):
            j = cab_idx.get(papel)
            return row[j] if j is not None and j < len(row) else None

        try:
            registros.append({
                "ano": ano,
                "cod": None if cel("cod") is None else str(cel("cod")).strip(),
                "sigla": None if cel("sigla") is None else str(cel("sigla")).strip(),
                "local": str(cel("local")).strip(),
                "populacao": _inteiro(cel("populacao")),
                "homens": _inteiro(cel("homens")),
                "mulheres": _inteiro(cel("mulheres")),
                "idosos_65_mais": _inteiro(cel("idosos_65_mais")),
            })
        except ValueError as e:
            raise IbgeFormato(f"projeção: valor não inteiro na linha do ano {ano}, local {cel('local')!r}: {e}") from None
    if cab_idx is None:
        raise IbgeFormato(f"projeção: nenhuma linha com {cfg_xlsx.get('cabecalho_coluna1')!r} na coluna 1 (cabeçalho não encontrado) na aba {ws.title!r}")
    if not registros:
        raise IbgeFormato(f"projeção: cabeçalho achado, mas nenhuma linha de dados na aba {ws.title!r}")
    return pl.DataFrame(registros, schema={"ano": pl.Int64, "cod": pl.Utf8, "sigla": pl.Utf8, "local": pl.Utf8, "populacao": pl.Int64, "homens": pl.Int64, "mulheres": pl.Int64, "idosos_65_mais": pl.Int64})


def ler_sidra_json(raw: bytes) -> pl.DataFrame:
    """Linhas do SIDRA (n1 = Brasil): ano pela coluna cujo cabeçalho é 'Ano', valor por 'V'."""
    payload = json.loads(raw.decode("utf-8"))
    linhas = linhas_sidra(payload)
    cab = payload[0]
    k_ano = next((k for k, v in cab.items() if k.endswith("N") and norm(str(v)) == "ANO"), None)
    k_var = next((k for k, v in cab.items() if k.endswith("N") and norm(str(v)).startswith("VARIAVEL")), None)
    if k_ano is None:
        raise IbgeFormato(f"SIDRA: nenhuma coluna 'Ano' no cabeçalho {cab}")
    regs = []
    for r in linhas:
        try:
            ano = int(str(r.get(k_ano)).strip())
        except ValueError:
            raise IbgeFormato(f"SIDRA: ano não inteiro: {r.get(k_ano)!r}") from None
        v = r.get("V")
        try:
            pop = _inteiro(v)
        except ValueError:
            raise IbgeFormato(f"SIDRA: valor não inteiro no ano {ano}: {v!r}") from None
        regs.append({"ano": ano, "cod": str(r.get("D1C") or ""), "sigla": None, "local": str(r.get("D1N") or r.get("NN") or ""), "populacao": pop,
                     "variavel_nome": str(r.get(k_var) if k_var else ""), "unidade": str(r.get("MN") or "")})
    if not regs:
        raise IbgeFormato("SIDRA: tabela sem linhas")
    return pl.DataFrame(regs, schema={"ano": pl.Int64, "cod": pl.Utf8, "sigla": pl.Utf8, "local": pl.Utf8, "populacao": pl.Int64, "variavel_nome": pl.Utf8, "unidade": pl.Utf8})


def conferir_escala(df: pl.DataFrame, local: str, minimo: int, maximo: int, origem: str) -> None:
    b = df.filter(pl.col("local") == local)
    if b.height == 0:
        raise IbgeFormato(f"{origem}: não há linha para {local!r}; locais: {sorted(df['local'].unique().to_list())[:12]}")
    fora = b.filter((pl.col("populacao") < minimo) | (pl.col("populacao") > maximo) | pl.col("populacao").is_null())
    if fora.height:
        ex = fora.select("ano", "populacao").head(5).rows()
        raise IbgeFormato(f"{origem}: população de {local} fora da faixa {minimo:,} a {maximo:,} em {fora.height} ano(s), ex.: {ex}. Sintoma de coluna ou unidade errada.")


def build_staging(store: Store, staging_dir: Path, src_cfg: dict, linhas_cfg: dict) -> Path:
    pcfg = linhas_cfg.get("populacao") or {}
    local = str(pcfg.get("local") or "Brasil")
    minimo, maximo = int(pcfg.get("brasil_minimo", 150_000_000)), int(pcfg.get("brasil_maximo", 300_000_000))
    frames: list[pl.DataFrame] = []
    erros: list[str] = []

    def procedencia(df: pl.DataFrame, manifest: dict, regua: str, data_ref: str | None, revisao, tabela, variavel, arquivo) -> pl.DataFrame:
        return df.with_columns(
            pl.lit(regua).alias("regua"),
            pl.col("ano").map_elements(lambda a: _data_ref(int(a), data_ref), return_dtype=pl.Date).alias("data_referencia"),
            pl.lit(None if revisao is None else str(revisao)).alias("revisao"),
            pl.lit(None if tabela is None else str(tabela)).alias("tabela"),
            pl.lit(None if variavel is None else str(variavel)).alias("variavel"),
            pl.lit(arquivo).alias("arquivo"),
            pl.lit(manifest.get("snapshot_id")).alias("snapshot_id"),
            pl.lit(manifest.get("sha256_canonical")).alias("sha256_canonical"),
            pl.lit(manifest.get("fetched_at")).alias("fetched_at"),
        )

    # projeção (a régua do per capita)
    pcfg_x = pcfg.get("projecao_xlsx") or {}
    chaves_proj = store.list_keys(SOURCE, DATASET_PROJECAO)
    for key in chaves_proj:
        got = store.get_latest(SOURCE, DATASET_PROJECAO, key)
        if not got:
            continue
        raw, manifest = got
        meta = manifest.get("projecao") or {}
        try:
            df = ler_projecao_xlsx(raw, pcfg_x)
            conferir_escala(df, local, minimo, maximo, f"projeção {key}")
        except IbgeFormato as e:
            erros.append(str(e))
            log.error("%s", e)
            continue
        frames.append(procedencia(df, manifest, str(meta.get("regua") or "projecao"), meta.get("data_referencia"), meta.get("revisao"), None, None, key))
        b = df.filter(pl.col("local") == local)
        log.info("projeção %s: %d linhas, %d locais, %s a %s; %s em %s: %s", key, df.height, df["local"].n_unique(), df["ano"].min(), df["ano"].max(), local, b["ano"].max(), f"{int(b.filter(pl.col('ano') == b['ano'].max())['populacao'][0]):,}")
    if not chaves_proj:
        log.warning("nenhum raw da projeção do IBGE (rode `recibo fetch ibge`)")

    # SIDRA (estimativas, censo)
    for key in store.list_keys(SOURCE, DATASET_SIDRA):
        got = store.get_latest(SOURCE, DATASET_SIDRA, key)
        if not got:
            continue
        raw, manifest = got
        meta = manifest.get("sidra") or {}
        regua = str(meta.get("regua") or "sidra")
        opcional = bool(next((t.get("opcional") for t in (src_cfg.get("sidra") or []) if str(t.get("tabela")) == str(meta.get("tabela"))), False))
        try:
            df = ler_sidra_json(raw)
            conferir_escala(df, local, minimo, maximo, f"SIDRA {key}")
        except (IbgeFormato, ValueError) as e:
            if opcional:
                log.warning("SIDRA %s (opcional): %s; seguindo sem ela", key, e)
                continue
            erros.append(f"SIDRA {key}: {e}")
            log.error("SIDRA %s: %s", key, e)
            continue
        df = df.with_columns(pl.lit(None, dtype=pl.Int64).alias("homens"), pl.lit(None, dtype=pl.Int64).alias("mulheres"), pl.lit(None, dtype=pl.Int64).alias("idosos_65_mais")).drop("variavel_nome", "unidade")
        frames.append(procedencia(df, manifest, regua, meta.get("data_referencia"), None, meta.get("tabela"), meta.get("variavel"), key))
        log.info("SIDRA %s (%s): %d anos, %s a %s", key, regua, df.height, df["ano"].min(), df["ano"].max())

    if erros:
        raise IbgeFormato(f"{len(erros)} arquivo(s) do IBGE com formato inesperado; nada foi gravado:\n  - " + "\n  - ".join(erros))
    if not frames:
        raise SystemExit("nenhum raw do IBGE encontrado; rode `recibo fetch ibge` antes")
    out = pl.concat([f.select(list(SCHEMA)).cast(SCHEMA) for f in frames], how="vertical").sort(["regua", "local", "ano"])
    staging_dir.mkdir(parents=True, exist_ok=True)
    path = staging_dir / STAGING_FILE
    out.write_parquet(path)
    log.info("staging ibge: %d linhas (%s) -> %s", out.height, ", ".join(f"{r}: {n}" for r, n in out.group_by("regua").len().sort("regua").rows()), path)
    return path
