"""S14 (IBGE): fetch com lacre, transform com guardas, inspect, população do ano e o per capita no recibo (memos) e no
mart da dívida (grupo por habitante). Planilha sintética no layout real da tab3 (revisão 2024) e JSONs no formato do
SIDRA; populações redondas para as contas fecharem à mão (Brasil 2024 = 200 milhões: R$ 2.408 do recibo sintético dão
R$ 0,00001204 por habitante)."""

import io
import json

import polars as pl
import pytest
from openpyxl import Workbook

from recibo.config import load_yaml, source_config
from recibo.fetchers.ibge import DATASET_PROJECAO, DATASET_SIDRA, SOURCE, chave_sidra, run_fetch
from recibo.inspect_ibge import inspect_ibge
from recibo.marts.divida import compute_divida
from recibo.marts.populacao import carregar_populacao, por_habitante
from recibo.marts.recibo import compute, print_summary, to_export
from recibo.transform.ibge import STAGING_FILE, IbgeFormato, build_staging, ler_projecao_xlsx, ler_sidra_json
from recibo.transform.siconfi import build_staging as build_staging_siconfi

from conftest import CONFIG_DIR, TOTAL_RECIBO, TRIBUTOS
from test_divida_bcb import _Resp
from test_mart_divida import staging_divida  # noqa: F401 - fixture reaproveitada (staging com dívida, BCB, COFOG e RREO sintéticos)

PROJ = {  # Brasil, projeção (1º de julho)
    2020: 197_000_000, 2021: 198_000_000, 2022: 198_500_000, 2023: 199_000_000, 2024: 200_000_000, 2025: 201_000_000, 2026: 202_000_000, 2027: 202_500_000,
}
EST = {2021: 198_500_000, 2024: 200_000_000, 2025: 201_000_000, 2026: 202_000_000}  # estimativas oficiais: 2021 na régua antiga, 2024+ iguais à projeção
CENSO_2022 = 193_000_000
URL_PROJ = source_config(CONFIG_DIR, "S14")["projecao"]["url"]
CFG_SIDRA = {t["id"]: t for t in source_config(CONFIG_DIR, "S14")["sidra"]}
CAB = ["ANO", "CÓD.", "SIGLA", "LOCAL", "POP_T", "POP_H", "POP_M", "0-1_T", "0-1_H", "0-1_M", "0-14_T", "15-64_T", "60+_T", "65+_T", "65+_H", "65+_M", "80+_T", "P_H", "P_M"]


def xlsx_projecao(colunas: list[str] | None = None, populacoes: dict | None = None) -> bytes:
    """Mesmo desenho da tab3 real: três linhas de título, grupos na linha 5 e 6, cabeçalho na 7, dados a partir da 8,
    um bloco por local (Brasil, depois uma região e uma UF), notas no fim."""
    cab = colunas or CAB
    pops = populacoes or PROJ
    wb = Workbook()
    ws = wb.active
    ws.title = "3) POP_GRUPO ETÁRIO ESPECÍFICO"
    ws.append(["Projeções das Populações, Revisão 2024"])
    ws.append(["Brasil, Grandes Regiões e Unidades da Federação"])
    ws.append(["População por sexo e grupos etários - 2000/2070"])
    ws.append([])
    ws.append([None, None, None, None, "POPULAÇÃO"] + [None] * 12 + ["PROPORÇÕES"])
    ws.append([None, None, None, None, "TOTAL", None, None, "CRIANÇAS E JOVENS"])
    ws.append(cab)
    for cod, sigla, local, fator in ((0, "BR", "Brasil", 1.0), (3, "SE", "Sudeste", 0.42), (35, "SP", "São Paulo", 0.22)):
        for ano, pop in sorted(pops.items()):
            total = int(round(pop * fator))
            homens, mulheres = total // 2 - 1000, total - (total // 2 - 1000)
            idosos = int(total * 0.11)
            linha = {"ANO": ano, "CÓD.": cod, "SIGLA": sigla, "LOCAL": local, "POP_T": total, "POP_H": homens, "POP_M": mulheres, "65+_T": idosos, "65+_H": idosos // 2, "65+_M": idosos - idosos // 2,
                     "0-1_T": int(total * 0.012), "0-1_H": int(total * 0.006), "0-1_M": int(total * 0.006), "0-14_T": int(total * 0.19), "15-64_T": int(total * 0.70), "60+_T": int(total * 0.16), "80+_T": int(total * 0.02), "P_H": 0.49, "P_M": 0.51}
            ws.append([linha.get(c) for c in cab])
    ws.append([])
    ws.append(["Fonte: IBGE, Diretoria de Pesquisas, Coordenação de População e Indicadores Sociais."])
    b = io.BytesIO()
    wb.save(b)
    return b.getvalue()


