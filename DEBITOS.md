# Débitos para a V2

O que a versão 1.0.0 do [cadareal.org](https://cadareal.org/) ainda não faz, dito com todas as letras, para quem for
conferir saber onde o projeto está. Cada item diz o que o site mostra hoje, por que ainda é assim e o que a V2 precisa
fazer. Nenhum item muda um número publicado: onde o dado não chega, o site diz que não chega.

Os números citados são os do último ano fechado (2025) no momento em que este arquivo foi escrito; eles mudam quando
os relatórios são republicados. As diferenças entre relatórios oficiais que ainda não sabemos explicar estão em
[Perguntas abertas](https://cadareal.org/perguntas-abertas/), com os pedidos feitos pela Lei de Acesso à Informação.

Para discutir um item, abra uma issue com o número dele no título
([modelos](https://github.com/cadareal/pipeline/issues/new/choose)).

## Dentro de cada linha

**D1. Ações do SIOP dentro de subfunções de onde saiu dinheiro por elemento de despesa.** A linha "Aposentados e
pensionistas do setor público" é medida no SIOP pelos elementos de despesa 01 (aposentadorias e reformas) e 03
(pensões), e esse dinheiro sai das subfunções onde estava (pensões militares saem de "Administração Geral" da Defesa;
aposentadorias civis saem de "Previdência do Regime Estatutário", no resíduo). A agregação do SIOP que lista as ações
(`acao_rp_subfuncao`: função, subfunção, grupo de despesa, resultado primário e ação) não traz o elemento, então não dá
para dizer de qual ação saiu o quê, e as ações apareceriam inteiras, com dinheiro que o site já mostra em outra faixa.
Hoje: quando o que saiu passa de um quarto do que as ações somam (`siop.niveis_acoes_saida_maxima` em
`config/linhas.yaml`), a subfunção não abre por ação e a ficha diz os dois números (2025: "Previdência do Regime
Estatutário" no resíduo, R$ 103,5 bi de R$ 104,5 bi fora; "Administração Geral" da Defesa, R$ 34,3 bi de R$ 71,0 bi).
Abaixo disso, as ações aparecem inteiras e a ficha diz quanto está em outra faixa (2025: "Transferências", R$ 10,4 bi de
R$ 469,9 bi; "Outros Encargos Especiais", R$ 30,4 bi de R$ 144,2 bi). V2: buscar no SIOP uma agregação ação x elemento
(dimensões esfera, modalidade, função, subfunção, grupo de despesa, elemento de despesa, resultado primário e ação, em
`config/sources.yaml`, fonte S8), aplicar nela o mesmo filtro negado da linha dos aposentados e abrir todas as
subfunções sem ressalva; `_acoes` em `src/recibo/marts/niveis.py` passa a ler essa agregação quando ela existir no
staging. Atenção ao corte de 10 mil linhas por consulta do endpoint SPARQL (o fetcher avisa quando bate nele): se uma
função passar disso, fatiar também por grupo de despesa.

**D2. As ações do SIOP e as subfunções do RREO não fecham ao centavo.** Os dois sistemas fecham por função, não por
subfunção. Hoje: a ficha diz os dois totais e o desenho reparte a faixa na proporção das ações. V2: investigar as
diferenças por subfunção (restos a pagar, reclassificações) e publicar a lista das maiores.

**D3. Linha de juros aberta pelo mart da dívida, não pelo SIOP.** A linha "Juros da dívida" abre pelas carteiras da
execução da DPF (mercado, Banco Central, externa, contratos, cobertura do BC). V2: cruzar com as ações do SIOP (0455,
0669 e outras) e com a abertura por título.

## Carimbo da fonte e ano em andamento

**D4. Carimbo da fonte só no ano fechado.** Na visão "Tudo o que passou pelo caixa", o outro lado de cada faixa é o
carimbo da fonte de recursos lido do SIOP, que só é comparável ao RREO com o ano fechado. Hoje: no ano em andamento, a
faixa de aviso "Sem carimbo da fonte até o ano fechar" aparece no lugar do rateio, e não há ações por subfunção. V2:
avaliar o SIOP acumulado até o mesmo mês do bimestre do RREO, com a diferença de data escrita.

**D5. O carimbo da fonte não separa os tributos.** Imposto de renda, IPI, COFINS e os demais entram juntos na fonte
"recursos livres". Hoje: dentro de "Tributos" e de cada imposto, o outro lado mostra o destino de toda a receita,
tracejado e dito como tal. V2: ligar as contribuições com carimbo próprio ao que elas pagam (a previdenciária ao INSS, o
salário-educação à educação), com a lista de fontes do SIOP conferida.

**D6. 2026 em andamento.** RREO até o 4º bimestre (janeiro a agosto), SIOP não comparável, execução da dívida até o
mês que o arquivo do Tesouro alcança (conferências contra ele saem como "não comparável", com os dois meses escritos).
Hoje: a linha de cobertura abaixo do título do ano diz exatamente o que o ano tem. V2: nada a corrigir; a cada
bimestre o programa roda de novo. Falta automatizar (D16).

## O que ainda não entrou

**D7. Isenções fiscais (gasto tributário).** Dinheiro que a lei dispensa de cobrar não passa pelo caixa, então não está
em nenhuma das três visões. Hoje: não aparece. Há três fontes, com naturezas diferentes: o Demonstrativo de Gastos
Tributários da Receita Federal que acompanha o projeto de lei orçamentária (previsão: R$ 620,8 bi para 2026, 4,53% do
PIB; R$ 543,6 bi para 2025), as Bases Efetivas da Receita Federal (o que de fato deixou de ser cobrado, publicadas com
cerca de três anos de atraso: 2021, R$ 420,8 bi, é o último ano efetivo; 2022 a 2024 ainda são projeções sobre ele) e a
DIRBI (declaração de benefícios fiscais das empresas, Instrução Normativa RFB 2.198/2024, desde 2024; não cobre o
Simples nem pessoas físicas). V2: uma página "O que a lei dispensou de cobrar", com previsão e efetivo nunca somados nem
comparados sem dizer qual é qual, lacre dos arquivos (planilha de 25 quadros nos Dados Abertos da Receita) e as réguas
nomeadas. Entrega em três passos: planilha do DGT por função e tributo; bases efetivas quando o ano existir; DIRBI por
benefício.

**D8. Estados e municípios.** A União é a fase 1. O RREO dos estados e municípios está no mesmo SICONFI e a estrutura
do site prevê as três esferas. V2: começar pelos estados, com as mesmas regras e as mesmas conferências.

**D9. Pedidos recorrentes que o site não responde.** Servidores que mais recebem, emendas parlamentares por autor,
Bolsa Família e BPC por estado. As fontes estão catalogadas (`config/sources.yaml`, S9 Portal da Transparência,
planejado). V2: depois de D7 e D8, uma de cada vez, com fonte, lacre e conferência.

## Dívida

**D10. Famílias de título pelo nome do arquivo.** A família é o nome do título sem o código de vencimento. Os
contratos externos saem um a um (2025: 47 famílias dentro de "Câmbio", 16 do BID e 12 do BIRD entre elas) e a carteira
"Demais" tem 364 famílias (TDA e CVS, uma por série). Hoje: todas aparecem, com nome do arquivo. V2: agrupar por
organismo ou tipo (BID, BIRD, NDB, KfW, bônus globais, TDA, CVS), com a regra em `config/linhas.yaml` e a lista
completa na ficha. (Na 1.0.0 a grafia da mesma família em maiúsculas e minúsculas, "NTN-I" e "ntn-i", foi unificada.)

**D11. Quem são os credores e o tamanho da reserva de liquidez.** O Tesouro publica os detentores da dívida e o colchão
de liquidez só no Relatório Mensal da Dívida, em PDF. Hoje: o site não mostra e diz que não mostra. V2: ler a planilha
quando houver uma em formato aberto; sem arquivo aberto, continua fora.

**D12. Carteira do Banco Central fora da visão "A dívida".** A visão mostra a DPF em poder do público (fora do BC). A
carteira do BC aparece à parte, na seção das réguas. V2: uma segunda régua no mesmo desenho, dita como tal.

**D13. A fatia que vence em 12 meses conta só o principal.** O Relatório Mensal da Dívida conta também os cupons de
juros e sai maior. Hoje: a diferença está escrita na ficha e é uma das perguntas abertas (protocolo 18800174015202609).
V2: quando a resposta chegar, conferir as duas contas lado a lado.

## Régua e comparações

**D14. Valores nominais entre anos.** A tabela "Ano a ano" de cada linha compara reais de cada ano, sem correção pela
inflação. Hoje: a tabela diz isso em uma linha. V2: uma segunda coluna corrigida pelo IPCA do IBGE (série do SIDRA,
lacrada), nunca no lugar da nominal.

**D15. População: estimativa do IBGE, média aritmética.** "Por habitante" divide pela projeção da população do IBGE
(revisão 2024, 1º de julho de cada ano: 213.421.037 em 2025), conferida com a estimativa oficial do SIDRA. É a parte
média de cada pessoa, não o que cada pessoa pagou ou recebeu. Hoje: a data e a régua estão na ficha. V2: nada a
corrigir; quando o IBGE publicar revisão nova, o número muda e a conferência acusa.

## Operação e repositório

**D16. Tudo roda à mão.** `recibo run`, `recibo site` e o commit de `docs/` são feitos por uma pessoa, de propósito,
enquanto o processo assenta. V2: GitHub Actions semanal, com os dados commitados em `data/` e a mesma regra: falhou,
não publica.

**D17. Cartões e Reel fora do repositório.** As imagens para redes sociais e o vídeo do diagrama são gerados por scripts
que leem os exports do site (nenhum número digitado), mas os scripts ainda não estão no pacote. V2: comandos `recibo
cartoes` e `recibo reel`, com os mesmos testes do site (um cartão só sai se os dois números vierem do export).

**D18. Celular: diagrama sob demanda.** Em tela estreita o desenho é uma lista de barras; o diagrama de fluxo abre com o
botão "Ver o diagrama aqui", dentro de uma área que rola para o lado. V2: um arranjo vertical do próprio diagrama.

**D19. COFOG com cerca de um ano de atraso.** A classificação internacional de funções (S7) sai depois do RREO. Hoje: a
página diz quando o ano ainda não existe. V2: nada a corrigir.

**D20. Três perguntas abertas ao Tesouro** (enviadas ao Ministério da Fazenda em 10/11/2026, protocolos
18800174011202612, 18800174015202609 e 18800174016202645): títulos vendidos pelo RREO contra emissões da DPF; a fatia
que vence em 12 meses; títulos classificados por um índice que o nome não revela. Hoje: a página diz o status de cada
uma. V2: quando as respostas chegarem, entram na página, com o que mudou no site.
