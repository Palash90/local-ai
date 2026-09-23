"""MCP get_image: binary passthrough + upstream-error mapping."""

import pytest

import server.mcp_gateway as gw


@pytest.mark.asyncio
async def test_get_image_passthrough(monkeypatch):
    from mcp.server.fastmcp import Image
    sentinel = Image(data=b"\x89PNG", format="png")

    async def fake_call(path):
        assert path == "/api/image/abc123"
        return sentinel, None

    monkeypatch.setattr(gw, "_call_image", fake_call)
    assert await gw.get_image("abc123") is sentinel


@pytest.mark.asyncio
async def test_get_image_strips_leading_slash(monkeypatch):
    seen = {}

    async def fake_call(path):
        seen["path"] = path
        return None, "gone"

    monkeypatch.setattr(gw, "_call_image", fake_call)
    assert await gw.get_image("///x/y.png") == "gone"
    assert seen["path"] == "/api/image/x/y.png"


@pytest.mark.asyncio
async def test_get_image_upstream_error_string(monkeypatch):
    async def fake_call(path):
        return None, '{"error": "Upstream 404"}'

    monkeypatch.setattr(gw, "_call_image", fake_call)
    out = await gw.get_image("missing")
    assert "404" in out


@pytest.mark.asyncio
async def test_call_image_maps_http_error(monkeypatch):
    import httpx

    class Resp:
        status_code = 404
        text = "nope"
        headers = {}
        content = b""

    class Client:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get(self, *a, **k): return Resp()

    async def fake_auth():
        return {}

    monkeypatch.setattr(gw, "_auth_headers", fake_auth)
    monkeypatch.setattr(httpx, "AsyncClient", lambda: Client())
    img, err = await gw._call_image("/api/image/z")
    assert img is None and "404" in err
