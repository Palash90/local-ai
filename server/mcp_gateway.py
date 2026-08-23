import os, sys, json, time, threading, base64, httpx
from mcp.server.fastmcp import FastMCP
from starlette.responses import JSONResponse
import uvicorn

try:
    from server.auth import oidc_password_grant
except ImportError:
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from server.auth import oidc_password_grant

API_BASE = os.environ.get("CHAT_API_BASE", "http://127.0.0.1:3001")
INBOUND_TOKEN = os.environ.get("MCP_INBOUND_TOKEN", "secret-mcp-key")
MCP_USER = os.environ.get("MCP_USER", "")
MCP_USER_PASSWORD = os.environ.get("MCP_USER_PASSWORD", "")
TOKEN_REFRESH_MARGIN = 60

mcp = FastMCP("chat-webui-api", stateless_http=True)

class EnforcementAuthMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            if scope.get("method") == "OPTIONS":
                await self.app(scope, receive, send)
                return

            headers = dict(scope.get("headers", []))
            auth_header = headers.get(b"authorization", b"").decode("utf-8")
            expected_token = f"Bearer {INBOUND_TOKEN}"

            if auth_header != expected_token:
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

# --- PROFILE ---

@mcp.tool()
async def get_user_context() -> str:
    """Get the current user's profile and memory context."""
    return await _call("GET", "/api/user-context")

# --- CHAT ---

@mcp.tool()
async def list_sessions() -> str:
    """List all chat sessions for the current user."""
    return await _call("GET", "/api/sessions")

@mcp.tool()
async def create_session(system_prompt: str = "", system_prompts: list = None) -> str:
    """Create a new chat session."""
    payload = {}
    if system_prompt:
        payload["system_prompt"] = system_prompt
    if system_prompts:
        payload["system_prompts"] = system_prompts
    return await _call("POST", "/api/sessions", json=payload)

@mcp.tool()
async def get_session_messages(session_id: str) -> str:
    """Get all messages from a specific chat session."""
    return await _call("GET", f"/api/sessions/{session_id}/messages")

@mcp.tool()
async def rename_session(session_id: str, name: str) -> str:
    """Rename an existing chat session."""
    return await _call("PUT", f"/api/sessions/{session_id}", json={"name": name})

@mcp.tool()
async def send_chat_message(
    session_id: str,
    message: str,
    research: bool = False,
    cpu: bool = False,
    no_tools: bool = False
) -> str:
    """Submit a message to a session and queue processing."""
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
    """Check status of an asynchronous chat message task."""
    return await _call("GET", f"/api/status/{task_id}")


app = EnforcementAuthMiddleware(mcp.streamable_http_app())

def run():
    host = os.environ.get("MCP_HOST", "127.0.0.1")
    uvicorn.run(app, host=host, port=8000)

if __name__ == "__main__":
    run()
