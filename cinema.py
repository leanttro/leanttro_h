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

from flask import Blueprint, render_template, request, jsonify, Response
import os
import re
import time
import threading
import unicodedata
import requests
from datetime import date, timedelta, datetime, timezone

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
    filme["slug"] = _registrar_slug(filme)
    return filme


# ════════════════════════════════════════════════════════════
#  Avaliações (1 a 5 estrelas) — filme e cinema (negócio)
#
#  Tabela NOVA e isolada (cinema_avaliacoes), criada sozinha pelo próprio
#  código (CREATE TABLE IF NOT EXISTS) — não mexe em hub_negocios nem em
#  nenhuma tabela já existente, e não precisa de migração manual: na
#  primeira chamada de avaliação/consulta depois do deploy, a tabela já
#  aparece. Só CREATE; em código nenhum daqui tem DROP/DELETE/TRUNCATE/
#  ALTER — impossível apagar dado de alguém sem querer.
#
#  Anônimo de propósito: não tem login público no site do cinema, então
#  o "visitante" é um ID aleatório (UUID) que o JS gera e guarda no
#  localStorage do navegador da pessoa — nenhum dado é pedido a ela.
#  Isso trava 1 voto por (filme/cinema, navegador): é uma barreira de
#  "mesma máquina", não uma prova de identidade — trocar de navegador/
#  aba anônima/dispositivo ou limpar o localStorage libera votar de novo,
#  o que é a limitação natural de qualquer sistema sem conta de usuário.
# ════════════════════════════════════════════════════════════

_TABELA_AVALIACOES_CRIADA = False


def _garantir_tabela_avaliacoes():
    """Cria cinema_avaliacoes se ainda não existir. Roda de verdade só na
    1ª vez do processo (flag em memória); depois disso as rotas nem
    tocam nesse SQL de novo. IF NOT EXISTS garante que rodar de novo
    (reinício do processo, deploy novo) nunca dá erro nem mexe no que
    já tem lá."""
    global _TABELA_AVALIACOES_CRIADA
    if _TABELA_AVALIACOES_CRIADA:
        return
    from app import query  # import tardio: mesmo motivo do get_hub_by_host acima
    query("""
        CREATE TABLE IF NOT EXISTS cinema_avaliacoes (
            id SERIAL PRIMARY KEY,
            tipo TEXT NOT NULL CHECK (tipo IN ('filme', 'negocio')),
            referencia TEXT NOT NULL,   -- tmdb_id (filme) ou id do hub_negocios (cinema)
            visitante TEXT NOT NULL,    -- UUID anônimo gerado no navegador
            nota SMALLINT NOT NULL CHECK (nota BETWEEN 1 AND 5),
            criado_em TIMESTAMPTZ NOT NULL DEFAULT now(),
            UNIQUE (tipo, referencia, visitante)
        )
    """, commit=True)
    query("""
        CREATE INDEX IF NOT EXISTS idx_cinema_avaliacoes_busca
        ON cinema_avaliacoes (tipo, referencia)
    """, commit=True)
    _TABELA_AVALIACOES_CRIADA = True


def _resumo_avaliacoes(tipo, referencia, visitante):
    """(media, total, minha_nota) pro par tipo/referencia. minha_nota vem
    None se esse visitante ainda não votou nesse item."""
    from app import query
    linha = query("""
        SELECT COUNT(*) AS total,
               AVG(nota)::numeric(3,2) AS media,
               MAX(nota) FILTER (WHERE visitante = %s) AS minha_nota
        FROM cinema_avaliacoes
        WHERE tipo = %s AND referencia = %s
    """, (visitante, tipo, referencia), one=True)
    return (
        float(linha["media"]) if linha["media"] is not None else None,
        linha["total"],
        linha["minha_nota"],
    )


@cinema_bp.route("/api/cinema/avaliacoes/<tipo>/<referencia>")
def api_cinema_avaliacoes(tipo, referencia):
    """Estado atual das avaliações de um filme/cinema — chamado ao abrir
    a página, pra pintar as estrelas já preenchidas (média) e marcar se
    esse navegador já votou."""
    if tipo not in ("filme", "negocio"):
        return jsonify(erro="tipo inválido"), 400

    _garantir_tabela_avaliacoes()
    visitante = (request.args.get("visitante") or "").strip()[:64]
    media, total, minha_nota = _resumo_avaliacoes(tipo, referencia, visitante)
    return jsonify(media=media, total=total, minha_nota=minha_nota)


