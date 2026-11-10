"""Mart dívida (v0.7.2): quatro medidas com réguas nomeadas, lidas de planilhas e CSVs sintéticos do Tesouro no layout
real (rótulos com notas de rodapé, cabeçalho Mmm/aa com coluna anual), de um staging sintético do BCB e do recibo
sintético de conftest (juros 350, amortização 160, refinanciamento 1500, serviço fora de 843/844 = 10; Anexo 06 com
DC 9000 -> 9220, DCL 6000 -> 6120, XI 500, X 100). Tudo construído para fechar exato."""

import io
import json
from datetime import date

import polars as pl
import pytest
from openpyxl import Workbook

from recibo.config import load_yaml, source_config
from recibo.fetchers.divida import run_fetch as run_fetch_divida
from recibo.grade import GradeFormato, Planilha, normalizar_rotulo
from recibo.marts.divida import compute_divida, print_summary_divida, to_export_divida, write_outputs_divida
from recibo.marts.recibo import SemDados
from recibo.transform.divida import build_staging as build_staging_divida, ler_xlsx_grade
from recibo.transform.siconfi import build_staging

from conftest import CONFIG_DIR
from test_divida_bcb import _Resp

MESES_ABREV = ["Jan", "Fev", "Mar", "Abr", "Mai", "Jun", "Jul", "Ago", "Set", "Out", "Nov", "Dez"]
BI = 1e9
MI = 1e6

# ---- fatores (R$ milhões): 2023 constante, 2024 com os números do teste, 2025 só até março (ano em andamento)
EST_ANT_2024 = 6_520_000.0
EMISSOES_MES, RESGATES_MES, JUROS_DPMFI_MES, JUROS_DPFE_MES = 120_000.0, -110_000.0, 55_000.0, 5_000.0
OP_BC_MAR = -500.0
VAR_2024 = 12 * (EMISSOES_MES + RESGATES_MES + JUROS_DPMFI_MES + JUROS_DPFE_MES) + OP_BC_MAR  # 839.500
EST_FIM_2024 = EST_ANT_2024 + VAR_2024  # 7.359.500
VAR_MES_OUTROS = 100_000.0 - 90_000.0 + 30_000.0 + 3_333.3  # 43.333,3 por mês em 2023 e 2025
EST_ANT_2023 = EST_ANT_2024 - 12 * VAR_MES_OUTROS  # para dezembro de 2023 fechar em EST_ANT_2024
EST_FIM_2025_03 = EST_FIM_2024 + 3 * VAR_MES_OUTROS  # 7.489.499,9 (ano em andamento)


def colunas_cabecalho() -> list[tuple[str, tuple[int, int] | int]]:
    """Cabeçalho como o Tesouro: Jan/23..Dez/23, 2023, Jan/24..Dez/24, 2024, Jan/25..Mar/25."""
    cols: list[tuple[str, tuple[int, int] | int]] = []
    for ano in (2023, 2024):
        for m in range(1, 13):
            cols.append((f"{MESES_ABREV[m - 1]}/{ano % 100:02d}", (ano, m)))
        cols.append((str(ano), ano))
    for m in range(1, 4):
        cols.append((f"{MESES_ABREV[m - 1]}/25", (2025, m)))
    return cols


def fatores_valores() -> dict[str, dict[tuple[int, int], float]]:
    v: dict[str, dict[tuple[int, int], float]] = {k: {} for k in ("Estoque anterior1", "Estoque mês em análise", "Variação Nominal", "I.1.1 - Emissões", "I.1.2 - Resgates", "I.2 - Juros  Apropriados", "- Juros Apropriados da DPMFi 9", "- Juros Apropriados da DPFe 10", "II - Operações do mercado com o Banco Central", "Total dos Fatores (I + II)")}
    est = EST_ANT_2023
    for ano, meses in ((2023, range(1, 13)), (2024, range(1, 13)), (2025, range(1, 4))):
        if ano == 2024:
            est = EST_ANT_2024  # igual ao fim de 2023, sem resíduo de ponto flutuante
        for m in meses:
            em, re_, jd, je = (100_000.0, -90_000.0, 30_000.0, 3_333.3) if ano != 2024 else (EMISSOES_MES, RESGATES_MES, JUROS_DPMFI_MES, JUROS_DPFE_MES)
            op = OP_BC_MAR if (ano == 2024 and m == 3) else 0.0
            var = em + re_ + jd + je + op
            k = (ano, m)
            v["Estoque anterior1"][k] = est
            v["I.1.1 - Emissões"][k] = em
            v["I.1.2 - Resgates"][k] = re_
            v["- Juros Apropriados da DPMFi 9"][k] = jd
            v["- Juros Apropriados da DPFe 10"][k] = je
            v["I.2 - Juros  Apropriados"][k] = jd + je
            v["II - Operações do mercado com o Banco Central"][k] = op
            v["Variação Nominal"][k] = var
            v["Total dos Fatores (I + II)"][k] = var
            est = est + var
            v["Estoque mês em análise"][k] = est
    return v


def xlsx_fatores_tesouro() -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "2.9"
    ws.append([])
    ws.append(["ANEXO 2.9 - FATORES DE VARIAÇÃO DA DPF"])
    for _ in range(4):
        ws.append([])
    cols = colunas_cabecalho()
    ws.append(["Indicadores"] + [c[0] for c in cols])  # linha 7, como no arquivo real
    ws.append([None] + ["R$ Milhões"] * len(cols))
    vals = fatores_valores()
    for rotulo, por_mes in vals.items():
        linha = [rotulo]
        for _, chave in cols:
            if isinstance(chave, int):  # coluna anual
                if rotulo == "Estoque anterior1":
                    linha.append(por_mes.get((chave, 1)))
                elif rotulo == "Estoque mês em análise":
                    linha.append(por_mes.get((chave, 12)))
                else:
                    linha.append(sum(por_mes.get((chave, m), 0.0) for m in range(1, 13)))
            else:
                linha.append(por_mes.get(chave))
        ws.append(linha)
    ws.append(["1 A coluna Mês/Ano refere-se ao último dia"])
    b = io.BytesIO()
    wb.save(b)
    return b.getvalue()


ESTOQUE_BI = {  # (ano, mes) -> (DPF público, DPMFi, DPFe, DPMFi no BC), R$ bilhões; dezembro de 2024 fecha com os fatores
    (2023, 12): (6_520.0, 6_200.0, 320.0, 2_300.0),
    (2024, 12): (EST_FIM_2024 / 1000, 7_000.0, EST_FIM_2024 / 1000 - 7_000.0, 2_500.0),
    (2025, 3): (EST_FIM_2025_03 / 1000, EST_FIM_2025_03 / 1000 - 370.0, 370.0, 2_550.0),
}


