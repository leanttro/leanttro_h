# ════════════════════════════════════════════════════════════
#  loteria.py — Blueprint "Lotérica Perto de Mim"
#  Cobre APENAS o conteúdo editorial de resultados de loteria
#  (via API da Caixa). O diretório de lotéricas físicas (listagem,
#  página de negócio, cadastro) continua 100% no app.py — não há
#  nenhuma tabela/rota nova pra isso, ver app.py (hub_negocios).
#
#  Registro (no fim do app.py, DEPOIS de get_hub_by_host/query/_cache_*
#  estarem definidos, igual ao cinema_bp):
#
#      from loteria import loteria_bp
#      app.register_blueprint(loteria_bp)
#
#  Variáveis de ambiente necessárias (banco SEPARADO do hub, chamado
#  "metro" — NÃO usa DB_HOST/DB_NAME/etc. do hub):
#      LOTERIA_DB_HOST, LOTERIA_DB_PORT, LOTERIA_DB_NAME,
#      LOTERIA_DB_USER, LOTERIA_DB_PASS
#
#  ⚠️ ATENÇÃO — SCHEMA DE loteria_resultados NÃO CONFIRMADO:
#  A tabela já existe (criada por migração separada, fora deste
#  código) e o pedido foi explícito pra NÃO criá-la nem alterá-la
#  aqui. Como eu não tenho acesso ao schema real, os nomes de coluna
#  usados no INSERT/SELECT abaixo (bloco _SQL_* no topo do arquivo)
#  são um palpite razoável baseado nos campos que a própria API da
#  Caixa devolve. CONFIRME esses nomes contra a tabela real antes de
#  rodar em produção — é só ajustar as constantes _SQL_* aqui embaixo,
#  o resto do arquivo não referencia nome de coluna solto em nenhum
#  outro lugar.
# ════════════════════════════════════════════════════════════

from flask import Blueprint, render_template, request, jsonify, g, Response
from datetime import datetime, date, timezone
import os
import re
import json
import time
import threading
import unicodedata
import psycopg2
import psycopg2.extras
import requests

loteria_bp = Blueprint("loteria", __name__)

CAIXA_BASE_URL = "https://servicebus2.caixa.gov.br/portaldeloterias/api"

# ── Jogos suportados ────────────────────────────────────────────
# chave = slug usado nas nossas URLs (/resultados/<jogo>/)
# "caixa"  = nome do jogo na URL da API da Caixa (sem hífen)
# "nome"   = nome de exibição
# "dias"   = texto informativo dos dias de sorteio (não crítico,
#            é só copy de página — a Caixa pode alterar a agenda)
JOGOS = {
    "mega-sena": {"caixa": "megasena",  "nome": "Mega-Sena",  "dias": "quartas e sábados"},
    "quina":     {"caixa": "quina",     "nome": "Quina",      "dias": "de segunda a sábado"},
    "lotofacil": {"caixa": "lotofacil", "nome": "Lotofácil",  "dias": "de segunda a sábado"},
    "lotomania": {"caixa": "lotomania", "nome": "Lotomania",  "dias": "segundas e quintas-feiras"},
}


# ════════════════════════════════════════════════════════════
#  SQL — únicos pontos do arquivo que tocam o nome de coluna real
#  de loteria_resultados. Ajuste SÓ aqui se o schema real for diferente.
# ════════════════════════════════════════════════════════════

_SQL_SELECT_POR_CONCURSO = """
    SELECT * FROM loteria_resultados WHERE jogo = %s AND concurso = %s
"""

_SQL_SELECT_ULTIMO = """
    SELECT * FROM loteria_resultados WHERE jogo = %s ORDER BY concurso DESC LIMIT 1
"""

_SQL_SELECT_ANO_ATUAL = """
    SELECT *, data_sorteio AS data_apuracao FROM loteria_resultados
    WHERE jogo = %s AND EXTRACT(YEAR FROM data_sorteio) = %s
    ORDER BY concurso DESC
"""

_SQL_SELECT_SITEMAP_ANO_ATUAL = """
    SELECT jogo, concurso, data_sorteio AS data_apuracao FROM loteria_resultados
    WHERE EXTRACT(YEAR FROM data_sorteio) = %s
    ORDER BY jogo, concurso
"""

