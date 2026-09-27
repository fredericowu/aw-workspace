# -*- coding: utf-8 -*-
"""The test docs/design/aw-knowledgeable-v2-retrieval.md §3 says the 12-query
tie structurally could NOT run: a Portuguese set whose answers sit BEYOND 128
tokens into the passage.

Design: all 12 passages share a near-identical generic Portuguese preamble
(>128 tokens on its own), so the discriminating fact lives ONLY in the tail.
A model whose window ends at 128 tokens therefore cannot see any discriminator
and must score at chance (1/12). Queries paraphrase the tail fact and share no
distinctive substring with it, per the original harness's discipline.
"""
PREAMBLE = (
    "Este documento integra o conjunto de normas internas de funcionamento "
    "aprovadas pela direcção e aplica-se a todas as unidades orgânicas da "
    "instituição. As disposições aqui reunidas devem ser lidas em articulação "
    "com o regulamento geral e com as orientações emitidas anualmente pelos "
    "serviços centrais. Compete aos responsáveis de cada unidade assegurar a "
    "divulgação do presente texto junto de todos os colaboradores, bem como "
    "garantir que as práticas correntes se conformam ao que nele se estabelece. "
    "Quaisquer dúvidas de interpretação são submetidas ao gabinete jurídico, "
    "que emite parecer no prazo habitual. As alterações ao presente documento "
    "seguem o procedimento normal de revisão, com registo da versão e da data "
    "de entrada em vigor. Considera-se que o incumprimento reiterado das "
    "disposições constantes deste texto constitui matéria passível de "
    "apreciação disciplinar, nos termos previstos na legislação aplicável e "
    "nos instrumentos de regulamentação colectiva de trabalho em vigor. "
    "Para efeitos do disposto nos números anteriores, entende-se por unidade "
    "orgânica qualquer estrutura dotada de autonomia funcional reconhecida. "
)
TAILS = [
 ("ferias",    "No que respeita ao gozo de ferias, o periodo anual e marcado ate ao dia 15 de Marco de cada ano, e o seu gozo interpolado depende de acordo escrito entre as partes."),
 ("teletrabalho", "O regime de prestacao de actividade fora das instalacoes exige parecer previo favoravel da chefia directa e nao pode exceder tres dias por semana."),
 ("viaturas",  "A utilizacao de veiculos da frota para deslocacoes ao servico obriga ao registo de quilometros no inicio e no fim de cada percurso, em formulario proprio."),
 ("formacao",  "Cada colaborador tem direito a quarenta horas anuais destinadas ao desenvolvimento de competencias, cumulaveis por um periodo maximo de dois anos."),
 ("compras",   "A aquisicao de bens de valor superior a cinco mil euros exige tres propostas concorrentes e visto do responsavel financeiro antes da encomenda."),
 ("dados",     "O tratamento de informacao pessoal de terceiros so pode ocorrer em equipamento cifrado e o respectivo acesso e revisto semestralmente."),
 ("seguranca", "A comunicacao de qualquer incidente que envolva credenciais de acesso deve ser feita no prazo de vinte e quatro horas ao responsavel de sistemas."),
 ("ajudas",    "O reembolso de despesas de alojamento em territorio nacional esta limitado a sessenta euros por noite, salvo autorizacao expressa em contrario."),
 ("arquivo",   "Os documentos de natureza contabilistica sao conservados por um periodo de dez anos, findo o qual se procede a destruicao mediante auto proprio."),
 ("parental",  "A dispensa para acompanhamento de filho menor pode ser repartida entre ambos os progenitores, mediante apresentacao de declaracao conjunta."),
 ("propriedade","Os resultados obtidos no ambito de projectos financiados pertencem a instituicao, ressalvado o direito dos autores a mencao da sua contribuicao."),
 ("estagios",  "A admissao de estagiarios nao remunerados esta vedada, devendo toda a colaboracao ser objecto de contrato com contrapartida financeira."),
]
QUERIES = [  # paraphrases; no distinctive substring shared with the tail
 ("ferias",    "Qual e o prazo limite para marcar o descanso anual?"),
 ("teletrabalho", "Quantos dias por semana posso trabalhar a partir de casa?"),
 ("viaturas",  "Preciso de anotar a distancia percorrida quando uso um carro da empresa?"),
 ("formacao",  "Quantas horas de aprendizagem tenho por ano e posso acumular?"),
 ("compras",   "A partir de que montante e necessario pedir varios orcamentos?"),
 ("dados",     "Posso guardar informacao de clientes num portatil sem protecao?"),
 ("seguranca", "Em quanto tempo tenho de avisar se a minha password for comprometida?"),
 ("ajudas",    "Qual o valor maximo que me pagam por uma dormida em Portugal?"),
 ("arquivo",   "Durante quantos anos e preciso guardar as facturas?"),
 ("parental",  "Os dois pais podem dividir a licenca para cuidar de uma crianca?"),
 ("propriedade","Quem fica com a titularidade do que se inventa num projecto com financiamento?"),
 ("estagios",  "E permitido receber alguem para estagiar sem lhe pagar nada?"),
]
def docs():
    # tail placed AFTER the full preamble -> beyond token 128 by construction
    return [(k, PREAMBLE + t) for k, t in TAILS]