def xlsx_estoque_tesouro() -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "2.1"
    ws.append([])
    ws.append(["ANEXO 2.1 - ESTOQUE DA DPF"])
    ws.append([None] * 10 + ["(R$ Bilhões)"])
    ws.append([])
    cols = [c for c in colunas_cabecalho() if not isinstance(c[1], int)]  # a planilha de estoque não tem coluna anual
    ws.append([None] + [c[0] for c in cols])  # linha 5
    ws.append([])

    def serie(idx: int) -> list:
        out = []
        for _, chave in cols:
            ref = ESTOQUE_BI.get(chave)
            if ref is None:  # meses sem valor definido: interpola grosseiramente (não entram nas conferências)
                ano, m = chave
                base = ESTOQUE_BI[(2023, 12)] if ano == 2023 else ESTOQUE_BI[(2024, 12)] if ano == 2024 else ESTOQUE_BI[(2025, 3)]
                out.append(round(base[idx] * (0.98 + m / 600), 6))
            else:
                out.append(ref[idx])
        return out

    ws.append(["DPF EM PODER DO PÚBLICO"] + serie(0))
    ws.append([])
    ws.append(["DPMFi"] + serie(1))
    ws.append(["Tesouro Nacional"])
    ws.append(["LFT"] + [None] * len(cols))
    ws.append(["DPFe ¹"] + serie(2))
    ws.append(["Dívida Mobiliária"] + [None] * len(cols))
    ws.append(["DPMFi EM PODER DO BANCO CENTRAL"] + serie(3))
    ws.append(["LTN"] + [None] * len(cols))
    ws.append(["¹ Valores da DPFe convertidos"])
    b = io.BytesIO()
    wb.save(b)
    return b.getvalue()


# ---- CSV do estoque título a título (reais, padrão brasileiro)
LINHAS_ESTOQUE = {
    (2024, 12): [
        ("LFT 010325", "01/03/2025", 3_400 * BI, "Mercado", "Dívida Interna"),
        ("LTN 010126", "01/01/2026", 1_000 * BI, "Mercado", "Dívida Interna"),
        ("NTN-F 010129", "01/01/2029", 600 * BI, "Mercado", "Dívida Interna"),
        ("NTN-B 150535", "15/05/2035", 1_900 * BI, "Mercado", "Dívida Interna"),
        ("TDA27070300", "01/07/2027", 100 * BI, "Mercado", "Dívida Interna"),
        ("Global 2034", "20/01/2034", 300 * BI, "Mercado", "Dívida Externa"),
        ("Global BRL 2028", "10/01/2028", 9.5 * BI, "Mercado", "Dívida Externa"),  # externo emitido em reais: prefixado
        ("BIRD9172-BR", "17/06/2050", 50 * BI, "Mercado", "Dívida Externa"),
        ("LFT 010326", "01/03/2026", 1_500 * BI, "Banco Central", "Dívida Interna"),
        ("NTN-B 150826", "15/08/2026", 1_000 * BI, "Banco Central", "Dívida Interna"),
    ],
    (2023, 12): [
        ("LFT 010325", "01/03/2025", 3_000 * BI, "Mercado", "Dívida Interna"),
        ("LTN 010126", "01/01/2026", 1_000 * BI, "Mercado", "Dívida Interna"),
        ("NTN-B 150535", "15/05/2035", 1_900 * BI, "Mercado", "Dívida Interna"),
        ("TDA27070300", "01/07/2027", 300 * BI, "Mercado", "Dívida Interna"),
        ("Global 2034", "20/01/2034", 320 * BI, "Mercado", "Dívida Externa"),
        ("LFT 010326", "01/03/2026", 2_300 * BI, "Banco Central", "Dívida Interna"),
    ],
    (2025, 3): [
        ("LFT 010326", "01/03/2026", 3_500 * BI, "Mercado", "Dívida Interna"),
        ("NTN-B 150535", "15/05/2035", (EST_FIM_2025_03 / 1000 - 370.0 - 3_500) * BI, "Mercado", "Dívida Interna"),
        ("Global 2034", "20/01/2034", 370 * BI, "Mercado", "Dívida Externa"),
        ("LFT 010327", "01/03/2027", 2_550 * BI, "Banco Central", "Dívida Interna"),
    ],
}


def br(v: float) -> str:
    return f"{v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def csv_estoque() -> bytes:
    rows = ["Titulo/Contrato;Vencimento do Titulo/Contrato;Valor do Estoque;Quantidade do Estoque;Mes do Estoque;Classe da Carteira;Tipo de Divida"]
    for (ano, mes), linhas in LINHAS_ESTOQUE.items():
        for titulo, venc, valor, classe, tipo in linhas:
            rows.append(f"{titulo};{venc};{br(valor)};{br(valor / 1000)};{mes:02d}/{ano};{classe};{tipo}")
    return ("\n".join(rows) + "\n").encode("latin-1")


def csv_execucao() -> bytes:
    cab = "Exercício;Tipo de Dívida;Modalidade da Dívida;Carteira;Movimentação Financeira;Grupo Despesa Código Grupo;Grupo Despesa Nome;Natureza Despesa Detalhada Código;Natureza Despesa Detalhada Nome;" + ";".join(["janeiro", "fevereiro", "março", "abril", "maio", "junho", "julho", "agosto", "setembro", "outubro", "novembro", "dezembro"]) + ";"
    meses = lambda **kw: ";".join(br(kw.get(m, 0.0)) if m in kw else "" for m in ["janeiro", "fevereiro", "março", "abril", "maio", "junho", "julho", "agosto", "setembro", "outubro", "novembro", "dezembro"])  # noqa: E731
    rows = [cab]
    # 2024: juros 300 (mercado) + 46 (BACEN) + 4 (política monetária) = 350; principal 1000 + 654 + 6 = 1660
    rows.append(f"2024;Interna;Mobiliária;MERCADO;Juros e Encargos;2;JUROS E ENCARGOS DA DIVIDA;32902301;JUROS DA DIVIDA MOBILIARIA;{meses(janeiro=200.0, dezembro=100.0)};")
    rows.append(f"2024;Interna;Mobiliária;BACEN;Juros e Encargos;2;JUROS E ENCARGOS DA DIVIDA;32902301;JUROS DA DIVIDA MOBILIARIA;{meses(março=46.0)};")
    rows.append(f"2024;Política Monetária;Política Monetária;BACEN_;Juros e Encargos;2;JUROS E ENCARGOS DA DIVIDA;32902601;COBERTURA DO RESULTADO NEGATIVO DO BANCO CENTRAL;{meses(janeiro=4.0)};")
    rows.append(f"2024;Interna;Mobiliária;MERCADO;Principal;6;AMORTIZACAO/REFINANCIAMENTO DA DIVIDA;46907601;REFINANCIAMENTO PRINCIPAL DIVIDA MOBILIARIA;{meses(janeiro=700.0, julho=300.0)};")
    rows.append(f"2024;Interna;Mobiliária;BACEN;Principal;6;AMORTIZACAO/REFINANCIAMENTO DA DIVIDA;46907601;REFINANCIAMENTO PRINCIPAL DIVIDA MOBILIARIA;{meses(fevereiro=654.0)};")
    rows.append(f"2024;Política Monetária;Política Monetária;BACEN_;Principal;6;AMORTIZACAO/REFINANCIAMENTO DA DIVIDA;46902601;COBERTURA DO RESULTADO NEGATIVO DO BANCO CENTRAL;{meses(janeiro=6.0)};")
    # 2023: 90% (o recibo sintético de 2023 é 90% do de 2024): juros 311,4 + 3,6 = 315; principal 1.488,6 + 5,4 = 1.494
    rows.append(f"2023;Interna;Mobiliária;MERCADO;Juros e Encargos;2;JUROS E ENCARGOS DA DIVIDA;32902301;JUROS DA DIVIDA MOBILIARIA;{meses(janeiro=311.4)};")
    rows.append(f"2023;Política Monetária;Política Monetária;BACEN_;Juros e Encargos;2;JUROS E ENCARGOS DA DIVIDA;32902601;COBERTURA DO RESULTADO NEGATIVO DO BANCO CENTRAL;{meses(janeiro=3.6)};")
    rows.append(f"2023;Interna;Mobiliária;MERCADO;Principal;6;AMORTIZACAO/REFINANCIAMENTO DA DIVIDA;46907601;REFINANCIAMENTO PRINCIPAL DIVIDA MOBILIARIA;{meses(janeiro=1488.6)};")
    rows.append(f"2023;Política Monetária;Política Monetária;BACEN_;Principal;6;AMORTIZACAO/REFINANCIAMENTO DA DIVIDA;46902601;COBERTURA DO RESULTADO NEGATIVO DO BANCO CENTRAL;{meses(janeiro=5.4)};")
    # 2025: só janeiro a março (ano em andamento); a cobertura do BC vem duas vezes, como no arquivo real de 2025
    # (mesmos valores, "Tipo de Dívida" Interna numa linha e Política Monetária na outra): conta uma vez
    rows.append(f"2025;Interna;Mobiliária;MERCADO;Juros e Encargos;2;JUROS E ENCARGOS DA DIVIDA;32902301;JUROS DA DIVIDA MOBILIARIA;{meses(janeiro=10.0, fevereiro=10.0, março=10.0)};")
    rows.append(f"2025;Interna;Política Monetária;BACEN_;Juros e Encargos;2;JUROS E ENCARGOS DA DIVIDA;32902601;COBERTURA DO RESULTADO NEGATIVO DO BANCO CENTRAL DO BRASIL (LRF, ART. 7º, §1º);{meses(janeiro=2.0)};")
    rows.append(f"2025;Interna;Política Monetária;BACEN_;Principal;6;AMORTIZACAO/REFINANCIAMENTO DA DIVIDA;46902601;COBERTURA DO RESULTADO NEGATIVO DO BANCO CENTRAL DO BRASIL (LRF, ART. 7º, §1º);{meses(janeiro=20.0)};")
    rows.append(f"2025;Política Monetária;Política Monetária;BACEN_;Juros e Encargos;2;JUROS E ENCARGOS DA DIVIDA;32902601;COBERTURA DO RESULTADO NEGATIVO DO BANCO CENTRAL DO BRASIL (LRF, ART. 7º, §1º);{meses(janeiro=2.0)};")
    rows.append(f"2025;Política Monetária;Política Monetária;BACEN_;Principal;6;AMORTIZACAO/REFINANCIAMENTO DA DIVIDA;46902601;COBERTURA DO RESULTADO NEGATIVO DO BANCO CENTRAL DO BRASIL (LRF, ART. 7º, §1º);{meses(janeiro=20.0)};")
    return ("\n".join(rows) + "\n").encode("latin-1")


