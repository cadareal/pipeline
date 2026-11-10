import json

import pytest

from recibo.config import load_yaml
from recibo.marts.recibo import ConfigInvalida, SemDados, compute, largest_remainder, to_export, tolerancia, write_outputs
from recibo.transform.siconfi import build_staging

from conftest import (
    AMORTIZACAO_ANEXO01,
    DESPESA_PRIMARIA_ANEXO06,
    ESPERADO,
    EXCLUSAO_TESOURO,
    FORA_843_844,
    INVERSOES_ANEXO01,
    INVERSOES_PRIMARIAS_ANEXO06,
    RECEITA_PRIMARIA,
    RECEITAS_FINANCEIRAS,
    REFINANCIAMENTO_ANEXO01,
    SERVICO_BRUTO,
    TOTAL_I,
    TOTAL_RECIBO,
    TRIBUTOS,
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
    assert res.anexo06["despesa_primaria_total:liquidadas"] == DESPESA_PRIMARIA_ANEXO06
    assert res.anexo06["inversoes:liquidadas"] == INVERSOES_PRIMARIAS_ANEXO06
    assert res.anexo01_receitas["receitas_exceto_intra"] == 3200.0 and res.anexo01_receitas["remuneracao_caixa"] == 140.0  # por prefixo

    assert sum(x.share for x in res.linhas) == pytest.approx(1.0)
    assert sum(x.centavos for x in res.linhas) == 100
    inss = next(x for x in res.linhas if x.id == "previdencia_inss")
    assert inss.centavos == 37  # 900/2408 = 37,4%

    # a exclusão de empréstimos vem da conta do Tesouro (exata, com ou sem SIOP); sem SIOP não há detalhe por linha
    excl_emp = next(e for e in res.excluidos if e.id == "emprestimos_financeiros")
    assert excl_emp.valor == pytest.approx(EXCLUSAO_TESOURO) and excl_emp.fonte_valor == "anexo06" and not excl_emp.por_linha
    assert excl_emp.por_grupo["inversoes_financeiras"] == {"anexo01": INVERSOES_ANEXO01, "anexo06_primaria": INVERSOES_PRIMARIAS_ANEXO06, "financeira": 41.0}
    assert excl_emp.por_grupo["outras_despesas_correntes"]["financeira"] == pytest.approx(1.0)
    assert excl_emp.por_grupo["pessoal"]["financeira"] == 0.0 and excl_emp.por_grupo["investimentos"]["financeira"] == 0.0
    assert any("sem detalhe por linha" in n for n in excl_emp.notas) and "Anexo 06" in excl_emp.sql
    trabalho = next(x for x in res.linhas if x.id == "trabalho")
    educacao = next(x for x in res.linhas if x.id == "educacao")
    assert any("FAT emprestado ao BNDES" in n for n in trabalho.notas) and any("FIES" in n for n in educacao.notas)
    assert not trabalho.transferencias  # sem SIOP nada sai das linhas nomeadas

    # mesmo período do exercício anterior (2023/6 sintético: 90% de 2024, com 100 movidos de 08 para 10)
    assert res.comparacao == {"exercicio": 2023, "periodo": 6, "disponivel": True}
    assert inss.anterior == pytest.approx(810.0) and inss.variacao == pytest.approx(1 / 0.9 - 1)
    saude = next(x for x in res.linhas if x.id == "saude")
    assert saude.anterior == pytest.approx(270.0) and saude.variacao == pytest.approx(200 / 270 - 1)
    maquina = next(x for x in res.linhas if x.cfg.get("residual"))
    assert maquina.anterior == pytest.approx(25.2)  # ajuste (9 = 315 + 144 - 450) e exclusão (37,8) também recalculados no ano anterior


def test_memos_from_anexo06_and_receitas(staging, config_dir):
    linhas = load_yaml(config_dir, "linhas.yaml")
    res = compute(staging, linhas, 2024, 6)
    memos = {m.id: m for m in res.memos}
    assert [m.id for m in res.memos] == [c["id"] for c in linhas["memos"]]

    trib = memos["tributos"]
    assert trib.disponivel and trib.valor == TRIBUTOS and trib.unidade == "reais"
    comp = {c["id"]: c["valor"] for c in trib.componentes}
    assert comp == {"impostos": 850.0, "taxas": 50.0, "contribuicoes_sociais": 1170.0, "contribuicoes_economicas": 30.0}

    rp = memos["receita_primaria"]
    assert rp.valor == RECEITA_PRIMARIA and {c["id"]: c["valor"] for c in rp.componentes}["patrimonial"] == 190.0

    fin = memos["receitas_financeiras"]
    assert fin.valor == pytest.approx(RECEITAS_FINANCEIRAS)
    comp = {c["id"]: c for c in fin.componentes}
    assert comp["operacoes_credito"]["valor"] == 600.0 and comp["remuneracao_caixa"]["valor"] == 140.0 and comp["resultado_bc"]["valor"] == 20.0
    assert comp["amortizacoes_emprestimos"]["valor"] == 30.0 and comp["outras_financeiras"]["valor"] == pytest.approx(60.0)
    assert comp["refinanciamento"]["valor"] == 1500.0 and comp["refinanciamento"]["no_total"] is False

    gpr = memos["gasto_por_real_de_tributo"]
    assert gpr.unidade == "reais_por_real" and gpr.valor == pytest.approx(TOTAL_RECIBO / TRIBUTOS)

    origem = memos["origem_do_real_gasto"]
    assert origem.unidade == "centavos" and origem.valor == pytest.approx(TOTAL_RECIBO - RECEITA_PRIMARIA)  # 58 cobertos com dívida ou caixa
    assert [(c["id"], c["centavos"]) for c in origem.componentes] == [("tributos", 87), ("outras_receitas_primarias", 10), ("divida_ou_caixa", 3)]
    assert sum(c["share"] for c in origem.componentes) == pytest.approx(1.0)

    prim = memos["resultado_primario"]
    comp = {c["id"]: c["valor"] for c in prim.componentes}
    assert prim.valor == 290.0 and comp["antes_do_ajuste"] == pytest.approx(299.0) and comp["ajuste_metodologico"] == -9.0 and comp["abaixo_da_linha"] == 299.0
    assert comp["despesa_paga"] == 1951.0 and comp["rp_processados_pagos"] == 55.0 and comp["rp_nao_processados_pagos"] == 45.0
    assert any("ajuste metodológico" in n for n in prim.notas)

    assert memos["juros_nominais"].valor == 400.0 and {c["id"]: c["valor"] for c in memos["juros_nominais"].componentes}["juros_pagos"] == 350.0
    assert memos["resultado_nominal"].valor == -110.0
    div = memos["divida"]
    assert div.valor == 120.0 and {c["id"]: c["valor"] for c in div.componentes}["liquida_final"] == 6120.0
    assert any("nominal_abaixo_vs_divida_liquida" in n for n in div.notas)
    assert any("Relatório do Tesouro Nacional" in n for n in prim.notas)
    rap = memos["restos_a_pagar"]
    comp = {c["id"]: c["valor"] for c in rap.componentes}
    assert rap.valor == 100.0 and comp["investimentos_pagos_no_ano"] == 80.0 and comp["investimentos_liquidados"] == 60.0 and comp["investimentos_restos"] == 30.0
    ret = memos["emprestimos_retorno"]
    assert ret.valor == 30.0 and {c["id"]: c["valor"] for c in ret.componentes}["emprestado"] == pytest.approx(EXCLUSAO_TESOURO)
    # sem staging do IBGE, os dois memos por habitante ficam indisponíveis com o motivo (nenhum centavo depende deles)
    assert not memos["imposto_futuro"].disponivel and "staging do IBGE ausente" in memos["imposto_futuro"].notas[0]
    assert not memos["por_habitante"].disponivel and memos["por_habitante"].fonte_id == "S4+S14"

    # memo marcado como implementado sem cálculo no código é erro de configuração, não silêncio
    linhas["memos"].append({"id": "inventado", "nome": "x", "status": "implementado"})
    with pytest.raises(ConfigInvalida):
        compute(staging, linhas, 2024, 6)


def test_reconciliations_pass_on_consistent_data(staging, config_dir):
    linhas = load_yaml(config_dir, "linhas.yaml")
    res = compute(staging, linhas, 2024, 6)
    by = {r.id: r for r in res.reconciliacoes}
    criticas = (
        "hierarquia", "total_cabecalho", "total_anexos", "fu_prefixo_consistente", "residual_nao_negativo", "centavos_somam_100", "identidade_total",
        "exclusao_financeira_por_grupo", "exclusao_por_linha", "tributos_anexo01_vs_anexo06", "receitas_anexo01_hierarquia",
        "receitas_anexo06_hierarquia", "despesas_anexo06_hierarquia", "resultado_primario_acima_da_linha",
    )
    for rid in criticas:
        assert by[rid].ok, (rid, by[rid])
        assert by[rid].critica, rid
    for rid in ("pessoal_anexo06_vs_anexo01", "investimentos_anexo06_vs_anexo01", "resultado_nominal_acima_da_linha", "resultado_primario_abaixo_da_linha", "divida_liquida_identidade", "nominal_abaixo_vs_divida_liquida"):
        assert by[rid].ok and not by[rid].critica, (rid, by[rid])
    assert by["nominal_abaixo_vs_divida_liquida"].esperado == -120.0 and "dívida nova 120.00" in by["nominal_abaixo_vs_divida_liquida"].detalhe
    # o cabeçalho (I) é só a primeira linha: a linha-resumo (II) do bloco I não entra
    assert by["total_cabecalho"].esperado == TOTAL_I
    assert by["identidade_total"].esperado == pytest.approx(DESPESA_PRIMARIA_ANEXO06 + 350.0) and by["identidade_total"].obtido == pytest.approx(TOTAL_RECIBO)
    assert by["exclusao_financeira_por_grupo"].esperado == pytest.approx(EXCLUSAO_TESOURO) and "inversoes_financeiras: Anexo 01 52.00" in by["exclusao_financeira_por_grupo"].detalhe
    assert "sem detalhe por linha" in by["exclusao_por_linha"].detalhe
    assert by["tributos_anexo01_vs_anexo06"].esperado == TRIBUTOS and by["tributos_anexo01_vs_anexo06"].obtido == TRIBUTOS
    assert by["resultado_primario_acima_da_linha"].esperado == pytest.approx(290.0) and "ajuste -9.00" in by["resultado_primario_acima_da_linha"].detalhe
    assert by["servico_divida_fora_843_844"].ok and not by["servico_divida_fora_843_844"].explicada
    assert by["servico_divida_fora_843_844"].obtido == pytest.approx(FORA_843_844)
    assert not by["servico_divida_fora_843_844"].critica
    assert "subfuncoes_ref_vs_siop" not in by  # só com SIOP e config_dir
    assert "lei_vs_tesouro_inversoes" not in by  # só com SIOP
    assert by["inss_vs_beneficios_anexo01"].ok  # 900 vs 920: 2,2% < 5%
    assert not by["inss_vs_beneficios_anexo01"].critica
    assert res.publicavel


def test_anexo06_formula_mismatch_blocks_publication(staging, config_dir):
    # se o Tesouro mudar a fórmula (ou a gente ler a coluna errada), a conferência crítica da fórmula do resultado acusa
    import polars as pl

    linhas = load_yaml(config_dir, "linhas.yaml")
    pdf = pl.read_parquet(staging).with_columns(
        pl.when((pl.col("cod_conta") == "RREO6ResultadoPrimarioEstadosMunicipios") & (pl.col("exercicio") == 2024))
        .then(pl.lit(250.0))
        .otherwise(pl.col("valor"))
        .alias("valor")
    )
    alt = staging.parent / "stg_alt06.parquet"
    pdf.write_parquet(alt)
    res = compute(alt, linhas, 2024, 6)
    by = {r.id: r for r in res.reconciliacoes}
    assert not by["resultado_primario_acima_da_linha"].ok and not res.publicavel
    assert by["identidade_total"].ok  # o total do recibo não depende dessa linha


def test_missing_anexo06_is_refused(staging, config_dir):
    import polars as pl

    linhas = load_yaml(config_dir, "linhas.yaml")
    pdf = pl.read_parquet(staging).filter(~((pl.col("anexo_norm") == "RREO-ANEXO 06") & (pl.col("exercicio") == 2024)))
    alt = staging.parent / "stg_sem06.parquet"
    pdf.write_parquet(alt)
    with pytest.raises(SemDados) as e:
        compute(alt, linhas, 2024, 6)
    assert "Anexo 06" in str(e.value) and "--exercicios 2024 --periodos 6" in str(e.value)
    # 2023 continua inteiro, mas 2024 (o exercício anterior de 2025, se existisse) não: para 2023 a comparação é com 2022, ausente
    res23 = compute(alt, linhas, 2023, 6)
    assert res23.publicavel and res23.total_recibo == pytest.approx(TOTAL_RECIBO * 0.9)


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
    assert "Anexo 06" in saude["ficha"]["denominador"]

    juros = next(x for x in export["linhas"] if x["id"] == "juros")
    assert "cod_conta = 'JurosEEncargosDaDivida'" in juros["ficha"]["sql"]
    assert "escopo" not in juros["ficha"]["sql"]

    assert export["total_recibo"] == pytest.approx(TOTAL_RECIBO) and "Anexo 06" in export["total_recibo_definicao"]
    assert export["fora_do_real"]["refinanciamento"]["valor"] == REFINANCIAMENTO_ANEXO01
    emp = export["fora_do_real"]["emprestimos_financeiros"]
    assert emp["valor"] == pytest.approx(EXCLUSAO_TESOURO) and emp["fonte_valor"] == "anexo06" and emp["por_linha_disponivel"] is False
    assert emp["por_linha"] == [] and emp["do_residual"] == pytest.approx(EXCLUSAO_TESOURO) and emp["acoes"] == [] and emp["lei_vs_tesouro"] is None
    assert emp["por_grupo"]["inversoes_financeiras"]["financeira"] == 41.0 and "Anexo 06" in emp["calculo"]
    assert "Dinheiro que saiu do caixa mas não foi gasto" in emp["motivo"]
    am = export["fora_do_real"]["amortizacao"]
    assert am["valor"] == AMORTIZACAO_ANEXO01
    assert am["em_843_844"] == pytest.approx(SERVICO_BRUTO - 350.0)
    assert am["fora_843_844_estimado"] == pytest.approx(FORA_843_844)

    memos = {m["id"]: m for m in export["memos"]}
    assert memos["tributos"]["valor"] == TRIBUTOS and memos["tributos"]["disponivel"] and memos["tributos"]["ficha"]["fontes"]
    assert memos["origem_do_real_gasto"]["componentes"][0]["centavos"] == 87
    assert memos["imposto_futuro"]["disponivel"] is False and memos["imposto_futuro"]["status"] == "implementado" and memos["imposto_futuro"]["ficha"]["fonte"]["id"] == "S4+S14"
    assert export["populacao"]["disponivel"] is False and "staging do IBGE ausente" in export["populacao"]["motivo"]
    assert export["anexo06_referencias"]["receita_primaria_total:realizadas"] == RECEITA_PRIMARIA
    assert export["anexo01_receitas"]["operacoes_credito"] == 600.0

    assert export["comparacao"] == {"exercicio": 2023, "periodo": 6, "disponivel": True}
    assert saude["anterior"] == pytest.approx(270.0) and saude["variacao_anual"] == pytest.approx(200 / 270 - 1)
    va = next(r for r in export["reconciliacoes"] if r["id"] == "variacao_anual")
    assert "saude" in va["detalhe"]

    p_json, p_csv, p_exp = write_outputs(export, tmp_path / "marts", tmp_path / "export")
    assert json.loads(p_json.read_text())["publicavel"] is True
    assert p_csv.read_text().splitlines()[0] == "id,nome,valor,share,centavos,anterior,variacao_anual"
    assert p_exp.exists()
    assert json.dumps(export, ensure_ascii=False)  # serializável


def test_print_summary_mentions_memos_and_exclusion(staging, config_dir, capsys):
    from recibo.marts.recibo import print_summary

    linhas = load_yaml(config_dir, "linhas.yaml")
    fontes = load_yaml(config_dir, "sources.yaml")
    export = to_export(compute(staging, linhas, 2024, 6), linhas, fontes, {"selo": "fechado"})
    print_summary(export)
    out = capsys.readouterr().out
    assert "Total do recibo (despesa primária liquidada do Anexo 06 + juros pagos do Anexo 01): 2,408.00" in out
    assert "Memos (fora do R$ 1,00" in out and "Tributos (impostos, taxas e contribuições)" in out and "2,100.00" in out
    assert "De onde veio cada R$ 1,00 gasto: Tributos 87 centavos | Outras receitas 10 centavos | Dívida nova ou caixa 3 centavos" in out
    assert "Gasto por R$ 1,00 de tributo" in out and "R$ 1.15 por R$ 1,00" in out
    assert "por grupo (Anexo 01 - Anexo 06 primária): pessoal 0.00; outras_despesas_correntes 1.00; investimentos 0.00; inversoes_financeiras 41.00" in out
    assert "sem detalhe por linha neste período: 42.00 saem do resíduo" in out
    assert "memos sem valor neste período ou planejados: Gasto e tributos por habitante, Dívida nova por habitante" in out
    assert "população (S14): população indisponível: staging do IBGE ausente" in out


def test_largest_remainder_sums_to_total():
    assert sum(largest_remainder([1, 1, 1], 100)) == 100
    assert largest_remainder([50, 50], 100) == [50, 50]
    assert largest_remainder([], 100) == []
    assert largest_remainder([0, 0], 100) == [0, 0]


def test_missing_period_is_refused_with_the_command_to_run(staging, config_dir):
    linhas = load_yaml(config_dir, "linhas.yaml")
    with pytest.raises(SemDados) as e:
        compute(staging, linhas, 2026, 3)
    assert "2026/p03" in str(e.value) and "recibo transform siconfi" in str(e.value) and "--exercicios 2026 --periodos 3" in str(e.value)
    # o diff precisa do período B ausente como zero: strict=False devolve um resultado vazio e não publicável
    res = compute(staging, linhas, 2026, 3, strict=False)
    assert res.total_recibo == 0 and not res.publicavel


def test_relative_tolerance_has_an_absolute_floor():
    # 2023 real: o RREO vem em dezenas de reais e a estimativa do serviço da dívida fora de 843/844 deu -2,26 contra 0,00
    # no SIOP; sem piso, 1 em 10 milhões de 2,26 reprovava um recibo de R$ 2,95 tri
    total = 2_952_134_769_270.0
    assert tolerancia(-2.26, 1e-7, total) == pytest.approx(total * 1e-9)  # R$ 2.952, o piso das identidades
    assert abs(0.0 - (-2.26)) <= tolerancia(-2.26, 1e-7, total)
    # com esperado grande, vale a relativa (1e-7 de 3,7 tri = R$ 370 mil), não o piso
    assert tolerancia(3_705_420_381_699.76, 1e-7, 3_705_420_381_699.76) == pytest.approx(370_542.04, rel=1e-6)
    # sem tolerância relativa, é a tolerância de identidade (R$ 1 no mínimo)
    assert tolerancia(0.0, None, total) == 1.0 and tolerancia(total, None, total) == pytest.approx(total * 1e-9)


def test_negative_rounding_dust_in_servico_divida_fora_is_ok(staging, config_dir):
    # amortização do Anexo 01 um pouco menor que 843/844 - juros (arredondamento da fonte): a informativa não acusa
    linhas = load_yaml(config_dir, "linhas.yaml")
    import polars as pl

    pdf = pl.read_parquet(staging)
    # amortização de 2024 de 160 para 149,5: fora de 843/844 = (350 + 149,5) - 500 = -0,5. No RREO real de 2023 foi -2,26
    # num total de R$ 2,95 tri (piso R$ 2.952); no sintético o total é 2.610, então o piso é R$ 1 e a poeira tem que ser menor
    pdf = pdf.with_columns(
        pl.when((pl.col("cod_conta") == "AmortizacaoDaDivida") & (pl.col("exercicio") == 2024) & (pl.col("valor") == 160.0))
        .then(pl.lit(149.5))
        .otherwise(pl.col("valor"))
        .alias("valor")
    )
    alt = staging.parent / "stg_alt.parquet"
    pdf.write_parquet(alt)
    res = compute(alt, linhas, 2024, 6)
    by = {r.id: r for r in res.reconciliacoes}
    assert by["servico_divida_fora_843_844"].obtido == pytest.approx(-0.5) and by["servico_divida_fora_843_844"].ok
    # a amortização menor aumenta a exclusão financeira (a conta do Tesouro é por diferença); o ajuste negativo não é
    # aplicado, então a soma das linhas fica R$ 0,50 abaixo do alvo: dentro do piso de R$ 1 da identidade
    assert by["identidade_total"].ok and res.total_recibo == pytest.approx(TOTAL_RECIBO - 0.5)
    assert next(e for e in res.excluidos if e.id == "emprestimos_financeiros").valor == pytest.approx(EXCLUSAO_TESOURO + 10.5)
    # ... e a decomposição por grupo deixa de fechar: é o que a crítica exclusao_financeira_por_grupo deve pegar
    assert not by["exclusao_financeira_por_grupo"].ok and not res.publicavel
    # mais do que o piso continua acusando (informativa)
    pdf2 = pl.read_parquet(staging).with_columns(
        pl.when((pl.col("cod_conta") == "AmortizacaoDaDivida") & (pl.col("exercicio") == 2024) & (pl.col("valor") == 160.0))
        .then(pl.lit(140.0))
        .otherwise(pl.col("valor"))
        .alias("valor")
    )
    pdf2.write_parquet(alt)
    by2 = {r.id: r for r in compute(alt, linhas, 2024, 6).reconciliacoes}
    assert by2["servico_divida_fora_843_844"].obtido == pytest.approx(-10.0) and not by2["servico_divida_fora_843_844"].ok
