"""`recibo site`: do export sintético (com SIOP e dívida) para as páginas. Confere que as páginas existem, que os
números do HTML são os do export (formatados em Python, uma vez só), que os links internos resolvem e que nada
vazou sem formatar (None, nan, undefined)."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from recibo.config import REPO_ROOT, load_yaml
from recibo.marts.divida import compute_divida, to_export_divida, write_outputs_divida
from recibo.marts.recibo import compute, to_export, write_outputs
from recibo.site.build import construir
from recibo.site.dados import carregar_exports, dados_do_desenho, fmt_bi, fmt_cent, fmt_exato, fmt_hab, fmt_pct, modelo_ano, slug
from recibo.transform.siconfi import build_staging as build_staging_rreo
from recibo.transform.siop import build_staging
from test_mart_divida import staging_divida  # noqa: F401 - staging sintético com dívida, BCB e RREO

CONTEUDO = REPO_ROOT / "conteudo"


def ano_em_andamento(exp: dict, divida: dict, ano: int = 2026, periodo: int = 4, mes_divida: int = 7) -> tuple[dict, dict]:
    """Deriva de um export fechado o que o pipeline produz num ano em andamento: RREO até o 4º bimestre, sem SIOP
    (sem detalhe por ação, sem carimbo da fonte), dívida com a posição do último mês publicado. Serve para conferir que
    o site trata o ano aberto igual em todas as visões (nada em branco sem aviso), sem precisar de dados reais."""
    e = json.loads(json.dumps(exp))
    e["exercicio"], e["periodo"] = ano, periodo
    e["estado"] = {"selo": "em_andamento", "nota": f"Dados até o {periodo}º bimestre de {ano} (RREO). Atualização a cada bimestre."}
    e["fontes_de_recursos"] = {"disponivel": False, "motivo": "ano em andamento: o carimbo da fonte (SIOP) só é comparável no ano fechado"}
    e["siop"] = {"disponivel": False, "usado_nas_linhas": False}
    for l in e["linhas"]:
        l["niveis"] = {"tipo": None, "itens": [], "nao_alocado": [], "soma": 0.0, "fecha": True, "diferenca": 0.0, "fonte": "", "acoes_fonte": ""}
    d = json.loads(json.dumps(divida))
    d["exercicio"], d["fechado"], d["mes_referencia"] = ano, False, mes_divida
    for bloco in ("estoque_detalhe", "conta_do_ano"):
        if d.get(bloco):
            d[bloco]["exercicio"], d[bloco]["fechado"] = ano, False
    if d.get("estoque_detalhe"):
        d["estoque_detalhe"]["mes"] = mes_divida
    if d.get("conta_do_ano"):
        d["conta_do_ano"]["meses"] = list(range(1, mes_divida + 1))
    if d.get("execucao"):
        d["execucao"]["meses"], d["execucao"]["ultimo_mes_com_dados"] = mes_divida, mes_divida
    return e, d


@pytest.fixture
def exports(populated_store_siop, staging_divida, config_dir, tmp_path):  # noqa: F811
    """Exports de 2024 (recibo com SIOP e níveis; dívida com execução por carteira) numa pasta data/export."""
    stg = build_staging_rreo(populated_store_siop, config_dir, tmp_path / "staging")
    siop = build_staging(populated_store_siop, tmp_path / "staging")
    linhas = load_yaml(config_dir, "linhas.yaml")
    fontes = load_yaml(config_dir, "sources.yaml")
    res = compute(stg, linhas, 2024, 6, siop_path=siop, config_dir=config_dir)
    exp = to_export(res, linhas, fontes, {"selo": "fechado", "nota": "6º bimestre: exercício completo (RREO)."})
    data = tmp_path / "data"
    write_outputs(exp, data / "marts", data / "export")
    res_d = compute_divida(staging_divida, linhas, fontes, 2024, stg_siconfi=staging_divida / "stg_siconfi_rreo.parquet", periodo_recibo=6, config_dir=config_dir)
    write_outputs_divida(to_export_divida(res_d, linhas), data / "marts", data / "export")
    return data / "export", exp


def test_formatacao_pt_br():
    assert fmt_bi(3222600839437.40) == "R$ 3,22 tri" and fmt_bi(990184611011.60) == "R$ 990,2 bi" and fmt_bi(282925018.14) == "R$ 282,9 mi"
    assert fmt_bi(12345.6) == "R$ 12 mil" and fmt_bi(350.0) == "R$ 350,00" and fmt_bi(-1.5e9) == "-R$ 1,5 bi" and fmt_bi(None) == "indisponível"
    assert fmt_exato(990184611011.60) == "R$ 990.184.611.011,60" and fmt_exato(-0.5) == "-R$ 0,50"
    assert fmt_cent(363.5e9, 3222.6e9) == "11 centavos" and fmt_cent(0.28e9, 3222.6e9) == "menos de 0,1 centavo"
    assert fmt_cent(32.6e9, 3222.6e9) == "1,0 centavo" and fmt_cent(93.1e9, 363.5e9) == "26 centavos" and fmt_cent(None, None, 31) == "31 centavos" and fmt_cent(None, None, 1) == "1 centavo"
    assert fmt_pct(990184611011.60, 3222600839437.40) == "30,7%" and fmt_pct(1, 0) == ""
    assert fmt_hab(3222600839437.40, 213421037) == "R$ 15.100 por habitante" and fmt_hab(50.0, 213421037) == "R$ 0,00 por habitante" and fmt_hab(1.0, None) == ""
    assert slug("previdencia_inss") == "previdencia-inss" and slug("Defesa e Segurança") == "defesa-e-seguran-a"


def test_modelo_do_ano_usa_so_o_export(exports, config_dir):
    export_dir, exp = exports
    ex = carregar_exports(export_dir)
    assert set(ex["recibo"]) == {2024} and set(ex["divida"]) == {2024}
    conteudo = __import__("yaml").safe_load((CONTEUDO / "site.yaml").read_text(encoding="utf-8"))
    linhas_cfg = load_yaml(config_dir, "linhas.yaml")
    m = modelo_ano(ex["recibo"][2024], ex["divida"][2024], conteudo, linhas_cfg, [{"ano": 2024, "fechado": True, "atual": True, "caminho": "2024/"}])
    assert m["ano"] == 2024 and m["fechado"] and m["total"] == exp["total_recibo"] and m["total_exato"] == fmt_exato(exp["total_recibo"])
    assert [v["id"] for v in m["visoes"]] == ["caixa", "gasto", "divida"] and m["visao_inicial"] == "caixa" and m["divida_visao"]["total"] == pytest.approx(7_359.5e9)
    # visão do gasto: dez linhas com os centavos oficiais do export, em ordem de tamanho; as três origens somam o total
    gasto = m["visoes"][1]
    assert len(gasto["saidas"]) == 10 and gasto["saidas"] == sorted(gasto["saidas"], key=lambda n: -n["valor"])
    por = {n["id"]: n for n in gasto["saidas"]}
    for l in exp["linhas"]:
        assert por[l["id"]]["cent"] == fmt_cent(None, None, l["centavos"]) and por[l["id"]]["valor"] == l["valor"] and por[l["id"]]["pagina"] == f"linha/{slug(l['id'])}/"
    assert sum(n["valor"] for n in gasto["entradas"]) == pytest.approx(exp["total_recibo"])
    assert [n["id"] for n in gasto["entradas"]] == ["tributos", "outras_primarias", "divida_ou_caixa"]
    assert [f["id"] for f in por["saude"]["filhos"]] == ["10-301", "10-302"] and por["saude"]["filhos"][0]["filhos"][0]["id"].startswith("acao-")
    assert por["saude"]["filhos"][0]["filhos"][0]["tipo"] == "acao" and por["saude"]["filhos"][0]["filhos"][0]["hab"] == ""
    assert por["juros"]["filhos"] and {f["id"] for f in por["juros"]["filhos"]} == {"carteira-mercado", "carteira-banco_central", "carteira-cobertura_bc"}
    assert por["maquina_demais"]["filhos"][0]["id"] == "funcao-28" and por["maquina_demais"]["nao_alocado"]
    assert por["inativos_pensionistas"]["filhos"][0]["id"] == "elemento-01" and por["inativos_pensionistas"]["filhos"][0]["filhos"][0]["id"].startswith("orgao-")
    # visão do caixa: entradas e saídas fecham com a faixa da diferença; a rolagem aparece dos dois lados
    caixa = m["visoes"][0]
    ent, sai = sum(n["valor"] for n in caixa["entradas"]), sum(n["valor"] for n in caixa["saidas"])
    assert ent == pytest.approx(sai) and caixa["total"] == pytest.approx(max(ent, sai))
    ids_e, ids_s = [n["id"] for n in caixa["entradas"]], [n["id"] for n in caixa["saidas"]]
    assert ids_e[:2] == ["receita", "titulos"] and ids_s[:2] == ["gasto", "divida_paga"] and ("caixa_guardado" in ids_s or "caixa_usado" in ids_e)
    r01 = exp["anexo01_receitas"]
    titulos = next(n for n in caixa["entradas"] if n["id"] == "titulos")
    assert titulos["valor"] == pytest.approx(r01["operacoes_credito"] + r01["refinanciamento"])
    assert {f["id"] for f in titulos["filhos"]} == {"rolagem_entrada", "divida_nova"}
    dp = next(n for n in caixa["saidas"] if n["id"] == "divida_paga")
    assert dp["valor"] == pytest.approx(exp["anexo01_referencias"]["amortizacao"] + exp["anexo01_referencias"]["refinanciamento"])
    assert {f["id"] for f in dp["filhos"]} == {"carteira-mercado", "carteira-banco_central", "carteira-cobertura_bc"}
    g = next(n for n in caixa["saidas"] if n["id"] == "gasto")
    assert g["valor"] == exp["total_recibo"] and [f["id"] for f in g["filhos"]] == [n["id"] for n in gasto["saidas"]]
    assert "R$ 1,00 de título vendido" in caixa["frase_rolagem"] and caixa["por_fonte"]
    # o outro lado dos zooms na visão do caixa é o carimbo da fonte (SIOP), nunca rateio
    dest = {d["id"].split("-dest-")[-1] if "-dest-" in d["id"] else d["id"]: d for d in titulos["outro_lado"]}
    assert dest["rolagem"]["valor"] == 1500.0 and dest["juros"]["valor"] == 350.0 and dest["principal"]["valor"] == 8.0
    assert dest["titulos-sobra"]["valor"] == pytest.approx(titulos["valor"] - 1858.0) and dest["titulos-sobra"]["tracejado"]
    assert sum(d["valor"] for d in titulos["outro_lado"]) == pytest.approx(titulos["valor"])
    rol = next(f for f in titulos["filhos"] if f["id"] == "rolagem_entrada")
    assert [d["valor"] for d in rol["outro_lado"] if d["id"].endswith("-dest-rolagem")] == [1500.0]
    orig = {o["id"].split("-orig-")[-1]: o for o in g["outro_lado"]}
    assert orig["receita-corrente"]["valor"] == 2048.0 and orig["titulos_novos-corrente"]["valor"] == 350.0 and "titulos_refinanciamento-corrente" not in orig
    assert {o["id"].split("-orig-")[-1] for o in dp["outro_lado"]} == {"receita-corrente", "titulos_refinanciamento-corrente", "titulos_novos-corrente"}
    # v0.10.3: a faixa de carimbo termina, mas aponta para a faixa de verdade ("Ir para"); a sobra calculada e as fontes de
    # anos anteriores não apontam para nada
    rec = next(n for n in caixa["entradas"] if n["id"] == "receita")
    dest_rec = {d_["id"].split("-dest-")[-1]: d_ for d_ in rec["outro_lado"] if "-dest-" in d_["id"]}
    assert dest_rec["primaria"]["ir_para"] == {"id": "gasto", "nome": g["nome"]} and dest["juros"]["ir_para"]["id"] == "juros" and dest["rolagem"]["ir_para"]["id"] == "divida_paga"
    assert orig["receita-corrente"]["ir_para"] == {"id": "receita", "nome": rec["nome"]} and orig["titulos_novos-corrente"]["ir_para"]["id"] == "titulos"
    assert "ir_para" not in dest["titulos-sobra"] and all("ir_para" not in o for o in g["outro_lado"] if o["id"].endswith("-anteriores") or o["id"].endswith("-outros"))
    assert dest_rec["primaria"]["proximo"].startswith("Esta faixa não abre: é o carimbo da fonte, que termina aqui") and f"Ir para {g['nome']}" in dest_rec["primaria"]["proximo"]
    # o que ficou: grupo próprio, tracejado, com a conta aberta
    assert caixa["ficou"] is not None and caixa["ficou"]["grupo"] == "ficou" and caixa["ficou"]["tracejado"] and caixa["ficou"]["nota"].startswith("A conta: ")
    assert "o déficit" in caixa["conta_diferenca"] and caixa["ficou"]["id"] in {"caixa_guardado", "caixa_usado"}
    # strings prontas: nada de número cru para o JavaScript formatar
    d = dados_do_desenho(m)
    texto = json.dumps(d, ensure_ascii=False)
    assert not re.search(r"\b(undefined|None|NaN|nan)\b", texto)
    no = d["visoes"][1]["saidas"][0]
    assert set(no) >= {"id", "nome", "valor", "txt", "exato", "pct", "cent", "pct_pai", "cent_pai", "cor", "pagina"}
    rec_d = next(n for n in d["visoes"][0]["entradas"] if n["id"] == "receita")
    assert next(x for x in rec_d["outro_lado"] if x["id"].endswith("-dest-primaria"))["ir_para"] == {"id": "gasto", "nome": g["nome"]}
    dv_d = d["visoes"][2]
    assert dv_d["entradas"][0]["outro_lado"][0]["ir_para"] == {"id": "venc-ate_12m", "nome": dv_d["saidas"][0]["nome"]}
    assert dv_d["saidas"][0]["filhos"][0]["outro_lado"][0]["ir_para"] == {"id": "idx-selic", "nome": dv_d["entradas"][0]["nome"]}
    # conferências e lacres
    assert m["conferencias"]["n"] == len(exp["reconciliacoes"]) and m["lacres"][0]["sha256"] and m["lacres"][0]["arquivo"].endswith("Anexo 01")


def test_site_gera_paginas_consistentes(exports, config_dir, tmp_path):
    export_dir, exp = exports
    out = tmp_path / "docs"
    rel = construir(export_dir, out, CONTEUDO, config_dir, limpar=True)
    assert rel.anos == [2024] and not rel.pulados
    esperadas = {"index.html", "2024/index.html", "metodologia/index.html", "quem-somos/index.html", "dados/index.html"} | {f"2024/linha/{slug(l['id'])}/index.html" for l in exp["linhas"]}
    assert esperadas <= set(rel.paginas) and all((out / p).exists() for p in esperadas)
    assert (out / ".nojekyll").exists() and (out / "static" / "site.css").exists() and (out / "static" / "fluxo.js").exists()
    assert (out / "dados" / "recibo_2024.json").exists() and (out / "dados" / "recibo_2024.csv").exists() and (out / "dados" / "divida_2024.json").exists()
    assert json.loads((out / "dados" / "recibo_2024.json").read_text(encoding="utf-8"))["total_recibo"] == exp["total_recibo"]

    home = (out / "index.html").read_text(encoding="utf-8")
    ano = (out / "2024" / "index.html").read_text(encoding="utf-8")
    # os números da lista são os do export, formatados pelo mesmo código; os centavos somam 100 na página
    for l in exp["linhas"]:
        assert fmt_exato(l["valor"]) in ano and f'id="linha-{l["id"]}"' in ano
    lista_gasto = ano[ano.index('aria-labelledby="h-linhas"'):ano.index('id="fora"')]
    cents = re.findall(r'<span class="num cent">(\d+)</span>', lista_gasto)
    assert len(cents) == 10 and sum(int(c) for c in cents) == 100
    # abaixo do desenho, cada aba tem o seu scorecard e a sua tabela: três frases por visão; a lista do gasto e o quadro
    # "fora do R$ 1,00" só na aba do gasto; a tabela do caixa (com a rolagem) e a da dívida nas delas; sem JS, tudo aparece
    assert ano.count('<section class="tres" data-visao=') == 3 and re.search(r'<section data-visao="gasto"( hidden)? aria-labelledby="h-linhas"', ano) and 'id="fora" class="card" data-visao="gasto"' in ano
    assert "Tudo o que passou pelo caixa, linha por linha" in ano and "A dívida, linha por linha" in ano and 'data-visao-alvo="caixa"' in ano and 'data-visao-alvo="divida"' in ano
    assert "passaram pelo caixa da União em 2024" in ano and "de cada real gasto foram para a maior linha" in ano and "imposto futuro, porque dívida se paga com tributo depois" not in ano
    assert '[data-visao][hidden]{display:revert !important}' in ano
    assert fmt_exato(exp["total_recibo"]) in ano and "100 centavos" in ano
    # JSON embutido é o do desenho; sem nada sem formatar
    dados = json.loads(re.search(r'<script id="dados-fluxo" type="application/json">(.*?)</script>', ano, re.S).group(1).replace("<\\/", "</"))
    assert dados["ano"] == 2024 and [v["id"] for v in dados["visoes"]] == ["caixa", "gasto", "divida"]
    # visão "A dívida" (v0.10.0): os dois lados somam o estoque título a título; cada nó tem o outro lado (cruzamento)
    dv = dados["visoes"][2]
    assert dv["titulo_esq"] == "Do que é feita" and dv["titulo_dir"] == "Quando vence" and dv["outro"]["modo"] == "cruzamento"
    assert sum(n["valor"] for n in dv["entradas"]) == pytest.approx(dv["total"]) and sum(n["valor"] for n in dv["saidas"]) == pytest.approx(dv["total"])
    assert [n["id"] for n in dv["saidas"]] == ["venc-ate_12m", "venc-de_1_a_2_anos", "venc-de_2_a_5_anos", "venc-mais_de_5_anos"]  # ordem do calendário
    assert dv["entradas"][0]["id"] == "idx-selic" and dv["entradas"][0]["cent"] == "46 centavos" and dv["entradas"][0]["outro_lado"][0]["id"] == "idx-selic-venc-ate_12m"
    assert dv["saidas"][0]["filhos"][0]["nome"] == "março de 2025" and dv["saidas"][0]["filhos"][0]["outro_lado"][0]["nome"] == "Selic (taxa flutuante)"
    assert dv["entradas"][0]["filhos"][0]["nome"] == "Tesouro Selic (LFT)" and dv["entradas"][0]["filhos"][0]["outro_lado"]
    assert 'data-visao="divida"' in ano and "A dívida, com todas as réguas" in ano and "Começou 2024 devendo" in ano and "Terminou dezembro de 2024 devendo" in ano
    assert "Quanto saiu do caixa em juros?" in ano and "Calendário: quanto vence em cada mês" in ano and "dpf_fatores_identidade" in ano
    assert 'id="titulo-esq"' in ano and "sem dados para 2024" not in ano
    for pagina in (home, ano):
        assert not re.search(r"\b(undefined|None|NaN)\b", re.sub(r"<script.*?</script>", "", pagina, flags=re.S))
    # home = ano atual, com os caminhos relativos certos
    assert 'href="static/site.css"' in home and 'href="../static/site.css"' in ano
    assert 'href="2024/linha/saude/"' in home and 'href="../2024/linha/saude/"' in ano
    # página da linha: níveis, ficha técnica com o SQL e os lacres, histórico, texto do conteúdo
    saude = (out / "2024" / "linha" / "saude" / "index.html").read_text(encoding="utf-8")
    assert "Atenção Básica" in saude and "219A" in saude and "Lacres dos arquivos originais" in saude and "sha256" not in saude.lower().replace("sha-256", "")
    assert "Gasto federal com saúde" in saude and 'href="../../../2024/#linha-saude"' in saude
    maq = (out / "2024" / "linha" / "maquina-demais" / "index.html").read_text(encoding="utf-8")
    assert "Ajustes sem subfunção" in maq and "Empréstimos pela lei que o Tesouro conta como gasto" in maq
    # links internos resolvem
    quebrados = []
    for p in rel.paginas:
        html = (out / p).read_text(encoding="utf-8")
        for href in re.findall(r'href="([^"#]+)(?:#[^"]*)?"', html):
            if href.startswith(("http", "mailto:", "data:")):
                continue
            alvo = (out / p).parent / href
            alvo = alvo / "index.html" if href.endswith("/") else alvo
            if not alvo.resolve().exists():
                quebrados.append((p, href))
    assert not quebrados, quebrados
    # metodologia e dados: conferências listadas, glossário com os termos do site e dos blocos, lacres
    met = (out / "metodologia" / "index.html").read_text(encoding="utf-8")
    assert "niveis_fecham" in met and "rateio" in met and "DPF, DPMFi e DPFe" in met and "Confira você mesmo" in met
    dad = (out / "dados" / "index.html").read_text(encoding="utf-8")
    assert "recibo_2024.csv" in dad and exp["linhas"][0]["ficha"]["snapshots"][0]["sha256_canonical"] in dad
    csv_txt = (out / "dados" / "recibo_2024.csv").read_text(encoding="utf-8").splitlines()
    assert csv_txt[0].startswith("id,nome,valor_reais") and len(csv_txt) == 11
    # quem somos: bandeira própria (SVG, sem imagem de fora), os três artigos da Constituição como citações no lugar do
    # marcador, quem fez preenchido; rodapé de toda página com a bandeira e o art. 1º
    quem = (out / "quem-somos" / "index.html").read_text(encoding="utf-8")
    assert "[CONSTITUICAO]" not in quem and "[QUEM FEZ]" not in quem and "Caue Bertolino" in quem
    for fonte in ("art. 1º, parágrafo único", "art. 70, parágrafo único", "art. 74, § 2º"):
        assert fonte in quem
    assert quem.index("Objetivo e filosofia") < quem.index("O que a Constituição diz") < quem.index("Quem paga")
    assert "ORDEM E PROGRESSO" in quem and 'aria-label="Bandeira do Brasil"' in quem and "<img" not in quem
    for pagina in (home, ano, met, dad, quem):
        assert "Todo o poder emana do povo" in pagina and 'class="bandeira bandeira-rodape"' in pagina
        # v0.11.0: as duas linhas do cifrão são azul e amarelo da bandeira, no cabeçalho e no favicon
        assert '<path d="M8 12 H56" stroke="#012169"' in pagina and '<path d="M8 20 H56" stroke="#FEDD00"' in pagina and "%23012169" in pagina and "%23FEDD00" in pagina
    # v0.10.2: a tubulação do domínio (CNAME, robots, sitemap, 404, Open Graph com imagem própria) e a página de perguntas
    cfg_site = __import__("yaml").safe_load((CONTEUDO / "site.yaml").read_text(encoding="utf-8"))
    url_base = cfg_site["url"]
    assert (out / "CNAME").read_text(encoding="utf-8").strip() == cfg_site["dominio"]
    assert (out / "robots.txt").read_text(encoding="utf-8").startswith("User-agent: *\nAllow: /\nSitemap: " + url_base + "sitemap.xml")
    mapa = (out / "sitemap.xml").read_text(encoding="utf-8")
    assert f"<loc>{url_base}</loc>" in mapa and f"<loc>{url_base}2024/</loc>" in mapa and f"<loc>{url_base}perguntas-abertas/</loc>" in mapa and f"<loc>{url_base}2024/linha/saude/</loc>" in mapa
    assert mapa.count("<url>") == len(rel.paginas)
    p404 = (out / "404.html").read_text(encoding="utf-8")
    assert 'content="noindex"' in p404 and f'href="{url_base}metodologia/"' in p404 and (out / "static" / "social.png").exists()
    assert f'<link rel="canonical" href="{url_base}2024/">' in ano and f'<link rel="canonical" href="{url_base}">' in home
    assert f'<meta property="og:image" content="{url_base}static/social.png">' in ano and 'property="og:title"' in ano and 'name="twitter:card" content="summary_large_image"' in met
    perg = (out / "perguntas-abertas" / "index.html").read_text(encoding="utf-8")
    assert "Perguntas abertas" in perg and 'id="titulos-rreo-vs-emissoes"' in perg and 'id="vincendo-12-meses"' in perg and 'id="indexador-por-titulo"' in perg
    assert "{ano}" not in perg and "{titulos_rreo}" not in perg and "(indisponível)" not in perg.split('id="indexador-por-titulo"')[0]
    assert "Lei 12.527/2011" in perg and 'href="../perguntas-abertas/"' in met and "Limitações e próximos passos" in met and 'id="limitacoes"' in met
    # v0.10.3: cada pergunta é um bloco expandível (<details>) com o status no resumo; o status sai dos campos do YAML
    # (sem protocolo = aberta; com protocolo = enviada; com resposta = respondida), então o teste lê o YAML do repositório
    pq = __import__("yaml").safe_load((CONTEUDO / "perguntas.yaml").read_text(encoding="utf-8"))["perguntas"]
    n_resp = sum(1 for q in pq if q.get("resposta") or q.get("respondida_em")); n_env = sum(1 for q in pq if q.get("protocolo") and not (q.get("resposta") or q.get("respondida_em"))); n_ab = len(pq) - n_resp - n_env
    assert perg.count('<details class="pergunta"') == len(pq) == 3 and perg.count("<summary>") == 3
    assert perg.count('class="status status-aberta"') == n_ab and perg.count('class="status status-enviada"') == n_env and perg.count('class="status status-respondida"') == n_resp
    assert f"{n_ab} aberta(s) · {n_env} enviada(s) · {n_resp} respondida(s)" in perg and "Tem uma dúvida que não está aqui?" in perg and "Fala.BR" in perg
    assert perg.count("Aguardando a resposta") == n_env
    for q in pq:
        if q.get("protocolo"):
            assert f"protocolo {q['protocolo']}" in perg and f"enviada em {q['enviada_em']}" in perg and f"Pergunta enviada ao {q.get('enviada_para') or q['orgao']}" in perg
    assert "Erro, bug ou discordância" in quem and "uma página que não abre" in quem and "o endereço da página" in quem
    assert '"ir_para"' in ano and "Ir para O gasto de verdade" in ano
    assert "Reserva de liquidez (não usado no ano)" in ano and "tracejado_motivo" in ano and 'id="legenda-tracejado"' in ano and "destaques" in ano
    # v0.11.1: em tela estreita, a linha "ver o diagrama" (o JS troca o arranjo; o CSS só a mostra até 900 px) e as
    # tabelas de três colunas ou mais marcadas como cartões, com o nome da coluna em cada célula
    assert 'id="ver-diagrama"' in ano and 'class="mini so-estreito modo-desenho"' in ano and 'id="sankey"' in ano
    assert 'class="tabela cartoes"' in met and 'data-rotulo="formato e cadência"' in met
    assert dad.count('class="tabela cartoes"') == 2 and 'class="lacres cartoes"' in dad and 'data-rotulo="SHA-256"' in dad and 'data-rotulo="comparação internacional"' in dad
    assert 'class="lacres cartoes"' in saude and 'data-rotulo="SHA-256"' in saude   # "ano a ano" só aparece com mais de um ano
    assert 'href="https://github.com/cadareal/pipeline/issues/new/choose"' in quem and 'href="https://github.com/cadareal/pipeline/issues/new/choose"' in perg


def _lados_vazios(view: dict) -> list[str]:
    """Emula a regra do fluxo.js: um nó que abre (tem partes ou outro lado) mostra do outro lado o seu `outro_lado`, ou o do
    conjunto de onde veio (`outro_lado_de`), ou nada. Devolve os ids que ficariam com o outro lado em branco."""
    if (view.get("outro") or {}).get("modo") == "rateio":
        return []
    idx: dict[str, dict] = {}

    def indexar(lista):
        for n in lista:
            idx[n["id"]] = n
            indexar(n.get("filhos") or [])
            indexar(n.get("outro_lado") or [])

    indexar(view["entradas"])
    indexar(view["saidas"])
    vazios = []

    def visitar(n):
        filhos = [f for f in n.get("filhos") or [] if f["valor"] > 0]
        outro = [o for o in n.get("outro_lado") or [] if o["valor"] > 0]
        if (filhos or outro) and not outro:
            dono = idx.get(n.get("outro_lado_de") or "")
            if not (dono and [o for o in dono.get("outro_lado") or [] if o["valor"] > 0]):
                vazios.append(n["id"])
        for f in filhos:
            visitar(f)

    for n in view["entradas"] + view["saidas"]:
        visitar(n)
    return vazios


def test_ano_em_andamento_tratado_igual_em_todas_as_visoes(exports, config_dir, tmp_path):
    """v0.11.0: o ano aberto (sem SIOP, dívida até julho) não deixa nenhum lado do desenho em branco sem aviso, ganha a
    linha de cobertura certa, e o ano fechado continua completo. A mesma regra vale para qualquer ano que entrar."""
    export_dir, exp = exports
    e24 = json.loads((export_dir / "recibo_2024.json").read_text(encoding="utf-8"))
    d24 = json.loads((export_dir / "divida_2024.json").read_text(encoding="utf-8"))
    e26, d26 = ano_em_andamento(e24, d24)
    (export_dir / "recibo_2026.json").write_text(json.dumps(e26, ensure_ascii=False), encoding="utf-8")
    (export_dir / "divida_2026.json").write_text(json.dumps(d26, ensure_ascii=False), encoding="utf-8")
    out = tmp_path / "docs"
    rel = construir(export_dir, out, CONTEUDO, config_dir, limpar=True)
    assert rel.anos == [2024, 2026] and not rel.pulados
    # a linha de cobertura, na página e no terminal, sai dos dados
    assert rel.cobertura[2024] == "Ano fechado: RREO de janeiro a dezembro (6º bimestre); SIOP do ano inteiro (detalhe por ação e carimbo da fonte); dívida até dezembro."
    assert rel.cobertura[2026].startswith("Ano em andamento: RREO de janeiro a agosto (4º bimestre); dívida até julho.")
    assert "Sem detalhe por ação nem carimbo da fonte, que só entram com o ano fechado" in rel.cobertura[2026] and rel.cobertura[2026].endswith("Os números mudam a cada bimestre.")
    assert "2026: Ano em andamento" in rel.resumo()
    p24 = (out / "2024" / "index.html").read_text(encoding="utf-8")
    p26 = (out / "2026" / "index.html").read_text(encoding="utf-8")
    assert 'class="cobertura completa"' in p24 and rel.cobertura[2024] in p24
    assert 'class="cobertura parcial"' in p26 and "Os números mudam a cada bimestre." in p26 and "2026, até o 4º bimestre" in p26
    # a home continua sendo o último ano fechado
    assert (out / "index.html").read_text(encoding="utf-8") == p24.replace('href="../', 'href="').replace('src="../', 'src="') or "2024" in (out / "index.html").read_text(encoding="utf-8")
    # nenhum lado em branco: no ano fechado, todo nó que abre tem o outro lado (próprio ou do conjunto); no ano aberto,
    # o caixa declara o motivo e os textos da faixa de aviso, e a dívida continua inteira
    for pagina, aberto in ((p24, False), (p26, True)):
        dados = json.loads(re.search(r'<script id="dados-fluxo" type="application/json">(.*?)</script>', pagina, re.S).group(1).replace("<\\/", "</"))
        visoes = {v["id"]: v for v in dados["visoes"]}
        assert set(visoes) == {"caixa", "gasto", "divida"}
        assert _lados_vazios(visoes["divida"]) == []
        if not aberto:
            assert visoes["caixa"]["por_fonte"] and _lados_vazios(visoes["caixa"]) == []
        else:
            assert not visoes["caixa"]["por_fonte"] and "ano em andamento" in visoes["caixa"]["sem_fonte_motivo"]
            assert visoes["caixa"]["outro"]["vazio_nome"] == "Sem carimbo da fonte até o ano fechar" and visoes["caixa"]["outro"]["vazio_sub"]
            assert _lados_vazios(visoes["caixa"]), "o ano aberto tem lados sem carimbo; é a faixa de aviso que os cobre"
            assert visoes["divida"]["entradas"] and all(n.get("outro_lado") for n in visoes["divida"]["entradas"])
    assert "Ano em andamento" in p26 and "dívida até julho" in p26 and "A dívida cresceu até julho de 2026" in p26


def test_cobertura_do_ano_sai_dos_dados():
    from recibo.site.dados import cobertura_do_ano
    fechado = {"periodo": 6, "estado": {"selo": "fechado"}, "siop": {"usado_nas_linhas": True}}
    aberto = {"periodo": 2, "estado": {"selo": "em_andamento"}, "siop": {"usado_nas_linhas": False}}
    div_fechada = {"mes": 12, "fechado": True}
    c = cobertura_do_ano(fechado, {"execucao": {"disponivel": True, "ultimo_mes_com_dados": 12}}, {"por_fonte": True}, div_fechada)
    assert c["completa"] and c["texto"] == "Ano fechado: RREO de janeiro a dezembro (6º bimestre); SIOP do ano inteiro (detalhe por ação e carimbo da fonte); dívida até dezembro."
    c = cobertura_do_ano(aberto, {"execucao": {"disponivel": True, "ultimo_mes_com_dados": 2}}, {"por_fonte": False}, {"mes": 3, "fechado": False})
    assert not c["completa"] and c["texto"].startswith("Ano em andamento: RREO de janeiro a abril (2º bimestre); dívida até março (juros e principal pagos até fevereiro).")
    assert "Sem detalhe por ação nem carimbo da fonte, que só entram com o ano fechado; a dívida é a posição do último mês publicado" in c["texto"]
    # fechado, mas sem o mart da dívida: diz o que falta, sem fingir completo
    c = cobertura_do_ano(fechado, None, {"por_fonte": False}, None)
    assert not c["completa"] and "SIOP: detalhe por ação" in c["texto"] and "Sem carimbo da fonte; sem a aba da dívida." in c["texto"]


def test_perguntas_abertas_status_sai_dos_campos():
    from recibo.site.dados import perguntas_abertas
    base = {"titulo": "T", "orgao": "STN", "contexto": "c", "pergunta": "p em {ano}", "afeta": ""}
    cfg = {"perguntas": {"titulo": "Perguntas abertas", "intro": "i", "tem_duvida": {"titulo": "Tem?", "texto": "t {ano}"}, "perguntas": [
        dict(base, id="a", protocolo="", enviada_em="", respondida_em="", resposta=""),
        dict(base, id="b", protocolo="23546.012345/2026-11", enviada_em="2026-11-12", respondida_em="", resposta=""),
        dict(base, id="c", protocolo="23546.012346/2026-11", enviada_em="2026-11-12", respondida_em="2026-12-01", resposta="Explicou."),
        dict(base, id="d", protocolo="x", status="aberta"),
    ]}}
    p = perguntas_abertas(cfg, None, None, 2025)
    assert [q["status"] for q in p["perguntas"]] == ["aberta", "enviada", "respondida", "aberta"]
    assert p["n_abertas"] == 2 and p["n_enviadas"] == 1 and p["n_respondidas"] == 1
    assert p["perguntas"][0]["pergunta"] == "p em 2025" and p["tem_duvida"] == {"titulo": "Tem?", "texto": "t 2025"}
    # sem exports, os marcadores viram "(indisponível)" em vez de quebrar a página
    sem = perguntas_abertas({"perguntas": {"perguntas": [dict(base, id="e", pergunta="{titulos_rreo} vs {emissoes}")]}}, None, None, None)
    assert sem["perguntas"][0]["pergunta"] == "(indisponível) vs (indisponível)" and sem["perguntas"][0]["status"] == "aberta"


def test_site_pula_ano_nao_publicavel_e_avisa(exports, config_dir, tmp_path):
    export_dir, exp = exports
    ruim = dict(exp)
    ruim["publicavel"] = False
    ruim["exercicio"] = 2023
    ruim["reconciliacoes"] = [{"id": "identidade_total", "ok": False, "critica": True}]
    (export_dir / "recibo_2023.json").write_text(json.dumps(ruim, ensure_ascii=False), encoding="utf-8")
    (export_dir / "recibo_2022.json").write_text("{ isto não é json", encoding="utf-8")
    rel = construir(export_dir, tmp_path / "docs", CONTEUDO, config_dir)
    assert rel.anos == [2024]
    motivos = dict(rel.pulados)
    assert "identidade_total" in motivos[2023] and "não publicável" in motivos[2023] and "ilegível" in motivos[2022]
    assert not (tmp_path / "docs" / "2023").exists()
    assert "ano 2023 ficou de fora" in rel.resumo()
    # sem nenhum export publicável, nada é gerado e o relatório diz isso
    rel2 = construir(export_dir, tmp_path / "docs2", CONTEUDO, config_dir, exercicios=[2023])
    assert rel2.anos == [] and rel2.avisos and not (tmp_path / "docs2").exists()


def test_site_sem_divida_do_ano_avisa_na_aba(exports, config_dir, tmp_path):
    """Sem o export da dívida do ano (ou sem o detalhe título a título), a página sai sem a visão "A dívida", com uma
    aba desativada que diz o motivo; nada de aproximação."""
    export_dir, exp = exports
    (export_dir / "divida_2024.json").unlink()
    rel = construir(export_dir, tmp_path / "docs", CONTEUDO, config_dir, limpar=True)
    ano = (tmp_path / "docs" / "2024" / "index.html").read_text(encoding="utf-8")
    dados = json.loads(re.search(r'<script id="dados-fluxo" type="application/json">(.*?)</script>', ano, re.S).group(1).replace("<\\/", "</"))
    assert rel.anos == [2024] and [v["id"] for v in dados["visoes"]] == ["caixa", "gasto"]
    assert "A dívida · sem dados para 2024" in ano and "mart da dívida deste ano ainda não rodou" in ano and 'data-visao="divida"' not in ano
    # a função de formatação das medidas da dívida respeita a unidade de cada uma
    from recibo.site.dados import fmt_medida

    assert fmt_medida({"valor": 7.3595e12, "unidade": "reais"}) == "R$ 7,36 tri" and fmt_medida({"valor": 76.3, "unidade": "% do PIB"}) == "76,3% do PIB"
    assert fmt_medida({"valor": 15.67, "unidade": "%"}) == "15,7%" and fmt_medida({"valor": 48.2, "unidade": "meses"}) == "48 meses"
    assert fmt_medida({"valor": None, "unidade": "reais", "motivo": "sem fonte aberta"}) == "indisponível" and fmt_medida(None) == "indisponível"
    from recibo.site.dados import motivo_leigo

    assert motivo_leigo("COFOG 2025 não está no staging (anos disponíveis: 2010 a 2024). O Tesouro publica o COFOG com cerca de um ano de defasagem; se o CKAN já tem o ano, rode `recibo fetch cofog`").startswith("o Tesouro publica a base COFOG")
    assert "PDF" in motivo_leigo("sem fonte aberta: o Tesouro publica só no Relatório Mensal da Dívida, em PDF") and "rode" not in motivo_leigo("staging do BCB não existe (x); rode `recibo fetch bcb`")
