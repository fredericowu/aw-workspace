"""service:manage contribution point (F4).

An app granted ``service:manage`` registers a managed process the runtime can
start / stop / report status for, and uninstall stops every service the app
registered (no orphan processes). A short-lived ``sleep`` stands in for a real
service so the test is fast and self-contained.
"""
from __future__ import annotations

import asyncio
import textwrap

from fastapi import FastAPI

from src.apps.journal import ActionJournal
from src.apps.runtime import AppRuntime


def _async(coro):
    return asyncio.run(coro)


_PLUGIN = """
    class AppPlugin:
        async def activate(self, ctx):
            ctx.services.register("worker", "sleep 30", autostart=True)
            ctx._probe = ctx.services.status("worker")
        async def deactivate(self):
            return None
"""


def _write_service_app(tmp_path):
    slug = "svc"
    pkg = tmp_path / slug
    pkg.mkdir()
    (pkg / "aw-app.json").write_text(textwrap.dedent(f"""
    {{
      "manifest_version": 1,
      "id": "{slug}",
      "name": "{slug}",
      "version": "1.0.0",
      "tier": "inprocess",
      "runtime": {{"entrypoint": "plugin:AppPlugin"}},
      "permissions": ["service:manage"]
    }}
    """))
    (pkg / "plugin.py").write_text(textwrap.dedent(_PLUGIN))
    return str(pkg)


def test_service_registers_autostarts_and_is_stopped_on_uninstall(tmp_path):
    pkg = _write_service_app(tmp_path)

    async def run():
        rt = AppRuntime(FastAPI(), journal=ActionJournal())
        await rt.load(pkg, granted_permissions=["service:manage"])

        # autostarted → running with a real pid
        st = rt.services.status("svc", "worker")
        assert st["running"] is True
        assert st["pid"]
        pid = st["pid"]

        # registration journaled
        kinds = [(e.kind, e.target) for e in rt.journal.entries_for("svc")]
        assert ("service:register", "worker") in kinds

        # stop/start round-trip
        rt.services.stop("svc", "worker")
        assert rt.services.status("svc", "worker")["running"] is False
        rt.services.start("svc", "worker")
        assert rt.services.status("svc", "worker")["running"] is True

        # uninstall stops + drops the service (no orphan)
        await rt.unload("svc")
        import os
        try:
            os.kill(pid, 0)
            alive = True
        except ProcessLookupError:
            alive = False
        assert alive is False

    _async(run())
