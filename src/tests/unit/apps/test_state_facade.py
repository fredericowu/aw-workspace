"""Unit tests for ``ctx.state`` (design doc app-shared-state-facade.md,
§11 amendment). No real Redis/filesystem cross-worker proof here — that is
``src/tests/integration/apps/test_state_facade_multiworker.py`` for
kv/broadcast and the lease cases below that only need one process (flock is
per-open-file-description, so two instances in one process already prove
mutual exclusion the same way ``test_flock_lease.py`` does for ``FlockLease``).
"""
from __future__ import annotations

import logging

import pytest

from src.apps.base import AppContext
from src.apps.state_facade import (
    StateKeyError,
    _BroadcastNamespace,
    _KvNamespace,
    _LeaseNamespace,
    _StateBreaker,
    broadcast_topic,
    bypass_report,
    kv_key,
    lease_path,
)


class _FakeJournal:
    def __init__(self) -> None:
        self.entries: list[tuple] = []

    def record(self, app_id, kind, name, detail):
        self.entries.append((app_id, kind, name, detail))


class _FakeRuntime:
    def __init__(self) -> None:
        self.journal = _FakeJournal()


def _ctx(granted: list[str], app_id: str = "myapp") -> AppContext:
    return AppContext(
        runtime=_FakeRuntime(), app_id=app_id, version="1.0.0",
        granted_permissions=granted, config={}, package_dir="/tmp/myapp",
    )


# --- capability gating (criterion 2) ----------------------------------------

class TestCapabilityGating:
    def test_ungranted_app_raises_and_journals(self):
        ctx = _ctx([])
        with pytest.raises(PermissionError):
            _ = ctx.state
        kinds = [e[1] for e in ctx._runtime.journal.entries]
        assert "capability:denied" in kinds

    def test_granted_app_gets_a_state_facade_with_exactly_three_namespaces(self):
        ctx = _ctx(["state:own"])
        facade = ctx.state
        assert hasattr(facade, "kv")
        assert hasattr(facade, "lease")
        assert hasattr(facade, "broadcast")
        assert hasattr(facade, "degraded")

    def test_cached_facade_still_enforces_per_method(self):
        """Defence in depth: even a facade an app cached across a grant
        change must re-check on every call, same as every other facade."""
        import asyncio
        ctx = _ctx(["state:own"])
        kv = ctx.state.kv
        ctx.granted_permissions.remove("state:own")
        with pytest.raises(PermissionError):
            asyncio.run(kv.get("x"))


# --- key/topic/lease-path construction (criterion 3) ------------------------

class TestNamespaceConstruction:
    def test_kv_key_always_starts_with_the_apps_own_prefix(self, monkeypatch):
        monkeypatch.setenv("AW_WORKSPACE", "test-ws")
        prefix = "aw:ws:test-ws:app:myapp:kv:"
        for adversarial in ("foo", "leader", "core", "a.b-c_1"):
            assert kv_key("myapp", adversarial).startswith(prefix)

    def test_kv_key_rejects_malformed_names(self):
        for bad in ("", "has:colon", "has/slash", "has space", "UPPER", "-leads-with-dash"):
            with pytest.raises(StateKeyError):
                kv_key("myapp", bad)

    def test_broadcast_topic_is_namespaced_under_app_id(self):
        assert broadcast_topic("myapp", "updates") == "app:myapp:updates"

    def test_no_argument_reaches_a_core_leader_key(self, monkeypatch):
        """Falsifiable claim §10.3: there is no key/topic argument shape
        that lands outside app:<app_id>: — try the obvious escape attempts.
        Concatenation makes this true by construction: either the attempt
        is rejected outright, or the resulting key still starts with the
        app's own fixed prefix, because that prefix is never caller input."""
        monkeypatch.setenv("AW_WORKSPACE", "test-ws")
        own_prefix = "aw:ws:test-ws:app:myapp:kv:"
        for attempt in ("leader:core", "..", "../leader", "app:other:x"):
            try:
                key = kv_key("myapp", attempt)
            except StateKeyError:
                continue  # rejected outright — also a pass
            assert key.startswith(own_prefix)

    def test_lease_path_is_scoped_under_its_own_app_subdir(self, tmp_path):
        p1 = lease_path("app-a", "sync", lock_dir=str(tmp_path))
        p2 = lease_path("app-b", "sync", lock_dir=str(tmp_path))
        assert p1 != p2
        assert "/apps/app-a/" in p1
        assert "/apps/app-b/" in p2


# --- kv TTL requirement ------------------------------------------------------

class TestKvTtlRequired:
    @pytest.mark.asyncio
    async def test_put_without_ttl_raises(self):
        ctx = _ctx(["state:own"])
        with pytest.raises(StateKeyError):
            await ctx.state.kv.put("x", "y", ttl=0)
        with pytest.raises(StateKeyError):
            await ctx.state.kv.put("x", "y", ttl=None)


# --- degrade-open + breaker (criterion 4, kv/broadcast only) ----------------

