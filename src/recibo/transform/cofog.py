"""Transform S7: raw do COFOG (um CSV por ano, do CKAN do Tesouro) -> staging (stg_cofog.parquet).

Uma linha por linha do CSV: ano, natureza de despesa detalhada (8 dígitos), ação, unidade orçamentária, classificação
econômica GFS, código COFOG, valor em reais. O cabeçalho é casado pelos nomes dos metadados do Tesouro (sem acento,
maiúsculas); se o Tesouro mudar um nome, o transform para e imprime o cabeçalho real, em vez de adivinhar.

Derivações (strings, para filtros em SQL):
  cofog_divisao / cofog_grupo / cofog_classe : 2, 3 e 4 primeiros dígitos do código COFOG sem o prefixo 7 do GFSM
                                               (7102 -> 10, 102, 102; 70711 -> 07, 071, 0711)
  categoria / gnd / modalidade / elemento     : dígitos 1, 2, 3-4 e 5-6 da natureza detalhada (3.1.90.11.01 -> 3, 1, 90, 11)
  gfs2                                        : 2 primeiros dígitos da classificação econômica GFS (21 pessoal, 24 juros...)
  ajuste                                      : a linha tem marcação em "Ajustes Metodológicos"

Valores: o CSV pode vir com vírgula decimal e ponto de milhar (padrão brasileiro) ou com ponto decimal. A decisão é por
arquivo: se alguma célula tem vírgula, o arquivo é brasileiro e os pontos são de milhar.

Unidade: o cabeçalho diz "VALOR (R$)" em todos os anos, mas os arquivos não estão na mesma unidade (2019 e 2020 em reais
com centavos; 2024 em R$ milhões com duas casas). A unidade de cada ano é declarada em sources.yaml
(`unidade_valor_padrao` e `unidade_valor_por_exercicio`), nunca adivinhada. O transform multiplica pelo fator, guarda o
número original em `valor_fonte` e anota `unidade_fonte`, `fator_unidade` e `resolucao_reais` (0,01 vezes o fator: em
milhões com duas casas cada linha só é precisa até R$ 10 mil). Depois, a guarda de escala: o total do ano, já em reais,
tem que ficar entre os limites de sources.yaml (R$ 0,5 tri a R$ 20 tri); fora disso, para com erro e diz qual unidade
faria o total cair na faixa, para a correção ser feita na config, à vista. Todos os anos são conferidos antes de parar,
para a mensagem trazer o quadro inteiro de uma vez.
"""

from __future__ import annotations

import io
import logging
import re
from pathlib import Path

import polars as pl

from ..fetchers.cofog import DATASET, KEY_PACKAGE, SOURCE
from ..store import Store
from ..textnorm import norm

log = logging.getLogger(__name__)

STAGING_FILE = "stg_cofog.parquet"

# nome interno -> cabeçalhos aceitos: o primeiro é o dos metadados ("Despesa por Função - COFOG", PDF do Tesouro), o
# segundo é o que o CSV real traz (lido em 9/nov/2026: maiúsculas, sem acento, sem " - Código", "ANO" em vez de
# "Ano de Lançamento", "AJUSTE METODOLOGICO" no singular). A comparação ignora acento, caixa e pontuação.
COLUNAS = {
    "ano": ["Ano de Lançamento", "Ano"],
    "ajuste_metodologico": ["Ajustes Metodológicos", "Ajuste Metodológico"],
    "natureza_cod": ["Natureza Despesa Detalhada"],
    "natureza_desc": ["Natureza Despesa Detalhada - Descrição"],
    "acao_cod": ["Ação Governo - Código", "Ação Governo"],
    "acao_desc": ["Ação Governo - Descrição"],
    "uo_cod": ["Unidade Orçamentária - Código", "Unidade Orçamentária"],
    "uo_desc": ["Unidade Orçamentária - Descrição"],
    "gfs_cod": ["Classificação Econômica GFS - Código", "Classificação Econômica GFS"],
    "gfs_desc": ["Classificação Econômica GFS - Descrição"],
    "cofog_cod": ["Classificação COFOG"],
    "cofog_desc": ["Classificação COFOG - Descrição"],
    "valor": ["Valor (R$)"],
}
AJUSTE_VAZIO = {"", "NA", "N/A", "-", "NAO", "NÃO", "NONE", "NULL"}  # o CSV real escreve "NA" na linha sem ajuste
DIVISOES_COFOG = [f"{i:02d}" for i in range(1, 11)]
FAIXA_TOTAL_PADRAO = (5e11, 2e13)  # R$ 0,5 tri a R$ 20 tri por ano: fora disso, a leitura dos números está errada
UNIDADES = {"reais": 1.0, "milhares": 1e3, "milhoes": 1e6}  # nomes aceitos em sources.yaml -> fator para reais


