"""Mart "recibo": de cada R$ 1,00, quantos centavos foram para cada linha.

Tudo nasce de config/linhas.yaml: o SQL de cada linha, a ficha técnica exportada e as reconciliações.
Motor local: DuckDB sobre o parquet de staging. (Em nuvem, o mesmo SQL roda no BigQuery com uma troca
de dialeto: regexp_matches -> REGEXP_CONTAINS.)
"""

from __future__ import annotations

import csv
import json
import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import duckdb

log = logging.getLogger(__name__)


def lit(s: str) -> str:
    return "'" + str(s).replace("'", "''") + "'"


def base_where(base: dict, exercicio: int, periodo: int, metrica: dict) -> str:
    parts = [
        f"exercicio = {int(exercicio)}",
        f"periodo = {int(periodo)}",
        f"regexp_matches(anexo_norm, {lit(base['anexo_regex'])})",
        f"regexp_matches(coluna_norm, {lit(metrica['coluna_regex'])})",
        f"escopo = {lit(metrica['escopo'])}",
    ]
    if base.get("nivel"):
        parts.append(f"nivel = {lit(base['nivel'])}")
    return " AND ".join(parts)


def sql_sum(where: str, filtro: str | None = None, table: str = "stg") -> str:
    w = where + (f" AND ({filtro})" if filtro else "")
    return f"SELECT COALESCE(SUM(valor), 0) AS valor FROM {table} WHERE {w}"


def largest_remainder(values: list[float], total: int = 100) -> list[int]:
    """Arredonda proporções para inteiros que somam exatamente `total`."""
    if not values or sum(values) <= 0:
        return [0 for _ in values]
    s = sum(values)
    exact = [v / s * total for v in values]
    floors = [math.floor(x) for x in exact]
    rest = total - sum(floors)
    order = sorted(range(len(values)), key=lambda i: exact[i] - floors[i], reverse=True)
    for i in order[:rest]:
        floors[i] += 1
    return floors


@dataclass
class Item:
    id: str
    nome: str
    valor: float
    sql: str
    cfg: dict
    share: float = 0.0
    centavos: int = 0


@dataclass
class Reconciliacao:
    id: str
    ok: bool
    critica: bool
    esperado: float
    obtido: float
    diff: float
    descricao: str = ""


@dataclass
class Resultado:
    exercicio: int
    periodo: int
    linhas: list[Item]
    excluidos: list[Item]
    total_base_anexo02: float
    total_recibo: float
    reconciliacoes: list[Reconciliacao]
    publicavel: bool
    snapshots: list[dict] = field(default_factory=list)


def _tol(total: float) -> float:
    return max(1000.0, abs(total) * 1e-6)


