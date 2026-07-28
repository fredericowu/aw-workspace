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


def test_http_502_when_upstream_unreachable(monkeypatch):
    async def boom(self, *args, **kwargs):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(httpx.AsyncClient, "request", boom)

    proxy = ContainerReverseProxy("http://127.0.0.1:1")
    app = Starlette(routes=[Mount("/api/apps/x", app=proxy)])
    client = TestClient(app)
    r = client.get("/api/apps/x/anything")
    assert r.status_code == 502
