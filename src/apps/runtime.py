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
import logging
import os
import sys
from dataclasses import dataclass, field
from typing import Any

from fastapi import FastAPI
from starlette.routing import Mount
from starlette.types import Receive, Scope, Send

import types

from src.apps.base import AppContext, Plugin
from src.apps.capabilities import filter_grants
from src.apps.commands import CommandInstaller
from src.apps.journal import ActionJournal
from src.apps.manifest import Manifest, load_manifest
from src.apps.secret_store import SecretStore
from src.apps.services import ServiceSupervisor

log = logging.getLogger(__name__)

DEFAULT_DRAIN_TIMEOUT = float(os.environ.get("AW_APPS_DRAIN_TIMEOUT", "10"))


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
                 drain_timeout: float = DEFAULT_DRAIN_TIMEOUT) -> None:
        self.host = host
        self.journal = journal or ActionJournal()
        self.drain_timeout = drain_timeout
        self._apps: dict[str, LoadedApp] = {}
        self._lock = asyncio.Lock()
        self._loading: LoadedApp | None = None  # set during activate() for _mount
        # F4 effect backends the capability facades route through.
        self.commands = CommandInstaller()
        self.services = ServiceSupervisor()
        self.secret_store = SecretStore()
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
                windows.append({"app": slug, **win})
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
        if manifest.tier != "inprocess":
            raise ValueError(f"F1 runtime only loads tier=inprocess (got {manifest.tier!r})")
        slug = manifest.id
        if slug in self._apps:
            raise ValueError(f"app {slug!r} is already loaded")

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

        # 2. Signal long-poll/WS handlers, then drain in-flight requests.
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

        # 3. Plugin teardown.
        try:
            await loaded.plugin.deactivate()
        except Exception:
            log.exception("apps: deactivate() failed for %s", slug)

        # 4. Replay the journal in reverse — actually REVERT each side effect
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

        drainable = _DrainableApp(subapp)
        mount = Mount(f"/api/apps/{app_id}", app=drainable)
        # Mutation happens on the event loop (single process) — the list append
        # is atomic w.r.t. request matching; no free-threading hazard.
        self.host.router.routes.append(mount)
        loaded.mount = mount
        loaded.drainable = drainable
        self.journal.record(app_id, "route:mount", f"/api/apps/{app_id}",
                            {"version": loaded.manifest.version})

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