def sidra_json(valores: dict[int, int], variavel: int, nome: str) -> bytes:
    cab = {"NC": "Nível Territorial (Código)", "NN": "Nível Territorial", "MC": "Unidade de Medida (Código)", "MN": "Unidade de Medida", "V": "Valor", "D1C": "Brasil (Código)", "D1N": "Brasil", "D2C": "Variável (Código)", "D2N": "Variável", "D3C": "Ano (Código)", "D3N": "Ano"}
    rows = [{"NC": "1", "NN": "Brasil", "MC": "45", "MN": "Pessoas", "V": str(v), "D1C": "1", "D1N": "Brasil", "D2C": str(variavel), "D2N": nome, "D3C": str(a), "D3N": str(a)} for a, v in sorted(valores.items())]
    return json.dumps([cab] + rows, ensure_ascii=False).encode("utf-8")


class FakeIbge:
    def __init__(self, xlsx: bytes | None = None, est: dict | None = None, censo_ok: bool = True):
        self.xlsx = xlsx if xlsx is not None else xlsx_projecao()
        self.est = est if est is not None else EST
        self.censo_ok = censo_ok
        self.chamadas: list[str] = []

    def get(self, url: str, params: dict | None = None):
        self.chamadas.append(url)
        if url == URL_PROJ:
            return _Resp(url, self.xlsx, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        if url == CFG_SIDRA["estimativas"]["url"]:
            return _Resp(url, sidra_json(self.est, 9324, "População residente estimada"), "application/json")
        if url == CFG_SIDRA["censo_2022"]["url"]:
            if not self.censo_ok:
                return _Resp(url, b"<html><body>erro</body></html>", "text/html")
            return _Resp(url, sidra_json({2022: CENSO_2022}, 93, "População residente"), "application/json")
        raise AssertionError(f"url inesperada: {url}")


@pytest.fixture
def staging_ibge(store, tmp_path):
    cfg = source_config(CONFIG_DIR, "S14")
    linhas = load_yaml(CONFIG_DIR, "linhas.yaml")
    res = run_fetch(FakeIbge(), store, cfg)
    assert [r.status for r in res] == ["ok", "ok", "ok"] and all(r.changed for r in res)
    staging = tmp_path / "staging"
    build_staging(store, staging, cfg, linhas)
    return staging / STAGING_FILE


def test_fetch_lacra_e_nao_repete(store):
    cfg = source_config(CONFIG_DIR, "S14")
    fake = FakeIbge()
    r1 = run_fetch(fake, store, cfg)
    assert {r.dataset for r in r1} == {DATASET_PROJECAO, DATASET_SIDRA}
    idx_p = store.read_index(SOURCE, DATASET_PROJECAO)
    idx_s = store.read_index(SOURCE, DATASET_SIDRA)
    assert list(idx_p) == ["projecoes_2024_tab3_grupos_etarios_especificos.xlsx"] and idx_p["projecoes_2024_tab3_grupos_etarios_especificos.xlsx"]["status"] == "ok"
    assert set(idx_s) == {chave_sidra(6579, 9324), chave_sidra(4709, 93)} and idx_s[chave_sidra(4709, 93)]["opcional"] is True and idx_s[chave_sidra(6579, 9324)]["n_linhas"] == 4
    # segunda rodada: mesmo conteúdo, nenhum snapshot novo
    r2 = run_fetch(fake, store, cfg)
    assert all(not r.changed and r.status == "ok" for r in r2)
    raw, manifest = store.get_latest(SOURCE, DATASET_PROJECAO, "projecoes_2024_tab3_grupos_etarios_especificos.xlsx")
    assert raw.startswith(b"PK") and manifest["projecao"]["revisao"] == "2024" and manifest["request"]["url"] == URL_PROJ
    # Censo opcional quebrado: erro vira aviso, as outras duas seguem; projeção em HTML não grava
    r3 = run_fetch(FakeIbge(censo_ok=False), store, cfg)
    assert [(r.chave, r.status, r.opcional) for r in r3 if r.dataset == DATASET_SIDRA] == [(chave_sidra(6579, 9324), "ok", False), (chave_sidra(4709, 93), "erro", True)]
    r4 = run_fetch(FakeIbge(xlsx=b"<html>bloqueado</html>"), store, cfg)
    assert r4[0].status == "erro" and store.read_index(SOURCE, DATASET_PROJECAO)["projecoes_2024_tab3_grupos_etarios_especificos.xlsx"]["status"] == "ok"  # o lacre anterior continua


def test_transform_le_a_planilha_real_e_os_jsons(staging_ibge):
    df = pl.read_parquet(staging_ibge)
    assert set(df["regua"].unique().to_list()) == {"projecao", "estimativa", "censo"}
    br = df.filter((pl.col("local") == "Brasil") & (pl.col("regua") == "projecao")).sort("ano")
    assert br["ano"].to_list() == sorted(PROJ) and br["populacao"].to_list() == [PROJ[a] for a in sorted(PROJ)]
    r24 = br.filter(pl.col("ano") == 2024).row(0, named=True)
    assert str(r24["data_referencia"]) == "2024-07-01" and r24["homens"] + r24["mulheres"] == 200_000_000 and r24["idosos_65_mais"] == 22_000_000 and r24["revisao"] == "2024" and r24["sigla"] == "BR"
    assert df.filter(pl.col("local") == "São Paulo").height == len(PROJ)
    est = df.filter(pl.col("regua") == "estimativa").sort("ano")
    assert est["ano"].to_list() == sorted(EST) and est["tabela"].to_list() == ["6579"] * 4 and est["variavel"][0] == "9324" and est["homens"].null_count() == 4
    censo = df.filter(pl.col("regua") == "censo").row(0, named=True)
    assert censo["ano"] == 2022 and censo["populacao"] == CENSO_2022 and str(censo["data_referencia"]) == "2022-08-01" and censo["tabela"] == "4709"
    assert df["snapshot_id"].null_count() == 0 and df["sha256_canonical"].null_count() == 0


def test_transform_guardas(store, tmp_path):
    cfg = source_config(CONFIG_DIR, "S14")
    linhas = load_yaml(CONFIG_DIR, "linhas.yaml")
    pcfg = linhas["populacao"]["projecao_xlsx"]
    # coluna que sumiu
    with pytest.raises(IbgeFormato) as e:
        ler_projecao_xlsx(xlsx_projecao(colunas=[c for c in CAB if c != "POP_T"]), pcfg)
    assert "POP_T" in str(e.value) and "cabeçalho" in str(e.value)
    # aba com outro nome
    with pytest.raises(IbgeFormato) as e:
        ler_projecao_xlsx(xlsx_projecao(), {**pcfg, "planilha": "outra aba"})
    assert "não existe" in str(e.value)
    # escala: população em milhares não passa
    run_fetch(FakeIbge(xlsx=xlsx_projecao(populacoes={a: v // 1000 for a, v in PROJ.items()})), store, cfg)
    with pytest.raises(IbgeFormato) as e:
        build_staging(store, tmp_path / "s1", cfg, linhas)
    assert "fora da faixa" in str(e.value) and "Brasil" in str(e.value)
    # SIDRA: cabeçalho sem "Ano"
    with pytest.raises(IbgeFormato):
        ler_sidra_json(json.dumps([{"V": "Valor", "D1N": "Brasil"}, {"V": "1", "D1N": "Brasil"}]).encode("utf-8"))


def test_populacao_do_ano_e_conferencia(staging_ibge):
    linhas = load_yaml(CONFIG_DIR, "linhas.yaml")
    p24 = carregar_populacao(staging_ibge, linhas, 2024)
    assert p24["disponivel"] and p24["valor"] == 200_000_000 and p24["regua"] == "projecao" and p24["data_referencia"] == "2024-07-01"
    assert p24["estimativa"]["valor"] == 200_000_000 and p24["conferencia"]["ok"] and p24["conferencia"]["diff"] == 0
    assert p24["censo"]["valor"] == CENSO_2022 and p24["censo"]["projecao_no_mesmo_ano"] == 198_500_000
    assert por_habitante(TOTAL_RECIBO, p24) == pytest.approx(TOTAL_RECIBO / 200_000_000)
    p23 = carregar_populacao(staging_ibge, linhas, 2023)  # sem estimativa oficial: só a projeção, conferência "não calculada"
    assert p23["disponivel"] and p23["valor"] == 199_000_000 and p23["estimativa"] is None and p23["conferencia"]["calculada"] is False
    p40 = carregar_populacao(staging_ibge, linhas, 2040)
    assert not p40["disponivel"] and "não tem 2040" in p40["motivo"]
    nada = carregar_populacao(None, linhas, 2024)
    assert not nada["disponivel"] and "staging do IBGE ausente" in nada["motivo"] and por_habitante(10.0, nada) is None


def test_populacao_divergente_suspende_o_per_capita(store, tmp_path):
    cfg = source_config(CONFIG_DIR, "S14")
    linhas = load_yaml(CONFIG_DIR, "linhas.yaml")
    run_fetch(FakeIbge(est={**EST, 2024: 200_000_001}), store, cfg)
    stg = build_staging(store, tmp_path / "staging", cfg, linhas)
    p = carregar_populacao(stg, linhas, 2024)
    assert not p["disponivel"] and "difere da projeção" in p["motivo"] and p["conferencia"]["ok"] is False and p["conferencia"]["diff"] == 1
    assert carregar_populacao(stg, linhas, 2025)["disponivel"]


def test_recibo_memos_por_habitante(populated_store, config_dir, tmp_path, staging_ibge, capsys):
    staging = build_staging_siconfi(populated_store, config_dir, tmp_path / "stg_rreo")
    linhas = load_yaml(config_dir, "linhas.yaml")
    fontes = load_yaml(config_dir, "sources.yaml")
    res = compute(staging, linhas, 2024, 6, stg_ibge=staging_ibge)
    memos = {m.id: m for m in res.memos}
    ph = memos["por_habitante"]
    n = 200_000_000
    assert ph.disponivel and ph.unidade == "reais_por_habitante" and ph.valor == pytest.approx(TOTAL_RECIBO / n) and ph.fonte_id == "S4+S14"
    comp = {c["id"]: c for c in ph.componentes}
    assert comp["populacao"]["valor"] == n and comp["populacao"]["unidade"] == "pessoas"
    assert comp["tributos"]["valor"] == pytest.approx(TRIBUTOS / n) and comp["total_recibo"]["valor"] == pytest.approx(TOTAL_RECIBO / n)
    assert comp["juros"]["valor"] == pytest.approx(350.0 / n) and sum(c["valor"] for cid, c in comp.items() if cid not in ("populacao", "tributos", "total_recibo")) == pytest.approx(TOTAL_RECIBO / n)
    assert any("estimativa oficial" in x for x in ph.notas) and any("Censo 2022" in x for x in ph.notas)
    imf = memos["imposto_futuro"]
    assert imf.disponivel and imf.valor == pytest.approx(120.0 / n) and {c["id"]: c["valor"] for c in imf.componentes}["divida_liquida_por_habitante"] == pytest.approx(6120.0 / n)
    # nenhum centavo mudou; a conferência da população entrou como informativa e OK; a população está no export
    assert [x.centavos for x in res.linhas] == [x.centavos for x in compute(staging, linhas, 2024, 6).linhas] and res.publicavel
    rec = {r.id: r for r in res.reconciliacoes}["populacao_projecao_vs_estimativa"]
    assert rec.ok and not rec.critica and rec.esperado == n and rec.obtido == n
    export = to_export(res, linhas, fontes, {"selo": "fechado", "nota": ""})
    assert export["populacao"]["disponivel"] and export["populacao"]["valor"] == n and export["populacao"]["fonte"]["arquivo"].endswith(".xlsx")
    print_summary(export)
    out = capsys.readouterr().out
    assert "Gasto e tributos por habitante" in out and "R$ 0.00 por habitante" in out and f"{n:,}" in out and "[OK ] populacao_projecao_vs_estimativa (informativa)" in out
    # ano anterior (2023) sem estimativa oficial: memo sai mesmo assim, pela projeção
    res23 = compute(staging, linhas, 2023, 6, stg_ibge=staging_ibge)
    assert {m.id: m for m in res23.memos}["por_habitante"].disponivel and "populacao_projecao_vs_estimativa" not in {r.id for r in res23.reconciliacoes}


def test_mart_divida_por_habitante(staging_divida, config_dir, staging_ibge):
    linhas = load_yaml(config_dir, "linhas.yaml")
    fontes = load_yaml(config_dir, "sources.yaml")
    res = compute_divida(staging_divida, linhas, fontes, 2024, stg_siconfi=staging_divida / "stg_siconfi_rreo.parquet", periodo_recibo=6, config_dir=config_dir, stg_ibge=staging_ibge)
    ph = {m.id: m for m in res.medidas["por_habitante"]}
    n = 200_000_000
    assert ph["populacao"].valor == n and ph["populacao"].unidade == "pessoas" and [c["id"] for c in ph["populacao"].componentes] == ["estimativa", "censo"]
    assert ph["dpf_publico_por_habitante"].valor == pytest.approx(7_359.5e9 / n) and ph["dpf_publico_por_habitante"].unidade == "reais por habitante"
    assert ph["variacao_dpf_por_habitante"].valor == pytest.approx(839_500e6 / n)
    assert ph["dlsp_gf_por_habitante"].valor == pytest.approx(6_400_000e6 / n)
    assert ph["dcl_rreo_por_habitante"].valor == pytest.approx(6120.0 / n) and ph["juros_pagos_por_habitante"].valor == pytest.approx(350.0 / n)
    assert "snapshot" in ph["juros_pagos_por_habitante"].fonte and res.publicavel
    recs = {r.id: r for r in res.reconciliacoes}
    assert recs["populacao_projecao_vs_estimativa"].ok and not recs["populacao_projecao_vs_estimativa"].critica
    assert res.fontes["populacao"]["valor"] == n and res.fontes["populacao"]["estimativa"]["tabela"] == "6579"


def test_inspect_ibge(staging_ibge):
    out = inspect_ibge(staging_ibge, local="Brasil", desde=2021)
    assert "réguas: ['censo', 'estimativa', 'projecao']" in out and "projecao    2020 a 2027" in out
    assert "2024  " in out and "200,000,000" in out and "+500,000" in out and "+0.253%" in out  # 2021: estimativa antiga 500 mil acima da projeção
    assert "193,000,000" in out and "sexo e 65+" in out and "22,000,000" in out and "11.0%" in out
    assert "nenhuma linha para 'Marte'" in inspect_ibge(staging_ibge, local="Marte")
    assert "não existe" in inspect_ibge(staging_ibge.parent / "nao_existe.parquet")