_SQL_UPSERT = """
    INSERT INTO loteria_resultados
        (jogo, concurso, data_sorteio, data_proximo_concurso,
         dezenas, acumulou, valor_premio, valor_estimado_proximo,
         dados_json)
    VALUES
        (%(jogo)s, %(concurso)s, %(data_sorteio)s, %(data_proximo_concurso)s,
         %(dezenas)s, %(acumulou)s, %(valor_premio)s, %(valor_estimado_proximo)s,
         %(dados_json)s)
    ON CONFLICT (jogo, concurso) DO UPDATE SET
        data_sorteio            = EXCLUDED.data_sorteio,
        data_proximo_concurso   = EXCLUDED.data_proximo_concurso,
        dezenas                 = EXCLUDED.dezenas,
        acumulou                = EXCLUDED.acumulou,
        valor_premio            = EXCLUDED.valor_premio,
        valor_estimado_proximo  = EXCLUDED.valor_estimado_proximo,
        dados_json              = EXCLUDED.dados_json,
        atualizado_em           = now()
"""


# ── Banco — 2ª conexão, isolada do hub ──────────────────────────

def get_db_loteria():
    if "db_loteria" not in g:
        g.db_loteria = psycopg2.connect(
            host     = os.getenv("LOTERIA_DB_HOST"),
            port     = int(os.getenv("LOTERIA_DB_PORT", 5432)),
            dbname   = os.getenv("LOTERIA_DB_NAME"),
            user     = os.getenv("LOTERIA_DB_USER"),
            password = os.getenv("LOTERIA_DB_PASS"),
            cursor_factory=psycopg2.extras.RealDictCursor
        )
    return g.db_loteria


@loteria_bp.teardown_app_request
def _close_db_loteria(exc=None):
    # teardown_app_request roda ao fim de TODA request da aplicação (não só
    # das rotas deste blueprint) — igual ao close_db(g.db) do app.py, só que
    # pra essa conexão separada. Se a request nunca abriu g.db_loteria, o
    # g.pop com default None não faz nada.
    db = g.pop("db_loteria", None)
    if db:
        db.close()


def query_loteria(sql, params=(), one=False, commit=False):
    db = get_db_loteria()
    cur = db.cursor()
    cur.execute(sql, params)
    if commit:
        db.commit()
        return cur.rowcount
    return cur.fetchone() if one else cur.fetchall()


# ── Datas: Caixa usa dd/mm/yyyy em texto ────────────────────────

def _parse_data_caixa(texto):
    if not texto:
        return None
    try:
        return datetime.strptime(texto, "%d/%m/%Y").date()
    except ValueError:
        return None


# ── Cliente da API da Caixa ──────────────────────────────────────
# Não é API oficialmente documentada como pública — por isso timeout
# curto e captura ampla de exceção, pra nunca derrubar a página por
# causa de instabilidade dela.

def _caixa_get(jogo_caixa, concurso=None):
    """GET na API da Caixa. concurso=None busca o último resultado
    disponível. Retorna (dados, erro); erro é string legível em caso
    de falha, None em caso de sucesso."""
    url = f"{CAIXA_BASE_URL}/{jogo_caixa}"
    if concurso is not None:
        url += f"/{concurso}"
    try:
        resp = requests.get(url, timeout=8, headers={"Content-Type": "application/json"})
        resp.raise_for_status()
        dados = resp.json()
    except requests.RequestException as e:
        return None, str(e)
    if not dados or dados.get("numero") is None:
        return None, "Resposta inesperada da API da Caixa"
    return dados, None


def _normalizar_para_gravar(jogo_slug, dados_caixa):
    """Converte o JSON bruto da Caixa pro formato de linha da nossa tabela
    (schema real: ver script_loteria_resultados.py — NÃO tem coluna própria
    pra local do sorteio nem pra rateio completo; tudo isso vai preservado
    dentro de dados_json, que guarda a resposta bruta inteira)."""
    dezenas_lista = dados_caixa.get("listaDezenas") or dados_caixa.get("dezenasSorteadasOrdemSorteio") or []
    rateio = dados_caixa.get("listaRateioPremio") or []
    # valor_premio = prêmio da faixa principal (1ª faixa do rateio) desse
    # concurso — é o número mais próximo de "quanto pagou o prêmio" que dá
    # pra extrair sem ambiguidade do payload da Caixa.
    valor_premio = rateio[0].get("valorPremio") if rateio else None
    return {
        "jogo": jogo_slug,
        "concurso": dados_caixa.get("numero"),
        "data_sorteio": _parse_data_caixa(dados_caixa.get("dataApuracao")),
        "data_proximo_concurso": _parse_data_caixa(dados_caixa.get("dataProximoConcurso")),
        "dezenas": ",".join(str(d) for d in dezenas_lista),
        "acumulou": bool(dados_caixa.get("acumulado")),
        "valor_premio": valor_premio,
        "valor_estimado_proximo": dados_caixa.get("valorEstimadoProximoConcurso"),
        "dados_json": json.dumps(dados_caixa, ensure_ascii=False, default=str),
    }


