from flask import Flask, render_template, request, jsonify, redirect, session, g, Response, stream_with_context, make_response
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.middleware.proxy_fix import ProxyFix
from functools import wraps
from datetime import datetime, timezone, date
import psycopg2
import psycopg2.extras
import os
import glob
import unicodedata as _uc
import time
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY")
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)
app.url_map.strict_slashes = False

DOMINIO_BASE = os.getenv("DOMINIO_BASE", "leanttro.com")

# campos de texto livre que sofrem de grafias inconsistentes (maiúscula/minúscula, acento, espaços)
CAMPOS_NORMALIZAVEIS = {"bairro", "cidade"}

# categorias de negócio que não têm localização física relevante pro usuário
# (ex.: ferramentas de IA são acessadas online, não visitadas num endereço).
# Pra essas, o template mostra um card simplificado em vez de mapa/endereço/
# distância/WhatsApp. Ver campo derivado "exibir_geo" nas rotas públicas.
CATEGORIAS_SEM_GEOLOCALIZACAO = {"ferramenta-de-ia"}

def _chave_normalizada(texto):
    """Normaliza texto pra comparação: sem acento, minúsculo, sem espaço nas pontas/duplicado."""
    if not texto:
        return ""
    sem_acento = "".join(
        c for c in _uc.normalize("NFKD", texto) if not _uc.combining(c)
    )
    return " ".join(sem_acento.strip().lower().split())

# ── Templates dinâmicos ───────────────────────────────────────

def listar_templates(tipo):
    """Retorna os slugs dos templates disponíveis para index/filtro/negocio"""
    pasta = os.path.join(app.root_path, "templates", "hub")
    arquivos = glob.glob(os.path.join(pasta, f"{tipo}_*.html"))
    return [os.path.basename(f).replace(".html", "") for f in sorted(arquivos)]

# ── Banco ─────────────────────────────────────────────────────

def get_db():
    if "db" not in g:
        g.db = psycopg2.connect(
            host     = os.getenv("DB_HOST"),
            port     = int(os.getenv("DB_PORT", 5452)),
            dbname   = os.getenv("DB_NAME"),
            user     = os.getenv("DB_USER"),
            password = os.getenv("DB_PASS"),
            cursor_factory=psycopg2.extras.RealDictCursor
        )
    return g.db

@app.teardown_appcontext
def close_db(e=None):
    db = g.pop("db", None)
    if db:
        db.close()

def query(sql, params=(), one=False, commit=False):
    db = get_db()
    cur = db.cursor()
    cur.execute(sql, params)
    if commit:
        db.commit()
        return cur.rowcount
    result = cur.fetchone() if one else cur.fetchall()
    return result

# ── Cache leve em memória (cidade/bairro/categoria/contagem) ───
# Essas consultas são as mesmas pra QUALQUER visitante da mesma cidade e só mudam
# quando um negócio é cadastrado/editado/aprovado — não precisam rodar a cada clique.
# TTL curto (poucos minutos) já tira a maior parte da carga do banco sem deixar
# os dados visivelmente desatualizados. É limpo automaticamente sempre que um
# negócio é criado/editado/aprovado/apagado (ver _cache_invalidar()).
_CACHE = {}
_CACHE_TTL_SEGUNDOS = 180  # 3 minutos

def _cache_get(chave):
    item = _CACHE.get(chave)
    if item and (time.time() - item[0]) < _CACHE_TTL_SEGUNDOS:
        return item[1]
    return None

def _cache_set(chave, valor):
    _CACHE[chave] = (time.time(), valor)
    return valor

def _cache_invalidar():
    """Chame depois de QUALQUER escrita em hub_negocios (criar/editar/apagar/aprovar/bulk)."""
    _CACHE.clear()

# ── Auth ──────────────────────────────────────────────────────

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("admin_id"):
            return redirect("/admin/login")
        return f(*args, **kwargs)
    return decorated

def is_ajax():
    return request.headers.get("X-Requested-With") == "XMLHttpRequest"

# ── Hub pelo host ─────────────────────────────────────────────

def get_hub_by_host():
    host = request.host.split(":")[0].lower().replace("www.", "")
    hub = query("SELECT * FROM hub_clientes WHERE dominio_proprio = %s AND ativo = true", (host,), one=True)
    if hub:
        return hub
    if host.endswith(f".{DOMINIO_BASE}"):
        slug = host.replace(f".{DOMINIO_BASE}", "")
        hub = query("SELECT * FROM hub_clientes WHERE hub_leanttro = %s AND ativo = true", (slug,), one=True)
    return hub

# ── Slugify ───────────────────────────────────────────────────

def _slugify(texto):
    if not texto:
        return ""
    texto = _uc.normalize("NFD", texto.lower())
    texto = "".join(c for c in texto if _uc.category(c) != "Mn")
    return texto.replace(" ", "-").strip("-")

def _parse_coord(valor):
    """Aceita '-23.6430556' ou '-23,6430556' (vírgula BR); retorna float ou None."""
    if valor is None:
        return None
    valor = str(valor).strip().replace(",", ".")
    if not valor:
        return None
    try:
        return float(valor)
    except ValueError:
        return None

@app.template_filter("slugify")
def _jinja_slugify(texto):
    return _slugify(texto) if texto else ""


# ════════════════════════════════════════════════════════════
#  ANÚNCIOS PAGOS
# ════════════════════════════════════════════════════════════

def _get_anuncios(hub_id, posicao, categoria_slug=None, cidade=None, bairro=None):
    """Retorna até 1 anúncio ativo que case com o contexto da página.
    Prioriza anúncios mais segmentados; desempate por RANDOM().

    `cidades`/`bairros` são arrays: um anúncio sem nada nesses arrays vale pra
    qualquer cidade/bairro; se tiver valores, a página tem que casar com PELO
    MENOS um deles.
    """
    hoje = date.today()
    sql = """
        SELECT * FROM anuncios
        WHERE hub_id = %s AND ativo = true AND posicao = %s
          AND (data_inicio IS NULL OR data_inicio <= %s)
          AND (data_fim   IS NULL OR data_fim   >= %s)
          AND (categoria_slug IS NULL OR categoria_slug = %s)
          AND (
                cidades IS NULL OR cardinality(cidades) = 0
                OR EXISTS (SELECT 1 FROM unnest(cidades) x WHERE LOWER(x) = LOWER(%s))
              )
          AND (
                bairros IS NULL OR cardinality(bairros) = 0
                OR EXISTS (SELECT 1 FROM unnest(bairros) x WHERE LOWER(x) = LOWER(%s))
              )
        ORDER BY
          (categoria_slug IS NOT NULL)::int +
          (cidades IS NOT NULL AND cardinality(cidades) > 0)::int +
          (bairros IS NOT NULL AND cardinality(bairros) > 0)::int DESC,
          RANDOM()
        LIMIT 1
    """
    return query(sql, (
        hub_id, posicao, hoje, hoje,
        categoria_slug or '', cidade or '', bairro or ''
    ), one=True)


# ════════════════════════════════════════════════════════════
#  ROTAS PÚBLICAS DO HUB
# ════════════════════════════════════════════════════════════

# "blog" e "cidade" adicionados para não colidir com /<segmento>/
ROTAS_RESERVADAS = {"admin", "static", "favicon.ico", "robots.txt", "sitemap.xml", "sitemap-1.xml", "sitemap-2.xml", "sitemap-3.xml", "blog", "cidade", "api"}
@app.route("/")
def index():
    hub = get_hub_by_host()
    if not hub:
        return "Hub não encontrado", 404
    categorias = query("SELECT * FROM hub_categorias WHERE ativo = true ORDER BY nome")
    template = hub.get("template_index") or "index_padrao"

    # Templates preparados p/ infinite-scroll (JS busca o resto via
    # /api/hub/negocios?offset=...) carregam só a 1ª leva no HTML.
    # Os demais hubs (volume pequeno/médio, sem esse JS) carregam a lista
    # inteira de uma vez — 5000 aqui é só um teto de segurança, não um
    # limite de negócio real.
    TEMPLATES_INFINITE_SCROLL = {"index_otp"}
    limite = 48 if template in TEMPLATES_INFINITE_SCROLL else 5000

    negocios = query("""
        SELECT n.*, c.nome as categoria_nome, c.slug as categoria_slug
        FROM hub_negocios n
        JOIN hub_negocio_hubs nh ON nh.negocio_id = n.id
        LEFT JOIN hub_categorias c ON c.id = n.categoria_id
        WHERE nh.hub_id = %s AND n.ativo = true
        ORDER BY n.nome
        LIMIT %s
    """, (hub["id"], limite))
    anuncio_topo = _get_anuncios(hub["id"], "topo")
    anuncio_meio = _get_anuncios(hub["id"], "meio")
    return render_template(f"hub/{template}.html", hub=hub, negocios=negocios, categorias=categorias,
                           anuncio_topo=anuncio_topo, anuncio_meio=anuncio_meio)


@app.route("/robots.txt")
def robots():
    hub = get_hub_by_host()
    base_url = f"https://{request.host}"
    linhas = [
        "User-agent: *",
        "Allow: /",
        f"Sitemap: {base_url}/sitemap.xml",
    ]
    if not hub:
        linhas.insert(1, "Disallow: /")
    return Response("\n".join(linhas), mimetype="text/plain")


