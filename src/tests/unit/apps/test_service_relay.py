"""Unit tests for src/apps/service_relay.py — no live Redis needed.

Exercises the real :class:`ServiceCommandRelay` against an in-memory stand-in
for the one thing it needs from Redis: PUBLISH/PSUBSCRIBE fan-out where every
subscriber (the publisher included) receives every message. Same posture as
``test_service_lease.py``'s ``FakeRedis`` — fake the transport, run the real
logic.

What this canNOT prove, and what the integration test is for: two processes
with no shared Python state converging purely through a real Redis. An
in-memory bus passes happily while the real thing is broken, so
``src/tests/integration/apps/test_services.py`` carries the cross-instance
proof with real ``ServiceSupervisor``s and real subprocesses.
"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from src.apps import service_relay
from src.apps.service_relay import (
    TOPIC_SERVICE_CMD,
    TOPIC_SERVICE_REPLY,
    ServiceCommandRelay,
)
from src.apps.services import ServiceError


class FakeBus:
    """The shared Redis every fake broadcaster publishes into."""

    def __init__(self) -> None:
        self.handlers: list = []
        self.published: list[tuple[str, dict]] = []


class FakeBroadcaster:
    """Stand-in for RedisBroadcaster: one PSUBSCRIBE over every topic, and a
    publish that reaches every subscriber including this one, out-of-band
    (a task, not an inline await) exactly like the real relay loop."""

    bus: FakeBus | None = None  # set by the fixture before construction

    def __init__(self, redis_url=None) -> None:
        self._bus = FakeBroadcaster.bus
        self._handler = None

    async def start_relay(self, handler) -> None:
        self._handler = handler
        self._bus.handlers.append(handler)

    async def publish(self, topic: str, payload: dict) -> int:
        self._bus.published.append((topic, payload))
        for handler in list(self._bus.handlers):
            # Round-trip through JSON like the real transport, so a payload
            # can never smuggle a live Python object between "workers".
            asyncio.get_running_loop().create_task(
                handler(topic, json.loads(json.dumps(payload))))
        return len(self._bus.handlers)

    async def stop(self) -> None:
        if self._handler in self._bus.handlers:
            self._bus.handlers.remove(self._handler)
        self._handler = None


class FakeSupervisor:
    """The slice of ServiceSupervisor the relay actually calls."""

    def __init__(self, *, owns: bool = False, pid: int = 4242) -> None:
        self.owns = owns
        self.pid = pid
        self.calls: list[str] = []
        self.raises: Exception | None = None

    def owns_locally(self, app_id: str, service_id: str) -> bool:
        return self.owns

    def _act(self, action: str, app_id: str, service_id: str) -> dict:
        self.calls.append(action)
        if self.raises is not None:
            raise self.raises
        return {"service": service_id, "running": action != "stop", "pid": self.pid}

    def start(self, app_id, service_id):
        return self._act("start", app_id, service_id)

    def stop(self, app_id, service_id):
        return self._act("stop", app_id, service_id)

    def restart(self, app_id, service_id):
        self.pid += 1  # a restart is a new process
        return self._act("restart", app_id, service_id)


@pytest.fixture
def bus(monkeypatch):
    fake = FakeBus()
    FakeBroadcaster.bus = fake
    import src.libs.redis_coord as redis_coord

    monkeypatch.setattr(redis_coord, "RedisBroadcaster", FakeBroadcaster)
    return fake


@pytest.fixture
def lease_held(monkeypatch):
    """Control what ``is_lease_held_anywhere`` reports, i.e. whether SOME
    worker owns the service. Defaults to "nobody"."""
    state = {"held": False}
    monkeypatch.setattr(service_relay, "is_lease_held_anywhere",
                        lambda *a, **k: state["held"])
    return state


async def _relay(supervisor, *, timeout=5.0, started=True):
    relay = ServiceCommandRelay(SimpleNamespace(services=supervisor), timeout=timeout)
    if started:
        assert await relay.start() is True
    return relay


def test_acts_locally_when_this_worker_owns_the_service(bus, lease_held):
    """Branch 1: the owner never goes near Redis."""
    async def run():
        sup = FakeSupervisor(owns=True)
        relay = await _relay(sup)
        result = await relay.dispatch("app", "svc", "restart")
        assert sup.calls == ["restart"]
        assert result["pid"] == sup.pid
        assert "forwarded" not in result
        assert bus.published == []  # nothing was broadcast at all
        await relay.stop()

    asyncio.run(run())


def test_forwards_to_the_worker_that_owns_the_service(bus, lease_held):
    """Branch 2: the request landed on a non-owner, and the real owner is the
    one whose supervisor executes it."""
    async def run():
        owner_sup = FakeSupervisor(owns=True, pid=1000)
        other_sup = FakeSupervisor(owns=False)
        owner = await _relay(owner_sup)
        other = await _relay(other_sup)
        lease_held["held"] = True

        result = await other.dispatch("app", "svc", "restart")

        assert owner_sup.calls == ["restart"]      # the owner did the work
        assert other_sup.calls == []               # the requester did not
        assert result["forwarded"] is True
        assert result["pid"] == 1001               # the owner's NEW pid
        topics = [t for t, _ in bus.published]
        assert topics == [TOPIC_SERVICE_CMD, TOPIC_SERVICE_REPLY]
        await owner.stop()
        await other.stop()

    asyncio.run(run())


def test_acts_locally_when_nobody_owns_the_service(bus, lease_held):
    """Branch 3: no lease anywhere — starting here is correct, and is what
    lets a stopped service be started from any worker."""
    async def run():
        sup = FakeSupervisor(owns=False)
        relay = await _relay(sup)
        lease_held["held"] = False

        result = await relay.dispatch("app", "svc", "start")

        assert sup.calls == ["start"]
        assert bus.published == []
        assert result["pid"] == sup.pid
        await relay.stop()

    asyncio.run(run())


def test_falls_back_locally_once_when_no_owner_answers(bus, lease_held):
    """Branch 4: a lease exists but its holder is wedged/gone. We must not
    hang past the timeout, and must not silently do nothing either — the
    lease gate in ServiceSupervisor.start is what keeps the fallback safe."""
    async def run():
        sup = FakeSupervisor(owns=False)
        relay = await _relay(sup, timeout=0.2)
        lease_held["held"] = True  # ...but no relay anywhere owns it

        result = await relay.dispatch("app", "svc", "restart")

        assert [t for t, _ in bus.published] == [TOPIC_SERVICE_CMD]  # no reply came
        assert sup.calls == ["restart"]  # fell back locally, exactly once
        assert "forwarded" not in result
        await relay.stop()

    asyncio.run(run())


def test_acts_locally_when_redis_is_unreachable(bus, lease_held, monkeypatch):
    """Branch 5: no relay at all — every worker acts locally, which IS the
    workers=1 behaviour this repo degrades to everywhere else."""
    async def run():
        class DeadBroadcaster(FakeBroadcaster):
            async def start_relay(self, handler):
                raise ConnectionError("no redis here")

        import src.libs.redis_coord as redis_coord
        monkeypatch.setattr(redis_coord, "RedisBroadcaster", DeadBroadcaster)

        sup = FakeSupervisor(owns=False)
        relay = ServiceCommandRelay(SimpleNamespace(services=sup))
        assert await relay.start() is False  # never raises into the lifespan

        lease_held["held"] = True  # would forward if it could
        result = await relay.dispatch("app", "svc", "start")
        assert sup.calls == ["start"]
        assert result["pid"] == sup.pid

    asyncio.run(run())


def test_a_reply_for_someone_elses_request_is_ignored(bus, lease_held):
    """Every worker receives every reply — only the one holding that corr_id
    may consume it, or a burst of concurrent restarts would cross wires."""
    async def run():
        sup = FakeSupervisor(owns=False)
        relay = await _relay(sup, timeout=0.3)
        lease_held["held"] = True

        async def inject_noise():
            await asyncio.sleep(0.05)
            # A reply from a DIFFERENT request, delivered to this relay.
            await relay._on_message(TOPIC_SERVICE_REPLY, {
                "corr_id": "some-other-request", "ok": True,
                "status": {"pid": 999}, "owner_pid": 7})

        noise = asyncio.ensure_future(inject_noise())
        result = await relay.dispatch("app", "svc", "restart")
        await noise

        # The stray reply was not mistaken for ours: we timed out and fell
        # back locally rather than returning pid 999.
        assert sup.calls == ["restart"]
        assert result.get("pid") != 999
        assert "forwarded" not in result
        await relay.stop()

    asyncio.run(run())


def test_owner_replies_even_when_the_action_raises(bus, lease_held):
    """An owner that raises and stays silent costs the requester the full
    timeout and then a misleading local fallback. It must answer."""
    async def run():
        owner_sup = FakeSupervisor(owns=True)
        owner_sup.raises = ServiceError("the venv interpreter is gone")
        other_sup = FakeSupervisor(owns=False)
        owner = await _relay(owner_sup)
        other = await _relay(other_sup, timeout=5.0)
        lease_held["held"] = True

        with pytest.raises(ServiceError, match="venv interpreter"):
            await other.dispatch("app", "svc", "start")

        # Failed loudly rather than falling back — the owner was reached and
        # said no, which is a real answer the user needs to see.
        assert other_sup.calls == []
        assert [t for t, _ in bus.published] == [TOPIC_SERVICE_CMD, TOPIC_SERVICE_REPLY]
        await owner.stop()
        await other.stop()

    asyncio.run(run())


def test_a_non_owner_never_executes_a_broadcast_command(bus, lease_held):
    """The whole safety property of broadcast-and-filter: ownership is decided
    by each receiver, per message."""
    async def run():
        a = FakeSupervisor(owns=False)
        b = FakeSupervisor(owns=False)
        relay_a = await _relay(a)
        relay_b = await _relay(b)

        await relay_a._on_message(TOPIC_SERVICE_CMD, {
            "corr_id": "x", "app_id": "app", "service_id": "svc",
            "action": "restart", "origin_pid": 1})
        await relay_b._on_message(TOPIC_SERVICE_CMD, {
            "corr_id": "x", "app_id": "app", "service_id": "svc",
            "action": "restart", "origin_pid": 1})

        assert a.calls == [] and b.calls == []
        assert bus.published == []  # nobody answered, nobody pretended to
        await relay_a.stop()
        await relay_b.stop()

    asyncio.run(run())


def test_an_unknown_action_is_never_reflected_into_the_supervisor(bus, lease_held):
    """This is a control channel now, not only a notification one."""
    async def run():
        sup = FakeSupervisor(owns=True)
        relay = await _relay(sup)

        await relay._on_message(TOPIC_SERVICE_CMD, {
            "corr_id": "x", "app_id": "app", "service_id": "svc",
            "action": "stop_all_for", "origin_pid": 1})
        assert sup.calls == []

        with pytest.raises(ServiceError):
            await relay.dispatch("app", "svc", "forget_all_for")
        assert sup.calls == []
        await relay.stop()

    asyncio.run(run())


def test_timeout_is_env_overridable_and_stays_under_the_tunnel_cut(monkeypatch):
    assert service_relay.resolve_timeout_s() == service_relay.DEFAULT_TIMEOUT_S
    assert service_relay.DEFAULT_TIMEOUT_S < 30.0  # the tunnel edge cuts at 30s
    monkeypatch.setenv(service_relay.TIMEOUT_ENV, "3.5")
    assert service_relay.resolve_timeout_s() == 3.5
    monkeypatch.setenv(service_relay.TIMEOUT_ENV, "not-a-number")
    assert service_relay.resolve_timeout_s() == service_relay.DEFAULT_TIMEOUT_S
    monkeypatch.setenv(service_relay.TIMEOUT_ENV, "0")
    assert service_relay.resolve_timeout_s() == service_relay.DEFAULT_TIMEOUT_S
