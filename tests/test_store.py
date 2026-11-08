import json

from recibo.store import LocalBackend, Store


def test_put_is_idempotent_by_canonical_hash(tmp_path):
    store = Store(LocalBackend(tmp_path))
    r1 = store.put(source="s", dataset="d", key="2024/06/X", filename="f.json", content=b'{"a":1}', canonical=b"A")
    r2 = store.put(source="s", dataset="d", key="2024/06/X", filename="f.json", content=b'{"a":1,"b":2}', canonical=b"A")
    assert r1.changed is True
    assert r2.changed is False
    assert r2.snapshot_id == r1.snapshot_id

    checks = (tmp_path / "raw/s/d/2024-06-X/_checks.jsonl").read_text().strip().splitlines()
    assert len(checks) == 2
    assert json.loads(checks[1])["changed"] is False


def test_changed_content_creates_new_snapshot_and_links_previous(tmp_path):
    store = Store(LocalBackend(tmp_path))
    r1 = store.put(source="s", dataset="d", key="k", filename="f.json", content=b"v1", canonical=b"A")
    r2 = store.put(source="s", dataset="d", key="k", filename="f.json", content=b"v2", canonical=b"B")
    assert r2.changed is True
    assert r2.snapshot_id != r1.snapshot_id
    assert r2.previous_snapshot_id == r1.snapshot_id

    content, manifest = store.get_latest("s", "d", "k")
    assert content == b"v2"
    assert manifest["previous_snapshot_id"] == r1.snapshot_id
    assert manifest["sha256_canonical"] == r2.sha256_canonical
    # o snapshot antigo continua lá, intacto
    assert (tmp_path / r1.path).read_bytes() == b"v1"


def test_list_keys_returns_original_keys(tmp_path):
    store = Store(LocalBackend(tmp_path))
    store.put(source="s", dataset="d", key="2024/06/RREO-Anexo 02", filename="f", content=b"x")
    store.put(source="s", dataset="d", key="2024/05/RREO-Anexo 02", filename="f", content=b"y")
    assert store.list_keys("s", "d") == ["2024/05/RREO-Anexo 02", "2024/06/RREO-Anexo 02"]
