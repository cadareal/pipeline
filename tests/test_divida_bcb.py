"""S1 (dívida, CKAN do Tesouro) e S16 (BCB SGS): fetch com lacre e versão, transform genérico (CSV tipado e grade de
planilha; séries do BC), inspect. Tudo sobre dados sintéticos: um CSV no padrão brasileiro, uma planilha-relatório feita
com openpyxl e três séries do SGS (um fluxo, um saldo, uma taxa)."""

import io
import json
from datetime import date

import polars as pl
import pytest
from openpyxl import Workbook

from recibo.config import source_config
from recibo.fetchers.bcb import DATASET as BCB_DATASET, DATASET_META, SOURCE as BCB_SOURCE, canonical_serie, escolher_package, run_fetch as run_fetch_bcb
from recibo.fetchers.cofog import KEY_PACKAGE
from recibo.fetchers.divida import SOURCE as DIV_SOURCE, recursos_do_dataset, run_fetch as run_fetch_divida
from recibo.inspect_bcb import agregado_anual, inspect_bcb
from recibo.inspect_divida import inspect_divida
from recibo.store import LocalBackend, Store
from recibo.transform.bcb import build_staging as build_staging_bcb, pontos_para_df
from recibo.transform.cofog import CofogFormato
from recibo.transform.divida import INDICE_FILE, build_staging as build_staging_divida, ler_csv_generico, ler_xlsx_grade

from conftest import CONFIG_DIR

CSV_ESTOQUE = (
    "Mês;Tipo de Dívida;Indexador;Valor (R$ milhões);Observação\n"
    "01/2024;DPMFi;Selic;1.234.567,89;\n"
    "01/2024;DPMFi;Prefixado;987.654,32;x\n"
    "02/2024;DPMFi;Selic;1.300.000,00;\n"
    "02/2024;DPFe;Câmbio;-10,50;y\n"
    "12/2023;DPMFi;Selic;1.000.000,00;\n"
).encode("latin-1")


def xlsx_fatores() -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "Fatores"
    ws.append(["Fatores de Variação da Dívida Pública Federal"])
    ws.append([])
    ws.append(["Mês", "Emissões", "Resgates", "Juros apropriados"])
    ws.append([date(2024, 1, 1), 100.5, 80, 60.25])
    ws.append([date(2024, 2, 1), 110, 90, 61])
    ws2 = wb.create_sheet("Notas")
    ws2.append(["Fonte: Tesouro Nacional"])
    b = io.BytesIO()
    wb.save(b)
    return b.getvalue()


class _Resp:
    def __init__(self, url, content: bytes, ctype: str, status: int = 200):
        self.url = url
        self.status_code = status
        self.headers = {"content-type": ctype}
        self.content = content
        self.text = content.decode("utf-8", errors="replace")

    def json(self):
        return json.loads(self.content.decode("utf-8"))


