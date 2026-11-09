"""Diagnóstico do raw do SIOP: o que veio para um exercício e uma agregação, com rótulos.

É o que se cola de volta para calibrar os filtros (esfera, intra, refinanciamento) e as conferências cruzadas.
"""

from __future__ import annotations

from collections import defaultdict

from .store import Store
from .transform.siop import _carregar, linhas_staging


def _fmt(v: float) -> str:
    return f"{v:,.2f}"


def inspect_siop(store: Store, exercicio: int, agregacao: str, funcao: str | None = None, rows: int = 30) -> str:
    datas, rotulos, agregacoes = _carregar(store)
    sel = [(m, r) for m, r in agregacoes if int(str(m["key"]).split("/")[0]) == exercicio and m["agregacao"] == agregacao]
    out: list[str] = []
    out.append(f"SIOP {exercicio} | agregação: {agregacao} | data da última atualização no SIOP: {datas.get(exercicio) or '-'}")
    if not sel:
        disponiveis = sorted({m["agregacao"] for m, _ in agregacoes if int(str(m['key']).split('/')[0]) == exercicio})
        out.append(f"sem raw para essa agregação; disponíveis em {exercicio}: {disponiveis or '-'}")
        return "\n".join(out)
    linhas = linhas_staging(datas, rotulos, sel)
    if funcao:
        funcao = funcao.zfill(2)
        linhas = [x for x in linhas if x["cod_funcao"] == funcao]
    out.append(f"fatias (chaves raw): {len(sel)} | linhas: {len(linhas)}" + (f" | função {funcao}" if funcao else ""))

    def soma_por(campo: str, nome: str | None = None) -> list[str]:
        acc: dict[str, float] = defaultdict(float)
        lab: dict[str, str] = {}
        for x in linhas:
            k = x.get(campo)
            if k is None:
                continue
            acc[str(k)] += x["liquidado"]
            if nome and x.get(nome):
                lab[str(k)] = x[nome]
        return [f"  {k:<8} {_fmt(v):>24}  {lab.get(k, '')}" for k, v in sorted(acc.items(), key=lambda kv: -kv[1])]

    if any(x["cod_esfera"] is not None for x in linhas):
        out.append("\nliquidado por esfera:")
        out += soma_por("cod_esfera", "nome_esfera")
        out.append("liquidado por esfera_tipo:")
        out += soma_por("esfera_tipo")
    if any(x["intra"] is not None for x in linhas):
        out.append("\nliquidado intra (modalidade 91) vs resto:")
        out += soma_por("intra")
    if any(x["cod_gnd"] is not None for x in linhas):
        out.append("\nliquidado por GND (todas as esferas e modalidades):")
        out += soma_por("cod_gnd", "nome_gnd")
    if any(x["refinanciamento"] is not None for x in linhas):
        out.append("\nliquidado com fonte de refinanciamento vs resto:")
        out += soma_por("refinanciamento")

    out.append(f"\nmaiores {rows} linhas por liquidado (OFSS, sem intra):")
    cols = [d for d in ("cod_esfera", "cod_modalidade_aplicacao", "cod_orgao", "cod_funcao", "cod_subfuncao", "cod_gnd", "cod_elemento_despesa", "cod_fonte", "cod_resultado_primario", "cod_acao") if any(x[d] is not None for x in linhas)]
    top = sorted((x for x in linhas if x["ofss"] is not False and not x["intra"]), key=lambda x: -x["liquidado"])[:rows]
    out.append("  " + " ".join(f"{c[4:][:10]:<10}" for c in cols) + f" {'liquidado':>22} {'pago':>22}  rótulo (última dimensão)")
    for x in top:
        ultima = cols[-1] if cols else None
        rot = x.get("nome_" + ultima[4:]) if ultima else ""
        out.append("  " + " ".join(f"{str(x[c] or ''):<10}" for c in cols) + f" {_fmt(x['liquidado']):>22} {_fmt(x['pago']):>22}  {rot or ''}")
    return "\n".join(out)
