"""recibo diff: o que mudou entre dois períodos, linha a linha, função a função, subfunção a subfunção.

Lê o staging (parquet) com a mesma métrica e as mesmas bases de linhas.yaml que o mart. Não grava nada no
pipeline: é a ferramenta de investigação ("para onde foi a subfunção 272 em 2025?", "o que cresceu na 846?").
A comparação padrão é mesmo período contra mesmo período (2026/p4 contra 2025/p4), porque o gasto não é um
sexto por bimestre: 13º, abono salarial, cupons de títulos e obras têm calendário próprio.

Saída: texto para colar no documento de regras; com --out, o diff completo em JSON.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

import duckdb

from .marts.recibo import Resultado, SemDados, base_where, base_where_static, compute, lit, periodo_where, sql_sum

ZERO = 1e-6  # abaixo disto (em R$) um valor é zero: evita variações absurdas sobre resíduos de ponto flutuante


@dataclass(frozen=True)
class Periodo:
    exercicio: int
    periodo: int

    @property
    def label(self) -> str:
        return f"{self.exercicio}/p{self.periodo}"


@dataclass
class Par:
    """Um valor em dois períodos: A (o período analisado) e B (o de comparação)."""

    chave: str
    nome: str
    a: float
    b: float

    @property
    def diff(self) -> float:
        return self.a - self.b

    @property
    def variacao(self) -> float | None:
        return (self.a / self.b - 1.0) if abs(self.b) >= ZERO else None

    def as_dict(self) -> dict:
        return {"chave": self.chave, "nome": self.nome, "a": self.a, "b": self.b, "diff": self.diff, "variacao": self.variacao}


@dataclass
class Diff:
    a: Periodo
    b: Periodo
    totais: list[Par] = field(default_factory=list)
    linhas: list[Par] = field(default_factory=list)
    anexo01: list[Par] = field(default_factory=list)
    funcoes: list[Par] = field(default_factory=list)
    subfuncoes: list[Par] = field(default_factory=list)
    memos: list[Par] = field(default_factory=list)

    def as_dict(self) -> dict:
        fator = fator_total(self)

        def grp(pares: list[Par]) -> list[dict]:
            return [{**p.as_dict(), "excesso": excesso(p, fator)} for p in pares]

        return {
            "a": asdict(self.a),
            "b": asdict(self.b),
            "unidade": "R$ (como publicado pela fonte)",
            "variacao": "a / b - 1",
            "fator_total": fator,
            "excesso": "a - b * fator_total (variação além do crescimento do total exceto intra)",
            "totais": grp(self.totais),
            "linhas": grp(self.linhas),
            "anexo01": grp(self.anexo01),
            "memos": grp(self.memos),
            "funcoes": grp(self.funcoes),
            "subfuncoes": grp(self.subfuncoes),
        }


def _pares_agrupados(con, where_static: str, a: Periodo, b: Periodo, chave_sql: str, nome_sql: str) -> list[Par]:
    """Soma por chave em A e em B numa consulta só; ordena pela maior variação absoluta."""
    wa, wb = periodo_where(a.exercicio, a.periodo), periodo_where(b.exercicio, b.periodo)
    sql = (
        f"SELECT {chave_sql} AS chave, MIN({nome_sql}) AS nome, "
        f"SUM(CASE WHEN {wa} THEN valor ELSE 0 END) AS a, "
        f"SUM(CASE WHEN {wb} THEN valor ELSE 0 END) AS b "
        f"FROM stg WHERE ({where_static}) AND (({wa}) OR ({wb})) "
        f"GROUP BY 1 ORDER BY ABS(a - b) DESC, chave"
    )
    return [Par(str(ch), str(nm), float(va or 0.0), float(vb or 0.0)) for ch, nm, va, vb in con.execute(sql).fetchall()]


def diff_periodos(stg_path: Path, linhas_cfg: dict, a: Periodo, b: Periodo) -> Diff:
    metrica = linhas_cfg["metrica"]
    bases = linhas_cfg["bases"]
    b02 = bases["anexo02_subfuncao"]
    b01 = bases.get("anexo01_conta")
    out = Diff(a, b)

    # 1) Linhas do recibo, totais e memos: o mesmo compute() do mart, nos dois períodos (sem SIOP: o diff compara o
    # que o RREO mostra; a exclusão de empréstimos é exata pelo Anexo 06, sem detalhe por linha). B pode não existir
    # no staging: aí tudo em B é zero e as linhas saem como "novo".
    ra = compute(stg_path, linhas_cfg, a.exercicio, a.periodo)
    rb: Resultado | None
    try:
        rb = compute(stg_path, linhas_cfg, b.exercicio, b.periodo)
    except SemDados:
        rb = None
    lb = {x.id: x for x in rb.linhas} if rb else {}
    out.linhas = [Par(x.id, x.nome, x.valor, lb[x.id].valor if x.id in lb else 0.0) for x in ra.linhas]

    def rec(res: Resultado | None, rid: str) -> float:
        return next((r.obtido for r in res.reconciliacoes if r.id == rid), 0.0) if res else 0.0

    out.totais = [
        Par("total_base_anexo02", "Despesas exceto intra (Anexo 02)", ra.total_base_anexo02, rb.total_base_anexo02 if rb else 0.0),
        Par("total_recibo", "Total do recibo (despesa primária Anexo 06 + juros; ex-rolagem, ex-amortização, ex-empréstimos)", ra.total_recibo, rb.total_recibo if rb else 0.0),
        Par("servico_divida_fora_843_844", "Juros + amortização fora de 843/844 (ajuste)", rec(ra, "servico_divida_fora_843_844"), rec(rb, "servico_divida_fora_843_844")),
    ]
    for x in ra.excluidos:
        xb = next((e for e in rb.excluidos if e.id == x.id), None) if rb else None
        out.totais.append(Par(f"excluido:{x.id}", x.nome, x.valor, xb.valor if xb else 0.0))
    # memos em reais (receitas, resultado primário e nominal, dívida, restos a pagar): só os disponíveis em A
    mb = {m.id: m for m in rb.memos} if rb else {}
    for m in ra.memos:
        if not m.disponivel or m.unidade != "reais" or m.valor is None:
            continue
        b_m = mb.get(m.id)
        out.memos.append(Par(m.id, m.nome, m.valor, (b_m.valor or 0.0) if (b_m and b_m.disponivel) else 0.0))

    con = duckdb.connect()
    con.execute(f"CREATE VIEW stg AS SELECT * FROM read_parquet({lit(str(stg_path))})")
    try:
        # 2) Referências do Anexo 01 (por cod_conta)
        if b01:
            for nome, cod in linhas_cfg.get("referencias_anexo01", {}).items():
                va = con.execute(sql_sum(base_where(b01, a.exercicio, a.periodo, metrica), f"cod_conta = {lit(cod)}")).fetchone()[0]
                vb = con.execute(sql_sum(base_where(b01, b.exercicio, b.periodo, metrica), f"cod_conta = {lit(cod)}")).fetchone()[0]
                out.anexo01.append(Par(nome, str(cod), float(va or 0.0), float(vb or 0.0)))

        # 3) Funções e 4) subfunções do Anexo 02 (escopo da métrica)
        w_fun = base_where_static({**b02, "nivel": "funcao"}, metrica)
        out.funcoes = _pares_agrupados(con, w_fun, a, b, "COALESCE(funcao_cod, conta_norm)", "conta_norm")
        w_sub = base_where_static({**b02, "nivel": "subfuncao"}, metrica)
        out.subfuncoes = _pares_agrupados(
            con, w_sub, a, b, "COALESCE(funcao_cod, '??') || '/' || COALESCE(subfuncao_cod, conta_norm)", "conta_norm"
        )
    finally:
        con.close()
    return out


def _escala(valores: list[float]) -> tuple[float, str, int]:
    m = max((abs(v) for v in valores), default=0.0)
    if m >= 1e9:
        return 1e9, "R$ bi", 3
    if m >= 1e6:
        return 1e6, "R$ mi", 3
    return 1.0, "R$", 2


def _pct(p: Par) -> str:
    if abs(p.b) < ZERO:
        return "novo" if abs(p.a) >= ZERO else "-"
    return f"{p.variacao * 100:+.1f}%"


ORDENS = ("diff", "excesso")


def fator_total(d: Diff) -> float | None:
    """Quanto a despesa exceto intra cresceu de B para A (A/B). Base da coluna 'excesso'."""
    tot = next((p for p in d.totais if p.chave == "total_base_anexo02"), None)
    return (tot.a / tot.b) if tot and tot.b else None


def excesso(p: Par, fator: float | None) -> float | None:
    """Quanto o item variou além do que variaria se tivesse crescido como o total: a - b * (A/B).
    Crescimento geral some; o que sobra é reclassificação ou mudança real de prioridade."""
    return None if fator is None else p.a - p.b * fator


def render(d: Diff, top: int = 40, funcoes: list[str] | None = None, ordem: str = "diff") -> str:
    if ordem not in ORDENS:
        raise ValueError(f"ordem deve ser uma de {ORDENS}")
    fator = fator_total(d)
    todos = [v for grp in (d.totais, d.linhas, d.anexo01, d.memos, d.funcoes, d.subfuncoes) for p in grp for v in (p.a, p.b)]
    div, unidade, dec = _escala(todos)
    w = 16

    def num(v: float | None) -> str:
        if v is None:
            return "-"
        x = round(v / div, dec)
        return f"{(0.0 if x == 0 else x):,.{dec}f}"  # evita "-0.00"

    def head(titulo: str) -> list[str]:
        return [f"\n{titulo}", f"  {'':<52}{d.a.label:>{w}}{d.b.label:>{w}}{'diff':>{w}}{'var':>10}{'excesso':>{w}}"]

    def row(nome: str, p: Par) -> str:
        return f"  {nome[:52]:<52}{num(p.a):>{w}}{num(p.b):>{w}}{num(p.diff):>{w}}{_pct(p):>10}{num(excesso(p, fator)):>{w}}"

    def ordenar(pares: list[Par]) -> list[Par]:
        if ordem == "excesso" and fator is not None:
            return sorted(pares, key=lambda p: abs(excesso(p, fator) or 0.0), reverse=True)
        return pares  # já vem por |diff| do SQL

    rotulo_ordem = "por variação absoluta" if ordem == "diff" else "por excesso absoluto"
    out = [
        f"diff {d.a.label} contra {d.b.label} | valores em {unidade} | var = {d.a.label} / {d.b.label} - 1",
        (
            f"excesso = quanto o item variou além do total (total exceto intra: x{fator:.4f}); perto de zero = cresceu como tudo"
            if fator is not None
            else "excesso: n/a (sem total em B)"
        ),
    ]
    out += head("TOTAIS E EXCLUSÕES")
    out += [row(p.nome, p) for p in d.totais]
    out += head("LINHAS DO RECIBO (ordem do recibo)")
    out += [row(p.nome, p) for p in d.linhas]
    out += head("REFERÊNCIAS DO ANEXO 01")
    out += [row(f"{p.chave} ({p.nome})", p) for p in d.anexo01]
    if d.memos:
        out += head("MEMOS (fora do R$ 1,00: receitas, resultado, juros por competência, dívida, restos a pagar)")
        out += [row(p.nome, p) for p in d.memos]
    out += head(f"FUNÇÕES DO ANEXO 02 ({rotulo_ordem})")
    out += [row(f"{p.chave} {p.nome}", p) for p in ordenar(d.funcoes)]
    subs = ordenar(d.subfuncoes)
    want = sorted({f.zfill(2) for f in funcoes}) if funcoes else []
    if want:
        subs = [p for p in subs if p.chave.split("/", 1)[0] in want]
    titulo = f"SUBFUNÇÕES DO ANEXO 02 ({rotulo_ordem}; {len(subs)} no total"
    if top and top < len(subs):
        titulo += f", mostrando {top}"
    if want:
        titulo += "; funções " + ", ".join(want)
    out += head(titulo + ")")
    out += [row(f"{p.chave:<8} {p.nome}", p) for p in (subs[:top] if top else subs)]
    return "\n".join(out)


def write_json(d: Diff, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(d.as_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    return path
