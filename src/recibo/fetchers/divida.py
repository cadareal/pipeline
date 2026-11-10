"""Fetcher S1: Dívida Pública Federal, datasets do CKAN do Tesouro Transparente (estoque, fatores de variação, emissões e
resgates, execução orçamentária da DPF, estoque do Tesouro Direto).

Mesmo desenho do COFOG, só que com vários datasets e vários recursos por dataset:
  GET {base}/package_show?id={dataset_id}   -> lista de recursos do dataset (lacrada no raw)
  GET {url do recurso}                       -> o arquivo (CSV ou XLSX), gravado byte a byte

Raw:
  raw/divida/{dataset}/package/{snapshot}/package_show.json
  raw/divida/{dataset}/{recurso}/{snapshot}/{arquivo}       chave do recurso = nome do arquivo na URL (com extensão)

Um recurso só é baixado de novo quando a data de modificação (ou de criação) ou o tamanho mudaram no CKAN (ou com
--force). Hash diferente em chave já existente vira snapshot novo e aviso de REVISAO NA FONTE.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass

from ..http import Http
from ..store import Store, safe_key, utcnow_iso
from .cofog import KEY_PACKAGE, canonical_package

log = logging.getLogger(__name__)

SOURCE = "divida"
FORMATOS_PADRAO = ("CSV", "XLSX", "XLS")


@dataclass
class RecursoDivida:
    dataset: str
    id: str
    nome: str
    url: str
    formato: str
    created: str | None
    last_modified: str | None
    size: int | None

    @property
    def versao(self) -> str:
        return f"{self.last_modified or self.created or ''}|{self.size or ''}"

    @property
    def arquivo(self) -> str:
        return self.url.rsplit("/", 1)[-1] or f"{self.dataset}.{self.formato.lower()}"

    @property
    def chave(self) -> str:
        """Chave do raw: o nome do arquivo (com extensão, porque o CSV e o XLSX do mesmo dataset costumam ter o mesmo
        nome), seguro para pasta."""
        return safe_key(self.arquivo) or safe_key(self.id)


@dataclass
class FetchResultDivida:
    dataset: str
    key: str
    status: str  # ok | pulado | erro
    n_bytes: int
    changed: bool
    snapshot_id: str | None


def recursos_do_dataset(package: dict, dataset: str, formatos: list[str] | tuple[str, ...] = FORMATOS_PADRAO) -> list[RecursoDivida]:
    """Recursos do dataset nos formatos pedidos (CSV, XLSX...), na ordem do CKAN."""
    aceitos = {f.upper() for f in formatos}
    result = package.get("result", package)
    out: list[RecursoDivida] = []
    for r in result.get("resources", []) or []:
        fmt = str(r.get("format", "")).upper().strip()
        if fmt not in aceitos:
            continue
        size = r.get("size")
        try:
            size = int(size) if size not in (None, "") else None
        except (TypeError, ValueError):
            size = None
        out.append(RecursoDivida(dataset, str(r.get("id")), str(r.get("name")), str(r.get("url")), fmt, r.get("created"), r.get("last_modified"), size))
    return out


def parece_html(content: bytes) -> bool:
    cabeca = content[:300].lstrip().lower()
    return cabeca.startswith(b"<!doctype html") or cabeca.startswith(b"<html")


class DividaFetcher:
    def __init__(self, http: Http, store: Store, src_cfg: dict):
        self.http = http
        self.store = store
        self.base = str(src_cfg["base_url"]).rstrip("/")
        self.datasets: dict[str, dict] = src_cfg.get("datasets") or {}

    def package(self, dataset: str) -> dict:
        cfg = self.datasets[dataset]
        url = f"{self.base}/package_show"
        r = self.http.get(url, params={"id": str(cfg["dataset_id"])})
        payload = r.json()
        if not payload.get("success", True):
            raise RuntimeError(f"CKAN respondeu success=false para {dataset} ({cfg['dataset_id']}): {str(payload)[:300]}")
        self.store.put(
            source=SOURCE,
            dataset=dataset,
            key=KEY_PACKAGE,
            filename="package_show.json",
            content=r.content if hasattr(r, "content") else r.text.encode("utf-8"),
            canonical=canonical_package(payload),
            meta={"request": {"url": url, "id": str(cfg["dataset_id"])}, "http": {"status": r.status_code, "content_type": r.headers.get("content-type"), "url": str(r.url)[:2000]}},
        )
        return payload

    def recurso(self, rec: RecursoDivida) -> FetchResultDivida:
        r = self.http.get(rec.url)
        content: bytes = r.content if hasattr(r, "content") else r.text.encode("utf-8")
        if not content or parece_html(content):
            log.warning("%s/%s: a resposta não parece um arquivo de dados (%d bytes; começo: %r); não gravando", rec.dataset, rec.chave, len(content), content[:80])
            return FetchResultDivida(rec.dataset, rec.chave, "erro", len(content), False, None)
        res = self.store.put(
            source=SOURCE,
            dataset=rec.dataset,
            key=rec.chave,
            filename=rec.arquivo,
            content=content,
            meta={"recurso": asdict(rec), "http": {"status": r.status_code, "content_type": r.headers.get("content-type"), "url": str(r.url)[:2000]}},
        )
        log.info("divida %s/%s: %.2f MB, %s", rec.dataset, rec.chave, len(content) / 1e6, "NOVO/ALTERADO" if res.changed else "sem mudança")
        return FetchResultDivida(rec.dataset, rec.chave, "ok", len(content), res.changed, res.snapshot_id)


def run_fetch(http: Http, store: Store, src_cfg: dict, datasets: list[str] | None = None, force: bool = False) -> list[FetchResultDivida]:
    """Para cada dataset de sources.yaml (ou os pedidos): lista no CKAN e baixa os recursos nos formatos configurados."""
    f = DividaFetcher(http, store, src_cfg)
    nomes = list(datasets) if datasets else list(f.datasets)
    desconhecidos = [n for n in nomes if n not in f.datasets]
    if desconhecidos:
        raise KeyError(f"datasets não configurados em sources.yaml (S1.datasets): {desconhecidos}; conhecidos: {list(f.datasets)}")
    results: list[FetchResultDivida] = []
    for nome in nomes:
        cfg = f.datasets[nome]
        package = f.package(nome)
        recs = recursos_do_dataset(package, nome, cfg.get("formatos") or FORMATOS_PADRAO)
        titulo = (package.get("result") or {}).get("title")
        log.info("divida %s: %r, %d recurso(s) nos formatos %s", nome, titulo, len(recs), list(cfg.get("formatos") or FORMATOS_PADRAO))
        if not recs:
            log.warning("divida %s: nenhum recurso nos formatos pedidos; formatos no CKAN: %s", nome, sorted({str(r.get('format')) for r in (package.get('result') or {}).get('resources', [])}))
        index = store.read_index(SOURCE, nome)
        for rec in recs:
            prev = index.get(rec.chave, {})
            if not force and prev.get("status") == "ok" and prev.get("versao_recurso") == rec.versao and prev.get("snapshot_id"):
                log.info("divida %s/%s: mesma versão no CKAN (%s); pulando o download", nome, rec.chave, rec.versao)
                index[rec.chave] = {**prev, "last_checked": utcnow_iso(), "changed_last_run": False}
                results.append(FetchResultDivida(nome, rec.chave, "pulado", 0, False, prev.get("snapshot_id")))
                continue
            r = f.recurso(rec)
            index[rec.chave] = {
                "status": r.status if r.status != "erro" else prev.get("status", "erro"),
                "snapshot_id": r.snapshot_id or prev.get("snapshot_id"),
                "n_bytes": r.n_bytes or prev.get("n_bytes", 0),
                "versao_recurso": rec.versao if r.status == "ok" else prev.get("versao_recurso"),
                "recurso_id": rec.id,
                "nome": rec.nome,
                "formato": rec.formato,
                "url": rec.url,
                "last_checked": utcnow_iso(),
                "changed_last_run": r.changed,
            }
            results.append(r)
        store.write_index(SOURCE, nome, index)
    log.info("fetch divida: %d recursos, %d baixados, %d novos/alterados, %d pulados, %d com erro", len(results),
             sum(1 for r in results if r.status == "ok"), sum(1 for r in results if r.changed),
             sum(1 for r in results if r.status == "pulado"), sum(1 for r in results if r.status == "erro"))
    return results
