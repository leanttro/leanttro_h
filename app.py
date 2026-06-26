from flask import Flask, render_template, request, jsonify, redirect, session, g, Response
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.middleware.proxy_fix import ProxyFix
from functools import wraps
from datetime import datetime, timezone
import psycopg2
import psycopg2.extras
import os
import glob
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "leanttro_hub_secret_2026")
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)
app.url_map.strict_slashes = False

DOMINIO_BASE = os.getenv("DOMINIO_BASE", "leanttro.com")

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
            host     = os.getenv("DB_HOST", "213.199.56.207"),
            port     = int(os.getenv("DB_PORT", 5452)),
            dbname   = os.getenv("DB_NAME", "postgres"),
            user     = os.getenv("DB_USER", "leanttro"),
            password = os.getenv("DB_PASS", "Fin@2021"),
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

# ════════════════════════════════════════════════════════════
#  ROTAS PÚBLICAS DO HUB
# ════════════════════════════════════════════════════════════

ROTAS_RESERVADAS = {"admin", "static", "favicon.ico", "robots.txt", "sitemap.xml"}

@app.route("/")
def index():
    hub = get_hub_by_host()
    if not hub:
        return "Hub não encontrado", 404
    categorias = query("SELECT * FROM hub_categorias WHERE ativo = true ORDER BY nome")
    negocios = query("""
        SELECT n.*, c.nome as categoria_nome, c.slug as categoria_slug
        FROM hub_negocios n
        JOIN hub_negocio_hubs nh ON nh.negocio_id = n.id
        JOIN hub_categorias c ON c.id = n.categoria_id
        WHERE nh.hub_id = %s AND n.ativo = true
        ORDER BY n.nome
    """, (hub["id"],))
    template = hub.get("template_index") or "index_padrao"
    return render_template(f"hub/{template}.html", hub=hub, negocios=negocios, categorias=categorias)


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

    # Negócios ativos com categoria e data de atualização
    negocios = query("""
        SELECT n.slug, n.bairro,
               n.criado_em as atualizado_em,
               c.slug as categoria_slug
        FROM hub_negocios n
        JOIN hub_negocio_hubs nh ON nh.negocio_id = n.id
        JOIN hub_categorias c ON c.id = n.categoria_id
        WHERE nh.hub_id = %s AND n.ativo = true
        ORDER BY n.nome
    """, (hub["id"],))

    # Segmentos únicos (categoria ou bairro conforme tipo do hub)
    eh_bairro = hub.get("tipo") == "bairro"
    segmentos_vistos = set()
    segmentos = []
    for n in negocios:
        seg = n["bairro"].lower() if eh_bairro and n["bairro"] else n["categoria_slug"]
        if seg and seg not in segmentos_vistos:
            segmentos_vistos.add(seg)
            segmentos.append(seg)

    def fmt_date(val):
        if not val:
            return hoje
        if hasattr(val, "strftime"):
            return val.strftime("%Y-%m-%d")
        return str(val)[:10]

    urls = []

    # Página inicial
    urls.append({"loc": f"{base_url}/", "lastmod": hoje, "priority": "1.0", "changefreq": "weekly"})

    # Páginas de filtro (categoria ou bairro)
    for seg in segmentos:
        urls.append({"loc": f"{base_url}/{seg}/", "lastmod": hoje, "priority": "0.8", "changefreq": "weekly"})

    # Páginas individuais de negócio
    for n in negocios:
        seg = n["bairro"].lower() if eh_bairro and n["bairro"] else n["categoria_slug"]
        if not seg:
            continue
        urls.append({
            "loc": f"{base_url}/{seg}/{n['slug']}/",
            "lastmod": fmt_date(n["atualizado_em"]),
            "priority": "0.6",
            "changefreq": "monthly",
        })

    linhas = ['<?xml version="1.0" encoding="UTF-8"?>']
    linhas.append('<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">')
    for u in urls:
        linhas.append("  <url>")
        linhas.append(f"    <loc>{u['loc']}</loc>")
        linhas.append(f"    <lastmod>{u['lastmod']}</lastmod>")
        linhas.append(f"    <changefreq>{u['changefreq']}</changefreq>")
        linhas.append(f"    <priority>{u['priority']}</priority>")
        linhas.append("  </url>")
    linhas.append("</urlset>")

    return Response("\n".join(linhas), mimetype="application/xml")


