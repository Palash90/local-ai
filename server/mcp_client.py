import asyncio
import json
import os
import sys
from typing import Dict, Any, List, Optional
from dataclasses import dataclass

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.sse import sse_client
from mcp.client.streamable_http import streamablehttp_client


@dataclass
class ServerConfig:
    name: str
    transport: str  # "stdio", "sse", or "http"
    command: Optional[str] = None
    args: Optional[List[str]] = None
    url: Optional[str] = None
    env: Optional[Dict[str, str]] = None


# Only these (read-only) MCP tool names are surfaced to the model and may be
# dispatched. Everything else — e.g. mutating/indexing tools like
# ``delete_project``, ``index_repository``, ``manage_adr``, ``ingest_traces`` —
# is never advertised and is hard-blocked at call time so the LLM cannot perform
# destructive operations on the codebase. Extend this list only for tools you
# explicitly trust to be side-effect-free.
MCP_READONLY_TOOL_NAMES = frozenset({
    "search_graph",
    "trace_path",
    "check_index_coverage",
    "detect_changes",
    "query_graph",
    "get_architecture",
    "get_graph_schema",
    "get_code_snippet",
    "search_code",
    "list_projects",
    "index_status",
})


class MCPClientManager:
    """Manages connection lifecycles and tool execution across multiple MCP servers."""
    
    def __init__(self, config_path: str = "mcp_config.json"):
        self.config_path = config_path
        self.sessions: Dict[str, ClientSession] = {}
        self.transports: Dict[str, Any] = {}
        self._tools_cache: Dict[str, List[Dict[str, Any]]] = {}
        # Bumped whenever tool definitions change so downstream per-session
        # caches (see server/features/llm.py) can detect staleness.
        self._tools_version = 0
        # Cached OpenAI-formatted tool list, rebuilt only on refresh_tools().
        self._openai_tools_cache: List[Dict[str, Any]] = []

    def load_configs(self) -> List[ServerConfig]:
        if not os.path.exists(self.config_path):
            return []
        
        with open(self.config_path, "r", encoding="utf-8") as f:
            raw_config = json.load(f)

        configs = []
        mcp_servers = raw_config.get("mcpServers", {})
        for name, cfg in mcp_servers.items():
            if cfg.get("enabled", True) is False:
                continue

            # Auto-detect transport type based on keys
            if "url" in cfg:
                transport_type = cfg.get("transport", "http") # "http" or "sse"
                configs.append(ServerConfig(
                    name=name,
                    transport=transport_type,
                    url=cfg["url"]
                ))
            elif "command" in cfg:
                env = os.environ.copy()
                if "env" in cfg:
                    env.update(cfg["env"])
                configs.append(ServerConfig(
                    name=name,
                    transport="stdio",
                    command=cfg["command"],
                    args=cfg.get("args", []),
                    env=env
                ))
        return configs

    async def connect_server(self, config: ServerConfig):
        """Connects to a single MCP server based on its transport type."""
        try:
            if config.transport == "stdio":
                params = StdioServerParameters(
                    command=config.command,
                    args=config.args or [],
                    env=config.env
                )
                transport_cm = stdio_client(params)
            elif config.transport == "sse":
                transport_cm = sse_client(config.url)
            elif config.transport in ("http", "streamable_http"):
                transport_cm = streamablehttp_client(config.url)
            else:
                raise ValueError(f"Unsupported transport: {config.transport}")

            # Enter transport context manager
            streams = await transport_cm.__aenter__()
            self.transports[config.name] = transport_cm

            # Initialize ClientSession
            session = ClientSession(streams[0], streams[1])
            await session.__aenter__()
            await session.initialize()

            self.sessions[config.name] = session
            print(f"[MCP] Successfully connected to '{config.name}' via {config.transport}")
        except Exception as e:
            print(f"[MCP] Failed to connect to server '{config.name}': {e}")

    async def initialize_all(self):
        """Discovers and connects to all configured servers."""
        configs = self.load_configs()
        for cfg in configs:
            await self.connect_server(cfg)
        await self.refresh_tools()

    async def refresh_tools(self):
        """Gathers tool definitions from all active sessions."""
        self._tools_cache.clear()
        for name, session in self.sessions.items():
            try:
                result = await session.list_tools()
                # Store tools with namespace to avoid collisions: serverName__toolName
                tools = []
                for t in result.tools:
                    tools.append({
                        "name": f"{name}__{t.name}",
                        "raw_name": t.name,
                        "server": name,
                        "description": t.description,
                        "parameters": t.inputSchema
                    })
                self._tools_cache[name] = tools
            except Exception as e:
                print(f"[MCP] Error fetching tools from '{name}': {e}")
        # Rebuild the cached OpenAI-formatted list once and bump the version so
        # per-session tool caches invalidate.
        self._openai_tools_cache = self._build_openai_tools()
        self._tools_version += 1

    def _build_openai_tools(self) -> List[Dict[str, Any]]:
        result = []
        for tools in self._tools_cache.values():
            for t in tools:
                if t["raw_name"] not in MCP_READONLY_TOOL_NAMES:
                    # Never advertise tools the model is not allowed to call.
                    continue
                result.append({
                    "type": "function",
                    "function": {
                        "name": t["name"],
                        "description": t["description"],
                        "parameters": t["parameters"]
                    }
                })
        return result

    def is_mcp_tool(self, namespaced_tool: str) -> bool:
        """True if ``namespaced_tool`` (e.g. ``server__tool``) names a read-only
        tool served by a connected MCP server. Destructive tools are never
        dispatchable regardless of connection state."""
        raw = namespaced_tool.split("__", 1)[1] if "__" in namespaced_tool else namespaced_tool
        if raw not in MCP_READONLY_TOOL_NAMES:
            return False
        if "__" in namespaced_tool:
            server_name = namespaced_tool.split("__", 1)[0]
            return server_name in self.sessions
        return any(
            t["raw_name"] == namespaced_tool
            for tools in self._tools_cache.values()
            for t in tools
        )

    def get_all_tools(self) -> List[Dict[str, Any]]:
        all_tools = []
        try:
            for tools in self._tools_cache.values():
                all_tools.extend(tools)
        except Exception as e:
            print(e)
        return all_tools

    def get_openai_tools(self):
        # Returned list is built once per refresh_tools() (see _build_openai_tools)
        # to avoid rebuilding the schemas on every LLM round. A copy is returned
        # so no caller can mutate (and thereby corrupt) the shared cache.
        return list(self._openai_tools_cache)


    async def call_tool(self, namespaced_tool: str, arguments: Dict[str, Any]) -> Any:
        """Routes execution to the correct MCP server session."""
        if "__" in namespaced_tool:
            server_name, tool_name = namespaced_tool.split("__", 1)
        else:
            server_name, tool_name = None, namespaced_tool
            for s_name, tools in self._tools_cache.items():
                if any(t["raw_name"] == tool_name for t in tools):
                    server_name = s_name
                    break

        session = self.sessions.get(server_name)
        if not session:
            raise RuntimeError(f"No active session for server '{server_name}'")

        # Hard block destructive MCP tools at execution time too, regardless of
        # how the tool was referenced (defense in depth).
        if tool_name not in MCP_READONLY_TOOL_NAMES:
            raise RuntimeError(
                f"MCP tool '{tool_name}' is not allowed (read-only allowlist)."
            )

        result = await session.call_tool(tool_name, arguments)

        # Stream the text blocks under a hard character budget instead of
        # materializing the whole result and slicing afterward. This bounds the
        # memory/pipe cost (a multi-MB search result never enters our context)
        # and preserves whole leading snippets rather than cutting the head.
        MAX_CHARS = 4000
        content_out = []
        total = 0
        omitted = 0
        for block in result.content:
            text = block.text if hasattr(block, "text") else str(block)
            if not text:
                continue
            if total + len(text) > MAX_CHARS:
                # If nothing has been emitted yet, cap this (single oversized)
                # block so one huge snippet can't evade the budget.
                if not content_out:
                    content_out.append(text[:MAX_CHARS])
                    total = MAX_CHARS
                omitted += 1
                continue
            content_out.append(text)
            total += len(text)

        full_text = "\n".join(content_out)
        if omitted:
            full_text += (
                f"\n\n[Output truncated: {omitted} additional result block(s) omitted "
                f"to keep within the {MAX_CHARS}-character limit. Narrow your query "
                f"to retrieve fewer matches.]"
            )
        return full_text

    async def close(self):
        """Gracefully shuts down all transports and sessions."""
        for name, session in self.sessions.items():
            try:
                await session.__aexit__(None, None, None)
            except Exception:
                pass
        for name, transport in self.transports.items():
            try:
                await transport.__aexit__(None, None, None)
            except Exception:
                pass
        self.sessions.clear()
        self.transports.clear()

mcp_manager = MCPClientManager("mcp_config.json")
_mcp_loop = None

def start_mcp_client():
    global _mcp_loop
    _mcp_loop = asyncio.new_event_loop()
    asyncio.set_event_loop(_mcp_loop)
    _mcp_loop.run_until_complete(mcp_manager.initialize_all())
    _mcp_loop.run_forever()

def dispatch_mcp_tool(tool_name, arguments, timeout):
    if _mcp_loop is None or not _mcp_loop.is_running():
        raise RuntimeError("[MCP] Event loop not running - MCP not yet initialized")

    future = asyncio.run_coroutine_threadsafe(mcp_manager.call_tool(tool_name, arguments), _mcp_loop)
    return future.result(timeout)