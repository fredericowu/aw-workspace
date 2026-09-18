"""Unit tests for src/apps/service_lease.py — no live Redis needed. Exercises
the real ServiceLease/is_lease_held_anywhere against an in-memory fake
implementing the tiny subset of redis-py's sync API they use (SET NX/PX,
EXISTS, EVAL of the renew/release scripts).

Cross-process behaviour (two real ServiceSupervisor instances sharing this
workspace's real Redis, simulating two AW_WORKSPACE_WORKERS) is covered
separately by
src/tests/integration/apps/test_services.py::test_manual_start_on_non_owning_worker_does_not_spawn_a_duplicate.
"""
from __future__ import annotations

import time

import pytest

from src.apps import service_lease
from src.apps.service_lease import (
    ServiceLease,
    _RELEASE_LUA,
    _RENEW_LUA,
    is_lease_held_anywhere,
)


class FakeRedis:
    """In-memory stand-in for the tiny subset of redis-py's sync API this
    module uses. Real wall-clock TTLs — no simulated clock — so this
    exercises the exact same expiry arithmetic the real thing would."""

    def __init__(self):
        self._store: dict[str, tuple[str, float | None]] = {}

    def _expire_if_due(self, key: str) -> None:
        if key in self._store:
            _, expires_at = self._store[key]
            if expires_at is not None and time.monotonic() >= expires_at:
                del self._store[key]

    def set(self, key, value, nx=False, px=None, ex=None):
        self._expire_if_due(key)
        if nx and key in self._store:
            return None
        expires_at = None
        if px is not None:
            expires_at = time.monotonic() + px / 1000.0
        elif ex is not None:
            expires_at = time.monotonic() + ex
        self._store[key] = (value, expires_at)
        return True

    def get(self, key):
        self._expire_if_due(key)
        entry = self._store.get(key)
        return entry[0] if entry else None

    def exists(self, key):
        self._expire_if_due(key)
        return 1 if key in self._store else 0

    def eval(self, script, numkeys, *args):
        self._expire_if_due(args[0])
        if script is _RENEW_LUA:
            key, token, px = args
            entry = self._store.get(key)
            if entry is None or entry[0] != token:
                return 0
            self._store[key] = (token, time.monotonic() + int(px) / 1000.0)
            return 1
        if script is _RELEASE_LUA:
            key, token = args
            entry = self._store.get(key)
            if entry is None or entry[0] != token:
                return 0
            del self._store[key]
            return 1
        raise AssertionError(f"unexpected script: {script!r}")


@pytest.fixture
def fake_redis(monkeypatch):
    fake = FakeRedis()
    monkeypatch.setattr(service_lease, "_get_shared_client", lambda *a, **k: fake)
    return fake


def test_acquire_succeeds_when_free(fake_redis):
    lease = ServiceLease("app", "svc")
    assert lease.acquire() is True


def test_second_worker_cannot_acquire_while_first_holds(fake_redis):
    first = ServiceLease("app", "svc")
    second = ServiceLease("app", "svc")
    assert first.acquire() is True
    assert second.acquire() is False


def test_release_lets_another_worker_acquire(fake_redis):
    first = ServiceLease("app", "svc")
    second = ServiceLease("app", "svc")
    assert first.acquire() is True
    first.release()
    assert second.acquire() is True


def test_release_by_a_non_owner_is_a_noop(fake_redis):
    """A worker whose lease was never won (or already lost) must never be
    able to delete another worker's live lease with a stray release()."""
    owner = ServiceLease("app", "svc")
    stranger = ServiceLease("app", "svc")
    assert owner.acquire() is True
    stranger.release()  # never acquired — must not touch owner's key
    assert is_lease_held_anywhere("app", "svc") is True


def test_is_lease_held_anywhere_reflects_current_holder(fake_redis):
    assert is_lease_held_anywhere("app", "svc") is False
    lease = ServiceLease("app", "svc")
    lease.acquire()
    assert is_lease_held_anywhere("app", "svc") is True
    lease.release()
    assert is_lease_held_anywhere("app", "svc") is False


def test_acquire_fails_open_when_redis_unreachable(monkeypatch):
    def _raise(*a, **k):
        raise ConnectionError("no redis here")

    monkeypatch.setattr(service_lease, "_get_shared_client", _raise)
    lease = ServiceLease("app", "svc")
    assert lease.acquire() is True  # degrade to today's single-worker behaviour
    assert is_lease_held_anywhere("app", "svc") is False  # fall back to local-only


def test_heartbeat_renews_past_the_initial_ttl(fake_redis):
    lease = ServiceLease("app", "svc", ttl=0.3, heartbeat_interval=0.1)
    other = ServiceLease("app", "svc", ttl=0.3, heartbeat_interval=0.1)
    assert lease.acquire() is True
    lease.start_heartbeat()
    try:
        time.sleep(0.5)  # longer than the initial TTL, shorter than a few heartbeats
        assert is_lease_held_anywhere("app", "svc") is True
        assert other.acquire() is False  # still owned by `lease`, not expired
    finally:
        lease.release()
