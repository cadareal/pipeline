"""CLI: recibo fetch | inspect | transform | mart (recibo, cofog, divida) | diff | run | site (fontes: siconfi S4, siop S8, cofog S7, divida S1, bcb S16, ibge S14)"""

from __future__ import annotations

import argparse
import logging
import sys

from . import __version__
from .config import load_yaml, settings, source_config
from .http import Http
from .store import make_store
from .textnorm import norm

log = logging.getLogger("recibo")

FONTES = {"siconfi": "S4", "siop": "S8", "cofog": "S7", "divida": "S1", "bcb": "S16", "ibge": "S14"}
EXERCICIOS_PADRAO = [2024, 2025, 2026]


def _http(st, timeout: float | None = None, ipv4: bool = False) -> Http:
    verify: bool | str = st.ca_bundle if st.ca_bundle else True
    return Http(user_agent=st.user_agent, timeout=timeout or st.timeout_seconds, pause=st.pause_seconds, verify=verify, ipv4=ipv4)


def cmd_fetch(args) -> int:
    import httpx

    st = settings()
    store = make_store(st)
    cfg = source_config(st.config_dir, FONTES[args.fonte])
    # consultas agregadas do SIOP podem levar dezenas de segundos: o timeout HTTP vem de sources.yaml (timeout_http_s)
    timeout = max(st.timeout_seconds, float(cfg.get("timeout_http_s", 0) or 0))
    exercicios = args.exercicios or (None if args.fonte in ("cofog", "divida", "bcb", "ibge") else EXERCICIOS_PADRAO)  # cofog: todos os anos do CKAN; divida, bcb e ibge: séries inteiras
    try:
        with _http(st, timeout=timeout, ipv4=bool(cfg.get("forcar_ipv4", False))) as http:
            if args.fonte == "siconfi":
                from .fetchers.siconfi import run_fetch

                run_fetch(http, store, cfg, exercicios, args.periodos, args.anexos)
            elif args.fonte == "cofog":
                from .fetchers.cofog import run_fetch as run_fetch_cofog

                results = run_fetch_cofog(http, store, cfg, exercicios, getattr(args, "force", False))
                if any(r.status == "erro" for r in results):
                    log.error("fetch cofog terminou com anos em erro (veja o log e o _index.json)")
                    return 5
            elif args.fonte == "divida":
                from .fetchers.divida import run_fetch as run_fetch_divida

                results = run_fetch_divida(http, store, cfg, getattr(args, "datasets", None), getattr(args, "force", False))
                if any(r.status == "erro" for r in results):
                    log.error("fetch divida terminou com recursos em erro (veja o log e o _index.json de cada dataset)")
                    return 5
            elif args.fonte == "bcb":
                from .fetchers.bcb import run_fetch as run_fetch_bcb

                results = run_fetch_bcb(http, store, cfg, getattr(args, "series", None), getattr(args, "force", False))
                if any(r.status == "erro" for r in results):
                    log.error("fetch bcb terminou com séries em erro (veja o log e o _index.json)")
                    return 5
            elif args.fonte == "ibge":
                from .fetchers.ibge import run_fetch as run_fetch_ibge

                results = run_fetch_ibge(http, store, cfg, getattr(args, "force", False))
                if any(r.status == "erro" and not r.opcional for r in results):
                    log.error("fetch ibge terminou com erro na projeção ou nas estimativas (veja o log); de fora do Brasil o IBGE pode barrar: o raw já lacrado continua valendo")
                    return 5
            else:
                from .fetchers.siop import run_fetch as run_fetch_siop

                results = run_fetch_siop(http, store, cfg, exercicios, getattr(args, "agregacoes", None), getattr(args, "force", False))
                if any(r.status == "erro" for r in results):
                    log.error("fetch siop terminou com consultas em erro (veja o log e o _index.json); rode de novo com --force depois")
                    return 5
    except httpx.HTTPError as e:
        log.error("falha de rede ao acessar %s: %s", cfg["base_url"], e)
        log.error("verifique conectividade/proxy; nada foi gravado para a consulta que falhou")
        return 4
    return 0


def _emit(texto: str, out: str | None) -> None:
    """Imprime e, com --out, grava o mesmo texto num arquivo (o terminal corta saídas longas; o arquivo não)."""
    print(texto)
    if out:
        from pathlib import Path

        Path(out).expanduser().write_text(texto + "\n", encoding="utf-8")
        log.info("saída gravada em %s", out)


