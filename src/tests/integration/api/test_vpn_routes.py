"""``/api/vpn*`` — the HTTP contract, the identity gate, and the **plane**.

The plane test is the one that matters. The falsifiable claim of this whole
design (``docs/architecture/vpn-profiles-in-general.md`` §2.1) is that a
relative ``/api/vpn/*`` fetch from the workspace SPA reaches *this* backend and
not ``aw-backend``, which already serves 17 routes under the same paths:
``apiBase.js:176-183`` rewrites relative ``/api/*`` on a workspace SPA host to
``api.<slug>.workspace.<apex>``. A test that proves an upload works against
``localhost`` proves nothing about which of the two backends answered — so
``test_the_routes_are_registered_on_the_core_app`` asserts registration on the
object ``src.api.app.create_app()`` returns, and nothing weaker.

The manager itself is exercised in ``src/tests/unit/vpn/test_profiles.py``;
what's here is the routing layer's own behaviour — status codes, error shapes,
multipart handling, and the two leak rules at the HTTP boundary.
"""
from __future__ import annotations

import io
import time

import jwt as pyjwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import FastAPI
from starlette.testclient import TestClient

from src.api.vpn import register_vpn_routes
from src.vpn.profiles import VpnProfiles

WG_OK = """
[Interface]
PrivateKey = aGVsbG8gd29ybGQgdGhpcyBpcyBub3QgYSBrZXk9
Address = 10.5.0.2/32

[Peer]
PublicKey = cHVibGljIGtleSB0aGF0IGlzIG5vdCByZWFsIGE9
AllowedIPs = 0.0.0.0/0
Endpoint = vpn.example.com:51820
""".strip()

WG_WITH_POSTUP = WG_OK.replace(
    "Address = 10.5.0.2/32",
    "Address = 10.5.0.2/32\nPostUp = /bin/sh -c 'id > /tmp/pwned'",
)


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
def client(tmp_path, monkeypatch):
    private_pem, public_pem = _pem_pair()
    monkeypatch.setenv("AW_AUTH_PUBLIC_KEY", public_pem)
    monkeypatch.setenv("AW_WORKSPACE_HOME", str(tmp_path / "home"))
    from cryptography.fernet import Fernet
    monkeypatch.setenv("AW_WORKSPACE_SECRET_KEY", Fernet.generate_key().decode())
    token = pyjwt.encode(
        {"sub": "u1", "exp": int(time.time()) + 3600}, private_pem, algorithm="EdDSA"
    )

    app = FastAPI()
    register_vpn_routes(app, VpnProfiles())
    c = TestClient(app)
    c.cookies.set("aw_id_jwt", token)
    return c


# --- the plane ----------------------------------------------------------------


