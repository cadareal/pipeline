"""Fixture: um RREO SINTÉTICO no formato da API do SICONFI.

ATENÇÃO: os valores abaixo são inventados para testar a lógica (hierarquia, escopo, rateio, reconciliações).
Não têm relação com números reais do orçamento.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from recibo.fetchers.siconfi import DATASET_RREO, SOURCE, canonical_items
from recibo.store import LocalBackend, Store

CONFIG_DIR = Path(__file__).resolve().parents[1] / "config"

COL_LIQ = "DESPESAS LIQUIDADAS ATÉ O BIMESTRE (e)"
COL_DOT = "DOTAÇÃO INICIAL"


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


# (conta, valor) na ordem em que o Anexo 02 lista: cabeçalho, função, suas subfunções...
ANEXO02_EXCETO_INTRA = [
    ("DESPESAS (EXCETO INTRAORÇAMENTÁRIAS) (I)", 4110.0),
    ("Legislativa", 0.0),
    ("Administração", 50.0),
    ("Administração Geral", 50.0),
    ("Defesa Nacional", 100.0),
    ("Defesa Aérea", 40.0),
    ("Previdência do Regime Estatutário", 60.0),
    ("Assistência Social", 200.0),
    ("Assistência ao Idoso", 50.0),
    ("Assistência Comunitária", 150.0),
    ("Previdência Social", 1010.0),
    ("Previdência Básica", 900.0),
    ("Previdência do Regime Estatutário", 100.0),
    ("Administração Geral", 10.0),
    ("Saúde", 200.0),
    ("Atenção Básica", 120.0),
    ("Assistência Hospitalar e Ambulatorial", 80.0),
    ("Trabalho", 90.0),
    ("Proteção e Benefícios ao Trabalhador", 90.0),
    ("Educação", 100.0),
    ("Ensino Fundamental", 30.0),
    ("Ensino Superior", 70.0),
    ("Encargos Especiais", 2360.0),
    ("Refinanciamento da Dívida Interna", 1500.0),
    ("Serviço da Dívida Interna", 500.0),
    ("Outras Transferências", 300.0),
    ("Outros Encargos Especiais", 20.0),
    ("Transferências para a Educação Básica", 40.0),
    ("Reserva de Contingência", 0.0),
]
ANEXO02_INTRA = [
    ("DESPESAS (INTRAORÇAMENTÁRIAS) (II)", 25.0),
    ("Previdência Social", 25.0),
    ("Previdência do Regime Estatutário", 25.0),
]
ANEXO02_TOTAL = [("TOTAL (III) = (I + II)", 4135.0)]

ANEXO01 = [
    ("DESPESAS (EXCETO INTRA-ORÇAMENTÁRIAS) (VIII)", 4110.0),
    ("DESPESAS CORRENTES", 3200.0),
    ("PESSOAL E ENCARGOS SOCIAIS", 700.0),
    ("JUROS E ENCARGOS DA DÍVIDA", 350.0),
    ("OUTRAS DESPESAS CORRENTES", 2150.0),
    ("DESPESAS DE CAPITAL", 910.0),
    ("INVESTIMENTOS", 60.0),
    ("INVERSÕES FINANCEIRAS", 700.0),
    ("AMORTIZAÇÃO DA DÍVIDA", 150.0),
    ("DESPESAS (INTRA-ORÇAMENTÁRIAS) (IX)", 25.0),
    ("SUBTOTAL DAS DESPESAS (X) = (VIII + IX)", 4135.0),
    ("AMORTIZAÇÃO DA DÍVIDA / REFINANCIAMENTO (XI)", 1500.0),
    ("Amortização da Dívida Interna", 1500.0),
    ("SUBTOTAL COM REFINANCIAMENTO (XII) = (X + XI)", 5635.0),
    ("TOTAL (XIV) = (XII + XIII)", 5635.0),
]

# Resultado esperado para o recibo (ver linhas.yaml):
ESPERADO = {
    "previdencia_inss": 900.0,        # 271
    "inativos_pensionistas": 160.0,   # 272: 60 (Defesa) + 100 (Previdência); intra (25) fora
    "assistencia": 200.0,
    "saude": 200.0,
    "educacao": 140.0,                # 100 + 847 (40)
    "trabalho": 90.0,
    "defesa_seguranca": 40.0,         # Defesa menos 272
    "transferencias": 300.0,          # 845
    "juros": 350.0,                   # Anexo 01
    "maquina_demais": 80.0,           # 50 (Adm) + 10 (Adm Geral do INSS) + 20 (846)
}
TOTAL_RECIBO = 2460.0
REFINANCIAMENTO = 1500.0
SERVICO_BRUTO = 500.0


def _pages(items: list[dict]) -> list[str]:
    """Simula duas páginas da API para exercitar a concatenação."""
    half = len(items) // 2
    p1 = {"items": items[:half], "hasMore": True, "limit": half, "offset": 0, "count": half}
    p2 = {"items": items[half:], "hasMore": False, "limit": half, "offset": half, "count": len(items) - half}
    return [json.dumps(p1, ensure_ascii=False), json.dumps(p2, ensure_ascii=False)]


def build_items(exercicio: int = 2024, periodo: int = 6) -> dict[str, list[dict]]:
    a02 = []
    for coluna, mult in ((COL_LIQ, 1.0), (COL_DOT, 1.3)):
        for conta, v in ANEXO02_EXCETO_INTRA + ANEXO02_INTRA + ANEXO02_TOTAL:
            a02.append(_row(exercicio, periodo, "RREO-Anexo 02", coluna, conta, round(v * mult, 2)))
    a01 = []
    for coluna, mult in ((COL_LIQ, 1.0), (COL_DOT, 1.3)):
        for conta, v in ANEXO01:
            a01.append(_row(exercicio, periodo, "RREO-Anexo 01", coluna, conta, round(v * mult, 2)))
    return {"RREO-Anexo 02": a02, "RREO-Anexo 01": a01}


@pytest.fixture
def store(tmp_path) -> Store:
    return Store(LocalBackend(tmp_path / "data"))


@pytest.fixture
def populated_store(store) -> Store:
    for anexo, items in build_items().items():
        pages = _pages(items)
        store.put(
            source=SOURCE,
            dataset=DATASET_RREO,
            key=f"2024/06/{anexo}",
            filename="pages.jsonl",
            content=("\n".join(pages) + "\n").encode("utf-8"),
            canonical=canonical_items(items),
            meta={"request": {"sintetico": True}, "n_items": len(items), "n_pages": 2},
        )
    store.write_index(
        SOURCE,
        DATASET_RREO,
        {
            "2024/06/RREO-Anexo 02": {"status": "ok", "n_items": 66, "snapshot_id": "x"},
            "2024/06/RREO-Anexo 01": {"status": "ok", "n_items": 30, "snapshot_id": "x"},
        },
    )
    return store


@pytest.fixture
def config_dir() -> Path:
    return CONFIG_DIR