def cmd_inspect(args) -> int:
    import re

    st = settings()
    store = make_store(st)
    if args.fonte in ("siconfi", "siop", "cofog") and args.exercicio is None:
        log.error("inspect %s exige --exercicio", args.fonte)
        return 2
    if args.fonte == "siconfi":
        from .inspect_ import inspect_key

        if args.periodo is None:
            log.error("inspect siconfi exige --periodo")
            return 2
        linhas = load_yaml(st.config_dir, "linhas.yaml")
        key = f"{args.exercicio}/{args.periodo:02d}/{args.anexo}"
        if args.coluna:
            # texto literal (não regex), comparado com a coluna normalizada: `--coluna "ATE O BIMESTRE (C)"` acha a
            # coluna de receitas realizadas do Anexo 01 sem escapar parênteses
            rx = re.escape(norm(args.coluna))
        else:
            rx = None if args.todas_colunas else linhas["metrica"]["coluna_regex"]
        _emit(inspect_key(store, st.config_dir, key, rx, rows=args.rows), args.out)
    elif args.fonte == "cofog":
        from .inspect_cofog import inspect_cofog
        from .transform.cofog import STAGING_FILE as COFOG_FILE

        _emit(inspect_cofog(st.staging_dir / COFOG_FILE, args.exercicio, por=args.por, rows=args.rows, cofog=args.cofog, config_dir=st.config_dir, store=store), args.out)
    elif args.fonte == "divida":
        from .inspect_divida import inspect_divida

        _emit(inspect_divida(st.staging_dir, dataset=getattr(args, "dataset", None), rows=args.rows, ano=getattr(args, "ano", None), filtros=getattr(args, "filtro", None)), args.out)
    elif args.fonte == "bcb":
        from .inspect_bcb import inspect_bcb
        from .transform.bcb import STAGING_FILE as BCB_FILE

        _emit(inspect_bcb(st.staging_dir / BCB_FILE, serie=getattr(args, "serie", None), ano=getattr(args, "ano", None), rows=args.rows), args.out)
    elif args.fonte == "ibge":
        from .inspect_ibge import inspect_ibge
        from .transform.ibge import STAGING_FILE as IBGE_FILE

        _emit(inspect_ibge(st.staging_dir / IBGE_FILE, local=getattr(args, "local", None) or "Brasil", desde=getattr(args, "ano", None), rows=args.rows), args.out)
    else:
        from .inspect_siop import inspect_siop

        _emit(inspect_siop(store, args.exercicio, args.agregacao, args.funcao, rows=args.rows), args.out)
    return 0


def cmd_transform(args) -> int:
    st = settings()
    store = make_store(st)
    if args.fonte == "siconfi":
        from .transform.siconfi import build_staging

        build_staging(store, st.config_dir, st.staging_dir)
    elif args.fonte == "cofog":
        from .transform.cofog import CofogFormato, build_staging as build_staging_cofog

        try:
            build_staging_cofog(store, st.staging_dir, source_config(st.config_dir, FONTES["cofog"]))
        except CofogFormato as e:
            log.error("transform cofog parou: %s", e)
            log.error("nada foi gravado no staging; o raw continua intacto")
            return 6
    elif args.fonte == "divida":
        from .transform.divida import build_staging as build_staging_divida

        build_staging_divida(store, st.staging_dir, source_config(st.config_dir, FONTES["divida"]))
    elif args.fonte == "bcb":
        from .transform.bcb import build_staging as build_staging_bcb
        from .transform.cofog import CofogFormato

        try:
            build_staging_bcb(store, st.staging_dir, source_config(st.config_dir, FONTES["bcb"]))
        except CofogFormato as e:
            log.error("transform bcb parou: %s", e)
            log.error("nada foi gravado no staging; o raw continua intacto")
            return 6
    elif args.fonte == "ibge":
        from .transform.ibge import IbgeFormato, build_staging as build_staging_ibge

        try:
            build_staging_ibge(store, st.staging_dir, source_config(st.config_dir, FONTES["ibge"]), load_yaml(st.config_dir, "linhas.yaml"))
        except IbgeFormato as e:
            log.error("transform ibge parou: %s", e)
            log.error("nada foi gravado no staging; o raw continua intacto")
            return 6
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
        return {"selo": "fechado", "nota": "6º bimestre: exercício completo (RREO)."}
    return {"selo": "em_andamento", "nota": f"Dados até o {periodo}º bimestre de {exercicio} (RREO). Atualização a cada bimestre."}


