# recibo

Pipeline aberto de dados fiscais. Pergunta que responde: **de cada R$ 1,00 de imposto, quantos centavos foram para onde?**

Fase 1: Recibo do Contribuinte (União). Fontes implementadas: SICONFI (RREO da União, S4) e SIOP (execução do
orçamento federal por classificador, S8). As demais fontes estão catalogadas em `config/sources.yaml` e entram em
seguida.

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
 fetch siconfi ──> raw/siconfi ──> transform siconfi ──> staging/stg_siconfi_rreo.parquet ──┐
                                                                                            ├──> mart ──> marts/ + export/
 fetch siop    ──> raw/siop    ──> transform siop    ──> staging/stg_siop_execucao.parquet ──┘  (linhas vêm do RREO;
                                                                                                 o SIOP confere e detalha)
```

| Camada | Onde | O que |
| --- | --- | --- |
| raw | `data/raw/{fonte}/{dataset}/{chave}/{snapshot}/` | resposta da API como veio (`pages.jsonl` no SICONFI, `result.json` no SIOP) + `manifest.json` (url ou consulta SPARQL, sha256, timestamp). `_checks.jsonl` registra toda verificação, mudou ou não. |
| staging | `data/staging/stg_siconfi_rreo.parquet` | todas as colunas da API + derivações (`escopo`, `nivel`, `funcao_cod`, `subfuncao_cod`) |
| staging | `data/staging/stg_siop_execucao.parquet` | agregações do SIOP por códigos (esfera, modalidade, função, subfunção, GND, elemento, fonte, RP, ação) com rótulos e flags (`intra`, `ofss`, `refinanciamento`) |
| marts | `data/marts/recibo_{ano}_p{bimestre}.json` e `.csv` | linhas do recibo, shares, centavos, exclusões, reconciliações |
| export | `data/export/recibo_{ano}.json` | o que o site consome |

Idempotência: rodar de novo sem mudança na fonte não cria snapshot (só registra a verificação). Mudança em
período já publicado gera snapshot novo com `previous_snapshot_id`, e um aviso `REVISAO NA FONTE` no log.

## Rodando localmente

Pré-requisitos: Python 3.11+ e [uv](https://docs.astral.sh/uv/).

```bash
uv sync --extra dev
uv run pytest                      # 27 testes sobre um RREO e um SIOP sintéticos (valores inventados, consistentes entre si)

# 1) baixar RREO da União (anexos 01, 02, 04-RGPS, 04-RPPS, 06; bimestres 1 a 6)
uv run recibo fetch siconfi --exercicios 2024 2025 2026

# 2) ver o que a API devolveu e como foi classificado (cole a saída de volta para calibrar)
uv run recibo inspect siconfi --exercicio 2024 --periodo 6 --anexo "RREO-Anexo 02" --rows 150

# 3) raw -> staging
uv run recibo transform siconfi

# 4) recibo de 2024 (usa o último bimestre disponível no índice; --periodo força um)
uv run recibo mart recibo --exercicio 2024

# 5) SIOP: baixa (só refaz um exercício quando a data de atualização do grafo muda), transforma e inspeciona
uv run recibo fetch siop --exercicios 2024 2025 2026        # ~70 consultas por exercício na primeira vez; depois, 1 por exercício sem mudança
uv run recibo transform siop
uv run recibo inspect siop --exercicio 2025 --agregacao acao_subfuncao --funcao 28 --rows 30
uv run recibo mart recibo --exercicio 2025                  # com o staging do SIOP presente, rodam as conferências cruzadas

# 6) o que mudou entre dois períodos: linhas, Anexo 01, funções e subfunções (mesmo bimestre por padrão)
uv run recibo diff --exercicio 2025 --contra 2024                         # 2025/p6 contra 2024/p6
uv run recibo diff --exercicio 2026 --contra 2025 --ordem excesso         # 2026/p4 contra 2025/p4, reclassificações primeiro
uv run recibo diff --exercicio 2025 --contra 2024 --funcao 28 09 --top 0  # só Encargos Especiais e Previdência, todas as subfunções
uv run recibo diff --exercicio 2025 --contra 2024 --out data/marts/diff_2025_2024.json