@cinema_bp.route("/api/cinema/avaliar", methods=["POST"])
def api_cinema_avaliar():
    """Registra 1 voto (1 a 5 estrelas) de um visitante pra um filme ou
    cinema. ON CONFLICT DO NOTHING: se esse UUID já votou nesse item
    antes, o voto novo é ignorado (não sobrescreve) e a resposta vem com
    ja_votou=true mostrando a nota que já estava registrada."""
    dados = request.get_json(silent=True) or {}
    tipo = (dados.get("tipo") or "").strip()
    referencia = str(dados.get("referencia") or "").strip()[:64]
    visitante = (dados.get("visitante") or "").strip()[:64]
    try:
        nota = int(dados.get("nota"))
    except (TypeError, ValueError):
        nota = None

    if tipo not in ("filme", "negocio") or not referencia or not visitante or nota not in (1, 2, 3, 4, 5):
        return jsonify(erro="Dados inválidos"), 400

    _garantir_tabela_avaliacoes()

    from app import query
    linhas_inseridas = query("""
        INSERT INTO cinema_avaliacoes (tipo, referencia, visitante, nota)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (tipo, referencia, visitante) DO NOTHING
    """, (tipo, referencia, visitante, nota), commit=True)

    media, total, minha_nota = _resumo_avaliacoes(tipo, referencia, visitante)
    return jsonify(
        ja_votou=(linhas_inseridas == 0),
        media=media, total=total, minha_nota=minha_nota,
    )


# Índice slug -> tmdb_id, em memória. Alimentado toda vez que um filme
# passa por qualquer listagem (em cartaz, streaming, busca...), então na
# hora de abrir /filme/<slug> a gente já sabe o id sem precisar dele na
# URL. Não é persistente (reinicia com o processo), mas não precisa ser:
# se der cache miss, _resolver_slug cai pro fallback de busca na TMDB.
_SLUG_INDEX = {}


def _registrar_slug(filme):
    """Calcula o slug do filme e registra no índice. Em caso de dois
    filmes com o mesmo título (remake, franquia repetida...), o segundo
    que aparecer ganha o ano junto pra não pisar na URL do primeiro."""
    base = _slugify(filme.get("title") or filme.get("original_title") or "")
    tmdb_id = filme.get("id")
    existente = _SLUG_INDEX.get(base)
    if existente is not None and existente != tmdb_id:
        ano = (filme.get("release_date") or "")[:4]
        slug = f"{base}-{ano}" if ano else base
    else:
        slug = base
    _SLUG_INDEX[slug] = tmdb_id
    return slug


def _resolver_slug(slug):
    """Slug -> tmdb_id. Primeiro tenta o índice em memória (caso comum:
    filme veio de alguma listagem antes). Se não achar (link direto pra
    um filme que ainda não passou por nenhuma listagem nesse processo),
    busca na TMDB pelo título e confirma pelo slug batendo certinho."""
    tmdb_id = _SLUG_INDEX.get(slug)
    if tmdb_id is not None:
        return tmdb_id

    m = re.match(r"^(.*)-(\d{4})$", slug)
    base, ano = (m.group(1), m.group(2)) if m else (slug, None)
    termo_busca = base.replace("-", " ")

    params = {"query": termo_busca}
    if ano:
        params["year"] = ano
    dados, erro = _tmdb_get("/search/movie", params)
    if erro or not dados:
        return None

    for candidato in dados.get("results", []):
        candidato_slug = _slugify(candidato.get("title") or candidato.get("original_title") or "")
        if candidato_slug == base:
            _SLUG_INDEX[slug] = candidato["id"]
            return candidato["id"]
    return None


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


def _tem_streaming_flatrate_br(tmdb_id):
    """True se o filme tem streaming por assinatura (flatrate) no Brasil
    agora, segundo a TMDB — mesma checagem usada em /filme/<slug> pra
    decidir o que mostrar. Cada chamada fica cacheada 30min, então
    repetir isso pro mesmo filme em páginas/buscas diferentes não pesa
    depois da primeira vez."""
    dados, erro = _tmdb_get(f"/movie/{tmdb_id}/watch/providers")
    if erro or not dados:
        return False
    br = (dados.get("results") or {}).get("BR", {})
    return bool(br.get("flatrate"))


