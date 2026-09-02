"""CRUD for scoped guest users — backs Settings > General > Users.

Ported from ``aw-backend/src/api/routes/guest_users.py`` (itself a port of the
``agentic-workspace`` monolith's). It lives here, in the per-workspace core,
because that is the origin the tab actually calls: ``aw-workspace-ui``'s
``apiBase.js`` rewrites every relative ``/api/*`` fetch on a workspace SPA host
to ``api.<slug>.workspace.<apex>`` — this process — never to ``api.<apex>``
(aw-backend). The control plane having the feature was therefore invisible to
the tab managing it, which is the "Failed to load users (HTTP 404)" bug this
module fixes.

Two things deliberately differ from the reference implementation:

**Auth is per-route, not ambient.** In aw-backend these endpoints were bare —
a blanket ``AuthMiddleware`` gated every ``/api/*`` path before routing, so the
routes themselves needed no dependency. This app has no such middleware; each
route declares its own ``Depends(require_identity)`` (same as
``skills_routes.py``, ``folders.py``, …). Porting the reference verbatim would
have published an *unauthenticated* endpoint that mints credentials.

**Storage is this workspace's schema.** ``GuestUser`` is declared schema-less in
``src.api.models`` and routed by ``db.get_engine()``'s ``schema_translate_map``,
so guests are per-workspace with no separate database, and
``db.create_all_tables()`` creates the table on the next boot — no migration.

## What is NOT here: the guest login

Only the admin-side CRUD is implemented. There is no ``/guest-login``, no
password verification path and no guest token, so **creating a guest here does
not yet let anyone in**. That half was left out on purpose rather than ported
blind, for two independent reasons:

1. *This process cannot mint the workspace's identity token.* ``src.api.identity``
   is verify-only — it checks ``aw_id_jwt`` offline against aw-backend's Ed25519
   **public** key. The private key lives in the control plane, so an
   ``aw_id_jwt`` for a guest can only be issued by aw-backend.
2. *Reproducing the old ``aw_guest_jwt`` would produce a token nothing checks.*
   Its enforcement half was the monolith's Caddy ``forward_auth`` calling
   ``app_auth_gate.py``; neither exists in this runtime. Minting a second,
   locally-signed trust root inside the data plane to be validated by nobody is
   worse than not having one.

So the login flow is a control-plane design decision (extend central identity
with app-scoped guest principals), not a port. Tracked on the card this module
was built from.
"""
from __future__ import annotations

import time

import bcrypt
from fastapi import Body, Depends, FastAPI, HTTPException
from sqlmodel import select

from src.api.db import get_session
from src.api.identity import require_identity
from src.api.models import GuestUser


def hash_password(plain: str) -> str:
    """bcrypt hash, matching the reference implementation's format so existing
    rows stay verifiable by whatever login design eventually lands."""
    return bcrypt.hashpw(plain.encode(), bcrypt.gensalt()).decode()


def _to_dict(user: GuestUser) -> dict:
    """Public shape — never includes ``password_hash``."""
    return {
        "id": user.id,
        "username": user.username,
        "allowed_apps": user.allowed_apps or [],
        "created_at": user.created_at,
    }


def register_guest_user_routes(app: FastAPI) -> None:
    # These handlers are deliberately plain `def`, not `async def`: they do
    # nothing but synchronous DB work through src.api.db.get_session (sync
    # psycopg) plus bcrypt hashing, and this process runs ONE uvicorn worker
    # (AW_WORKSPACE_WORKERS=1), so an `async def` body would run that blocking
    # work directly on the single event-loop thread and freeze every other
    # in-flight request for its duration. FastAPI runs a sync handler in its
    # own threadpool instead, which is exactly what's wanted here.
    @app.get("/api/guest-users")
    def list_guest_users(identity: dict = Depends(require_identity)):
        with get_session() as session:
            rows = session.exec(select(GuestUser).order_by(GuestUser.username)).all()
            return [_to_dict(u) for u in rows]

    @app.post("/api/guest-users")
    def create_guest_user(
        payload: dict = Body(...), identity: dict = Depends(require_identity)
    ):
        username = (payload.get("username") or "").strip()
        password = payload.get("password") or ""
        allowed_apps = [a for a in (payload.get("allowed_apps") or []) if a]

        if not username or not password:
            raise HTTPException(status_code=400, detail="username and password are required")

        with get_session() as session:
            existing = session.exec(
                select(GuestUser).where(GuestUser.username == username)
            ).first()
            if existing:
                raise HTTPException(status_code=409, detail=f"user {username!r} already exists")

            user = GuestUser(
                username=username,
                password_hash=hash_password(password),
                allowed_apps=allowed_apps,
                created_at=time.time(),
            )
            session.add(user)
            session.commit()
            session.refresh(user)
            return _to_dict(user)

    @app.put("/api/guest-users/{user_id}")
    def update_guest_user(
        user_id: int, payload: dict = Body(...), identity: dict = Depends(require_identity)
    ):
        with get_session() as session:
            user = session.get(GuestUser, user_id)
            if not user:
                raise HTTPException(status_code=404, detail="user not found")

            if "allowed_apps" in payload:
                user.allowed_apps = [a for a in (payload.get("allowed_apps") or []) if a]
            if payload.get("password"):
                user.password_hash = hash_password(payload["password"])

            session.add(user)
            session.commit()
            session.refresh(user)
            return _to_dict(user)

    @app.delete("/api/guest-users/{user_id}")
    def delete_guest_user(user_id: int, identity: dict = Depends(require_identity)):
        with get_session() as session:
            user = session.get(GuestUser, user_id)
            if not user:
                raise HTTPException(status_code=404, detail="user not found")
            session.delete(user)
            session.commit()
            return {"ok": True}
