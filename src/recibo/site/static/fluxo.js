/* Cada Real: o desenho de fluxo (entradas à esquerda, destinos à direita) com zoom ao clicar, trilha, card no hover
   e ficha. Este arquivo só desenha e troca de visão: não faz conta nem formata número. Todos os textos numéricos
   ("R$ 990,2 bi", "31 centavos", "30,7%") vêm prontos do JSON embutido na página, gerado pelo pipeline em Python. */
(function () {
  'use strict';
  var el = document.getElementById('dados-fluxo');
  if (!el) return;
  var dados;
  try { dados = JSON.parse(el.textContent); } catch (e) { return; }
  var svg = document.getElementById('svg');
  var tip = document.getElementById('tip');
  var fichaEl = document.getElementById('ficha');
  var fichaVazia = document.getElementById('ficha-vazia');
  var trilhaEl = document.getElementById('trilha');
  var subirBtn = document.getElementById('subir');
  var estadoEl = document.getElementById('estado');
  if (!svg) return;

  var NS = 'http://www.w3.org/2000/svg';
  var W = 1120, H_MIN = 560, Y0 = 100, X_ESQ = 296, X_C = 534, X_C2 = 566, X_DIR = 806, MARGEM_BAIXO = 100, MIN_H = 5;
  var LARG_ESQ = X_ESQ - 18, LARG_DIR = W - (X_DIR + 26) - 4;   // largura disponível para os rótulos de cada lado
  // A altura do desenho é calculada a cada vez: barra central (H), espaços entre as faixas e o espaço que cada rótulo
  // precisa (uma ou duas linhas de nome e uma ou duas de números). Nada fica acima do topo nem abaixo do fim do SVG:
  // com muitas partes o desenho cresce para baixo e a página rola.
  var H = H_MIN, YC = Y0, ALT = Y0 + H_MIN + MARGEM_BAIXO;

  var state = { visao: dados.visao_inicial || (dados.visoes[0] && dados.visoes[0].id), caminho: [], sel: null, hover: null };

  function esc(s) { return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) { return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]; }); }
  function visao() { for (var i = 0; i < dados.visoes.length; i++) if (dados.visoes[i].id === state.visao) return dados.visoes[i]; return dados.visoes[0]; }
  function desenhaveis(filhos) { var out = []; (filhos || []).forEach(function (f) { if (f.valor > 0) out.push(f); }); return out; }
  function temFilhos(n) { return desenhaveis(n.filhos).length > 0; }
  // uma faixa sem partes mas com "outro lado" próprio (carimbo da fonte no caixa; cruzamento título a título na dívida)
  // também abre: ela fica inteira de um lado e o outro lado dela do outro
  function podeEntrar(n) { return temFilhos(n) || desenhaveis(n.outro_lado).length > 0; }
  // o outro lado de um zoom: rateio (visão do gasto) ou o que cada nó traz em outro_lado (caixa, dívida)
  function outroDaVisao(v) { return v.outro || (v.id === 'caixa' ? { modo: 'carimbo' } : { modo: 'rateio' }); }
  function cruza(v) { return outroDaVisao(v).modo !== 'rateio'; }

  // procura um nó pelo id nas duas árvores da visão; devolve {no, lado, pais}
  function achar(id) {
    var v = visao();
    function busca(lista, lado, pais) {
      for (var i = 0; i < lista.length; i++) {
        var it = lista[i];
        if (it.id === id) return { no: it, lado: lado, pais: pais };
        if (it.filhos) { var r = busca(it.filhos, lado, pais.concat([it])); if (r) return r; }
        if (it.outro_lado) { var o = busca(it.outro_lado, lado === 'esq' ? 'dir' : 'esq', pais.concat([it])); if (o) return o; }
      }
      return null;
    }
    return busca(v.entradas, 'esq', []) || busca(v.saidas, 'dir', []);
  }
  function alvo() {
    if (!state.caminho.length) return null;
    var a = achar(state.caminho[state.caminho.length - 1]);
    return a && podeEntrar(a.no) ? a : null;
  }
  function tint(hex, f) {
    if (!hex || hex[0] !== '#' || hex.length !== 7) return '#A0A6A3';
    var n = parseInt(hex.slice(1), 16), r = n >> 16, g = (n >> 8) & 255, b = n & 255;
    function m(c) { return ('0' + Math.round(c + (255 - c) * f).toString(16)).slice(-2); }
    return '#' + m(r) + m(g) + m(b);
  }
  function corDe(a) {
    if (a.no.cor) return a.no.cor;
    if (a.pais.length) {
      var pai = a.pais[a.pais.length - 1];
      var idx = 0; (pai.filhos || []).some(function (f, i) { if (f.id === a.no.id) { idx = i; return true; } return false; });
      var base = pai.cor || (a.pais.length > 1 ? corDe({ no: pai, pais: a.pais.slice(0, -1) }) : '#64748B');
      return tint(base, 0.15 + (idx % 6) * 0.1);   // tons que se alternam: com muitas partes, vizinhas continuam distinguíveis
    }
    return '#64748B';
  }
  // "O gasto de verdade" -> "do gasto de verdade"; "Saúde" -> "de saúde"
  function de(nome, no) {
    var s = String(nome || '');
    if (no && (no.tipo === 'prazo' || no.tipo === 'periodo')) return 'de \u201c' + s + '\u201d';   // "de “Vence em até 12 meses”"
    var m = /^(O|A|Os|As)\s+(.*)$/.exec(s);
    if (m) return { O: 'do ', A: 'da ', Os: 'dos ', As: 'das ' }[m[1]] + m[2].toLowerCase();
    return 'de ' + s.toLowerCase();
  }
  function encurtar(s, n) { s = String(s || ''); return s.length > n ? s.slice(0, n - 1).replace(/\s+\S*$/, '') + '…' : s; }

  // ---------------------------------------------------------------- medida de texto: os rótulos quebram em linhas pelo que cabe, não por contagem de letras
  var medidor = null;
  try { var canvas = document.createElement('canvas'); medidor = canvas.getContext ? canvas.getContext('2d') : null; } catch (e) { medidor = null; }
  var familia = '';
  function largura(texto, fs, peso) {
    if (medidor) {
      if (!familia) { try { familia = getComputedStyle(svg).fontFamily || ''; } catch (e) { familia = ''; } if (!familia) familia = 'sans-serif'; }
      medidor.font = (peso || 400) + ' ' + fs + 'px ' + familia;
      return medidor.measureText(String(texto)).width * 1.04;   // folga para diferenças entre a medida do canvas e o texto do SVG
    }
    return String(texto).length * fs * 0.6;
  }
  function cortar(s, fs, peso, max) {
    if (largura(s, fs, peso) <= max) return s;
    var lo = 1, hi = s.length;
    while (lo < hi) { var meio = (lo + hi + 1) >> 1; if (largura(s.slice(0, meio) + '…', fs, peso) <= max) lo = meio; else hi = meio - 1; }
    return s.slice(0, lo).replace(/[\s,;:(]+$/, '') + '…';
  }
  // quebra o texto em até maxLinhas linhas: pelas palavras, ou pelo separador quando ele é dado (" · " na linha dos
  // números); só a última linha ganha "…", e só se nem assim couber. O nome inteiro continua no card e na ficha.
  function quebrar(texto, fs, peso, max, maxLinhas, sep) {
    var partes = sep ? String(texto || '').split(sep) : String(texto || '').split(/\s+/).filter(Boolean);
    var junta = sep || ' ', linhas = [], atual = '';
    for (var i = 0; i < partes.length; i++) {
      var tentativa = atual ? atual + junta + partes[i] : partes[i];
      if (!atual || largura(tentativa, fs, peso) <= max) { atual = tentativa; continue; }
      linhas.push(atual); atual = partes[i];
      if (linhas.length === maxLinhas - 1) { atual = partes.slice(i).join(junta); break; }
    }
    if (atual) linhas.push(atual);
    return linhas.map(function (l) { return cortar(l, fs, peso, max); });
  }
  // as linhas de um rótulo (nome em até duas linhas, números em até duas) e a altura que ele ocupa
  function linhasRotulo(it, max) {
    var linhas = [];
    quebrar(it.no.nome, 14, 600, max, 2).forEach(function (t) { linhas.push({ t: t, fs: 14, peso: 600, cor: '#15201F', alt: 17, num: false }); });
    quebrar(rotulo(it), 12, 400, max, 2, ' · ').forEach(function (t) { linhas.push({ t: t, fs: 12, peso: 400, cor: '#5B625F', alt: 15, num: true }); });
    var alt = 12 + 4;   // primeira linha de base a 12 px do topo do bloco; 4 px de descida no fim
    for (var i = 1; i < linhas.length; i++) alt += linhas[i].alt;
    return { linhas: linhas, alt: alt, passo: alt + 8 };
  }

  // ---------------------------------------------------------------- layout
  function banda(x1, y1, x2, y2, h) {
    var xm = (x1 + x2) / 2;
    return 'M' + x1 + ',' + y1 + ' C' + xm + ',' + y1 + ' ' + xm + ',' + y2 + ' ' + x2 + ',' + y2 + ' L' + x2 + ',' + (y2 + h) + ' C' + xm + ',' + (y2 + h) + ' ' + xm + ',' + (y1 + h) + ' ' + x1 + ',' + (y1 + h) + ' Z';
  }
  function layout() {
    var v = visao(), z = alvo();
    var esqItens, dirItens, total, ladoZoom = null, modoOutro = 'raiz';
    if (z) {
      ladoZoom = z.lado;
      var outroLado = z.lado === 'esq' ? 'dir' : 'esq';
      var filhos = desenhaveis(z.no.filhos).map(function (f) { return { no: f, pais: z.pais.concat([z.no]), lado: z.lado, dentro: true }; });
      if (!filhos.length) filhos = [{ no: z.no, pais: z.pais, lado: z.lado, inteira: true }];  // sem partes: a faixa inteira de um lado, o carimbo do outro
      var outros;
      if (cruza(v)) {
        // nestas visões o outro lado nunca é rateio: é o que o nó traz (carimbo da fonte no caixa, cruzamento título a título na dívida);
        // se o nó não tem o seu, mas o conjunto de onde veio tem, mostra o do conjunto, tracejado e dito como tal; senão, nada
        outros = (z.no.outro_lado || []).filter(function (o) { return o.valor > 0; }).map(function (o) { return { no: o, pais: [z.no], lado: outroLado, carimbo: true }; });
        modoOutro = outros.length ? 'carimbo' : 'vazio';
        if (!outros.length && z.no.outro_lado_de) {
          var dono = achar(z.no.outro_lado_de);
          if (dono && dono.no.outro_lado) {
            outros = dono.no.outro_lado.filter(function (o) { return o.valor > 0; }).map(function (o) { return { no: o, pais: [dono.no], lado: outroLado, carimbo: true, herdado: true, dono: dono.no }; });
            modoOutro = outros.length ? 'herdado' : 'vazio';
          }
        }
      } else {
        outros = (z.lado === 'esq' ? v.saidas : v.entradas).map(function (o) { return { no: o, pais: [], lado: outroLado, rateado: true }; });
        modoOutro = 'rateio';
      }
      total = filhos.reduce(function (s, f) { return s + f.no.valor; }, 0) || z.no.valor;
      if (z.lado === 'esq') { esqItens = filhos; dirItens = outros; } else { dirItens = filhos; esqItens = outros; }
    } else {
      esqItens = v.entradas.map(function (o) { return { no: o, pais: [], lado: 'esq' }; });
      dirItens = v.saidas.map(function (o) { return { no: o, pais: [], lado: 'dir' }; });
      total = v.total;
    }
    // 1) rótulos de cada lado: quantas linhas e quanta altura cada um precisa
    esqItens.forEach(function (it) { it.rot = linhasRotulo(it, LARG_ESQ); });
    dirItens.forEach(function (it) { it.rot = linhasRotulo(it, LARG_DIR); });
    // 2) altura: a coluna de fora de cada lado é a barra central mais os espaços entre as faixas (14 px à esquerda,
    //    10 à direita); a coluna mais alta precisa caber, e os rótulos também. Nada fica fora do SVG.
    var GAP_ESQ = 14, GAP_DIR = 10;
    var gEsq = GAP_ESQ * Math.max(esqItens.length - 1, 0), gDir = GAP_DIR * Math.max(dirItens.length - 1, 0), gMax = Math.max(gEsq, gDir);
    function precisam(itens) { var s = 12; itens.forEach(function (it) { s += it.rot.passo; }); return s; }
    var HOUT = Math.max(H_MIN + gMax, precisam(esqItens), precisam(dirItens));
    H = HOUT - gMax; YC = Y0 + gMax / 2; ALT = Y0 + HOUT + MARGEM_BAIXO;
    svg.setAttribute('viewBox', '0 0 ' + W + ' ' + ALT);
    function colocar(itens, gap, ladoEsq) {
      var soma = itens.reduce(function (s, it) { return s + it.no.valor; }, 0) || 1;
      var esc = H / soma, gTot = gap * Math.max(itens.length - 1, 0);
      // faixa mínima: uma parte com menos de MIN_H px vira MIN_H px (dá para passar o mouse e clicar); as maiores cedem o
      // espaço na proporção do que têm além do mínimo. O valor escrito no rótulo é sempre o verdadeiro.
      var alturas = itens.map(function (it) { return it.no.valor * esc; });
      var deficit = 0, folga = 0;
      alturas.forEach(function (h) { if (h < MIN_H) deficit += MIN_H - h; else folga += h - MIN_H; });
      if (deficit > 0) {
        alturas = alturas.map(function (h) { return h < MIN_H ? MIN_H : (folga > 0 ? h - (h - MIN_H) * Math.min(1, deficit / folga) : h); });
        var somaH = alturas.reduce(function (s, h) { return s + h; }, 0);
        if (somaH > H + 0.01) alturas = alturas.map(function (h) { return h * H / somaH; });
      }
      var y = Y0 + (gMax - gTot) / 2, yc = YC, nos = [];
      itens.forEach(function (it, i) {
        if (i) y += gap;
        var h = alturas[i];
        nos.push({ item: it, x: ladoEsq ? X_ESQ : X_DIR, y: y, h: h, yc: yc, w: 12, cor: corDe(it), minima: h > it.no.valor * esc + 0.01 });
        y += h; yc += h;
      });
      // rótulos: cada um quer ficar centrado na sua faixa. Primeiro, de cima para baixo, desce quem colide com o de cima;
      // depois, de baixo para cima, sobe quem passou do fim. As duas passadas usam a mesma altura de cada rótulo, e o
      // espaço total (ALT) já foi calculado para caber todos.
      var topo = 8;
      nos.forEach(function (n) { var t = n.y + n.h / 2 - n.item.rot.alt / 2; if (t < topo) t = topo; n.lt = t; topo = t + n.item.rot.passo; });
      var fim = ALT - 8;
      for (var i = nos.length - 1; i >= 0; i--) {
        var lim = (i === nos.length - 1) ? fim - nos[i].item.rot.alt : nos[i + 1].lt - nos[i].item.rot.passo;
        if (nos[i].lt > lim) nos[i].lt = lim;
      }
      nos.forEach(function (n) {
        n.lt = Math.round(n.lt); n.ly = n.lt + 12;
        var ymeio = n.y + n.h / 2, ycentro = n.lt + n.item.rot.alt / 2, desloc = Math.abs(ycentro - ymeio) > 10;
        if (ladoEsq) { n.lx = X_ESQ - 14; n.lig = desloc ? 'M' + (X_ESQ - 2) + ',' + ymeio.toFixed(1) + ' L' + (X_ESQ - 9) + ',' + ycentro.toFixed(1) : ''; }
        else { n.lx = X_DIR + 26; n.lig = desloc ? 'M' + (X_DIR + 14) + ',' + ymeio.toFixed(1) + ' L' + (X_DIR + 21) + ',' + ycentro.toFixed(1) : ''; }
        n.d = ladoEsq ? banda(X_ESQ + 12, n.y, X_C, n.yc, n.h) : banda(X_C2, n.yc, X_DIR, n.y, n.h);
        n.rotulo = rotulo(n.item);
        n.anchor = ladoEsq ? 'end' : 'start';
      });
      return nos;
    }
    return { v: v, z: z, total: total, esq: colocar(esqItens, GAP_ESQ, true), dir: colocar(dirItens, GAP_DIR, false), ladoZoom: ladoZoom, modoOutro: modoOutro };
  }
  function rotulo(it) {
    var n = it.no;
    if (it.rateado) return [n.pct, n.cent, 'rateio'].filter(Boolean).join(' · ');
    if (it.carimbo) return [n.txt, n.pct_pai, n.cent_pai].filter(Boolean).join(' · ');
    if (it.inteira) return n.txt + ' · a faixa inteira = R$ 1,00';
    if (it.dentro) return [n.txt, n.pct_pai || n.pct, n.cent_pai || n.cent].filter(Boolean).join(' · ');
    return it.lado === 'esq' ? [n.txt, n.pct, n.cent].filter(Boolean).join(' · ') : [n.cent, n.pct, n.txt].filter(Boolean).join(' · ');
  }

  // ---------------------------------------------------------------- SVG (elementos reaproveitados por id, para a transição CSS)
  var cache = {};
  function elemento(chave, tag, pai) {
    var e = cache[chave];
    if (!e) { e = document.createElementNS(NS, tag); e.setAttribute('data-k', chave); cache[chave] = e; pai.appendChild(e); }
    else if (e.parentNode !== pai) pai.appendChild(e);
    e._vivo = true;
    return e;
  }
  function liga(e, it) {
    if (e._ligado) return;
    e._ligado = true;
    e.addEventListener('click', function () { abrir(e._item); });
    e.addEventListener('mouseenter', function () { state.hover = e._item.no.id; pintar(); mostrarTip(e._item); });
    e.addEventListener('mouseleave', function () { state.hover = null; pintar(); tip.hidden = true; });
    e.addEventListener('keydown', function (ev) { if (ev.key === 'Enter' || ev.key === ' ') { ev.preventDefault(); abrir(e._item); } });
    e.addEventListener('focus', function () { state.hover = e._item.no.id; pintar(); mostrarTip(e._item); });
    e.addEventListener('blur', function () { state.hover = null; pintar(); tip.hidden = true; });
  }
  var camadas = {};
  function camada(nome) {
    if (!camadas[nome]) { camadas[nome] = document.createElementNS(NS, 'g'); camadas[nome].setAttribute('data-camada', nome); svg.appendChild(camadas[nome]); }
    return camadas[nome];
  }
  var L;
  function desenhar() {
    L = layout();
    Object.keys(cache).forEach(function (k) { cache[k]._vivo = false; });
    var gFluxos = camada('fluxos'), gNos = camada('nos'), gCentro = camada('centro'), gTexto = camada('texto');
    var centro = elemento('centro', 'rect', gCentro);
    centro.setAttribute('x', X_C); centro.setAttribute('y', YC.toFixed(1)); centro.setAttribute('width', 32); centro.setAttribute('height', H.toFixed(1)); centro.setAttribute('fill', '#15201F'); centro.setAttribute('rx', 3);
    var fundoSvg = elemento('fundo', 'rect', gCentro);
    fundoSvg.setAttribute('x', 0); fundoSvg.setAttribute('y', 0); fundoSvg.setAttribute('width', W); fundoSvg.setAttribute('height', ALT); fundoSvg.setAttribute('fill', 'transparent'); fundoSvg.setAttribute('pointer-events', 'none');
    [L.esq, L.dir].forEach(function (nos, li) {
      nos.forEach(function (n) {
        var id = (li ? 'd:' : 'e:') + n.item.no.id;
        var p = elemento('f' + id, 'path', gFluxos); p.setAttribute('d', n.d); p.setAttribute('fill', n.cor); p.setAttribute('class', 'clique'); p._item = n.item; liga(p, n.item);
        if (n.item.no.tracejado || n.item.herdado) { p.setAttribute('stroke', '#5B625F'); p.setAttribute('stroke-dasharray', '6 4'); p.setAttribute('stroke-width', 1.5); } else { p.removeAttribute('stroke'); p.removeAttribute('stroke-dasharray'); }
        var r = elemento('r' + id, 'rect', gNos); r.setAttribute('x', n.x); r.setAttribute('y', n.y.toFixed(1)); r.setAttribute('width', n.w); r.setAttribute('height', Math.max(n.h, 0.5).toFixed(1)); r.setAttribute('fill', n.cor); r.setAttribute('rx', 2); r.setAttribute('class', 'clique'); r.setAttribute('tabindex', '0'); r.setAttribute('role', 'button');
        r.setAttribute('aria-label', n.item.no.nome + ': ' + n.rotulo + (podeEntrar(n.item.no) && !n.item.inteira ? '. Enter para entrar' : '. Enter para ver a ficha')); r._item = n.item; liga(r, n.item);
        var lg = elemento('l' + id, 'path', gTexto); lg.setAttribute('d', n.lig || 'M0,0'); lg.setAttribute('stroke', '#A8ADAA'); lg.setAttribute('stroke-width', 1); lg.setAttribute('fill', 'none');
        // um <text> por rótulo, uma linha (tspan) por pedaço: nome em até duas linhas, números em até duas
        var t1 = elemento('t' + id, 'text', gTexto); t1.setAttribute('x', n.lx); t1.setAttribute('y', n.ly); t1.setAttribute('text-anchor', n.anchor); t1.setAttribute('class', 'clique');
        while (t1.firstChild) t1.removeChild(t1.firstChild);
        n.item.rot.linhas.forEach(function (ln, k) {
          var ts = document.createElementNS(NS, 'tspan');
          ts.setAttribute('x', n.lx); if (k) ts.setAttribute('dy', ln.alt);
          ts.setAttribute('font-size', ln.fs); if (ln.peso !== 400) ts.setAttribute('font-weight', ln.peso); ts.setAttribute('fill', ln.cor); if (ln.num) ts.setAttribute('class', 'num');
          ts.textContent = ln.t; t1.appendChild(ts);
        });
        t1._item = n.item; liga(t1, n.item);
      });
    });
    Object.keys(cache).forEach(function (k) { if (!cache[k]._vivo) { if (cache[k].parentNode) cache[k].parentNode.removeChild(cache[k]); delete cache[k]; } });
    pintar();
    cabecalho();
    trilha();
    estado();
    legendaTracejado();
  }
  var legendaEl = document.getElementById('legenda-tracejado');
  function legendaTracejado() {
    if (!legendaEl) return;
    var itens = L.esq.concat(L.dir);
    var temTrac = itens.some(function (x) { return x.item.no.tracejado || x.item.herdado; });
    var temMin = itens.some(function (x) { return x.minima; });
    var partes = [];
    if (temTrac) partes.push('Faixas tracejadas são números calculados por este site (uma diferença entre o que entrou e o que saiu) ou o conjunto de onde a parte veio, quando o carimbo da fonte não a separa; nunca uma linha lida direto de um relatório. Passe o mouse numa faixa tracejada para ver o motivo dela.');
    if (temMin) partes.push('Faixas muito finas são desenhadas com ' + MIN_H + ' px para dar para passar o mouse e clicar; o valor escrito é o verdadeiro.');
    legendaEl.textContent = partes.join(' ');
    legendaEl.hidden = !partes.length;
  }
  function pintar() {
    Object.keys(cache).forEach(function (k) {
      var e = cache[k]; if (!e._item) return;
      var ativo = state.sel === e._item.no.id || state.hover === e._item.no.id;
      if (k[0] === 'f') e.setAttribute('fill-opacity', e._item.herdado ? (ativo ? 0.6 : 0.38) : (ativo ? 0.96 : 0.74));
    });
  }
  var TEXTOS_CARIMBO = {
    esq: 'com que dinheiro foi pago, pelo carimbo da fonte no orçamento (SIOP)',
    dir: 'o que esse dinheiro pagou no ano, pelo carimbo da fonte no orçamento (SIOP)',
    estado: 'O outro lado não é rateio: é o carimbo da fonte na lei orçamentária (SIOP), que diz com que dinheiro cada despesa foi paga.',
    vazio_estado: 'O outro lado fica em branco porque o carimbo da fonte (SIOP) só é comparável no ano fechado; nada de aproximação.',
    ficha_esq: 'O que esse dinheiro pagou no ano', ficha_dir: 'Com que dinheiro foi pago', rotulo: 'carimbo da fonte',
    termina: 'o carimbo da fonte'  // o que termina nas faixas do outro lado (elas não abrem): "o carimbo da fonte termina aqui"
  };
  function textoOutro(v, chave, nome) { var o = outroDaVisao(v); var t = o[chave] || TEXTOS_CARIMBO[chave] || ''; return nome ? t.replace(/\{nome\}/g, '“' + nome + '”') : t; }
  function motivoTracejado(it) {
    var n = it.no;
    if (it.herdado) return 'É o outro lado do conjunto ' + (it.dono ? '\u201c' + it.dono.nome + '\u201d' : '') + ': o carimbo da fonte não separa esta parte, então o desenho mostra o conjunto de onde ela veio, com as porcentagens dele.';
    if (n.tracejado) return n.tracejado_motivo || 'É um número calculado por este site (uma diferença entre entradas e saídas), não uma linha lida de um relatório.';
    return '';
  }
  function donoHerdado() { var it = (L.esq.concat(L.dir)).map(function (n) { return n.item; }).filter(function (it) { return it.herdado; })[0]; return it ? it.dono : null; }
  function cabecalho() {
    var v = L.v, z = L.z;
    document.getElementById('centro-txt').textContent = z ? z.no.txt : v.centro_txt;
    document.getElementById('centro-nome').textContent = z ? (z.no.nome + ' em ' + dados.ano + ' = R$ 1,00') : v.centro_nome;
    var tEsq = document.getElementById('titulo-esq'), tDir = document.getElementById('titulo-dir');
    if (tEsq) tEsq.textContent = v.titulo_esq || 'De onde veio';
    if (tDir) tDir.textContent = v.titulo_dir || 'Para onde foi';
    var rate = ' (rateio: cada real tem a mesma origem e o mesmo destino que o conjunto)';
    var partes = z ? 'as partes ' + de(z.no.nome, z.no) + ': R$ · % · centavos de cada R$ 1,00 ' + de(z.no.nome, z.no) : '';
    var outroEsq = '', outroDir = '';
    if (z) {
      if (L.modoOutro === 'rateio') { outroEsq = 'de onde veio cada real ' + de(z.no.nome, z.no) + rate; outroDir = 'para onde foi cada real ' + de(z.no.nome, z.no) + rate; }
      else if (L.modoOutro === 'carimbo') { outroEsq = textoOutro(v, 'esq'); outroDir = textoOutro(v, 'dir'); }
      else if (L.modoOutro === 'herdado') { var dono = donoHerdado(); outroEsq = textoOutro(v, 'herdado_esq', dono && dono.nome); outroDir = textoOutro(v, 'herdado_dir', dono && dono.nome); }
      else { outroEsq = outroDir = textoOutro(v, 'vazio') || ('sem detalhe por fonte neste período: ' + (v.sem_fonte_motivo || 'o carimbo da fonte vem do SIOP e só vale no ano fechado')); }
    }
    document.getElementById('legenda-esq').textContent = z ? (z.lado === 'dir' ? outroEsq : partes) : v.legenda_esq;
    document.getElementById('legenda-dir').textContent = z ? (z.lado === 'esq' ? outroDir : partes) : v.legenda_dir;
  }
  function trilha() {
    var v = L.v, cam = L.z ? state.caminho : [];
    var html = '<span class="mini" style="margin-right:4px">Você está em:</span>';
    html += '<button type="button" class="link' + (cam.length ? '' : ' passo') + '" data-trilha="-1">' + esc(v.centro_txt + ' de ' + dados.ano) + '</button>';
    cam.forEach(function (id, i) {
      var a = achar(id);
      html += '<span class="mini">›</span><button type="button" class="link' + (i === cam.length - 1 ? ' passo' : '') + '" data-trilha="' + i + '">' + esc(a ? a.no.nome : id) + '</button>';
    });
    trilhaEl.innerHTML = html;
    subirBtn.hidden = !cam.length;
  }
  function estado() {
    var z = L.z;
    if (z) {
      var dono = L.modoOutro === 'herdado' ? donoHerdado() : null;
      var outro = L.modoOutro === 'rateio'
        ? 'O outro lado é o rateio: na visão do R$ 1,00 o dinheiro não tem carimbo, então cada real tem a mesma origem (ou o mesmo destino) que o conjunto.'
        : (L.modoOutro === 'carimbo' ? textoOutro(L.v, 'estado') : (L.modoOutro === 'herdado' ? textoOutro(L.v, 'herdado_estado', dono && dono.nome) : textoOutro(L.v, 'vazio_estado')));
      var fim = (L.modoOutro === 'carimbo' || L.modoOutro === 'herdado') ? ' As faixas do outro lado não abrem (' + textoOutro(L.v, 'termina') + ' termina nelas): a ficha de cada uma tem o botão “Ir para”, que leva à faixa inteira correspondente.' : '';
      estadoEl.textContent = 'Você está dentro ' + de(z.no.nome, z.no) + ': cada faixa deste lado é uma parte dela, com valor, porcentagem e centavos de cada R$ 1,00 ' + de(z.no.nome, z.no) + '. ' + outro + fim + ' Clique numa parte que tenha mais níveis para descer; a trilha acima traz de volta.';
    } else {
      estadoEl.textContent = 'Passe o mouse numa faixa para ver os números dela; clique para entrar. As faixas sem nível de dentro mostram a ficha e dizem qual será o próximo nível.';
    }
  }

  // ---------------------------------------------------------------- interação
  function abrir(it) {
    var a = achar(it.no.id);
    if (!a) return;
    if (it.inteira) { state.sel = a.no.id; pintar(); ficha(); return; }
    if (podeEntrar(a.no) && !(L.z && L.z.no.id === a.no.id)) {
      state.caminho = a.pais.map(function (p) { return p.id; }).concat([a.no.id]);
      state.sel = a.no.id; state.hover = null; tip.hidden = true;
      desenhar(); ficha();
      return;
    }
    state.sel = state.sel === a.no.id ? null : a.no.id;
    pintar(); ficha();
  }
  // "Ir para X": sai da faixa de carimbo (que termina) e vai à faixa de verdade, inteira; entra nela se ela abre
  function irParaNo(id) {
    var a = achar(id);
    if (!a) return;
    var pais = a.pais.map(function (p) { return p.id; });
    state.caminho = podeEntrar(a.no) ? pais.concat([a.no.id]) : pais;
    state.sel = a.no.id; state.hover = null; tip.hidden = true;
    desenhar(); ficha();
    var h = document.getElementById('h-fluxo'); if (h && h.parentNode && h.parentNode.scrollIntoView) h.parentNode.scrollIntoView({ behavior: 'smooth', block: 'start' });
  }
  function irPara(nivel) {
    state.caminho = nivel < 0 ? [] : state.caminho.slice(0, nivel + 1);
    state.hover = null; tip.hidden = true;
    desenhar(); ficha();
  }
  trilhaEl.addEventListener('click', function (ev) { var b = ev.target.closest('[data-trilha]'); if (b) irPara(parseInt(b.getAttribute('data-trilha'), 10)); });
  subirBtn.addEventListener('click', function () { irPara(state.caminho.length - 2); });

  function mostrarTip(it) {
    var n = it.no, z = L.z;
    var resumo = it.rateado ? [n.pct, n.cent].filter(Boolean).join(' · ') + (z ? ' de cada R$ 1,00 ' + de(z.no.nome, z.no) + ' (rateio)' : '')
      : ((it.dentro || it.carimbo) && z ? [n.txt, n.pct_pai, n.cent_pai].filter(Boolean).join(' · ') + ' de cada R$ 1,00 ' + de(it.herdado && it.dono ? it.dono.nome : z.no.nome, it.herdado && it.dono ? it.dono : z.no) + (it.carimbo ? ' (' + textoOutro(L.v, 'rotulo') + (it.herdado ? ', do conjunto' : '') + ')' : '') : [n.txt, n.pct, n.cent].filter(Boolean).join(' · '));
    var html = '<div style="display:flex;align-items:center;gap:8px"><span style="display:block;width:10px;height:10px;border-radius:3px;background:' + esc(corDe(it)) + ';flex:none"></span><strong style="font-size:15px">' + esc(n.nome) + '</strong></div>';
    html += '<div class="num" style="font-weight:600">' + esc(resumo) + '</div>';
    if (n.exato) html += '<div class="num tip-mini">Valor exato: ' + esc(n.exato) + '</div>';
    if (it.dentro && n.cent) html += '<div class="num tip-mini">No real inteiro: ' + esc(n.cent) + (n.pct ? ' (' + esc(n.pct) + ')' : '') + '</div>';
    if (n.hab) html += '<div class="num tip-mini">Dividido pela população: ' + esc(n.hab) + '</div>';
    if (n.tipo === 'acao') html += '<div class="tip-mini">Ação do orçamento (SIOP, liquidado)</div>';
    if (n.tipo === 'orgao') html += '<div class="tip-mini">Órgão (SIOP, liquidado)</div>';
    if (n.definicao) html += '<p>' + esc(encurtar(n.definicao, 220)) + '</p>';
    var motivoT = motivoTracejado(it);
    if (motivoT) html += '<p class="tip-mini"><b>Por que tracejada:</b> ' + esc(encurtar(motivoT, 150)) + ' (a ficha tem o texto inteiro)</p>';
    var dica;
    if (podeEntrar(n) && !it.inteira) {
      dica = z && z.no.id === n.id ? 'Você está dentro desta faixa'
        : (temFilhos(n) ? 'Clique para entrar e ver o que tem dentro'
          : (outroDaVisao(L.v).modo === 'cruzamento' ? 'Clique para entrar e ver o outro lado, título a título'
            : 'Clique para entrar e ver ' + (it.lado === 'esq' ? 'o que esse dinheiro pagou' : 'com que dinheiro foi pago')));
    } else if (n.ir_para && n.ir_para.nome) {
      dica = 'Esta faixa não abre: ' + esc(textoOutro(L.v, 'termina')) + ' termina aqui. Clique para ver a ficha e o botão “Ir para ' + esc(n.ir_para.nome) + '”, que leva à faixa inteira';
    } else {
      dica = 'Clique para ver a ficha' + (n.proximo ? ' · ' + esc(encurtar(n.proximo, 110)) : '');
    }
    html += '<div class="tip-dica">' + dica + '</div>';
    tip.innerHTML = html;
    var no = null;
    [L.esq, L.dir].forEach(function (nos) { nos.forEach(function (x) { if (x.item.no.id === n.id && x.item.lado === it.lado) no = x; }); });
    if (!no) return;
    // o card fica inteiro dentro da área do desenho: mede a própria altura e desliza para cima quando a faixa é das últimas
    tip.style.left = ((it.lado === 'esq' ? no.x + no.w + 10 : no.x - 10) / W * 100).toFixed(2) + '%';
    tip.style.transform = it.lado === 'esq' ? 'none' : 'translateX(-100%)';
    tip.style.top = '0px';
    tip.hidden = false;
    var areaAlt = svg.getBoundingClientRect().height || 1, escalaTip = areaAlt / ALT;
    var th = tip.offsetHeight || 0, yMeio = (no.y + no.h / 2) * escalaTip;
    tip.style.top = Math.max(0, Math.min(yMeio - th / 2, areaAlt - th)).toFixed(0) + 'px';
  }

  function ficha() {
    if (!fichaEl) return;
    var a = state.sel ? achar(state.sel) : null;
    if (!a) { fichaEl.hidden = true; fichaEl.innerHTML = ''; if (fichaVazia) fichaVazia.hidden = false; return; }
    var n = a.no, pai = a.pais.length ? a.pais[a.pais.length - 1] : null, z = L.z;
    var titulo = n.grupo === 'ficou' ? 'O que ficou: ' + n.nome : (pai ? pai.nome + ' › ' + n.nome : ((a.lado === 'esq' ? (L.v.titulo_esq || 'De onde veio') : (L.v.titulo_dir || 'Para onde foi')) + ': ' + n.nome));
    var filhos = desenhaveis(n.filhos);
    var html = '<div class="col">';
    html += '<div style="display:flex;justify-content:space-between;gap:12px;align-items:flex-start"><span class="eyebrow">Ficha da faixa selecionada</span><button type="button" class="link" id="fechar-ficha">Fechar</button></div>';
    html += '<div style="display:flex;align-items:center;gap:12px"><span style="display:block;width:14px;height:14px;border-radius:4px;background:' + esc(corDe(a)) + '" aria-hidden="true"></span><h2>' + esc(titulo) + '</h2></div>';
    if (n.definicao) html += '<p style="font-size:15px;line-height:1.55;color:#3A423F">' + esc(n.definicao) + '</p>';
    html += '<dl class="dl">';
    html += '<dt>Valor em ' + dados.ano + '</dt><dd class="num">' + esc(n.exato || n.txt) + '</dd>';
    if (n.cent) html += '<dt>Dentro do R$ 1,00</dt><dd class="num">' + esc(n.cent) + (n.pct ? ' (' + esc(n.pct) + ' de ' + esc(n.base_exato || L.v.total_exato) + ')' : '') + '</dd>';
    if (pai && n.cent_pai) html += '<dt>Dentro de ' + esc(pai.nome) + '</dt><dd class="num">' + esc(n.cent_pai) + ' de cada R$ 1,00 ' + esc(de(pai.nome, pai)) + (n.pct_pai ? ' (' + esc(n.pct_pai) + ')' : '') + '</dd>';
    if (n.hab) html += '<dt>Dividido pela população</dt><dd class="num">' + esc(n.hab) + ' (' + esc(L.v.hab_nota || 'não é o que cada pessoa pagou nem recebeu') + ')</dd>';
    html += '<dt class="mini">Como trocar</dt><dd class="mini">Clique noutra faixa do desenho; "Fechar" esconde a ficha.</dd>';
    if (n.var) html += '<dt>Variação</dt><dd class="num">' + esc(n.var) + ' contra o mesmo período do ano anterior</dd>';
    var fonteTxt = n.fonte || (n.tipo === 'acao' ? 'SIOP, ação (despesa liquidada)' : (n.tipo === 'orgao' ? 'SIOP, órgão (despesa liquidada)' : ''));
    if (fonteTxt) html += '<dt>Fonte</dt><dd>' + esc(fonteTxt) + '</dd>';
    var itemL = null;
    [L.esq, L.dir].forEach(function (nos) { nos.forEach(function (x) { if (x.item.no.id === n.id) itemL = x.item; }); });
    var motivoF = motivoTracejado(itemL || { no: n });
    if (motivoF) html += '<dt>Por que tracejada</dt><dd>' + esc(motivoF) + '</dd>';
    html += '</dl>';
    html += '<div style="display:flex;flex-wrap:wrap;gap:10px;margin-top:4px">';
    if (podeEntrar(n) && !(z && z.no.id === n.id)) html += '<button type="button" class="btn cheio" id="entrar-ficha">Entrar: ver ' + esc(n.nome) + ' como R$ 1,00</button>';
    if (n.ir_para && n.ir_para.id && achar(n.ir_para.id)) html += '<button type="button" class="btn' + (podeEntrar(n) ? '' : ' cheio') + '" id="ir-para-ficha">Ir para ' + esc(n.ir_para.nome || n.ir_para.id) + ' →</button>';
    if (n.pagina) html += '<a class="btn" href="' + esc((document.body.getAttribute('data-raiz') || '') + dados.ano + '/' + n.pagina) + '">Página de ' + esc(n.nome) + ' →</a>';
    html += '</div></div>';
    html += '<div class="col">';
    if (filhos.length) {
      html += '<h3>O que tem dentro</h3><table class="tabela"><tbody>';
      filhos.slice(0, 12).forEach(function (f) {
        html += '<tr><td><span style="display:inline-block;width:10px;height:10px;border-radius:3px;background:' + esc(corDe({ no: f, pais: a.pais.concat([n]) })) + ';margin-right:8px" aria-hidden="true"></span>' + esc(f.nome) + '</td><td class="num">' + esc([f.txt, f.cent_pai].filter(Boolean).join(' · ')) + '</td></tr>';
      });
      if (filhos.length > 12) html += '<tr><td class="mini">e mais ' + (filhos.length - 12) + ' partes (todas aparecem no desenho ao entrar' + (n.pagina ? ' e na página da linha' : '') + ')</td><td></td></tr>';
      html += '</tbody></table>';
    } else {
      html += '<h3>' + (pai ? 'Dentro de ' + esc(pai.nome) : 'O que tem dentro') + '</h3>';
    }
    if (n.outro_lado && n.outro_lado.length) {
      html += '<h3 style="margin-top:8px">' + esc(a.lado === 'esq' ? textoOutro(L.v, 'ficha_esq') : textoOutro(L.v, 'ficha_dir')) + ' <span class="mini">(' + esc(textoOutro(L.v, 'rotulo')) + ')</span></h3><table class="tabela"><tbody>';
      n.outro_lado.forEach(function (f) { html += '<tr><td>' + esc(f.nome) + '</td><td class="num">' + esc([f.txt, f.cent_pai].filter(Boolean).join(' · ')) + '</td></tr>'; });
      html += '</tbody></table>';
    } else if (n.outro_lado_de) {
      var donoF = achar(n.outro_lado_de);
      if (donoF && donoF.no.outro_lado && donoF.no.outro_lado.length) {
        html += '<h3 style="margin-top:8px">' + esc(textoOutro(L.v, 'herdado_ficha', donoF.no.nome)) + '</h3><table class="tabela"><tbody>';
        donoF.no.outro_lado.forEach(function (f) { html += '<tr><td>' + esc(f.nome) + '</td><td class="num">' + esc([f.txt, f.cent_pai].filter(Boolean).join(' · ')) + '</td></tr>'; });
        html += '</tbody></table>';
      }
    }
    (n.nao_alocado || []).forEach(function (x) { html += '<p class="mini">Ajuste sem subfunção: ' + esc(x.nome) + ': ' + esc(x.txt) + '.</p>'; });
    (n.filhos || []).forEach(function (f) { if (f.valor <= 0) html += '<p class="mini">Parte com valor zero ou negativo depois dos ajustes (fica fora do desenho): ' + esc(f.nome) + ': ' + esc(f.txt) + '.</p>'; });
    if (n.nota) html += '<p style="font-size:14px;line-height:1.55;color:#3A423F">' + esc(n.nota) + '</p>';
    if (n.nao_inclui) html += '<p style="font-size:14px;line-height:1.55;color:#3A423F">' + esc(n.nao_inclui) + '</p>';
    if (!filhos.length && n.proximo) html += '<p style="font-size:14px;line-height:1.55;color:#3A423F">' + esc(n.proximo) + '</p>';
    html += '</div>';
    fichaEl.innerHTML = html;
    fichaEl.hidden = false;
    if (fichaVazia) fichaVazia.hidden = true;
    var fechar = document.getElementById('fechar-ficha'); if (fechar) fechar.addEventListener('click', function () { state.sel = null; pintar(); ficha(); });
    var entrar = document.getElementById('entrar-ficha'); if (entrar) entrar.addEventListener('click', function () { abrir({ no: n, lado: a.lado }); });
    var irBtn = document.getElementById('ir-para-ficha'); if (irBtn) irBtn.addEventListener('click', function () { irParaNo(n.ir_para.id); });
  }

  // visões
  document.querySelectorAll('.aba[data-visao]').forEach(function (b) {
    b.addEventListener('click', function () { trocarVisao(b.getAttribute('data-visao')); });
  });
  function trocarVisao(id, manterSel) {
    state.visao = id; state.caminho = []; if (!manterSel) state.sel = null; state.hover = null; tip.hidden = true;
    document.querySelectorAll('.aba[data-visao]').forEach(function (b) { b.setAttribute('aria-pressed', b.getAttribute('data-visao') === id ? 'true' : 'false'); });
    document.querySelectorAll('[data-visao]:not(.aba)').forEach(function (e) { e.hidden = e.getAttribute('data-visao') !== id; });
    desenhar(); ficha();
  }
  document.querySelectorAll('[data-ver-no-desenho]').forEach(function (b) {
    b.addEventListener('click', function () {
      var id = b.getAttribute('data-ver-no-desenho');
      state.sel = id;
      trocarVisao(b.getAttribute('data-visao-alvo') || 'gasto', true);
      var h = document.getElementById('h-fluxo'); if (h && h.scrollIntoView) h.parentNode.scrollIntoView({ behavior: 'smooth', block: 'start' });
    });
  });

  desenhar();
  ficha();
})();
