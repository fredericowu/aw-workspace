"""``POST /api/workspace-api-key/sync-env`` — the CLI/agent-runner recovery
endpoint for a missing/stale ``AW_WORKSPACE_API_KEY`` in ``.env``. Per the
Architect's design (card 3e95bf3b-9510-81f0-84f0-d01299c99f23): unauthenticated,
never echoes the key, just re-runs the server's own boot-time publish
(``get_or_create_workspace_api_key()``) so a sibling process can re-read
``.env`` from disk. The trust boundary stays filesystem read access to the
0600 ``.env`` — no secret may ever cross this endpoint's response body.

Real-Postgres only — skips cleanly if 127.0.0.1:5432 isn't reachable, same
pattern as test_workspace_api_key.py / test_app_lifespan_order.py.
"""
from __future__ import annotations

import os

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

_SCHEMA = "workspace_syncenvtest"


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


def test_sync_env_requires_no_auth(env):
    import src.api.app as app_mod

    app = app_mod.create_app()
    with TestClient(app) as client:
        res = client.post("/api/workspace-api-key/sync-env")
        assert res.status_code == 200


def test_sync_env_never_returns_the_key(env):
    import src.api.app as app_mod

    app = app_mod.create_app()
    with TestClient(app) as client:
        res = client.post("/api/workspace-api-key/sync-env")
        assert res.json() == {"ok": True}


def test_sync_env_restores_missing_env_line(env):
    import src.api.app as app_mod
    from src.api.workspace_api_key import ENV_VAR_NAME, _env_path, get_or_create_workspace_api_key

    app = app_mod.create_app()
    with TestClient(app):
        original_key = get_or_create_workspace_api_key()

        # Simulate the exact incident: .env on disk lost the line (stale copy,
        # manual edit, restored-from-backup drift) while Postgres still has it.
        env_path = _env_path()
        with open(env_path) as f:
            lines = [l for l in f.read().splitlines() if not l.startswith(f"{ENV_VAR_NAME}=")]
        with open(env_path, "w") as f:
            f.write("\n".join(lines) + "\n")

        with open(env_path) as f:
            assert ENV_VAR_NAME not in f.read()

    # A fresh app/TestClient stands in for the sibling CLI process hitting
    # the already-running server over HTTP.
    app2 = app_mod.create_app()
    with TestClient(app2) as client:
        res = client.post("/api/workspace-api-key/sync-env")
        assert res.status_code == 200

        with open(env_path) as f:
            content = f.read()
        assert f"{ENV_VAR_NAME}={original_key}" in content


def test_sync_env_does_not_rotate_an_existing_key(env):
    """Regression guard for the Architect's firmly-rejected alternative:
    sync-env must be read-only with respect to the key's value — it must
    NEVER behave like regenerate, or every other consumer (MCP gateway,
    self-registered app mcp.json files) would 401 on the next call."""
    import src.api.app as app_mod
    from src.api.workspace_api_key import get_or_create_workspace_api_key

    app = app_mod.create_app()
    with TestClient(app) as client:
        original_key = get_or_create_workspace_api_key()

        for _ in range(3):
            res = client.post("/api/workspace-api-key/sync-env")
            assert res.status_code == 200

        assert get_or_create_workspace_api_key() == original_key
        assert os.environ["AW_WORKSPACE_API_KEY"] == original_key
