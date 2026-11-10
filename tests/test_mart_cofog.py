"""Mart COFOG (v0.6.3): régua das dez divisões, ajustes do Tesouro, ponte com o recibo. O COFOG sintético é construído
para fechar exatamente com o Anexo 06 sintético de conftest (2024: despesa primária liquidada 2058, restos não
processados pagos 45, juros pagos 350; 2023 a 90%)."""

import json

import polars as pl
import pytest

from recibo.config import load_yaml, source_config
from recibo.fetchers.cofog import run_fetch
from recibo.marts.cofog import compute_cofog, print_summary_cofog, to_export_cofog, write_outputs_cofog
from recibo.marts.recibo import SemDados
from recibo.transform.cofog import build_staging as build_staging_cofog
from recibo.transform.siconfi import build_staging

from conftest import CONFIG_DIR, DESPESA_PRIMARIA_ANEXO06, ESCALA_2023, TOTAL_RECIBO
from test_cofog import CABECALHO, FakeCkan

# (ajuste, natureza, descrição, ação, UO, GFS, COFOG, descrição COFOG, valor em reais). Sem ajuste: 2053.
# Ajustes: JUROS 600 (soma), IRRF 70 + 30 (soma), FUNDEB 50 (remaneja), venda de bens -3 (soma, negativa). Total 2800.
LINHAS_BASE = [
    ("NA", "33905404", "APOSENTADORIA POR TEMPO DE CONTRIBUICAO", "00SJ", "33904", "271", "7102", "PROTECAO SOCIAL - TERCEIRA IDADE", 900.0),
    ("NA", "33905601", "PENSAO POR MORTE", "00SJ", "33904", "271", "7103", "PROTECAO SOCIAL - SOBREVIVENTES", 100.0),
    ("NA", "33904805", "PROGRAMA BOLSA FAMILIA", "00H5", "55101", "272", "7104", "PROTECAO SOCIAL - FAMILIA E FILHOS", 200.0),
    ("NA", "33903009", "MATERIAL FARMACOLOGICO", "8585", "36901", "22", "7073", "SAUDE - SERVICOS HOSPITALARES", 200.0),
    ("NA", "31901101", "VENCIMENTOS E SALARIOS", "20TP", "26298", "211", "70911", "EDUCACAO - EDUCACAO INFANTIL E ENSINO FUNDAMENTAL I", 90.0),
    ("NA", "33901001", "SEGURO DESEMPREGO", "0583", "40901", "272", "7105", "PROTECAO SOCIAL - DESEMPREGO", 90.0),
    ("NA", "31901201", "SOLDO", "2000", "52121", "211", "7021", "DEFESA - DEFESA MILITAR", 60.0),
    ("NA", "31901101", "VENCIMENTOS E SALARIOS", "2000", "30108", "211", "7031", "ORDEM PUBLICA E SEGURANCA - SERVICOS DE POLICIA", 40.0),
    ("NA", "33903039", "SERVICOS", "2000", "30108", "22", "7032", "ORDEM PUBLICA E SEGURANCA - PROTECAO CONTRA INCENDIOS", 10.0),
    ("NA", "33308135", "RIO DE JANEIRO", "0045", "73108", "2631", "7018", "SERVICOS PUBLICOS GERAIS - TRANSFERENCIAS DE CARATER GERAL", 300.0),
    ("NA", "31901101", "VENCIMENTOS E SALARIOS", "2000", "01101", "211", "7011", "SERVICOS PUBLICOS GERAIS - PODER EXECUTIVO", 28.0),
    ("NA", "33903039", "SERVICOS", "2000", "22101", "22", "7042", "ASSUNTOS ECONOMICOS - AGRICULTURA", 20.0),
    ("NA", "33903039", "SERVICOS", "2000", "44101", "22", "7054", "PROTECAO AMBIENTAL - BIODIVERSIDADE", 5.0),
    ("NA", "44905191", "OBRAS", "2000", "53101", "311", "7063", "HABITACAO E SERVICOS COMUNITARIOS - ABASTECIMENTO DE AGUA", 5.0),
    ("NA", "33903039", "SERVICOS", "2000", "54101", "22", "7082", "LAZER, CULTURA E RELIGIAO - SERVICOS CULTURAIS", 5.0),
    ("JUROS", "ND", "ND", "ND", "ND", "24", "7017", "SERVICOS PUBLICOS GERAIS - TRANSACOES DA DIVIDA PUBLICA", 600.0),
    ("TRANSFERENCIA DO IRRF DE COMPETENCIA DO GOVERNO CENTRAL ARRECADADO PELOS ESTADOS", "ND", "ND", "ND", "ND", "2631", "7018", "SERVICOS PUBLICOS GERAIS - TRANSFERENCIAS DE CARATER GERAL", 70.0),
    ("TRANSFERENCIA DO IRRF DE COMPETENCIA DO GOVERNO CENTRAL ARRECADADO PELOS MUNICIPIOS", "ND", "ND", "ND", "ND", "2631", "7018", "SERVICOS PUBLICOS GERAIS - TRANSFERENCIAS DE CARATER GERAL", 30.0),
    ("FUNDEB ESTADOS", "ND", "ND", "ND", "ND", "2631", "7091", "EDUCACAO - EDUCACAO INFANTIL E ENSINO FUNDAMENTAL I", 50.0),
    ("RECEITA DE ALIENACOES DE ATIVOS NAO FINANCEIROS", "ND", "ND", "ND", "ND", "31", "7011", "SERVICOS PUBLICOS GERAIS - PODER EXECUTIVO", -3.0),
]
TOTAL_COFOG_2024 = 2800.0
JUROS_BCB_2024 = 600.0
IRRF_2024 = 100.0
ALIENACOES_2024 = -3.0
RPNP_PAGOS_2024 = 45.0


