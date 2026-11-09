"""CLI: recibo fetch | inspect | transform | mart | diff | run"""

from __future__ import annotations

import argparse
import logging
import sys

from . import __version__
from .config import load_yaml, settings, source_config
from .http import Http
from .store import make_store

log = logging.getLogger("recibo")

FONTES = {"siconfi": "S4", "siop": "S8"}


def _http(st, timeout: float | None = None) -> Http:
    verify: bool | str = st.ca_bundle if st.ca_bundle else True
    return Http(user_agent=st.user_agent, timeout=timeout or st.timeout_seconds, pause=st.pause_seconds, verify=verify)


def cmd_fetch(args) -> int:
    import httpx

    st = settings()
    store = make_store(st)
    cfg = source_config(st.config_dir, FONTES[args.fonte])
    # consultas agregadas do SIOP podem levar dezenas de segundos: o timeout HTTP vem de sources.yaml (timeout_http_s)
    timeout = max(st.timeout_seconds, float(cfg.get("timeout_http_s", 0) or 0))
    try:
        with _http(st, timeout=timeout) as http:
            if args.fonte == "siconfi":
                from .fetchers.siconfi import run_fetch

                run_fetch(http, store, cfg, args.exercicios, args.periodos, args.anexos)
            else:
                from .fetchers.siop import run_fetch as run_fetch_siop

                results = run_fetch_siop(http, store, cfg, args.exercicios, getattr(args, "agregacoes", None), getattr(args, "force", False))
                if any(r.status == "erro" for r in results):
                    log.error("fetch siop terminou com consultas em erro (veja o log e o _index.json); rode de novo com --force depois")
                    return 5
    except httpx.HTTPError as e:
        log.error("falha de rede ao acessar %s: %s", cfg["base_url"], e)
        log.error("verifique conectividade/proxy; nada foi gravado para a consulta que falhou")
        return 4
    return 0


def cmd_inspect(args) -> int:
    st = settings()
    store = make_store(st)
    if args.fonte == "siconfi":
        from .inspect_ import inspect_key

        if args.periodo is None:
            log.error("inspect siconfi exige --periodo")
            return 2
        linhas = load_yaml(st.config_dir, "linhas.yaml")
        key = f"{args.exercicio}/{args.periodo:02d}/{args.anexo}"
        rx = None if args.todas_colunas else linhas["metrica"]["coluna_regex"]
        print(inspect_key(store, st.config_dir, key, rx, rows=args.rows))
    else:
        from .inspect_siop import inspect_siop

        print(inspect_siop(store, args.exercicio, args.agregacao, args.funcao, rows=args.rows))
    return 0


def cmd_transform(args) -> int:
    st = settings()
    store = make_store(st)
    if args.fonte == "siconfi":
        from .transform.siconfi import build_staging

        build_staging(store, st.config_dir, st.staging_dir)
    else:
        from .transform.siop import build_staging as build_staging_siop

        build_staging_siop(store, st.staging_dir, source_config(st.config_dir, FONTES["siop"]))
    return 0


def _periodo_disponivel(store, exercicio: int, anexo_regex_hint: str = "Anexo 02") -> int | None:
    from .fetchers.siconfi import DATASET_RREO, SOURCE

    index = store.read_index(SOURCE, DATASET_RREO)
    pers = []
    for key, st in index.items():
        ex, per, anexo = key.split("/", 2)
        if int(ex) == exercicio and st.get("status") in ("ok", "vazio_apos_ok") and anexo_regex_hint.lower() in anexo.lower():
            pers.append(int(per))
    return max(pers) if pers else None


def _estado(exercicio: int, periodo: int) -> dict:
    if periodo >= 6:
        return {"selo": "fechado", "nota": "6º bimestre: exercício completo (RREO). Preliminar até o COFOG do ano ser publicado."}
    return {"selo": "em_andamento", "nota": f"Dados até o {periodo}º bimestre de {exercicio} (RREO). Atualização a cada bimestre."}


def _siop_staging(st):
    from .transform.siop import STAGING_FILE as SIOP_FILE

    p = st.staging_dir / SIOP_FILE
    return p if p.exists() else None


def cmd_mart(args) -> int:
    from .marts.recibo import compute, print_summary, to_export, write_outputs
    from .transform.siconfi import STAGING_FILE

    st = settings()
    store = make_store(st)
    stg = st.staging_dir / STAGING_FILE
    if not stg.exists():
        log.error("staging não encontrado (%s); rode `recibo transform siconfi`", stg)
        return 2
    periodo = args.periodo or _periodo_disponivel(store, args.exercicio)
    if not periodo:
        log.error("nenhum período com dados para %s no índice; rode `recibo fetch siconfi`", args.exercicio)
        return 2
    linhas = load_yaml(st.config_dir, "linhas.yaml")
    fontes = load_yaml(st.config_dir, "sources.yaml")
    siop = _siop_staging(st)
    if siop is None:
        log.info("staging do SIOP ausente: conferências cruzadas não rodam (rode `recibo fetch siop` e `recibo transform siop`)")
    res = compute(stg, linhas, args.exercicio, periodo, siop_path=siop)
    export = to_export(res, linhas, fontes, _estado(args.exercicio, periodo))
    p_json, p_csv, p_exp = write_outputs(export, st.marts_dir, st.export_dir)
    print_summary(export)
    print(f"\narquivos: {p_json}\n          {p_csv}\n          {p_exp}")
    if not res.publicavel:
        log.error("reconciliação crítica falhou: NÃO PUBLICÁVEL. Rode `recibo inspect siconfi` e calibre.")
        return 3
    return 0


