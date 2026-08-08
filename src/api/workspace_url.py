"""This workspace's own external API base URL — composed at boot and mirrored
to ``<home>/.env`` (next to ``AW_WORKSPACE_API_KEY``) so a sibling process
with no loopback route to the server can still reach it.

Why this exists: the ``aw-workspace-cli`` normally talks to the server over
loopback (``127.0.0.1:9030``) because it's co-located with it. But the same
CLI, when invoked BY AN AGENT inside a spawned agent-runner container, runs
in a different network namespace — it shares the workspace filesystem (so it
can read ``.env``) but NOT the server's loopback. Its only inbound path is the
public tunnel edge ``https://api.<slug>.workspace.<domain>``. Publishing that
URL here lets ``src/cli/local_client.py`` pick it up with zero guessing.

Host shape (mirrors ``src.api.app._spa_origin_regex``): the SPA is served at
``<slug>.workspace.<domain>`` and the API at ``api.<slug>.workspace.<domain>``.
Only the slug is truly per-workspace; the rest is a base domain that rarely
changes and is resolved from config so a future domain change needs no code
edit.
"""
from __future__ import annotations

import logging
import os
from urllib.parse import urlparse

from src.apps.paths import upsert_workspace_env

logger = logging.getLogger(__name__)

ENV_VAR_NAME = "AW_WORKSPACE_API_URL"
SLUG_ENV_VAR = "AW_WORKSPACE"
DOMAIN_ENV_VAR = "AW_WORKSPACE_BASE_DOMAIN"
STATIC_BASE_DOMAIN = "workspace.aw.tekflox.com"


def base_domain() -> str:
    """The host suffix after ``<slug>.`` — e.g. ``workspace.aw.tekflox.com``.

    Resolved in layers so the domain can move without touching this code:

    1. ``AW_WORKSPACE_BASE_DOMAIN`` — explicit override, wins outright.
    2. Derived from ``AW_BACKEND_URL``'s apex (``api.aw.tekflox.com`` →
       ``workspace.aw.tekflox.com``) — reuses config already set at deploy,
       so a staging/other-domain deploy tracks automatically.
    3. Static default ``workspace.aw.tekflox.com``.
    """
    explicit = os.environ.get(DOMAIN_ENV_VAR, "").strip()
    if explicit:
        return explicit

    backend = os.environ.get("AW_BACKEND_URL", "").strip()
    if backend:
        try:
            host = urlparse(backend).hostname or ""
            apex = host[len("api."):] if host.startswith("api.") else host
            if apex:
                return f"workspace.{apex}"
        except Exception as exc:  # a malformed URL must never break boot
            logger.warning("could not derive base domain from %s=%r: %s",
                           "AW_BACKEND_URL", backend, exc)

    return STATIC_BASE_DOMAIN


def workspace_api_base_url() -> str | None:
    """``https://api.<slug>.<base_domain>``, or ``None`` when the slug is
    unknown (``AW_WORKSPACE`` unset) — then we publish nothing and the CLI
    keeps its loopback default."""
    slug = os.environ.get(SLUG_ENV_VAR, "").strip()
    if not slug:
        logger.warning("%s (slug) unset — cannot compose the external API URL",
                       SLUG_ENV_VAR)
        return None
    return f"https://api.{slug}.{base_domain()}"


def publish_workspace_api_url() -> str | None:
    """Compose the external API URL and mirror it into ``os.environ`` +
    ``<home>/.env`` (alongside ``AW_WORKSPACE_API_KEY``). Called at boot.
    Returns the URL, or ``None`` if it couldn't be composed."""
    url = workspace_api_base_url()
    if not url:
        return None
    os.environ[ENV_VAR_NAME] = url
    upsert_workspace_env(ENV_VAR_NAME, url)
    logger.info("published workspace external API URL: %s", url)
    return url
