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

# Espelho de terceiros usado como FALLBACK quando a Caixa não responde
# direto (ver _buscar_do_espelho mais abaixo) — API pública bem estabelecida,
# usada por dezenas de projetos open source (loteriascaixa-api).
_ESPELHO_BASE_URL = "https://loteriascaixa-api.herokuapp.com/api"

# ── Jogos suportados ────────────────────────────────────────────
# chave = slug usado nas nossas URLs (/resultados/<jogo>/)
# "caixa"  = nome do jogo na URL da API da Caixa (sem hífen)
# "nome"   = nome de exibição
# "dias"   = texto informativo dos dias de sorteio (não crítico,
#            é só copy de página — a Caixa pode alterar a agenda)
JOGOS = {
    # ⚠️ Dias atualizados conforme mudança oficial da Caixa em 19/07/2026:
    # os sorteios que caíam aos sábados passaram pra domingo (11h). Se a
    # Caixa mudar de novo, é só ajustar o texto "dias" aqui — não afeta
    # nenhuma lógica, é só copy exibido na página.
    "mega-sena": {"caixa": "megasena",  "nome": "Mega-Sena",  "dias": "terças, quintas e domingos"},
    "quina":     {"caixa": "quina",     "nome": "Quina",      "dias": "diariamente, exceto sábados"},
    "lotofacil": {"caixa": "lotofacil", "nome": "Lotofácil",  "dias": "diariamente, exceto sábados"},
    "lotomania": {"caixa": "lotomania", "nome": "Lotomania",  "dias": "segundas, quartas e sextas-feiras"},
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
    WHERE
        -- nunca deixa um write AUTOMÁTICO (Caixa/espelho/backfill) sobrescrever
        -- uma linha marcada como preenchida manualmente pelo admin (ver seção
        -- ADMIN mais abaixo). Só um novo save manual (que também vem com
        -- _manual=true) pode sobrescrever um manual já existente.
        COALESCE(NULLIF(loteria_resultados.dados_json, '')::jsonb ->> '_manual', 'false') <> 'true'
        OR COALESCE(NULLIF(EXCLUDED.dados_json, '')::jsonb ->> '_manual', 'false') = 'true'
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

# ── Cliente da API da Caixa ──────────────────────────────────────
# Não é API oficialmente documentada como pública — por isso timeout
# curto e captura ampla de exceção, pra nunca derrubar a página por
# causa de instabilidade dela.
#
# DESCOBERTA (26/07/2026): a Caixa bloqueia (403) requisições vindas de
# IP fora do Brasil — não é sobre header/User-Agent (confirmado: persiste
# mesmo simulando navegador). Servidores de hospedagem fora do Brasil
# (ex.: Railway, que roda nos EUA) sempre vão tomar 403 daqui. Por isso
# existe o fallback pro espelho logo abaixo — ele é quem efetivamente
# fala com a Caixa a partir de infraestrutura própria, e nós só
# consumimos o resultado já processado.

def _buscar_da_caixa(jogo_caixa, concurso=None):
    """Tentativa DIRETA na API oficial da Caixa. Só funciona se o
    servidor estiver com IP do Brasil — ver nota acima. Mantida como
    1ª tentativa de propósito: se um dia a hospedagem mudar pra uma
    com IP brasileiro, volta a funcionar sozinha, sem precisar mexer
    em mais nada."""
    url = f"{CAIXA_BASE_URL}/{jogo_caixa}"
    if concurso is not None:
        url += f"/{concurso}"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "pt-BR,pt;q=0.9",
        "Referer": "https://loterias.caixa.gov.br/",
        "Origin": "https://loterias.caixa.gov.br",
    }
    try:
        resp = requests.get(url, timeout=8, headers=headers)
        resp.raise_for_status()
        dados = resp.json()
    except requests.RequestException as e:
        return None, str(e)
    if not dados or dados.get("numero") is None:
        return None, "Resposta inesperada da API da Caixa"
    return dados, None


