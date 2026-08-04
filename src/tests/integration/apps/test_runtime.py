"""Hot register / unregister + in-flight-drain tests for the Tier-1 runtime.

These are the F1 acceptance core: an app's routes appear the moment it loads
(no restart) and disappear the moment it unloads, and an unload waits for
in-flight requests to drain before tearing the app down.
"""
from __future__ import annotations

import asyncio
import sys
import textwrap

import httpx
import pytest
from fastapi import FastAPI

from src.apps.runtime import AppRuntime


def _write_app(tmp_path, slug, plugin_src, permissions='["routes:register"]'):
    pkg = tmp_path / slug
    pkg.mkdir()
    # only declare a routes contribution when the app is granted the capability
    # (the validator rejects contributes.routes without routes:register)
    routes_contrib = f'"routes": [{{"prefix": "/api/apps/{slug}"}}]' if "routes:register" in permissions else ""
    (pkg / "aw-app.json").write_text(textwrap.dedent(f"""
    {{
      "manifest_version": 1,
      "id": "{slug}",
      "name": "{slug}",
      "version": "1.0.0",
      "tier": "inprocess",
      "runtime": {{"entrypoint": "plugin:AppPlugin"}},
      "permissions": {permissions},
      "contributes": {{{routes_contrib}}}
    }}
    """))
    (pkg / "plugin.py").write_text(textwrap.dedent(plugin_src))
    return str(pkg)


def _async(coro):
    return asyncio.run(coro)


def test_unload_waits_for_in_flight_request_to_drain(tmp_path):
    plugin_src = """
        import asyncio
        from fastapi import FastAPI

        GATE = asyncio.Event()

        class AppPlugin:
            async def activate(self, ctx):
                api = FastAPI()

                @api.get("/slow")
                async def slow():
                    await GATE.wait()
                    return {"done": True}

                ctx.routes.register(api)

            async def deactivate(self):
                return None
    """
    pkg = _write_app(tmp_path, "slowapp", plugin_src)

    async def run():
        host = FastAPI()
        rt = AppRuntime(host, drain_timeout=5, guard_identity=False)
        await rt.load(pkg)
        loaded = rt.get("slowapp")
        gate = sys.modules[f"{loaded.module_prefix}.plugin"].GATE

        transport = httpx.ASGITransport(app=host)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            req = asyncio.create_task(c.get("/api/apps/slowapp/slow"))

            # wait until the request is genuinely in flight
            for _ in range(100):
                if loaded.drainable and loaded.drainable.active == 1:
                    break
                await asyncio.sleep(0.01)
            assert loaded.drainable.active == 1

            unload = asyncio.create_task(rt.unload("slowapp"))
            await asyncio.sleep(0.1)
            # unload must NOT finish while the request is still in flight
            assert not unload.done()

            # new requests already 404 (mount removed first)
            r404 = await c.get("/api/apps/slowapp/slow")
            assert r404.status_code == 404

            gate.set()  # let the in-flight request complete
            resp = await req
            assert resp.json() == {"done": True}
            await asyncio.wait_for(unload, timeout=5)

        assert not rt.is_loaded("slowapp")

    _async(run())


def test_load_rejects_ungranted_routes_permission(tmp_path):
    plugin_src = """
        from fastapi import FastAPI
        class AppPlugin:
            async def activate(self, ctx):
                ctx.routes.register(FastAPI())
            async def deactivate(self):
                return None
    """
    # grant nothing → ctx.routes must raise, load fails, no residue
    pkg = _write_app(tmp_path, "noperm", plugin_src, permissions='[]')

    async def run():
        host = FastAPI()
        rt = AppRuntime(host)
        with pytest.raises(PermissionError):
            await rt.load(pkg, granted_permissions=[])
        assert not rt.is_loaded("noperm")
        assert not any(n.startswith("aw_apps.noperm") for n in sys.modules)

    _async(run())
