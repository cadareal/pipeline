"""Fetcher S4: SICONFI (RREO da União) via API ORDS do Tesouro.

Endpoints usados:
  GET {base}/entes                                   -> lista de entes (acha o cod_ibge da esfera U)
  GET {base}/anexos-relatorios?co_tipo_demonstrativo -> nomes de anexo válidos
  GET {base}/rreo?an_exercicio&nr_periodo&co_tipo_demonstrativo&no_anexo&co_esfera&id_ente[&offset]

Raw gravado como veio: um arquivo pages.jsonl com o texto exato de cada página da API, uma por linha.
Hash canônico (para detectar mudança) é calculado sobre os itens ordenados, independente de paginação.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass

from ..http import Http
from ..store import Store, utcnow_iso

log = logging.getLogger(__name__)

SOURCE = "siconfi"
DATASET_RREO = "rreo"
MAX_PAGES = 1000


def canonical_items(items: list[dict]) -> bytes:
    ordered = sorted(items, key=lambda d: json.dumps(d, sort_keys=True, ensure_ascii=False))
    return json.dumps(ordered, sort_keys=True, ensure_ascii=False).encode("utf-8")


def paginate(http: Http, url: str, params: dict) -> tuple[list[dict], list[str], dict]:
    """Segue hasMore/offset do ORDS. Retorna (itens, textos das páginas, metadados http da 1ª página)."""
    items: list[dict] = []
    pages: list[str] = []
    meta: dict = {}
    offset = 0
    while True:
        p = dict(params)
        if offset:
            p["offset"] = offset
        r = http.get(url, params=p)
        if not meta:
            meta = {
                "status": r.status_code,
                "content_type": r.headers.get("content-type"),
                "etag": r.headers.get("etag"),
                "last_modified": r.headers.get("last-modified"),
                "url": str(r.url),
            }
        pages.append(r.text)
        body = r.json()
        page_items = body.get("items", []) or []
        items.extend(page_items)
        if not body.get("hasMore"):
            break
        step = body.get("limit") or len(page_items)
        if not page_items or not step:
            break
        offset += int(step)
        if len(pages) >= MAX_PAGES:
            raise RuntimeError(f"paginação excessiva em {url}; abortando")
    return items, pages, meta


@dataclass
class FetchResult:
    key: str
    status: str  # ok | vazio | vazio_apos_ok
    n_items: int
    changed: bool
    snapshot_id: str | None


class SiconfiFetcher:
    def __init__(self, http: Http, store: Store, src_cfg: dict):
        self.http = http
        self.store = store
        self.base = str(src_cfg["base_url"]).rstrip("/")
        self.tipo = src_cfg.get("tipo_demonstrativo", "RREO")
        self.esfera = str(src_cfg.get("esfera", "U")).upper()
        self._id_ente: str | None = str(src_cfg["id_ente"]) if src_cfg.get("id_ente") else None

    def _put(self, dataset: str, key: str, items: list[dict], pages: list[str], meta: dict, request: dict):
        raw = ("\n".join(pages) + "\n").encode("utf-8")
        return self.store.put(
            source=SOURCE,
            dataset=dataset,
            key=key,
            filename="pages.jsonl",
            content=raw,
            canonical=canonical_items(items),
            meta={"request": request, "http": meta, "n_items": len(items), "n_pages": len(pages)},
        )

    def entes(self) -> list[dict]:
        url = f"{self.base}/entes"
        items, pages, meta = paginate(self.http, url, {})
        self._put("entes", "entes", items, pages, meta, {"url": url})
        return items

    def id_ente(self) -> str:
        if self._id_ente:
            return self._id_ente
        for e in self.entes():
            if str(e.get("esfera", "")).upper() == self.esfera:
                self._id_ente = str(e.get("cod_ibge"))
                log.info("ente da esfera %s: cod_ibge=%s (%s)", self.esfera, self._id_ente, e.get("ente"))
                return self._id_ente
        raise RuntimeError(f"não encontrei ente da esfera {self.esfera} em /entes")

    def anexos_disponiveis(self) -> list[str]:
        url = f"{self.base}/anexos-relatorios"
        items, pages, meta = paginate(self.http, url, {"co_tipo_demonstrativo": self.tipo})
        self._put("anexos_relatorios", self.tipo, items, pages, meta, {"url": url, "co_tipo_demonstrativo": self.tipo})
        nomes = {str(i.get("anexo") or i.get("no_anexo") or "") for i in items}
        return sorted(n for n in nomes if n)

    def rreo(self, exercicio: int, periodo: int, anexo: str) -> FetchResult:
        url = f"{self.base}/rreo"
        params = {
            "an_exercicio": exercicio,
            "nr_periodo": periodo,
            "co_tipo_demonstrativo": self.tipo,
            "no_anexo": anexo,
            "co_esfera": self.esfera,
            "id_ente": self.id_ente(),
        }
        key = f"{exercicio}/{periodo:02d}/{anexo}"
        items, pages, meta = paginate(self.http, url, params)
        if not items:
            log.info("%s: vazio (não publicado ainda, ou anexo não se aplica)", key)
            return FetchResult(key, "vazio", 0, False, None)
        res = self._put(DATASET_RREO, key, items, pages, meta, {"url": url, **params})
        log.info("%s: %d linhas, %s", key, len(items), "NOVO/ALTERADO" if res.changed else "sem mudança")
        return FetchResult(key, "ok", len(items), res.changed, res.snapshot_id)


def run_fetch(
    http: Http,
    store: Store,
    src_cfg: dict,
    exercicios: list[int],
    periodos: list[int] | None = None,
    anexos: list[str] | None = None,
) -> list[FetchResult]:
    periodos = periodos or list(src_cfg.get("periodos_padrao", [1, 2, 3, 4, 5, 6]))
    anexos = anexos or list(src_cfg["anexos_padrao"])
    f = SiconfiFetcher(http, store, src_cfg)

    disponiveis = f.anexos_disponiveis()
    log.info("anexos disponíveis para %s (%d): %s", f.tipo, len(disponiveis), disponiveis)
    for a in anexos:
        if disponiveis and a not in disponiveis:
            log.warning("anexo configurado '%s' não está na lista da API; tentando mesmo assim", a)

    index = store.read_index(SOURCE, DATASET_RREO)
    results: list[FetchResult] = []
    for ex in exercicios:
        for per in periodos:
            for an in anexos:
                r = f.rreo(ex, per, an)
                prev = index.get(r.key, {})
                status = r.status
                if status == "vazio" and prev.get("status") == "ok":
                    status = "vazio_apos_ok"
                    log.warning("%s: estava publicado e agora voltou vazio; mantendo snapshot anterior", r.key)
                index[r.key] = {
                    "status": status,
                    "n_items": r.n_items or prev.get("n_items", 0),
                    "snapshot_id": r.snapshot_id or prev.get("snapshot_id"),
                    "last_checked": utcnow_iso(),
                    "changed_last_run": r.changed,
                }
                results.append(FetchResult(r.key, status, r.n_items, r.changed, r.snapshot_id))
    store.write_index(SOURCE, DATASET_RREO, index)

    ok = sum(1 for r in results if r.status == "ok")
    novos = sum(1 for r in results if r.changed)
    log.info("fetch siconfi: %d consultas, %d com dados, %d novos/alterados", len(results), ok, novos)
    return results
