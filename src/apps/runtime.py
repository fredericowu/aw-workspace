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
from typing import Any

from fastapi import FastAPI
from starlette.routing import Mount, get_route_path
from starlette.types import Receive, Scope, Send

import types

from src.apps import paths
from src.apps.base import AppContext, Plugin
from src.apps.capabilities import filter_grants
from src.apps.commands import CommandInstaller
from src.apps.containers import ContainerError, ContainerSupervisor
from src.apps.journal import ActionJournal
from src.apps.manifest import Manifest, load_manifest
from src.apps.proxy import ContainerReverseProxy
from src.apps.secret_store import SecretStore
from src.apps.services import ServiceSupervisor
from src.apps.skills import SkillError, SkillsRegistry
from src.apps.watchdog import WatchdogSupervisor

log = logging.getLogger(__name__)

DEFAULT_DRAIN_TIMEOUT = float(os.environ.get("AW_APPS_DRAIN_TIMEOUT", "10"))

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
    """Verify the identity JWT on an HTTP scope (bearer header or apex cookie)."""
    from src.api.identity import decode_identity_jwt
    headers = _headers_dict(scope)
    auth = headers.get(b"authorization", b"").decode()
    if auth.lower().startswith("bearer "):
        token = auth[7:].strip()
    else:
        token = _cookie_value(headers.get(b"cookie", b"").decode(), _ID_COOKIE)
    return decode_identity_jwt(token) if token else None


