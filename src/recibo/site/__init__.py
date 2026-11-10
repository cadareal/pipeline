"""Gerador do site estático (`recibo site`): lê data/export/*.json e escreve páginas HTML em docs/.

Toda formatação de número acontece aqui, em Python, uma vez só (dados.py); o HTML e o JavaScript do desenho
consomem as mesmas strings prontas. O JavaScript (static/fluxo.js) só desenha e troca de visão: não faz conta.
"""
