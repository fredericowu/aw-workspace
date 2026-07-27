"""``/api/apps`` management surface + registry-driven reconcile (F1 + F3).

Endpoints (all identity-gated):

- ``GET  /api/apps``                 — list loaded apps
- ``POST /api/apps/install``         — hot-install (fetch repo | on-disk dir),
                                       writing the cloud registry (F3)
- ``DELETE /api/apps/{slug}``        — hot-uninstall (drain + revert journal +
                                       remove repo + drop registry rows)
- ``POST /api/apps/reconcile``       — converge to the cloud registry on demand
- ``POST /api/apps/install-my-apps`` — the "Install My Apps" backend flow: read
                                       the user's registry set + converge
- ``GET  /api/apps/-/contributions`` — frontend contributions (declarative + code)
- ``GET  /api/apps/-/catalog``       — marketplace catalog (available apps)
- ``GET  /api/apps/{slug}/ui/{path}``— serve a component-mode app's ESM bundle

The cloud registry (aw-backend ``app_installs``) is the source of truth (ADR
Decision 5). ``install`` writes there and hot-loads; the reconciler reads there
and converges — so a recreated workspace auto-reinstalls the user's apps. ``-``
is the reserved control slug (slugs must start with a letter — see
``manifest.SLUG_RE``) so it can never collide with an app slug.
"""
from __future__ import annotations

import logging
import os

from fastapi import Body, Depends, FastAPI
from fastapi.responses import FileResponse, JSONResponse

from src.api.identity import require_identity
from src.apps.catalog import get_catalog
from src.apps.manifest import ManifestError, load_manifest
from src.apps.reconciler import AppSpec, Reconciler
from src.apps.runtime import AppRuntime

log = logging.getLogger(__name__)


