"""``/api/settings/observability`` — the HTTP contract, the identity gate,
and that the route actually goes through the local-mode gating logic
instead of behaving like the raw ``GET/PUT /api/settings/{key}`` pair.
"""
from __future__ import annotations

import time
from types import SimpleNamespace

import jwt as pyjwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import FastAPI
from starlette.testclient import TestClient

from src.api import observability as observability_mod
from src.api.observability import register_observability_routes


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
    private_pem, public_pem = _pem_pair()
    monkeypatch.setenv("AW_AUTH_PUBLIC_KEY", public_pem)
    token = pyjwt.encode(
        {"sub": "u1", "exp": int(time.time()) + 3600}, private_pem, algorithm="EdDSA"
    )

    state = {"value": {}}
    monkeypatch.setattr(observability_mod, "_load", lambda session=None: dict(state["value"]))

    def fake_save(value):
        state["value"] = dict(value)

    monkeypatch.setattr(observability_mod, "_save", fake_save)
    monkeypatch.setattr(observability_mod, "app_public_url",
                         lambda app_id: f"https://{app_id}.app.ws.example.com")
    monkeypatch.setattr(observability_mod, "get_or_create_workspace_api_key",
                         lambda: "the-workspace-key")

    app = FastAPI()
    app.state.app_runtime = SimpleNamespace(is_loaded=lambda slug: True)
    register_observability_routes(app)
    client = TestClient(app)
    client.cookies.set("aw_id_jwt", token)
    return client, app


def test_requires_identity(ctx):
    client, _ = ctx
    client.cookies.clear()
    assert client.get("/api/settings/observability").status_code == 401


def test_defaults_to_off(ctx):
    client, _ = ctx
    res = client.get("/api/settings/observability")
    assert res.status_code == 200
    assert res.json()["mode"] == "off"


def test_set_local_then_read_it_back(ctx):
    client, _ = ctx
    put = client.put("/api/settings/observability", json={"mode": "local"})
    assert put.status_code == 200
    assert put.json()["resolved"]["endpoint"] == "https://signoz.app.ws.example.com"

    got = client.get("/api/settings/observability")
    assert got.json()["mode"] == "local"


def test_local_rejected_with_400_when_app_not_installed(ctx):
    client, app = ctx
    app.state.app_runtime.is_loaded = lambda slug: False

    res = client.put("/api/settings/observability", json={"mode": "local"})

    assert res.status_code == 400
    assert "installed" in res.json()["detail"]


def test_local_mode_falls_back_to_off_once_app_disappears(ctx):
    client, app = ctx
    client.put("/api/settings/observability", json={"mode": "local"})

    app.state.app_runtime.is_loaded = lambda slug: False
    res = client.get("/api/settings/observability")

    assert res.status_code == 200
    assert res.json()["mode"] == "off"
    assert "uninstalled" in res.json()["warning"]


def test_custom_round_trips_endpoint_and_key(ctx):
    client, _ = ctx
    put = client.put("/api/settings/observability", json={
        "mode": "custom",
        "custom": {"endpoint": "https://other.example.com", "api_key": "k"},
    })
    assert put.status_code == 200
    assert put.json()["resolved"] == {
        "endpoint": "https://other.example.com", "api_key": "k", "source": "custom",
    }

    got = client.get("/api/settings/observability")
    assert got.json()["custom"] == {"endpoint": "https://other.example.com", "api_key": "k"}


def test_custom_without_endpoint_is_400(ctx):
    client, _ = ctx
    res = client.put("/api/settings/observability", json={"mode": "custom"})
    assert res.status_code == 400


def test_unknown_mode_is_400(ctx):
    client, _ = ctx
    res = client.put("/api/settings/observability", json={"mode": "bogus"})
    assert res.status_code == 400
