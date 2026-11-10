# recibo

Pipeline aberto de dados fiscais. Pergunta que responde: **de cada R$ 1,00 de imposto, quantos centavos foram para onde?**

Fase 1: Recibo do Contribuinte (União). Fontes implementadas: SICONFI (RREO da União, S4), SIOP (execução do
orçamento federal por classificador, S8) e COFOG (despesa do Governo Central na classificação internacional de
funções, S7; produto separado do recibo, `mart cofog`, desde a v0.6.3), Dívida Pública Federal do Tesouro (S1) e séries do
SGS do Banco Central (S16; as duas com raw, staging e inspect na v0.7.0, o `mart divida` vem na v0.7.1). As demais fontes estão catalogadas em
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
 fetch bcb     ──> raw/bcb     ──> transform bcb     ──> staging/stg_bcb_sgs.parquet     ──┴─> mart divida (v0.7.1)
```

| Camada | Onde | O que |
| --- | --- | --- |
| raw | `data/raw/{fonte}/{dataset}/{chave}/{snapshot}/` | resposta da API como veio (`pages.jsonl` no SICONFI, `result.json` no SIOP, o CSV do ano e o `package_show.json` do CKAN no COFOG) + `manifest.json` (url ou consulta SPARQL, sha256, timestamp). `_checks.jsonl` registra toda verificação, mudou ou não. |
| staging | `data/staging/stg_siconfi_rreo.parquet` | todas as colunas da API + derivações (`escopo`, `nivel`, `funcao_cod`, `subfuncao_cod`) |
| staging | `data/staging/stg_siop_execucao.parquet` | agregações do SIOP por códigos (esfera, modalidade, função, subfunção, GND, elemento, fonte, RP, ação) com rótulos e flags (`intra`, `ofss`, `refinanciamento`) |
| raw | `data/raw/divida/{dataset}/{arquivo}/{snapshot}/` e `data/raw/bcb/sgs/{codigo}/{snapshot}/` | Tesouro: `package_show.json` de cada dataset e o CSV/XLSX como veio; BCB: o JSON da série e, em `sgs_meta`, a ficha da série no portal de dados abertos |
| staging | `data/staging/stg_divida__{dataset}__{arquivo}.parquet` + `stg_divida_arquivos.parquet` | um parquet por arquivo do Tesouro: CSV com as colunas originais como texto mais `{coluna}__num` e `{coluna}__data` quando a leitura é segura; XLSX como grade (planilha, linha, coluna, valor). O índice lista arquivos, procedência e resumo |
| staging | `data/staging/stg_bcb_sgs.parquet` | um ponto por linha: código, data, valor, ficha de sources.yaml (nome, tipo, unidade), título no BC, procedência |
| staging | `data/staging/stg_cofog.parquet` | uma linha por linha do CSV do Tesouro (ano, natureza detalhada, ação, UO, classificação econômica GFS, código COFOG, valor) + derivações (`cofog_divisao/grupo/classe`, `categoria`, `gnd`, `modalidade`, `elemento`, `gfs2`, `ajuste`) + unidade (`valor` em reais, `valor_fonte` como veio, `unidade_fonte`, `fator_unidade`, `resolucao_reais`) |
| marts | `data/marts/recibo_{ano}_p{bimestre}.json` e `.csv` | linhas do recibo, shares, centavos, exclusões, memos (receitas, resultado, dívida), reconciliações |
| marts | `data/marts/cofog_{ano}.json` e `.csv` | régua COFOG (dez divisões e 69 grupos), ajustes do Tesouro, ponte com o recibo, reconciliações, glossário |
| export | `data/export/recibo_{ano}.json` e `data/export/cofog_{ano}.json` | o que o site consome (recibo; aba separada do COFOG) |

Idempotência: rodar de novo sem mudança na fonte não cria snapshot (só registra a verificação). Mudança em
período já publicado gera snapshot novo com `previous_snapshot_id`, e um aviso `REVISAO NA FONTE` no log.

## Rodando localmente

Pré-requisitos: Python 3.11+ e [uv](https://docs.astral.sh/uv/).

```bash
uv sync --extra dev
uv run pytest                      # 54 testes sobre um RREO (Anexos 01, 02 e 06), um SIOP, um COFOG, arquivos da dívida e séries do BC sintéticos

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

# 9) dívida (S1, CKAN do Tesouro) e BCB (S16, séries do SGS): só acesso, lacre e leitura na v0.7.0; o mart vem depois de ver os arquivos
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

# tudo de uma vez (fetch e transform de SICONFI e SIOP, depois os recibos); --sem-siop pula o SIOP; o COFOG é à parte
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
  relatórios formatados. O layout de cada arquivo será fixado em config na v0.7.1, depois de visto pelo `inspect divida`.
  Arquivo ilegível fica registrado no índice com o erro e não derruba os outros.
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
  (com o número de meses publicados), saldos e taxas no último valor do ano. A hipótese a conferir na primeira rodada:
  a soma de 2024 de uma das séries de juros nominais (4607 ou 4606) é igual aos R$ 937,68 bi da linha JUROS do COFOG.

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

## Operação

- **V1 roda à mão**, de propósito: `uv run recibo run --exercicios 2024 2025 2026` quando sair RREO novo (a cada
  bimestre) ou quando o SIOP mudar (o fetcher só refaz um exercício se a data de atualização do grafo mudou). Cada
  etapa é lida antes de publicar; a automação (GitHub Actions semanal, dados commitados em `data/`) fica para quando o
  processo estiver estável. Os arquivos do Cloud Run (`infra/`) foram removidos: não fazem parte da V1.
- `RECIBO_STORE=gcs` grava o raw num bucket com versionamento, se alguém preferir rodar fora da máquina local.

## Licença

MIT (ver `LICENSE`).