def csv_tesouro_direto() -> bytes:
    rows = ["Tipo Titulo;Vencimento do Titulo;Mes Estoque;PU;Quantidade;Valor Estoque"]
    rows.append(f"Tesouro Selic;01/03/2027;12/2024;15000,00;6666666,67;{br(100 * BI)}")
    rows.append(f"Tesouro IPCA+;15/05/2035;12/2024;2500,00;20000000,00;{br(50 * BI)}")
    rows.append(f"Tesouro Selic;01/03/2027;12/2023;14000,00;6000000,00;{br(84 * BI)}")
    rows.append(f"Tesouro Selic;01/03/2027;03/2025;15100,00;7000000,00;{br(105.7 * BI)}")
    return ("\n".join(rows) + "\n").encode("latin-1")


class FakeTesouroDivida:
    def __init__(self):
        self.arquivos = {
            "https://x/estoque-da-divida-publica-federal.xlsx": xlsx_estoque_tesouro(),
            "https://x/estoquedpf.csv": csv_estoque(),
            "https://x/fatores-de-variacao-da-divida-publica-federal.xlsx": xlsx_fatores_tesouro(),
            "https://x/execucao-orcamentaria-e-financeira-da-dpf-por-nd--2009-2026.csv": csv_execucao(),
            "https://x/estoquetesourodireto.csv": csv_tesouro_direto(),
        }
        cfg = source_config(CONFIG_DIR, "S1")
        self.ids = {v["dataset_id"]: k for k, v in cfg["datasets"].items()}
        self.recursos = {
            "estoque_dpf": [("CSV", "https://x/estoquedpf.csv"), ("XLSX", "https://x/estoque-da-divida-publica-federal.xlsx"), ("PDF", "https://x/meta.pdf")],
            "fatores_variacao": [("XLSX", "https://x/fatores-de-variacao-da-divida-publica-federal.xlsx")],
            "emissoes_resgates": [("PDF", "https://x/meta2.pdf")],
            "execucao_natureza": [("CSV", "https://x/execucao-orcamentaria-e-financeira-da-dpf-por-nd--2009-2026.csv")],
            "tesouro_direto_estoque": [("CSV", "https://x/estoquetesourodireto.csv")],
        }

    def get(self, url: str, params: dict | None = None):
        if url.endswith("/package_show"):
            nome = self.ids[params["id"]]
            res = [{"id": f"{nome}-{i}", "name": f"{nome} {fmt}", "format": fmt, "url": u, "created": "2025-01-01T00:00:00", "last_modified": "2026-10-29T00:00:00", "size": str(len(self.arquivos.get(u, b"x")))} for i, (fmt, u) in enumerate(self.recursos[nome])]
            return _Resp(url, json.dumps({"success": True, "result": {"id": params["id"], "title": nome, "resources": res}}, ensure_ascii=False).encode("utf-8"), "application/json")
        ct = "text/csv" if url.endswith(".csv") else "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        return _Resp(url, self.arquivos[url], ct)


# ---- BCB sintético, direto no staging (código -> valores por (ano, mes))
BCB_SINTETICO = {
    4607: ("fluxo", "R$ milhões", {a: 50_000.0 for a in ("mensal",)}),
}


