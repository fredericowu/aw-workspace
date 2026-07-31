"""HTTP client for the ``./aw`` CLI talking to *this same workspace's* own
FastAPI server (the one started by ``src.start.workspace``) over loopback.

Auth: the browser SPA authenticates with the central-identity ``aw_id_jwt``
(see ``src/api/identity.py``), which the CLI has no way to hold. Instead the
CLI proves it's running on the same machine/filesystem as the server via a
random per-workspace secret (``<workspace_home>/cli-token``, 0600) sent as
the ``X-AW-Local-Cli-Token`` header — ``require_identity`` accepts either.
"""
from __future__ import annotations

import json
import os
from typing import Any

import httpx

from src.apps.paths import get_or_create_cli_token, LOCAL_CLI_HEADER


def base_url() -> str:
    return os.environ.get(
        "AW_LOCAL_API_URL", f"http://127.0.0.1:{os.environ.get('AW_PORT', '9030')}"
    )


def request(method: str, path: str, json_body: dict | None = None,
            timeout: float = 30.0) -> tuple[int, Any]:
    """Return ``(status_code, parsed_body_or_text)``. Never raises for HTTP
    errors — connection failures are the only thing that propagate."""
    headers = {LOCAL_CLI_HEADER: get_or_create_cli_token()}
    resp = httpx.request(
        method, base_url() + path, json=json_body, headers=headers, timeout=timeout,
    )
    try:
        body = resp.json()
    except (json.JSONDecodeError, ValueError):
        body = resp.text
    return resp.status_code, body
