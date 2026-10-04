"""Scoped API keys — a second credential class next to the workspace master
key (``src.api.workspace_api_key``), for a caller that must be able to reach
exactly one set of app routes and nothing else. First consumer: an Alexa
Lambda calling aw-app-xiaomi's ``/tv/*``.

Shape (docs/design/scoped-api-keys.md):

* A **scope** is a named, reusable set of route rules
  (``{"app": slug, "paths": [...], "methods": [...]?}``), managed in
  Settings → Integrations. Paths are **mount-relative** (``/tv/power``,
  ``/tv/*``) — exact match or a trailing ``/*`` prefix wildcard, no regex.
* A **key** binds to exactly one scope, has a selectable expiry
  (``expires_at`` NULL = never) and is revocable instantly.
* The token travels in the SAME ``X-Api-Key`` header as the master key and is
  told apart by the ``awsk_`` prefix — which is what keeps aw-backend's edge
  proxy out of this change entirely (it already forwards any non-empty
  ``X-Api-Key`` and delegates validation to this workspace).

At rest: ``sha256(full_token)``, hex, unique-indexed. Deliberately NOT
bcrypt/argon2 — the token is 256 bits of ``secrets.token_hex``, so there is
no brute-force surface to defend, and a KDF would add real latency to every
single request.

**No in-process cache, on purpose.** Every resolve reads Postgres, exactly
like ``verify_workspace_api_key`` does. At ``AW_WORKSPACE_WORKERS>1`` a
per-worker cache is this house's recurring silent breakage: a key revoked
through one worker would keep working on the other N-1 until a restart.
``last_used_at`` is the only write on the read path, and it is throttled
(:data:`_LAST_USED_THROTTLE_S`) so a busy key doesn't turn every GET into an
UPDATE.

Callers are all synchronous (``src.api.db.get_session``) — the HTTP/WS
entry points wrap them in ``asyncio.to_thread``.
"""
from __future__ import annotations

import hashlib
import secrets
import time
import uuid
from typing import Any

from sqlmodel import select

from src.api.db import get_session
from src.api.models import ApiScope, ScopedApiKey

KEY_PREFIX = "awsk_"

# Don't rewrite last_used_at more than once a minute per key — it is
# bookkeeping for the Settings list, not an audit log.
_LAST_USED_THROTTLE_S = 60.0


def is_scoped_key(presented: str) -> bool:
    """True for a value that CLAIMS to be a scoped key.

    The discriminator, not a validation: a value answering True here must
    never be retried against the master key if it fails to resolve, or a
    typo'd scoped key would silently fall back to a wider credential.
    """
    return bool(presented) and presented.startswith(KEY_PREFIX)