def _sem_streaming(filmes):
    """Tira da lista quem já tem streaming por assinatura no Brasil — em
    cartaz e streaming são categorias mutuamente exclusivas aqui no site
    (ver _params_em_cartaz). Fazemos a checagem filme a filme em vez de
    tentar filtrar isso direto na consulta à TMDB (with_watch_providers
    negado + monetization type combinados têm comportamento inconsistente
    na API deles — já causou a listagem inteira vir vazia)."""
    return [f for f in filmes if not _tem_streaming_flatrate_br(f["id"])]


def _normalizar_busca(txt):
    """minúsculo + sem acento, pra 'sao paulo' bater com 'São Paulo' etc."""
    txt = (txt or "").strip().lower()
    txt = unicodedata.normalize("NFKD", txt)
    return "".join(c for c in txt if not unicodedata.combining(c))


def _slugify(texto):
    """Vira 'Duna: Parte Dois' em 'duna-parte-dois', pra usar na URL do filme."""
    texto = _normalizar_busca(texto)
    texto = re.sub(r"[^a-z0-9]+", "-", texto).strip("-")
    return texto or "filme"


def _params_em_cartaz(pagina):
    """Parâmetros do /discover/movie pra 'em cartaz': lançamento teatral/
    limitado (2|3) nos últimos 45 dias no Brasil. A exclusão de quem já
    tem streaming acontece DEPOIS, com _sem_streaming(), não aqui — ver
    comentário lá."""
    hoje = date.today()
    return {
        "region": "BR",
        "with_release_type": "2|3",
        "primary_release_date.gte": (hoje - timedelta(days=45)).isoformat(),
        "primary_release_date.lte": hoje.isoformat(),
        "sort_by": "popularity.desc",
        "include_adult": "false",
        "page": pagina,
    }


# ════════════════════════════════════════════════════════════
#  /em-cartaz — filmes em exibição no Brasil agora
# ════════════════════════════════════════════════════════════

@cinema_bp.route("/em-cartaz")
def em_cartaz():
    from app import get_hub_by_host  # import tardio: evita ciclo de import com app.py
    hub = get_hub_by_host()
    if not hub:
        return "Hub não encontrado", 404

    # Antes: 1 página da TMDB (20 filmes) + link "Próxima" (?page=N). Trocado
    # por scroll infinito, igual à vitrine de streaming — carrega já de cara
    # PAGINAS_INICIAIS páginas (cada uma cacheada por 30min) e o resto do
    # catálogo dos últimos 45 dias entra sozinho conforme a pessoa rola a
    # tela, via /api/cinema/em-cartaz?page=N chamado pelo JS no fim do
    # template.
    PAGINAS_INICIAIS = 5

    # /movie/now_playing é impreciso pro Brasil: mistura relançamentos e
    # títulos de catálogo que a TMDB marcou como "now playing" em algum
    # momento, mesmo sem estarem mais em cartaz de verdade (ex.: sagas
    # antigas voltando pra reprise pontual). A própria TMDB recomenda, como
    # alternativa mais confiável, usar /discover/movie filtrando por tipo de
    # lançamento teatral (2=limitado, 3=amplo) dentro de uma janela de data
    # recente — assim só entra o que de fato estreou/está em exibição
    # nos últimos ~45 dias, e não o catálogo inteiro já lançado algum dia.
    filmes = []
    total_paginas_tmdb = 1
    houve_erro_tmdb = False
    for pagina in range(1, PAGINAS_INICIAIS + 1):
        dados, erro = _tmdb_get("/discover/movie", _params_em_cartaz(pagina))
        if erro or not dados:
            houve_erro_tmdb = True
            break
        total_paginas_tmdb = min(dados.get("total_pages", 1), 500)
        filmes.extend(_enriquecer_filme(f) for f in dados.get("results", []))
        if pagina >= total_paginas_tmdb:
            break

    if not filmes and houve_erro_tmdb:
        return render_template("cinema/erro.html", hub=hub,
                               mensagem="Não foi possível carregar os filmes em cartaz agora. Tenta de novo em alguns minutos."), 502

    filmes = _sem_streaming(filmes)

    return render_template(
        "cinema/em_cartaz.html",
        hub=hub, filmes=filmes,
        proxima_pagina_tmdb=PAGINAS_INICIAIS + 1,
        tem_mais=(PAGINAS_INICIAIS < total_paginas_tmdb),
    )


