"""HTTP client for the ``aw-workspace-cli`` CLI talking to *this same
workspace's* own FastAPI server (the one started by ``src.start.workspace``).

Address: co-located with the server, the CLI reaches it over loopback
(``127.0.0.1:9030``). But when the CLI is invoked BY AN AGENT inside a spawned
agent-runner container (a different network namespace — shares the workspace
filesystem, not the server's loopback), loopback is dead; the server published
its external tunnel URL to ``<home>/.env`` as ``AW_WORKSPACE_API_URL`` (see
``src/api/workspace_url.py``) exactly for this, so we prefer it when present.

Auth: the browser SPA authenticates with the central-identity ``aw_id_jwt``
(see ``src/api/identity.py``), which the CLI has no way to hold. Instead the
CLI presents the workspace-wide API key (``AW_WORKSPACE_API_KEY``) as the
``X-Api-Key`` header — the server mints it at boot and mirrors it to
``<workspace_home>/.env`` (0600) exactly so sibling processes with no DB
access, like this CLI, can read it. ``require_identity`` accepts that header.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any

import httpx

from src.api.workspace_api_key import ENV_VAR_NAME as API_KEY_ENV_VAR, HEADER_NAME
from src.api.workspace_url import ENV_VAR_NAME as API_URL_ENV_VAR
from src.apps.paths import workspace_home_path

logger = logging.getLogger(__name__)


def _read_env_value(name: str) -> str | None:
    """Return ``name``'s value from ``<home>/.env``, or ``None`` if the file
    or the key is absent. Logs and re-raises on an unexpected read error
    (e.g. a permissions problem) — the single ``.env`` reader for this CLI."""
    env_file = os.path.join(workspace_home_path(), ".env")
    prefix = f"{name}="
    try:
        with open(env_file, "r", encoding="utf-8") as f:
            for line in f:
                if line.startswith(prefix):
                    return line[len(prefix):].strip()
    except FileNotFoundError:
        return None
    except OSError as exc:
        logger.error("could not read %s from %s: %s", name, env_file, exc)
        raise
    return None


def base_url() -> str:
    """Where to reach the server. Explicit override first, then the
    server-published external URL (the runner-container path), then the
    co-located loopback default."""
    override = os.environ.get("AW_LOCAL_API_URL")
    if override:
        return override
    external = os.environ.get(API_URL_ENV_VAR) or _read_env_value(API_URL_ENV_VAR)
    if external:
        return external
    return f"http://127.0.0.1:{os.environ.get('AW_PORT', '9030')}"


def _workspace_api_key() -> str:
    """The workspace API key, read from ``<home>/.env`` (the server writes it
    there at boot). Raises with a clear message — and logs — if it's missing."""
    key = _read_env_value(API_KEY_ENV_VAR)
    if not key:
        env_file = os.path.join(workspace_home_path(), ".env")
        logger.error("%s not found in %s", API_KEY_ENV_VAR, env_file)
        raise RuntimeError(
            f"{API_KEY_ENV_VAR} not found in {env_file} — is the workspace server running?"
        )
    return key


def request(method: str, path: str, json_body: dict | None = None,
            timeout: float = 30.0) -> tuple[int, Any]:
    """Return ``(status_code, parsed_body_or_text)``. Never raises for HTTP
    errors — connection failures are the only thing that propagate."""
    headers = {HEADER_NAME: _workspace_api_key()}
    try:
        resp = httpx.request(
            method, base_url() + path, json=json_body, headers=headers, timeout=timeout,
        )
    except httpx.HTTPError as exc:
        logger.error("%s %s failed to reach the workspace server: %s", method, path, exc)
        raise
    try:
        body = resp.json()
    except (json.JSONDecodeError, ValueError):
        body = resp.text
    return resp.status_code, body