def hash_key(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _mint_token() -> str:
    return KEY_PREFIX + secrets.token_hex(32)


def _hint(token: str) -> str:
    return token[:10]


# ---- scope matching ----------------------------------------------------


def _path_matches(pattern: str, route_path: str) -> bool:
    if pattern.endswith("/*"):
        return route_path.startswith(pattern[:-1])
    return route_path == pattern


def scope_allows(rules: Any, app_id: str, route_path: str, method: str) -> bool:
    """Does ``rules`` cover ``method route_path`` on app ``app_id``?

    ``route_path`` is mount-relative — derive it with
    ``starlette.routing.get_route_path(scope)``, never by stripping a prefix
    off ``scope["path"]`` (the per-app ``Host`` mount has no prefix to
    strip). An absent/empty ``methods`` on a rule means all methods.

    Tolerant of garbage: rules come from JSONB a human edited, and a
    malformed rule must fail closed (no match) rather than raise inside the
    request path.
    """
    if not isinstance(rules, list):
        return False
    wanted = (method or "").upper()
    for rule in rules:
        if not isinstance(rule, dict):
            continue
        if str(rule.get("app", "")) != app_id:
            continue
        methods = rule.get("methods") or []
        if isinstance(methods, list) and methods:
            if wanted not in {str(m).upper() for m in methods}:
                continue
        paths = rule.get("paths") or []
        if not isinstance(paths, list):
            continue
        if any(_path_matches(str(p), route_path) for p in paths):
            return True
    return False


def normalize_rules(rules: Any) -> list[dict[str, Any]]:
    """Validate/clean a scope's rules for storage. Raises ``ValueError`` with
    a human-readable reason — the CRUD routes turn that into a 400."""
    if not isinstance(rules, list) or not rules:
        raise ValueError("rules must be a non-empty list")
    cleaned: list[dict[str, Any]] = []
    for rule in rules:
        if not isinstance(rule, dict):
            raise ValueError("each rule must be an object")
        app = str(rule.get("app") or "").strip()
        if not app:
            raise ValueError("each rule needs an 'app' slug")
        raw_paths = rule.get("paths")
        if not isinstance(raw_paths, list) or not raw_paths:
            raise ValueError(f"rule for app {app!r} needs a non-empty 'paths' list")
        paths = [str(p).strip() for p in raw_paths if str(p).strip()]
        if not paths:
            raise ValueError(f"rule for app {app!r} needs a non-empty 'paths' list")
        for path in paths:
            if not path.startswith("/"):
                raise ValueError(f"path {path!r} must be mount-relative (start with '/')")
        entry: dict[str, Any] = {"app": app, "paths": paths}
        raw_methods = rule.get("methods")
        if raw_methods:
            if not isinstance(raw_methods, list):
                raise ValueError(f"rule for app {app!r}: 'methods' must be a list")
            entry["methods"] = [str(m).strip().upper() for m in raw_methods if str(m).strip()]
        cleaned.append(entry)
    return cleaned


# ---- resolution (the request path) -------------------------------------


def _touch_last_used(session, row: ScopedApiKey, now: float) -> None:
    if row.last_used_at is not None and now - row.last_used_at < _LAST_USED_THROTTLE_S:
        return
    row.last_used_at = now
    session.add(row)
    session.commit()


def resolve_scoped_key(presented: str) -> dict | None:
    """Claims for a valid scoped key, or ``None`` for anything invalid,
    expired or revoked (the caller 401s — a 403 is for a VALID key that the
    route is simply outside of).

    Returned claims mirror the master key's shape (``sub``/flag) plus what
    the guard needs to decide: ``scoped: True`` and the scope's ``rules``.
    """
    if not is_scoped_key(presented):
        return None
    digest = hash_key(presented)
    now = time.time()
    with get_session() as session:
        row = session.exec(
            select(ScopedApiKey).where(ScopedApiKey.key_hash == digest)
        ).first()
        if row is None or row.revoked_at is not None:
            return None
        if row.expires_at is not None and row.expires_at <= now:
            return None
        scope = session.get(ApiScope, row.scope_id)
        if scope is None:
            # A scope cannot be deleted while keys reference it (409), so
            # this is a repaired-by-hand DB, not a reachable state. Fail
            # closed rather than grant an empty ruleset.
            return None
        claims = {
            "sub": "scoped-api-key",
            "scoped": True,
            "key_id": row.id,
            "key_name": row.name,
            "scope_id": scope.id,
            "scope_name": scope.name,
            "rules": list(scope.rules or []),
        }
        _touch_last_used(session, row, now)
    return claims


# ---- CRUD (Settings → Integrations) ------------------------------------


def _scope_public(row: ApiScope) -> dict[str, Any]:
    return {"id": row.id, "name": row.name, "rules": list(row.rules or []),
            "created_at": row.created_at}


def _key_public(row: ScopedApiKey, scope_name: str) -> dict[str, Any]:
    return {
        "id": row.id, "name": row.name, "scope_id": row.scope_id,
        "scope_name": scope_name, "key_hint": row.key_hint,
        "expires_at": row.expires_at, "created_at": row.created_at,
        "revoked_at": row.revoked_at, "last_used_at": row.last_used_at,
    }


def list_scopes() -> list[dict[str, Any]]:
    with get_session() as session:
        rows = session.exec(select(ApiScope).order_by(ApiScope.name)).all()
        return [_scope_public(r) for r in rows]


def create_scope(name: str, rules: Any) -> dict[str, Any]:
    """Create a scope. Raises ``ValueError`` on a bad name/rules or a
    duplicate name — the route maps that to 400/409."""
    clean_name = str(name or "").strip()
    if not clean_name:
        raise ValueError("name is required")
    clean_rules = normalize_rules(rules)
    with get_session() as session:
        existing = session.exec(
            select(ApiScope).where(ApiScope.name == clean_name)).first()
        if existing is not None:
            raise ValueError(f"a scope named {clean_name!r} already exists")
        row = ApiScope(id=uuid.uuid4().hex, name=clean_name, rules=clean_rules,
                       created_at=time.time())
        session.add(row)
        session.commit()
        session.refresh(row)
        return _scope_public(row)


def update_scope(scope_id: str, *, name: str | None = None,
                 rules: Any = None) -> dict[str, Any] | None:
    """Patch a scope's name and/or rules. ``None`` if it doesn't exist.

    Editing rules immediately changes what every key bound to this scope can
    reach — that is the point of scopes being a named, reusable entity, and
    the reason there is no cache to invalidate.
    """
    with get_session() as session:
        row = session.get(ApiScope, scope_id)
        if row is None:
            return None
        if name is not None:
            clean_name = str(name).strip()
            if not clean_name:
                raise ValueError("name is required")
            clash = session.exec(
                select(ApiScope).where(ApiScope.name == clean_name)).first()
            if clash is not None and clash.id != scope_id:
                raise ValueError(f"a scope named {clean_name!r} already exists")
            row.name = clean_name
        if rules is not None:
            row.rules = normalize_rules(rules)
        session.add(row)
        session.commit()
        session.refresh(row)
        return _scope_public(row)


def scope_key_count(scope_id: str) -> int:
    """Keys still bound to a scope — including revoked ones, which keep the
    row (and therefore the FK) around for the audit trail."""
    with get_session() as session:
        return len(session.exec(
            select(ScopedApiKey).where(ScopedApiKey.scope_id == scope_id)).all())


def delete_scope(scope_id: str) -> str:
    """``"deleted"`` | ``"not_found"`` | ``"in_use"`` (→ 409)."""
    with get_session() as session:
        row = session.get(ApiScope, scope_id)
        if row is None:
            return "not_found"
        referencing = session.exec(
            select(ScopedApiKey).where(ScopedApiKey.scope_id == scope_id)).all()
        if referencing:
            return "in_use"
        session.delete(row)
        session.commit()
        return "deleted"


def list_keys() -> list[dict[str, Any]]:
    with get_session() as session:
        scopes = {s.id: s.name for s in session.exec(select(ApiScope)).all()}
        rows = session.exec(
            select(ScopedApiKey).order_by(ScopedApiKey.created_at)).all()
        return [_key_public(r, scopes.get(r.scope_id, "")) for r in rows]


def create_key(name: str, scope_id: str,
               expires_in_seconds: float | None) -> dict[str, Any]:
    """Mint a key bound to ``scope_id``. The returned dict carries ``token``
    — the ONLY time the full value is ever available, since only its sha256
    is stored. ``expires_in_seconds=None`` means never.

    Raises ``ValueError`` on a missing name/unknown scope/non-positive TTL.
    """
    clean_name = str(name or "").strip()
    if not clean_name:
        raise ValueError("name is required")
    if expires_in_seconds is not None:
        expires_in_seconds = float(expires_in_seconds)
        if expires_in_seconds <= 0:
            raise ValueError("expires_in_seconds must be positive, or null for never")
    now = time.time()
    token = _mint_token()
    with get_session() as session:
        scope = session.get(ApiScope, scope_id)
        if scope is None:
            raise ValueError(f"no such scope: {scope_id!r}")
        row = ScopedApiKey(
            id=uuid.uuid4().hex,
            name=clean_name,
            scope_id=scope_id,
            key_hash=hash_key(token),
            key_hint=_hint(token),
            expires_at=None if expires_in_seconds is None else now + expires_in_seconds,
            created_at=now,
        )
        session.add(row)
        session.commit()
        session.refresh(row)
        public = _key_public(row, scope.name)
    public["token"] = token
    return public


def revoke_key(key_id: str) -> dict[str, Any] | None:
    """Revoke immediately (next request 401s). ``None`` if no such key.
    Idempotent — re-revoking keeps the original timestamp."""
    with get_session() as session:
        row = session.get(ScopedApiKey, key_id)
        if row is None:
            return None
        if row.revoked_at is None:
            row.revoked_at = time.time()
            session.add(row)
            session.commit()
            session.refresh(row)
        scope = session.get(ApiScope, row.scope_id)
        return _key_public(row, scope.name if scope else "")


def delete_key(key_id: str) -> bool:
    """Drop the row entirely — the only way to free a scope for deletion."""
    with get_session() as session:
        row = session.get(ScopedApiKey, key_id)
        if row is None:
            return False
        session.delete(row)
        session.commit()
        return True
