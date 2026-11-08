"""Diagnóstico de uma chave raw: o que a API devolveu e como a derivação classificou cada linha.

É o que se cola de volta para calibrar `derive()` e `linhas.yaml` depois da primeira execução real.
"""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path

from .store import Store
from .transform.siconfi import rows_for_key


def inspect_key(store: Store, config_dir: Path, key: str, coluna_regex: str | None, rows: int = 100) -> str:
    data = rows_for_key(store, config_dir, key)
    out: list[str] = []
    out.append(f"chave: {key} | linhas: {len(data)} | snapshot: {data[0]['snapshot_id'] if data else '-'}")

    cols = Counter(r["coluna_norm"] for r in data)
    out.append("\ncolunas (coluna_norm -> n linhas):")
    for c, n in sorted(cols.items()):
        out.append(f"  {n:>6}  {c}")

    sel = data
    if coluna_regex:
        rx = re.compile(coluna_regex)
        sel = [r for r in data if rx.search(r["coluna_norm"])]
        out.append(f"\nfiltrando coluna_norm ~ /{coluna_regex}/ -> {len(sel)} linhas")

    ne = Counter((r["nivel"], r["escopo"]) for r in sel)
    out.append("\nnivel x escopo:")
    for (n, e), c in sorted(ne.items()):
        out.append(f"  {c:>6}  nivel={n:<10} escopo={e}")

    nao_reconhecidas = sorted({r["conta_norm"] for r in sel if r["nivel"] == "subfuncao" and not r["subfuncao_cod"]})
    out.append(f"\nsubfunções sem código reconhecido ({len(nao_reconhecidas)}):")
    for c in nao_reconhecidas[:60]:
        out.append(f"  - {c}")

    orfas = sum(1 for r in sel if r["nivel"] == "subfuncao" and not r["funcao_cod"])
    out.append(f"\nsubfunções sem função-mãe (ordem sem função anterior): {orfas}")

    out.append(f"\nprimeiras {rows} linhas (ordem da API):")
    out.append(f"{'ord':>4} {'nivel':<9} {'escopo':<13} {'fun':<4} {'sub':<4} {'cod_conta':<40} {'conta':<55} {'valor':>20}")
    for r in sel[:rows]:
        v = r.get("valor")
        vs = f"{float(v):,.2f}" if v not in (None, "") else "-"
        out.append(
            f"{r['ordem']:>4} {r['nivel']:<9} {r['escopo']:<13} {(r['funcao_cod'] or ''):<4} {(r['subfuncao_cod'] or ''):<4} "
            f"{str(r.get('cod_conta') or '')[:40]:<40} {str(r.get('conta') or '')[:55]:<55} {vs:>20}"
        )
    return "\n".join(out)
