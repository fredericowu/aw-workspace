"""Every ``authorize_ws``-gated WebSocket still rejects a credential-less handshake.

``authorize_ws`` became ``async def`` on 2026-09-04 (both of its checks block:
a synchronous Postgres read for the ``X-Api-Key`` path, and a synchronous
``httpx.get`` for the public key on a worker's first authenticated request).
That makes a forgotten ``await`` at any of its call sites an *authentication
bypass*, not a crash — the call returns a coroutine object, which is truthy, so
``if not claims`` is False and the handler proceeds to accept the socket for a
caller who presented nothing.

Three of the five call sites already had a 4401 test where they live —
``/ws/terminal/{id}`` and ``/ws/status`` in ``test_terminal_routes.py``,
``/ws/notifications`` in ``test_notification_routes.py``. This file covers the
two that did not, so the invariant holds at every site rather than at 3/5. A
test that only asserts "an authorized handshake succeeds" would pass happily
against the un-awaited version.
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from src.api.components import register_component_routes
from src.apps.routes import register_apps_routes


def _assert_ws_closed(client: TestClient, path: str, expected_code: int) -> None:
    """The handlers accept() first and then close() with a code — closing
    before accept is delivered as a bare HTTP rejection that TestClient never
    surfaces as a disconnect (see the note in TerminalRoutes.terminal_stream)."""
    try:
        with client.websocket_connect(path) as ws:
            msg = ws.receive()
        assert msg["type"] == "websocket.close" and msg["code"] == expected_code, msg
    except WebSocketDisconnect as exc:
        assert exc.code == expected_code


@pytest.fixture()
def component_client(monkeypatch):
    monkeypatch.delenv("AW_AUTH_PUBLIC_KEY", raising=False)
    app = FastAPI()
    register_component_routes(app)
    return TestClient(app)


@pytest.fixture()
def apps_client(monkeypatch, tmp_path):
    monkeypatch.delenv("AW_AUTH_PUBLIC_KEY", raising=False)
    monkeypatch.setenv("AW_WORKSPACE_HOME", str(tmp_path / "home"))
    app = FastAPI()
    register_apps_routes(app)
    return TestClient(app)


def test_ws_component_logs_rejects_missing_credential(component_client):
    _assert_ws_closed(component_client, "/ws/logs/docker:aw-browser", 4401)


def test_ws_apps_install_status_rejects_missing_credential(apps_client):
    _assert_ws_closed(apps_client, "/ws/apps/install-status", 4401)