class FakeTesouro:
    """CKAN do Tesouro com dois datasets: estoque_dpf (CSV + XLSX + PDF) e fatores_variacao (XLSX + PDF)."""

    def __init__(self):
        self.csv = CSV_ESTOQUE
        self.xlsx = xlsx_fatores()
        self.packages = {
            "0998f610-bc25-4ce3-b32c-a873447500c2": {"id": "0998f610", "title": "Estoque da Dívida Pública Federal", "resources": [
                {"id": "r1", "name": "Estoque DPF", "format": "CSV", "url": "https://x/estoque-dpf.csv", "created": "2020-01-01T00:00:00", "last_modified": "2026-10-29T10:00:00", "size": str(len(self.csv))},
                {"id": "r2", "name": "Estoque DPF (xlsx)", "format": "XLSX", "url": "https://x/estoque-dpf.xlsx", "created": "2020-01-01T00:00:00", "last_modified": "2026-10-29T10:00:00", "size": str(len(self.xlsx))},
                {"id": "r3", "name": "Metadados", "format": "PDF", "url": "https://x/meta.pdf", "created": "2020-01-01T00:00:00", "last_modified": None, "size": "1000"},
            ]},
            "fc2ea6ab-2130-4525-98f6-8bf04407f7fe": {"id": "fc2ea6ab", "title": "Fatores de Variação da Dívida Pública Federal", "resources": [
                {"id": "r4", "name": "Fatores", "format": "XLSX", "url": "https://x/fatores-variacao.xlsx", "created": "2020-01-01T00:00:00", "last_modified": "2026-10-29T10:00:00", "size": str(len(self.xlsx))},
                {"id": "r5", "name": "Metadados", "format": "PDF", "url": "https://x/meta2.pdf", "created": "2020-01-01T00:00:00", "last_modified": None, "size": "1000"},
            ]},
        }
        self.downloads: list[str] = []

    def get(self, url: str, params: dict | None = None):
        if url.endswith("/package_show"):
            pkg = self.packages[params["id"]]
            return _Resp(url, json.dumps({"success": True, "result": pkg}, ensure_ascii=False).encode("utf-8"), "application/json")
        self.downloads.append(url)
        if url.endswith(".csv"):
            return _Resp(url, self.csv, "text/csv")
        if url.endswith(".xlsx") and not url.endswith("quebrado.xlsx"):
            return _Resp(url, self.xlsx, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        return _Resp(url, b"<!DOCTYPE html><html>erro</html>", "text/html")


SERIES = {
    4607: [{"data": f"01/{m:02d}/{a}", "valor": f"{(a - 2000) * 1000 + m * 10}.50"} for a in (2023, 2024) for m in range(1, 13)] + [{"data": "01/01/2025", "valor": "99.00"}],
    4469: [{"data": f"01/{m:02d}/2024", "valor": f"{7000000 + m}.00"} for m in range(1, 13)],
    13762: [{"data": f"01/{m:02d}/2024", "valor": f"{76 + m / 10:.2f}"} for m in range(1, 9)],  # ano incompleto
}


class FakeBcb:
    def __init__(self):
        self.calls: list[str] = []
        self.meta_ok = True

    def get(self, url: str, params: dict | None = None):
        self.calls.append(url)
        if url.endswith("/package_search"):
            cod = int(params["q"])
            if not self.meta_ok:
                raise RuntimeError("portal fora do ar")
            res = {"success": True, "result": {"results": [{"id": "x", "name": f"{cod}-nome-no-portal", "title": f"Título oficial da série {cod}", "notes": "descrição"}]}}
            return _Resp(url, json.dumps(res).encode("utf-8"), "application/json")
        cod = int(url.rsplit("bcdata.sgs.", 1)[1].split("/")[0])
        pontos = SERIES.get(cod, [])
        return _Resp(url, json.dumps(pontos).encode("utf-8"), "application/json")


@pytest.fixture
def store_div(tmp_path):
    return Store(LocalBackend(tmp_path / "data"))


def cfg_s1():
    cfg = source_config(CONFIG_DIR, "S1")
    return {**cfg, "datasets": {k: v for k, v in cfg["datasets"].items() if k in ("estoque_dpf", "fatores_variacao")}}


def cfg_s16():
    cfg = source_config(CONFIG_DIR, "S16")
    return {**cfg, "series": [s for s in cfg["series"] if int(s["codigo"]) in SERIES]}


def test_divida_fetch_lacra_e_pula_versao_igual(store_div):
    fake = FakeTesouro()
    cfg = cfg_s1()
    res = run_fetch_divida(fake, store_div, cfg)
    assert [(r.dataset, r.key, r.status, r.changed) for r in res] == [("estoque_dpf", "estoque-dpf.csv", "ok", True), ("estoque_dpf", "estoque-dpf.xlsx", "ok", True), ("fatores_variacao", "fatores-variacao.xlsx", "ok", True)]
    # CSV e XLSX do mesmo dataset costumam ter o mesmo nome: a chave leva a extensão para não se atropelarem
    assert len(fake.downloads) == 3 and store_div.get_latest(DIV_SOURCE, "estoque_dpf", KEY_PACKAGE) is not None
    raw, manifest = store_div.get_latest(DIV_SOURCE, "fatores_variacao", "fatores-variacao.xlsx")
    assert raw == fake.xlsx and manifest["recurso"]["formato"] == "XLSX" and manifest["filename"] == "fatores-variacao.xlsx"
    assert store_div.get_latest(DIV_SOURCE, "estoque_dpf", "estoque-dpf.csv")[0] == fake.csv
    idx = store_div.read_index(DIV_SOURCE, "fatores_variacao")
    assert idx["fatores-variacao.xlsx"]["status"] == "ok" and idx["fatores-variacao.xlsx"]["versao_recurso"].startswith("2026-10-29T10:00:00|")
    # segunda rodada: nada baixado; --force baixa e, conteúdo igual, não cria snapshot
    res2 = run_fetch_divida(fake, store_div, cfg, datasets=["fatores_variacao"])
    assert [(r.status, r.changed) for r in res2] == [("pulado", False)] and len(fake.downloads) == 3
    res3 = run_fetch_divida(fake, store_div, cfg, datasets=["fatores_variacao"], force=True)
    assert [(r.status, r.changed) for r in res3] == [("ok", False)] and len(fake.downloads) == 4
    # recurso que devolve HTML (página de erro) não é gravado
    fake.packages["fc2ea6ab-2130-4525-98f6-8bf04407f7fe"]["resources"][0]["url"] = "https://x/quebrado.xlsx"
    fake.packages["fc2ea6ab-2130-4525-98f6-8bf04407f7fe"]["resources"][0]["last_modified"] = "2026-11-01T00:00:00"
    res4 = run_fetch_divida(fake, store_div, cfg, datasets=["fatores_variacao"])
    assert res4[0].status == "erro" and store_div.read_index(DIV_SOURCE, "fatores_variacao")["quebrado.xlsx"]["status"] == "erro"
    with pytest.raises(KeyError):
        run_fetch_divida(fake, store_div, cfg, datasets=["nao_existe"])
    recs = recursos_do_dataset({"result": fake.packages["0998f610-bc25-4ce3-b32c-a873447500c2"]}, "estoque_dpf", ["CSV"])
    assert [r.chave for r in recs] == ["estoque-dpf.csv"] and recs[0].arquivo == "estoque-dpf.csv"


def test_divida_leitores_genericos():
    df = ler_csv_generico(CSV_ESTOQUE)
    assert df.columns[:5] == ["Mês", "Tipo de Dívida", "Indexador", "Valor (R$ milhões)", "Observação"]
    assert "Mês__data" in df.columns and "Valor (R$ milhões)__num" in df.columns and "Indexador__num" not in df.columns
    assert df["Mês__data"].to_list()[0] == date(2024, 1, 1) and df["Mês__data"].to_list()[-1] == date(2023, 12, 1)
    assert df["Valor (R$ milhões)__num"].to_list() == [1_234_567.89, 987_654.32, 1_300_000.0, -10.5, 1_000_000.0]
    g = ler_xlsx_grade(xlsx_fatores())
    assert g["planilha"].unique(maintain_order=True).to_list() == ["Fatores", "Notas"]
    assert g.filter((pl.col("planilha") == "Fatores") & (pl.col("linha") == 3))["valor_texto"].to_list() == ["Mês", "Emissões", "Resgates", "Juros apropriados"]
    l4 = g.filter((pl.col("planilha") == "Fatores") & (pl.col("linha") == 4)).sort("coluna")
    assert l4["valor_texto"].to_list() == ["2024-01-01", "100.5", "80", "60.25"] and l4["eh_data"].to_list() == [True, False, False, False]
    assert l4["valor_num"].to_list() == [None, 100.5, 80.0, 60.25]


def test_divida_transform_e_inspect(store_div, tmp_path):
    fake = FakeTesouro()
    cfg = cfg_s1()
    run_fetch_divida(fake, store_div, cfg)
    path = build_staging_divida(store_div, tmp_path / "staging2", cfg)
    idx2 = pl.read_parquet(path).sort("chave")
    assert path.name == INDICE_FILE
    assert idx2["chave"].to_list() == ["estoque-dpf.csv", "estoque-dpf.xlsx", "fatores-variacao.xlsx"] and idx2["tipo"].to_list() == ["csv", "xlsx_grade", "xlsx_grade"]
    csv_row = idx2.filter(pl.col("chave") == "estoque-dpf.csv").row(0, named=True)
    assert csv_row["linhas"] == 5 and csv_row["colunas"] == 5 and csv_row["sha256_canonical"] and csv_row["snapshot_id"]
    assert csv_row["staging"] == "stg_divida__estoque_dpf__estoque-dpf_csv.parquet"
    stg = pl.read_parquet(tmp_path / "staging2" / csv_row["staging"])
    assert {"_dataset", "_chave", "_snapshot_id", "Mês__data", "Valor (R$ milhões)__num"} <= set(stg.columns)
    txt = inspect_divida(tmp_path / "staging2", rows=10)
    assert "arquivos no staging da dívida: 3" in txt and "estoque_dpf / estoque-dpf.csv | estoque-dpf.csv | CSV" in txt
    assert "'Tipo de Dívida': texto, 2 distintos, 0 vazias: ['DPFe', 'DPMFi']" in txt
    assert "'Mês': data, 3 distintos, 0 vazias, de 2023-12-01 a 2024-02-01" in txt
    assert "'Valor (R$ milhões)': número, 5 distintos, 0 vazias, soma 4,522,211.71" in txt
    assert "soma por ano (pela coluna de data 'Mês'" in txt and "2024" in txt and "3,522,211.71" in txt
    assert "planilha 'Fatores': 5 linhas x 4 colunas (13 células preenchidas)" in txt and "[1] 2024-01-01 | [2] 100.5" in txt
    txt_f = inspect_divida(tmp_path / "staging2", dataset="fatores_variacao")
    assert "filtrando dataset 'fatores_variacao'" in txt_f and "estoque-dpf.csv" not in txt_f
    assert "somas das colunas numéricas por valor distinto" in txt and "    DPFe                                              -10.50        1" in txt
    txt_fl = inspect_divida(tmp_path / "staging2", dataset="estoque_dpf", filtros=["Tipo de Dívida=DPFe"])
    assert "filtro 'Tipo de Dívida' = 'DPFe': 1 de 5 linhas" in txt_fl and "'Indexador': texto, 1 distintos, 0 vazias: ['Câmbio']" in txt_fl
    assert "nenhum arquivo do dataset 'x'" in inspect_divida(tmp_path / "staging2", dataset="x")
    assert "staging da dívida não existe" in inspect_divida(tmp_path / "vazio")


def xlsx_anexo_tesouro() -> bytes:
    """Layout dos anexos do Tesouro: título, cabeçalho de meses Mmm/aa com coluna anual aaaa, rótulos na coluna 1."""
    wb = Workbook()
    ws = wb.active
    ws.title = "2.9"
    ws.append(["ANEXO 2.9 - FATORES DE VARIAÇÃO DA DPF"])
    ws.append([])
    meses = ["Jan", "Fev", "Mar", "Abr", "Mai", "Jun", "Jul", "Ago", "Set", "Out", "Nov", "Dez"]
    ws.append(["Indicadores"] + [f"{m}/23" for m in meses] + ["2023"] + [f"{m}/24" for m in meses] + ["2024", "Jan/25"])
    ws.append(["Estoque anterior"] + [1000 + i for i in range(13)] + [2000 + i for i in range(13)] + [3000])
    ws.append(["I.2 - Juros Apropriados"] + [10.5] * 12 + [126.0] + [20.25] * 12 + [243.0] + [30])
    ws.append(["Nota: texto sem números"])
    b = io.BytesIO()
    wb.save(b)
    return b.getvalue()


def test_divida_inspect_ano_e_filtro(tmp_path):
    from recibo.inspect_divida import _grade_ano, aplicar_filtros, linha_cabecalho

    g = ler_xlsx_grade(xlsx_anexo_tesouro())
    assert linha_cabecalho(g) == 3
    txt = "\n".join(_grade_ano(g, "2.9", 2024))
    assert "colunas de 2024 (cabeçalho na linha 3" in txt
    cab = [ln for ln in txt.splitlines() if ln.strip().startswith("linha")][0]
    assert cab.split()[2:] == ["Jan/24", "Fev/24", "Mar/24", "Abr/24", "Mai/24", "Jun/24", "Jul/24", "Ago/24", "Set/24", "Out/24", "Nov/24", "Dez/24", "2024"]
    juros = [ln for ln in txt.splitlines() if "Juros Apropriados" in ln][0]
    assert juros.rstrip().endswith("243.00") and juros.count("20.25") == 12
    assert "Nota: texto sem números" in txt  # linha sem valores nas colunas do ano ainda aparece, pelo rótulo
    assert "nenhuma coluna de 2030 no cabeçalho" in "\n".join(_grade_ano(g, "2.9", 2030)) and "['2023', '2024']" in "\n".join(_grade_ano(g, "2.9", 2030))
    assert "não achei a linha de cabeçalho" in "\n".join(_grade_ano(ler_xlsx_grade(xlsx_fatores()), "Fatores", 2024))
    df = ler_csv_generico(CSV_ESTOQUE)
    f, msgs = aplicar_filtros(df, ["Tipo de Dívida=DPMFi", "Mês=01/2024"])
    assert f.height == 2 and msgs == ["filtro 'Tipo de Dívida' = 'DPMFi': 4 de 5 linhas", "filtro 'Mês' = '01/2024': 2 de 4 linhas"]
    f2, msgs2 = aplicar_filtros(df, ["Nada=1", "semigual"])
    assert f2.height == 5 and msgs2[0].startswith("filtro ignorado: coluna 'Nada' não existe") and msgs2[1].startswith("filtro ignorado (use COLUNA=VALOR)")


def test_bcb_fetch_canonico_e_ficha(store_div):
    fake = FakeBcb()
    cfg = cfg_s16()
    res = run_fetch_bcb(fake, store_div, cfg, hoje=date(2026, 11, 9))
    assert [(r.codigo, r.status, r.n_pontos, r.changed) for r in res] == [(4469, "ok", 12, True), (4607, "ok", 25, True), (13762, "ok", 8, True)]
    assert res[1].primeira == "2023-01-01" and res[1].ultima == "2025-01-01"
    raw, manifest = store_div.get_latest(BCB_SOURCE, BCB_DATASET, "4607")
    assert json.loads(raw) == SERIES[4607] and manifest["request"]["params"]["dataFinal"] == "09/11/2026" and manifest["request"]["params"]["dataInicial"] == cfg["desde"]
    meta, _ = store_div.get_latest(BCB_SOURCE, DATASET_META, "4607")
    assert json.loads(meta)["title"] == "Título oficial da série 4607"
    idx = store_div.read_index(BCB_SOURCE, BCB_DATASET)
    assert idx["4607"]["nome"].startswith("juros nominais, Governo Federal") and idx["4607"]["ficha_dados_abertos"] is True and idx["13762"]["tipo"] == "taxa"
    # mesmos pontos em outra ordem e outra data final: hash canônico igual, nenhum snapshot novo
    SERIES[4607].reverse()
    try:
        res2 = run_fetch_bcb(fake, store_div, cfg, codigos=[4607], hoje=date(2026, 11, 10))
        assert res2[0].status == "ok" and res2[0].changed is False
    finally:
        SERIES[4607].reverse()
    assert canonical_serie([{"data": "01/02/2024", "valor": "2"}, {"data": "01/01/2024", "valor": "1"}]) == canonical_serie([{"data": "01/01/2024", "valor": "1"}, {"data": "01/02/2024", "valor": "2"}])
    assert escolher_package({"result": {"results": [{"name": "46070-outra"}, {"name": "4607-certa"}]}}, 4607)["name"] == "4607-certa"
    # série fora de sources.yaml e vazia: "vazio", sem gravar; portal de fichas fora do ar não derruba a série
    fake.meta_ok = False
    res3 = run_fetch_bcb(fake, store_div, cfg, codigos=[9999, 4469])
    assert [(r.codigo, r.status) for r in res3] == [(9999, "vazio"), (4469, "ok")]
    assert store_div.get_latest(BCB_SOURCE, BCB_DATASET, "9999") is None


def test_bcb_transform_e_inspect(store_div, tmp_path):
    fake = FakeBcb()
    cfg = cfg_s16()
    run_fetch_bcb(fake, store_div, cfg, hoje=date(2026, 11, 9))
    path = build_staging_bcb(store_div, tmp_path / "staging", cfg)
    df = pl.read_parquet(path)
    assert sorted(df["codigo"].unique().to_list()) == [4469, 4607, 13762] and df.height == 45
    s = df.filter(pl.col("codigo") == 4607).sort("data")
    assert s["data"][0] == date(2023, 1, 1) and s["valor"][0] == 23010.5 and s["tipo"][0] == "fluxo" and s["unidade"][0] == "R$ milhões"
    assert s["titulo_bc"][0] == "Título oficial da série 4607" and s["primeira_data"][0] == date(2023, 1, 1) and s["ultima_data"][0] == date(2025, 1, 1)
    # agregados: fluxo soma os meses; saldo e taxa pegam o último valor do ano
    a = agregado_anual(s, "fluxo")
    assert a.filter(pl.col("ano") == 2024).row(0, named=True) == {"ano": 2024, "valor": pytest.approx(12 * 24000 + 10 * 78 + 12 * 0.5), "meses": 12, "ultimo_mes": 12}
    b = agregado_anual(df.filter(pl.col("codigo") == 4469), "saldo")
    assert b.row(0, named=True) == {"ano": 2024, "valor": 7000012.0, "meses": 12, "ultimo_mes": 12}
    txt = inspect_bcb(path, ano=2024)
    assert "séries no staging do BCB: 3; 45 pontos" in txt and "agregado de 2024 por série" in txt
    assert "4607   fluxo  R$ milhões              288,786.00     12  juros nominais, Governo Federal (fluxo mensal)" in txt
    assert "4469   saldo  R$ milhões            7,000,012.00     12  dívida líquida do setor público, Governo Federal (saldo)" in txt
    assert "13762  taxa   % do PIB                     76.80      8  dívida bruta do governo geral (% PIB, metodologia de 2008) (até o mês 8)" in txt
    txt_s = inspect_bcb(path, serie=4607, rows=3)
    assert "SGS 4607 | juros nominais, Governo Federal (fluxo mensal) | tipo fluxo | unidade R$ milhões" in txt_s and "nome no BC: Título oficial da série 4607" in txt_s
    assert "por ano: 2023: 276,786.00; 2024: 288,786.00; 2025: 99.00 (1 meses)" in txt_s and "2025-01-01                 99.00" in txt_s
    assert "série 1 não está no staging" in inspect_bcb(path, serie=1)
    # valor não numérico ou data fora do padrão param o transform
    with pytest.raises(CofogFormato) as e:
        pontos_para_df(1, [{"data": "2024-01-01", "valor": "1"}, {"data": "01/01/2024", "valor": "abc"}])
    assert "1 datas fora de dd/mm/aaaa" in str(e.value) and "1 valores não numéricos" in str(e.value)
    assert pontos_para_df(1, [{"data": "01/01/2024", "valor": "1,5"}])["valor"][0] == 1.5  # vírgula decimal também é aceita
