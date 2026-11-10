"""Leitura das planilhas-relatório do Tesouro a partir da grade do staging (planilha, linha, coluna, valor).

Os anexos da dívida (estoque 2.1, fatores 2.9, emissões 1.1) têm o mesmo desenho: rótulos na coluna 1, uma linha de
cabeçalho com os meses no formato Mmm/aa (Jan/07, Dez/24...) e, depois de cada dezembro, uma coluna com o total do
ano (2007, 2024...). Aqui ficam as funções que acham o cabeçalho, as colunas de um mês ou de um ano e a linha de um
rótulo, sem adivinhar: o rótulo vem de linhas.yaml e é comparado sem acento, sem caixa, sem espaços duplicados e sem
os números de nota de rodapé que o Tesouro cola no fim ("Estoque anterior1", "DPFe ¹", "- Juros Apropriados da DPMFi 9").
"""

from __future__ import annotations

import re

import polars as pl

from .textnorm import norm

RX_MES = re.compile(r"^([A-Za-z]{3})/(\d{2})$")
RX_ANO = re.compile(r"^\d{4}$")
MESES = {"JAN": 1, "FEV": 2, "MAR": 3, "ABR": 4, "MAI": 5, "JUN": 6, "JUL": 7, "AGO": 8, "SET": 9, "OUT": 10, "NOV": 11, "DEZ": 12}


class GradeFormato(RuntimeError):
    """A planilha não tem o desenho esperado (cabeçalho de meses, rótulo, coluna)."""


def normalizar_rotulo(s: str) -> str:
    t = norm(s)
    t = re.sub(r"[\s\d]+$", "", t)  # notas de rodapé coladas no fim: "ESTOQUE ANTERIOR1", "DPFE 1"
    t = re.sub(r"\s+", " ", t).strip()
    return t


def mes_do_cabecalho(texto: str) -> tuple[int, int] | None:
    """'Dez/24' -> (2024, 12); '2024' -> None."""
    m = RX_MES.match(texto.strip())
    if not m:
        return None
    mes = MESES.get(m.group(1).upper())
    if mes is None:
        return None
    aa = int(m.group(2))
    return (2000 + aa if aa < 70 else 1900 + aa, mes)


def linha_cabecalho(d: pl.DataFrame) -> int | None:
    """A linha com mais células no padrão Mmm/aa ou aaaa (pelo menos 6)."""
    melhor, n_melhor = None, 0
    for i in d["linha"].unique().to_list():
        textos = d.filter(pl.col("linha") == i)["valor_texto"].to_list()
        n = sum(1 for t in textos if RX_MES.match(t.strip()) or RX_ANO.match(t.strip()))
        if n > n_melhor:
            melhor, n_melhor = int(i), n
    return melhor if n_melhor >= 6 else None


class Planilha:
    """Uma aba da grade, com o cabeçalho de meses localizado."""

    def __init__(self, grade: pl.DataFrame, planilha: str | None = None):
        if planilha is None:
            planilha = str(grade["planilha"][0]) if grade.height else ""
        self.nome = planilha
        self.d = grade.filter(pl.col("planilha") == planilha)
        if self.d.height == 0:
            raise GradeFormato(f"planilha {planilha!r} não existe; abas: {grade['planilha'].unique().to_list()}")
        self.cab = linha_cabecalho(self.d)
        if self.cab is None:
            raise GradeFormato(f"planilha {planilha!r}: não achei a linha de cabeçalho com meses (Mmm/aa)")
        cabs = self.d.filter(pl.col("linha") == self.cab).sort("coluna")
        self.col_mes: dict[tuple[int, int], int] = {}
        self.col_ano: dict[int, int] = {}
        for c, t in zip(cabs["coluna"].to_list(), cabs["valor_texto"].to_list()):
            t = t.strip()
            m = mes_do_cabecalho(t)
            if m:
                self.col_mes.setdefault(m, int(c))
            elif RX_ANO.match(t):
                self.col_ano.setdefault(int(t), int(c))
        self._rotulos: dict[str, list[int]] = {}
        r1 = self.d.filter(pl.col("coluna") == 1).sort("linha")
        for i, t in zip(r1["linha"].to_list(), r1["valor_texto"].to_list()):
            self._rotulos.setdefault(normalizar_rotulo(t), []).append(int(i))

    def linha(self, rotulo: str, ocorrencia: int = 1) -> int:
        chave = normalizar_rotulo(rotulo)
        linhas = self._rotulos.get(chave) or []
        if len(linhas) < ocorrencia:
            parecidos = [k for k in self._rotulos if chave.split(" ")[0] in k][:8]
            raise GradeFormato(f"planilha {self.nome!r}: rótulo {rotulo!r} (ocorrência {ocorrencia}) não encontrado; parecidos: {parecidos}")
        return linhas[ocorrencia - 1]

    def celula(self, linha: int, coluna: int) -> float | None:
        c = self.d.filter((pl.col("linha") == linha) & (pl.col("coluna") == coluna))
        if c.height == 0:
            return None
        v = c["valor_num"][0]
        return float(v) if v is not None else None

    def meses_do_ano(self, ano: int) -> list[int]:
        return sorted(m for (a, m) in self.col_mes if a == ano)

    def valor_mes(self, rotulo: str, ano: int, mes: int, ocorrencia: int = 1) -> float | None:
        col = self.col_mes.get((ano, mes))
        if col is None:
            return None
        return self.celula(self.linha(rotulo, ocorrencia), col)

    def valor_anual(self, rotulo: str, ano: int, ocorrencia: int = 1) -> tuple[float | None, str]:
        """(valor, origem): a coluna anual do Tesouro quando existe ('coluna_anual'), senão a soma dos meses
        disponíveis ('soma_de_N_meses'); None quando o ano não está na planilha."""
        lin = self.linha(rotulo, ocorrencia)
        col = self.col_ano.get(ano)
        if col is not None:
            v = self.celula(lin, col)
            if v is not None:
                return v, "coluna_anual"
        meses = self.meses_do_ano(ano)
        if not meses:
            return None, "ano_ausente"
        vals = [self.celula(lin, self.col_mes[(ano, m)]) for m in meses]
        vals = [v for v in vals if v is not None]
        if not vals:
            return None, "sem_valores"
        return sum(vals), f"soma_de_{len(vals)}_meses"

    def ultimo_mes(self, ano: int) -> int | None:
        meses = self.meses_do_ano(ano)
        return meses[-1] if meses else None
