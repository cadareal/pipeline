"""`recibo site`: de data/export/*.json para um site estático em docs/ (GitHub Pages serve essa pasta).

Regras:
  - só anos publicáveis viram página (reconciliação crítica falhou: o ano fica de fora, e o comando diz qual e por quê);
  - nenhum número é formatado aqui ou nos gabaritos: tudo vem pronto de dados.py;
  - sem nada carregado de fora (fontes, scripts, rastreadores): um CSS e um JS próprios, copiados para docs/static/;
  - os exports originais são copiados para docs/dados/, com um CSV simples das linhas por ano, para download.
"""

from __future__ import annotations

import csv
import json
import logging
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import markdown
import yaml
from jinja2 import Environment, FileSystemLoader, select_autoescape

from .. import __version__
from .dados import carregar_exports, dados_do_desenho, fmt_bi, fmt_cent, fmt_exato, fmt_hab, fmt_var, modelo_ano, perguntas_abertas, slug

log = logging.getLogger(__name__)

AQUI = Path(__file__).resolve().parent
TEMPLATES = AQUI / "templates"
STATIC = AQUI / "static"


@dataclass
class Relatorio:
    saida: Path
    paginas: list[str] = field(default_factory=list)
    anos: list[int] = field(default_factory=list)
    pulados: list[tuple[int, str]] = field(default_factory=list)
    avisos: list[str] = field(default_factory=list)

    def resumo(self) -> str:
        linhas = [f"site gerado em {self.saida} ({len(self.paginas)} páginas; anos: {', '.join(str(a) for a in self.anos) or 'nenhum'})"]
        for ano, motivo in self.pulados:
            linhas.append(f"  ano {ano} ficou de fora: {motivo}")
        for a in self.avisos:
            linhas.append(f"  aviso: {a}")
        return "\n".join(linhas)


def _md(texto: str) -> str:
    return markdown.markdown(texto or "", extensions=["tables", "sane_lists", "attr_list"])


def _ambiente() -> Environment:
    env = Environment(loader=FileSystemLoader(str(TEMPLATES)), autoescape=select_autoescape(["html"]), trim_blocks=True, lstrip_blocks=True)
    env.filters["md"] = _md
    env.filters["bi"] = fmt_bi
    env.filters["exato"] = fmt_exato
    env.filters["var"] = fmt_var
    env.filters["tojson_html"] = lambda d: json.dumps(d, ensure_ascii=False).replace("</", "<\\/")
    return env


def _ler_conteudo(conteudo_dir: Path) -> dict:
    cfg = yaml.safe_load((conteudo_dir / "site.yaml").read_text(encoding="utf-8")) or {}
    for nome in ("metodologia", "quem-somos"):
        p = conteudo_dir / f"{nome}.md"
        cfg[f"md_{nome.replace('-', '_')}"] = p.read_text(encoding="utf-8") if p.exists() else ""
    pq = conteudo_dir / "perguntas.yaml"
    cfg["perguntas"] = (yaml.safe_load(pq.read_text(encoding="utf-8")) or {}) if pq.exists() else {}
    return cfg


def _historico(exports: dict[int, dict], lid: str, pop_por_ano: dict[int, float | None]) -> list[dict]:
    out = []
    for ano in sorted(exports):
        e = exports[ano]
        l = next((x for x in e.get("linhas", []) if x["id"] == lid), None)
        if not l:
            continue
        pop = pop_por_ano.get(ano)
        out.append({
            "ano": ano, "periodo": e.get("periodo"), "fechado": (e.get("estado") or {}).get("selo") == "fechado",
            "valor_txt": fmt_bi(l["valor"]), "exato": fmt_exato(l["valor"]), "centavos": l.get("centavos"), "cent": fmt_cent(None, None, l.get("centavos")),
            "hab": fmt_hab(l["valor"], pop) if pop else "", "var": fmt_var(l.get("variacao_anual")),
        })
    return out


def _escrever(out_dir: Path, caminho: str, html: str, rel: Relatorio) -> None:
    p = out_dir / caminho
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(html, encoding="utf-8")
    rel.paginas.append(caminho)


def _csv_linhas(exp: dict) -> str:
    import io

    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["id", "nome", "valor_reais", "share", "centavos", "valor_ano_anterior", "variacao_anual"])
    for x in exp.get("linhas", []):
        w.writerow([x["id"], x["nome"], f"{x['valor']:.2f}", f"{x['share']:.6f}", x["centavos"], "" if x.get("anterior") is None else f"{x['anterior']:.2f}", "" if x.get("variacao_anual") is None else f"{x['variacao_anual']:.6f}"])
    return buf.getvalue()