def _csv(ano: int, escala: float, linhas=LINHAS_BASE) -> bytes:
    def br(v: float) -> str:
        return f"{v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")

    rows = [CABECALHO]
    for aj, nat, nat_d, acao, uo, gfs, cof, cof_d, v in linhas:
        rows.append(";".join([str(ano), aj, nat, nat_d, acao, f"ACAO {acao}", uo, f"UO {uo}", gfs, f"GFS {gfs}", cof, cof_d, br(round(v * escala, 2))]))
    return ("\n".join(rows) + "\n").encode("latin-1")


@pytest.fixture
def stg_cofog(populated_store, tmp_path):
    fake = FakeCkan()
    fake.csv = {2024: _csv(2024, 1.0), 2023: _csv(2023, ESCALA_2023)}
    fake.resources[1]["size"] = str(len(fake.csv[2024]))
    fake.resources[2]["size"] = str(len(fake.csv[2023]))
    cfg = source_config(CONFIG_DIR, "S7")
    run_fetch(fake, populated_store, cfg)
    # o sintético é pequeno e está em reais nos dois anos: faixa e unidade só para o teste
    return build_staging_cofog(populated_store, tmp_path / "staging", {**cfg, "total_minimo_reais": 1, "total_maximo_reais": 1e13, "unidade_valor_por_exercicio": {}})


@pytest.fixture
def stg_siconfi(populated_store, config_dir, tmp_path):
    return build_staging(populated_store, config_dir, tmp_path / "staging")


def test_regua_divisoes_grupos_e_ajustes(stg_cofog, stg_siconfi, config_dir):
    linhas = load_yaml(config_dir, "linhas.yaml")
    fontes = load_yaml(config_dir, "sources.yaml")
    res = compute_cofog(stg_cofog, linhas, fontes, 2024, stg_siconfi=stg_siconfi, periodo_recibo=6, config_dir=config_dir)
    assert res.total == TOTAL_COFOG_2024 and res.linhas_arquivo == len(LINHAS_BASE)
    div = {d.codigo: d for d in res.divisoes}
    assert [d.codigo for d in res.divisoes] == [f"{i:02d}" for i in range(1, 11)]
    assert div["10"].valor == 1290.0 and div["01"].valor == 1025.0 and div["09"].valor == 140.0 and div["07"].valor == 200.0
    assert div["10"].nome == "Proteção social" and div["01"].nome == "Serviços públicos gerais"
    assert {d.codigo: d.centavos for d in res.divisoes} == {"01": 37, "02": 2, "03": 2, "04": 1, "05": 0, "06": 0, "07": 7, "08": 0, "09": 5, "10": 46}
    assert sum(d.centavos for d in res.divisoes) == 100 and sum(d.valor for d in res.divisoes) == TOTAL_COFOG_2024
    grp = {g.codigo: g for g in res.grupos}
    assert grp["102"].valor == 900.0 and grp["102"].nome == "Velhice" and grp["102"].divisao == "10"
    assert grp["017"].valor == JUROS_BCB_2024 and grp["018"].valor == 400.0 and grp["091"].valor == 140.0  # 90 do orçamento + 50 do FUNDEB remanejado
    assert sum(g.valor for g in res.grupos) == TOTAL_COFOG_2024 and sum(g.centavos for g in res.grupos) == 100
    aj = {a.texto: a for a in res.ajustes}
    assert aj["JUROS"].papel == "soma" and aj["JUROS"].item == "juros" and aj["JUROS"].valor == JUROS_BCB_2024
    assert aj["FUNDEB ESTADOS"].papel == "remaneja" and aj["FUNDEB ESTADOS"].item is None
    assert aj["RECEITA DE ALIENACOES DE ATIVOS NAO FINANCEIROS"].papel == "soma" and aj["RECEITA DE ALIENACOES DE ATIVOS NAO FINANCEIROS"].valor == ALIENACOES_2024
    assert res.negativos == {"valor": -3.0, "linhas": 1}
    assert res.unidade["fonte"] == "reais" and res.unidade["fator"] == 1.0
    assert res.fonte["arquivo"] == "dados-abertos-cofog-gc-2024.csv" and res.fonte["sha256_canonical"] and res.fonte["snapshot_id"]


