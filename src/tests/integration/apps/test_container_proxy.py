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


def test_http_forwarding_does_not_alter_accept_encoding(monkeypatch):
    # No reason to touch this anymore — the proxy never decodes the
    # response, so it doesn't matter what encoding the upstream ends up
    # picking. Forward the caller's own Accept-Encoding unchanged, same as
    # every other non-hop-by-hop header.
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
    assert r.json() == {"accept_encoding": "gzip, deflate, br, zstd"}


def test_http_forwarding_relays_a_compressed_body_untouched(monkeypatch):
    # Upstream serves a real gzip-compressed body (like code-server's HTML)
    # with an accurate content-encoding header. The proxy must relay the RAW
    # bytes AND that header exactly as received — no decode, no re-describe
    # — so a standards-compliant client downstream (TestClient's own httpx,
    # here) can decode it correctly. Earlier versions of this proxy called
    # resp.content (httpx's DECOMPRESSED body) while still forwarding the
    # upstream's original content-encoding header, or silently failed to
    # decompress encodings httpx has no decoder for (br/zstd without the
    # optional packages) — both produced a body/header mismatch that
    # rendered as garbage. Confirmed live 2026-08-08 against
    # aw-app-code-server.
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
    assert r.headers["content-encoding"] == "gzip"
    # TestClient's own httpx client decodes this correctly ONLY if the raw
    # bytes we relayed are genuinely gzip-compressed AND the header truthfully
    # describes them — the exact pairing the old implementation broke.
    assert r.content == body


def test_http_502_when_upstream_unreachable(monkeypatch):
    async def boom(self, *args, **kwargs):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(httpx.AsyncClient, "send", boom)

    proxy = ContainerReverseProxy("http://127.0.0.1:1")
    app = Starlette(routes=[Mount("/api/apps/x", app=proxy)])
    client = TestClient(app)
    r = client.get("/api/apps/x/anything")
    assert r.status_code == 502