@app.route("/<segmento>/")
def pagina_filtro(segmento):
    if segmento in ROTAS_RESERVADAS:
        return "Não encontrado", 404
    hub = get_hub_by_host()
    if not hub:
        return "Hub não encontrado", 404
    if hub["tipo"] == "bairro":
        negocios = query("""
            SELECT n.*, c.nome as categoria_nome, c.slug as categoria_slug
            FROM hub_negocios n
            JOIN hub_negocio_hubs nh ON nh.negocio_id = n.id
            JOIN hub_categorias c ON c.id = n.categoria_id
            WHERE nh.hub_id = %s AND n.ativo = true AND LOWER(n.bairro) = LOWER(%s)
            ORDER BY n.nome
        """, (hub["id"], segmento))
        filtro_tipo, filtro_valor = "bairro", segmento
    elif hub["tipo"] == "cidade":
        # Tenta categoria primeiro, senão trata como bairro
        categoria = query(
            "SELECT * FROM hub_categorias WHERE slug = %s AND ativo = true",
            (segmento,), one=True
        )
        if categoria:
            negocios = query("""
                SELECT n.*, c.nome as categoria_nome, c.slug as categoria_slug
                FROM hub_negocios n
                JOIN hub_negocio_hubs nh ON nh.negocio_id = n.id
                JOIN hub_categorias c ON c.id = n.categoria_id
                WHERE nh.hub_id = %s AND n.ativo = true AND c.slug = %s
                ORDER BY n.nome
            """, (hub["id"], segmento))
            filtro_tipo, filtro_valor = "categoria", segmento
        else:
            negocios = query("""
                SELECT n.*, c.nome as categoria_nome, c.slug as categoria_slug
                FROM hub_negocios n
                JOIN hub_negocio_hubs nh ON nh.negocio_id = n.id
                JOIN hub_categorias c ON c.id = n.categoria_id
                WHERE nh.hub_id = %s AND n.ativo = true AND LOWER(n.bairro) = LOWER(%s)
                ORDER BY n.nome
            """, (hub["id"], segmento))
            filtro_tipo, filtro_valor = "bairro", segmento
    else:
        negocios = query("""
            SELECT n.*, c.nome as categoria_nome, c.slug as categoria_slug
            FROM hub_negocios n
            JOIN hub_negocio_hubs nh ON nh.negocio_id = n.id
            JOIN hub_categorias c ON c.id = n.categoria_id
            WHERE nh.hub_id = %s AND n.ativo = true AND c.slug = %s
            ORDER BY n.nome
        """, (hub["id"], segmento))
        filtro_tipo, filtro_valor = "categoria", segmento
    categorias = query("SELECT * FROM hub_categorias WHERE ativo = true ORDER BY nome")
    template = hub.get("template_filtro") or "filtro_padrao"
    return render_template(f"hub/{template}.html", hub=hub, negocios=negocios,
                           categorias=categorias, filtro_tipo=filtro_tipo,
                           filtro_valor=filtro_valor, segmento=segmento)


@app.route("/<segmento>/<slug_negocio>/")
def pagina_negocio(segmento, slug_negocio):
    hub = get_hub_by_host()
    if not hub:
        return "Hub não encontrado", 404
    negocio = query("""
        SELECT n.*, c.nome as categoria_nome, c.slug as categoria_slug
        FROM hub_negocios n
        JOIN hub_negocio_hubs nh ON nh.negocio_id = n.id
        JOIN hub_categorias c ON c.id = n.categoria_id
        WHERE nh.hub_id = %s AND n.slug = %s AND n.ativo = true
    """, (hub["id"], slug_negocio), one=True)
    if not negocio:
        return "Negócio não encontrado", 404
    query("UPDATE hub_negocios SET visualizacoes = visualizacoes + 1 WHERE id = %s",
          (negocio["id"],), commit=True)
    template = hub.get("template_negocio") or "negocio_padrao"
    return render_template(f"hub/{template}.html", hub=hub, negocio=negocio, segmento=segmento)