def _salvar_resultado(jogo_slug, dados_caixa):
    linha = _normalizar_para_gravar(jogo_slug, dados_caixa)
    if not linha["concurso"] or not linha["data_sorteio"]:
        return  # dado incompleto vindo da Caixa — não grava lixo no banco
    try:
        query_loteria(_SQL_UPSERT, linha, commit=True)
    except Exception as e:
        # Nunca deixa uma falha de escrita do cache derrubar a página —
        # o visitante já tem o resultado (veio direto da Caixa), só não
        # conseguimos guardar cópia local dessa vez. Fica logado pra dar
        # pra investigar depois.
        print(f"[loteria] Falha ao salvar cache {jogo_slug}/{linha.get('concurso')}: {e}")


# ── Cache leve em memória só pro "último resultado" ─────────────
# O resultado de um concurso ESPECÍFICO já passado nunca muda — pra esse
# caso o banco já é cache suficiente (ver rota de detalhe). Já o "último
# resultado" muda a cada sorteio, então aqui cabe um TTL curto só pra não
# martelar a API da Caixa a cada visitante — sem isso, toda entrada em
# /resultados/<jogo>/ chamaria a Caixa de novo.
_CACHE_ULTIMO = {}
_CACHE_ULTIMO_TTL = 600  # 10 minutos


def _linha_banco_para_template(linha):
    """Os templates (resultado_jogo.html, concurso_detalhe.html) esperam as
    MESMAS chaves que a API da Caixa devolve (numero, dataApuracao,
    listaDezenas, acumulado, listaRateioPremio, nomeMunicipioUFSorteio...).
    Quando o resultado vem do banco (fallback ou concurso já sorteado), a
    fonte mais completa e fiel é dados_json (resposta bruta salva
    inteira) — as colunas soltas (concurso, data_sorteio etc.) existem só
    pra permitir filtro/ordenação em SQL, não pra remontar a página."""
    bruto_txt = linha.get("dados_json")
    if bruto_txt:
        try:
            bruto = json.loads(bruto_txt)
            if isinstance(bruto, dict):
                return bruto
        except (ValueError, TypeError):
            pass
    # dados_json vazio/corrompido (linha antiga, por exemplo): monta o
    # mínimo a partir das colunas soltas, o suficiente pro template não quebrar
    return {
        "numero": linha.get("concurso"),
        "dataApuracao": linha["data_sorteio"].strftime("%d/%m/%Y") if linha.get("data_sorteio") else None,
        "dezenasSorteadasOrdemSorteio": (linha.get("dezenas") or "").split(",") if linha.get("dezenas") else [],
        "acumulado": bool(linha.get("acumulou")),
        "valorEstimadoProximoConcurso": linha.get("valor_estimado_proximo"),
        "dataProximoConcurso": linha["data_proximo_concurso"].strftime("%d/%m/%Y") if linha.get("data_proximo_concurso") else None,
    }


def _resultado_mais_recente(jogo_slug, jogo_caixa):
    """Sempre tenta refletir o sorteio mais recente. Fluxo:
    1) cache em memória (10min) evita chamada repetida à Caixa
    2) chama a Caixa (fonte da verdade pro "mais recente")
    3) se a Caixa falhar, cai pro que já está salvo no banco (pode
       estar desatualizado, mas é melhor que quebrar a página)
    """
    agora = time.time()
    item = _CACHE_ULTIMO.get(jogo_slug)
    if item and (agora - item[0]) < _CACHE_ULTIMO_TTL:
        return item[1], None

    dados, erro = _caixa_get(jogo_caixa)
    if dados:
        _salvar_resultado(jogo_slug, dados)
        _CACHE_ULTIMO[jogo_slug] = (agora, dados)
        return dados, None

    print(f"[loteria] Caixa indisponível pra {jogo_caixa} ({jogo_slug}): {erro}")

    # Caixa indisponível: cai pro cache local (loteria_resultados)
    linha_banco = query_loteria(_SQL_SELECT_ULTIMO, (jogo_slug,), one=True)
    if linha_banco:
        return _linha_banco_para_template(dict(linha_banco)), None
    return None, erro


