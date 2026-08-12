"""User-managed marketplace sources — the private-marketplace surface.

Until now the set of marketplaces the catalog merged came from one env var
(``AW_MARKETPLACE_SOURCES``, see ``src/apps/catalog.py``) and every source
was assumed public. Two things that couldn't do:

* **A user can't change it.** Editing the env var means recreating the
  container — an ops action, not something the owner of a workspace does
  from Settings.
* **Private marketplaces need credentials**, and there was only ever one
  global token (``AW_APP_GIT_TOKEN``) shared by every source.

This module owns the registry (one Postgres row per source — see
``models.MarketplaceSource``) and the REST surface Settings → Marketplace
drives. ``src/apps/catalog.py`` reads it and does the fetching.

Credentials
-----------

The token never touches the ``marketplace_sources`` table. It's written to
the workspace secret store (Fernet-encrypted at rest) under the reserved
namespace ``_marketplace``, keyed by the source id, and is **never** echoed
back by any route — the API reports ``has_credential: true/false`` and
nothing more. A source row carries only ``auth_type`` and ``auth_host``.

``auth_host`` exists because of a concrete attack. With one global token
attached to every configured source, a user who adds
``https://evil.example/apps.json`` as a marketplace has just sent their
GitHub PAT to that host in an ``Authorization`` header. Binding the
credential to a host and refusing to attach it to a request for any other
host is the fix (``credential_for()`` below), and it keeps holding across
redirects because the check is re-run per request URL rather than once at
config time.

Reserved namespace note: ``_marketplace`` is not a valid app slug (app ids
are validated against a slug pattern and can't start with ``_``), so this
can never collide with a real app's secret namespace.
"""
from __future__ import annotations

import logging
import re
import time
from typing import Any
from urllib.parse import urlsplit

from fastapi import Body, Depends, FastAPI, HTTPException
from sqlmodel import select

from src.api.db import get_session
from src.api.identity import require_identity
from src.api.models import MarketplaceSource
from src.apps.secret_store import SecretStore

log = logging.getLogger(__name__)

