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
import re

from fastapi import Body, Depends, FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse

from src.api.identity import authorize_ws, require_identity
from src.apps import config_store
from src.apps import hostpower
from src.apps.catalog import get_catalog, is_marketplace_app, list_tags
from src.apps.install_jobs import InstallJobs
from src.apps.lifecycle import AppLifecycle
from src.apps.manifest import ManifestError, load_manifest
from src.apps.reconciler import AppSpec, Reconciler
from src.apps.containers import ContainerError, expand_env
from src.apps.runtime import AppRuntime

log = logging.getLogger(__name__)



#: A doctor check that hangs must not hang doctor.
_DOCTOR_CHECK_TIMEOUT_S = 20.0


def _autostart_disabled(runtime) -> list[dict]:
    """Installed apps that will NOT come up with the workspace.

    ``auto_start`` is stored per-workspace, never in code, and turning it off
    leaves no trace anywhere a human or an agent looks: the app is simply
    absent after a boot, ``status`` renders it exactly like one that crashed,
    and the manifest still says ``"default": true`` — so reading the code
    tells you the opposite of what the workspace will do.

    That cost a day on aw-app-browser (2026-08-23). Its ``auto_start`` had
    been false for long enough that nobody remembered setting it, so every
    cookie the aw-sync extension pushed was persisted into a browser that was
    never started, and the whole sync read as broken code.

    Like ``host_power``'s ``unused``, this is deliberately NOT counted as a
    failure — switching an app off is a legitimate thing to do. It is
    reported because it is the one thing about that setting nobody can see
    otherwise.
    """
    out: list[dict] = []
    for slug in runtime.loaded_slugs():
        loaded = runtime.get(slug)
        if loaded is None:
            continue
        # Only apps that actually have something to start: the framework adds
        # auto_start for managed apps, and an in-process app declares its own
        # when it supervises a service (aw-app-proxy's proxy-server).
        props = (loaded.manifest.effective_config_schema or {}).get("properties") or {}
        if "auto_start" not in props:
            continue
        if (loaded.config or {}).get("auto_start", True):
            continue
        out.append({"app": slug, "tier": loaded.manifest.tier})
    return out


async def _app_doctor_checks(runtime) -> list[dict]:
    """Ask every loaded app the self-checks it declared in ``contributes.doctor``.

    Called in-process against the app's own mounted ASGI app rather than over
    HTTP: core is already inside the trust boundary, and going out through the
    tunnel to ask itself a question would make ``doctor`` fail for the same
    reasons it exists to diagnose.

    An app that raises, times out or answers something unreadable is reported
    as a FAILING check, not skipped. "The app could not tell us" is exactly the
    silent degradation this command is for, and swallowing it would make doctor
    green because a check is broken — the worst possible failure mode for the
    tool you reach for when things are broken.
    """
    import httpx

    out: list[dict] = []
    for slug in runtime.loaded_slugs():
        loaded = runtime.get(slug)
        if loaded is None:
            continue
        checks = loaded.manifest.doctor_checks
        if not checks:
            continue
        inner = getattr(loaded.drainable, "app", None)
        for check in checks:
            row = {"app": slug, "label": check["label"], "route": check["route"]}
            if inner is None:
                out.append({**row, "ok": False,
                            "detail": "app declares a doctor check but mounted no routes"})
                continue
            try:
                transport = httpx.ASGITransport(app=inner)
                async with httpx.AsyncClient(transport=transport,
                                             base_url="http://app") as client:
                    resp = await client.get(check["route"], timeout=_DOCTOR_CHECK_TIMEOUT_S)
                body = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
                if resp.status_code != 200:
                    out.append({**row, "ok": False,
                                "detail": f"HTTP {resp.status_code} from {check['route']}"})
                    continue
                out.append({**row, "ok": bool(body.get("ok")), "detail": body})
            except Exception as exc:                       # noqa: BLE001
                out.append({**row, "ok": False,
                            "detail": f"{type(exc).__name__}: {exc}"})
    return out


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


