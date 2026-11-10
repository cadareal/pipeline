"""População do ano para o per capita (recibo e mart da dívida), lida do staging do IBGE (S14).

`carregar_populacao` devolve um dicionário, nunca exceção: `disponivel` diz se o per capita pode sair, `motivo` diz por
que não. A régua é a de linhas.yaml (`populacao.regua_per_capita`, a projeção revisão 2024); a estimativa oficial do
mesmo ano, quando existe, tem que ser igual (reconciliação `populacao_projecao_vs_estimativa`); se divergir, o per
capita fica indisponível e o produto que o usa continua publicável, porque nenhum centavo dele depende da população.
"""

from __future__ import annotations

from pathlib import Path

import polars as pl

from ..transform.ibge import STAGING_FILE as IBGE_FILE


def caminho_staging(staging_dir: Path | None) -> Path | None:
    if staging_dir is None:
        return None
    p = Path(staging_dir) / IBGE_FILE
    return p if p.exists() else None


def carregar_populacao(stg_ibge: Path | None, linhas_cfg: dict, exercicio: int) -> dict:
    cfg = linhas_cfg.get("populacao") or {}
    local = str(cfg.get("local") or "Brasil")
    regua = str(cfg.get("regua_per_capita") or "projecao")
    rec_cfg = {r["id"]: r for r in cfg.get("reconciliacoes", [])}
    tol = float((rec_cfg.get("populacao_projecao_vs_estimativa") or {}).get("tolerancia_absoluta", 0) or 0)
    base = {"disponivel": False, "exercicio": exercicio, "local": local, "regua": regua, "valor": None, "motivo": None, "estimativa": None, "censo": None, "conferencia": None, "fonte": None}
    if stg_ibge is None or not Path(stg_ibge).exists():
        base["motivo"] = "população indisponível: staging do IBGE ausente (rode `recibo fetch ibge` e `recibo transform ibge`)"
        return base
    df = pl.read_parquet(stg_ibge).filter(pl.col("local") == local)
    if df.height == 0:
        base["motivo"] = f"população indisponível: o staging do IBGE não tem {local!r}"
        return base
    proj = df.filter((pl.col("regua") == regua) & (pl.col("ano") == exercicio))
    if proj.height == 0:
        anos = df.filter(pl.col("regua") == regua)["ano"]
        base["motivo"] = f"população indisponível: a régua {regua!r} não tem {exercicio} (anos: {anos.min()} a {anos.max()})" if anos.len() else f"população indisponível: a régua {regua!r} não está no staging do IBGE (réguas: {sorted(df['regua'].unique().to_list())})"
        return base
    r = proj.row(0, named=True)
    base.update({
        "valor": int(r["populacao"]),
        "homens": None if r["homens"] is None else int(r["homens"]),
        "mulheres": None if r["mulheres"] is None else int(r["mulheres"]),
        "idosos_65_mais": None if r["idosos_65_mais"] is None else int(r["idosos_65_mais"]),
        "data_referencia": str(r["data_referencia"]),
        "revisao": r["revisao"],
        "fonte": {"regua": regua, "arquivo": r["arquivo"], "revisao": r["revisao"], "snapshot_id": r["snapshot_id"], "sha256_canonical": r["sha256_canonical"], "fetched_at": r["fetched_at"], "data_referencia": str(r["data_referencia"])},
    })
    est = df.filter((pl.col("regua") == "estimativa") & (pl.col("ano") == exercicio))
    if est.height:
        e = est.row(0, named=True)
        diff = int(e["populacao"]) - int(r["populacao"])
        ok = abs(diff) <= tol
        base["estimativa"] = {"valor": int(e["populacao"]), "tabela": e["tabela"], "variavel": e["variavel"], "snapshot_id": e["snapshot_id"], "data_referencia": str(e["data_referencia"])}
        base["conferencia"] = {"id": "populacao_projecao_vs_estimativa", "ok": ok, "critica": False, "esperado": int(r["populacao"]), "obtido": int(e["populacao"]), "diff": diff,
                               "detalhe": f"projeção revisão {r['revisao']} contra estimativa oficial (SIDRA {e['tabela']}) em {exercicio}; tolerância {tol:g}"}
        if not ok:
            base["motivo"] = f"população indisponível: a estimativa oficial do IBGE ({int(e['populacao']):,}) difere da projeção ({int(r['populacao']):,}) em {exercicio}; per capita suspenso até entender a diferença"
            return base
    else:
        base["conferencia"] = {"id": "populacao_projecao_vs_estimativa", "ok": True, "critica": False, "esperado": int(r["populacao"]), "obtido": int(r["populacao"]), "diff": 0, "calculada": False,
                               "detalhe": f"o IBGE não publica estimativa oficial para {exercicio} (SIDRA 6579 não tem 2007, 2010, 2022 e 2023); só a projeção"}
    censo = df.filter(pl.col("regua") == "censo").sort("ano")
    if censo.height:
        c = censo.row(censo.height - 1, named=True)
        mesmo_ano = df.filter((pl.col("regua") == regua) & (pl.col("ano") == c["ano"]))
        base["censo"] = {"ano": int(c["ano"]), "valor": int(c["populacao"]), "data_referencia": str(c["data_referencia"]), "tabela": c["tabela"],
                         "projecao_no_mesmo_ano": int(mesmo_ano["populacao"][0]) if mesmo_ano.height else None,
                         "nota": "o Censo conta pessoas numa data; a projeção corrige a cobertura do Censo e por isso é maior: per capita pelo Censo sairia maior na mesma proporção"}
    base["disponivel"] = True
    return base


def por_habitante(valor: float | None, pop: dict) -> float | None:
    if valor is None or not pop.get("disponivel") or not pop.get("valor"):
        return None
    return float(valor) / float(pop["valor"])
