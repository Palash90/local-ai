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
        # Only process HTTP requests
        if scope["type"] == "http":
            # Handle CORS preflight
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


@mcp.tool()
async def list_sessions() -> str:
    """List the current user's chat sessions."""
    return await _call("GET", "/api/sessions")


@mcp.tool()
async def get_session_messages(session_id: str) -> str:
    """Get all messages in a chat session."""
    return await _call("GET", f"/api/sessions/{session_id}/messages")


@mcp.tool()
async def list_tasks() -> str:
    """List the current user's tasks."""
    return await _call("GET", "/api/tasks")


@mcp.tool()
async def list_shares() -> str:
    """List the current user's shared messages."""
    return await _call("GET", "/api/shares")


@mcp.tool()
async def share_message(session_id: str, msg_index: int) -> str:
    """Create a public share link for a message."""
    return await _call(
        "POST",
        "/api/shares",
        json={"session_id": session_id, "msg_index": msg_index},
    )


# Extract the FastMCP HTTP app and wrap it directly
app = EnforcementAuthMiddleware(mcp.streamable_http_app())

def run():
    uvicorn.run(app, host="0.0.0.0", port=8000)

if __name__ == "__main__":
    run()