class CofogFormato(RuntimeError):
    """O CSV não tem o formato que os metadados do Tesouro descrevem (cabeçalho, números ou escala)."""


def _chave(s: str) -> str:
    """Cabeçalho comparável: sem acento, maiúsculas, só letras e dígitos."""
    return re.sub(r"[^A-Z0-9]+", "", norm(s))


def mapear_colunas(cabecalho: list[str]) -> dict[str, str]:
    """nome interno -> nome real da coluna no CSV. Primeiro por igualdade do cabeçalho normalizado com um dos nomes
    aceitos; depois por conter todas as palavras de um nome aceito (sem reutilizar coluna já casada); ambiguidade ou
    ausência é erro com o cabeçalho real impresso, para a correção ser feita aqui, não por adivinhação."""
    reais = {_chave(c): c for c in cabecalho}
    out: dict[str, str] = {}
    faltando: list[str] = []
    for interno, aceitos in COLUNAS.items():
        achado = next((reais[_chave(a)] for a in aceitos if _chave(a) in reais and reais[_chave(a)] not in out.values()), None)
        if achado is None:
            for a in aceitos:
                palavras = [p for p in re.split(r"[^A-Z0-9]+", norm(a)) if p]
                candidatos = [c for c in cabecalho if all(p in norm(c) for p in palavras) and c not in out.values()]
                if len(candidatos) == 1:
                    achado = candidatos[0]
                    break
                if len(candidatos) > 1:
                    faltando.append(f"{interno} ('{a}') ambíguo entre {candidatos}")
                    break
        if achado is None:
            if not any(f.startswith(f"{interno} (") for f in faltando):
                faltando.append(f"{interno} ({' | '.join(repr(a) for a in aceitos)})")
            continue
        out[interno] = achado
    if faltando:
        raise CofogFormato(f"colunas não encontradas no CSV do COFOG: {faltando}. Cabeçalho real: {cabecalho}")
    return out


