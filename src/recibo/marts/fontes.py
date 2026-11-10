"""Fontes de recursos: com que dinheiro cada despesa foi paga, pelo carimbo da fonte na lei orçamentária (SIOP).

Cada despesa do orçamento federal traz o código da fonte de recursos que a pagou. As fontes de título (443,
refinanciamento; 444, outras aplicações) só podem pagar o que a lei autoriza, então o carimbo responde, com número
oficial, à pergunta que o rateio não responde: para onde foi o dinheiro dos títulos vendidos, e com que dinheiro o
gasto de verdade foi pago. O primeiro dígito do código diz quando o dinheiro entrou (1 e 2: no próprio ano; 3 e 6: em
anos anteriores, o "colchão" do Tesouro).

Saída (bloco `fontes_de_recursos` do export): por grupo de fonte (títulos de refinanciamento, títulos novos, receitas
carimbadas para a dívida, receita), o total, a divisão por origem do dinheiro (corrente, anteriores, outros), por
destino (rolagem, principal, juros, despesa primária, inversões) e o cruzamento origem x destino, mais a lista das
fontes do grupo. Só no ano fechado, com o SIOP: fora disso, `disponivel: false` com o motivo.
"""

from __future__ import annotations

import logging
import re

from .recibo import lit

log = logging.getLogger(__name__)

DESTINOS = ("rolagem", "principal", "juros", "primaria", "inversoes")
ORIGENS = ("corrente", "anteriores", "outros")


def destino_de(gnd: str, refinanciamento: bool) -> str:
    g = str(gnd or "")
    if g == "6":
        return "rolagem" if refinanciamento else "principal"
    if g == "2":
        return "juros"
    if g == "5":
        return "inversoes"
    return "primaria"


def _grupo_de(nome: str, grupos: list[dict]) -> str:
    for g in grupos:
        rx = g.get("regex")
        if rx and re.search(rx, nome or "", flags=re.I):
            return g["id"]
    resto = next((g["id"] for g in grupos if g.get("resto")), None)
    return resto or "outros"


def calcular_fontes(q, rows, linhas_cfg: dict, exercicio: int, anexo01: dict, usa_siop: bool, tem_siop: bool) -> dict:
    cfg = linhas_cfg.get("fontes_de_recursos") or {}
    if not cfg or not cfg.get("grupos"):
        return {"disponivel": False, "motivo": "sem bloco fontes_de_recursos em linhas.yaml"}
    if not tem_siop:
        return {"disponivel": False, "motivo": "sem staging do SIOP"}
    if not usa_siop:
        return {"disponivel": False, "motivo": "ano em andamento: o SIOP não cobre o mesmo período do RREO; o carimbo da fonte entra quando o ano fecha"}
    ag = cfg.get("agregacao", "fonte_gnd_subfuncao")
    if not q(f"SELECT COUNT(*) FROM siop WHERE exercicio = {int(exercicio)} AND agregacao = {lit(ag)}"):
        return {"disponivel": False, "motivo": f"agregação {ag} do SIOP ausente para {exercicio}"}
    try:
        lista = rows(
            f"SELECT cod_fonte, ANY_VALUE(nome_fonte), cod_gnd, COALESCE(refinanciamento, false), SUM(liquidado) FROM siop "
            f"WHERE exercicio = {int(exercicio)} AND agregacao = {lit(ag)} AND ofss AND NOT intra GROUP BY 1, 3, 4"
        )
    except Exception as e:  # coluna ausente numa agregação antiga: o bloco sai indisponível, o recibo não muda
        return {"disponivel": False, "motivo": f"não foi possível ler as fontes no SIOP: {e}"}
    grupos_cfg = cfg["grupos"]
    origem_por_digito = {str(k): str(v) for k, v in (cfg.get("origem_por_digito") or {}).items()}
    acum: dict[str, dict] = {}
    for g in grupos_cfg:
        acum[g["id"]] = {
            "id": g["id"], "nome": g.get("nome", g["id"]), "regex": g.get("regex"), "valor": 0.0,
            "por_origem": {o: 0.0 for o in ORIGENS}, "por_destino": {d: 0.0 for d in DESTINOS},
            "por_origem_destino": {o: {d: 0.0 for d in DESTINOS} for o in ORIGENS}, "fontes": {},
        }
    total = 0.0
    for cod, nome, gnd, refin, val in lista:
        cod, nome = str(cod or ""), str(nome or "").strip()
        val = float(val or 0.0)
        gid = _grupo_de(nome, grupos_cfg)
        if gid not in acum:
            acum[gid] = {"id": gid, "nome": "Outras fontes", "regex": None, "valor": 0.0, "por_origem": {o: 0.0 for o in ORIGENS}, "por_destino": {d: 0.0 for d in DESTINOS},
                         "por_origem_destino": {o: {d: 0.0 for d in DESTINOS} for o in ORIGENS}, "fontes": {}}
        origem = origem_por_digito.get(cod[:1], "outros")
        destino = destino_de(gnd, bool(refin))
        it = acum[gid]
        it["valor"] += val
        it["por_origem"][origem] += val
        it["por_destino"][destino] += val
        it["por_origem_destino"][origem][destino] += val
        f = it["fontes"].setdefault(cod, {"cod": cod, "nome": nome, "origem": origem, "valor": 0.0, "por_destino": {d: 0.0 for d in DESTINOS}})
        f["valor"] += val
        f["por_destino"][destino] += val
        total += val
    grupos = []
    for g in grupos_cfg + [x for x in acum.values() if x["id"] not in {y["id"] for y in grupos_cfg}]:
        it = acum[g["id"]]
        it["fontes"] = sorted(it["fontes"].values(), key=lambda f: -f["valor"])
        grupos.append(it)
    por_destino_total = {d: sum(g["por_destino"][d] for g in grupos) for d in DESTINOS}
    por_origem_total = {o: sum(g["por_origem"][o] for g in grupos) for o in ORIGENS}
    conf = {}
    if "refinanciamento" in anexo01:
        conf["rolagem_siop"] = por_destino_total["rolagem"]
        conf["rolagem_rreo"] = float(anexo01["refinanciamento"])
    if "total_exceto_intra" in anexo01 and "refinanciamento" in anexo01:
        conf["total_siop"] = total
        conf["total_rreo"] = float(anexo01["total_exceto_intra"]) + float(anexo01["refinanciamento"])
    return {
        "disponivel": True,
        "fonte": f"SIOP (S8), agregação {ag}: despesa liquidada por fonte de recursos, grupo de natureza e subfunção (OFSS, sem intra), com a marca de refinanciamento",
        "exercicio": int(exercicio),
        "total": total,
        "por_destino": por_destino_total,
        "por_origem": por_origem_total,
        "grupos": grupos,
        "destinos_nomes": dict(cfg.get("destinos") or {}),
        "origens_nomes": {"corrente": "dinheiro que entrou no próprio ano", "anteriores": "dinheiro de anos anteriores (o colchão do Tesouro)", "outros": "fontes de outros tipos (condicionadas e afins)"},
        "gnd_nomes": dict(cfg.get("gnd") or {}),
        "conferencia": conf,
        "nota": "O carimbo diz com que fonte a despesa foi paga, não de onde cada real saiu fisicamente: dinheiro de título e de receita ficam na mesma conta no Banco Central. É a melhor régua oficial que existe para a pergunta.",
    }
