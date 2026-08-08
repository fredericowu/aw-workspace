"""ContainerReverseProxy — HTTP forwarding + target URL building (Phase 6).

The proxy forwards to a container ``base_url``; here the upstream is a tiny
in-process ASGI app reached through httpx's ASGI transport (monkeypatched onto
the proxy's ``httpx.AsyncClient``), so no real container/socket is needed.
"""
from __future__ import annotations

import asyncio

import httpx
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route
from starlette.testclient import TestClient

from src.apps.proxy import ContainerReverseProxy


def test_target_url_building():
    proxy = ContainerReverseProxy("http://aw-app-browser:9222")
    scope = {"path": "/json/version", "query_string": b"foo=bar"}
    assert proxy._target(scope) == "http://aw-app-browser:9222/json/version?foo=bar"
    assert proxy._target(scope, ws=True) == "ws://aw-app-browser:9222/json/version?foo=bar"


def test_http_forwarding(monkeypatch):
    # Upstream "container": echoes path + method back as JSON.
    async def echo(request):
        return JSONResponse({"path": request.url.path, "method": request.method,
                             "q": request.url.query})

    upstream = Starlette(routes=[Route("/{p:path}", echo, methods=["GET", "POST"])])

    # Point the proxy's httpx client at the upstream ASGI app instead of the net.
    orig_init = httpx.AsyncClient.__init__

    def patched_init(self, *args, **kwargs):
        kwargs["transport"] = httpx.ASGITransport(app=upstream)
        orig_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", patched_init)

    proxy = ContainerReverseProxy("http://upstream")
    app = Starlette(routes=[Mount("/api/apps/browser", app=proxy)])
    client = TestClient(app)

    r = client.get("/api/apps/browser/json/version?x=1")
    assert r.status_code == 200
    body = r.json()
    assert body["path"] == "/json/version"
    assert body["method"] == "GET"
    assert body["q"] == "x=1"


def test_http_forwarding_adds_verified_identity_headers(monkeypatch):
    async def echo_headers(request):
        return JSONResponse({
            "sub": request.headers.get("x-aw-identity-sub"),
            "email": request.headers.get("x-aw-identity-email"),
        })

    upstream = Starlette(routes=[Route("/{p:path}", echo_headers, methods=["GET"])])
    orig_init = httpx.AsyncClient.__init__

    def patched_init(self, *args, **kwargs):
        kwargs["transport"] = httpx.ASGITransport(app=upstream)
        orig_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", patched_init)

    proxy = ContainerReverseProxy("http://upstream")

    async def with_identity(scope, receive, send):
        scope["aw_identity"] = {"sub": "verified-user", "email": "user@example.test"}
        await proxy(scope, receive, send)

    app = Starlette(routes=[Mount("/api/apps/browser", app=with_identity)])
    client = TestClient(app)

    r = client.get("/api/apps/browser/headers", headers={"x-aw-identity-sub": "spoofed"})
    assert r.status_code == 200
    assert r.json() == {"sub": "verified-user", "email": "user@example.test"}


def test_http_forwarding_pins_accept_encoding_to_what_httpx_can_decode(monkeypatch):
    # A real browser's Accept-Encoding lists br/zstd, which httpx can only
    # decode if the optional brotli/zstandard packages are installed (they
    # aren't in this image). If forwarded verbatim, the upstream could pick
    # `br`; httpx then silently falls back to IdentityDecoder (no error) and
    # resp.content ends up as the raw, still-compressed bytes — this proxy's
    # own content-encoding stripping would then lie to Caddy about the body
    # being plain, and Caddy would gzip already-compressed bytes. Confirmed
    # live 2026-08-08 against aw-app-code-server. Pin to gzip/deflate
    # (Python stdlib zlib, always decodable) regardless of what the
    # downstream client advertised.
    async def echo_accept_encoding(request):
        return JSONResponse({"accept_encoding": request.headers.get("accept-encoding")})

    upstream = Starlette(routes=[Route("/{p:path}", echo_accept_encoding, methods=["GET"])])
    orig_init = httpx.AsyncClient.__init__

    def patched_init(self, *args, **kwargs):
        kwargs["transport"] = httpx.ASGITransport(app=upstream)
        orig_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", patched_init)

    proxy = ContainerReverseProxy("http://upstream")
    app = Starlette(routes=[Mount("/api/apps/code-server", app=proxy)])
    client = TestClient(app)

    r = client.get(
        "/api/apps/code-server/",
        headers={"accept-encoding": "gzip, deflate, br, zstd"},
    )
    assert r.status_code == 200
    assert r.json() == {"accept_encoding": "gzip, deflate"}


def test_http_forwarding_strips_stale_content_encoding(monkeypatch):
    # Upstream serves a real gzip-compressed body (like code-server's HTML).
    # httpx auto-decompresses before we ever see resp.content, so forwarding
    # the upstream's own content-encoding/content-length verbatim would tell
    # the downstream client "this body is still gzipped" when it isn't —
    # confirmed live against aw-app-code-server (page rendered as mojibake).
    import gzip

    from starlette.responses import Response

    body = b"<html>hello world</html>" * 50

    async def gzipped(request):
        return Response(
            content=gzip.compress(body),
            media_type="text/html",
            headers={"content-encoding": "gzip"},
        )

    upstream = Starlette(routes=[Route("/{p:path}", gzipped, methods=["GET"])])
    orig_init = httpx.AsyncClient.__init__

    def patched_init(self, *args, **kwargs):
        kwargs["transport"] = httpx.ASGITransport(app=upstream)
        orig_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", patched_init)

    proxy = ContainerReverseProxy("http://upstream")
    app = Starlette(routes=[Mount("/api/apps/code-server", app=proxy)])
    client = TestClient(app)

    r = client.get("/api/apps/code-server/")
    assert r.status_code == 200
    assert r.content == body
    assert "content-encoding" not in r.headers


def test_http_502_when_upstream_unreachable(monkeypatch):
    async def boom(self, *args, **kwargs):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(httpx.AsyncClient, "request", boom)

    proxy = ContainerReverseProxy("http://127.0.0.1:1")
    app = Starlette(routes=[Mount("/api/apps/x", app=proxy)])
    client = TestClient(app)
    r = client.get("/api/apps/x/anything")
    assert r.status_code == 502
