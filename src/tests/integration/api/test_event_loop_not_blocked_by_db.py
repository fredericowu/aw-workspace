"""A slow DB read on one route must not stall every other request.

The 2026-09-02 report: four unrelated GETs — apps status, contributions,
``/api/settings/workspace-api-key`` and ``/api/settings/mcp`` — each returned
200 but took 23-25s, right after a restart. ``/api/settings/mcp`` is served by
the generic ``/api/settings/{key}`` route below, which read Postgres through
the SYNCHRONOUS ``src.api.db.get_session`` directly inside its ``async def``.
With ``AW_WORKSPACE_WORKERS=1`` (deliberate — terminal PTY sessions keep
in-memory state) one asyncio thread serves every request, so that read froze
all of them for its full duration.

Companion to ``src/tests/unit/apps/test_reconciler_does_not_block_loop.py``,
which covers the boot-reconcile pass that actually triggered the freeze.

Deliberately needs NO Postgres: the property under test is "does the loop keep
turning", and the blocking helper is patched anyway. Gating this on a live
database would make it skip in exactly the environments most likely to
regress — so ``create_all_tables`` and the lifespan's own DB calls are stubbed
instead.

``TestClient`` MUST be used as a context manager here. Outside one it builds a
fresh blocking portal — and therefore a fresh event loop — per request, so two
concurrent requests never share a loop and the freeze this test exists to
catch cannot happen. Verified: without the ``with``, this test passed happily
against the unfixed code.
"""
from __future__ import annotations

import threading
import time

from fastapi.testclient import TestClient


async def _noop_async(*_args, **_kwargs):
    return None


def _build_app(monkeypatch):
    import src.api.app as app_mod
    from src.api.identity import require_identity

    # Everything the lifespan does that would need a real database. The
    # lifespan has to actually run (see the module docstring on TestClient),
    # it just must not touch Postgres.
    monkeypatch.setattr(app_mod, "create_all_tables", lambda: None)
    monkeypatch.setattr(app_mod, "get_or_create_workspace_api_key", lambda: "test-key")
    monkeypatch.setattr(app_mod, "publish_workspace_api_url", lambda: None)
    monkeypatch.setattr(app_mod, "reconcile_sources_on_boot", lambda: None)
    monkeypatch.setattr(app_mod, "reconcile_on_boot", _noop_async)
    monkeypatch.setattr(app_mod, "sync_on_boot", _noop_async)

    app = app_mod.create_app()
    # Every settings route is identity-gated; the gate is not what is under
    # test here (and require_identity does its own DB read for API-key auth).
    app.dependency_overrides[require_identity] = lambda: {"sub": "test"}
    return app_mod, app


def test_a_slow_settings_read_does_not_stall_a_concurrent_request(monkeypatch):
    app_mod, app = _build_app(monkeypatch)

    entered = threading.Event()
    release = threading.Event()

    # Auto-release bounds the FAILURE path. If the loop is frozen, the
    # /api/health call below cannot return until this read finishes, and the
    # `finally` that would release it is downstream of that same call — so
    # without a timer the two deadlock until a long timeout. Releasing at 3s
    # while asserting health returns in under 1s keeps a regression fast and
    # gives it a clean "took ~3s" message instead of a hang.
    BLOCK_SECONDS = 3.0

    def blocking_read(key: str) -> dict:
        """Stands in for a slow psycopg round-trip inside get_session()."""
        entered.set()
        release.wait(timeout=BLOCK_SECONDS)
        return {"key": key, "value": None}

    monkeypatch.setattr(app_mod, "_read_setting", blocking_read)

    slow_result: dict = {}

    # `with`: one persistent portal, so both requests share ONE event loop —
    # the single-worker shape this bug lives in.
    with TestClient(app) as client:
        def slow_request() -> None:
            res = client.get("/api/settings/mcp")
            slow_result["status"] = res.status_code

        slow = threading.Thread(target=slow_request, daemon=True)
        slow.start()
        try:
            assert entered.wait(timeout=10), "the slow settings read never started"

            # The freeze under test: with the read sitting on the event-loop
            # thread, this unrelated request (no DB at all) cannot be served
            # until it finishes.
            started = time.monotonic()
            health = client.get("/api/health")
            elapsed = time.monotonic() - started

            assert health.status_code == 200
            assert elapsed < 1.0, (
                f"/api/health took {elapsed:.1f}s while an unrelated settings "
                "read was in flight — the synchronous DB call is running on "
                "the single event-loop thread and freezing every concurrent "
                "request (AW_WORKSPACE_WORKERS=1). Wrap it in "
                "asyncio.to_thread(...)."
            )
        finally:
            release.set()
            slow.join(timeout=10)

    assert slow_result.get("status") == 200