@app.route("/sitemap.xml")
def sitemap():
    hub = get_hub_by_host()
    if not hub:
        return "Hub não encontrado", 404

    base_url = f"https://{request.host}"
    hoje = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    total = query("""
        SELECT COUNT(*) as total
        FROM hub_negocios n
        JOIN hub_negocio_hubs nh ON nh.negocio_id = n.id
        WHERE nh.hub_id = %s AND n.ativo = true
    """, (hub["id"],), one=True)["total"]

    LIMITE = 5000
    num_partes = max(1, -(-total // LIMITE))

    linhas = ['<?xml version="1.0" encoding="UTF-8"?>']
    linhas.append('<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">')
    for i in range(1, num_partes + 1):
        linhas.append("  <sitemap>")
        linhas.append(f"    <loc>{base_url}/sitemap-{i}.xml</loc>")
        linhas.append(f"    <lastmod>{hoje}</lastmod>")
        linhas.append("  </sitemap>")
    linhas.append("</sitemapindex>")

    return Response("\n".join(linhas), mimetype="application/xml")


@app.route("/sitemap-<int:parte>.xml")
def sitemap_parte(parte):
    hub = get_hub_by_host()
    if not hub:
        return "Hub não encontrado", 404

    base_url = f"https://{request.host}"
    hoje = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    LIMITE = 5000
    offset = (parte - 1) * LIMITE

    negocios = query("""
        SELECT n.slug, n.bairro,
               n.criado_em as atualizado_em,
               c.slug as categoria_slug
        FROM hub_negocios n
        JOIN hub_negocio_hubs nh ON nh.negocio_id = n.id
        LEFT JOIN hub_categorias c ON c.id = n.categoria_id
        WHERE nh.hub_id = %s AND n.ativo = true
        ORDER BY n.nome
        LIMIT %s OFFSET %s
    """, (hub["id"], LIMITE, offset))

    if not negocios:
        return "Não encontrado", 404

    eh_bairro = hub.get("tipo") == "bairro"

    def fmt_date(val):
        if not val:
            return hoje
        if hasattr(val, "strftime"):
            return val.strftime("%Y-%m-%d")
        return str(val)[:10]

    def gerar():
        yield '<?xml version="1.0" encoding="UTF-8"?>\n'
        yield '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'

        if parte == 1:
            yield (
                "  <url>\n"
                f"    <loc>{base_url}/</loc>\n"
                f"    <lastmod>{hoje}</lastmod>\n"
                "    <changefreq>weekly</changefreq>\n"
                "    <priority>1.0</priority>\n"
                "  </url>\n"
            )

            segmentos_vistos = set()
            for n in negocios:
                seg = n["bairro"].lower() if eh_bairro and n["bairro"] else n["categoria_slug"]
                if seg and seg not in segmentos_vistos:
                    segmentos_vistos.add(seg)
                    yield (
                        "  <url>\n"
                        f"    <loc>{base_url}/{seg}/</loc>\n"
                        f"    <lastmod>{hoje}</lastmod>\n"
                        "    <changefreq>weekly</changefreq>\n"
                        "    <priority>0.8</priority>\n"
                        "  </url>\n"
                    )

            posts = query("""
                SELECT p.slug, p.publicado_em
                FROM blog_posts p
                JOIN blog_post_hubs ph ON ph.post_id = p.id
                WHERE ph.hub_id = %s AND p.publicado = true
                ORDER BY p.publicado_em DESC
            """, (hub["id"],))
            for p in posts:
                yield (
                    "  <url>\n"
                    f"    <loc>{base_url}/blog/{p['slug']}/</loc>\n"
                    f"    <lastmod>{fmt_date(p['publicado_em'])}</lastmod>\n"
                    "    <changefreq>monthly</changefreq>\n"
                    "    <priority>0.5</priority>\n"
                    "  </url>\n"
                )

        for n in negocios:
            seg = n["bairro"].lower() if eh_bairro and n["bairro"] else n["categoria_slug"]
            if not seg:
                continue
            yield (
                "  <url>\n"
                f"    <loc>{base_url}/{seg}/{n['slug']}/</loc>\n"
                f"    <lastmod>{fmt_date(n['atualizado_em'])}</lastmod>\n"
                "    <changefreq>monthly</changefreq>\n"
                "    <priority>0.6</priority>\n"
                "  </url>\n"
            )

        yield "</urlset>"

    return Response(stream_with_context(gerar()), mimetype="application/xml")


@app.route("/<segmento>/")
def pagina_filtro(segmento):
    if segmento in ROTAS_RESERVADAS:
        return "Não encontrado", 404
    hub = get_hub_by_host()
    if not hub:
        return "Hub não encontrado", 404

    # Mesma lógica da home: só os templates com JS de infinite-scroll
    # (busca o resto via /api/hub/negocios?offset=...) recebem a página
    # inicial cortada em 48. Os demais carregam a lista inteira do filtro.
    template_filtro_nome = hub.get("template_filtro") or "filtro_padrao"
    limite = 4000

    if hub["tipo"] == "bairro":
        total = query("""
            SELECT COUNT(*) as total FROM hub_negocios n
            JOIN hub_negocio_hubs nh ON nh.negocio_id = n.id
            WHERE nh.hub_id = %s AND n.ativo = true AND LOWER(n.bairro) = LOWER(%s)
        """, (hub["id"], segmento), one=True)["total"]
        negocios = query("""
            SELECT n.*, c.nome as categoria_nome, c.slug as categoria_slug
            FROM hub_negocios n
            JOIN hub_negocio_hubs nh ON nh.negocio_id = n.id
            LEFT JOIN hub_categorias c ON c.id = n.categoria_id
            WHERE nh.hub_id = %s AND n.ativo = true AND LOWER(n.bairro) = LOWER(%s)
            ORDER BY n.nome LIMIT %s
        """, (hub["id"], segmento, limite))
        filtro_tipo, filtro_valor = "bairro", segmento
    elif hub["tipo"] == "cidade":
        categoria = query(
            "SELECT * FROM hub_categorias WHERE slug = %s AND ativo = true",
            (segmento,), one=True
        )
        if categoria:
            total = query("""
                SELECT COUNT(*) as total FROM hub_negocios n
                JOIN hub_negocio_hubs nh ON nh.negocio_id = n.id
                LEFT JOIN hub_categorias c ON c.id = n.categoria_id
                WHERE nh.hub_id = %s AND n.ativo = true AND c.slug = %s
            """, (hub["id"], segmento), one=True)["total"]
            negocios = query("""
                SELECT n.*, c.nome as categoria_nome, c.slug as categoria_slug
                FROM hub_negocios n
                JOIN hub_negocio_hubs nh ON nh.negocio_id = n.id
                LEFT JOIN hub_categorias c ON c.id = n.categoria_id
                WHERE nh.hub_id = %s AND n.ativo = true AND c.slug = %s
                ORDER BY n.nome LIMIT %s
            """, (hub["id"], segmento, limite))
            filtro_tipo, filtro_valor = "categoria", segmento
        else:
            total = query("""
                SELECT COUNT(*) as total FROM hub_negocios n
                JOIN hub_negocio_hubs nh ON nh.negocio_id = n.id
                WHERE nh.hub_id = %s AND n.ativo = true AND LOWER(n.bairro) = LOWER(%s)
            """, (hub["id"], segmento), one=True)["total"]
            negocios = query("""
                SELECT n.*, c.nome as categoria_nome, c.slug as categoria_slug
                FROM hub_negocios n
                JOIN hub_negocio_hubs nh ON nh.negocio_id = n.id
                LEFT JOIN hub_categorias c ON c.id = n.categoria_id
                WHERE nh.hub_id = %s AND n.ativo = true AND LOWER(n.bairro) = LOWER(%s)
                ORDER BY n.nome LIMIT %s
            """, (hub["id"], segmento, limite))
            filtro_tipo, filtro_valor = "bairro", segmento
    else:
        total = query("""
            SELECT COUNT(*) as total FROM hub_negocios n
            JOIN hub_negocio_hubs nh ON nh.negocio_id = n.id
            LEFT JOIN hub_categorias c ON c.id = n.categoria_id
            WHERE nh.hub_id = %s AND n.ativo = true AND c.slug = %s
        """, (hub["id"], segmento), one=True)["total"]
        negocios = query("""
            SELECT n.*, c.nome as categoria_nome, c.slug as categoria_slug
            FROM hub_negocios n
            JOIN hub_negocio_hubs nh ON nh.negocio_id = n.id
            LEFT JOIN hub_categorias c ON c.id = n.categoria_id
            WHERE nh.hub_id = %s AND n.ativo = true AND c.slug = %s
            ORDER BY n.nome LIMIT %s
        """, (hub["id"], segmento, limite))
        filtro_tipo, filtro_valor = "categoria", segmento

    for n in negocios:
        n["exibir_geo"] = n["categoria_slug"] not in CATEGORIAS_SEM_GEOLOCALIZACAO

    categorias = query("""
        SELECT DISTINCT c.* FROM hub_categorias c
        JOIN hub_negocios n ON n.categoria_id = c.id
        JOIN hub_negocio_hubs nh ON nh.negocio_id = n.id
        WHERE nh.hub_id = %s AND c.ativo = true AND n.ativo = true
        ORDER BY c.nome
    """, (hub["id"],))
    # Bairros únicos para o slicer — query leve só com DISTINCT
    bairros_disponiveis = []
    if filtro_tipo == "categoria":
        rows = query("""
            SELECT DISTINCT n.bairro FROM hub_negocios n
            JOIN hub_negocio_hubs nh ON nh.negocio_id = n.id
            LEFT JOIN hub_categorias c ON c.id = n.categoria_id
            WHERE nh.hub_id = %s AND n.ativo = true AND c.slug = %s
              AND n.bairro IS NOT NULL AND n.bairro <> ''
            ORDER BY n.bairro
        """, (hub["id"], segmento))
        bairros_disponiveis = [r["bairro"] for r in rows]
    template = hub.get("template_filtro") or "filtro_padrao"
    anuncio_topo = _get_anuncios(hub["id"], "topo", categoria_slug=(segmento if filtro_tipo == "categoria" else None))
    anuncio_meio = _get_anuncios(hub["id"], "meio", categoria_slug=(segmento if filtro_tipo == "categoria" else None))
    return render_template(f"hub/{template}.html", hub=hub, negocios=negocios,
                           categorias=categorias, filtro_tipo=filtro_tipo,
                           filtro_valor=filtro_valor, segmento=segmento,
                           total_negocios=total,
                           bairros_disponiveis=bairros_disponiveis,
                           anuncio_topo=anuncio_topo, anuncio_meio=anuncio_meio)


@app.route("/<segmento>/<slug_negocio>/")
def pagina_negocio(segmento, slug_negocio):
    if segmento in ROTAS_RESERVADAS:
        return "Não encontrado", 404
    hub = get_hub_by_host()
    if not hub:
        return "Hub não encontrado", 404
    negocio = query("""
        SELECT n.*, c.nome as categoria_nome, c.slug as categoria_slug
        FROM hub_negocios n
        JOIN hub_negocio_hubs nh ON nh.negocio_id = n.id
        LEFT JOIN hub_categorias c ON c.id = n.categoria_id
        WHERE nh.hub_id = %s AND n.slug = %s AND n.ativo = true
    """, (hub["id"], slug_negocio), one=True)
    if not negocio:
        return "Negócio não encontrado", 404
    negocio["exibir_geo"] = negocio["categoria_slug"] not in CATEGORIAS_SEM_GEOLOCALIZACAO
    query("UPDATE hub_negocios SET visualizacoes = visualizacoes + 1 WHERE id = %s",
          (negocio["id"],), commit=True)
    anuncio_topo = _get_anuncios(hub["id"], "topo",
                                 categoria_slug=negocio["categoria_slug"],
                                 cidade=negocio.get("cidade"),
                                 bairro=negocio.get("bairro"))
    anuncio_meio = _get_anuncios(hub["id"], "meio",
                                 categoria_slug=negocio["categoria_slug"],
                                 cidade=negocio.get("cidade"),
                                 bairro=negocio.get("bairro"))
    template = hub.get("template_negocio") or "negocio_padrao"
    return render_template(f"hub/{template}.html", hub=hub, negocio=negocio, segmento=segmento,
                           anuncio_topo=anuncio_topo, anuncio_meio=anuncio_meio)


# ════════════════════════════════════════════════════════════
#  ROTAS DE CIDADE
# ════════════════════════════════════════════════════════════

def _normalizar_cidade_bairro(cidade, bairro):
    """Corrige a grafia de cidade/bairro recebida (formulário, importação automática, etc.)
    para bater com a grafia que JÁ existe no banco — evita criar uma duplicata nova
    (ex: a automação manda 'sao paulo' e o sistema já tem 'São Paulo' -> grava 'São Paulo').
    Se a cidade/bairro ainda não existir de jeito nenhum, mantém o valor recebido como está
    (essa grafia vira a referência pra próxima vez). Chame isso em TODO ponto que grava
    negócio (cadastro público, admin novo/editar, aprovação de pendente)."""
    cidade = (cidade or "").strip() or None
    bairro = (bairro or "").strip() or None

    if cidade:
        existentes = query("""
            SELECT cidade, COUNT(*) as qtd FROM hub_negocios
            WHERE cidade IS NOT NULL AND TRIM(cidade) != ''
            GROUP BY cidade ORDER BY qtd DESC
        """)
        chave = _chave_normalizada(cidade)
        for row in existentes:
            if _chave_normalizada(row["cidade"]) == chave:
                cidade = row["cidade"]
                break

    if bairro:
        sql = """
            SELECT bairro, COUNT(*) as qtd FROM hub_negocios
            WHERE bairro IS NOT NULL AND TRIM(bairro) != ''
        """
        params = []
        if cidade:
            sql += " AND cidade = %s"
            params.append(cidade)
        sql += " GROUP BY bairro ORDER BY qtd DESC"
        chave_b = _chave_normalizada(bairro)
        for row in query(sql, params):
            if _chave_normalizada(row["bairro"]) == chave_b:
                bairro = row["bairro"]
                break

    return cidade, bairro


def _variantes_cidade(hub_id, cidade_nome):
    """Retorna TODAS as grafias salvas no banco (com/sem acento, maiúsc/minúsc) que
    representam a mesma cidade que `cidade_nome`. Usada só pelos pontos de entrada que
    recebem uma STRING solta (ex: api_negocios) — as rotas de página usam _resolve_cidade,
    que já devolve as variantes junto, numa única query."""
    chave_alvo = _chave_normalizada(cidade_nome)
    rows = query("""
        SELECT DISTINCT n.cidade
        FROM hub_negocios n
        JOIN hub_negocio_hubs nh ON nh.negocio_id = n.id
        WHERE nh.hub_id = %s AND n.ativo = true AND n.cidade IS NOT NULL
    """, (hub_id,))
    return [r["cidade"] for r in rows if _chave_normalizada(r["cidade"]) == chave_alvo] or [cidade_nome]


def _variantes_bairro(hub_id, bairro_nome, cidade_variantes=None):
    """Mesma ideia de _variantes_cidade, mas pra bairro. Aceita `cidade_variantes` já
    calculada (lista) pra não repetir a consulta de cidade."""
    chave_alvo = _chave_normalizada(bairro_nome)
    sql = """
        SELECT DISTINCT n.bairro
        FROM hub_negocios n
        JOIN hub_negocio_hubs nh ON nh.negocio_id = n.id
        WHERE nh.hub_id = %s AND n.ativo = true AND n.bairro IS NOT NULL
    """
    params = [hub_id]
    if cidade_variantes:
        sql += " AND n.cidade = ANY(%s)"
        params.append(cidade_variantes)
    rows = query(sql, params)
    return [r["bairro"] for r in rows if _chave_normalizada(r["bairro"]) == chave_alvo] or [bairro_nome]


def _resolve_bairro(hub_id, bairro_slug, cidade_variantes=None):
    """Resolve o slug de bairro pra (nome_canonico, variantes) numa ÚNICA query —
    nome_canonico é a grafia com mais negócios; variantes são todas as grafias da mesma
    (ex: 'Agua Rasa' x 'Água Rasa'). Passe `cidade_variantes` (lista, já resolvida por
    _resolve_cidade) pra escopar a busca sem rodar outra query de cidade. CACHEADO."""
    bairro_slug_norm = _slugify(bairro_slug)
    chave_cache = ("resolve_bairro", hub_id, tuple(sorted(cidade_variantes or [])), bairro_slug_norm)
    cached = _cache_get(chave_cache)
    if cached is not None:
        return cached
    sql = """
        SELECT n.bairro, COUNT(*) as qtd
        FROM hub_negocios n
        JOIN hub_negocio_hubs nh ON nh.negocio_id = n.id
        WHERE nh.hub_id = %s AND n.ativo = true AND n.bairro IS NOT NULL
    """
    params = [hub_id]
    if cidade_variantes:
        sql += " AND n.cidade = ANY(%s)"
        params.append(cidade_variantes)
    sql += " GROUP BY n.bairro ORDER BY qtd DESC, n.bairro"
    rows = query(sql, params)
    candidatos = [row for row in rows if _slugify(row["bairro"]) == bairro_slug_norm]
    if not candidatos:
        return _cache_set(chave_cache, (None, []))
    nome_canonico = candidatos[0]["bairro"]
    chave_alvo = _chave_normalizada(nome_canonico)
    variantes = [row["bairro"] for row in rows if _chave_normalizada(row["bairro"]) == chave_alvo]
    return _cache_set(chave_cache, (nome_canonico, variantes))


def _resolve_cidade(hub_id, cidade_slug):
    """Resolve o slug de cidade pra (nome_canonico, variantes) numa ÚNICA query —
    nome_canonico é a grafia com mais negócios cadastrados (escolha determinística;
    sem isso, um SELECT DISTINCT sem ORDER BY podia devolver 'sao paulo' em vez de
    'São Paulo' de forma aleatória — foi o bug que fazia só 2 categorias aparecerem).
    `variantes` traz todas as grafias da mesma cidade, pra usar nos filtros seguintes
    sem precisar rodar essa consulta de novo a cada helper chamado.
    CACHEADO: essa consulta faz GROUP BY em cima de TODOS os negócios do hub — é a
    mais cara de todas e o resultado é igual pra qualquer visitante, então cachear
    aqui é o que mais economiza."""
    cidade_slug_norm = _slugify(cidade_slug)
    chave_cache = ("resolve_cidade", hub_id, cidade_slug_norm)
    cached = _cache_get(chave_cache)
    if cached is not None:
        return cached
    rows = query("""
        SELECT n.cidade, COUNT(*) as qtd
        FROM hub_negocios n
        JOIN hub_negocio_hubs nh ON nh.negocio_id = n.id
        WHERE nh.hub_id = %s AND n.ativo = true AND n.cidade IS NOT NULL
        GROUP BY n.cidade
        ORDER BY qtd DESC, n.cidade
    """, (hub_id,))
    candidatos = [row for row in rows if _slugify(row["cidade"]) == cidade_slug_norm]
    if not candidatos:
        return _cache_set(chave_cache, (None, []))
    nome_canonico = candidatos[0]["cidade"]
    chave_alvo = _chave_normalizada(nome_canonico)
    variantes = [row["cidade"] for row in rows if _chave_normalizada(row["cidade"]) == chave_alvo]
    return _cache_set(chave_cache, (nome_canonico, variantes))


def _negocios_cidade(hub_id, cidade_variantes=None, cat_slug=None, bairro_variantes=None, limit=None, offset=None):
    sql = """
        SELECT n.*, c.nome as categoria_nome, c.slug as categoria_slug
        FROM hub_negocios n
        JOIN hub_negocio_hubs nh ON nh.negocio_id = n.id
        LEFT JOIN hub_categorias c ON c.id = n.categoria_id
        WHERE nh.hub_id = %s AND n.ativo = true
    """
    params = [hub_id]
    if cidade_variantes:
        sql += " AND n.cidade = ANY(%s)"
        params.append(cidade_variantes)
    if cat_slug:
        sql += " AND c.slug = %s"
        params.append(cat_slug)
    if bairro_variantes:
        sql += " AND n.bairro = ANY(%s)"
        params.append(bairro_variantes)
    sql += " ORDER BY n.nome"
    if limit is not None:
        sql += " LIMIT %s"
        params.append(limit)
        if offset is not None:
            sql += " OFFSET %s"
            params.append(offset)
    return query(sql, params)


def _contar_negocios_cidade(hub_id, cidade_variantes=None, cat_slug=None, bairro_variantes=None):
    """COUNT(*) leve — usado para mostrar o total real mesmo quando _negocios_cidade vem limitado.
    CACHEADO: é reconsultado em toda visita mas só muda quando algum negócio é criado/editado."""
    chave_cache = ("contar", hub_id, tuple(sorted(cidade_variantes or [])), cat_slug,
                   tuple(sorted(bairro_variantes or [])))
    cached = _cache_get(chave_cache)
    if cached is not None:
        return cached
    sql = """
        SELECT COUNT(*) as total
        FROM hub_negocios n
        JOIN hub_negocio_hubs nh ON nh.negocio_id = n.id
        LEFT JOIN hub_categorias c ON c.id = n.categoria_id
        WHERE nh.hub_id = %s AND n.ativo = true
    """
    params = [hub_id]
    if cidade_variantes:
        sql += " AND n.cidade = ANY(%s)"
        params.append(cidade_variantes)
    if cat_slug:
        sql += " AND c.slug = %s"
        params.append(cat_slug)
    if bairro_variantes:
        sql += " AND n.bairro = ANY(%s)"
        params.append(bairro_variantes)
    total = query(sql, params, one=True)["total"]
    return _cache_set(chave_cache, total)


def _bairros_cidade(hub_id, cidade_variantes):
    """Lista de bairros da cidade, DEDUPLICADA por grafia (acento/maiúscula).
    Cada bairro aparece uma única vez, usando a grafia com mais negócios cadastrados.
    CACHEADO — é a mesma lista pra qualquer visitante da cidade."""
    chave_cache = ("bairros_cidade", hub_id, tuple(sorted(cidade_variantes)))
    cached = _cache_get(chave_cache)
    if cached is not None:
        return cached
    rows = query("""
        SELECT n.bairro, COUNT(*) as qtd
        FROM hub_negocios n
        JOIN hub_negocio_hubs nh ON nh.negocio_id = n.id
        WHERE nh.hub_id = %s AND n.ativo = true
          AND n.bairro IS NOT NULL AND TRIM(n.bairro) != ''
          AND n.cidade = ANY(%s)
        GROUP BY n.bairro
        ORDER BY qtd DESC, n.bairro
    """, (hub_id, cidade_variantes))
    vistos = {}
    for r in rows:
        chave = _chave_normalizada(r["bairro"])
        if chave not in vistos:
            vistos[chave] = r["bairro"]
    return _cache_set(chave_cache, sorted(vistos.values()))


def _categorias_cidade(hub_id, cidade_variantes):
    """CACHEADO — lista de categorias com negócios na cidade, igual pra qualquer visitante."""
    chave_cache = ("categorias_cidade", hub_id, tuple(sorted(cidade_variantes)))
    cached = _cache_get(chave_cache)
    if cached is not None:
        return cached
    resultado = query("""
        SELECT DISTINCT c.id, c.nome, c.slug, c.icone_url
        FROM hub_categorias c
        JOIN hub_negocios n ON n.categoria_id = c.id
        JOIN hub_negocio_hubs nh ON nh.negocio_id = n.id
        WHERE nh.hub_id = %s AND n.ativo = true AND c.ativo = true
          AND n.cidade = ANY(%s)
        ORDER BY c.nome
    """, (hub_id, cidade_variantes))
    return _cache_set(chave_cache, resultado)


def _render_cidade(hub, negocios, categorias, cidade_nome,
                   bairro=None, categoria=None,
                   bairros_disponiveis=None, categorias_disponiveis=None,
                   total_negocios=None):
    template = hub.get("template_cidade") or "cidade_otp"
    cat_slug = categoria["slug"] if categoria else None
    anuncio_topo = _get_anuncios(hub["id"], "topo", categoria_slug=cat_slug, cidade=cidade_nome, bairro=bairro)
    anuncio_meio = _get_anuncios(hub["id"], "meio", categoria_slug=cat_slug, cidade=cidade_nome, bairro=bairro)
    total_final = total_negocios if total_negocios is not None else len(negocios)
    resp = make_response(render_template(
        f"hub/{template}.html",
        hub=hub,
        negocios=negocios,
        categorias=categorias,
        cidade=cidade_nome,
        bairro=bairro,
        categoria=categoria,
        bairros_disponiveis=bairros_disponiveis or [],
        categorias_disponiveis=categorias_disponiveis or [],
        total_negocios=total_final,
        anuncio_topo=anuncio_topo,
        anuncio_meio=anuncio_meio,
    ))
    # SEO: páginas sem nenhum negócio (combinação vazia de cidade/bairro/categoria)
    # recebem noindex via header HTTP — não altera o HTML, não muda o status (200),
    # não afeta em nada páginas que já têm resultados e já estão indexadas.
    if total_final == 0:
        resp.headers["X-Robots-Tag"] = "noindex, follow"
    return resp


@app.route("/cidade/<cidade_slug>/")
def pagina_cidade(cidade_slug):
    hub = get_hub_by_host()
    if not hub:
        return "Hub não encontrado", 404
    cidade_nome, cidade_var = _resolve_cidade(hub["id"], cidade_slug)
    if not cidade_nome:
        return "Cidade não encontrada", 404
    negocios   = _negocios_cidade(hub["id"], cidade_variantes=cidade_var)
    total      = _contar_negocios_cidade(hub["id"], cidade_variantes=cidade_var)
    categorias = query("SELECT * FROM hub_categorias WHERE ativo = true ORDER BY nome")
    bairros    = _bairros_cidade(hub["id"], cidade_var)
    cats_disp  = _categorias_cidade(hub["id"], cidade_var)
    return _render_cidade(hub, negocios, categorias, cidade_nome,
                          bairros_disponiveis=bairros,
                          categorias_disponiveis=cats_disp,
                          total_negocios=total)


@app.route("/cidade/<cidade_slug>/<segundo_slug>/")
def pagina_cidade_segundo(cidade_slug, segundo_slug):
    hub = get_hub_by_host()
    if not hub:
        return "Hub não encontrado", 404
    cidade_nome, cidade_var = _resolve_cidade(hub["id"], cidade_slug)
    if not cidade_nome:
        return "Cidade não encontrada", 404
    categoria = query(
        "SELECT * FROM hub_categorias WHERE slug = %s AND ativo = true",
        (segundo_slug,), one=True
    )
    categorias = query("SELECT * FROM hub_categorias WHERE ativo = true ORDER BY nome")
    bairros    = _bairros_cidade(hub["id"], cidade_var)
    cats_disp  = _categorias_cidade(hub["id"], cidade_var)
    if categoria:
        negocios = _negocios_cidade(hub["id"], cidade_variantes=cidade_var, cat_slug=segundo_slug)
        total    = _contar_negocios_cidade(hub["id"], cidade_variantes=cidade_var, cat_slug=segundo_slug)
        return _render_cidade(hub, negocios, categorias, cidade_nome,
                              categoria=dict(categoria),
                              bairros_disponiveis=bairros,
                              categorias_disponiveis=cats_disp,
                              total_negocios=total)
    else:
        bairro_nome, bairro_var = _resolve_bairro(hub["id"], segundo_slug, cidade_var)
        if not bairro_nome:
            bairro_nome = segundo_slug.replace("-", " ").title()
            bairro_var  = [bairro_nome]
        negocios    = _negocios_cidade(hub["id"], cidade_variantes=cidade_var, bairro_variantes=bairro_var)
        total       = _contar_negocios_cidade(hub["id"], cidade_variantes=cidade_var, bairro_variantes=bairro_var)
        return _render_cidade(hub, negocios, categorias, cidade_nome,
                              bairro=bairro_nome,
                              bairros_disponiveis=bairros,
                              categorias_disponiveis=cats_disp,
                              total_negocios=total)


@app.route("/cidade/<cidade_slug>/<bairro_slug>/<cat_slug>/")
def pagina_cidade_bairro_cat(cidade_slug, bairro_slug, cat_slug):
    hub = get_hub_by_host()
    if not hub:
        return "Hub não encontrado", 404
    cidade_nome, cidade_var = _resolve_cidade(hub["id"], cidade_slug)
    if not cidade_nome:
        return "Cidade não encontrada", 404
    categoria   = query(
        "SELECT * FROM hub_categorias WHERE slug = %s AND ativo = true",
        (cat_slug,), one=True
    )
    categorias  = query("SELECT * FROM hub_categorias WHERE ativo = true ORDER BY nome")
    bairros     = _bairros_cidade(hub["id"], cidade_var)
    cats_disp   = _categorias_cidade(hub["id"], cidade_var)
    bairro_nome, bairro_var = _resolve_bairro(hub["id"], bairro_slug, cidade_var)
    if not bairro_nome:
        bairro_nome = bairro_slug.replace("-", " ").title()
        bairro_var  = [bairro_nome]
    negocios    = _negocios_cidade(hub["id"], cidade_variantes=cidade_var,
                                   cat_slug=cat_slug, bairro_variantes=bairro_var)
    total       = _contar_negocios_cidade(hub["id"], cidade_variantes=cidade_var,
                                          cat_slug=cat_slug, bairro_variantes=bairro_var)
    return _render_cidade(hub, negocios, categorias, cidade_nome,
                          bairro=bairro_nome,
                          categoria=dict(categoria) if categoria else None,
                          bairros_disponiveis=bairros,
                          categorias_disponiveis=cats_disp,
                          total_negocios=total)


@app.route("/<cat_slug>/em/<cidade_slug>/")
def pagina_cat_cidade(cat_slug, cidade_slug):
    if cat_slug in ROTAS_RESERVADAS:
        return "Não encontrado", 404
    hub = get_hub_by_host()
    if not hub:
        return "Hub não encontrado", 404
    categoria = query(
        "SELECT * FROM hub_categorias WHERE slug = %s AND ativo = true",
        (cat_slug,), one=True
    )
    if not categoria:
        return "Categoria não encontrada", 404
    cidade_nome, cidade_var = _resolve_cidade(hub["id"], cidade_slug)
    if not cidade_nome:
        return "Cidade não encontrada", 404
    negocios   = _negocios_cidade(hub["id"], cidade_variantes=cidade_var, cat_slug=cat_slug)
    total      = _contar_negocios_cidade(hub["id"], cidade_variantes=cidade_var, cat_slug=cat_slug)
    categorias = query("SELECT * FROM hub_categorias WHERE ativo = true ORDER BY nome")
    bairros    = _bairros_cidade(hub["id"], cidade_var)
    cats_disp  = _categorias_cidade(hub["id"], cidade_var)
    return _render_cidade(hub, negocios, categorias, cidade_nome,
                          categoria=dict(categoria),
                          bairros_disponiveis=bairros,
                          categorias_disponiveis=cats_disp,
                          total_negocios=total)


@app.route("/<cat_slug>/em/<cidade_slug>/<bairro_slug>/")
def pagina_cat_cidade_bairro(cat_slug, cidade_slug, bairro_slug):
    if cat_slug in ROTAS_RESERVADAS:
        return "Não encontrado", 404
    hub = get_hub_by_host()
    if not hub:
        return "Hub não encontrado", 404
    categoria = query(
        "SELECT * FROM hub_categorias WHERE slug = %s AND ativo = true",
        (cat_slug,), one=True
    )
    if not categoria:
        return "Categoria não encontrada", 404
    cidade_nome, cidade_var = _resolve_cidade(hub["id"], cidade_slug)
    if not cidade_nome:
        return "Cidade não encontrada", 404
    bairro_nome, bairro_var = _resolve_bairro(hub["id"], bairro_slug, cidade_var)
    if not bairro_nome:
        bairro_nome = bairro_slug.replace("-", " ").title()
        bairro_var  = [bairro_nome]
    negocios    = _negocios_cidade(hub["id"], cidade_variantes=cidade_var,
                                    cat_slug=cat_slug, bairro_variantes=bairro_var)
    total       = _contar_negocios_cidade(hub["id"], cidade_variantes=cidade_var,
                                          cat_slug=cat_slug, bairro_variantes=bairro_var)
    categorias  = query("SELECT * FROM hub_categorias WHERE ativo = true ORDER BY nome")
    bairros     = _bairros_cidade(hub["id"], cidade_var)
    cats_disp   = _categorias_cidade(hub["id"], cidade_var)
    return _render_cidade(hub, negocios, categorias, cidade_nome,
                          bairro=bairro_nome,
                          categoria=dict(categoria),
                          bairros_disponiveis=bairros,
                          categorias_disponiveis=cats_disp,
                          total_negocios=total)


# ════════════════════════════════════════════════════════════
#  BLOG — ROTAS PÚBLICAS
# ════════════════════════════════════════════════════════════

@app.route("/blog/")
def blog_index():
    hub = get_hub_by_host()
    if not hub:
        return "Hub não encontrado", 404
    tag      = request.args.get("tag", "").strip()
    page     = max(int(request.args.get("page", 1)), 1)
    per_page = 12
    offset   = (page - 1) * per_page

    sql = """
        SELECT p.*, array_agg(t.slug ORDER BY t.nome) FILTER (WHERE t.id IS NOT NULL) as tags
        FROM blog_posts p
        JOIN blog_post_hubs ph ON ph.post_id = p.id
        LEFT JOIN blog_post_tags pt ON pt.post_id = p.id
        LEFT JOIN blog_tags t ON t.id = pt.tag_id
        WHERE ph.hub_id = %s AND p.publicado = true
    """
    params = [hub["id"]]
    if tag:
        sql += " AND EXISTS (SELECT 1 FROM blog_post_tags pt2 JOIN blog_tags t2 ON t2.id = pt2.tag_id WHERE pt2.post_id = p.id AND t2.slug = %s)"
        params.append(tag)
    sql += " GROUP BY p.id ORDER BY p.publicado_em DESC LIMIT %s OFFSET %s"
    params += [per_page, offset]
    posts = query(sql, params)

    sql_count = """
        SELECT COUNT(DISTINCT p.id) as n
        FROM blog_posts p
        JOIN blog_post_hubs ph ON ph.post_id = p.id
        WHERE ph.hub_id = %s AND p.publicado = true
    """
    count_params = [hub["id"]]
    if tag:
        sql_count += " AND EXISTS (SELECT 1 FROM blog_post_tags pt2 JOIN blog_tags t2 ON t2.id = pt2.tag_id WHERE pt2.post_id = p.id AND t2.slug = %s)"
        count_params.append(tag)
    total = query(sql_count, count_params, one=True)["n"]
    total_pages = max(1, (total + per_page - 1) // per_page)

    tags_disponiveis = query("""
        SELECT DISTINCT t.id, t.nome, t.slug
        FROM blog_tags t
        JOIN blog_post_tags pt ON pt.tag_id = t.id
        JOIN blog_posts p ON p.id = pt.post_id
        JOIN blog_post_hubs ph ON ph.post_id = p.id
        WHERE ph.hub_id = %s AND p.publicado = true
        ORDER BY t.nome
    """, (hub["id"],))

    categorias = query("SELECT * FROM hub_categorias WHERE ativo = true ORDER BY nome")
    template = hub.get("template_blog") or "blog_otp"
    return render_template(
        f"hub/{template}.html",
        hub=hub, posts=posts, tags=tags_disponiveis,
        tag_ativa=tag, page=page, total_pages=total_pages,
        categorias=categorias,
    )


@app.route("/blog/<slug>/")
def blog_post(slug):
    hub = get_hub_by_host()
    if not hub:
        return "Hub não encontrado", 404
    post = query("""
        SELECT p.*
        FROM blog_posts p
        JOIN blog_post_hubs ph ON ph.post_id = p.id
        WHERE ph.hub_id = %s AND p.slug = %s AND p.publicado = true
    """, (hub["id"], slug), one=True)
    if not post:
        return "Post não encontrado", 404

    query("UPDATE blog_posts SET visualizacoes = visualizacoes + 1 WHERE id = %s",
          (post["id"],), commit=True)

    tags = query("""
        SELECT t.* FROM blog_tags t
        JOIN blog_post_tags pt ON pt.tag_id = t.id
        WHERE pt.post_id = %s ORDER BY t.nome
    """, (post["id"],))

    relacionados = query("""
        SELECT DISTINCT p.id, p.titulo, p.slug, p.capa_url, p.publicado_em, p.resumo
        FROM blog_posts p
        JOIN blog_post_hubs ph ON ph.post_id = p.id
        JOIN blog_post_tags pt ON pt.post_id = p.id
        WHERE ph.hub_id = %s AND p.publicado = true AND p.id <> %s
          AND pt.tag_id IN (
              SELECT tag_id FROM blog_post_tags WHERE post_id = %s
          )
        ORDER BY p.publicado_em DESC
        LIMIT 3
    """, (hub["id"], post["id"], post["id"]))

    categorias = query("SELECT * FROM hub_categorias WHERE ativo = true ORDER BY nome")
    anuncio_topo = _get_anuncios(hub["id"], "topo")
    anuncio_meio = _get_anuncios(hub["id"], "meio")
    template_post = hub.get("template_blog_post") or "blog_post_otp"
    return render_template(
        f"hub/{template_post}.html",
        hub=hub, post=post, tags=tags,
        relacionados=relacionados, categorias=categorias,
        anuncio_topo=anuncio_topo, anuncio_meio=anuncio_meio,
    )


# ════════════════════════════════════════════════════════════
#  CADASTRO DE NEGÓCIO — ROTA PÚBLICA
# ════════════════════════════════════════════════════════════

@app.route("/cadastrar-negocio", methods=["POST"])
def cadastrar_negocio():
    hub = get_hub_by_host()
    if not hub:
        return jsonify({"erro": "Hub não encontrado"}), 404

    f = request.form

    nome = (f.get("nome") or "").strip()
    if not nome:
        return jsonify({"erro": "Nome é obrigatório"}), 400

    categoria_id = f.get("categoria_id") or None
    if categoria_id:
        cat = query("SELECT id FROM hub_categorias WHERE id = %s AND ativo = true",
                    (categoria_id,), one=True)
        if not cat:
            categoria_id = None

    cidade_norm, bairro_norm = _normalizar_cidade_bairro(f.get("cidade"), f.get("bairro"))

    query("""
        INSERT INTO hub_negocios_pendentes
            (hub_id, nome, categoria_id, descricao, foto_url,
             endereco, bairro, cidade,
             whatsapp, telefone, instagram, site_url)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    """, (
        hub["id"],
        nome,
        categoria_id,
        (f.get("descricao") or "").strip() or None,
        (f.get("foto_url") or "").strip() or None,
        (f.get("endereco") or "").strip() or None,
        bairro_norm,
        cidade_norm,
        (f.get("whatsapp") or "").strip() or None,
        (f.get("telefone") or "").strip() or None,
        (f.get("instagram") or "").strip() or None,
        (f.get("site_url") or "").strip() or None,
    ), commit=True)

    return jsonify({"ok": True})


# ════════════════════════════════════════════════════════════
#  ADMIN — AUTH
# ════════════════════════════════════════════════════════════

@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if request.method == "POST":
        email = request.form.get("email", "").strip()
        senha = request.form.get("senha", "")
        user = query("SELECT * FROM usuarios WHERE email = %s", (email,), one=True)
        if not user:
            return render_template("admin/login.html", erro="Usuário não encontrado")
        admin = query("SELECT * FROM admins WHERE user_id = %s", (user["id"],), one=True)
        if not admin:
            return render_template("admin/login.html", erro="Sem permissão de admin")
        senha_hash = user.get("senha_hash") or ""
        if not senha_hash:
            return render_template("admin/login.html", erro="Senha não configurada. Redefina pelo banco.")
        if not check_password_hash(senha_hash, senha):
            return render_template("admin/login.html", erro="Senha incorreta")
        session["admin_id"]    = user["id"]
        session["admin_nome"]  = user["nome"]
        session["admin_nivel"] = admin["nivel"]
        return redirect("/admin")
    return render_template("admin/login.html")


@app.route("/admin/logout")
def admin_logout():
    session.clear()
    return redirect("/admin/login")


# ════════════════════════════════════════════════════════════
#  ADMIN — PAINEL
# ════════════════════════════════════════════════════════════

@app.route("/admin")
@login_required
def admin_dashboard():
    total_hubs       = query("SELECT COUNT(*) as n FROM hub_clientes", one=True)["n"]
    total_negocios   = query("SELECT COUNT(*) as n FROM hub_negocios", one=True)["n"]
    total_categorias = query("SELECT COUNT(*) as n FROM hub_categorias", one=True)["n"]
    total_usuarios   = query("SELECT COUNT(*) as n FROM usuarios", one=True)["n"]
    total_posts      = query("SELECT COUNT(*) as n FROM blog_posts", one=True)["n"]
    return render_template("admin/index.html",
                           total_hubs=total_hubs,
                           total_negocios=total_negocios,
                           total_categorias=total_categorias,
                           total_usuarios=total_usuarios,
                           total_posts=total_posts)


@app.route("/admin/templates")
@login_required
def admin_templates():
    return jsonify({
        "index":   listar_templates("index"),
        "filtro":  listar_templates("filtro"),
        "negocio": listar_templates("negocio"),
        "cidade":  listar_templates("cidade"),
        "blog":    listar_templates("blog"),
        "blog_post": listar_templates("blog_post"),
    })


# ════════════════════════════════════════════════════════════
#  ADMIN — HUBS
# ════════════════════════════════════════════════════════════

@app.route("/admin/hubs")
@login_required
def admin_hubs():
    hubs = query("""
        SELECT h.*, u.nome as usuario_nome,
               (SELECT COUNT(*) FROM hub_negocio_hubs nh WHERE nh.hub_id = h.id) as total_negocios
        FROM hub_clientes h
        LEFT JOIN usuarios u ON u.id = h.user_id
        ORDER BY h.criado_em DESC
    """)
    return jsonify([dict(h) for h in hubs])


@app.route("/admin/hubs/novo", methods=["POST"])
@login_required
def admin_hub_novo():
    d = request.form
    query("""
        INSERT INTO hub_clientes
        (user_id, nome, slug, dominio_proprio, hub_leanttro, tipo,
         bairro_fixo, categoria_fixa, logo_url, cor_primaria, cor_secundaria,
         titulo, descricao, ga4_id, pixel_id, instagram_url, whatsapp,
         template_index, template_filtro, template_negocio, template_cidade,
         template_blog, template_blog_post,
         banner_fundo_url, banner1_foto_url, banner1_link, banner2_foto_url, banner2_link,
         ativo)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
    """, (
        d.get("user_id") or None, d["nome"], d["slug"],
        d.get("dominio_proprio") or None, d.get("hub_leanttro") or None,
        d.get("tipo", "bairro"), d.get("bairro_fixo") or None,
        d.get("categoria_fixa") or None, d.get("logo_url") or None,
        d.get("cor_primaria", "#7943e2"), d.get("cor_secundaria", "#5c2ec2"),
        d.get("titulo") or None, d.get("descricao") or None,
        d.get("ga4_id") or None, d.get("pixel_id") or None,
        d.get("instagram_url") or None, d.get("whatsapp") or None,
        d.get("template_index", "index_padrao"),
        d.get("template_filtro", "filtro_padrao"),
        d.get("template_negocio", "negocio_padrao"),
        d.get("template_cidade", "cidade_otp"),
        d.get("template_blog", "blog_otp"),
        d.get("template_blog_post", "blog_post_otp"),
        d.get("banner_fundo_url") or None,
        d.get("banner1_foto_url") or None, d.get("banner1_link") or None,
        d.get("banner2_foto_url") or None, d.get("banner2_link") or None,
        "ativo" in d
    ), commit=True)
    return jsonify({"ok": True})


@app.route("/admin/hubs/<int:hub_id>/editar", methods=["GET", "POST"])
@login_required
def admin_hub_editar(hub_id):
    hub = query("SELECT * FROM hub_clientes WHERE id = %s", (hub_id,), one=True)
    if not hub:
        return jsonify({"erro": "Hub não encontrado"}), 404
    if request.method == "POST":
        d = request.form
        query("""
            UPDATE hub_clientes SET
            user_id=%s, nome=%s, slug=%s, dominio_proprio=%s, hub_leanttro=%s,
            tipo=%s, bairro_fixo=%s, categoria_fixa=%s, logo_url=%s,
            cor_primaria=%s, cor_secundaria=%s, titulo=%s, descricao=%s,
            ga4_id=%s, pixel_id=%s, instagram_url=%s, whatsapp=%s,
            template_index=%s, template_filtro=%s, template_negocio=%s, template_cidade=%s,
            template_blog=%s, template_blog_post=%s,
            banner_fundo_url=%s, banner1_foto_url=%s, banner1_link=%s,
            banner2_foto_url=%s, banner2_link=%s, ativo=%s
            WHERE id=%s
        """, (
            d.get("user_id") or None, d["nome"], d["slug"],
            d.get("dominio_proprio") or None, d.get("hub_leanttro") or None,
            d.get("tipo", "bairro"), d.get("bairro_fixo") or None,
            d.get("categoria_fixa") or None, d.get("logo_url") or None,
            d.get("cor_primaria", "#7943e2"), d.get("cor_secundaria", "#5c2ec2"),
            d.get("titulo") or None, d.get("descricao") or None,
            d.get("ga4_id") or None, d.get("pixel_id") or None,
            d.get("instagram_url") or None, d.get("whatsapp") or None,
            d.get("template_index", "index_padrao"),
            d.get("template_filtro", "filtro_padrao"),
            d.get("template_negocio", "negocio_padrao"),
            d.get("template_cidade", "cidade_otp"),
            d.get("template_blog", "blog_otp"),
            d.get("template_blog_post", "blog_post_otp"),
            d.get("banner_fundo_url") or None,
            d.get("banner1_foto_url") or None, d.get("banner1_link") or None,
            d.get("banner2_foto_url") or None, d.get("banner2_link") or None,
            "ativo" in d, hub_id
        ), commit=True)
        return jsonify({"ok": True})
    return jsonify(dict(hub))


@app.route("/admin/hubs/<int:hub_id>/deletar", methods=["POST"])
@login_required
def admin_hub_deletar(hub_id):
    query("DELETE FROM hub_clientes WHERE id = %s", (hub_id,), commit=True)
    return jsonify({"ok": True})


# ════════════════════════════════════════════════════════════
#  ADMIN — NEGÓCIOS
# ════════════════════════════════════════════════════════════

@app.route("/admin/negocios")
@login_required
def admin_negocios():
    """Listagem paginada e filtrada no servidor (essencial com 150k+ linhas).
    Aceita: q (busca por nome), categoria_id, bairro, status (ativo/inativo),
    limit, offset. Retorna {negocios: [...], total: N}.
    """
    q          = request.args.get("q", "").strip()
    categoria_id = request.args.get("categoria_id", "").strip()
    bairro     = request.args.get("bairro", "").strip()
    status     = request.args.get("status", "").strip()
    try:
        limit  = min(int(request.args.get("limit", 50)), 5000)
        offset = max(int(request.args.get("offset", 0)), 0)
    except (ValueError, TypeError):
        limit, offset = 50, 0

    condicoes = []
    params = []
    if q:
        condicoes.append("n.nome ILIKE %s")
        params.append(f"%{q}%")
    if categoria_id:
        condicoes.append("n.categoria_id = %s")
        params.append(categoria_id)
    if bairro:
        condicoes.append("LOWER(n.bairro) = LOWER(%s)")
        params.append(bairro)
    if status == "ativo":
        condicoes.append("n.ativo = true")
    elif status == "inativo":
        condicoes.append("n.ativo = false")
    where_sql = (" WHERE " + " AND ".join(condicoes)) if condicoes else ""

    total = query(f"""
        SELECT COUNT(*) as total
        FROM hub_negocios n
        {where_sql}
    """, params, one=True)["total"]

    # Seleciona só as colunas que a tabela do admin realmente usa —
    # evita trafegar descricao/foto_url/endereco/lat/lng/etc. em 150k linhas.
    negocios = query(f"""
        SELECT n.id, n.nome, n.slug, n.bairro, n.visualizacoes, n.ativo, n.criado_em,
               c.nome as categoria_nome
        FROM hub_negocios n
        LEFT JOIN hub_categorias c ON c.id = n.categoria_id
        {where_sql}
        ORDER BY n.criado_em DESC
        LIMIT %s OFFSET %s
    """, params + [limit, offset])

    return jsonify({
        "negocios": [dict(n) for n in negocios],
        "total": total,
        "limit": limit,
        "offset": offset,
    })


@app.route("/admin/negocios/bairros")
@login_required
def admin_negocios_bairros():
    """Lista leve de bairros distintos, para popular o filtro sem carregar negócios."""
    bairros = query("""
        SELECT DISTINCT bairro
        FROM hub_negocios
        WHERE bairro IS NOT NULL AND TRIM(bairro) != ''
        ORDER BY bairro
    """)
    return jsonify([b["bairro"] for b in bairros])


@app.route("/admin/negocios/localidades")
@login_required
def admin_negocios_localidades():
    """Mapa {cidade: [bairros...]} pra popular o seletor de segmentação de anúncios
    (todas as cidades/bairros que existem em negócios cadastrados)."""
    rows = query("""
        SELECT DISTINCT cidade, bairro
        FROM hub_negocios
        WHERE cidade IS NOT NULL AND TRIM(cidade) != ''
        ORDER BY cidade, bairro
    """)
    mapa = {}
    for r in rows:
        cidade = r["cidade"]
        bairro = r["bairro"]
        mapa.setdefault(cidade, [])
        if bairro and bairro.strip():
            mapa[cidade].append(bairro)
    return jsonify(mapa)


@app.route("/admin/negocios/duplicados/<campo>")
@login_required
def admin_negocios_duplicados_campo(campo):
    """Agrupa valores de 'bairro' ou 'cidade' que só diferem por maiúscula/minúscula ou acento
    (ex: 'Aldeia da Serra' x 'Aldeia Da Serra', ou 'São Paulo' x 'Sao paulo')."""
    if campo not in CAMPOS_NORMALIZAVEIS:
        return jsonify({"error": "Campo inválido"}), 400
    linhas = query(f"""
        SELECT {campo} AS variante, COUNT(*) AS qtd
        FROM hub_negocios
        WHERE {campo} IS NOT NULL AND TRIM({campo}) != ''
        GROUP BY {campo}
        ORDER BY {campo}
    """)
    grupos = {}
    for r in linhas:
        chave = _chave_normalizada(r["variante"])
        grupos.setdefault(chave, []).append({"bairro": r["variante"], "qtd": r["qtd"]})
    for variantes in grupos.values():
        variantes.sort(key=lambda v: v["qtd"], reverse=True)
    resultado = [
        {"chave": chave, "variantes": variantes}
        for chave, variantes in grupos.items()
        if len(variantes) > 1
    ]
    resultado.sort(key=lambda g: g["chave"])
    return jsonify(resultado)


@app.route("/admin/negocios/novo", methods=["POST"])
@login_required
def admin_negocio_novo():
    d = request.form
    cidade_norm, bairro_norm = _normalizar_cidade_bairro(d.get("cidade", "São Paulo"), d.get("bairro"))
    cur = get_db().cursor()
    cur.execute("""
        INSERT INTO hub_negocios
        (categoria_id, nome, slug, descricao, foto_url, endereco, bairro, cidade,
         lat, lng, whatsapp, telefone, instagram, site_url,
         mostrar_foto, mostrar_descricao, mostrar_whatsapp,
         mostrar_instagram, mostrar_telefone, mostrar_site,
         mostrar_endereco, mostrar_mapa, ativo)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        RETURNING id
    """, (
        d.get("categoria_id") or None, d["nome"], d["slug"],
        d.get("descricao") or None, d.get("foto_url") or None,
        d.get("endereco") or None, bairro_norm,
        cidade_norm,
        _parse_coord(d.get("lat")), _parse_coord(d.get("lng")),
        d.get("whatsapp") or None, d.get("telefone") or None,
        d.get("instagram") or None, d.get("site_url") or None,
        "mostrar_foto"      in d, "mostrar_descricao" in d,
        "mostrar_whatsapp"  in d, "mostrar_instagram" in d,
        "mostrar_telefone"  in d, "mostrar_site"      in d,
        "mostrar_endereco"  in d, "mostrar_mapa"      in d,
        "ativo"             in d,
    ))
    negocio_id = cur.fetchone()["id"]
    for hub_id in request.form.getlist("hubs"):
        cur.execute("""
            INSERT INTO hub_negocio_hubs (negocio_id, hub_id)
            VALUES (%s, %s) ON CONFLICT DO NOTHING
        """, (negocio_id, hub_id))
    get_db().commit()
    _cache_invalidar()
    return jsonify({"ok": True})


@app.route("/admin/negocios/<int:negocio_id>/editar", methods=["GET", "POST"])
@login_required
def admin_negocio_editar(negocio_id):
    negocio = query("SELECT * FROM hub_negocios WHERE id = %s", (negocio_id,), one=True)
    if not negocio:
        return jsonify({"erro": "Negócio não encontrado"}), 404
    if request.method == "POST":
        d = request.form
        cidade_norm, bairro_norm = _normalizar_cidade_bairro(d.get("cidade", "São Paulo"), d.get("bairro"))
        query("""
            UPDATE hub_negocios SET
            categoria_id=%s, nome=%s, slug=%s, descricao=%s, foto_url=%s,
            endereco=%s, bairro=%s, cidade=%s, lat=%s, lng=%s,
            whatsapp=%s, telefone=%s, instagram=%s, site_url=%s,
            mostrar_foto=%s, mostrar_descricao=%s, mostrar_whatsapp=%s,
            mostrar_instagram=%s, mostrar_telefone=%s, mostrar_site=%s,
            mostrar_endereco=%s, mostrar_mapa=%s, ativo=%s
            WHERE id=%s
        """, (
            d.get("categoria_id") or None, d["nome"], d["slug"],
            d.get("descricao") or None, d.get("foto_url") or None,
            d.get("endereco") or None, bairro_norm,
            cidade_norm,
            _parse_coord(d.get("lat")), _parse_coord(d.get("lng")),
            d.get("whatsapp") or None, d.get("telefone") or None,
            d.get("instagram") or None, d.get("site_url") or None,
            "mostrar_foto"      in d, "mostrar_descricao" in d,
            "mostrar_whatsapp"  in d, "mostrar_instagram" in d,
            "mostrar_telefone"  in d, "mostrar_site"      in d,
            "mostrar_endereco"  in d, "mostrar_mapa"      in d,
            "ativo"             in d, negocio_id
        ), commit=True)
        if "hubs" in request.form:
            query("DELETE FROM hub_negocio_hubs WHERE negocio_id = %s", (negocio_id,), commit=True)
            for hub_id in request.form.getlist("hubs"):
                query("""
                    INSERT INTO hub_negocio_hubs (negocio_id, hub_id)
                    VALUES (%s, %s) ON CONFLICT DO NOTHING
                """, (negocio_id, hub_id), commit=True)
        _cache_invalidar()
        return jsonify({"ok": True})
    hubs_do_negocio = [r["hub_id"] for r in query(
        "SELECT hub_id FROM hub_negocio_hubs WHERE negocio_id = %s", (negocio_id,)
    )]
    data = dict(negocio)
    data["hubs_do_negocio"] = hubs_do_negocio
    return jsonify(data)


@app.route("/admin/negocios/<int:negocio_id>/deletar", methods=["POST"])
@login_required
def admin_negocio_deletar(negocio_id):
    query("DELETE FROM hub_negocios WHERE id = %s", (negocio_id,), commit=True)
    _cache_invalidar()
    return jsonify({"ok": True})


@app.route("/admin/negocios/bulk", methods=["POST"])
@login_required
def admin_negocios_bulk():
    data   = request.get_json(force=True)
    action = data.get("action", "")

    if action == "mesclar_bairros":
        bairros_origem = [b.strip() for b in data.get("bairros_origem", []) if isinstance(b, str) and b.strip()]
        bairro_destino = (data.get("bairro_destino") or "").strip()
        if not bairros_origem:
            return jsonify({"error": "Nenhum bairro de origem informado"}), 400
        if not bairro_destino:
            return jsonify({"error": "bairro_destino obrigatório"}), 400
        # tira o próprio destino da lista de origem, só se for EXATAMENTE igual (senão não precisa de update).
        # atenção: comparação sensível a maiúsculas/minúsculas de propósito — "Aldeia da Serra" e
        # "Aldeia Da Serra" são grafias diferentes e as duas precisam ser corrigidas para o destino.
        bairros_origem = [b for b in bairros_origem if b != bairro_destino]
        if not bairros_origem:
            return jsonify({"error": "bairro_destino não pode ser o único bairro selecionado"}), 400
        affected = query(
            "UPDATE hub_negocios SET bairro = %s WHERE bairro = ANY(%s)",
            (bairro_destino, bairros_origem), commit=True
        )
        _cache_invalidar()
        return jsonify({"ok": True, "affected": affected})

    if action == "normalizar_case_bairros":
        # escolhas: { "aldeia da serra": "Aldeia da Serra", "vila mariana": "Vila Mariana", ... }
        escolhas = data.get("escolhas") or {}
        if not isinstance(escolhas, dict) or not escolhas:
            return jsonify({"error": "Nenhuma escolha de padronização informada"}), 400
        total_afetado = 0
        for chave, bairro_escolhido in escolhas.items():
            bairro_escolhido = (bairro_escolhido or "").strip()
            if not bairro_escolhido:
                continue
            # atualiza só quem está com a mesma grafia (ignorando maiúsculas/minúsculas e espaços nas pontas)
            # e é diferente do valor escolhido, pra não fazer update desnecessário
            total_afetado += query(
                "UPDATE hub_negocios SET bairro = %s WHERE lower(trim(bairro)) = lower(trim(%s)) AND bairro <> %s",
                (bairro_escolhido, chave, bairro_escolhido), commit=True
            )
        _cache_invalidar()
        return jsonify({"ok": True, "affected": total_afetado})

    ids    = [int(i) for i in data.get("ids", []) if str(i).isdigit()]
    hub_id = data.get("hub_id")

    if not ids:
        return jsonify({"error": "Nenhum negócio selecionado"}), 400

    if action == "ativar":
        query("UPDATE hub_negocios SET ativo = TRUE WHERE id = ANY(%s)", (ids,), commit=True)
    elif action == "desativar":
        query("UPDATE hub_negocios SET ativo = FALSE WHERE id = ANY(%s)", (ids,), commit=True)
    elif action == "excluir":
        query("DELETE FROM hub_negocio_hubs WHERE negocio_id = ANY(%s)", (ids,), commit=True)
        query("DELETE FROM hub_negocios WHERE id = ANY(%s)", (ids,), commit=True)
    elif action == "vincular" and hub_id:
        hub_id = int(hub_id)
        for nid in ids:
            query("""
                INSERT INTO hub_negocio_hubs (negocio_id, hub_id)
                VALUES (%s, %s) ON CONFLICT (negocio_id, hub_id) DO NOTHING
            """, (nid, hub_id), commit=True)
    elif action == "desvincular" and hub_id:
        hub_id = int(hub_id)
        query(
            "DELETE FROM hub_negocio_hubs WHERE negocio_id = ANY(%s) AND hub_id = %s",
            (ids, hub_id), commit=True
        )
    elif action == "mudar_categoria":
        categoria_id = data.get("categoria_id")
        if not categoria_id:
            return jsonify({"error": "categoria_id obrigatório"}), 400
        categoria_id = int(categoria_id)
        query(
            "UPDATE hub_negocios SET categoria_id = %s WHERE id = ANY(%s)",
            (categoria_id, ids), commit=True
        )
    elif action == "mudar_foto":
        foto_url = (data.get("foto_url") or "").strip()
        if not foto_url:
            return jsonify({"error": "foto_url obrigatório"}), 400
        query(
            "UPDATE hub_negocios SET foto_url = %s WHERE id = ANY(%s)",
            (foto_url, ids), commit=True
        )
    else:
        return jsonify({"error": "Ação inválida"}), 400

    _cache_invalidar()
    return jsonify({"ok": True, "affected": len(ids)})


# ════════════════════════════════════════════════════════════
#  ADMIN — CATEGORIAS
# ════════════════════════════════════════════════════════════

@app.route("/admin/categorias")
@login_required
def admin_categorias():
    categorias = query("SELECT * FROM hub_categorias ORDER BY nome")
    return jsonify([dict(c) for c in categorias])


@app.route("/admin/categorias/nova", methods=["POST"])
@login_required
def admin_categoria_nova():
    d = request.form
    query("""
        INSERT INTO hub_categorias (nome, slug, icone_url, ativo)
        VALUES (%s, %s, %s, %s)
    """, (d["nome"], d["slug"], d.get("icone_url") or None, "ativo" in d), commit=True)
    return jsonify({"ok": True})


@app.route("/admin/categorias/<int:cat_id>/editar", methods=["GET", "POST"])
@login_required
def admin_categoria_editar(cat_id):
    cat = query("SELECT * FROM hub_categorias WHERE id = %s", (cat_id,), one=True)
    if not cat:
        return jsonify({"erro": "Categoria não encontrada"}), 404
    if request.method == "POST":
        d = request.form
        query("""
            UPDATE hub_categorias SET nome=%s, slug=%s, icone_url=%s, ativo=%s
            WHERE id=%s
        """, (d["nome"], d["slug"], d.get("icone_url") or None, "ativo" in d, cat_id), commit=True)
        return jsonify({"ok": True})
    return jsonify(dict(cat))


@app.route("/admin/categorias/<int:cat_id>/deletar", methods=["POST"])
@login_required
def admin_categoria_deletar(cat_id):
    query("DELETE FROM hub_categorias WHERE id = %s", (cat_id,), commit=True)
    return jsonify({"ok": True})


# ════════════════════════════════════════════════════════════
#  ADMIN — USUÁRIOS
# ════════════════════════════════════════════════════════════

@app.route("/admin/usuarios")
@login_required
def admin_usuarios():
    usuarios = query("""
        SELECT u.id, u.nome, u.email, u.whatsapp, u.criado_em, a.nivel as admin_nivel
        FROM usuarios u
        LEFT JOIN admins a ON a.user_id = u.id
        ORDER BY u.criado_em DESC
    """)
    return jsonify([dict(u) for u in usuarios])


@app.route("/admin/usuarios/novo", methods=["POST"])
@login_required
def admin_usuario_novo():
    d = request.form
    senha_hash = generate_password_hash(d["senha"])
    cur = get_db().cursor()
    cur.execute("""
        INSERT INTO usuarios (nome, email, senha_hash, whatsapp)
        VALUES (%s, %s, %s, %s) RETURNING id
    """, (d["nome"], d["email"], senha_hash, d.get("whatsapp") or None))
    user_id = cur.fetchone()["id"]
    if d.get("nivel"):
        cur.execute("INSERT INTO admins (user_id, nivel) VALUES (%s, %s)", (user_id, d["nivel"]))
    get_db().commit()
    return jsonify({"ok": True})


@app.route("/admin/usuarios/<int:user_id>/editar", methods=["GET", "POST"])
@login_required
def admin_usuario_editar(user_id):
    usuario = query("SELECT * FROM usuarios WHERE id = %s", (user_id,), one=True)
    if not usuario:
        return jsonify({"erro": "Usuário não encontrado"}), 404
    if request.method == "POST":
        d = request.form
        if d.get("senha"):
            senha_hash = generate_password_hash(d["senha"])
            query("UPDATE usuarios SET nome=%s, email=%s, senha_hash=%s, whatsapp=%s WHERE id=%s",
                  (d["nome"], d["email"], senha_hash, d.get("whatsapp") or None, user_id), commit=True)
        else:
            query("UPDATE usuarios SET nome=%s, email=%s, whatsapp=%s WHERE id=%s",
                  (d["nome"], d["email"], d.get("whatsapp") or None, user_id), commit=True)
        query("DELETE FROM admins WHERE user_id = %s", (user_id,), commit=True)
        if d.get("nivel"):
            query("INSERT INTO admins (user_id, nivel) VALUES (%s, %s)", (user_id, d["nivel"]), commit=True)
        return jsonify({"ok": True})
    admin = query("SELECT * FROM admins WHERE user_id = %s", (user_id,), one=True)
    data = dict(usuario)
    data.pop("senha_hash", None)
    data["admin_nivel"] = admin["nivel"] if admin else None
    return jsonify(data)


@app.route("/admin/usuarios/<int:user_id>/deletar", methods=["POST"])
@login_required
def admin_usuario_deletar(user_id):
    query("DELETE FROM usuarios WHERE id = %s", (user_id,), commit=True)
    return jsonify({"ok": True})


# ════════════════════════════════════════════════════════════
#  ADMIN — ASSINATURAS
# ════════════════════════════════════════════════════════════

@app.route("/admin/assinaturas")
@login_required
def admin_assinaturas():
    assinaturas = query("""
        SELECT a.*, u.nome as usuario_nome, u.email as usuario_email
        FROM assinaturas a
        JOIN usuarios u ON u.id = a.user_id
        ORDER BY a.criado_em DESC
    """)
    return jsonify([dict(a) for a in assinaturas])


@app.route("/admin/assinaturas/nova", methods=["POST"])
@login_required
def admin_assinatura_nova():
    d = request.form
    query("""
        INSERT INTO assinaturas (user_id, plano, status, valido_ate, mp_sub_id)
        VALUES (%s, %s, %s, %s, %s)
    """, (
        d["user_id"], d["plano"], d.get("status", "ativo"),
        d.get("valido_ate") or None, d.get("mp_sub_id") or None
    ), commit=True)
    return jsonify({"ok": True})


@app.route("/admin/assinaturas/<int:ass_id>/editar", methods=["GET", "POST"])
@login_required
def admin_assinatura_editar(ass_id):
    ass = query("SELECT * FROM assinaturas WHERE id = %s", (ass_id,), one=True)
    if not ass:
        return jsonify({"erro": "Assinatura não encontrada"}), 404
    if request.method == "POST":
        d = request.form
        query("""
            UPDATE assinaturas SET user_id=%s, plano=%s, status=%s, valido_ate=%s, mp_sub_id=%s
            WHERE id=%s
        """, (
            d["user_id"], d["plano"], d.get("status", "ativo"),
            d.get("valido_ate") or None, d.get("mp_sub_id") or None, ass_id
        ), commit=True)
        return jsonify({"ok": True})
    return jsonify(dict(ass))


@app.route("/admin/assinaturas/<int:ass_id>/deletar", methods=["POST"])
@login_required
def admin_assinatura_deletar(ass_id):
    query("DELETE FROM assinaturas WHERE id = %s", (ass_id,), commit=True)
    return jsonify({"ok": True})


# ════════════════════════════════════════════════════════════
#  ADMIN — BLOG (CRUD completo)
# ════════════════════════════════════════════════════════════

@app.route("/admin/blog")
@login_required
def admin_blog():
    posts = query("""
        SELECT p.*,
               array_agg(DISTINCT ph.hub_id) FILTER (WHERE ph.hub_id IS NOT NULL) as hub_ids,
               array_agg(DISTINCT t.nome)    FILTER (WHERE t.id      IS NOT NULL) as tag_nomes
        FROM blog_posts p
        LEFT JOIN blog_post_hubs ph ON ph.post_id = p.id
        LEFT JOIN blog_post_tags pt ON pt.post_id = p.id
        LEFT JOIN blog_tags t ON t.id = pt.tag_id
        GROUP BY p.id
        ORDER BY p.criado_em DESC
    """)
    return jsonify([dict(p) for p in posts])


@app.route("/admin/blog/novo", methods=["POST"])
@login_required
def admin_blog_novo():
    d = request.form
    cur = get_db().cursor()
    cur.execute("""
        INSERT INTO blog_posts
            (titulo, slug, resumo, conteudo, capa_url, publicado, publicado_em)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        RETURNING id
    """, (
        d["titulo"], d["slug"],
        d.get("resumo") or None,
        d.get("conteudo") or None,
        d.get("capa_url") or None,
        "publicado" in d,
        d.get("publicado_em") or None,
    ))
    post_id = cur.fetchone()["id"]

    for hub_id in request.form.getlist("hubs"):
        cur.execute("""
            INSERT INTO blog_post_hubs (post_id, hub_id)
            VALUES (%s, %s) ON CONFLICT DO NOTHING
        """, (post_id, int(hub_id)))

    for tag_slug in request.form.getlist("tags"):
        tag_slug = tag_slug.strip().lower().replace(" ", "-")
        if not tag_slug:
            continue
        cur.execute("""
            INSERT INTO blog_tags (nome, slug)
            VALUES (%s, %s)
            ON CONFLICT (slug) DO UPDATE SET nome = EXCLUDED.nome
            RETURNING id
        """, (tag_slug.replace("-", " ").title(), tag_slug))
        tag_id = cur.fetchone()["id"]
        cur.execute("""
            INSERT INTO blog_post_tags (post_id, tag_id)
            VALUES (%s, %s) ON CONFLICT DO NOTHING
        """, (post_id, tag_id))

    get_db().commit()
    return jsonify({"ok": True, "id": post_id})


@app.route("/admin/blog/<int:post_id>/editar", methods=["GET", "POST"])
@login_required
def admin_blog_editar(post_id):
    post = query("SELECT * FROM blog_posts WHERE id = %s", (post_id,), one=True)
    if not post:
        return jsonify({"erro": "Post não encontrado"}), 404

    if request.method == "POST":
        d = request.form
        query("""
            UPDATE blog_posts SET
                titulo=%s, slug=%s, resumo=%s, conteudo=%s,
                capa_url=%s, publicado=%s, publicado_em=%s
            WHERE id=%s
        """, (
            d["titulo"], d["slug"],
            d.get("resumo") or None,
            d.get("conteudo") or None,
            d.get("capa_url") or None,
            "publicado" in d,
            d.get("publicado_em") or None,
            post_id,
        ), commit=True)

        query("DELETE FROM blog_post_hubs WHERE post_id = %s", (post_id,), commit=True)
        for hub_id in request.form.getlist("hubs"):
            query("""
                INSERT INTO blog_post_hubs (post_id, hub_id)
                VALUES (%s, %s) ON CONFLICT DO NOTHING
            """, (post_id, int(hub_id)), commit=True)

        query("DELETE FROM blog_post_tags WHERE post_id = %s", (post_id,), commit=True)
        cur = get_db().cursor()
        for tag_slug in request.form.getlist("tags"):
            tag_slug = tag_slug.strip().lower().replace(" ", "-")
            if not tag_slug:
                continue
            cur.execute("""
                INSERT INTO blog_tags (nome, slug)
                VALUES (%s, %s)
                ON CONFLICT (slug) DO UPDATE SET nome = EXCLUDED.nome
                RETURNING id
            """, (tag_slug.replace("-", " ").title(), tag_slug))
            tag_id = cur.fetchone()["id"]
            cur.execute("""
                INSERT INTO blog_post_tags (post_id, tag_id)
                VALUES (%s, %s) ON CONFLICT DO NOTHING
            """, (post_id, tag_id))
        get_db().commit()
        return jsonify({"ok": True})

    hubs_do_post = [r["hub_id"] for r in query(
        "SELECT hub_id FROM blog_post_hubs WHERE post_id = %s", (post_id,)
    )]
    tags_do_post = [r["slug"] for r in query("""
        SELECT t.slug FROM blog_tags t
        JOIN blog_post_tags pt ON pt.tag_id = t.id
        WHERE pt.post_id = %s
    """, (post_id,))]
    data = dict(post)
    data["hubs_do_post"] = hubs_do_post
    data["tags_do_post"] = tags_do_post
    return jsonify(data)


@app.route("/admin/blog/<int:post_id>/deletar", methods=["POST"])
@login_required
def admin_blog_deletar(post_id):
    query("DELETE FROM blog_post_tags WHERE post_id = %s", (post_id,), commit=True)
    query("DELETE FROM blog_post_hubs WHERE post_id = %s", (post_id,), commit=True)
    query("DELETE FROM blog_posts WHERE id = %s", (post_id,), commit=True)
    return jsonify({"ok": True})


@app.route("/admin/blog/tags")
@login_required
def admin_blog_tags():
    tags = query("""
        SELECT t.*, COUNT(pt.post_id) as total_posts
        FROM blog_tags t
        LEFT JOIN blog_post_tags pt ON pt.tag_id = t.id
        GROUP BY t.id ORDER BY t.nome
    """)
    return jsonify([dict(t) for t in tags])


@app.route("/admin/blog/tags/nova", methods=["POST"])
@login_required
def admin_blog_tag_nova():
    d = request.form
    query("""
        INSERT INTO blog_tags (nome, slug)
        VALUES (%s, %s) ON CONFLICT (slug) DO NOTHING
    """, (d["nome"], d["slug"]), commit=True)
    return jsonify({"ok": True})


@app.route("/admin/blog/tags/<int:tag_id>/editar", methods=["GET", "POST"])
@login_required
def admin_blog_tag_editar(tag_id):
    tag = query("SELECT * FROM blog_tags WHERE id = %s", (tag_id,), one=True)
    if not tag:
        return jsonify({"erro": "Tag não encontrada"}), 404
    if request.method == "POST":
        d = request.form
        query("UPDATE blog_tags SET nome=%s, slug=%s WHERE id=%s",
              (d["nome"], d["slug"], tag_id), commit=True)
        return jsonify({"ok": True})
    return jsonify(dict(tag))


@app.route("/admin/blog/tags/<int:tag_id>/deletar", methods=["POST"])
@login_required
def admin_blog_tag_deletar(tag_id):
    query("DELETE FROM blog_post_tags WHERE tag_id = %s", (tag_id,), commit=True)
    query("DELETE FROM blog_tags WHERE id = %s", (tag_id,), commit=True)
    return jsonify({"ok": True})


# ════════════════════════════════════════════════════════════
#  Admin — Anúncios
# ════════════════════════════════════════════════════════════

@app.route("/admin/anuncios")
@login_required
def admin_anuncios():
    anuncios = query("""
        SELECT a.*, h.nome as hub_nome
        FROM anuncios a
        LEFT JOIN hub_clientes h ON h.id = a.hub_id
        ORDER BY a.id DESC
    """)
    return jsonify([dict(a) for a in anuncios])


def _parse_lista_localidades(valor):
    """Converte a string 'a, b, c' (vinda do hidden input do seletor de
    cidades/bairros) numa lista limpa, sem vazios e sem duplicatas."""
    if not valor:
        return []
    vistos = set()
    resultado = []
    for item in valor.split(","):
        item = item.strip()
        if item and item.lower() not in vistos:
            vistos.add(item.lower())
            resultado.append(item)
    return resultado


@app.route("/admin/anuncios/novo", methods=["POST"])
@login_required
def admin_anuncio_novo():
    f = request.form
    ativo = f.get("ativo") in ("on", "true", "1", True)
    cidades = _parse_lista_localidades(f.get("cidades"))
    bairros = _parse_lista_localidades(f.get("bairros"))
    query("""
        INSERT INTO anuncios (hub_id, titulo, foto_url, link, posicao,
                              categoria_slug, cidade, bairro, cidades, bairros,
                              data_inicio, data_fim, ativo)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    """, (
        f.get("hub_id") or None,
        f.get("titulo"),
        f.get("foto_url"),
        f.get("link"),
        f.get("posicao", "topo"),
        f.get("categoria_slug") or None,
        cidades[0] if cidades else None,
        bairros[0] if bairros else None,
        cidades or None,
        bairros or None,
        f.get("data_inicio") or None,
        f.get("data_fim") or None,
        ativo,
    ), commit=True)
    return jsonify({"ok": True})


@app.route("/admin/anuncios/<int:anuncio_id>/editar", methods=["GET", "POST"])
@login_required
def admin_anuncio_editar(anuncio_id):
    if request.method == "GET":
        row = query("SELECT * FROM anuncios WHERE id = %s", (anuncio_id,), one=True)
        if not row:
            return jsonify({"erro": "Não encontrado"}), 404
        return jsonify(dict(row))
    f = request.form
    ativo = f.get("ativo") in ("on", "true", "1", True)
    cidades = _parse_lista_localidades(f.get("cidades"))
    bairros = _parse_lista_localidades(f.get("bairros"))
    query("""
        UPDATE anuncios SET
            hub_id = %s, titulo = %s, foto_url = %s, link = %s, posicao = %s,
            categoria_slug = %s, cidade = %s, bairro = %s, cidades = %s, bairros = %s,
            data_inicio = %s, data_fim = %s, ativo = %s
        WHERE id = %s
    """, (
        f.get("hub_id") or None,
        f.get("titulo"),
        f.get("foto_url"),
        f.get("link"),
        f.get("posicao", "topo"),
        f.get("categoria_slug") or None,
        cidades[0] if cidades else None,
        bairros[0] if bairros else None,
        cidades or None,
        bairros or None,
        f.get("data_inicio") or None,
        f.get("data_fim") or None,
        ativo,
        anuncio_id,
    ), commit=True)
    return jsonify({"ok": True})


@app.route("/admin/anuncios/<int:anuncio_id>/deletar", methods=["POST"])
@login_required
def admin_anuncio_deletar(anuncio_id):
    query("DELETE FROM anuncios WHERE id = %s", (anuncio_id,), commit=True)
    return jsonify({"ok": True})


# ════════════════════════════════════════════════════════════
#  Admin — Pendentes
# ════════════════════════════════════════════════════════════

@app.route("/admin/pendentes")
@login_required
def admin_pendentes():
    hub_id = request.args.get("hub_id", "").strip()
    status = request.args.get("status", "pendente").strip()

    condicoes = ["p.status = %s"]
    params    = [status]

    if hub_id:
        condicoes.append("p.hub_id = %s")
        params.append(hub_id)

    where = " AND ".join(condicoes)

    pendentes = query(f"""
        SELECT p.*, h.nome as hub_nome, c.nome as categoria_nome
        FROM hub_negocios_pendentes p
        LEFT JOIN hub_clientes h ON h.id = p.hub_id
        LEFT JOIN hub_categorias c ON c.id = p.categoria_id
        WHERE {where}
        ORDER BY p.criado_em DESC
    """, params)

    return jsonify([dict(p) for p in pendentes])


@app.route("/admin/pendentes/<int:pendente_id>/aprovar", methods=["POST"])
@login_required
def admin_pendente_aprovar(pendente_id):
    p = query("SELECT * FROM hub_negocios_pendentes WHERE id = %s", (pendente_id,), one=True)
    if not p:
        return jsonify({"erro": "Não encontrado"}), 404
    if p["status"] != "pendente":
        return jsonify({"erro": f"Cadastro já está '{p['status']}'"}), 400

    base_slug = _slugify(p["nome"])
    slug      = base_slug
    contador  = 1
    while query("SELECT id FROM hub_negocios WHERE slug = %s", (slug,), one=True):
        slug = f"{base_slug}-{contador}"
        contador += 1

    db  = get_db()
    cur = db.cursor()

    cidade_norm, bairro_norm = _normalizar_cidade_bairro(p["cidade"], p["bairro"])

    cur.execute("""
        INSERT INTO hub_negocios
            (categoria_id, nome, slug, descricao, foto_url,
             endereco, bairro, cidade,
             whatsapp, telefone, instagram, site_url,
             mostrar_foto, mostrar_descricao, mostrar_whatsapp,
             mostrar_instagram, mostrar_telefone, mostrar_site,
             mostrar_endereco, mostrar_mapa, ativo)
        VALUES
            (%s,%s,%s,%s,%s,
             %s,%s,%s,
             %s,%s,%s,%s,
             true,true,true,
             true,true,true,
             true,false,true)
        RETURNING id
    """, (
        p["categoria_id"], p["nome"], slug,
        p["descricao"], p["foto_url"],
        p["endereco"], bairro_norm, cidade_norm,
        p["whatsapp"], p["telefone"], p["instagram"], p["site_url"],
    ))
    negocio_id = cur.fetchone()["id"]

    cur.execute("""
        INSERT INTO hub_negocio_hubs (negocio_id, hub_id)
        VALUES (%s, %s) ON CONFLICT DO NOTHING
    """, (negocio_id, p["hub_id"]))

    cur.execute("""
        UPDATE hub_negocios_pendentes SET status = 'aprovado' WHERE id = %s
    """, (pendente_id,))

    db.commit()
    _cache_invalidar()
    return jsonify({"ok": True, "negocio_id": negocio_id, "slug": slug})


@app.route("/admin/pendentes/<int:pendente_id>/rejeitar", methods=["POST"])
@login_required
def admin_pendente_rejeitar(pendente_id):
    p = query("SELECT id FROM hub_negocios_pendentes WHERE id = %s", (pendente_id,), one=True)
    if not p:
        return jsonify({"erro": "Não encontrado"}), 404
    query("UPDATE hub_negocios_pendentes SET status = 'rejeitado' WHERE id = %s",
          (pendente_id,), commit=True)
    return jsonify({"ok": True})


@app.route("/admin/pendentes/<int:pendente_id>/deletar", methods=["POST"])
@login_required
def admin_pendente_deletar(pendente_id):
    query("DELETE FROM hub_negocios_pendentes WHERE id = %s", (pendente_id,), commit=True)
    return jsonify({"ok": True})


# ════════════════════════════════════════════════════════════
#  API pública — JSON
# ════════════════════════════════════════════════════════════

@app.route("/api/hub/negocios")
def api_negocios():
    hub = get_hub_by_host()
    if not hub:
        return jsonify({"erro": "Hub não encontrado"}), 404
    categoria = request.args.get("categoria")
    bairro    = request.args.get("bairro")
    cidade    = request.args.get("cidade")
    # Categorias com volume muito maior que a média (ex.: pontos de ônibus, que
    # numa cidade grande passam de milhares de registros). Pra essas, o corte de
    # 200 em ordem alfabética escondia o resultado mais próximo de verdade —
    # então liberamos um teto bem mais alto só pra elas. Ajuste essa lista
    # conforme identificar outras categorias com o mesmo problema.
    CATEGORIAS_TETO_ALTO = {"ponto-de-onibus"}
    teto = 20000 if categoria in CATEGORIAS_TETO_ALTO else 4000
    try:
        limit  = min(int(request.args.get("limit",  96)), teto)
        offset = max(int(request.args.get("offset",  0)),   0)
    except (ValueError, TypeError):
        limit, offset = 96, 0
    sql = """
        SELECT n.*, c.nome as categoria_nome, c.slug as categoria_slug
        FROM hub_negocios n
        JOIN hub_negocio_hubs nh ON nh.negocio_id = n.id
        LEFT JOIN hub_categorias c ON c.id = n.categoria_id
        WHERE nh.hub_id = %s AND n.ativo = true
    """
    params = [hub["id"]]
    if categoria:
        sql += " AND c.slug = %s"
        params.append(categoria)
    cidade_var = _variantes_cidade(hub["id"], cidade) if cidade else None
    if cidade_var:
        sql += " AND n.cidade = ANY(%s)"
        params.append(cidade_var)
    if bairro:
        sql += " AND n.bairro = ANY(%s)"
        params.append(_variantes_bairro(hub["id"], bairro, cidade_var))
    sql += " ORDER BY n.nome LIMIT %s OFFSET %s"
    params += [limit, offset]
    negocios = query(sql, params)
    negocios_json = [dict(n) for n in negocios]
    for n in negocios_json:
        n["exibir_geo"] = n["categoria_slug"] not in CATEGORIAS_SEM_GEOLOCALIZACAO
    return jsonify(negocios_json)


@app.route("/api/hub/categorias")
def api_categorias():
    categorias = query("SELECT * FROM hub_categorias WHERE ativo = true ORDER BY nome")
    return jsonify([dict(c) for c in categorias])


@app.route("/api/hub/cidades")
def api_cidades():
    """Retorna todas as cidades com contagem de negócios — leve, sem trazer todos os negócios."""
    hub = get_hub_by_host()
    if not hub:
        return jsonify({"erro": "Hub não encontrado"}), 404
    cidades = query("""
        SELECT n.cidade, COUNT(*) as total
        FROM hub_negocios n
        JOIN hub_negocio_hubs nh ON nh.negocio_id = n.id
        WHERE nh.hub_id = %s AND n.ativo = true
          AND n.cidade IS NOT NULL AND TRIM(n.cidade) != ''
        GROUP BY n.cidade
        ORDER BY total DESC, n.cidade
    """, (hub["id"],))
    return jsonify([dict(c) for c in cidades])


@app.route("/api/hub/blog")
def api_blog():
    """Posts do hub atual, paginados. Aceita ?tag=slug&limit=12&offset=0"""
    hub = get_hub_by_host()
    if not hub:
        return jsonify({"erro": "Hub não encontrado"}), 404
    tag = request.args.get("tag", "").strip()
    try:
        limit  = min(int(request.args.get("limit",  12)), 50)
        offset = max(int(request.args.get("offset",  0)),  0)
    except (ValueError, TypeError):
        limit, offset = 12, 0
    sql = """
        SELECT p.id, p.titulo, p.slug, p.resumo, p.capa_url, p.publicado_em,
               array_agg(t.slug ORDER BY t.nome) FILTER (WHERE t.id IS NOT NULL) as tags
        FROM blog_posts p
        JOIN blog_post_hubs ph ON ph.post_id = p.id
        LEFT JOIN blog_post_tags pt ON pt.post_id = p.id
        LEFT JOIN blog_tags t ON t.id = pt.tag_id
        WHERE ph.hub_id = %s AND p.publicado = true
    """
    params = [hub["id"]]
    if tag:
        sql += " AND EXISTS (SELECT 1 FROM blog_post_tags pt2 JOIN blog_tags t2 ON t2.id = pt2.tag_id WHERE pt2.post_id = p.id AND t2.slug = %s)"
        params.append(tag)
    sql += " GROUP BY p.id ORDER BY p.publicado_em DESC LIMIT %s OFFSET %s"
    params += [limit, offset]
    posts = query(sql, params)
    return jsonify([dict(p) for p in posts])


# ════════════════════════════════════════════════════════════

if __name__ == "__main__":
    app.run(debug=True, port=5000)
