"""Route-level tests for the ported notification API + WebSocket.

Covers the identity gate (REST + WS), the POST/GET/DELETE contract the SPA's
``TopNotifications``/``App.jsx`` panels depend on, and WS broadcast delivery.
Real-Postgres only (the notifications table lives in a real schema) — skips
cleanly if 127.0.0.1:5432 isn't reachable, same pattern as ``test_isolation``.
"""
from __future__ import annotations

import time

import psycopg
import pytest
import jwt as pyjwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from sqlalchemy import text
from starlette.websockets import WebSocketDisconnect


def _postgres_reachable() -> bool:
    try:
        psycopg.connect(
            "postgresql://postgres:postgres@127.0.0.1:5432/postgres",
            autocommit=True, connect_timeout=2,
        ).close()
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _postgres_reachable(), reason="live Postgres at 127.0.0.1:5432 not reachable"
)

_SCHEMA = "workspace_notiftest"


def _pem_pair():
    key = Ed25519PrivateKey.generate()
    private_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    public_pem = key.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode()
    return private_pem, public_pem


@pytest.fixture()
def ctx(monkeypatch):
    priv, pub = _pem_pair()
    monkeypatch.setenv("AW_WORKSPACE", "test")
    monkeypatch.setenv("AW_WORKSPACE_SCHEMA", _SCHEMA)
    monkeypatch.setenv("AW_WORKSPACE_DB_URL",
                       "postgresql://postgres:postgres@127.0.0.1:5432/awserv")
    monkeypatch.setenv("AW_AUTH_PUBLIC_KEY", pub)

    import src.api.db as dbmod
    monkeypatch.setattr(dbmod, "_engine", None)

    from starlette.testclient import TestClient
    from src.api.app import create_app

    token = pyjwt.encode(
        {"sub": "u1", "exp": int(time.time()) + 3600}, priv, algorithm="EdDSA"
    )
    # `with` triggers the ASGI lifespan (startup/shutdown) — needed here because
    # NotificationManager.set_loop() only runs in lifespan startup, and the WS
    # broadcast path is a no-op (silently drops the message) without a loop set.
    with TestClient(create_app()) as client:
        yield client, token

    from src.api.db import get_engine
    with get_engine().begin() as conn:
        conn.execute(text(f'DROP SCHEMA IF EXISTS "{_SCHEMA}" CASCADE'))
    monkeypatch.setattr(dbmod, "_engine", None)


def test_rest_requires_identity(ctx):
    client, _ = ctx
    assert client.post("/api/notify", json={"message": "hi"}).status_code == 401
    assert client.get("/api/notify/pending").status_code == 401
    assert client.get("/api/notify/recent").status_code == 401
    assert client.delete("/api/notify/1").status_code == 401


def test_notify_persists_pending_and_dismiss(ctx):
    client, token = ctx
    client.cookies.set("aw_id_jwt", token)

    r = client.post("/api/notify", json={
        "message": "build finished", "level": "success", "title": "CI",
    })
    assert r.status_code == 200
    body = r.json()
    assert body["success"] is True
    notif = body["notification"]
    assert notif["message"] == "build finished"
    assert notif["level"] == "success"
    assert notif["status"] == "new"
    notif_id = notif["id"]

    pending = client.get("/api/notify/pending").json()["notifications"]
    assert any(n["id"] == notif_id for n in pending)

    recent = client.get("/api/notify/recent").json()["notifications"]
    assert any(n["id"] == notif_id for n in recent)

    assert client.delete(f"/api/notify/{notif_id}").json()["success"] is True
    pending_after = client.get("/api/notify/pending").json()["notifications"]
    assert all(n["id"] != notif_id for n in pending_after)


def test_notify_requires_message(ctx):
    client, token = ctx
    client.cookies.set("aw_id_jwt", token)
    assert client.post("/api/notify", json={}).json() == {"error": "message is required"}


def test_notify_dedupes_by_external_id(ctx):
    client, token = ctx
    client.cookies.set("aw_id_jwt", token)

    first = client.post("/api/notify", json={
        "message": "PR opened", "external_id": "pr-1", "external_status": "open",
    }).json()
    assert first["success"] is True

    dup = client.post("/api/notify", json={
        "message": "PR opened again", "external_id": "pr-1", "external_status": "open",
    }).json()
    assert dup == {"success": False, "reason": "duplicate"}


def _assert_ws_closed(client, path, expected_code):
    try:
        with client.websocket_connect(path) as ws:
            msg = ws.receive()
        assert msg["type"] == "websocket.close" and msg["code"] == expected_code, msg
    except WebSocketDisconnect as exc:
        assert exc.code == expected_code


def test_ws_notifications_rejects_missing_token(ctx):
    client, _ = ctx
    _assert_ws_closed(client, "/ws/notifications", 4401)


def test_ws_notifications_delivers_broadcast(ctx):
    client, token = ctx
    # No pending notifications yet on a fresh schema, so the server sends no
    # ninja_init frame on connect (see notification_stream: `if pending: ...`)
    # — go straight to triggering a live broadcast.
    with client.websocket_connect(f"/ws/notifications?token={token}") as ws:
        client.cookies.set("aw_id_jwt", token)
        client.post("/api/notify", json={"message": "live update"})

        pushed = ws.receive_json()
        assert pushed["type"] == "ninja_notification"
        assert pushed["notification"]["message"] == "live update"


def test_ws_notifications_sends_init_with_pending(ctx):
    client, token = ctx
    client.cookies.set("aw_id_jwt", token)
    client.post("/api/notify", json={"message": "queued before connect"})

    with client.websocket_connect(f"/ws/notifications?token={token}") as ws:
        init = ws.receive_json()
        assert init["type"] == "ninja_init"
        assert any(n["message"] == "queued before connect" for n in init["notifications"])
