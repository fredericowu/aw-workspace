"""Scoped API keys: the store, the CRUD routes, and the framework-route gate.

Guard behavior (what happens on an APP route) lives in
``src/tests/integration/apps/test_scoped_api_key_guard.py``. This file owns
the other half: minting/resolving/expiring/revoking against a real Postgres,
the Settings CRUD surface, and ``require_identity``/``authorize_ws`` refusing
a scoped key on framework routes — the thing that stops a scoped key from
minting itself a wider one.

Real-Postgres only (schema isolation is a real-Postgres concept) — skips
cleanly if 127.0.0.1:5432 isn't reachable, same pattern as
``test_workspace_api_key.py`` / ``test_settings_route_order.py``. Nothing
here monkeypatches the resolver: the point is that a key minted through the
public route resolves through the real DB path.
"""
from __future__ import annotations

import time

import psycopg
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
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

_SCHEMA = "workspace_scopedkeytest"
_RULES = [{"app": "xiaomi", "paths": ["/tv/*"]}]


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("AW_WORKSPACE_SCHEMA", _SCHEMA)
    monkeypatch.setenv("AW_WORKSPACE_DB_URL",
                       "postgresql://postgres:postgres@127.0.0.1:5432/awserv")
    monkeypatch.setenv("AW_WORKSPACE_HOME", str(tmp_path / "home"))
    import src.api.db as dbmod
    monkeypatch.setattr(dbmod, "_engine", None)
    from src.api.db import create_all_tables, get_engine
    create_all_tables()

    yield

    with get_engine().begin() as conn:
        conn.execute(text(f'DROP SCHEMA IF EXISTS "{_SCHEMA}" CASCADE'))
    monkeypatch.setattr(dbmod, "_engine", None)


# ---- the tables exist at all (create_all, no hand-rolled ALTER) --------


