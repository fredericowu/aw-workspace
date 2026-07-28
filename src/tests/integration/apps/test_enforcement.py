"""Capability enforcement + trust-tier tests for the Tier-1 runtime (F2).

The F2 acceptance core: a plugin can only use the capabilities it was granted.
An app granted only ``routes:register`` mounts its route but is DENIED (and the
denial is JOURNALED) when it reaches for ``commands`` or ``secrets``; a high-risk
capability (``ui:code``) is stripped from an unsigned app's effective grant.
"""
from __future__ import annotations

import asyncio
import os
import textwrap

import httpx
from fastapi import FastAPI

from src.apps.journal import ActionJournal
from src.apps.runtime import AppRuntime


def _async(coro):
    return asyncio.run(coro)


def _write_app(tmp_path, slug, plugin_src, permissions):
    pkg = tmp_path / slug
    pkg.mkdir()
    routes_contrib = (
        f'"routes": [{{"prefix": "/api/apps/{slug}"}}]'
        if "routes:register" in permissions else ""
    )
    perms = "[" + ", ".join(f'"{p}"' for p in permissions) + "]"
    (pkg / "aw-app.json").write_text(textwrap.dedent(f"""
    {{
      "manifest_version": 1,
      "id": "{slug}",
      "name": "{slug}",
      "version": "1.0.0",
      "tier": "inprocess",
      "runtime": {{"entrypoint": "plugin:AppPlugin"}},
      "permissions": {perms},
      "contributes": {{{routes_contrib}}}
    }}
    """))
    (pkg / "plugin.py").write_text(textwrap.dedent(plugin_src))
    return str(pkg)


# A plugin granted only routes:register: mounts its route, then probes two
# capabilities it does NOT have and records the outcome so activate succeeds and
# we can inspect the journal afterwards.
_PROBE_PLUGIN = """
    from fastapi import FastAPI

    RESULT = {}

    class AppPlugin:
        async def activate(self, ctx):
            api = FastAPI()

            @api.get("/")
            async def root():
                return {"ok": True}

            ctx.routes.register(api)  # granted -> works

            try:
                ctx.commands.install("probe-x", "bin/x")
            except PermissionError as e:
                RESULT["commands"] = str(e)

            try:
                ctx.secrets.read("api_key")
            except PermissionError as e:
                RESULT["secrets"] = str(e)

        async def deactivate(self):
            return None
"""


def test_granted_route_works_but_ungranted_facades_denied_and_journaled(tmp_path):
    import sys

    pkg = _write_app(tmp_path, "probe", _PROBE_PLUGIN, ["routes:register"])

    async def run():
        host = FastAPI()
        rt = AppRuntime(host, journal=ActionJournal(), guard_identity=False)
        await rt.load(pkg, granted_permissions=["routes:register"])

        # the granted capability worked: the route is live
        transport = httpx.ASGITransport(app=host)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            r = await c.get("/api/apps/probe/")
            assert r.status_code == 200

        # the ungranted capabilities raised PermissionError inside the plugin
        result = sys.modules[f"{rt.get('probe').module_prefix}.plugin"].RESULT
        assert "commands:install" in result["commands"]
        assert "secrets:own" in result["secrets"]

        # ...and each denial was journaled (the audit trail)
        kinds = [(e.kind, e.target) for e in rt.journal.entries_for("probe")]
        assert ("capability:denied", "commands:install") in kinds
        assert ("capability:denied", "secrets:own") in kinds
        assert ("route:mount", "/api/apps/probe") in kinds

        # granted set reflects only what was granted
        assert rt.get("probe").ctx.has("routes:register")
        assert not rt.get("probe").ctx.has("commands:install")

        await rt.unload("probe")

    _async(run())


def test_granted_facade_action_is_allowed_and_journaled(tmp_path, monkeypatch):
    monkeypatch.setenv("AW_WORKSPACE_HOME", str(tmp_path / "home"))
    plugin = """
        class AppPlugin:
            async def activate(self, ctx):
                ctx.commands.install("cmdapp-do", "bin/do")
            async def deactivate(self):
                return None
    """
    pkg = _write_app(tmp_path, "cmdapp", plugin, ["commands:install"])
    # a real exec target for the shim to wrap (F4 install_shim resolves it)
    bindir = tmp_path / "cmdapp" / "bin"
    bindir.mkdir()
    (bindir / "do").write_text("#!/usr/bin/env bash\necho hi\n")

    async def run():
        from src.apps import paths

        rt = AppRuntime(FastAPI(), journal=ActionJournal())
        await rt.load(pkg, granted_permissions=["commands:install"])
        kinds = [(e.kind, e.target) for e in rt.journal.entries_for("cmdapp")]
        assert ("command:install", "cmdapp-do") in kinds
        # the shim was really written onto the persistent bin dir …
        shim = os.path.join(paths.bin_dir(), "cmdapp-do")
        assert os.path.isfile(shim)
        await rt.unload("cmdapp")
        # … and uninstall reverted it (journal reverse replay)
        assert not os.path.exists(shim)

    _async(run())


def test_high_risk_cap_refused_for_unsigned_app(tmp_path):
    plugin = """
        class AppPlugin:
            async def activate(self, ctx):
                return None
            async def deactivate(self):
                return None
    """
    pkg = _write_app(tmp_path, "risky", plugin, ["routes:register", "ui:code"])

    async def run():
        # unsigned: ui:code stripped from the effective grant
        rt = AppRuntime(FastAPI())
        await rt.load(pkg, granted_permissions=["routes:register", "ui:code"], signed=False)
        ctx = rt.get("risky").ctx
        assert ctx.has("routes:register")
        assert not ctx.has("ui:code")
        await rt.unload("risky")

        # signed: ui:code survives
        rt2 = AppRuntime(FastAPI())
        await rt2.load(pkg, granted_permissions=["routes:register", "ui:code"], signed=True)
        assert rt2.get("risky").ctx.has("ui:code")
        await rt2.unload("risky")

    _async(run())
