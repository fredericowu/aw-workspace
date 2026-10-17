"""The request-boundary floor under the ``apps:changed`` broadcast.

Card ``3fc5bf3b``: ``POST /api/apps/<slug>/config`` lands on one of
``AW_WORKSPACE_WORKERS`` uvicorn processes and only that one updates its
``ctx.config``. The broadcast is what tells the other four — and when the
broadcast dies (two ways it silently did, both fixed alongside this), they
answer from the pre-save config indefinitely. Measured live 1-in-5 correct;
after a core restart, 6/6.

This is the part that makes the acceptance deterministic rather than
dependent on Redis: one ``os.stat`` of ``app-config/.generation`` per
``/api/apps/*`` request, and a config-only refresh before dispatch when the
token has actually moved. No Redis anywhere in here — including in these
tests, which share this container's live keyspace with a running workspace.

See ``docs/design/app-config-cross-worker-propagation.md``.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from src.api.app import _ConfigGenerationMiddleware
from src.apps import config_store


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setattr(config_store.paths, "workspace_home", lambda: str(tmp_path))
    monkeypatch.setattr(config_store.paths, "workspace_home_path", lambda: str(tmp_path))
    return tmp_path


class _Reconciler:
    def __init__(self, raises: bool = False) -> None:
        self.calls = 0
        self.raises = raises

    async def refresh_configs_in_process(self):
        self.calls += 1
        # A real refresh reads the mirror off a thread and awaits every changed
        # app's hook, so it always yields — which is exactly when a burst of
        # concurrent requests could pile up behind it if the lock were wrong.
        await asyncio.sleep(0)
        if self.raises:
            raise RuntimeError("the mirror is unreachable")
        return ["cfgapp"]


def _stack(reconciler=None):
    """The middleware wrapped around a recorder, with the per-worker state
    ``create_app`` initializes on ``app.state``."""
    served: list[str] = []

    async def inner(scope, receive, send):
        served.append(scope.get("path", ""))

    host_app = SimpleNamespace(state=SimpleNamespace(
        config_generation_mtime_ns=None,
        config_generation_token=None,
        config_generation_lock=asyncio.Lock(),
        app_reconciler=reconciler,
    ))
    return _ConfigGenerationMiddleware(inner, host_app=host_app), host_app, served


def _get(mw, path="/api/apps/knowledgeable/mcp"):
    return mw({"type": "http", "path": path}, None, None)


def test_no_token_yet_means_nothing_to_converge_to():
    """A fresh workspace where no config has ever been saved. The request must
    still be served, and no refresh attempted."""
    mw, host_app, served = _stack(_Reconciler())
    asyncio.run(_get(mw))
    assert served == ["/api/apps/knowledgeable/mcp"]
    assert host_app.state.app_reconciler.calls == 0


def test_a_save_on_another_worker_is_picked_up_before_the_request_is_served():
    """The whole point: this worker never heard the broadcast, and still
    answers from the new config."""
    rec = _Reconciler()
    mw, host_app, served = _stack(rec)
    config_store.bump_generation()

    asyncio.run(_get(mw))

    assert rec.calls == 1
    assert served == ["/api/apps/knowledgeable/mcp"]
    assert host_app.state.config_generation_token is not None


def test_an_unchanged_token_costs_one_stat_and_nothing_else():
    rec = _Reconciler()
    mw, _host_app, served = _stack(rec)
    config_store.bump_generation()

    for _ in range(20):
        asyncio.run(_get(mw))

    # The acceptance's 20 consecutive reads: one convergence, not twenty.
    assert rec.calls == 1
    assert len(served) == 20


def test_each_further_save_converges_once_more():
    rec = _Reconciler()
    mw, _host_app, _served = _stack(rec)

    for expected in (1, 2, 3):
        config_store.bump_generation()
        asyncio.run(_get(mw))
        asyncio.run(_get(mw))
        assert rec.calls == expected


def test_a_burst_right_after_a_save_converges_once_not_n_times():
    """The lock is held across the re-check AND the refresh, so the first of a
    burst does the work and the rest find nothing left to do. Without that,
    every request in flight when a save lands would run its own pass — each
    one awaiting the mirror and every changed app's hook."""
    rec = _Reconciler()
    mw, _host_app, served = _stack(rec)
    config_store.bump_generation()

    async def burst():
        await asyncio.gather(*(_get(mw) for _ in range(10)))

    asyncio.run(burst())
    assert rec.calls == 1
    assert len(served) == 10


