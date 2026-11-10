import json

import pytest

from recibo.config import load_yaml
from recibo.diff import Periodo, diff_periodos, render, write_json
from recibo.transform.siconfi import build_staging

from conftest import ESCALA_2023, ESPERADO


@pytest.fixture
def staging(populated_store, config_dir, tmp_path):
    return build_staging(populated_store, config_dir, tmp_path / "staging")


def test_diff_lists_lines_functions_and_subfunctions(staging, config_dir):
    linhas = load_yaml(config_dir, "linhas.yaml")
    d = diff_periodos(staging, linhas, Periodo(2024, 6), Periodo(2023, 6))

    # linhas do recibo: A = 2024 (valores esperados), B = 2023 sintético
    by_line = {p.chave: p for p in d.linhas}
    assert [p.chave for p in d.linhas] == list(ESPERADO)  # ordem do recibo, não por variação
    assert by_line["saude"].a == 200.0 and by_line["saude"].b == pytest.approx(270.0)
    assert by_line["saude"].variacao == pytest.approx(200 / 270 - 1)
    assert by_line["assistencia"].b == pytest.approx(90.0)
    assert by_line["juros"].b == pytest.approx(350.0 * ESCALA_2023)
    assert by_line["maquina_demais"].b == pytest.approx(25.2)  # 0,9 x (70 - 42): sem SIOP a exclusão (conta do Tesouro) sai inteira do resíduo

    # totais e exclusões
    tot = {p.chave: p for p in d.totais}
    assert tot["total_base_anexo02"].a == 2610.0 and tot["total_base_anexo02"].b == pytest.approx(2349.0)
    assert tot["total_recibo"].a == pytest.approx(2408.0) and "Anexo 06" in tot["total_recibo"].nome
    assert tot["servico_divida_fora_843_844"].a == pytest.approx(10.0) and tot["servico_divida_fora_843_844"].b == pytest.approx(9.0)
    assert tot["excluido:servico_divida_bruto"].b == pytest.approx(450.0)
    # a exclusão de empréstimos é a conta do Tesouro nos dois períodos (exata, sem SIOP)
    emp = tot["excluido:emprestimos_financeiros"]
    assert emp.a == pytest.approx(42.0) and emp.b == pytest.approx(37.8) and "aproximação" not in emp.nome

    # memos em reais nos dois períodos (2023 = 90% de 2024)
    memos = {p.chave: p for p in d.memos}
    assert memos["tributos"].a == 2100.0 and memos["tributos"].b == pytest.approx(1890.0)
    assert memos["resultado_primario"].a == 290.0 and memos["divida"].a == 120.0
    assert "gasto_por_real_de_tributo" not in memos and "origem_do_real_gasto" not in memos  # só unidades em reais

    # Anexo 01 por cod_conta
    a01 = {p.chave: p for p in d.anexo01}
    assert a01["amortizacao"].nome == "AmortizacaoDaDivida"
    assert a01["amortizacao"].a == 160.0 and a01["amortizacao"].b == pytest.approx(144.0)

    # funções: ordem por variação absoluta (08: +110; 09: +101 só pela escala; 28: +86; 10: -70)
    assert [p.chave for p in d.funcoes[:4]] == ["08", "09", "28", "10"]
    by_fun = {p.chave: p for p in d.funcoes}
    assert by_fun["10"].nome == "SAUDE" and by_fun["10"].b == pytest.approx(270.0)
    assert by_fun["09"].variacao == pytest.approx(1 / ESCALA_2023 - 1)

    # subfunções: chave função/subfunção, nome da linha como veio; 08/244 (+105) e 09/271 (+90) lideram
    by_sub = {p.chave: p for p in d.subfuncoes}
    assert [p.chave for p in d.subfuncoes[:3]] == ["08/244", "09/271", "10/301"]
    assert by_sub["10/301"].nome == "ATENCAO BASICA"
    assert by_sub["10/301"].a == 120.0 and by_sub["10/301"].b == pytest.approx(198.0)
    assert by_sub["08/244"].a == 150.0 and by_sub["08/244"].b == pytest.approx(45.0)
    assert by_sub["12/demais"].a == 70.0 and by_sub["12/demais"].b == pytest.approx(63.0)
    assert by_sub["09/272"].a == 100.0  # só o escopo exceto intra (a 272 intra de 25 não entra)


def test_render_and_json(staging, config_dir, tmp_path):
    linhas = load_yaml(config_dir, "linhas.yaml")
    d = diff_periodos(staging, linhas, Periodo(2024, 6), Periodo(2023, 6))

    txt = render(d, top=3)
    assert txt.startswith("diff 2024/p6 contra 2023/p6 | valores em R$")
    assert "SUBFUNÇÕES DO ANEXO 02" in txt and "mostrando 3" in txt and "MEMOS (fora do R$ 1,00" in txt and "Receita primária total" in txt
    assert "ATENCAO BASICA" in txt and "+122.2%" in txt and "-25.9%" in txt

    so28 = render(d, top=0, funcoes=["28"])
    assert "funções 28" in so28 and "28/843" in so28 and "10/301" not in so28

    p = write_json(d, tmp_path / "out" / "diff.json")
    data = json.loads(p.read_text(encoding="utf-8"))
    assert data["a"] == {"exercicio": 2024, "periodo": 6} and data["b"] == {"exercicio": 2023, "periodo": 6}
    sub = next(s for s in data["subfuncoes"] if s["chave"] == "10/301")
    assert sub["diff"] == pytest.approx(120.0 - 198.0) and sub["variacao"] == pytest.approx(120 / 198 - 1)


def test_diff_against_missing_period_shows_new(staging, config_dir):
    linhas = load_yaml(config_dir, "linhas.yaml")
    d = diff_periodos(staging, linhas, Periodo(2024, 6), Periodo(2022, 6))  # 2022 não está no staging: B inteiro zero
    assert all(p.b == 0.0 for p in d.subfuncoes)
    assert all(p.b == 0.0 for p in d.linhas) and all(p.b == 0.0 for p in d.totais) and all(p.b == 0.0 for p in d.memos)
    assert "novo" in render(d, top=5)


def test_percent_treats_float_dust_as_zero():
    from recibo.diff import Par, _pct

    # resíduo de ponto flutuante em B (soma de positivos e negativos) não pode virar "-1803879903137.8%"
    assert _pct(Par("x", "x", 6.5e9, -3.6e-12)) == "novo"
    assert _pct(Par("x", "x", 0.0, 0.0)) == "-"
    assert _pct(Par("x", "x", 110.0, 100.0)) == "+10.0%"
    assert Par("x", "x", 1.0, 1e-9).variacao is None
