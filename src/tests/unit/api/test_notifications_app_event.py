"""``NotificationManager.emit_app_event`` — the ephemeral WS nudge used by
``ctx.notify.event()`` (``src/apps/base.py``) for push-on-change UI refreshes
(e.g. the Google OAuth broker's "connect completed" signal).

Pure unit test, no Postgres/Redis required: ``emit_app_event`` never touches
the DB, and with no Redis relay started ``_publish`` degrades to the local
``_broadcast`` path — enough to assert the frame shape and the no-loop no-op.
"""
from __future__ import annotations

import asyncio
import json

from src.api.notifications import NotificationManager


class _FakeWebSocket:
    def __init__(self):
        self.received: list[str] = []

    async def send_text(self, msg: str) -> None:
        self.received.append(msg)


def test_emit_app_event_broadcasts_frame_shape():
    async def scenario():
        mgr = NotificationManager()
        mgr.set_loop(asyncio.get_running_loop())
        ws = _FakeWebSocket()
        mgr.add_listener(ws)

        mgr.emit_app_event("google-workspace-mcp", "oauth_completed",
                           {"email": "a@b.com", "usable": True})

        for _ in range(50):
            if ws.received:
                break
            await asyncio.sleep(0.05)
        return ws.received

    received = asyncio.run(scenario())
    assert received, "emit_app_event did not reach the local listener"
    payload = json.loads(received[-1])
    assert payload["type"] == "app_event"
    assert payload["data"] == {
        "app": "google-workspace-mcp",
        "event": "oauth_completed",
        "email": "a@b.com",
        "usable": True,
    }


def test_emit_app_event_is_a_noop_without_a_loop():
    mgr = NotificationManager()
    ws = _FakeWebSocket()
    mgr.add_listener(ws)

    mgr.emit_app_event("some-app", "some_event")

    assert ws.received == []
