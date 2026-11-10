"""Fontes de recursos (carimbo da fonte no SIOP): com que dinheiro cada despesa foi paga. Só no ano fechado."""

from __future__ import annotations

import pytest

from recibo.config import load_yaml
from recibo.marts.fontes import destino_de
from recibo.marts.recibo import compute, to_export
from recibo.transform.siconfi import build_staging as build_staging_rreo
from recibo.transform.siop import build_staging


@pytest.fixture
def stagings(populated_store_siop, config_dir, tmp_path):
    return build_staging_rreo(populated_store_siop, config_dir, tmp_path / "staging"), build_staging(populated_store_siop, tmp_path / "staging")


def test_fontes_por_grupo_origem_e_destino(stagings, config_dir):
    stg, siop = stagings
    linhas = load_yaml(config_dir, "linhas.yaml")
    res = compute(stg, linhas, 2024, 6, siop_path=siop, config_dir=config_dir)
    f = res.fontes
    assert f["disponivel"] and f["total"] == 4110.0
    por = {g["id"]: g for g in f["grupos"]}
    assert [g["id"] for g in f["grupos"]] == ["titulos_novos", "titulos_refinanciamento", "receitas_para_divida", "receita"]
    # títulos de refinanciamento (fonte 1443) pagaram só rolagem, e batem com o Anexo 01
    assert por["titulos_refinanciamento"]["valor"] == 1500.0 and por["titulos_refinanciamento"]["por_destino"]["rolagem"] == 1500.0
    assert por["titulos_refinanciamento"]["fontes"] == [{"cod": "1443", "nome": "Refinanciamento da Dívida Pública Federal", "origem": "corrente", "valor": 1500.0,
                                                          "por_destino": {"rolagem": 1500.0, "principal": 0.0, "juros": 0.0, "primaria": 0.0, "inversoes": 0.0}}]
    # títulos novos (fonte 1444) pagaram juros (348 + 2) e a parte de amortização da cobertura do BC (8)
    tn = por["titulos_novos"]
    assert tn["valor"] == 358.0 and tn["por_destino"] == {"rolagem": 0.0, "principal": 8.0, "juros": 350.0, "primaria": 0.0, "inversoes": 0.0}
    assert tn["por_origem"] == {"corrente": 358.0, "anteriores": 0.0, "outros": 0.0} and tn["por_origem_destino"]["corrente"]["juros"] == 350.0
    # receita (fonte 100): despesa primária, principal pago com caixa e inversões
    rc = por["receita"]
    assert rc["valor"] == 2252.0 and rc["por_destino"] == {"rolagem": 0.0, "principal": 152.0, "juros": 0.0, "primaria": 2048.0, "inversoes": 52.0}
    assert por["receitas_para_divida"]["valor"] == 0.0
    # totais e conferência contra o RREO (Anexo 01 total exceto intra + refinanciamento)
    assert f["por_destino"] == {"rolagem": 1500.0, "principal": 160.0, "juros": 350.0, "primaria": 2048.0, "inversoes": 52.0}
    assert f["conferencia"] == {"rolagem_siop": 1500.0, "rolagem_rreo": 1500.0, "total_siop": 4110.0, "total_rreo": 4110.0}
    rec = next(r for r in res.reconciliacoes if r.id == "fontes_total_vs_anexo01")
    assert rec.ok and not rec.critica
    assert f["destinos_nomes"]["rolagem"] == "Principal de títulos velhos (rolagem)" and f["gnd_nomes"]["6"] == "Amortização da dívida"
    # no export
    export = to_export(res, linhas, load_yaml(config_dir, "sources.yaml"), {"selo": "fechado", "nota": ""})
    assert export["fontes_de_recursos"]["grupos"][1]["id"] == "titulos_refinanciamento"


def test_fontes_indisponiveis_sem_siop_ou_ano_aberto(stagings, config_dir):
    from recibo.marts.fontes import calcular_fontes

    stg, siop = stagings
    linhas = load_yaml(config_dir, "linhas.yaml")
    res = compute(stg, linhas, 2024, 6)
    assert res.fontes == {"disponivel": False, "motivo": "sem staging do SIOP"}
    assert "fontes_total_vs_anexo01" not in [r.id for r in res.reconciliacoes]
    # ano em andamento (usa_siop False) e agregação ausente: indisponível com o motivo, nunca erro
    aberto = calcular_fontes(lambda sql: 1.0, lambda sql: [], linhas, 2024, {}, usa_siop=False, tem_siop=True)
    assert not aberto["disponivel"] and "ano em andamento" in aberto["motivo"]
    sem_ag = calcular_fontes(lambda sql: 0.0, lambda sql: [], linhas, 2024, {}, usa_siop=True, tem_siop=True)
    assert not sem_ag["disponivel"] and "ausente" in sem_ag["motivo"]


def test_destino_por_gnd():
    assert destino_de("6", True) == "rolagem" and destino_de("6", False) == "principal"
    assert destino_de("2", False) == "juros" and destino_de("5", False) == "inversoes"
    assert destino_de("1", False) == "primaria" and destino_de("3", True) == "primaria" and destino_de(None, False) == "primaria"