# tudo de uma vez (fetch e transform das duas fontes, depois os recibos); --sem-siop pula o SIOP
uv run recibo run --exercicios 2024 2025 2026
```

`diff` mostra, para cada item, os dois valores, a diferença, a variação e o **excesso**: quanto o item variou além
do que variaria se tivesse crescido como o total. Excesso perto de zero é crescimento geral; excesso grande é
reclassificação ou mudança real de prioridade. Compare sempre o mesmo bimestre: o gasto não é um sexto por bimestre
(13º, abono salarial, cupons de títulos e obras têm calendário próprio).

Variáveis de ambiente opcionais: ver `.env.example`. Em modo local nada é obrigatório.

### Como a API do SICONFI estrutura o RREO da União (calibrado em 2026-11-08)

- `rotulo` repete o nome da seção ("DESPESAS (EXCETO INTRA-ORÇAMENTÁRIAS) (I)" / "(INTRA-...) (II)") em toda
  linha: é de onde vem o `escopo`.
- No Anexo 02, `cod_conta` é constante (`RREO2TotalDespesas`); a hierarquia vem da ordem e dos nomes. Funções
  pelo nome exato; subfunções típicas pelo nome; e duas linhas por função com prefixo da função-mãe:
  `FUxx - Administração Geral` (subfunção 122) e `FUxx - Demais Subfunções` (balde das subfunções atípicas).
- No Anexo 01, `cod_conta` é um código estável (`JurosEEncargosDaDivida`, `AmortizacaoDaDivida`,
  `AmortizacaoRefinanciamentoDaDivida`...) com sufixo `Intra` nas intraorçamentárias. Os filtros usam esse campo.
- O total (I) do Anexo 02 é igual ao `DespesasExcetoIntraOrcamentarias` do Anexo 01, **exclui** o refinanciamento
  (que só aparece na linha XII do Anexo 01) e **inclui** a amortização.
- Para a União, `RREO-Anexo 04` puro vem vazio; os anexos previdenciários são `RREO-Anexo 04 - RGPS` e
  `RREO-Anexo 04 - RPPS` (há também as variantes `04.0`, `04.1`...). Até agora todas as variantes vieram vazias.
- A União chama a subfunção 784 de "Transporte Aquaviário" (Portaria 42: "Hidroviário"); o CSV de referência
  aceita os dois nomes.

Para recalibrar depois de uma mudança na fonte, cole a saída do `inspect` (Anexo 02 e Anexo 01), do `mart` e do
`diff` contra o exercício anterior.

### Como o SIOP é acessado (S8)

- Endpoint SPARQL público (`https://www1.siop.planejamento.gov.br/sparql/`), um grafo por exercício
  (`http://orcamento.dados.gov.br/{ano}/`), vocabulário `http://vocab.e.gov.br/2013/09/loa#`. Modelo documentado pelo
  pacote oficial `orcamentobr` (Ministério do Planejamento; GPL-3, por isso este repositório usa só o modelo de dados,
  público, e nenhuma linha daquele código). Cada item orçamentário aponta para órgão, UO, função, subfunção, programa,
  ação, plano orçamentário, esfera, GND, modalidade, elemento, fonte, identificador de uso e resultado primário, e traz
  dotação inicial, dotação atual, empenhado, liquidado e pago (R$ inteiros). Não traz restos a pagar pagos.