# Reserved secret-store namespace for marketplace credentials (see docstring).
SECRETS_NS = "_marketplace"

ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
OWNER_REPO_RE = re.compile(r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+(@[^\s]+)?$")

AUTH_TYPES = ("none", "github_pat", "bearer")

_secrets = SecretStore()


class SourceError(ValueError):
    """Invalid marketplace-source input — surfaced as a 400 by the routes."""


# --- helpers -----------------------------------------------------------------


def host_of(url: str) -> str:
    """Host of a source spec. ``owner/repo`` implies github.com."""
    if url.startswith("http://") or url.startswith("https://"):
        return (urlsplit(url).hostname or "").lower()
    return "github.com"


def host_allowed(auth_host: str, request_host: str) -> bool:
    """May a credential bound to ``auth_host`` be sent to ``request_host``?

    Exact match, with one deliberate widening: GitHub splits a single logical
    credential across several hostnames — the catalog comes from
    ``raw.githubusercontent.com``, the tarball from ``api.github.com`` then
    ``codeload.github.com`` — so a token bound to ``github.com`` is usable
    across that whole family. Without this a private GitHub marketplace
    would authenticate nowhere at all.

    The widening is an explicit allowlist, never a suffix test:
    ``github.com.evil.example`` is not a GitHub host, and a credential bound
    to a non-GitHub host stays pinned to exactly that host.
    """
    auth_host = (auth_host or "").lower()
    request_host = (request_host or "").lower()
    if not auth_host or not request_host:
        return False
    if auth_host == request_host:
        return True
    return auth_host in GITHUB_HOSTS and request_host in GITHUB_HOSTS


# GitHub serves the catalog, the manifest and the tarball from different
# hostnames, none of which is a suffix of the others ("raw.githubusercontent.com"
# does NOT end in "github.com"). Membership is the only correct test.
GITHUB_HOSTS = frozenset({
    "github.com",
    "api.github.com",
    "raw.githubusercontent.com",
    "codeload.github.com",
    "objects.githubusercontent.com",
})


def validate(body: dict[str, Any], *, source_id: str | None = None) -> dict[str, Any]:
    """Validate + canonicalise a create/update payload. Raises ``SourceError``."""
    url = str(body.get("url") or "").strip()
    if not url:
        raise SourceError("url is required")
    if not (url.startswith("http://") or url.startswith("https://")):
        if not OWNER_REPO_RE.match(url):
            raise SourceError(
                f"url must be 'owner/repo', 'owner/repo@ref', or a full http(s) "
                f"URL to the catalog JSON (got {url!r})"
            )

    sid = str(source_id or body.get("id") or "").strip().lower()
    if not sid:
        # Derive a stable id from the spec: "tekflox/aw-marketplace@master"
        # -> "tekflox-aw-marketplace".
        base = url.split("@")[0]
        if base.startswith("http"):
            base = (urlsplit(base).hostname or "source") + urlsplit(base).path
        sid = re.sub(r"[^a-z0-9]+", "-", base.lower()).strip("-") or "source"
    if not ID_RE.match(sid):
        raise SourceError(
            f"invalid id {sid!r} — lowercase letters, digits, '.', '_' or '-' "
            f"(must start with a letter or digit)"
        )

    auth_type = str(body.get("auth_type") or "none").strip()
    if auth_type not in AUTH_TYPES:
        raise SourceError(f"auth_type must be one of {', '.join(AUTH_TYPES)} (got {auth_type!r})")

    # The credential is bound to the source's own host unless the caller
    # names one explicitly (a self-hosted catalog whose tarballs live
    # elsewhere). Never left blank when auth is on — a blank host would
    # disable the binding check and re-open the exfiltration hole.
    auth_host = str(body.get("auth_host") or "").strip().lower()
    if auth_type == "none":
        auth_host = ""
    elif not auth_host:
        auth_host = host_of(url)

    priority = body.get("priority", 100)
    try:
        priority = int(priority)
    except (TypeError, ValueError) as exc:
        raise SourceError(f"priority must be an integer (got {priority!r})") from exc

    return {
        "id": sid,
        "name": str(body.get("name") or "").strip() or sid,
        "url": url,
        "enabled": bool(body.get("enabled", True)),
        "priority": priority,
        "auth_type": auth_type,
        "auth_host": auth_host,
    }


def describe(row: MarketplaceSource) -> dict[str, Any]:
    """Registry row → the API shape. Never includes the credential itself."""
    return {
        "id": row.id,
        "name": row.name,
        "url": row.url,
        "enabled": row.enabled,
        "priority": row.priority,
        "auth_type": row.auth_type,
        "auth_host": row.auth_host,
        "has_credential": _secrets.get(SECRETS_NS, row.id) is not None,
        "created_at": row.created_at,
    }


# --- registry ----------------------------------------------------------------


def list_sources(enabled_only: bool = False) -> list[MarketplaceSource]:
    """Every configured source in merge order (priority asc, then id)."""
    with get_session() as session:
        rows = list(session.exec(select(MarketplaceSource)).all())
    if enabled_only:
        rows = [r for r in rows if r.enabled]
    return sorted(rows, key=lambda r: (r.priority, r.id))


def get_source(source_id: str) -> MarketplaceSource | None:
    with get_session() as session:
        return session.get(MarketplaceSource, source_id)


def upsert_source(entry: dict[str, Any], credential: str | None = None) -> MarketplaceSource:
    """Create or update a source. ``credential`` is written to the secret store.

    Passing ``credential=None`` on an update **keeps** any existing stored
    credential — the UI can't re-send a secret it was never allowed to read,
    so "no new token supplied" must not silently wipe the old one. Clearing
    is explicit, via ``delete_credential``.
    """
    with get_session() as session:
        row = session.get(MarketplaceSource, entry["id"])
        if row is None:
            row = MarketplaceSource(**entry, created_at=time.time())
        else:
            for key, value in entry.items():
                setattr(row, key, value)
        session.add(row)
        session.commit()
        session.refresh(row)

    if credential:
        _secrets.put(SECRETS_NS, row.id, credential)
    if row.auth_type == "none":
        # Auth turned off — don't leave an orphaned token behind.
        _secrets.delete(SECRETS_NS, row.id)

    log.info("marketplace: saved source %s (%s, auth=%s)", row.id, row.url, row.auth_type)
    return row


def delete_source(source_id: str) -> bool:
    with get_session() as session:
        row = session.get(MarketplaceSource, source_id)
        if row is None:
            return False
        session.delete(row)
        session.commit()
    _secrets.delete(SECRETS_NS, source_id)
    log.info("marketplace: removed source %s", source_id)
    return True


def delete_credential(source_id: str) -> bool:
    return _secrets.delete(SECRETS_NS, source_id)


def credential_for(source_id: str, url: str) -> tuple[str, str] | None:
    """The auth header to attach when fetching ``url`` for ``source_id``.

    Returns ``(header_name, header_value)`` or ``None``. Returns ``None``
    whenever ``url``'s host differs from the source's ``auth_host`` — the
    host binding described in the module docstring. The check is evaluated
    against the URL actually being requested rather than once at config
    time, so every distinct host a source causes us to contact (its catalog,
    an app's manifest, that app's tarball) is checked on its own.
    """
    row = get_source(source_id)
    if row is None or row.auth_type == "none":
        return None
    if not host_allowed(row.auth_host, host_of(url)):
        log.warning(
            "marketplace: withholding %s credential from %s — not allowed by binding to %s",
            row.id, host_of(url), row.auth_host,
        )
        return None
    token = _secrets.get(SECRETS_NS, row.id)
    if not token:
        return None
    # A GitHub PAT is sent as `token <pat>`, which raw.githubusercontent.com
    # and api.github.com both accept; a generic catalog gets plain Bearer.
    if row.auth_type == "github_pat":
        return ("Authorization", f"token {token}")
    return ("Authorization", f"Bearer {token}")


def registry_credential(registry_host: str) -> tuple[str, str] | None:
    """Username/password for pulling an app image from ``registry_host``.

    A private app published by a private marketplace also has a **private
    container image**, and ``containers.py`` pulls with no auth at all — so
    without this the app lists, installs, and then fails to start.

    Rather than invent a second credential store, this reuses the
    marketplace's own token: a GitHub PAT that can read a private repo can
    also read that org's GHCR packages (``read:packages``), so the source
    that published the app is exactly the right thing to authenticate its
    image. Only GHCR is mapped this way; any other registry returns ``None``
    until it has a credential type of its own.

    Returns ``(username, token)`` — GHCR ignores the username, but the
    docker SDK requires one.
    """
    if registry_host.lower() not in ("ghcr.io", "docker.pkg.github.com"):
        return None
    for row in list_sources(enabled_only=True):
        if row.auth_type != "github_pat":
            continue
        token = _secrets.get(SECRETS_NS, row.id)
        if token:
            return ("x-access-token", token)
    return None


# --- routes ------------------------------------------------------------------


def register_marketplace_routes(app: FastAPI) -> None:
    """Mount ``/api/marketplace/sources*`` — identity-gated like every route."""

    @app.get("/api/marketplace/sources")
    async def get_sources(identity: dict = Depends(require_identity)):
        return {"sources": [describe(r) for r in list_sources()]}

    @app.post("/api/marketplace/sources")
    async def post_source(body: dict = Body(...),
                          identity: dict = Depends(require_identity)):
        try:
            entry = validate(body)
        except SourceError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        row = upsert_source(entry, credential=body.get("credential") or None)
        _invalidate_catalog()
        return {"source": describe(row)}

    @app.put("/api/marketplace/sources/{source_id}")
    async def put_source(source_id: str, body: dict = Body(...),
                         identity: dict = Depends(require_identity)):
        if get_source(source_id) is None:
            raise HTTPException(status_code=404, detail=f"no marketplace source {source_id!r}")
        try:
            entry = validate(body, source_id=source_id)
        except SourceError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        row = upsert_source(entry, credential=body.get("credential") or None)
        _invalidate_catalog()
        return {"source": describe(row)}

    @app.delete("/api/marketplace/sources/{source_id}")
    async def del_source(source_id: str, identity: dict = Depends(require_identity)):
        if not delete_source(source_id):
            raise HTTPException(status_code=404, detail=f"no marketplace source {source_id!r}")
        _invalidate_catalog()
        return {"removed": source_id}

    @app.delete("/api/marketplace/sources/{source_id}/credential")
    async def del_credential(source_id: str, identity: dict = Depends(require_identity)):
        if get_source(source_id) is None:
            raise HTTPException(status_code=404, detail=f"no marketplace source {source_id!r}")
        delete_credential(source_id)
        _invalidate_catalog()
        return {"cleared": source_id}

    @app.post("/api/marketplace/sources/{source_id}/test")
    async def test_source(source_id: str, identity: dict = Depends(require_identity)):
        """Fetch this one source now and report what came back.

        The point of a private marketplace is that failures are invisible
        until someone opens the Marketplace panel and sees nothing — a
        wrong/expired token looks identical to an empty catalog. This gives
        the user a direct answer at the moment they paste the token.
        """
        row = get_source(source_id)
        if row is None:
            raise HTTPException(status_code=404, detail=f"no marketplace source {source_id!r}")
        from src.apps import catalog

        try:
            apps = catalog.fetch_source(row.url, source_id=row.id)
        except Exception as exc:  # noqa: BLE001 — the whole point is reporting it
            return {"ok": False, "error": str(exc), "app_count": 0}
        return {"ok": True, "app_count": len(apps), "app_ids": [a.get("id") for a in apps][:50]}


def _invalidate_catalog() -> None:
    """Drop the catalog cache so a source change shows up immediately."""
    from src.apps import catalog

    catalog.clear_cache()
