"""Starlette matches routes in REGISTRATION order, not by specificity — a
catch-all ``/api/settings/{key}`` registered before a literal route like
``/api/settings/workspace-api-key`` shadows it forever, silently returning
the catch-all's own (wrong) shape instead of ever reaching the dedicated
handler. Found 2026-08-29 while building the Observability settings item:
``/api/settings/workspace-api-key`` had been returning
``{"key": "workspace-api-key", "value": null}`` (looked up under the wrong
storage key) instead of the real key since the route was added, because the
generic get/put pair was registered earlier in ``create_app()``. Fixed by
registering the generic pair last; this pins the fix against every
dedicated settings item, not just the one that surfaced it.

Real-Postgres only — skips cleanly if 127.0.0.1:5432 isn't reachable, same
pattern as ``test_app_lifespan_order.py``.
"""
from __future__ import annotations

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

_SCHEMA = "workspace_settingsroutetest"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("AW_WORKSPACE_SCHEMA", _SCHEMA)
    monkeypatch.setenv("AW_WORKSPACE_DB_URL",
                        "postgresql://postgres:postgres@127.0.0.1:5432/awserv")
    monkeypatch.setenv("AW_WORKSPACE_HOME", str(tmp_path / "home"))
    import src.api.db as dbmod
    monkeypatch.setattr(dbmod, "_engine", None)

    import src.api.app as app_mod
    from src.apps.routes import reconcile_on_boot as real_reconcile

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
    return {"X-Api-Key": __import__("os").environ.get("AW_WORKSPACE_API_KEY", "")}


def test_workspace_api_key_route_is_not_shadowed_by_the_generic_catchall(client):
    key_res = client.get("/api/settings/workspace-api-key", headers=_auth_headers())
    assert key_res.status_code == 200
    body = key_res.json()
    # The generic catch-all's shape is {"key": "workspace-api-key", "value": ...}
    # (it echoes the URL key back). The dedicated route's shape has no
    # "value" field at all — just the real key.
    assert "value" not in body
    assert len(body["key"]) == 64  # a real hex token, not the literal "workspace-api-key"


def test_observability_route_is_not_shadowed_by_the_generic_catchall(client):
    res = client.get("/api/settings/observability", headers=_auth_headers())
    assert res.status_code == 200
    body = res.json()
    assert "value" not in body
    assert body["mode"] == "auto"