# ════════════════════════════════════════════════════════════
#  ROTAS DE CIDADE — NOVAS (não tocam nas existentes)
# ════════════════════════════════════════════════════════════

import unicodedata as _uc

def _slugify(texto):
    if not texto:
        return ""
    texto = _uc.normalize("NFD", texto.lower())
    texto = "".join(c for c in texto if _uc.category(c) != "Mn")
    return texto.replace(" ", "-").strip("-")


def _resolve_bairro(hub_id, bairro_slug, cidade_nome=None):
    """Devolve o nome real do bairro a partir do slug."""
    bairro_slug_norm = _slugify(bairro_slug)
    sql = """
        SELECT DISTINCT n.bairro
        FROM hub_negocios n
        JOIN hub_negocio_hubs nh ON nh.negocio_id = n.id
        WHERE nh.hub_id = %s AND n.ativo = true AND n.bairro IS NOT NULL
    """
    params = [hub_id]
    if cidade_nome:
        sql += " AND LOWER(TRIM(n.cidade)) = LOWER(%s)"
        params.append(cidade_nome)
    rows = query(sql, params)
    for row in rows:
        if _slugify(row["bairro"]) == bairro_slug_norm:
            return row["bairro"]
    return None


def _resolve_cidade(hub_id, cidade_slug):
    """Devolve o nome real da cidade a partir do slug."""
    # Normaliza o slug recebido (remove acentos que o browser pode mandar)
    cidade_slug_norm = _slugify(cidade_slug)
    rows = query("""
        SELECT DISTINCT n.cidade
        FROM hub_negocios n
        JOIN hub_negocio_hubs nh ON nh.negocio_id = n.id
        WHERE nh.hub_id = %s AND n.ativo = true AND n.cidade IS NOT NULL
    """, (hub_id,))
    for row in rows:
        if _slugify(row["cidade"]) == cidade_slug_norm:
            return row["cidade"]
    return None


def _negocios_cidade(hub_id, cidade_nome=None, cat_slug=None, bairro_slug=None):
    sql = """
        SELECT n.*, c.nome as categoria_nome, c.slug as categoria_slug
        FROM hub_negocios n
        JOIN hub_negocio_hubs nh ON nh.negocio_id = n.id
        JOIN hub_categorias c ON c.id = n.categoria_id
        WHERE nh.hub_id = %s AND n.ativo = true
    """
    params = [hub_id]
    if cidade_nome:
        sql += " AND LOWER(TRIM(n.cidade)) = LOWER(%s)"
        params.append(cidade_nome)
    if cat_slug:
        sql += " AND c.slug = %s"
        params.append(cat_slug)
    if bairro_slug:
        sql += " AND LOWER(TRIM(n.bairro)) = LOWER(TRIM(%s))"
        params.append(bairro_slug)
    sql += " ORDER BY n.nome"
    return query(sql, params)


def _bairros_cidade(hub_id, cidade_nome):
    rows = query("""
        SELECT DISTINCT n.bairro
        FROM hub_negocios n
        JOIN hub_negocio_hubs nh ON nh.negocio_id = n.id
        WHERE nh.hub_id = %s AND n.ativo = true
          AND n.bairro IS NOT NULL AND TRIM(n.bairro) != ''
          AND LOWER(TRIM(n.cidade)) = LOWER(%s)
        ORDER BY n.bairro
    """, (hub_id, cidade_nome))
    return [r["bairro"] for r in rows]


