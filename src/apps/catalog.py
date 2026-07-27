"""Marketplace catalog fetch + cache (ADR Decision 5 — the SPA's Marketplace).

The private ``tekflox/aw-marketplace`` repo holds ``apps.json`` — the curated
list of installable apps (id/name/description/icon/repo/ref/has_config/…). The
Apps view's **Marketplace** panel lists these AVAILABLE apps so the user can
Install one. The workspace fetches it (authed with the same host git token used
for private app-repo clones) and serves it to the SPA at
``GET /api/apps/-/catalog``.

Cloud (aw-backend) owns ``apps_catalog_cache`` as the eventual source of truth;
until that sync lands, the workspace reads the marketplace repo directly. Either
way the SPA sees one endpoint. Cached in-process with a short TTL so opening the
Marketplace panel repeatedly doesn't hammer the git host.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import time
from typing import Any

import httpx

log = logging.getLogger(__name__)

# `owner/repo` of the curated marketplace, and the ref to read apps.json from.
MARKETPLACE_REPO = os.environ.get("AW_MARKETPLACE_REPO", "tekflox/aw-marketplace")
MARKETPLACE_REF = os.environ.get("AW_MARKETPLACE_REF", "master")
CATALOG_PATH = os.environ.get("AW_MARKETPLACE_CATALOG_PATH", "apps.json")
CACHE_TTL = float(os.environ.get("AW_MARKETPLACE_CACHE_TTL", "300"))  # seconds

_cache: dict[str, Any] = {"at": 0.0, "data": None}


def _git_token() -> str | None:
    return os.environ.get("AW_APP_GIT_TOKEN") or None


def _fetch_apps_json(timeout: float = 15.0) -> dict[str, Any]:
    """Fetch ``apps.json`` from the (private) marketplace repo via the GitHub
    contents API. Raises on transport/HTTP/parse failure — the caller decides
    whether to fall back to a stale cache."""
    url = (
        f"https://api.github.com/repos/{MARKETPLACE_REPO}/contents/{CATALOG_PATH}"
        f"?ref={MARKETPLACE_REF}"
    )
    headers = {"Accept": "application/vnd.github.raw+json"}
    token = _git_token()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    resp = httpx.get(url, headers=headers, timeout=timeout)
    resp.raise_for_status()
    # With the raw media type GitHub returns the file bytes directly; if the
    # server ignored it (older API), fall back to the base64 JSON envelope.
    ctype = resp.headers.get("content-type", "")
    if "application/json" in ctype and resp.text.lstrip().startswith("{") and '"content"' in resp.text[:200]:
        env = resp.json()
        if isinstance(env, dict) and env.get("encoding") == "base64":
            return json.loads(base64.b64decode(env["content"]).decode("utf-8"))
    return json.loads(resp.text)


def get_catalog(force: bool = False) -> dict[str, Any]:
    """Return the marketplace catalog ``{"apps": [...]}``, cached with a TTL.

    On a fetch failure a still-usable stale cache is returned (with a
    ``stale`` flag) rather than erroring the Marketplace panel; only a cold
    failure (no cache yet) surfaces ``{"apps": [], "error": ...}``.
    """
    now = time.monotonic()
    if not force and _cache["data"] is not None and (now - _cache["at"]) < CACHE_TTL:
        return _cache["data"]
    try:
        data = _fetch_apps_json()
        if not isinstance(data, dict) or not isinstance(data.get("apps"), list):
            raise ValueError("catalog apps.json missing an 'apps' array")
        result = {
            "apps": data["apps"],
            "manifest_version": data.get("manifest_version", 1),
            "source": MARKETPLACE_REPO,
            "ref": MARKETPLACE_REF,
        }
        _cache["data"] = result
        _cache["at"] = now
        return result
    except Exception as e:  # noqa: BLE001 — degrade, never break the panel
        log.warning("apps: catalog fetch failed: %s", e)
        if _cache["data"] is not None:
            return {**_cache["data"], "stale": True}
        return {"apps": [], "error": str(e), "source": MARKETPLACE_REPO}


def clear_cache() -> None:
    _cache["data"] = None
    _cache["at"] = 0.0