def _default_verify_ws(scope: Scope) -> dict | None:
    """Verify the identity JWT on a WebSocket handshake (``?token=`` then cookie).

    A browser cannot set custom headers on a WS, so the token comes from the
    query param first, then the apex ``aw_id_jwt`` cookie — same order as
    ``src.api.identity.authorize_ws``.
    """
    from urllib.parse import parse_qs

    from src.api.identity import decode_identity_jwt
    qs = parse_qs(scope.get("query_string", b"").decode())
    token = (qs.get("token") or [""])[0]
    if not token:
        headers = _headers_dict(scope)
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

    v1: **all** app routes are guarded — no ``public:`` escape hatch (webhooks
    are a later manifest extension), EXCEPT the ``routes:local`` bypass below.
    Verifiers are injectable for tests.

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
                 local_paths: "list[str] | None" = None) -> None:
        self.app = app
        self._verify_http = verify_http or _default_verify_http
        self._verify_ws = verify_ws or _default_verify_ws
        self._local_paths = frozenset(local_paths or [])

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
            claims = self._verify_http(scope)
            if claims is None:
                await self._send_401(send)
                return
            scope["aw_identity"] = claims
        elif stype == "websocket":
            if self._local_bypass(scope):
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
        self._db_tables: Any = None  # lazy — only apps granted db:own-tables need it

    @property
    def db_tables(self) -> Any:
        """Lazily built so a pure-unit runtime never imports the PG engine layer."""
        if self._db_tables is None:
            from src.apps.db_tables import DbTables
            self._db_tables = DbTables()
        return self._db_tables

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

    def skills_index(self) -> list[dict[str, Any]]:
        """Index of every ``contributes.skills`` entry for ``GET /api/apps/-/skills``.

        Points at the symlink :meth:`load` registered into the shared skills
        dir (:func:`src.apps.paths.skills_dir`) — no SKILL.md content is
        duplicated here, just the pointer an agent runtime can read.
        """
        out: list[dict[str, Any]] = []
        for app in self._apps.values():
            slug = app.manifest.id
            for entry in app.manifest.skills:
                skill_id = entry.get("id")
                if not skill_id:
                    continue
                link_path = os.path.join(paths.skills_dir(), f"{slug}__{skill_id}")
                out.append({
                    "app": slug,
                    "id": skill_id,
                    "description": entry.get("description", ""),
                    "skill_md_path": os.path.join(link_path, "SKILL.md"),
                    "registered": os.path.islink(link_path),
                })
        return out

    def _register_skills(self, loaded: LoadedApp) -> None:
        """Symlink each ``contributes.skills`` entry into the shared skills index.

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
                link_path = self.skills.register(slug, skill_id, loaded.package_dir, path)
            except SkillError:
                log.exception("apps: failed to register skill %r for %s", skill_id, slug)
                continue
            self.journal.record(slug, "skill:register", skill_id, {"link_path": link_path})

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
            self.journal.clear_app(slug)
            self._unimport(module_prefix)
            raise
        self._loading = None

        self._apps[slug] = loaded
        self._register_skills(loaded)
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
                self._revert_entry(entry, loaded)
            except Exception:
                log.exception("apps: revert of %s %s failed for %s",
                              entry.kind, entry.target, slug)
        # Purge the app's secret namespace unconditionally (no residue even if a
        # secret was written in a prior process whose in-memory journal is gone).
        try:
            self.secret_store.purge(slug)
        except Exception:
            log.exception("apps: purging secrets for %s failed", slug)

        self.journal.clear_app(slug)
        self._unimport(loaded.module_prefix)
        del self._apps[slug]
        log.info("apps: unloaded %s", slug)

    def _revert_entry(self, entry: Any, loaded: LoadedApp) -> None:
        """Reverse a single journaled side effect (uninstall replay, F4)."""
        kind = entry.kind
        if kind == "command:install":
            self.commands.remove_shim(entry.payload.get("bin_path", ""))
        elif kind == "system_cli:revert-hook":
            self.commands.run_revert(loaded.package_dir, entry.target)
        elif kind == "db:table":
            self.db_tables.drop(loaded.manifest.id, entry.target)
        elif kind == "service:register":
            self.services.stop_all_for(loaded.manifest.id)
        elif kind == "container:register":
            self.containers.stop_all_for(loaded.manifest.id)
        elif kind == "watchdog:register":
            # Idempotent with the explicit cancel_all_for in unload() above.
            self.watchdog.cancel_all_for(loaded.manifest.id)
        elif kind == "skill:register":
            self.skills.unregister(entry.payload.get("link_path", ""))
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
        guarded = (IdentityGuard(drainable, local_paths=_local_paths_for(loaded))
                   if self.guard_identity else drainable)
        mount = Mount(f"/api/apps/{app_id}", app=guarded)
        # Mutation happens on the event loop (single process) — the list append
        # is atomic w.r.t. request matching; no free-threading hazard.
        self.host.router.routes.append(mount)
        loaded.mount = mount
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
        if "containers:manage" not in granted:
            raise PermissionError(
                f"app {slug!r} tier=container requires the 'containers:manage' "
                f"capability (high-risk — signed/marketplace apps only)")
        if not self.containers.available:
            raise ContainerError(
                f"Tier-2 unavailable: no container engine socket configured "
                f"(AW_CONTAINER_SOCKET) — cannot load {slug!r}")

        rt = manifest.runtime
        image = str(rt.get("image", ""))
        port = rt.get("port")
        resources = rt.get("resources") or {}
        run_flags = rt.get("run_flags_needed") or rt.get("run_flags") or []

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
            slug, image, port, run_flags=run_flags, resources=resources)
        self.journal.record(slug, "container:register", image,
                            {"port": port, "run_flags": run_flags, "resources": resources})
        try:
            self.containers.start(slug)
            proxy = ContainerReverseProxy(self.containers.base_url(slug))
            self._attach_mount(loaded, proxy)
        except Exception:
            # residue-free failed load: drop the Mount + stop the container +
            # forget journal entries for this app.
            if loaded.mount is not None and loaded.mount in self.host.router.routes:
                self.host.router.routes.remove(loaded.mount)
            self.containers.stop_all_for(slug)
            self.journal.clear_app(slug)
            raise

        self._apps[slug] = loaded
        self._invalidate_openapi()
        log.info("apps: loaded container app %s v%s (image=%s)",
                 slug, manifest.version, image)
        return manifest

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