def _categorias_cidade(hub_id, cidade_nome):
    return query("""
        SELECT DISTINCT c.id, c.nome, c.slug, c.icone_url
        FROM hub_categorias c
        JOIN hub_negocios n ON n.categoria_id = c.id
        JOIN hub_negocio_hubs nh ON nh.negocio_id = n.id
        WHERE nh.hub_id = %s AND n.ativo = true AND c.ativo = true
          AND LOWER(TRIM(n.cidade)) = LOWER(%s)
        ORDER BY c.nome
    """, (hub_id, cidade_nome))


def _render_cidade(hub, negocios, categorias, cidade_nome,
                   bairro=None, categoria=None,
                   bairros_disponiveis=None, categorias_disponiveis=None):
    template = hub.get("template_cidade") or "cidade_otp"
    return render_template(
        f"hub/{template}.html",
        hub=hub,
        negocios=negocios,
        categorias=categorias,
        cidade=cidade_nome,
        bairro=bairro,
        categoria=categoria,
        bairros_disponiveis=bairros_disponiveis or [],
        categorias_disponiveis=categorias_disponiveis or [],
    )


@app.route("/cidade/<cidade_slug>/")
def pagina_cidade(cidade_slug):
    hub = get_hub_by_host()
    if not hub:
        return "Hub não encontrado", 404
    cidade_nome = _resolve_cidade(hub["id"], cidade_slug)
    if not cidade_nome:
        return "Cidade não encontrada", 404
    negocios   = _negocios_cidade(hub["id"], cidade_nome=cidade_nome)
    categorias = query("SELECT * FROM hub_categorias WHERE ativo = true ORDER BY nome")
    bairros    = _bairros_cidade(hub["id"], cidade_nome)
    cats_disp  = _categorias_cidade(hub["id"], cidade_nome)
    return _render_cidade(hub, negocios, categorias, cidade_nome,
                          bairros_disponiveis=bairros,
                          categorias_disponiveis=cats_disp)


@app.route("/cidade/<cidade_slug>/<segundo_slug>/")
def pagina_cidade_segundo(cidade_slug, segundo_slug):
    """Detecta automaticamente se o segundo segmento é categoria ou bairro."""
    hub = get_hub_by_host()
    if not hub:
        return "Hub não encontrado", 404
    cidade_nome = _resolve_cidade(hub["id"], cidade_slug)
    if not cidade_nome:
        return "Cidade não encontrada", 404
    categoria = query(
        "SELECT * FROM hub_categorias WHERE slug = %s AND ativo = true",
        (segundo_slug,), one=True
    )
    categorias = query("SELECT * FROM hub_categorias WHERE ativo = true ORDER BY nome")
    bairros    = _bairros_cidade(hub["id"], cidade_nome)
    cats_disp  = _categorias_cidade(hub["id"], cidade_nome)
    if categoria:
        negocios = _negocios_cidade(hub["id"], cidade_nome=cidade_nome, cat_slug=segundo_slug)
        return _render_cidade(hub, negocios, categorias, cidade_nome,
                              categoria=dict(categoria),
                              bairros_disponiveis=bairros,
                              categorias_disponiveis=cats_disp)
    else:
        bairro_nome = _resolve_bairro(hub["id"], segundo_slug, cidade_nome) or segundo_slug.replace("-", " ").title()
        negocios    = _negocios_cidade(hub["id"], cidade_nome=cidade_nome, bairro_slug=bairro_nome)
        return _render_cidade(hub, negocios, categorias, cidade_nome,
                              bairro=bairro_nome,
                              bairros_disponiveis=bairros,
                              categorias_disponiveis=cats_disp)


