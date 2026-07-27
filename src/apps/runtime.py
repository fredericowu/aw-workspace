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

from src.apps.base import AppContext, Plugin
from src.apps.journal import ActionJournal
from src.apps.manifest import Manifest, load_manifest

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

    # ---- introspection --------------------------------------------------

    def is_loaded(self, slug: str) -> bool:
        return slug in self._apps

    def loaded_slugs(self) -> list[str]:
        return list(self._apps)

    def get(self, slug: str) -> LoadedApp | None:
        return self._apps.get(slug)

    def contributions(self) -> dict[str, Any]:
        """Declarative frontend contributions for ``GET /api/apps/-/contributions``."""
        windows: list[dict[str, Any]] = []
        nav: list[dict[str, Any]] = []
        for app in self._apps.values():
            for win in app.manifest.windows:
                windows.append({"app": app.manifest.id, **win})
            for entry in app.manifest.nav:
                nav.append({"app": app.manifest.id, **entry})
        return {"windows": windows, "nav": nav}

    # ---- load / unload --------------------------------------------------

    async def load(self, package_dir: str, granted_permissions: list[str] | None = None,
                   config: dict[str, Any] | None = None) -> Manifest:
        """Validate, import, and activate an app from its package dir (hot)."""
        manifest = load_manifest(package_dir)
        if manifest.tier != "inprocess":
            raise ValueError(f"F1 runtime only loads tier=inprocess (got {manifest.tier!r})")
        slug = manifest.id
        if slug in self._apps:
            raise ValueError(f"app {slug!r} is already loaded")

        granted = granted_permissions if granted_permissions is not None else list(manifest.permissions)
        cfg = config or {}

        plugin, module_prefix = self._import_plugin(manifest, package_dir)
        ctx = AppContext(
            runtime=self, app_id=slug, version=manifest.version,
            granted_permissions=granted, config=cfg, package_dir=package_dir,
        )
        loaded = LoadedApp(
            manifest=manifest, plugin=plugin, ctx=ctx, package_dir=package_dir,
            granted_permissions=granted, config=cfg, module_prefix=module_prefix,
        )

        # _mount (called from within activate via ctx.routes.register) attaches
        # to the app currently loading.
        self._loading = loaded
        try:
            await plugin.activate(ctx)
        except Exception:
            self._loading = None
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

        # 4. Replay the journal in reverse (F1: route mounts, already unmounted
        #    above — this is the audit-complete + residue-free step) and unimport.
        for entry in self.journal.reverse_for(slug):
            log.debug("apps: reverting %s %s for %s", entry.kind, entry.target, slug)
        self.journal.clear_app(slug)
        self._unimport(loaded.module_prefix)
        del self._apps[slug]
        log.info("apps: unloaded %s", slug)

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
        """Load the entrypoint under a synthetic ``aw_apps.<slug>`` namespace."""
        module_path, _, class_name = manifest.entrypoint.partition(":")
        rel = module_path.replace(".", os.sep) + ".py"
        file_path = os.path.join(package_dir, rel)
        if not os.path.isfile(file_path):
            raise ManifestFileMissing(f"entrypoint module not found: {rel}")

        module_prefix = f"aw_apps.{manifest.id}"
        mod_name = f"{module_prefix}.{module_path}"
        spec = importlib.util.spec_from_file_location(mod_name, file_path)
        if spec is None or spec.loader is None:
            raise RuntimeError(f"cannot build import spec for {file_path}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[mod_name] = module
        try:
            spec.loader.exec_module(module)
        except Exception:
            sys.modules.pop(mod_name, None)
            raise

        cls = getattr(module, class_name, None)
        # Duck-typed Plugin contract: an app package can't import the host's
        # ``Plugin`` base (there is no installable host package), so we accept
        # any class exposing a callable ``activate`` rather than requiring a
        # subclass. Subclassing ``src.apps.base.Plugin`` stays a convenience.
        if cls is None or not isinstance(cls, type) or not callable(getattr(cls, "activate", None)):
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
