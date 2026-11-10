"""Mart COFOG (S7): a despesa do Governo Central medida pela régua internacional (GFSM 2014, funções COFOG), como o
Tesouro publica. Produto separado do recibo: nada daqui entra no R$ 1,00, que segue a conta de caixa do Tesouro.

O que sai, por exercício:
  - régua: as dez divisões e os 69 grupos COFOG com valor, share e centavos do total do COFOG (maior resto);
  - ficha: arquivo, data do recurso no CKAN, snapshot e hash, unidade do arquivo e resolução por linha, os ajustes
    metodológicos do Tesouro com valores (quais somam ao total e quais só remanejam);
  - ponte com o recibo do mesmo ano: total do recibo - juros pagos + restos a pagar não processados pagos + juros pelo
    Banco Central + IRRF retido por estados e municípios + venda de bens (negativa) = esperado; comparado ao total
    publicado. Reconciliação `ponte_cofog`, tolerância relativa de linhas.yaml. Sem ponte dentro da tolerância, o mart
    não é publicável (o recibo continua sendo).

Os números do recibo vêm de marts.recibo.compute para o ano fechado (6º bimestre), os mesmos que o recibo publica.
"""

from __future__ import annotations

import csv
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import polars as pl

from ..textnorm import norm
from .recibo import SemDados, largest_remainder

log = logging.getLogger(__name__)

DIVISOES = [f"{i:02d}" for i in range(1, 11)]


@dataclass
class LinhaCofog:
    codigo: str
    nivel: str  # divisao | grupo
    nome: str
    valor: float
    linhas: int
    share: float = 0.0
    centavos: int = 0
    divisao: str = ""  # grupo: a divisão a que pertence


@dataclass
class Ajuste:
    texto: str
    valor: float
    linhas: int
    papel: str  # soma | remaneja
    item: str | None = None  # id em cofog.ponte.somam quando soma


@dataclass
class ItemPonte:
    id: str
    nome: str
    valor: float  # já com o sinal com que entra na soma
    fonte: str


@dataclass
class ReconciliacaoCofog:
    id: str
    ok: bool
    critica: bool
    esperado: float
    obtido: float
    diff: float
    descricao: str = ""
    detalhe: str = ""


@dataclass
class ResultadoCofog:
    exercicio: int
    total: float
    linhas_arquivo: int
    fonte: dict
    unidade: dict
    divisoes: list[LinhaCofog]
    grupos: list[LinhaCofog]
    ajustes: list[Ajuste]
    negativos: dict
    ponte: dict
    reconciliacoes: list[ReconciliacaoCofog] = field(default_factory=list)
    publicavel: bool = False
    selo: str = ""


def rotulos(config_dir: Path | None, ref: str) -> dict[str, str]:
    if config_dir is None:
        return {}
    p = Path(config_dir) / ref
    if not p.exists():
        return {}
    with open(p, encoding="utf-8") as f:
        return {r["codigo"]: r["nome"] for r in csv.DictReader(f)}


def anos_disponiveis(stg_cofog: Path) -> list[int]:
    if not Path(stg_cofog).exists():
        return []
    return sorted(pl.scan_parquet(stg_cofog).select("exercicio").unique().collect()["exercicio"].to_list())


def ler_ano(stg_cofog: Path, exercicio: int) -> pl.DataFrame:
    if not Path(stg_cofog).exists():
        raise SemDados(f"staging do COFOG não existe ({stg_cofog}); rode `recibo fetch cofog` e `recibo transform cofog`")
    df = pl.scan_parquet(stg_cofog).filter(pl.col("exercicio") == exercicio).collect()
    if df.height == 0:
        anos = anos_disponiveis(stg_cofog)
        raise SemDados(
            f"COFOG {exercicio} não está no staging (anos disponíveis: {anos[0] if anos else '-'} a {anos[-1] if anos else '-'}). "
            "O Tesouro publica o COFOG com cerca de um ano de defasagem; se o CKAN já tem o ano, rode `recibo fetch cofog` e `recibo transform cofog`."
        )
    if "unidade_fonte" not in df.columns:  # staging de antes da v0.6.2
        df = df.with_columns(pl.lit("reais").alias("unidade_fonte"), pl.lit(1.0).alias("fator_unidade"), pl.lit(0.01).alias("resolucao_reais"), pl.col("valor").alias("valor_fonte"))
    return df