@app.route("/cidade/<cidade_slug>/<bairro_slug>/<cat_slug>/")
def pagina_cidade_bairro_cat(cidade_slug, bairro_slug, cat_slug):
    hub = get_hub_by_host()
    if not hub:
        return "Hub não encontrado", 404
    cidade_nome = _resolve_cidade(hub["id"], cidade_slug)
    if not cidade_nome:
        return "Cidade não encontrada", 404
    categoria  = query(
        "SELECT * FROM hub_categorias WHERE slug = %s AND ativo = true",
        (cat_slug,), one=True
    )
    negocios   = _negocios_cidade(hub["id"], cidade_nome=cidade_nome,
                                   cat_slug=cat_slug, bairro_slug=bairro_slug)
    categorias = query("SELECT * FROM hub_categorias WHERE ativo = true ORDER BY nome")
    bairros    = _bairros_cidade(hub["id"], cidade_nome)
    cats_disp  = _categorias_cidade(hub["id"], cidade_nome)
    bairro_nome = _resolve_bairro(hub["id"], bairro_slug, cidade_nome) or bairro_slug.replace("-", " ").title()
    negocios    = _negocios_cidade(hub["id"], cidade_nome=cidade_nome,
                                   cat_slug=cat_slug, bairro_slug=bairro_nome)
    return _render_cidade(hub, negocios, categorias, cidade_nome,
                          bairro=bairro_nome,
                          categoria=dict(categoria) if categoria else None,
                          bairros_disponiveis=bairros,
                          categorias_disponiveis=cats_disp)


@app.route("/<cat_slug>/em/<cidade_slug>/")
def pagina_cat_cidade(cat_slug, cidade_slug):
    """/<cat_slug>/em/<cidade_slug>/ — usa /em/ para não colidir com /<seg>/<slug_negocio>/"""
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
    cidade_nome = _resolve_cidade(hub["id"], cidade_slug)
    if not cidade_nome:
        return "Cidade não encontrada", 404
    negocios   = _negocios_cidade(hub["id"], cidade_nome=cidade_nome, cat_slug=cat_slug)
    categorias = query("SELECT * FROM hub_categorias WHERE ativo = true ORDER BY nome")
    bairros    = _bairros_cidade(hub["id"], cidade_nome)
    cats_disp  = _categorias_cidade(hub["id"], cidade_nome)
    return _render_cidade(hub, negocios, categorias, cidade_nome,
                          categoria=dict(categoria),
                          bairros_disponiveis=bairros,
                          categorias_disponiveis=cats_disp)


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
    cidade_nome = _resolve_cidade(hub["id"], cidade_slug)
    if not cidade_nome:
        return "Cidade não encontrada", 404
    bairro_nome = bairro_slug.replace("-", " ").title()
    negocios    = _negocios_cidade(hub["id"], cidade_nome=cidade_nome,
                                    cat_slug=cat_slug, bairro_slug=bairro_slug)
    categorias  = query("SELECT * FROM hub_categorias WHERE ativo = true ORDER BY nome")
    bairros     = _bairros_cidade(hub["id"], cidade_nome)
    cats_disp   = _categorias_cidade(hub["id"], cidade_nome)
    return _render_cidade(hub, negocios, categorias, cidade_nome,
                          bairro=bairro_nome,
                          categoria=dict(categoria),
                          bairros_disponiveis=bairros,
                          categorias_disponiveis=cats_disp)


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
#  ADMIN — PAINEL ÚNICO (index.html)
# ════════════════════════════════════════════════════════════

@app.route("/admin")
@login_required
def admin_dashboard():
    total_hubs       = query("SELECT COUNT(*) as n FROM hub_clientes", one=True)["n"]
    total_negocios   = query("SELECT COUNT(*) as n FROM hub_negocios", one=True)["n"]
    total_categorias = query("SELECT COUNT(*) as n FROM hub_categorias", one=True)["n"]
    total_usuarios   = query("SELECT COUNT(*) as n FROM usuarios", one=True)["n"]
    return render_template("admin/index.html",
                           total_hubs=total_hubs,
                           total_negocios=total_negocios,
                           total_categorias=total_categorias,
                           total_usuarios=total_usuarios)


@app.route("/admin/templates")
@login_required
def admin_templates():
    return jsonify({
        "index":   listar_templates("index"),
        "filtro":  listar_templates("filtro"),
        "negocio": listar_templates("negocio"),
        "cidade":  listar_templates("cidade"),
    })


