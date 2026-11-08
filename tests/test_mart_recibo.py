import json

import pytest

from recibo.config import load_yaml
from recibo.marts.recibo import compute, largest_remainder, to_export, write_outputs
from recibo.transform.siconfi import build_staging

from conftest import ESPERADO, REFINANCIAMENTO, SERVICO_BRUTO, TOTAL_RECIBO


@pytest.fixture
def staging(populated_store, config_dir, tmp_path):
    return build_staging(populated_store, config_dir, tmp_path / "staging")


def test_recibo_values_match_expected(staging, config_dir):
    linhas = load_yaml(config_dir, "linhas.yaml")
    res = compute(staging, linhas, 2024, 6)

    got = {x.id: round(x.valor, 2) for x in res.linhas}
    assert got == ESPERADO
    assert res.total_recibo == pytest.approx(TOTAL_RECIBO)

    excl = {x.id: x.valor for x in res.excluidos}
    assert excl["refinanciamento"] == REFINANCIAMENTO
    assert excl["servico_divida_bruto"] == SERVICO_BRUTO

    # shares somam 1 e centavos somam 100
    assert sum(x.share for x in res.linhas) == pytest.approx(1.0)
    assert sum(x.centavos for x in res.linhas) == 100
    inss = next(x for x in res.linhas if x.id == "previdencia_inss")
    assert inss.centavos == 37  # 900/2460 = 36,6%


def test_reconciliations_pass_on_consistent_data(staging, config_dir):
    linhas = load_yaml(config_dir, "linhas.yaml")
    res = compute(staging, linhas, 2024, 6)
    by = {r.id: r for r in res.reconciliacoes}
    assert by["hierarquia"].ok
    assert by["total_cabecalho"].ok
    assert by["residual_nao_negativo"].ok
    assert by["centavos_somam_100"].ok
    # amortização implícita (500 - 350) bate com o Anexo 01 (150)
    assert by["amortizacao_vs_anexo01"].ok
    assert res.publicavel


def test_export_contains_ficha_and_writes_files(staging, config_dir, tmp_path):
    linhas = load_yaml(config_dir, "linhas.yaml")
    fontes = load_yaml(config_dir, "sources.yaml")
    res = compute(staging, linhas, 2024, 6)
    export = to_export(res, linhas, fontes, {"selo": "fechado", "nota": "teste"})

    saude = next(x for x in export["linhas"] if x["id"] == "saude")
    assert saude["ficha"]["fonte"]["id"] == "S4"
    assert "funcao_cod = '10'" in saude["ficha"]["sql"]
    assert saude["ficha"]["snapshots"]
    assert export["fora_do_real"]["amortizacao_implicita"]["valor"] == pytest.approx(150.0)

    p_json, p_csv, p_exp = write_outputs(export, tmp_path / "marts", tmp_path / "export")
    assert json.loads(p_json.read_text())["publicavel"] is True
    assert p_csv.read_text().splitlines()[0] == "id,nome,valor,share,centavos"
    assert p_exp.exists()


def test_largest_remainder_sums_to_total():
    assert sum(largest_remainder([1, 1, 1], 100)) == 100
    assert largest_remainder([50, 50], 100) == [50, 50]
    assert largest_remainder([], 100) == []
    assert largest_remainder([0, 0], 100) == [0, 0]


def test_missing_period_yields_zero_not_crash(staging, config_dir):
    linhas = load_yaml(config_dir, "linhas.yaml")
    res = compute(staging, linhas, 2026, 3)
    assert res.total_recibo == 0
    assert not res.publicavel
