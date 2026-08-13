"""Marketplace catalog fetch + cache (ADR Decision 5 — the SPA's Marketplace).

Each source is a repo (or any URL) holding an ``apps.json`` (id/name/
description/icon/tags/repo/ref/has_config/…), read via a **raw GET**
(``raw.githubusercontent.com``, no rate-limited API). The workspace merges
**N sources** and serves the result to the SPA at ``GET /api/apps/-/catalog``.

## Where sources come from

The user-managed registry in Postgres (``src/api/marketplace.py``, driven by
Settings → Marketplace) is the source of truth, and is what makes **private**
marketplaces work: each row carries its own credential reference, bound to
the host it may be sent to. ``AW_MARKETPLACE_SOURCES`` is still read and
appended after it, so an existing env-configured deployment is unchanged and
the built-in public marketplace stays present in a workspace where the user
has only added their own.

## Env-var config — ``AW_MARKETPLACE_SOURCES``

A comma-separated list of sources. Each entry is either:

- ``owner/repo`` or ``owner/repo@ref`` — a GitHub repo; ``ref`` defaults to
  ``master`` if omitted. Resolved to
  ``https://raw.githubusercontent.com/<owner>/<repo>/<ref>/<AW_MARKETPLACE_CATALOG_PATH>``
  (``AW_MARKETPLACE_CATALOG_PATH`` defaults to ``apps.json``).
- a full ``http(s)://`` URL — used as-is (the raw URL to the ``apps.json``
  file itself), for a source that doesn't follow the ``owner/repo`` shape.

Default: a single source, ``tekflox/aw-marketplace@master`` (built from the
legacy ``AW_MARKETPLACE_REPO``/``AW_MARKETPLACE_REF`` envs so an unset
``AW_MARKETPLACE_SOURCES`` keeps working as before).

An env-var source is public: it has no registry row, so no per-source
credential. It falls back to the legacy global ``AW_APP_GIT_TOKEN``, which
is sent **only to GitHub's own hosts** (``_GITHUB_HOSTS``). A global token
must never be attached to an arbitrary user-supplied URL — that would hand
the PAT to whatever host was configured.

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
import re
import time
from typing import Any
from urllib.parse import urlsplit

import httpx

from src.apps.manifest import ManifestError, validate_manifest
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


def db_sources() -> list[tuple[str, str]]:
    """User-configured sources from Postgres as ``(source_id, url)`` pairs.

    Empty (never raising) when the registry is unreachable — during early
    boot, or in a unit test with no DB — so the env-var path below stays a
    working fallback rather than a hard dependency.
    """
    try:
        from src.api.marketplace import list_sources

        return [(row.id, row.url) for row in list_sources(enabled_only=True)]
    except Exception as e:  # noqa: BLE001 — no DB yet is a normal cold-start state
        log.debug("apps: marketplace source registry unavailable (%s)", e)
        return []


def marketplace_sources() -> list[tuple[str, str]]:
    """Sources to merge, in precedence order, as ``(source_id, url)``.

    The Postgres registry (Settings → Marketplace) is the source of truth.
    ``AW_MARKETPLACE_SOURCES`` — and the legacy single-repo envs behind it —
    remain as a **seed** appended after it: an existing deployment keeps
    working untouched, and the built-in public marketplace still shows up in
    a workspace where the user has added only their own private one. Entries
    are deduped by resolved URL, registry first.
    """
    sources: list[tuple[str, str]] = list(db_sources())

    raw = os.environ.get("AW_MARKETPLACE_SOURCES")
    env_specs = (
        [s.strip() for s in raw.split(",") if s.strip()]
        if raw and raw.strip()
        else [f"{MARKETPLACE_REPO}@{MARKETPLACE_REF}"]
    )
    # Env-derived sources have no registry row, so they get no credential —
    # `_git_token()` is their only auth, exactly as before.
    sources.extend(("", spec) for spec in env_specs)

    seen: set[str] = set()
    out: list[tuple[str, str]] = []
    for source_id, spec in sources:
        url = _raw_url_for(spec)
        if url in seen:
            continue
        seen.add(url)
        out.append((source_id, spec))
    return out


def _raw_url_for(source: str) -> str:
    """Resolve a source spec to the raw URL of its ``apps.json``."""
    if source.startswith("http://") or source.startswith("https://"):
        return source
    owner_repo, _, ref = source.partition("@")
    ref = ref or "master"
    return f"https://raw.githubusercontent.com/{owner_repo}/{ref}/{CATALOG_PATH}"


def _api_url_for(source: str) -> str | None:
    """The GitHub **API** contents URL for a source's ``apps.json``, or
    ``None`` when the spec isn't an ``owner/repo[@ref]`` GitHub reference
    (a full-URL source points at an arbitrary host that has no such API).

    Preferred over ``_raw_url_for`` purely for freshness:
    ``raw.githubusercontent.com`` serves ``max-age=300`` from a CDN that
    strips the query string from its cache key — a ``?cb=<random>``
    cache-buster measurably returns ``x-cache: HIT`` — so a just-released
    version can stay invisible for ~5 minutes with no way to hurry it. The
    API serves the same bytes at ``s-maxage=60``.
    """
    if source.startswith("http://") or source.startswith("https://"):
        return None
    owner_repo, _, ref = source.partition("@")
    owner, sep, repo = owner_repo.partition("/")
    if not (owner and sep and repo) or "/" in repo:
        return None
    return (f"https://api.github.com/repos/{owner_repo}/contents/{CATALOG_PATH}"
            f"?ref={ref or 'master'}")


# Returns the file's bytes as-is instead of the base64 JSON envelope, so the
# API and raw responses parse through the exact same path.
GITHUB_RAW_MEDIA_TYPE = "application/vnd.github.raw"


def _is_github_host(host: str) -> bool:
    """Whether the legacy *global* ``AW_APP_GIT_TOKEN`` may go to ``host``.

    Shares one allowlist with the per-source binding
    (``src.api.marketplace.GITHUB_HOSTS``) so the two can't drift — the
    import is local because that module pulls in FastAPI and the DB, which
    this one deliberately doesn't need.
    """
    from src.api.marketplace import GITHUB_HOSTS

    return host.lower() in GITHUB_HOSTS


def _auth_headers(url: str, source_id: str = "") -> dict[str, str]:
    """Auth header for ``url``, from this source's own bound credential.

    A registry source uses its own token, attached only when ``url``'s host
    matches the credential's ``auth_host`` (``marketplace.credential_for``).
    Sources with no registry row fall back to the legacy global
    ``AW_APP_GIT_TOKEN``, which — being global — is only ever sent to
    github.com, never to a user-supplied third-party host.
    """
    if source_id:
        try:
            from src.api.marketplace import credential_for

            header = credential_for(source_id, url)
            if header:
                return {header[0]: header[1]}
            return {}
        except Exception as e:  # noqa: BLE001
            log.debug("apps: credential lookup failed for source %s (%s)", source_id, e)
            return {}

    token = _git_token()
    if token and _is_github_host(urlsplit(url).hostname or ""):
        return {"Authorization": f"Bearer {token}"}
    return {}


def fetch_source(source: str, source_id: str = "", timeout: float = 15.0) -> list[dict[str, Any]]:
    """GET one source's ``apps.json`` and return its ``apps`` list.

    Tries the GitHub API first (``s-maxage=60``) and falls back to the raw
    URL (``max-age=300``) on any failure, so this can only be fresher than
    it was, never less available: the API is rate limited (60 requests/hour
    unauthenticated, 5000 with a token) and covers only ``owner/repo``
    specs, which makes raw the necessary backstop. Deliberately ONE API call
    per source per refresh — per-app manifests stay on raw
    (``_fetch_app_manifest``), where 25-plus fetches per refresh would burn
    an unauthenticated hourly budget in two runs and leave the catalog worse
    off than the CDN lag it set out to fix.

    Raises on transport/HTTP/parse failure — the caller decides whether to
    skip this source or fall back to a cache. Also used directly by the
    "test this source" route so a bad token reports itself: a credential
    that can't read a private repo fails BOTH fetches, so the raw error
    still surfaces.
    """
    api_url = _api_url_for(source)
    if api_url is not None:
        try:
            return _parse_catalog(
                _get_catalog_text(api_url, source_id, timeout,
                                  accept=GITHUB_RAW_MEDIA_TYPE),
                source,
            )
        except Exception as e:  # noqa: BLE001 — raw is the backstop for every API failure
            log.warning("apps: catalog API fetch failed for %s (%s) — falling back to raw",
                        source, e)

    url = _raw_url_for(source)
    return _parse_catalog(_get_catalog_text(url, source_id, timeout), source)


def _get_catalog_text(url: str, source_id: str, timeout: float,
                      accept: str | None = None) -> str:
    """GET ``url`` with this source's own credential; return the body text."""
    headers = _auth_headers(url, source_id)
    if accept:
        headers = {**headers, "Accept": accept}
    resp = httpx.get(url, headers=headers, timeout=timeout)
    resp.raise_for_status()
    return resp.text


