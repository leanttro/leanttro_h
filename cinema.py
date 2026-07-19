# ════════════════════════════════════════════════════════════
#  cinema.py — Blueprint "Cinema Perto de Mim"
#  Cobre APENAS o conteúdo editorial de filme (em cartaz / streaming),
#  via API oficial da TMDB. O diretório de cinemas físicos (listagem,
#  página de negócio, cadastro) continua 100% no app.py — não há
#  nenhuma tabela/rota nova pra isso, ver app.py (hub_negocios).
#
#  Registro (no fim do app.py, DEPOIS de get_hub_by_host/query/_cache_*
#  estarem definidos):
#
#      from cinema import cinema_bp
#      app.register_blueprint(cinema_bp)
#
#  Variável de ambiente necessária: TMDB_API_KEY (chave v3 "API Key",
#  não o Bearer token v4 — é a mais simples de usar em query string).
# ════════════════════════════════════════════════════════════

from flask import Blueprint, render_template, request, jsonify
import os
import re
import time
import unicodedata
import requests
from datetime import date, timedelta

cinema_bp = Blueprint("cinema", __name__)

TMDB_BASE_URL = "https://api.themoviedb.org/3"
TMDB_IMG_BASE = "https://image.tmdb.org/t/p"

# ── Cache local pras respostas da TMDB ──────────────────────────
# Separado do _CACHE do app.py de propósito: são domínios de dado
# diferentes (negócio local vs. catálogo de filme) e TTLs diferentes
# fazem sentido diferentes. "Em cartaz" e "providers" não mudam de
# minuto em minuto — 30min de TTL já poupa a MAIORIA das chamadas
# repetidas sem deixar o dado sensivelmente desatualizado.
_TMDB_CACHE = {}
_TMDB_CACHE_TTL = 1800  # 30 minutos


def _cache_get(chave):
    item = _TMDB_CACHE.get(chave)
    if item and (time.time() - item[0]) < _TMDB_CACHE_TTL:
        return item[1]
    return None


def _cache_set(chave, valor):
    _TMDB_CACHE[chave] = (time.time(), valor)
    return valor


def _tmdb_get(caminho, params=None):
    """GET autenticado na TMDB, com cache. Retorna (dados, erro).
    `erro` é None em caso de sucesso; string legível em caso de falha
    (chave ausente, timeout, 4xx/5xx) — quem chama decide o que fazer
    (nunca deixa a rota quebrar com 500 só porque a TMDB caiu)."""
    api_key = os.getenv("TMDB_API_KEY")
    if not api_key:
        return None, "TMDB_API_KEY não configurada"

    params = dict(params or {})
    params["api_key"] = api_key
    params.setdefault("language", "pt-BR")

    chave_cache = (caminho, tuple(sorted(params.items())))
    cached = _cache_get(chave_cache)
    if cached is not None:
        return cached, None

    try:
        resp = requests.get(f"{TMDB_BASE_URL}{caminho}", params=params, timeout=8)
        resp.raise_for_status()
        dados = resp.json()
    except requests.RequestException as e:
        return None, str(e)

    return _cache_set(chave_cache, dados), None


def _poster_url(path, tamanho="w500"):
    return f"{TMDB_IMG_BASE}/{tamanho}{path}" if path else None


def _backdrop_url(path, tamanho="w1280"):
    return f"{TMDB_IMG_BASE}/{tamanho}{path}" if path else None


def _enriquecer_filme(filme):
    """Adiciona URLs de imagem prontas pro template — evita montar
    string de URL de imagem dentro do Jinja."""
    filme["poster_url"] = _poster_url(filme.get("poster_path"))
    filme["backdrop_url"] = _backdrop_url(filme.get("backdrop_path"))
    return filme


