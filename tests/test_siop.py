import json

import polars as pl
import pytest

from recibo.config import load_yaml, source_config
from recibo.fetchers.siop import DATASET, SOURCE, bindings, canonical_bindings, query_agregacao, query_exercicio, query_membros, run_fetch
from recibo.inspect_siop import inspect_siop
from recibo.marts.recibo import compute, to_export
from recibo.transform.siconfi import build_staging as build_staging_rreo
from recibo.transform.siop import STAGING_FILE, build_staging, eh_refinanciamento, esfera_tipo

from conftest import SIOP_BASE_2024, FakeSiop


@pytest.fixture
def cfg_s8(config_dir):
    return source_config(config_dir, "S8")


def test_queries_follow_the_siop_model(cfg_s8):
    q = query_agregacao(cfg_s8, 2025, ["esfera", "funcao", "gnd"], funcao="28")
    assert q.startswith("PREFIX loa: <http://vocab.e.gov.br/2013/09/loa#>")
    assert "GRAPH <http://orcamento.dados.gov.br/2025/>" in q
    assert "?i loa:temGND ?d_gnd . ?d_gnd loa:codigo ?cod_gnd ." in q
    assert '?d_funcao loa:codigo "28" .' in q
    assert "(SUM(?v_liquidado) AS ?liquidado)" in q and "(COUNT(DISTINCT ?i) AS ?n_itens)" in q
    assert q.rstrip().endswith("GROUP BY ?cod_esfera ?cod_funcao ?cod_gnd ORDER BY ?cod_esfera ?cod_funcao ?cod_gnd")

    # fatia por função quando a função não é dimensão: o padrão é acrescentado
    q2 = query_agregacao(cfg_s8, 2024, ["gnd"], funcao="09")
    assert "?i loa:temFuncao ?d_funcao ." in q2 and '?d_funcao loa:codigo "09" .' in q2

    # órgão vem pela unidade orçamentária
    qm = query_membros(cfg_s8, 2024, "orgao")
    assert "?i loa:temUnidadeOrcamentaria ?uo_orgao . ?uo_orgao loa:temOrgao ?d_orgao ." in qm
    assert "OPTIONAL { ?d_orgao rdfs:label ?rotulo }" in qm

    assert "loa:dataUltimaAtualizacao" in query_exercicio(cfg_s8, 2026)
    with pytest.raises(KeyError):
        query_agregacao(cfg_s8, 2024, ["inexistente"])


def test_bindings_and_canonical_are_order_independent():
    payload = {"results": {"bindings": [
        {"cod_gnd": {"type": "literal", "value": "2"}, "liquidado": {"type": "typed-literal", "datatype": "x", "value": "350"}},
        {"cod_gnd": {"type": "literal", "value": "6"}, "liquidado": {"type": "typed-literal", "datatype": "x", "value": "160"}},
    ]}}
    rows = bindings(payload)
    assert rows == [{"cod_gnd": "2", "liquidado": "350"}, {"cod_gnd": "6", "liquidado": "160"}]
    assert canonical_bindings(rows) == canonical_bindings(list(reversed(rows)))


