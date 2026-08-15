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
from src.apps import config_store
from src.apps.catalog import get_catalog, is_marketplace_app, list_tags
from src.apps.install_jobs import InstallJobs
from src.apps.manifest import ManifestError, load_manifest
from src.apps.reconciler import AppSpec, Reconciler
from src.apps.containers import expand_env
from src.apps.runtime import AppRuntime

log = logging.getLogger(__name__)



def _config_for_install(app_id: str, data: dict, runtime, reconciler) -> dict:
    """Config to install ``app_id`` with: the caller's if it sent one,
    otherwise whatever is already on record for that app.

    An install must never be a way to LOSE configuration. Looks at the
    running app first, then the local mirror (which survives an app whose
    install failed halfway and left nothing loaded — exactly the case that
    made this necessary). Only a genuinely new app starts empty.
    """
    incoming = data.get("config")
    if isinstance(incoming, dict) and incoming:
        return incoming

    loaded = runtime.get(app_id)
    if loaded is not None and getattr(loaded, "config", None):
        return dict(loaded.config)

    try:
        for row in reconciler.local.list():
            if (row.get("app_id") or row.get("id")) == app_id:
                cfg = row.get("config")
                if isinstance(cfg, dict) and cfg:
                    log.info("apps: install of %s inherited the config already on "
                             "record (the request carried none)", app_id)
                    return dict(cfg)
    except Exception:
        log.exception("apps: could not read the stored config for %s — "
                      "installing with an empty one", app_id)
    return {}



def _app_config_payload(loaded) -> dict:
    config = loaded.manifest.config_with_defaults(loaded.config)
    return {
        "slug": loaded.manifest.id,
        "config": config,
        "config_schema": loaded.manifest.effective_config_schema,
    }


def _coerce_config(schema: dict, incoming: dict) -> dict:
    props = schema.get("properties") or {}
    config: dict = {}
    for key, spec in props.items():
        if key not in incoming:
            continue
        value = incoming[key]
        if isinstance(spec, dict) and spec.get("type") == "boolean":
            value = bool(value)
        config[key] = value
    for key, value in incoming.items():
        if key not in config:
            config[key] = value
    return config


def _merge_config(previous: dict, incoming: dict) -> dict:
    """Fold ``incoming`` over ``previous``, so a PARTIAL save keeps the keys
    it did not mention.

    Saving used to replace the config wholesale: whatever the request carried
    became the whole config, and every key it omitted was dropped. That reads
    as "set these" but behaves as "the config is now exactly this", and the
    difference is invisible until something stops working. It cost real time
    twice on 2026-08-13 — writing four new crispal keys silently erased the
    four already there, and the gallery tools went back to "connection
    refused" with nothing to say why.

    An explicit ``None`` still REMOVES a key, so clearing a setting stays
    possible; a UI that posts the whole form is unaffected, since every key
    it manages is present either way.
    """
    merged = dict(previous or {})
    for key, value in (incoming or {}).items():
        if value is None:
            merged.pop(key, None)
        else:
            merged[key] = value
    return merged