def stg_bcb_sintetico(path, cfg_s16: dict) -> None:
    fichas = {int(s["codigo"]): s for s in cfg_s16["series"]}
    mensal = {  # fluxos: valor por mês; saldos e taxas: valor em cada mês (constante no ano, salvo dezembro de 2023 para variações)
        4607: {2023: 45_000.0, 2024: 50_000.0, 2025: 55_000.0},
        4608: {2023: 1_500.0, 2024: 20_000.0, 2025: 2_000.0},
        4606: {2023: 46_500.0, 2024: 70_000.0, 2025: 57_000.0},
        4616: {2023: 55_000.0, 2024: 80_000.0, 2025: 60_000.0},
        4640: {2023: 20_000.0, 2024: 3_700.0, 2025: 1_000.0},
        4639: {2023: 20_100.0, 2024: 4_000.0, 2025: 1_100.0},
        4574: {2023: 65_000.0, 2024: 57_000.0, 2025: 50_000.0},
        4573: {2023: 66_000.0, 2024: 75_000.0, 2025: 51_000.0},
    }
    saldos = {
        4469: {2023: 5_600_000.0, 2024: 6_400_000.0, 2025: 6_500_000.0},
        4468: {2023: 5_650_000.0, 2024: 6_170_000.0, 2025: 6_200_000.0},
        4478: {2023: 6_600_000.0, 2024: 7_200_000.0, 2025: 7_300_000.0},
        4504: {2023: 51.3, 2024: 54.66, 2025: 55.0},
        4503: {2023: 51.7, 2024: 52.4, 2025: 52.5},
        4513: {2023: 60.4, 2024: 61.3, 2025: 61.5},
        4536: {2023: 59.6, 2024: 63.2, 2025: 63.5},
        13762: {2023: 73.8, 2024: 76.27, 2025: 76.5},
        4151: {2023: 8_520_000.0, 2024: 9_500_025.51, 2025: 9_580_000.0},
        4154: {2023: 6_200_028.58, 2024: 7_000_025.51, 2025: EST_FIM_2025_03 - 370_000.0},
        4152: {2023: 2_300_000.0, 2024: 2_500_000.0, 2025: 2_550_000.0},
        4177: {2023: 48.39, 2024: 48.57, 2025: round(3_500_000.0 / (EST_FIM_2025_03 - 370_000.0) * 100, 2)},
        4178: {2023: 16.13, 2024: 22.86, 2025: 0.0},
        4173: {2023: 0.3, 2024: 0.06, 2025: 0.05},
        10618: {2023: 46.1, 2024: 47.0, 2025: 46.5},
    }
    rows = []
    for codigo, por_ano in {**mensal, **saldos}.items():
        f = fichas[codigo]
        for ano, v in por_ano.items():
            for m in range(1, 13 if ano < 2025 else 4):
                rows.append({"codigo": codigo, "data_texto": f"01/{m:02d}/{ano}", "valor_texto": str(v), "data": date(ano, m, 1), "valor": float(v), "nome": f["nome"], "tipo": f["tipo"], "unidade": f["unidade"], "uso": f.get("uso"),
                             "titulo_bc": f"Título BC {codigo}", "snapshot_id": "snap-bcb", "sha256_canonical": "abc", "fetched_at": "2026-11-10T00:00:00+00:00", "primeira_data": date(2023, 1, 1), "ultima_data": date(2025, 3, 1)})
    pl.DataFrame(rows).write_parquet(path)


@pytest.fixture
def staging_divida(populated_store, config_dir, tmp_path):
    """Um staging só com tudo: RREO sintético, dívida (via fetch falso + transform real), BCB sintético e COFOG sintético."""
    staging = tmp_path / "staging"
    build_staging(populated_store, config_dir, staging)
    cfg_s1 = source_config(CONFIG_DIR, "S1")
    run_fetch_divida(FakeTesouroDivida(), populated_store, cfg_s1)
    build_staging_divida(populated_store, staging, cfg_s1)
    stg_bcb_sintetico(staging / "stg_bcb_sgs.parquet", source_config(CONFIG_DIR, "S16"))
    # COFOG sintético de test_mart_cofog (juros 600 em 2024)
    from recibo.fetchers.cofog import run_fetch as run_fetch_cofog
    from recibo.transform.cofog import build_staging as build_staging_cofog
    from test_cofog import FakeCkan
    from test_mart_cofog import _csv as cofog_csv

    fake = FakeCkan()
    fake.csv = {2024: cofog_csv(2024, 1.0), 2023: cofog_csv(2023, 0.9)}
    fake.resources[1]["size"] = str(len(fake.csv[2024]))
    fake.resources[2]["size"] = str(len(fake.csv[2023]))
    cfg7 = source_config(CONFIG_DIR, "S7")
    run_fetch_cofog(fake, populated_store, cfg7)
    build_staging_cofog(populated_store, staging, {**cfg7, "total_minimo_reais": 1, "total_maximo_reais": 1e13, "unidade_valor_por_exercicio": {}})
    return staging


def _medida(res, grupo: str, id_: str):
    return next(m for m in res.medidas[grupo] if m.id == id_)


def test_grade_le_rotulos_com_notas_de_rodape_e_colunas_do_ano():
    g = ler_xlsx_grade(xlsx_fatores_tesouro())
    p = Planilha(g, "2.9")
    assert p.cab == 7 and p.col_ano[2024] > p.col_mes[(2024, 12)] and (2025, 3) in p.col_mes and 2025 not in p.col_ano
    assert normalizar_rotulo("Estoque anterior1") == "ESTOQUE ANTERIOR" and normalizar_rotulo("DPFe ¹") == "DPFE" and normalizar_rotulo("- Juros Apropriados da DPMFi 9") == "- JUROS APROPRIADOS DA DPMFI"
    assert p.valor_mes("I.2 - Juros Apropriados", 2024, 1) == 60_000.0 and p.valor_mes("Estoque anterior", 2024, 1) == EST_ANT_2024
    assert p.valor_anual("I.1.1 - Emissões", 2024) == (12 * EMISSOES_MES, "coluna_anual")
    assert p.valor_anual("I.1.1 - Emissões", 2025)[1] == "soma_de_3_meses" and p.valor_anual("I.1.1 - Emissões", 2025)[0] == 3 * 100_000.0
    assert p.valor_anual("I.1.1 - Emissões", 2030) == (None, "ano_ausente") and p.ultimo_mes(2025) == 3 and p.ultimo_mes(2024) == 12
    with pytest.raises(GradeFormato) as e:
        p.linha("I.9 - Não existe")
    assert "não encontrado" in str(e.value)
    with pytest.raises(GradeFormato):
        Planilha(g, "aba que não existe")
    e2 = Planilha(ler_xlsx_grade(xlsx_estoque_tesouro()), "2.1")
    assert e2.valor_mes("DPFe", 2024, 12) == pytest.approx(EST_FIM_2024 / 1000 - 7_000.0) and e2.valor_mes("DPMFi EM PODER DO BANCO CENTRAL", 2024, 12) == 2_500.0
    assert e2.valor_mes("DPMFi", 2024, 12) == 7_000.0  # primeira ocorrência do rótulo (a do estoque em poder do público)