async def _mcp_gateway_status(runtime: AppRuntime, *, expect_tools: bool,
                              expected: dict[str, str] | None = None) -> dict:
    """What the gateway itself reports serving, for doctor's ``mcp`` section.

    Before this, ``mcp.apps_contributing_tools`` only listed apps that ship an
    ``mcp.json`` — a pure presence check that can never fail, so ``doctor``
    could exit 0 while the gateway itself was unreachable or serving zero
    tools (confirmed live 2026-08-19: two upstreams were dead and doctor's
    exit code owed entirely to an unrelated architecture self-check). This
    calls the gateway's own unauthenticated ``GET /healthz`` — cheap, no
    admin token needed — and folds unreachable/zero-tools/dead-upstreams into
    ``degraded`` so it actually counts as a problem.

    ``expected`` maps upstream/server name -> owning app slug, i.e. the
    ``mcpServers`` keys read straight out of each app's own ``mcp.json`` —
    NOT a guess normalized from the app's slug (a gateway upstream is named
    after that key, which does not match the app slug 1:1; see
    ``aw-mcp-gateway``'s ``config.scan_app_mcp_servers``). Reading the real
    name from the source of truth lets this flag a SPECIFIC dead upstream by
    name, which a bare ``tools == 0`` aggregate count cannot: the 2026-08-19
    incident had exactly two dead upstreams while everything else kept the
    gateway's total tool count well above zero. ``expect_tools`` alone (no
    ``expected``) still catches the coarser total-collapse case.
    """
    # NOT ``runtime.is_loaded``: that tracks apps with an in-process plugin, and
    # mcp-gateway is ``tier: container`` — it has none, so is_loaded() is False
    # for a gateway that is installed, running and serving 600+ tools. Gating on
    # it reintroduced exactly the bug this function exists to kill: doctor
    # printed "mcp-gateway app is not installed — nothing to check" and returned
    # 0 while two upstreams were dead (seen live 2026-08-19, right after the
    # first fix shipped). The container registry is the predicate that answers
    # "is this app actually here" for both tiers.
    import httpx
    try:
        base_url = runtime.containers.base_url("mcp-gateway")
    except ContainerError:
        return {"reachable": None, "degraded": False,
                "note": "mcp-gateway app is not installed"}
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"{base_url}/healthz")
            resp.raise_for_status()
            payload = resp.json()
    except Exception as e:  # noqa: BLE001 — the failure itself IS the finding
        log.warning("apps: doctor could not reach mcp-gateway /healthz: %s", e)
        return {"reachable": False, "degraded": True,
                "note": f"gateway unreachable at {base_url}/healthz: {e}"}
    tools = payload.get("tools", 0)
    upstreams = payload.get("local_upstreams") or []
    live = set(upstreams)
    dead = sorted(
        ({"server": name, "app": app} for name, app in (expected or {}).items()
         if name not in live),
        key=lambda d: (d["app"], d["server"]),
    )
    degraded = bool(dead) or (expect_tools and tools == 0)
    if dead:
        note = ("%d upstream(s) declared but not live in the gateway: %s" %
                (len(dead), ", ".join(f"{d['server']} ({d['app']})" for d in dead)))
    elif degraded:
        note = ("gateway reachable but serving ZERO tools despite apps "
                 "declaring mcp.json — at least one upstream is dead")
    elif expected:
        note = "gateway reachable; every declared upstream is live"
    else:
        note = ("gateway reachable; compare local_upstreams against apps_contributing_tools "
                 "for per-app drift (names don't match 1:1 — see docstring)")
    return {
        "reachable": True,
        "tools": tools,
        "local_upstreams": upstreams,
        "dead_upstreams": dead,
        "degraded": degraded,
        "note": note,
    }