def _parse_catalog(text: str, source: str) -> list[dict[str, Any]]:
    data = json.loads(text)
    if not isinstance(data, dict) or not isinstance(data.get("apps"), list):
        raise ValueError(f"catalog apps.json missing an 'apps' array (source={source})")
    return data["apps"]


# Back-compat alias — the private name predates the route that needs it.
_fetch_source = fetch_source


def _manifest_raw_url(repo: str, ref: str) -> str:
    return f"https://raw.githubusercontent.com/{repo}/{ref or 'master'}/aw-app.json"


def _fetch_app_manifest(repo: str, ref: str, timeout: float = 10.0,
                        source_id: str = "") -> dict[str, Any] | None:
    """Best-effort raw GET of an app's own ``aw-app.json``.

    ``source_id`` is the marketplace the entry came from: a private app repo
    listed by a private marketplace is read with that marketplace's own
    credential, so enrichment doesn't silently degrade to "no manifest" for
    exactly the apps the feature exists to serve.
    """
    url = _manifest_raw_url(repo, ref)
    try:
        resp = httpx.get(url, headers=_auth_headers(url, source_id), timeout=timeout)
        resp.raise_for_status()
        return json.loads(resp.text)
    except Exception as e:  # noqa: BLE001 — one bad manifest shouldn't break the catalog
        log.warning("apps: catalog manifest fetch failed for %s@%s: %s", repo, ref, e)
        return None


