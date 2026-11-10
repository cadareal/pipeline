"""Níveis de dentro de cada linha (o que o site abre ao clicar): subfunções do RREO com ajustes nomeados pelo SIOP,
funções do resíduo, elementos e órgãos das linhas do SIOP. A conta fecha linha a linha, com e sem SIOP."""

from __future__ import annotations

import pytest

from recibo.config import load_yaml
from recibo.marts.niveis import nome_subfuncao
from recibo.marts.recibo import compute, to_export
from recibo.transform.siconfi import build_staging as build_staging_rreo
from recibo.transform.siop import build_staging


@pytest.fixture
def stagings(populated_store_siop, config_dir, tmp_path):
    stg = build_staging_rreo(populated_store_siop, config_dir, tmp_path / "staging")
    siop = build_staging(populated_store_siop, tmp_path / "staging")
    return stg, siop


def _sub(n: dict, sid: str) -> dict:
    return next(it for it in n["itens"] if it["id"] == sid)


def test_niveis_fecham_com_siop(stagings, config_dir):
    stg, siop = stagings
    linhas = load_yaml(config_dir, "linhas.yaml")
    res = compute(stg, linhas, 2024, 6, siop_path=siop, config_dir=config_dir)
    rec = next(r for r in res.reconciliacoes if r.id == "niveis_fecham")
    assert rec.ok and not rec.critica and "9 linhas abertas em níveis, todas fecham" in rec.detalhe
    val = {x.id: x for x in res.linhas}
    for x in res.linhas:
        n = res.niveis[x.id]
        assert n["fecha"], (x.id, n)
        if n["tipo"]:
            assert sum(it["valor"] for it in n["itens"]) + sum(a["valor"] for a in n["nao_alocado"]) == pytest.approx(x.valor)

    # subfunções do RREO, com o que saiu da linha (SIOP) descontado da subfunção certa e as ações dentro
    saude = res.niveis["saude"]
    assert saude["tipo"] == "subfuncoes" and [it["id"] for it in saude["itens"]] == ["10-301", "10-302"]
    assert _sub(saude, "10-301")["nome"] == "Atenção Básica" and _sub(saude, "10-301")["valor"] == 120.0 and _sub(saude, "10-301")["funcao"] == "Saúde"
    assert len(_sub(saude, "10-301")["acoes"]) == 2 and _sub(saude, "10-301")["acoes_total_siop"] == 120.0 and saude["acoes_fonte"].startswith("SIOP")
    defesa = res.niveis["defesa_seguranca"]
    adm = _sub(defesa, "05-122")
    assert adm["valor_rreo"] == 50.0 and adm["ajustes"] == [{"nome": "saiu para a linha Aposentados e pensionistas do setor público", "para": "inativos_pensionistas", "valor": -10.0}] and adm["valor"] == 40.0
    assert _sub(defesa, "05-demais")["nome"] == "Demais subfunções"
    educ = res.niveis["educacao"]
    assert _sub(educ, "12-demais")["ajustes"][0]["para"] == "fora_do_real" and _sub(educ, "12-demais")["valor"] == 65.0
    # quando o SIOP acha o que saiu numa subfunção que o RREO não lista na linha, o ajuste fica nomeado, não some
    trab = res.niveis["trabalho"]
    assert trab["nao_alocado"] and trab["nao_alocado"][0]["valor"] == -20.0 and "subfunções que o RREO não lista" in trab["nao_alocado"][0]["nome"]
    assert sum(it["valor"] for it in trab["itens"]) - 20.0 == val["trabalho"].valor

    # linha do SIOP: elementos (aposentadorias, pensões) abertos por órgão
    inat = res.niveis["inativos_pensionistas"]
    assert inat["tipo"] == "elementos" and [it["cod"] for it in inat["itens"]] == ["01", "03"] and inat["itens"][0]["valor"] == 100.0
    assert inat["itens"][1]["orgaos"] == [{"cod": "52000", "nome": inat["itens"][1]["orgaos"][0]["nome"], "valor": 10.0}]

    # resíduo: funções, com o que saiu dele (aposentados, empréstimos pela lei, serviço da dívida fora de 843/844)
    # descontado, e o que a lei chama de empréstimo e o Tesouro conta como gasto de volta, nomeado
    maq = res.niveis["maquina_demais"]
    assert maq["tipo"] == "funcoes" and [f["cod"] for f in maq["itens"]] == ["28", "09", "01"] and maq["soma"] == pytest.approx(53.0)
    f28 = maq["itens"][0]
    por = {s["id"]: s for s in f28["subfuncoes"]}
    assert por["28-846"]["valor_rreo"] == 20.0 and {a["para"] for a in por["28-846"]["ajustes"]} == {"fora_do_real", "juros"}
    assert por["lei_vs_tesouro"]["valor"] == 4.0 and por["lei_vs_tesouro"]["detalhe"]["fundos_constitucionais"] == 3.0
    f09 = maq["itens"][1]
    assert next(s for s in f09["subfuncoes"] if s["subfuncao_cod"] == "272")["ajustes"][0]["para"] == "inativos_pensionistas"
    assert maq["nao_alocado"] == [{"nome": "fora do R$ 1,00: despesas que o Tesouro trata como financeiras em outros grupos (sem detalhe por subfunção)", "para": "fora_do_real", "valor": -1.0}]
    # juros não abre aqui: as partes vêm do mart da dívida
    assert res.niveis["juros"]["tipo"] is None and "mart da dívida" in res.niveis["juros"]["fonte"]

    # o export carrega os níveis em cada linha
    export = to_export(res, linhas, load_yaml(config_dir, "sources.yaml"), {"selo": "fechado", "nota": ""})
    by = {x["id"]: x for x in export["linhas"]}
    assert by["saude"]["niveis"]["itens"][0]["acoes"][0]["cod"] and by["maquina_demais"]["niveis"]["tipo"] == "funcoes"
    assert next(r for r in export["reconciliacoes"] if r["id"] == "niveis_fecham")["ok"]


