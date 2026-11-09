import polars as pl

from recibo.transform.siconfi import STAGING_FILE, SUBFUNCAO_DEMAIS, build_staging, derive, load_ref, rows_for_key

from conftest import N_ROWS_STAGING, TOTAL_I, _row


def _liq(rows):
    return [r for r in rows if "LIQUIDADAS" in r["coluna_norm"]]


def test_header_and_scope_come_from_rotulo(populated_store, config_dir):
    liq = _liq(rows_for_key(populated_store, config_dir, "2024/06/RREO-Anexo 02"))

    # três cabeçalhos: (I) no bloco I, a linha-resumo (II) ainda no bloco I, e o cabeçalho do bloco II
    headers = [r for r in liq if r["is_header"]]
    assert [(h["escopo"], h["header_escopo"]) for h in headers] == [
        ("exceto_intra", "exceto_intra"),
        ("exceto_intra", "intra"),
        ("intra", "intra"),
    ]
    assert headers[0]["nivel"] == "total" and headers[0]["valor"] == TOTAL_I
    assert headers[1]["valor"] == 25.0  # a linha-resumo (II) dentro do bloco I: não é o cabeçalho de (I)

    # o rotulo repete o bloco em toda linha: nenhuma linha de função/subfunção vira cabeçalho
    assert all(not r["is_header"] for r in liq if r["nivel"] in ("funcao", "subfuncao"))
    assert {r["escopo"] for r in liq if r["nivel"] in ("funcao", "subfuncao")} == {"exceto_intra", "intra"}

    total_iii = next(r for r in liq if r["conta_norm"].startswith("TOTAL (III)"))
    assert total_iii["nivel"] == "total" and total_iii["escopo"] == "exceto_intra" and not total_iii["is_header"]


def test_hierarchy_by_name_and_fu_prefix(populated_store, config_dir):
    liq = _liq(rows_for_key(populated_store, config_dir, "2024/06/RREO-Anexo 02"))

    saude = next(r for r in liq if r["conta_norm"] == "SAUDE" and r["escopo"] == "exceto_intra")
    assert saude["nivel"] == "funcao" and saude["funcao_cod"] == "10"

    atencao = next(r for r in liq if r["conta_norm"] == "ATENCAO BASICA")
    assert (atencao["nivel"], atencao["funcao_cod"], atencao["subfuncao_cod"]) == ("subfuncao", "10", "301")

    adm05 = next(r for r in liq if r["conta_norm"] == "FU05 - ADMINISTRACAO GERAL")
    assert (adm05["nivel"], adm05["funcao_cod"], adm05["subfuncao_cod"], adm05["funcao_cod_prefixo"]) == ("subfuncao", "05", "122", "05")
    assert adm05["fu_inconsistente"] is False

    demais12 = next(r for r in liq if r["conta_norm"] == "FU12 - DEMAIS SUBFUNCOES")
    assert (demais12["funcao_cod"], demais12["subfuncao_cod"]) == ("12", SUBFUNCAO_DEMAIS)

    # 272 explícita só sob Previdência Social (09), nas duas seções
    estat = [r for r in liq if r["subfuncao_cod"] == "272"]
    assert {r["funcao_cod"] for r in estat} == {"09"}
    assert sorted(r["escopo"] for r in estat) == ["exceto_intra", "intra"]

    servico = next(r for r in liq if r["conta_norm"] == "SERVICO DA DIVIDA INTERNA")
    assert (servico["funcao_cod"], servico["subfuncao_cod"]) == ("28", "843")

    # a União chama a 845 de "Transferências" (não "Outras Transferências")
    transf = next(r for r in liq if r["conta_norm"] == "TRANSFERENCIAS")
    assert (transf["funcao_cod"], transf["subfuncao_cod"]) == ("28", "845")

    assert not any(r["nivel"] == "subfuncao" and not r["funcao_cod"] for r in liq)
    assert not any(r["nivel"] == "subfuncao" and not r["subfuncao_cod"] for r in liq)


def test_fu_prefix_disagreeing_with_order_is_flagged(config_dir):
    funcoes, subfuncoes = load_ref(config_dir)
    manifest = {"key": "k", "snapshot_id": "s", "fetched_at": "t", "sha256_canonical": "h"}
    rows = [
        _row(2024, 6, "RREO-Anexo 02", "X", "Saúde", 10.0, rotulo="DESPESAS (EXCETO INTRA-ORÇAMENTÁRIAS) (I)"),
        _row(2024, 6, "RREO-Anexo 02", "X", "FU12 - Administração Geral", 10.0, rotulo="DESPESAS (EXCETO INTRA-ORÇAMENTÁRIAS) (I)"),
    ]
    for r in rows:
        r.update(raw_key="k", snapshot_id="s", fetched_at="t", sha256_canonical="h")
    out = derive(rows, funcoes, subfuncoes)
    assert out[1]["fu_inconsistente"] is True
    assert out[1]["funcao_cod"] == "12"  # o prefixo manda; a reconciliação acusa


def test_anexo01_rows_keep_cod_conta_and_are_not_hierarchized(populated_store, config_dir):
    rows = _liq(rows_for_key(populated_store, config_dir, "2024/06/RREO-Anexo 01"))
    juros = next(r for r in rows if r["cod_conta"] == "JurosEEncargosDaDivida")
    assert juros["nivel"] == "conta"
    assert juros["valor"] == 350.0
    intra = next(r for r in rows if r["cod_conta"] == "PessoalEEncargosSociaisIntra")
    assert intra["nivel"] == "conta"
    header = next(r for r in rows if r["cod_conta"] == "DespesasExcetoIntraOrcamentarias")
    assert header["is_header"] and header["nivel"] == "total" and header["escopo"] == "exceto_intra"


def test_build_staging_writes_parquet(populated_store, config_dir, tmp_path):
    path = build_staging(populated_store, config_dir, tmp_path / "staging")
    assert path.name == STAGING_FILE
    df = pl.read_parquet(path)
    assert df.height == N_ROWS_STAGING
    assert df.schema["valor"] == pl.Float64
    assert set(df["anexo_norm"].unique().to_list()) == {"RREO-ANEXO 01", "RREO-ANEXO 02"}
    assert "fu_inconsistente" in df.columns and "rotulo_norm" in df.columns
