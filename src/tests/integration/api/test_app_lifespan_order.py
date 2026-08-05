"""The workspace API key must be published to os.environ BEFORE apps load
on boot — an app's own activate() (e.g. aw-app-whiteboard's
mcp/self_register.py) reads AW_WORKSPACE_API_KEY to put in its
self-registered mcp.json; publishing the key after apps already loaded
would silently ship that entry with no auth header.

Real-Postgres only — skips cleanly if 127.0.0.1:5432 isn't reachable, same
pattern as test_notification_db / test_isolation.
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

_SCHEMA = "workspace_lifespanordertest"


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("AW_WORKSPACE_SCHEMA", _SCHEMA)
    monkeypatch.setenv("AW_WORKSPACE_DB_URL",
                        "postgresql://postgres:postgres@127.0.0.1:5432/awserv")
    monkeypatch.setenv("AW_WORKSPACE_HOME", str(tmp_path / "home"))
    import src.api.db as dbmod
    monkeypatch.setattr(dbmod, "_engine", None)

    yield

    from src.api.db import get_engine
    with get_engine().begin() as conn:
        conn.execute(text(f'DROP SCHEMA IF EXISTS "{_SCHEMA}" CASCADE'))
    monkeypatch.setattr(dbmod, "_engine", None)


def test_workspace_api_key_is_published_before_apps_reconcile(env, monkeypatch):
    import src.api.app as app_mod

    call_order = []

    async def fake_reconcile(app):
        import os
        call_order.append(("reconcile_on_boot", os.environ.get("AW_WORKSPACE_API_KEY")))

    def fake_get_or_create():
        call_order.append(("get_or_create_workspace_api_key", None))
        import os
        os.environ["AW_WORKSPACE_API_KEY"] = "the-real-key"
        return "the-real-key"

    monkeypatch.setattr(app_mod, "reconcile_on_boot", fake_reconcile)
    monkeypatch.setattr(app_mod, "get_or_create_workspace_api_key", fake_get_or_create)

    with TestClient(app_mod.create_app()):
        pass

    names = [c[0] for c in call_order]
    assert names.index("get_or_create_workspace_api_key") < names.index("reconcile_on_boot")
    # By the time reconcile_on_boot (which loads/activates apps) runs, the
    # key must already be visible in os.environ.
    assert call_order[names.index("reconcile_on_boot")][1] == "the-real-key"