def test_ponte_fecha_exata_no_sintetico_e_publica(stg_cofog, stg_siconfi, config_dir):
    linhas = load_yaml(config_dir, "linhas.yaml")
    fontes = load_yaml(config_dir, "sources.yaml")
    res = compute_cofog(stg_cofog, linhas, fontes, 2024, stg_siconfi=stg_siconfi, periodo_recibo=6, config_dir=config_dir)
    p = res.ponte
    assert p["disponivel"] and p["recibo"]["total_recibo"] == TOTAL_RECIBO and p["recibo"]["juros_pagos"] == 350.0
    assert p["recibo"]["primaria_liquidada"] == DESPESA_PRIMARIA_ANEXO06 and p["recibo"]["rpnp_pagos"] == RPNP_PAGOS_2024
    assert [i["id"] for i in p["itens"]] == ["total_recibo", "juros_pagos", "rpnp_pagos", "juros", "irrf", "alienacoes"]
    assert [i["valor"] for i in p["itens"]] == [TOTAL_RECIBO, -350.0, RPNP_PAGOS_2024, JUROS_BCB_2024, IRRF_2024, ALIENACOES_2024]
    assert p["esperado"] == TOTAL_COFOG_2024 and p["obtido"] == TOTAL_COFOG_2024 and p["diff"] == 0.0 and p["ok"]
    assert p["remanejamentos"] == {"valor": 50.0, "linhas": 1, "itens": ["FUNDEB ESTADOS"]} and p["ajustes_esperados_ausentes"] == []
    recs = {r.id: r for r in res.reconciliacoes}
    assert recs["ponte_cofog"].ok and recs["cofog_soma_divisoes"].ok and recs["cofog_centavos"].ok
    assert res.publicavel and res.selo.startswith("ponte com o recibo 2024 fecha em +0.00%")
    # 2023: tudo a 90%, a ponte também fecha (o recibo sintético de 2023 é 90% do de 2024)
    res23 = compute_cofog(stg_cofog, linhas, fontes, 2023, stg_siconfi=stg_siconfi, periodo_recibo=6, config_dir=config_dir)
    assert res23.total == pytest.approx(TOTAL_COFOG_2024 * ESCALA_2023) and res23.ponte["ok"] and res23.publicavel
    assert abs(res23.ponte["diff"]) < 0.01


def test_ponte_fora_da_tolerancia_nao_publica_mas_nao_afeta_o_recibo(stg_cofog, stg_siconfi, config_dir, tmp_path):
    linhas = load_yaml(config_dir, "linhas.yaml")
    fontes = load_yaml(config_dir, "sources.yaml")
    # o Tesouro publica juros 100 maiores do que a ponte explica: 100 / 2900 = 3,4% > 2%
    df = pl.read_parquet(stg_cofog)
    df = df.with_columns(pl.when((pl.col("exercicio") == 2024) & (pl.col("ajuste_metodologico") == "JUROS")).then(pl.lit(700.0)).otherwise(pl.col("valor")).alias("valor"))
    alt = tmp_path / "alt.parquet"
    df.write_parquet(alt)
    res = compute_cofog(alt, linhas, fontes, 2024, stg_siconfi=stg_siconfi, periodo_recibo=6, config_dir=config_dir)
    assert res.total == 2900.0 and res.ponte["esperado"] == 2900.0  # o item "juros" da ponte vem do próprio COFOG, então...
    # ...a ponte só reprova quando a diferença está fora dos itens declarados: tira 100 do orçamento (sem ajuste)
    df2 = pl.read_parquet(stg_cofog).with_columns(pl.when((pl.col("exercicio") == 2024) & (pl.col("cofog_grupo") == "102")).then(pl.lit(800.0)).otherwise(pl.col("valor")).alias("valor"))
    alt2 = tmp_path / "alt2.parquet"
    df2.write_parquet(alt2)
    res2 = compute_cofog(alt2, linhas, fontes, 2024, stg_siconfi=stg_siconfi, periodo_recibo=6, config_dir=config_dir)
    assert res2.total == 2700.0 and res2.ponte["esperado"] == 2800.0 and res2.ponte["diff"] == -100.0
    assert not res2.ponte["ok"] and not res2.publicavel and "NÃO fecha" in res2.selo and res2.ponte["recibo"]["publicavel"] is True
    # a tolerância é config: com 5% passa
    linhas_folga = json.loads(json.dumps(linhas))
    linhas_folga["cofog"]["ponte"]["tolerancia_relativa"] = 0.05
    assert compute_cofog(alt2, linhas_folga, fontes, 2024, stg_siconfi=stg_siconfi, periodo_recibo=6, config_dir=config_dir).publicavel


