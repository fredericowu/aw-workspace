"""Plugin lifecycle contract + capability-gated ``AppContext`` (ADR Decision 3/4).

A Tier-1 app's ``runtime.entrypoint`` points at a subclass of :class:`Plugin`.
The runtime instantiates it and calls ``activate(ctx)`` on load / ``deactivate()``
on unload. **All** side effects go through ``ctx`` facades — never by touching
FastAPI/host internals directly — which is what makes unload clean, auditable,
and journaled.

F1 implements the ``routes`` facade (gated by ``routes:register``) and the
``on_deactivate`` hook. Other facades (``db``, ``commands``, ``secrets``,
``extensions``) are F4/F7; accessing an ungranted/unimplemented facade raises so
the boundary is honest, not silently permissive.
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


class RoutesFacade:
    """``ctx.routes`` — mount an app's own FastAPI sub-application.

    Only handed to apps granted ``routes:register``. The app owns one sub-app
    mounted at ``/api/apps/<slug>``; registering it journals a ``route:mount``
    action so uninstall can reverse it.
    """

    def __init__(self, runtime: "AppRuntime", app_id: str) -> None:
        self._runtime = runtime
        self._app_id = app_id
        self._registered = False

    def register(self, subapp: "FastAPI") -> None:
        if self._registered:
            raise RuntimeError(f"app {self._app_id!r} already registered a routes sub-app")
        self._runtime._mount(self._app_id, subapp)
        self._registered = True


class AppContext:
    """Capability-gated facade bundle handed to ``Plugin.activate``.

    Ungranted (or not-yet-implemented) capabilities raise on access — the
    facade is genuinely absent, not a no-op.
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

        self._routes: RoutesFacade | None = (
            RoutesFacade(runtime, app_id)
            if "routes:register" in granted_permissions else None
        )

    def has(self, capability: str) -> bool:
        return capability in self.granted_permissions

    @property
    def routes(self) -> RoutesFacade:
        if self._routes is None:
            raise PermissionError(
                f"app {self.app_id!r} was not granted 'routes:register'"
            )
        return self._routes

    def on_deactivate(self, hook: Callable[[], Awaitable[None] | None]) -> None:
        """Register a callback run on unload (e.g. cancel a long-poll/WS)."""
        self._deactivate_hooks.append(hook)

    def _drain_hooks(self) -> list[Callable[[], Awaitable[None] | None]]:
        return list(self._deactivate_hooks)
