"""Fetcher S14: população do Brasil pelo IBGE, em duas réguas lacradas.

  projecao  -> a planilha das Projeções da População, revisão 2024 (ftp.ibge.gov.br), um arquivo XLSX com a população
               de 1º de julho de cada ano, 2000 a 2070, para Brasil, regiões e UFs. É a régua do per capita.
  sidra     -> tabelas do SIDRA (apisidra.ibge.gov.br), só Brasil e todos os anos: as estimativas oficiais (6579, a
               conferência da projeção) e, opcional, o Censo 2022 (4709, só para a nota).

Não há catálogo a consultar (de fora do Brasil o IBGE barra o servicodados e o www): cada arquivo é baixado inteiro e o
lacre por hash do store decide se há snapshot novo. Uma fonte que não responde vira erro no log e no índice; o raw já
lacrado continua valendo, porque população muda uma vez por ano.

Raw:
  raw/ibge/projecao/{arquivo}/{snapshot}/{arquivo}          hash canônico = o próprio arquivo
  raw/ibge/sidra/t{tabela}_v{variavel}/{snapshot}/sidra.json hash canônico = linhas ordenadas (ano, variável), sem o cabeçalho
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from urllib.parse import urlparse

from ..http import Http
from ..store import Store, utcnow_iso

log = logging.getLogger(__name__)

SOURCE = "ibge"
DATASET_PROJECAO = "projecao"
DATASET_SIDRA = "sidra"


@dataclass
class FetchResultIbge:
    dataset: str
    chave: str
    status: str  # ok | erro | vazio
    changed: bool
    snapshot_id: str | None
    bytes: int = 0
    n_linhas: int | None = None
    opcional: bool = False


def nome_arquivo(url: str) -> str:
    return urlparse(url).path.rstrip("/").split("/")[-1]


def chave_sidra(tabela: int | str, variavel: int | str) -> str:
    return f"t{tabela}_v{variavel}"


def parece_html(content: bytes) -> bool:
    inicio = content[:300].lstrip().lower()
    return inicio.startswith(b"<!doctype html") or inicio.startswith(b"<html")


def linhas_sidra(payload) -> list[dict]:
    """O SIDRA devolve uma lista cujo primeiro item é o cabeçalho (nomes das colunas); o resto são as linhas."""
    if not isinstance(payload, list) or not payload or not isinstance(payload[0], dict):
        raise ValueError("resposta do SIDRA não é uma lista de registros com cabeçalho")
    return [r for r in payload[1:] if isinstance(r, dict)]


def canonical_sidra(payload: list) -> bytes:
    linhas = linhas_sidra(payload)
    ordenadas = sorted(({k: str(v) for k, v in r.items()} for r in linhas), key=lambda r: (r.get("D3C", ""), r.get("D2C", ""), r.get("D1C", ""), r.get("V", "")))
    return json.dumps(ordenadas, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")


class IbgeFetcher:
    def __init__(self, http: Http, store: Store, src_cfg: dict):
        self.http = http
        self.store = store
        self.cfg = src_cfg

    def projecao(self) -> FetchResultIbge:
        c = self.cfg.get("projecao") or {}
        url = str(c.get("url") or "")
        arquivo = nome_arquivo(url)
        if not url or not arquivo:
            return FetchResultIbge(DATASET_PROJECAO, arquivo or "?", "erro", False, None)
        r = self.http.get(url)
        content = r.content
        if not content or parece_html(content) or not content.startswith(b"PK"):
            log.error("projeção IBGE: %s não veio como XLSX (%d bytes, content-type %s); não gravando", arquivo, len(content), r.headers.get("content-type"))
            return FetchResultIbge(DATASET_PROJECAO, arquivo, "erro", False, None, len(content))
        res = self.store.put(
            source=SOURCE,
            dataset=DATASET_PROJECAO,
            key=arquivo,
            filename=arquivo,
            content=content,
            meta={"request": {"url": url}, "projecao": {"revisao": c.get("revisao"), "regua": c.get("regua", "projecao"), "data_referencia": c.get("data_referencia")},
                  "http": {"status": r.status_code, "content_type": r.headers.get("content-type"), "last_modified": r.headers.get("last-modified"), "url": str(r.url)[:2000]}},
        )
        log.info("projeção IBGE: %s, %.2f MB, %s", arquivo, len(content) / 1e6, "NOVO/ALTERADO" if res.changed else "sem mudança")
        return FetchResultIbge(DATASET_PROJECAO, arquivo, "ok", res.changed, res.snapshot_id, len(content))

    def sidra(self, t: dict) -> FetchResultIbge:
        chave = chave_sidra(t["tabela"], t["variavel"])
        opcional = bool(t.get("opcional", False))
        url = str(t["url"])
        r = self.http.get(url)
        try:
            payload = r.json()
            linhas = linhas_sidra(payload)
        except ValueError as e:
            log.error("SIDRA %s: resposta inesperada (%s): %r; não gravando", chave, e, (r.text or "")[:160])
            return FetchResultIbge(DATASET_SIDRA, chave, "erro", False, None, len(r.content), opcional=opcional)
        if not linhas:
            log.warning("SIDRA %s: sem linhas", chave)
            return FetchResultIbge(DATASET_SIDRA, chave, "vazio", False, None, len(r.content), 0, opcional)
        res = self.store.put(
            source=SOURCE,
            dataset=DATASET_SIDRA,
            key=chave,
            filename="sidra.json",
            content=r.content,
            canonical=canonical_sidra(payload),
            meta={"request": {"url": url}, "sidra": {"id": t.get("id"), "tabela": t["tabela"], "variavel": t["variavel"], "regua": t.get("regua"), "data_referencia": t.get("data_referencia"), "o_que": t.get("o_que"), "n_linhas": len(linhas)},
                  "http": {"status": r.status_code, "content_type": r.headers.get("content-type"), "url": str(r.url)[:2000]}},
        )
        anos = sorted(str(x.get("D3C") or x.get("D3N") or "") for x in linhas)
        log.info("SIDRA %s (%s): %d linhas, %s a %s, %s", chave, t.get("id"), len(linhas), anos[0], anos[-1], "NOVO/ALTERADO" if res.changed else "sem mudança")
        return FetchResultIbge(DATASET_SIDRA, chave, "ok", res.changed, res.snapshot_id, len(r.content), len(linhas), opcional)


def run_fetch(http: Http, store: Store, src_cfg: dict, force: bool = False) -> list[FetchResultIbge]:
    """Baixa a projeção e as tabelas do SIDRA de sources.yaml. `force` não muda nada aqui (sempre baixa inteiro; o
    store só cria snapshot se o conteúdo mudou); fica pela simetria com as outras fontes. Uma fonte com erro não
    derruba as outras; uma tabela `opcional` com erro vira aviso."""
    f = IbgeFetcher(http, store, src_cfg)
    results: list[FetchResultIbge] = []
    try:
        results.append(f.projecao())
    except Exception as e:  # noqa: BLE001
        log.error("projeção IBGE: falha ao baixar (%s)", e)
        results.append(FetchResultIbge(DATASET_PROJECAO, nome_arquivo(str((src_cfg.get("projecao") or {}).get("url") or "")), "erro", False, None))
    for t in src_cfg.get("sidra") or []:
        try:
            results.append(f.sidra(t))
        except Exception as e:  # noqa: BLE001
            nivel = log.warning if t.get("opcional") else log.error
            nivel("SIDRA %s: falha ao baixar (%s)%s", chave_sidra(t["tabela"], t["variavel"]), e, " (opcional)" if t.get("opcional") else "")
            results.append(FetchResultIbge(DATASET_SIDRA, chave_sidra(t["tabela"], t["variavel"]), "erro", False, None, opcional=bool(t.get("opcional"))))
    for dataset in (DATASET_PROJECAO, DATASET_SIDRA):
        index = store.read_index(SOURCE, dataset)
        for r in results:
            if r.dataset != dataset:
                continue
            prev = index.get(r.chave, {})
            index[r.chave] = {
                "status": r.status if r.status != "erro" else prev.get("status", "erro"),
                "snapshot_id": r.snapshot_id or prev.get("snapshot_id"),
                "bytes": r.bytes or prev.get("bytes", 0),
                "n_linhas": r.n_linhas if r.n_linhas is not None else prev.get("n_linhas"),
                "opcional": r.opcional,
                "last_checked": utcnow_iso(),
                "changed_last_run": r.changed,
                "ultimo_erro": None if r.status != "erro" else utcnow_iso(),
            }
        store.write_index(SOURCE, dataset, index)
    log.info("fetch ibge: %d itens, %d ok, %d novos/alterados, %d com erro", len(results), sum(1 for r in results if r.status == "ok"),
             sum(1 for r in results if r.changed), sum(1 for r in results if r.status == "erro"))
    return results
