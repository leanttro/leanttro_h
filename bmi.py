# ════════════════════════════════════════════════════════════
#  bmi.py — Blueprint "Adult BMI Calculator"
#  Cobre APENAS as páginas de país/cidade (calculadora + diretório
#  de academias). A calculadora da home e do perfil de academia
#  usam os templates genéricos do hub (index_bmi.html / negocio_bmi.html)
#  e NÃO passam por aqui — não precisam de rota nova nenhuma.
#
#  Convenção combinada (gambiarra proposital, documentada aqui pra
#  não confundir daqui a 6 meses):
#      hub_negocios.cidade  guarda o PAÍS   ("Australia", "United States")
#      hub_negocios.bairro  guarda a CIDADE de verdade ("Sydney", "Melbourne")
#  Isso é só pra este hub do BMI. Os outros hubs (lotérica, cestas etc.)
#  continuam usando cidade/bairro do jeito normal — não muda nada pra eles.
#
#  Registro (no fim do app.py, DEPOIS de get_hub_by_host/query/_resolve_*
#  estarem definidos — mesmo padrão do loteria_bp e cinema_bp):
#
#      from bmi import bmi_bp
#      app.register_blueprint(bmi_bp)
#
#  Nenhuma tabela nova. Usa o MESMO banco e as MESMAS tabelas
#  (hub_negocios, hub_categorias) que os outros hubs já usam,
#  só filtrado pelo hub_id do BMI (criado via /admin/hubs/novo,
#  igual qualquer outro hub).
# ════════════════════════════════════════════════════════════

from flask import Blueprint, render_template

# Import "tardio" de propósito: como este arquivo só é importado no
# FIM do app.py (depois dessas funções já estarem definidas), isso
# funciona sem import circular — mesmo padrão que loteria.py já usa.
from app import (
    query,
    get_hub_by_host,
    _resolve_cidade,
    _resolve_bairro,
    _get_anuncios,
    CATEGORIAS_SEM_GEOLOCALIZACAO,
)

bmi_bp = Blueprint("bmi", __name__)


# ── Tabelas de classificação por padrão ─────────────────────────
# ⚠️ VALORES DE EXEMPLO — confirme os números oficiais em cada fonte
# antes de publicar (who.int, cdc.gov, nhs.uk). É uma ferramenta de
# saúde pública, não vale a pena arriscar número errado.
# As faixas numéricas do adulto (18.5 / 25 / 30) são, na prática,
# as mesmas entre OMS e CDC — a diferença real de país costuma estar
# na metodologia pra criança/adolescente (percentil por idade) e em
# orientações adicionais (ex: NHS tem alerta de risco mais cedo pra
# etnia sul-asiática). Ajuste o texto/copy de cada `standard` pra
# refletir isso, não invente limiares diferentes sem checar a fonte.
STANDARDS = {
    "who": {
        "nome": "WHO (World Health Organization)",
        "faixas": [
            {"min": None, "max": 18.5, "label": "Underweight"},
            {"min": 18.5, "max": 25.0, "label": "Normal weight"},
            {"min": 25.0, "max": 30.0, "label": "Overweight"},
            {"min": 30.0, "max": None, "label": "Obese"},
        ],
    },
    "us_cdc": {
        "nome": "CDC (United States)",
        "faixas": [
            {"min": None, "max": 18.5, "label": "Underweight"},
            {"min": 18.5, "max": 25.0, "label": "Healthy weight"},
            {"min": 25.0, "max": 30.0, "label": "Overweight"},
            {"min": 30.0, "max": None, "label": "Obesity"},
        ],
    },
    "uk_nhs": {
        "nome": "NHS (United Kingdom)",
        "faixas": [
            {"min": None, "max": 18.5, "label": "Underweight"},
            {"min": 18.5, "max": 25.0, "label": "Healthy weight"},
            {"min": 25.0, "max": 30.0, "label": "Overweight"},
            {"min": 30.0, "max": None, "label": "Obese"},
        ],
    },
}

# Slug da URL -> nome de exibição + qual tabela de STANDARDS usar.
# Adicione um país novo aqui SEMPRE que cadastrar academias dele —
# se o slug não estiver aqui, a página de país devolve 404 mesmo que
# já existam negócios cadastrados com aquele país no banco (proteção
# de propósito: evita página de país "fantasma" sem copy/SEO revisado).
COUNTRIES = {
    "australia": {"nome": "Australia", "standard": "who"},
    "united-states": {"nome": "United States", "standard": "us_cdc"},
    "united-kingdom": {"nome": "United Kingdom", "standard": "uk_nhs"},
}


@bmi_bp.app_context_processor
def _injetar_countries_lancados():
    """Disponibiliza a lista de países OFICIALMENTE lançados (chave do dict
    COUNTRIES) pra QUALQUER template do hub (index_bmi.html, negocio_bmi.html,
    bmi/country.html, bmi/city.html, blog_bmi.html etc.) — não só pras rotas
    deste blueprint. Existe porque a lista antiga (derivada de `negocios`)
    só mostrava país que já tinha academia cadastrada, escondendo páginas de
    país que já existem e são válidas (/country/<slug>/ funciona mesmo sem
    negócio nenhum, ver pagina_country abaixo) — o que prejudicava o link
    interno pra elas antes do diretório estar populado.
    Uso no template: {% for country in countries_launched %} ... {{ country.nome }}
    / {{ country.slug }} ... {% endfor %} — não precisa mais do filtro
    `slugify` em cima do nome de exibição, o slug já vem pronto e garantido
    igual ao que o backend espera em COUNTRIES.
    """
    return {
        "countries_launched": [
            {"slug": slug, "nome": info["nome"]}
            for slug, info in COUNTRIES.items()
        ]
    }