def test_sem_recibo_a_ponte_fica_indisponivel_e_nao_publica(stg_cofog, stg_siconfi, config_dir):
    linhas = load_yaml(config_dir, "linhas.yaml")
    fontes = load_yaml(config_dir, "sources.yaml")
    r1 = compute_cofog(stg_cofog, linhas, fontes, 2024, stg_siconfi=None, config_dir=config_dir)
    assert not r1.ponte["disponivel"] and "staging do RREO ausente" in r1.ponte["motivo"] and not r1.publicavel and r1.selo.startswith("sem ponte")
    assert r1.total == TOTAL_COFOG_2024 and len(r1.divisoes) == 10  # a régua sai mesmo assim
    r2 = compute_cofog(stg_cofog, linhas, fontes, 2024, stg_siconfi=stg_siconfi, periodo_recibo=4, config_dir=config_dir)
    assert not r2.ponte["disponivel"] and "não está fechado" in r2.ponte["motivo"]
    r3 = compute_cofog(stg_cofog, linhas, fontes, 2024, stg_siconfi=stg_siconfi, periodo_recibo=None, config_dir=config_dir)
    assert not r3.ponte["disponivel"]
    with pytest.raises(SemDados) as e:
        compute_cofog(stg_cofog, linhas, fontes, 2025, stg_siconfi=stg_siconfi, periodo_recibo=6, config_dir=config_dir)
    assert "COFOG 2025 não está no staging" in str(e.value) and "2023 a 2024" in str(e.value)
    with pytest.raises(SemDados):
        compute_cofog(stg_cofog.parent / "nao_existe.parquet", linhas, fontes, 2024)


def test_export_escreve_arquivos_e_resumo(stg_cofog, stg_siconfi, config_dir, tmp_path, capsys):
    linhas = load_yaml(config_dir, "linhas.yaml")
    fontes = load_yaml(config_dir, "sources.yaml")
    res = compute_cofog(stg_cofog, linhas, fontes, 2024, stg_siconfi=stg_siconfi, periodo_recibo=6, config_dir=config_dir)
    export = to_export_cofog(res, linhas)
    assert export["produto"] == "cofog" and export["publicavel"] and export["versao_spec"] == linhas["versao"]
    assert len(export["divisoes"]) == 10 and len(export["grupos"]) == len({r[6][1:4] for r in LINHAS_BASE}) == 16
    assert [g["termo"] for g in export["glossario"]][:2] == ["competência e caixa", "restos a pagar"]
    assert export["ponte"]["itens"][3]["nome"].startswith("mais Juros pelo Banco Central")
    p_json, p_csv, p_exp = write_outputs_cofog(export, tmp_path / "marts", tmp_path / "export")
    assert p_json.name == "cofog_2024.json" and p_exp.name == "cofog_2024.json" and p_csv.exists()
    back = json.loads(p_exp.read_text(encoding="utf-8"))
    assert back["total"] == TOTAL_COFOG_2024 and back["fonte"]["id"] == "S7" and back["unidade"]["fonte"] == "reais"
    assert len(p_csv.read_text(encoding="utf-8").splitlines()) == 1 + 10 + 16
    print_summary_cofog(export)
    out = capsys.readouterr().out
    assert "COFOG 2024 | publicável: True" in out and "10      Proteção social" in out and "diferença não explicada" in out
    assert "soma                       600.00      1 linhas  JUROS" in out and "linhas  FUNDEB ESTADOS" in out and "[OK ] ponte_cofog" in out
    assert "-350.00  menos juros pagos em dinheiro (linha do recibo)" in out and "2,800.00  = esperado" in out