def _adaptar_espelho_para_formato_caixa(d):
    """O espelho (loteriascaixa-api) devolve um JSON com nomes de campo
    DIFERENTES da Caixa (concurso/data/dezenas/premiacoes/acumulou, em vez
    de numero/dataApuracao/listaDezenas/listaRateioPremio/acumulado).
    Aqui a gente converte pro formato da Caixa — assim todo o resto do
    arquivo (templates, gravação no banco) nem percebe de onde veio o
    dado, sempre trabalha com as mesmas chaves."""
    premiacoes = d.get("premiacoes") or []
    return {
        "numero": d.get("concurso"),
        "dataApuracao": d.get("data"),
        "dataProximoConcurso": d.get("dataProximoConcurso"),
        "listaDezenas": d.get("dezenas"),
        "dezenasSorteadasOrdemSorteio": d.get("dezenasOrdemSorteio") or d.get("dezenas"),
        "acumulado": bool(d.get("acumulou")),
        "valorArrecadado": d.get("valorArrecadado"),
        "valorEstimadoProximoConcurso": d.get("valorEstimadoProximoConcurso"),
        "valorAcumuladoProximoConcurso": d.get("valorAcumuladoProximoConcurso"),
        "nomeMunicipioUFSorteio": d.get("local"),
        "listaRateioPremio": [
            {
                "descricaoFaixa": p.get("descricao"),
                "faixa": p.get("faixa"),
                "numeroDeGanhadores": p.get("ganhadores"),
                "valorPremio": p.get("valorPremio"),
            }
            for p in premiacoes
        ],
    }


def _buscar_do_espelho(jogo_caixa, concurso=None):
    """Fallback quando a Caixa direta falha (403 por IP fora do Brasil,
    instabilidade, etc.). Usa um espelho de terceiros bem estabelecido
    (loteriascaixa-api, usado por dezenas de projetos open source),
    que já resolve esse mesmo bloqueio geográfico do lado dele."""
    url = f"{_ESPELHO_BASE_URL}/{jogo_caixa}/" + (str(concurso) if concurso is not None else "latest")
    try:
        resp = requests.get(url, timeout=8)
        resp.raise_for_status()
        bruto = resp.json()
    except requests.RequestException as e:
        return None, str(e)
    if not bruto or bruto.get("concurso") is None:
        return None, "Resposta inesperada do espelho"
    return _adaptar_espelho_para_formato_caixa(bruto), None