class TestBreakerDegradeOpen:
    @pytest.mark.asyncio
    async def test_kv_degrades_open_and_breaker_logs_once_per_window(self, monkeypatch, caplog):
        import src.apps.state_facade as sf

        class _BoomClient:
            async def get(self, *a, **kw):
                raise ConnectionError("no redis here")

            async def set(self, *a, **kw):
                raise ConnectionError("no redis here")

        monkeypatch.setattr(sf, "_get_client", lambda: _BoomClient())
        breaker = sf._StateBreaker()
        monkeypatch.setattr(sf, "_breaker", breaker)

        ctx = _ctx(["state:own"])
        with caplog.at_level(logging.DEBUG, logger="src.apps.state_facade"):
            assert await ctx.state.kv.get("k1") is None
            assert ctx.state.degraded is True
            assert await ctx.state.kv.get("k2") is None  # still degraded

        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        debugs = [r for r in caplog.records if r.levelno == logging.DEBUG]
        assert len(warnings) == 1, "exactly one WARNING on the transition into degraded"
        assert len(debugs) >= 1, "subsequent failures inside the window stay DEBUG"

    @pytest.mark.asyncio
    async def test_recovery_logs_info_and_clears_degraded(self, monkeypatch, caplog):
        import src.apps.state_facade as sf

        breaker = sf._StateBreaker()
        breaker.trip(ConnectionError("x"), "kv.get")
        monkeypatch.setattr(sf, "_breaker", breaker)
        assert breaker.degraded is True

        class _OkClient:
            async def get(self, *a, **kw):
                return None

        monkeypatch.setattr(sf, "_get_client", lambda: _OkClient())
        ctx = _ctx(["state:own"])
        with caplog.at_level(logging.INFO, logger="src.apps.state_facade"):
            await ctx.state.kv.get("k")
        assert ctx.state.degraded is False
        infos = [r for r in caplog.records if r.levelno == logging.INFO]
        assert len(infos) == 1


# --- lease: flock-backed, no degrade mode (design §11) -----------------------

class TestLease:
    def test_two_workers_contend_one_wins(self, tmp_path):
        ctx_a = _ctx(["state:own"], app_id="myapp")
        ctx_b = _ctx(["state:own"], app_id="myapp")
        worker_a = _LeaseNamespace(ctx_a, lock_dir=str(tmp_path))
        worker_b = _LeaseNamespace(ctx_b, lock_dir=str(tmp_path))

        assert worker_a.claim("sync-job") is True
        assert worker_b.claim("sync-job") is False, (
            "a second worker must not be able to claim a lease the first "
            "worker still holds")
        assert worker_b.is_held("sync-job") is True

        worker_a.release("sync-job")
        assert worker_b.claim("sync-job") is True, (
            "once released, another worker must be able to claim it")
        worker_b.release("sync-job")

    def test_claim_is_idempotent_for_the_same_holder(self, tmp_path):
        ctx = _ctx(["state:own"])
        lease = _LeaseNamespace(ctx, lock_dir=str(tmp_path))
        assert lease.claim("x") is True
        assert lease.claim("x") is True  # already held — not a re-open
        lease.release("x")

    def test_is_held_false_when_nobody_holds_it(self, tmp_path):
        ctx = _ctx(["state:own"])
        lease = _LeaseNamespace(ctx, lock_dir=str(tmp_path))
        assert lease.is_held("never-claimed") is False

    def test_lease_has_no_ttl_or_renew_api(self):
        ctx = _ctx(["state:own"])
        lease = ctx.state.lease
        assert not hasattr(lease, "renew")
        import inspect
        assert "ttl" not in inspect.signature(lease.claim).parameters

    def test_release_all_drops_every_held_fd(self, tmp_path):
        ctx = _ctx(["state:own"])
        lease = _LeaseNamespace(ctx, lock_dir=str(tmp_path))
        lease.claim("a")
        lease.claim("b")
        lease.release_all()
        other = _LeaseNamespace(_ctx(["state:own"]), lock_dir=str(tmp_path))
        assert other.claim("a") is True
        assert other.claim("b") is True


# --- doctor §4.3: facade-bypass detection -----------------------------------

class _FakeLoadedApp:
    def __init__(self, tier: str, package_dir: str):
        class _M:
            pass
        self.manifest = _M()
        self.manifest.tier = tier
        self.package_dir = package_dir


class _FakeDoctorRuntime:
    def __init__(self, apps: dict):
        self._apps = apps

    def loaded_slugs(self):
        return list(self._apps)

    def get(self, slug):
        return self._apps.get(slug)


class TestDoctorBypass:
    def test_flags_a_tier1_app_importing_redis_coord_directly(self, tmp_path):
        bad_app = tmp_path / "devctl"
        bad_app.mkdir()
        (bad_app / "relay.py").write_text(
            "from src.libs.redis_coord import RedisBroadcaster\n")

        clean_app = tmp_path / "clean"
        clean_app.mkdir()
        (clean_app / "plugin.py").write_text("from src.apps.state_facade import StateKeyError\n")

        runtime = _FakeDoctorRuntime({
            "devctl": _FakeLoadedApp("inprocess", str(bad_app)),
            "clean": _FakeLoadedApp("inprocess", str(clean_app)),
        })
        hits = bypass_report(runtime)
        assert {"app": "devctl", "file": "relay.py"} in hits
        assert not any(h["app"] == "clean" for h in hits)

    def test_does_not_flag_the_ruled_exemption(self, tmp_path):
        exempt_app = tmp_path / "agents-platform-runners"
        exempt_app.mkdir()
        (exempt_app / "warm_pool.py").write_text(
            "from src.libs.redis_coord import RedisBroadcaster\n")

        runtime = _FakeDoctorRuntime({
            "agents-platform-runners": _FakeLoadedApp("inprocess", str(exempt_app)),
        })
        assert bypass_report(runtime) == []

    def test_does_not_flag_a_tier2_container_app(self, tmp_path):
        app_dir = tmp_path / "containerapp"
        app_dir.mkdir()
        (app_dir / "main.py").write_text(
            "from src.libs.redis_coord import RedisBroadcaster\n")

        runtime = _FakeDoctorRuntime({
            "containerapp": _FakeLoadedApp("container", str(app_dir)),
        })
        assert bypass_report(runtime) == []