def _siop_staging(st):
    from .transform.siop import STAGING_FILE as SIOP_FILE

    p = st.staging_dir / SIOP_FILE
    return p if p.exists() else None


def _ibge_staging(st):
    from .transform.ibge import STAGING_FILE as IBGE_FILE

    p = st.staging_dir / IBGE_FILE
    if not p.exists():
        log.info("staging do IBGE ausente: memos por habitante saem como indisponíveis (rode `recibo fetch ibge` e `recibo transform ibge`)")
        return None
    return p


def cmd_mart_cofog(args) -> int:
    """Produto separado do recibo: a despesa do Governo Central pela régua internacional, com a ponte para o recibo."""
    from .marts.cofog import compute_cofog, print_summary_cofog, to_export_cofog, write_outputs_cofog
    from .marts.recibo import SemDados
    from .transform.cofog import STAGING_FILE as COFOG_FILE
    from .transform.siconfi import STAGING_FILE

    st = settings()
    store = make_store(st)
    stg_cofog = st.staging_dir / COFOG_FILE
    if not stg_cofog.exists():
        log.error("staging do COFOG não encontrado (%s); rode `recibo fetch cofog` e `recibo transform cofog`", stg_cofog)
        return 2
    stg = st.staging_dir / STAGING_FILE
    periodo = _periodo_disponivel(store, args.exercicio) if stg.exists() else None
    linhas = load_yaml(st.config_dir, "linhas.yaml")
    fontes = load_yaml(st.config_dir, "sources.yaml")
    try:
        res = compute_cofog(stg_cofog, linhas, fontes, args.exercicio, stg_siconfi=stg if stg.exists() else None, periodo_recibo=periodo, siop_path=_siop_staging(st), config_dir=st.config_dir)
    except SemDados as e:
        log.error("%s", e)
        return 2
    export = to_export_cofog(res, linhas)
    p_json, p_csv, p_exp = write_outputs_cofog(export, st.marts_dir, st.export_dir)
    import contextlib
    import io

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        print_summary_cofog(export)
        print(f"\narquivos: {p_json}\n          {p_csv}\n          {p_exp}")
    _emit(buf.getvalue().rstrip("\n"), getattr(args, "out", None))
    if not res.publicavel:
        log.error("COFOG %s: NÃO PUBLICÁVEL (%s). O recibo não é afetado.", args.exercicio, res.selo)
        return 3
    return 0


def cmd_mart_divida(args) -> int:
    """Produto separado do recibo: a dívida em quatro medidas, cada uma com suas réguas nomeadas."""
    from .marts.divida import compute_divida, print_summary_divida, to_export_divida, write_outputs_divida
    from .marts.recibo import SemDados
    from .transform.siconfi import STAGING_FILE

    st = settings()
    store = make_store(st)
    stg = st.staging_dir / STAGING_FILE
    periodo = args.periodo or (_periodo_disponivel(store, args.exercicio) if stg.exists() else None)
    linhas = load_yaml(st.config_dir, "linhas.yaml")
    fontes = load_yaml(st.config_dir, "sources.yaml")
    try:
        res = compute_divida(st.staging_dir, linhas, fontes, args.exercicio, stg_siconfi=stg if stg.exists() else None, periodo_recibo=periodo, siop_path=_siop_staging(st), config_dir=st.config_dir, stg_ibge=_ibge_staging(st))
    except SemDados as e:
        log.error("%s", e)
        return 2
    export = to_export_divida(res, linhas)
    p_json, p_csv, p_exp = write_outputs_divida(export, st.marts_dir, st.export_dir)
    import contextlib
    import io

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        print_summary_divida(export)
        print(f"\narquivos: {p_json}\n          {p_csv}\n          {p_exp}")
    _emit(buf.getvalue().rstrip("\n"), getattr(args, "out", None))
    if not res.publicavel:
        log.error("dívida %s: NÃO PUBLICÁVEL (%s). O recibo não é afetado.", args.exercicio, res.selo)
        return 3
    return 0