def test_mart_divida_2024_fecha_e_publica(staging_divida, config_dir, tmp_path):
    linhas = load_yaml(config_dir, "linhas.yaml")
    fontes = load_yaml(config_dir, "sources.yaml")
    res = compute_divida(staging_divida, linhas, fontes, 2024, stg_siconfi=staging_divida / "stg_siconfi_rreo.parquet", periodo_recibo=6, config_dir=config_dir)
    assert res.mes_referencia == 12 and res.fechado and res.publicavel, (res.selo, [(r.id, r.diff) for r in res.reconciliacoes if not r.ok], res.avisos)
    # 1. juros: cinco réguas
    assert _medida(res, "juros", "juros_pagos").valor == 350.0
    assert _medida(res, "juros", "juros_execucao_dpf").valor == 350.0 and _medida(res, "juros", "juros_execucao_dpf").componentes[0]["valor"] == 4.0
    assert _medida(res, "juros", "juros_apropriados_dpf").valor == pytest.approx(12 * (JUROS_DPMFI_MES + JUROS_DPFE_MES) * MI)
    assert _medida(res, "juros", "juros_bcb_gf").valor == pytest.approx(12 * 50_000.0 * MI) and _medida(res, "juros", "juros_bcb_gc").valor == pytest.approx(12 * 70_000.0 * MI)
    assert _medida(res, "juros", "juros_rreo_brutos").valor == 500.0 and _medida(res, "juros", "juros_rreo_liquidos").valor == 400.0
    assert _medida(res, "juros", "juros_cofog").valor == 600.0
    # 2. dívida nova
    vd = _medida(res, "divida_nova", "variacao_dpf")
    assert vd.valor == pytest.approx(VAR_2024 * MI) and [c["id"] for c in vd.componentes] == ["estoque_anterior", "emissoes", "resgates", "juros", "operacoes_bc", "estoque_fim"]
    assert vd.componentes[-1]["valor"] == pytest.approx(EST_FIM_2024 * MI)
    assert _medida(res, "divida_nova", "variacao_dcl_rreo").valor == 120.0
    assert _medida(res, "divida_nova", "variacao_dlsp_gf").valor == pytest.approx(800_000.0 * MI)
    assert _medida(res, "divida_nova", "nominal_bcb_gc").valor == pytest.approx(12 * 75_000.0 * MI) and _medida(res, "divida_nova", "primario_bcb_gc").valor == pytest.approx(48_000.0 * MI)
    # 3. estoque
    assert _medida(res, "estoque", "dpf_publico").valor == pytest.approx(EST_FIM_2024 * MI)
    assert _medida(res, "estoque", "dpf_publico_csv").valor == pytest.approx(EST_FIM_2024 * MI)
    assert _medida(res, "estoque", "carteira_bc").valor == pytest.approx(2_500 * BI)
    assert _medida(res, "estoque", "dc_rreo").valor == 9220.0 and _medida(res, "estoque", "dcl_rreo").valor == 6120.0
    assert _medida(res, "estoque", "dlsp_gf").valor == pytest.approx(6_400_000.0 * MI) and _medida(res, "estoque", "dlsp_gf_pib").valor == 54.66
    assert _medida(res, "estoque", "dbgg_pib").valor == 76.27 and _medida(res, "estoque", "dbgg_pib").unidade == "% do PIB"
    td = _medida(res, "estoque", "tesouro_direto")
    assert td.valor == pytest.approx(150 * BI) and td.componentes[0]["nome"] == "Tesouro Selic"
    # 4. rolagem
    v12 = _medida(res, "rolagem", "vincendo_12m")
    assert v12.valor == pytest.approx(3_400 / 7_359.5 * 100) and v12.componentes[0]["valor"] == pytest.approx(3_400 * BI) and "2025-12-31" in v12.fonte
    assert _medida(res, "rolagem", "prazo_medio_bcb").valor == 47.0 and _medida(res, "rolagem", "prazo_medio_bcb").unidade == "meses"
    comp = {c["id"]: c for c in res.composicao}
    assert comp["selic"]["share_dpmfi"] == pytest.approx(3_400 / 7_000) and comp["prefixado"]["share_dpmfi"] == pytest.approx(1_600 / 7_000)
    assert comp["prefixado"]["valor"] == pytest.approx(1_609.5 * BI) and comp["prefixado"]["valor_interna"] == pytest.approx(1_600 * BI)  # Global BRL é prefixado, mas externo
    assert comp["cambio"]["valor"] == pytest.approx(350 * BI) and comp["demais"]["valor"] == pytest.approx(100 * BI) and comp["precos"]["share_dpf"] == pytest.approx(1_900 / 7_359.5)
    assert sum(c["share_dpf"] for c in res.composicao) == pytest.approx(1.0)
    assert _medida(res, "rolagem", "indexador_cambio_bcb").valor == 0.06
    assert _medida(res, "juros", "juros_execucao_dpf").componentes[-1]["id"] == "politica_monetaria_juros"  # 2024 sem linhas repetidas
    assert not _medida(res, "rolagem", "colchao_liquidez").disponivel and "sem fonte aberta" in _medida(res, "rolagem", "colchao_liquidez").motivo
    assert not _medida(res, "rolagem", "detentores").disponivel
    # reconciliações: todas OK, inclusive as de folga pequena
    recs = {r.id: r for r in res.reconciliacoes}
    assert recs["dpf_fatores_identidade"].ok and recs["dpf_fatores_identidade"].critica and abs(recs["dpf_fatores_identidade"].diff) < 1.0
    assert recs["dpf_estoque_xlsx_vs_csv"].ok and recs["dpf_estoque_xlsx_vs_fatores"].ok and recs["fatores_coluna_anual_vs_soma_meses"].ok
    assert recs["carteira_bc_tesouro_vs_bcb"].ok and recs["dpmfi_mercado_tesouro_vs_bcb"].ok and recs["dpmfi_mercado_tesouro_vs_bcb"].diff == pytest.approx(25.51 * MI)
    assert recs["juros_anexo01_vs_execucao_dpf"].ok and recs["principal_anexo01_vs_execucao_dpf"].ok and recs["cobertura_bc_execucao_vs_recibo"].ok
    assert recs["cobertura_bc_execucao_vs_recibo"].esperado == 10.0 and recs["cobertura_bc_execucao_vs_recibo"].obtido == 10.0
    assert recs["indexador_selic_tesouro_vs_bcb"].ok and abs(recs["indexador_selic_tesouro_vs_bcb"].diff) < 0.01 and recs["indexador_prefixado_tesouro_vs_bcb"].ok
    assert res.fontes["estoque_xlsx"]["unidade_arquivo"] == "bilhoes" and res.fontes["fatores_xlsx"]["unidade_arquivo"] == "milhoes" and res.fontes["recibo"]["disponivel"]
    assert res.fontes["cofog"]["disponivel"] and res.fontes["ultimo_mes_por_fonte"]["estoque_csv"] == 12
    # a execução recortada por mês (é o que o mart usa para comparar com um RREO de bimestre parcial)
    from recibo.marts.divida import execucao_do_ano

    idx = pl.read_parquet(staging_divida / "stg_divida_arquivos.parquet")
    nome = idx.filter(pl.col("dataset") == "execucao_natureza")["staging"][0]
    exe3 = execucao_do_ano(pl.read_parquet(staging_divida / nome), linhas["divida"]["execucao_csv"], 2024, 3)
    assert exe3["meses"] == 3 and exe3["juros"] == 250.0 and exe3["principal"] == 1360.0 and exe3["politica_monetaria_juros"] == 4.0


