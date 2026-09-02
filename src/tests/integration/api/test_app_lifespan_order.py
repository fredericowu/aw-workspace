"""The workspace API key must be published to os.environ BEFORE apps load
on boot — an app's own activate() (e.g. aw-app-whiteboard's
mcp/self_register.py) reads AW_WORKSPACE_API_KEY to put in its
self-registered mcp.json; publishing the key after apps already loaded
would silently ship that entry with no auth header.

Also covers bug 3cf5bf3b-9510-8149-be2d-db20915f6872: reconcile_on_boot (and
the skills sync that follows it) must run as a BACKGROUND task after
``yield``, not inline before it — inline, a slow/unreachable app's GitHub
fetch blacked out the whole public edge (incl. /api/health) for as long as
reconciler.reconcile() took, proven at 4m14s on a real redeploy.

Real-Postgres only — skips cleanly if 127.0.0.1:5432 isn't reachable, same
pattern as test_notification_db / test_isolation.
"""
from __future__ import annotations

import threading
import time

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


def _wait_for_boot_reconcile(app, timeout=5.0):
    """reconcile_on_boot now runs as a background task (app.state.boot_reconcile_task,
    see src/api/app.py's lifespan) instead of being awaited inline — poll for
    it to finish instead of assuming it already has by the time a test's
    ``with TestClient(...)`` body runs."""
    deadline = time.monotonic() + timeout
    task = getattr(app.state, "boot_reconcile_task", None)
    while task is None or not task.done():
        if time.monotonic() > deadline:
            raise AssertionError("boot_reconcile_task did not complete in time")
        time.sleep(0.02)
        task = getattr(app.state, "boot_reconcile_task", None)


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

    app = app_mod.create_app()
    with TestClient(app):
        _wait_for_boot_reconcile(app)

    names = [c[0] for c in call_order]
    assert names.index("get_or_create_workspace_api_key") < names.index("reconcile_on_boot")
    # By the time reconcile_on_boot (which loads/activates apps) runs, the
    # key must already be visible in os.environ.
    assert call_order[names.index("reconcile_on_boot")][1] == "the-real-key"


def test_health_is_served_immediately_even_while_boot_reconcile_hangs(env, monkeypatch):
    """The actual bug: reconcile_on_boot used to run BEFORE `yield`, so a
    slow/unreachable app's GitHub fetch blocked every request — including
    /api/health — for as long as it took. Prove the opposite now: entering
    the app (lifespan startup) and hitting /api/health both complete fast
    even while reconcile_on_boot is still hung in the background."""
    import src.api.app as app_mod

    hang = threading.Event()

    async def hanging_reconcile(app):
        import asyncio
        await asyncio.to_thread(hang.wait)

    monkeypatch.setattr(app_mod, "reconcile_on_boot", hanging_reconcile)

    app = app_mod.create_app()
    try:
        started = time.monotonic()
        with TestClient(app) as client:
            startup_elapsed = time.monotonic() - started
            res = client.get("/api/health")
            assert res.status_code == 200
            # Generous bound — this used to be gated on reconciler.reconcile(),
            # which alone could run for minutes; a healthy boot is near-instant.
            assert startup_elapsed < 5.0
            # The hang is still in flight — startup did NOT wait for it.
            assert not app.state.boot_reconcile_task.done()
    finally:
        # Let the background task finish so it doesn't leak past the test.
        hang.set()