def _agrupar(df: pl.DataFrame, chave: str, nivel: str, nomes: dict[str, str], total: float) -> list[LinhaCofog]:
    g = df.group_by(chave).agg(pl.col("valor").sum().alias("valor"), pl.len().alias("linhas")).sort(chave)
    out = [
        LinhaCofog(str(k), nivel, nomes.get(str(k), ""), float(v), int(n), divisao=str(k)[:2])
        for k, v, n in zip(g[chave].to_list(), g["valor"].to_list(), g["linhas"].to_list())
    ]
    if nivel == "divisao":  # as dez divisões aparecem sempre, mesmo com zero
        presentes = {x.codigo for x in out}
        out += [LinhaCofog(d, nivel, nomes.get(d, ""), 0.0, 0, divisao=d) for d in DIVISOES if d not in presentes]
        out.sort(key=lambda x: x.codigo)
    shares = [(x.valor / total) if total else 0.0 for x in out]
    cents = largest_remainder([max(s, 0.0) for s in shares])
    for x, s, c in zip(out, shares, cents):
        x.share, x.centavos = s, c
    return out


def classificar_ajustes(df: pl.DataFrame, somam_cfg: dict) -> list[Ajuste]:
    """Cada texto distinto da coluna de ajuste: valor, linhas e papel (soma ao total ou remaneja), pelo prefixo em config."""
    aj = df.filter(pl.col("ajuste")).with_columns(pl.col("ajuste_metodologico").str.strip_chars().alias("texto"))
    g = aj.group_by("texto").agg(pl.col("valor").sum().alias("valor"), pl.len().alias("linhas")).sort("valor", descending=True)
    out: list[Ajuste] = []
    for texto, valor, n in zip(g["texto"].to_list(), g["valor"].to_list(), g["linhas"].to_list()):
        item = next((k for k, c in (somam_cfg or {}).items() if norm(texto).startswith(norm(c["prefixo"]))), None)
        out.append(Ajuste(str(texto), float(valor), int(n), "soma" if item else "remaneja", item))
    return out


def numeros_do_recibo(stg_siconfi: Path | None, linhas_cfg: dict, exercicio: int, periodo: int | None, siop_path: Path | None, config_dir: Path | None) -> dict:
    """Total do recibo, juros pagos, despesa primária liquidada e restos não processados pagos do ano fechado, pelo
    mesmo cálculo que o recibo publica. Sem staging ou sem o 6º bimestre, a ponte fica indisponível com o motivo."""
    from .recibo import compute

    if stg_siconfi is None or not Path(stg_siconfi).exists():
        return {"disponivel": False, "motivo": "staging do RREO ausente: rode `recibo fetch siconfi` e `recibo transform siconfi`"}
    if periodo is None or periodo < 6:
        return {"disponivel": False, "motivo": f"o recibo de {exercicio} não está fechado (6º bimestre); período disponível: {periodo if periodo else 'nenhum'}"}
    try:
        res = compute(Path(stg_siconfi), linhas_cfg, exercicio, periodo, siop_path=siop_path, config_dir=config_dir)
    except SemDados as e:
        return {"disponivel": False, "motivo": str(e)}
    juros = next((x.valor for x in res.linhas if x.id == "juros"), 0.0)
    return {
        "disponivel": True,
        "exercicio": exercicio,
        "periodo": periodo,
        "total_recibo": res.total_recibo,
        "juros_pagos": juros,
        "primaria_liquidada": res.anexo06.get("despesa_primaria_total:liquidadas", 0.0),
        "rpnp_pagos": res.anexo06.get("despesa_primaria_total:rpnp_pagos", 0.0),
        "publicavel": res.publicavel,
    }


