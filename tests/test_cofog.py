"""S7 COFOG: fetch pelo CKAN (lista de recursos + CSV por ano), transform (cabeçalho, números brasileiros, derivações,
guarda de escala) e inspect. Tudo sobre um CSV sintético no formato dos metadados do Tesouro."""

import json

import polars as pl
import pytest

from recibo.config import source_config
from recibo.fetchers.cofog import DATASET, KEY_PACKAGE, SOURCE, recursos_por_ano, run_fetch
from recibo.inspect_cofog import inspect_cofog
from recibo.store import LocalBackend, Store
from recibo.transform.cofog import CofogFormato, build_staging, derivar, ler_csv, mapear_colunas

from conftest import CONFIG_DIR

# Cabeçalho como o CSV real do Tesouro traz (lido em 9/nov/2026): maiúsculas sem acento, "ANO", "AJUSTE METODOLOGICO"
# no singular, códigos sem " - Código". O PDF de metadados descreve outros nomes (teste separado).
CABECALHO = (
    "ANO;AJUSTE METODOLOGICO;NATUREZA DESPESA DETALHADA;NATUREZA DESPESA DETALHADA DESCRICAO;ACAO GOVERNO;ACAO GOVERNO DESCRICAO;"
    "UNIDADE ORCAMENTARIA;UNIDADE ORCAMENTARIA DESCRICAO;CLASSIFICACAO ECONOMICA GFS;CLASSIFICACAO ECONOMICA GFS DESCRICAO;"
    "CLASSIFICACAO COFOG;CLASSIFICACAO COFOG DESCRICAO;VALOR (R$)"
)
CABECALHO_METADADOS = (
    "Ano de Lançamento;Ajustes Metodológicos;Natureza Despesa Detalhada;Natureza Despesa Detalhada - Descrição;"
    "Ação Governo - Código;Ação Governo - Descrição;Unidade Orçamentária - Código;Unidade Orçamentária - Descrição;"
    "Classificação Econômica GFS - Código;Classificação Econômica GFS - Descrição;Classificação COFOG;Classificação COFOG - Descrição;Valor (R$)"
)
# Linhas no formato real: "NA" na coluna de ajuste quando não há ajuste; natureza com 8 dígitos sem pontos; código COFOG
# no padrão do GFSM com o 7 na frente (7102 = proteção social, velhice; 70911 = educação pré-primária, classe); GFS com 3
# ou 4 dígitos (273, 2631); vírgula decimal, com e sem ponto de milhar; latin-1. Como no arquivo real, 2024 está em
# R$ milhões com duas casas (sources.yaml declara `2024: milhoes`) e 2023 em reais com centavos.
LINHAS_2024 = [
    "2024;NA;31900111;VENCIMENTOS E SALARIOS;20TP;ATIVOS CIVIS DA UNIAO;26298;FNDE;211;GASTO - REMUNERACAO DE EMPREGADOS - SALARIOS;70911;EDUCACAO - PRE-PRIMARIA;1.000,00",
    "2024;NA;33903901;SERVICOS DE TERCEIROS;8585;ATENCAO ESPECIALIZADA;36901;FUNDO NACIONAL DE SAUDE;22;GASTO - USO DE BENS E SERVICOS;7073;SAUDE - SERVICOS HOSPITALARES;0,25",
    "2024;JUROS;32902101;JUROS DA DIVIDA MOBILIARIA;0455;JUROS;71101;ENCARGOS FINANCEIROS;24;GASTO - JUROS;7017;SERVICOS PUBLICOS GERAIS - TRANSACOES DA DIVIDA PUBLICA;500,00",
    "2024;NA;44905101;OBRAS E INSTALACOES;1234;CONSTRUCAO;26298;FNDE;311;AQUISICAO LIQUIDA DE ATIVOS NAO FINANCEIROS - ATIVOS FIXOS;7092;EDUCACAO - SECUNDARIA;-0,01",
    "2024;NA;31900101;PROVENTOS - PESSOAL CIVIL;0E81;BENEFICIOS RGPS;33904;INSS;271;GASTO - BENEFICIOS PREVIDENCIARIOS E ASSISTENCIAIS - PREVIDENCIA SOCIAL;7102;PROTECAO SOCIAL - TERCEIRA IDADE;3.000,00",
    "2024;NA;33904101;CONTRIBUICOES;00SJ;BENEFICIOS PREVIDENCIARIOS;33904;INSS;271;GASTO - BENEFICIOS PREVIDENCIARIOS E ASSISTENCIAIS - PREVIDENCIA SOCIAL;7102;PROTECAO SOCIAL - TERCEIRA IDADE;600.000,00",
]
TOTAL_2024_FONTE = 604_500.24  # em milhões, como no arquivo
TOTAL_2024_REAIS = 604_500_240_000.00
LINHAS_2023 = [
    "2023;NA;31900111;VENCIMENTOS E SALARIOS;20TP;ATIVOS CIVIS DA UNIAO;26298;FNDE;211;GASTO - REMUNERACAO DE EMPREGADOS - SALARIOS;70911;EDUCACAO - PRE-PRIMARIA;900.000.000,00",
    "2023;NA;31900101;PROVENTOS - PESSOAL CIVIL;0E81;BENEFICIOS RGPS;33904;INSS;271;GASTO - BENEFICIOS PREVIDENCIARIOS E ASSISTENCIAIS - PREVIDENCIA SOCIAL;7102;PROTECAO SOCIAL - TERCEIRA IDADE;2.700.000.000,00",
]