def cmd_mart(args) -> int:
    if args.nome == "cofog":
        return cmd_mart_cofog(args)
    if args.nome == "divida":
        return cmd_mart_divida(args)
    from .marts.recibo import SemDados, compute, print_summary, to_export, write_outputs
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
    try:
        res = compute(stg, linhas, args.exercicio, periodo, siop_path=siop, config_dir=st.config_dir, stg_ibge=_ibge_staging(st))
    except SemDados as e:
        log.error("%s", e)
        return 2
    export = to_export(res, linhas, fontes, _estado(args.exercicio, periodo))
    p_json, p_csv, p_exp = write_outputs(export, st.marts_dir, st.export_dir)
    import contextlib
    import io

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        print_summary(export)
        print(f"\narquivos: {p_json}\n          {p_csv}\n          {p_exp}")
    _emit(buf.getvalue().rstrip("\n"), getattr(args, "out", None))
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
    from .marts.recibo import SemDados

    try:
        d = diff_periodos(stg, linhas, Periodo(args.exercicio, periodo), Periodo(args.contra, periodo_contra))
    except SemDados as e:
        log.error("%s", e)
        return 2
    _emit(render(d, top=args.top, funcoes=args.funcao, ordem=args.ordem), getattr(args, "out_txt", None))
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
    if not args.sem_ibge:
        # população: falha macia. Se o IBGE não responder (de fora do Brasil ele barra parte dos servidores), os recibos
        # saem com os memos por habitante indisponíveis, ou com o staging anterior se ele existir.
        rc = cmd_fetch(argparse.Namespace(fonte="ibge", exercicios=None, force=False))
        if rc:
            log.warning("fetch ibge falhou (código %s); seguindo com o que há no raw", rc)
        try:
            rc = cmd_transform(argparse.Namespace(fonte="ibge"))
        except SystemExit as e:
            log.warning("transform ibge não rodou (%s); recibos saem sem per capita", e)
            rc = 0
        if rc:
            log.warning("transform ibge falhou (código %s); recibos saem sem per capita", rc)
    worst = 0
    for ex in args.exercicios:
        rc = cmd_mart(argparse.Namespace(nome="recibo", exercicio=ex, periodo=None, out=None))
        worst = max(worst, rc)
    return worst