# ════════════════════════════════════════════════════════════
#  /filme/<tmdb_id> — detalhe + onde assistir
# ════════════════════════════════════════════════════════════

@cinema_bp.route("/filme/<slug>")
def filme_detalhe(slug):
    from app import get_hub_by_host
    hub = get_hub_by_host()
    if not hub:
        return "Hub não encontrado", 404

    tmdb_id = _resolver_slug(slug)
    if tmdb_id is None:
        return "Filme não encontrado", 404

    filme, erro = _tmdb_get(f"/movie/{tmdb_id}")
    if erro or not filme or filme.get("success") is False:
        return "Filme não encontrado", 404
    _enriquecer_filme(filme)

    providers_dados, _ = _tmdb_get(f"/movie/{tmdb_id}/watch/providers")
    # A chave "link" é uma URL da própria TMDB (não da JustWatch) que já
    # resolve pro provider certo — é a alternativa oficial ao deep link
    # que a API não fornece. Ver nota no topo do arquivo.
    providers_br = (providers_dados or {}).get("results", {}).get("BR", {})

    # Mesma regra de negócio da listagem (_params_em_cartaz): em cartaz e
    # streaming são categorias mutuamente exclusivas aqui — se tem
    # streaming por assinatura no Brasil, a página trata como "filme de
    # streaming" (mostra "Onde assistir", esconde "veja cinemas perto de
    # você"); senão, trata como "filme em cartaz" (o inverso).
    esta_em_streaming = bool(providers_br.get("flatrate"))

    return render_template(
        "cinema/filme_detalhe.html",
        hub=hub, filme=filme, providers=providers_br,
        esta_em_streaming=esta_em_streaming,
        img_base=TMDB_IMG_BASE,
    )


# URLs antigas (/filme/<id> e /filme/<id>/<slug>) continuam existindo só
# pra redirecionar — link já compartilhado ou indexado no Google não
# pode virar 404 do nada.
@cinema_bp.route("/filme/<int:tmdb_id_antigo>")
@cinema_bp.route("/filme/<int:tmdb_id_antigo>/<slug_antigo>")
def filme_detalhe_id_antigo(tmdb_id_antigo, slug_antigo=None):
    from flask import redirect, url_for
    filme, erro = _tmdb_get(f"/movie/{tmdb_id_antigo}")
    if erro or not filme or filme.get("success") is False:
        return "Filme não encontrado", 404
    _enriquecer_filme(filme)
    return redirect(url_for("cinema.filme_detalhe", slug=filme["slug"]), code=301)


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
    """Filmes em cartaz (últimos ~45 dias), em JSON.
    Sem ?page= -> devolve a 1ª página (usado pelo carrossel da home).
    Com ?page=N -> usado pelo scroll infinito de /em-cartaz/, que vai
    chamando página a página (a partir da 6, já que a página carrega
    1-5 de cara)."""
    try:
        pagina = max(int(request.args.get("page", 1)), 1)
    except (TypeError, ValueError):
        pagina = 1

    dados, erro = _tmdb_get("/discover/movie", _params_em_cartaz(pagina))
    if erro or not dados:
        return jsonify(filmes=[], tem_mais=False), 200

    total_paginas_tmdb = min(dados.get("total_pages", 1), 500)
    filmes = _sem_streaming([_enriquecer_filme(f) for f in dados.get("results", [])])
    return jsonify(filmes=filmes, tem_mais=(pagina < total_paginas_tmdb))


@cinema_bp.route("/api/cinema/em-cartaz/buscar")
def api_em_cartaz_buscar():
    """Busca por título dentro de TODO o catálogo em cartaz (últimos ~45
    dias), não só o que já foi carregado na tela — mesmo padrão de
    /api/cinema/streaming/<slug>/buscar (ver comentário dos limites lá em
    baixo, que valem igual aqui)."""
    termo = _normalizar_busca(request.args.get("q", ""))
    if not termo:
        return jsonify(filmes=[], truncado=False)

    encontrados = []
    pagina = 1
    total_paginas_tmdb = 1
    while pagina <= _BUSCA_LIMITE_PAGINAS and pagina <= total_paginas_tmdb and len(encontrados) < _BUSCA_LIMITE_RESULTADOS:
        dados, erro = _tmdb_get("/discover/movie", _params_em_cartaz(pagina))
        if erro or not dados:
            break
        total_paginas_tmdb = min(dados.get("total_pages", 1), 500)
        for f in dados.get("results", []):
            if termo in _normalizar_busca(f.get("title", "")) and not _tem_streaming_flatrate_br(f["id"]):
                encontrados.append(_enriquecer_filme(f))
                if len(encontrados) >= _BUSCA_LIMITE_RESULTADOS:
                    break
        pagina += 1

    truncado = pagina <= total_paginas_tmdb
    return jsonify(filmes=encontrados, truncado=truncado)


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