def csv_bytes(linhas: list[str], encoding: str = "latin-1") -> bytes:
    return ("\n".join([CABECALHO] + linhas) + "\n").encode(encoding)


def package(resources: list[dict]) -> dict:
    return {"success": True, "result": {"id": "22d13d17", "name": "despesas-por-funcao-do-governo-central-classificacao-cofog", "resources": resources, "tracking_summary": {"total": 123}}}


class _Resp:
    def __init__(self, url, content: bytes, ctype: str):
        self.url = url
        self.status_code = 200
        self.headers = {"content-type": ctype}
        self.content = content
        self.text = content.decode("latin-1")

    def json(self):
        return json.loads(self.content.decode("utf-8"))


class FakeCkan:
    """CKAN com um PDF de metadados, dois CSVs (2024 e 2023) e um recurso de 2023 duplicado mais antigo."""

    def __init__(self):
        self.csv = {2024: csv_bytes(LINHAS_2024), 2023: csv_bytes(LINHAS_2023)}
        self.resources = [
            {"id": "pdf1", "name": "Metadados das Bases - Despesa Por Função COFOG GC", "format": "PDF", "url": "https://x/meta.pdf", "created": "2019-01-11", "last_modified": "2024-05-10", "size": "181134"},
            {"id": "r2024", "name": "Base COFOG 2024", "format": "CSV", "url": "https://x/dados-abertos-cofog-gc-2024.csv", "created": "2025-12-12T15:14:04", "last_modified": "2025-12-18T18:39:13", "size": str(len(self.csv[2024]))},
            {"id": "r2023", "name": "Base COFOG 2023", "format": "CSV", "url": "https://x/dados-abertos-cofog-gc-2023.csv", "created": "2025-03-10T09:31:33", "last_modified": None, "size": str(len(self.csv[2023]))},
            {"id": "r2023old", "name": "Base COFOG 2023", "format": "CSV", "url": "https://x/old-2023.csv", "created": "2024-06-01T00:00:00", "last_modified": None, "size": "10"},
        ]
        self.downloads: list[str] = []

    def get(self, url: str, params: dict | None = None):
        if url.endswith("/package_show"):
            assert params == {"id": "22d13d17-bf69-4a1a-add2-25cc1e25f2d7"}
            return _Resp(url, json.dumps(package(self.resources), ensure_ascii=False).encode("utf-8"), "application/json")
        self.downloads.append(url)
        ano = int(url.rsplit("-", 1)[-1].split(".")[0])
        return _Resp(url, self.csv[ano], "text/csv")


@pytest.fixture
def ckan_store(tmp_path):
    return FakeCkan(), Store(LocalBackend(tmp_path / "data")), source_config(CONFIG_DIR, "S7")


def test_recursos_por_ano_picks_csvs_and_latest_duplicate():
    fake = FakeCkan()
    recs = recursos_por_ano(package(fake.resources))
    assert sorted(recs) == [2023, 2024]
    assert recs[2023].id == "r2023" and recs[2024].size == len(fake.csv[2024])
    assert recs[2024].versao == f"2025-12-18T18:39:13|{len(fake.csv[2024])}"
    assert recs[2023].versao.startswith("2025-03-10T09:31:33|")  # sem last_modified, vale a data de criação


