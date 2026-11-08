"""Configuração via variáveis de ambiente e leitura dos YAML de spec."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import yaml

_HERE = Path(__file__).resolve()
REPO_ROOT = _HERE.parents[2]  # src/recibo/config.py -> raiz do repositório


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    config_dir: Path
    store: str  # local | gcs
    gcs_bucket: str | None
    user_agent: str
    pause_seconds: float
    timeout_seconds: float
    ca_bundle: str | None

    @property
    def staging_dir(self) -> Path:
        return self.data_dir / "staging"

    @property
    def marts_dir(self) -> Path:
        return self.data_dir / "marts"

    @property
    def export_dir(self) -> Path:
        return self.data_dir / "export"


def settings() -> Settings:
    env = os.environ.get
    return Settings(
        data_dir=Path(env("RECIBO_DATA_DIR", str(REPO_ROOT / "data"))),
        config_dir=Path(env("RECIBO_CONFIG_DIR", str(REPO_ROOT / "config"))),
        store=env("RECIBO_STORE", "local"),
        gcs_bucket=env("RECIBO_GCS_BUCKET") or None,
        user_agent=env(
            "RECIBO_USER_AGENT",
            "recibo-publico/0.1 (pipeline aberto de dados fiscais; contato via issues no repositorio)",
        ),
        pause_seconds=float(env("RECIBO_PAUSE_SECONDS", "0.7")),
        timeout_seconds=float(env("RECIBO_TIMEOUT_SECONDS", "90")),
        ca_bundle=env("RECIBO_CA_BUNDLE") or None,
    )


def load_yaml(config_dir: Path, name: str) -> dict:
    with open(config_dir / name, encoding="utf-8") as f:
        return yaml.safe_load(f)


def source_config(config_dir: Path, source_id: str) -> dict:
    fontes = load_yaml(config_dir, "sources.yaml")["fontes"]
    for f in fontes:
        if f["id"] == source_id:
            return f
    raise KeyError(f"fonte {source_id} não está em sources.yaml")