def cmd_site(args) -> int:
    """data/export/*.json -> site estático em docs/ (GitHub Pages). Só anos publicáveis viram página."""
    from pathlib import Path

    from .config import REPO_ROOT
    from .site.build import construir

    st = settings()
    out = Path(args.out).expanduser() if args.out else REPO_ROOT / "docs"
    conteudo = Path(args.conteudo).expanduser() if args.conteudo else REPO_ROOT / "conteudo"
    if not (conteudo / "site.yaml").exists():
        log.error("pasta de conteúdo sem site.yaml: %s", conteudo)
        return 2
    rel = construir(st.export_dir, out, conteudo, st.config_dir, exercicios=args.exercicios, limpar=bool(args.limpar))
    print(rel.resumo())
    if not rel.anos:
        return 3
    print(f"\npara ver no navegador: python -m http.server -d {out} 8000  (depois abra http://localhost:8000/)")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="recibo", description="Pipeline aberto: de cada R$ 1,00 de imposto, quantos centavos foram para onde.")
    p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument("--version", action="version", version=f"recibo {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    f = sub.add_parser("fetch", help="baixa dados de uma fonte para a camada raw")
    f.add_argument("fonte", choices=sorted(FONTES))
    f.add_argument("--exercicios", type=int, nargs="+", default=None, help="padrão: 2024 2025 2026 (siconfi, siop); cofog: todos os anos do CKAN; divida e bcb ignoram")
    f.add_argument("--datasets", nargs="+", help="divida: nomes em sources.yaml (S1.datasets), ex.: estoque_dpf fatores_variacao (padrão: todos)")
    f.add_argument("--series", type=int, nargs="+", help="bcb: códigos do SGS (padrão: todas as de sources.yaml)")
    f.add_argument("--periodos", type=int, nargs="+", help="siconfi: bimestres (padrão: 1 a 6)")
    f.add_argument("--anexos", nargs="+", help='siconfi: nomes exatos, ex.: "RREO-Anexo 02" (padrão: sources.yaml)')
    f.add_argument("--agregacoes", nargs="+", help="siop: ids de agregação em sources.yaml (padrão: todas)")
    f.add_argument("--force", action="store_true", help="siop: refaz mesmo sem mudança na data de atualização do grafo; cofog e divida: baixam de novo mesmo sem mudança no CKAN; bcb e ibge ignoram (sempre baixam inteiro)")
    f.set_defaults(fn=cmd_fetch)

    i = sub.add_parser("inspect", help="mostra como a API devolveu uma chave e como foi classificada")
    i.add_argument("fonte", choices=sorted(FONTES))
    i.add_argument("--exercicio", type=int, help="siconfi, siop e cofog: obrigatório; divida, bcb e ibge não usam")
    i.add_argument("--periodo", type=int, help="siconfi: bimestre (obrigatório)")
    i.add_argument("--anexo", default="RREO-Anexo 02", help="siconfi: nome do anexo")
    i.add_argument("--agregacao", default="acao_subfuncao", help="siop: id da agregação (padrão: acao_subfuncao)")
    i.add_argument("--funcao", help="siop: restringe a uma função, ex.: 28")
    i.add_argument("--rows", type=int, default=100)
    i.add_argument("--todas-colunas", action="store_true", help="siconfi: não filtrar pela coluna da métrica")
    i.add_argument("--coluna", help='siconfi: filtrar por esta coluna (texto literal, ex.: "ATE O BIMESTRE (C)" para receitas realizadas)')
    i.add_argument("--por", default="divisao", help="cofog: divisao | grupo | classe | gfs | gnd | ajuste | uo | acao | natureza | anos")
    i.add_argument("--cofog", help="cofog: restringe a um código COFOG (prefixo), ex.: 10.2 ou 017")
    i.add_argument("--dataset", help="divida: só esse dataset (nome em sources.yaml), ex.: estoque_dpf")
    i.add_argument("--filtro", action="append", help='divida (CSV): recorta linhas, COLUNA=VALOR (texto exato); repetível, ex.: --filtro "Mes do Estoque=12/2024"')
    i.add_argument("--serie", type=int, help="bcb: só essa série (código do SGS)")
    i.add_argument("--ano", type=int, help="bcb: tabela de todas as séries nesse ano; divida (XLSX): todas as linhas nas colunas desse ano; ibge: réguas desde esse ano")
    i.add_argument("--local", help="ibge: Brasil (padrão), uma região ou UF pelo nome da planilha, ex.: 'São Paulo'")
    i.add_argument("--out", help="grava a mesma saída neste arquivo (o terminal corta saídas longas)")
    i.set_defaults(fn=cmd_inspect)

    t = sub.add_parser("transform", help="raw -> staging (parquet)")
    t.add_argument("fonte", choices=sorted(FONTES))
    t.set_defaults(fn=cmd_transform)

    m = sub.add_parser("mart", help="staging -> recibo (json/csv) com reconciliações; cofog: régua internacional com a ponte para o recibo; divida: quatro medidas da dívida")
    m.add_argument("nome", choices=["recibo", "cofog", "divida"])
    m.add_argument("--exercicio", type=int, required=True)
    m.add_argument("--periodo", type=int, help="recibo: bimestre (padrão: o último disponível no índice); cofog ignora (ano fechado)")
    m.add_argument("--out", help="grava o resumo do terminal neste arquivo (além do json/csv de sempre)")
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
    d.add_argument("--out-txt", dest="out_txt", help="grava o texto do terminal neste arquivo")
    d.set_defaults(fn=cmd_diff)

    r = sub.add_parser("run", help="fetch + transform (siconfi, siop e ibge) + mart para os exercícios")
    r.add_argument("--exercicios", type=int, nargs="+", default=[2024, 2025, 2026])
    r.add_argument("--sem-siop", action="store_true", help="não busca nem transforma o SIOP nesta execução")
    r.add_argument("--sem-ibge", action="store_true", help="não busca nem transforma a população do IBGE nesta execução")
    r.set_defaults(fn=cmd_run)

    s_ = sub.add_parser("site", help="gera o site estático (docs/) a partir de data/export/*.json; só anos publicáveis viram página")
    s_.add_argument("--out", help="pasta de saída (padrão: docs/ na raiz do repositório, a que o GitHub Pages serve)")
    s_.add_argument("--conteudo", help="pasta com site.yaml, metodologia.md e quem-somos.md (padrão: conteudo/)")
    s_.add_argument("--exercicios", type=int, nargs="+", help="só estes anos (padrão: todos os exports publicáveis)")
    s_.add_argument("--limpar", action="store_true", help="apaga a pasta de saída antes de gerar (remove páginas de anos que deixaram de existir)")
    s_.set_defaults(fn=cmd_site)

    args = p.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
    )
    return int(args.fn(args) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
