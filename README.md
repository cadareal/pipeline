# recibo

Pipeline aberto de dados fiscais. Pergunta que responde: **de cada R$ 1,00 de imposto, quantos centavos foram para onde?**

Fase 1: Recibo do Contribuinte (União). Fonte implementada: SICONFI (RREO da União). As demais fontes estão
catalogadas em `config/sources.yaml` e entram em seguida.

Princípios (ver documento "Transparência do Orçamento: Regras e Fontes"):

- Nenhum número publicado é digitado à mão: tudo nasce de um fetcher versionado sobre um snapshot raw imutável.
- Fluxo bruto (rolagem) nunca soma como gasto. As linhas somam exatamente R$ 1,00.
- Cada número sai com ficha técnica: numerador, denominador, fonte, filtro, SQL e snapshot.
- Reconciliações rodam a cada execução. Falhou, não publica.

## Como funciona

```
config/linhas.yaml  ──┐
config/sources.yaml ──┤  (spec como código: dirige o fetch, o SQL, a ficha e os testes)
                      │
 fetch ──> raw/ (imutável, hash + manifest) ──> transform ──> staging/ (parquet) ──> mart ──> marts/ + export/ (json, csv)
```

| Camada | Onde | O que |
| --- | --- | --- |
| raw | `data/raw/{fonte}/{dataset}/{chave}/{snapshot}/` | resposta da API como veio (`pages.jsonl`) + `manifest.json` (url, sha256, timestamp). `_checks.jsonl` registra toda verificação, mudou ou não. |
| staging | `data/staging/stg_siconfi_rreo.parquet` | todas as colunas da API + derivações (`escopo`, `nivel`, `funcao_cod`, `subfuncao_cod`) |
| marts | `data/marts/recibo_{ano}_p{bimestre}.json` e `.csv` | linhas do recibo, shares, centavos, exclusões, reconciliações |
| export | `data/export/recibo_{ano}.json` | o que o site consome |

Idempotência: rodar de novo sem mudança na fonte não cria snapshot (só registra a verificação). Mudança em
período já publicado gera snapshot novo com `previous_snapshot_id`, e um aviso `REVISAO NA FONTE` no log.

## Rodando localmente

Pré-requisitos: Python 3.11+ e [uv](https://docs.astral.sh/uv/).

```bash
uv sync --extra dev
uv run pytest                      # 13 testes sobre um RREO sintético (valores inventados)

# 1) baixar RREO da União (anexos 01, 02, 04, 06; bimestres 1 a 6)
uv run recibo fetch siconfi --exercicios 2024 2025 2026

# 2) ver o que a API devolveu e como foi classificado (cole a saída de volta para calibrar)
uv run recibo inspect siconfi --exercicio 2024 --periodo 6 --anexo "RREO-Anexo 02" --rows 150

# 3) raw -> staging
uv run recibo transform siconfi

# 4) recibo de 2024 (usa o último bimestre disponível no índice; --periodo força um)
uv run recibo mart recibo --exercicio 2024

# tudo de uma vez
uv run recibo run --exercicios 2024 2025 2026
```

Variáveis de ambiente opcionais: ver `.env.example`. Em modo local nada é obrigatório.

### O que colar de volta depois da primeira execução real

1. O log do `fetch`: a linha `anexos disponíveis para RREO` (nomes exatos) e a linha `ente da esfera U: cod_ibge=...`.
2. A saída completa do `inspect` para `2024 / 6 / RREO-Anexo 02` e para `RREO-Anexo 01`.
3. A saída do `mart` (tabela + reconciliações).

Com isso se calibra `derive()` (se o `cod_conta` da API carregar a hierarquia, a derivação por ordem é
substituída) e os filtros de `config/linhas.yaml`.

## Metodologia (v0, resumo)

- Métrica: despesas liquidadas até o bimestre, exceto intraorçamentárias (no 6º bimestre, o exercício inteiro).
- Partição por **subfunção**, não por função: a função 09 "Previdência Social" não é o INSS; o INSS é a
  subfunção 271, e os inativos (272, 274) aparecem em várias funções.
- Juros vêm do Anexo 01 (grupo "Juros e Encargos da Dívida"). As subfunções 843/844 (serviço da dívida) saem do
  Anexo 02 porque misturam juros e amortização; a diferença é publicada como amortização, fora do R$ 1,00.
- Refinanciamento (841/842) fica fora do R$ 1,00 e é exibido à parte.
- Residual ("máquina pública e demais funções") = total exceto intra − linhas atribuídas − exclusões.
- Reconciliações críticas: soma das subfunções = soma das funções; soma das funções = total do cabeçalho;
  residual ≥ 0; centavos somam 100. Informativa: (843 + 844) − juros ≈ amortização do Anexo 01.

Tudo isso está em `config/linhas.yaml`, que gera o SQL, a ficha e os testes.

## Nuvem (depois da primeira execução local)

- Cloud Run Job em `southamerica-east1`, disparado toda semana pelo Cloud Scheduler (`infra/deploy.sh`).
- `RECIBO_STORE=gcs` grava o raw num bucket com versionamento; BigQuery (US) recebe staging e marts.
- Imagem: `infra/Dockerfile`.

## Licença

A definir.