def construir(export_dir: Path, out_dir: Path, conteudo_dir: Path, config_dir: Path, exercicios: list[int] | None = None, limpar: bool = False) -> Relatorio:
    out_dir = Path(out_dir)
    rel = Relatorio(saida=out_dir)
    conteudo = _ler_conteudo(Path(conteudo_dir))
    linhas_cfg = yaml.safe_load((Path(config_dir) / "linhas.yaml").read_text(encoding="utf-8"))
    fontes_cfg = yaml.safe_load((Path(config_dir) / "sources.yaml").read_text(encoding="utf-8"))
    exports = carregar_exports(Path(export_dir))

    recibos: dict[int, dict] = {}
    for ano, e in sorted(exports["recibo"].items()):
        if exercicios and ano not in exercicios:
            continue
        if e.get("_erro"):
            rel.pulados.append((ano, f"export ilegível: {e['_erro']}"))
            continue
        if not e.get("publicavel"):
            falhas = [r["id"] for r in e.get("reconciliacoes", []) if r.get("critica") and not r.get("ok")]
            rel.pulados.append((ano, "não publicável: conferência crítica falhou (" + ", ".join(falhas) + "); rode `recibo mart recibo --exercicio " + str(ano) + "` e investigue"))
            continue
        if not e.get("linhas"):
            rel.pulados.append((ano, "export sem linhas"))
            continue
        recibos[ano] = e
    if not recibos:
        rel.avisos.append("nenhum export publicável em " + str(export_dir) + "; nada foi gerado")
        return rel

    if limpar and out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / ".nojekyll").write_text("", encoding="utf-8")

    env = _ambiente()
    fechados = [a for a, e in recibos.items() if (e.get("estado") or {}).get("selo") == "fechado" or int(e.get("periodo") or 0) >= 6]
    atual = max(fechados) if fechados else max(recibos)
    anos = [{"ano": a, "periodo": e.get("periodo"), "fechado": a in fechados, "atual": a == atual, "caminho": f"{a}/"} for a, e in sorted(recibos.items(), reverse=True)]
    pop_por_ano = {a: (float(e["populacao"]["valor"]) if (e.get("populacao") or {}).get("disponivel") else None) for a, e in recibos.items()}
    gerado_em = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    base = {"site": conteudo, "anos": anos, "atual": atual, "gerado_em": gerado_em, "versao": __version__}

    # páginas por ano
    modelos: dict[int, dict] = {}
    for ano, e in recibos.items():
        divida = exports["divida"].get(ano)
        if divida and divida.get("_erro"):
            rel.avisos.append(f"divida_{ano}.json ilegível ({divida['_erro']}); a página sai sem as partes dos juros")
            divida = None
        m = modelo_ano(e, divida, conteudo, linhas_cfg, anos)
        modelos[ano] = m
        if not m["conferencias"]["todas_ok"]:
            rel.avisos.append(f"{ano}: {m['conferencias']['falhas']} conferência(s) informativa(s) com aviso (nenhuma crítica)")
        ctx = {**base, "m": m, "dados_json": dados_do_desenho(m), "pagina": "ano"}
        _escrever(out_dir, f"{ano}/index.html", env.get_template("ano.html").render(**ctx, raiz="../", caminho=f"{ano}/"), rel)
        if ano == atual:
            _escrever(out_dir, "index.html", env.get_template("ano.html").render(**ctx, raiz="", caminho=""), rel)
        for n in m["linhas"]:
            lid = n["id"]
            l_exp = next(x for x in e["linhas"] if x["id"] == lid)
            cfg_l = next((c for c in linhas_cfg.get("linhas", []) if c["id"] == lid), {})
            ctx_l = {
                **base, "m": m, "n": n, "linha": l_exp, "cfg_linha": cfg_l, "texto": (conteudo.get("linhas") or {}).get(lid) or {},
                "historico": _historico(recibos, lid, pop_por_ano), "pagina": "linha",
                "transferencias": [{**t, "txt": fmt_bi(t.get("valor"))} for t in (l_exp.get("transferencias") or [])],
                "niveis_completos": n,
            }
            _escrever(out_dir, f"{ano}/linha/{slug(lid)}/index.html", env.get_template("linha.html").render(**ctx_l, raiz="../../../", caminho=f"{ano}/linha/{slug(lid)}/"), rel)
        # downloads
        dados_dir = out_dir / "dados"
        dados_dir.mkdir(exist_ok=True)
        (dados_dir / f"recibo_{ano}.json").write_text(json.dumps(e, ensure_ascii=False, indent=1), encoding="utf-8")
        (dados_dir / f"recibo_{ano}.csv").write_text(_csv_linhas(e), encoding="utf-8")
        if divida:
            (dados_dir / f"divida_{ano}.json").write_text(json.dumps(divida, ensure_ascii=False, indent=1), encoding="utf-8")
        cof = exports["cofog"].get(ano)
        if cof and not cof.get("_erro"):
            (dados_dir / f"cofog_{ano}.json").write_text(json.dumps(cof, ensure_ascii=False, indent=1), encoding="utf-8")

    # páginas fixas
    m_atual = modelos[atual]
    glossario = list(conteudo.get("glossario") or [])
    for bloco in ("divida", "populacao", "cofog"):
        for g in (linhas_cfg.get(bloco) or {}).get("glossario") or []:
            glossario.append({"termo": g.get("termo"), "explica": " ".join(str(g.get("texto") or g.get("explica") or "").split()), "bloco": bloco})
    fontes = []
    for f in fontes_cfg.get("fontes", []):
        if f.get("status") in ("ativo", "implementado") or f.get("fetcher"):
            fontes.append({"id": f["id"], "nome": f.get("nome"), "orgao": f.get("orgao"), "url": f.get("base_url"), "formato": f.get("formato"), "cadencia": f.get("cadencia")})
    ctx = {**base, "m": m_atual, "glossario": glossario, "fontes": fontes, "pagina": "metodologia", "reconciliacoes_cfg": linhas_cfg.get("reconciliacoes", [])}
    _escrever(out_dir, "metodologia/index.html", env.get_template("metodologia.html").render(**ctx, raiz="../", caminho="metodologia/"), rel)
    _escrever(out_dir, "quem-somos/index.html", env.get_template("quem_somos.html").render(**base, m=m_atual, pagina="quem-somos", raiz="../", caminho="quem-somos/"), rel)
    if conteudo.get("perguntas"):
        div_atual = exports["divida"].get(atual)
        p_ab = perguntas_abertas(conteudo, recibos.get(atual), None if (div_atual or {}).get("_erro") else div_atual, atual)
        _escrever(out_dir, "perguntas-abertas/index.html", env.get_template("perguntas.html").render(**base, m=m_atual, p=p_ab, pagina="perguntas-abertas", raiz="../", caminho="perguntas-abertas/"), rel)
    arquivos = []
    for ano in sorted(recibos, reverse=True):
        arquivos.append({"ano": ano, "recibo_json": f"dados/recibo_{ano}.json", "recibo_csv": f"dados/recibo_{ano}.csv",
                         "divida_json": f"dados/divida_{ano}.json" if (out_dir / "dados" / f"divida_{ano}.json").exists() else None,
                         "cofog_json": f"dados/cofog_{ano}.json" if (out_dir / "dados" / f"cofog_{ano}.json").exists() else None,
                         "lacres": modelos[ano]["lacres"], "gerado_em": modelos[ano]["gerado_em"], "versao_spec": modelos[ano]["versao_spec"]})
    _escrever(out_dir, "dados/index.html", env.get_template("dados.html").render(**base, m=m_atual, arquivos=arquivos, fontes=fontes, pagina="dados", raiz="../", caminho="dados/"), rel)

    # estáticos
    (out_dir / "static").mkdir(exist_ok=True)
    for p in STATIC.iterdir():
        if p.is_file():
            shutil.copy2(p, out_dir / "static" / p.name)
    # a tubulação do domínio: CNAME (o GitHub Pages guarda o domínio aqui; sem ele, o --limpar derruba o site do domínio),
    # robots.txt, sitemap.xml com todas as páginas e a página 404 (links absolutos, porque ela é servida de qualquer caminho)
    url_base = str(conteudo.get("url") or "").strip()
    if url_base and not url_base.endswith("/"):
        url_base += "/"
    dominio = str(conteudo.get("dominio") or "").strip()
    if dominio:
        (out_dir / "CNAME").write_text(dominio + "\n", encoding="utf-8")
    (out_dir / "robots.txt").write_text("User-agent: *\nAllow: /\n" + (f"Sitemap: {url_base}sitemap.xml\n" if url_base else ""), encoding="utf-8")
    (out_dir / "404.html").write_text(env.get_template("404.html").render(**base, m=m_atual, pagina="404", raiz=url_base or "/", caminho="404.html"), encoding="utf-8")
    if url_base:
        hoje = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        urls = []
        for pg in rel.paginas:
            loc = url_base + (pg[: -len("index.html")] if pg.endswith("index.html") else pg)
            urls.append(f"  <url><loc>{loc}</loc><lastmod>{hoje}</lastmod></url>")
        (out_dir / "sitemap.xml").write_text('<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n' + "\n".join(urls) + "\n</urlset>\n", encoding="utf-8")
    rel.anos = sorted(recibos)
    return rel
