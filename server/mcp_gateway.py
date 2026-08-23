"""MCP gateway exposing a narrow chat-oriented surface of the local-ai API.

All tools act as the single configured identity (``MCP_USER``): the gateway
authenticates inbound clients via OAuth client_credentials
(``MCP_OAUTH_CLIENT_ID``/``MCP_OAUTH_CLIENT_SECRET``) and then talks to the
main API on loopback using a self-refreshing Authentik OIDC access token
obtained via a password grant.

Intended workflow for an MCP client (e.g. Claude):
1. ``list_sessions`` to discover existing conversations (or ``create_session``)
2. optionally ``get_user_context`` / ``get_session_messages`` for background
3. ``send_chat_message`` to submit a user message — this is ASYNCHRONOUS and
   returns a task id
4. poll ``get_message_status`` until the task reaches a terminal state
5. ``get_session_messages`` again to read the assistant's reply
"""

import os, sys, json, time, threading, base64, httpx
from mcp.server.fastmcp import FastMCP, Image
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route, Mount
import uvicorn

try:
    from server.auth import identity_from_bearer, oidc_password_grant
except ImportError:
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from server.auth import identity_from_bearer, oidc_password_grant

API_BASE = os.environ.get("CHAT_API_BASE", "http://127.0.0.1:3001")
MCP_ALLOWED_USERS = [
    u.strip() for u in os.environ.get("MCP_ALLOWED_USERS", "").split(",") if u.strip()
]
MCP_USER = os.environ.get("MCP_USER", "")
MCP_USER_PASSWORD = os.environ.get("MCP_USER_PASSWORD", "")
# Paste these into Claude's "Add custom connector" -> Advanced settings.
# MCP_OAUTH_CLIENT_SECRET doubles as the bearer token: /oauth/token hands it
# back as the access_token, and the middleware below validates against it
# directly — no separate inbound-token secret to keep in sync.
MCP_OAUTH_CLIENT_ID = os.environ.get("MCP_OAUTH_CLIENT_ID", "")
MCP_OAUTH_CLIENT_SECRET = os.environ.get("MCP_OAUTH_CLIENT_SECRET", "")
TOKEN_REFRESH_MARGIN = 60

mcp = FastMCP("chat-webui-api", stateless_http=True)

