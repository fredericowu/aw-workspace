"""``GET /api/health``'s HTTP contract (design §C).

The payload shape is unit-tested in ``src/tests/unit/apps/test_health_degraded.py``;
what can only be pinned here is the part that is about HTTP rather than about
the check: **a degraded workspace still answers 200**. A 5xx from this
endpoint makes any container-orchestrator healthcheck restart core — which is
the exact restart that killed every Tier-1 managed service on 2026-10-11 and
started the incident this whole mechanism exists to end. Callers that
genuinely want gate semantics ask for them with ``?strict=1``.

Real-Postgres only — skips cleanly if 127.0.0.1:5432 isn't reachable, same
pattern as ``test_settings_route_order.py``. CI provides one.
"""
from __future__ import annotations

import psycopg
import pytest
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

_SCHEMA = "workspace_healthdegradedtest"

_DEGRADED = {
    "status": "degraded",
    "degraded": [{"kind": "service", "app": "proxy", "service": "proxy-server",
                  "state": "off", "autostart": True}],
}


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("AW_WORKSPACE_SCHEMA", _SCHEMA)
    monkeypatch.setenv("AW_WORKSPACE_DB_URL",
                       "postgresql://postgres:postgres@127.0.0.1:5432/awserv")
    monkeypatch.setenv("AW_WORKSPACE_HOME", str(tmp_path / "home"))
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


def _stub(monkeypatch, report):
    import src.api.app as app_mod
    monkeypatch.setattr(app_mod, "health_report_cached", lambda runtime, **kw: report)


def test_a_healthy_workspace_still_answers_the_old_shape(client):
    res = client.get("/api/health")
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "ok"
    assert body["degraded"] == []
    # The pre-existing fields every caller already reads are untouched.
    for key in ("workspace", "version", "boot_id", "git_head", "started_at"):
        assert key in body


def test_a_degraded_workspace_answers_200_not_5xx(client, monkeypatch):
    """The whole point. An orchestrator healthcheck must read "serving" here,
    because restarting core is what caused the outage being reported."""
    _stub(monkeypatch, _DEGRADED)
    res = client.get("/api/health")
    assert res.status_code == 200
    assert res.json()["status"] == "degraded"
    assert res.json()["degraded"][0]["app"] == "proxy"


def test_strict_opts_into_503(client, monkeypatch):
    _stub(monkeypatch, _DEGRADED)
    res = client.get("/api/health?strict=1")
    assert res.status_code == 503
    # Same payload, different status code — a gate, not a different endpoint.
    assert res.json()["status"] == "degraded"


def test_strict_on_a_healthy_workspace_is_still_200(client):
    assert client.get("/api/health?strict=1").status_code == 200


def test_the_local_worker_scope_is_surfaced(client, monkeypatch):
    """Redis away means this worker cannot see other workers' leases or
    give-up markers. Health still answers; it just says how far it can see."""
    _stub(monkeypatch, {**_DEGRADED, "health_scope": "local-worker"})
    body = client.get("/api/health").json()
    assert body["health_scope"] == "local-worker"