def _enrich_with_manifest(app: dict[str, Any]) -> dict[str, Any]:
    """Add ``publisher`` / ``resource_estimate`` / ``what_you_get`` /
    ``dependencies`` to a catalog entry, derived from the app's own
    ``aw-app.json`` (best-effort — the entry is returned unchanged if the
    manifest can't be fetched or fails validation). ``dependencies`` is what
    ``marketplace update-all`` (aw-workspace-cli) reads to order its updates —
    without it the CLI would only see id/version/repo, not the app-dependency
    graph the reconciler already enforces at install time."""
    repo = app.get("repo")
    if not repo:
        return app
    data = _fetch_app_manifest(repo, app.get("ref") or "master",
                               source_id=app.get("_source_id") or "")
    if data is None:
        return app
    try:
        manifest = validate_manifest(data)
    except ManifestError as e:
        log.warning("apps: catalog manifest invalid for %s: %s", repo, e)
        return app
    return {
        **app,
        "publisher": app.get("publisher") or manifest.publisher,
        "resource_estimate": app.get("resource_estimate") or manifest.resource_estimate,
        "what_you_get": manifest.what_you_get,
        "dependencies": manifest.dependencies,
    }


def _merge_sources(
    sources: list[tuple[str, str]],
) -> tuple[list[dict[str, Any]], list[str], list[str]]:
    """Fetch + merge every ``(source_id, spec)``.

    Returns (merged_apps, ok_specs, failed_specs). Each merged app carries
    ``_source`` (the spec, for display) and ``_source_id`` (the registry row,
    so a later fetch of that app can reuse the right credential).
    """
    merged: list[dict[str, Any]] = []
    seen: set[str] = set()
    ok: list[str] = []
    failed: list[str] = []
    for source_id, spec in sources:
        try:
            apps = fetch_source(spec, source_id=source_id)
        except Exception as e:  # noqa: BLE001 — one bad source shouldn't break the rest
            log.warning("apps: catalog source %s fetch failed: %s", spec, e)
            failed.append(spec)
            continue
        ok.append(spec)
        for app in apps:
            app_id = app.get("id") or app.get("slug")
            if not app_id or app_id in seen:
                continue
            seen.add(app_id)
            merged.append(_enrich_with_manifest({**app, "_source": spec,
                                                 "_source_id": source_id}))
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

    specs = [spec for _, spec in sources]
    log.warning("apps: all catalog sources failed: %s", specs)
    if _cache["data"] is not None:
        return {**_cache["data"], "stale": True}

    disk = _read_disk_cache()
    if disk is not None:
        return {**disk, "stale": True}

    return {"apps": [], "error": "all catalog sources failed", "sources": specs}