- `fetch siop` lê primeiro a data da última atualização do grafo (`loa:dataUltimaAtualizacao`). Se não mudou desde o
  último snapshot e o raw está completo, o exercício inteiro é pulado. Quando mudou, baixa os membros (código e rótulo)
  de cada dimensão e as quatro agregações de `sources.yaml`: `fonte_gnd_subfuncao` e `rp_subfuncao` inteiras,
  `elemento_orgao` e `acao_subfuncao` fatiadas por função (uma consulta por função). Rótulos ficam fora das
  agregações para nenhum rótulo duplicado inflar uma soma. Consulta exata no `manifest.json`; aviso se uma resposta
  vier com exatamente 10 mil linhas (teto típico do Virtuoso; em 2026-11-08 não havia teto até 20 mil).
- Universo comparável ao RREO: esferas fiscal e da seguridade (`ofss`), sem modalidade 91 (`intra`) e, para o Anexo 02,
  sem refinanciamento (`refinanciamento` = GND 6 com fonte de código 443, exatamente como o RREO monta a linha XII;
  a fonte 444, "demais aplicações" dos títulos, não é rolagem e paga juros, a cobertura do BC e, em 2025, R$ 109 bi de
  benefícios do INSS, a insuficiência da regra de ouro).
- O que a primeira rodada real mostrou (2025): juros por GND batem ao bilhão com o Anexo 01 (R$ 363,5 bi); GND 6 total
  bate exatamente com amortização + refinanciamento (R$ 1.771,6 bi), com R$ 13,8 bi que o RREO chama de refinanciamento
  e o SIOP de amortização comum; e o serviço da dívida fora de 843/844 que o recibo removia como "ajuste declarado" é a
  ação 0669 "Cobertura do Resultado Negativo Apurado no Banco Central do Brasil" (R$ 111,2 bi em GND 6 e R$ 12,8 bi em
  GND 2 em 2025; R$ 40,7 bi em 2024; zero em 2026 até agosto), lançada na subfunção 846.

### O que as rodadas reais mostraram (2024/6, 2025/6, 2026/4)

- 2024 e 2025 publicáveis (fechados); 2026 publicável até o 4º bimestre (em andamento). Sete críticas OK nos três.
- Juros + amortização do Anexo 01 excedem as subfunções 843/844 em R$ 40,8 bi (2024, 1,2%), R$ 124,0 bi (2025, 3,3%:
  a informativa acusa) e zero ao centavo em 2026/4. Em 2025 o valor é idêntico do 4º ao 6º bimestre: lançamento
  único, concluído até agosto, e a única subfunção com espaço para ele é a 846 (que quase dobrou: cerca de R$ 182 bi
  para R$ 346 bi). O RREO não diz o que é; o SIOP dirá.
- A linha de aposentados do setor público (272/274 explícitas) caiu de R$ 124,1 bi para R$ 104,5 bi entre 2024 e
  2025: mudança de classificação na fonte, não de realidade. Reforça que a linha é piso e precisa do SIOP.
- Um ano em andamento se compara com o mesmo bimestre do ano anterior, nunca anualizado: até agosto de 2025 já
  estavam executados 74% dos juros do ano e 75% do trabalho (abono), mas só 58% da máquina e 59% da saúde.

## Metodologia (v0.2.1, resumo)

- Métrica: despesas liquidadas até o bimestre, exceto intraorçamentárias (no 6º bimestre, o exercício inteiro).
- Partição por **subfunção**, não por função: a função 09 "Previdência Social" não é o INSS; o INSS é a
  subfunção 271.
- **Aposentados e pensionistas do setor público vêm do SIOP** (elementos de despesa 01 e 03, em qualquer função e
  órgão, no mesmo universo do RREO), porque pela função/subfunção do RREO não dá para ver: em 2025, dos R$ 179,7 bi,
  só R$ 103,5 bi estavam na subfunção 272; R$ 34,2 bi de reserva e reforma dos militares estavam em Defesa, R$ 30,4 bi
  de pensões militares em Encargos Especiais (movidas da função 09 para a 28 em 2025) e R$ 10,4 bi do Fundo
  Constitucional do DF em Transferências. O que o SIOP encontra dentro de uma linha do RREO sai dela e entra na
  linha de aposentados (transferências declaradas na ficha de cada linha); o total do recibo não muda. Em ano em
  andamento, ou sem o staging do SIOP, vale o fallback do RREO (272/274), marcado como `piso`.
