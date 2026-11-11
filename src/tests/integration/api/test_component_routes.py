"""Component compatibility routes for Tier-2 app containers."""
from __future__ import annotations

import time
from types import SimpleNamespace

import jwt as pyjwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import FastAPI
from starlette.testclient import TestClient

from src.api.components import register_component_routes


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


class _FakeContainers:
    def __init__(self, *, emulated: bool = False, start_error: Exception | None = None) -> None:
        self.running = True
        self.container = SimpleNamespace(
            name="aw-app-browser",
            image="ghcr.io/example/browser:1",
            port=9222,
        )
        self._emulated = emulated
        self._start_error = start_error

    def registered(self):
        return [("browser", self.container)]

    def status(self, app_id: str):
        assert app_id == "browser"
        return {
            "container": self.container.name,
            "running": self.running,
            "status": "running" if self.running else "exited",
            "url": "http://aw-app-browser:9222",
            "image_arch": "amd64" if self._emulated else "arm64",
            "emulated": self._emulated and self.running,
            "emulated_detail": "running emulated: image is amd64, host is arm64"
                                if self._emulated else None,
        }

    def start(self, app_id: str):
        assert app_id == "browser"
        if self._start_error is not None:
            raise self._start_error
        self.running = True
        return self.status(app_id)

    def stop(self, app_id: str):
        assert app_id == "browser"
        self.running = False
        return self.status(app_id)


class _FakeHub:
    def __init__(self) -> None:
        self.messages = []

    def broadcast_soon(self, message: dict):
        self.messages.append(message)


def _make_ctx(monkeypatch, containers):
    private_pem, public_pem = _pem_pair()
    monkeypatch.setenv("AW_AUTH_PUBLIC_KEY", public_pem)
    token = pyjwt.encode(
        {"sub": "u1", "exp": int(time.time()) + 3600}, private_pem, algorithm="EdDSA"
    )

    app = FastAPI()
    app.state.app_runtime = SimpleNamespace(
        containers=containers,
        get=lambda app_id: SimpleNamespace(
            manifest=SimpleNamespace(description="Browser workspace app")
        ),
    )
    app.state.status_hub = _FakeHub()
    register_component_routes(app)
    client = TestClient(app)
    client.cookies.set("aw_id_jwt", token)
    return client, app


@pytest.fixture()
def ctx(monkeypatch):
    return _make_ctx(monkeypatch, _FakeContainers())


def test_lists_tier2_containers_as_legacy_components(ctx):
    client, _ = ctx
    rows = client.get("/api/components").json()

    assert rows[0]["key"] == "docker:aw-browser"
    assert rows[0]["component"] == "browser"
    assert rows[0]["mode"] == "docker"
    assert rows[0]["status"] == "running"
    assert rows[0]["running"] is True


def test_lifecycle_routes_accept_legacy_aliases_and_broadcast(ctx):
    client, app = ctx

    stopped = client.post("/api/components/docker:browser/stop").json()
    assert stopped["key"] == "docker:aw-browser"
    assert stopped["action"] == "stopped"
    assert stopped["running"] is False

    restarted = client.post("/api/components/docker:aw-browser/restart").json()
    assert restarted["action"] == "restarted"
    assert restarted["running"] is True
    assert app.state.status_hub.messages[-1]["key"] == "docker:aw-browser"


def test_component_routes_require_identity(ctx):
    client, _ = ctx
    client.cookies.clear()

    assert client.get("/api/components").status_code == 401
    assert client.post("/api/components/docker:browser/start").status_code == 401


def test_an_emulated_app_surfaces_its_arch_in_the_component_row(monkeypatch):
    """card 4155bf3b-9510-8127: ``ContainerSupervisor.status()``'s
    ``image_arch``/``emulated``/``emulated_detail`` must reach the legacy
    component row — this is what the restart API response and the
    components table both read from."""
    client, _ = _make_ctx(monkeypatch, _FakeContainers(emulated=True))

    row = client.get("/api/components").json()[0]

    assert row["image_arch"] == "amd64"
    assert row["emulated"] is True
    assert row["emulated_detail"] == "running emulated: image is amd64, host is arm64"


def test_a_restart_that_cannot_satisfy_the_arch_fallback_returns_400(monkeypatch):
    """When neither the native pull nor the amd64 fallback can succeed,
    ``ContainerSupervisor.start()`` raises ``ContainerError`` — this must
    reach the caller as a clear 400 JSON error, never a bare 500
    traceback (the PO's success condition 3)."""
    from src.apps.containers import ContainerError

    message = ("image ghcr.io/example/browser:1 has no build for this host "
               "(host architecture arm64) and the linux/amd64 fallback could "
               "not be pulled — native pull: boom; amd64 pull: boom2")
    client, _ = _make_ctx(
        monkeypatch, _FakeContainers(start_error=ContainerError(message)))

    resp = client.post("/api/components/docker:browser/restart")

    assert resp.status_code == 400
    assert resp.json() == {"error": message}