async def _apply_runtime_config(runtime: AppRuntime, loaded, previous: dict) -> None:
    """Apply framework-owned config to already-loaded runtime state."""
    app_id = loaded.manifest.id
    if loaded.manifest.tier != "container":
        return

    # Config that feeds the container's environment (${config.x} in
    # runtime.env) only reaches the process at container-creation time, so a
    # save that changes it has to recreate the container. Without this the
    # user edits a setting, the panel says saved, and the app keeps running
    # on the old value until something else happens to restart it.
    declared_env = (loaded.manifest.runtime or {}).get("env") or {}
    new_env = expand_env(declared_env, loaded.config, app_id)
    # Sidecars read the SAME config (runtime.sidecars[].env supports the
    # identical ${config.x} placeholders), so a save that only recreated the
    # app's own container left them running on the old values — and for
    # aw-app-crispal the setting that matters most, site_url, is consumed by
    # a sidecar and by nothing else, so the save appeared to do nothing at all.
    sidecar_envs: list[tuple[str, dict]] = []
    for spec in loaded.manifest.sidecars:
        key = runtime.containers.sidecar_key(app_id, str(spec.get("name") or ""))
        declared = spec.get("env") or {}
        after = expand_env(declared, loaded.config, app_id)
        if after != expand_env(declared, previous, app_id):
            sidecar_envs.append((key, after))

    if new_env != expand_env(declared_env, previous, app_id) or sidecar_envs:
        restart = bool(loaded.config.get("auto_start", True))
        # Sidecars first, and unconditionally before the app: the app dials
        # them on startup, so recreating them underneath a running app is
        # what the ordering in _load_container already avoids.
        for key, env in sidecar_envs:
            changed = await asyncio.to_thread(runtime.containers.update_env, key, env)
            if changed and restart:
                log.info("apps: %s config changed sidecar %s env — restarting", app_id, key)
                await asyncio.to_thread(runtime.containers.start, key)
        changed = await asyncio.to_thread(runtime.containers.update_env, app_id, new_env)
        # Only restart what was already meant to be running — a config save
        # must not start a container the user had deliberately stopped.
        if changed and restart:
            log.info("apps: %s config changed its container env — restarting", app_id)
            await asyncio.to_thread(runtime.containers.start, app_id)
        return

    before = bool(previous.get("auto_start", True))
    after = bool(loaded.config.get("auto_start", True))
    if before == after:
        return
    if after:
        await asyncio.to_thread(runtime.containers.start, app_id)
    else:
        await asyncio.to_thread(runtime.containers.stop, app_id)