async def _redis_coord_status() -> dict:
    """Whether `src/libs/redis_coord.py` can actually reach a Redis right
    now (W0). That module (F5b: RedisBroadcaster/RedisLease/cooldown/
    RedisPollQueue) has zero consumers today, so nothing else would ever
    notice its resolved URL going dead — its hardcoded fallback
    (127.0.0.1:6379) answers nothing outside local dev sharing
    aw-sandbox's netns, and the per-workspace companion that would provide
    a real `AW_WORKSPACE_REDIS_URL` (F5a) is blocked in Need Human. This
    exists so that dead-end is visible in ``doctor`` instead of only
    surfacing the day a consumer is finally wired in and breaks silently.
    """
    import redis.asyncio as aioredis
    from src.libs.redis_coord import get_workspace_redis_url

    url = get_workspace_redis_url()
    safe_url = re.sub(r"://[^@/]+@", "://***@", url)
    client = aioredis.from_url(url, socket_connect_timeout=3)
    try:
        await asyncio.wait_for(client.ping(), timeout=3.0)
    except Exception as e:  # noqa: BLE001 — the failure itself IS the finding
        return {"reachable": False, "url": safe_url,
                "note": f"redis_coord cannot reach {safe_url}: {e}"}
    finally:
        await client.aclose()
    return {"reachable": True, "url": safe_url,
            "note": "redis_coord's resolved Redis answers PING"}