class EnforcementAuthMiddleware:
    """Inbound gate: callers must present a valid Authentik access token.

    The bearer JWT is verified against Authentik's JWKS on every request —
    no shared static secret exists anymore, so tokens are revocable and
    every call carries a real identity. Optionally restricted to specific
    usernames via MCP_ALLOWED_USERS (empty = any realm user).
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            if scope.get("method") == "OPTIONS":
                await self.app(scope, receive, send)
                return

            headers = dict(scope.get("headers", []))
            auth_header = headers.get(b"authorization", b"").decode("utf-8")
            presented = auth_header[7:] if auth_header.lower().startswith("bearer ") else ""

            identity = None
            if MCP_OAUTH_CLIENT_SECRET and presented == MCP_OAUTH_CLIENT_SECRET:
                # Token issued by /oauth/token IS the client secret — a
                # client that already proved it knows the secret at the
                # token endpoint is trusted to present it again here.
                identity = {"username": MCP_USER}
            else:
                try:
                    identity = identity_from_bearer(auth_header)
                except RuntimeError:
                    # Transient JWKS failure — treat as unauthenticated rather
                    # than crashing the request handler.
                    identity = None
                if identity and MCP_ALLOWED_USERS:
                    identity = (
                        identity if identity.get("username") in MCP_ALLOWED_USERS else None
                    )
            if not identity:
                response = JSONResponse({"error": "Unauthorized"}, status_code=401)
                await response(scope, receive, send)
                return

        await self.app(scope, receive, send)


_token_lock = threading.Lock()
_token_cache = {"value": "", "exp": 0}


def _decode_exp(token):
    """Expiry timestamp of a JWT access token, or 0 if unreadable."""
    try:
        payload = token.split(".")[1]
        data = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        return int(data.get("exp", 0))
    except Exception:
        return 0


def _auth_headers():
    """Bearer headers holding a fresh Authentik access token for MCP_USER.

    Re-grants via the OIDC password grant shortly before expiry, mirroring
    the self-chat agent token lifecycle.
    """
    if not MCP_USER or not MCP_USER_PASSWORD:
        raise RuntimeError(
            "MCP_USER / MCP_USER_PASSWORD not set — cannot authenticate to the API"
        )
    global _token_cache
    with _token_lock:
        now = time.time()
        exp = _token_cache["exp"]
        fresh = _token_cache["value"] and (
            exp and now < exp - TOKEN_REFRESH_MARGIN
            or not exp and now < TOKEN_REFRESH_MARGIN
        )
        if not fresh:
            token = oidc_password_grant(MCP_USER, MCP_USER_PASSWORD)
            _token_cache = {"value": token, "exp": _decode_exp(token)}
        return {"Authorization": f"Bearer {_token_cache['value']}"}


async def _call(method: str, path: str, **kw) -> str:
    try:
        headers = _auth_headers()
    except Exception as e:
        return json.dumps({"error": f"MCP upstream auth failed: {e}"})
    async with httpx.AsyncClient() as client:
        r = await client.request(method, f"{API_BASE}{path}", headers=headers, timeout=30.0, **kw)
        if r.status_code >= 400:
            return json.dumps({"error": f"Upstream {r.status_code}", "detail": r.text})
        return r.text


async def _call_image(path: str):
    """Fetch binary image data from the API, returning (Image|None, error|None).

    Unlike _call, this handles raw bytes: the response body is wrapped in a
    FastMCP Image so clients receive a real image content block instead of
    text.
    """
    try:
        headers = _auth_headers()
    except Exception as e:
        return None, json.dumps({"error": f"MCP upstream auth failed: {e}"})
    async with httpx.AsyncClient() as client:
        r = await client.get(f"{API_BASE}{path}", headers=headers, timeout=60.0)
        if r.status_code >= 400:
            return None, json.dumps(
                {"error": f"Upstream {r.status_code}", "detail": r.text}
            )
        content_type = r.headers.get("content-type", "image/png")
        fmt = content_type.split("/")[-1].split(";")[0]
        return Image(data=r.content, format=fmt), None

# --- PROFILE ---

@mcp.tool()
async def get_user_context() -> str:
    """Read the current user's persistent profile / memory context.

    Returns a JSON object:
      {
        "context": "<free-text profile notes saved across ALL conversations>",
        "username": "<owner of this gateway identity>",
        "context_file": "<server-side path of the context file>"
      }

    The context holds durable facts about the user (preferences, personal
    details, ongoing projects) that survive across sessions. Use it to
    personalize answers BEFORE asking the user to repeat known information.
    This is memory, NOT conversation history — use get_session_messages for
    the transcript of a specific conversation.

    No parameters. Read-only; it never modifies anything.
    """
    return await _call("GET", "/api/user-context")

# --- CHAT ---

@mcp.tool()
async def list_sessions() -> str:
    """List all chat sessions of the user, most recently active first.

    Returns a JSON array (possibly empty), one entry per session:
      [
        {
          "session_id": "<uuid — pass this to other chat tools>",
          "name": "<sidebar title, e.g. 'New Chat'>",
          "created": <unix epoch seconds>,
          "updated": <unix epoch seconds>,
          ...context-token accounting fields...
        },
        ...
      ]

    Use this FIRST whenever a task references "the conversation/chat" without
    an explicit id: pick the relevant session (usually the one with the most
    recent "updated"), then call get_session_messages on its session_id.
    Sessions listed here are owned exclusively by this gateway's user.
    """
    return await _call("GET", "/api/sessions")

@mcp.tool()
async def create_session(system_prompt: str = "", system_prompts: list = None) -> str:
    """Create a brand-new empty chat session and return its id.

    Args:
      system_prompt: optional single system prompt string applied to the new
        conversation (persona/instructions). Omit for the default behavior.
      system_prompts: optional list of additional named system-prompt entries
        supported by the backend (advanced; rarely needed).

    Returns JSON: {"session_id": "<uuid>"}.

    The new session starts with no messages and the name "New Chat". To send
    the first message into it, pass this session_id to send_chat_message.
    Creating a session does NOT automatically switch any UI state — it is a
    pure data operation.
    """
    payload = {}
    if system_prompt:
        payload["system_prompt"] = system_prompt
    if system_prompts:
        payload["system_prompts"] = system_prompts
    return await _call("POST", "/api/sessions", json=payload)

@mcp.tool()
async def get_session_messages(session_id: str) -> str:
    """Read the full message transcript of one chat session.

    Args:
      session_id: uuid of the session (from list_sessions or create_session).

    Returns JSON:
      {
        "messages": [ {"role": "user"|"assistant"|..., "content": "...",
                       ...attachment/image fields when present...}, ... ],
        ...context-token accounting fields...
      }

    Messages are ordered oldest → newest, exactly as rendered in the web UI.
    Errors: HTTP-404 wrapped as {"error": "Upstream 404", ...} when the id is
    unknown or belongs to another user. Use after get_message_status reports
    "done" to read the assistant's reply (it is the last message).
    """
    return await _call("GET", f"/api/sessions/{session_id}/messages")

@mcp.tool()
async def rename_session(session_id: str, name: str) -> str:
    """Rename a chat session (the title shown in the UI sidebar).

    Args:
      session_id: uuid of an existing session.
      name: the new human-readable title (short is best).

    Returns JSON: {"status": "updated"}.

    Purely cosmetic metadata — renaming never alters the messages. Useful
    after the first exchange so the conversation can be recognized later in
    list_sessions.
    """
    return await _call("PUT", f"/api/sessions/{session_id}", json={"name": name})

@mcp.tool()
async def send_chat_message(
    session_id: str,
    message: str,
    research: bool = False,
    cpu: bool = False,
    no_tools: bool = False
) -> str:
    """Submit a user message into a chat session for ASYNC processing.

    This is the equivalent of typing into the web chat box and pressing send.
    It ENQUEUES the message and returns immediately — the model has NOT yet
    answered when this call returns.

    Args:
      session_id: target conversation (from list_sessions/create_session).
      message: the user's message text.
      research: enable the deep web-research pipeline (slower, cited reports).
      cpu: force the request onto the CPU model lane instead of GPU.
      no_tools: disable the model's built-in tool calling for this turn.

    Returns JSON: {"task_id": "<uuid>"}.

    REQUIRED follow-up: poll get_message_status(task_id) until the status is
    a terminal value ("done", "error" or "cancelled"), then fetch the reply
    with get_session_messages(session_id). Never assume the answer exists
    right after this call.
    """
    payload = {
        "session_id": session_id,
        "message": message,
        "research": research,
        "cpu": cpu,
        "no_tools": no_tools,
    }
    return await _call("POST", "/api/chat", json=payload)

@mcp.tool()
async def get_message_status(task_id: str) -> str:
    """Check the processing state of one submitted chat message.

    Args:
      task_id: the uuid returned by send_chat_message.

    Returns JSON describing the task. "status" progresses:
      "queued"    → accepted, waiting for a worker
      "working"   → the LLM is generating ("message" may hold progress text)
      "done"      → finished; the reply IS persisted in the session —
                    retrieve it with get_session_messages
      "error"     → failed; details in the payload
      "cancelled" → cancelled by the user/server
    An unknown task_id yields {"status": "unknown"}.

    Poll this roughly every 1–2 seconds; generation can take from a few
    seconds up to several minutes for research mode. This tool NEVER returns
    the reply text itself — always read the final answer via
    get_session_messages.
    """
    return await _call("GET", f"/api/status/{task_id}")

@mcp.tool()
async def get_image(image_id: str):
    """Fetch and display an image referenced in a chat conversation.

    Chat messages reference images as site-relative URLs, e.g.:
      - "/uploads/<name>.jpg"  — files uploaded by the user
      - "/output/<file>.png"   — images generated by ComfyUI in-chat

    These markers appear in message content (often as "[IMAGE: /output/...]")
    or in attachment fields of messages returned by get_session_messages.
    Such paths are NOT resolvable by the client itself — they only exist on
    this server. Pass the path/identifier to this tool to obtain the actual
    image.

    Args:
      image_id: the image reference exactly as it appears in the message,
        e.g. "/uploads/photo.jpg", "/output/art_00001_.png", or a bare id
        accepted by the backend's resolver. Do not prepend a hostname.

    Returns:
      A rendered image content block the client can display directly, so the
      conversation can "see" what was generated or uploaded. On failure
      (unknown/expired file, upstream error), returns a JSON error object —
      check for that before treating the result as an image.

    Typical use: after get_session_messages shows an assistant reply that
    generated an image, call this with the referenced path so you can view
    and describe it to the user. Images are served read-only through the
    authenticated gateway; there is deliberately no way to upload or modify
    images via MCP.
    """
    img, err = await _call_image(f"/api/image/{image_id.lstrip('/')}")
    return err if err else img


import secrets, hashlib
from starlette.responses import RedirectResponse

_auth_codes = {}
_auth_codes_lock = threading.Lock()
AUTH_CODE_TTL = 300


async def oauth_metadata(request):
    """RFC 8414 authorization-server metadata. Claude's connector discovers
    this from the base URL to find /authorize and /oauth/token."""
    base = str(request.base_url).rstrip("/")
    return JSONResponse({
        "issuer": base,
        "authorization_endpoint": f"{base}/authorize",
        "token_endpoint": f"{base}/oauth/token",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "client_credentials"],
        "code_challenge_methods_supported": ["S256", "plain"],
        "token_endpoint_auth_methods_supported": [
            "client_secret_post", "client_secret_basic", "none",
        ],
    })


async def oauth_authorize(request):
    """Authorization-code leg. Single-tenant server: reaching this endpoint
    at all already implies it's the owner (no separate login screen) — we
    just check client_id and mint a short-lived code tied to the PKCE
    challenge, then bounce back to Claude's redirect_uri."""
    p = request.query_params
    client_id = p.get("client_id", "")
    redirect_uri = p.get("redirect_uri", "")
    state = p.get("state", "")
    code_challenge = p.get("code_challenge", "")
    code_challenge_method = p.get("code_challenge_method", "S256")

    if p.get("response_type") != "code" or not redirect_uri:
        return JSONResponse({"error": "invalid_request"}, status_code=400)
    if MCP_OAUTH_CLIENT_ID and client_id != MCP_OAUTH_CLIENT_ID:
        return JSONResponse({"error": "unauthorized_client"}, status_code=401)

    code = secrets.token_urlsafe(32)
    with _auth_codes_lock:
        _auth_codes[code] = {
            "redirect_uri": redirect_uri,
            "code_challenge": code_challenge,
            "code_challenge_method": code_challenge_method,
            "exp": time.time() + AUTH_CODE_TTL,
        }
    sep = "&" if "?" in redirect_uri else "?"
    location = f"{redirect_uri}{sep}code={code}"
    if state:
        location += f"&state={state}"
    return RedirectResponse(location, status_code=302)