# ── Provedores de streaming (Brasil) ────────────────────────────
# A vitrine antiga usava /trending/movie/week e só ESCONDIA o ícone de
# provider quando o filme não tinha nenhum — mas o filme continuava
# aparecendo no grid mesmo sendo só "em cartaz" no cinema, sem streaming
# nenhum. A correção: cada provedor (Netflix, Prime...) vira sua própria
# página, usando /discover/movie com with_watch_providers — a própria TMDB
# já filtra só o que está catalogado ali, então nada de filme sem
# streaming entra na lista, e também não precisa mais de 1 chamada extra
# de "watch/providers" por filme (a página carrega bem mais rápido).

def _slugify_provider(nome):
    nome = nome.lower().replace("+", "-plus").replace("&", "e")
    nome = re.sub(r"[^a-z0-9]+", "-", nome).strip("-")
    return nome


def _providers_disponiveis_br():
    """Lista completa de provedores de streaming pra filme no Brasil,
    conforme a própria TMDB (cacheada — essa lista quase não muda)."""
    dados, erro = _tmdb_get("/watch/providers/movie", {"watch_region": "BR"})
    if erro or not dados:
        return []
    provedores = []
    for p in dados.get("results", []):
        provedores.append({
            "id": p["provider_id"],
            "nome": p["provider_name"],
            "logo_url": _poster_url(p.get("logo_path"), "w92"),
            "slug": _slugify_provider(p["provider_name"]),
        })
    return provedores


# Curadoria dos provedores mais relevantes pro público daqui — evita
# misturar com serviço de aluguel avulso (Google Play Movies, compra
# avulsa etc.) que a TMDB também lista junto.
#
# Antes isso casava por SLUG EXATO (ex.: "apple-tv-plus"), gerado a partir
# do nome que a TMDB devolve — mas a TMDB pode grafar o nome de um jeito
# ligeiramente diferente do esperado (acento, "+", espaço extra...), e aí
# o slug batia errado e o provedor sumia da lista mesmo estando disponível
# de verdade (foi o que aconteceu com o Apple TV+). Agora casa por
# "o nome do provedor CONTÉM esse termo" (sem acento/maiúscula, com borda
# de palavra pra não confundir "Max" com "IMAX") — bem mais tolerante a
# variação de grafia. A ORDEM da lista abaixo é a ordem de exibição no site;
# pra adicionar um provedor novo, só incluir um termo que apareça no nome
# dele.
ALIASES_PROVEDORES_PRINCIPAIS = [
    ["netflix"],
    ["prime video"],
    ["disney plus", "disney+", "disney"],
    ["max", "hbo max"],
    ["apple tv"],
    ["paramount"],
    ["globoplay"],
    ["mubi"],
    ["tela brasil"],
]


def _provedores_curados():
    """Escolhe, na lista completa que a TMDB devolve, o 1º provedor cujo
    nome bate com cada termo de ALIASES_PROVEDORES_PRINCIPAIS, respeitando
    a ordem definida ali. Provedor que a TMDB não tem catalogado pra filme
    no Brasil simplesmente não aparece — isso é limitação de dado da
    própria TMDB, não tem como forçar no nosso código."""
    todos = _providers_disponiveis_br()
    escolhidos = []
    usados = set()
    for aliases in ALIASES_PROVEDORES_PRINCIPAIS:
        termos = [_normalizar_busca(a) for a in aliases]
        for p in todos:
            if p["slug"] in usados:
                continue
            nome_norm = _normalizar_busca(p["nome"])
            if any(re.search(r"\b" + re.escape(t) + r"\b", nome_norm) for t in termos):
                escolhidos.append(p)
                usados.add(p["slug"])
                break
    return escolhidos


def _provider_por_slug(slug):
    for p in _providers_disponiveis_br():
        if p["slug"] == slug:
            return p
    return None


def _normalizar_busca(txt):
    """minúsculo + sem acento, pra 'sao paulo' bater com 'São Paulo' etc."""
    txt = (txt or "").strip().lower()
    txt = unicodedata.normalize("NFKD", txt)
    return "".join(c for c in txt if not unicodedata.combining(c))


