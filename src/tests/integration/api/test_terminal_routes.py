"""Route-level tests for the ported terminal API + WebSockets.

Covers the identity gate (REST + both WebSockets) and the CRUD contract the
SPA depends on. Agent-session history (``/api/v2/agent-sessions*``) moved to
``aw-app-code-agent-clis`` (2026-08-03) — no longer a core route, so no
longer tested here. The DB layer is stubbed
(``create_all_tables`` → no-op) because terminals are in-memory and identity
is verified offline — so this runs with no Postgres.

The live PTY byte-stream is covered by ``test_terminal_manager`` (TestClient's
portal event loop does not service ``loop.add_reader`` fd callbacks, so the
byte plumbing is asserted at the manager level instead).
"""
from __future__ import annotations

import time

import jwt as pyjwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from starlette.websockets import WebSocketDisconnect


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
    monkeypatch.setenv("AW_WORKSPACE_SCHEMA", "workspace_test")
    monkeypatch.setenv("AW_AUTH_PUBLIC_KEY", pub)

    from starlette.testclient import TestClient
    import src.api.app as app_module
    from src.api.app import create_app

    # Patch the name as bound into src.api.app (`from src.api.db import
    # create_all_tables`), not src.api.db itself — src.api.app may already be
    # imported (and its own reference already resolved) by an earlier test in
    # the same session, in which case patching src.api.db has no effect here.
    monkeypatch.setattr(app_module, "create_all_tables", lambda: None)

    token = pyjwt.encode(
        {"sub": "u1", "exp": int(time.time()) + 3600}, priv, algorithm="EdDSA"
    )
    client = TestClient(create_app())
    return client, token


def test_rest_requires_identity(ctx):
    client, _ = ctx
    assert client.get("/api/terminals").status_code == 401
    assert client.post("/api/terminals", json={}).status_code == 401


def test_terminal_crud_contract(ctx):
    client, token = ctx
    client.cookies.set("aw_id_jwt", token)

    r = client.post("/api/terminals", json={"name": "one"})
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"id", "name", "type"} and body["type"] == "terminal"
    tid = body["id"]

    listed = client.get("/api/terminals").json()
    row = next(t for t in listed if t["id"] == tid)
    assert row["name"] == "one" and row["alive"] is True
    assert set(row) >= {"id", "name", "type", "alive", "insecure", "agent_session_id"}

    assert client.put(f"/api/terminals/{tid}", json={"name": "two"}).json()["name"] == "two"

    assert client.delete(f"/api/terminals/{tid}").json()["success"] is True
    assert all(t["id"] != tid for t in client.get("/api/terminals").json())


def _assert_ws_closed(client, path, expected_code):
    """Connect and assert the server closed with ``expected_code``.

    The server rejects unauthorized sockets by accept()-then-close(code) — a
    close frame the client sees either as a returned ``websocket.close``
    message or as a raised ``WebSocketDisconnect`` (depending on the Starlette
    version). Accept either.
    """
    try:
        with client.websocket_connect(path) as ws:
            msg = ws.receive()
        assert msg["type"] == "websocket.close" and msg["code"] == expected_code, msg
    except WebSocketDisconnect as exc:
        assert exc.code == expected_code


def test_ws_terminal_rejects_missing_token(ctx):
    client, token = ctx
    client.cookies.set("aw_id_jwt", token)
    tid = client.post("/api/terminals", json={}).json()["id"]
    # Present NO credential: clear the cookie so the handshake carries no token.
    client.cookies.clear()
    _assert_ws_closed(client, f"/ws/terminal/{tid}", 4401)


def test_ws_status_rejects_missing_token(ctx):
    client, _ = ctx
    _assert_ws_closed(client, "/ws/status", 4401)


def test_ws_terminal_accepts_with_query_token(ctx):
    client, token = ctx
    client.cookies.set("aw_id_jwt", token)
    tid = client.post("/api/terminals", json={}).json()["id"]
    with client.websocket_connect(f"/ws/terminal/{tid}?token={token}") as ws:
        # Accepted: a resize control frame must not raise.
        ws.send_json({"type": "resize", "rows": 30, "cols": 100})
    client.delete(f"/api/terminals/{tid}")


def test_ws_status_pushes_init(ctx):
    client, token = ctx
    client.cookies.set("aw_id_jwt", token)
    with client.websocket_connect(f"/ws/status?token={token}") as ws:
        init = ws.receive_json()
        assert init["type"] == "init"
        assert "terminals" in init and "components" in init
