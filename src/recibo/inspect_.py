"""Diagnóstico de uma chave raw: o que a API devolveu e como a derivação classificou cada linha.

É o que se cola de volta para calibrar `derive()` e `linhas.yaml`.
"""

from __future__ import annotations

import re
from collections import Counter, OrderedDict
from pathlib import Path

from .store import Store
from .transform.siconfi import SUBFUNCAO_DEMAIS, rows_for_key


def _fmt(v) -> str:
    return f"{float(v):,.2f}" if v not in (None, "") else "-"


def inspect_key(store: Store, config_dir: Path, key: str, coluna_regex: str | None, rows: int = 100) -> str:
    data = rows_for_key(store, config_dir, key)
    out: list[str] = []
    out.append(f"chave: {key} | linhas: {len(data)} | snapshot: {data[0]['snapshot_id'] if data else '-'}")

    cols = Counter(r["coluna_norm"] for r in data)
    out.append("\ncolunas (coluna_norm -> n linhas):")
    for c, n in sorted(cols.items()):
        out.append(f"  {n:>6}  {c}")

    rotulos = Counter(r["rotulo_norm"] for r in data)
    out.append("\nrótulos distintos (rotulo_norm -> n linhas):")
    for c, n in sorted(rotulos.items()):
        out.append(f"  {n:>6}  {c or '(vazio)'}")

    sel = data
    if coluna_regex:
        rx = re.compile(coluna_regex)
        sel = [r for r in data if rx.search(r["coluna_norm"])]
        out.append(f"\nfiltrando coluna_norm ~ /{coluna_regex}/ -> {len(sel)} linhas")

    ne = Counter((r["nivel"], r["escopo"]) for r in sel)
    out.append("\nnivel x escopo:")
    for (n, e), c in sorted(ne.items()):
        out.append(f"  {c:>6}  nivel={n:<10} escopo={e}")

    nao_reconhecidas = sorted(
        {r["conta_norm"] for r in sel if r["nivel"] == "subfuncao" and not r["subfuncao_cod"]}
    )
    out.append(f"\nsubfunções sem código reconhecido ({len(nao_reconhecidas)}):")
    for c in nao_reconhecidas[:60]:
        out.append(f"  - {c}")

    orfas = sum(1 for r in sel if r["nivel"] == "subfuncao" and not r["funcao_cod"])
    inconsist = sum(1 for r in sel if r.get("fu_inconsistente"))
    out.append(f"\nsubfunções sem função-mãe: {orfas} | prefixo FUxx discordando da ordem: {inconsist}")

    # Resumo por função (escopo exceto intra): valor da função vs soma das subfunções filhas
    por_funcao: "OrderedDict[str, dict]" = OrderedDict()
    for r in sel:
        if r["escopo"] != "exceto_intra" or r["nivel"] not in ("funcao", "subfuncao") or not r["funcao_cod"]:
            continue
        f = por_funcao.setdefault(r["funcao_cod"], {"nome": "", "funcao": 0.0, "subs": 0.0, "n": 0, "demais": 0.0, "adm": 0.0})
        v = float(r.get("valor") or 0.0)
        if r["nivel"] == "funcao":
            f["nome"] = r["conta_norm"]
            f["funcao"] += v
        else:
            f["subs"] += v
            f["n"] += 1
            if r["subfuncao_cod"] == SUBFUNCAO_DEMAIS:
                f["demais"] += v
            elif r["subfuncao_cod"] == "122":
                f["adm"] += v
    if por_funcao:
        out.append("\nresumo por função (exceto intra): função | soma das subfunções | diff | n subs | adm geral (122) | demais")
        for cod, f in por_funcao.items():
            diff = f["subs"] - f["funcao"]
            out.append(
                f"  {cod} {f['nome'][:28]:<28} {_fmt(f['funcao']):>22} {_fmt(f['subs']):>22} {_fmt(diff):>16} "
                f"{f['n']:>3} {_fmt(f['adm']):>20} {_fmt(f['demais']):>20}"
            )

    out.append(f"\nprimeiras {rows} linhas (ordem da API):")
    out.append(f"{'ord':>4} {'nivel':<9} {'escopo':<13} {'fun':<4} {'sub':<6} {'rotulo':<28} {'cod_conta':<34} {'conta':<50} {'valor':>20}")
    for r in sel[:rows]:
        out.append(
            f"{r['ordem']:>4} {r['nivel']:<9} {r['escopo']:<13} {(r['funcao_cod'] or ''):<4} {(r['subfuncao_cod'] or ''):<6} "
            f"{str(r.get('rotulo') or '')[:28]:<28} {str(r.get('cod_conta') or '')[:34]:<34} "
            f"{str(r.get('conta') or '')[:50]:<50} {_fmt(r.get('valor')):>20}"
        )
    return "\n".join(out)
