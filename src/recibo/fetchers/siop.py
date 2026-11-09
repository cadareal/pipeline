"""Fetcher S8: SIOP (orçamento federal) via endpoint SPARQL público.

Modelo de dados (vocabulário loa, confirmado em 2026-11-08): um grafo por exercício,
<http://orcamento.dados.gov.br/{exercicio}/>, onde cada item orçamentário (?i) aponta para suas dimensões
(loa:temFuncao, loa:temSubfuncao, loa:temGND, loa:temElementoDespesa, loa:temResultadoPrimario...), cada
dimensão tem loa:codigo e rdfs:label, e o item carrega os valores (loa:valorDotacaoInicial, loa:valorLeiMaisCredito,
loa:valorEmpenhado, loa:valorLiquidado, loa:valorPago). O exercício publica loa:dataUltimaAtualizacao.

O que este fetcher grava, por exercício, tudo como raw imutável (Store.put, hash canônico sobre os bindings):
  {ex}/exercicio                      data da última atualização do grafo (consulta minúscula). Se o hash não mudou
                                      desde o último snapshot, o exercício inteiro é pulado (o SIOP é a própria etiqueta
                                      de versão). --force ignora isso.
  {ex}/membros/{dimensao}             código -> rótulo de cada dimensão usada (rótulos ficam fora das agregações para
                                      nenhum rótulo duplicado inflar uma soma)
  {ex}/{agregacao}[/{funcao}]         uma agregação (SUM dos valores, COUNT DISTINCT dos itens) por combinação de
                                      códigos; as agregações grandes são fatiadas por função (uma consulta cada)

Agregações e dimensões vêm de config/sources.yaml (fonte S8). Nenhum valor é digitado; a consulta exata fica no
manifest de cada snapshot.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass

from ..http import Http
from ..store import Store, utcnow_iso

log = logging.getLogger(__name__)

SOURCE = "siop"
DATASET = "execucao"
VOCAB_DEFAULT = "http://vocab.e.gov.br/2013/09/loa#"
GRAFO_DEFAULT = "http://orcamento.dados.gov.br/{exercicio}/"
TETO_SUSPEITO = 10_000  # Virtuoso costuma cortar resultados em 10 mil linhas; avisamos se batermos exatamente nisso

# nome da dimensão em sources.yaml -> (caminho a partir do item, variável de dimensão)
DIMENSOES: dict[str, str] = {
    "esfera": "?i loa:temEsfera ?d_{n} .",
    "orgao": "?i loa:temUnidadeOrcamentaria ?uo_{n} . ?uo_{n} loa:temOrgao ?d_{n} .",
    "uo": "?i loa:temUnidadeOrcamentaria ?d_{n} .",
    "funcao": "?i loa:temFuncao ?d_{n} .",
    "subfuncao": "?i loa:temSubfuncao ?d_{n} .",
    "programa": "?i loa:temPrograma ?d_{n} .",
    "acao": "?i loa:temAcao ?d_{n} .",
    "plano_orcamentario": "?i loa:temPlanoOrcamentario ?d_{n} .",
    "subtitulo": "?i loa:temSubtitulo ?d_{n} .",
    "categoria_economica": "?i loa:temCategoriaEconomica ?d_{n} .",
    "gnd": "?i loa:temGND ?d_{n} .",
    "modalidade_aplicacao": "?i loa:temModalidadeAplicacao ?d_{n} .",
    "elemento_despesa": "?i loa:temElementoDespesa ?d_{n} .",
    "fonte": "?i loa:temFonteRecursos ?d_{n} .",
    "id_uso": "?i loa:temIdentificadorUso ?d_{n} .",
    "resultado_primario": "?i loa:temResultadoPrimario ?d_{n} .",
}

# medida -> predicado. Padrões obrigatórios (todo item tem os cinco), como no pacote oficial.
VALORES: dict[str, str] = {
    "dotacao_inicial": "loa:valorDotacaoInicial",
    "dotacao_atual": "loa:valorLeiMaisCredito",
    "empenhado": "loa:valorEmpenhado",
    "liquidado": "loa:valorLiquidado",
    "pago": "loa:valorPago",
}


def _prefixos(vocab: str) -> str:
    return f"PREFIX loa: <{vocab}> PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#> "


def grafo_iri(cfg: dict, exercicio: int) -> str:
    return str(cfg.get("grafo", GRAFO_DEFAULT)).format(exercicio=int(exercicio))


def query_exercicio(cfg: dict, exercicio: int) -> str:
    """Identificador e data da última atualização do grafo do exercício."""
    return (
        _prefixos(cfg.get("vocabulario", VOCAB_DEFAULT))
        + f"SELECT DISTINCT ?identificador ?data WHERE {{ GRAPH <{grafo_iri(cfg, exercicio)}> {{ "
        "?e loa:identificador ?identificador . ?e loa:dataUltimaAtualizacao ?data } } ORDER BY ?identificador ?data"
    )


def query_membros(cfg: dict, exercicio: int, dimensao: str) -> str:
    """Código -> rótulo dos membros de uma dimensão realmente usados por itens do exercício."""
    if dimensao not in DIMENSOES:
        raise KeyError(f"dimensão desconhecida: {dimensao}")
    caminho = DIMENSOES[dimensao].format(n=dimensao)
    return (
        _prefixos(cfg.get("vocabulario", VOCAB_DEFAULT))
        + f"SELECT DISTINCT ?codigo ?rotulo WHERE {{ GRAPH <{grafo_iri(cfg, exercicio)}> {{ "
        f"{caminho} ?d_{dimensao} loa:codigo ?codigo . OPTIONAL {{ ?d_{dimensao} rdfs:label ?rotulo }} }} }} ORDER BY ?codigo ?rotulo"
    )


def query_agregacao(cfg: dict, exercicio: int, dimensoes: list[str], funcao: str | None = None) -> str:
    """SUM dos cinco valores e COUNT DISTINCT dos itens por combinação de códigos das dimensões pedidas.
    `funcao` restringe a uma função (fatia): acrescenta o literal do código à dimensão função."""
    for d in dimensoes:
        if d not in DIMENSOES:
            raise KeyError(f"dimensão desconhecida: {d}")
    sel = " ".join(f"?cod_{d}" for d in dimensoes)
    soma = " ".join(f"(SUM(?v_{m}) AS ?{m})" for m in VALORES)
    corpo = " ".join(DIMENSOES[d].format(n=d) + f" ?d_{d} loa:codigo ?cod_{d} ." for d in dimensoes)
    if funcao is not None:
        if "funcao" not in dimensoes:
            corpo += " ?i loa:temFuncao ?d_funcao ."
        corpo += f' ?d_funcao loa:codigo "{funcao}" .'
    valores = " ".join(f"?i {p} ?v_{m} ." for m, p in VALORES.items())
    return (
        _prefixos(cfg.get("vocabulario", VOCAB_DEFAULT))
        + f"SELECT {sel} {soma} (COUNT(DISTINCT ?i) AS ?n_itens) WHERE {{ GRAPH <{grafo_iri(cfg, exercicio)}> {{ "
        f"{corpo} {valores} }} }} GROUP BY {sel} ORDER BY {sel}"
    )


def bindings(payload: dict) -> list[dict[str, str]]:
    """Resultado SPARQL/JSON -> lista de {variável: valor} (só o valor; tipo e datatype ficam no raw)."""
    out = []
    for b in payload.get("results", {}).get("bindings", []) or []:
        out.append({k: v.get("value") for k, v in b.items()})
    return out


def canonical_bindings(rows: list[dict[str, str]]) -> bytes:
    ordered = sorted(rows, key=lambda d: json.dumps(d, sort_keys=True, ensure_ascii=False))
    return json.dumps(ordered, sort_keys=True, ensure_ascii=False).encode("utf-8")


@dataclass
class FetchResult:
    key: str
    status: str  # ok | vazio | erro
    n_rows: int
    changed: bool
    snapshot_id: str | None


class SiopFetcher:
    def __init__(self, http: Http, store: Store, src_cfg: dict):
        self.http = http
        self.store = store
        self.cfg = src_cfg
        self.endpoint = str(src_cfg["base_url"])
        self.timeout_ms = int(src_cfg.get("timeout_ms", 0))

    def consultar(self, query: str) -> tuple[list[dict[str, str]], str, dict]:
        params = {"query": query, "format": "application/sparql-results+json", "timeout": self.timeout_ms}
        r = self.http.get(self.endpoint, params=params)
        texto = r.text
        try:
            payload = r.json()
        except ValueError as e:  # Virtuoso devolve HTML em erro de sintaxe ou estouro de tempo
            raise RuntimeError(f"SIOP não devolveu JSON ({r.status_code}): {texto[:300]}") from e
        meta = {
            "status": r.status_code,
            "content_type": r.headers.get("content-type"),
            "url": str(r.url)[:2000],
        }
        rows = bindings(payload)
        if len(rows) == TETO_SUSPEITO:
            log.warning("resposta com exatamente %d linhas: possível teto do servidor; fatie mais a consulta", TETO_SUSPEITO)
        return rows, texto, meta

    def _put(self, key: str, rows: list[dict[str, str]], texto: str, meta: dict, query: str, extra: dict | None = None):
        return self.store.put(
            source=SOURCE,
            dataset=DATASET,
            key=key,
            filename="result.json",
            content=texto.encode("utf-8"),
            canonical=canonical_bindings(rows),
            meta={"request": {"endpoint": self.endpoint, "query": query}, "http": meta, "n_rows": len(rows), **(extra or {})},
        )

    def exercicio(self, exercicio: int) -> tuple[FetchResult, str | None]:
        q = query_exercicio(self.cfg, exercicio)
        rows, texto, meta = self.consultar(q)
        key = f"{exercicio}/exercicio"
        if not rows:
            log.info("%s: grafo vazio ou inexistente", key)
            return FetchResult(key, "vazio", 0, False, None), None
        data = max(r.get("data") or "" for r in rows) or None
        res = self._put(key, rows, texto, meta, q, {"data_atualizacao": data})
        return FetchResult(key, "ok", len(rows), res.changed, res.snapshot_id), data

    def membros(self, exercicio: int, dimensao: str) -> tuple[FetchResult, list[dict[str, str]]]:
        q = query_membros(self.cfg, exercicio, dimensao)
        rows, texto, meta = self.consultar(q)
        key = f"{exercicio}/membros/{dimensao}"
        if not rows:
            return FetchResult(key, "vazio", 0, False, None), rows
        res = self._put(key, rows, texto, meta, q, {"dimensao": dimensao})
        return FetchResult(key, "ok", len(rows), res.changed, res.snapshot_id), rows

    def agregacao(self, exercicio: int, ag: dict, funcao: str | None = None) -> FetchResult:
        q = query_agregacao(self.cfg, exercicio, list(ag["dimensoes"]), funcao)
        rows, texto, meta = self.consultar(q)
        key = f"{exercicio}/{ag['id']}" + (f"/{funcao}" if funcao is not None else "")
        if not rows:
            log.info("%s: sem linhas", key)
            return FetchResult(key, "vazio", 0, False, None)
        res = self._put(key, rows, texto, meta, q, {"agregacao": ag["id"], "dimensoes": list(ag["dimensoes"]), "funcao": funcao})
        log.info("%s: %d linhas, %s", key, len(rows), "NOVO/ALTERADO" if res.changed else "sem mudança")
        return FetchResult(key, "ok", len(rows), res.changed, res.snapshot_id)


def dimensoes_usadas(src_cfg: dict) -> list[str]:
    vistas: list[str] = []
    for ag in src_cfg.get("agregacoes", []):
        for d in ag["dimensoes"]:
            if d not in vistas:
                vistas.append(d)
    if "funcao" not in vistas:
        vistas.append("funcao")
    return vistas


def raw_completo(index: dict, exercicio: int, agregacoes: list[dict]) -> bool:
    """Todas as agregações pedidas já têm raw 'ok' para o exercício, sem nenhuma consulta em erro?
    (fatiadas: ao menos uma fatia ok e nenhuma em erro)"""
    if any(k.startswith(f"{exercicio}/") and v.get("status") == "erro" for k, v in index.items()):
        return False
    for ag in agregacoes:
        prefixo = f"{exercicio}/{ag['id']}"
        if ag.get("fatiar_por_funcao"):
            if not any(k.startswith(prefixo + "/") and v.get("status") == "ok" for k, v in index.items()):
                return False
        elif index.get(prefixo, {}).get("status") != "ok":
            return False
    return True


def run_fetch(
    http: Http,
    store: Store,
    src_cfg: dict,
    exercicios: list[int],
    agregacoes: list[str] | None = None,
    force: bool = False,
) -> list[FetchResult]:
    todas = {ag["id"]: ag for ag in src_cfg.get("agregacoes", [])}
    desconhecidas = [a for a in (agregacoes or []) if a not in todas]
    if desconhecidas:
        raise KeyError(f"agregações não configuradas em sources.yaml: {desconhecidas}")
    pedidas = [todas[a] for a in (agregacoes or list(todas))]
    index = store.read_index(SOURCE, DATASET)
    results: list[FetchResult] = []
    f = SiopFetcher(http, store, src_cfg)

    def registrar(r: FetchResult, extra: dict | None = None):
        prev = index.get(r.key, {})
        status = r.status
        if status == "vazio" and prev.get("status") == "ok":
            status = "vazio_apos_ok"
            log.warning("%s: tinha dados e agora voltou vazio; mantendo snapshot anterior", r.key)
        index[r.key] = {
            "status": status,
            "n_rows": r.n_rows or prev.get("n_rows", 0),
            "snapshot_id": r.snapshot_id or prev.get("snapshot_id"),
            "last_checked": utcnow_iso(),
            "changed_last_run": r.changed,
            **(extra or {}),
        }
        results.append(r)

    for ex in exercicios:
        r_ex, data = f.exercicio(ex)
        registrar(r_ex, {"data_atualizacao": data})
        if r_ex.status == "vazio":
            continue
        if not r_ex.changed and not force and raw_completo(index, ex, pedidas):
            log.info("%s: SIOP sem atualização desde %s e raw completo; pulando (use --force para refazer)", ex, data)
            continue

        rotulos_funcao: list[dict[str, str]] = []
        for dim in dimensoes_usadas(src_cfg):
            try:
                r_m, rows = f.membros(ex, dim)
            except RuntimeError as e:
                log.error("%s/membros/%s: %s", ex, dim, e)
                registrar(FetchResult(f"{ex}/membros/{dim}", "erro", 0, False, None), {"erro": str(e)[:500]})
                continue
            registrar(r_m)
            if dim == "funcao":
                rotulos_funcao = rows
        funcoes = sorted({r["codigo"] for r in rotulos_funcao if r.get("codigo")})
        if not funcoes:
            log.error("%s: sem lista de funções; agregações fatiadas não podem rodar", ex)

        for ag in pedidas:
            fatias: list[str | None] = funcoes if ag.get("fatiar_por_funcao") else [None]
            for fc in fatias:
                key = f"{ex}/{ag['id']}" + (f"/{fc}" if fc is not None else "")
                try:
                    registrar(f.agregacao(ex, ag, fc))
                except RuntimeError as e:  # resposta não-JSON (estouro de tempo, erro de sintaxe): segue para a próxima
                    log.error("%s: %s", key, e)
                    registrar(FetchResult(key, "erro", 0, False, None), {"erro": str(e)[:500]})

    store.write_index(SOURCE, DATASET, index)
    ok = sum(1 for r in results if r.status == "ok")
    novos = sum(1 for r in results if r.changed)
    erros = sum(1 for r in results if r.status == "erro")
    log.info("fetch siop: %d consultas, %d com dados, %d novos/alterados, %d com erro", len(results), ok, novos, erros)
    return results