def test_create_all_tables_creates_both_new_tables(env):
    # Both tables are NEW, so SQLModel.metadata.create_all covers them and
    # no idempotent ALTER is needed (src/api/db.py's create_all_tables).
    from src.api.db import get_engine

    with get_engine().begin() as conn:
        rows = conn.execute(text(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = :s"), {"s": _SCHEMA}).fetchall()
    present = {r[0] for r in rows}
    assert "api_scopes" in present
    assert "scoped_api_keys" in present


# ---- the store: mint, resolve, expire, revoke -------------------------


def test_mint_then_resolve_returns_the_scopes_rules(env):
    from src.api.scoped_api_keys import create_key, create_scope, resolve_scoped_key

    scope = create_scope("xiaomi-tv", _RULES)
    key = create_key("alexa", scope["id"], None)

    assert key["token"].startswith("awsk_")
    assert len(key["token"]) == len("awsk_") + 64  # token_hex(32)
    assert key["expires_at"] is None  # never
    assert key["key_hint"] == key["token"][:10]

    claims = resolve_scoped_key(key["token"])
    assert claims["sub"] == "scoped-api-key"
    assert claims["scoped"] is True
    assert claims["key_name"] == "alexa"
    assert claims["scope_name"] == "xiaomi-tv"
    assert claims["rules"] == _RULES


def test_only_the_hash_is_stored_never_the_token(env):
    from src.api.scoped_api_keys import create_key, create_scope, hash_key
    from src.api.db import get_engine

    scope = create_scope("xiaomi-tv", _RULES)
    key = create_key("alexa", scope["id"], None)

    with get_engine().begin() as conn:
        # Raw SQL bypasses the engine's schema_translate_map (that only
        # rewrites Table objects), so qualify the schema by hand here.
        row = conn.execute(text(
            f'SELECT key_hash FROM "{_SCHEMA}".scoped_api_keys WHERE id = :i'),
            {"i": key["id"]}).fetchone()
    assert row[0] == hash_key(key["token"])
    assert key["token"] not in row[0]


def test_listing_keys_never_exposes_the_token(env):
    from src.api.scoped_api_keys import create_key, create_scope, list_keys

    scope = create_scope("xiaomi-tv", _RULES)
    key = create_key("alexa", scope["id"], None)

    listed = list_keys()
    assert len(listed) == 1
    assert "token" not in listed[0]
    assert "key_hash" not in listed[0]
    assert listed[0]["key_hint"] == key["key_hint"]


def test_resolve_rejects_a_wrong_token(env):
    from src.api.scoped_api_keys import create_key, create_scope, resolve_scoped_key

    scope = create_scope("xiaomi-tv", _RULES)
    create_key("alexa", scope["id"], None)
    assert resolve_scoped_key("awsk_" + "f" * 64) is None


def test_resolve_rejects_a_value_without_the_prefix(env):
    # The prefix IS the discriminator — a raw hex token that happens to
    # match a stored hash must still not resolve here, or the two
    # credential classes would overlap.
    from src.api.scoped_api_keys import create_key, create_scope, resolve_scoped_key

    scope = create_scope("xiaomi-tv", _RULES)
    key = create_key("alexa", scope["id"], None)
    assert resolve_scoped_key(key["token"][len("awsk_"):]) is None
    assert resolve_scoped_key("") is None


def test_resolve_rejects_an_expired_key(env):
    from src.api.scoped_api_keys import create_key, create_scope, resolve_scoped_key

    scope = create_scope("xiaomi-tv", _RULES)
    key = create_key("short-lived", scope["id"], 1.0)
    assert resolve_scoped_key(key["token"]) is not None

    # Don't sleep — move the stored expiry into the past instead.
    from src.api.db import get_engine
    with get_engine().begin() as conn:
        conn.execute(text(
            f'UPDATE "{_SCHEMA}".scoped_api_keys SET expires_at = :e WHERE id = :i'),
            {"e": time.time() - 1, "i": key["id"]})
    assert resolve_scoped_key(key["token"]) is None


def test_revoke_takes_effect_immediately(env):
    from src.api.scoped_api_keys import (create_key, create_scope, resolve_scoped_key,
                                         revoke_key)

    scope = create_scope("xiaomi-tv", _RULES)
    key = create_key("alexa", scope["id"], None)
    assert resolve_scoped_key(key["token"]) is not None

    revoked = revoke_key(key["id"])
    assert revoked["revoked_at"] is not None
    assert resolve_scoped_key(key["token"]) is None

    # Idempotent: re-revoking keeps the original timestamp.
    again = revoke_key(key["id"])
    assert again["revoked_at"] == revoked["revoked_at"]


def test_revoke_unknown_key_is_none(env):
    from src.api.scoped_api_keys import revoke_key

    assert revoke_key("nope") is None


def test_last_used_at_is_recorded_and_then_throttled(env, monkeypatch):
    # One write per minute per key, not one per request — this is the
    # Settings list's "last used", not an audit log.
    import src.api.scoped_api_keys as mod
    from src.api.scoped_api_keys import create_key, create_scope, list_keys, resolve_scoped_key

    scope = create_scope("xiaomi-tv", _RULES)
    key = create_key("alexa", scope["id"], None)
    assert list_keys()[0]["last_used_at"] is None

    resolve_scoped_key(key["token"])
    first = list_keys()[0]["last_used_at"]
    assert first is not None

    resolve_scoped_key(key["token"])
    assert list_keys()[0]["last_used_at"] == first  # throttled

    monkeypatch.setattr(mod, "_LAST_USED_THROTTLE_S", 0.0)
    resolve_scoped_key(key["token"])
    assert list_keys()[0]["last_used_at"] > first


def test_resolving_is_not_cached_so_a_rules_edit_lands_at_once(env):
    # No in-process cache, on purpose: at AW_WORKSPACE_WORKERS>1 a
    # per-worker cache would keep serving the old rules on every worker but
    # the one that took the edit.
    from src.api.scoped_api_keys import (create_key, create_scope, resolve_scoped_key,
                                         update_scope)

    scope = create_scope("xiaomi-tv", _RULES)
    key = create_key("alexa", scope["id"], None)
    assert resolve_scoped_key(key["token"])["rules"] == _RULES

    new_rules = [{"app": "xiaomi", "paths": ["/light/*"], "methods": ["POST"]}]
    update_scope(scope["id"], rules=new_rules)
    assert resolve_scoped_key(key["token"])["rules"] == new_rules


# ---- scope validation / lifecycle -------------------------------------


def test_duplicate_scope_name_is_rejected(env):
    from src.api.scoped_api_keys import create_scope

    create_scope("xiaomi-tv", _RULES)
    with pytest.raises(ValueError, match="already exists"):
        create_scope("xiaomi-tv", _RULES)


@pytest.mark.parametrize("bad", [
    [],                                                   # empty
    "not-a-list",                                         # wrong type
    [{"paths": ["/tv/*"]}],                               # no app
    [{"app": "xiaomi"}],                                  # no paths
    [{"app": "xiaomi", "paths": []}],                     # empty paths
    [{"app": "xiaomi", "paths": ["tv/power"]}],           # not mount-relative
    [{"app": "xiaomi", "paths": ["/tv/*"], "methods": "GET"}],  # methods not a list
])
def test_bad_rules_are_rejected(env, bad):
    from src.api.scoped_api_keys import create_scope

    with pytest.raises(ValueError):
        create_scope("bad", bad)


def test_methods_are_upper_cased_on_the_way_in(env):
    from src.api.scoped_api_keys import create_scope

    scope = create_scope("xiaomi-tv",
                         [{"app": "xiaomi", "paths": ["/tv/*"], "methods": ["post"]}])
    assert scope["rules"][0]["methods"] == ["POST"]


def test_creating_a_key_for_an_unknown_scope_is_rejected(env):
    from src.api.scoped_api_keys import create_key

    with pytest.raises(ValueError, match="no such scope"):
        create_key("alexa", "does-not-exist", None)


def test_scope_delete_is_refused_while_a_key_references_it(env):
    from src.api.scoped_api_keys import (create_key, create_scope, delete_key,
                                         delete_scope, revoke_key)

    scope = create_scope("xiaomi-tv", _RULES)
    key = create_key("alexa", scope["id"], None)

    assert delete_scope(scope["id"]) == "in_use"
    # A revoked key still holds the reference — the row stays for the audit
    # trail, so revoking alone does not free the scope.
    revoke_key(key["id"])
    assert delete_scope(scope["id"]) == "in_use"

    assert delete_key(key["id"]) is True
    assert delete_scope(scope["id"]) == "deleted"
    assert delete_scope(scope["id"]) == "not_found"


# ---- the CRUD routes --------------------------------------------------


@pytest.fixture()
def client(env, tmp_path, monkeypatch):
    """A real app over the throwaway schema, with the boot background task
    neutered at BOTH ends.

    ``reconcile_on_boot`` is stubbed for the documented reason (see
    ``src/api/app.py``'s lifespan: it walks every configured app with real
    network retries, and a TestClient exiting mid-pass cannot cancel the
    thread already in flight).

    ``sync_on_boot`` is stubbed for the same class of reason, and this file
    is what makes it matter: it builds ~20 TestClients, so ~20 fire-and-
    forget ``agent_sync.sync_all()`` threads would be left racing. Those
    threads read ``AW_WORKSPACE_CONTAINER_DIR`` from the process environment
    when they run, not when they were started, so one leaking out of this
    file lands on whatever root a LATER test has monkeypatched — and
    ``materialize()`` is an exact-mirror rewrite, so two of them on one tree
    produce a torn tree rather than a redundant one (the same hazard
    ``app.py`` documents for N workers). It cost a red CI run on
    ``test_skills_routes.py``, which boots its own app and then asserts on
    the tree that sync is supposed to produce.

    ``AW_WORKSPACE_CONTAINER_DIR`` is pointed at a throwaway dir as well, so
    nothing here can reach the live workspace tree even if something else
    syncs — same posture as conftest's ``_isolated_app_lock_dir``.
    """
    import src.api.app as app_mod

    monkeypatch.setenv("AW_WORKSPACE_CONTAINER_DIR", str(tmp_path / "root"))

    async def noop_reconcile(app):
        return None

    async def noop_sync():
        return None

    monkeypatch.setattr(app_mod, "reconcile_on_boot", noop_reconcile)
    monkeypatch.setattr(app_mod, "sync_on_boot", noop_sync)
    with TestClient(app_mod.create_app()) as c:
        yield c


def _master_headers():
    import os
    return {"X-Api-Key": os.environ.get("AW_WORKSPACE_API_KEY", "")}


def test_crud_round_trip_over_http(client):
    created = client.post("/api/settings/api-scopes", headers=_master_headers(),
                          json={"name": "xiaomi-tv", "rules": _RULES})
    assert created.status_code == 200, created.text
    scope_id = created.json()["id"]

    listed = client.get("/api/settings/api-scopes", headers=_master_headers())
    assert [s["name"] for s in listed.json()["scopes"]] == ["xiaomi-tv"]

    minted = client.post("/api/settings/api-keys", headers=_master_headers(),
                         json={"name": "alexa", "scope_id": scope_id,
                               "expires_in_seconds": None})
    assert minted.status_code == 200, minted.text
    body = minted.json()
    assert body["token"].startswith("awsk_")
    assert body["expires_at"] is None

    keys = client.get("/api/settings/api-keys", headers=_master_headers()).json()["keys"]
    assert len(keys) == 1 and "token" not in keys[0]

    revoked = client.post(f"/api/settings/api-keys/{body['id']}/revoke",
                          headers=_master_headers())
    assert revoked.status_code == 200 and revoked.json()["revoked_at"] is not None


def test_the_token_is_returned_exactly_once(client):
    scope_id = client.post("/api/settings/api-scopes", headers=_master_headers(),
                           json={"name": "xiaomi-tv", "rules": _RULES}).json()["id"]
    token = client.post("/api/settings/api-keys", headers=_master_headers(),
                        json={"name": "alexa", "scope_id": scope_id,
                              "expires_in_seconds": None}).json()["token"]
    # Nothing in any later read carries it back — only its hint.
    keys = client.get("/api/settings/api-keys", headers=_master_headers()).json()["keys"]
    assert token not in client.get(
        "/api/settings/api-keys", headers=_master_headers()).text
    assert keys[0]["key_hint"] == token[:10]


def test_expiry_preset_is_an_arbitrary_duration_on_the_api(client):
    scope_id = client.post("/api/settings/api-scopes", headers=_master_headers(),
                           json={"name": "xiaomi-tv", "rules": _RULES}).json()["id"]
    body = client.post("/api/settings/api-keys", headers=_master_headers(),
                       json={"name": "hour", "scope_id": scope_id,
                             "expires_in_seconds": 3600}).json()
    assert 3500 < body["expires_at"] - time.time() < 3700


def test_duplicate_scope_name_is_409_over_http(client):
    payload = {"name": "xiaomi-tv", "rules": _RULES}
    assert client.post("/api/settings/api-scopes", headers=_master_headers(),
                       json=payload).status_code == 200
    assert client.post("/api/settings/api-scopes", headers=_master_headers(),
                       json=payload).status_code == 409


def test_bad_rules_are_400_over_http(client):
    r = client.post("/api/settings/api-scopes", headers=_master_headers(),
                    json={"name": "bad", "rules": [{"app": "xiaomi"}]})
    assert r.status_code == 400
    assert "paths" in r.json()["detail"]


def test_scope_delete_is_409_while_keys_reference_it(client):
    scope_id = client.post("/api/settings/api-scopes", headers=_master_headers(),
                           json={"name": "xiaomi-tv", "rules": _RULES}).json()["id"]
    key_id = client.post("/api/settings/api-keys", headers=_master_headers(),
                         json={"name": "alexa", "scope_id": scope_id,
                               "expires_in_seconds": None}).json()["id"]

    conflict = client.delete(f"/api/settings/api-scopes/{scope_id}",
                             headers=_master_headers())
    assert conflict.status_code == 409
    assert "still reference" in conflict.json()["detail"]

    assert client.delete(f"/api/settings/api-keys/{key_id}",
                         headers=_master_headers()).status_code == 200
    assert client.delete(f"/api/settings/api-scopes/{scope_id}",
                         headers=_master_headers()).status_code == 200


def test_crud_routes_require_identity(client):
    # No credential at all → 401, same as every other framework route.
    assert client.get("/api/settings/api-scopes").status_code == 401
    assert client.get("/api/settings/api-keys").status_code == 401


def test_the_crud_routes_are_not_shadowed_by_the_generic_settings_catchall(client):
    # /api/settings/{key} is registered last on purpose (see
    # test_settings_route_order.py) — pin that these two literals reach
    # their own handlers rather than the catch-all's {"key","value"} shape.
    body = client.get("/api/settings/api-scopes", headers=_master_headers()).json()
    assert "scopes" in body and "value" not in body
    body = client.get("/api/settings/api-keys", headers=_master_headers()).json()
    assert "keys" in body and "value" not in body


# ---- the framework-route gate ----------------------------------------


def _mint(client, *, expires_in_seconds=None, name="alexa"):
    scope_id = client.post("/api/settings/api-scopes", headers=_master_headers(),
                           json={"name": f"scope-{name}", "rules": _RULES}).json()["id"]
    return client.post("/api/settings/api-keys", headers=_master_headers(),
                       json={"name": name, "scope_id": scope_id,
                             "expires_in_seconds": expires_in_seconds}).json()


def test_a_valid_scoped_key_is_403ed_on_framework_routes(client):
    key = _mint(client)
    r = client.get("/api/settings/api-keys", headers={"X-Api-Key": key["token"]})
    assert r.status_code == 403
    assert r.json()["detail"] == "scoped keys cannot access framework routes"


def test_a_scoped_key_cannot_mint_another_key(client):
    # The capability the whole credential class exists to withhold.
    key = _mint(client)
    scope_id = client.get("/api/settings/api-scopes",
                          headers=_master_headers()).json()["scopes"][0]["id"]
    r = client.post("/api/settings/api-keys", headers={"X-Api-Key": key["token"]},
                    json={"name": "escalated", "scope_id": scope_id,
                          "expires_in_seconds": None})
    assert r.status_code == 403


def test_an_invalid_scoped_key_is_401ed_on_framework_routes(client):
    r = client.get("/api/settings/api-keys",
                   headers={"X-Api-Key": "awsk_" + "e" * 64})
    assert r.status_code == 401


def test_a_revoked_scoped_key_is_401ed_not_403ed(client):
    key = _mint(client)
    client.post(f"/api/settings/api-keys/{key['id']}/revoke", headers=_master_headers())
    r = client.get("/api/settings/api-keys", headers={"X-Api-Key": key["token"]})
    assert r.status_code == 401


def test_the_master_key_still_reaches_framework_routes(client):
    # No regression: the credential every internal caller uses is untouched.
    assert client.get("/api/settings/api-keys",
                      headers=_master_headers()).status_code == 200


def _ws_close_code(client, path, headers=None):
    """The handlers accept() then close() with a code — see test_ws_auth_gate."""
    try:
        with client.websocket_connect(path, headers=headers or {}) as ws:
            msg = ws.receive()
        assert msg["type"] == "websocket.close", msg
        return msg["code"]
    except WebSocketDisconnect as exc:
        return exc.code


@pytest.fixture()
def ws_client(env, monkeypatch):
    from src.api.components import register_component_routes

    monkeypatch.delenv("AW_AUTH_PUBLIC_KEY", raising=False)
    app = FastAPI()
    register_component_routes(app)
    return TestClient(app)


def test_framework_ws_closes_4403_for_a_valid_scoped_key(ws_client, client):
    # 4403, not 4401: the credential is real, this socket is simply outside
    # every scope (framework routes are, structurally, in v1).
    key = _mint(client)
    assert _ws_close_code(ws_client, "/ws/logs/docker:aw-browser",
                          {"X-Api-Key": key["token"]}) == 4403


def test_framework_ws_closes_4401_for_an_invalid_scoped_key(ws_client):
    assert _ws_close_code(ws_client, "/ws/logs/docker:aw-browser",
                          {"X-Api-Key": "awsk_" + "d" * 64}) == 4401


def test_framework_ws_closes_4401_with_no_credential_at_all(ws_client):
    assert _ws_close_code(ws_client, "/ws/logs/docker:aw-browser") == 4401