def test_fetch_writes_raw_and_skips_unchanged_years(populated_store, config_dir, cfg_s8):
    fake = FakeSiop(anos={2024: SIOP_BASE_2024})
    res = run_fetch(fake, populated_store, cfg_s8, [2024, 2023])
    chaves = {r.key: r for r in res}
    assert chaves["2024/exercicio"].status == "ok" and chaves["2023/exercicio"].status == "vazio"
    assert chaves["2024/membros/funcao"].n_rows == 9
    assert chaves["2024/fonte_gnd_subfuncao"].status == "ok"          # inteira
    assert "2024/acao_subfuncao/28" in chaves and "2024/elemento_orgao/09" in chaves  # fatiadas por função
    n_funcoes = 9
    assert fake.calls == {"exercicio": 2, "membros": 10, "agregacao": 2 + 2 * n_funcoes}
    index = populated_store.read_index(SOURCE, DATASET)
    assert index["2024/exercicio"]["data_atualizacao"] == "2025-01-31T10:00:00"
    assert index["2024/acao_subfuncao/28"]["status"] == "ok"

    # segunda rodada, SIOP sem atualização: só a consulta de exercício roda
    fake.calls = {"exercicio": 0, "membros": 0, "agregacao": 0}
    run_fetch(fake, populated_store, cfg_s8, [2024])
    assert fake.calls == {"exercicio": 1, "membros": 0, "agregacao": 0}

    # --force refaz; conteúdo igual não gera snapshot novo
    run_fetch(fake, populated_store, cfg_s8, [2024], force=True)
    assert fake.calls["agregacao"] == 2 + 2 * n_funcoes
    assert not any(r.changed for r in run_fetch(fake, populated_store, cfg_s8, [2024], force=True))

    # data de atualização nova: o exercício muda e tudo é refeito
    fake.data = "2025-02-15T08:00:00"
    fake.calls = {"exercicio": 0, "membros": 0, "agregacao": 0}
    res3 = run_fetch(fake, populated_store, cfg_s8, [2024])
    assert {r.key: r.changed for r in res3}["2024/exercicio"] is True
    assert fake.calls["agregacao"] == 2 + 2 * n_funcoes

    manifest = populated_store.get_latest(SOURCE, DATASET, "2024/acao_subfuncao/28")[1]
    assert manifest["agregacao"] == "acao_subfuncao" and manifest["funcao"] == "28"
    assert 'loa:codigo "28"' in manifest["request"]["query"]


def test_transform_builds_staging_with_labels_and_flags(populated_store_siop, tmp_path):
    path = build_staging(populated_store_siop, tmp_path / "staging")
    assert path.name == STAGING_FILE
    df = pl.read_parquet(path)
    assert set(df["agregacao"].unique().to_list()) == {"fonte_gnd_subfuncao", "elemento_orgao", "rp_subfuncao", "acao_subfuncao"}
    assert df["data_atualizacao"].unique().to_list() == ["2025-01-31T10:00:00"]

    gnd = df.filter((pl.col("agregacao") == "fonte_gnd_subfuncao") & (pl.col("exercicio") == 2024))
    bc = gnd.filter((pl.col("cod_subfuncao") == "846") & (pl.col("cod_gnd") == "6")).row(0, named=True)
    assert bc["liquidado"] == 8.0 and bc["refinanciamento"] is False and bc["intra"] is False and bc["ofss"] is True
    assert bc["cod_fonte"] == "1444"  # títulos, "demais aplicações": não é rolagem, mesmo sendo GND 6
    assert bc["nome_subfuncao"] == "Outros Encargos Especiais" and bc["nome_gnd"] == "Amortização da Dívida"
    refin = gnd.filter(pl.col("cod_subfuncao") == "841").row(0, named=True)
    assert refin["refinanciamento"] is True and refin["liquidado"] == 1500.0 and refin["cod_fonte"] == "1443"
    juros = gnd.filter((pl.col("cod_subfuncao") == "843") & (pl.col("cod_gnd") == "2")).row(0, named=True)
    assert juros["cod_fonte"] == "1444" and juros["refinanciamento"] is False  # fonte 444 com GND 2: juros, não rolagem
    assert eh_refinanciamento("6", "3443", ["443"]) and not eh_refinanciamento("2", "1443", ["443"]) and not eh_refinanciamento("6", "1444", ["443"])
    intra = gnd.filter(pl.col("intra")).row(0, named=True)
    assert intra["cod_modalidade_aplicacao"] == "91" and intra["liquidado"] == 25.0
    estatal = gnd.filter(pl.col("esfera_tipo") == "investimento").row(0, named=True)
    assert estatal["ofss"] is False and estatal["cod_funcao"] == "25"

    # totais do universo comparável ao RREO: fiscal + seguridade, sem intra, sem refinanciamento
    comparavel = gnd.filter(pl.col("ofss") & ~pl.col("intra") & ~pl.col("refinanciamento"))
    assert comparavel["liquidado"].sum() == pytest.approx(2610.0)
    assert comparavel.filter(pl.col("cod_gnd") == "2")["liquidado"].sum() == pytest.approx(350.0)

    acao = df.filter((pl.col("agregacao") == "acao_subfuncao") & (pl.col("cod_acao") == "0669") & (pl.col("exercicio") == 2024)).row(0, named=True)
    assert acao["nome_acao"].startswith("Cobertura do Resultado Negativo") and acao["fatia_funcao"] == "28"
    # dimensões ausentes numa agregação ficam nulas
    assert acao["cod_elemento_despesa"] is None and acao["cod_fonte"] is None

    assert esfera_tipo("Orçamento da Seguridade Social") == "seguridade" and esfera_tipo(None) == "desconhecida"