def test_the_routes_are_registered_on_the_core_app(monkeypatch, tmp_path):
    """The claim: these routes answer on the WORKSPACE plane.

    Built against ``create_app()`` itself, with only the Postgres bootstrap
    stubbed, so this fails the day someone moves the surface back to
    ``aw-backend`` or forgets the ``register_vpn_routes`` call — which a test
    hitting a hand-built FastAPI app would not notice.
    """
    import src.api.app as app_mod

    # Point the home dir at a tmp path before anything registers: with
    # AW_WORKSPACE_HOME unset, paths.py falls back to
    # /opt/aw-workspace/.aw-workspace, which on a CI runner is either absent or
    # read-only. Only the Postgres bootstrap is stubbed — everything else in
    # create_app() runs for real, which is the point.
    monkeypatch.setenv("AW_WORKSPACE_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(app_mod, "create_all_tables", lambda: None)

    paths = {getattr(r, "path", None) for r in app_mod.create_app().routes}

    assert "/api/vpn/configs" in paths
    assert "/api/vpn/configs/{name}" in paths
    assert "/api/vpn/configs/upload" in paths
    assert "/api/vpn/status" in paths
    assert "/api/vpn/nord/credentials" in paths
    assert "/api/vpn/nord/countries" in paths
    assert "/api/vpn/nord/recommendations" in paths
    assert "/api/vpn/nord/import" in paths


def test_upload_is_not_shadowed_by_the_name_route(client):
    """Starlette matches in registration order, not by specificity: registered
    after ``/configs/{name}``, ``POST /configs/upload`` would be swallowed as a
    profile literally named "upload" — 405 on a route that looks present."""
    res = client.post(
        "/api/vpn/configs/upload",
        data={"type": "wireguard", "name": "wg0"},
        files={"file": ("wg0.conf", io.BytesIO(WG_OK.encode()), "text/plain")},
    )
    assert res.status_code == 200, res.text
    assert res.json()["name"] == "wg0"


# --- identity -----------------------------------------------------------------


@pytest.mark.parametrize("path", ["/api/vpn/configs", "/api/vpn/status",
                                  "/api/vpn/nord/credentials"])
def test_requires_identity(client, path):
    client.cookies.clear()
    assert client.get(path).status_code == 401


# --- rule 1: rejection, at the HTTP boundary ---------------------------------


def test_put_rejects_postup_by_name_and_stores_nothing(client):
    res = client.put("/api/vpn/configs/wg0",
                     json={"type": "wireguard", "content": WG_WITH_POSTUP})

    assert res.status_code == 400
    detail = res.json()["detail"]
    assert detail["error"] == "rejected_directive"
    assert detail["directive"] == "PostUp"
    # Not merged, not silently stripped — and not on disk either.
    assert client.get("/api/vpn/configs").json()["configs"] == []
    assert client.get("/api/vpn/configs/wg0").status_code == 404


def test_upload_rejects_postup_too(client):
    res = client.post(
        "/api/vpn/configs/upload",
        data={"type": "wireguard"},
        files={"file": ("wg0.conf", io.BytesIO(WG_WITH_POSTUP.encode()), "text/plain")},
    )

    assert res.status_code == 400
    assert res.json()["detail"]["directive"] == "PostUp"
    assert client.get("/api/vpn/configs").json()["configs"] == []


def test_upload_refuses_a_binary_file(client):
    res = client.post(
        "/api/vpn/configs/upload",
        data={"type": "wireguard", "name": "wg0"},
        files={"file": ("wg0.conf", io.BytesIO(b"\xff\xfe\x00binary"), "application/octet-stream")},
    )
    assert res.status_code == 400
    assert "UTF-8" in res.json()["detail"]


def test_upload_refuses_an_oversized_file(client):
    from src.api import vpn as vpn_mod

    big = io.BytesIO(b"x" * (vpn_mod.MAX_UPLOAD_BYTES + 1))
    res = client.post(
        "/api/vpn/configs/upload",
        data={"type": "wireguard", "name": "wg0"},
        files={"file": ("wg0.conf", big, "text/plain")},
    )
    assert res.status_code == 400
    assert "too large" in res.json()["detail"]


def test_put_without_type_or_content_is_400(client):
    assert client.put("/api/vpn/configs/wg0", json={"type": "wireguard"}).status_code == 400
    assert client.put("/api/vpn/configs/wg0", json={"content": WG_OK}).status_code == 400


# --- rule 2: no key material over HTTP ---------------------------------------


def test_the_detail_view_never_returns_the_private_key(client):
    client.put("/api/vpn/configs/wg0", json={"type": "wireguard", "content": WG_OK})

    body = client.get("/api/vpn/configs/wg0").json()

    assert body["redacted"] is True
    assert "aGVsbG8gd29ybGQ" not in body["content"]
    # And the list view carries no body at all.
    assert "content" not in client.get("/api/vpn/configs").json()["configs"][0]


# --- rule 3: Nord credentials never round-trip -------------------------------


def test_nord_credentials_get_returns_presence_not_the_secret(client):
    put = client.put("/api/vpn/nord/credentials",
                     json={"service_username": "fredericoservice",
                           "service_password": "sup3rs3cret"})
    assert put.status_code == 200

    res = client.get("/api/vpn/nord/credentials")

    assert res.status_code == 200
    assert res.json()["configured"] is True
    assert "sup3rs3cret" not in res.text, "the service password left the server"
    assert "fredericoservice" not in res.text


def test_nord_credentials_put_validates(client):
    res = client.put("/api/vpn/nord/credentials", json={"service_username": "only"})
    assert res.status_code == 400


# --- status -------------------------------------------------------------------


def test_status_never_fabricates_a_connection(client):
    body = client.get("/api/vpn/status").json()

    assert body["connected"] is False
    assert body["can_dial"] is False
    assert body["state"] == "no_tunnel_host"
    assert "nothing dials" in body["detail"]


def test_there_are_no_lifecycle_routes(client):
    """Phase 1 dials nothing, so the routes that would dial must not exist —
    a 404 here is the contract, not a gap."""
    assert client.post("/api/vpn/start", json={"name": "wg0"}).status_code == 404
    assert client.post("/api/vpn/stop").status_code == 404
    assert client.put("/api/vpn/settings/vpn-only", json={"enabled": True}).status_code == 404


# --- CRUD ---------------------------------------------------------------------


def test_round_trip_over_http(client):
    created = client.put("/api/vpn/configs/wg0",
                         json={"type": "wireguard", "content": WG_OK})
    assert created.status_code == 200
    assert created.json()["endpoint"] == "vpn.example.com:51820"

    listed = client.get("/api/vpn/configs").json()["configs"]
    assert [c["name"] for c in listed] == ["wg0"]

    assert client.delete("/api/vpn/configs/wg0").status_code == 200
    assert client.get("/api/vpn/configs").json()["configs"] == []


def test_deleting_an_unknown_profile_is_404(client):
    assert client.delete("/api/vpn/configs/nope").status_code == 404


# --- Nord ---------------------------------------------------------------------


def test_nord_countries_are_proxied(client, monkeypatch):
    from src.vpn import profiles as mod

    class _Res:
        def raise_for_status(self):
            return None

        def json(self):
            return [{"id": 1, "name": "Portugal", "code": "PT",
                     "cities": [{"id": 9, "name": "Lisbon"}]}]

    monkeypatch.setattr(mod.httpx, "get", lambda url, **kw: _Res())

    body = client.get("/api/vpn/nord/countries").json()

    assert body["countries"][0]["code"] == "PT"
    assert body["countries"][0]["cities"] == [{"id": 9, "name": "Lisbon"}]


def test_a_nord_outage_is_a_502_not_a_500(client, monkeypatch):
    """Nord's public API is an unversioned third-party dependency; when it is
    down that is not this workspace failing."""
    import httpx as httpx_mod
    from src.vpn import profiles as mod

    def boom(url, **kw):
        raise httpx_mod.ConnectError("nord is down")

    monkeypatch.setattr(mod.httpx, "get", boom)

    assert client.get("/api/vpn/nord/countries").status_code == 502
    assert client.get("/api/vpn/nord/recommendations").status_code == 502
    assert client.post("/api/vpn/nord/import",
                       json={"hostname": "pt121.nordvpn.com"}).status_code == 502


def test_nord_import_stores_the_profile(client, monkeypatch):
    from src.vpn import profiles as mod

    ovpn = "client\ndev tun\nproto udp\nremote pt121.nordvpn.com 1194\n"

    class _Res:
        text = ovpn

        def raise_for_status(self):
            return None

    monkeypatch.setattr(mod.httpx, "get", lambda url, **kw: _Res())

    res = client.post("/api/vpn/nord/import",
                      json={"hostname": "pt121.nordvpn.com", "protocol": "udp"})

    assert res.status_code == 200
    assert res.json()["name"] == "pt121-udp"
    assert [c["source"] for c in client.get("/api/vpn/configs").json()["configs"]] == ["nord"]


def test_nord_import_without_a_hostname_is_400(client):
    assert client.post("/api/vpn/nord/import", json={}).status_code == 400
