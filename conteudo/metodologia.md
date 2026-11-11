# Como fazemos, passo a passo

Esta página explica, do começo ao fim, como o site chega a frases como "de cada R$ 1,00 que a União gastou, 31 centavos foram para o INSS". Está escrita para quem nunca abriu um relatório de orçamento. Cada palavra técnica vem com a explicação ao lado; o [glossário](#glossario) no fim repete as principais.

A regra que vale para tudo: **nenhum número é digitado**. Um programa baixa os arquivos oficiais, guarda cada um com um lacre (uma impressão digital, o *hash*), faz as contas pelas regras escritas num arquivo de configuração público, confere o resultado de dezenas de jeitos e só então gera estas páginas. Se uma conferência importante falha, a página do ano não sai. O código é aberto: qualquer pessoa pode rodar e chegar aos mesmos números.

## 1. De onde vêm os números

A fonte principal é o **RREO**, o Relatório Resumido da Execução Orçamentária. Todo governo do país (União, estados e municípios) publica esse relatório a cada dois meses no **SICONFI**, um sistema do Tesouro Nacional. Nós usamos o da União. Ele tem vários anexos, e usamos três:

- **Anexo 02**: a despesa por função e subfunção. Função é a caixinha grande (Saúde, Educação, Previdência Social); subfunção é a caixinha menor dentro dela (Atenção Básica, Ensino Superior, Previdência Básica). É daqui que saem nove das dez linhas.
- **Anexo 01**: o balanço do orçamento: toda a receita (o que entrou) e toda a despesa (o que saiu) por tipo. É daqui que saem os juros da dívida, a rolagem, o principal pago e a lista do que entrou.
- **Anexo 06**: o resultado primário, que é a conta oficial do Tesouro de quanto o governo gastou de verdade e quanto arrecadou, sem contar dívida. É a régua do total.

As outras fontes entram para conferir e para abrir detalhes:

- **SIOP**, o sistema do orçamento federal (Ministério do Planejamento): o mesmo gasto, visto por ação (programa), órgão e tipo de despesa. Serve para conferir o RREO de outro ângulo e para abrir cada linha até o detalhe.
- **Tesouro Nacional**: planilhas e tabelas da dívida pública (quanto deve, para quem, quanto pagou de juros e de principal).
- **Banco Central**: séries da dívida e dos juros pela régua do Banco Central.
- **IBGE**: a população do Brasil, para dividir os valores por habitante.

Todas são públicas e gratuitas. A lista completa, com os endereços, está na página de [dados](../dados/).

## 2. Baixar e lacrar

O programa baixa cada arquivo direto da fonte e guarda duas coisas: o arquivo como veio e uma impressão digital dele, o *hash* SHA-256, uma sequência de letras e números que muda se um único byte do arquivo mudar. Chamamos isso de **lacre**. Toda página mostra os lacres dos arquivos que usou. Se alguém contestar um número, o arquivo lacrado mostra o que a fonte publicou naquele dia.

Quando a fonte publica de novo o mesmo período com números diferentes (acontece: o governo corrige relatórios), o programa guarda a nova versão com um novo lacre e não apaga a antiga.

## 3. Arrumar os dados

Os relatórios vêm como a fonte publica: tabelas com nomes de linhas, códigos, colunas por bimestre. O programa lê tudo e guarda numa tabela única e organizada (o *staging*), sem mudar nenhum valor: só dá nome às colunas, identifica qual linha é função e qual é subfunção, e marca o escopo de cada uma. Qualquer pessoa pode abrir essa tabela e comparar com o relatório original.

## 4. O que é o R$ 1,00

Aqui está a decisão mais importante do site, e ela não é nossa: é a conta oficial do Tesouro. O R$ 1,00 é **tudo o que a União gastou de verdade no período**:

- a **despesa primária liquidada** (Anexo 06): aposentadorias, benefícios, salários, saúde, educação, repasses a estados e municípios, obras, custeio;
- mais os **juros da dívida pagos em dinheiro** (Anexo 01).

"Liquidada" é o estágio em que o governo reconhece que deve pagar: o serviço foi prestado, o benefício é devido. É a medida que o relatório traz por função. "Paga" é quando o dinheiro sai; a diferença entre as duas vira "restos a pagar", contas pagas no ano seguinte.

Fica **fora** do R$ 1,00, de propósito, o que não é gasto e sim troca de dinheiro por dinheiro:

- a **rolagem** da dívida: vender título novo para pagar título velho que venceu;
- o **pagamento de principal**: devolver o que foi emprestado (o custo da dívida, que são os juros, está dentro);
- **empréstimos e aplicações financeiras**: dinheiro que o Tesouro empresta ou aplica e deve voltar (financiamento estudantil, crédito via bancos públicos, capital em empresas).

Esses valores não somem: aparecem listados em cada página e desenhados na visão "Tudo o que passou pelo caixa".

## 5. As dez linhas

O Anexo 02 tem dezenas de funções e centenas de subfunções. Para caber na cabeça, agrupamos em dez linhas, por regras escritas no arquivo de configuração (cada linha diz quais funções e subfunções entram nela):

1. **Previdência (INSS)**: a subfunção Previdência Básica, em qualquer função.
2. **Aposentados e pensionistas do setor público**: pelo SIOP, os tipos de despesa "aposentadorias e reformas" e "pensões", em qualquer órgão (inclui militares).
3. **Assistência social**: a função Assistência Social.
4. **Saúde**: a função Saúde.
5. **Educação**: a função Educação, mais a subfunção que carrega o FUNDEB.
6. **Trabalho**: a função Trabalho.
7. **Defesa e segurança**: as funções Defesa Nacional e Segurança Pública.
8. **Transferências a estados e municípios**: a subfunção Transferências (fundos de participação e repartição de receita).
9. **Juros da dívida**: do Anexo 01, juros e encargos pagos.
10. **Encargos especiais e demais funções**: tudo o que sobrou.

Por que agrupar por subfunção e não por função? Porque a função "Previdência Social" não é o INSS: tem dentro a previdência dos servidores e a administração. Olhando a subfunção, separamos o que é o quê.

Uma linha nunca é "o que o governo chama de X" sem conferir: quando o SIOP mostra que um gasto está numa função, mas é de outra natureza (pensões militares dentro de Defesa, por exemplo), ele muda de linha, e a mudança fica registrada na ficha das duas linhas, com o valor.

## 6. Centavos que somam 100

Cada linha dividida pelo total dá uma fração. Para virar centavos inteiros que somam exatamente 100, usamos o método dos maiores restos: arredonda tudo para baixo e distribui o que faltou para as linhas com a maior parte decimal. É o mesmo método usado para distribuir cadeiras em eleições.

## 7. De onde veio o dinheiro

O lado esquerdo do desenho é a receita (o que entrou), pelos Anexos 01 e 06:

- **tributos**: impostos, taxas e contribuições;
- **outras receitas**: royalties, dividendos, concessões, serviços, multas, venda de bens;
- **dívida nova ou caixa**: o que faltou. Quando o gasto passa da receita, a diferença veio de títulos vendidos além da rolagem ou do dinheiro que já estava no caixa.

Como o dinheiro não tem carimbo (um real de imposto não vai "para a saúde" e outro "para a aposentadoria"), o site usa a **regra do rateio**: cada real que entrou é dividido na mesma proporção do que foi gasto. É assim que se chega a "de cada R$ 1,00 de tributo, X centavos foram para Y".

## 8. As conferências

Antes de publicar, o programa faz dezenas de contas que têm que fechar. Algumas:

- as subfunções somam as funções, e as funções somam o total do relatório;
- o total do Anexo 02 é igual ao do Anexo 01 (mesmo universo);
- as dez linhas somam o total ao centavo;
- os tributos são iguais nos dois anexos que os publicam;
- o RREO bate com o SIOP função por função (duas fontes independentes descrevendo o mesmo bolo);
- cada linha, comparada com o mesmo período do ano anterior, não deu um salto sem explicação;
- as partes de dentro de cada linha somam a linha.

As conferências **críticas** travam a publicação se falharem: a página do ano não sai. As demais são avisos, e ficam explicadas na própria página. A lista completa do ano, com o resultado de cada uma, está [abaixo](#conferencias).

## 9. Abrir cada linha

Ao clicar numa faixa do desenho, ela abre nas partes de dentro. De onde vêm:

- **subfunções** do RREO (Atenção Básica, Ensino Superior...), com os ajustes nomeados: se um valor saiu daquela subfunção para outra linha, ou para fora do R$ 1,00, isso está escrito ali, com o número;
- **ações** do SIOP dentro de cada subfunção (o programa de governo: Bolsa Família, Farmácia Popular, uma universidade), só no ano fechado, porque no ano em andamento o SIOP e o RREO não cobrem o mesmo período;
- para aposentados e pensionistas, **tipos de despesa e órgãos** do SIOP;
- para os juros, as **carteiras** da dívida pelo Tesouro: títulos no mercado, títulos no Banco Central, dívida externa, contratos, cobertura do resultado do Banco Central.

Quando um ajuste não tem detalhe por subfunção (ano em andamento, sem SIOP), ele fica nomeado, com o valor, numa lista à parte: a conta fecha com ele à vista, não escondido.

## 10. Dividido pela população

Os valores por habitante usam a população do Brasil em 1º de julho do ano, pela projeção do IBGE (revisão 2024), a mesma régua das estimativas oficiais. É só um jeito de ler o tamanho: R$ 363 bilhões de juros viram "R$ 1.700 por pessoa". Não é o que cada pessoa pagou (quem paga mais imposto paga mais) nem recebeu.

## 10a. A dívida: quanto devemos, do que é feita e quando vence

A aba "A dívida" responde a três perguntas com os arquivos abertos do Tesouro Nacional, sem nenhum número do noticiário.

**Quanto devemos.** O número do meio é a dívida da União fora do Banco Central no último dia do período (a "DPF em poder do público", a medida do próprio Tesouro): títulos vendidos a bancos, fundos, previdência privada, pessoas e estrangeiros, mais os contratos de empréstimo. Vem de duas fontes que têm que bater: a planilha mensal do estoque (em bilhões) e o arquivo título a título (em reais, uma linha por título e vencimento). Dividido pela população do IBGE, dá a parte média de cada habitante. Os títulos que estão com o Banco Central ficam fora desse número e aparecem à parte.

**A conta do ano.** Como a dívida saiu do número do fim do ano anterior para o do fim deste: mais o que o Tesouro vendeu (emissões), menos o que pagou em dinheiro (resgates: principal e juros), mais os juros que a dívida gerou no ano, pagos ou não (apropriados: todos entram no saldo; os pagos em dinheiro no mesmo ano já estão descontados nos resgates), mais as operações do mercado com o Banco Central. É a abertura que o próprio Tesouro publica (Anexo 2.9 do Relatório Mensal da Dívida, em planilha). A conta tem que fechar com o estoque do fim do período; é uma conferência crítica.

**Do que é feita.** Cada título é corrigido por um índice: a Selic (a taxa básica), uma taxa combinada na venda (prefixado), a inflação (IPCA ou IGP-M) ou o câmbio. O site classifica cada título pelo começo do nome (LFT é Selic, LTN e NTN-F são prefixados, NTN-B é IPCA, e assim por diante; a regra está em `config/linhas.yaml`), e a fatia de cada índice é conferida com as séries do Banco Central. O Tesouro classifica pelo contrato de cada papel, que o nome nem sempre revela; por isso a composição do Relatório Mensal difere daqui em até 0,1 ponto percentual, e isso está dito.

**Quando vence.** Cada título tem uma data de vencimento. O site conta o principal de cada um pelo vencimento final e soma por faixa (até 12 meses, de 1 a 2 anos, de 2 a 5, mais de 5); dentro da primeira faixa, mês a mês. É um número diferente do "percentual vincendo em 12 meses" do Relatório Mensal, que inclui também os juros (cupons) que caem nos 12 meses e por isso sai maior. Os dois são verdadeiros; medem coisas diferentes, e a página diz qual é qual.

**Juros, uma régua por pergunta.** "Juros da dívida" tem vários números oficiais: o que saiu do caixa no ano (RREO, a linha do recibo), o que a dívida gerou pagos ou não (Tesouro), a conta líquida do Banco Central (o número do noticiário), a conta por competência do RREO e o número da comparação internacional. Cada um responde a uma pergunta diferente; a página mostra todos, com a pergunta e a fonte, e nunca soma um com o outro.

## 10b. Limitações e próximos passos {#limitacoes}

O que o site ainda não separa, dito com todas as letras, e o que vem depois. A lista completa, item por item, com o que a próxima versão precisa fazer, está no repositório: [DEBITOS.md](https://github.com/cadareal/pipeline/blob/main/DEBITOS.md). As diferenças entre relatórios oficiais que ainda não sabemos explicar estão na página [Perguntas abertas](../perguntas-abertas/), cada uma com a pergunta pronta para o órgão responsável.

- **Ações dentro de subfunções de onde saiu dinheiro por elemento de despesa.** A linha "Aposentados e pensionistas do setor público" é medida pelos elementos de despesa 01 e 03 do SIOP, e esse dinheiro sai das subfunções onde estava (pensões militares, de "Administração Geral" da Defesa; aposentadorias civis, de "Previdência do Regime Estatutário"). A lista de ações do SIOP não traz o elemento, então não dá para dizer de qual ação saiu o quê. Quando o que saiu passa de um quarto do que as ações somam, a subfunção não abre por ação e a ficha diz os dois números; abaixo disso, as ações aparecem inteiras e a ficha diz quanto está em outra faixa. Próximo passo: a agregação ação por elemento do SIOP.

- **O carimbo da fonte não separa os tributos.** O SIOP diz com que fonte cada despesa foi paga, mas imposto de renda, IPI, COFINS e companhia entram juntos na fonte "recursos livres". Por isso, dentro de "Tributos" (e de cada imposto), o outro lado do desenho mostra o destino de toda a receita, tracejado e dito como tal. Próximo passo: ligar as contribuições que têm carimbo próprio ao que elas pagam (a previdenciária ao INSS, o salário-educação à educação), com a lista de fontes do SIOP conferida.
- **Faixas tracejadas são números calculados.** A diferença entre o que entrou e o que saiu do caixa, e a parte dos títulos vendidos não usada no ano, não são linhas de relatório: são contas deste site entre relatórios (RREO e SIOP). Elas aparecem tracejadas, com o motivo no card e na ficha.
- **Faixas muito finas são desenhadas com 5 px**, para dar para passar o mouse e clicar. O valor escrito é sempre o verdadeiro; só o desenho tem um mínimo.
- **O tamanho da reserva de liquidez e quem são os credores** (bancos, fundos, previdência, estrangeiros) o Tesouro publica só no Relatório Mensal da Dívida, em PDF. Sem arquivo aberto, o site não mostra; diz que não mostra.
- **A base COFOG** (comparação internacional) sai com cerca de um ano de atraso; a página diz quando o ano ainda não existe.
- **Estados e municípios** ainda não entraram: a União é a fase 1. O RREO dos estados e municípios está no mesmo SICONFI, e a estrutura do site já prevê as três esferas.
- **Celular**: em tela estreita o desenho é uma lista de barras; o diagrama de fluxo abre com o botão "Ver o diagrama aqui", dentro de uma área que rola para o lado. Um arranjo vertical do próprio diagrama fica para a próxima versão.
- **Valores de cada ano**: as comparações entre anos são em reais de cada ano, sem correção pela inflação; a tabela diz isso. Uma coluna corrigida pelo IPCA fica para a próxima versão, ao lado da nominal, nunca no lugar dela.

## 11. O que o site não faz

- Não projeta nem estima: só números publicados.
- Não opina se um gasto é bom ou ruim.
- Não mistura réguas: quando há mais de um número oficial para a mesma coisa (há cinco para "juros"), mostra todos, com nome, e nunca soma um com o outro.
- Não esconde diferença: quando duas fontes não fecham, a diferença aparece com nome e valor.

## 12. Quando os números mudam

O RREO sai a cada dois meses; o SIOP é atualizado continuamente; a dívida, todo mês; a população, uma vez por ano. O programa roda de novo, lacra o que mudou e gera as páginas de novo. O histórico de versões da metodologia e dos números está no repositório, no GitHub.