def test_fetch_downloads_once_and_skips_unchanged(ckan_store):
    fake, store, cfg = ckan_store
    res = run_fetch(fake, store, cfg)  # sem --exercicios: todos os anos do CKAN
    assert [(r.key, r.status, r.changed) for r in res] == [("2023", "ok", True), ("2024", "ok", True)]
    assert len(fake.downloads) == 2 and store.get_latest(SOURCE, DATASET, KEY_PACKAGE) is not None
    raw, manifest = store.get_latest(SOURCE, DATASET, "2024")
    assert raw == fake.csv[2024] and manifest["recurso"]["id"] == "r2024" and manifest["filename"] == "dados-abertos-cofog-gc-2024.csv"
    idx = store.read_index(SOURCE, DATASET)
    assert idx["2024"]["status"] == "ok" and idx["2024"]["versao_recurso"] == f"2025-12-18T18:39:13|{len(fake.csv[2024])}"

    # segunda rodada: mesma versão no CKAN, nada baixado; ano que o CKAN não tem fica "ausente"
    res2 = run_fetch(fake, store, cfg, exercicios=[2024, 2025])
    assert [(r.key, r.status) for r in res2] == [("2024", "pulado"), ("2025", "ausente")]
    assert len(fake.downloads) == 2
    assert store.read_index(SOURCE, DATASET)["2025"]["status"] == "ausente"

    # --force baixa de novo; conteúdo igual não gera snapshot novo (idempotência do store)
    res3 = run_fetch(fake, store, cfg, exercicios=[2024], force=True)
    assert res3[0].status == "ok" and res3[0].changed is False and len(fake.downloads) == 3

    # o Tesouro republica o arquivo (tamanho e data mudam, conteúdo muda): snapshot novo, revisão na fonte
    fake.csv[2024] = csv_bytes(LINHAS_2024 + ["2024;NA;33903901;SERVICOS;8585;ATENCAO;36901;FNS;22;USO;7073;HOSP;1,00"])
    fake.resources[1]["last_modified"] = "2026-01-05T00:00:00"
    fake.resources[1]["size"] = str(len(fake.csv[2024]))
    res4 = run_fetch(fake, store, cfg, exercicios=[2024])
    assert res4[0].status == "ok" and res4[0].changed is True
    assert store.get_latest(SOURCE, DATASET, "2024")[1]["previous_snapshot_id"] is not None


def test_ler_csv_brazilian_numbers_and_header_mapping():
    df = ler_csv(csv_bytes(LINHAS_2024))
    assert df.height == 6 and df["valor"].to_list() == [1_000.0, 0.25, 500.0, -0.01, 3_000.0, 600_000.0]  # como no arquivo, sem unidade
    assert df["cofog_desc"][0] == "EDUCACAO - PRE-PRIMARIA" and df["ajuste_metodologico"][0] == "NA"
    # o cabeçalho dos metadados (PDF) também é aceito, com acentos (utf-8), ponto decimal e vírgula como separador
    us = CABECALHO_METADADOS.replace(";", ",") + "\n" + '2024,,3.1.90.11.01,"Vencimentos, salários",20TP,Ativos,26298,FNDE,21,Remuneração,09.1.1,Educação,1000000000.50\n'
    df2 = ler_csv(us.encode("utf-8-sig"))
    assert df2["valor"].to_list() == [1_000_000_000.50] and df2["natureza_desc"][0] == "Vencimentos, salários"
    # as duas grafias mapeiam para as mesmas colunas internas
    from recibo.transform.cofog import COLUNAS

    assert set(mapear_colunas(CABECALHO.split(";"))) == set(mapear_colunas(CABECALHO_METADADOS.split(";"))) == set(COLUNAS)
    assert mapear_colunas(CABECALHO.split(";"))["acao_cod"] == "ACAO GOVERNO" and mapear_colunas(CABECALHO.split(";"))["acao_desc"] == "ACAO GOVERNO DESCRICAO"
    # nome mudado mas com as mesmas palavras ainda casa; coluna faltando é erro com o cabeçalho impresso
    m = mapear_colunas(["Ano de Lançamento", "Ajustes Metodológicos", "Natureza Despesa Detalhada", "Descrição Natureza Despesa Detalhada"] + CABECALHO_METADADOS.split(";")[4:])
    assert m["natureza_desc"] == "Descrição Natureza Despesa Detalhada"
    with pytest.raises(CofogFormato) as e:
        mapear_colunas(CABECALHO.split(";")[:-1])
    assert "valor ('Valor (R$)')" in str(e.value) and "Cabeçalho real" in str(e.value)
    with pytest.raises(CofogFormato):
        ler_csv(csv_bytes(["2024;NA;31900111;x;20TP;y;26298;z;21;w;7091;v;abc"]))


