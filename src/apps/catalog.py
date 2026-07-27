"""Marketplace catalog fetch + cache (ADR Decision 5 — the SPA's Marketplace).

Marketplaces are **public** repos — no git token needed. Each source is a
public repo holding an ``apps.json`` (id/name/description/icon/tags/repo/
ref/has_config/…), read via a **raw GET** (``raw.githubusercontent.com``,
no auth, no rate-limited API). The workspace merges **N sources** and serves
the result to the SPA at ``GET /api/apps/-/catalog``.

## Multi-source config — ``AW_MARKETPLACE_SOURCES``

A comma-separated list of sources, dynamic: adding a marketplace is adding
one entry to this env var, no code change. Each entry is either:

- ``owner/repo`` or ``owner/repo@ref`` — a GitHub repo; ``ref`` defaults to
  ``master`` if omitted. Resolved to
  ``https://raw.githubusercontent.com/<owner>/<repo>/<ref>/<AW_MARKETPLACE_CATALOG_PATH>``
  (``AW_MARKETPLACE_CATALOG_PATH`` defaults to ``apps.json``).
- a full ``http(s)://`` URL — used as-is (the raw URL to the ``apps.json``
  file itself), for a source that doesn't follow the ``owner/repo`` shape.

Default: a single source, ``tekflox/aw-marketplace@master`` (built from the
legacy ``AW_MARKETPLACE_REPO``/``AW_MARKETPLACE_REF`` envs so an unset
``AW_MARKETPLACE_SOURCES`` keeps working as before).

Each source is public by default (no token). ``AW_APP_GIT_TOKEN`` — the same
token ``fetch.py`` uses for private app-repo clones — is sent as a Bearer
header on every source fetch too, so a *future* private source works without
new config; the default (unset) stays fully tokenless.

## Merge + dedup

Apps from all sources are concatenated in source order and deduped by
``id`` — **first source wins** ties. Every app in the merged result carries
``_source`` (the source spec it came from) so the SPA/ops can tell where an
app is published from.

## Persistent cache

Beyond the in-memory TTL cache, the merged catalog is written to a file
under the host-mounted workspace home (``src.apps.paths.workspace_home()``)
so it survives a container restart. If **every** source fetch fails and
there's no usable in-memory cache, the on-disk cache is served instead
(marked ``stale``) — offline / cold-start resilient, never breaks the panel.
"""
from __future__ import annotations

import json
import logging
import os
import time
from typing import Any

import httpx

from src.apps.paths import workspace_home

log = logging.getLogger(__name__)

# Legacy single-source envs — still used to build the DEFAULT entry of
# AW_MARKETPLACE_SOURCES when that var is unset.
MARKETPLACE_REPO = os.environ.get("AW_MARKETPLACE_REPO", "tekflox/aw-marketplace")
MARKETPLACE_REF = os.environ.get("AW_MARKETPLACE_REF", "master")
CATALOG_PATH = os.environ.get("AW_MARKETPLACE_CATALOG_PATH", "apps.json")
CACHE_TTL = float(os.environ.get("AW_MARKETPLACE_CACHE_TTL", "300"))  # seconds

_cache: dict[str, Any] = {"at": 0.0, "data": None}


def _disk_cache_path() -> str:
    return os.path.join(workspace_home(), "marketplace_catalog.json")


def _git_token() -> str | None:
    return os.environ.get("AW_APP_GIT_TOKEN") or None


def marketplace_sources() -> list[str]:
    """The configured list of source specs, in precedence order."""
    raw = os.environ.get("AW_MARKETPLACE_SOURCES")
    if raw and raw.strip():
        return [s.strip() for s in raw.split(",") if s.strip()]
    return [f"{MARKETPLACE_REPO}@{MARKETPLACE_REF}"]


def _raw_url_for(source: str) -> str:
    """Resolve a source spec to the raw URL of its ``apps.json``."""
    if source.startswith("http://") or source.startswith("https://"):
        return source
    owner_repo, _, ref = source.partition("@")
    ref = ref or "master"
    return f"https://raw.githubusercontent.com/{owner_repo}/{ref}/{CATALOG_PATH}"


def _fetch_source(source: str, timeout: float = 15.0) -> list[dict[str, Any]]:
    """Raw-GET one source's ``apps.json`` and return its ``apps`` list.

    Raises on transport/HTTP/parse failure — the caller decides whether to
    skip this source or fall back to a cache.
    """
    url = _raw_url_for(source)
    headers = {}
    token = _git_token()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    resp = httpx.get(url, headers=headers, timeout=timeout)
    resp.raise_for_status()
    data = json.loads(resp.text)
    if not isinstance(data, dict) or not isinstance(data.get("apps"), list):
        raise ValueError(f"catalog apps.json missing an 'apps' array (source={source})")
    return data["apps"]


def _merge_sources(sources: list[str]) -> tuple[list[dict[str, Any]], list[str], list[str]]:
    """Fetch + merge every source. Returns (merged_apps, ok_sources, failed_sources)."""
    merged: list[dict[str, Any]] = []
    seen: set[str] = set()
    ok: list[str] = []
    failed: list[str] = []
    for source in sources:
        try:
            apps = _fetch_source(source)
        except Exception as e:  # noqa: BLE001 — one bad source shouldn't break the rest
            log.warning("apps: catalog source %s fetch failed: %s", source, e)
            failed.append(source)
            continue
        ok.append(source)
        for app in apps:
            app_id = app.get("id") or app.get("slug")
            if not app_id or app_id in seen:
                continue
            seen.add(app_id)
            merged.append({**app, "_source": source})
    return merged, ok, failed


def _write_disk_cache(result: dict[str, Any]) -> None:
    try:
        with open(_disk_cache_path(), "w") as f:
            json.dump(result, f)
    except OSError as e:  # noqa: BLE001 — disk cache is best-effort
        log.warning("apps: failed to persist catalog cache: %s", e)


def _read_disk_cache() -> dict[str, Any] | None:
    try:
        with open(_disk_cache_path()) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def get_catalog(force: bool = False) -> dict[str, Any]:
    """Return the merged marketplace catalog ``{"apps": [...]}``.

    Cached in-memory with a TTL. On a total fetch failure (every source
    down) a still-usable cache is returned instead of erroring the
    Marketplace panel — in-memory first, then the on-disk persistent cache
    (both marked ``stale``); only a cold failure (no cache anywhere)
    surfaces ``{"apps": [], "error": ...}``.
    """
    now = time.monotonic()
    if not force and _cache["data"] is not None and (now - _cache["at"]) < CACHE_TTL:
        return _cache["data"]

    sources = marketplace_sources()
    apps, ok_sources, failed_sources = _merge_sources(sources)

    if ok_sources:
        result = {
            "apps": apps,
            "manifest_version": 1,
            "sources": ok_sources,
            "failed_sources": failed_sources,
        }
        _cache["data"] = result
        _cache["at"] = now
        _write_disk_cache(result)
        return result

    log.warning("apps: all catalog sources failed: %s", sources)
    if _cache["data"] is not None:
        return {**_cache["data"], "stale": True}

    disk = _read_disk_cache()
    if disk is not None:
        return {**disk, "stale": True}

    return {"apps": [], "error": "all catalog sources failed", "sources": sources}


def clear_cache() -> None:
    _cache["data"] = None
    _cache["at"] = 0.0
