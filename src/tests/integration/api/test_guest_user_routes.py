"""``/api/guest-users`` CRUD — backs Settings > General > Users.

Real-Postgres only — skips cleanly if 127.0.0.1:5432 isn't reachable, same
pattern as ``test_skills_routes.py``.

The regression these guard is the one the tab actually hit: the routes have to
exist *in this app* (aw-backend having them is invisible to the SPA, which
rewrites relative /api/* to this origin), an empty table must list as ``[]``
rather than 404, and every route must be identity-gated — the reference
implementation relied on a blanket AuthMiddleware this app doesn't have, so an
unauthenticated 200 here would mean credential CRUD was published to the world.
"""
from __future__ import annotations

import os

import pytest
import psycopg
from fastapi.testclient import TestClient
from sqlalchemy import text


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

_SCHEMA = "workspace_guestusersroutestest"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("AW_WORKSPACE_SCHEMA", _SCHEMA)
    monkeypatch.setenv("AW_WORKSPACE_DB_URL",
                        "postgresql://postgres:postgres@127.0.0.1:5432/awserv")
    monkeypatch.setenv("AW_WORKSPACE_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("AW_WORKSPACE_CONTAINER_DIR", str(tmp_path / "root"))

    import src.api.db as dbmod
    monkeypatch.setattr(dbmod, "_engine", None)

    import src.api.app as app_mod

    async def noop_reconcile(app):
        return None

    monkeypatch.setattr(app_mod, "reconcile_on_boot", noop_reconcile)

    with TestClient(app_mod.create_app()) as c:
        yield c

    from src.api.db import get_engine
    with get_engine().begin() as conn:
        conn.execute(text(f'DROP SCHEMA IF EXISTS "{_SCHEMA}" CASCADE'))
    monkeypatch.setattr(dbmod, "_engine", None)


def _auth_headers():
    return {"X-Api-Key": os.environ.get("AW_WORKSPACE_API_KEY", "")}


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("get", "/api/guest-users"),
        ("post", "/api/guest-users"),
        ("put", "/api/guest-users/1"),
        ("delete", "/api/guest-users/1"),
    ],
)
def test_every_route_is_identity_gated(client, method, path):
    # client.request(), not getattr(client, method) — httpx's get()/delete()
    # take no `json` kwarg, and the body has to be sent for post/put to reach
    # the identity dependency rather than failing body validation first.
    res = client.request(method.upper(), path, json={})
    assert res.status_code == 401, f"{method.upper()} {path} -> {res.status_code}"


def test_empty_list_is_an_empty_array_not_404(client):
    """The reported bug: the tab renders "Failed to load users (HTTP 404)" for
    anything non-200, so a fresh workspace must answer 200 + []."""
    res = client.get("/api/guest-users", headers=_auth_headers())
    assert res.status_code == 200, res.text
    assert res.json() == []


def test_create_list_update_delete_round_trip(client):
    created = client.post(
        "/api/guest-users",
        json={"username": "guest1", "password": "s3cret", "allowed_apps": ["crispal"]},
        headers=_auth_headers(),
    )
    assert created.status_code == 200, created.text
    body = created.json()
    user_id = body["id"]
    assert body["username"] == "guest1"
    assert body["allowed_apps"] == ["crispal"]
    assert "password_hash" not in body and "password" not in body

    listed = client.get("/api/guest-users", headers=_auth_headers()).json()
    assert [u["username"] for u in listed] == ["guest1"]

    updated = client.put(
        f"/api/guest-users/{user_id}",
        json={"allowed_apps": ["crispal", "kali"]},
        headers=_auth_headers(),
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["allowed_apps"] == ["crispal", "kali"]

    deleted = client.delete(f"/api/guest-users/{user_id}", headers=_auth_headers())
    assert deleted.status_code == 200
    assert client.get("/api/guest-users", headers=_auth_headers()).json() == []


def test_password_is_stored_hashed_never_in_plaintext(client):
    client.post(
        "/api/guest-users",
        json={"username": "guest2", "password": "plaintext-pw"},
        headers=_auth_headers(),
    )

    from sqlmodel import select

    from src.api.db import get_session
    from src.api.models import GuestUser

    with get_session() as session:
        row = session.exec(select(GuestUser).where(GuestUser.username == "guest2")).one()

    assert row.password_hash != "plaintext-pw"
    assert row.password_hash.startswith("$2")  # bcrypt

    import bcrypt

    assert bcrypt.checkpw(b"plaintext-pw", row.password_hash.encode())


def test_create_rejects_missing_username_or_password(client):
    assert client.post(
        "/api/guest-users", json={"username": "", "password": "x"}, headers=_auth_headers()
    ).status_code == 400
    assert client.post(
        "/api/guest-users", json={"username": "guest3", "password": ""},
        headers=_auth_headers(),
    ).status_code == 400


def test_create_rejects_a_duplicate_username(client):
    payload = {"username": "dupe", "password": "pw"}
    assert client.post(
        "/api/guest-users", json=payload, headers=_auth_headers()
    ).status_code == 200
    assert client.post(
        "/api/guest-users", json=payload, headers=_auth_headers()
    ).status_code == 409


def test_update_and_delete_404_on_an_unknown_id(client):
    assert client.put(
        "/api/guest-users/9999", json={"allowed_apps": []}, headers=_auth_headers()
    ).status_code == 404
    assert client.delete(
        "/api/guest-users/9999", headers=_auth_headers()
    ).status_code == 404
