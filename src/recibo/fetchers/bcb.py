"""Fetcher S16: séries do SGS do Banco Central (dívida líquida e bruta, juros nominais e resultados por esfera, dívida
mobiliária, composição por indexador, prazo médio), via API pública de dados abertos.

Endpoints:
  GET {base}.{codigo}/dados?formato=json&dataInicial=dd/mm/aaaa&dataFinal=dd/mm/aaaa
      -> [{"data": "01/01/2006", "valor": "12345.67"}, ...]  (série inteira desde `desde`, em sources.yaml)
  GET {metadados_url}/package_search?q={codigo}&rows=5
      -> ficha da série no portal de dados abertos do BC (nome e descrição com as palavras do BC); opcional: se falhar,
         a série é gravada mesmo assim e a ficha fica com o nome de sources.yaml

Raw:
  raw/bcb/sgs/{codigo}/{snapshot}/sgs_{codigo}.json          hash canônico = pontos (data, valor) ordenados por data,
                                                             para a data final da consulta não criar snapshot novo
  raw/bcb/sgs_meta/{codigo}/{snapshot}/package.json          hash canônico = id, name, title, notes do dataset
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import date

from ..http import Http
from ..store import Store, utcnow_iso

log = logging.getLogger(__name__)

SOURCE = "bcb"
DATASET = "sgs"
DATASET_META = "sgs_meta"


@dataclass
class FetchResultBcb:
    codigo: int
    status: str  # ok | erro | vazio
    n_pontos: int
    changed: bool
    snapshot_id: str | None
    primeira: str | None = None
    ultima: str | None = None


def canonical_serie(pontos: list[dict]) -> bytes:
    ordenados = sorted(({"data": str(p.get("data")), "valor": str(p.get("valor"))} for p in pontos), key=lambda d: _iso(d["data"]))
    return json.dumps(ordenados, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _iso(d: str) -> str:
    """'dd/mm/aaaa' -> 'aaaa-mm-dd' (para ordenar); outra coisa fica como está."""
    partes = str(d).split("/")
    if len(partes) == 3 and all(x.isdigit() for x in partes):
        return f"{partes[2]}-{partes[1].zfill(2)}-{partes[0].zfill(2)}"
    return str(d)


def canonical_meta(pkg: dict) -> bytes:
    return json.dumps({k: pkg.get(k) for k in ("id", "name", "title", "notes")}, sort_keys=True, ensure_ascii=False).encode("utf-8")


def escolher_package(payload: dict, codigo: int) -> dict | None:
    """No resultado do package_search, o dataset cujo nome começa com '{codigo}-' (é como o portal do BC nomeia)."""
    result = payload.get("result") or {}
    for p in result.get("results", []) or []:
        if str(p.get("name", "")).startswith(f"{codigo}-"):
            return p
    return None


class BcbFetcher:
    def __init__(self, http: Http, store: Store, src_cfg: dict):
        self.http = http
        self.store = store
        self.base = str(src_cfg["base_url"]).rstrip("/")
        self.meta_base = str(src_cfg.get("metadados_url") or "").rstrip("/")
        self.desde = str(src_cfg.get("desde") or "01/01/2006")

    def serie(self, codigo: int, nome_cfg: str, hoje: date | None = None) -> FetchResultBcb:
        hoje = hoje or date.today()
        url = f"{self.base}.{codigo}/dados"
        params = {"formato": "json", "dataInicial": self.desde, "dataFinal": hoje.strftime("%d/%m/%Y")}
        r = self.http.get(url, params=params)
        try:
            pontos = r.json()
        except ValueError:
            log.warning("SGS %s: resposta não é JSON (%r); não gravando", codigo, (r.text or "")[:120])
            return FetchResultBcb(codigo, "erro", 0, False, None)
        if not isinstance(pontos, list):
            log.warning("SGS %s: resposta inesperada (%s); não gravando", codigo, str(pontos)[:200])
            return FetchResultBcb(codigo, "erro", 0, False, None)
        if not pontos:
            log.warning("SGS %s: série vazia desde %s", codigo, self.desde)
            return FetchResultBcb(codigo, "vazio", 0, False, None)
        content = r.content if hasattr(r, "content") else r.text.encode("utf-8")
        res = self.store.put(
            source=SOURCE,
            dataset=DATASET,
            key=str(codigo),
            filename=f"sgs_{codigo}.json",
            content=content,
            canonical=canonical_serie(pontos),
            meta={"request": {"url": url, "params": params}, "serie": {"codigo": codigo, "nome_config": nome_cfg, "n_pontos": len(pontos)},
                  "http": {"status": r.status_code, "content_type": r.headers.get("content-type"), "url": str(r.url)[:2000]}},
        )
        datas = sorted(_iso(str(p.get("data"))) for p in pontos)
        log.info("SGS %s (%s): %d pontos, %s a %s, %s", codigo, nome_cfg, len(pontos), datas[0], datas[-1], "NOVO/ALTERADO" if res.changed else "sem mudança")
        return FetchResultBcb(codigo, "ok", len(pontos), res.changed, res.snapshot_id, datas[0], datas[-1])

    def metadados(self, codigo: int) -> bool:
        if not self.meta_base:
            return False
        url = f"{self.meta_base}/package_search"
        try:
            r = self.http.get(url, params={"q": str(codigo), "rows": 5})
            payload = r.json()
        except Exception as e:  # noqa: BLE001 - a ficha é opcional; a série é o que importa
            log.warning("SGS %s: ficha no portal de dados abertos indisponível (%s)", codigo, e)
            return False
        pkg = escolher_package(payload, codigo)
        if pkg is None:
            log.warning("SGS %s: nenhum dataset '%s-...' no portal de dados abertos", codigo, codigo)
            return False
        self.store.put(
            source=SOURCE,
            dataset=DATASET_META,
            key=str(codigo),
            filename="package.json",
            content=json.dumps(pkg, ensure_ascii=False, indent=2).encode("utf-8"),
            canonical=canonical_meta(pkg),
            meta={"request": {"url": url, "q": str(codigo)}, "titulo": pkg.get("title")},
        )
        return True


def run_fetch(http: Http, store: Store, src_cfg: dict, codigos: list[int] | None = None, force: bool = False, hoje: date | None = None) -> list[FetchResultBcb]:
    """Baixa as séries de sources.yaml (ou só os códigos pedidos). `force` não muda nada aqui (a série inteira é sempre
    pedida; o store só cria snapshot se os pontos mudaram); fica pela simetria com as outras fontes."""
    f = BcbFetcher(http, store, src_cfg)
    series = {int(s["codigo"]): s for s in (src_cfg.get("series") or [])}
    pedidos = list(codigos) if codigos else sorted(series)
    index = store.read_index(SOURCE, DATASET)
    results: list[FetchResultBcb] = []
    for codigo in pedidos:
        cfg = series.get(codigo, {"nome": f"série {codigo} (fora de sources.yaml)"})
        try:
            r = f.serie(codigo, str(cfg.get("nome")), hoje=hoje)
        except Exception as e:  # noqa: BLE001 - uma série com erro não derruba as outras
            log.error("SGS %s: falha ao baixar (%s)", codigo, e)
            r = FetchResultBcb(codigo, "erro", 0, False, None)
        prev = index.get(str(codigo), {})
        index[str(codigo)] = {
            "status": r.status if r.status != "erro" else prev.get("status", "erro"),
            "snapshot_id": r.snapshot_id or prev.get("snapshot_id"),
            "n_pontos": r.n_pontos or prev.get("n_pontos", 0),
            "primeira": r.primeira or prev.get("primeira"),
            "ultima": r.ultima or prev.get("ultima"),
            "nome": cfg.get("nome"),
            "tipo": cfg.get("tipo"),
            "unidade": cfg.get("unidade"),
            "last_checked": utcnow_iso(),
            "changed_last_run": r.changed,
            "ficha_dados_abertos": f.metadados(codigo) if r.status == "ok" else prev.get("ficha_dados_abertos", False),
        }
        results.append(r)
    store.write_index(SOURCE, DATASET, index)
    log.info("fetch bcb: %d séries, %d ok, %d novas/alteradas, %d vazias, %d com erro", len(results), sum(1 for r in results if r.status == "ok"),
             sum(1 for r in results if r.changed), sum(1 for r in results if r.status == "vazio"), sum(1 for r in results if r.status == "erro"))
    return results
