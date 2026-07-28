"""``ctx.notify`` facade (F2 capability: ``notifications:send``) — routes an
app's notification into the same ``NotificationManager`` singleton that backs
``POST /api/notify`` / ``/ws/notifications`` (``src/api/notifications.py``).

Real-Postgres only — skips cleanly if 127.0.0.1:5432 isn't reachable, same
pattern as ``test_db_tables``.
"""
from __future__ import annotations

import asyncio
import textwrap

import psycopg
import pytest
from fastapi import FastAPI
from sqlalchemy import text

from src.apps.journal import ActionJournal
from src.apps.runtime import AppRuntime


def _postgres_reachable() -> bool:
    try:
        psycopg.connect(
            "postgresql://postgres:postgres@127.0.0.1:5432/postgres",
            autocommit=True, connect_timeout=2,
        ).close()
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _postgres_reachable(), reason="live Postgres at 127.0.0.1:5432 not reachable"
)

_SCHEMA = "workspace_notifyfacade"


def _async(coro):
    return asyncio.run(coro)


@pytest.fixture()
def host(monkeypatch):
    monkeypatch.setenv("AW_WORKSPACE_SCHEMA", _SCHEMA)
    monkeypatch.setenv("AW_WORKSPACE_DB_URL",
                       "postgresql://postgres:postgres@127.0.0.1:5432/awserv")
    import src.api.db as dbmod
    monkeypatch.setattr(dbmod, "_engine", None)
    from src.api.db import create_all_tables, get_engine
    create_all_tables()

    from src.api.notifications import NotificationManager
    app = FastAPI()
    app.state.notification_mgr = NotificationManager()
    yield app

    with get_engine().begin() as conn:
        conn.execute(text(f'DROP SCHEMA IF EXISTS "{_SCHEMA}" CASCADE'))
    monkeypatch.setattr(dbmod, "_engine", None)


def _write_app(tmp_path, slug, plugin_src, permissions):
    pkg = tmp_path / slug
    pkg.mkdir()
    perms = "[" + ", ".join(f'"{p}"' for p in permissions) + "]"
    (pkg / "aw-app.json").write_text(textwrap.dedent(f"""
    {{
      "manifest_version": 1,
      "id": "{slug}",
      "name": "{slug}",
      "version": "1.0.0",
      "tier": "inprocess",
      "runtime": {{"entrypoint": "plugin:AppPlugin"}},
      "permissions": {perms}
    }}
    """))
    (pkg / "plugin.py").write_text(textwrap.dedent(plugin_src))
    return str(pkg)


_NOTIFY_PLUGIN = """
    class AppPlugin:
        async def activate(self, ctx):
            ctx.notify("hello from the app", level="warning", title="Notify App")
        async def deactivate(self):
            return None
"""


def test_granted_app_fires_notification_via_engine(tmp_path, host):
    pkg = _write_app(tmp_path, "notifyapp", _NOTIFY_PLUGIN, ["notifications:send"])

    async def run():
        rt = AppRuntime(host, journal=ActionJournal())
        await rt.load(pkg, granted_permissions=["notifications:send"])

        pending = host.state.notification_mgr.get_pending()
        assert any(
            n["message"] == "hello from the app"
            and n["level"] == "warning"
            and n["source"] == "notifyapp"
            for n in pending
        )
        await rt.unload("notifyapp")

    _async(run())


_PROBE_PLUGIN = """
    RESULT = {}

    class AppPlugin:
        async def activate(self, ctx):
            try:
                ctx.notify("should not be delivered")
            except PermissionError as e:
                RESULT["notify"] = str(e)
        async def deactivate(self):
            return None
"""


def test_ungranted_app_denied_and_journaled(tmp_path, host):
    import sys

    pkg = _write_app(tmp_path, "noperms", _PROBE_PLUGIN, [])

    async def run():
        rt = AppRuntime(host, journal=ActionJournal())
        await rt.load(pkg, granted_permissions=[])

        result = sys.modules[f"{rt.get('noperms').module_prefix}.plugin"].RESULT
        assert "notifications:send" in result["notify"]

        kinds = [(e.kind, e.target) for e in rt.journal.entries_for("noperms")]
        assert ("capability:denied", "notifications:send") in kinds

        assert host.state.notification_mgr.get_pending() == []
        await rt.unload("noperms")

    _async(run())