# ════════════════════════════════════════════════════════════
#  "Ache seu filme" — busca livre na home, integrada à tela 3D
#  (cine-hero). Diferente das buscas acima, essa NÃO fica restrita a
#  em-cartaz nem a um streaming específico: é o /search/movie da TMDB
#  puro, cobre o catálogo inteiro.
# ════════════════════════════════════════════════════════════

@cinema_bp.route("/api/cinema/buscar-filme")
def api_buscar_filme():
    """Autocomplete da busca livre: título + pôster só, pra lista de
    sugestão enquanto a pessoa digita. Sem paginação de propósito — é
    só uma prévia rápida, o clique num resultado busca o detalhe
    completo em /api/cinema/filme/<slug>."""
    termo = (request.args.get("q") or "").strip()
    if not termo:
        return jsonify(filmes=[])

    dados, erro = _tmdb_get("/search/movie", {
        "query": termo,
        "region": "BR",
        "include_adult": "false",
    })
    if erro or not dados:
        return jsonify(filmes=[])

    filmes = [_enriquecer_filme(f) for f in dados.get("results", [])[:8]]
    return jsonify(filmes=filmes)


@cinema_bp.route("/api/cinema/filme/<slug>")
def api_filme_detalhe(slug):
    """Mesmo dado (e mesma regra de negócio) de /filme/<slug>, em JSON —
    usado pela busca "Ache seu filme" da home pra mostrar o filme
    escolhido direto na tela 3D, sem precisar sair da página."""
    tmdb_id = _resolver_slug(slug)
    if tmdb_id is None:
        return jsonify(erro="Filme não encontrado"), 404

    filme, erro = _tmdb_get(f"/movie/{tmdb_id}")
    if erro or not filme or filme.get("success") is False:
        return jsonify(erro="Filme não encontrado"), 404
    _enriquecer_filme(filme)

    providers_dados, _ = _tmdb_get(f"/movie/{tmdb_id}/watch/providers")
    providers_br = (providers_dados or {}).get("results", {}).get("BR", {})
    esta_em_streaming = bool(providers_br.get("flatrate"))

    return jsonify(
        filme={
            "title": filme.get("title"),
            "overview": filme.get("overview"),
            "poster_url": filme.get("poster_url"),
            "backdrop_url": filme.get("backdrop_url"),
            "release_date": filme.get("release_date"),
            "slug": filme.get("slug"),
        },
        esta_em_streaming=esta_em_streaming,
        flatrate=[
            {"nome": p["provider_name"], "logo_url": _poster_url(p.get("logo_path"), "w92")}
            for p in providers_br.get("flatrate", [])
        ],
    )


# ════════════════════════════════════════════════════════════
#  SITEMAP — todo filme que aparece em /em-cartaz ou em algum
#  /nao-quer-sair-de-casa/<provedor> ganha aqui uma <url> pra
#  /filme/<slug>. O /sitemap.xml principal (app.py) só referencia
#  o índice daqui embaixo — quem monta a lista de filmes é este
#  arquivo, não o app.py.
#
#  Por que cacheado por horas (e não por request, como o resto da
#  TMDB aqui): montar essa lista varre página por página o
#  em-cartaz + o catálogo de CADA streaming curado — pode ser
#  bem mais que as ~30-40 chamadas de uma página normal do site.
#  A 1ª geração depois do cache vencer fica mais lenta; as
#  seguintes usam o resultado pronto.
# ════════════════════════════════════════════════════════════

_SITEMAP_CACHE_TTL = 6 * 3600  # 6 horas
_sitemap_filmes_cache = {"gerado_em": 0.0, "filmes": []}


