"""aw-workspace FastAPI application factory — F2 skeleton.

Full runtime (apps/terminal/presentation/gateway/redis) is F5 — this only
proves the two F2 primitives wire together: a schema-isolated engine
(``src.api.db``) and offline EdDSA identity verification
(``src.api.identity``).
"""
from __future__ import annotations

import logging
import os
import re

from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Header, Request
from fastapi.middleware.cors import CORSMiddleware

from src.api.db import create_all_tables, get_session, get_workspace_schema
from src.api.identity import _extract_token, decode_identity_jwt, require_identity
from src.api.models import Setting
from src.api.terminal import register_terminal_routes
from src.apps.routes import reconcile_on_boot, register_apps_routes

log = logging.getLogger(__name__)


def _spa_origin_regex() -> str:
    """CORS allow-origin regex for the cloud SPA that talks to this workspace.

    Three-plane split: the SPA is served at ``https://<slug>.workspace.<domain>``
    and calls this API cross-origin (same apex) at ``api.<slug>.workspace.<domain>``,
    sending the apex ``aw_id_jwt`` cookie. Restrict to this workspace's own slug
    when known; fall back to any ``<slug>.workspace`` host in dev."""
    slug = os.environ.get("AW_WORKSPACE", "")
    if slug:
        return rf"^https://{re.escape(slug)}\.workspace\..+$"
    return r"^https://[^.]+\.workspace\..+$"


def create_app() -> FastAPI:
    create_all_tables()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # Converge the running app set to the cloud registry on startup — a
        # fresh/recreated workspace auto-reinstalls the user's apps (F3).
        await reconcile_on_boot(app)
        yield

    app = FastAPI(title="aw-workspace", version="0.1.0", lifespan=lifespan)

    # SPA→API is cross-origin (same apex) and credentialed — allow the SPA
    # origin with credentials so the apex aw_id_jwt cookie is accepted and
    # preflight (OPTIONS) succeeds. allow_credentials forbids a "*" origin,
    # hence the per-workspace regex.
    app.add_middleware(
        CORSMiddleware,
        allow_origin_regex=_spa_origin_regex(),
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/api/health")
    async def health():
        return {"status": "ok", "workspace": os.environ.get("AW_WORKSPACE", "")}

    @app.get("/api/auth/status")
    async def auth_status(request: Request, authorization: str = Header(default="")):
        """Login-gate check for the cloud SPA. Validates the apex aw_id_jwt
        cookie offline against the cloud JWKS (never 401s — the SPA reads the
        boolean and renders its login/dashboard accordingly). Minimal stub for
        Milestone 1; membership/role enforcement is a later card."""
        token = _extract_token(request, authorization)
        claims = decode_identity_jwt(token) if token else None
        return {"authenticated": bool(claims), "mode": "identity"}

    @app.get("/api/settings/{key}")
    async def get_setting(key: str, identity: dict = Depends(require_identity)):
        with get_session() as session:
            row = session.get(Setting, key)
            return {"key": key, "value": row.value if row else None}

    @app.put("/api/settings/{key}")
    async def put_setting(key: str, value: dict, identity: dict = Depends(require_identity)):
        with get_session() as session:
            row = session.get(Setting, key)
            if row is None:
                row = Setting(key=key, value=value)
            else:
                row.value = value
            session.add(row)
            session.commit()
        return {"key": key, "value": value, "schema": get_workspace_schema()}

    # Terminal feature (strangler migration #1): PTY shells on this BYOD host.
    # In-memory session state → must run single-worker (see AW_WORKSPACE_WORKERS
    # in the Dockerfile/compose and MIGRATION.md).
    register_terminal_routes(app)

    # Decoupled-apps framework (F1): plugin runtime + /api/apps management.
    # Tier-1 apps hot-load into THIS process — no restart. Installed apps are
    # reloaded from the local registry on startup.
    register_apps_routes(app)

    return app