def test_niveis_sem_siop_nomeiam_o_que_nao_tem_detalhe(stagings, config_dir):
    stg, _ = stagings
    linhas = load_yaml(config_dir, "linhas.yaml")
    res = compute(stg, linhas, 2024, 6)
    assert next(r for r in res.reconciliacoes if r.id == "niveis_fecham").ok
    # aposentados pelo RREO (piso): abre pelas subfunções do fallback
    inat = res.niveis["inativos_pensionistas"]
    assert inat["tipo"] == "subfuncoes" and [it["id"] for it in inat["itens"]] == ["09-272"] and inat["fonte"].startswith("RREO")
    # sem ações (precisam do SIOP) e sem ajustes por subfunção; o resíduo nomeia o que sai dele sem detalhe
    assert "acoes" not in res.niveis["saude"]["itens"][0] and res.niveis["saude"]["acoes_fonte"] is None
    maq = res.niveis["maquina_demais"]
    nomes = [a["nome"] for a in maq["nao_alocado"]]
    assert any("empréstimos e aplicações financeiras (sem detalhe" in n for n in nomes) and any("serviço da dívida fora de 843/844 (sem detalhe" in n for n in nomes)
    assert sum(a["valor"] for a in maq["nao_alocado"]) == pytest.approx(-52.0) and maq["fecha"]
    assert [f["cod"] for f in maq["itens"]] == ["28", "09", "01"] and maq["itens"][0]["valor"] == 60.0


def test_nome_subfuncao_limpa_prefixo():
    assert nome_subfuncao("FU10 - Administração Geral", "122") == "Administração Geral"
    assert nome_subfuncao("FU28 - Demais Subfunções", "demais") == "Demais subfunções"
    assert nome_subfuncao("Atenção Básica", "301") == "Atenção Básica"
    assert nome_subfuncao(None, "999") == "999"
