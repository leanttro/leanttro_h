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

from flask import Blueprint, render_template, request
import os
import time
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
#  /nao-quer-sair-de-casa — vitrine de streaming
#  (nome provisório — ver sugestões de alternativa na resposta)
# ════════════════════════════════════════════════════════════

@cinema_bp.route("/nao-quer-sair-de-casa")
def vitrine_streaming():
    from app import get_hub_by_host
    hub = get_hub_by_host()
    if not hub:
        return "Hub não encontrado", 404

    try:
        page = max(int(request.args.get("page", 1)), 1)
    except (TypeError, ValueError):
        page = 1

    # /trending traz filmes em alta (não necessariamente lançamento novo),
    # que é o ângulo certo pra "o que assistir hoje" — diferente de
    # /movie/popular, que tende a ficar preso nos mesmos blockbusters.
    dados, erro = _tmdb_get("/trending/movie/week", {"page": page})
    if erro or not dados:
        return render_template("cinema/erro.html", hub=hub,
                               mensagem="Não foi possível carregar as sugestões agora."), 502

    filmes = [_enriquecer_filme(f) for f in dados.get("results", [])]

    # Busca os providers de cada filme da vitrine em paralelo seria o ideal,
    # mas pra manter o cinema.py simples (sem threadpool) e dentro do cache
    # de 30min, uma chamada sequencial por filme já é aceitável pro volume
    # de uma página (20 filmes). Se a vitrine crescer muito, revisitar isso.
    for f in filmes:
        prov_dados, _ = _tmdb_get(f"/movie/{f['id']}/watch/providers")
        f["providers"] = (prov_dados or {}).get("results", {}).get("BR", {})

    return render_template(
        "cinema/vitrine_streaming.html",
        hub=hub, filmes=filmes, page=page,
        total_pages=min(dados.get("total_pages", 1), 500),
        img_base=TMDB_IMG_BASE,
    )
