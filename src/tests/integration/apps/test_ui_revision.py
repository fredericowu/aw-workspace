"""Live-UI phase 1 (card 4205bf3b): the contributions ``revision`` + ETag, and
the ``ui_changed`` frame on ``/ws/notifications`` (with and without Redis)."""
from __future__ import annotations

import asyncio
import json
import os

from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api.identity import require_identity
from src.api.notifications import NotificationManager
from src.apps.lifecycle import AppLifecycle
from src.apps.reconciler import Reconciler
from src.apps.routes import register_apps_routes
from src.apps.runtime import AppRuntime
from src.tests.integration.apps.test_frontend_bundles import (
    _client, _write_frontend_app)


def _pkg(tmp_path, slug, code="export default {};\n"):
    pkg = _write_frontend_app(tmp_path, slug, f"ui/dist/{slug}.js", True)
    with open(os.path.join(pkg, "ui", "dist", f"{slug}.js"), "w") as fh:
        fh.write(code)
    return pkg


def test_revision_identical_across_workers_regardless_of_load_order(tmp_path, monkeypatch):
    monkeypatch.setenv("AW_WORKSPACE_HOME", str(tmp_path / "home"))
    a, b = _pkg(tmp_path, "uirevaa"), _pkg(tmp_path, "uirevbb")
    _, rt1, _ = _client()
    _, rt2, _ = _client()
    asyncio.run(rt1.load(a, granted_permissions=["ui:code"]))
    asyncio.run(rt1.load(b, granted_permissions=["ui:code"]))
    asyncio.run(rt2.load(b, granted_permissions=["ui:code"]))
    asyncio.run(rt2.load(a, granted_permissions=["ui:code"]))
    assert rt1.revision() == rt2.revision()
    assert rt1.contributions()["revision"] == rt1.revision()


def test_revision_moves_on_bundle_content_grants_and_install(tmp_path, monkeypatch):
    monkeypatch.setenv("AW_WORKSPACE_HOME", str(tmp_path / "home"))
    a = _pkg(tmp_path, "uirevaa")
    _, rt, _ = _client()
    empty = rt.revision()
    asyncio.run(rt.load(a, granted_permissions=["ui:code"]))
    r1 = rt.revision()
    assert r1 != empty  # install
    # code-only update: same version, different bundle bytes
    bundle = os.path.join(a, "ui", "dist", "uirevaa.js")
    with open(bundle, "w") as fh:
        fh.write("export default { changed: 1 };\n")
    r2 = rt.revision()
    assert r2 != r1
    # grants
    rt._apps["uirevaa"].granted_permissions = ["ui:code", "routes:register"]
    assert rt.revision() != r2


def test_etag_and_304(tmp_path, monkeypatch):
    monkeypatch.setenv("AW_WORKSPACE_HOME", str(tmp_path / "home"))
    _, rt, client = _client()
    asyncio.run(rt.load(_pkg(tmp_path, "uirevaa"), granted_permissions=["ui:code"]))
    first = client.get("/api/apps/-/contributions")
    etag = first.headers["etag"]
    assert etag == f'"{first.json()["revision"]}"'
    again = client.get("/api/apps/-/contributions", headers={"If-None-Match": etag})
    assert again.status_code == 304 and again.content == b""
    stale = client.get("/api/apps/-/contributions", headers={"If-None-Match": '"nope"'})
    assert stale.status_code == 200
    # install moves the revision, so the old ETag no longer matches
    asyncio.run(rt.load(_pkg(tmp_path, "uirevbb"), granted_permissions=["ui:code"]))
    moved = client.get("/api/apps/-/contributions", headers={"If-None-Match": etag})
    assert moved.status_code == 200 and moved.headers["etag"] != etag


class _WS:
    def __init__(self):
        self.received: list[dict] = []

    async def send_text(self, msg):
        self.received.append(json.loads(msg))


def test_ui_changed_frame_carries_current_revision():
    async def go():
        mgr = NotificationManager()
        mgr.set_revision_provider(lambda: "rev1")
        ws = _WS()
        mgr.add_listener(ws)
        await mgr.broadcast_ui_changed()
        return ws.received
    assert asyncio.run(go()) == [{"type": "ui_changed", "version": 1, "revision": "rev1"}]


class _FakeLocal:
    def list(self):
        return []


def _reconciler(lifecycle):
    r = Reconciler(AppRuntime(FastAPI()), local=_FakeLocal(), lifecycle=lifecycle)
    return r


def test_ui_changed_fires_locally_when_redis_is_absent():
    """lifecycle.publish is a silent no-op without a relay; the reconciler
    must then emit the frame itself or the browser is never told."""
    async def go():
        life = AppLifecycle()  # never started -> no broadcaster (no Redis)
        r = _reconciler(life)
        mgr = NotificationManager()
        mgr.set_revision_provider(lambda: "r")
        ws = _WS()
        mgr.add_listener(ws)
        r.on_ui_changed = mgr.broadcast_ui_changed
        await r._trigger_broadcast("install", "x")
        return ws.received
    assert asyncio.run(go()) == [{"type": "ui_changed", "version": 1, "revision": "r"}]


def test_no_local_duplicate_when_the_relay_published():
    """With the relay up each worker emits from its own apps:changed handler,
    so the reconciler must NOT also emit (would double-fire on this worker)."""
    class Life(AppLifecycle):
        async def publish(self, reason, app_id=None):
            return True
    async def go():
        r = _reconciler(Life())
        calls = []

        async def emit():
            calls.append(1)
        r.on_ui_changed = emit
        await r._trigger_broadcast("install", "x")
        return calls
    assert asyncio.run(go()) == []