def test_derivar_codes():
    df = derivar(ler_csv(csv_bytes(LINHAS_2024)))
    r = df.filter(pl.col("acao_cod") == "0455").row(0, named=True)
    # 7017 no GFSM = divisão 01 (serviços públicos gerais), grupo 7 (transações da dívida pública)
    assert (r["cofog_digitos_fonte"], r["cofog_digitos"], r["cofog_divisao"], r["cofog_grupo"], r["cofog_classe"]) == ("7017", "017", "01", "017", "017")
    assert (r["categoria"], r["gnd"], r["modalidade"], r["elemento"], r["gfs2"]) == ("3", "2", "90", "21", "24")
    assert r["ajuste"] is True and r["ano"] == 2024
    e = df.filter(pl.col("acao_cod") == "20TP").row(0, named=True)
    assert (e["cofog_divisao"], e["cofog_grupo"], e["cofog_classe"], e["gfs2"]) == ("09", "091", "0911", "21")  # 70911 = classe
    assert df.filter(pl.col("acao_cod") == "0E81").row(0, named=True)["cofog_grupo"] == "102"
    assert df["ajuste"].sum() == 1  # "NA" não é ajuste
    # unidade declarada: valor vira reais, o número do arquivo fica em valor_fonte, resolução de R$ 10 mil em milhões
    from recibo.transform.cofog import aplicar_unidade, unidade_do_exercicio

    assert unidade_do_exercicio({"unidade_valor_padrao": "reais", "unidade_valor_por_exercicio": {2024: "milhoes"}}, 2024) == ("milhoes", 1e6)
    assert unidade_do_exercicio({"unidade_valor_padrao": "reais", "unidade_valor_por_exercicio": {"2024": "milhoes"}}, 2024) == ("milhoes", 1e6)
    assert unidade_do_exercicio({"unidade_valor_por_exercicio": {2024: "milhoes"}}, 2023) == ("reais", 1.0)
    assert unidade_do_exercicio(None, 2023) == ("reais", 1.0)
    with pytest.raises(CofogFormato):
        unidade_do_exercicio({"unidade_valor_padrao": "bilhoes"}, 2023)
    u = aplicar_unidade(df, "milhoes", 1e6)
    assert u["valor"].to_list() == [1_000_000_000.0, 250_000.0, 500_000_000.0, -10_000.0, 3_000_000_000.0, 600_000_000_000.0]
    assert u["valor_fonte"].to_list() == df["valor"].to_list() and u["unidade_fonte"][0] == "milhoes" and u["resolucao_reais"][0] == 10_000.0
    assert aplicar_unidade(df, "reais", 1.0)["valor"].to_list() == df["valor"].to_list()
    # código já sem o 7 (10.2, 0711) passa como está; código fora das divisões 01 a 10 é erro na guarda
    df2 = derivar(ler_csv(csv_bytes(["2024;NA;31900111;x;20TP;y;26298;z;21;w;10.2;v;1,00", "2024;NA;31900111;x;20TP;y;26298;z;21;w;0711;v;1,00"])))
    assert df2["cofog_grupo"].to_list() == ["102", "071"] and df2["cofog_classe"].to_list() == ["102", "0711"]
    from recibo.transform.cofog import conferir_escala

    df3 = derivar(ler_csv(csv_bytes(["2024;NA;31900111;x;20TP;y;26298;z;21;w;9999;v;1,00"])))
    with pytest.raises(CofogFormato) as ex:
        conferir_escala(df3, 2024, None)
    assert "fora das divisões 01 a 10" in str(ex.value)