def test_a_moved_mtime_with_the_same_token_does_not_refresh(_home):
    """``st_mtime_ns`` is the cheap hint; the token is the fact. A restore or a
    copy moves the mtime without changing anything."""
    import os

    rec = _Reconciler()
    mw, host_app, _served = _stack(rec)
    config_store.bump_generation()
    asyncio.run(_get(mw))
    assert rec.calls == 1

    path = config_store.generation_path()
    os.utime(path, ns=(1_000_000_000, 1_000_000_000))  # backwards, on purpose
    asyncio.run(_get(mw))

    assert rec.calls == 1
    # ...and the new mtime was recorded, so this costs one read rather than
    # one per request from here on.
    assert host_app.state.config_generation_mtime_ns == 1_000_000_000


def test_routes_outside_api_apps_are_not_checked_at_all():
    """Scope rule, named in the design doc: a core route outside
    ``/api/apps/`` that reads ``ctx.config`` is push-covered but not
    floor-covered. Asserting it keeps that honest instead of accidental."""
    rec = _Reconciler()
    mw, _host_app, served = _stack(rec)
    config_store.bump_generation()

    asyncio.run(_get(mw, "/api/health"))
    asyncio.run(_get(mw, "/api/settings/workspace-api-key"))

    assert rec.calls == 0
    assert served == ["/api/health", "/api/settings/workspace-api-key"]


def test_a_websocket_scope_is_passed_straight_through():
    rec = _Reconciler()
    mw, _host_app, _served = _stack(rec)
    config_store.bump_generation()

    handled: list[str] = []

    async def inner(scope, receive, send):
        handled.append(scope["type"])

    mw.app = inner
    asyncio.run(mw({"type": "websocket", "path": "/api/apps/x/ws"}, None, None))

    assert handled == ["websocket"]
    assert rec.calls == 0


def test_a_refresh_that_blows_up_still_serves_the_request_and_is_retried():
    """A floor that can 500 a request is worse than no floor — the broadcast
    is still the primary path. And the token is recorded only after a
    SUCCESSFUL refresh, so a transient failure is retried instead of leaving
    this worker quietly on the pre-save config forever."""
    rec = _Reconciler(raises=True)
    mw, host_app, served = _stack(rec)
    config_store.bump_generation()

    asyncio.run(_get(mw))
    assert rec.calls == 1
    assert served == ["/api/apps/knowledgeable/mcp"]
    assert host_app.state.config_generation_token is None

    rec.raises = False
    asyncio.run(_get(mw))
    assert rec.calls == 2
    assert host_app.state.config_generation_token is not None

    asyncio.run(_get(mw))
    assert rec.calls == 2  # and now it settles


def test_no_reconciler_on_state_yet_is_not_an_error():
    """``/api/apps/-/...`` can be hit before the apps framework has attached
    anything — most visibly during a boot, which is when it is watched most
    closely."""
    mw, host_app, served = _stack(None)
    config_store.bump_generation()
    asyncio.run(_get(mw))
    assert served == ["/api/apps/knowledgeable/mcp"]
    # Not recorded, so the request that arrives once the reconciler IS wired
    # still converges rather than inheriting a token nobody acted on.
    assert host_app.state.config_generation_token is None
    rec = _Reconciler()
    host_app.state.app_reconciler = rec
    asyncio.run(_get(mw))
    assert rec.calls == 1
