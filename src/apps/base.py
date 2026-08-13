"""Plugin lifecycle contract + capability-gated ``AppContext`` (ADR Decision 3/4).

A Tier-1 app's ``runtime.entrypoint`` points at a subclass of :class:`Plugin`.
The runtime instantiates it and calls ``activate(ctx)`` on load / ``deactivate()``
on unload. **All** side effects go through ``ctx`` facades — never by touching
FastAPI/host internals directly — which is what makes unload clean, auditable,
and journaled.

F2 generalizes the F1 ``routes`` gate to **every** capability: an app receives a
facade for a capability only if it was granted it. Touching an ungranted facade
raises :class:`PermissionError` **and journals a ``capability:denied`` entry** —
an honest, auditable boundary (in-process Python cannot stop determined malice;
that is what Tier 2 is for — see the ADR trust model). The effectful bodies of
the F4/F7 facades (``commands``/``secrets``/``db`` real backing) land in those
phases; F2 delivers the enforcement + audit trail.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any, Awaitable, Callable

if TYPE_CHECKING:  # avoid importing FastAPI at module import time for pure-unit tests
    from fastapi import FastAPI

    from src.apps.runtime import AppRuntime


class Plugin:
    """Base class for a Tier-1 in-process app plugin."""

    async def activate(self, ctx: "AppContext") -> None:  # pragma: no cover - overridden
        raise NotImplementedError

    async def deactivate(self) -> None:  # pragma: no cover - overridden
        return None

    async def on_config_saved(self, ctx: "AppContext") -> None:
        """Called by save_app_config AFTER ``ctx.config`` is updated to the
        newly-saved values — optional hook for a plugin that needs to react
        to a settings change beyond just reading ``ctx.config`` lazily next
        time (e.g. an app with ``contributes.mcp: true`` regenerating its
        own mcp.json on disk from the new config, which the MCP Gateway's
        POST /reload — triggered right after this returns, see
        routes.save_app_config — then picks up).

        No-op by default; a plugin only needs to override this if a config
        change has some side effect to apply beyond the config dict itself.
        """
        return None


class _Facade:
    """Base for a capability-gated facade.

    Its methods call ``self._ctx._enforce(<capability>)`` before doing anything;
    but a facade is only ever *handed out* by :class:`AppContext` after the same
    check, so a denied capability raises at attribute-access time already. The
    per-method check is defence in depth for facades an app cached.
    """

    def __init__(self, ctx: "AppContext") -> None:
        self._ctx = ctx


class RoutesFacade(_Facade):
    """``ctx.routes`` — mount an app's own FastAPI sub-application.

    Only handed to apps granted ``routes:register``. The app owns one sub-app
    mounted at ``/api/apps/<slug>``; registering it journals a ``route:mount``
    action so uninstall can reverse it.
    """

    #: Fallback only. The real list is COMPUTED from the routes core actually
    #: registered — see :meth:`_reserved_prefixes`. A hand-maintained copy of a
    #: truth you can read directly always drifts, and this one drifted in both
    #: directions at once: it claimed ``/settings`` and ``/uninstall``, which
    #: core does not serve (uninstall is ``DELETE`` on the bare slug), while
    #: missing ``/version``, which it does. Refusing ``/settings`` took four
    #: shipped apps — git, aws, google-cloud, notion — out of a live workspace
    #: on 2026-08-13; ``POST /api/apps/<slug>/settings`` is a real convention
    #: the SPA falls back to when ``/config`` 404s (AppConfigBody.jsx).
    RESERVED_PREFIXES = ("/ui", "/config", "/install-status", "/versions",
                         "/version", "/update")

    @staticmethod
    def _reserved_prefixes(runtime) -> tuple[str, ...]:
        """The ``/api/apps/<slug>/…`` sub-paths core has actually registered.

        Read off the host app's own router, so this cannot disagree with
        reality — add or remove a core route and the reserved set follows.
        """
        host = getattr(runtime, "host", None)
        marker = "/api/apps/{slug}"
        found: set[str] = set()
        for route in getattr(host, "routes", []) or []:
            path = getattr(route, "path", "")
            if not path.startswith(marker + "/"):
                continue
            sub = path[len(marker):]
            # First segment only: /ui/{path:path} reserves /ui, not the whole
            # parameterised path.
            first = sub.split("/")[1].split("{")[0]
            if first:
                found.add("/" + first)
        return tuple(sorted(found)) or RoutesFacade.RESERVED_PREFIXES

    def __init__(self, ctx: "AppContext") -> None:
        super().__init__(ctx)
        self._registered = False

    @classmethod
    def _reserved_conflicts(cls, subapp: "FastAPI",
                            reserved: tuple[str, ...] | None = None) -> list[str]:
        """Mount-relative paths in ``subapp`` that core already owns.

        Silent shadowing is the worst failure shape this framework has: every
        other route on the same sub-app answers normally, so it reads as a bug
        in the app, and it CANNOT be reproduced with
        ``TestClient(build_routes(...))`` because that mounts the sub-app with
        no core router in front of it. Only a real install surfaces it. Found
        the hard way on aw-app-tunnel + aw-app-remote-screen (2026-08-13),
        whose settings pages sat under ``/ui/`` and 404'd.
        """
        conflicts = []
        for route in getattr(subapp, "routes", []):
            path = getattr(route, "path", "")
            if not path:
                continue
            for prefix in (reserved or cls.RESERVED_PREFIXES):
                # Match on a SEGMENT boundary, never a raw string prefix:
                # "/configuration-wizard" is not "/config", and refusing it
                # would be a false positive that blocks a legitimate install.
                if path == prefix or path.startswith(prefix + "/"):
                    conflicts.append(path)
                    break
        return conflicts

    def register(self, subapp: "FastAPI") -> None:
        self._ctx._enforce("routes:register")
        if self._registered:
            raise RuntimeError(f"app {self._ctx.app_id!r} already registered a routes sub-app")
        reserved = self._reserved_prefixes(self._ctx._runtime)
        conflicts = self._reserved_conflicts(subapp, reserved)
        if conflicts:
            # Refuse rather than warn: a mounted-but-shadowed route is a
            # feature the developer believes shipped. Failing the install is
            # noisy, but it is noise at the only moment anyone is looking.
            raise RuntimeError(
                f"app {self._ctx.app_id!r} declares route(s) under a path core "
                f"already serves at /api/apps/<slug>/: {', '.join(sorted(conflicts))}. "
                f"Core wins the match, so these would be unreachable. Reserved: "
                f"{', '.join(reserved)} — use e.g. /panel/... instead."
            )
        self._ctx._runtime._mount(self._ctx.app_id, subapp)
        self._registered = True


class CommandsFacade(_Facade):
    """``ctx.commands`` — install commands / system CLIs into the workspace.

    Gated by ``commands:install``. Every install is journaled so uninstall
    reverses it (ADR Decision 7). Two surfaces (F4):

    * :meth:`install` — a ``<slug>-*`` command **shim** onto the persistent bin
      dir (``~/.aw-workspace/bin``, on PATH, survives restart); revert removes it.
    * :meth:`install_system_cli` — run the app's installer script to install a
      real system CLI (``git``/``gh``/``vim``/…) INTO the workspace. The scripts
      are idempotent (safe to re-run on every reconcile pass); ``uninstall``
      names the app's revert script, journaled once, run on uninstall.
    """

    def __init__(self, ctx: "AppContext") -> None:
        super().__init__(ctx)
        self._revert_recorded = False

    def install(self, name: str, exec_path: str) -> dict[str, Any]:
        self._ctx._enforce("commands:install")
        prefix = f"{self._ctx.app_id}-"
        if not name.startswith(prefix):
            raise ValueError(f"command name {name!r} must be namespaced under {prefix!r}")
        shim_path = self._ctx._runtime.commands.install_shim(
            name, self._ctx.package_dir, exec_path)
        self._ctx._runtime.journal.record(
            self._ctx.app_id, "command:install", name,
            {"exec": exec_path, "bin_path": shim_path})
        return {"command": name, "installed": True, "bin_path": shim_path}

    def install_system_cli(self, name: str, installer: str,
                           uninstall: str | None = None,
                           verify: str | bool | None = None) -> dict[str, Any]:
        """``verify`` decides what "installed" MEANS for this CLI.

        A shell command that must exit 0 to call the CLI healthy; ``None``
        (default) runs ``<name> --version``; ``False`` falls back to a
        presence check. Pass a command whenever presence can lie about
        working — see ``src/apps/commands.py`` for the git case that made
        this necessary — and prefer ``False`` over a check you know is
        meaningless, so the weakening is explicit rather than the default.
        """
        self._ctx._enforce("commands:install")
        output = self._ctx._runtime.commands.run_installer(
            self._ctx.package_dir, installer)
        self._ctx._runtime.commands.record_system_cli(
            self._ctx.app_id, name, self._ctx.package_dir, installer, verify=verify)
        self._ctx._runtime.journal.record(
            self._ctx.app_id, "system_cli:install", name, {"installer": installer})
        # The app's revert script is app-level (one uninstall.sh reverses every
        # CLI); journal it once so reverse replay runs it a single time.
        if uninstall and not self._revert_recorded:
            self._ctx._runtime.journal.record(
                self._ctx.app_id, "system_cli:revert-hook", uninstall, {})
            self._revert_recorded = True
        return {"cli": name, "installed": True, "output": output}


class SecretsFacade(_Facade):
    """``ctx.secrets`` — read/write the app's own secrets.

    Gated by ``secrets:own``. Backed by the workspace-side encrypted secret
    store (F4); the zero-knowledge store plugs in behind this same contract
    later (separate deferred card). Writes are journaled; uninstall purges the
    app's whole secret namespace.
    """

    def read(self, key: str) -> str | None:
        self._ctx._enforce("secrets:own")
        return self._ctx._runtime.secret_store.get(self._ctx.app_id, key)

    def write(self, key: str, value: str) -> dict[str, Any]:
        self._ctx._enforce("secrets:own")
        self._ctx._runtime.secret_store.put(self._ctx.app_id, key, value)
        self._ctx._runtime.journal.record(
            self._ctx.app_id, "secret:write", key, {})
        return {"key": key, "written": True}

    def delete(self, key: str) -> dict[str, Any]:
        self._ctx._enforce("secrets:own")
        removed = self._ctx._runtime.secret_store.delete(self._ctx.app_id, key)
        return {"key": key, "deleted": removed}

    def keys(self) -> list[str]:
        self._ctx._enforce("secrets:own")
        return self._ctx._runtime.secret_store.keys(self._ctx.app_id)


class DbFacade(_Facade):
    """``ctx.db`` — create/use app-owned workspace tables.

    Gated by ``db:own-tables``. Enforces the ``app__<slug>__`` prefix (ADR
    Decision 8) and creates tables in this workspace's own schema (F2 isolation).
    Each create journals ``db:table`` so uninstall drops it (F4).
    """

    def table(self, name: str) -> str:
        self._ctx._enforce("db:own-tables")
        from src.apps.db_tables import _validate
        return _validate(self._ctx.app_id, name)

    def create(self, name: str, columns_sql: str) -> str:
        self._ctx._enforce("db:own-tables")
        self._ctx._runtime.db_tables.create(self._ctx.app_id, name, columns_sql)
        self._ctx._runtime.journal.record(self._ctx.app_id, "db:table", name, {})
        return name

    def execute(self, name: str, sql: str, params: dict[str, Any] | None = None):
        self._ctx._enforce("db:own-tables")
        return self._ctx._runtime.db_tables.execute(
            self._ctx.app_id, name, sql, params)


class WatchdogFacade(_Facade):
    """``ctx.watchdog`` — register in-process periodic (watchdog) tasks.

    Gated by ``watchdog:tasks``. An app registers an ``async`` callable the
    runtime runs on a cadence (F6 Capability 3), distinct from ``service:manage``
    (subprocesses). Registration journals ``watchdog:register`` so uninstall
    cancels it (the runtime also cancels every task on unload before drain).
    """

    def register(self, task_id: str, fn: "Callable[[], Awaitable[Any]]",
                 interval_s: "float | Callable[[], float]",
                 run_immediately: bool = True) -> dict[str, Any]:
        self._ctx._enforce("watchdog:tasks")
        result = self._ctx._runtime.watchdog.register(
            self._ctx.app_id, task_id, fn, interval_s, run_immediately)
        self._ctx._runtime.journal.record(
            self._ctx.app_id, "watchdog:register", task_id, {})
        return result


class NotificationsFacade(_Facade):
    """``ctx.notify`` — fire a notification through the workspace notification engine.

    Gated by ``notifications:send``. Routes into the same ``NotificationManager``
    singleton ``/api/notify`` and ``/ws/notifications`` use (``src.api.notifications``,
    stashed on ``app.state.notification_mgr``), so an app-fired notification shows
    up in the SPA's notification panel exactly like an external ``POST /api/notify``.
    """

    def __call__(self, message: str, level: str = "info", title: str = "",
                 url: str = "", **kwargs: Any) -> dict[str, Any] | None:
        self._ctx._enforce("notifications:send")
        mgr = self._ctx._runtime.host.state.notification_mgr
        return mgr.add_notification(
            message=message, level=level, title=title,
            source=self._ctx.app_id, url=url, **kwargs,
        )


class ServicesFacade(_Facade):
    """``ctx.services`` — register + control a start/stop background service.

    Gated by ``service:manage``. Registration journals ``service:register`` so
    uninstall stops + drops it (F4 supervisor).
    """

    def register(self, service_id: str, start: str, autostart: bool = False) -> dict[str, Any]:
        self._ctx._enforce("service:manage")
        self._ctx._runtime.services.register(
            self._ctx.app_id, service_id, start, self._ctx.package_dir, autostart)
        self._ctx._runtime.journal.record(
            self._ctx.app_id, "service:register", service_id,
            {"start": start, "autostart": autostart})
        return {"service": service_id, "registered": True}

    def start(self, service_id: str) -> dict[str, Any]:
        self._ctx._enforce("service:manage")
        return self._ctx._runtime.services.start(self._ctx.app_id, service_id)

    def stop(self, service_id: str) -> dict[str, Any]:
        self._ctx._enforce("service:manage")
        return self._ctx._runtime.services.stop(self._ctx.app_id, service_id)

    def status(self, service_id: str) -> dict[str, Any]:
        self._ctx._enforce("service:manage")
        return self._ctx._runtime.services.status(self._ctx.app_id, service_id)


class ContainersFacade(_Facade):
    """``ctx.containers`` — run + control a Tier-2 sidecar container.

    Gated by ``containers:manage`` (high-risk → signed apps only). Registration
    journals ``container:register`` so uninstall stops + removes it (Phase 6
    supervisor). Mirrors :class:`ServicesFacade` but backed by the container
    engine socket instead of a subprocess.
    """

    def register(self, image: str, port: int, run_flags: list[str] | None = None,
                 resources: dict[str, Any] | None = None,
                 env: dict[str, str] | None = None,
                 autostart: bool = False) -> dict[str, Any]:
        self._ctx._enforce("containers:manage")
        self._ctx._runtime.containers.register(
            self._ctx.app_id, image, port, run_flags=run_flags,
            resources=resources, env=env, autostart=autostart)
        self._ctx._runtime.journal.record(
            self._ctx.app_id, "container:register", image,
            {"port": port, "run_flags": run_flags or [], "resources": resources or {}})
        return {"container": f"aw-app-{self._ctx.app_id}", "registered": True}

    def start(self) -> dict[str, Any]:
        self._ctx._enforce("containers:manage")
        return self._ctx._runtime.containers.start(self._ctx.app_id)

    def stop(self) -> dict[str, Any]:
        self._ctx._enforce("containers:manage")
        return self._ctx._runtime.containers.stop(self._ctx.app_id)

    def status(self) -> dict[str, Any]:
        self._ctx._enforce("containers:manage")
        return self._ctx._runtime.containers.status(self._ctx.app_id)

    def stop_all_for(self) -> None:
        self._ctx._enforce("containers:manage")
        self._ctx._runtime.containers.stop_all_for(self._ctx.app_id)


# capability -> (attribute name, facade class). One facade per single-capability
# contribution surface. Parameterised caps (config:extend:*, ui:slots:*) and the
# cross-app extension registry are F7 and not exposed as facades here.
_FACADES: dict[str, tuple[str, type[_Facade]]] = {
    "routes:register":  ("routes", RoutesFacade),
    "commands:install": ("commands", CommandsFacade),
    "secrets:own":      ("secrets", SecretsFacade),
    "db:own-tables":    ("db", DbFacade),
    "service:manage":   ("services", ServicesFacade),
    "watchdog:tasks":   ("watchdog", WatchdogFacade),
    "notifications:send": ("notify", NotificationsFacade),
    "containers:manage": ("containers", ContainersFacade),
}


class AppContext:
    """Capability-gated facade bundle handed to ``Plugin.activate``.

    Ungranted (or not-yet-implemented) capabilities raise on access — the
    facade is genuinely absent, not a no-op — and the denial is journaled.
    """

    def __init__(self, runtime: "AppRuntime", app_id: str,
                 version: str, granted_permissions: list[str],
                 config: dict[str, Any], package_dir: str) -> None:
        self._runtime = runtime
        self.app_id = app_id
        self.version = version
        self.granted_permissions = list(granted_permissions)
        self.config = dict(config)
        self.package_dir = package_dir
        self._deactivate_hooks: list[Callable[[], Awaitable[None] | None]] = []
        # instantiate only the facades the app was actually granted
        self._facades: dict[str, _Facade] = {
            attr: cls(self)
            for cap, (attr, cls) in _FACADES.items()
            if cap in self.granted_permissions
        }

    def has(self, capability: str) -> bool:
        return capability in self.granted_permissions

    def _enforce(self, capability: str) -> None:
        """Raise + journal if ``capability`` was not granted to this app.

        The single enforcement primitive every facade routes through. A denied
        access is recorded as an append-only ``capability:denied`` journal entry
        (the audit trail) before the :class:`PermissionError` propagates.
        """
        if capability not in self.granted_permissions:
            self._runtime.journal.record(
                self.app_id, "capability:denied", capability,
                {"version": self.version})
            raise PermissionError(
                f"app {self.app_id!r} was not granted {capability!r}")

    def _get_facade(self, attr: str, capability: str) -> _Facade:
        facade = self._facades.get(attr)
        if facade is None:
            self._enforce(capability)  # not granted -> journals + raises
        return facade  # type: ignore[return-value]

    @property
    def routes(self) -> RoutesFacade:
        return self._get_facade("routes", "routes:register")  # type: ignore[return-value]

    @property
    def commands(self) -> CommandsFacade:
        return self._get_facade("commands", "commands:install")  # type: ignore[return-value]

    @property
    def secrets(self) -> SecretsFacade:
        return self._get_facade("secrets", "secrets:own")  # type: ignore[return-value]

    @property
    def db(self) -> DbFacade:
        return self._get_facade("db", "db:own-tables")  # type: ignore[return-value]

    @property
    def services(self) -> ServicesFacade:
        return self._get_facade("services", "service:manage")  # type: ignore[return-value]

    @property
    def watchdog(self) -> WatchdogFacade:
        return self._get_facade("watchdog", "watchdog:tasks")  # type: ignore[return-value]

    @property
    def notify(self) -> NotificationsFacade:
        return self._get_facade("notify", "notifications:send")  # type: ignore[return-value]

    @property
    def containers(self) -> ContainersFacade:
        return self._get_facade("containers", "containers:manage")  # type: ignore[return-value]

    def on_deactivate(self, hook: Callable[[], Awaitable[None] | None]) -> None:
        """Register a callback run on unload (e.g. cancel a long-poll/WS)."""
        self._deactivate_hooks.append(hook)

    def _drain_hooks(self) -> list[Callable[[], Awaitable[None] | None]]:
        return list(self._deactivate_hooks)