def test_mart_divida_ano_em_andamento_e_ausente(staging_divida, config_dir):
    linhas = load_yaml(config_dir, "linhas.yaml")
    fontes = load_yaml(config_dir, "sources.yaml")
    # 2025: planilhas, CSVs e BC só até março; o recibo sintético não tem 2025 -> medidas do RREO indisponíveis, o resto sai
    res = compute_divida(staging_divida, linhas, fontes, 2025, stg_siconfi=staging_divida / "stg_siconfi_rreo.parquet", periodo_recibo=None, config_dir=config_dir)
    assert res.mes_referencia == 3 and not res.fechado and "em andamento: até o mês 03/2025" in res.selo
    assert _medida(res, "juros", "juros_apropriados_dpf").valor == pytest.approx(3 * (30_000.0 + 3_333.3) * MI)
    assert _medida(res, "juros", "juros_bcb_gf").valor == pytest.approx(3 * 55_000.0 * MI)
    assert not _medida(res, "juros", "juros_pagos").disponivel and "nenhum período" in _medida(res, "juros", "juros_pagos").motivo
    exe25 = _medida(res, "juros", "juros_execucao_dpf")
    assert exe25.valor == 32.0  # três meses de 10 mais a cobertura do BC (2), contada uma vez
    assert exe25.componentes[0]["valor"] == 2.0 and exe25.componentes[1]["id"] == "linhas_repetidas" and exe25.componentes[1]["valor"] == 2.0 and "principal repetido 20.00" in exe25.componentes[1]["nome"]
    assert any("2 linha(s) repetida(s)" in a and "22.00" in a for a in res.avisos)
    assert _medida(res, "estoque", "dpf_publico").valor == pytest.approx(EST_FIM_2025_03 * MI) and _medida(res, "estoque", "dpf_publico_csv").valor == pytest.approx(EST_FIM_2025_03 * MI)
    assert _medida(res, "divida_nova", "variacao_dlsp_gf").valor == pytest.approx(100_000.0 * MI)  # março de 2025 menos dezembro de 2024
    recs = {r.id: r for r in res.reconciliacoes}
    assert recs["dpf_fatores_identidade"].ok and recs["dpf_estoque_xlsx_vs_csv"].ok and "fatores_coluna_anual_vs_soma_meses" not in recs
    assert not recs["juros_anexo01_vs_execucao_dpf"].ok and not recs["juros_anexo01_vs_execucao_dpf"].calculada and "fonte indisponível" in recs["juros_anexo01_vs_execucao_dpf"].detalhe
    assert all(r.calculada for r in res.reconciliacoes if r.critica)
    assert recs["dpmfi_mercado_tesouro_vs_bcb"].ok and recs["indexador_selic_tesouro_vs_bcb"].ok and recs["indexador_prefixado_tesouro_vs_bcb"].ok
    assert res.publicavel  # as críticas (Tesouro contra Tesouro) fecham; o que falta está marcado, não escondido
    with pytest.raises(SemDados) as e:
        compute_divida(staging_divida, linhas, fontes, 2030, config_dir=config_dir)
    assert "nenhuma fonte da dívida tem o ano 2030" in str(e.value)


def test_classificacao_indexador_e_duplicatas(config_dir):
    from recibo.marts.divida import classificar_indexador, execucao_do_ano
    from recibo.transform.divida import ler_csv_generico

    linhas = load_yaml(config_dir, "linhas.yaml")
    idx = linhas["divida"]["estoque_csv"]["indexadores"]
    assert classificar_indexador("Global BRL 2028", True, idx) == "prefixado"
    assert classificar_indexador("Global 2034A", True, idx) == "cambio" and classificar_indexador("BIRD9172-BR", True, idx) == "cambio"
    assert classificar_indexador("NTN-A3", False, idx) == "cambio" and classificar_indexador("LFT 010327", False, idx) == "selic"
    assert classificar_indexador("TDA27070300", False, idx) == "demais" and classificar_indexador("NTN-B 150535", False, idx) == "precos"
    cfg = linhas["divida"]["execucao_csv"]
    df = ler_csv_generico(csv_execucao())
    e24 = execucao_do_ano(df, cfg, 2024, 12)
    assert e24["linhas_repetidas"] == 0 and e24["juros"] == 350.0 and e24["politica_monetaria_juros"] == 4.0 and e24["juros_sem_politica_monetaria"] == 346.0
    e25 = execucao_do_ano(df, cfg, 2025, 3)
    assert e25["linhas_repetidas"] == 2 and e25["valor_repetido"] == 22.0 and e25["valor_repetido_juros"] == 2.0 and e25["valor_repetido_principal"] == 20.0 and e25["juros"] == 32.0 and e25["principal"] == 20.0
    assert e25["politica_monetaria_juros"] == 2.0 and e25["politica_monetaria_principal"] == 20.0
    assert e25["repetidas_detalhe"][0]["tipo"] == "Política Monetária" and e25["repetidas_detalhe"][0]["natureza"].startswith("COBERTURA")
    # sem a regra de duplicatas, a repetição entra duas vezes (é o que o arquivo diz)
    e25b = execucao_do_ano(df, {**cfg, "duplicatas_ignorando": []}, 2025, 3)
    assert e25b["linhas_repetidas"] == 0 and e25b["juros"] == 34.0


def test_mart_divida_identidade_quebrada_nao_publica(staging_divida, config_dir, tmp_path):
    linhas = load_yaml(config_dir, "linhas.yaml")
    fontes = load_yaml(config_dir, "sources.yaml")
    # mexe nos juros de dezembro da planilha de fatores: a identidade do Tesouro deixa de fechar
    idx = pl.read_parquet(staging_divida / "stg_divida_arquivos.parquet")
    nome = idx.filter(pl.col("dataset") == "fatores_variacao")["staging"][0]
    g = pl.read_parquet(staging_divida / nome)
    p = Planilha(g, "2.9")
    lin, col = p.linha("I.2 - Juros Apropriados"), p.col_mes[(2024, 12)]
    g2 = g.with_columns(pl.when((pl.col("linha") == lin) & (pl.col("coluna") == col)).then(pl.lit(61_000.0)).otherwise(pl.col("valor_num")).alias("valor_num"))
    g2.write_parquet(staging_divida / nome)
    res = compute_divida(staging_divida, linhas, fontes, 2024, stg_siconfi=staging_divida / "stg_siconfi_rreo.parquet", periodo_recibo=6, config_dir=config_dir)
    recs = {r.id: r for r in res.reconciliacoes}
    assert not recs["dpf_fatores_identidade"].ok and recs["dpf_fatores_identidade"].diff == pytest.approx(-1_000.0 * MI)
    assert not res.publicavel and "NÃO PUBLICÁVEL: ['dpf_fatores_identidade']" in res.selo


def test_mart_divida_sem_bcb_e_export(staging_divida, config_dir, tmp_path, capsys):
    linhas = load_yaml(config_dir, "linhas.yaml")
    fontes = load_yaml(config_dir, "sources.yaml")
    (staging_divida / "stg_bcb_sgs.parquet").unlink()
    res = compute_divida(staging_divida, linhas, fontes, 2024, stg_siconfi=staging_divida / "stg_siconfi_rreo.parquet", periodo_recibo=6, config_dir=config_dir)
    assert res.publicavel  # as críticas são Tesouro contra Tesouro
    assert not _medida(res, "estoque", "dbgg_pib").disponivel and "staging do BCB não existe" in _medida(res, "estoque", "dbgg_pib").motivo
    assert any("fonte indisponível: bcb" in a for a in res.avisos)
    export = to_export_divida(res, linhas)
    assert export["produto"] == "divida" and [g["id"] for g in export["grupos"]] == ["juros", "divida_nova", "estoque", "rolagem", "por_habitante"]
    # sem staging do IBGE: o grupo por habitante sai inteiro como indisponível, com o motivo, e o produto segue publicável
    ph = {m["id"]: m for m in export["grupos"][4]["medidas"]}
    assert set(ph) == {"populacao", "dpf_publico_por_habitante", "variacao_dpf_por_habitante", "dlsp_gf_por_habitante", "dcl_rreo_por_habitante", "juros_pagos_por_habitante"}
    assert all(not m["disponivel"] and "staging do IBGE ausente" in m["motivo"] for m in ph.values())
    assert "populacao_projecao_vs_estimativa" not in {r["id"] for r in export["reconciliacoes"]}
    assert export["glossario"][0]["termo"] == "DPF, DPMFi e DPFe" and export["fontes"]["indisponiveis"]["bcb"]
    p_json, p_csv, p_exp = write_outputs_divida(export, tmp_path / "marts", tmp_path / "export")
    assert p_json.name == "divida_2024.json" and p_exp.exists()
    linhas_csv = p_csv.read_text(encoding="utf-8").splitlines()
    assert linhas_csv[0] == "grupo,id,nome,valor,unidade,disponivel,fonte" and len(linhas_csv) == 1 + sum(len(g["medidas"]) for g in export["grupos"])
    back = json.loads(p_exp.read_text(encoding="utf-8"))
    assert back["publicavel"] is True and back["mes_referencia"] == 12
    print_summary_divida(export)
    out = capsys.readouterr().out
    assert "Dívida 2024 | publicável: True" in out and "Juros do ano: o mesmo ano, cinco réguas" in out
    assert "Pagos em dinheiro pelo orçamento (a linha do recibo)" in out and "R$                   350.00" in out
    assert "(indisponível: staging do BCB não existe" in out and "[OK ] dpf_fatores_identidade (crítica)" in out and "fontes (snapshot | recurso):" in out


