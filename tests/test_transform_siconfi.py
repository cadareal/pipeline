import polars as pl

from recibo.transform.siconfi import STAGING_FILE, build_staging, rows_for_key


def test_derive_hierarchy_and_scope(populated_store, config_dir):
    rows = rows_for_key(populated_store, config_dir, "2024/06/RREO-Anexo 02")
    liq = [r for r in rows if "LIQUIDADAS" in r["coluna_norm"]]

    by = {(r["conta_norm"], r["escopo"], r["ordem"]): r for r in liq}

    header = next(r for r in liq if r["is_header"] and r["escopo"] == "exceto_intra")
    assert header["nivel"] == "total"
    assert header["valor"] == 4110.0

    saude = next(r for r in liq if r["conta_norm"] == "SAUDE" and r["escopo"] == "exceto_intra")
    assert saude["nivel"] == "funcao" and saude["funcao_cod"] == "10"

    atencao = next(r for r in liq if r["conta_norm"] == "ATENCAO BASICA")
    assert atencao["nivel"] == "subfuncao"
    assert atencao["funcao_cod"] == "10"
    assert atencao["subfuncao_cod"] == "301"

    # a mesma subfunção 272 aparece sob Defesa (05) e sob Previdência (09)
    estat = [r for r in liq if r["subfuncao_cod"] == "272" and r["escopo"] == "exceto_intra"]
    assert sorted(r["funcao_cod"] for r in estat) == ["05", "09"]

    # seção intraorçamentária marcada como tal
    intra = [r for r in liq if r["escopo"] == "intra" and r["nivel"] == "subfuncao"]
    assert len(intra) == 1 and intra[0]["valor"] == 25.0

    refin = next(r for r in liq if r["conta_norm"] == "REFINANCIAMENTO DA DIVIDA INTERNA")
    assert refin["subfuncao_cod"] == "841" and refin["funcao_cod"] == "28"


def test_anexo01_rows_are_not_hierarchized(populated_store, config_dir):
    rows = rows_for_key(populated_store, config_dir, "2024/06/RREO-Anexo 01")
    juros = next(r for r in rows if r["conta_norm"].startswith("JUROS") and "LIQUIDADAS" in r["coluna_norm"])
    assert juros["nivel"] == "conta"
    assert juros["escopo"] == "exceto_intra"
    assert juros["valor"] == 350.0


def test_build_staging_writes_parquet(populated_store, config_dir, tmp_path):
    path = build_staging(populated_store, config_dir, tmp_path / "staging")
    assert path.name == STAGING_FILE
    df = pl.read_parquet(path)
    assert df.height == 2 * (33 + 15)  # duas colunas x (33 linhas do anexo 02 + 15 do anexo 01)
    assert df.schema["valor"] == pl.Float64
    assert set(df["anexo_norm"].unique().to_list()) == {"RREO-ANEXO 01", "RREO-ANEXO 02"}