def _eh_hub_cinema(hub):
    """Só o hub de cinema pode gerar/expor esse sitemap — isso aqui é uma
    plataforma multi-nicho (app.py registra vários hub_clientes, cinema é
    só um deles). Reaproveita o mesmo mapa hub_leanttro/domínio -> prefixo
    que o app.py já usa pra decidir categorias por nicho."""
    from app import _PREFIXO_CATEGORIA_POR_HUB, _PREFIXO_CATEGORIA_POR_DOMINIO
    return (
        _PREFIXO_CATEGORIA_POR_HUB.get(hub.get("hub_leanttro")) == "cinema_"
        or _PREFIXO_CATEGORIA_POR_DOMINIO.get(hub.get("dominio_proprio")) == "cinema_"
    )

# Teto de páginas por fonte (em-cartaz, e cada streaming). Reduzido de
# propósito: com em-cartaz + os 9 streamings curados (10 fontes), ir até
# o teto real da TMDB (500 páginas cada) significa milhares de chamadas
# — mesmo em background, isso trava por muito tempo sem necessidade.
# 150 páginas (~3000 filmes) por fonte já cobre folgado o catálogo real
# de qualquer streaming grande no Brasil hoje.
_SITEMAP_MAX_PAGINAS_POR_FONTE = 150


def _coletar_pra_sitemap(params_base):
    """Varre /discover/movie com os params dados, página por página, até
    acabar o catálogo da fonte ou bater o teto de segurança. Cada
    página individual já passa pelo cache de 30min do _tmdb_get, então
    reprocessar isso pouco depois (ex.: 2 fontes que compartilham
    filme) não bate na TMDB de novo."""
    filmes = []
    pagina = 1
    total_paginas = 1
    while pagina <= min(total_paginas, _SITEMAP_MAX_PAGINAS_POR_FONTE):
        dados, erro = _tmdb_get("/discover/movie", dict(params_base, page=pagina))
        if erro or not dados:
            break
        total_paginas = min(dados.get("total_pages", 1), 500)
        filmes.extend(dados.get("results", []))
        pagina += 1
    return filmes


def _construir_indice_sitemap_filmes(forcar=False):
    """Lista completa (sem repetir tmdb_id) de todo filme que tem
    página própria indicável no site: em-cartaz + catálogo de cada
    streaming curado. Cacheada por _SITEMAP_CACHE_TTL — ver comentário
    da seção acima sobre o custo de montar isso do zero.

    IMPORTANTE: essa função é lenta de propósito (varre até 10 fontes x
    150 páginas da TMDB) — NUNCA chamar direto de dentro de uma rota.
    Quem serve o sitemap chama _filmes_sitemap_cache_ou_disparar_build()
    abaixo, que devolve na hora e roda isso em background."""
    agora = time.time()
    if not forcar and (agora - _sitemap_filmes_cache["gerado_em"]) < _SITEMAP_CACHE_TTL:
        return _sitemap_filmes_cache["filmes"]

    vistos = {}

    hoje = date.today()
    params_em_cartaz = {
        "region": "BR",
        "with_release_type": "2|3",
        "primary_release_date.gte": (hoje - timedelta(days=45)).isoformat(),
        "primary_release_date.lte": hoje.isoformat(),
        "sort_by": "popularity.desc",
        "include_adult": "false",
    }
    for f in _coletar_pra_sitemap(params_em_cartaz):
        vistos[f["id"]] = f

    for provedor in _provedores_curados():
        params_provedor = {
            "watch_region": "BR",
            "with_watch_providers": provedor["id"],
            "sort_by": "popularity.desc",
            "include_adult": "false",
        }
        for f in _coletar_pra_sitemap(params_provedor):
            vistos[f["id"]] = f

    filmes = [_enriquecer_filme(f) for f in vistos.values()]
    _sitemap_filmes_cache["filmes"] = filmes
    _sitemap_filmes_cache["gerado_em"] = agora
    return filmes


# ── Build em background: a rota do sitemap NUNCA espera a varredura ──
# _sitemap_building trava com um Lock pra não disparar 2 varreduras ao
# mesmo tempo se 2 requests chegarem juntas com o cache vencido (ex.:
# Google batendo em /sitemap-cinema.xml e /sitemap-filmes-1.xml quase
# junto). É por-processo (não compartilhado entre workers, se houver
# mais de um) — mesmo trade-off que o _TMDB_CACHE já assume hoje.
_sitemap_build_lock = threading.Lock()
_sitemap_building = False