def test_mart_cross_checks_siop_against_rreo(populated_store_siop, config_dir, tmp_path):
    stg = build_staging_rreo(populated_store_siop, config_dir, tmp_path / "staging")
    siop = build_staging(populated_store_siop, tmp_path / "staging")
    linhas = load_yaml(config_dir, "linhas.yaml")
    fontes = load_yaml(config_dir, "sources.yaml")

    res = compute(stg, linhas, 2024, 6, siop_path=siop)
    by = {r.id: r for r in res.reconciliacoes}
    for rid in ("siop_vs_rreo_funcao", "siop_juros_vs_anexo01", "siop_gnd6_vs_anexo01", "siop_refinanciamento_vs_anexo01", "servico_divida_fora_843_844_siop", "inativos_pensionistas_siop_vs_rreo"):
        assert rid in by and by[rid].ok, (rid, by.get(rid))
    assert {rid for rid, r in by.items() if r.critica and rid.startswith(("siop", "servico_divida_fora_843_844_siop"))} == {"siop_vs_rreo_funcao", "servico_divida_fora_843_844_siop"}
    assert by["siop_vs_rreo_funcao"].esperado == pytest.approx(2610.0) and by["siop_vs_rreo_funcao"].obtido == pytest.approx(2610.0)
    assert "todas as 8 funções" in by["siop_vs_rreo_funcao"].detalhe
    assert by["inativos_pensionistas_siop_vs_rreo"].esperado == 100.0 and by["inativos_pensionistas_siop_vs_rreo"].obtido == 100.0

    # a linha de aposentados vem do SIOP (elementos 01 e 03, em qualquer função) e tira de Defesa as pensões militares
    val = {x.id: x for x in res.linhas}
    inat, defesa, maquina = val["inativos_pensionistas"], val["defesa_seguranca"], val["maquina_demais"]
    assert inat.fonte_valor == "siop" and not inat.piso and inat.valor == 110.0
    assert defesa.valor == 90.0 and defesa.fonte_valor == "rreo"
    assert inat.transferencias == [{"de": "defesa_seguranca", "valor": 10.0, "sql": inat.transferencias[0]["sql"]}]
    assert defesa.transferencias[0]["para"] == "inativos_pensionistas" and defesa.transferencias[0]["valor"] == -10.0
    assert any("não inclui 10.00" in n for n in defesa.notas)
    assert maquina.valor == 70.0 and maquina.nome == "Encargos especiais e demais funções"
    assert res.total_recibo == pytest.approx(2450.0) and by["identidade_total"].ok and by["centavos_somam_100"].ok
    assert inat.centavos == 4 and defesa.centavos == 4 and val["previdencia_inss"].centavos == 37
    assert any("juros classificados fora de 843/844: 0669" in n for n in val["juros"].notas)
    # 2023 sintético também tem SIOP: a variação anual compara SIOP com SIOP (110 vs 99, +11,1%: não acusa)
    assert inat.anterior == pytest.approx(99.0) and inat.variacao == pytest.approx(110 / 99 - 1)
    assert "inativos" not in by["variacao_anual"].detalhe and by["variacao_anual"].obtido == 2
    assert by["siop_juros_vs_anexo01"].obtido == pytest.approx(350.0)
    assert by["siop_gnd6_vs_anexo01"].esperado == pytest.approx(1660.0) and by["siop_gnd6_vs_anexo01"].obtido == pytest.approx(1660.0)
    assert by["siop_refinanciamento_vs_anexo01"].obtido == pytest.approx(1500.0)
    fora = by["servico_divida_fora_843_844_siop"]
    assert "0669" in fora.detalhe and "Banco Central" in fora.detalhe
    assert "siop_comparavel" not in by
    assert res.publicavel

    assert res.siop["disponivel"] is True and res.siop["usado_nas_linhas"] is True and res.siop["data_atualizacao"] == "2025-01-31T10:00:00"
    assert res.siop["servico_divida_fora_843_844"] == [
        {"acao": "0669", "nome": "Cobertura do Resultado Negativo Apurado no Banco Central do Brasil", "gnd": "6", "liquidado": 8.0},
        {"acao": "0669", "nome": "Cobertura do Resultado Negativo Apurado no Banco Central do Brasil", "gnd": "2", "liquidado": 2.0},
    ]
    assert by["servico_divida_fora_843_844_siop"].esperado == pytest.approx(10.0) and by["servico_divida_fora_843_844_siop"].obtido == pytest.approx(10.0)
    assert sorted((d["acao"], d["gnd"]) for d in res.siop["detalhe_846"]) == [("0005", "3"), ("0669", "2"), ("0669", "6")]
    export = to_export(res, linhas, fontes, {"selo": "fechado"})
    assert export["siop"]["servico_divida_fora_843_844"][0]["acao"] == "0669"
    assert export["fora_do_real"]["servico_divida_fora_843_844"]["valor_rreo"] == pytest.approx(10.0)
    linha_inat = next(x for x in export["linhas"] if x["id"] == "inativos_pensionistas")
    assert linha_inat["fonte_valor"] == "siop" and linha_inat["transferencias"] == [{"de": "defesa_seguranca", "valor": 10.0}]
    assert linha_inat["ficha"]["fallback"]["filtro"] == "subfuncao_cod IN ('272', '274')"
    assert json.dumps(export, ensure_ascii=False)  # serializável

    # sem o staging do SIOP, nada de cruzado e a linha de aposentados usa o fallback do RREO (piso)
    res_sem = compute(stg, linhas, 2024, 6)
    assert res_sem.siop == {"disponivel": False, "usado_nas_linhas": False}
    assert not any(r.id.startswith("siop") for r in res_sem.reconciliacoes)
    inat_sem = next(x for x in res_sem.linhas if x.id == "inativos_pensionistas")
    assert inat_sem.piso and inat_sem.fonte_valor == "rreo" and inat_sem.valor == 100.0 and any("piso" in n for n in inat_sem.notas)
    assert next(x for x in res_sem.linhas if x.id == "defesa_seguranca").valor == 100.0
    assert res_sem.total_recibo == pytest.approx(2450.0)

    # ano em andamento: só o aviso e o detalhe por ação; linha de aposentados no piso
    res_p3 = compute(stg, linhas, 2024, 3, siop_path=siop)
    by3 = {r.id: r for r in res_p3.reconciliacoes}
    assert by3["siop_comparavel"].ok and "ano em andamento" in by3["siop_comparavel"].detalhe and "0669" in by3["siop_comparavel"].detalhe
    assert "siop_vs_rreo_funcao" not in by3
    assert next(x for x in res_p3.linhas if x.id == "inativos_pensionistas").piso