def compute(stg_path: Path, linhas_cfg: dict, exercicio: int, periodo: int) -> Resultado:
    con = duckdb.connect()
    con.execute(f"CREATE VIEW stg AS SELECT * FROM read_parquet({lit(str(stg_path))})")

    def q(sql: str) -> float:
        v = con.execute(sql).fetchone()[0]
        return float(v or 0.0)

    metrica = linhas_cfg["metrica"]
    bases = linhas_cfg["bases"]
    b02 = bases["anexo02_subfuncao"]
    w02 = base_where(b02, exercicio, periodo, metrica)
    total_base = q(sql_sum(w02))

    linhas: list[Item] = []
    excluidos: list[Item] = []
    for cfg in linhas_cfg["linhas"]:
        if cfg.get("residual"):
            continue
        w = base_where(bases[cfg["base"]], exercicio, periodo, metrica)
        sql = sql_sum(w, cfg.get("filtro"))
        linhas.append(Item(cfg["id"], cfg["nome"], q(sql), sql, cfg))
    for cfg in linhas_cfg.get("excluidos", []):
        w = base_where(bases[cfg["base"]], exercicio, periodo, metrica)
        sql = sql_sum(w, cfg.get("filtro"))
        excluidos.append(Item(cfg["id"], cfg["nome"], q(sql), sql, cfg))

    residual_cfg = next((c for c in linhas_cfg["linhas"] if c.get("residual")), None)
    if residual_cfg:
        atribuido = sum(x.valor for x in linhas if x.cfg["base"] == "anexo02_subfuncao")
        excl = sum(x.valor for x in excluidos if x.cfg["base"] == "anexo02_subfuncao")
        residual = total_base - atribuido - excl
        sql = f"-- residual = total_base ({total_base:.2f}) - atribuido ({atribuido:.2f}) - excluido ({excl:.2f})"
        linhas.append(Item(residual_cfg["id"], residual_cfg["nome"], residual, sql, residual_cfg))

    total_recibo = sum(x.valor for x in linhas)
    shares = [(x.valor / total_recibo) if total_recibo else 0.0 for x in linhas]
    cents = largest_remainder([max(s, 0.0) for s in shares])
    for x, s, c in zip(linhas, shares, cents):
        x.share, x.centavos = s, c

    # Reconciliações
    recs: list[Reconciliacao] = []
    cfg_rec = {r["id"]: r for r in linhas_cfg.get("reconciliacoes", [])}

    def add(rid: str, esperado: float, obtido: float, ok: bool | None = None):
        c = cfg_rec.get(rid, {})
        diff = obtido - esperado
        ok_ = ok if ok is not None else abs(diff) <= _tol(esperado)
        recs.append(Reconciliacao(rid, ok_, bool(c.get("critica", True)), esperado, obtido, diff, c.get("descricao", "")))

    w02_fun = base_where({**b02, "nivel": "funcao"}, exercicio, periodo, metrica)
    soma_funcoes = q(sql_sum(w02_fun))
    add("hierarquia", soma_funcoes, total_base)

    w02_hdr = base_where({**b02, "nivel": "total"}, exercicio, periodo, metrica) + " AND is_header"
    total_header = q(sql_sum(w02_hdr))
    add("total_cabecalho", total_header, soma_funcoes)

    residual_val = next((x.valor for x in linhas if x.cfg.get("residual")), 0.0)
    add("residual_nao_negativo", 0.0, residual_val, ok=residual_val >= -_tol(total_base))

    add("centavos_somam_100", 100.0, float(sum(cents)), ok=sum(cents) == 100)

    juros = next((x.valor for x in linhas if x.id == "juros"), 0.0)
    servico = next((x.valor for x in excluidos if x.id == "servico_divida_bruto"), 0.0)
    b01 = bases.get("anexo01_conta")
    if b01:
        w01 = base_where(b01, exercicio, periodo, metrica)
        amort01 = q(sql_sum(w01, "conta_norm LIKE 'AMORTIZACAO DA DIVIDA' OR conta_norm LIKE 'AMORTIZACAO DA DIVIDA (%'"))
        add("amortizacao_vs_anexo01", amort01, servico - juros)

    publicavel = all(r.ok for r in recs if r.critica)

    snaps = [
        dict(zip(("raw_key", "snapshot_id", "sha256_canonical", "fetched_at"), row))
        for row in con.execute(
            f"SELECT DISTINCT raw_key, snapshot_id, sha256_canonical, fetched_at FROM stg "
            f"WHERE exercicio = {int(exercicio)} AND periodo = {int(periodo)} ORDER BY raw_key"
        ).fetchall()
    ]
    con.close()
    return Resultado(exercicio, periodo, linhas, excluidos, total_base, total_recibo, recs, publicavel, snaps)


def ficha(item: Item, linhas_cfg: dict, fontes: dict[str, dict], res: Resultado) -> dict:
    base = linhas_cfg["bases"].get(item.cfg.get("base", ""), {})
    fonte = fontes.get(base.get("fonte", ""), {})
    return {
        "definicao": item.cfg.get("definicao") or item.cfg.get("motivo"),
        "numerador": f"{item.nome}: soma de 'valor' nas linhas selecionadas pelo filtro",
        "denominador": "Soma de todas as linhas do recibo (despesa executada, ex-refinanciamento e ex-amortização)",
        "fonte": {"id": base.get("fonte"), "nome": fonte.get("nome"), "url": fonte.get("base_url")},
        "dataset": base.get("dataset"),
        "anexo_regex": base.get("anexo_regex"),
        "metrica": linhas_cfg["metrica"]["descricao"],
        "coluna_regex": linhas_cfg["metrica"]["coluna_regex"],
        "escopo": linhas_cfg["metrica"]["escopo"],
        "filtro": item.cfg.get("filtro"),
        "residual": bool(item.cfg.get("residual", False)),
        "sql": item.sql,
        "snapshots": res.snapshots,
    }