def decodificar(raw: bytes) -> str:
    for enc in ("utf-8-sig", "utf-8"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1")


def separador(primeira_linha: str) -> str:
    cont = {sep: primeira_linha.count(sep) for sep in (";", ",", "\t", "|")}
    sep = max(cont, key=cont.get)
    if cont[sep] == 0:
        raise CofogFormato(f"não achei separador na primeira linha do CSV: {primeira_linha[:200]!r}")
    return sep


def ler_csv(raw: bytes) -> pl.DataFrame:
    """CSV do Tesouro -> DataFrame com as 13 colunas internas (strings) e `valor` em float."""
    texto = decodificar(raw)
    primeira = texto.split("\n", 1)[0]
    sep = separador(primeira)
    df = pl.read_csv(io.BytesIO(texto.encode("utf-8")), separator=sep, infer_schema_length=0, quote_char='"', truncate_ragged_lines=True, encoding="utf8")
    mapa = mapear_colunas(list(df.columns))
    df = df.select([pl.col(real).alias(interno) for interno, real in mapa.items()])
    v = pl.col("valor").cast(pl.Utf8).str.strip_chars().str.replace_all(r"[R$\s]", "")
    tem_virgula = df.select(pl.col("valor").cast(pl.Utf8).str.contains(",").any()).item()
    if tem_virgula:
        v = v.str.replace_all(r"\.", "").str.replace(",", ".")
    df = df.with_columns(v.cast(pl.Float64, strict=False).alias("valor_num"), pl.col("valor").alias("valor_texto"))
    ruins = df.filter(pl.col("valor_num").is_null() & pl.col("valor_texto").is_not_null() & (pl.col("valor_texto").str.strip_chars() != ""))
    if ruins.height:
        exemplos = ruins["valor_texto"].head(5).to_list()
        raise CofogFormato(f"{ruins.height} valores não numéricos na coluna de valor (formato {'brasileiro' if tem_virgula else 'ponto decimal'}); exemplos: {exemplos}")
    return df.drop("valor").rename({"valor_num": "valor"}).with_columns(pl.col("valor").fill_null(0.0))


def _digitos(col: str) -> pl.Expr:
    return pl.col(col).cast(pl.Utf8).fill_null("").str.replace_all(r"\D", "")


def derivar(df: pl.DataFrame) -> pl.DataFrame:
    """Códigos como strings de dígitos. O código COFOG do Tesouro vem no padrão do GFSM, com um 7 na frente (7 = total
    das funções; 7102 = divisão 10, grupo 2, "proteção social, velhice"): o 7 é tirado quando o que sobra começa por
    uma divisão válida (01 a 10). Códigos já sem o 7 (10.2, 0711) passam como estão."""
    dig = _digitos("cofog_cod")
    sem7 = dig.str.slice(1)
    base_cofog = (
        pl.when(dig.str.starts_with("7") & (dig.str.len_chars() >= 3) & sem7.str.slice(0, 2).is_in(DIVISOES_COFOG))
        .then(sem7)
        .otherwise(dig)
    )
    return df.with_columns(
        pl.col("ano").cast(pl.Utf8).str.strip_chars().str.slice(0, 4).cast(pl.Int64, strict=False).alias("ano"),
        dig.alias("cofog_digitos_fonte"),
        base_cofog.alias("cofog_digitos"),
        _digitos("natureza_cod").alias("natureza_digitos"),
        _digitos("gfs_cod").alias("gfs_digitos"),
        (~pl.col("ajuste_metodologico").cast(pl.Utf8).fill_null("").str.strip_chars().str.to_uppercase().is_in(list(AJUSTE_VAZIO))).alias("ajuste"),
    ).with_columns(
        pl.col("cofog_digitos").str.slice(0, 2).alias("cofog_divisao"),
        pl.col("cofog_digitos").str.slice(0, 3).alias("cofog_grupo"),
        pl.col("cofog_digitos").str.slice(0, 4).alias("cofog_classe"),
        pl.col("natureza_digitos").str.slice(0, 1).alias("categoria"),
        pl.col("natureza_digitos").str.slice(1, 1).alias("gnd"),
        pl.col("natureza_digitos").str.slice(2, 2).alias("modalidade"),
        pl.col("natureza_digitos").str.slice(4, 2).alias("elemento"),
        pl.col("gfs_digitos").str.slice(0, 2).alias("gfs2"),
    )


def unidade_do_exercicio(cfg: dict | None, ano: int) -> tuple[str, float]:
    """(nome, fator) da unidade declarada em sources.yaml para o ano: `unidade_valor_por_exercicio[ano]`, senão
    `unidade_valor_padrao`, senão reais. Nome desconhecido é erro de config, não palpite."""
    cfg = cfg or {}
    por_ano = cfg.get("unidade_valor_por_exercicio") or {}
    nome = por_ano.get(ano, por_ano.get(str(ano), cfg.get("unidade_valor_padrao", "reais")))
    nome = str(nome).strip().lower()
    if nome not in UNIDADES:
        raise CofogFormato(f"COFOG {ano}: unidade de valor {nome!r} em sources.yaml não é uma de {sorted(UNIDADES)}")
    return nome, UNIDADES[nome]


def aplicar_unidade(df: pl.DataFrame, nome: str, fator: float) -> pl.DataFrame:
    """`valor` passa a reais (arredondado ao centavo); o número como veio fica em `valor_fonte`."""
    return df.with_columns(
        pl.col("valor").alias("valor_fonte"),
        (pl.col("valor") * fator).round(2).alias("valor"),
        pl.lit(nome).alias("unidade_fonte"),
        pl.lit(fator).alias("fator_unidade"),
        pl.lit(round(0.01 * fator, 2)).alias("resolucao_reais"),
    )


def conferir_escala(df: pl.DataFrame, ano_chave: int, faixa: tuple[float, float] | None, unidade: str = "reais") -> None:
    total = float(df["valor"].sum())
    if faixa is not None and not (faixa[0] <= total <= faixa[1]):
        fator = UNIDADES.get(unidade, 1.0)
        total_fonte = total / fator if fator else total
        caberia = [u for u, f in UNIDADES.items() if u != unidade and faixa[0] <= total_fonte * f <= faixa[1]]
        dica = (
            f"O total do arquivo ({total_fonte:,.2f}) cairia na faixa lido em {caberia[0]}: confira com `recibo inspect cofog` e, "
            f"se for isso, declare em sources.yaml (unidade_valor_por_exercicio: {{{ano_chave}: {caberia[0]}}})."
            if caberia
            else "Nenhuma unidade (reais, milhares, milhoes) faz o total cair na faixa; sintoma típico: separador decimal lido errado (1.234 virando 1,234). Confira com `recibo inspect cofog`."
        )
        raise CofogFormato(f"COFOG {ano_chave}: total do ano R$ {total:,.2f} (unidade declarada: {unidade}) fora da faixa esperada ({faixa[0]:,.0f} a {faixa[1]:,.0f}). {dica}")
    anos = df["ano"].drop_nulls().unique().to_list()
    if anos and anos != [ano_chave]:
        log.warning("COFOG %s: a coluna de ano traz %s; a chave do raw manda", ano_chave, anos)
    fora = df.filter(~pl.col("cofog_divisao").is_in(DIVISOES_COFOG))
    if fora.height:
        exemplos = fora["cofog_cod"].unique().head(8).to_list()
        raise CofogFormato(f"COFOG {ano_chave}: {fora.height} linhas com código COFOG fora das divisões 01 a 10 (exemplos: {exemplos}); o formato do código mudou?")


def build_staging(store: Store, staging_dir: Path, src_cfg: dict | None = None) -> Path:
    cfg = src_cfg or {}
    faixa = None
    if cfg.get("total_minimo_reais") is not None and cfg.get("total_maximo_reais") is not None:
        faixa = (float(cfg["total_minimo_reais"]), float(cfg["total_maximo_reais"]))
    elif "total_minimo_reais" not in cfg:
        faixa = FAIXA_TOTAL_PADRAO
    frames: list[pl.DataFrame] = []
    erros: list[str] = []
    for key in store.list_keys(SOURCE, DATASET):
        if key == KEY_PACKAGE or not key.isdigit():
            continue
        got = store.get_latest(SOURCE, DATASET, key)
        if not got:
            continue
        raw, manifest = got
        ano = int(key)
        try:
            nome_unidade, fator = unidade_do_exercicio(cfg, ano)
            df = aplicar_unidade(derivar(ler_csv(raw)), nome_unidade, fator)
            conferir_escala(df, ano, faixa, nome_unidade)
        except CofogFormato as e:  # confere todos os anos antes de parar, para a mensagem trazer o quadro inteiro
            erros.append(str(e))
            log.error("%s", e)
            continue
        rec = manifest.get("recurso") or {}
        df = df.with_columns(
            pl.lit(ano).cast(pl.Int64).alias("exercicio"),
            pl.lit(manifest.get("filename")).alias("arquivo"),
            pl.lit(rec.get("last_modified") or rec.get("created")).alias("recurso_data"),
            pl.lit(manifest["key"]).alias("raw_key"),
            pl.lit(manifest["snapshot_id"]).alias("snapshot_id"),
            pl.lit(manifest["fetched_at"]).alias("fetched_at"),
            pl.lit(manifest["sha256_canonical"]).alias("sha256_canonical"),
        )
        log.info("COFOG %s: %d linhas, total R$ %s (arquivo em %s, fator %s), %d com ajuste metodológico", ano, df.height, f"{float(df['valor'].sum()):,.2f}", nome_unidade, f"{fator:,.0f}", int(df["ajuste"].sum()))
        frames.append(df)
    if erros:
        raise CofogFormato(f"{len(erros)} ano(s) recusado(s); nada foi gravado:\n  - " + "\n  - ".join(erros))
    if not frames:
        raise SystemExit("nenhum raw do COFOG encontrado; rode `recibo fetch cofog` antes")
    out = pl.concat(frames, how="vertical_relaxed")
    staging_dir.mkdir(parents=True, exist_ok=True)
    path = staging_dir / STAGING_FILE
    out.write_parquet(path)
    log.info("staging cofog: %d anos, %d linhas -> %s", len(frames), out.height, path)
    return path
