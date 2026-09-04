"""Workspace-wide API key — lets other apps/MCPs authenticate into this
workspace with a single shared secret instead of a browser-issued identity
JWT (``aw_id_jwt``). First consumer: an external whiteboard MCP process
calling this workspace's HTTP API with an ``X-Api-Key`` header.

Storage: the generic ``settings`` KV table (``src.api.models.Setting``,
schema-isolated per workspace like everything else in ``src.api.db``) — NOT
a file, so it survives a full container recreation as long as Postgres does.
Auto-generated on first read (``get_or_create``) so a freshly-installed
workspace always has one without any manual step.

This is the single shared secret for non-browser callers — a separate MCP
server, another app, or this workspace's own ``aw-workspace-cli`` — which is
exactly why every mint/rotate ALSO writes it to ``<AW_WORKSPACE_HOME>/.env``
(see ``_write_env``): a process outside this FastAPI app (no DB access of its
own) can source that file and read ``AW_WORKSPACE_API_KEY`` directly.

An in-process Tier-1 app (e.g. aw-app-whiteboard) shares this SAME Python
process, so a rewritten .env file alone would never reach it — nothing
re-execs this process or reloads its environment. ``_publish`` also sets
``os.environ[ENV_VAR_NAME]`` directly (every time the key is read/minted,
not just on first create), so an app's own plain ``os.environ.get(...)``
read (no core-module import — apps must not reach into ``src.api.*``,
only the ``ctx`` facades and, for exactly this shared-secret case, this one
environment variable) always sees the live value in the same process.
"""
from __future__ import annotations

import os
import secrets

from src.api.db import get_engine, get_session
from src.api.models import Setting
from src.apps.paths import env_file, upsert_workspace_env

SETTING_KEY = "workspace_api_key"
ENV_VAR_NAME = "AW_WORKSPACE_API_KEY"
HEADER_NAME = "X-Api-Key"


def _env_path() -> str:
    return env_file()


def _publish(key: str) -> None:
    """Make ``key`` visible both to THIS process (in-process apps reading
    ``os.environ`` directly) and to any OTHER process (external MCP/app, or
    the ``aw-workspace-cli``, reading ``.env``)."""
    os.environ[ENV_VAR_NAME] = key
    upsert_workspace_env(ENV_VAR_NAME, key)


def _read(session) -> str | None:
    row = session.get(Setting, SETTING_KEY)
    return (row.value or {}).get("key") if row else None


def _store(session, key: str) -> None:
    row = session.get(Setting, SETTING_KEY)
    if row is None:
        row = Setting(key=SETTING_KEY, value={"key": key})
    else:
        row.value = {"key": key}
    session.add(row)
    session.commit()


def get_workspace_api_key() -> str | None:
    """Current key, or ``None`` if never generated. Does not create one."""
    with get_session() as session:
        return _read(session)


def get_or_create_workspace_api_key() -> str:
    """Return the current key, generating (and publishing to os.environ +
    .env) one on first use — called at app boot so every installed
    workspace has a key with no manual step, and again lazily if the
    Settings UI is opened before boot ever ran (belt and suspenders).
    Always (re)publishes even when not freshly generated, so a worker
    process that didn't mint the key still gets it into its own env.

    W2: the insert is atomic (``INSERT ... ON CONFLICT DO NOTHING``,
    followed by a re-``SELECT``), so N workers racing this at boot never
    each mint and store a DIFFERENT key. Previously this was a plain
    read-then-write: every worker could read "absent" and each generate
    + store its own key, with the last writer winning in Postgres while
    every OTHER worker kept running with the key IT generated, only
    ever in its own ``os.environ`` — see the KB memory "restoring an
    older postgres cluster rotates the workspace API key" for the blast
    radius of a wrong key here (401s the whole MCP gateway while
    ``/api/health`` stays 200)."""
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    candidate = secrets.token_hex(32)
    stmt = (
        pg_insert(Setting.__table__)
        .values(key=SETTING_KEY, value={"key": candidate})
        .on_conflict_do_nothing(index_elements=["key"])
    )
    with get_engine().begin() as conn:
        conn.execute(stmt)
    with get_session() as session:
        winner = _read(session)
    assert winner is not None  # the insert above guarantees the row exists
    _publish(winner)
    return winner


def regenerate_workspace_api_key() -> str:
    """Mint a brand new key, persist it, and republish — invalidates the
    previous key immediately (any caller still presenting it gets 401)."""
    key = secrets.token_hex(32)
    with get_session() as session:
        _store(session, key)
    _publish(key)
    return key


def verify_workspace_api_key(presented: str) -> bool:
    """Constant-time comparison against the stored key. Empty/missing
    presented value never matches, even against an empty stored key."""
    if not presented:
        return False
    current = get_workspace_api_key()
    if not current:
        return False
    return secrets.compare_digest(presented, current)