def _resultado_por_concurso(jogo_slug, jogo_caixa, concurso):
    """Concurso já sorteado não muda mais — sempre confere o banco
    (cache permanente) antes de chamar a Caixa."""
    linha_banco = query_loteria(_SQL_SELECT_POR_CONCURSO, (jogo_slug, concurso), one=True)
    if linha_banco:
        return _linha_banco_para_template(dict(linha_banco)), None

    dados, erro = _caixa_get(jogo_caixa, concurso)
    if erro or not dados:
        return None, erro or "Concurso não encontrado"
    _salvar_resultado(jogo_slug, dados)
    return dados, None


# ── Backfill limitado ao ano atual ───────────────────────────────
# Intencional: nada de histórico completo desde o início do jogo — só o
# ano corrente. Roda em background (nunca bloqueia uma request), 1x por
# jogo por processo, andando pra trás a partir do concurso mais recente
# até encontrar uma data de apuração de ano anterior ao atual.

_BACKFILL_FEITO = set()
_backfill_lock = threading.Lock()


def _backfill_ano_atual(jogo_slug, jogo_caixa):
    ano_atual = date.today().year

    dados_ultimo, erro = _caixa_get(jogo_caixa)
    if erro or not dados_ultimo:
        return
    _salvar_resultado(jogo_slug, dados_ultimo)

    concurso = dados_ultimo.get("numero")
    if not concurso:
        return

    concurso -= 1
    while concurso > 0:
        # já no banco? não chama a Caixa de novo pra esse concurso.
        existente = query_loteria(_SQL_SELECT_POR_CONCURSO, (jogo_slug, concurso), one=True)
        if existente:
            if existente["data_apuracao"] and existente["data_apuracao"].year < ano_atual:
                break
            concurso -= 1
            continue

        dados, erro = _caixa_get(jogo_caixa, concurso)
        if erro or not dados:
            break  # instabilidade da Caixa: a próxima chamada tenta de novo depois
        data_apuracao = _parse_data_caixa(dados.get("dataApuracao"))
        if data_apuracao and data_apuracao.year < ano_atual:
            break
        _salvar_resultado(jogo_slug, dados)
        concurso -= 1


def _disparar_backfill_em_background(jogo_slug, jogo_caixa):
    with _backfill_lock:
        if jogo_slug in _BACKFILL_FEITO:
            return
        _BACKFILL_FEITO.add(jogo_slug)

    def _rodar():
        from app import app  # import tardio: precisa de app_context pra usar g/query_loteria
        with app.app_context():
            try:
                _backfill_ano_atual(jogo_slug, jogo_caixa)
            except Exception:
                pass  # próxima subida do processo tenta de novo

    threading.Thread(target=_rodar, daemon=True, name=f"backfill-loteria-{jogo_slug}").start()


# ════════════════════════════════════════════════════════════
#  /resultados/<jogo>/ — explicação do jogo + resultado mais recente
# ════════════════════════════════════════════════════════════

@loteria_bp.route("/resultados/<jogo>/")
def resultado_jogo(jogo):
    from app import get_hub_by_host  # import tardio: mesmo padrão do cinema_bp
    hub = get_hub_by_host()
    if not hub:
        return "Hub não encontrado", 404

    info = JOGOS.get(jogo)
    if not info:
        return "Jogo não encontrado", 404

    dados, erro = _resultado_mais_recente(jogo, info["caixa"])
    if not dados:
        print(f"[loteria] /resultados/{jogo}/ sem dados (Caixa e banco falharam): {erro}")
        return render_template("loteria/erro.html", hub=hub, jogo=info,
                                mensagem="Não foi possível carregar o resultado agora. Tenta de novo em instantes."), 502

    _disparar_backfill_em_background(jogo, info["caixa"])

    ano_atual = date.today().year
    historico_ano = query_loteria(_SQL_SELECT_ANO_ATUAL, (jogo, ano_atual))

    return render_template(
        "loteria/resultado_jogo.html",
        hub=hub, jogo_slug=jogo, jogo=info,
        resultado=dados, ano_atual=ano_atual,
        historico_ano=historico_ano, jogos_menu=JOGOS,
    )


# ════════════════════════════════════════════════════════════
#  /resultados/<jogo>/<concurso>/ — detalhe de um concurso
# ════════════════════════════════════════════════════════════

