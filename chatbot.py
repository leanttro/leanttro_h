# ════════════════════════════════════════════════════════════
#  chatbot.py — Blueprint "Assistente de Chat"
#  Responde perguntas sobre FILMES (via TMDB, reaproveitando as
#  funções que já existem no cinema.py) e sobre CINEMAS cadastrados
#  no hub atual (via hub_negocios, reaproveitando query()/get_hub_by_host
#  do app.py). Não guarda nenhum dado novo — só CONSULTA o que já existe
#  nos dois lugares.
#
#  Modelo de IA: Groq (endpoint compatível com a API da OpenAI), usando
#  tool-calling — o modelo decide sozinho quando precisa buscar filme ou
#  cinema, a gente executa a função Python de verdade e devolve o
#  resultado pra ele formular a resposta final. Isso evita alucinação:
#  o modelo nunca inventa endereço/telefone/nome de filme, só reformula
#  em português o que veio do banco/TMDB.
#
#  Registro (no fim do app.py, igual o cinema_bp):
#
#      from chatbot import chatbot_bp
#      app.register_blueprint(chatbot_bp)
#
#  Variáveis de ambiente necessárias:
#      GROQ_API_KEY  — sua chave da Groq
#      GROQ_MODEL    — opcional, default abaixo (precisa suportar tool use)
# ════════════════════════════════════════════════════════════

from flask import Blueprint, request, jsonify
import os
import json
import requests

from cinema import (
    _tmdb_get,
    _enriquecer_filme,
    _tem_streaming_flatrate_br,
    _params_em_cartaz,
    _sem_streaming,
)

chatbot_bp = Blueprint("chatbot", __name__)

GROQ_API_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODEL = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")

# Teto de idas-e-vindas modelo -> tool -> modelo numa mesma pergunta.
# Evita loop infinito se o modelo insistir em chamar ferramenta pra sempre.
_MAX_RODADAS_TOOLS = 4

# Teto de mensagens de histórico aceitas do cliente — o navegador manda o
# histórico inteiro a cada request (chat é stateless no servidor), então
# isso é só uma trava de segurança pra não deixar o payload crescer sem fim.
_MAX_HISTORICO = 12

_SYSTEM_PROMPT = """Você é o assistente virtual do site "{hub_nome}".
Você ajuda visitantes com DUAS coisas, e nada além disso:
1) Informação sobre filmes (sinopse, se está em cartaz no cinema ou em qual streaming).
2) Informação sobre os cinemas cadastrados no site (nome, endereço, telefone, WhatsApp).

Regras importantes:
- SEMPRE use as ferramentas disponíveis pra buscar dado real antes de responder
  sobre um filme ou cinema específico. NUNCA invente título, sinopse, endereço,
  telefone ou WhatsApp — se a ferramenta não retornar nada, diga que não encontrou.
- Se a pergunta não tiver nada a ver com filme ou cinema, explique educadamente
  que você só ajuda com isso.
- Respostas curtas, diretas, em português do Brasil, tom simpático e informal.
- Se o visitante perguntar por cinemas mas não disser cidade/bairro, pergunte
  antes de buscar (senão a lista fica genérica demais)."""

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "buscar_filmes",
            "description": (
                "Busca filmes pelo título ou tema no catálogo geral (TMDB). "
                "Devolve título, ano, sinopse curta e se o filme está disponível "
                "em streaming por assinatura no Brasil agora."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "termo": {
                        "type": "string",
                        "description": "título ou tema do filme buscado",
                    }
                },
                "required": ["termo"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "filmes_em_cartaz",
            "description": (
                "Lista os filmes em cartaz agora nos cinemas do Brasil "
                "(lançamentos dos últimos ~45 dias), do mais popular pro menos popular."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "buscar_cinemas",
            "description": (
                "Busca cinemas cadastrados neste site: nome, endereço, telefone e "
                "WhatsApp. Pode filtrar por cidade e/ou bairro; se nenhum for "
                "passado, traz os cinemas cadastrados no geral (até 12)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "cidade": {"type": "string", "description": "cidade a filtrar (opcional)"},
                    "bairro": {"type": "string", "description": "bairro a filtrar (opcional)"},
                },
            },
        },
    },
]


def _tool_buscar_filmes(args):
    termo = (args.get("termo") or "").strip()
    if not termo:
        return {"filmes": []}

    dados, erro = _tmdb_get("/search/movie", {
        "query": termo, "region": "BR", "include_adult": "false",
    })
    if erro or not dados:
        return {"filmes": [], "erro": erro}

    filmes = []
    for f in dados.get("results", [])[:6]:
        _enriquecer_filme(f)
        filmes.append({
            "titulo": f.get("title"),
            "ano": (f.get("release_date") or "")[:4],
            "sinopse": (f.get("overview") or "")[:300],
            "em_streaming_no_brasil": _tem_streaming_flatrate_br(f["id"]),
        })
    return {"filmes": filmes}