# ════════════════════════════════════════════════════════════
#  ADMIN — HUBS  (JSON para o painel SPA)
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
         banner_fundo_url, banner1_foto_url, banner1_link, banner2_foto_url, banner2_link,
         ativo)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
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
    negocios = query("""
        SELECT n.*, c.nome as categoria_nome
        FROM hub_negocios n
        LEFT JOIN hub_categorias c ON c.id = n.categoria_id
        ORDER BY n.criado_em DESC
    """)
    return jsonify([dict(n) for n in negocios])


@app.route("/admin/negocios/novo", methods=["POST"])
@login_required
def admin_negocio_novo():
    d = request.form
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
        d.get("endereco") or None, d.get("bairro") or None,
        d.get("cidade", "São Paulo"),
        d.get("lat") or None, d.get("lng") or None,
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
    return jsonify({"ok": True})


@app.route("/admin/negocios/<int:negocio_id>/editar", methods=["GET", "POST"])
@login_required
def admin_negocio_editar(negocio_id):
    negocio = query("SELECT * FROM hub_negocios WHERE id = %s", (negocio_id,), one=True)
    if not negocio:
        return jsonify({"erro": "Negócio não encontrado"}), 404
    if request.method == "POST":
        d = request.form
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
            d.get("endereco") or None, d.get("bairro") or None,
            d.get("cidade", "São Paulo"),
            d.get("lat") or None, d.get("lng") or None,
            d.get("whatsapp") or None, d.get("telefone") or None,
            d.get("instagram") or None, d.get("site_url") or None,
            "mostrar_foto"      in d, "mostrar_descricao" in d,
            "mostrar_whatsapp"  in d, "mostrar_instagram" in d,
            "mostrar_telefone"  in d, "mostrar_site"      in d,
            "mostrar_endereco"  in d, "mostrar_mapa"      in d,
            "ativo"             in d, negocio_id
        ), commit=True)
        query("DELETE FROM hub_negocio_hubs WHERE negocio_id = %s", (negocio_id,), commit=True)
        for hub_id in request.form.getlist("hubs"):
            query("""
                INSERT INTO hub_negocio_hubs (negocio_id, hub_id)
                VALUES (%s, %s) ON CONFLICT DO NOTHING
            """, (negocio_id, hub_id), commit=True)
        return jsonify({"ok": True})
    # GET — retorna dados + hubs vinculados
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
    return jsonify({"ok": True})


@app.route("/admin/negocios/bulk", methods=["POST"])
@login_required
def admin_negocios_bulk():
    data   = request.get_json(force=True)
    ids    = [int(i) for i in data.get("ids", []) if str(i).isdigit()]
    action = data.get("action", "")
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

    else:
        return jsonify({"error": "Ação inválida"}), 400

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
    data.pop("senha_hash", None)  # nunca expõe o hash
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
#  API pública — JSON
# ════════════════════════════════════════════════════════════

@app.route("/api/hub/negocios")
def api_negocios():
    hub = get_hub_by_host()
    if not hub:
        return jsonify({"erro": "Hub não encontrado"}), 404
    categoria = request.args.get("categoria")
    bairro    = request.args.get("bairro")
    sql = """
        SELECT n.*, c.nome as categoria_nome, c.slug as categoria_slug
        FROM hub_negocios n
        JOIN hub_negocio_hubs nh ON nh.negocio_id = n.id
        JOIN hub_categorias c ON c.id = n.categoria_id
        WHERE nh.hub_id = %s AND n.ativo = true
    """
    params = [hub["id"]]
    if categoria:
        sql += " AND c.slug = %s"
        params.append(categoria)
    if bairro:
        sql += " AND LOWER(n.bairro) = LOWER(%s)"
        params.append(bairro)
    sql += " ORDER BY n.nome"
    negocios = query(sql, params)
    return jsonify([dict(n) for n in negocios])


@app.route("/api/hub/categorias")
def api_categorias():
    categorias = query("SELECT * FROM hub_categorias WHERE ativo = true ORDER BY nome")
    return jsonify([dict(c) for c in categorias])


# ════════════════════════════════════════════════════════════

if __name__ == "__main__":
    app.run(debug=True, port=5000)