def montar_ponte(recibo: dict, ajustes: list[Ajuste], total_cofog: float, ponte_cfg: dict) -> dict:
    somam_cfg = ponte_cfg.get("somam") or {}
    tol_rel = float(ponte_cfg.get("tolerancia_relativa", 0.02))
    remanejam = [a for a in ajustes if a.papel == "remaneja"]
    por_item = {k: sum(a.valor for a in ajustes if a.item == k) for k in somam_cfg}
    faltando = [k for k in somam_cfg if not any(a.item == k for a in ajustes)]
    base = {
        "tolerancia_relativa": tol_rel,
        "remanejamentos": {"valor": sum(a.valor for a in remanejam), "linhas": sum(a.linhas for a in remanejam), "itens": [a.texto for a in remanejam]},
        "ajustes_que_somam": por_item,
        "ajustes_esperados_ausentes": faltando,
    }
    if not recibo.get("disponivel"):
        return {**base, "disponivel": False, "motivo": recibo.get("motivo"), "ok": False, "itens": []}
    itens = [
        ItemPonte("total_recibo", "Total do recibo (despesa primária liquidada + juros pagos)", recibo["total_recibo"], "recibo"),
        ItemPonte("juros_pagos", "menos juros pagos em dinheiro (linha do recibo)", -recibo["juros_pagos"], "recibo"),
        ItemPonte("rpnp_pagos", "mais restos a pagar não processados pagos (contas de anos anteriores pagas no ano)", recibo["rpnp_pagos"], "anexo06"),
    ] + [ItemPonte(k, f"mais {c['nome']}", por_item[k], "cofog") for k, c in somam_cfg.items()]
    esperado = sum(i.valor for i in itens)
    diff = total_cofog - esperado
    rel = (diff / total_cofog) if total_cofog else 0.0
    return {
        **base,
        "disponivel": True,
        "recibo": recibo,
        "itens": [i.__dict__ for i in itens],
        "esperado": esperado,
        "obtido": total_cofog,
        "diff": diff,
        "diff_relativa": rel,
        "ok": abs(diff) <= tol_rel * abs(total_cofog),
        "nota": "Os outros ajustes do Tesouro remanejam despesas que já estão no orçamento para a função certa e não entram na soma.",
    }


