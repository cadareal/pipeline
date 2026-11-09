"""Fixture: um RREO SINTÉTICO no formato REAL da API do SICONFI (calibrado em 2026-11-08 com 2024/6).

ATENÇÃO: os valores abaixo são inventados para testar a lógica (hierarquia, escopo, rateio, reconciliações).
Não têm relação com números reais do orçamento.

Formato observado:
  Anexo 02: cod_conta constante "RREO2TotalDespesas"; rotulo repete o nome da seção em toda linha;
            funções pelo nome; subfunções típicas pelo nome; "FUxx - Administração Geral" e
            "FUxx - Demais Subfunções" com prefixo da função-mãe.
  Anexo 01: cod_conta é código estável (JurosEEncargosDaDivida...), sufixo "Intra" nas intraorçamentárias.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from recibo.fetchers.siconfi import DATASET_RREO, SOURCE, canonical_items
from recibo.store import LocalBackend, Store

CONFIG_DIR = Path(__file__).resolve().parents[1] / "config"

COL_LIQ = "DESPESAS LIQUIDADAS ATÉ O BIMESTRE (d)"
COL_DOT = "DOTAÇÃO INICIAL"

# rotulo real: nome do BLOCO, repetido em toda linha dele
ROT_I = "TOTAL DAS DESPESAS EXCETO INTRA-ORÇAMENTÁRIAS"
ROT_II = "TOTAL DAS DESPESAS INTRA-ORÇAMENTÁRIAS"


def _row(exercicio, periodo, anexo, coluna, conta, valor, rotulo="", cod_conta=""):
    return {
        "exercicio": exercicio,
        "demonstrativo": "RREO",
        "periodo": periodo,
        "periodicidade": "B",
        "instituicao": "Governo Federal",
        "cod_ibge": 1,
        "uf": "BR",
        "populacao": 212583750,
        "anexo": anexo,
        "esfera": "U",
        "rotulo": rotulo,
        "coluna": coluna,
        "cod_conta": cod_conta,
        "conta": conta,
        "valor": valor,
    }


# Anexo 02, bloco I (exceto intra): (conta, valor), na ordem da API. cod_conta sempre "RREO2TotalDespesas".
# O bloco I termina com a linha-resumo "(II)" e o "TOTAL (III)", ambos com o rotulo do bloco I (como na API real).
ANEXO02_I = [
    ("DESPESAS (EXCETO INTRA-ORÇAMENTÁRIAS) (I)", 2610.0),
    ("Legislativa", 10.0),
    ("Ação Legislativa", 4.0),
    ("FU01 - Administração Geral", 5.0),
    ("FU01 - Demais Subfunções", 1.0),
    ("Defesa Nacional", 100.0),
    ("Defesa Aérea", 40.0),
    ("FU05 - Administração Geral", 50.0),
    ("FU05 - Demais Subfunções", 10.0),
    ("Assistência Social", 200.0),
    ("Assistência ao Idoso", 50.0),
    ("Assistência Comunitária", 150.0),
    ("Previdência Social", 1010.0),
    ("Previdência Básica", 900.0),
    ("Previdência do Regime Estatutário", 100.0),
    ("FU09 - Administração Geral", 10.0),
    ("Saúde", 200.0),
    ("Atenção Básica", 120.0),
    ("Assistência Hospitalar e Ambulatorial", 80.0),
    ("Trabalho", 90.0),
    ("Proteção e Benefícios ao Trabalhador", 90.0),
    ("Educação", 140.0),
    ("Ensino Superior", 70.0),
    ("FU12 - Demais Subfunções", 70.0),
    ("Encargos Especiais", 860.0),
    ("Serviço da Dívida Interna", 500.0),
    ("Transferências", 300.0),
    ("Outros Encargos Especiais", 20.0),
    ("FU28 - Demais Subfunções", 40.0),
    ("DESPESAS (INTRA-ORÇAMENTÁRIAS) (II)", 25.0),
    ("TOTAL (III) = (I + II)", 2635.0),
]
# Bloco II (detalhe intra por função), com o rotulo do bloco II
ANEXO02_II = [
    ("DESPESAS (INTRA-ORÇAMENTÁRIAS)", 25.0),
    ("Previdência Social", 25.0),
    ("Previdência do Regime Estatutário", 25.0),
]

# Anexo 01: (cod_conta, conta, valor), na ordem da API. rotulo vazio (exercita o fallback por ordem).
ANEXO01 = [
    ("DespesasExcetoIntraOrcamentarias", "DESPESAS (EXCETO INTRA-ORÇAMENTÁRIAS) (IX)", 2610.0),
    ("DespesasCorrentes", "DESPESAS CORRENTES", 2000.0),
    ("PessoalEEncargosSociais", "PESSOAL E ENCARGOS SOCIAIS", 300.0),
    ("JurosEEncargosDaDivida", "JUROS E ENCARGOS DA DÍVIDA", 350.0),
    ("OutrasDespesasCorrentes", "OUTRAS DESPESAS CORRENTES", 1350.0),
    ("TransferenciasAEstadosDistritoFederalEMunicipios", "Transferências a Estados, Distrito Federal e Municípios", 400.0),
    ("BeneficiosPrevidenciarios", "Benefícios Previdenciários", 920.0),
    ("DemaisDespesasCorrentes", "Demais Despesas Correntes", 30.0),
    ("DespesasDeCapital", "DESPESAS DE CAPITAL", 610.0),
    ("Investimentos", "INVESTIMENTOS", 60.0),
    ("InversoesFinanceiras", "INVERSÕES FINANCEIRAS", 390.0),
    ("AmortizacaoDaDivida", "AMORTIZAÇÃO DA DÍVIDA", 160.0),  # 10 a mais do que 843 - juros: fora de 843/844
    ("DespesasIntraOrcamentariasTotal", "DESPESAS (INTRA-ORÇAMENTÁRIAS) (X)", 25.0),
    ("SubtotalDasDespesas", "SUBTOTAL DAS DESPESAS (XI) = (IX + X)", 2635.0),
    ("AmortizacaoRefinanciamentoDaDivida", "AMORTIZAÇÃO DA DÍVIDA / REFINANCIAMENTO (XII)", 1500.0),
    ("AmortizacaoRefinanciamentoDaDividaInterna", "Amortização da Dívida Interna", 1500.0),
    ("TotalDespesas", "TOTAL DAS DESPESAS (XIII) = (XI + XII)", 4135.0),
    ("DespesasIntraOrcamentarias", "DESPESAS (INTRA-ORÇAMENTÁRIAS) (X)", 25.0),
    ("PessoalEEncargosSociaisIntra", "PESSOAL E ENCARGOS SOCIAIS", 25.0),
]

# Resultado esperado para o recibo (ver linhas.yaml):
ESPERADO = {
    "previdencia_inss": 900.0,        # 271
    "inativos_pensionistas": 100.0,   # 272 explícita só sob 09; intra (25) fora
    "assistencia": 200.0,
    "saude": 200.0,
    "educacao": 140.0,                # 70 + 70 do balde "Demais" (FUNDEB dentro)
    "trabalho": 90.0,
    "defesa_seguranca": 100.0,        # inclui FU05 adm geral e demais
    "transferencias": 300.0,          # 845
    "juros": 350.0,                   # Anexo 01, cod_conta
    "maquina_demais": 70.0,           # Legislativa 10 + FU09 adm 10 + 846 20 + FU28 demais 40 - 10 (serviço fora de 843/844)
}
TOTAL_I = 2610.0
SERVICO_BRUTO = 500.0
REFINANCIAMENTO_ANEXO01 = 1500.0
AMORTIZACAO_ANEXO01 = 160.0
FORA_843_844 = 10.0                  # (350 + 160) - 500
TOTAL_RECIBO = TOTAL_I - AMORTIZACAO_ANEXO01  # 2450: identidade do recibo


def _pages(items: list[dict]) -> list[str]:
    """Simula duas páginas da API para exercitar a concatenação."""
    half = len(items) // 2
    p1 = {"items": items[:half], "hasMore": True, "limit": half, "offset": 0, "count": half}
    p2 = {"items": items[half:], "hasMore": False, "limit": half, "offset": half, "count": len(items) - half}
    return [json.dumps(p1, ensure_ascii=False), json.dumps(p2, ensure_ascii=False)]


def build_items(exercicio: int = 2024, periodo: int = 6, escala: float = 1.0, deltas: dict[str, float] | None = None) -> dict[str, list[dict]]:
    """Itens da API para um (exercicio, periodo). `deltas` soma valores a contas do Anexo 02 ANTES da escala
    (use pares que somem zero e ajuste a função-mãe junto, para as identidades continuarem valendo)."""
    deltas = deltas or {}
    a02 = []
    for coluna, mult in ((COL_LIQ, 1.0), (COL_DOT, 1.3)):
        for conta, v in ANEXO02_I:
            val = round((v + deltas.get(conta, 0.0)) * escala * mult, 2)
            a02.append(_row(exercicio, periodo, "RREO-Anexo 02", coluna, conta, val, rotulo=ROT_I, cod_conta="RREO2TotalDespesas"))
        for conta, v in ANEXO02_II:
            a02.append(_row(exercicio, periodo, "RREO-Anexo 02", coluna, conta, round(v * escala * mult, 2), rotulo=ROT_II, cod_conta="RREO2TotalDespesas"))
    a01 = []
    for coluna, mult in ((COL_LIQ, 1.0), (COL_DOT, 1.3)):
        for cod, conta, v in ANEXO01:
            a01.append(_row(exercicio, periodo, "RREO-Anexo 01", coluna, conta, round(v * escala * mult, 2), rotulo="", cod_conta=cod))
    return {"RREO-Anexo 02": a02, "RREO-Anexo 01": a01}


# Exercício anterior sintético (2023/6): tudo a 90% de 2024, com 100 movidos de Assistência Comunitária (08) para
# Atenção Básica (10). Resultado: toda linha cresce 11,1% de 2023 para 2024, exceto saúde (-25,9%) e assistência
# (+122%), que a reconciliação informativa `variacao_anual` deve acusar. Totais e identidades continuam valendo.
ESCALA_2023 = 0.9
DELTAS_2023 = {"Saúde": 100.0, "Atenção Básica": 100.0, "Assistência Social": -100.0, "Assistência Comunitária": -100.0}
PERIODOS_FIXTURE = [(2024, 6, 1.0, None), (2023, 6, ESCALA_2023, DELTAS_2023)]

N_ROWS_STAGING = len(PERIODOS_FIXTURE) * (2 * (len(ANEXO02_I) + len(ANEXO02_II)) + 2 * len(ANEXO01))


@pytest.fixture
def store(tmp_path) -> Store:
    return Store(LocalBackend(tmp_path / "data"))


@pytest.fixture
def populated_store(store) -> Store:
    index = {}
    for exercicio, periodo, escala, deltas in PERIODOS_FIXTURE:
        for anexo, items in build_items(exercicio, periodo, escala, deltas).items():
            key = f"{exercicio}/{periodo:02d}/{anexo}"
            pages = _pages(items)
            r = store.put(
                source=SOURCE,
                dataset=DATASET_RREO,
                key=key,
                filename="pages.jsonl",
                content=("\n".join(pages) + "\n").encode("utf-8"),
                canonical=canonical_items(items),
                meta={"request": {"sintetico": True}, "n_items": len(items), "n_pages": 2},
            )
            index[key] = {"status": "ok", "n_items": len(items), "snapshot_id": r.snapshot_id}
    store.write_index(SOURCE, DATASET_RREO, index)
    return store


@pytest.fixture
def config_dir() -> Path:
    return CONFIG_DIR


# ----------------------------------------------------------------------------------------------------------------
# SIOP sintético (S8), consistente com o RREO sintético acima: mesmas funções e subfunções, mesmos totais por
# função (2.610 fora do refinanciamento), juros GND 2 = 350 (348 em 843 + 2 em 846), amortização GND 6 = 160 (152 em 843
# + 8 em 846; os 10 da 846 são a ação 0669, fonte 1444: não é rolagem) e refinanciamento 1.500 (subfunção 841, GND 6,
# fonte 1443). Inativos pela definição
# contábil (elementos 01 e 03) = 110: 100 na 09/272 e 10 de pensões militares dentro de "FU05 - Administração Geral"
# (que no RREO estão na linha de defesa). Mais uma linha intra (modalidade 91) e uma do orçamento de investimento
# (esfera 30), que as conferências devem ignorar. Valores inventados.
# ----------------------------------------------------------------------------------------------------------------

SIOP_DATA_ATUALIZACAO = "2025-01-31T10:00:00"

# (esfera, modalidade, orgao, funcao, subfuncao, gnd, elemento, fonte, rp, acao, liquidado)
SIOP_BASE_2024 = [
    ("10", "90", "01000", "01", "031", "1", "11", "100", "1", "2004", 4.0),
    ("10", "90", "01000", "01", "122", "1", "11", "100", "1", "2000", 5.0),
    ("10", "90", "01000", "01", "126", "3", "39", "100", "2", "2000", 1.0),
    ("10", "90", "52000", "05", "151", "1", "11", "100", "1", "2058", 40.0),
    ("10", "90", "52000", "05", "122", "1", "11", "100", "1", "2000", 40.0),
    ("10", "90", "52000", "05", "122", "1", "03", "100", "1", "0179", 10.0),   # pensões militares dentro de Defesa
    ("10", "90", "52000", "05", "128", "4", "52", "100", "2", "20X0", 10.0),
    ("20", "90", "55000", "08", "241", "3", "39", "100", "2", "0561", 30.0),
    ("20", "90", "55000", "08", "241", "4", "52", "100", "6", "0561", 20.0),
    ("20", "90", "55000", "08", "244", "3", "41", "100", "1", "8442", 150.0),
    ("20", "90", "33000", "09", "271", "3", "54", "100", "1", "0E81", 900.0),
    ("20", "90", "33000", "09", "272", "1", "01", "100", "1", "0181", 100.0),
    ("20", "91", "33000", "09", "272", "1", "13", "100", "1", "09HB", 25.0),   # intra: patronal do RPPS
    ("20", "90", "33000", "09", "122", "1", "11", "100", "1", "2000", 10.0),
    ("20", "90", "36000", "10", "301", "3", "41", "100", "2", "219A", 90.0),
    ("20", "90", "36000", "10", "301", "4", "51", "100", "6", "8535", 30.0),  # emenda
    ("20", "90", "36000", "10", "302", "3", "41", "100", "2", "8585", 59.0),
    ("20", "90", "36000", "10", "302", "1", "11", "100", "1", "2000", 21.0),
    ("20", "90", "40000", "11", "331", "3", "48", "100", "1", "00H4", 90.0),
    ("10", "90", "26000", "12", "364", "1", "11", "100", "1", "20RK", 70.0),
    ("10", "31", "26000", "12", "847", "3", "41", "100", "1", "0E36", 70.0),
    ("10", "90", "71000", "28", "843", "2", "21", "1444", "0", "0455", 348.0),  # juros pagos com títulos (fonte 444)
    ("10", "90", "71000", "28", "843", "6", "71", "100", "0", "0455", 152.0),
    ("10", "30", "73000", "28", "845", "3", "81", "100", "1", "0045", 300.0),
    ("10", "90", "71000", "28", "846", "6", "71", "1444", "0", "0669", 8.0),   # cobertura do resultado do BC, parte amortização (fonte 444: não é rolagem)
    ("10", "90", "71000", "28", "846", "2", "21", "1444", "0", "0669", 2.0),   # cobertura do resultado do BC, parte juros
    ("10", "90", "71000", "28", "846", "3", "91", "100", "1", "0005", 10.0),
    ("10", "90", "71000", "28", "331", "3", "41", "100", "2", "00SY", 40.0),
    ("10", "90", "71000", "28", "841", "6", "71", "1443", "0", "0365", 1500.0),  # refinanciamento (GND 6, fonte 443)
    ("30", "90", "32000", "25", "752", "4", "51", "100", "2", "4101", 999.0),  # orçamento de investimento (fora)
]
SIOP_DIMS_ORDEM = ["esfera", "modalidade_aplicacao", "orgao", "funcao", "subfuncao", "gnd", "elemento_despesa", "fonte", "resultado_primario", "acao"]


def siop_base_2023() -> list[tuple]:
    """SIOP de 2023 consistente com o RREO sintético de 2023: as mesmas mudanças (100 de Assistência Comunitária
    para Atenção Básica) e tudo a 90%."""
    out = []
    for r in SIOP_BASE_2024:
        v = r[-1]
        if r[3:5] == ("10", "301") and r[9] == "219A":
            v += 100.0
        if r[3:5] == ("08", "244"):
            v -= 100.0
        out.append((*r[:-1], round(v * ESCALA_2023, 6)))
    return out

SIOP_ROTULOS = {
    "esfera": {"10": "Orçamento Fiscal", "20": "Orçamento da Seguridade Social", "30": "Orçamento de Investimento"},
    "modalidade_aplicacao": {"90": "Aplicação Direta", "91": "Aplicação Direta Decorrente de Operação entre Órgãos", "30": "Transferências a Estados e ao DF", "31": "Transferências a Estados e ao DF - Fundo a Fundo"},
    "orgao": {"01000": "Câmara dos Deputados", "52000": "Ministério da Defesa", "55000": "Ministério do Desenvolvimento Social", "33000": "Ministério da Previdência Social", "36000": "Ministério da Saúde", "40000": "Ministério do Trabalho", "26000": "Ministério da Educação", "71000": "Encargos Financeiros da União", "73000": "Transferências a Estados, DF e Municípios", "32000": "Ministério de Minas e Energia"},
    "funcao": {"01": "Legislativa", "05": "Defesa Nacional", "08": "Assistência Social", "09": "Previdência Social", "10": "Saúde", "11": "Trabalho", "12": "Educação", "25": "Energia", "28": "Encargos Especiais"},
    "subfuncao": {"031": "Ação Legislativa", "122": "Administração Geral", "126": "Tecnologia da Informação", "151": "Defesa Aérea", "128": "Formação de Recursos Humanos", "241": "Assistência ao Idoso", "244": "Assistência Comunitária", "271": "Previdência Básica", "272": "Previdência do Regime Estatutário", "301": "Atenção Básica", "302": "Assistência Hospitalar e Ambulatorial", "331": "Proteção e Benefícios ao Trabalhador", "364": "Ensino Superior", "847": "Transferências para a Educação Básica", "841": "Refinanciamento da Dívida Interna", "843": "Serviço da Dívida Interna", "845": "Outras Transferências", "846": "Outros Encargos Especiais", "752": "Energia Elétrica"},
    "gnd": {"1": "Pessoal e Encargos Sociais", "2": "Juros e Encargos da Dívida", "3": "Outras Despesas Correntes", "4": "Investimentos", "6": "Amortização da Dívida"},
    "elemento_despesa": {"01": "Aposentadorias do RPPS, Reserva Remunerada e Reformas dos Militares", "03": "Pensões do RPPS e do Militar", "11": "Vencimentos e Vantagens Fixas - Pessoal Civil", "13": "Obrigações Patronais", "21": "Juros sobre a Dívida por Contrato", "39": "Outros Serviços de Terceiros - PJ", "41": "Contribuições", "48": "Outros Auxílios Financeiros a Pessoas Físicas", "51": "Obras e Instalações", "52": "Equipamentos e Material Permanente", "54": "Aposentadorias do RGPS - Área Urbana", "71": "Principal da Dívida Contratual Resgatado", "81": "Distribuição Constitucional ou Legal de Receitas", "91": "Sentenças Judiciais"},
    "fonte": {"100": "Recursos Primários de Livre Aplicação", "1443": "Refinanciamento da Dívida Pública Federal", "1444": "Demais Aplicações Autorizadas para Recursos Oriundos de Títulos do Tesouro"},
    "resultado_primario": {"0": "Financeira", "1": "Primária obrigatória", "2": "Primária discricionária", "6": "Emenda individual"},
    "acao": {"2004": "Assistência Médica", "2000": "Administração da Unidade", "2058": "Política Nacional de Defesa", "20X0": "Formação", "0561": "Benefício de Prestação Continuada", "8442": "Transferência de Renda", "0E81": "Benefícios Previdenciários Urbanos", "0181": "Aposentadorias e Pensões Civis da União", "0179": "Pensões Militares da União", "0365": "Refinanciamento da Dívida Pública Mobiliária Federal Interna", "09HB": "Contribuição da União para o RPPS", "219A": "Piso de Atenção Primária", "8535": "Estruturação de Unidades de Atenção Especializada", "8585": "Atenção à Saúde da População", "00H4": "Seguro-Desemprego", "20RK": "Funcionamento das Universidades", "0E36": "Complementação da União ao FUNDEB", "0455": "Serviço da Dívida Pública Federal Interna", "0045": "Fundo de Participação dos Municípios", "0669": "Cobertura do Resultado Negativo Apurado no Banco Central do Brasil", "0005": "Cumprimento de Sentenças Judiciais", "00SY": "Operações Especiais", "4101": "Expansão da Geração"},
}


class FakeSiop:
    """Simula o endpoint SPARQL do SIOP sobre SIOP_BASE_2024: lista membros, agrega por dimensões, fatia por função.
    Conta as consultas por tipo para testar o pulo por data de atualização."""

    def __init__(self, data_atualizacao: str = SIOP_DATA_ATUALIZACAO, anos: dict[int, list] | None = None):
        self.data = data_atualizacao
        self.anos = anos if anos is not None else {2024: SIOP_BASE_2024}
        self.calls = {"exercicio": 0, "membros": 0, "agregacao": 0}
        self.queries: list[str] = []

    @staticmethod
    def _lit(v):
        return {"type": "literal", "value": str(v)}

    def _payload(self, rows: list[dict]) -> dict:
        vars_ = list(rows[0]) if rows else []
        return {"head": {"vars": vars_}, "results": {"bindings": [{k: self._lit(v) for k, v in r.items()} for r in rows]}}

    def responder(self, query: str) -> dict:
        import re

        self.queries.append(query)
        ano = int(re.search(r"orcamento\.dados\.gov\.br/(\d{4})/", query).group(1))
        base = self.anos.get(ano, [])
        if "loa:dataUltimaAtualizacao" in query:
            self.calls["exercicio"] += 1
            return self._payload([{"identificador": str(ano), "data": self.data}] if base else [])
        if "SELECT DISTINCT ?codigo ?rotulo" in query:
            self.calls["membros"] += 1
            dim = next(d for d in SIOP_DIMS_ORDEM if f"?d_{d} loa:codigo ?codigo" in query)
            idx = SIOP_DIMS_ORDEM.index(dim)
            usados = sorted({r[idx] for r in base})
            return self._payload([{"codigo": c, "rotulo": SIOP_ROTULOS[dim].get(c, "")} for c in usados])
        self.calls["agregacao"] += 1
        dims = re.findall(r"\?cod_(\w+)", query.split("(SUM")[0])
        m = re.search(r'\?d_funcao loa:codigo "(\d+)"', query)
        fatia = m.group(1) if m else None
        acc: dict[tuple, dict] = {}
        for r in base:
            if fatia and r[SIOP_DIMS_ORDEM.index("funcao")] != fatia:
                continue
            chave = tuple(r[SIOP_DIMS_ORDEM.index(d)] for d in dims)
            a = acc.setdefault(chave, {"liquidado": 0.0, "n": 0})
            a["liquidado"] += r[-1]
            a["n"] += 1
        rows = []
        for chave, a in sorted(acc.items()):
            liq = a["liquidado"]
            row = {f"cod_{d}": c for d, c in zip(dims, chave)}
            row.update({
                "dotacao_inicial": round(liq * 1.2, 2), "dotacao_atual": round(liq * 1.25, 2), "empenhado": round(liq * 1.05, 2),
                "liquidado": liq, "pago": round(liq * 0.95, 2), "n_itens": a["n"],
            })
            rows.append(row)
        return self._payload(rows)

    # interface mínima de recibo.http.Http usada pelo fetcher
    def get(self, url: str, params: dict | None = None):
        payload = self.responder((params or {})["query"])
        return _Resp(url, payload)


class _Resp:
    def __init__(self, url: str, payload: dict):
        self.url = url
        self.status_code = 200
        self.headers = {"content-type": "application/sparql-results+json"}
        self.text = json.dumps(payload, ensure_ascii=False)

    def json(self):
        return json.loads(self.text)


@pytest.fixture
def siop_fake() -> FakeSiop:
    return FakeSiop()


@pytest.fixture
def populated_store_siop(populated_store, config_dir) -> Store:
    """O mesmo store do RREO sintético, mais o raw do SIOP sintético de 2024 e 2023 (via fetcher real com endpoint falso)."""
    from recibo.config import source_config
    from recibo.fetchers.siop import run_fetch

    fake = FakeSiop(anos={2024: SIOP_BASE_2024, 2023: siop_base_2023()})
    run_fetch(fake, populated_store, source_config(config_dir, "S8"), [2024, 2023])
    return populated_store