def test_publish_reports_false_without_a_relay():
    assert asyncio.run(AppLifecycle().publish("install", "x")) is False


def test_connect_frame_repeats_revision(monkeypatch):
    from src.api import notifications as n

    async def _claims(ws):
        return {"sub": "t"}
    monkeypatch.setattr(n, "authorize_ws", _claims)
    app = FastAPI()
    mgr = NotificationManager()
    mgr.set_revision_provider(lambda: "revX")
    mgr._db = type("D", (), {"get_pending": lambda s: [], "mark_delivered": lambda s, i: None})()
    n.NotificationRoutes(app, mgr)
    with TestClient(app).websocket_connect("/ws/notifications") as ws:
        frame = ws.receive_json()
    assert frame == {"type": "ui_changed", "version": 1, "revision": "revX", "initial": True}


def test_revision_moves_when_a_config_backed_window_resolves_differently(tmp_path, monkeypatch):
    """Review HIGH: a non-secret config change changes a declarative window's
    resolved spec_data; the ETag must move with it or polls 304 forever."""
    monkeypatch.setenv("AW_WORKSPACE_HOME", str(tmp_path / "home"))
    _, rt, client = _client()
    pkg = _pkg(tmp_path, "uirevcfg")
    asyncio.run(rt.load(pkg, granted_permissions=["ui:code"]))
    state = {"url": "http://a"}
    real = rt._resolve_window
    rt._resolve_window = lambda app, win: {**real(app, win), "resolved": state["url"]}
    monkeypatch.setattr(type(rt._apps["uirevcfg"].manifest), "windows",
                        property(lambda self: [{"id": "uirevcfg.main", "title": "W"}]))
    first = client.get("/api/apps/-/contributions")
    etag = first.headers["etag"]
    assert client.get("/api/apps/-/contributions", headers={"If-None-Match": etag}).status_code == 304
    state["url"] = "http://b"
    changed = client.get("/api/apps/-/contributions", headers={"If-None-Match": etag})
    assert changed.status_code == 200 and changed.json()["windows"][0]["resolved"] == "http://b"


def test_same_size_same_mtime_overwrite_still_moves_the_revision(tmp_path, monkeypatch):
    monkeypatch.setenv("AW_WORKSPACE_HOME", str(tmp_path / "home"))
    a = _pkg(tmp_path, "uirevaa", code="AAAA")
    _, rt, _ = _client()
    asyncio.run(rt.load(a, granted_permissions=["ui:code"]))
    bundle = os.path.join(a, "ui", "dist", "uirevaa.js")
    before = os.stat(bundle)
    r1 = rt.revision()
    with open(bundle, "w") as fh:
        fh.write("BBBB")                       # same length
    os.utime(bundle, ns=(before.st_atime_ns, before.st_mtime_ns))  # same mtime
    assert rt.revision() != r1


def test_wildcard_if_none_match_is_304(tmp_path, monkeypatch):
    monkeypatch.setenv("AW_WORKSPACE_HOME", str(tmp_path / "home"))
    _, _, client = _client()
    assert client.get("/api/apps/-/contributions", headers={"If-None-Match": "*"}).status_code == 304


def test_apps_list_carries_the_revision_header(tmp_path, monkeypatch):
    monkeypatch.setenv("AW_WORKSPACE_HOME", str(tmp_path / "home"))
    _, rt, client = _client()
    asyncio.run(rt.load(_pkg(tmp_path, "uirevaa"), granted_permissions=["ui:code"]))
    assert client.get("/api/apps").headers["x-apps-revision"] == rt.revision()


def test_publish_with_zero_subscribers_reports_false():
    class B:
        async def publish(self, topic, payload):
            return 0
    life = AppLifecycle()
    life._broadcaster = B()
    assert asyncio.run(life.publish("install", "x")) is False


def test_stalled_socket_does_not_block_or_starve_others(monkeypatch):
    from src.api import notifications as n
    monkeypatch.setattr(n, "_UI_SEND_TIMEOUT_S", 0.05)

    class Stuck:
        async def send_text(self, msg):
            await asyncio.sleep(10)

    async def go():
        mgr = NotificationManager()
        mgr.set_revision_provider(lambda: "r")
        good = _WS()
        mgr.add_listener(Stuck())
        mgr.add_listener(good)
        await asyncio.wait_for(mgr.broadcast_ui_changed(), 1)
        return good.received, len(mgr._listeners)
    received, remaining = asyncio.run(go())
    assert received and remaining == 1


def test_revision_that_moves_during_connect_is_still_delivered(monkeypatch):
    from src.api import notifications as n

    async def _claims(ws):
        return {"sub": "t"}
    monkeypatch.setattr(n, "authorize_ws", _claims)
    revs = iter(["old", "new", "new", "new"])
    app = FastAPI()
    mgr = NotificationManager()
    mgr.set_revision_provider(lambda: next(revs))
    mgr._db = type("D", (), {"get_pending": lambda s: [], "mark_delivered": lambda s, i: None})()
    n.NotificationRoutes(app, mgr)
    with TestClient(app).websocket_connect("/ws/notifications") as ws:
        first, second = ws.receive_json(), ws.receive_json()
    assert first["revision"] == "old" and first["initial"] is True
    assert second == {"type": "ui_changed", "version": 1, "revision": "new"}