# ════════════════════════════════════════════════════════════
#  /em-cartaz — filmes em exibição no Brasil agora
# ════════════════════════════════════════════════════════════

@cinema_bp.route("/em-cartaz")
def em_cartaz():
    from app import get_hub_by_host  # import tardio: evita ciclo de import com app.py
    hub = get_hub_by_host()
    if not hub:
        return "Hub não encontrado", 404

    try:
        page = max(int(request.args.get("page", 1)), 1)
    except (TypeError, ValueError):
        page = 1

    # /movie/now_playing é impreciso pro Brasil: mistura relançamentos e
    # títulos de catálogo que a TMDB marcou como "now playing" em algum
    # momento, mesmo sem estarem mais em cartaz de verdade (ex.: sagas
    # antigas voltando pra reprise pontual). A própria TMDB recomenda, como
    # alternativa mais confiável, usar /discover/movie filtrando por tipo de
    # lançamento teatral (2=limitado, 3=amplo) dentro de uma janela de data
    # recente — assim só entra o que de fato estreou/está em exibição
    # nos últimos ~45 dias, e não o catálogo inteiro já lançado algum dia.
    hoje = date.today()
    params = {
        "region": "BR",
        "with_release_type": "2|3",
        "primary_release_date.gte": (hoje - timedelta(days=45)).isoformat(),
        "primary_release_date.lte": hoje.isoformat(),
        "sort_by": "popularity.desc",
        "include_adult": "false",
        "page": page,
    }
    dados, erro = _tmdb_get("/discover/movie", params)
    if erro or not dados:
        return render_template("cinema/erro.html", hub=hub,
                               mensagem="Não foi possível carregar os filmes em cartaz agora. Tenta de novo em alguns minutos."), 502

    filmes = [_enriquecer_filme(f) for f in dados.get("results", [])]
    return render_template(
        "cinema/em_cartaz.html",
        hub=hub, filmes=filmes,
        page=page, total_pages=min(dados.get("total_pages", 1), 500),  # a própria TMDB limita paginação a 500
    )


# ════════════════════════════════════════════════════════════
#  /filme/<tmdb_id> — detalhe + onde assistir
# ════════════════════════════════════════════════════════════

@cinema_bp.route("/filme/<int:tmdb_id>")
def filme_detalhe(tmdb_id):
    from app import get_hub_by_host
    hub = get_hub_by_host()
    if not hub:
        return "Hub não encontrado", 404

    filme, erro = _tmdb_get(f"/movie/{tmdb_id}")
    if erro or not filme or filme.get("success") is False:
        return "Filme não encontrado", 404
    _enriquecer_filme(filme)

    providers_dados, _ = _tmdb_get(f"/movie/{tmdb_id}/watch/providers")
    # A chave "link" é uma URL da própria TMDB (não da JustWatch) que já
    # resolve pro provider certo — é a alternativa oficial ao deep link
    # que a API não fornece. Ver nota no topo do arquivo.
    providers_br = (providers_dados or {}).get("results", {}).get("BR", {})

    return render_template(
        "cinema/filme_detalhe.html",
        hub=hub, filme=filme, providers=providers_br,
        img_base=TMDB_IMG_BASE,
    )


# ════════════════════════════════════════════════════════════
#  /nao-quer-sair-de-casa — índice dos serviços de streaming
# ════════════════════════════════════════════════════════════

@cinema_bp.route("/nao-quer-sair-de-casa")
def nao_quer_sair_de_casa_index():
    from app import get_hub_by_host  # import tardio: evita ciclo de import com app.py
    hub = get_hub_by_host()
    if not hub:
        return "Hub não encontrado", 404

    provedores = _provedores_curados()

    return render_template("cinema/streaming_index.html", hub=hub, provedores=provedores)


