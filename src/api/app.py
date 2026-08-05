"""aw-workspace FastAPI application factory — F2 skeleton.

Full runtime (apps/terminal/presentation/gateway/redis) is F5 — this only
proves the two F2 primitives wire together: a schema-isolated engine
(``src.api.db``) and offline EdDSA identity verification
(``src.api.identity``).
"""
from __future__ import annotations

import asyncio
import logging
import os
import re

from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Header, Request
from fastapi.middleware.cors import CORSMiddleware

from src.api.db import create_all_tables, get_session, get_workspace_schema
from src.api.components import register_component_routes
from src.api.identity import _extract_token, decode_identity_jwt, require_identity
from src.api.models import Setting
from src.api.notifications import register_notification_routes
from src.api.terminal import register_terminal_routes
from src.api.workspace_api_key import (
    get_or_create_workspace_api_key,
    regenerate_workspace_api_key,
)
from src.apps.routes import reconcile_on_boot, register_apps_routes

log = logging.getLogger(__name__)


def _spa_origin_regex() -> str:
    """CORS allow-origin regex for callers that talk to this workspace.

    Three-plane split: the SPA is served at ``https://<slug>.workspace.<domain>``
    and calls this API cross-origin (same apex) at ``api.<slug>.workspace.<domain>``,
    sending the apex ``aw_id_jwt`` cookie. Restrict to this workspace's own slug
    when known; fall back to any ``<slug>.workspace`` host in dev.

    Also allows browser-extension origins (``chrome-extension://…``,
    ``moz-extension://…``, ``safari-web-extension://…``) — apps like
    aw-app-proxy's aw-sync extension are, by design, called cross-origin
    from an extension popup, not from this workspace's own SPA. CORS here
    is a browser courtesy, not the security boundary: every one of these
    routes is still behind IdentityGuard's Bearer JWT check regardless of
    what Origin sent the request, so widening this doesn't widen who can
    actually call in — only who gets a CORS grant to try (found
    2026-08-02: the aw-sync extension's POSTs were silently blocked at
    the browser's own preflight, request never left the client)."""
    slug = os.environ.get("AW_WORKSPACE", "")
    spa = rf"https://{re.escape(slug)}\.workspace\..+" if slug else r"https://[^.]+\.workspace\..+"
    extensions = r"(?:chrome|moz|safari-web)-extension://.+"
    return rf"^(?:{spa}|{extensions})$"


def create_app() -> FastAPI:
    create_all_tables()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # In-process broadcast needs the running loop to schedule sends from
        # sync callers (e.g. the apps facade) — see NotificationManager.set_loop.
        app.state.notification_mgr.set_loop(asyncio.get_running_loop())
        app.state.app_install_jobs.set_loop(asyncio.get_running_loop())
        # Converge the running app set to the cloud registry on startup — a
        # fresh/recreated workspace auto-reinstalls the user's apps (F3).
        await reconcile_on_boot(app)
        # A freshly-installed/recreated workspace always gets a workspace API
        # key with zero manual steps — get_or_create is a no-op once one
        # already exists in the settings table.
        get_or_create_workspace_api_key()
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
        return {
            "status": "ok",
            "workspace": os.environ.get("AW_WORKSPACE", ""),
            "version": os.environ.get("AW_WORKSPACE_VERSION", ""),
        }

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

    # Workspace API key (Integrations settings item): a single shared secret
    # other apps/MCPs present as an X-Api-Key header to authenticate into
    # this workspace (see src/api/workspace_api_key.py + IdentityGuard's
    # _default_verify_http). Owner-only (JWT-gated) — the key itself is
    # only ever handed out over these two identity-gated routes.
    @app.get("/api/settings/workspace-api-key")
    async def get_workspace_api_key_route(identity: dict = Depends(require_identity)):
        return {"key": get_or_create_workspace_api_key()}

    @app.post("/api/settings/workspace-api-key/regenerate")
    async def regenerate_workspace_api_key_route(identity: dict = Depends(require_identity)):
        return {"key": regenerate_workspace_api_key()}

    # Terminal feature (strangler migration #1): PTY shells on this BYOD host.
    # In-memory session state → must run single-worker (see AW_WORKSPACE_WORKERS
    # in the Dockerfile/compose and MIGRATION.md).
    register_terminal_routes(app)

    # Notification engine (strangler migration): POST /api/notify + WS
    # /ws/notifications, backed by this workspace's own Postgres schema. Any
    # Tier-1 app can also fire through it via AppContext.notifications
    # (src/apps/base.py), which reaches the same manager off app.state.
    register_notification_routes(app)

    # Decoupled-apps framework (F1): plugin runtime + /api/apps management.
    # Tier-1 apps hot-load into THIS process — no restart. Installed apps are
    # reloaded from the local registry on startup.
    register_apps_routes(app)

    # Legacy component compatibility for the cloud SPA: exposes Tier-2 app
    # containers through /api/components and /ws/logs so managed app title-bar
    # controls operate against this workspace backend.
    register_component_routes(app)

    return app