def test_inspect_siop_text(populated_store_siop):
    txt = inspect_siop(populated_store_siop, 2024, "acao_subfuncao", funcao="28", rows=8)
    assert "data da última atualização no SIOP: 2025-01-31T10:00:00" in txt
    assert "liquidado por GND" in txt and "Cobertura do Resultado Negativo" in txt
    assert "função 28" in txt
    vazio = inspect_siop(populated_store_siop, 2024, "nao_existe")
    assert "sem raw para essa agregação" in vazio and "acao_subfuncao" in vazio
    assert len(SIOP_BASE_2024) == 30


def test_fetch_keeps_going_when_one_query_fails(populated_store, cfg_s8):
    class Quebrado(FakeSiop):
        def responder(self, query):
            if 'loa:codigo "28"' in query and "?cod_acao" in query:
                raise RuntimeError("SIOP não devolveu JSON (200): <html>Transaction timed out</html>")
            return super().responder(query)

    res = run_fetch(Quebrado(), populated_store, cfg_s8, [2024])
    by = {r.key: r for r in res}
    assert by["2024/acao_subfuncao/28"].status == "erro"
    assert by["2024/acao_subfuncao/09"].status == "ok" and by["2024/elemento_orgao/28"].status == "ok"
    index = populated_store.read_index(SOURCE, DATASET)
    assert "Transaction timed out" in index["2024/acao_subfuncao/28"]["erro"]
    # na rodada seguinte o exercício não é pulado, porque o raw não está completo
    fake = FakeSiop()
    res2 = run_fetch(fake, populated_store, cfg_s8, [2024])
    assert {r.key: r for r in res2}["2024/acao_subfuncao/28"].status == "ok"
    assert fake.calls["agregacao"] > 0
