"""CLI: recibo fetch | inspect | transform | mart | run"""

from __future__ import annotations

import argparse
import logging
import sys

from . import __version__
from .config import load_yaml, settings, source_config
from .http import Http
from .store import make_store

log = logging.getLogger("recibo")


def _http(st) -> Http:
    verify: bool | str = st.ca_bundle if st.ca_bundle else True
    return Http(user_agent=st.user_agent, timeout=st.timeout_seconds, pause=st.pause_seconds, verify=verify)


def cmd_fetch(args) -> int:
    from .fetchers.siconfi import run_fetch

    import httpx

    st = settings()
    store = make_store(st)
    cfg = source_config(st.config_dir, "S4")
    try:
        with _http(st) as http:
            run_fetch(http, store, cfg, args.exercicios, args.periodos, args.anexos)
    except httpx.HTTPError as e:
        log.error("falha de rede ao acessar %s: %s", cfg["base_url"], e)
        log.error("verifique conectividade/proxy; nada foi gravado para a consulta que falhou")
        return 4
    return 0


def cmd_inspect(args) -> int:
    from .inspect_ import inspect_key

    st = settings()
    store = make_store(st)
    linhas = load_yaml(st.config_dir, "linhas.yaml")
    key = f"{args.exercicio}/{args.periodo:02d}/{args.anexo}"
    rx = None if args.todas_colunas else linhas["metrica"]["coluna_regex"]
    print(inspect_key(store, st.config_dir, key, rx, rows=args.rows))
    return 0


def cmd_transform(args) -> int:
    from .transform.siconfi import build_staging

    st = settings()
    store = make_store(st)
    build_staging(store, st.config_dir, st.staging_dir)
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
    res = compute(stg, linhas, args.exercicio, periodo)
    export = to_export(res, linhas, fontes, _estado(args.exercicio, periodo))
    p_json, p_csv, p_exp = write_outputs(export, st.marts_dir, st.export_dir)
    print_summary(export)
    print(f"\narquivos: {p_json}\n          {p_csv}\n          {p_exp}")
    if not res.publicavel:
        log.error("reconciliação crítica falhou: NÃO PUBLICÁVEL. Rode `recibo inspect siconfi` e calibre.")
        return 3
    return 0


def cmd_run(args) -> int:
    rc = cmd_fetch(argparse.Namespace(exercicios=args.exercicios, periodos=None, anexos=None))
    if rc:
        return rc
    rc = cmd_transform(args)
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
    f.add_argument("fonte", choices=["siconfi"])
    f.add_argument("--exercicios", type=int, nargs="+", default=[2024, 2025, 2026])
    f.add_argument("--periodos", type=int, nargs="+", help="bimestres (padrão: 1 a 6)")
    f.add_argument("--anexos", nargs="+", help='nomes exatos, ex.: "RREO-Anexo 02" (padrão: sources.yaml)')
    f.set_defaults(fn=cmd_fetch)

    i = sub.add_parser("inspect", help="mostra como a API devolveu uma chave e como foi classificada")
    i.add_argument("fonte", choices=["siconfi"])
    i.add_argument("--exercicio", type=int, required=True)
    i.add_argument("--periodo", type=int, required=True)
    i.add_argument("--anexo", default="RREO-Anexo 02")
    i.add_argument("--rows", type=int, default=100)
    i.add_argument("--todas-colunas", action="store_true", help="não filtrar pela coluna da métrica")
    i.set_defaults(fn=cmd_inspect)

    t = sub.add_parser("transform", help="raw -> staging (parquet)")
    t.add_argument("fonte", choices=["siconfi"])
    t.set_defaults(fn=cmd_transform)

    m = sub.add_parser("mart", help="staging -> recibo (json/csv) com reconciliações")
    m.add_argument("nome", choices=["recibo"])
    m.add_argument("--exercicio", type=int, required=True)
    m.add_argument("--periodo", type=int, help="bimestre (padrão: o último disponível no índice)")
    m.set_defaults(fn=cmd_mart)

    r = sub.add_parser("run", help="fetch + transform + mart para os exercícios")
    r.add_argument("--exercicios", type=int, nargs="+", default=[2024, 2025, 2026])
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
