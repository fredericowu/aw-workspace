"""A slow container-status read must not stall every other request.

The 2026-09-04 report: a WebSocket handshake to ``/ws/status`` through the
tunnel sometimes sat "pending" forever — no upgrade, no close frame, no error.
The connection that hung was a *victim*, not the culprit: the reported curl
carried no cookie, no ``?token=`` and no ``X-Api-Key``, so ``authorize_ws``
short-circuits and closes 4401 immediately. Something else had frozen that
worker's event loop.

That something else was on the same endpoint. ``status_stream`` called
``component_snapshot(self.app)`` inline inside its ``async def``, and
``component_snapshot`` runs ``rt.containers.status(app_id)`` once per
registered Tier-2 container — each of those two serial round-trips to the
podman socket, on a docker-py client that had no ``timeout=`` at all. Dozens of
app containers behind an unresponsive socket is an effectively unbounded freeze
of the ONE loop thread that worker uses to serve every concurrent request. The
tell was the very next line: ``list_sessions`` was already wrapped in
``asyncio.to_thread``, so the handler was known to block and only the cheap
call had been threaded.

Sibling of ``test_event_loop_not_blocked_by_db.py`` — same property, different
blocking dependency (a container socket rather than Postgres). Kept behavioural
rather than added to ``src/tests/unit/api/test_no_blocking_db_in_async_routes.py``
on purpose: read that scanner's "WHAT THIS DELIBERATELY DOES NOT CATCH" section.
Both offenders here are cross-module, and ``containers.status`` is not
``get_session`` at all, so the scanner was never going to see them — and
chasing blocking calls across modules by name was tried and rejected there for
producing more false alarms than findings.

``TestClient`` MUST be used as a context manager here, for the reason spelled
out in that sibling file: outside one it builds a fresh blocking portal, and
therefore a fresh event loop, per request — two concurrent requests never share
a loop and the freeze cannot reproduce.
"""
from __future__ import annotations

import threading
import time
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api.components import register_component_routes
from src.api.identity import require_identity

# Auto-release bounds the FAILURE path. If the loop is frozen, the concurrent
# request below cannot return until this status read finishes, and the
# `release.set()` that would free it sits downstream of that same call — so
# without a timer the two deadlock until some long timeout. Releasing at 3s
# while asserting the concurrent request returns in under 1s keeps a regression
# fast and gives it a clean "took ~3s" message instead of a hang.
BLOCK_SECONDS = 3.0


class _SlowContainers:
    """Stands in for a podman socket that accepts but does not answer."""

    def __init__(self, entered: threading.Event, release: threading.Event) -> None:
        self.entered = entered
        self.release = release
        self.container = SimpleNamespace(name="aw-app-slow", image="x:1", port=9001)

    def registered(self):
        return [("slow", self.container)]

    def status(self, app_id: str):
        self.entered.set()
        self.release.wait(timeout=BLOCK_SECONDS)
        return {"container": self.container.name, "running": True, "status": "running"}


@pytest.fixture()
def ctx():
    entered = threading.Event()
    release = threading.Event()

    app = FastAPI()
    app.state.app_runtime = SimpleNamespace(
        containers=_SlowContainers(entered, release),
        get=lambda app_id: SimpleNamespace(manifest=SimpleNamespace(description="slow app")),
    )
    register_component_routes(app)

    @app.get("/api/health")
    async def health():
        """Touches nothing — if this stalls, the loop itself is frozen."""
        return {"ok": True}

    # The identity gate is not what is under test, and require_identity does
    # its own DB read for the API-key path.
    app.dependency_overrides[require_identity] = lambda: {"sub": "test"}
    return app, entered, release


def _assert_health_stays_responsive(client, entered):
    assert entered.wait(timeout=10), "the slow container status read never started"
    started = time.monotonic()
    health = client.get("/api/health")
    elapsed = time.monotonic() - started
    assert health.status_code == 200
    assert elapsed < 1.0, (
        f"/api/health took {elapsed:.1f}s while a container-status read was in "
        "flight — component_snapshot is running on the single event-loop thread "
        "and freezing every concurrent request. Wrap it in "
        "asyncio.to_thread(...)."
    )


def test_slow_container_status_does_not_stall_a_concurrent_request(ctx):
    """GET /api/components — the route the SPA polls."""
    app, entered, release = ctx
    result: dict = {}

    with TestClient(app) as client:
        def slow_request() -> None:
            result["status"] = client.get("/api/components").status_code

        slow = threading.Thread(target=slow_request, daemon=True)
        slow.start()
        try:
            _assert_health_stays_responsive(client, entered)
        finally:
            release.set()
            slow.join(timeout=10)

    assert result.get("status") == 200


def test_ws_status_snapshot_does_not_stall_a_concurrent_request(ctx):
    """/ws/status — the endpoint whose handshake was reported hanging.

    Driven through ``/ws/logs/{key}``, which is the WebSocket that
    ``register_component_routes`` owns and which reaches the same blocking
    ``component_snapshot`` (via ``_component_for``) after ``authorize_ws``,
    exactly as ``TerminalRoutes.status_stream`` does for its ``init`` frame.
    Wiring the whole terminal subsystem here would drag in a PTY manager and
    Redis for no extra coverage of the property under test.
    """
    app, entered, release = ctx
    import src.api.components as components_mod

    async def _authorized(_websocket):
        return {"sub": "test"}

    original = components_mod.authorize_ws
    components_mod.authorize_ws = _authorized
    try:
        with TestClient(app) as client:
            def slow_connect() -> None:
                try:
                    with client.websocket_connect("/ws/logs/docker:aw-slow"):
                        pass
                except Exception:  # noqa: BLE001 — the connect itself is not the assertion
                    pass

            slow = threading.Thread(target=slow_connect, daemon=True)
            slow.start()
            try:
                _assert_health_stays_responsive(client, entered)
            finally:
                release.set()
                slow.join(timeout=10)
    finally:
        components_mod.authorize_ws = original