@loteria_bp.route("/resultados/<jogo>/<int:concurso>/")
def resultado_concurso(jogo, concurso):
    from app import get_hub_by_host
    hub = get_hub_by_host()
    if not hub:
        return "Hub não encontrado", 404

    info = JOGOS.get(jogo)
    if not info:
        return "Jogo não encontrado", 404

    dados, erro = _resultado_por_concurso(jogo, info["caixa"], concurso)
    if not dados:
        print(f"[loteria] /resultados/{jogo}/{concurso}/ sem dados: {erro}")
        return render_template("loteria/erro.html", hub=hub, jogo=info,
                                mensagem="Concurso não encontrado."), 404

    return render_template(
        "loteria/concurso_detalhe.html",
        hub=hub, jogo_slug=jogo, jogo=info,
        resultado=dados, concurso=concurso,
    )


# ════════════════════════════════════════════════════════════
#  /sitemap-resultados.xml — só concursos do ano atual (backfill)
# ════════════════════════════════════════════════════════════

# ════════════════════════════════════════════════════════════
#  ⚠️ ROTA TEMPORÁRIA DE DIAGNÓSTICO — REMOVER DEPOIS DE USAR
#  Não exige nada (sem senha/login) e mostra erro técnico cru,
#  então não deve ficar em produção depois que o problema for achado.
#  Testa, isoladamente: (1) se dá pra alcançar a API da Caixa,
#  (2) se a conexão com o banco 'metro' (LOTERIA_DB_*) funciona.
# ════════════════════════════════════════════════════════════

@loteria_bp.route("/resultados/_debug/")
def _debug_diagnostico():
    import traceback
    resultado = {}

    # 1) Testa a API da Caixa
    try:
        r = requests.get(f"{CAIXA_BASE_URL}/megasena", timeout=8,
                          headers={"Content-Type": "application/json"})
        resultado["caixa_status_code"] = r.status_code
        resultado["caixa_preview"] = r.text[:300]
    except Exception as e:
        resultado["caixa_excecao"] = f"{type(e).__name__}: {e}"
        resultado["caixa_traceback"] = traceback.format_exc()

    # 2) Testa a conexão com o banco 'metro' (LOTERIA_DB_*)
    resultado["env_vars_presentes"] = {
        "LOTERIA_DB_HOST": bool(os.getenv("LOTERIA_DB_HOST")),
        "LOTERIA_DB_PORT": bool(os.getenv("LOTERIA_DB_PORT")),
        "LOTERIA_DB_NAME": bool(os.getenv("LOTERIA_DB_NAME")),
        "LOTERIA_DB_USER": bool(os.getenv("LOTERIA_DB_USER")),
        "LOTERIA_DB_PASS": bool(os.getenv("LOTERIA_DB_PASS")),
    }
    try:
        linha = query_loteria("SELECT COUNT(*) as total FROM loteria_resultados", one=True)
        resultado["banco_ok"] = True
        resultado["banco_total_linhas"] = linha["total"] if linha else None
    except Exception as e:
        resultado["banco_ok"] = False
        resultado["banco_excecao"] = f"{type(e).__name__}: {e}"
        resultado["banco_traceback"] = traceback.format_exc()

    return jsonify(resultado)


@loteria_bp.route("/sitemap-resultados.xml")
def sitemap_resultados():
    from app import get_hub_by_host
    hub = get_hub_by_host()
    if not hub:
        return "Hub não encontrado", 404

    base_url = f"https://{request.host}"
    ano_atual = date.today().year
    linhas_banco = query_loteria(_SQL_SELECT_SITEMAP_ANO_ATUAL, (ano_atual,))

    linhas = ['<?xml version="1.0" encoding="UTF-8"?>']
    linhas.append('<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">')
    for jogo_slug in JOGOS:
        linhas.append("  <url>")
        linhas.append(f"    <loc>{base_url}/resultados/{jogo_slug}/</loc>")
        linhas.append("    <changefreq>daily</changefreq>")
        linhas.append("    <priority>0.8</priority>")
        linhas.append("  </url>")
    for row in linhas_banco:
        data_str = row["data_apuracao"].isoformat() if row["data_apuracao"] else datetime.now(timezone.utc).strftime("%Y-%m-%d")
        linhas.append("  <url>")
        linhas.append(f"    <loc>{base_url}/resultados/{row['jogo']}/{row['concurso']}/</loc>")
        linhas.append(f"    <lastmod>{data_str}</lastmod>")
        linhas.append("    <changefreq>monthly</changefreq>")
        linhas.append("    <priority>0.5</priority>")
        linhas.append("  </url>")
    linhas.append("</urlset>")

    return Response("\n".join(linhas), mimetype="application/xml")
