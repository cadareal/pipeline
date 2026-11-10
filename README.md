# Cada Real

**O site: [cadareal.org](https://cadareal.org/).** De cada R$ 1,00 de tributo que a União arrecada, quantos centavos vão
para onde? Este repositório é o programa que responde: baixa os relatórios oficiais, guarda cada um com um lacre, faz
as contas por regras públicas, confere o resultado de dezenas de jeitos e gera o site. Nenhum número é digitado à mão.

## Por quê

O projeto nasceu de uma pergunta feita numa conversa entre amigos, que cabe no bolso de qualquer cidadão: "para onde
foi o meu dinheiro?". Responder no detalhe é difícil porque a resposta está espalhada em relatórios de fontes
diferentes, com jargão de economia e contabilidade e metodologias que não conversam entre si. Quem faz o projeto
trabalha com dados e decidiu aplicar o ofício nas contas públicas e compartilhar o resultado. O Cada Real é cívico,
independente e mantido com recursos próprios: sem partido, sem patrocinador, sem publicidade e sem opinião sobre se o
gasto é bom ou ruim. Ele mostra o que foi encontrado, independentemente do que for encontrado, com a fonte, o lacre e a
conferência de cada número. A base disso está na Constituição: todo o poder emana do povo (art. 1º), quem gere dinheiro
público presta contas (art. 70) e qualquer cidadão pode cobrar (art. 74, § 2º).

Achou um número que não bate, um bug ou discorda de uma regra? [Abra uma issue](https://github.com/cadareal/pipeline/issues/new/choose)
(há um modelo para cada caso) ou escreva para contato@cadareal.org. As perguntas que o próprio site ainda não sabe
responder estão em [cadareal.org/perguntas-abertas](https://cadareal.org/perguntas-abertas/), com os pedidos feitos ao
Tesouro pela Lei de Acesso à Informação.

## O que é

Pipeline aberto de dados fiscais (pacote Python `recibo`). Fase 1: Recibo do Contribuinte (União). Fontes implementadas: SICONFI (RREO da União, S4), SIOP (execução do
orçamento federal por classificador, S8) e COFOG (despesa do Governo Central na classificação internacional de
funções, S7; produto separado do recibo, `mart cofog`, desde a v0.6.3), Dívida Pública Federal do Tesouro (S1) e séries do
SGS do Banco Central (S16; raw, staging e inspect na v0.7.0, layout fixado em config na v0.7.1 e o `mart divida`, também
produto separado, na v0.7.2, conferido contra os arquivos reais na v0.7.3) e população do IBGE (S14; per capita como
memo do recibo e grupo do mart da dívida, v0.8.0). O site estático (`recibo site`, v0.9.0) gera as páginas em `docs/` a
partir dos exports, com o desenho de fluxo que abre cada linha nível a nível e, desde a v0.10.0, a visão "A dívida"
(quanto a União deve, do que a dívida é feita e quando vence, título a título). As demais fontes estão catalogadas em
`config/sources.yaml` e entram em seguida.

Princípios (ver documento "Transparência do Orçamento: Regras e Fontes"):

- Nenhum número publicado é digitado à mão: tudo nasce de um fetcher versionado sobre um snapshot raw imutável.
- O R$ 1,00 é o que o governo gastou pela conta oficial do Tesouro (despesa primária do Anexo 06 do RREO) mais os
  juros pagos. Troca de ativo nunca soma como gasto: rolagem, pagamento de principal e empréstimos e aplicações do
  Tesouro ficam fora do R$ 1,00 (visíveis, com lista). As linhas somam exatamente R$ 1,00.
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
 fetch cofog   ──> raw/cofog   ──> transform cofog   ──> staging/stg_cofog.parquet ──> mart cofog ──> marts/ + export/
                                                                                 (régua internacional; produto separado,
                                                                                  com a ponte para o total do recibo)
 fetch divida  ──> raw/divida  ──> transform divida  ──> staging/stg_divida__*.parquet  ──┐
 fetch bcb     ──> raw/bcb     ──> transform bcb     ──> staging/stg_bcb_sgs.parquet     ──┴─> mart divida ──> marts/ + export/
                                                                                 (quatro medidas da dívida, cada uma com suas
                                                                                  réguas nomeadas; produto separado do recibo)
 fetch ibge    ──> raw/ibge    ──> transform ibge    ──> staging/stg_ibge_populacao.parquet ──> população do ano: memos por
                                                                                  habitante no recibo e grupo por habitante
                                                                                  no mart divida (nenhum centavo muda)
 export/recibo_AAAA.json + export/divida_AAAA.json ──> site ──> docs/ (HTML estático: página do ano com o desenho, uma
 conteudo/site.yaml, metodologia.md, quem-somos.md ──┘       página por linha, metodologia, quem somos, dados; GitHub Pages)
```

| Camada | Onde | O que |
| --- | --- | --- |
| raw | `data/raw/{fonte}/{dataset}/{chave}/{snapshot}/` | resposta da API como veio (`pages.jsonl` no SICONFI, `result.json` no SIOP, o CSV do ano e o `package_show.json` do CKAN no COFOG) + `manifest.json` (url ou consulta SPARQL, sha256, timestamp). `_checks.jsonl` registra toda verificação, mudou ou não. |
| staging | `data/staging/stg_siconfi_rreo.parquet` | todas as colunas da API + derivações (`escopo`, `nivel`, `funcao_cod`, `subfuncao_cod`) |
| staging | `data/staging/stg_siop_execucao.parquet` | agregações do SIOP por códigos (esfera, modalidade, função, subfunção, GND, elemento, fonte, RP, ação) com rótulos e flags (`intra`, `ofss`, `refinanciamento`) |
| raw | `data/raw/divida/{dataset}/{arquivo}/{snapshot}/` e `data/raw/bcb/sgs/{codigo}/{snapshot}/` | Tesouro: `package_show.json` de cada dataset e o CSV/XLSX como veio; BCB: o JSON da série e, em `sgs_meta`, a ficha da série no portal de dados abertos |
| staging | `data/staging/stg_divida__{dataset}__{arquivo}.parquet` + `stg_divida_arquivos.parquet` | um parquet por arquivo do Tesouro: CSV com as colunas originais como texto mais `{coluna}__num` e `{coluna}__data` quando a leitura é segura; XLSX como grade (planilha, linha, coluna, valor). O índice lista arquivos, procedência e resumo |
| staging | `data/staging/stg_bcb_sgs.parquet` | um ponto por linha: código, data, valor, ficha de sources.yaml (nome, tipo, unidade), título no BC, procedência |
| raw | `data/raw/ibge/projecao/{arquivo}/{snapshot}/` e `data/raw/ibge/sidra/t{tabela}_v{variavel}/{snapshot}/` | IBGE: a planilha da projeção revisão 2024 como veio e o JSON de cada tabela do SIDRA |
| staging | `data/staging/stg_ibge_populacao.parquet` | uma linha por (régua, ano, local): projeção (1º de julho, 2000 a 2070, Brasil, regiões e UFs, com sexo e 65+), estimativas oficiais e Censo 2022; procedência |
| staging | `data/staging/stg_cofog.parquet` | uma linha por linha do CSV do Tesouro (ano, natureza detalhada, ação, UO, classificação econômica GFS, código COFOG, valor) + derivações (`cofog_divisao/grupo/classe`, `categoria`, `gnd`, `modalidade`, `elemento`, `gfs2`, `ajuste`) + unidade (`valor` em reais, `valor_fonte` como veio, `unidade_fonte`, `fator_unidade`, `resolucao_reais`) |
| marts | `data/marts/recibo_{ano}_p{bimestre}.json` e `.csv` | linhas do recibo, shares, centavos, exclusões, memos (receitas, resultado, dívida), reconciliações |
| marts | `data/marts/cofog_{ano}.json` e `.csv` | régua COFOG (dez divisões e 69 grupos), ajustes do Tesouro, ponte com o recibo, reconciliações, glossário |
| marts | `data/marts/divida_{ano}.json` e `.csv` | dívida em quatro medidas (juros do ano, dívida nova, estoque, risco de rolagem), cada uma com várias réguas oficiais nomeadas, composição por indexador, reconciliações, glossário |
| export | `data/export/recibo_{ano}.json`, `data/export/cofog_{ano}.json` e `data/export/divida_{ano}.json` | o que o site consome (recibo, com o bloco `niveis` de cada linha desde a v0.9.0 e `fontes_de_recursos` desde a v0.9.1; dívida com `execucao.por_carteira` e, desde a v0.10.0, `estoque_detalhe`, `conta_do_ano` e `estoque_mensal`; COFOG) |
| site | `docs/` | páginas HTML geradas por `recibo site`: `index.html` (ano atual), `AAAA/`, `AAAA/linha/<linha>/`, `metodologia/`, `quem-somos/`, `dados/` (com os exports copiados e um CSV por ano), `static/` (um CSS e um JS, nada de fora) |

Idempotência: rodar de novo sem mudança na fonte não cria snapshot (só registra a verificação). Mudança em
período já publicado gera snapshot novo com `previous_snapshot_id`, e um aviso `REVISAO NA FONTE` no log.

## Rodando localmente

Pré-requisitos: Python 3.11+ e [uv](https://docs.astral.sh/uv/).

```bash
uv sync --extra dev
uv run pytest                      # 88 testes sobre um RREO (Anexos 01, 02 e 06), um SIOP, um COFOG, arquivos da dívida, séries do BC e população do IBGE sintéticos, e o site gerado a partir deles

# 1) baixar RREO da União (anexos 01, 02, 04-RGPS, 04-RPPS, 06; bimestres 1 a 6)
uv run recibo fetch siconfi --exercicios 2024 2025 2026

# 2) ver o que a API devolveu e como foi classificado (cole a saída de volta para calibrar)
uv run recibo inspect siconfi --exercicio 2024 --periodo 6 --anexo "RREO-Anexo 02" --rows 150
uv run recibo inspect siconfi --exercicio 2025 --periodo 6 --anexo "RREO-Anexo 01" --coluna "ATE O BIMESTRE (C)" --rows 120   # receitas realizadas
uv run recibo inspect siconfi --exercicio 2025 --periodo 6 --anexo "RREO-Anexo 06" --todas-colunas --rows 200                   # resultado primário e nominal
#    --out arquivo.txt grava a mesma saída num arquivo (inspect, mart e, como --out-txt, diff): o terminal corta saídas longas

# 3) raw -> staging
uv run recibo transform siconfi

# 4) recibo de 2024 (usa o último bimestre disponível no índice; --periodo força um)
uv run recibo mart recibo --exercicio 2024

# 5) SIOP: baixa (só refaz um exercício quando a data de atualização do grafo muda), transforma e inspeciona
uv run recibo fetch siop --exercicios 2024 2025 2026        # ~100 consultas por exercício na primeira vez; depois, 1 por exercício sem mudança
uv run recibo fetch siop --exercicios 2023 2024 2025 2026 --agregacoes acao_rp_subfuncao   # só uma agregação nova (v0.4.0), sem refazer as outras
uv run recibo transform siop
uv run recibo inspect siop --exercicio 2025 --agregacao acao_subfuncao --funcao 28 --rows 30
uv run recibo mart recibo --exercicio 2025                  # com o staging do SIOP presente, rodam as conferências cruzadas

# 6) o que mudou entre dois períodos: linhas, Anexo 01, funções e subfunções (mesmo bimestre por padrão)
uv run recibo diff --exercicio 2025 --contra 2024                         # 2025/p6 contra 2024/p6
uv run recibo diff --exercicio 2026 --contra 2025 --ordem excesso         # 2026/p4 contra 2025/p4, reclassificações primeiro
uv run recibo diff --exercicio 2025 --contra 2024 --funcao 28 09 --top 0  # só Encargos Especiais e Previdência, todas as subfunções
uv run recibo diff --exercicio 2025 --contra 2024 --out data/marts/diff_2025_2024.json

# 7) COFOG (S7): lista o dataset no CKAN do Tesouro e baixa um CSV por ano (~20 MB cada; só baixa de novo o que mudou)
uv run recibo fetch cofog                                   # todos os anos que o CKAN oferece (2010 a 2024 em 9/nov/2026)
uv run recibo fetch cofog --exercicios 2023 2024            # só esses; --force baixa de novo mesmo sem mudança no CKAN
uv run recibo transform cofog                               # para com erro claro se o cabeçalho ou a escala dos números não forem os esperados
uv run recibo inspect cofog --exercicio 2024 --por anos     # total por ano
uv run recibo inspect cofog --exercicio 2024 --por divisao  # dez divisões COFOG; também grupo | classe | gfs | gnd | ajuste | uo | acao | natureza
uv run recibo inspect cofog --exercicio 2024 --por acao --cofog 10.2 --rows 40 --out ~/Downloads/cofog_102.txt   # dentro de "velhice", por ação

# 8) mart do COFOG (produto separado do recibo): régua das dez divisões, ajustes do Tesouro e a ponte com o total do recibo
#    do mesmo ano (precisa do staging do RREO com o 6º bimestre do ano); sai em data/marts/cofog_AAAA.json e data/export/
uv run recibo mart cofog --exercicio 2024 --out ~/Downloads/cofog_mart_2024.txt

# 9) dívida (S1, CKAN do Tesouro) e BCB (S16, séries do SGS): acesso, lacre e leitura
uv run recibo fetch divida                                  # os datasets de sources.yaml (S1.datasets); --datasets estoque_dpf fatores_variacao para só esses
uv run recibo transform divida                              # um parquet por arquivo, sem adivinhar layout
uv run recibo inspect divida --rows 25 --out ~/Downloads/divida_inspect.txt           # colunas, tipos lidos, valores distintos, somas por ano; planilhas em grade
uv run recibo inspect divida --dataset fatores_variacao --rows 40                     # só um dataset
uv run recibo inspect divida --dataset fatores_variacao --ano 2024                    # planilha: todas as linhas nas colunas de 2024 (meses e total anual)
uv run recibo inspect divida --dataset estoque_dpf --filtro "Mes do Estoque=12/2024"  # CSV: recorta linhas (COLUNA=VALOR, repetível) e soma por valor distinto
uv run recibo fetch bcb                                     # as 34 séries de sources.yaml (S16.series); --series 4607 4606 para só essas
uv run recibo transform bcb
uv run recibo inspect bcb --ano 2024 --out ~/Downloads/bcb_2024.txt                   # todas as séries no ano: soma dos meses (fluxos) ou último valor (saldos, taxas)
uv run recibo inspect bcb --serie 4607 --rows 24                                      # uma série: ficha, por ano, últimos valores

# 10) mart da dívida (produto separado do recibo): quatro medidas, cada uma com suas réguas nomeadas (Tesouro, BC, RREO,
#     COFOG), lidas do staging da dívida, do BCB, do recibo do ano e do COFOG quando existe; o layout dos arquivos do
#     Tesouro vem de linhas.yaml (bloco `divida`). Num ano em andamento usa o último mês presente em todas as fontes.
#     Sai em data/marts/divida_AAAA.json e .csv e data/export/divida_AAAA.json; código de saída 3 se uma conferência crítica falhar
uv run recibo mart divida --exercicio 2024 --out ~/Downloads/divida_2024.txt
uv run recibo mart divida --exercicio 2025 --periodo 4      # --periodo força o bimestre do RREO usado para a linha do recibo

# 11) população (S14, IBGE): a planilha da projeção revisão 2024 (ftp.ibge) e as estimativas oficiais (apisidra, tabela 6579);
#     de fora do Brasil o IBGE barra o catálogo e o site, mas serve esses dois; IPv4 forçado. Liga os memos por habitante do
#     recibo e o grupo por habitante do mart divida (basta o staging existir; sem ele, saem como indisponíveis)
uv run recibo fetch ibge
uv run recibo transform ibge
uv run recibo inspect ibge --ano 2019 --out ~/Downloads/ibge.txt   # réguas lado a lado por ano (projeção, estimativa, censo) e sexo/65+
uv run recibo inspect ibge --local "São Paulo"                      # uma UF ou região, pelo nome da planilha
uv run recibo mart recibo --exercicio 2024                           # agora com "Gasto e tributos por habitante" e "Dívida nova por habitante"

# tudo de uma vez (fetch e transform de SICONFI, SIOP e IBGE, depois os recibos); --sem-siop e --sem-ibge pulam essas fontes
# (o IBGE falha macio: sem resposta, os recibos saem sem per capita); COFOG e dívida são à parte
uv run recibo run --exercicios 2024 2025 2026

# 12) o site (v0.9.0; carimbo da fonte na v0.9.1; bandeira, Constituição e desenho sem cortes na v0.9.2; visão "A dívida"
#     na v0.10.0): lê data/export/*.json (recibo de cada ano; dívida e COFOG quando existem) e escreve docs/.
#     Só anos publicáveis viram página; o comando diz qual ficou de fora e por quê. Textos em conteudo/ (site.yaml,
#     metodologia.md, quem-somos.md): edite e gere de novo. Antes, refaça os exports com a versão atual (o bloco
#     `niveis` das linhas e a execução por carteira da dívida nasceram na 0.9.0; `fontes_de_recursos`, na 0.9.1; o
#     detalhe título a título, a conta do ano e o estoque mensal da dívida, na 0.10.0). Sem o export da dívida de um
#     ano, a página sai sem a aba "A dívida" (uma aba desativada diz o motivo); 2026 em andamento também tem a aba
uv run recibo mart recibo --exercicio 2023 && uv run recibo mart recibo --exercicio 2024 && uv run recibo mart recibo --exercicio 2025 && uv run recibo mart recibo --exercicio 2026
uv run recibo mart divida --exercicio 2023 && uv run recibo mart divida --exercicio 2024 && uv run recibo mart divida --exercicio 2025 && uv run recibo mart divida --exercicio 2026
uv run recibo site --limpar                                  # docs/ na raiz do repositório; --out para outra pasta; --exercicios 2025 para um ano só
uv run python -m http.server -d docs 8000                   # abra http://localhost:8000/ (Ctrl+C para parar)

# 13) publicar no GitHub Pages: commite docs/ e, no GitHub, Settings > Pages > "Deploy from a branch" > main, pasta /docs.
#     Domínio próprio: `dominio` e `url` em conteudo/site.yaml (o gerador escreve docs/CNAME, robots.txt, sitemap.xml e
#     404.html); no registrador, registros A do GitHub Pages e CNAME www; em Settings > Pages, "Custom domain" e
#     "Enforce HTTPS". A cada rodada nova: mart, site, commit. O site é estático: sem servidor, sem banco, sem cookie.
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
  `AmortizacaoRefinanciamentoDaDivida`...) com sufixo `Intra` nas intraorçamentárias. Os filtros usam esse campo. As
  receitas estão na mesma chave, na coluna `ATE O BIMESTRE (C)` (realizadas até o bimestre); a hierarquia fecha ao
  centavo (correntes + capital = exceto intra). Dois códigos longos (remuneração das disponibilidades do Tesouro e
  resultado do Banco Central) o `inspect` corta em 34 caracteres; o config os referencia por prefixo.
- No Anexo 06 (resultados primário e nominal), `cod_conta` também é estável, quase sempre com prefixo `RREO6`
  (exceções: a linha (I) das receitas, `ReceitasPrimariasCorrentes`, e as do resultado nominal e da dívida). Uma
  coluna por medida: `RECEITAS REALIZADAS (A)`, `DESPESAS LIQUIDADAS`, `DESPESAS PAGAS (A)`, `RESTOS A PAGAR
  PROCESSADOS PAGOS (B)`, `LIQUIDADOS` e `PAGOS (C)` (restos não processados), `VALOR`, `VALOR INCORRIDO` (juros por
  competência), `EM 31/12/AAAA (A)` e `ATE O BIMESTRE AAAA (B)` (dívida). A mesma conta aparece em mais de uma linha,
  com colunas disjuntas (a API devolve os restos a pagar processados em linha separada): somar por (conta, coluna)
  é seguro. A linha "RESULTADO PRIMÁRIO - Acima da Linha (IX)" tem o cod_conta `RREO6ResultadoPrimarioEstadosMunicipios`.
  O "ajuste metodológico do resultado primário acima da linha" só existe em 2025 e 2026.
- O total (I) do Anexo 02 é igual ao `DespesasExcetoIntraOrcamentarias` do Anexo 01, **exclui** o refinanciamento
  (que só aparece na linha XII do Anexo 01) e **inclui** a amortização.
- Para a União, `RREO-Anexo 04` puro vem vazio; os anexos previdenciários são `RREO-Anexo 04 - RGPS` e
  `RREO-Anexo 04 - RPPS` (há também as variantes `04.0`, `04.1`...). Até agora todas as variantes vieram vazias.
- A União chama a subfunção 784 de "Transporte Aquaviário" (Portaria 42: "Hidroviário"); o CSV de referência
  aceita os dois nomes.

Para recalibrar depois de uma mudança na fonte, cole a saída do `inspect` (Anexo 02 e Anexo 01), do `mart` e do
`diff` contra o exercício anterior.

### Como o COFOG é acessado (S7)

- Dataset "Despesas por Função do Governo Central - Classificação COFOG" no CKAN do Tesouro Transparente (CESEF), id
  `22d13d17-bf69-4a1a-add2-25cc1e25f2d7`: um CSV por ano ("Base COFOG AAAA", ~20 MB, 2010 a 2024 em 9/nov/2026; o de
  2024 foi criado em 12/dez/2025) e um PDF de metadados. `fetch cofog` chama `package_show`, lacra a lista de recursos
  no raw (hash sobre id, nome, url, datas e tamanho, não sobre contadores de acesso) e baixa cada CSV byte a byte. Um
  ano só é baixado de novo quando a data ou o tamanho do recurso mudaram no CKAN (`--force` ignora isso); conteúdo
  igual não cria snapshot. O Tesouro republica arquivos antigos (os de 2010 a 2022 tinham last_modified de março de
  2025): hash diferente vira snapshot novo e aviso `REVISAO NA FONTE`.
- Pelos metadados: Governo Central Orçamentário (Executivo, Legislativo, Judiciário, RPPS e RGPS; sem o Banco
  Central); despesa liquidada mais restos a pagar não processados pagos (aproxima a competência); transferências a
  outros entes pelo caixa; juros por competência (dado do Banco Central); venda de bens entra negativa; coluna
  "Ajustes Metodológicos" para alinhar ao GFSM 2014. Colunas: ano, natureza detalhada (8 dígitos), ação, unidade
  orçamentária, classificação econômica GFS, código COFOG e valor.
- O CSV real (lido em 9/nov/2026) não segue à risca o PDF de metadados, e a v0.6.0 recusou os arquivos por isso. O que
  o arquivo traz de verdade: cabeçalho em maiúsculas sem acento e com nomes mais curtos (`ANO`, `AJUSTE METODOLOGICO`,
  `ACAO GOVERNO`, `UNIDADE ORCAMENTARIA`, `CLASSIFICACAO ECONOMICA GFS`, as descrições com o sufixo `DESCRICAO`);
  a palavra `NA` na coluna de ajuste quando a linha não é ajuste; o código COFOG no padrão do manual do FMI (GFSM), que
  põe um 7 na frente: `7102` é a divisão 10 (proteção social), grupo 2 (velhice), e `70911` é a divisão 09, grupo 1,
  classe 1. Desde a v0.6.1, `transform cofog` aceita as duas grafias de cabeçalho (a real e a dos metadados), trata
  `NA`, `-` e vazio como "sem ajuste", tira o 7 da frente quando o que sobra é uma divisão de 01 a 10 (o código original
  fica em `cofog_digitos_fonte`) e para com erro se algum código cair fora das dez divisões.
- Os arquivos não estão todos na mesma unidade, e o cabeçalho ("VALOR (R$)") não avisa. Na primeira rodada real
  (9/nov/2026) a guarda de escala recusou 2024: o arquivo somava 3.786.067,04, e 2019 e 2020 somavam 2,28 tri e 2,64 tri.
  Ou seja, 2019 e 2020 (republicados em março de 2025) estão em reais com centavos e 2024 (criado em dezembro de 2025)
  está em R$ milhões com duas casas, o que dá R$ 3,786 tri (bate, com 1,2% de diferença, com a conta do Anexo 06 de 2024:
  despesa primária liquidada 2.633,7 bi + restos não processados pagos 103,0 bi + juros nominais 1.005,4 bi). Desde a
  v0.6.2 a unidade de cada ano é declarada em `sources.yaml` (`unidade_valor_padrao: reais`,
  `unidade_valor_por_exercicio: {2024: milhoes}`), nunca adivinhada: o transform multiplica pelo fator, guarda o número
  original em `valor_fonte` e anota `unidade_fonte`, `fator_unidade` e `resolucao_reais`. Em milhões com duas casas,
  cada linha só é precisa até R$ 10 mil: por isso o COFOG nunca vai bater com o Anexo 06 no centavo, e as conferências
  entre os dois serão em fração de por cento, não em centavos. Quando um ano novo chegar em outra unidade, a guarda
  para e a mensagem diz qual unidade faria o total caber; a correção é uma linha na config, à vista no git.
- Para conferir a unidade há também o Boletim COFOG 2020 do Tesouro, que publica a mesma base em R$ milhões:
  2019 = 2.389.291 e 2020 = 2.733.405 (preliminar). Os arquivos abertos de hoje somam 2.281.854 e 2.635.975 milhões,
  4,5% e 3,6% abaixo do boletim (a série foi republicada em 2025; o pipeline usa o arquivo aberto, lacrado por hash,
  e não o boletim).
- `transform cofog` casa o cabeçalho pelos nomes (sem acento, maiúsculas e minúsculas tanto faz; se o Tesouro renomear
  uma coluna de um jeito que nenhuma das grafias conhecidas alcance, para e imprime o cabeçalho real), decide por
  arquivo se os números são brasileiros (vírgula decimal, ponto de milhar), aplica a unidade declarada e a guarda de
  escala: o total do ano, em reais, tem que ficar entre R$ 0,5 tri e R$ 20 tri (`sources.yaml`), senão o ano é
  recusado (todos os anos são conferidos antes de parar, e nada é gravado). `inspect cofog` lê o staging e, quando o
  transform recusou o arquivo, lê direto do raw, sem a guarda mas com a unidade da config, para mostrar o que veio.
  Referência das dez divisões e 69 grupos COFOG em `config/ref/cofog.csv`.
- `mart cofog` (v0.6.3) é um produto separado do recibo, e a separação é de propósito: o recibo responde ao cidadão
  "o que aconteceu com o dinheiro" pela conta de caixa do Tesouro; o COFOG responde a quem pergunta "e pelo padrão
  internacional?" (manual de estatísticas fiscais do FMI, GFSM 2014). Nenhum número do recibo depende do COFOG. O mart
  publica, por ano: as dez divisões e os 69 grupos com valor, share e centavos do total do COFOG; a ficha (arquivo, data
  do recurso, snapshot e hash, unidade e resolução); os ajustes metodológicos do Tesouro com valores, separando os que
  somam ao total dos que só remanejam; e a ponte com o recibo do mesmo ano (seção `cofog` de `linhas.yaml`):
  total do recibo, menos juros pagos, mais restos a pagar não processados pagos (Anexo 06), mais juros pelo Banco
  Central (ajuste "JUROS"), mais IRRF retido por estados e municípios (ajuste "TRANSFERENCIA DO IRRF"), mais venda de
  bens (ajuste "RECEITA DE ALIENACOES", negativo) = esperado, contra o total publicado. Reconciliação `ponte_cofog`,
  tolerância de 2% do total do COFOG: fora dela o mart do COFOG não é publicável e o recibo não é afetado. Observado em
  9/nov/2026: 2024 fecha em -0,31% (2.633,68 + 102,97 + 937,68 + 130,54 - 7,13 = 3.797,75 bi contra 3.786,07
  publicados) e 2023 em -0,66%. Os demais ajustes (FUNDEB, Fundo do DF, precatórios, bolsas, Minha Casa Minha Vida,
  Pé-de-Meia, educação profissional; 161,8 bi em 2024) remanejam despesas que já estão no orçamento para a função
  certa e não entram na soma: com eles a ponte abriria 173 bi. Os juros do COFOG (937,68 bi em 2024) são um número do
  Banco Central que não é nem o bruto (1.005,43) nem o líquido (679,34) do Anexo 06; qual série é, a fase da dívida
  amarra. A comparação lado a lado das dez linhas do recibo com as gavetas do COFOG ficou para a V2, assim como a
  comparação com outros países (base GFS do FMI, outra fonte).

### Como a dívida (S1) e as séries do Banco Central (S16) são acessadas

- **S1, Tesouro.** Cinco datasets do CKAN do Tesouro Transparente (ids em `sources.yaml`, `S1.datasets`): Estoque da
  Dívida Pública Federal (CSV e XLSX), Fatores de Variação da DPF (XLSX; emissões, resgates e juros apropriados, isto é,
  os juros que o estoque gerou no mês, pagos ou não), Emissões e Resgates (XLSX), Execução Orçamentária e Financeira da
  DPF por Natureza da Despesa (CSV; juros, amortização e refinanciamento, a conferir com o Anexo 01) e Estoque do
  Tesouro Direto (CSV). `fetch divida` lacra o `package_show` de cada dataset e baixa cada recurso nos formatos listados;
  a chave do raw é o nome do arquivo com extensão (o CSV e o XLSX do mesmo dataset costumam ter o mesmo nome); um
  recurso só é baixado de novo quando a data ou o tamanho mudaram no CKAN (`--force` ignora). Catálogo lido em 9/nov/2026:
  o Relatório Mensal da Dívida (detentores por classe, colchão de liquidez) não está no CKAN em planilha, e os limites da
  LRF para a dívida da União pararam em 2021.
- **`transform divida` não adivinha layout.** CSV: todas as colunas originais como texto, mais `{coluna}__num` quando
  pelo menos 90% das células não vazias são números (vírgula decimal e ponto de milhar, ou ponto decimal, decidido por
  coluna) e `{coluna}__data` quando 90% são datas (dd/mm/aaaa, aaaa-mm-dd, mm/aaaa, aaaa-mm, aaaamm; mês vira dia 1).
  XLSX: grade longa (planilha, linha, coluna, valor_texto, valor_num, eh_data), porque as planilhas do Tesouro são
  relatórios formatados. O layout de cada arquivo está fixado em `linhas.yaml` (bloco `divida`, v0.7.1/0.7.2), depois de
  visto pelo `inspect divida`: aba, unidade, rótulos de linha, nomes de coluna e valores de texto que o mart usa.
  Arquivo ilegível fica registrado no índice com o erro e não derruba os outros.
- **Como o mart lê as planilhas do Tesouro (`src/recibo/grade.py`).** Os anexos 2.1 (estoque, R$ bilhões) e 2.9
  (fatores de variação, R$ milhões) têm rótulos na coluna 1, uma linha de cabeçalho com os meses no formato `Mmm/aa`
  (Jan/07 ... Dez/24) e, depois de cada dezembro, uma coluna com o total do ano. O rótulo de `linhas.yaml` é comparado
  sem acento, sem caixa, sem espaços duplicados e sem os números de nota de rodapé que o Tesouro cola no fim
  ("Estoque anterior1", "DPFe ¹"). Rótulo não encontrado ou aba sem cabeçalho param com erro que lista os parecidos.
- **S16, Banco Central.** SGS é o sistema de séries do BC; cada série tem um número e sai pela API pública de dados
  abertos (`https://api.bcb.gov.br/dados/serie/bcdata.sgs.{codigo}/dados?formato=json&dataInicial=...&dataFinal=...`),
  sem chave. As 34 séries de `sources.yaml` (`S16.series`, códigos lidos no portal de dados abertos do BC em 9/nov/2026)
  cobrem: juros nominais por esfera (4607 Governo Federal, 4608 Banco Central, 4606 os dois, 4616 setor público), resultado
  primário e nominal por esfera (4640, 4641, 4639, 4649, 7853, 7854; 4574, 4575, 4573, 4583), dívida líquida do setor
  público em R$ milhões (4469, 4470, 4468, 4478) e em % do PIB (4504, 4503, 4513), dívida líquida do governo geral
  (4536) e bruta (13762, % do PIB), dívida mobiliária (4151 emitida, 4154 em mercado, 4152 na carteira do BC),
  participação por indexador (4177 Selic, 4178 prefixado, 4173 câmbio, 4175, 4176, 4174, 4180) e prazo médio (10618).
  NFSP = necessidades de financiamento do setor público, a conta "abaixo da linha" do BC. `fetch bcb` pede a série inteira
  desde `desde` (2006) até hoje; o hash canônico são os pontos ordenados por data, então a data final da consulta não
  cria snapshot novo; a ficha da série no portal de dados abertos é lacrada em `sgs_meta` (opcional: se o portal falhar,
  a série é gravada mesmo assim). `transform bcb` para se uma data vier fora de dd/mm/aaaa ou um valor não for número.
- **`inspect bcb --ano AAAA`** é a tabela que se compara com o Anexo 06 e com o COFOG: fluxos somados nos meses do ano
  (com o número de meses publicados), saldos e taxas no último valor do ano. A hipótese da primeira rodada (a soma de
  2024 de uma das séries de juros nominais, 4607 ou 4606, seria igual aos R$ 937,68 bi da linha JUROS do COFOG) não se
  confirmou: em 2024 a 4607 (Governo Federal) soma R$ 648,4 bi, a 4608 (Banco Central) 206,8 bi, a 4606 (os dois) 855,2 bi
  e a 4616 (setor público) 950,4 bi, contra 937,7 bi no COFOG; o número do COFOG fica nomeado como "informado pelo BC".

### O que o `mart divida` publica (v0.7.2)

Quatro medidas, cada uma com várias réguas oficiais, **nomeadas e nunca somadas entre si**. Produto separado: nada no
recibo depende dele.

1. **Juros do ano.** Pagos em dinheiro pelo orçamento (Anexo 01, a linha do recibo) e pela execução da DPF do Tesouro
   (devem bater; a execução inclui a cobertura do resultado negativo do BC, ação 0669); apropriados no estoque da DPF
   (Tesouro, Anexo 2.9: o que a dívida gerou de juros, pagos ou não); nominais pelo Banco Central (NFSP, líquidos; Governo
   Federal e Governo Central); por competência no RREO (Anexo 06, XI brutos e XI menos X líquidos); e o número do COFOG.
2. **Dívida nova no ano.** Variação da DPF em poder do público aberta como o Tesouro abre (estoque anterior, mais
   emissões, menos resgates, mais juros apropriados, mais operações com o BC, estoque final); variação da dívida
   consolidada líquida do RREO (o "abaixo da linha" do RREO, com o sinal trocado); variação da dívida líquida do Governo
   Federal pelo BC; resultado nominal e primário do Governo Central pelo BC.
3. **Estoque.** DPF em poder do público (planilha 2.1 e, título a título, o CSV: têm que dar o mesmo número), carteira
   do BC, dívida consolidada e consolidada líquida do RREO, dívida líquida do Governo Federal e do setor público (reais e
   % do PIB), dívida bruta do governo geral (% do PIB) e Tesouro Direto.
4. **Risco de rolagem.** Fatia da DPF em mercado que vence em 12 meses (título a título), prazo médio (BC), composição
   por indexador pelo nome do título (Selic, prefixado, índice de preços, câmbio, demais; regras em `linhas.yaml`),
   conferida com as séries 4177 e 4178 do BC. Colchão de liquidez e detentores por classe não têm fonte aberta em
   planilha ou API (só o Relatório Mensal da Dívida, em PDF) e aparecem como indisponíveis, com o motivo.

Mês de referência: dezembro num ano fechado; num ano em andamento, o último mês presente em todas as fontes mensais
(fluxos somados de janeiro até ele, saldos nele). As comparações com o Anexo 01 recortam a execução da DPF até o fim do
bimestre do RREO usado. Linhas da execução iguais em tudo menos em "Tipo de Dívida" são contadas uma vez, com aviso: o
arquivo de 2025 repete a cobertura do resultado negativo do BC (12,78 bi de juros e 111,25 bi de principal) sob "Interna"
e sob "Política Monetária", e o Anexo 01 e o SIOP a trazem uma vez só. Conferências críticas (falhou, não publica): identidade dos fatores do Tesouro (estoque anterior +
emissões + resgates + juros + operações com o BC = estoque final), planilha 2.1 contra CSV título a título e planilha 2.1
contra planilha 2.9. Informativas: carteira do BC e DPMFi contra o BC, juros e principal do Anexo 01 contra a execução
da DPF, cobertura do BC contra o serviço fora de 843/844 do recibo, indexadores contra o BC, coluna anual do Tesouro
contra a soma dos meses. Uma conferência cujo lado falta (fonte indisponível) sai como "não calculada", não como falha
de número.

Rodadas reais (10/nov/2026, 2023 a 2025): identidade dos fatores fechada a R$ 0,05 (2024: 6.520.328.667.414,06 +
1.492.472.570.279,10 − 1.458.474.245.086,66 + 762.426.822.297,24 − 681.099.657,97 = 7.316.072.715.245,72); planilha 2.1 e
CSV título a título iguais a R$ 0,17; carteira do BC igual à série 4152 (que só tem R$ 10 mil de resolução); série 4154
do BC 23 a 29 mi acima da DPMFi do Tesouro em 7 tri; juros da execução da DPF iguais ao Anexo 01 a R$ 363 (2023), 55 mi
(2024, juros fora da DPF) e R$ 1,23 (2025, depois de contar a cobertura do BC uma vez); cobertura do BC contra o serviço
fora de 843/844 do recibo: 72,29 mi (2024, o cacau 00QM) e 460 mil (2025); LFT e prefixados iguais ao BC em duas casas
nos três anos. Contra o Relatório Mensal da Dívida de dezembro de 2024 (PDF): composição 46,29 / 21,99 / 26,96 / 4,76
lá e 46,18 / 21,99 / 26,84 / 4,73 aqui (mais 0,25 de "demais": TDA e securitizadas, que o Tesouro distribui pelo
indexador contratual); "vincendo em 12 meses" 17,87% lá contra 15,67% aqui, porque o Tesouro conta o fluxo de principal
e juros a valor presente e o mart só o principal dos títulos que vencem (a medida está nomeada assim); prazo médio 4,05
anos lá (DPF inteira, fluxos a valor presente) contra 47,04 meses na série 10618 do BC (dívida mobiliária interna).
Em 2025 os juros apropriados da dívida externa saem negativos (−16,35 bi): a linha inclui a variação do câmbio e o real
se valorizou.

### Como a população é acessada (S14, IBGE) e vira per capita

- **Duas réguas, nunca misturadas.** A Projeção da População, revisão 2024 (`ftp.ibge.gov.br`, planilha tab3 "grupos
  etários específicos": uma linha por ano e lugar, 2000 a 2070, com total, sexo e grupos de idade) é a régua do per
  capita: população em 1º de julho, numa série só. As estimativas oficiais que o IBGE publica para o TCU (SIDRA, tabela
  6579, variável 9324) saem dessa projeção de 2024 em diante e entram como conferência (`populacao_projecao_vs_estimativa`,
  tolerância zero): 212.583.750 (2024), 213.421.037 (2025) e 214.211.951 (2026) são iguais nas duas. A 6579 não tem
  2007, 2010, 2022 nem 2023, e de 2001 a 2021 vem da projeção antiga (1,1% a 1,5% acima da revisão 2024 em 2019 a
  2021). O Censo 2022 contou 203.080.756 pessoas em 1º de agosto de 2022; a projeção dá 210.862.983 para o mesmo ano
  (+3,83%), porque corrige a cobertura do Censo. Por isso 2023 só existe na projeção (211.695.158). O Censo é lido da
  tabela 4709 do SIDRA como `opcional`: se não vier, fica sem a nota.
- **Acesso de fora do Brasil (10/nov/2026, Buenos Aires).** `servicodados.ibge.gov.br` (o catálogo da API) não responde
  e `www.ibge.gov.br` devolve 403; `apisidra.ibge.gov.br` e `ftp.ibge.gov.br` respondem em menos de um segundo. O
  fetcher usa só esses dois, força IPv4 (a rota IPv6 fica pendurada até o timeout) e não depende de catálogo: baixa os
  arquivos inteiros e o lacre por hash do store decide se há snapshot novo. Se o IBGE não responder, o raw lacrado
  continua valendo; `run` segue sem per capita.
- **O que o per capita é e não é.** Valor do ano dividido pela população de 1º de julho do mesmo ano: uma média, não o
  que cada pessoa pagou. No recibo são dois memos (`por_habitante`: total do recibo, tributos e cada linha; `imposto_futuro`:
  variação da dívida consolidada líquida), com a ficha (arquivo, lacre, data de referência) e as notas da estimativa e do
  Censo. No mart da dívida é o grupo "por habitante": DPF em poder do público, dívida nova no ano, dívida líquida do
  Governo Federal (BC), dívida consolidada líquida (RREO) e juros pagos. Nenhum centavo do recibo muda; se a projeção e a
  estimativa divergirem, o per capita sai como indisponível com o motivo e os dois produtos continuam publicáveis.
  Números reais de 2024: R$ 14.045 de gasto do recibo por habitante, R$ 1.656 de juros pagos, R$ 34.415 de DPF em poder
  do público; em 2025 a DPF passa a R$ 40.460 por habitante (R$ 6.180 a mais no ano).

### Como o SIOP é acessado (S8)

- Endpoint SPARQL público (`https://www1.siop.planejamento.gov.br/sparql/`), um grafo por exercício
  (`http://orcamento.dados.gov.br/{ano}/`), vocabulário `http://vocab.e.gov.br/2013/09/loa#`. Modelo documentado pelo
  pacote oficial `orcamentobr` (Ministério do Planejamento; GPL-3, por isso este repositório usa só o modelo de dados,
  público, e nenhuma linha daquele código). Cada item orçamentário aponta para órgão, UO, função, subfunção, programa,
  ação, plano orçamentário, esfera, GND, modalidade, elemento, fonte, identificador de uso e resultado primário, e traz
  dotação inicial, dotação atual, empenhado, liquidado e pago (R$ inteiros). Não traz restos a pagar pagos.
- `fetch siop` lê primeiro a data da última atualização do grafo (`loa:dataUltimaAtualizacao`). Se não mudou desde o
  último snapshot e o raw está completo, o exercício inteiro é pulado. Quando mudou, baixa os membros (código e rótulo)
  de cada dimensão e as cinco agregações de `sources.yaml`: `fonte_gnd_subfuncao` e `rp_subfuncao` inteiras,
  `elemento_orgao`, `acao_subfuncao` e `acao_rp_subfuncao` fatiadas por função (uma consulta por função). Com
  `--agregacoes` só as pedidas são conferidas e baixadas (é como uma agregação nova entra sem refazer as outras). Rótulos ficam fora das
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
- v0.3.0 (chips reais): 10,3% da Saúde federal de 2025 foi emenda parlamentar (R$ 22,4 bi de R$ 217,1 bi); 61% da
  linha de Defesa é salário de ativo; pelo empenhado as emendas são R$ 47,1 bi (1,40 centavo), pelo liquidado
  R$ 31,7 bi (0,95). O resíduo cresceu 25,5% em 2025: precatórios de volta ao normal depois do vale de 2024 (dezembro
  de 2023 pagou R$ 147 bi em sentenças, 2024 R$ 54 bi, 2025 R$ 77 bi), empréstimos novos (Brasil Soberano, dívidas
  rurais) e Agricultura +99% (dois terços empréstimo, um terço equalização do Plano Safra).
- v0.4.0 (empréstimos fora do R$ 1,00): inversões financeiras com RP 0 somaram R$ 64,5 bi em 2023, R$ 106,9 bi em 2024
  e R$ 160,3 bi em 2025 (R$ 28,9 bi na linha Trabalho: FAT emprestado ao BNDES; R$ 5,6 bi em Educação: FIES; o resto no
  resíduo); em 2026 até agosto, R$ 199,5 bi pelo Anexo 01, mais do que 2025 inteiro. Com a exclusão, 2025 fica: INSS 31,
  transferências 14, juros 11, encargos e demais 9, assistência 9, educação 8, saúde 7, aposentados 6, trabalho 3,
  defesa 2; 2024: 31, 14, 12, 8, 10, 7, 7, 6, 3, 2; 2023: 31, 14, 9, 11, 10, 8, 6, 6, 3, 2. Investimento liquidado é
  54% do empenhado em 2025 (49% em 2024, 45% em 2023): quase metade vira restos a pagar. A conferência de apelidos
  achou as subfunções novas da assistência de 2025 (245 "Serviços Socioassistenciais", 246 "Segurança de Renda"): é a
  reclassificação do Bolsa Família que o RREO mostra como "FU08 - Demais".
- v0.5.0 (conta do Tesouro): o Anexo 06 publica a despesa primária oficial. Comparada grupo a grupo com o Anexo 01,
  pessoal e investimentos batem ao centavo em 2023 a 2026; outras despesas correntes diferem em R$ 0,45 bi (2025); e
  inversões financeiras diferem em R$ 128,4 bi (2025): é o que o Tesouro trata como financeiro. A lei orçamentária
  carimba R$ 160,3 bi como financeiros (RP 0), R$ 31,9 bi a mais do que o Tesouro; desses, R$ 27,6 bi são os repasses
  aos fundos constitucionais FNE, FNO e FCO (ações 0029, 0030, 0031 e 0534; R$ 24,8 bi em 2024, R$ 21,4 bi em 2023),
  que o Tesouro trata como repartição de receita, igual ao FPM; os R$ 4,3 bi restantes (R$ 2,4 bi em 2024, R$ 3,9 bi em
  2023) não têm uma ação que explique nos três anos. A própria LDO (art. 7º, §4º) diz que o carimbo serve para
  "auxiliar a apuração do resultado primário" pela metodologia do Tesouro, então o total do recibo passou a ser a conta
  do Tesouro: despesa primária liquidada (R$ 2.859,1 bi em 2025) mais juros pagos (R$ 363,5 bi) = R$ 3.222,6 bi
  (v0.4.x: R$ 3.191,1 bi). Em ano aberto o Anexo 06 do bimestre dá o total exato: acabou a aproximação. Tributos do
  Anexo 06 batem ao centavo com o Anexo 01 (R$ 2.634,4 bi em 2025); receita primária R$ 2.940,0 bi; resultado primário
  acima da linha -R$ 61,7 bi (-R$ 22,3 bi antes do ajuste metodológico de -R$ 39,4 bi), abaixo da linha -R$ 22,3 bi;
  juros por competência R$ 885 bi líquidos contra R$ 363 bi pagos; dívida consolidada líquida de R$ 7.177 bi para
  R$ 8.023 bi; investimento pago no ano com restos a pagar R$ 69,9 bi contra R$ 38,6 bi liquidados.
- v0.5.0, rodada real (9/nov, 16h): os quatro anos publicáveis, todas as críticas OK; identidade do total, fórmula do
  resultado primário e tributos iguais nos dois anexos ao centavo (2023 com R$ 2,26 e R$ 3,47 de arredondamento em
  dezenas, dentro do piso). Centavos (INSS, aposentados, assistência, saúde, educação, trabalho, defesa,
  transferências, juros, demais): 2025 31/5/9/7/8/3/2/14/11/10, total R$ 3.222,6 bi, R$ 128,8 bi fora (R$ 28,9 bi de
  Trabalho, R$ 5,6 bi de Educação, R$ 0,17 bi de Defesa, R$ 94,1 bi do resíduo); 2024 31/6/9/7/7/3/2/14/12/9,
  R$ 2.985,7 bi, R$ 79,7 bi fora; 2023 31/6/10/6/7/3/2/14/9/12, R$ 2.733,8 bi, R$ 39,5 bi fora; 2026 até agosto
  29/3/8/6/8/3/3/13/17/10, R$ 2.497,1 bi, R$ 157,2 bi fora, exatos. Em cada ano um centavo saiu de uma linha com nome
  para a décima linha, que agora carrega os fundos constitucionais (lei × Tesouro: R$ 31,9 bi, 27,3 e 25,3; fundos
  R$ 27,6 bi, 24,8 e 21,4). A variação anual do resíduo em 2025 caiu de +25,5% para +12,7%. Gasto por R$ 1,00 de
  tributo: R$ 1,31 (2023), R$ 1,24 (2024), R$ 1,22 (2025). De cada R$ 1,00 gasto: tributos 76/81/82 centavos, outras
  receitas 10/9/9, dívida ou caixa 14/10/9. Juros pagos sobre juros gerados: 43%, 52%, 41%. Dívida líquida nova:
  R$ 907,9 bi, 1.076,0, 845,7 e 801,9 (2026 até agosto), igual ao resultado nominal abaixo da linha com o sinal
  trocado nos quatro anos (conferência nova na v0.5.1). Em 2024 e 2025 o resultado primário abaixo da linha é igual
  ao centavo à conta de caixa antes do ajuste metodológico; o ajuste, pela nota 1 do Anexo 06 do RREO da União, é a
  diferença para o Relatório do Tesouro Nacional (RTN), que segue o Manual de Estatísticas de Finanças Públicas
  (MEFP). 2026 traz um empréstimo novo de R$ 30 bi (ação 00YA, motoristas de aplicativo); o SIOP já carimba R$ 212,4
  bi como RP 0 até 7/nov, e o Tesouro até agosto trata R$ 157,1 bi como financeiros.

## Metodologia (v0.5.1, resumo)

- **O total do recibo é a conta oficial do Tesouro**: despesa primária liquidada do Anexo 06 (salários, aposentadorias,
  benefícios, custeio, obras, repasses) mais os juros e encargos da dívida pagos (Anexo 01). Fica fora o que não é
  gasto e sim troca de ativo: rolagem, amortização e o que o Tesouro classifica como financeiro (emprestar, comprar
  título, integralizar capital com retorno). O mart recusa rodar sem o Anexo 06 do período.
- Métrica das linhas: despesas liquidadas até o bimestre, exceto intraorçamentárias (no 6º bimestre, o exercício inteiro).
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
- **Chips** (`chips` em `linhas.yaml` e no export): medidas transversais, calculadas no SIOP em ano fechado, no mesmo
  universo do RREO, com o filtro do chip E o filtro de cada linha (o resíduo recebe o que sobra, fora 841-844). O mesmo
  real já está em alguma linha; o chip diz quanto daquela linha é isso. Três: **emendas** (RP 6, 7, 8 e 9),
  **investimento** (GND 4, liquidado; o COFOG trará os restos a pagar) e **folha de ativos** (GND 1 menos elementos 01 e
  03). GND 4 e GND 1 totais são conferidos contra o Anexo 01 (`siop_investimentos_vs_anexo01`, `siop_pessoal_vs_anexo01`).
  Cada chip sai com os quatro estágios (dotação atual, empenhado, liquidado, pago) e o empenhado por linha, porque em
  emendas e investimento a diferença entre empenhado e liquidado (restos a pagar) é o tamanho da história: em 2025 as
  emendas empenhadas foram R$ 47,1 bi e as liquidadas R$ 31,7 bi; individuais liquidam 83% no ano, bancada 55%,
  comissão 48%; Saúde 87%, Urbanismo 2%. Em ano em andamento os chips saem sem valor, com nota.
- FUNDEB e salário-educação (subfunção 847) já estão dentro de `FU12 - Demais Subfunções`, ou seja, em Educação.
- Juros vêm do Anexo 01 (`JurosEEncargosDaDivida`). As subfunções 843/844 (serviço da dívida) saem do Anexo 02
  porque misturam juros e amortização; a diferença é publicada como amortização, fora do R$ 1,00, e conferida
  contra `AmortizacaoDaDivida`.
- Refinanciamento fica fora do R$ 1,00; o valor exibido vem do Anexo 01 (`AmortizacaoRefinanciamentoDaDivida`).
- **Empréstimos e aplicações financeiras do Tesouro ficam fora do R$ 1,00** (`excluidos.emprestimos_financeiros`,
  `calculo: tesouro`): dinheiro que saiu do caixa e virou um direito de receber de volta (empréstimo, título, capital
  com retorno), como pagar o principal da dívida. O tamanho é a conta do Tesouro, por diferença: despesa exceto intra
  (Anexo 02) − amortização (Anexo 01) − despesa primária liquidada (Anexo 06) − juros (Anexo 01); em 2025, R$ 128,8 bi.
  Por grupo de despesa, é Anexo 01 menos a parte primária do Anexo 06 (`por_grupo`: inversões R$ 128,4 bi e outras
  despesas correntes R$ 0,45 bi em 2025; pessoal e investimentos zero), e a soma por grupo tem que fechar com a
  diferença (crítica `exclusao_financeira_por_grupo`). Por linha, em ano fechado, vale o carimbo da lei orçamentária no
  SIOP (GND 5 com RP 0, `por_linha`): o FAT emprestado ao BNDES sai de Trabalho, o FIES sai de Educação (ações nomeadas
  na nota da linha) e o resto sai do resíduo; o que sai das linhas tem que caber no total do Tesouro (crítica
  `exclusao_por_linha`). Em ano em andamento, ou sem SIOP, o total continua exato (Anexo 06) e tudo sai do resíduo,
  com avisos nas linhas de Trabalho e Educação. **Lei × Tesouro**: a lei carimba R$ 160,3 bi como financeiros em 2025,
  o Tesouro trata R$ 128,4 bi; a diferença (R$ 31,9 bi: R$ 27,6 bi de fundos constitucionais FNE/FNO/FCO, que são 3% do
  IR e do IPI entregues por mandamento constitucional e não voltam ao Tesouro, e R$ 4,3 bi sem ação identificada) fica
  dentro do R$ 1,00, no resíduo, nomeada no hover do resíduo e em `fora_do_real.emprestimos_financeiros.lei_vs_tesouro`
  (informativa `lei_vs_tesouro_inversoes`, que só acusa se o Tesouro passar a tratar como financeiro mais do que a lei
  carimba). O export traz também o GND 5 por RP e a lista das maiores ações pelo carimbo da lei (agregação
  `acao_rp_subfuncao`), com a nota de que ela soma mais do que o valor fora do R$ 1,00.
- Residual ("encargos especiais e demais funções") = total exceto intra − linhas atribuídas − exclusões − ajuste −
  empréstimos não atribuídos a linha nomeada.
- **Ajuste declarado** (`ajustes.servico_divida_fora_843_844` em `linhas.yaml`): juros + amortização do Anexo 01
  excedem as subfunções 843/844 (R$ 40,8 bi em 2024, R$ 124,0 bi em 2025: ação 0669, cobertura do Banco Central).
  Essa parcela, classificada na 846, é removida do residual; com isso vale a identidade soma das linhas = despesa
  primária do Anexo 06 + juros do Anexo 01 (crítica `identidade_total`).
- **Memos** (`memos` em `linhas.yaml`, `memos` no export): números fora do R$ 1,00, cada um com valor, componentes,
  notas e as chaves do Anexo 06 / Anexo 01 usadas. Tributos (impostos, taxas e contribuições: Anexo 06, conferido ao
  centavo com o Anexo 01, crítica `tributos_anexo01_vs_anexo06`); receita primária total; o que entrou além dela
  (títulos vendidos além da rolagem, rendimento do caixa no Banco Central, resultado do BC, empréstimos devolvidos,
  outras financeiras por diferença; a rolagem ao lado, fora do total); gasto por R$ 1,00 de tributo (total do recibo /
  tributos); de onde veio cada R$ 1,00 gasto (centavos de tributos, outras receitas e dívida ou caixa, maior resto);
  resultado primário (receita − pagas − restos pagos, o ajuste metodológico do Tesouro quando existe (pela nota 1 do
  anexo, a diferença para o RTN, que segue o MEFP), acima da linha (IX) e abaixo da linha (XXVII), com a fórmula
  conferida ao centavo: crítica `resultado_primario_acima_da_linha`);
  juros por competência (XI − X) ao lado dos pagos; resultado nominal (XII, XVII, XXVI e os ajustes); dívida consolidada
  e líquida no início e no fim do período; restos a pagar pagos no ano, com o investimento pago somando os restos;
  empréstimos que voltaram (`AmortizacoesDeEmprestimos`). Dívida nova por habitante continua planejada (IBGE). Um memo
  marcado como implementado sem cálculo no código derruba o mart (`ConfigInvalida`).
- Reconciliações críticas: soma das subfunções = soma das funções; soma das funções = total do cabeçalho (I); total
  do Anexo 02 = total do Anexo 01; prefixo `FUxx` consistente com a ordem; residual ≥ 0; centavos somam 100;
  identidade do total (despesa primária do Anexo 06 + juros); exclusão financeira fecha por grupo; exclusão por linha
  cabe no total; tributos iguais nos Anexos 01 e 06; hierarquias das receitas do Anexo 01 e das receitas e despesas do
  Anexo 06; fórmula do resultado primário acima da linha; apelidos de subfunção do CSV conferidos contra os rótulos do
  SIOP (`subfuncoes_ref_vs_siop`; em 9/nov pegou "Promoção da Produção Agropecuária" como 601 em vez de 608 e "Defesa
  Agropecuária" como 604 em vez de 609, sem efeito em centavos). Informativas: pessoal e investimentos do Anexo 06
  contra o Anexo 01; resultado nominal acima da linha, resultado primário abaixo da linha e dívida líquida pelas
  fórmulas do próprio anexo; resultado nominal abaixo da linha = dívida líquida nova com o sinal trocado; lei ×
  Tesouro nas inversões; serviço da dívida fora de 843/844 ≤ 2% do total (acima
  disso, mas com o valor nomeado ao real pela conferência do SIOP, sai como "explicada", não como falha); Previdência
  Básica a menos de 5% de `BeneficiosPrevidenciarios`; GND 5 do SIOP contra `InversoesFinanceiras` do Anexo 01, com o
  detalhe por RP; variação anual de cada linha contra o mesmo bimestre do exercício anterior dentro de ±15% (acima
  disso, aviso com as linhas que saltaram; investigue com `recibo diff`).
- O mart recusa rodar quando o staging do RREO não tem o período pedido (Anexo 02 na métrica ou Anexo 06), e diz o
  comando que falta (antes, rodava com zeros e imprimia linhas negativas antes do "não publicável").
- Cada linha do recibo sai com `anterior` (mesmo bimestre do exercício anterior) e `variacao_anual`, quando o
  staging tem o exercício anterior. É o que o site mostra no hover.
- Conferências cruzadas com o SIOP (só em ano fechado), calibradas em 2025 com diferença de centavos: liquidado por
  função (OFSS, sem intra, sem refinanciamento) contra o Anexo 02 (crítica); GND 2 contra juros do Anexo 01; GND 6
  contra amortização + refinanciamento; GND 6 com fonte 443 contra a linha XII; GND 2 e 6 fora de 841-844 contra o
  excesso que o RREO só estima por diferença (crítica), com o detalhe por ação: em 2024 e 2025 é a ação 0669,
  cobertura do resultado negativo do Banco Central (Lei 13.820/2019), nomeada no export em
  `fora_do_real.servico_divida_fora_843_844` e na nota da linha de juros; e elementos 01/03 dentro de 272/274 contra o
  RREO. Tolerância de 1 em 10 milhões porque o SIOP publica reais inteiros, com um piso absoluto igual ao das
  identidades (R$ 1 ou um bilionésimo do total): um esperado perto de zero não vira tolerância zero (o RREO de 2023 vem
  em dezenas de reais e a estimativa do serviço da dívida fora de 843/844 deu -R$ 2,26 contra 0,00 no SIOP; sem o piso,
  isso reprovava o ano). Em ano em andamento sai só o aviso `siop_comparavel`. O export traz tudo em `siop`.

Tudo isso está em `config/linhas.yaml`, que gera o SQL, a ficha e os testes.

### O que o site publica (v0.9.0 a v0.11.1)

- **Uma página por ano** (`docs/AAAA/`; a do ano fechado mais recente também é a `index.html`): a frase do ano, as
  palavras do desenho explicadas, o desenho de fluxo com duas visões, a ficha da faixa selecionada, três frases, a
  lista linha por linha (cada linha abre em gaveta e tem página própria), o que ficou fora do R$ 1,00, as conferências
  do ano e os lacres. **Visão "Tudo o que passou pelo caixa"**: entradas (receita; títulos vendidos, abertos em rolagem e
  dívida nova) e saídas (o gasto de verdade; dívida antiga paga, aberta por carteira; empréstimos feitos; o que ficou no
  caixa ou para o ano seguinte), pelo Anexo 01; a rolagem aparece dos dois lados. **Visão "O gasto de verdade"**: o R$ 1,00
  do recibo (tributos, outras receitas e dívida nova ou caixa de um lado; as dez linhas do outro). Clicar numa faixa
  entra nela: ela vira o R$ 1,00 e o outro lado vira rateio; a trilha e "Subir um nível" voltam.
- **Níveis de dentro de cada linha** (`linhas[].niveis` no export; `src/recibo/marts/niveis.py`): subfunções do RREO com os
  ajustes nomeados por subfunção (o que saiu para outra linha ou para fora do R$ 1,00, medido no SIOP); no resíduo,
  funções e subfunções, menos o que o SIOP mostra que saiu (aposentados, empréstimos pela lei, serviço da dívida fora de
  843/844) e mais o que a lei chama de empréstimo e o Tesouro conta como gasto; nas linhas do SIOP, elementos e órgãos;
  ações do SIOP dentro de cada subfunção no ano fechado (sem GND 5 com RP 0 e sem GND 2 e 6). Juros abrem pelas carteiras
  do mart da dívida (`execucao.por_carteira`: mercado, Banco Central, externa, contratos, cobertura do BC). O que não tem
  detalhe por subfunção (ano em andamento) fica em `nao_alocado`, com nome e valor; a conferência informativa
  `niveis_fecham` acusa qualquer linha em que as partes não somem a linha.
- **Carimbo da fonte no lugar do rateio (v0.9.1).** Na visão do caixa, dentro de uma faixa o outro lado nunca é rateio:
  é o carimbo da fonte de recursos na lei orçamentária, lido do SIOP (bloco `fontes_de_recursos` do export,
  `src/recibo/marts/fontes.py`): com que dinheiro cada despesa foi paga, por grupo de fonte (títulos de refinanciamento,
  códigos terminados em 443; títulos novos, 444; receitas carimbadas para a dívida, 1400/1401; receita), por origem do
  dinheiro (primeiro dígito: 1 e 2 no próprio ano, 3 e 6 de anos anteriores, o colchão) e por destino (rolagem, principal,
  juros, despesa primária, inversões). Dentro de "Títulos vendidos" aparece o que o dinheiro de título pagou no ano e
  quanto ficou no caixa para os anos seguintes; dentro de "O gasto de verdade" e "Dívida antiga paga", com que dinheiro
  foram pagos. Observado em 2025: 1443 + 3443 = R$ 1,42 tri, igual à rolagem do RREO; o Tesouro vendeu R$ 1,75 tri para
  a rolagem e gastou R$ 966 bi dela no ano, o resto ficou guardado. Conferência informativa `fontes_total_vs_anexo01`.
  No ano em andamento (sem SIOP comparável) o outro lado fica em branco, com o motivo, em vez de uma aproximação. A
  faixa "o que ficou" (entradas menos saídas liquidadas) sai tracejada, em grupo próprio, com a conta aberta na ficha:
  não é lucro, é o que o Tesouro levantou além do que o ano consumiu (restos a pagar e colchão).
- **Nada é cortado no desenho (v0.9.2).** Todas as partes de todos os níveis entram (ações por subfunção e órgãos por
  elemento sem limite: `siop.niveis_acoes_maximo: 0`). A altura do SVG é calculada a cada desenho: barra central, mais
  os espaços entre as faixas, mais o espaço exato que cada rótulo ocupa; nada começa acima do topo nem passa do fim. Os
  rótulos quebram em até duas linhas de nome e duas de números pelo que cabe de fato (texto medido, não contagem de
  letras) e só ganham "…" se nem assim couberem; o nome inteiro está no card e na ficha. Com 100 partes o desenho tem
  uns 4.000 px e a página rola: é o combinado, tudo aparece. Antes da 0.9.2 os espaços entre as faixas não entravam na
  conta e a segunda passada dos rótulos usava outro passo, e uma faixa com 40 partes vazava pelo topo e pelo fim.
- **Bandeira e Constituição (v0.9.2).** A Bandeira Nacional é desenhada pelo próprio site (`templates/_bandeira.html`,
  geometria da Lei 5.700/1971: 20 por 14 módulos, losango a 1,7, círculo de raio 3,5, faixa entre arcos de raio 8 e 8,5,
  lema, 27 estrelas em cinco tamanhos; posições das estrelas pelo desenho de domínio público do anexo da lei, conferidas
  com o traçado do flag-icons, MIT), sem imagem de fora. Fica no alto de Quem somos e no rodapé de todas as páginas, ao
  lado do art. 1º, parágrafo único, da Constituição. Em Quem somos, três artigos entram como citações com uma frase em
  linguagem comum cada (art. 1º, parágrafo único; art. 70, parágrafo único; art. 74, § 2º), no lugar do marcador
  `[CONSTITUICAO]` de `conteudo/quem-somos.md`; os textos estão em `conteudo/site.yaml` (`constituicao`).
- **Visão "A dívida" (v0.10.0).** Terceira aba da home, no mesmo desenho. No centro, a dívida da União fora do Banco
  Central no último dia do período (DPF em poder do público, pelo CSV título a título do Tesouro; conferida com a
  planilha do estoque). À esquerda, do que ela é feita: o índice que corrige cada título (Selic, prefixado, inflação,
  câmbio, demais; regra em `divida.estoque_csv.indexadores`), e dentro de cada índice as famílias de título (LFT, LTN,
  NTN-B..., nome sem o código de vencimento, com nome para leigo em `site.yaml`). À direita, quando vence: faixas pelo
  vencimento final (`divida.estoque_csv.faixas_vencimento`: até 12 meses, 1 a 2 anos, 2 a 5, mais de 5; sem data à
  parte), e dentro delas mês a mês (primeira faixa) ou ano a ano. Ao entrar numa faixa, o outro lado não é rateio: é o
  cruzamento título a título (dentro de um índice, quando ele vence; dentro de um prazo, do que ele é feito), porque
  cada título tem um índice e um vencimento. O mart exporta as células índice x família x faixa x período
  (`estoque_detalhe.celulas`, soma igual ao total do CSV ao centavo), a conta do ano pelos fatores do Tesouro
  (`conta_do_ano`: começou devendo, vendeu, pagou, juros apropriados, operações com o BC, terminou devendo; fecha com a
  conferência crítica `dpf_fatores_identidade`) e o estoque mês a mês (`estoque_mensal`). A tira da visão traz o total,
  o valor por habitante (a parte média de cada um) e a conta do ano; abaixo do desenho, a seção "A dívida, com todas
  as réguas": os juros por pergunta (as oito réguas), as outras medidas de estoque (carteira do BC, LRF, BC, % do PIB,
  Tesouro Direto), vencimento em 12 meses e prazo médio, o calendário mês a mês, as conferências e os lacres da dívida.
  Textos em `site.yaml` (`visoes.divida`), com as duas ressalvas escritas: "em poder do público" exclui o BC; a fatia
  que vence em 12 meses conta só o principal (o RMD conta cupons e sai maior).
- **Abaixo do desenho, tudo é da aba ativa (v0.10.1).** Cada visão tem o seu scorecard (três frases) e a sua tabela
  "linha por linha": no caixa, a visão inteira com a rolagem (o que entrou, o que saiu, o que ficou, cada um com as
  partes de dentro e os centavos de cada real que passou pelo caixa); no gasto, a lista das dez linhas e o quadro "Fora
  do R$ 1,00"; na dívida, índices e prazos e, abaixo, a seção das réguas. Sem JavaScript, tudo aparece de uma vez.
  Dentro de uma parte que o carimbo da fonte não separa (tributos, cada imposto, outras receitas), o outro lado do
  zoom mostra o do conjunto de onde ela veio, tracejado e dito como tal (`outro_lado_de` no JSON do desenho): o SIOP
  diz com que fonte cada despesa foi paga, e os tributos entram juntos na fonte "recursos livres". Na seção da dívida,
  uma medida indisponível mostra "indisponível" e o motivo em linguagem comum (`motivo_leigo`), sem comando de terminal
  na página. No mart da dívida, quando o arquivo de execução da DPF no CKAN para antes do fim do bimestre do RREO (2026:
  publicado em agosto com dados até julho), as conferências contra ele saem como "não comparável" (`calculada: false`),
  com os dois meses escritos, em vez de "falhou"; `execucao.ultimo_mes_com_dados` registra até onde o arquivo vai.
- **Reserva de liquidez, tracejado explicado, faixas mínimas, perguntas abertas e domínio (v0.10.2).** Dentro de
  "Títulos vendidos", a sobra passa a se chamar "Reserva de liquidez (não usado no ano)" e traz a conta inteira no card e
  na ficha: o que entrou de título no ano (RREO), o que foi pago com título do ano (carimbo, SIOP), a sobra, o que o
  Tesouro usou de títulos de anos anteriores e o saldo (textos em `site.yaml`, `nos.reserva_liquidez`, com os números do
  ano preenchidos). Toda faixa tracejada diz por quê (`tracejado_motivo`) no card, na ficha e numa legenda abaixo do
  desenho; faixas com menos de 5 px são desenhadas com 5 px (as maiores cedem o espaço; o valor escrito é o verdadeiro).
  A aba da dívida abre com três números grandes (por habitante, quanto cresceu, quanto vence em 12 meses). Página
  `perguntas-abertas/` (`conteudo/perguntas.yaml`): as pontas soltas, cada uma com contexto, o que o site mediu (números
  lidos dos exports do último ano fechado, nunca digitados), a pergunta pronta para a LAI e o status (aberta, enviada,
  respondida); a conferência informativa `titulos_rreo_vs_emissoes_dpf` (títulos vendidos pelo RREO contra emissões da
  DPF) registra a diferença todo ano. Metodologia ganhou "Limitações e próximos passos". Tubulação do domínio: com
  `dominio` e `url` em `site.yaml`, o gerador escreve `docs/CNAME` (obrigatório: `--limpar` apaga a pasta e o GitHub
  Pages guarda o domínio nesse arquivo), `robots.txt`, `sitemap.xml`, `404.html` e as tags canonical, Open Graph e
  Twitter em toda página, com `static/social.png` (1200x630) como imagem da prévia do link.
- **O carimbo termina, mas aponta o caminho; perguntas expandíveis com protocolo (v0.10.3).** As faixas do outro lado
  de um zoom (carimbo da fonte no caixa, cruzamento título a título na dívida) nunca abriram, porque o SIOP diz com que
  fonte cada despesa foi paga e só; agora o card diz isso na hora e a ficha ganha o botão "Ir para X", que leva à faixa
  de verdade correspondente (o gasto, a linha de juros, a dívida paga, a receita, os títulos; na dívida, a faixa de
  prazo ou o índice inteiro), inteira, e entra nela quando ela abre (`ir_para` em cada nó de carimbo, em `dados.py`;
  `irParaNo` em `fluxo.js`). Em `perguntas-abertas/`, cada pergunta é um bloco que abre e fecha (`<details>`), com o
  status no resumo; o status sai dos campos de `conteudo/perguntas.yaml` (sem `protocolo` = aberta; com `protocolo` =
  enviada; com `resposta` ou `respondida_em` = respondida), e a página traz protocolo, datas, a resposta e o que mudou no
  site. O rodapé da página convida quem tem outra dúvida a perguntar ao órgão pela LAI (Fala.BR) e mandar a resposta
  para entrar no site. "Quem somos" passa a pedir também bugs (página que não abre, botão que não funciona, desenho
  que vaza), com o que mandar junto.
- **Celular, cobertura por ano, aviso no lugar do branco, marca azul e amarela (v0.11.0).** Em tela estreita (até
  900 px: celular, tablet em pé) o desenho vira duas listas de barras, "De onde veio" em cima e "Para onde foi"
  embaixo, cada faixa uma linha com o rótulo e uma barra proporcional à parte dela no lado; sem as fitas (a ligação é
  o total, no alto), 1 unidade do SVG = 1 px, por isso o texto fica legível. A mesma máquina de estados (zoom, trilha,
  ficha, "Ir para") roda nos dois arranjos; `fluxo.js` troca de arranjo no mesmo ponto de corte do CSS e redesenha ao
  girar o aparelho. Sem mouse, o card não existe: tocar numa faixa entra nela (ou abre a ficha, que rola para a tela) e
  os textos dizem "toque". Tabelas, lista linha por linha (nome numa linha, valor embaixo, centavos à direita), contato,
  fontes e lacres cabem em 390 px sem rolagem lateral (conferido com Playwright, iPhone 14 e 768 px). Abaixo do título
  de cada ano, uma linha de cobertura montada dos dados diz o que o ano tem e o que falta ("Ano fechado: RREO de
  janeiro a dezembro (6º bimestre); SIOP do ano inteiro...; dívida até dezembro" ou "Ano em andamento: RREO de janeiro
  a agosto (4º bimestre); dívida até julho. Sem detalhe por ação nem carimbo da fonte, que só entram com o ano fechado;
  ... Os números mudam a cada bimestre."); o `recibo site` imprime a mesma linha por ano. No ano em andamento, ao entrar
  numa faixa do caixa, o lado que ficava em branco (sem carimbo da fonte) ganha uma faixa de aviso cinza tracejada, sem
  números e sem clique, "Sem carimbo da fonte até o ano fechar", com o motivo; o teste `test_ano_em_andamento_tratado_igual_em_todas_as_visoes`
  deriva um 2026 em andamento do export fechado e confere que nenhum lado fica em branco sem aviso, em todas as visões.
  As duas linhas do cifrão da marca passam a azul e amarelo da bandeira (cabeçalho, favicon e imagem de prévia de
  link). A dica do fim do `recibo site` e o README usam `uv run python -m http.server` (no macOS não existe `python`).
- **Celular, segunda passada; repositório pronto para receber gente (v0.11.1).** Em tela estreita, uma linha acima do
  desenho diz que a lista é a versão de bolso e que no computador ele é um diagrama de fluxo, com o botão "Ver o diagrama
  aqui" (o arranjo largo dentro da área que rola para o lado, com zoom de pinça; "Voltar à lista" desfaz). Tabelas de
  três colunas ou mais (fontes, arquivos por ano, lacres, ano a ano da linha) viram cartões em telas até 600 px: uma
  linha da tabela por bloco, cada célula numa linha própria precedida do nome da coluna (`data-rotulo` nos templates,
  classe `cartoes`); nada quebra no meio da palavra nem rola para o lado. No repositório: este README começa pelo porquê
  e pelo link do site, `LICENSE` (MIT) e três modelos de issue em `.github/ISSUE_TEMPLATE/` (número que não bate, bug no
  site, discordância de metodologia), para onde o botão "Abrir uma issue" do site passa a apontar
  (`url_issue_nova` em `site.yaml`).
- **Formatação uma vez só.** `src/recibo/site/dados.py` transforma o export no modelo das páginas com todas as strings
  prontas ("R$ 990,2 bi", "31 centavos", "30,7%", "R$ 4.640 por habitante", em pt-BR); o HTML (Jinja2) e o `fluxo.js`
  só colocam as strings no lugar. Dentro de uma faixa, as porcentagens e os centavos são de cada R$ 1,00 daquela faixa
  (`pct_pai`, `cent_pai`); no lado do rateio, os do todo.
- **Sem nada de fora**: um CSS e um JS próprios, fontes do sistema, sem rastreamento; funciona aberto do disco. A
  página do ano funciona sem JavaScript (a lista e as páginas das linhas têm todos os números); o desenho precisa dele.
- **Páginas fixas**: `metodologia/` (texto de `conteudo/metodologia.md` mais "confira você mesmo", a lista das conferências
  do ano, as fontes e o glossário), `quem-somos/` (texto de `conteudo/quem-somos.md` mais contato), `dados/` (download dos
  exports e de um CSV por ano, lacres, fontes). Os textos de cada nó e de cada linha (nome curto, explicação para
  leigo, o que não está nela, o que a página vai ter) estão em `conteudo/site.yaml`.

## Operação

- **V1 roda à mão**, de propósito: `uv run recibo run --exercicios 2024 2025 2026` quando sair RREO novo (a cada
  bimestre) ou quando o SIOP mudar (o fetcher só refaz um exercício se a data de atualização do grafo mudou), depois
  `uv run recibo site --limpar` e o commit de `docs/`. Cada etapa é lida antes de publicar; a automação (GitHub Actions
  semanal, dados commitados em `data/`) fica para quando o processo estiver estável. Os arquivos do Cloud Run (`infra/`)
  foram removidos: não fazem parte da V1.
- `RECIBO_STORE=gcs` grava o raw num bucket com versionamento, se alguém preferir rodar fora da máquina local.

## Licença

MIT (ver `LICENSE`).
