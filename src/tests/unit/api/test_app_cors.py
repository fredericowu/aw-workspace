"""``_ScopedCORSMiddleware`` — the SPA<->API CORS grant must never touch a
Tier-2 container app's own hostname (``<app_id>.app.<anything>``).

Regression for the aw-app-signoz login bounce: Starlette's own
``CORSMiddleware`` stamps ``Access-Control-Allow-Credentials: true`` onto
ANY response carrying an ``Origin`` header, regardless of whether that
origin actually matched ``allow_origin_regex`` (see its ``send()``). An app
mounted at ``<app_id>.app.<slug>.<domain>`` that sets its own blanket
``Access-Control-Allow-Origin: *`` (SigNoz does) ended up with both headers
on the same response — a combination the Fetch/CORS spec forbids and a
spec-compliant browser refuses to use.
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api.app import _is_app_mount_host, _ScopedCORSMiddleware


@pytest.mark.parametrize("host,expected", [
    ("signoz.app.aw.workspace.aw.tekflox.com", True),
    ("proxy.app.ws.example.com", True),
    ("aw.workspace.aw.tekflox.com", False),
    ("api.aw.workspace.aw.tekflox.com", False),
    ("example.com", False),
])
def test_is_app_mount_host(host, expected):
    assert _is_app_mount_host(host) is expected


def _app():
    app = FastAPI()

    @app.get("/whatever")
    def whatever():
        # Mirrors a Tier-2 container app that sets its own blanket CORS
        # header on every response (e.g. SigNoz's backend).
        from fastapi import Response
        return Response(content="{}", media_type="application/json",
                         headers={"Access-Control-Allow-Origin": "*"})

    app.add_middleware(
        _ScopedCORSMiddleware,
        allow_origin_regex=r"^https://aw\.workspace\..+$",
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    return app


def test_app_mount_host_keeps_only_the_apps_own_cors_header():
    client = TestClient(_app())
    resp = client.get(
        "/whatever",
        headers={"Host": "signoz.app.aw.workspace.aw.tekflox.com",
                 "Origin": "https://signoz.app.aw.workspace.aw.tekflox.com"},
    )
    assert resp.headers.get("access-control-allow-origin") == "*"
    assert "access-control-allow-credentials" not in resp.headers


def test_spa_host_still_gets_the_credentialed_grant():
    client = TestClient(_app())
    resp = client.get(
        "/whatever",
        headers={"Host": "api.aw.workspace.aw.tekflox.com",
                 "Origin": "https://aw.workspace.aw.tekflox.com"},
    )
    assert resp.headers.get("access-control-allow-credentials") == "true"
