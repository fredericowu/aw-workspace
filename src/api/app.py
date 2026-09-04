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

from src.api import boot_info
from src.api.db import create_all_tables, get_session, get_workspace_schema
from src.api.components import register_component_routes
from src.api.agent_routes import register_agent_routes, sync_on_boot
from src.api.folders import register_folder_routes
from src.api.guest_users import register_guest_user_routes
from src.api.marketplace import reconcile_sources_on_boot, register_marketplace_routes
from src.api.identity import _extract_token, decode_identity_jwt, require_identity
from src.api.models import Setting
from src.api.notifications import register_notification_routes
from src.api.observability import register_observability_routes
from src.api.skills_routes import register_skills_routes
from src.api.terminal import register_terminal_routes
from src.api.vpn import register_vpn_routes
from src.api.workspace_api_key import (
    get_or_create_workspace_api_key,
    regenerate_workspace_api_key,
)
from src.api.workspace_url import publish_workspace_api_url
from src.apps.routes import (_redis_coord_status, attach_on_boot, reconcile_on_boot,
                             register_apps_routes)

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


def _read_setting(key: str) -> dict:
    """Blocking read of one generic settings row. Never call from the event
    loop directly — see ``_write_setting``."""
    with get_session() as session:
        row = session.get(Setting, key)
        return {"key": key, "value": row.value if row else None}


def _write_setting(key: str, value: dict) -> None:
    """Blocking upsert of one generic settings row.

    Split out of the route handler so the handler can hand it to
    ``asyncio.to_thread``: ``src.api.db.get_session`` is a SYNCHRONOUS
    session over a sync psycopg driver, and each worker process has its
    own single event-loop thread — true regardless of
    ``AW_WORKSPACE_WORKERS`` (which may be >1 as of W1/W2). Called inline
    from an ``async def`` it blocks that thread for the whole DB
    round-trip, freezing every other in-flight request on the SAME worker
    — including ones touching no DB at all."""
    with get_session() as session:
        row = session.get(Setting, key)
        if row is None:
            row = Setting(key=key, value=value)
        else:
            row.value = value
        session.add(row)
        session.commit()


async def _is_boot_provisioner() -> bool:
    """W3: does THIS worker run the side-effecting boot reconcile?

    Two rules, in order:

    1. **At ``AW_WORKSPACE_WORKERS<=1`` — which is what ships — always yes**,
       without consulting Redis at all. There is no other worker to
       coordinate with, so any coordination here could only ever subtract
       behaviour, and the card's golden rule is that a single-worker
       deployment behaves identically. Getting this wrong is not theoretical:
       a plain ``cooldown_acquire`` gate made a single worker that restarted
       twice inside the cooldown window skip its own boot reconcile AND its
       ``agent sync`` — caught by ``test_skills_routes`` before this landed.

    2. **At >1, one worker per FLEET BOOT wins the claim.** Keyed on the
       parent pid, which is the uvicorn master every worker of one boot is
       forked from (``src/start/workspace.py`` uses the factory import string
       exactly when workers>1) — so a later restart is a different master and
       gets its own claim, rather than inheriting a window opened by the boot
       before it. The window only has to cover how far apart the workers of
       ONE boot start; the provisioning mutex covers the reconcile's own
       runtime.

    Redis unreachable — the normal case in every environment today — falls
    back to yes, i.e. today's behaviour: every worker converges independently,
    wastefully but correctly.
    """
    if int(os.environ.get("AW_WORKSPACE_WORKERS", "1") or "1") <= 1:
        return True
    from src.libs.redis_coord import cooldown_acquire

    try:
        return await cooldown_acquire(
            f"boot-apps-reconcile:{os.getppid()}", seconds=120.0)
    except Exception:
        log.exception("apps: could not claim the boot reconcile — this worker "
                      "will run its own, as it did before W3")
        return True