def _disparar_build_sitemap_em_background():
    global _sitemap_building
    with _sitemap_build_lock:
        if _sitemap_building:
            return
        _sitemap_building = True

    def _build():
        global _sitemap_building
        try:
            _construir_indice_sitemap_filmes(forcar=True)
        except Exception:
            pass  # a próxima chamada com cache vencido tenta de novo
        finally:
            with _sitemap_build_lock:
                _sitemap_building = False

    threading.Thread(target=_build, daemon=True, name="sitemap-filmes-build").start()


def _filmes_sitemap_cache_ou_disparar_build():
    """O que toda rota de sitemap de filme deve chamar. NUNCA bloqueia:
    se o cache tiver vencido, dispara a varredura completa em background
    (pode levar alguns minutos) e devolve JÁ o que tiver disponível —
    mesmo que seja a leva anterior ou, logo após o deploy, uma lista
    vazia. Bloquear a resposta esperando a varredura terminar foi
    exatamente o que causou o "Não foi possível ler o sitemap" no
    Search Console: com em-cartaz + 9 streamings, uma varredura do zero
    é lenta demais pra caber no tempo de uma request HTTP."""
    if (time.time() - _sitemap_filmes_cache["gerado_em"]) >= _SITEMAP_CACHE_TTL:
        _disparar_build_sitemap_em_background()
    return _sitemap_filmes_cache["filmes"]


# Dispara a 1ª varredura já na subida do processo (não espera alguém
# bater no sitemap pra começar) — reduz a janela em que /sitemap-filmes-1.xml
# responderia vazio logo depois de um deploy.
_disparar_build_sitemap_em_background()


_SITEMAP_FILMES_POR_PARTE = 5000


@cinema_bp.route("/sitemap-cinema.xml")
def sitemap_cinema_index():
    """Índice dos sitemaps de filme. É essa URL que o /sitemap.xml
    principal do app.py referencia — o app.py não sabe (nem precisa
    saber) quantas partes existem, só aponta pra cá."""
    from app import get_hub_by_host  # import tardio: evita ciclo de import com app.py
    hub = get_hub_by_host()
    if not hub or not _eh_hub_cinema(hub):
        return "Hub não encontrado", 404

    base_url = f"https://{request.host}"
    hoje = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    total = len(_filmes_sitemap_cache_ou_disparar_build())
    num_partes = max(1, -(-total // _SITEMAP_FILMES_POR_PARTE))

    linhas = ['<?xml version="1.0" encoding="UTF-8"?>']
    linhas.append('<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">')
    for i in range(1, num_partes + 1):
        linhas.append("  <sitemap>")
        linhas.append(f"    <loc>{base_url}/sitemap-filmes-{i}.xml</loc>")
        linhas.append(f"    <lastmod>{hoje}</lastmod>")
        linhas.append("  </sitemap>")
    linhas.append("</sitemapindex>")

    return Response("\n".join(linhas), mimetype="application/xml")


@cinema_bp.route("/sitemap-filmes-<int:parte>.xml")
def sitemap_filmes_parte(parte):
    from app import get_hub_by_host  # import tardio: evita ciclo de import com app.py
    hub = get_hub_by_host()
    if not hub or not _eh_hub_cinema(hub):
        return "Hub não encontrado", 404

    base_url = f"https://{request.host}"
    hoje = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    filmes = _filmes_sitemap_cache_ou_disparar_build()
    inicio = (parte - 1) * _SITEMAP_FILMES_POR_PARTE
    pedaco = filmes[inicio:inicio + _SITEMAP_FILMES_POR_PARTE]

    if not pedaco:
        return "Não encontrado", 404

    def fmt_date(val):
        return val[:10] if val else hoje

    def gerar():
        yield '<?xml version="1.0" encoding="UTF-8"?>\n'
        yield '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        for f in pedaco:
            slug = f.get("slug")
            if not slug:
                continue
            yield (
                "  <url>\n"
                f"    <loc>{base_url}/filme/{slug}</loc>\n"
                f"    <lastmod>{fmt_date(f.get('release_date'))}</lastmod>\n"
                "    <changefreq>monthly</changefreq>\n"
                "    <priority>0.6</priority>\n"
                "  </url>\n"
            )
        yield "</urlset>"

    return Response(gerar(), mimetype="application/xml")
