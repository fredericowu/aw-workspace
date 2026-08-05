"""Workspace API key: KV-table storage + .env persistence + verification.

Real-Postgres only (schema isolation is a real-Postgres concept) — skips
cleanly if 127.0.0.1:5432 isn't reachable, same pattern as
``test_notification_db`` / ``test_isolation``.
"""
from __future__ import annotations

import os

import psycopg
import pytest
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

_SCHEMA = "workspace_apikeytest"


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


def test_get_or_create_generates_once_and_persists(env):
    from src.api.workspace_api_key import get_or_create_workspace_api_key

    key1 = get_or_create_workspace_api_key()
    key2 = get_or_create_workspace_api_key()
    assert key1 == key2
    assert len(key1) == 64  # secrets.token_hex(32)


def test_get_or_create_writes_env_file(env):
    from src.api.workspace_api_key import ENV_VAR_NAME, get_or_create_workspace_api_key, _env_path

    key = get_or_create_workspace_api_key()
    with open(_env_path()) as f:
        content = f.read()
    assert f"{ENV_VAR_NAME}={key}" in content


def test_env_write_preserves_other_lines(env):
    from src.api.workspace_api_key import _env_path, get_or_create_workspace_api_key

    os.makedirs(os.path.dirname(_env_path()), exist_ok=True)
    with open(_env_path(), "w") as f:
        f.write("SOME_OTHER_VAR=keep-me\n")

    get_or_create_workspace_api_key()

    with open(_env_path()) as f:
        content = f.read()
    assert "SOME_OTHER_VAR=keep-me" in content


def test_regenerate_replaces_key_and_invalidates_old_one(env):
    from src.api.workspace_api_key import (
        get_or_create_workspace_api_key,
        regenerate_workspace_api_key,
        verify_workspace_api_key,
    )

    old_key = get_or_create_workspace_api_key()
    new_key = regenerate_workspace_api_key()

    assert new_key != old_key
    assert verify_workspace_api_key(new_key) is True
    assert verify_workspace_api_key(old_key) is False
    assert get_or_create_workspace_api_key() == new_key  # no re-mint


def test_regenerate_rewrites_env_file(env):
    from src.api.workspace_api_key import ENV_VAR_NAME, _env_path, get_or_create_workspace_api_key, regenerate_workspace_api_key

    get_or_create_workspace_api_key()
    new_key = regenerate_workspace_api_key()

    with open(_env_path()) as f:
        lines = f.read().splitlines()
    matches = [l for l in lines if l.startswith(f"{ENV_VAR_NAME}=")]
    assert matches == [f"{ENV_VAR_NAME}={new_key}"]  # exactly one, the new one


def test_verify_rejects_empty_or_missing_key(env):
    from src.api.workspace_api_key import verify_workspace_api_key

    assert verify_workspace_api_key("") is False
    assert verify_workspace_api_key("anything") is False  # none generated yet


def test_verify_rejects_wrong_key(env):
    from src.api.workspace_api_key import get_or_create_workspace_api_key, verify_workspace_api_key

    get_or_create_workspace_api_key()
    assert verify_workspace_api_key("not-the-real-key") is False


def test_get_or_create_publishes_to_process_env(env, monkeypatch):
    # An in-process Tier-1 app (e.g. aw-app-whiteboard) reads plain
    # os.environ, not the .env file — get_or_create must set it directly,
    # not just write it to disk for OTHER processes to pick up.
    from src.api.workspace_api_key import ENV_VAR_NAME, get_or_create_workspace_api_key

    monkeypatch.delenv(ENV_VAR_NAME, raising=False)
    key = get_or_create_workspace_api_key()
    assert os.environ[ENV_VAR_NAME] == key


def test_get_or_create_republishes_even_when_key_already_existed(env, monkeypatch):
    # A worker process that didn't mint the key (another worker did) must
    # still see it in ITS OWN os.environ once it calls get_or_create.
    from src.api.workspace_api_key import ENV_VAR_NAME, get_or_create_workspace_api_key

    key = get_or_create_workspace_api_key()
    monkeypatch.delenv(ENV_VAR_NAME, raising=False)
    assert get_or_create_workspace_api_key() == key
    assert os.environ[ENV_VAR_NAME] == key


def test_regenerate_publishes_to_process_env(env):
    from src.api.workspace_api_key import ENV_VAR_NAME, get_or_create_workspace_api_key, regenerate_workspace_api_key

    get_or_create_workspace_api_key()
    new_key = regenerate_workspace_api_key()
    assert os.environ[ENV_VAR_NAME] == new_key
