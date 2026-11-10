"""O repositório pronto para receber gente: licença no lugar, modelos de issue válidos (os formulários que a página de
contato do site aponta) e o README começando pelo site."""

from __future__ import annotations

import yaml

from recibo.config import REPO_ROOT


def test_licenca_e_readme():
    lic = (REPO_ROOT / "LICENSE").read_text(encoding="utf-8")
    assert lic.startswith("MIT License") and "Caue Bertolino" in lic
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    assert readme.startswith("# Cada Real") and "https://cadareal.org/" in readme[:600] and "## Por quê" in readme[:1200]
    assert "issues/new/choose" in readme and "contato@cadareal.org" in readme


def test_modelos_de_issue():
    pasta = REPO_ROOT / ".github" / "ISSUE_TEMPLATE"
    nomes = sorted(p.name for p in pasta.glob("*.yml"))
    assert nomes == ["bug.yml", "config.yml", "metodologia.yml", "numero.yml"]
    cfg = yaml.safe_load((pasta / "config.yml").read_text(encoding="utf-8"))
    assert cfg["blank_issues_enabled"] is True and {c["url"] for c in cfg["contact_links"]} >= {"https://cadareal.org/perguntas-abertas/", "mailto:contato@cadareal.org"}
    for nome in ("bug.yml", "metodologia.yml", "numero.yml"):
        d = yaml.safe_load((pasta / nome).read_text(encoding="utf-8"))
        assert d["name"] and d["description"] and d["body"], nome
        tipos = [b["type"] for b in d["body"]]
        assert set(tipos) <= {"markdown", "input", "textarea", "dropdown", "checkboxes"}
        obrigatorios = [b for b in d["body"] if (b.get("validations") or {}).get("required")]
        assert obrigatorios, f"{nome}: nenhum campo obrigatório"
        ids = [b["id"] for b in d["body"] if b.get("id")]
        assert len(ids) == len(set(ids)), f"{nome}: ids repetidos"
        for b in d["body"]:
            if b["type"] != "markdown":
                assert b["attributes"].get("label"), f"{nome}: campo sem label"
    # os dois botões do site e o README apontam para a escolha de modelo
    site = yaml.safe_load((REPO_ROOT / "conteudo" / "site.yaml").read_text(encoding="utf-8"))
    assert site["url_issue_nova"].endswith("/issues/new/choose") and site["email_contato"] == "contato@cadareal.org"