def cmd_diff(args) -> int:
    from pathlib import Path

    from .diff import Periodo, diff_periodos, render, write_json
    from .transform.siconfi import STAGING_FILE

    st = settings()
    store = make_store(st)
    stg = st.staging_dir / STAGING_FILE
    if not stg.exists():
        log.error("staging não encontrado (%s); rode `recibo transform siconfi`", stg)
        return 2
    periodo = args.periodo or _periodo_disponivel(store, args.exercicio)
    if not periodo:
        log.error("nenhum período com dados para %s no índice; rode `recibo fetch siconfi`", args.exercicio)
        return 2
    periodo_contra = args.periodo_contra or periodo
    linhas = load_yaml(st.config_dir, "linhas.yaml")
    d = diff_periodos(stg, linhas, Periodo(args.exercicio, periodo), Periodo(args.contra, periodo_contra))
    print(render(d, top=args.top, funcoes=args.funcao, ordem=args.ordem))
    if args.out:
        print(f"\njson: {write_json(d, Path(args.out))}")
    return 0


def cmd_run(args) -> int:
    rc = cmd_fetch(argparse.Namespace(fonte="siconfi", exercicios=args.exercicios, periodos=None, anexos=None))
    if rc:
        return rc
    rc = cmd_transform(argparse.Namespace(fonte="siconfi"))
    if rc:
        return rc
    if not args.sem_siop:
        rc = cmd_fetch(argparse.Namespace(fonte="siop", exercicios=args.exercicios, agregacoes=None, force=False))
        if rc:
            return rc
        rc = cmd_transform(argparse.Namespace(fonte="siop"))
        if rc:
            return rc
    worst = 0
    for ex in args.exercicios:
        rc = cmd_mart(argparse.Namespace(exercicio=ex, periodo=None))
        worst = max(worst, rc)
    return worst


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="recibo", description="Pipeline aberto: de cada R$ 1,00 de imposto, quantos centavos foram para onde.")
    p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument("--version", action="version", version=f"recibo {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    f = sub.add_parser("fetch", help="baixa dados de uma fonte para a camada raw")
    f.add_argument("fonte", choices=sorted(FONTES))
    f.add_argument("--exercicios", type=int, nargs="+", default=[2024, 2025, 2026])
    f.add_argument("--periodos", type=int, nargs="+", help="siconfi: bimestres (padrão: 1 a 6)")
    f.add_argument("--anexos", nargs="+", help='siconfi: nomes exatos, ex.: "RREO-Anexo 02" (padrão: sources.yaml)')
    f.add_argument("--agregacoes", nargs="+", help="siop: ids de agregação em sources.yaml (padrão: todas)")
    f.add_argument("--force", action="store_true", help="siop: refaz mesmo sem mudança na data de atualização do grafo")
    f.set_defaults(fn=cmd_fetch)

    i = sub.add_parser("inspect", help="mostra como a API devolveu uma chave e como foi classificada")
    i.add_argument("fonte", choices=sorted(FONTES))
    i.add_argument("--exercicio", type=int, required=True)
    i.add_argument("--periodo", type=int, help="siconfi: bimestre (obrigatório)")
    i.add_argument("--anexo", default="RREO-Anexo 02", help="siconfi: nome do anexo")
    i.add_argument("--agregacao", default="acao_subfuncao", help="siop: id da agregação (padrão: acao_subfuncao)")
    i.add_argument("--funcao", help="siop: restringe a uma função, ex.: 28")
    i.add_argument("--rows", type=int, default=100)
    i.add_argument("--todas-colunas", action="store_true", help="siconfi: não filtrar pela coluna da métrica")
    i.set_defaults(fn=cmd_inspect)

    t = sub.add_parser("transform", help="raw -> staging (parquet)")
    t.add_argument("fonte", choices=sorted(FONTES))
    t.set_defaults(fn=cmd_transform)

    m = sub.add_parser("mart", help="staging -> recibo (json/csv) com reconciliações")
    m.add_argument("nome", choices=["recibo"])
    m.add_argument("--exercicio", type=int, required=True)
    m.add_argument("--periodo", type=int, help="bimestre (padrão: o último disponível no índice)")
    m.set_defaults(fn=cmd_mart)

    d = sub.add_parser("diff", help="o que mudou entre dois períodos: linhas, Anexo 01, funções e subfunções")
    d.add_argument("--exercicio", type=int, required=True, help="exercício analisado (A)")
    d.add_argument("--periodo", type=int, help="bimestre de A (padrão: o último disponível no índice)")
    d.add_argument("--contra", type=int, required=True, help="exercício de comparação (B)")
    d.add_argument("--periodo-contra", type=int, help="bimestre de B (padrão: o mesmo de A, isto é, mesmo período)")
    d.add_argument("--top", type=int, default=40, help="subfunções listadas, por variação absoluta (0 = todas)")
    d.add_argument("--funcao", nargs="+", help="restringe a lista de subfunções a estas funções, ex.: 28 09")
    d.add_argument("--ordem", choices=["diff", "excesso"], default="diff", help="diff: variação absoluta; excesso: variação além do crescimento do total")
    d.add_argument("--out", help="salva o diff completo em JSON neste caminho")
    d.set_defaults(fn=cmd_diff)

    r = sub.add_parser("run", help="fetch + transform (siconfi e siop) + mart para os exercícios")
    r.add_argument("--exercicios", type=int, nargs="+", default=[2024, 2025, 2026])
    r.add_argument("--sem-siop", action="store_true", help="não busca nem transforma o SIOP nesta execução")
    r.set_defaults(fn=cmd_run)

    args = p.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
    )
    return int(args.fn(args) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
