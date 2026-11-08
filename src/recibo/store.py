"""Camada raw imutável.

Layout: raw/{fonte}/{dataset}/{chave}/{snapshot_id}/{arquivo} + manifest.json
        raw/{fonte}/{dataset}/{chave}/_latest.json   (ponteiro para o snapshot vigente)
        raw/{fonte}/{dataset}/{chave}/_checks.jsonl  (toda verificação, mudou ou não)
        raw/{fonte}/{dataset}/_index.json            (estado por chave: ok, vazio...)

Idempotência: o conteúdo só gera snapshot novo quando o hash canônico muda. Hash igual = registro em
_checks.jsonl e nada mais. Hash diferente em chave já existente = revisão na fonte, registrada no manifest
(previous_snapshot_id) e no log.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

log = logging.getLogger(__name__)

_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


def safe_key(key: str) -> str:
    return _SAFE.sub("-", key).strip("-")


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def snapshot_id_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")


def sha256(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


class Backend(Protocol):
    def read(self, path: str) -> bytes | None: ...
    def write(self, path: str, data: bytes) -> None: ...
    def append_line(self, path: str, line: str) -> None: ...
    def list_dirs(self, prefix: str) -> list[str]: ...


class LocalBackend:
    def __init__(self, root: Path):
        self.root = Path(root)

    def _p(self, path: str) -> Path:
        return self.root / path

    def read(self, path: str) -> bytes | None:
        p = self._p(path)
        return p.read_bytes() if p.exists() else None

    def write(self, path: str, data: bytes) -> None:
        p = self._p(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)

    def append_line(self, path: str, line: str) -> None:
        p = self._p(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a", encoding="utf-8") as f:
            f.write(line.rstrip("\n") + "\n")

    def list_dirs(self, prefix: str) -> list[str]:
        p = self._p(prefix)
        if not p.exists():
            return []
        return sorted(d.name for d in p.iterdir() if d.is_dir())


class GcsBackend:
    """Mesma interface sobre um bucket (versionamento ligado no bucket garante imutabilidade)."""

    def __init__(self, bucket: str):
        from google.cloud import storage  # dependência opcional (extra "gcp")

        self.client = storage.Client()
        self.bucket = self.client.bucket(bucket)

    def read(self, path: str) -> bytes | None:
        blob = self.bucket.blob(path)
        return blob.download_as_bytes() if blob.exists() else None

    def write(self, path: str, data: bytes) -> None:
        self.bucket.blob(path).upload_from_string(data)

    def append_line(self, path: str, line: str) -> None:
        cur = self.read(path) or b""
        self.write(path, cur + (line.rstrip("\n") + "\n").encode("utf-8"))

    def list_dirs(self, prefix: str) -> list[str]:
        prefix = prefix.rstrip("/") + "/"
        it = self.client.list_blobs(self.bucket, prefix=prefix, delimiter="/")
        list(it)  # consome para popular prefixes
        return sorted(p[len(prefix) :].rstrip("/") for p in it.prefixes)


@dataclass
class PutResult:
    changed: bool
    snapshot_id: str
    path: str
    sha256_raw: str
    sha256_canonical: str
    previous_snapshot_id: str | None


class Store:
    def __init__(self, backend: Backend):
        self.b = backend

    @staticmethod
    def _base(source: str, dataset: str, key: str) -> str:
        return f"raw/{source}/{dataset}/{safe_key(key)}"

    def put(
        self,
        *,
        source: str,
        dataset: str,
        key: str,
        filename: str,
        content: bytes,
        canonical: bytes | None = None,
        meta: dict | None = None,
    ) -> PutResult:
        base = self._base(source, dataset, key)
        raw_hash = sha256(content)
        can_hash = sha256(canonical if canonical is not None else content)
        latest_b = self.b.read(f"{base}/_latest.json")
        latest = json.loads(latest_b) if latest_b else None
        now = utcnow_iso()

        if latest and latest.get("sha256_canonical") == can_hash:
            self.b.append_line(
                f"{base}/_checks.jsonl",
                json.dumps(
                    {"checked_at": now, "sha256_canonical": can_hash, "snapshot_id": latest["snapshot_id"], "changed": False},
                    ensure_ascii=False,
                ),
            )
            return PutResult(False, latest["snapshot_id"], latest["path"], raw_hash, can_hash, latest["snapshot_id"])

        snap = snapshot_id_now()
        path = f"{base}/{snap}/{filename}"
        manifest = {
            "source": source,
            "dataset": dataset,
            "key": key,
            "filename": filename,
            "snapshot_id": snap,
            "fetched_at": now,
            "sha256_raw": raw_hash,
            "sha256_canonical": can_hash,
            "bytes": len(content),
            "previous_snapshot_id": latest["snapshot_id"] if latest else None,
            **(meta or {}),
        }
        self.b.write(path, content)
        self.b.write(f"{base}/{snap}/manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8"))
        self.b.write(
            f"{base}/_latest.json",
            json.dumps(
                {"snapshot_id": snap, "path": path, "sha256_canonical": can_hash, "sha256_raw": raw_hash, "fetched_at": now},
                ensure_ascii=False,
                indent=2,
            ).encode("utf-8"),
        )
        self.b.append_line(
            f"{base}/_checks.jsonl",
            json.dumps({"checked_at": now, "sha256_canonical": can_hash, "snapshot_id": snap, "changed": True}, ensure_ascii=False),
        )
        if latest:
            log.warning("REVISAO NA FONTE: %s/%s/%s mudou (%s -> %s)", source, dataset, key, latest["snapshot_id"], snap)
        return PutResult(True, snap, path, raw_hash, can_hash, latest["snapshot_id"] if latest else None)

    def get_latest(self, source: str, dataset: str, key: str) -> tuple[bytes, dict] | None:
        base = self._base(source, dataset, key)
        latest_b = self.b.read(f"{base}/_latest.json")
        if not latest_b:
            return None
        latest = json.loads(latest_b)
        content = self.b.read(latest["path"])
        man_b = self.b.read(f"{base}/{latest['snapshot_id']}/manifest.json")
        if content is None or man_b is None:
            return None
        return content, json.loads(man_b)

    def list_keys(self, source: str, dataset: str) -> list[str]:
        """Chaves originais (lidas dos manifests), em ordem."""
        out: list[str] = []
        for d in self.b.list_dirs(f"raw/{source}/{dataset}"):
            lb = self.b.read(f"raw/{source}/{dataset}/{d}/_latest.json")
            if not lb:
                continue
            latest = json.loads(lb)
            mb = self.b.read(f"raw/{source}/{dataset}/{d}/{latest['snapshot_id']}/manifest.json")
            if mb:
                out.append(json.loads(mb)["key"])
        return sorted(out)

    def write_index(self, source: str, dataset: str, index: dict) -> None:
        self.b.write(f"raw/{source}/{dataset}/_index.json", json.dumps(index, ensure_ascii=False, indent=2).encode("utf-8"))

    def read_index(self, source: str, dataset: str) -> dict:
        b = self.b.read(f"raw/{source}/{dataset}/_index.json")
        return json.loads(b) if b else {}


def make_store(settings) -> Store:
    if settings.store == "gcs":
        if not settings.gcs_bucket:
            raise SystemExit("RECIBO_STORE=gcs exige RECIBO_GCS_BUCKET")
        return Store(GcsBackend(settings.gcs_bucket))
    return Store(LocalBackend(settings.data_dir))
