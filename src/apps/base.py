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

    def __init__(self, ctx: "AppContext") -> None:
        super().__init__(ctx)
        self._registered = False

    def register(self, subapp: "FastAPI") -> None:
        self._ctx._enforce("routes:register")
        if self._registered:
            raise RuntimeError(f"app {self._ctx.app_id!r} already registered a routes sub-app")
        self._ctx._runtime._mount(self._ctx.app_id, subapp)
        self._registered = True


class CommandsFacade(_Facade):
    """``ctx.commands`` — install commands/CLIs onto the persistent bin dir.

    Gated by ``commands:install``. F2 enforces the grant and journals the
    ``command:install`` action (so uninstall can remove it); the actual write to
    ``~/.aw-workspace/bin`` is F4.
    """

    def install(self, name: str, exec_path: str) -> dict[str, Any]:
        self._ctx._enforce("commands:install")
        prefix = f"{self._ctx.app_id}-"
        if not name.startswith(prefix):
            raise ValueError(f"command name {name!r} must be namespaced under {prefix!r}")
        self._ctx._runtime.journal.record(
            self._ctx.app_id, "command:install", name, {"exec": exec_path})
        return {"command": name, "installed": True}


class SecretsFacade(_Facade):
    """``ctx.secrets`` — read/write the app's own secrets.

    Gated by ``secrets:own``. F2 enforces the grant; the zero-knowledge store
    resolution is F4, so the granted bodies journal the intent and return a
    placeholder rather than a real value.
    """

    def read(self, key: str) -> None:
        self._ctx._enforce("secrets:own")
        return None  # F4: resolve the reference against the zero-knowledge store

    def write(self, key: str, value: str) -> dict[str, Any]:
        self._ctx._enforce("secrets:own")
        self._ctx._runtime.journal.record(
            self._ctx.app_id, "secret:write", key, {})
        return {"key": key, "written": True}


class DbFacade(_Facade):
    """``ctx.db`` — create/use app-owned workspace tables.

    Gated by ``db:own-tables``. F2 enforces the grant + the ``app__<slug>__``
    table-name prefix and journals ``db:table``; the bound engine is F4.
    """

    def table(self, name: str) -> str:
        self._ctx._enforce("db:own-tables")
        prefix = f"app__{self._ctx.app_id}__"
        if not name.startswith(prefix):
            raise ValueError(f"table name {name!r} must be prefixed with {prefix!r}")
        self._ctx._runtime.journal.record(self._ctx.app_id, "db:table", name, {})
        return name


class ServicesFacade(_Facade):
    """``ctx.services`` — register a start/stop background service.

    Gated by ``service:manage``. F2 enforces the grant + journals; the actual
    service supervisor is F4.
    """

    def register(self, service_id: str, start: str, autostart: bool = False) -> dict[str, Any]:
        self._ctx._enforce("service:manage")
        self._ctx._runtime.journal.record(
            self._ctx.app_id, "service:register", service_id,
            {"start": start, "autostart": autostart})
        return {"service": service_id, "registered": True}


# capability -> (attribute name, facade class). One facade per single-capability
# contribution surface. Parameterised caps (config:extend:*, ui:slots:*) and the
# cross-app extension registry are F7 and not exposed as facades here.
_FACADES: dict[str, tuple[str, type[_Facade]]] = {
    "routes:register":  ("routes", RoutesFacade),
    "commands:install": ("commands", CommandsFacade),
    "secrets:own":      ("secrets", SecretsFacade),
    "db:own-tables":    ("db", DbFacade),
    "service:manage":   ("services", ServicesFacade),
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

    def on_deactivate(self, hook: Callable[[], Awaitable[None] | None]) -> None:
        """Register a callback run on unload (e.g. cancel a long-poll/WS)."""
        self._deactivate_hooks.append(hook)

    def _drain_hooks(self) -> list[Callable[[], Awaitable[None] | None]]:
        return list(self._deactivate_hooks)