def test_execucao_por_carteira_no_export(staging_divida, config_dir):
    """A execução do ano abre por carteira (mercado, Banco Central, cobertura do BC...) e a soma fecha com o total;
    os grupos originais do arquivo ficam dentro de cada carteira. É o segundo nível de "juros" e de "dívida antiga
    paga" no site."""
    from recibo.marts.divida import classificar_carteira

    linhas = load_yaml(config_dir, "linhas.yaml")
    fontes = load_yaml(config_dir, "sources.yaml")
    res = compute_divida(staging_divida, linhas, fontes, 2024, stg_siconfi=staging_divida / "stg_siconfi_rreo.parquet", periodo_recibo=6, config_dir=config_dir)
    exe = to_export_divida(res, linhas)["execucao"]
    assert exe["disponivel"] and exe["juros"] == 350.0 and exe["principal"] == 1660.0 and exe["meses"] == 12
    por = {c["id"]: c for c in exe["por_carteira"]}
    assert [c["id"] for c in exe["por_carteira"]] == ["mercado", "banco_central", "cobertura_bc"]
    assert por["mercado"]["juros"] == 300.0 and por["mercado"]["principal"] == 1000.0
    assert por["banco_central"]["juros"] == 46.0 and por["banco_central"]["principal"] == 654.0
    assert por["cobertura_bc"]["juros"] == 4.0 and por["cobertura_bc"]["principal"] == 6.0
    assert sum(c["juros"] for c in exe["por_carteira"]) == exe["juros"] and sum(c["principal"] for c in exe["por_carteira"]) == exe["principal"]
    assert por["mercado"]["grupos_originais"][0] == {"tipo": "Interna", "modalidade": "Mobiliária", "carteira": "MERCADO", "juros": 300.0, "principal": 1000.0}
    assert "Relatório Mensal da Dívida" in exe["nota"] and por["mercado"]["definicao"].startswith("Juros e principal pagos")
    # 2025 (ano em andamento): a cobertura do BC repetida sob dois tipos conta uma vez e cai numa carteira só
    res25 = compute_divida(staging_divida, linhas, fontes, 2025, stg_siconfi=staging_divida / "stg_siconfi_rreo.parquet", periodo_recibo=2, config_dir=config_dir)
    por25 = {c["id"]: c for c in to_export_divida(res25, linhas)["execucao"]["por_carteira"]}
    assert por25["cobertura_bc"]["juros"] == 2.0 and por25["cobertura_bc"]["principal"] == 20.0 and por25["mercado"]["juros"] == 30.0
    # a classificação olha o texto normalizado, e a cobertura do BC vence qualquer rótulo
    assert classificar_carteira("Externa", "Mobiliária", "MERCADO", False) == "externa"
    assert classificar_carteira("Interna", "Contratual", "", False) == "contratos"
    assert classificar_carteira("Interna", "Mobiliária", "Banco Central", False) == "banco_central"
    assert classificar_carteira("Interna", "Política Monetária", "BACEN_", True) == "cobertura_bc"
    assert classificar_carteira("", "", "", False) == "outros"


def test_estoque_detalhe_conta_do_ano_e_mensal_no_export(staging_divida, config_dir):
    """Os blocos da visão "A dívida" (v0.10.0): a DPF em mercado em células índice x família x faixa de vencimento, cuja
    soma é o total do CSV ao centavo; a conta do ano pelos fatores do Tesouro, que fecha; e o estoque mês a mês."""
    from recibo.marts.divida import faixa_de, familia_do_titulo

    linhas = load_yaml(config_dir, "linhas.yaml")
    fontes = load_yaml(config_dir, "sources.yaml")
    res = compute_divida(staging_divida, linhas, fontes, 2024, stg_siconfi=staging_divida / "stg_siconfi_rreo.parquet", periodo_recibo=6, config_dir=config_dir)
    exp = to_export_divida(res, linhas)
    det = exp["estoque_detalhe"]
    assert det["disponivel"] and det["mes"] == 12 and det["exercicio"] == 2024 and det["fechado"]
    assert det["total"] == pytest.approx(7_359.5 * BI)
    assert sum(c["valor"] for c in det["celulas"]) == pytest.approx(det["total"])
    faixas = {f["id"]: f for f in det["faixas"]}
    assert list(faixas) == ["ate_12m", "de_1_a_2_anos", "de_2_a_5_anos", "mais_de_5_anos"]  # sem "sem_data": todos têm vencimento
    assert faixas["ate_12m"]["valor"] == pytest.approx(3_400 * BI)          # LFT 01/03/2025, 3 meses depois de 12/2024
    assert faixas["de_1_a_2_anos"]["valor"] == pytest.approx(1_000 * BI)    # LTN 01/01/2026, 13 meses
    assert faixas["de_2_a_5_anos"]["valor"] == pytest.approx(709.5 * BI)    # NTN-F 2029, TDA 2027, Global BRL 2028
    assert faixas["mais_de_5_anos"]["valor"] == pytest.approx(2_250 * BI)   # NTN-B 2035, Global 2034, BIRD 2050
    assert faixas["ate_12m"]["share"] == pytest.approx(3_400 / 7_359.5)
    cel = {(c["indexador"], c["familia"], c["faixa"], c["periodo"]): c for c in det["celulas"]}
    assert cel[("selic", "LFT", "ate_12m", "2025-03")]["valor"] == pytest.approx(3_400 * BI) and cel[("selic", "LFT", "ate_12m", "2025-03")]["periodo_nome"] == "março de 2025"
    assert cel[("prefixado", "LTN", "de_1_a_2_anos", "2026")]["periodo_nome"] == "2026"
    assert cel[("prefixado", "Global BRL", "de_2_a_5_anos", "2028")]["externa"] is True and cel[("cambio", "BIRD9172-BR", "mais_de_5_anos", "2050")]["titulos"] == 1
    assert [i["id"] for i in det["indexadores"]] == ["selic", "prefixado", "precos", "cambio", "demais"] and "soma das células" in det["nota"]
    assert familia_do_titulo("NTN-B 150535") == "NTN-B" and familia_do_titulo("Global BRL 2028") == "Global BRL" and familia_do_titulo("BIRD9172-BR") == "BIRD9172-BR" and familia_do_titulo("") == "(sem nome)"
    fx = linhas["divida"]["estoque_csv"]["faixas_vencimento"]
    assert faixa_de(0, fx, {"id": "sem_data"})["id"] == "ate_12m" and faixa_de(12, fx, {})["id"] == "ate_12m" and faixa_de(13, fx, {})["id"] == "de_1_a_2_anos" and faixa_de(61, fx, {})["id"] == "mais_de_5_anos" and faixa_de(None, fx, {"id": "sem_data"})["id"] == "sem_data"
    conta = exp["conta_do_ano"]
    assert conta["disponivel"] and conta["fecha"] and conta["meses"] == list(range(1, 13))
    assert conta["estoque_anterior"] == pytest.approx(EST_ANT_2024 * 1e6) and conta["estoque_fim"] == pytest.approx(7_359.5 * BI)
    assert conta["resgates"] < 0 < conta["emissoes"] and conta["variacao"] == pytest.approx(conta["estoque_fim"] - conta["estoque_anterior"])
    assert conta["estoque_anterior"] + conta["emissoes"] + conta["resgates"] + conta["juros_apropriados"] + conta["operacoes_bc"] == pytest.approx(conta["estoque_fim"])
    assert conta["juros_apropriados"] == pytest.approx(conta["juros_dpmfi"] + conta["juros_dpfe"])
    mensal = exp["estoque_mensal"]
    assert [m["mes"] for m in mensal] == list(range(1, 13)) and mensal[-1]["dpf_publico"] == pytest.approx(7_359.5 * BI)
    # ano em andamento: só os meses que existem; o detalhe é do último mês presente em todas as fontes
    res25 = compute_divida(staging_divida, linhas, fontes, 2025, stg_siconfi=staging_divida / "stg_siconfi_rreo.parquet", periodo_recibo=2, config_dir=config_dir)
    exp25 = to_export_divida(res25, linhas)
    assert exp25["estoque_detalhe"]["mes"] == 3 and not exp25["estoque_detalhe"]["fechado"] and [m["mes"] for m in exp25["estoque_mensal"]] == [1, 2, 3]
    assert exp25["conta_do_ano"]["meses"] == [1, 2, 3]