# ════════════════════════════════════════════════════════════
#  /nao-quer-sair-de-casa/<slug> — vitrine de UM streaming só
#  (só filme que está catalogado ali de verdade, direto da TMDB)
# ════════════════════════════════════════════════════════════

@cinema_bp.route("/nao-quer-sair-de-casa/<slug>")
def nao_quer_sair_de_casa_provedor(slug):
    from app import get_hub_by_host
    hub = get_hub_by_host()
    if not hub:
        return "Hub não encontrado", 404

    provedor = _provider_por_slug(slug)
    if not provedor:
        return render_template("cinema/erro.html", hub=hub,
                               mensagem="Esse serviço de streaming não foi encontrado."), 404

    # Antes: 1 página da TMDB (20 filmes) + link "Próxima" (?page=N). Trocado
    # por scroll infinito — carrega já de cara 5 páginas da TMDB (~100 filmes,
    # não pesa: cada página é uma chamada cacheada por 30min) e o resto do
    # catálogo entra sozinho conforme a pessoa rola a tela, via
    # /api/cinema/streaming/<slug>?page=N chamado pelo JS no fim do template.
    PAGINAS_INICIAIS = 5
    filmes = []
    total_paginas_tmdb = 1
    for pagina in range(1, PAGINAS_INICIAIS + 1):
        params = {
            "watch_region": "BR",
            "with_watch_providers": provedor["id"],
            "sort_by": "popularity.desc",
            "include_adult": "false",
            "page": pagina,
        }
        dados, erro = _tmdb_get("/discover/movie", params)
        if erro or not dados:
            break
        total_paginas_tmdb = min(dados.get("total_pages", 1), 500)
        filmes.extend(_enriquecer_filme(f) for f in dados.get("results", []))
        if pagina >= total_paginas_tmdb:
            break

    if not filmes and total_paginas_tmdb <= 1:
        return render_template("cinema/erro.html", hub=hub,
                               mensagem=f"Não foi possível carregar o catálogo de {provedor['nome']} agora."), 502

    return render_template(
        "cinema/vitrine_streaming.html",
        hub=hub, filmes=filmes, provedor=provedor,
        proxima_pagina_tmdb=PAGINAS_INICIAIS + 1,
        tem_mais=(PAGINAS_INICIAIS < total_paginas_tmdb),
        img_base=TMDB_IMG_BASE,
    )


# ════════════════════════════════════════════════════════════
#  API JSON — usadas pelos carrosséis da home (index_cinema).
#  O index NÃO renderiza filme via Jinja: ele busca essas rotas
#  via fetch() e monta os cards em JS (ver <script> no fim do
#  index_cinema__*.html). Sem essas rotas os carrosséis ficam
#  vazios pra sempre (cai direto no .catch() do fetch).
# ════════════════════════════════════════════════════════════

@cinema_bp.route("/api/cinema/em-cartaz")
def api_em_cartaz():
    """Mesmos filmes de /em-cartaz/, só que em JSON e sem paginação
    (a home mostra só a 1ª leva, o botão 'Ver todos' leva pra página
    completa com paginação de verdade)."""
    hoje = date.today()
    params = {
        "region": "BR",
        "with_release_type": "2|3",
        "primary_release_date.gte": (hoje - timedelta(days=45)).isoformat(),
        "primary_release_date.lte": hoje.isoformat(),
        "sort_by": "popularity.desc",
        "include_adult": "false",
        "page": 1,
    }
    dados, erro = _tmdb_get("/discover/movie", params)
    if erro or not dados:
        return jsonify(filmes=[]), 200

    filmes = [_enriquecer_filme(f) for f in dados.get("results", [])]
    return jsonify(filmes=filmes)


@cinema_bp.route("/api/cinema/streaming/provedores")
def api_streaming_provedores():
    """Lista dos provedores curados (mesma lista de /nao-quer-sair-de-casa),
    em JSON, pro filtro/slicer da home montar os chips dinamicamente."""
    provedores = _provedores_curados()
    return jsonify(provedores=provedores)