def compute_cofog(
    stg_cofog: Path,
    linhas_cfg: dict,
    fontes_cfg: dict,
    exercicio: int,
    stg_siconfi: Path | None = None,
    periodo_recibo: int | None = None,
    siop_path: Path | None = None,
    config_dir: Path | None = None,
) -> ResultadoCofog:
    cfg = linhas_cfg.get("cofog") or {}
    df = ler_ano(stg_cofog, exercicio)
    total = float(df["valor"].sum())
    nomes = rotulos(config_dir, cfg.get("ref", "ref/cofog.csv"))
    divisoes = _agrupar(df, "cofog_divisao", "divisao", nomes, total)
    grupos = _agrupar(df, "cofog_grupo", "grupo", nomes, total)
    for g in grupos:
        if not g.nome:  # grupo sem nome na referência: a descrição mais comum do próprio arquivo
            m = df.filter(pl.col("cofog_grupo") == g.codigo).group_by("cofog_desc").agg(pl.len().alias("n")).sort("n", descending=True)
            g.nome = str(m["cofog_desc"][0]) if m.height else ""
    ajustes = classificar_ajustes(df, (cfg.get("ponte") or {}).get("somam") or {})
    neg = df.filter(pl.col("valor") < 0)
    negativos = {"valor": float(neg["valor"].sum()), "linhas": neg.height}

    r0 = df.row(0, named=True)
    s7 = next((f for f in fontes_cfg.get("fontes", []) if f.get("id") == "S7"), {})
    fonte = {
        "id": "S7",
        "nome": s7.get("nome"),
        "orgao": s7.get("orgao"),
        "dataset_id": s7.get("dataset_id"),
        "arquivo": r0.get("arquivo"),
        "recurso_data": r0.get("recurso_data"),
        "raw_key": r0.get("raw_key"),
        "snapshot_id": r0.get("snapshot_id"),
        "fetched_at": r0.get("fetched_at"),
        "sha256_canonical": r0.get("sha256_canonical"),
        "linhas_arquivo": df.height,
    }
    fator = float(r0.get("fator_unidade") or 1.0)
    unidade = {
        "fonte": r0.get("unidade_fonte"),
        "fator": fator,
        "resolucao_reais": float(r0.get("resolucao_reais") or 0.01),
        "nota": (
            "valores em reais; o arquivo do Tesouro está em R$ milhões com duas casas (declarado em sources.yaml), então cada linha só é precisa até R$ 10 mil"
            if fator != 1.0
            else "valores em reais, como no arquivo do Tesouro (centavos)"
        ),
    }

    recibo = numeros_do_recibo(stg_siconfi, linhas_cfg, exercicio, periodo_recibo, siop_path, config_dir)
    ponte = montar_ponte(recibo, ajustes, total, cfg.get("ponte") or {})

    recs: list[ReconciliacaoCofog] = []
    soma_div = sum(x.valor for x in divisoes)
    recs.append(ReconciliacaoCofog("cofog_soma_divisoes", abs(soma_div - total) <= max(1.0, abs(total) * 1e-9), True, total, soma_div, soma_div - total, "As dez divisões somam o total do ano (todo código do arquivo está em uma divisão de 01 a 10)."))
    cents = sum(x.centavos for x in divisoes)
    recs.append(ReconciliacaoCofog("cofog_centavos", cents == 100, True, 100, cents, cents - 100, "Os centavos das dez divisões somam R$ 1,00 (maior resto)."))
    if ponte.get("disponivel"):
        det = f"diferença {ponte['diff']:,.2f} ({ponte['diff_relativa'] * 100:+.2f}% do total do COFOG; tolerância {ponte['tolerancia_relativa'] * 100:.0f}%)"
        if ponte["ajustes_esperados_ausentes"]:
            det += f"; ajustes esperados e ausentes no arquivo: {ponte['ajustes_esperados_ausentes']}"
        recs.append(ReconciliacaoCofog("ponte_cofog", bool(ponte["ok"]), True, ponte["esperado"], total, ponte["diff"], "Total do recibo, menos juros pagos, mais restos não processados pagos, juros pelo Banco Central, IRRF retido e venda de bens = total do COFOG, dentro da tolerância.", det))
    else:
        recs.append(ReconciliacaoCofog("ponte_cofog", False, True, 0.0, total, 0.0, "Ponte com o recibo não calculada.", str(ponte.get("motivo"))))

    publicavel = all(r.ok for r in recs if r.critica)
    if ponte.get("disponivel"):
        selo = (f"ponte com o recibo {exercicio} fecha em {ponte['diff_relativa'] * 100:+.2f}% (tolerância {ponte['tolerancia_relativa'] * 100:.0f}%)" if ponte["ok"]
                else f"ponte com o recibo {exercicio} NÃO fecha: {ponte['diff_relativa'] * 100:+.2f}% (tolerância {ponte['tolerancia_relativa'] * 100:.0f}%)")
    else:
        selo = f"sem ponte com o recibo: {ponte.get('motivo')}"
    return ResultadoCofog(exercicio, total, df.height, fonte, unidade, divisoes, grupos, ajustes, negativos, ponte, recs, publicavel, selo)


def to_export_cofog(res: ResultadoCofog, linhas_cfg: dict) -> dict:
    cfg = linhas_cfg.get("cofog") or {}
    return {
        "produto": "cofog",
        "versao_spec": linhas_cfg.get("versao"),
        "gerado_em": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "exercicio": res.exercicio,
        "publicavel": res.publicavel,
        "selo": res.selo,
        "o_que_e": "Despesa do Governo Central pela régua internacional (GFSM 2014, funções COFOG), como o Tesouro publica. Produto separado do recibo: não entra no R$ 1,00.",
        "total": res.total,
        "total_definicao": "despesa liquidada + restos a pagar não processados pagos, juros por competência (Banco Central), transferências por caixa, venda de bens negativa, com os ajustes metodológicos do Tesouro",
        "unidade": res.unidade,
        "fonte": res.fonte,
        "divisoes": [x.__dict__ for x in res.divisoes],
        "grupos": [x.__dict__ for x in res.grupos],
        "ajustes": [a.__dict__ for a in res.ajustes],
        "negativos": res.negativos,
        "ponte": res.ponte,
        "reconciliacoes": [r.__dict__ for r in res.reconciliacoes],
        "glossario": cfg.get("glossario", []),
    }