- O resíduo chama-se "Encargos especiais e demais funções": em 2025, metade dele é a subfunção 846 (sentenças
  judiciais, financiamentos com retorno, honras de aval) e metade são todas as outras funções juntas. O export abre
  a 846 por ação (`siop.detalhe_846`).
- FUNDEB e salário-educação (subfunção 847) já estão dentro de `FU12 - Demais Subfunções`, ou seja, em Educação.
- Juros vêm do Anexo 01 (`JurosEEncargosDaDivida`). As subfunções 843/844 (serviço da dívida) saem do Anexo 02
  porque misturam juros e amortização; a diferença é publicada como amortização, fora do R$ 1,00, e conferida
  contra `AmortizacaoDaDivida`.
- Refinanciamento fica fora do R$ 1,00; o valor exibido vem do Anexo 01 (`AmortizacaoRefinanciamentoDaDivida`).
- Residual ("máquina pública e demais funções") = total exceto intra − linhas atribuídas − exclusões.
- **Ajuste declarado** (`ajustes.servico_divida_fora_843_844` em `linhas.yaml`): juros + amortização do Anexo 01
  excedem as subfunções 843/844 (R$ 40,8 bi em 2024). Essa parcela, classificada em outras subfunções, é removida
  do residual; com isso vale a identidade total do recibo = despesa exceto intra − amortização do Anexo 01.
- Reconciliações críticas: soma das subfunções = soma das funções; soma das funções = total do cabeçalho (I); total
  do Anexo 02 = total do Anexo 01; prefixo `FUxx` consistente com a ordem; residual ≥ 0; centavos somam 100;
  identidade do total. Informativas: serviço da dívida fora de 843/844 ≤ 2% do total; Previdência Básica a menos
  de 5% de `BeneficiosPrevidenciarios`; variação anual de cada linha contra o mesmo bimestre do exercício anterior
  dentro de ±15% (acima disso, aviso com as linhas que saltaram; investigue com `recibo diff`).
- Cada linha do recibo sai com `anterior` (mesmo bimestre do exercício anterior) e `variacao_anual`, quando o
  staging tem o exercício anterior. É o que o site mostra no hover.
- Conferências cruzadas com o SIOP (só em ano fechado), calibradas em 2025 com diferença de centavos: liquidado por
  função (OFSS, sem intra, sem refinanciamento) contra o Anexo 02 (crítica); GND 2 contra juros do Anexo 01; GND 6
  contra amortização + refinanciamento; GND 6 com fonte 443 contra a linha XII; GND 2 e 6 fora de 841-844 contra o
  excesso que o RREO só estima por diferença (crítica), com o detalhe por ação: em 2024 e 2025 é a ação 0669,
  cobertura do resultado negativo do Banco Central (Lei 13.820/2019), nomeada no export em
  `fora_do_real.servico_divida_fora_843_844` e na nota da linha de juros; e elementos 01/03 dentro de 272/274 contra o
  RREO. Tolerância de 1 em 10 milhões porque o SIOP publica reais inteiros. Em ano em andamento sai só o aviso
  `siop_comparavel`. O export traz tudo em `siop`.

Tudo isso está em `config/linhas.yaml`, que gera o SQL, a ficha e os testes.

## Nuvem (depois da primeira execução local)

- Direção decidida: GitHub Actions como executor semanal, dados commitados em `data/`, BigQuery como espelho
  opcional (próxima versão). `infra/Dockerfile` e `infra/deploy.sh` (Cloud Run) serão removidos nessa versão.
- `RECIBO_STORE=gcs` grava o raw num bucket com versionamento, se alguém preferir rodar fora do GitHub.

## Licença

MIT (ver `LICENSE`).