def _montar_negocios(hub_id, cidade_variantes, bairro_variantes=None, limite=200):
    """cidade_variantes aqui = variantes do PAÍS; bairro_variantes = variantes da CIDADE.
    Nomes de parâmetro seguem o significado real (país/cidade), não o nome de coluna
    do banco, pra não confundir quem for mexer aqui depois."""
    sql = """
        SELECT n.*, c.nome as categoria_nome, c.slug as categoria_slug
        FROM hub_negocios n
        JOIN hub_negocio_hubs nh ON nh.negocio_id = n.id
        LEFT JOIN hub_categorias c ON c.id = n.categoria_id
        WHERE nh.hub_id = %s AND n.ativo = true AND n.cidade = ANY(%s)
    """
    params = [hub_id, cidade_variantes]
    if bairro_variantes:
        sql += " AND n.bairro = ANY(%s)"
        params.append(bairro_variantes)
    sql += " ORDER BY n.nome LIMIT %s"
    params.append(limite)
    negocios = query(sql, params)
    for n in negocios:
        n["exibir_geo"] = n["categoria_slug"] not in CATEGORIAS_SEM_GEOLOCALIZACAO
    return negocios


@bmi_bp.route("/country/<pais_slug>/")
def pagina_country(pais_slug):
    hub = get_hub_by_host()
    if not hub:
        return "Hub não encontrado", 404

    pais_info = COUNTRIES.get(pais_slug)
    if not pais_info:
        return "País não encontrado", 404
    standard = STANDARDS.get(pais_info["standard"])

    # "cidade" aqui, no banco, é o país (ver convenção no topo do arquivo)
    pais_nome_canonico, pais_variantes = _resolve_cidade(hub["id"], pais_slug)

    negocios, total_negocios, cidades_disponiveis = [], 0, []
    if pais_nome_canonico:
        negocios = _montar_negocios(hub["id"], pais_variantes, limite=60)
        total_negocios = query("""
            SELECT COUNT(*) as total FROM hub_negocios n
            JOIN hub_negocio_hubs nh ON nh.negocio_id = n.id
            WHERE nh.hub_id = %s AND n.ativo = true AND n.cidade = ANY(%s)
        """, (hub["id"], pais_variantes), one=True)["total"]
        rows = query("""
            SELECT DISTINCT n.bairro FROM hub_negocios n
            JOIN hub_negocio_hubs nh ON nh.negocio_id = n.id
            WHERE nh.hub_id = %s AND n.ativo = true AND n.cidade = ANY(%s)
              AND n.bairro IS NOT NULL AND n.bairro <> ''
            ORDER BY n.bairro
        """, (hub["id"], pais_variantes))
        cidades_disponiveis = [r["bairro"] for r in rows]

    anuncio_topo = _get_anuncios(hub["id"], "topo")
    anuncio_meio = _get_anuncios(hub["id"], "meio")

    return render_template(
        "bmi/country.html",
        hub=hub,
        pais_slug=pais_slug,
        pais_nome=pais_info["nome"],
        standard=standard,
        negocios=negocios,
        total_negocios=total_negocios,
        cidades_disponiveis=cidades_disponiveis,
        anuncio_topo=anuncio_topo,
        anuncio_meio=anuncio_meio,
    )


@bmi_bp.route("/country/<pais_slug>/<cidade_slug>/")
def pagina_country_cidade(pais_slug, cidade_slug):
    hub = get_hub_by_host()
    if not hub:
        return "Hub não encontrado", 404

    pais_info = COUNTRIES.get(pais_slug)
    if not pais_info:
        return "País não encontrado", 404
    standard = STANDARDS.get(pais_info["standard"])

    pais_nome_canonico, pais_variantes = _resolve_cidade(hub["id"], pais_slug)
    if not pais_nome_canonico:
        return "País ainda sem academias cadastradas", 404

    # "bairro" aqui, no banco, é a cidade de verdade (ver convenção no topo)
    cidade_nome_canonico, cidade_variantes = _resolve_bairro(
        hub["id"], cidade_slug, cidade_variantes=pais_variantes
    )
    if not cidade_nome_canonico:
        return "Cidade não encontrada", 404

    negocios = _montar_negocios(hub["id"], pais_variantes, cidade_variantes, limite=200)

    anuncio_topo = _get_anuncios(hub["id"], "topo", cidade=cidade_nome_canonico)
    anuncio_meio = _get_anuncios(hub["id"], "meio", cidade=cidade_nome_canonico)

    return render_template(
        "bmi/city.html",
        hub=hub,
        pais_slug=pais_slug,
        pais_nome=pais_info["nome"],
        cidade_nome=cidade_nome_canonico,
        standard=standard,
        negocios=negocios,
        total_negocios=len(negocios),
        anuncio_topo=anuncio_topo,
        anuncio_meio=anuncio_meio,
    )
