"""``/api/apps`` management surface + registry-driven reconcile (F1 + F3).

Endpoints (all identity-gated):

- ``GET  /api/apps``                 — list loaded apps
- ``POST /api/apps/install``         — kick off a hot-install (fetch repo |
                                       on-disk dir) in the BACKGROUND and
                                       return immediately (202, status
                                       "installing") — see install_jobs.py
- ``GET  /api/apps/{slug}/install-status`` — poll a background install's
                                       progress (installing/installed/failed)
- ``DELETE /api/apps/{slug}``        — hot-uninstall (drain + revert journal +
                                       remove repo + drop registry rows)
- ``POST /api/apps/reconcile``       — converge to the cloud registry on demand
- ``POST /api/apps/install-my-apps`` — the "Install My Apps" backend flow: read
                                       the user's registry set + converge
- ``GET  /api/apps/-/contributions`` — frontend contributions (declarative + code)
- ``GET  /api/apps/-/skills``        — index of app-contributed skills (``contributes.skills``)
- ``GET  /api/apps/-/catalog``       — marketplace catalog (available apps)
- ``GET  /api/apps/{slug}/ui/{path}``— serve a component-mode app's ESM bundle

The cloud registry (aw-backend ``app_installs``) is the source of truth (ADR
Decision 5). ``install`` writes there and hot-loads; the reconciler reads there
and converges — so a recreated workspace auto-reinstalls the user's apps. ``-``
is the reserved control slug (slugs must start with a letter — see
``manifest.SLUG_RE``) so it can never collide with an app slug.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os

from fastapi import Body, Depends, FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse

from src.api.identity import authorize_ws, require_identity
from src.apps.catalog import get_catalog, list_tags
from src.apps.install_jobs import InstallJobs
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
    jobs = InstallJobs()
    app.state.app_runtime = runtime
    app.state.app_reconciler = reconciler
    app.state.app_install_jobs = jobs

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
                "has_windows": bool(a.manifest.windows),
                "permissions": a.granted_permissions,
                "signed": a.signed,
                "routes": bool(a.mount),
                "has_config": a.manifest.has_config,
                "config_schema": a.manifest.config_schema,
                "settings_panels": a.manifest.settings_panels,
                "frontend": a.manifest.frontend,
                "requires_ui_refresh": a.manifest.requires_ui_refresh,
            }
            for a in (runtime.get(s) for s in runtime.loaded_slugs())
            if a is not None
        ]

    @app.post("/api/apps/install")
    async def install_app(data: dict = Body(...), identity: dict = Depends(require_identity)):
        """Kick off an install from a git ``repo`` (fetched + pinned to ``ref``)
        or an on-disk ``package_dir`` (the bundled PoC / a dev sideload) in the
        BACKGROUND and return immediately (202, status "installing") — the
        fetch + system-CLI ``apt install`` can take 30-60s, long enough for the
        BYOD tunnel to drop a held-open request before the response lands
        ("Failed to fetch" despite the install succeeding). Poll
        ``GET /api/apps/{slug}/install-status`` for progress; it ends
        "installed" or "failed" (+ error)."""
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

        if not app_id:
            return JSONResponse(
                {"error": "app_id is required for a repo install"}, status_code=400)
        if runtime.is_loaded(app_id):
            return JSONResponse({"error": f"{app_id} already installed"}, status_code=409)
        if jobs.is_installing(app_id):
            # already in flight (double-click, retried "Failed to fetch") — don't
            # start a second install, just report the job already running.
            return JSONResponse({"app_id": app_id, "status": "installing"}, status_code=202)

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
        job = jobs.start(app_id)

        async def _run_install() -> None:
            try:
                summary = await reconciler.install(spec, write_cloud=write_cloud)
                jobs.mark_installed(app_id, summary)
            except ManifestError as e:
                log.warning("apps: install failed for %s: %s", app_id, e)
                jobs.mark_failed(app_id, f"invalid manifest: {e}")
            except Exception as e:  # noqa: BLE001 — surfaced via the status endpoint
                log.exception("apps: install failed for %s", app_id)
                jobs.mark_failed(app_id, f"install failed: {e}")

        job.task = asyncio.create_task(_run_install())
        return JSONResponse({"app_id": app_id, "status": "installing"}, status_code=202)

    @app.get("/api/apps/{slug}/install-status")
    async def install_status(slug: str, identity: dict = Depends(require_identity)):
        """Poll the progress of a background install kicked off above."""
        job = jobs.get(slug)
        if job is not None:
            return job.as_dict()
        if runtime.is_loaded(slug):
            return {"app_id": slug, "status": "installed", "error": None, "summary": None}
        return JSONResponse({"error": f"{slug} not installed"}, status_code=404)

    @app.delete("/api/apps/{slug}")
    async def uninstall_app(slug: str, identity: dict = Depends(require_identity)):
        if not runtime.is_loaded(slug):
            return JSONResponse({"error": f"{slug} not installed"}, status_code=404)
        summary = await reconciler.uninstall(slug)
        jobs.clear(slug)
        return summary

    @app.post("/api/apps/{slug}/update")
    async def update_app(slug: str, identity: dict = Depends(require_identity)):
        """Update an installed app to the marketplace catalog's current version
        (ADR app-update-mechanism.md, Metade B). Resolves the app's catalog
        entry, writes a new ``desired`` row (version/ref from the catalog,
        config/granted_permissions preserved from the current install) and
        runs ``reconcile()`` in the BACKGROUND via the same job tracker as
        ``POST /api/apps/install`` — poll ``GET /api/apps/{slug}/install-status``
        for progress, same status contract as install."""
        loaded = runtime.get(slug)
        if loaded is None:
            return JSONResponse({"error": f"{slug} not installed"}, status_code=404)

        if jobs.is_installing(slug):
            return JSONResponse({"app_id": slug, "status": "installing"}, status_code=202)

        job = jobs.start(slug)

        async def _run_update() -> None:
            try:
                catalog_entry = next(
                    (a for a in get_catalog().get("apps", [])
                     if (a.get("id") or a.get("slug")) == slug),
                    None,
                )
                if catalog_entry is None:
                    jobs.mark_failed(slug, f"{slug} not found in catalog")
                    return

                catalog_version = catalog_entry.get("version") or ""
                if catalog_version and catalog_version == loaded.manifest.version:
                    jobs.mark_installed(
                        slug,
                        {"app_id": slug, "status": "no-op", "version": loaded.manifest.version},
                    )
                    return

                spec = AppSpec(
                    app_id=slug,
                    version=catalog_version,
                    repo=catalog_entry.get("repo"),
                    ref=catalog_entry.get("ref") or "HEAD",
                    granted_permissions=loaded.granted_permissions,
                    config=loaded.config,
                    signed=loaded.signed,
                )
                if reconciler.cloud.configured:
                    try:
                        reconciler.cloud.put_desired(
                            slug, version=spec.version, repo=spec.repo, ref=spec.ref,
                            granted_permissions=spec.granted_permissions, config=spec.config,
                            signed=spec.signed)
                    except Exception:
                        log.exception("apps: update of %s did not reach the cloud registry", slug)
                reconciler.local.upsert(spec, loaded.package_dir)

                summary = await reconciler.reconcile()
                for error in summary.get("errors", []):
                    if error.get("app_id") == slug:
                        jobs.mark_failed(slug, f"update failed: {error.get('error')}")
                        return
                jobs.mark_installed(slug, summary)
            except Exception as e:  # noqa: BLE001 — surfaced via the status endpoint
                log.exception("apps: update failed for %s", slug)
                jobs.mark_failed(slug, f"update failed: {e}")

        job.task = asyncio.create_task(_run_update())
        return JSONResponse({"app_id": slug, "status": "installing"}, status_code=202)

    @app.get("/api/apps/{slug}/versions")
    async def app_versions(slug: str, identity: dict = Depends(require_identity)):
        """Version history for pin/rollback — replaces "which version to
        install" as a config field (ADR: marketplace shouldn't force a
        config prompt on install). Lists the app repo's tagged releases so
        the Installed row's Version window can install an older/newer one
        directly instead of editing settings."""
        loaded = runtime.get(slug)
        if loaded is None:
            return JSONResponse({"error": f"{slug} not installed"}, status_code=404)

        catalog_entry = next(
            (a for a in get_catalog().get("apps", [])
             if (a.get("id") or a.get("slug")) == slug),
            None,
        )
        repo = catalog_entry.get("repo") if catalog_entry else None
        return {
            "slug": slug,
            "installed_version": loaded.manifest.version,
            "catalog_version": catalog_entry.get("version") if catalog_entry else None,
            "versions": list_tags(repo) if repo else [],
        }

    @app.post("/api/apps/{slug}/version")
    async def set_app_version(slug: str, data: dict = Body(...),
                              identity: dict = Depends(require_identity)):
        """Pin/rollback: reinstall the app at an explicit tagged ``ref``
        (older or newer than what's running — same mechanics either way),
        instead of always tracking the catalog's latest like ``/update``
        does. Same async job/status contract as install/update."""
        loaded = runtime.get(slug)
        if loaded is None:
            return JSONResponse({"error": f"{slug} not installed"}, status_code=404)

        ref = data.get("ref")
        if not ref:
            return JSONResponse({"error": "ref is required"}, status_code=400)
        version = data.get("version") or ref

        catalog_entry = next(
            (a for a in get_catalog().get("apps", [])
             if (a.get("id") or a.get("slug")) == slug),
            None,
        )
        repo = catalog_entry.get("repo") if catalog_entry else None
        if not repo:
            return JSONResponse({"error": f"{slug} not found in catalog"}, status_code=404)

        if jobs.is_installing(slug):
            return JSONResponse({"app_id": slug, "status": "installing"}, status_code=202)

        spec = AppSpec(
            app_id=slug,
            version=version,
            repo=repo,
            ref=ref,
            granted_permissions=loaded.granted_permissions,
            config=loaded.config,
            signed=loaded.signed,
        )
        if reconciler.cloud.configured:
            try:
                reconciler.cloud.put_desired(
                    slug, version=spec.version, repo=spec.repo, ref=spec.ref,
                    granted_permissions=spec.granted_permissions, config=spec.config,
                    signed=spec.signed)
            except Exception:
                log.exception("apps: version pin of %s did not reach the cloud registry", slug)
        reconciler.local.upsert(spec, loaded.package_dir)

        job = jobs.start(slug)

        async def _run_version() -> None:
            try:
                summary = await reconciler.reconcile()
                jobs.mark_installed(slug, summary)
            except Exception as e:  # noqa: BLE001 — surfaced via the status endpoint
                log.exception("apps: version pin failed for %s", slug)
                jobs.mark_failed(slug, f"version pin failed: {e}")

        job.task = asyncio.create_task(_run_version())
        return JSONResponse({"app_id": slug, "status": "installing"}, status_code=202)

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

    @app.get("/api/apps/-/skills")
    async def skills(identity: dict = Depends(require_identity)):
        """Index of ``contributes.skills`` from every loaded app — a pointer
        list (id/description/SKILL.md path), not the skill content itself."""
        return {"skills": runtime.skills_index()}

    @app.get("/api/apps/-/watchdog")
    async def watchdog_tasks(identity: dict = Depends(require_identity)):
        """Introspection over every app's registered watchdog tasks (F6 Cap 3):
        per-task last_run / last_ok / last_error / consecutive_failures /
        next_run — so a wedged poller (e.g. gh logged out) is visible without
        crashing the app."""
        return {"tasks": runtime.watchdog.snapshot()}

    @app.get("/api/apps/-/catalog")
    async def catalog(identity: dict = Depends(require_identity),
                      refresh: bool = False):
        """The marketplace catalog (available apps) for the Marketplace panel."""
        return get_catalog(force=refresh)

    @app.websocket("/ws/apps/install-status")
    async def install_status_stream(websocket: WebSocket):
        """Live push for background install progress (Marketplace panel).

        On connect, sends a snapshot of every tracked job (so a client that
        opens/refreshes mid-install catches up immediately), then streams a
        message for every subsequent status transition (installing → installed
        | failed). ``AppsMarketplace.jsx`` falls back to polling
        ``GET /api/apps/{slug}/install-status`` if this connection drops."""
        claims = authorize_ws(websocket)
        if not claims:
            await websocket.accept()
            await websocket.close(code=4401, reason="unauthorized")
            return
        await websocket.accept()
        for job in jobs.all_active():
            await websocket.send_text(json.dumps({"type": "app_install_status", "job": job}))
        jobs.add_listener(websocket)
        try:
            while True:
                await websocket.receive_text()
        except WebSocketDisconnect:
            pass
        finally:
            jobs.remove_listener(websocket)

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