def _tool_filmes_em_cartaz(args):
    dados, erro = _tmdb_get("/discover/movie", _params_em_cartaz(1))
    if erro or not dados:
        return {"filmes": [], "erro": erro}

    filmes = _sem_streaming([_enriquecer_filme(f) for f in dados.get("results", [])])[:10]
    return {"filmes": [
        {"titulo": f.get("title"), "ano": (f.get("release_date") or "")[:4]}
        for f in filmes
    ]}


def _tool_buscar_cinemas(args, hub_id):
    from app import query  # import tardio: evita ciclo de import com app.py

    sql = """
        SELECT n.nome, n.endereco, n.bairro, n.cidade, n.telefone, n.whatsapp, n.site_url
        FROM hub_negocios n
        JOIN hub_negocio_hubs nh ON nh.negocio_id = n.id
        WHERE nh.hub_id = %s AND n.ativo = true
    """
    params = [hub_id]

    if args.get("cidade"):
        sql += " AND LOWER(n.cidade) = LOWER(%s)"
        params.append(args["cidade"])
    if args.get("bairro"):
        sql += " AND LOWER(n.bairro) = LOWER(%s)"
        params.append(args["bairro"])

    sql += " ORDER BY n.nome LIMIT 12"
    cinemas = query(sql, tuple(params))
    return {"cinemas": [dict(c) for c in cinemas]}


def _executar_tool(nome, args, hub_id):
    if nome == "buscar_filmes":
        return _tool_buscar_filmes(args)
    if nome == "filmes_em_cartaz":
        return _tool_filmes_em_cartaz(args)
    if nome == "buscar_cinemas":
        return _tool_buscar_cinemas(args, hub_id)
    return {"erro": f"ferramenta desconhecida: {nome}"}


@chatbot_bp.route("/api/chatbot", methods=["POST"])
def chatbot():
    from app import get_hub_by_host  # import tardio: evita ciclo de import com app.py

    hub = get_hub_by_host()
    if not hub:
        return jsonify(erro="Hub não encontrado"), 404

    api_key = os.getenv("GROQ_API_KEY")
    if not api_key:
        return jsonify(erro="GROQ_API_KEY não configurada"), 500

    body = request.get_json(silent=True) or {}
    mensagem = (body.get("mensagem") or "").strip()[:500]
    historico = (body.get("historico") or [])[-_MAX_HISTORICO:]

    if not mensagem:
        return jsonify(erro="Mensagem vazia"), 400

    # Sanitiza o histórico vindo do cliente: só aceita os campos esperados,
    # pra não deixar o front injetar role="system"/"tool" ou outra chave.
    historico_limpo = [
        {"role": m.get("role"), "content": m.get("content")}
        for m in historico
        if m.get("role") in ("user", "assistant") and isinstance(m.get("content"), str)
    ]

    system = _SYSTEM_PROMPT.format(hub_nome=hub.get("nome") or "Cinema Perto de Mim")
    mensagens = [{"role": "system", "content": system}] + historico_limpo + [
        {"role": "user", "content": mensagem}
    ]

    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

    for _ in range(_MAX_RODADAS_TOOLS):
        payload = {
            "model": GROQ_MODEL,
            "messages": mensagens,
            "tools": TOOLS,
            "tool_choice": "auto",
            "temperature": 0.4,
        }
        try:
            resp = requests.post(GROQ_API_URL, headers=headers, json=payload, timeout=20)
            resp.raise_for_status()
            dados = resp.json()
        except requests.RequestException as e:
            return jsonify(erro=f"Falha ao falar com a IA: {e}"), 502

        escolha = dados["choices"][0]["message"]
        mensagens.append(escolha)

        tool_calls = escolha.get("tool_calls")
        if not tool_calls:
            return jsonify(resposta=escolha.get("content") or "")

        for tc in tool_calls:
            nome_fn = tc["function"]["name"]
            try:
                args = json.loads(tc["function"].get("arguments") or "{}")
            except (TypeError, ValueError):
                args = {}

            resultado = _executar_tool(nome_fn, args, hub["id"])
            mensagens.append({
                "role": "tool",
                "tool_call_id": tc["id"],
                "content": json.dumps(resultado, ensure_ascii=False),
            })

    return jsonify(resposta="Desculpa, não consegui montar uma resposta agora. Tenta reformular a pergunta.")