async def _boot_reconcile_and_sync(app: FastAPI) -> None:
    """Background half of boot: converge apps to the cloud registry, then
    mirror their skills — split out of the lifespan itself so it can run
    concurrently with request-serving instead of gating it. Both steps are
    documented never to raise (best-effort by design), so this is safe as a
    bare background task. See the call site in ``lifespan`` for why this
    isn't inline anymore.

    W3: only ONE worker runs the full (side-effecting) reconcile. It is the
    single most expensive thing this workspace does — a cold pass over ~47
    apps means ~47 GitHub fetches, pip installs, migrations and podman starts,
    measured at 450s+ live — and at ``AW_WORKSPACE_WORKERS>1`` every worker
    would otherwise run its own copy of it, concurrently, over the same venv
    and the same podman socket. The rest attach to whatever is already on disk
    (fast, no network, no side effects) and converge again off the
    ``apps:changed`` that pass publishes at its end. See src/apps/lifecycle.py.
    """
    provisioner = await _is_boot_provisioner()

    if provisioner:
        await reconcile_on_boot(app)
    else:
        log.info("apps: another worker is running this boot's app reconcile — "
                 "attaching to what is already installed instead")
        await attach_on_boot(app)
    # AFTER reconcile: an app's activate() copies its contributes.skills
    # into skills/ (AppRuntime._register_skills), so syncing earlier would
    # mirror a skills/ tree that's about to change.
    #
    # Skipped on a non-provisioning worker for the same reason its reconcile
    # was: `agent sync` is an exact-mirror rewrite of the shared skills/ tree
    # (and of the .claude/.cursor/.gemini mirrors), so N workers rewriting it
    # concurrently — while the provisioning worker is still materializing new
    # entries into it — produces a torn tree, not a redundant one.
    if provisioner:
        await sync_on_boot()