def test_transform_and_inspect(ckan_store, tmp_path):
    fake, store, cfg = ckan_store
    run_fetch(fake, store, cfg)
    assert cfg["unidade_valor_por_exercicio"] == {2024: "milhoes"} and cfg["unidade_valor_padrao"] == "reais"  # sources.yaml real
    cfg_teste = {**cfg, "total_minimo_reais": 1, "total_maximo_reais": 1e13}  # o sintético de 2023 é pequeno
    path = build_staging(store, tmp_path / "staging", cfg_teste)
    df = pl.read_parquet(path)
    assert sorted(df["exercicio"].unique().to_list()) == [2023, 2024] and df.height == 8
    assert {"cofog_divisao", "gnd", "gfs2", "ajuste", "raw_key", "snapshot_id", "sha256_canonical", "arquivo", "recurso_data", "valor_fonte", "unidade_fonte", "fator_unidade", "resolucao_reais"} <= set(df.columns)
    d24 = df.filter(pl.col("exercicio") == 2024)
    assert d24["valor"].sum() == pytest.approx(TOTAL_2024_REAIS) and d24["valor_fonte"].sum() == pytest.approx(TOTAL_2024_FONTE)
    assert d24["unidade_fonte"].unique().to_list() == ["milhoes"] and d24["resolucao_reais"][0] == 10_000.0
    d23 = df.filter(pl.col("exercicio") == 2023)
    assert d23["unidade_fonte"].unique().to_list() == ["reais"] and d23["valor"].sum() == d23["valor_fonte"].sum() == 3_600_000_000.0
    # guarda de escala com a faixa real: 2024 (R$ 604,5 bi em reais) passa; 2023 (R$ 3,6 bi) reprova e a mensagem diz
    # qual unidade faria o total caber. Todos os anos são conferidos antes de parar.
    with pytest.raises(CofogFormato) as e:
        build_staging(store, tmp_path / "staging2", cfg)
    assert "1 ano(s) recusado(s)" in str(e.value) and "COFOG 2023: total do ano R$ 3,600,000,000.00 (unidade declarada: reais) fora da faixa" in str(e.value)
    assert "cairia na faixa lido em milhares" in str(e.value) and "unidade_valor_por_exercicio: {2023: milhares}" in str(e.value)
    assert not (tmp_path / "staging2").exists()
    # unidade errada na config (2024 como reais): a guarda pega e aponta milhoes; 2023 continua reprovando: dois anos na mensagem
    with pytest.raises(CofogFormato) as e2:
        build_staging(store, tmp_path / "staging3", {**cfg, "unidade_valor_por_exercicio": {}})
    assert "2 ano(s) recusado(s)" in str(e2.value) and "COFOG 2024: total do ano R$ 604,500.24 (unidade declarada: reais)" in str(e2.value)
    assert "unidade_valor_por_exercicio: {2024: milhoes}" in str(e2.value)
    # unidade que não existe é erro de config, não palpite
    with pytest.raises(CofogFormato) as e3:
        build_staging(store, tmp_path / "staging4", {**cfg_teste, "unidade_valor_por_exercicio": {2024: "bilhoes"}})
    assert "'bilhoes'" in str(e3.value)

    txt = inspect_cofog(path, 2024, por="divisao", config_dir=CONFIG_DIR)
    assert "COFOG 2024 | 6 linhas | total R$ 604,500,240,000.00" in txt
    assert "o arquivo está em milhoes (fator 1,000,000, declarado em sources.yaml), com resolução de R$ 10,000.00 por linha" in txt
    assert "10                 603,000,000,000.00   99.75%" in txt and "Proteção social" in txt and "Serviços públicos gerais" in txt
    assert "linhas negativas: 1, somando R$ -10,000.00" in txt and "linhas com ajuste metodológico: 1, somando R$ 500,000,000.00" in txt
    txt_g = inspect_cofog(path, 2024, por="gnd")
    assert "2                      500,000,000.00    0.08%" in txt_g and "juros e encargos da dívida" in txt_g
    txt_23 = inspect_cofog(path, 2023, por="divisao", config_dir=CONFIG_DIR)
    assert "o arquivo está em" not in txt_23 and "total R$ 3,600,000,000.00" in txt_23
    txt_c = inspect_cofog(path, 2024, por="classe", cofog="09")
    assert "só COFOG 09" in txt_c and "0911" in txt_c and "EDUCACAO - PRE-PRIMARIA" in txt_c
    txt_a = inspect_cofog(path, 2022, por="divisao", store=store)
    assert "não está no staging" in txt_a and "2023" in txt_a and "2024" in txt_a and "também não há raw" in txt_a
    # ano que o transform recusou (ou staging ausente): o inspect lê direto do raw, sem a guarda de escala, mas com a
    # unidade declarada na config (com config_dir); sem config, os números saem como estão no arquivo
    txt_r = inspect_cofog(tmp_path / "nao_existe.parquet", 2024, por="divisao", config_dir=CONFIG_DIR, store=store)
    assert "lido direto do raw" in txt_r and "Proteção social" in txt_r and "603,000,000,000.00" in txt_r and "o arquivo está em milhoes" in txt_r
    txt_r0 = inspect_cofog(tmp_path / "nao_existe.parquet", 2024, por="divisao", store=store)
    assert "total R$ 604,500.24" in txt_r0
    txt_a2 = inspect_cofog(path, 2022, por="divisao")
    assert "não está no staging" in txt_a2
    txt_anos = inspect_cofog(path, 2024, por="anos")
    assert "total por ano" in txt_anos and "unidade no arquivo" in txt_anos and "3,600,000,000.00" in txt_anos and "604,500,240,000.00" in txt_anos
    assert "milhoes" in txt_anos and "reais" in txt_anos
