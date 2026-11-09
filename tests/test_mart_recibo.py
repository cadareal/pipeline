import json

import pytest

from recibo.config import load_yaml
from recibo.marts.recibo import compute, largest_remainder, to_export, write_outputs
from recibo.transform.siconfi import build_staging

from conftest import (
    AMORTIZACAO_ANEXO01,
    ESPERADO,
    FORA_843_844,
    REFINANCIAMENTO_ANEXO01,
    SERVICO_BRUTO,
    TOTAL_I,
    TOTAL_RECIBO,
)


@pytest.fixture
def staging(populated_store, config_dir, tmp_path):
    return build_staging(populated_store, config_dir, tmp_path / "staging")


def test_recibo_values_match_expected(staging, config_dir):
    linhas = load_yaml(config_dir, "linhas.yaml")
    res = compute(staging, linhas, 2024, 6)

    got = {x.id: round(x.valor, 2) for x in res.linhas}
    assert got == ESPERADO
    assert res.total_recibo == pytest.approx(TOTAL_RECIBO)
    assert res.total_base_anexo02 == pytest.approx(TOTAL_I)

    excl = {x.id: x.valor for x in res.excluidos}
    assert excl["refinanciamento"] == 0.0  # a União já deixa o refinanciamento fora do total do Anexo 02
    assert excl["servico_divida_bruto"] == SERVICO_BRUTO

    assert res.anexo01["refinanciamento"] == REFINANCIAMENTO_ANEXO01
    assert res.anexo01["amortizacao"] == AMORTIZACAO_ANEXO01
    assert res.anexo01["juros"] == 350.0

    assert sum(x.share for x in res.linhas) == pytest.approx(1.0)
    assert sum(x.centavos for x in res.linhas) == 100
    inss = next(x for x in res.linhas if x.id == "previdencia_inss")
    assert inss.centavos == 37  # 900/2450 = 36,7%

    # mesmo período do exercício anterior (2023/6 sintético: 90% de 2024, com 100 movidos de 08 para 10)
    assert res.comparacao == {"exercicio": 2023, "periodo": 6, "disponivel": True}
    assert inss.anterior == pytest.approx(810.0) and inss.variacao == pytest.approx(1 / 0.9 - 1)
    saude = next(x for x in res.linhas if x.id == "saude")
    assert saude.anterior == pytest.approx(270.0) and saude.variacao == pytest.approx(200 / 270 - 1)
    maquina = next(x for x in res.linhas if x.cfg.get("residual"))
    assert maquina.anterior == pytest.approx(63.0)  # o ajuste também é recalculado no ano anterior (9 = 315 + 144 - 450)


def test_reconciliations_pass_on_consistent_data(staging, config_dir):
    linhas = load_yaml(config_dir, "linhas.yaml")
    res = compute(staging, linhas, 2024, 6)
    by = {r.id: r for r in res.reconciliacoes}
    for rid in ("hierarquia", "total_cabecalho", "total_anexos", "fu_prefixo_consistente", "residual_nao_negativo", "centavos_somam_100", "identidade_total"):
        assert by[rid].ok, rid
        assert by[rid].critica, rid
    # o cabeçalho (I) é só a primeira linha: a linha-resumo (II) do bloco I não entra
    assert by["total_cabecalho"].esperado == TOTAL_I
    assert by["servico_divida_fora_843_844"].ok
    assert by["servico_divida_fora_843_844"].obtido == pytest.approx(FORA_843_844)
    assert not by["servico_divida_fora_843_844"].critica
    assert by["inss_vs_beneficios_anexo01"].ok  # 900 vs 920: 2,2% < 5%
    assert not by["inss_vs_beneficios_anexo01"].critica
    assert res.publicavel


def test_variacao_anual_flags_lines_above_limit_without_blocking(staging, config_dir):
    linhas = load_yaml(config_dir, "linhas.yaml")
    res = compute(staging, linhas, 2024, 6)
    by = {r.id: r for r in res.reconciliacoes}
    va = by["variacao_anual"]
    assert not va.critica
    assert not va.ok and va.obtido == 2  # saúde (-25,9%) e assistência (+122%); as demais variam 11,1%
    assert "saude: -25.9%" in va.detalhe and "assistencia: +122.2%" in va.detalhe
    assert "previdencia_inss" not in va.detalhe
    assert res.publicavel  # informativa não trava

    # sem exercício anterior no staging: comparação não feita, reconciliação passa e diz por quê
    res23 = compute(staging, linhas, 2023, 6)
    va23 = {r.id: r for r in res23.reconciliacoes}["variacao_anual"]
    assert va23.ok and "sem dados de 2022/p6" in va23.detalhe
    assert res23.comparacao["disponivel"] is False
    assert all(x.anterior is None and x.variacao is None for x in res23.linhas)


def test_adjustment_can_be_switched_off_and_identity_then_fails(staging, config_dir):
    linhas = load_yaml(config_dir, "linhas.yaml")
    linhas["ajustes"]["servico_divida_fora_843_844"] = False
    res = compute(staging, linhas, 2024, 6)
    by = {r.id: r for r in res.reconciliacoes}
    assert res.total_recibo == pytest.approx(TOTAL_RECIBO + FORA_843_844)
    assert not by["identidade_total"].ok
    assert not res.publicavel


def test_export_contains_ficha_and_writes_files(staging, config_dir, tmp_path):
    linhas = load_yaml(config_dir, "linhas.yaml")
    fontes = load_yaml(config_dir, "sources.yaml")
    res = compute(staging, linhas, 2024, 6)
    export = to_export(res, linhas, fontes, {"selo": "fechado", "nota": "teste"})

    saude = next(x for x in export["linhas"] if x["id"] == "saude")
    assert saude["ficha"]["fonte"]["id"] == "S4"
    assert "funcao_cod = '10'" in saude["ficha"]["sql"]
    assert saude["ficha"]["snapshots"]

    juros = next(x for x in export["linhas"] if x["id"] == "juros")
    assert "cod_conta = 'JurosEEncargosDaDivida'" in juros["ficha"]["sql"]
    assert "escopo" not in juros["ficha"]["sql"]

    assert export["fora_do_real"]["refinanciamento"]["valor"] == REFINANCIAMENTO_ANEXO01
    am = export["fora_do_real"]["amortizacao"]
    assert am["valor"] == AMORTIZACAO_ANEXO01
    assert am["em_843_844"] == pytest.approx(SERVICO_BRUTO - 350.0)
    assert am["fora_843_844_estimado"] == pytest.approx(FORA_843_844)

    assert export["comparacao"] == {"exercicio": 2023, "periodo": 6, "disponivel": True}
    assert saude["anterior"] == pytest.approx(270.0) and saude["variacao_anual"] == pytest.approx(200 / 270 - 1)
    va = next(r for r in export["reconciliacoes"] if r["id"] == "variacao_anual")
    assert "saude" in va["detalhe"]

    p_json, p_csv, p_exp = write_outputs(export, tmp_path / "marts", tmp_path / "export")
    assert json.loads(p_json.read_text())["publicavel"] is True
    assert p_csv.read_text().splitlines()[0] == "id,nome,valor,share,centavos,anterior,variacao_anual"
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