def write_outputs_cofog(export: dict, marts_dir: Path, export_dir: Path) -> tuple[Path, Path, Path]:
    marts_dir.mkdir(parents=True, exist_ok=True)
    export_dir.mkdir(parents=True, exist_ok=True)
    ex = export["exercicio"]
    p_json = marts_dir / f"cofog_{ex}.json"
    p_json.write_text(json.dumps(export, ensure_ascii=False, indent=2), encoding="utf-8")
    p_csv = marts_dir / f"cofog_{ex}.csv"
    with open(p_csv, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["nivel", "codigo", "divisao", "nome", "valor", "share", "centavos", "linhas"])
        for x in export["divisoes"] + export["grupos"]:
            w.writerow([x["nivel"], x["codigo"], x["divisao"], x["nome"], f"{x['valor']:.2f}", f"{x['share']:.6f}", x["centavos"], x["linhas"]])
    p_exp = export_dir / f"cofog_{ex}.json"
    p_exp.write_text(json.dumps(export, ensure_ascii=False, indent=2), encoding="utf-8")
    return p_json, p_csv, p_exp


def print_summary_cofog(export: dict, grupos: int = 15) -> None:
    print(f"\nCOFOG {export['exercicio']} | publicável: {export['publicavel']} | {export['selo']}")
    print(f"Total pela régua internacional (GFSM 2014): {export['total']:,.2f}  ({export['unidade']['nota']})")
    f = export["fonte"]
    print(f"fonte: {f.get('arquivo')} | recurso {f.get('recurso_data')} | snapshot {f.get('snapshot_id')} | {f.get('linhas_arquivo')} linhas")
    print(f"\n{'divisão':<8}{'nome':<40}{'valor':>24}{'share':>9}{'centavos':>10}")
    for x in sorted(export["divisoes"], key=lambda d: d["valor"], reverse=True):
        print(f"{x['codigo']:<8}{x['nome'][:39]:<40}{x['valor']:>24,.2f}{x['share'] * 100:>8.2f}%{x['centavos']:>10}")
    print(f"\nmaiores grupos ({grupos} de {len(export['grupos'])}):")
    for x in sorted(export["grupos"], key=lambda d: d["valor"], reverse=True)[:grupos]:
        print(f"  {x['codigo']:<6}{x['nome'][:52]:<53}{x['valor']:>24,.2f}{x['share'] * 100:>8.2f}%{x['centavos']:>6}")
    print("\najustes metodológicos do Tesouro (soma = entra na ponte; remaneja = já estava no orçamento, só mudou de função):")
    for a in export["ajustes"]:
        print(f"  {a['papel']:<9}{a['valor']:>24,.2f}  {a['linhas']:>5} linhas  {a['texto']}")
    if export["negativos"]["linhas"]:
        print(f"  linhas negativas (venda de bens): {export['negativos']['linhas']}, somando {export['negativos']['valor']:,.2f}")
    p = export["ponte"]
    print("\nponte com o recibo:")
    if p.get("disponivel"):
        for i in p["itens"]:
            print(f"  {i['valor']:>24,.2f}  {i['nome']}")
        print(f"  {p['esperado']:>24,.2f}  = esperado")
        print(f"  {p['obtido']:>24,.2f}  COFOG publicado")
        print(f"  {p['diff']:>24,.2f}  diferença não explicada ({p['diff_relativa'] * 100:+.2f}% do total do COFOG; tolerância {p['tolerancia_relativa'] * 100:.0f}%)")
        print(f"  remanejamentos (fora da soma): {p['remanejamentos']['valor']:,.2f} em {p['remanejamentos']['linhas']} linhas: {', '.join(p['remanejamentos']['itens'])[:200]}")
        if p.get("ajustes_esperados_ausentes"):
            print(f"  AVISO: ajustes esperados e ausentes no arquivo: {p['ajustes_esperados_ausentes']}")
    else:
        print(f"  indisponível: {p.get('motivo')}")
    print("\nreconciliações:")
    for r in export["reconciliacoes"]:
        flag = "OK " if r["ok"] else "FALHOU"
        print(f"  [{flag}] {r['id']}: esperado {r['esperado']:,.2f} | obtido {r['obtido']:,.2f} | diff {r['diff']:,.2f}  {r.get('detalhe') or ''}")