@cinema_bp.route("/api/cinema/streaming/<slug>")
def api_streaming_provedor(slug):
    """Mesmos filmes de /nao-quer-sair-de-casa/<slug>/, em JSON.
    Sem ?page= -> devolve a 1ª página (usado pelo carrossel da home).
    Com ?page=N -> usado pelo scroll infinito da vitrine, que vai chamando
    página a página (a partir da 6, já que a vitrine carrega 1-5 de cara)."""
    provedor = _provider_por_slug(slug)
    if not provedor:
        return jsonify(filmes=[], tem_mais=False), 404

    try:
        pagina = max(int(request.args.get("page", 1)), 1)
    except (TypeError, ValueError):
        pagina = 1

    params = {
        "watch_region": "BR",
        "with_watch_providers": provedor["id"],
        "sort_by": "popularity.desc",
        "include_adult": "false",
        "page": pagina,
    }
    dados, erro = _tmdb_get("/discover/movie", params)
    if erro or not dados:
        return jsonify(filmes=[], tem_mais=False), 200

    total_paginas_tmdb = min(dados.get("total_pages", 1), 500)
    filmes = [_enriquecer_filme(f) for f in dados.get("results", [])]
    return jsonify(filmes=filmes, tem_mais=(pagina < total_paginas_tmdb))


# Limites da busca: a TMDB não tem um endpoint que combine "busca por texto"
# + "filtro por provedor" ao mesmo tempo (o /search/movie não aceita
# with_watch_providers). Então a busca varre o catálogo do provedor
# página por página (20 filmes cada, na ordem de popularidade) e filtra
# pelo título no servidor. Cada página já fica cacheada por 30min (mesmo
# cache do resto do site), então buscas repetidas do mesmo provedor ficam
# rápidas depois da primeira. O limite de páginas é só um teto de
# segurança pra não ficar varrendo um catálogo gigante numa busca só —
# na prática cobre bem mais filme do que qualquer usuário rolaria a mão.
_BUSCA_LIMITE_PAGINAS = 25   # até ~500 filmes do catálogo do provedor
_BUSCA_LIMITE_RESULTADOS = 60


@cinema_bp.route("/api/cinema/streaming/<slug>/buscar")
def api_streaming_buscar(slug):
    provedor = _provider_por_slug(slug)
    if not provedor:
        return jsonify(filmes=[], truncado=False), 404

    termo = _normalizar_busca(request.args.get("q", ""))
    if not termo:
        return jsonify(filmes=[], truncado=False)

    encontrados = []
    pagina = 1
    total_paginas_tmdb = 1
    while pagina <= _BUSCA_LIMITE_PAGINAS and pagina <= total_paginas_tmdb and len(encontrados) < _BUSCA_LIMITE_RESULTADOS:
        params = {
            "watch_region": "BR",
            "with_watch_providers": provedor["id"],
            "sort_by": "popularity.desc",
            "include_adult": "false",
            "page": pagina,
        }
        dados, erro = _tmdb_get("/discover/movie", params)
        if erro or not dados:
            break
        total_paginas_tmdb = min(dados.get("total_pages", 1), 500)
        for f in dados.get("results", []):
            if termo in _normalizar_busca(f.get("title", "")):
                encontrados.append(_enriquecer_filme(f))
                if len(encontrados) >= _BUSCA_LIMITE_RESULTADOS:
                    break
        pagina += 1

    # "truncado" = ainda tinha catálogo pra escanear quando a busca parou
    # (bateu no teto de páginas ou de resultados) — o front pode avisar o
    # usuário que são os resultados mais populares, não literalmente 100%.
    truncado = pagina <= total_paginas_tmdb
    return jsonify(filmes=encontrados, truncado=truncado)
