import os, json, requests
from mcp.server.fastmcp import FastMCP, Context

API_BASE = "http://127.0.0.1:3001"
mcp = FastMCP("chat-webui-api", stateless_http=True)

def _call(method, path, ctx: Context, **kw):
    token = ctx.request_context.request.headers.get("authorization", "")
    headers = {"Authorization": token} if token else {}
    r = requests.request(method, f"{API_BASE}{path}", headers=headers, timeout=30, **kw)
    return r.text

@mcp.tool()
def list_sessions(ctx: Context) -> str:
    """List the current user's chat sessions."""
    return _call("GET", "/api/sessions", ctx)

@mcp.tool()
def get_session_messages(session_id: str, ctx: Context) -> str:
    """Get all messages in a chat session."""
    return _call("GET", f"/api/sessions/{session_id}/messages", ctx)

@mcp.tool()
def list_tasks(ctx: Context) -> str:
    """List the current user's tasks."""
    return _call("GET", "/api/tasks", ctx)

@mcp.tool()
def list_shares(ctx: Context) -> str:
    """List the current user's shared messages."""
    return _call("GET", "/api/shares", ctx)

@mcp.tool()
def share_message(session_id: str, msg_index: int, ctx: Context) -> str:
    """Create a public share link for a message."""
    return _call("POST", "/api/shares", ctx, json={"session_id": session_id, "msg_index": msg_index})

def run():
    mcp.run(transport="streamable-http")