def test_execucao_atrasada_vira_nao_comparavel(staging_divida, config_dir, tmp_path):
    """Quando o arquivo de execução da DPF no CKAN para antes do fim do bimestre do RREO (2026 real: publicado em agosto
    com dados até julho, RREO até agosto), as conferências contra ele saem como "não comparável", não como falha; o
    aviso e a fonte da medida dizem até que mês o arquivo vai."""
    import shutil

    import polars as pl

    from recibo.marts.divida import _indice, execucao_do_ano, localizar

    linhas = load_yaml(config_dir, "linhas.yaml")
    fontes = load_yaml(config_dir, "sources.yaml")
    cfg = linhas["divida"]["execucao_csv"]
    r = localizar(_indice(staging_divida), cfg)
    df = pl.read_parquet(staging_divida / r["staging"])
    assert execucao_do_ano(df, cfg, 2025, 3)["ultimo_mes_com_dados"] == 3 and execucao_do_ano(df, cfg, 2024, 12)["ultimo_mes_com_dados"] == 12
    # cópia do staging com dezembro de 2024 em branco: o arquivo "parou" em julho (o último mês com lançamento)
    atrasado = tmp_path / "staging_atrasado"
    shutil.copytree(staging_divida, atrasado)
    ex = pl.col(cfg["colunas"]["exercicio"]).cast(pl.Utf8).str.strip_chars() == "2024"
    cols = [pl.when(ex).then(pl.lit(None)).otherwise(pl.col(c)).alias(c) for c in ("dezembro", "dezembro__num") if c in df.columns]
    df.with_columns(cols).write_parquet(atrasado / r["staging"])
    exe = execucao_do_ano(pl.read_parquet(atrasado / r["staging"]), cfg, 2024, 12)
    assert exe["ultimo_mes_com_dados"] == 7 and exe["meses"] == 12 and exe["juros"] == 250.0
    res = compute_divida(atrasado, linhas, fontes, 2024, stg_siconfi=atrasado / "stg_siconfi_rreo.parquet", periodo_recibo=6, config_dir=config_dir)
    recs = {x.id: x for x in res.reconciliacoes}
    for rid in ("juros_anexo01_vs_execucao_dpf", "principal_anexo01_vs_execucao_dpf", "cobertura_bc_execucao_vs_recibo"):
        assert not recs[rid].calculada and recs[rid].detalhe.startswith("não comparável: a execução da DPF do Tesouro tem dados até o mês 07/2024") and "6º bimestre vai até o mês 12" in recs[rid].detalhe
    assert res.publicavel and any("execução da DPF 2024: a execução da DPF do Tesouro tem dados até o mês 07/2024" in a for a in res.avisos)
    exp = to_export_divida(res, linhas)
    assert exp["execucao"]["ultimo_mes_com_dados"] == 7
    m = next(x for g in exp["grupos"] if g["id"] == "juros" for x in g["medidas"] if x["id"] == "juros_execucao_dpf")
    assert "tem dados até o mês 07/2024, antes do mês de referência 12" in m["fonte"]
    # no staging íntegro nada muda: as conferências continuam calculadas e OK
    res_ok = compute_divida(staging_divida, linhas, fontes, 2024, stg_siconfi=staging_divida / "stg_siconfi_rreo.parquet", periodo_recibo=6, config_dir=config_dir)
    assert all(x.calculada and x.ok for x in res_ok.reconciliacoes if x.id in ("juros_anexo01_vs_execucao_dpf", "principal_anexo01_vs_execucao_dpf"))


def test_titulos_rreo_vs_emissoes_dpf(staging_divida, config_dir):
    """A conferência informativa nova (v0.10.2): títulos vendidos pelo RREO (operações de crédito + refinanciamento do
    Anexo 01) contra as emissões da DPF em mercado (fatores do Tesouro). No sintético os dois lados existem e diferem;
    a diferença fica registrada, não aprovada. Sem RREO do ano (2025 sintético), fica "não calculada"."""
    linhas = load_yaml(config_dir, "linhas.yaml")
    fontes = load_yaml(config_dir, "sources.yaml")
    res = compute_divida(staging_divida, linhas, fontes, 2024, stg_siconfi=staging_divida / "stg_siconfi_rreo.parquet", periodo_recibo=6, config_dir=config_dir)
    r = next(x for x in res.reconciliacoes if x.id == "titulos_rreo_vs_emissoes_dpf")
    assert r.calculada and not r.critica and r.esperado == pytest.approx(600.0 + 1500.0) and r.obtido == pytest.approx(12 * EMISSOES_MES * MI)
    assert "Perguntas abertas" in r.detalhe and "600.00" in r.detalhe and "1,500.00" in r.detalhe
    assert res.publicavel  # informativa: não trava
    res25 = compute_divida(staging_divida, linhas, fontes, 2025, stg_siconfi=staging_divida / "stg_siconfi_rreo.parquet", periodo_recibo=None, config_dir=config_dir)
    r25 = next(x for x in res25.reconciliacoes if x.id == "titulos_rreo_vs_emissoes_dpf")
    assert not r25.calculada and "fonte indisponível" in r25.detalhe