def register_apps_routes(app: FastAPI) -> AppRuntime:
    """Wire the plugin runtime + management routes onto ``app``.

    Returns the :class:`AppRuntime` (stored on ``app.state.app_runtime`` too);
    the :class:`Reconciler` is stored on ``app.state.app_reconciler``.
    """
    runtime = AppRuntime(app)
    reconciler = Reconciler(runtime)
    app.state.app_runtime = runtime
    app.state.app_reconciler = reconciler

    @app.get("/api/apps")
    async def list_apps(identity: dict = Depends(require_identity)):
        """Installed apps for the SPA's Apps view — includes what the UI needs
        to render each row: icon, whether a config gear applies (``has_config``
        + ``config_schema``), and the frontend contribution mode (Decision 3b)."""
        return [
            {
                "slug": a.manifest.id,
                "name": a.manifest.name,
                "description": a.manifest.description,
                "version": a.manifest.version,
                "tier": a.manifest.tier,
                "icon": a.manifest.icon,
                "permissions": a.granted_permissions,
                "signed": a.signed,
                "routes": bool(a.mount),
                "has_config": a.manifest.has_config,
                "config_schema": a.manifest.config_schema,
                "settings_panels": a.manifest.settings_panels,
                "frontend": a.manifest.frontend,
            }
            for a in (runtime.get(s) for s in runtime.loaded_slugs())
            if a is not None
        ]

    @app.post("/api/apps/install")
    async def install_app(data: dict = Body(...), identity: dict = Depends(require_identity)):
        """Install an app either from a git ``repo`` (fetched + pinned to ``ref``)
        or from an on-disk ``package_dir`` (the bundled PoC / a dev sideload).
        Hot-loads with no restart and records the cloud registry row."""
        repo = data.get("repo")
        package_dir = data.get("package_dir") or data.get("source")
        if not repo and not package_dir:
            return JSONResponse({"error": "repo or package_dir is required"}, status_code=400)

        app_id = data.get("app_id") or data.get("slug") or ""
        if package_dir and not repo:
            package_dir = os.path.abspath(package_dir)
            try:
                manifest = load_manifest(package_dir)
            except ManifestError as e:
                return JSONResponse({"error": f"invalid manifest: {e}"}, status_code=400)
            app_id = manifest.id

        if app_id and runtime.is_loaded(app_id):
            return JSONResponse({"error": f"{app_id} already installed"}, status_code=409)

        spec = AppSpec(
            app_id=app_id,
            version=data.get("version", "") or "",
            repo=repo,
            ref=data.get("ref") or "HEAD",
            granted_permissions=data.get("granted_permissions") or [],
            config=data.get("config") or {},
            signed=bool(data.get("signed", False)),
            package_dir=package_dir if not repo else None,
        )
        write_cloud = bool(data.get("persist", True))
        try:
            summary = await reconciler.install(spec, write_cloud=write_cloud)
        except ManifestError as e:
            return JSONResponse({"error": f"invalid manifest: {e}"}, status_code=400)
        except Exception as e:  # noqa: BLE001 — surface the failure to the caller
            log.exception("apps: install failed for %s", app_id or repo)
            return JSONResponse({"error": f"install failed: {e}"}, status_code=500)

        return {"installed": True, **summary}

    @app.delete("/api/apps/{slug}")
    async def uninstall_app(slug: str, identity: dict = Depends(require_identity)):
        if not runtime.is_loaded(slug):
            return JSONResponse({"error": f"{slug} not installed"}, status_code=404)
        summary = await reconciler.uninstall(slug)
        return summary

    @app.post("/api/apps/reconcile")
    async def reconcile_now(identity: dict = Depends(require_identity)):
        """Converge the running app set to the cloud registry on demand."""
        return await reconciler.reconcile()

    @app.post("/api/apps/install-my-apps")
    async def install_my_apps(identity: dict = Depends(require_identity)):
        """"Install My Apps" backend flow: read the user's registry set for this
        workspace and converge the runtime to it (install missing / remove
        extra). The frontend button (deferred) calls this. Returns the desired
        set plus what changed."""
        return await reconciler.reconcile()

    @app.get("/api/apps/-/contributions")
    async def contributions(identity: dict = Depends(require_identity)):
        return runtime.contributions()

    @app.get("/api/apps/-/catalog")
    async def catalog(identity: dict = Depends(require_identity),
                      refresh: bool = False):
        """The marketplace catalog (available apps) for the Marketplace panel."""
        return get_catalog(force=refresh)

    @app.get("/api/apps/{slug}/ui/{path:path}")
    async def app_ui_bundle(slug: str, path: str,
                            identity: dict = Depends(require_identity)):
        """Serve a component-mode app's prebuilt ESM bundle (Decision 3b).

        The SPA dynamic-``import()``s the hashed URL announced in
        ``contributions().frontend[].bundle_url``. Files are read from the app's
        own package under ``ui/dist/`` — path-traversal-guarded and scoped to
        the loaded app.
        """
        loaded = runtime.get(slug)
        if loaded is None:
            return JSONResponse({"error": f"{slug} not installed"}, status_code=404)
        ui_root = os.path.realpath(os.path.join(loaded.package_dir, "ui", "dist"))
        target = os.path.realpath(os.path.join(ui_root, path))
        if not target.startswith(ui_root + os.sep) or not os.path.isfile(target):
            return JSONResponse({"error": "not found"}, status_code=404)
        media = "text/javascript" if target.endswith(".js") else "application/octet-stream"
        return FileResponse(target, media_type=media)

    return runtime


async def reconcile_on_boot(app: FastAPI) -> None:
    """Reconcile to the cloud registry on startup (ADR Decision 5).

    This is what makes a recreated/fresh workspace auto-reinstall the user's
    apps: an empty loaded-set + a populated registry → the reconciler fetches +
    hot-loads each app. Falls back to the local mirror when the cloud isn't
    configured/reachable. Best-effort — never blocks boot.
    """
    reconciler: Reconciler = app.state.app_reconciler
    try:
        result = await reconciler.reconcile()
        log.info("apps: boot reconcile — %s", result)
    except Exception:
        log.exception("apps: boot reconcile failed")
