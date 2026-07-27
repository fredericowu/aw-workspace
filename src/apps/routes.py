"""``/api/apps`` management surface + boot reconcile (F1).

Endpoints (all identity-gated):

- ``GET  /api/apps``                 — list loaded apps
- ``POST /api/apps/install``         — hot-install from a package dir (no restart)
- ``DELETE /api/apps/{slug}``        — hot-uninstall (drain + revert journal)
- ``GET  /api/apps/-/contributions`` — declarative frontend contributions

The install persists an ``AppInstall`` row so ``reconcile_on_boot`` reloads the
app when the workspace process restarts (ADR Decision 5, minimal). ``-`` is used
as the reserved control slug so it can never collide with an app slug (slugs
must start with a letter — see ``manifest.SLUG_RE``).
"""
from __future__ import annotations

import logging
import os

from fastapi import Body, Depends, FastAPI
from fastapi.responses import JSONResponse

from src.api.identity import require_identity
from src.apps.manifest import ManifestError, load_manifest
from src.apps.runtime import AppRuntime

log = logging.getLogger(__name__)


def _persist_install(slug, version, package_dir, granted, config) -> None:
    from src.api.db import get_session
    from src.api.models import AppInstall

    with get_session() as session:
        row = session.get(AppInstall, slug)
        if row is None:
            row = AppInstall(slug=slug, version=version, package_dir=package_dir,
                             granted_permissions=granted, config=config, enabled=True)
        else:
            row.version = version
            row.package_dir = package_dir
            row.granted_permissions = granted
            row.config = config
            row.enabled = True
        session.add(row)
        session.commit()


def _forget_install(slug: str) -> None:
    from src.api.db import get_session
    from src.api.models import AppInstall

    with get_session() as session:
        row = session.get(AppInstall, slug)
        if row is not None:
            session.delete(row)
            session.commit()


def register_apps_routes(app: FastAPI) -> AppRuntime:
    """Wire the plugin runtime + management routes onto ``app``.

    Returns the :class:`AppRuntime` (stored on ``app.state.app_runtime`` too).
    """
    runtime = AppRuntime(app)
    app.state.app_runtime = runtime

    @app.get("/api/apps")
    async def list_apps(identity: dict = Depends(require_identity)):
        return [
            {
                "slug": a.manifest.id,
                "name": a.manifest.name,
                "version": a.manifest.version,
                "tier": a.manifest.tier,
                "permissions": a.granted_permissions,
                "routes": bool(a.mount),
            }
            for a in (runtime.get(s) for s in runtime.loaded_slugs())
            if a is not None
        ]

    @app.post("/api/apps/install")
    async def install_app(data: dict = Body(...), identity: dict = Depends(require_identity)):
        package_dir = data.get("package_dir") or data.get("source")
        if not package_dir:
            return JSONResponse({"error": "package_dir is required"}, status_code=400)
        package_dir = os.path.abspath(package_dir)
        try:
            manifest = load_manifest(package_dir)
        except ManifestError as e:
            return JSONResponse({"error": f"invalid manifest: {e}"}, status_code=400)

        if runtime.is_loaded(manifest.id):
            return JSONResponse({"error": f"{manifest.id} already installed"}, status_code=409)

        granted = data.get("granted_permissions", manifest.permissions)
        config = data.get("config", {})
        try:
            await runtime.load(package_dir, granted_permissions=granted, config=config)
        except Exception as e:  # noqa: BLE001 — surface the load failure to the caller
            log.exception("apps: install failed for %s", manifest.id)
            return JSONResponse({"error": f"load failed: {e}"}, status_code=500)

        if data.get("persist", True):
            try:
                _persist_install(manifest.id, manifest.version, package_dir, granted, config)
            except Exception:
                log.exception("apps: could not persist install of %s", manifest.id)

        return {"slug": manifest.id, "version": manifest.version, "installed": True}

    @app.delete("/api/apps/{slug}")
    async def uninstall_app(slug: str, identity: dict = Depends(require_identity)):
        if not runtime.is_loaded(slug):
            return JSONResponse({"error": f"{slug} not installed"}, status_code=404)
        await runtime.unload(slug)
        try:
            _forget_install(slug)
        except Exception:
            log.exception("apps: could not forget install of %s", slug)
        return {"slug": slug, "uninstalled": True}

    @app.get("/api/apps/-/contributions")
    async def contributions(identity: dict = Depends(require_identity)):
        return runtime.contributions()

    return runtime


async def reconcile_on_boot(runtime: AppRuntime) -> None:
    """Load every enabled ``AppInstall`` row (ADR Decision 5 — minimal).

    Best-effort: a failed app is logged and skipped, never blocking boot.
    """
    from src.api.db import get_session
    from src.api.models import AppInstall
    from sqlmodel import select

    try:
        with get_session() as session:
            rows = list(session.exec(select(AppInstall).where(AppInstall.enabled == True)))  # noqa: E712
    except Exception:
        log.exception("apps: boot reconcile could not read app_installs")
        return

    for row in rows:
        if runtime.is_loaded(row.slug):
            continue
        try:
            await runtime.load(row.package_dir,
                               granted_permissions=row.granted_permissions,
                               config=row.config)
            log.info("apps: reloaded %s v%s on boot", row.slug, row.version)
        except Exception:
            log.exception("apps: failed to reload %s on boot", row.slug)