def create_app() -> FastAPI:
    create_all_tables()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # In-process broadcast needs the running loop to schedule sends from
        # sync callers (e.g. the apps facade) — see NotificationManager.set_loop.
        app.state.notification_mgr.set_loop(asyncio.get_running_loop())
        app.state.app_install_jobs.set_loop(asyncio.get_running_loop())
        # W4: same shape as the two calls above — starts the Redis relay
        # that makes a terminal_update fired on one worker reach a
        # /ws/status client connected to another.
        app.state.status_hub.set_loop(asyncio.get_running_loop())
        # Awaited, not fire-and-forget: each start_relay() only returns once
        # its psubscribe is confirmed, so no broadcast in the gap between
        # "server starts accepting requests" and "this worker's relay is up"
        # can be silently dropped (redis_coord's relay has no replay). Never
        # raises — see each start_relay()'s own docstring.
        await app.state.notification_mgr.start_relay()
        await app.state.app_install_jobs.start_relay()
        await app.state.status_hub.start_relay()
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
        # Restore any private-marketplace source Postgres forgot but the
        # local mirror still remembers (see marketplace.py's module
        # docstring — incident 2026-09-02). Local-only (Postgres + one
        # file, no network) so it's cheap enough to await inline, and MUST
        # run before the app reconciler below: a private app's image pull
        # needs its source's credential (registry_credential), so restoring
        # the source first is what lets a lost-Postgres crispal-style app
        # come back on the very first boot instead of needing a second,
        # manual `marketplace install --update` once someone notices.
        # W2: at AW_WORKSPACE_WORKERS>1 every worker would otherwise run
        # this at the same instant — harmless (each row is upserted only
        # if `get_source(sid) is None`, see reconcile_sources_on_boot's own
        # per-row check) but wasteful, N redundant Postgres round-trips for
        # convergence that only needs to happen once. `cooldown_acquire` is
        # a one-shot "first worker through the door wins" claim: whichever
        # worker wins the 30s window runs it, the rest skip. If Redis is
        # unreachable (true in every environment today — see
        # src/libs/redis_coord.py), fall back to today's behaviour and just
        # run it locally, same as the watchdog lease two blocks down.
        from src.libs.redis_coord import cooldown_acquire
        try:
            should_reconcile_sources = await cooldown_acquire(
                "boot-marketplace-reconcile", seconds=30.0
            )
        except Exception:
            should_reconcile_sources = True
        if should_reconcile_sources:
            reconcile_sources_on_boot()
        # W1: gate WatchdogSupervisor's periodic tasks (CLI healer,
        # mcp-gateway rescan, zombie reaper, any app-contributed
        # watchdog:tasks) on a Redis leader lease — at AW_WORKSPACE_WORKERS>1
        # every worker's supervisor registers the SAME tasks below via
        # reconcile_on_boot; without this gate that's N concurrent copies,
        # and N CLI healers racing the same apt/npm install path is
        # corruption, not just waste. resume()/pause() are idempotent and
        # WatchdogSupervisor defaults to leader (see its module docstring),
        # so if Redis is unreachable — no environment here has ever had the
        # F5a companion, see src/libs/redis_coord.py — on_acquire simply
        # never fires and this process keeps running its own tasks locally,
        # unlike aw-backend's F3 (a replica with no leader runs nothing).
        # At AW_WORKSPACE_WORKERS=1 (what ships) there's only ever one
        # candidate, so behaviour is identical either way — the lease is
        # still always attempted so a later scale-up to workers>1 needs no
        # further change here.
        #
        # The reachability check below is ONLY for the loud log line (the
        # lease itself is always started regardless of its result) — NOT
        # awaited inline, matching this lifespan's own "don't gate serving
        # on a slow check" rule two comments up (_boot_reconcile_and_sync).
        # _redis_coord_status carries its own 3s connect + 3s ping timeout;
        # inline here that's up to 6s added to EVERY app boot (every test
        # using create_app() included) whenever Redis is unreachable — which
        # is the CI runner's normal case today (no AW_REDIS_URL / Postgres-
        # style ephemeral service wired for it, see .github/workflows/test.yml).
        async def _log_watchdog_redis_status() -> None:
            redis_status = await _redis_coord_status()
            if not redis_status["reachable"]:
                log.warning(
                    "lifespan: Redis unreachable (%s) — WatchdogSupervisor has no "
                    "leader lease to win; every periodic task runs locally in "
                    "this process instead (today's single-worker behaviour; see "
                    "doctor's `redis` check)",
                    redis_status["note"],
                )

        asyncio.ensure_future(_log_watchdog_redis_status())

        async def _on_watchdog_lease_acquire() -> None:
            log.warning(
                "lifespan: acquired RedisLease(\"core\") — this worker runs the periodic watchdog tasks"
            )
            app.state.app_runtime.watchdog.resume()
            # W7 one-shot, DELETE ONE RELEASE AFTER W7 (together with
            # src/api/terminal_screen_sweep.py). `/opt/aw-workspace` is a bind
            # mount, so this deploy landed on a RUNNING container whose pre-W7
            # code left GNU screen servers behind with user shells inside them
            # — and nothing in terminal_manager lists or kills them any more.
            # Under the lease so only the leader sweeps; a no-op on any
            # container built from the post-W7 Dockerfile (no `screen` binary).
            from src.api.terminal_screen_sweep import sweep_orphaned_screens
            asyncio.ensure_future(asyncio.to_thread(sweep_orphaned_screens))

        async def _on_watchdog_lease_release() -> None:
            log.warning(
                "lifespan: released RedisLease(\"core\") — pausing this worker's periodic watchdog tasks"
            )
            app.state.app_runtime.watchdog.pause()

        from src.libs.redis_coord import RedisLease
        watchdog_lease = RedisLease(
            role="core",
            on_acquire=_on_watchdog_lease_acquire,
            on_release=_on_watchdog_lease_release,
        )
        await watchdog_lease.start()
        app.state.watchdog_lease = watchdog_lease
        # W3: subscribe to apps:changed BEFORE the boot reconcile starts, so
        # this worker cannot miss the broadcast the provisioning worker fires
        # at the end of its pass. redis_coord's relay is subscribe-then-listen
        # with no replay — a publish that lands before start_relay() is simply
        # not delivered, and the worker would then serve a stale app set until
        # its next restart. Never raises: with Redis unreachable it logs and
        # returns False, which at AW_WORKSPACE_WORKERS=1 costs nothing (there
        # is no other worker whose changes this would have carried).
        async def _on_apps_changed(payload: dict) -> None:
            await app.state.app_reconciler.converge_in_process()

        await app.state.app_lifecycle.start(_on_apps_changed)
        # Converge the running app set to the cloud registry, then (only
        # after) mirror contributes.skills — a fresh/recreated workspace
        # auto-reinstalls the user's apps this way (F3). Backgrounded, NOT
        # awaited here: this used to run inline before `yield`, which means
        # Starlette served ZERO requests — including /api/health — until it
        # finished. reconciler.reconcile() walks every configured app
        # SERIALLY, each GitHub fetch (src/apps/fetch.py) carrying its own
        # 60s timeout x 3 retries, so one slow/unreachable app could black
        # out the whole public edge for minutes on every redeploy (proven:
        # commit 89f7b99's redeploy stalled 4m14s this way, see bug
        # 3cf5bf3b-9510-8149-be2d-db20915f6872). The server now starts
        # accepting requests immediately; apps converge shortly after in the
        # background. reconcile_on_boot itself also bounds the reconcile
        # pass with an overall timeout, so a hung fetch can't stall
        # convergence forever either. Kept on app.state so it isn't
        # garbage-collected mid-flight (asyncio only holds a weak ref to a
        # bare fire-and-forget task).
        app.state.boot_reconcile_task = asyncio.ensure_future(_boot_reconcile_and_sync(app))
        yield
        # Cancel rather than leave it running past shutdown: reconcile_on_boot
        # walks every configured app serially, each network fetch (GitHub
        # tarball / catalog) carrying its own retries, and nothing here was
        # cancelling it on a short-lived process (e.g. a test's TestClient)
        # exiting mid-reconcile — see incident 2026-09-03 (commit e9c8745),
        # where exactly that kept a pytest process retrying real GitHub
        # fetches long after the test that started it had finished. This
        # bounds the *next* iteration of reconcile's per-app loop; a fetch
        # already in flight runs via asyncio.to_thread, whose worker thread
        # cancellation can't reach — real tests must not rely on this and
        # should stub reconcile_on_boot instead (see test_skills_routes.py).
        task = app.state.boot_reconcile_task
        if not task.done():
            task.cancel()
        # Best-effort: RedisLease.stop() re-raises whatever killed its
        # internal renew loop (e.g. a connection error from a Redis outage
        # that started after acquisition — redis_coord.py's _run() has no
        # broad except around the redis calls) when it awaits that already-
        # failed task. That's a shutdown-path detail of a lease we may never
        # have actually won; it must not block or crash this process's own
        # teardown.
        try:
            await app.state.watchdog_lease.stop()
        except Exception:
            log.exception("lifespan: watchdog_lease.stop() raised during shutdown")
        # W3: drop the apps:changed subscription + the install-job mirror's
        # client. Same best-effort posture as the lease above — a shutdown
        # must not hang on a Redis that has already gone away.
        try:
            await app.state.app_lifecycle.stop()
            await app.state.app_install_jobs.aclose()
        except Exception:
            log.exception("lifespan: app lifecycle teardown raised during shutdown")
        # W4: drop the three WS-registry Redis relays the same best-effort way.
        try:
            await app.state.notification_mgr.aclose()
        except Exception:
            log.exception("lifespan: notification relay teardown raised during shutdown")
        try:
            await app.state.status_hub.aclose()
        except Exception:
            log.exception("lifespan: status_hub relay teardown raised during shutdown")

    app = FastAPI(title="aw-workspace", version="0.1.0", lifespan=lifespan)

    # This process's own OTel graph (logs + traces to whatever Observability
    # currently resolves to) — unconditional and BEFORE add_middleware:
    # set_tracer_provider is one-shot and Starlette refuses middleware once
    # the app has started serving, so this can't be deferred until an app
    # (aw-app-signoz) is actually installed. See src/api/otel.py.
    from src.api.otel import init_otel
    init_otel(app)

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
            "boot_id": boot_info.boot_id(),
            "git_head": boot_info.git_head(),
            "started_at": boot_info.started_at(),
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
        return {"key": await asyncio.to_thread(get_or_create_workspace_api_key)}

    @app.post("/api/settings/workspace-api-key/regenerate")
    async def regenerate_workspace_api_key_route(identity: dict = Depends(require_identity)):
        return {"key": await asyncio.to_thread(regenerate_workspace_api_key)}

    # Terminal feature (strangler migration #1): PTY shells on this BYOD host.
    # Safe at AW_WORKSPACE_WORKERS>1 since W5 and, since W7, without the GNU
    # `screen` dependency: the PTY is still forked by exactly one worker (a
    # master fd cannot cross a process boundary), but its bytes travel over
    # two Redis Streams per session, so any worker can serve
    # /ws/terminal/<id>. So this is no longer the piece that pins the
    # workspace to one worker — W1 (watchdog), W2 (boot), W3 (app lifecycle),
    # W4 (WS registries) and W5/W7 (here) together cover the boot/runtime
    # state, and AW_WORKSPACE_WORKERS ships as 10 as of W6.
    # Caveat worth keeping in view: terminals degrade to a worker-owned PTY
    # wherever Redis is unreachable, which is single-worker behaviour and the
    # golden rule of the whole series — never "no Redis, no terminals". The
    # price W7 charges instead: a PTY now dies with its owning worker
    # (deploy, crash or recycle), where a screen used to survive one.
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

    # Settings > General > Skills: list/create/delete native skills and open
    # one in code-server. See src/api/skills_routes.py.
    register_skills_routes(app)

    # Settings > General > Users: CRUD for scoped guest logins, stored in this
    # workspace's own schema. The SPA calls this origin (apiBase.js rewrites
    # relative /api/* to api.<slug>.workspace.<apex>), which is why the
    # aw-backend copy of this feature never answered it. See
    # src/api/guest_users.py — admin CRUD only, no guest login yet.
    register_guest_user_routes(app)

    # Settings NEW > General > VPNs: stored VPN config profiles (upload, edit,
    # delete, Nord import). Phase 1 stores and validates; NOTHING dials — the
    # dialer is a Tier-2 app holding the `tun` host-power grant. These routes
    # live HERE, not on aw-backend, because apiBase.js rewrites the SPA's
    # relative /api/vpn/* to this origin. See src/api/vpn.py.
    register_vpn_routes(app)

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
        return await asyncio.to_thread(_read_setting, key)

    @app.put("/api/settings/{key}")
    async def put_setting(key: str, value: dict, identity: dict = Depends(require_identity)):
        await asyncio.to_thread(_write_setting, key, value)
        return {"key": key, "value": value, "schema": get_workspace_schema()}

    return app