def _caixa_get(jogo_caixa, concurso=None):
    """Ponto único chamado pelo resto do arquivo. Tenta a Caixa direto
    primeiro; se falhar (por qualquer motivo), cai pro espelho antes de
    desistir. Retorna (dados, erro) sempre no formato da Caixa."""
    dados, erro_caixa = _buscar_da_caixa(jogo_caixa, concurso)
    if dados:
        return dados, None

    print(f"[loteria] Caixa direta falhou pra {jogo_caixa} ({erro_caixa}); tentando espelho...")
    dados, erro_espelho = _buscar_do_espelho(jogo_caixa, concurso)
    if dados:
        return dados, None

    return None, f"Caixa: {erro_caixa} | Espelho: {erro_espelho}"


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
    # mínimo a partir das colunas soltas. IMPORTANTE: precisa incluir TODAS
    # as chaves que a Caixa devolveria (mesmo que vazias/None), porque os
    # templates fazem "{% for x in resultado.listaRateioPremio %}" etc. —
    # se a chave não existisse, o Jinja tenta iterar um Undefined e
    # quebra com 500 (foi exatamente isso que aconteceu com concursos
    # antigos sem dados_json, ex.: mega-sena/3029).
    dezenas_lista = (linha.get("dezenas") or "").split(",") if linha.get("dezenas") else []
    return {
        "numero": linha.get("concurso"),
        "dataApuracao": linha["data_sorteio"].strftime("%d/%m/%Y") if linha.get("data_sorteio") else None,
        "dataProximoConcurso": linha["data_proximo_concurso"].strftime("%d/%m/%Y") if linha.get("data_proximo_concurso") else None,
        "listaDezenas": dezenas_lista,
        "dezenasSorteadasOrdemSorteio": dezenas_lista,
        "acumulado": bool(linha.get("acumulou")),
        "valorArrecadado": None,
        "valorEstimadoProximoConcurso": linha.get("valor_estimado_proximo"),
        "valorAcumuladoProximoConcurso": None,
        "nomeMunicipioUFSorteio": None,
        "listaRateioPremio": [],
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

    # Vê se existe um preenchimento MANUAL salvo pro último concurso do banco
    # (ver seção ADMIN mais abaixo). Se existir, ele só perde pra API quando a
    # API devolver um concurso MAIS NOVO que o manual — assim o admin não
    # precisa fazer nada quando a Caixa/espelho finalmente atualizar.
    linha_banco = query_loteria(_SQL_SELECT_ULTIMO, (jogo_slug,), one=True)
    override_manual = None
    if linha_banco:
        bruto_banco = _linha_banco_para_template(dict(linha_banco))
        if bruto_banco.get("_manual"):
            override_manual = bruto_banco

    dados, erro = _caixa_get(jogo_caixa)
    if dados:
        concurso_api = dados.get("numero") or 0
        concurso_manual = (override_manual or {}).get("numero") or 0
        if override_manual and concurso_manual >= concurso_api:
            _CACHE_ULTIMO[jogo_slug] = (agora, override_manual)
            return override_manual, None
        _salvar_resultado(jogo_slug, dados)
        _CACHE_ULTIMO[jogo_slug] = (agora, dados)
        return dados, None

    print(f"[loteria] Caixa indisponível pra {jogo_caixa} ({jogo_slug}): {erro}")

    # Caixa indisponível: cai pro cache local (loteria_resultados) — que já
    # é o override manual, se existir, ou o último salvo automaticamente.
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


# ════════════════════════════════════════════════════════════
#  ADMIN — preenchimento manual de resultado
#
#  Pra que serve: quando um sorteio acabou de sair e a Caixa/espelho ainda
#  não atualizou (comum — vimos isso acontecer com o concurso de hoje),
#  você pode preencher aqui na mão. Enquanto o concurso que você preencheu
#  for igual ou mais novo que o que a API devolve, o site usa o SEU dado.
#  Assim que a API alcançar (ou passar) esse concurso, ela volta a
#  assumir sozinha — não precisa fazer nada.
#
#  Autenticação: HTTP Basic Auth simples (usuário: qualquer coisa, senha:
#  variável de ambiente LOTERIA_ADMIN_SENHA). Se essa variável não estiver
#  configurada, o painel fica bloqueado pra todo mundo (fail-closed).
#
#  Variável de ambiente necessária: LOTERIA_ADMIN_SENHA
# ════════════════════════════════════════════════════════════

from flask import render_template_string
import functools

_SQL_SELECT_MANUAIS = """
    SELECT jogo, concurso, data_sorteio, dados_json
    FROM loteria_resultados
    WHERE COALESCE(NULLIF(dados_json, '')::jsonb ->> '_manual', 'false') = 'true'
    ORDER BY jogo, concurso DESC
"""

_SQL_ATUALIZAR_DADOS_JSON = """
    UPDATE loteria_resultados SET dados_json = %(dados_json)s, atualizado_em = now()
    WHERE jogo = %(jogo)s AND concurso = %(concurso)s
"""


def _checar_senha_admin():
    senha_certa = os.getenv("LOTERIA_ADMIN_SENHA")
    if not senha_certa:
        return False  # sem senha configurada = painel desativado (fail-closed)
    auth = request.authorization
    return bool(auth) and auth.password == senha_certa


def _exigir_admin(view):
    @functools.wraps(view)
    def wrapper(*args, **kwargs):
        if not _checar_senha_admin():
            return Response(
                "Autenticação necessária.", 401,
                {"WWW-Authenticate": 'Basic realm="Admin Loteria"'},
            )
        return view(*args, **kwargs)
    return wrapper


def _parse_data_html(valor):
    """Input <input type=date> vem como yyyy-mm-dd; converte pro formato
    dd/mm/yyyy que o resto do arquivo usa (mesmo formato que a Caixa manda)."""
    if not valor:
        return None
    try:
        return datetime.strptime(valor, "%Y-%m-%d").strftime("%d/%m/%Y")
    except ValueError:
        return None


def _parse_premiacoes_texto(texto):
    """Cada linha: 'descrição | ganhadores | valor'. Linhas em branco ou mal
    formatadas são ignoradas (não derruba o salvamento por causa de 1 linha
    digitada errado)."""
    premiacoes = []
    for i, linha in enumerate((texto or "").splitlines(), start=1):
        linha = linha.strip()
        if not linha:
            continue
        partes = [p.strip() for p in linha.split("|")]
        if len(partes) != 3:
            continue
        descricao, ganhadores_txt, valor_txt = partes
        try:
            ganhadores = int(ganhadores_txt.replace(".", "").replace(",", ""))
        except ValueError:
            ganhadores = 0
        try:
            valor = float(valor_txt.replace(".", "").replace(",", "."))
        except ValueError:
            valor = 0.0
        premiacoes.append({
            "descricaoFaixa": descricao,
            "faixa": i,
            "numeroDeGanhadores": ganhadores,
            "valorPremio": valor,
        })
    return premiacoes


_ADMIN_HTML = """
<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="UTF-8">
<title>Admin — Lotérica Perto de Mim</title>
<meta name="robots" content="noindex, nofollow">
<style>
  body { font-family: system-ui, sans-serif; background: #def8c3; color: #0a2e0a; padding: 24px; max-width: 720px; margin: 0 auto; }
  h1 { color: #035105; }
  .card { background: #fff; border-radius: 12px; padding: 20px; margin-bottom: 20px; box-shadow: 0 2px 6px rgba(0,0,0,.08); }
  label { display: block; margin-top: 12px; font-weight: 600; font-size: 14px; }
  input, select, textarea { width: 100%; padding: 8px; margin-top: 4px; border: 1px solid #ccc; border-radius: 6px; font-size: 14px; box-sizing: border-box; }
  textarea { font-family: monospace; height: 80px; }
  button { margin-top: 16px; background: #035105; color: #fff; border: none; padding: 10px 20px; border-radius: 8px; font-size: 15px; cursor: pointer; }
  button:hover { background: #024004; }
  .checkbox-row { display: flex; align-items: center; gap: 8px; margin-top: 12px; }
  .checkbox-row input { width: auto; }
  table { width: 100%; border-collapse: collapse; margin-top: 10px; }
  th, td { text-align: left; padding: 6px 8px; border-bottom: 1px solid #eee; font-size: 14px; }
  .btn-remover { background: #a11; padding: 4px 10px; font-size: 12px; }
  .msg { background: #d4f7d4; border: 1px solid #4a4; padding: 10px; border-radius: 8px; margin-bottom: 16px; }
  .aviso { background: #fff3cd; border: 1px solid #cc9; padding: 10px; border-radius: 8px; margin-bottom: 16px; font-size: 14px; }
</style>
</head>
<body>
  <h1>Admin — Resultados manuais</h1>
  <p class="aviso">Preenchimentos aqui têm prioridade sobre a API enquanto o concurso digitado for igual ou mais novo que o que ela devolve. Quando a API alcançar, ela assume sozinha.</p>

  {% if mensagem %}<div class="msg">{{ mensagem }}</div>{% endif %}

  <div class="card">
    <form method="post" action="/resultados/admin/salvar">
      <label>Jogo</label>
      <select name="jogo" required>
        {% for slug, info in jogos.items() %}
        <option value="{{ slug }}">{{ info.nome }}</option>
        {% endfor %}
      </select>

      <label>Concurso</label>
      <input type="number" name="concurso" required min="1">

      <label>Data do sorteio</label>
      <input type="date" name="data_sorteio" required>

      <label>Dezenas sorteadas (separadas por vírgula, ex: 05,07,17,51,56,59)</label>
      <input type="text" name="dezenas" required placeholder="05,07,17,51,56,59">

      <div class="checkbox-row">
        <input type="checkbox" name="acumulou" id="acumulou">
        <label for="acumulou" style="margin:0">Acumulou?</label>
      </div>

      <label>Estimativa de prêmio do próximo concurso (R$, opcional)</label>
      <input type="text" name="valor_estimado_proximo" placeholder="70000000.00">

      <label>Data do próximo concurso (opcional)</label>
      <input type="date" name="data_proximo_concurso">

      <label>Local do sorteio (opcional)</label>
      <input type="text" name="local_sorteio" placeholder="ESPAÇO DA SORTE em SÃO PAULO, SP">

      <label>Premiação por faixa (opcional — uma linha por faixa: descrição | ganhadores | valor)</label>
      <textarea name="premiacoes_texto" placeholder="6 acertos | 0 | 0&#10;5 acertos | 92 | 28109.79&#10;4 acertos | 7263 | 586.92"></textarea>

      <button type="submit">Salvar resultado manual</button>
    </form>
  </div>

  <div class="card">
    <h2 style="color:#035105; margin-top:0;">Overrides manuais ativos</h2>
    {% if manuais %}
    <table>
      <tr><th>Jogo</th><th>Concurso</th><th>Data</th><th></th></tr>
      {% for m in manuais %}
      <tr>
        <td>{{ jogos.get(m.jogo, {}).get('nome', m.jogo) }}</td>
        <td>{{ m.concurso }}</td>
        <td>{{ m.data_sorteio.strftime('%d/%m/%Y') if m.data_sorteio else '—' }}</td>
        <td>
          <form method="post" action="/resultados/admin/remover" style="margin:0">
            <input type="hidden" name="jogo" value="{{ m.jogo }}">
            <input type="hidden" name="concurso" value="{{ m.concurso }}">
            <button type="submit" class="btn-remover" onclick="return confirm('Remover o preenchimento manual desse concurso? A API volta a poder atualizar ele.')">Remover override</button>
          </form>
        </td>
      </tr>
      {% endfor %}
    </table>
    {% else %}
    <p>Nenhum override manual ativo no momento.</p>
    {% endif %}
  </div>
</body>
</html>
"""


@loteria_bp.route("/resultados/admin/")
@_exigir_admin
def admin_painel():
    manuais = query_loteria(_SQL_SELECT_MANUAIS)
    mensagem = request.args.get("msg")
    return render_template_string(_ADMIN_HTML, jogos=JOGOS, manuais=manuais, mensagem=mensagem)


@loteria_bp.route("/resultados/admin/salvar", methods=["POST"])
@_exigir_admin
def admin_salvar():
    jogo_slug = request.form.get("jogo")
    info = JOGOS.get(jogo_slug)
    if not info:
        return redirect_admin("Jogo inválido.")

    try:
        concurso = int(request.form.get("concurso", ""))
    except ValueError:
        return redirect_admin("Número de concurso inválido.")

    data_sorteio = _parse_data_html(request.form.get("data_sorteio"))
    if not data_sorteio:
        return redirect_admin("Data do sorteio inválida.")

    dezenas_txt = request.form.get("dezenas", "")
    dezenas = [d.strip().zfill(2) for d in dezenas_txt.split(",") if d.strip()]
    if not dezenas:
        return redirect_admin("Informe as dezenas sorteadas.")

    valor_estimado_txt = (request.form.get("valor_estimado_proximo") or "").strip()
    try:
        valor_estimado = float(valor_estimado_txt) if valor_estimado_txt else None
    except ValueError:
        valor_estimado = None

    dados_caixa = {
        "numero": concurso,
        "dataApuracao": data_sorteio,
        "dataProximoConcurso": _parse_data_html(request.form.get("data_proximo_concurso")),
        "listaDezenas": dezenas,
        "dezenasSorteadasOrdemSorteio": dezenas,
        "acumulado": request.form.get("acumulou") == "on",
        "valorArrecadado": None,
        "valorEstimadoProximoConcurso": valor_estimado,
        "valorAcumuladoProximoConcurso": None,
        "nomeMunicipioUFSorteio": (request.form.get("local_sorteio") or "").strip() or None,
        "listaRateioPremio": _parse_premiacoes_texto(request.form.get("premiacoes_texto")),
        "_manual": True,
    }

    _salvar_resultado(jogo_slug, dados_caixa)
    # atualiza o cache em memória na hora, pra aparecer no site sem esperar
    # o TTL de 10min expirar
    _CACHE_ULTIMO[jogo_slug] = (time.time(), dados_caixa)

    return redirect_admin(f"Resultado do concurso {concurso} ({info['nome']}) salvo como manual.")


@loteria_bp.route("/resultados/admin/remover", methods=["POST"])
@_exigir_admin
def admin_remover():
    jogo_slug = request.form.get("jogo")
    try:
        concurso = int(request.form.get("concurso", ""))
    except ValueError:
        return redirect_admin("Concurso inválido.")

    linha = query_loteria(_SQL_SELECT_POR_CONCURSO, (jogo_slug, concurso), one=True)
    if not linha:
        return redirect_admin("Concurso não encontrado no banco.")

    # remove só a flag _manual, mantendo o resto do dado intacto — assim não
    # perde histórico, só libera esse concurso pra API poder atualizar de novo
    try:
        bruto = json.loads(linha["dados_json"]) if linha.get("dados_json") else {}
    except (ValueError, TypeError):
        bruto = {}
    bruto.pop("_manual", None)

    query_loteria(_SQL_ATUALIZAR_DADOS_JSON, {
        "dados_json": json.dumps(bruto, ensure_ascii=False, default=str),
        "jogo": jogo_slug,
        "concurso": concurso,
    }, commit=True)

    # também limpa o cache em memória pra não continuar servindo o manual
    _CACHE_ULTIMO.pop(jogo_slug, None)

    return redirect_admin(f"Override do concurso {concurso} ({jogo_slug}) removido.")


def redirect_admin(mensagem):
    from flask import redirect, url_for
    from urllib.parse import quote
    return redirect(f"/resultados/admin/?msg={quote(mensagem)}")
