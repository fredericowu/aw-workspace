"""W4: verify the three in-memory WS registries fan out cross-worker via
RedisBroadcaster (``src/libs/redis_coord.py``, ported in W0).

Mirrors aw-backend's F1 precedent (``repos/aw-backend/src/tests/integration/
api/test_f1_ws_registries_redis_relay.py``): one manager/hub instance per
test stands in for one worker process (each real worker constructs its own
instance at import time; here we construct two explicitly to share one
pytest process and one Redis). A message published exclusively through
instance A must reach a listener registered only on instance B — the
publisher never touches the listener directly, so this also proves the
golden rule (workers=1, single delivery path): the SAME code path is what
fans a message back out to a publisher's own local listeners.

``InstallJobs`` and ``StatusHub`` only need a reachable Redis. ``Notification
Manager`` also persists to Postgres (``NotificationDB``) — gated separately,
same pattern as ``test_notification_routes.py``.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
import uuid

import psycopg
import pytest
import redis as sync_redis

pytestmark = [pytest.mark.integration, pytest.mark.slow]


def _redis_url() -> str:
    """Same resolution order as ``src.libs.redis_coord.get_workspace_redis_url``
    — most explicit env var first, else the default the CI job's ephemeral
    Redis is actually reachable at (see ``.github/workflows/test.yml``)."""
    for var in ("AW_TEST_REDIS_URL", "AW_WORKSPACE_REDIS_URL", "AW_REDIS_URL"):
        url = os.environ.get(var)
        if url:
            return url
    return "redis://127.0.0.1:6379/0"


REDIS_URL = _redis_url()


def _redis_available() -> bool:
    try:
        client = sync_redis.Redis.from_url(REDIS_URL, socket_connect_timeout=2)
        return bool(client.ping())
    except Exception:
        return False


def _postgres_available() -> bool:
    try:
        psycopg.connect(
            "postgresql://postgres:postgres@127.0.0.1:5432/postgres",
            autocommit=True, connect_timeout=2,
        ).close()
        return True
    except Exception:
        return False


pytestmark.append(pytest.mark.skipif(not _redis_available(), reason="Redis not reachable"))


class _FakeWebSocket:
    """Minimal stand-in for FastAPI's WebSocket — just records sent text."""

    def __init__(self):
        self.received: list[str] = []

    async def send_text(self, msg: str) -> None:
        self.received.append(msg)


async def _wait_until(predicate, timeout: float = 5.0, interval: float = 0.05):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(interval)
    return predicate()


class TestInstallJobsRelay:
    def test_transition_on_worker_a_reaches_listener_on_worker_b(self):
        async def scenario():
            from src.apps.install_jobs import InstallJobs

            app_id = f"w4-test-{uuid.uuid4().hex[:8]}"
            worker_a = InstallJobs(share=True)
            worker_b = InstallJobs(share=True)
            loop = asyncio.get_running_loop()
            worker_a.set_loop(loop)
            worker_b.set_loop(loop)
            await worker_a.start_relay()
            await worker_b.start_relay()

            ws = _FakeWebSocket()
            worker_b.add_listener(ws)

            # Both transitions fired via worker_a only — worker_b never
            # touches this job directly, delivery must come exclusively
            # through the Redis relay.
            worker_a.start(app_id)
            worker_a.mark_installed(app_id, {"ok": True})

            await _wait_until(lambda: len(ws.received) >= 2)
            await worker_a.aclose()
            await worker_b.aclose()
            return app_id, ws.received

        app_id, received = asyncio.run(scenario())
        assert received, "no install-status message delivered via the relay"
        payload = json.loads(received[-1])
        assert payload["type"] == "app_install_status"
        assert payload["job"]["app_id"] == app_id
        assert payload["job"]["status"] == "installed"

    def test_replay_on_connect_sees_a_job_owned_by_another_worker(self):
        """The other half of the W3/W4 split: a WS client connecting on a
        worker that never ran the install must still see it in its initial
        snapshot (``all_active_shared``), not just in later live pushes."""
        async def scenario():
            from src.apps.install_jobs import InstallJobs

            app_id = f"w4-test-replay-{uuid.uuid4().hex[:8]}"
            worker_a = InstallJobs(share=True)
            worker_b = InstallJobs(share=True)
            loop = asyncio.get_running_loop()
            worker_a.set_loop(loop)
            worker_b.set_loop(loop)
            await worker_a.start_relay()
            await worker_b.start_relay()

            worker_a.start(app_id)
            await asyncio.sleep(0.3)  # STATE mirror write is fire-and-forget

            snapshot = await worker_b.all_active_shared()
            await worker_a.aclose()
            await worker_b.aclose()
            return app_id, snapshot

        app_id, snapshot = asyncio.run(scenario())
        by_id = {j["app_id"]: j for j in snapshot}
        assert app_id in by_id, (
            "a job owned by another worker did not show up in this worker's "
            "on-connect replay")
        assert by_id[app_id]["status"] == "installing"