async def _reload_mcp_gateway(runtime: AppRuntime, *,
                              raise_on_failure: bool = False,
                              attempts: int = 3) -> None:
    """POST /reload on the installed mcp-gateway app's OWN internal
    container address — never the public app-proxy route, so this never
    "hairpins" out through the edge/Caddy just to call back in.
    ``containers.base_url()`` resolves the same podman-network hostname the
    reverse proxy itself targets (see ContainerSupervisor.base_url).

    X-AW-Identity-Sub satisfies the gateway's admin auth (same trust path
    /admin/config already accepts for a forwarded end-user identity) without
    aw-workspace needing to know the gateway's own bearer secret, which
    lives only in that container's own config/gateway.json.

    Best-effort by default: a missing/unreachable/not-yet-started gateway
    logs and does not fail the config save that triggered it — the app's own
    config change already landed either way, and the gateway will pick it up
    on its own next reload/restart regardless (the periodic rescan watchdog
    in ``AppRuntime.start_mcp_gateway_rescan`` is exactly that safety net).

    ``raise_on_failure=True`` inverts that for the watchdog itself, whose
    whole job IS this call: it needs the failure to reach the supervisor's
    backoff + ``last_error`` introspection rather than being swallowed.

    Retried a few times with backoff: ``is_loaded`` only means the
    gateway's CONTAINER was created (``containers.start()`` returned),
    not that its own FastAPI app has finished booting and is listening yet
    — confirmed live, this call raced a just-created gateway container and
    silently connection-refused on the first attempt during the SAME
    reconcile pass that just installed it."""
    if not runtime.is_loaded("mcp-gateway"):
        if raise_on_failure:
            raise RuntimeError("mcp-gateway app is not installed")
        return
    import asyncio

    import httpx
    base_url = runtime.containers.base_url("mcp-gateway")
    for attempt in range(1, attempts + 1):
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(f"{base_url}/reload",
                                         headers={"X-AW-Identity-Sub": "aw-workspace"})
                resp.raise_for_status()
            payload = resp.json()
            # Quiet on the steady state — the rescan watchdog runs every 5
            # minutes and an unchanged reload is the normal case, so only a
            # real diff is worth a log line.
            if payload.get("added") or payload.get("removed") or payload.get("changed"):
                log.info("apps: mcp-gateway reload triggered — %s", payload)
            else:
                log.debug("apps: mcp-gateway reload — no change (%s upstreams, %s tools)",
                          len(payload.get("upstreams") or []), payload.get("tools"))
            return
        except Exception:
            if attempt < attempts:
                # Capped so a long budget (the boot caller's) stays a patient
                # wait rather than an exponential one.
                await asyncio.sleep(min(1.0 * attempt, 5.0))
            else:
                log.exception("apps: failed to trigger mcp-gateway /reload after %d attempts", attempts)
                if raise_on_failure:
                    raise


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
                "category": a.manifest.category,
                "standalone_app": a.manifest.standalone_app,
                "icon": a.manifest.icon,
                "has_windows": bool(a.manifest.launchable_windows),
                "permissions": a.granted_permissions,
                "signed": a.signed,
                "routes": bool(a.mount),
                "has_config": a.manifest.has_config,
                "config_schema": a.manifest.effective_config_schema,
                "config": a.manifest.config_with_defaults(a.config),
                "settings_panels": a.manifest.settings_panels,
                "frontend": a.manifest.frontend,
                "managed_app": a.manifest.is_managed_app,
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
            # Inherit the config already on record unless the caller sends one.
            #
            # This used to be `data.get("config") or {}`, i.e. whatever the
            # request body happened to carry — so ANY caller that simply
            # didn't think to send it silently WIPED the app's configuration,
            # locally and (write_cloud defaults on) in the cloud registry too.
            # `aw-workspace-cli marketplace install` sends no config, so
            # reinstalling a configured app — the documented recovery when an
            # install fails halfway — erased it. Hit for real on crispal
            # 2026-08-13: a failed fetch left the app uninstalled, the
            # marketplace reinstall cleared ap_gallery_base/ap_token, and the
            # gallery tools went back to "connection refused" with nothing in
            # any log to say the config had been dropped.
            #
            # Exactly the shape of the `signed` bug documented just below —
            # an absent field read as an assertion instead of as silence.
            config=_config_for_install(app_id, data, runtime, reconciler),
            # DERIVED, never taken from the request body (ADR Decision 4 — the
            # cloud registry has computed it from catalog membership since
            # 2026-08-04, and this is the local twin of that rule).
            #
            # It used to be `bool(data.get("signed"))`, i.e. self-certified: a
            # side-loaded app could assert its own trust, while a caller that
            # simply didn't think to send the field got an UNSIGNED install of
            # a first-party catalog app. The second half is what bit — the
            # `aw-workspace-cli marketplace install` payload has no `signed`,
            # so `ui:code` (high-risk) was refused by filter_grants and the
            # app's entire frontend silently vanished: no window body, no nav
            # row, no titlebar actions, while the window chrome still drew, so
            # it read as a bug in the app. Hit for real on diff-tool,
            # 2026-08-12.
            signed=is_marketplace_app(app_id, repo),
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

    def _has_desired_row(slug: str) -> bool:
        """Is there a registry row for ``slug``, loaded or not?

        An app that FAILS to install still has one, and that is the case this
        exists for: reconcile retries it on every pass forever, logging the
        same failure, with nothing the user can do about it. Seen live
        2026-08-12 with two rows whose repo had gone away entirely — "app
        'remote-screen' has neither a repo to fetch nor an on-disk
        package_dir" on every boot.
        """
        for source in (reconciler.local, reconciler.cloud):
            lister = getattr(source, "list", None) or getattr(source, "list_desired", None)
            if lister is None or getattr(source, "configured", True) is False:
                continue
            try:
                if any((row or {}).get("app_id") == slug for row in lister()):
                    return True
            except Exception:  # noqa: BLE001 — a source being down must not block the other
                log.exception("apps: could not read desired state from %s", type(source).__name__)
        return False

    @app.delete("/api/apps/{slug}")
    async def uninstall_app(slug: str, purge_config: bool = False,
                            identity: dict = Depends(require_identity)):
        # Not `is_loaded` alone: that made a broken install unremovable, which
        # is precisely when you need to remove it. Reconciler.uninstall already
        # tolerates an unloaded app — it drops the mirror + registry rows and
        # the package dir either way.
        if not runtime.is_loaded(slug) and not _has_desired_row(slug):
            return JSONResponse({"error": f"{slug} not installed"}, status_code=404)
        summary = await reconciler.uninstall(slug)
        # Uninstall keeps the app's settings (and its $AW_APP_DATA dir) on
        # purpose — a delete + install is how an image gets rebuilt here, not
        # a statement that the configuration is unwanted. Throwing the
        # settings away needs saying so.
        summary["config_kept"] = not purge_config
        if purge_config:
            summary["config_purged"] = config_store.purge(slug)
        jobs.clear(slug)
        return summary

    @app.get("/api/apps/{slug}/config")
    async def get_app_config(slug: str, identity: dict = Depends(require_identity)):
        loaded = runtime.get(slug)
        if loaded is None:
            return JSONResponse({"error": f"{slug} not installed"}, status_code=404)
        return _app_config_payload(loaded)

    @app.post("/api/apps/{slug}/config")
    async def save_app_config(slug: str, data: dict = Body(...),
                              identity: dict = Depends(require_identity)):
        loaded = runtime.get(slug)
        if loaded is None:
            return JSONResponse({"error": f"{slug} not installed"}, status_code=404)

        schema = loaded.manifest.effective_config_schema
        previous = loaded.manifest.config_with_defaults(loaded.config)
        incoming = data.get("config") if isinstance(data.get("config"), dict) else data
        merged = _merge_config(loaded.config or {}, incoming or {})
        loaded.config = loaded.manifest.config_with_defaults(_coerce_config(schema, merged))
        loaded.ctx.config = dict(loaded.config)

        # Keep the durable snapshot current, so an uninstall + install (or a
        # reconcile that resolves this app from a config-less catalog row)
        # brings these values back instead of schema defaults. See
        # src/apps/config_store.py.
        config_store.save(slug, loaded.config)

        update_config = getattr(reconciler.local, "update_config", None)
        if callable(update_config):
            update_config(slug, loaded.config)
        else:
            spec = AppSpec(
                app_id=slug,
                version=loaded.manifest.version,
                granted_permissions=loaded.granted_permissions,
                config=loaded.config,
                signed=loaded.signed,
                package_dir=loaded.package_dir,
            )
            reconciler.local.upsert(spec, loaded.package_dir)
        if reconciler.cloud.configured:
            try:
                reconciler.cloud.put_desired(
                    slug,
                    version=loaded.manifest.version,
                    ref=None,
                    granted_permissions=loaded.granted_permissions,
                    config=loaded.config,
                    signed=loaded.signed,
                )
            except Exception:
                log.exception("apps: config save for %s did not reach the cloud registry", slug)

        await _apply_runtime_config(runtime, loaded, previous)

        # Let the app react to its own new config first (e.g. an app with
        # contributes.mcp.reload_on_save regenerates its own mcp.json on
        # disk here) — THEN hot-reload the gateway, so it scans the
        # already-updated file rather than racing it. Duck-typed getattr,
        # not a hard dependency on the Plugin base class — some in-repo test
        # plugins predate this hook and don't subclass Plugin.
        on_config_saved = getattr(loaded.plugin, "on_config_saved", None)
        if callable(on_config_saved):
            await on_config_saved(loaded.ctx)
        # Same broad gate install/uninstall use (Reconciler._app_touches_mcp):
        # any app whose config change can rewrite its mcp.json, not just the
        # ones that remembered to set contributes.mcp.reload_on_save.
        if reconciler._app_touches_mcp(loaded.manifest, loaded.package_dir):
            await _reload_mcp_gateway(runtime)

        return _app_config_payload(loaded)

    @app.post("/api/apps/{slug}/update")
    async def update_app(slug: str, identity: dict = Depends(require_identity)):
        """Update an installed app to the marketplace catalog's current version
        (ADR app-update-mechanism.md, Metade B). Resolves the app's catalog
        entry, writes a new ``desired`` row (version/ref from the catalog,
        config preserved from the current install; the grant carried here is
        the last effective one, and is re-derived from the NEW version's
        manifest by ``Reconciler.install`` — see its ``granted_req``) and
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
                    (a for a in get_catalog(force=True).get("apps", [])
                     if (a.get("id") or a.get("slug")) == slug),
                    None,
                )
                if catalog_entry is None:
                    jobs.mark_failed(slug, f"{slug} not found in catalog")
                    return

                catalog_version = catalog_entry.get("version") or ""
                catalog_repo = catalog_entry.get("repo")

                # Same version is NOT automatically nothing to do: the install
                # can still be at the wrong TRUST level, and trust decides
                # whether high-risk capabilities (`ui:code` above all) were
                # granted. A side-loaded app carries the version its manifest
                # declares, so side-loading the very version the catalog
                # publishes — the normal end of a dev loop, since CI bumped
                # the manifest — lands exactly here, and short-circuiting left
                # the app unsigned with no way to update out of it.
                trust_is_stale = loaded.signed != is_marketplace_app(slug, catalog_repo)
                if catalog_version and catalog_version == loaded.manifest.version \
                        and not trust_is_stale:
                    jobs.mark_installed(
                        slug,
                        {"app_id": slug, "status": "no-op", "version": loaded.manifest.version},
                    )
                    return

                spec = AppSpec(
                    app_id=slug,
                    version=catalog_version,
                    repo=catalog_repo,
                    ref=catalog_entry.get("ref") or "HEAD",
                    granted_permissions=loaded.granted_permissions,
                    config=loaded.config,
                    # DERIVED from the catalog entry we just resolved, exactly
                    # like the install route — NOT carried over from the
                    # install this one replaces.
                    #
                    # It used to be `signed=loaded.signed`, which made trust
                    # sticky: an app that was ever installed unsigned stayed
                    # unsigned through every future update, because this is
                    # the only path that rewrites the spec. Side-loading is
                    # the normal way to test an app change (`aw-workspace-cli
                    # sideload`, unsigned by construction — no repo, so
                    # `is_marketplace_app` is false), so the whole dev loop
                    # ended in an app permanently missing `ui:code`: no window
                    # body, no nav row, no titlebar actions, while the chrome
                    # still drew. The only escape was a full uninstall +
                    # install, which also wipes the app's config. Hit for real
                    # on `tasks`, 2026-08-13.
                    #
                    # We are updating TO a catalog version here — the app is
                    # about to be re-fetched from `catalog_repo` — so catalog
                    # membership is the right question to ask, and asking it
                    # again is what makes trust self-healing.
                    signed=is_marketplace_app(slug, catalog_repo),
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

    @app.get("/api/apps/-/doctor")
    async def doctor(identity: dict = Depends(require_identity)):
        """Everything that can be SILENTLY degraded, in one place.

        `GET /api/apps` answers "is it installed", `/api/health` answers "is
        the server up" — and a workspace can pass both while its CLIs are
        broken, an app's whole frontend is missing, and the MCP gateway serves
        none of its tools. Every one of those happened on 2026-08-12, none of
        them showed anywhere but a container log nobody reads.

        Three checks, each comparing what was DECLARED against what is
        actually true:

        * ``system_clis`` — declared vs passing their verify command.
        * ``permissions`` — requested in the manifest vs granted (a refused
          high-risk capability silently removes an app's entire frontend, so
          this is the one that reads as "the app is broken").
        * ``mcp`` — apps contributing tools vs upstreams the gateway serves.
        """
        clis = runtime.commands.system_cli_report()

        permissions = []
        for slug in runtime.loaded_slugs():
            loaded = runtime.get(slug)
            if loaded is None:
                continue
            requested = list(loaded.manifest.permissions)
            granted = set(loaded.granted_permissions)
            refused = [p for p in requested if p not in granted]
            if refused:
                permissions.append({
                    "app": slug, "refused": refused, "signed": loaded.signed,
                    "reason": "unsigned app — high-risk capabilities are not granted"
                              if not loaded.signed else "not granted",
                })

        mcp_apps = sorted(
            slug for slug in runtime.loaded_slugs()
            if os.path.isfile(os.path.join(runtime.get(slug).package_dir, "mcp.json"))
        ) if runtime.loaded_slugs() else []

        unhealthy = [c for c in clis if not c["healthy"]]
        return {
            "ok": not unhealthy and not permissions,
            "system_clis": {
                "total": len(clis),
                "unhealthy": unhealthy,
            },
            "permissions": permissions,
            "mcp": {
                "apps_contributing_tools": mcp_apps,
                "note": "compare with the gateway's own upstream list "
                        "(POST /reload returns it) — an upstream that failed to "
                        "connect serves zero tools until something reloads it",
            },
        }

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
        # No Cache-Control here previously meant browsers fell back to
        # heuristic caching (RFC 7234 §4.2.2) and kept serving a stale
        # bundle after an app update — found live 2026-08-04 when mcp-tools'
        # updated bundle (confirmed correct on disk + via a no-store fetch)
        # still rendered the old nav pill after a normal reload. `no-cache`
        # forces revalidation on every load (a cheap 304 via the ETag/
        # Last-Modified FileResponse already sets) instead of no caching at
        # all.
        return FileResponse(target, media_type=media, headers={"Cache-Control": "no-cache"})

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
    # Boot reconcile only (re)installs apps that aren't currently loaded; an
    # already-loaded app's system CLIs are never re-checked otherwise. Start
    # the runtime-owned healer so drift (a CLI removed outside the app's own
    # lifecycle) gets caught while the process keeps running, not just on the
    # next full workspace recreation.
    app.state.app_runtime.start_system_cli_healer()
    # At boot an already-running mcp-gateway will have scanned BEFORE the
    # inprocess apps above activated and (re)wrote their own mcp.json, and
    # nothing in that path is ordered — so reload once, now that they have.
    #
    # The reconcile above only fires its coalesced reload if it CHANGED
    # something, which a plain restart doesn't. Without this, the gateway sat
    # on a stale upstream set until the watchdog's first tick a full interval
    # (5 min) later, and every agent in that window got "Unknown tool" for
    # anything a Tier-1 app provides. Measured live 2026-08-12 after a
    # restart: aw-diff-tool, aw-presentation and whiteboard were all missing,
    # and one reload brought back 3 upstreams / 237 tools.
    #
    # In the BACKGROUND with a long retry budget, because the thing we're
    # waiting for is a container that this same boot has only just started:
    # `is_loaded` means created, not listening, and it takes tens of seconds
    # to serve. The default 3 attempts (~3s) always lost that race, and
    # blocking boot until it won would hold up the workspace's own API for no
    # good reason — so this rides alongside instead.
    #
    # Best-effort on purpose (no raise_on_failure): if the gateway never
    # answers, the watchdog below still covers it on its normal schedule.
    # Routing this through the watchdog with run_immediately=True instead
    # would be worse — a boot-race failure feeds its exponential backoff
    # (min(interval * 2**n, 1800s)), pushing the retry out to 10 minutes,
    # i.e. later than doing nothing.
    asyncio.ensure_future(_reload_mcp_gateway(app.state.app_runtime, attempts=20))
    # Safety net under the install/uninstall/config-save reload hooks and the
    # boot reload above.
    app.state.app_runtime.start_mcp_gateway_rescan()
