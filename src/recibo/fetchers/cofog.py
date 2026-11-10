"""Fetcher S7: COFOG, despesa do Governo Central por função (GFSM 2014), via CKAN do Tesouro Transparente.

Endpoints:
  GET {base}/package_show?id={dataset_id}   -> metadados do dataset e a lista de recursos (um CSV por ano, ~20 MB cada)
  GET {url do recurso}                       -> o CSV do ano, gravado byte a byte no raw

Raw:
  raw/cofog/gc/package/{snapshot}/package_show.json   a lista de arquivos que o Tesouro oferecia na data (lacrada; o hash
                                                      canônico ignora contadores de acesso e olha só id, nome, url, datas e
                                                      tamanho de cada recurso)
  raw/cofog/gc/{ano}/{snapshot}/dados-abertos-cofog-gc-{ano}.csv   hash canônico = hash do arquivo

O Tesouro republica os arquivos antigos de vez em quando (em 9/nov/2026, os de 2010 a 2022 tinham last_modified de março
de 2025): hash diferente vira snapshot novo e aviso de REVISÃO NA FONTE, como nas outras fontes. Para não baixar 300 MB a
cada rodada, um ano só é baixado de novo quando a data ou o tamanho do recurso mudaram no CKAN (ou com --force).
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import asdict, dataclass

from ..http import Http
from ..store import Store, utcnow_iso

log = logging.getLogger(__name__)

SOURCE = "cofog"
DATASET = "gc"
KEY_PACKAGE = "package"
RECURSO_REGEX_PADRAO = r"^Base COFOG (\d{4})$"


@dataclass
class Recurso:
    ano: int
    id: str
    nome: str
    url: str
    formato: str
    created: str | None
    last_modified: str | None
    size: int | None

    @property
    def versao(self) -> str:
        """O que identifica a versão do arquivo no CKAN sem baixá-lo: data de modificação (ou de criação) e tamanho."""
        return f"{self.last_modified or self.created or ''}|{self.size or ''}"


@dataclass
class FetchResult:
    key: str
    status: str  # ok | ausente | pulado | erro
    n_bytes: int
    changed: bool
    snapshot_id: str | None


def recursos_por_ano(package: dict, regex: str = RECURSO_REGEX_PADRAO) -> dict[int, Recurso]:
    """Recursos do dataset cujo nome casa com o regex (o grupo 1 é o ano). Só formatos tabulares (CSV, XLSX)."""
    rx = re.compile(regex)
    result = package.get("result", package)
    out: dict[int, Recurso] = {}
    for r in result.get("resources", []) or []:
        m = rx.match(str(r.get("name", "")).strip())
        if not m:
            continue
        fmt = str(r.get("format", "")).upper()
        if fmt not in ("CSV", "XLSX", "XLS"):
            continue
        ano = int(m.group(1))
        size = r.get("size")
        try:
            size = int(size) if size not in (None, "") else None
        except (TypeError, ValueError):
            size = None
        rec = Recurso(ano, str(r.get("id")), str(r.get("name")), str(r.get("url")), fmt, r.get("created"), r.get("last_modified"), size)
        if ano in out:
            # dois recursos para o mesmo ano: fica o mais recente (created maior)
            if (rec.created or "") <= (out[ano].created or ""):
                continue
        out[ano] = rec
    return out


def canonical_package(package: dict) -> bytes:
    """Só o que importa da lista de recursos: id, nome, url, datas e tamanho (contadores de acesso mudam a toda hora)."""
    result = package.get("result", package)
    recs = sorted(
        (
            {k: r.get(k) for k in ("id", "name", "url", "format", "created", "last_modified", "size")}
            for r in result.get("resources", []) or []
        ),
        key=lambda d: str(d.get("id")),
    )
    return json.dumps({"id": result.get("id"), "name": result.get("name"), "resources": recs}, sort_keys=True, ensure_ascii=False).encode("utf-8")


class CofogFetcher:
    def __init__(self, http: Http, store: Store, src_cfg: dict):
        self.http = http
        self.store = store
        self.base = str(src_cfg["base_url"]).rstrip("/")
        self.dataset_id = str(src_cfg["dataset_id"])
        self.regex = str(src_cfg.get("recurso_regex") or RECURSO_REGEX_PADRAO)

    def package(self) -> dict:
        url = f"{self.base}/package_show"
        r = self.http.get(url, params={"id": self.dataset_id})
        payload = r.json()
        if not payload.get("success", True):
            raise RuntimeError(f"CKAN respondeu success=false para {self.dataset_id}: {str(payload)[:300]}")
        self.store.put(
            source=SOURCE,
            dataset=DATASET,
            key=KEY_PACKAGE,
            filename="package_show.json",
            content=r.content if hasattr(r, "content") else r.text.encode("utf-8"),
            canonical=canonical_package(payload),
            meta={"request": {"url": url, "id": self.dataset_id}, "http": {"status": r.status_code, "content_type": r.headers.get("content-type"), "url": str(r.url)[:2000]}},
        )
        return payload

    def ano(self, rec: Recurso) -> FetchResult:
        key = str(rec.ano)
        r = self.http.get(rec.url)
        content: bytes = r.content if hasattr(r, "content") else r.text.encode("utf-8")
        primeira = content.split(b"\n", 1)[0] if content else b""
        if b"COFOG" not in primeira.upper():  # o cabeçalho tem "Classificação COFOG"; outra coisa é página de erro ou HTML
            log.warning("%s: a resposta não parece o CSV do COFOG (%d bytes; primeira linha: %r); não gravando", key, len(content), primeira[:120])
            return FetchResult(key, "erro", len(content), False, None)
        filename = rec.url.rsplit("/", 1)[-1] or f"cofog-{rec.ano}.{rec.formato.lower()}"
        res = self.store.put(
            source=SOURCE,
            dataset=DATASET,
            key=key,
            filename=filename,
            content=content,
            meta={
                "recurso": asdict(rec),
                "http": {"status": r.status_code, "content_type": r.headers.get("content-type"), "url": str(r.url)[:2000]},
            },
        )
        log.info("COFOG %s: %.1f MB, %s", key, len(content) / 1e6, "NOVO/ALTERADO" if res.changed else "sem mudança")
        return FetchResult(key, "ok", len(content), res.changed, res.snapshot_id)


def run_fetch(http: Http, store: Store, src_cfg: dict, exercicios: list[int] | None = None, force: bool = False) -> list[FetchResult]:
    """Lista o dataset no CKAN e baixa os CSVs dos anos pedidos (padrão: todos os que existem).

    Um ano é pulado quando o índice já tem a mesma versão do recurso (data e tamanho no CKAN) e o snapshot existe; `force`
    baixa de novo mesmo assim (o store só cria snapshot novo se o conteúdo mudou).
    """
    f = CofogFetcher(http, store, src_cfg)
    package = f.package()
    recs = recursos_por_ano(package, f.regex)
    log.info("COFOG: %d anos no CKAN (%s a %s)", len(recs), min(recs) if recs else "-", max(recs) if recs else "-")
    anos = list(exercicios) if exercicios else sorted(recs)
    index = store.read_index(SOURCE, DATASET)
    results: list[FetchResult] = []
    for ano in anos:
        key = str(ano)
        prev = index.get(key, {})
        rec = recs.get(ano)
        if rec is None:
            log.warning("COFOG %s: o CKAN não oferece esse ano (publica com um ano de defasagem)", key)
            index[key] = {**prev, "status": "ausente" if prev.get("status") != "ok" else "ok", "last_checked": utcnow_iso(), "changed_last_run": False,
                          "nota": "não está na lista do CKAN nesta rodada" + ("; mantendo snapshot anterior" if prev.get("status") == "ok" else "")}
            results.append(FetchResult(key, "ausente", 0, False, prev.get("snapshot_id")))
            continue
        if not force and prev.get("status") == "ok" and prev.get("versao_recurso") == rec.versao and prev.get("snapshot_id"):
            log.info("COFOG %s: mesma versão no CKAN (%s); pulando o download", key, rec.versao)
            index[key] = {**prev, "last_checked": utcnow_iso(), "changed_last_run": False}
            results.append(FetchResult(key, "pulado", 0, False, prev.get("snapshot_id")))
            continue
        r = f.ano(rec)
        index[key] = {
            "status": r.status if r.status != "erro" else prev.get("status", "erro"),
            "snapshot_id": r.snapshot_id or prev.get("snapshot_id"),
            "n_bytes": r.n_bytes or prev.get("n_bytes", 0),
            "versao_recurso": rec.versao if r.status == "ok" else prev.get("versao_recurso"),
            "recurso_id": rec.id,
            "url": rec.url,
            "last_checked": utcnow_iso(),
            "changed_last_run": r.changed,
        }
        results.append(r)
    store.write_index(SOURCE, DATASET, index)
    ok = sum(1 for r in results if r.status == "ok")
    novos = sum(1 for r in results if r.changed)
    log.info("fetch cofog: %d anos pedidos, %d baixados, %d novos/alterados, %d pulados, %d ausentes", len(results), ok, novos,
             sum(1 for r in results if r.status == "pulado"), sum(1 for r in results if r.status == "ausente"))
    return results
