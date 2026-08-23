import os, httpx
from mcp.server.fastmcp import FastMCP
from starlette.responses import JSONResponse
import uvicorn

API_BASE = "http://127.0.0.1:3001"
STATIC_TOKEN = os.environ.get("MCP_GATEWAY_TOKEN", "")
INBOUND_TOKEN = os.environ.get("MCP_INBOUND_TOKEN", "secret-mcp-key")

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


async def _call(method: str, path: str, **kw) -> str:
    headers = {"Authorization": f"Bearer {STATIC_TOKEN}"} if STATIC_TOKEN else {}
    async with httpx.AsyncClient() as client:
        r = await client.request(method, f"{API_BASE}{path}", headers=headers, timeout=30.0, **kw)
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
async def get_task_status(task_id: str) -> str:
    """Check status of an asynchronous processing task."""
    return await _call("GET", f"/api/status/{task_id}")


app = EnforcementAuthMiddleware(mcp.streamable_http_app())

def run():
    uvicorn.run(app, host="0.0.0.0", port=8000)

if __name__ == "__main__":
    run()
