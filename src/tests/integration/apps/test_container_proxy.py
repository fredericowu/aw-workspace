"""ContainerReverseProxy — HTTP forwarding + target URL building (Phase 6).

The proxy forwards to a container ``base_url``; here the upstream is a tiny
in-process ASGI app reached through httpx's ASGI transport (monkeypatched onto
the proxy's ``httpx.AsyncClient``), so no real container/socket is needed.
"""
from __future__ import annotations

import asyncio

import httpx
from starlette.applications import Starlette
from starlette.responses import HTMLResponse, JSONResponse, Response
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


# --- heuristic-caching guard -------------------------------------------------
#
# App backends set no Cache-Control at all, so browsers invent a freshness
# lifetime and serve stale HTML/JSON without revalidating (RFC 9111 4.2.2).
# The proxy closes that hole for the two content types where it's never
# correct, and only when the upstream said nothing.


def _proxy_to(upstream, monkeypatch, mount="/api/apps/kb"):
    orig_init = httpx.AsyncClient.__init__

    def patched_init(self, *args, **kwargs):
        kwargs["transport"] = httpx.ASGITransport(app=upstream)
        orig_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", patched_init)
    proxy = ContainerReverseProxy("http://upstream")
    return TestClient(Starlette(routes=[Mount(mount, app=proxy)]))


def test_html_without_cache_control_becomes_no_store(monkeypatch):
    """index.html is the only unhashed file Vite emits and it names the hashed
    bundle — caching it pins the whole app to the previous build."""
    async def index(request):
        return HTMLResponse("<!doctype html><html></html>")

    upstream = Starlette(routes=[Route("/{p:path}", index, methods=["GET"])])
    client = _proxy_to(upstream, monkeypatch)

    r = client.get("/api/apps/kb/")

    assert r.headers["cache-control"] == "no-store, must-revalidate"


def test_json_without_cache_control_becomes_no_store(monkeypatch):
    """API GETs come back with no validator at all, so a cached response can
    outlive the state it describes."""
    async def data(request):
        return JSONResponse({"folders": ["aw-docs"]})

    upstream = Starlette(routes=[Route("/{p:path}", data, methods=["GET"])])
    client = _proxy_to(upstream, monkeypatch)

    r = client.get("/api/apps/kb/api/kb/repos")

    assert r.headers["cache-control"] == "no-store, must-revalidate"


def test_upstream_cache_control_is_never_overridden(monkeypatch):
    """An app that made a deliberate caching choice keeps it — this proxy adds,
    it does not rewrite."""
    async def cached(request):
        return JSONResponse({"ok": True}, headers={"cache-control": "max-age=60"})

    upstream = Starlette(routes=[Route("/{p:path}", cached, methods=["GET"])])
    client = _proxy_to(upstream, monkeypatch)

    r = client.get("/api/apps/kb/api/thing")

    assert r.headers["cache-control"] == "max-age=60"
    # Exactly one — an appended duplicate would let either value win per client.
    assert [v for k, v in r.headers.multi_items() if k.lower() == "cache-control"] == ["max-age=60"]


def test_hashed_assets_are_left_cacheable(monkeypatch):
    """JS/CSS filenames ARE their cache keys — caching them hard is the point,
    and no-store here would defeat the whole hashed-bundle design."""
    async def asset(request):
        return Response(b"console.log(1)", media_type="text/javascript")

    upstream = Starlette(routes=[Route("/{p:path}", asset, methods=["GET"])])
    client = _proxy_to(upstream, monkeypatch)

    r = client.get("/api/apps/kb/assets/index-D5K2CSjZ.js")

    assert "cache-control" not in r.headers