def _pkce_ok(verifier, challenge, method):
    if not challenge:
        return True  # PKCE not used by this client
    if method == "plain":
        return verifier == challenge
    calc = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
    return calc.rstrip(b"=").decode() == challenge


async def oauth_token(request):
    """Token endpoint: supports the authorization_code grant Claude actually
    uses (code + PKCE verifier from /authorize), and client_credentials as a
    fallback for other callers. Either way the issued access token is
    MCP_OAUTH_CLIENT_SECRET, which the middleware below validates."""
    try:
        form = await request.form()
    except Exception:
        form = {}
    grant_type = form.get("grant_type", "")
    client_id = form.get("client_id", "")
    client_secret = form.get("client_secret", "")
    if not client_id:
        auth = request.headers.get("authorization", "")
        if auth.lower().startswith("basic "):
            decoded = base64.b64decode(auth[6:]).decode("utf-8", "ignore")
            client_id, _, client_secret = decoded.partition(":")

    if MCP_OAUTH_CLIENT_ID and client_id != MCP_OAUTH_CLIENT_ID:
        return JSONResponse({"error": "invalid_client"}, status_code=401)
    # Confidential clients that pass a secret must get it right; public
    # clients (PKCE-only, no secret — Claude's default) skip this check.
    if client_secret and client_secret != MCP_OAUTH_CLIENT_SECRET:
        return JSONResponse({"error": "invalid_client"}, status_code=401)

    if grant_type == "authorization_code":
        code = form.get("code", "")
        with _auth_codes_lock:
            entry = _auth_codes.pop(code, None)
        if not entry or entry["exp"] < time.time():
            return JSONResponse({"error": "invalid_grant"}, status_code=400)
        if form.get("redirect_uri", "") != entry["redirect_uri"]:
            return JSONResponse({"error": "invalid_grant"}, status_code=400)
        if not _pkce_ok(form.get("code_verifier", ""), entry["code_challenge"],
                         entry["code_challenge_method"]):
            return JSONResponse({"error": "invalid_grant"}, status_code=400)
    elif grant_type == "client_credentials":
        if not client_secret or client_secret != MCP_OAUTH_CLIENT_SECRET:
            return JSONResponse({"error": "invalid_client"}, status_code=401)
    else:
        return JSONResponse({"error": "unsupported_grant_type"}, status_code=400)

    return JSONResponse({
        "access_token": MCP_OAUTH_CLIENT_SECRET,
        "token_type": "bearer",
        "expires_in": 31536000,
    })


async def oauth_protected_resource(request):
    """RFC 9728 protected-resource metadata. Claude checks this (both the
    bare path and the /mcp-suffixed variant) to confirm which authorization
    server backs this resource before it will call /mcp at all."""
    base = str(request.base_url).rstrip("/")
    return JSONResponse({
        "resource": f"{base}/mcp",
        "authorization_servers": [base],
    })


app = Starlette(routes=[
    Route("/.well-known/oauth-authorization-server", oauth_metadata),
    Route("/.well-known/oauth-protected-resource", oauth_protected_resource),
    Route("/.well-known/oauth-protected-resource/mcp", oauth_protected_resource),
    Route("/authorize", oauth_authorize),
    Route("/oauth/token", oauth_token, methods=["POST"]),
    Mount("/", app=EnforcementAuthMiddleware(mcp.streamable_http_app())),
])

def run():
    host = os.environ.get("MCP_HOST", "127.0.0.1")
    uvicorn.run(app, host=host, port=8000)

if __name__ == "__main__":
    run()