def is_marketplace_app(app_id: str, repo: str | None, force: bool = False) -> bool:
    """True if ``app_id`` is published in a configured catalog under ``repo``.

    The workspace-side twin of aw-backend's
    ``src/api/marketplace_catalog.py::is_marketplace_app``, and the local
    answer to the ``signed`` trust flag (ADR Decision 4): catalog membership
    is a real, if lightweight, signal, because every entry lands there through
    a reviewed PR merge plus the release pipeline's ``validate-apps-json``.
    It is NOT full F8 content-hash pinning — it proves the id+repo pair is one
    the catalog publishes, not that the installed bytes match what it did.

    **Both must match.** An id collision with a different repo (a side-loaded
    app reusing a marketplace app's id) is not signed — that check is the
    whole point, so never relax it to an id-only lookup.

    Only the OFFICIAL source counts, not the merged catalog, because the cloud
    twin reads that one repo alone. Answering more generously here would make
    an app from a user-added private source signed locally and unsigned in the
    cloud — and `Reconciler.reconcile` treats a trust difference as drift and
    reinstalls, so the two would fight on every pass. Whether a private
    marketplace can confer trust is a decision for the cloud registry to make
    once, for both sides; it is not something to fork locally.
    """
    if not app_id:
        return False
    wanted = (repo or "").strip().lower()
    official = f"{MARKETPLACE_REPO}@{MARKETPLACE_REF}"
    for app in get_catalog(force=force).get("apps", []) or []:
        if (app.get("id") or app.get("slug")) != app_id:
            continue
        if app.get("_source") != official:
            continue
        if (app.get("repo") or "").strip().lower() == wanted:
            return True
    return False


def clear_cache() -> None:
    _cache["data"] = None
    _cache["at"] = 0.0


def auth_headers_for_repo(repo: str, url: str) -> dict[str, str]:
    """Auth header for downloading ``repo``'s code from ``url``.

    Resolves *which marketplace listed this app* from the merged catalog and
    reuses that source's credential. Without this, a private app published by
    a private marketplace would list fine in the panel and then fail to
    install — the catalog fetch is authenticated but the tarball fetch isn't.

    Falls back to the legacy global token when the app isn't in the catalog
    (a direct install by repo, the reconciler replaying an old row).
    """
    try:
        entry = next(
            (a for a in (_cache["data"] or {}).get("apps", [])
             if (a.get("repo") or "").lower() == (repo or "").lower()),
            None,
        )
        if entry is None:
            entry = next(
                (a for a in get_catalog().get("apps", [])
                 if (a.get("repo") or "").lower() == (repo or "").lower()),
                None,
            )
        if entry and entry.get("_source_id"):
            return _auth_headers(url, entry["_source_id"])
    except Exception as e:  # noqa: BLE001 — never block an install on catalog lookup
        log.debug("apps: credential lookup for repo %s failed (%s)", repo, e)
    return _auth_headers(url, "")


_TAG_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)(?:[-+][0-9A-Za-z.-]+)?$")


def list_tags(repo: str, timeout: float = 10.0) -> list[dict[str, str]]:
    """Semver-shaped git tags for a public GitHub repo, newest first.

    Backs the pin/rollback version picker (an installed app's "Version"
    window) — best-effort, returns ``[]`` on any failure rather than raising,
    since a version list is a nice-to-have, not required for install/update
    to keep working.
    """
    url = f"https://api.github.com/repos/{repo}/tags"
    headers = {"Accept": "application/vnd.github+json"}
    token = _git_token()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        resp = httpx.get(url, headers=headers, timeout=timeout)
        resp.raise_for_status()
        tags = resp.json()
    except Exception as e:  # noqa: BLE001 — best-effort, see docstring
        log.warning("apps: tag list fetch failed for %s: %s", repo, e)
        return []

    parsed: list[tuple[tuple[int, int, int], dict[str, str]]] = []
    for t in tags if isinstance(tags, list) else []:
        name = t.get("name") or ""
        m = _TAG_RE.match(name)
        if not m:
            continue
        key = tuple(int(g) for g in m.groups())
        parsed.append((key, {"ref": name, "version": ".".join(m.groups())}))
    parsed.sort(key=lambda p: p[0], reverse=True)
    return [entry for _, entry in parsed]
