"""Tier-1 in-process hot-load runtime (ADR Decision 3) — the hard part.

Loads an app's Python plugin into the RUNNING aw-workspace FastAPI process and
hot-registers its backend routes under ``/api/apps/<slug>`` with **no restart**;
hot-unregisters on uninstall. Each app's routes live in their OWN sub-app
(``FastAPI()``), mounted as a single ``Mount`` entry in ``host.router.routes`` —
so add/remove is one list mutation Starlette picks up per request. Mutations run
in the event loop guarded by an ``asyncio.Lock``.

On unload: remove the Mount first (no NEW requests route to the app), then await
in-flight requests draining (with timeout), fire ``on_deactivate`` hooks,
``await plugin.deactivate()``, then unimport the app's modules and drop refs.
Every mount is journaled; uninstall replays the journal in reverse.
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import logging
import os
import sys
from dataclasses import dataclass, field
from typing import Any, Callable

from fastapi import FastAPI
from starlette.routing import Host, Mount, get_route_path
from starlette.types import Receive, Scope, Send

import types

from src.apps import paths
from src.apps.fetch import apps_root
from src.apps.base import AppContext, Plugin
from src.apps.capabilities import filter_grants
from src.apps.commands import CommandInstaller
from src.apps.containers import ContainerError, ContainerSupervisor, expand_env
from src.apps.journal import ActionJournal
from src.apps.manifest import Manifest, load_manifest
from src.apps.proxy import ContainerReverseProxy
from src.apps.secret_store import SecretStore
from src.apps.services import ServiceSupervisor
from src.apps import agents as agents_mod
from src.apps import tasks as tasks_mod
from src.apps.agents import AgentsRegistry
from src.apps.skills import SkillError, SkillsRegistry
from src.apps.tasks import TasksRegistry
from src.apps.watchdog import WatchdogSupervisor

log = logging.getLogger(__name__)

DEFAULT_DRAIN_TIMEOUT = float(os.environ.get("AW_APPS_DRAIN_TIMEOUT", "10"))
DEFAULT_WORKSPACE_CONTAINER_DIR = "/opt/aw-workspace"

# System-CLI healer cadence — see CommandInstaller's module docstring for why
# this exists. "__system__" can't collide with a real app slug (manifest.
# SLUG_RE requires slugs to start with a letter), so it's a safe sentinel key
# for the runtime's own watchdog task.
DEFAULT_CLI_HEAL_INTERVAL_S = float(os.environ.get("AW_APPS_CLI_HEAL_INTERVAL_S", "300"))
_SYSTEM_APP_ID = "__system__"
_CLI_HEALER_APP_ID = _SYSTEM_APP_ID  # back-compat alias
_CLI_HEALER_TASK_ID = "system-cli-health"

# MCP-gateway rescan cadence. The install/uninstall/config-save hooks push a
# reload the moment an app changes what the gateway's app-scan would find;
# this is the safety net UNDER those hooks, for the paths no hook covers —
# chiefly boot, where an already-running gateway scanned before the inprocess
# apps activated and rewrote their mcp.json (aw-app-whiteboard and
# aw-app-presentations both went missing this way, 2026-08-12: 183 tools
# instead of 209). Set to 0 to disable.
#
# 60s, not the original 300s. The boot reload in routes.py cannot close this
# on its own: it fires once the workspace's apps are up, but the gateway is a
# CONTAINER this same boot restarts, and a gateway that comes up afterwards
# scans while the workspace API is still booting, connects to nothing, and
# serves 0 tools for every Tier-1 upstream until something reloads it. There
# is no ordering between the two, so the honest fix is to converge fast:
# 5 minutes of "Unknown tool" for show_diff/whiteboard/presentations is a real
# outage for every agent, and a reload is differential and cheap (a no-op pass
# is one HTTP call and a few hundred ms).
#
# The deeper fix belongs in aw-mcp-gateway — an upstream that fails to connect
# during a scan should be retried, not left at zero tools until someone else
# happens to reload it. Until that lands, this bounds the damage.
DEFAULT_MCP_RESCAN_INTERVAL_S = float(os.environ.get("AW_APPS_MCP_RESCAN_INTERVAL_S", "60"))
_MCP_RESCAN_TASK_ID = "mcp-gateway-rescan"

# Cookie the central-identity JWT lands in (mirrors src.api.identity.COOKIE_NAME).
_ID_COOKIE = "aw_id_jwt"


def _cookie_value(cookie_header: str, name: str) -> str:
    """Pull one cookie value out of a raw ``Cookie:`` header (no deps)."""
    from http.cookies import SimpleCookie
    try:
        jar = SimpleCookie()
        jar.load(cookie_header)
        morsel = jar.get(name)
        return morsel.value if morsel else ""
    except Exception:
        return ""


def _headers_dict(scope: Scope) -> dict[bytes, bytes]:
    return {k.lower(): v for k, v in (scope.get("headers") or [])}


def _default_verify_http(scope: Scope) -> dict | None:
    """Verify the identity JWT on an HTTP scope (bearer header or apex cookie),
    or the workspace-wide ``X-Api-Key`` header (``src.api.workspace_api_key``)
    — lets another app/MCP authenticate into ANY installed app's routes with
    the shared workspace key instead of a browser-issued JWT (first consumer:
    an external whiteboard MCP process)."""
    from src.api.identity import decode_identity_jwt
    from src.api.workspace_api_key import HEADER_NAME, verify_workspace_api_key
    headers = _headers_dict(scope)
    api_key = headers.get(HEADER_NAME.lower().encode(), b"").decode()
    if api_key and verify_workspace_api_key(api_key):
        return {"sub": "workspace-api-key", "api_key": True}
    auth = headers.get(b"authorization", b"").decode()
    if auth.lower().startswith("bearer "):
        token = auth[7:].strip()
    else:
        token = _cookie_value(headers.get(b"cookie", b"").decode(), _ID_COOKIE)
    return decode_identity_jwt(token) if token else None


def _default_verify_ws(scope: Scope) -> dict | None:
    """Verify a WebSocket handshake: workspace ``X-Api-Key`` header, then the
    identity JWT (``?token=`` query param, then the apex ``aw_id_jwt`` cookie).

    A JS ``new WebSocket()`` in a real browser tab can't set custom headers,
    so a human's session always resolves via the query-param/cookie path
    (same order as ``src.api.identity.authorize_ws``) — this mirrors
    ``_default_verify_http``'s API-key check for the same reason that one
    exists: a non-browser caller (this workspace's own CLI, an external MCP,
    a CDP-driven automation tool that CAN set upgrade-request headers) needs
    a way in that isn't a browser-issued JWT. Confirmed missing live
    2026-08-08: a Tier-2 app's WebSocket-dependent UI (aw-app-code-server's
    extension host connections) hung/timed out under a Playwright session
    authenticated only via ``X-Api-Key`` — every one of its WS handshakes
    was silently 4401ing since this function never looked at the header the
    HTTP requests in the very same session were already passing.
    """
    from urllib.parse import parse_qs

    from src.api.identity import decode_identity_jwt
    from src.api.workspace_api_key import HEADER_NAME, verify_workspace_api_key
    headers = _headers_dict(scope)
    api_key = headers.get(HEADER_NAME.lower().encode(), b"").decode()
    if api_key and verify_workspace_api_key(api_key):
        return {"sub": "workspace-api-key", "api_key": True}
    qs = parse_qs(scope.get("query_string", b"").decode())
    token = (qs.get("token") or [""])[0]
    if not token:
        token = _cookie_value(headers.get(b"cookie", b"").decode(), _ID_COOKIE)
    return decode_identity_jwt(token) if token else None


def _local_paths_for(loaded: "LoadedApp") -> list[str]:
    """Mount-relative ``local_paths`` an app is allowed to bypass auth on.

    Only honored when the app was actually granted ``routes:local`` (declaring
    it in the manifest is not enough — an unsigned/side-loaded app could still
    have it stripped by :func:`src.apps.capabilities.filter_grants`, though
    it's low-risk so that's not the common case).
    """
    if "routes:local" not in loaded.granted_permissions:
        return []
    paths: list[str] = []
    for route in loaded.manifest.contributes.get("routes", []) or []:
        if isinstance(route, dict):
            paths.extend(str(p) for p in route.get("local_paths", []) or [])
    return paths


class IdentityGuard:
    """ASGI wrapper enforcing the central identity JWT on an app's sub-app.

    F1 mounts the raw sub-app, so every ``/api/apps/<slug>/*`` route is
    unauthenticated (framework routes use ``Depends(require_identity)``; app
    routes never did). This guard is inserted by :meth:`AppRuntime._mount`
    between the ``Mount`` and the ``_DrainableApp`` so the app never sees an
    unauthenticated request (F6 Capability 1):

    * ``http`` — missing/invalid token → 401 JSON, app never invoked.
    * ``websocket`` — missing/invalid token → accept then ``close(4401)`` before
      the app accepts (mirrors ``src/api/terminal.py``); app never invoked.

    By default all app routes are guarded (401/4401 on missing or invalid
    identity). A managed app can set ``auth_required: false`` in its
    framework config to let the app decide access itself instead — but this
    is NOT "no auth", it's "the app's own auth is the final gate": identity
    is still verified and forwarded (``scope["aw_identity"]``) whenever the
    caller presents one (e.g. a logged-in browser's cookie), just never
    required. This lets one route serve both a cookie-based dashboard caller
    and a bearer-token-only external caller under the same relaxed setting —
    see ``mcp-gateway``'s ``admin/config``, which accepts either. The
    ``routes:local`` bypass below remains narrower and unconditional: loopback
    callers only, only for declared local paths, no identity check attempted
    at all. Verifiers are injectable for tests.

    ``local_paths`` (ADR "Apps Own Their Front + Back Routes" Decision 2): a
    mount-relative path (e.g. ``/eval``) that skips the JWT check when the
    caller's ``scope.client.host`` is a loopback address — the escape hatch
    for agent-driven endpoints called from inside the workspace with no
    cookie/bearer token. Only populated when the app was granted
    ``routes:local`` (:meth:`AppRuntime._attach_mount`).

    On a successful identity verification, the decoded claims are stashed at
    ``scope["aw_identity"]`` so app WS/HTTP handlers can read who's calling
    (``websocket.scope.get("aw_identity")``) — a local-bypass request has no
    claims, so it's simply absent.
    """

    _LOCAL_HOSTS = ("127.0.0.1", "::1")

    def __init__(self, app: Any, verify_http=None, verify_ws=None,
                 local_paths: "list[str] | None" = None,
                 auth_required: "bool | Callable[[], bool]" = True) -> None:
        self.app = app
        self._verify_http = verify_http or _default_verify_http
        self._verify_ws = verify_ws or _default_verify_ws
        self._local_paths = frozenset(local_paths or [])
        self._auth_required = auth_required

    def _requires_auth(self) -> bool:
        if callable(self._auth_required):
            return bool(self._auth_required())
        return bool(self._auth_required)

    def _local_bypass(self, scope: Scope) -> bool:
        if not self._local_paths:
            return False
        client = scope.get("client")
        host = client[0] if client else None
        if host not in self._LOCAL_HOSTS:
            return False
        # scope["path"] is the FULL path — Mount only adjusts root_path, not
        # path (starlette.routing.Mount.matches) — so the mount-relative
        # remainder has to be derived the same way starlette itself does.
        return get_route_path(scope) in self._local_paths

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        stype = scope["type"]
        if stype == "http":
            if self._local_bypass(scope):
                await self.app(scope, receive, send)
                return
            if not self._requires_auth():
                # "App decides": the framework no longer hard-gates this
                # route, but a caller's identity is still verified and
                # forwarded when present (e.g. a logged-in browser session)
                # — only a MISSING/invalid identity is tolerated instead of
                # 401ing, deferring the final call to the app's own auth (a
                # bearer token, an API key, ...). Without this, an app that
                # turns auth_required off to accept external bearer-token
                # callers would ALSO stop seeing identity for its own
                # dashboard users, breaking any admin UI that expects
                # scope["aw_identity"] to be set for a normal logged-in call.
                claims = self._verify_http(scope)
                if claims is not None:
                    scope["aw_identity"] = claims
                await self.app(scope, receive, send)
                return
            claims = self._verify_http(scope)
            if claims is None:
                await self._send_401(send)
                return
            scope["aw_identity"] = claims
        elif stype == "websocket":
            if self._local_bypass(scope):
                await self.app(scope, receive, send)
                return
            if not self._requires_auth():
                claims = self._verify_ws(scope)
                if claims is not None:
                    scope["aw_identity"] = claims
                await self.app(scope, receive, send)
                return
            claims = self._verify_ws(scope)
            if claims is None:
                await self._reject_ws(receive, send)
                return
            scope["aw_identity"] = claims
        await self.app(scope, receive, send)

    @staticmethod
    async def _send_401(send: Send) -> None:
        body = b'{"detail":"unauthorized"}'
        await send({
            "type": "http.response.start", "status": 401,
            "headers": [(b"content-type", b"application/json"),
                        (b"content-length", str(len(body)).encode())],
        })
        await send({"type": "http.response.body", "body": body})

    @staticmethod
    async def _reject_ws(receive: Receive, send: Send) -> None:
        # Drain the connect event, accept, then close with the unauthorized code
        # (4401) — same handshake as terminal.py so the browser sees a real code.
        try:
            await receive()  # websocket.connect
        except Exception:
            pass
        await send({"type": "websocket.accept"})
        await send({"type": "websocket.close", "code": 4401})


class _DrainableApp:
    """ASGI wrapper tracking in-flight HTTP/WS requests for one app's sub-app.

    Lets the runtime await a clean drain before unimporting the app.
    """

    def __init__(self, app: Any) -> None:
        self.app = app
        self._active = 0
        self._idle = asyncio.Event()
        self._idle.set()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        self._active += 1
        self._idle.clear()
        try:
            await self.app(scope, receive, send)
        finally:
            self._active -= 1
            if self._active <= 0:
                self._active = 0
                self._idle.set()

    @property
    def active(self) -> int:
        return self._active

    async def drain(self, timeout: float) -> bool:
        """Wait until no requests are in flight. Returns False on timeout."""
        try:
            await asyncio.wait_for(self._idle.wait(), timeout)
            return True
        except asyncio.TimeoutError:
            return False


class _ContainerPlugin(Plugin):
    """No-op plugin standing in for a Tier-2 (container) app.

    A container app runs its backend in the container, not in-process — there is
    no Python entrypoint to activate. This keeps the ``LoadedApp`` shape uniform
    (drain hooks / deactivate() are safe no-ops) so unload works unchanged.
    """

    async def activate(self, ctx: "AppContext") -> None:  # pragma: no cover
        return None

    async def deactivate(self) -> None:
        return None


@dataclass
class LoadedApp:
    manifest: Manifest
    plugin: Plugin
    ctx: AppContext
    package_dir: str
    granted_permissions: list[str]
    config: dict[str, Any] = field(default_factory=dict)
    signed: bool = False
    mount: Mount | None = None
    host_mount: Host | None = None
    drainable: _DrainableApp | None = None
    module_prefix: str = ""


class AppRuntime:
    """Owns the set of loaded Tier-1 apps for one host FastAPI process."""

    def __init__(self, host: FastAPI, journal: ActionJournal | None = None,
                 drain_timeout: float = DEFAULT_DRAIN_TIMEOUT,
                 guard_identity: bool = True) -> None:
        self.host = host
        self.journal = journal or ActionJournal()
        self.drain_timeout = drain_timeout
        # F6 Cap 1: wrap every app mount with IdentityGuard. Off only for pure
        # runtime unit tests that hit app subroutes without a real JWT.
        self.guard_identity = guard_identity
        self._apps: dict[str, LoadedApp] = {}
        self._lock = asyncio.Lock()
        self._loading: LoadedApp | None = None  # set during activate() for _mount
        # F4 effect backends the capability facades route through.
        self.commands = CommandInstaller()
        self.services = ServiceSupervisor()
        self.containers = ContainerSupervisor()
        self.watchdog = WatchdogSupervisor()
        self.secret_store = SecretStore()
        self.skills = SkillsRegistry()
        self.tasks = TasksRegistry()
        self.agents = AgentsRegistry()
        self._db_tables: Any = None  # lazy — only apps granted db:own-tables need it

    @property
    def db_tables(self) -> Any:
        """Lazily built so a pure-unit runtime never imports the PG engine layer."""
        if self._db_tables is None:
            from src.apps.db_tables import DbTables
            self._db_tables = DbTables()
        return self._db_tables

    def _apply_migrations(self, manifest: Manifest, package_dir: str) -> None:
        """Apply the app's pending migrations/*.sql, if it declares a dir
        (src/apps/migrations.py). Called on every load() — install AND
        update — after plugin.activate() so a migration can ALTER a table
        the app's own bootstrap code just ensured exists. One app's
        migration failure is logged, not fatal to the load — the app's
        CREATE TABLE IF NOT EXISTS already gave it a working baseline
        schema; a missed ALTER degrades a feature, it doesn't brick the app."""
        from src.apps.migrations import apply_migrations, migrations_dir_for

        migrations_dir = migrations_dir_for(package_dir, manifest.migrations)
        if migrations_dir is None:
            return
        try:
            apply_migrations(manifest.id, migrations_dir)
        except Exception:
            log.exception("apps: applying migrations for %s failed", manifest.id)

    # ---- system-CLI drift healing ----------------------------------------

    def start_system_cli_healer(self, interval_s: float = DEFAULT_CLI_HEAL_INTERVAL_S) -> None:
        """Start the runtime-owned periodic re-check of every CLI installed
        via ``install_system_cli`` (git, gh, aws, gcloud, ...) — one task,
        covering every app for free, no ``watchdog:tasks`` permission needed
        since this is core runtime code, not an app driving its own facade.
        Call once, from an async context (``reconcile_on_boot``); idempotent.
        """
        if _CLI_HEALER_TASK_ID in self.watchdog.task_ids_for(_CLI_HEALER_APP_ID):
            return
        self.watchdog.register(
            _CLI_HEALER_APP_ID, _CLI_HEALER_TASK_ID, self._heal_system_clis,
            interval_s, run_immediately=False,
        )

    async def _heal_system_clis(self) -> None:
        for app_id, name in self.commands.missing_system_clis():
            try:
                await asyncio.to_thread(self.commands.heal, app_id, name)
            except Exception as exc:  # noqa: BLE001 — one bad CLI must not stop the rest
                # Recorded, not just logged. This used to be a log line and
                # nothing else, repeated every pass — 65 times in one boot for
                # four CLIs that were never coming back — so a permanently
                # broken install was invisible to everything except someone
                # reading the container log. `doctor` reads this state.
                self.commands.record_heal_result(app_id, name, str(exc))
                log.exception("apps: failed to heal system CLI %r for %s", name, app_id)
                continue
            healthy, reason = self.commands.check_system_cli(app_id, name)
            # An installer that exits 0 is not proof either: it can succeed and
            # still leave the CLI unusable. Only the health check settles it.
            self.commands.record_heal_result(app_id, name, None if healthy else reason)
            if healthy:
                log.warning("apps: healed system CLI %r for %s (was unhealthy, reinstalled)",
                            name, app_id)
            else:
                log.warning("apps: installer for %r (%s) exited 0 but the CLI is still "
                            "unhealthy: %s", name, app_id, reason)

    # ---- MCP gateway rescan ----------------------------------------------

    def start_mcp_gateway_rescan(
            self, interval_s: float = DEFAULT_MCP_RESCAN_INTERVAL_S) -> None:
        """Start the runtime-owned periodic ``POST /reload`` against the
        installed mcp-gateway app. Same shape and rationale as
        ``start_system_cli_healer``: core runtime code, so no
        ``watchdog:tasks`` grant is involved — and mcp-gateway is a
        container-tier app with no in-process plugin, so it could not
        register a watchdog task for itself even if it wanted to.

        ``run_immediately=False``: boot already reloads via reconcile's
        coalesced trigger, so the first tick belongs one interval later.
        Idempotent; a non-positive interval disables the task entirely.
        """
        if interval_s <= 0:
            log.info("apps: mcp-gateway rescan watchdog disabled (interval=%s)", interval_s)
            return
        if _MCP_RESCAN_TASK_ID in self.watchdog.task_ids_for(_SYSTEM_APP_ID):
            return
        self.watchdog.register(
            _SYSTEM_APP_ID, _MCP_RESCAN_TASK_ID, self._rescan_mcp_gateway,
            interval_s, run_immediately=False,
        )

    async def _rescan_mcp_gateway(self) -> None:
        """One tick. Raises on failure so the supervisor's backoff and the
        ``last_ok``/``last_error`` introspection in ``GET /api/apps/-/watchdog``
        mean something — unlike the fire-and-forget call sites, a watchdog
        that silently no-ops every 5 minutes is worse than no watchdog.
        Deferred import: routes.py imports FROM this module."""
        from src.apps.routes import _reload_mcp_gateway
        await _reload_mcp_gateway(self, raise_on_failure=True)

    # ---- introspection --------------------------------------------------

    def is_loaded(self, slug: str) -> bool:
        return slug in self._apps

    def loaded_slugs(self) -> list[str]:
        return list(self._apps)

    def get(self, slug: str) -> LoadedApp | None:
        return self._apps.get(slug)

    def contributions(self) -> dict[str, Any]:
        """Frontend contributions for ``GET /api/apps/-/contributions``.

        Declarative (windows/nav/settings) fill slots as data; the ``frontend``
        block (Decision 3b) tells the SPA plugin runtime which apps ship a
        real ESM bundle (``component``/``iframe`` mode) and what slots they
        register into. The SPA re-fetches this on a ``contributions-changed``
        event and mounts/unmounts accordingly.
        """
        windows: list[dict[str, Any]] = []
        nav: list[dict[str, Any]] = []
        settings: list[dict[str, Any]] = []
        frontend: list[dict[str, Any]] = []
        for app in self._apps.values():
            slug = app.manifest.id
            for win in app.manifest.windows:
                windows.append(self._resolve_window(app, {"app": slug, **win}))
            for entry in app.manifest.nav:
                nav.append({"app": slug, **entry})
            for panel in app.manifest.settings_panels:
                settings.append({"app": slug, **panel})
            fe = app.manifest.frontend
            if fe:
                bundle = fe.get("bundle")
                # A declared bundle the package doesn't actually ship is a
                # phantom: the SPA import()s the announced URL and takes a 404
                # (+ a console error) on EVERY page load, for as long as the app
                # stays installed. There's no build step at install time — the
                # compiled bundle is committed by the app (see aw-app-diff-tool's
                # .gitignore), so a missing file means a packaging bug in that
                # app, not a transient state. Found live 2026-08-12 on the
                # `hello` template app, whose ui/dist is gitignored. Announce
                # only what can be served; the rest of the contribution
                # (windows, nav, settings) is unaffected.
                if bundle and not self._bundle_exists(app, bundle):
                    log.warning(
                        "apps: %s declares frontend bundle %r but the package "
                        "has no ui/dist/%s — not announcing it",
                        slug, bundle, os.path.basename(str(bundle)))
                    bundle = None
                frontend.append({
                    "app": slug,
                    "mode": fe.get("mode", "iframe"),
                    # Content-hashed URL busts the SPA module cache on upgrade.
                    "bundle_url": (
                        f"/api/apps/{slug}/ui/{os.path.basename(str(bundle))}"
                        if bundle else None
                    ),
                    "components": fe.get("components", []),
                    "slot_extensions": fe.get("slot_extensions", []),
                    "slots": fe.get("slots", []),
                    # Trust gate inputs (Decision 3b/4): component mode is only
                    # honored for a signed app that was granted ui:code.
                    "signed": app.signed,
                    "granted_permissions": app.granted_permissions,
                })
        return {"windows": windows, "nav": nav, "settings": settings,
                "frontend": frontend}

    @staticmethod
    def _bundle_exists(app: LoadedApp, bundle: str) -> bool:
        """Is the declared frontend bundle actually in the package?

        Resolved exactly like ``GET /api/apps/{slug}/ui/{path}`` serves it
        (basename under the package's ``ui/dist``), so "announced" and
        "servable" can't drift apart.
        """
        target = os.path.join(app.package_dir, "ui", "dist",
                              os.path.basename(str(bundle)))
        return os.path.isfile(target)

    def skills_index(self) -> list[dict[str, Any]]:
        """Index of every ``contributes.skills`` entry for ``GET /api/apps/-/skills``.

        Points at the copy :meth:`load` placed into the shared skills dir
        (:func:`src.apps.paths.skills_dir`) — an agent runtime reads
        ``skill_md_path`` directly; this index itself carries no content.
        """
        out: list[dict[str, Any]] = []
        for app in self._apps.values():
            slug = app.manifest.id
            for entry in app.manifest.skills:
                skill_id = entry.get("id")
                if not skill_id:
                    continue
                dest_path = os.path.join(paths.skills_dir(), skill_id)
                out.append({
                    "app": slug,
                    "id": skill_id,
                    "description": entry.get("description", ""),
                    "skill_md_path": os.path.join(dest_path, "SKILL.md"),
                    "registered": os.path.isdir(dest_path),
                })
        return out

    def _register_skills(self, loaded: LoadedApp) -> None:
        """Copy each ``contributes.skills`` entry into the shared skills index.

        No-op for an app that declares none. A bad entry (missing SKILL.md,
        path escaping the package dir) is logged and skipped rather than
        failing the whole install — same non-fatal posture as window/nav spec
        resolution.
        """
        slug = loaded.manifest.id
        for entry in loaded.manifest.skills:
            skill_id = entry.get("id")
            path = entry.get("path")
            if not skill_id or not path:
                continue
            try:
                dest_path = self.skills.register(slug, skill_id, loaded.package_dir, path)
            except SkillError:
                log.exception("apps: failed to register skill %r for %s", skill_id, slug)
                continue
            self.journal.record(slug, "skill:register", skill_id, {"dest_path": dest_path})

    def _register_tasks(self, loaded: LoadedApp) -> None:
        """Seed this app's ``contributes.tasks`` (create-if-absent, by name).

        Two directions, because activation order isn't guaranteed: this app's
        own declarations go out now (held if no provider is loaded yet), and
        if THIS app is itself the provider, sweep every app already loaded.
        See ``src/apps/tasks.py``.
        """
        slug = loaded.manifest.id
        try:
            self.tasks.register(self, slug, loaded.manifest.tasks)
            plugin = getattr(loaded, "plugin", None)
            if plugin is not None and callable(getattr(plugin, tasks_mod.PROVIDER_METHOD, None)):
                seeded = self.tasks.sweep(self)
                if seeded:
                    log.info("apps: %s is the task provider, seeded %d task(s)", slug, seeded)
        except Exception:  # noqa: BLE001 — seeding must never fail an install
            log.exception("apps: task seeding failed for %s", slug)

    def _register_agents(self, loaded: LoadedApp) -> None:
        """Seed this app's ``contributes.agents`` (create-if-absent, by slug).

        Same two directions as ``_register_tasks``, for the same reason —
        activation order isn't guaranteed. See ``src/apps/agents.py``.
        """
        slug = loaded.manifest.id
        try:
            self.agents.register(self, slug, loaded.manifest.agents, loaded.package_dir)
            plugin = getattr(loaded, "plugin", None)
            if plugin is not None and callable(getattr(plugin, agents_mod.PROVIDER_METHOD, None)):
                seeded = self.agents.sweep(self)
                if any(seeded.values()):
                    log.info("apps: %s is the agent provider, seeded %s", slug, seeded)
        except Exception:  # noqa: BLE001 — seeding must never fail an install
            log.exception("apps: agent seeding failed for %s", slug)

    def _resolve_window(self, app: LoadedApp, entry: dict[str, Any]) -> dict[str, Any]:
        """Inline a declarative window's spec file into ``body.spec_data``.

        The SPA's ``AppWindow`` renders a spec object; the F1 manifest only
        carries a ``body.spec`` file ref (``windows/main.json``), which was never
        served — so the window was unreachable (F6 Cap 2). Resolve it here, once
        per contributions fetch, path-scoped to the app's own package.
        """
        body = entry.get("body") or {}
        spec_ref = body.get("spec")
        if body.get("type") != "declarative" or not spec_ref or body.get("spec_data"):
            return entry
        pkg_root = os.path.realpath(app.package_dir)
        target = os.path.realpath(os.path.join(pkg_root, spec_ref))
        if not target.startswith(pkg_root + os.sep) or not os.path.isfile(target):
            log.warning("apps: window spec %r for %s not found / out of package",
                        spec_ref, app.manifest.id)
            return entry
        try:
            with open(target, encoding="utf-8") as fh:
                spec_data = json.load(fh)
        except Exception:
            log.exception("apps: failed to load window spec %r for %s",
                          spec_ref, app.manifest.id)
            return entry
        return {**entry, "body": {**body, "spec_data": spec_data}}

    # ---- load / unload --------------------------------------------------

    async def load(self, package_dir: str, granted_permissions: list[str] | None = None,
                   config: dict[str, Any] | None = None, signed: bool = False) -> Manifest:
        """Validate, import, and activate an app from its package dir (hot).

        ``signed`` reflects whether the package is trusted (marketplace-signed).
        High-risk capabilities are stripped from the effective grant for an
        unsigned app (ADR Decision 4) — defence in depth on top of the same
        filter the cloud registry applies, so the runtime never hands out a
        high-risk facade to a side-loaded app even if the registry row lists it.
        """
        manifest = load_manifest(package_dir)
        slug = manifest.id
        if slug in self._apps:
            raise ValueError(f"app {slug!r} is already loaded")
        if manifest.tier == "container":
            return await self._load_container(
                manifest, package_dir, granted_permissions, config or {}, signed)
        if manifest.tier != "inprocess":
            raise ValueError(f"runtime only loads tier=inprocess|container (got {manifest.tier!r})")

        requested = granted_permissions if granted_permissions is not None else list(manifest.permissions)
        granted, refused = filter_grants(requested, signed=signed)
        if refused:
            log.warning("apps: refused high-risk caps %s for unsigned app %s", refused, slug)
        cfg = config or {}

        plugin, module_prefix = self._import_plugin(manifest, package_dir)
        ctx = AppContext(
            runtime=self, app_id=slug, version=manifest.version,
            granted_permissions=granted, config=cfg, package_dir=package_dir,
        )
        loaded = LoadedApp(
            manifest=manifest, plugin=plugin, ctx=ctx, package_dir=package_dir,
            granted_permissions=granted, config=cfg, signed=signed,
            module_prefix=module_prefix,
        )

        # _mount (called from within activate via ctx.routes.register) attaches
        # to the app currently loading.
        self._loading = loaded
        try:
            await plugin.activate(ctx)
        except Exception:
            self._loading = None
            # residue-free failed load: drop any Mount recorded before the
            # failure, forget journal entries (incl. capability:denied), unimport
            if loaded.mount is not None and loaded.mount in self.host.router.routes:
                self.host.router.routes.remove(loaded.mount)
                self._invalidate_openapi()
            if loaded.host_mount is not None and loaded.host_mount in self.host.router.routes:
                self.host.router.routes.remove(loaded.host_mount)
            self.journal.clear_app(slug)
            self._unimport(module_prefix)
            raise
        self._loading = None

        if "db:own-tables" in granted:
            self._apply_migrations(manifest, package_dir)

        self._apps[slug] = loaded
        self._register_skills(loaded)
        self._register_tasks(loaded)
        self._register_agents(loaded)
        self._invalidate_openapi()
        log.info("apps: loaded %s v%s (routes mounted=%s)",
                 slug, manifest.version, loaded.mount is not None)
        return manifest

    async def unload(self, slug: str, drain_timeout: float | None = None) -> None:
        """Hot-unregister an app: unmount → drain → deactivate → unimport.

        Reverses every journaled side effect; leaves no residue.
        """
        loaded = self._apps.get(slug)
        if loaded is None:
            raise ValueError(f"app {slug!r} is not loaded")

        timeout = self.drain_timeout if drain_timeout is None else drain_timeout

        # 1. Remove the Mount FIRST so no new request routes to the app.
        async with self._lock:
            if loaded.mount is not None and loaded.mount in self.host.router.routes:
                self.host.router.routes.remove(loaded.mount)
            if loaded.host_mount is not None and loaded.host_mount in self.host.router.routes:
                self.host.router.routes.remove(loaded.host_mount)
            self._invalidate_openapi()

        # 2. Cancel the app's watchdog tasks BEFORE draining — stop producing
        #    (poll cache, WS broadcast) while its long-lived sockets close.
        try:
            self.watchdog.cancel_all_for(slug)
        except Exception:
            log.exception("apps: cancelling watchdog tasks for %s failed", slug)

        # 3. Signal long-poll/WS handlers, then drain in-flight requests.
        for hook in loaded.ctx._drain_hooks():
            try:
                res = hook()
                if asyncio.iscoroutine(res):
                    await res
            except Exception:
                log.exception("apps: on_deactivate hook failed for %s", slug)
        if loaded.drainable is not None:
            drained = await loaded.drainable.drain(timeout)
            if not drained:
                log.warning("apps: %s did not drain within %ss (%d in flight)",
                            slug, timeout, loaded.drainable.active)

        # 4. Plugin teardown.
        try:
            await loaded.plugin.deactivate()
        except Exception:
            log.exception("apps: deactivate() failed for %s", slug)

        # 5. Replay the journal in reverse — actually REVERT each side effect
        #    (F4): remove command shims, run the app's CLI revert script, drop
        #    app-owned tables, stop registered services. One failing revert must
        #    not block the rest; the route Mount was already removed above.
        for entry in self.journal.reverse_for(slug):
            try:
                await self._revert_entry(entry, loaded)
            except Exception:
                log.exception("apps: revert of %s %s failed for %s",
                              entry.kind, entry.target, slug)
        # Purge the app's secret namespace unconditionally (no residue even if a
        # secret was written in a prior process whose in-memory journal is gone).
        try:
            self.secret_store.purge(slug)
        except Exception:
            log.exception("apps: purging secrets for %s failed", slug)

        # An uninstalled app's CLI is gone on purpose — stop the healer from
        # trying to resurrect it (system_cli:revert-hook already ran above).
        self.commands.forget_system_clis_for(slug)
        self.journal.clear_app(slug)
        self._unimport(loaded.module_prefix)
        del self._apps[slug]
        log.info("apps: unloaded %s", slug)

    async def _revert_entry(self, entry: Any, loaded: LoadedApp) -> None:
        """Reverse a single journaled side effect (uninstall replay, F4).

        reconcile()'s upgrade path is uninstall+install for a plain version
        bump (see the db:table comment below), so this runs on EVERY routine
        app update, not just a real uninstall. ``services.stop_all_for``
        (subprocess wait), ``containers.stop_all_for`` (docker/podman API),
        and ``commands.run_revert`` (subprocess) all do blocking I/O — each
        offloaded to a thread so an app update can't freeze the whole
        workspace's single asyncio event loop (every other request/WS/
        terminal) for however long that takes. Reported live 2026-08-05.
        """
        kind = entry.kind
        if kind == "command:install":
            self.commands.remove_shim(entry.payload.get("bin_path", ""))
        elif kind == "system_cli:revert-hook":
            await asyncio.to_thread(self.commands.run_revert, loaded.package_dir, entry.target)
        elif kind == "db:table":
            # Deliberately NOT dropped (2026-08-04 decision — see db_tables.py's
            # module docstring): reconcile()'s upgrade path is uninstall+install
            # for a plain version bump, so dropping here wiped an app's data on
            # every routine update, not just a real uninstall. CREATE TABLE IF
            # NOT EXISTS on the next load() is already idempotent-safe against
            # existing data; schema evolution is the migrations/ mechanism's job
            # (src/apps/migrations.py), not an unload-time drop.
            pass
        elif kind == "service:register":
            await asyncio.to_thread(self.services.stop_all_for, loaded.manifest.id)
        elif kind == "container:register":
            await asyncio.to_thread(self.containers.stop_all_for, loaded.manifest.id)
        elif kind == "watchdog:register":
            # Idempotent with the explicit cancel_all_for in unload() above.
            self.watchdog.cancel_all_for(loaded.manifest.id)
        elif kind == "skill:register":
            self.skills.unregister(entry.payload.get("dest_path", ""))
        # route:mount (already unmounted), system_cli:install (audit-only),
        # secret:write (namespace purged above), capability:denied → no-op.

    # ---- facade callback (from RoutesFacade.register) -------------------

    def _mount(self, app_id: str, subapp: FastAPI) -> None:
        """Attach an app's sub-application under ``/api/apps/<slug>`` (hot)."""
        loaded = self._loading
        if loaded is None or loaded.manifest.id != app_id:
            raise RuntimeError("routes.register() may only be called during activate()")
        if loaded.mount is not None:
            raise RuntimeError(f"app {app_id!r} already mounted a sub-app")
        self._attach_mount(loaded, subapp)

    def _attach_mount(self, loaded: LoadedApp, asgi_app: Any) -> None:
        """Mount an ASGI app (Tier-1 sub-app or Tier-2 reverse proxy) hot.

        Wraps it in the drain tracker + :class:`IdentityGuard` and journals the
        ``route:mount`` so unload removes it. Shared by the Tier-1 routes facade
        (:meth:`_mount`) and the Tier-2 container path (:meth:`_load_container`).
        """
        app_id = loaded.manifest.id
        drainable = _DrainableApp(asgi_app)
        # IdentityGuard sits OUTSIDE the drain tracker so a rejected (401/4401)
        # request is never counted as in-flight against unload's drain.
        guarded = (IdentityGuard(
                       drainable,
                       local_paths=_local_paths_for(loaded),
                       auth_required=lambda: bool(loaded.config.get("auth_required", True)),
                   )
                   if self.guard_identity else drainable)
        mount = Mount(f"/api/apps/{app_id}", app=guarded)
        # Second entry point for the SAME guarded ASGI app: a per-app subdomain
        # (<app_id>.app.<anything>, e.g. proxy.app.aw.workspace.aw.tekflox.com)
        # that Caddy's *.app.<slug>.workspace wildcard already forwards here
        # unconditionally (see aw-backend's workspace_caddy_template.py — that
        # wildcard has been reserved and tunneled since the F4 split, just
        # never read on this side). Host() dispatches with the path taken
        # as-is (no /api/apps/<slug> prefix needed) — same view, same
        # IdentityGuard, same permissions, just addressed by host instead of
        # path. {_}` is a required-but-unused capture group so Host's path-
        # style compiler accepts a bare literal-with-suffix pattern; Starlette's
        # default str convertor doesn't exclude "." so it happily swallows the
        # rest of the hostname (workspace slug + domain) in one match.
        host_mount = Host(f"{app_id}.app.{{_:str}}", app=guarded)
        # Mutation happens on the event loop (single process) — the list append
        # is atomic w.r.t. request matching; no free-threading hazard.
        self.host.router.routes.append(mount)
        self.host.router.routes.append(host_mount)
        loaded.mount = mount
        loaded.host_mount = host_mount
        loaded.drainable = drainable
        self.journal.record(app_id, "route:mount", f"/api/apps/{app_id}",
                            {"version": loaded.manifest.version})

    # ---- Tier-2 (container) load ---------------------------------------

    async def _load_container(self, manifest: Manifest, package_dir: str,
                              granted_permissions: list[str] | None,
                              config: dict[str, Any], signed: bool) -> Manifest:
        """Load a ``tier: container`` app (Phase 6): spawn the image, reverse-proxy it.

        No Python entrypoint runs; the runtime brings up the container via the
        :class:`ContainerSupervisor` and mounts a :class:`ContainerReverseProxy`
        at ``/api/apps/<slug>`` behind the same IdentityGuard as Tier-1. Enforces
        ``containers:manage`` (high-risk → the grant filter strips it from an
        unsigned app, so Tier-2 needs a signed/marketplace app).
        """
        slug = manifest.id
        requested = granted_permissions if granted_permissions is not None else list(manifest.permissions)
        granted, refused = filter_grants(requested, signed=signed)
        if refused:
            log.warning("apps: refused high-risk caps %s for unsigned app %s", refused, slug)
        # Signing/trust (F8) isn't wired up yet — nothing ever sets signed=True
        # (not the CLI, not the catalog), so this gate currently blocks every
        # tier=container app unconditionally. Disabled until F8 lands; the
        # manifest-declared permission is still required below.
        # if "containers:manage" not in granted:
        #     raise PermissionError(
        #         f"app {slug!r} tier=container requires the 'containers:manage' "
        #         f"capability (high-risk — signed/marketplace apps only)")
        if "containers:manage" not in requested:
            raise PermissionError(
                f"app {slug!r} tier=container requires the 'containers:manage' permission "
                f"declared in its manifest")
        granted = list(dict.fromkeys(granted + ["containers:manage"]))
        if not self.containers.available:
            raise ContainerError(
                f"Tier-2 unavailable: no container engine socket configured "
                f"(AW_CONTAINER_SOCKET) — cannot load {slug!r}")

        rt = manifest.runtime
        image = str(rt.get("image", ""))
        port = rt.get("port")
        resources = rt.get("resources") or {}
        run_flags = rt.get("run_flags_needed") or rt.get("run_flags") or []
        # Resolve ${config.x} / ${env.X} so a container app's own settings
        # actually reach it — see containers.expand_env.
        env = expand_env(rt.get("env") or {}, config)
        volumes = self._container_volumes(manifest, package_dir)

        ctx = AppContext(
            runtime=self, app_id=slug, version=manifest.version,
            granted_permissions=granted, config=config, package_dir=package_dir,
        )
        loaded = LoadedApp(
            manifest=manifest, plugin=_ContainerPlugin(), ctx=ctx,
            package_dir=package_dir, granted_permissions=granted, config=config,
            signed=signed, module_prefix="",
        )

        self.containers.register(
            slug, image, port, run_flags=run_flags, resources=resources, env=env,
            volumes=volumes)
        self.journal.record(slug, "container:register", image,
                            {"port": port, "run_flags": run_flags, "resources": resources})
        try:
            if config.get("auto_start", True):
                # Blocking docker/podman API call (image pull + container run) —
                # offloaded to a thread so it can't freeze the workspace's single
                # asyncio event loop (every other request/WS/terminal) for
                # however long the pull takes. Reported live 2026-08-05.
                await asyncio.to_thread(self.containers.start, slug)
            proxy = ContainerReverseProxy(self.containers.base_url(slug))
            self._attach_mount(loaded, proxy)
        except Exception:
            # residue-free failed load: drop the Mount + stop the container +
            # forget journal entries for this app.
            if loaded.mount is not None and loaded.mount in self.host.router.routes:
                self.host.router.routes.remove(loaded.mount)
            if loaded.host_mount is not None and loaded.host_mount in self.host.router.routes:
                self.host.router.routes.remove(loaded.host_mount)
            await asyncio.to_thread(self.containers.stop_all_for, slug)
            self.journal.clear_app(slug)
            raise

        self._apps[slug] = loaded
        self._register_skills(loaded)
        self._register_tasks(loaded)
        self._register_agents(loaded)
        self._invalidate_openapi()
        log.info("apps: loaded container app %s v%s (image=%s)",
                 slug, manifest.version, image)
        return manifest

    def _container_host_bind_path(self, container_path: str) -> str:
        """Translate workspace-internal paths to the host bind-mount path.

        Tier-2 app containers are created by the host's rootless Podman socket.
        The workspace process sees installed apps under the container path
        (``/opt/aw-workspace``), but host Podman must receive the host-side bind
        source (``~/aw-workspace`` by default). Without this translation Podman
        tries to create ``/opt/aw-workspace`` on the host and fails for rootless
        users.
        """
        host_root = os.environ.get("AW_WORKSPACE_HOST_DIR", "").strip()
        container_root = os.path.realpath(
            os.environ.get("AW_WORKSPACE_CONTAINER_DIR", DEFAULT_WORKSPACE_CONTAINER_DIR)
        )
        real_path = os.path.realpath(container_path)
        if real_path == container_root:
            rel = ""
        elif real_path.startswith(container_root + os.sep):
            rel = os.path.relpath(real_path, container_root)
        else:
            return real_path
        if not host_root:
            if os.environ.get("AW_CONTAINER_SOCKET"):
                raise ContainerError(
                    "Tier-2 app volume source resolves inside "
                    f"{container_root}, but AW_WORKSPACE_HOST_DIR is not set. "
                    "The workspace is using a host container-engine socket, so "
                    "bind mounts must be translated to the host workspace path."
                )
            return real_path
        return os.path.realpath(os.path.join(host_root, rel))

    def _container_volumes(self, manifest: Manifest, package_dir: str) -> dict[str, dict]:
        """Resolve package-relative ``runtime.volumes`` into Docker binds.

        Shape:

            "runtime": {
              "volumes": [
                {"source": "back/config", "target": "/app/config", "mode": "rw"}
              ]
            }

        ``source`` normally stays inside the installed app package. The special
        source ``$AW_APPS_ROOT`` mounts the installed-apps root read-only so
        infrastructure apps can inspect sibling app packages without declaring
        arbitrary host paths. ``$AW_WORKSPACE_REPOS`` similarly mounts
        ``paths.repos_dir()`` (``/opt/aw-workspace/repos`` — where a workspace
        terminal clones repos for general dev work) read-only, so an app can
        read something already checked out there instead of needing its own
        redundant clone (Frederico, 2026-08-05: "eu quero apontar uma pasta e
        ele mapear" — kb should be able to map a repo already present in the
        workspace's own repos/ dir, not be forced through its own git clone).
        ``$AW_MCP_JSON`` mounts ONLY the workspace's root
        ``.mcp.json`` file (never a wider host path) read-write, so an app that
        is itself an MCP endpoint (today: aw-mcp-gateway) can register its own
        entry there on boot — gated behind the high-risk ``mcp:register-gateway``
        permission (ADR Decision 4 trust tiering) since it's the one volume kind
        that lets a container touch something outside its own package.

        ``$AW_APP_DATA`` mounts a per-app directory under the workspace home
        (``paths.workspace_home()/data/<app_id>``, the same durable-storage
        tree ``bin/``/``secrets/``/``skills/`` already live under — see
        ``paths.py``'s module docstring) read-write. Package-relative volumes
        look persistent but are NOT: ``uninstall`` (``fetch.remove_app_repo``)
        deletes the entire installed package directory, including anything
        mounted from inside it — found live 2026-08-03 when reinstalling
        aw-mcp-gateway silently wiped its ``back/config/gateway.json`` (the
        persisted bearer token) back to the repo's fresh default. Any app
        with real state to keep across uninstall/update (a database
        directory, generated config, tokens) should mount ``$AW_APP_DATA``
        instead. Gated behind ``fs:workspace-data`` (already the Tier-1
        equivalent — "read/write under the app's own data dir") rather than
        a new capability, since the blast radius is the same: an app's own
        namespaced slice, nothing else's.

        ``$AW_WORKSPACE_FOLDERS`` is the general form of ``$AW_WORKSPACE_REPOS``
        and the one that finally drops the repo binding: it expands to one bind
        per **mapped folder** (``src/api/folders.py``) at ``<target>/<name>``,
        so a user can point at any directory — not only a git checkout that
        happens to sit under ``repos/`` — and every app declaring the
        placeholder picks it up (Frederico 2026-08-08). Also reuses
        ``fs:workspace-data``; see ``$AW_KB_DIR`` below for the same rationale.

        ``$AW_KB_DIR`` mounts ``paths.workspace_home()/knowledge_base`` — unlike
        ``$AW_APP_DATA`` this is a shared, TOP-LEVEL, non-namespaced location
        (deliberately not ``data/<app_id>``), so the kb app's indexed markdown
        tree is directly browsable from a workspace terminal at
        ``/opt/aw-workspace/.aw-workspace/knowledge_base`` rather than buried
        inside its own private data dir. Reuses ``fs:workspace-data`` rather
        than a new capability since only the kb app is expected to declare it.
        """
        binds: dict[str, dict] = {}
        package_root = os.path.realpath(package_dir)
        for raw in manifest.runtime.get("volumes") or []:
            if not isinstance(raw, dict):
                raise ContainerError(
                    f"app {manifest.id!r} runtime.volumes entries must be objects")
            source = str(raw.get("source") or "").strip()
            target = str(raw.get("target") or "").strip()
            mode = str(raw.get("mode") or "rw").strip()
            if not source or not target:
                raise ContainerError(
                    f"app {manifest.id!r} runtime.volumes entries need source and target")
            if mode not in ("rw", "ro"):
                raise ContainerError(
                    f"app {manifest.id!r} volume mode must be 'rw' or 'ro'")
            if source == "$AW_APPS_ROOT":
                if mode != "ro":
                    raise ContainerError(
                        f"app {manifest.id!r} $AW_APPS_ROOT volume must be read-only")
                # mkdir on the container-local path — this process only has
                # that one mounted (see the bug note below) — THEN translate
                # for the bind-mount source the host's podman needs.
                os.makedirs(apps_root(), exist_ok=True)
                host_path = self._container_host_bind_path(apps_root())
                binds[host_path] = {"bind": target, "mode": mode}
                continue
            if source == "$AW_WORKSPACE_REPOS":
                if mode != "ro":
                    raise ContainerError(
                        f"app {manifest.id!r} $AW_WORKSPACE_REPOS volume must be read-only")
                # paths.repos_dir() — /opt/aw-workspace/repos, where a user/agent
                # working from a workspace terminal clones repos for general
                # dev work (git app's watchdog scans it too). Read-only so an
                # app with a legitimate reason to READ a repo already checked
                # out here (e.g. kb mapping it without a redundant clone of
                # its own) can, without being able to touch the user's actual
                # working checkout.
                os.makedirs(paths.repos_dir(), exist_ok=True)
                host_path = self._container_host_bind_path(paths.repos_dir())
                binds[host_path] = {"bind": target, "mode": mode}
                continue
            if source == "$AW_MCP_JSON":
                if mode != "rw":
                    raise ContainerError(
                        f"app {manifest.id!r} $AW_MCP_JSON volume must be read-write")
                if "mcp:register-gateway" not in manifest.permissions:
                    raise ContainerError(
                        f"app {manifest.id!r} $AW_MCP_JSON volume requires the "
                        f"'mcp:register-gateway' permission declared in its manifest")
                mcp_json_path = os.path.join(
                    os.path.realpath(os.environ.get(
                        "AW_WORKSPACE_CONTAINER_DIR", DEFAULT_WORKSPACE_CONTAINER_DIR)),
                    ".mcp.json")
                if not os.path.isfile(mcp_json_path):
                    with open(mcp_json_path, "w") as f:
                        f.write('{"mcpServers": {}}\n')
                host_path = self._container_host_bind_path(mcp_json_path)
                binds[host_path] = {"bind": target, "mode": mode}
                continue
            if source == "$AW_APP_DATA":
                if mode != "rw":
                    raise ContainerError(
                        f"app {manifest.id!r} $AW_APP_DATA volume must be read-write")
                if "fs:workspace-data" not in manifest.permissions:
                    raise ContainerError(
                        f"app {manifest.id!r} $AW_APP_DATA volume requires the "
                        f"'fs:workspace-data' permission declared in its manifest")
                data_dir = os.path.join(paths.workspace_home(), "data", manifest.id)
                os.makedirs(data_dir, exist_ok=True)
                host_path = self._container_host_bind_path(data_dir)
                binds[host_path] = {"bind": target, "mode": mode}
                continue
            if source == "$AW_KB_DIR":
                if mode != "rw":
                    raise ContainerError(
                        f"app {manifest.id!r} $AW_KB_DIR volume must be read-write")
                if "fs:workspace-data" not in manifest.permissions:
                    raise ContainerError(
                        f"app {manifest.id!r} $AW_KB_DIR volume requires the "
                        f"'fs:workspace-data' permission declared in its manifest")
                # Deliberately NOT namespaced under data/<app_id> like $AW_APP_DATA —
                # this mounts workspace_home()/knowledge_base directly, a shared,
                # top-level, workspace-visible location (Frederico 2026-08-04: wants
                # the KB's indexed markdown tree browsable at
                # /opt/aw-workspace/.aw-workspace/knowledge_base from a workspace
                # terminal, not buried inside the kb app's own private data dir).
                # Only the kb app is expected to ever declare this.
                kb_dir = os.path.join(paths.workspace_home(), "knowledge_base")
                os.makedirs(kb_dir, exist_ok=True)
                host_path = self._container_host_bind_path(kb_dir)
                binds[host_path] = {"bind": target, "mode": mode}
                continue
            if source == "$AW_WORKSPACE_FOLDERS":
                # The repo-binding escape hatch. Unlike every other placeholder
                # this one expands to *N* binds — one per folder the user mapped
                # (see src/api/folders.py) — landing each at <target>/<name>.
                # An app therefore declares ONE volume and transparently gains
                # every folder the user points at afterwards, with no manifest
                # change and no requirement that the folder be a git checkout
                # under repos/ (which is exactly what $AW_WORKSPACE_REPOS could
                # never express).
                #
                # Per-folder mode wins over the declared one, but is CLAMPED by
                # it: a manifest asking for "ro" can never be widened to "rw" by
                # a folder the user happened to map read-write, so the app's own
                # declaration stays the ceiling.
                if "fs:workspace-data" not in manifest.permissions:
                    raise ContainerError(
                        f"app {manifest.id!r} $AW_WORKSPACE_FOLDERS volume requires the "
                        f"'fs:workspace-data' permission declared in its manifest")
                for folder in self._mapped_folders():
                    folder_mode = "ro" if mode == "ro" else folder.get("mode", "ro")
                    src_path = folder["path"]
                    # No mkdir: a mapped folder is something that already exists
                    # (possibly only on the host — see folders.describe()'s
                    # `exists`), never something we conjure into being.
                    host_path = self._container_host_bind_path(src_path)
                    binds[host_path] = {
                        "bind": f"{target.rstrip('/')}/{folder['name']}",
                        "mode": folder_mode,
                    }
                continue
            if os.path.isabs(source):
                raise ContainerError(
                    f"app {manifest.id!r} volume source must be package-relative")
            local_path = os.path.realpath(os.path.join(package_root, source))
            if not (local_path == package_root or local_path.startswith(package_root + os.sep)):
                raise ContainerError(
                    f"app {manifest.id!r} volume source escapes the package dir")
            # mkdir BEFORE translating to the host bind-mount path — a
            # Tier-2 install (host podman socket, AW_WORKSPACE_HOST_DIR set)
            # runs this process inside the workspace container, which only
            # has the container-local path mounted; the translated host
            # path (e.g. /home/aw-remote-host/aw-workspace/...) isn't
            # visible here at all, so os.makedirs on it always raised
            # PermissionError/FileNotFoundError — found live installing
            # mcp-gateway on workspace "aw". The container-local mkdir
            # reaches the same directory via the bind mount either way.
            os.makedirs(local_path, exist_ok=True)
            host_path = self._container_host_bind_path(local_path)
            binds[host_path] = {"bind": target, "mode": mode}
        return binds

    # ---- mapped folders --------------------------------------------------

    @staticmethod
    def _mapped_folders() -> list[dict]:
        """The user's mapped folders (``src/api/folders.py``), or ``[]``.

        Imported lazily and defensively: ``_container_volumes`` also runs in
        unit tests and offline tooling with no DB behind it, and "no folders
        mapped" is the correct degradation there — an unreachable settings
        table must not make every Tier-2 app fail to load.
        """
        try:
            from src.api.folders import list_folders
            return list_folders()
        except Exception:  # noqa: BLE001 — no DB / not migrated yet
            log.debug("apps: mapped-folder lookup failed; treating as none", exc_info=True)
            return []

    def _declares_mapped_folders(self, manifest: Manifest) -> bool:
        return any(
            isinstance(v, dict) and str(v.get("source") or "").strip() == "$AW_WORKSPACE_FOLDERS"
            for v in (manifest.runtime.get("volumes") or [])
        )

    async def remap_folders(self) -> list[str]:
        """Recreate every container app that mounts ``$AW_WORKSPACE_FOLDERS``.

        Bind mounts are frozen at container creation, so the folder set an app
        sees is whatever existed when it last started. Called after a folder is
        mapped/unmapped so the change lands without the user having to know
        that, and restricted to apps that actually declare the placeholder —
        mapping a folder shouldn't bounce unrelated containers.

        Returns the slugs that were recreated (best-effort per app: one app
        failing to come back is logged, not propagated, so the rest still get
        the new mapping).
        """
        remapped: list[str] = []
        for slug, loaded in list(self._apps.items()):
            if loaded.manifest.tier != "container":
                continue
            if not self._declares_mapped_folders(loaded.manifest):
                continue
            try:
                volumes = self._container_volumes(loaded.manifest, loaded.package_dir)
                # set_volumes + start, NOT register: the app is already
                # registered and nothing but the bind set is changing.
                self.containers.set_volumes(slug, volumes)
                await asyncio.to_thread(self.containers.start, slug)
                remapped.append(slug)
                log.info("apps: remapped folders into %s (%d binds)", slug, len(volumes))
            except Exception:  # noqa: BLE001 — one app must not block the others
                log.exception("apps: could not remap folders into %s", slug)
        return remapped

    # ---- import isolation ----------------------------------------------

    def _import_plugin(self, manifest: Manifest, package_dir: str) -> tuple[Plugin, str]:
        """Load the entrypoint under a synthetic ``aw_apps.<slug>`` namespace.

        The per-app namespace package is rooted at ``package_dir`` (its
        ``__path__``), so a **flat** entrypoint (``plugin:AppPlugin``) and a
        **packaged** one (``git_app.plugin:GitAppPlugin`` with intra-package
        relative imports like ``from . import installer``) both resolve — and
        neither pollutes the global module namespace (everything lives under
        ``aw_apps.<slug>.*``, dropped wholesale on unload). No two apps collide
        even if both ship a ``plugin.py``.
        """
        module_path, _, class_name = manifest.entrypoint.partition(":")
        if not module_path or not class_name:
            raise RuntimeError(
                f"entrypoint {manifest.entrypoint!r} must be \"module:ClassName\"")

        module_prefix = f"aw_apps.{manifest.id}"
        # umbrella namespace package (shared, path-less) …
        if "aw_apps" not in sys.modules:
            umbrella = types.ModuleType("aw_apps")
            umbrella.__path__ = []  # namespace package
            sys.modules["aw_apps"] = umbrella
        # … and the per-app package rooted at this app's dir.
        base_pkg = types.ModuleType(module_prefix)
        base_pkg.__path__ = [package_dir]  # type: ignore[attr-defined]
        sys.modules[module_prefix] = base_pkg

        mod_name = f"{module_prefix}.{module_path}"
        try:
            module = importlib.import_module(mod_name)
        except Exception:
            self._unimport(module_prefix)
            raise

        cls = getattr(module, class_name, None)
        # Duck-typed Plugin contract: an app package can't import the host's
        # ``Plugin`` base (there is no installable host package), so we accept
        # any class exposing a callable ``activate`` rather than requiring a
        # subclass. Subclassing ``src.apps.base.Plugin`` stays a convenience.
        if cls is None or not isinstance(cls, type) or not callable(getattr(cls, "activate", None)):
            self._unimport(module_prefix)
            raise RuntimeError(
                f"entrypoint {manifest.entrypoint!r} must be a class with an "
                f"async activate(ctx) method"
            )
        return cls(), module_prefix

    def _unimport(self, module_prefix: str) -> None:
        """Best-effort unload: drop the app's modules from ``sys.modules``.

        C-extension modules can't truly unload — acceptable; a leaked module
        with no references is inert (ADR).
        """
        if not module_prefix:
            return
        for name in [n for n in sys.modules if n == module_prefix or n.startswith(module_prefix + ".")]:
            sys.modules.pop(name, None)

    def _invalidate_openapi(self) -> None:
        """Bust the host OpenAPI cache so /openapi.json reflects the change."""
        self.host.openapi_schema = None


class ManifestFileMissing(RuntimeError):
    pass