class TestStatusHubRelay:
    def test_terminal_update_on_worker_a_reaches_listener_on_worker_b(self):
        async def scenario():
            from src.api.terminal import StatusHub

            worker_a = StatusHub()
            worker_b = StatusHub()
            loop = asyncio.get_running_loop()
            worker_a.set_loop(loop)
            worker_b.set_loop(loop)
            await worker_a.start_relay()
            await worker_b.start_relay()

            ws = _FakeWebSocket()
            worker_b.add(ws)

            marker = f"w4-test-{uuid.uuid4().hex[:8]}"
            await worker_a.broadcast({
                "type": "terminal_update", "action": "create", "marker": marker,
            })

            await _wait_until(lambda: ws.received)
            await worker_a.aclose()
            await worker_b.aclose()
            return marker, ws.received

        marker, received = asyncio.run(scenario())
        assert received, "no terminal_update delivered via the /ws/status relay"
        payload = json.loads(received[-1])
        assert payload["type"] == "terminal_update"
        assert payload["marker"] == marker


@pytest.mark.skipif(not _postgres_available(),
                    reason="live Postgres at 127.0.0.1:5432 not reachable")
class TestNotificationManagerRelay:
    def test_add_notification_on_worker_a_reaches_listener_on_worker_b(self, monkeypatch):
        _schema = f"workspace_w4nottest_{uuid.uuid4().hex[:8]}"
        monkeypatch.setenv("AW_WORKSPACE", "w4nottest")
        monkeypatch.setenv("AW_WORKSPACE_SCHEMA", _schema)
        monkeypatch.setenv("AW_WORKSPACE_DB_URL",
                           "postgresql://postgres:postgres@127.0.0.1:5432/awserv")

        import src.api.db as dbmod
        monkeypatch.setattr(dbmod, "_engine", None)

        async def scenario():
            from src.api.db import create_all_tables
            create_all_tables()

            from src.api.notifications import NotificationManager

            worker_a = NotificationManager()
            worker_b = NotificationManager()
            loop = asyncio.get_running_loop()
            worker_a.set_loop(loop)
            worker_b.set_loop(loop)
            await worker_a.start_relay()
            await worker_b.start_relay()

            ws = _FakeWebSocket()
            worker_b.add_listener(ws)

            external_id = f"w4-test-{uuid.uuid4().hex[:8]}"
            worker_a.add_notification(
                message="W4 relay test", level="info", title="W4",
                source="w4-test", external_id=external_id, external_status="new",
            )

            await _wait_until(lambda: ws.received)
            await worker_a.aclose()
            await worker_b.aclose()
            return external_id, ws.received

        try:
            external_id, received = asyncio.run(scenario())
        finally:
            from sqlalchemy import text
            from src.api.db import get_engine
            with get_engine().begin() as conn:
                conn.execute(text(f'DROP SCHEMA IF EXISTS "{_schema}" CASCADE'))
            monkeypatch.setattr(dbmod, "_engine", None)

        assert received, "no message delivered via the notifications relay"
        payload = json.loads(received[-1])
        assert payload["type"] == "ninja_notification"
        assert payload["notification"]["external_id"] == external_id