def to_export(res: Resultado, linhas_cfg: dict, fontes_cfg: dict, estado: dict) -> dict:
    fontes = {f["id"]: f for f in fontes_cfg["fontes"]}
    juros = next((x.valor for x in res.linhas if x.id == "juros"), 0.0)
    servico = next((x.valor for x in res.excluidos if x.id == "servico_divida_bruto"), 0.0)
    refin = next((x.valor for x in res.excluidos if x.id == "refinanciamento"), 0.0)
    return {
        "versao_spec": linhas_cfg.get("versao"),
        "gerado_em": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "exercicio": res.exercicio,
        "periodo": res.periodo,
        "estado": estado,
        "publicavel": res.publicavel,
        "unidade": "R$ (como publicado pela fonte)",
        "regra_rateio": linhas_cfg.get("regra_rateio"),
        "total_recibo": res.total_recibo,
        "total_base_anexo02": res.total_base_anexo02,
        "linhas": [
            {
                "id": x.id,
                "nome": x.nome,
                "valor": x.valor,
                "share": x.share,
                "centavos": x.centavos,
                "ficha": ficha(x, linhas_cfg, fontes, res),
            }
            for x in res.linhas
        ],
        "fora_do_real": {
            "refinanciamento": {"valor": refin, "motivo": next((e.cfg["motivo"] for e in res.excluidos if e.id == "refinanciamento"), None)},
            "amortizacao_implicita": {"valor": servico - juros, "nota": "(843 + 844) menos juros do Anexo 01"},
            "servico_divida_bruto_anexo02": servico,
        },
        "reconciliacoes": [r.__dict__ for r in res.reconciliacoes],
        "chips_planejados": linhas_cfg.get("chips", []),
        "memos_planejados": linhas_cfg.get("memos", []),
    }


def write_outputs(export: dict, marts_dir: Path, export_dir: Path) -> tuple[Path, Path, Path]:
    marts_dir.mkdir(parents=True, exist_ok=True)
    export_dir.mkdir(parents=True, exist_ok=True)
    ex, per = export["exercicio"], export["periodo"]
    p_json = marts_dir / f"recibo_{ex}_p{per:02d}.json"
    p_json.write_text(json.dumps(export, ensure_ascii=False, indent=2), encoding="utf-8")
    p_csv = marts_dir / f"recibo_{ex}_p{per:02d}.csv"
    with open(p_csv, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["id", "nome", "valor", "share", "centavos"])
        for x in export["linhas"]:
            w.writerow([x["id"], x["nome"], f"{x['valor']:.2f}", f"{x['share']:.6f}", x["centavos"]])
    p_exp = export_dir / f"recibo_{ex}.json"
    p_exp.write_text(json.dumps(export, ensure_ascii=False, indent=2), encoding="utf-8")
    return p_json, p_csv, p_exp


def print_summary(export: dict) -> None:
    print(f"\nRecibo {export['exercicio']} (período {export['periodo']}) | estado: {export['estado'].get('selo')} | publicável: {export['publicavel']}")
    print(f"Total do recibo (ex-rolagem, ex-amortização): {export['total_recibo']:,.2f}")
    print(f"{'linha':<36}{'valor':>22}{'share':>10}{'centavos':>10}")
    for x in export["linhas"]:
        print(f"{x['nome'][:35]:<36}{x['valor']:>22,.2f}{x['share']*100:>9.2f}%{x['centavos']:>10}")
    fr = export["fora_do_real"]
    print(f"\nFora do R$ 1,00: refinanciamento {fr['refinanciamento']['valor']:,.2f} | amortização implícita {fr['amortizacao_implicita']['valor']:,.2f}")
    print("\nReconciliações:")
    for r in export["reconciliacoes"]:
        flag = "OK " if r["ok"] else "FALHOU"
        crit = "crítica" if r["critica"] else "informativa"
        print(f"  [{flag}] {r['id']} ({crit}): esperado {r['esperado']:,.2f} | obtido {r['obtido']:,.2f} | diff {r['diff']:,.2f}")
