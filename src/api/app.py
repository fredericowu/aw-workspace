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
from starlette.datastructures import Headers

from src.api.db import create_all_tables, get_session, get_workspace_schema
from src.api.components import register_component_routes
from src.api.agent_routes import register_agent_routes, sync_on_boot
from src.api.folders import register_folder_routes
from src.api.marketplace import register_marketplace_routes
from src.api.identity import _extract_token, decode_identity_jwt, require_identity
from src.api.models import Setting
from src.api.notifications import register_notification_routes
from src.api.observability import register_observability_routes
from src.api.terminal import register_terminal_routes
from src.api.workspace_api_key import (
    get_or_create_workspace_api_key,
    regenerate_workspace_api_key,
)
from src.api.workspace_url import publish_workspace_api_url
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


def _is_app_mount_host(host: str) -> bool:
    """True for a Tier-2 container app's own hostname (``<app_id>.app.<anything>``,
    e.g. ``signoz.app.aw.workspace.aw.tekflox.com``) — the same shape
    ``src/apps/runtime.py``'s ``_attach_mount`` mounts a ``Host(f"{app_id}.app.
    {{_:str}}"...)`` route for."""
    return ".app." in f"{host}."


class _ScopedCORSMiddleware(CORSMiddleware):
    """Starlette's ``CORSMiddleware``, skipped entirely for an app-mount host.

    A Tier-2 container app (e.g. SigNoz) is reverse-proxied byte-for-byte
    (``src/apps/proxy.py``) and owns its own CORS policy end-to-end — some
    set a blanket ``Access-Control-Allow-Origin: *`` on every response.
    ``CORSMiddleware.send()`` stamps ``Access-Control-Allow-Credentials:
    true`` onto ANY response that carries an ``Origin`` header, regardless
    of whether that origin actually matched ``allow_origin_regex`` (it only
    gates whether to ALSO reflect an explicit ``Access-Control-Allow-
    Origin`` — see ``send()`` in Starlette's ``cors.py``). Left unscoped,
    that turns into ``Access-Control-Allow-Origin: *`` (the app's own,
    forwarded verbatim) plus ``Access-Control-Allow-Credentials: true``
    (stamped by this middleware for an origin it was never asked to allow)
    on every credentialed request to an app's own hostname — a combination
    the Fetch/CORS spec forbids and a spec-compliant browser refuses to use
    (confirmed live against aw-app-signoz, 2026-08-30: this is what turned
    a genuinely valid login session into an unusable one). This middleware
    exists only for the SPA<->API pair (``_spa_origin_regex``); an app-mount
    host was never in scope for it, so skip straight to the wrapped app and
    leave the app's own CORS headers untouched."""

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] == "http":
            host = Headers(scope=scope).get("host", "").split(":", 1)[0]
            if _is_app_mount_host(host):
                await self.app(scope, receive, send)
                return
        await super().__call__(scope, receive, send)


def create_app() -> FastAPI:
    create_all_tables()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # In-process broadcast needs the running loop to schedule sends from
        # sync callers (e.g. the apps facade) — see NotificationManager.set_loop.
        app.state.notification_mgr.set_loop(asyncio.get_running_loop())
        app.state.app_install_jobs.set_loop(asyncio.get_running_loop())
        # A freshly-installed/recreated workspace always gets a workspace API
        # key with zero manual steps — get_or_create is a no-op once one
        # already exists in the settings table. MUST run before
        # reconcile_on_boot: an app's own activate() (e.g. aw-app-whiteboard's
        # mcp/self_register.py) reads AW_WORKSPACE_API_KEY from os.environ to
        # put in its self-registered mcp.json — publishing the key AFTER apps
        # already loaded would silently ship that entry with no auth header.
        get_or_create_workspace_api_key()
        # Publish this workspace's own external API URL next to the key, so an
        # aw-workspace-cli running in a spawned agent-runner container (no
        # loopback to the server) can reach it via the public tunnel edge.
        publish_workspace_api_url()
        # Converge the running app set to the cloud registry on startup — a
        # fresh/recreated workspace auto-reinstalls the user's apps (F3).
        await reconcile_on_boot(app)
        # AFTER reconcile: an app's activate() copies its contributes.skills
        # into skills/ (AppRuntime._register_skills), so syncing earlier would
        # mirror a skills/ tree that's about to change.
        await sync_on_boot()
        yield

    app = FastAPI(title="aw-workspace", version="0.1.0", lifespan=lifespan)

    # SPA→API is cross-origin (same apex) and credentialed — allow the SPA
    # origin with credentials so the apex aw_id_jwt cookie is accepted and
    # preflight (OPTIONS) succeeds. allow_credentials forbids a "*" origin,
    # hence the per-workspace regex.
    app.add_middleware(
        _ScopedCORSMiddleware,
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

    # Mapped folders: point the workspace at ANY directory (no git repo
    # required) and hand it to apps by name — the repo-bound
    # $AW_WORKSPACE_REPOS placeholder's general replacement. See
    # src/api/folders.py.
    register_folder_routes(app)

    # Observability: where this workspace sends OTLP telemetry (Desligado /
    # Local auto-detected via aw-app-signoz / Custom endpoint+key). See
    # src/api/observability.py.
    register_observability_routes(app)

    # Marketplace sources: user-managed list of marketplaces the catalog is
    # merged from, including private ones (per-source credential in the
    # secret store, bound to its host). Settings → Marketplace drives this.
    # See src/api/marketplace.py.
    register_marketplace_routes(app)

    # POST /api/agent/sync — the HTTP face of `aw-workspace-cli agent sync`,
    # so the SPA can re-run the fan-out without a terminal. See
    # src/api/agent_routes.py.
    register_agent_routes(app)

    # Decoupled-apps framework (F1): plugin runtime + /api/apps management.
    # Tier-1 apps hot-load into THIS process — no restart. Installed apps are
    # reloaded from the local registry on startup.
    register_apps_routes(app)

    # Legacy component compatibility for the cloud SPA: exposes Tier-2 app
    # containers through /api/components and /ws/logs so managed app title-bar
    # controls operate against this workspace backend.
    register_component_routes(app)

    # Generic settings KV, registered LAST: Starlette matches routes in
    # registration order, not by specificity, so a catch-all `{key}` route
    # registered earlier than a literal one (e.g. `/workspace-api-key`,
    # `/observability`) would shadow it forever — every dedicated settings
    # item above depends on being reachable, so this must come after all of
    # them. Found 2026-08-29 verified live: `/api/settings/workspace-api-key`
    # was silently returning `{"key": "workspace-api-key", "value": null}`
    # (the catch-all's own shape, looked up under the wrong storage key)
    # instead of the real key, ever since that route was added.
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

    return app