def register_apps_routes(app: FastAPI) -> AppRuntime:
    """Wire the plugin runtime + management routes onto ``app``.

    Returns the :class:`AppRuntime` (stored on ``app.state.app_runtime`` too);
    the :class:`Reconciler` is stored on ``app.state.app_reconciler``.
    """
    runtime = AppRuntime(app)
    # W3: the provision/attach seam. The reconciler publishes apps:changed
    # through it after every side-effecting half and takes the cross-worker
    # provisioning mutex from it; src/api/app.py's lifespan starts its relay
    # and points it back at reconciler.converge_in_process. Constructed even
    # at AW_WORKSPACE_WORKERS=1 — with one worker there is simply nobody else
    # subscribed, so behaviour is identical. See src/apps/lifecycle.py.
    lifecycle = AppLifecycle()
    reconciler = Reconciler(runtime, lifecycle=lifecycle)
    jobs = InstallJobs()
    app.state.app_runtime = runtime
    app.state.app_reconciler = reconciler
    app.state.app_install_jobs = jobs
    app.state.app_lifecycle = lifecycle

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
        # W3: the SHARED check, not the per-process one. At
        # AW_WORKSPACE_WORKERS>1 a double-click reaches two different workers,
        # and neither would see the other's in-flight job — two concurrent
        # fetches + pip installs of the same app. (The provisioning mutex
        # would serialize them, but serialized-and-duplicated is still wrong.)
        if await jobs.is_installing_shared(app_id):
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
        """Poll the progress of a background install kicked off above.

        W3: reads the shared job state, so a poll that lands on a worker other
        than the one running the install still answers. ``is_loaded`` is
        checked second and is per-process on purpose — once this worker has
        the app attached, "installed" is the truthful answer for the client
        talking to it.
        """
        shared = await jobs.get_shared(slug)
        if shared is not None:
            return shared
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

        # to_thread: LocalMirror writes go through the SYNCHRONOUS get_session
        # and this process serves every request on one event-loop thread
        # (AW_WORKSPACE_WORKERS=1), so an inline write stalls all of them.
        update_config = getattr(reconciler.local, "update_config", None)
        if callable(update_config):
            await asyncio.to_thread(update_config, slug, loaded.config)
        else:
            spec = AppSpec(
                app_id=slug,
                version=loaded.manifest.version,
                granted_permissions=loaded.granted_permissions,
                config=loaded.config,
                signed=loaded.signed,
                package_dir=loaded.package_dir,
            )
            await asyncio.to_thread(reconciler.local.upsert, spec, loaded.package_dir)
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
        # A Tier-2 app has no plugin and therefore no on_config_saved, so the
        # hook above can't be what re-renders its mcp.json — this is. Pasting
        # a token into the settings form has to take effect on save, not on
        # the next workspace boot.
        runtime._render_mcp_template(loaded)
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
                await asyncio.to_thread(reconciler.local.upsert, spec, loaded.package_dir)

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
        await asyncio.to_thread(reconciler.local.upsert, spec, loaded.package_dir)

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
        crashing the app.

        W1: ``leader`` reports whether THIS process currently runs the
        tasks at all — at ``AW_WORKSPACE_WORKERS>1`` only the worker holding
        ``RedisLease("core")`` is the leader, and every other worker's tasks
        come back with ``paused: true``. A paused task is a normal
        non-leader state, not a failure — see ``WatchdogSupervisor.pause()``.
        """
        return {"leader": runtime.watchdog.is_leader, "tasks": runtime.watchdog.snapshot()}

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
        * ``autostart`` — apps the manifest says start on boot vs apps this
          workspace's stored config will actually start (see
          ``_autostart_disabled``).
        * ``host_power`` — what the BYOD host opted into vs what each loaded
          app was actually granted. An app whose grant is empty while its
          manifest asks for one cannot be loaded at all (the load raises), so
          what this catches is the subtler shape: a host that granted power
          nothing is using, and — once ``all`` is in play — a host offering
          less than it was asked for.
        * ``redis`` — whether ``src/libs/redis_coord.py``'s resolved Redis
          URL (W0) actually answers PING. That module has zero consumers
          today, so nothing else would ever surface it silently resolving
          to a dead address.
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

        # Server name -> owning app slug, read from each app's own mcp.json
        # (not guessed from the slug — see _mcp_gateway_status's docstring)
        # so a specific dead upstream can be named, not just inferred from a
        # zero total.
        mcp_expected: dict[str, str] = {}
        for slug in mcp_apps:
            loaded = runtime.get(slug)
            try:
                with open(os.path.join(loaded.package_dir, "mcp.json"), encoding="utf-8") as f:
                    doc = json.load(f)
            except (OSError, ValueError):
                continue
            for name, spec in (doc.get("mcpServers") or {}).items():
                if isinstance(spec, dict) and spec.get("enabled") is False:
                    continue
                mcp_expected[name] = slug

        mcp_status = await _mcp_gateway_status(
            runtime, expect_tools=bool(mcp_apps), expected=mcp_expected)
        redis_status = await _redis_coord_status()

        host_offers = hostpower.host_grants()
        host_apps = []
        for slug in runtime.loaded_slugs():
            loaded = runtime.get(slug)
            if loaded is None or not loaded.manifest.host_power:
                continue
            host_apps.append({"app": slug, "grants": list(loaded.manifest.host_power)})
        claimed = {g for row in host_apps for g in row["grants"]}
        # Not a "problem" (it breaks nothing), but it is the one thing about
        # this feature nobody can see otherwise: a machine still carrying an
        # elevated grant for an app that was uninstalled months ago.
        unused = sorted(g for g in host_offers if g not in claimed)

        app_checks = await _app_doctor_checks(runtime)
        autostart_off = _autostart_disabled(runtime)

        unhealthy = [c for c in clis if not c["healthy"]]
        failing_app_checks = [c for c in app_checks if not c["ok"]]
        return {
            # redis_status is NOT folded into "ok": src/libs/redis_coord.py
            # has zero consumers today (W0), so its Redis being unreachable
            # breaks nothing yet — it's still reported below for a human (or
            # the CLI's own problem count) to see, just not treated as an
            # active production degradation until something depends on it.
            "ok": (not unhealthy and not permissions and not failing_app_checks
                   and not mcp_status["degraded"]),
            "app_checks": app_checks,
            "system_clis": {
                "total": len(clis),
                "unhealthy": unhealthy,
            },
            "permissions": permissions,
            "autostart": autostart_off,
            "host_power": {
                "host_offers": list(host_offers),
                "summary": hostpower.describe(host_offers),
                "apps": host_apps,
                "unused": unused,
            },
            "mcp": {
                "apps_contributing_tools": mcp_apps,
                **mcp_status,
            },
            "redis": redis_status,
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


# Overall cap on the boot reconcile pass (src/api/app.py runs this in the
# background, but a hung GitHub fetch — reconciler.reconcile() walks every
# configured app serially, each fetch (src/apps/fetch.py) carrying its own
# 60s httpx timeout x 3 retries — could otherwise stall app convergence
# indefinitely; see bug 3cf5bf3b-9510-8149-be2d-db20915f6872). 300s looked
# generous until it wasn't: this workspace's real boot reconcile (47 apps)
# ran past it live on 2026-09-02, leaving notion/aw-kanban and others
# unloaded until a manual POST /api/apps/reconcile finished the remaining
# apps in ~150s on top of the 300s already spent — a genuine full cold
# convergence needs ~450s+ here alone, before any app count growth. 1200s
# keeps this a real safety net (not a no-op) instead of a threshold normal
# operation already clears.
_BOOT_RECONCILE_TIMEOUT = 1200.0


async def reconcile_on_boot(app: FastAPI) -> None:
    """Reconcile to the cloud registry on startup (ADR Decision 5).

    This is what makes a recreated/fresh workspace auto-reinstall the user's
    apps: an empty loaded-set + a populated registry → the reconciler fetches +
    hot-loads each app. Falls back to the local mirror when the cloud isn't
    configured/reachable. Best-effort — never blocks boot: the caller runs
    this in the background (see src/api/app.py's lifespan), and this function
    itself bounds the reconcile pass with an overall timeout so a stalled
    network path can't run forever either.
    """
    reconciler: Reconciler = app.state.app_reconciler
    try:
        result = await asyncio.wait_for(reconciler.reconcile(), timeout=_BOOT_RECONCILE_TIMEOUT)
        log.info("apps: boot reconcile — %s", result)
    except asyncio.TimeoutError:
        log.error(
            "apps: boot reconcile exceeded %ss — giving up for this boot; "
            "apps may be partially converged. There is no periodic reconcile "
            "watchdog today — convergence only resumes on the next restart "
            "or a manual POST /api/apps/reconcile",
            _BOOT_RECONCILE_TIMEOUT,
        )
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
    # This container's CMD runs the workspace process with no init/tini
    # wrapper, so it IS PID 1 — nothing else ever waits() a process that ends
    # up reparented onto it. See src.api.terminal_manager.reap_pid1_orphans.
    app.state.app_runtime.start_zombie_reaper()
    # Re-evaluate what this process's own OTel exporters should send to —
    # a boot reconcile is exactly the moment "auto" mode's answer can have
    # changed underneath a stale in-memory target (e.g. aw-app-signoz was
    # uninstalled while this process was down). See src/api/otel.py.
    from src.api.otel import ensure_export_state
    ensure_export_state(app.state.app_runtime)


async def attach_on_boot(app: FastAPI) -> None:
    """W3: boot for a worker that is NOT running this boot's reconcile.

    The provisioning worker is doing the expensive, side-effecting pass
    (fetch/pip/migrate/podman). This one just makes itself able to SERVE the
    apps that are already installed, from the shared mirror + what is already
    on disk — no network, no pip, no podman, nothing written. Anything the
    provisioning worker adds or removes arrives afterwards as ``apps:changed``.

    Everything below the converge is deliberately identical to
    ``reconcile_on_boot``'s tail, minus the two shared side effects:

    * the mcp-gateway ``/reload`` — one HTTP call that re-dials every upstream
      for the whole workspace; the provisioning worker fires it once.
    * ``sync_on_boot`` — its caller skips that (see src/api/app.py).

    The three watchdog starters DO run here, in every worker, because that is
    exactly how W1 designed them: each worker registers the tasks and only the
    ``RedisLease("core")`` holder's supervisor actually spins them — so a
    worker that later wins the lease on failover already has them registered.
    """
    reconciler: Reconciler = app.state.app_reconciler
    try:
        result = await asyncio.wait_for(reconciler.converge_in_process(),
                                        timeout=_BOOT_RECONCILE_TIMEOUT)
        log.info("apps: boot attach — %s", result)
    except asyncio.TimeoutError:
        log.error("apps: boot attach exceeded %ss — this worker may be serving "
                  "only some of the installed apps until the next apps:changed",
                  _BOOT_RECONCILE_TIMEOUT)
    except Exception:
        log.exception("apps: boot attach failed")
    app.state.app_runtime.start_system_cli_healer()
    app.state.app_runtime.start_mcp_gateway_rescan()
    app.state.app_runtime.start_zombie_reaper()
    from src.api.otel import ensure_export_state
    ensure_export_state(app.state.app_runtime)
