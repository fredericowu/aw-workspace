"""Workspace-wide API key — lets other apps/MCPs authenticate into this
workspace with a single shared secret instead of a browser-issued identity
JWT (``aw_id_jwt``). First consumer: an external whiteboard MCP process
calling this workspace's HTTP API with an ``X-Api-Key`` header.

Storage: the generic ``settings`` KV table (``src.api.models.Setting``,
schema-isolated per workspace like everything else in ``src.api.db``) — NOT
a file, so it survives a full container recreation as long as Postgres does.
Auto-generated on first read (``get_or_create``) so a freshly-installed
workspace always has one without any manual step.

Mirrors ``src.apps.paths.get_or_create_cli_token``'s "same machine" secret,
but this one is meant to be handed to *other processes* (a separate MCP
server, another app) rather than kept workspace-local — which is exactly
why every mint/rotate ALSO writes it to ``<AW_WORKSPACE_HOME>/.env`` (see
``_write_env``): a process outside this FastAPI app (no DB access of its
own) can source that file and read ``AW_WORKSPACE_API_KEY`` directly.
"""
from __future__ import annotations

import os
import secrets

from src.api.db import get_session
from src.api.models import Setting
from src.apps.paths import workspace_home

SETTING_KEY = "workspace_api_key"
ENV_VAR_NAME = "AW_WORKSPACE_API_KEY"
HEADER_NAME = "X-Api-Key"


def _env_path() -> str:
    return os.path.join(workspace_home(), ".env")


def _write_env(key: str) -> None:
    """Upsert ``AW_WORKSPACE_API_KEY=<key>`` into ``<home>/.env``, preserving
    every other line already there (other apps may share this same file)."""
    path = _env_path()
    prefix = f"{ENV_VAR_NAME}="
    lines: list[str] = []
    found = False
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                if line.startswith(prefix):
                    lines.append(f"{prefix}{key}\n")
                    found = True
                else:
                    lines.append(line)
    except FileNotFoundError:
        pass
    if not found:
        lines.append(f"{prefix}{key}\n")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.writelines(lines)


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
    """Return the current key, generating (and persisting to .env) one on
    first use — called at app boot so every installed workspace has a key
    with no manual step, and again lazily if the Settings UI is opened
    before boot ever ran (belt and suspenders)."""
    with get_session() as session:
        existing = _read(session)
        if existing:
            return existing
        key = secrets.token_hex(32)
        _store(session, key)
    _write_env(key)
    return key


def regenerate_workspace_api_key() -> str:
    """Mint a brand new key, persist it, and rewrite .env — invalidates the
    previous key immediately (any caller still presenting it gets 401)."""
    key = secrets.token_hex(32)
    with get_session() as session:
        _store(session, key)
    _write_env(key)
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
