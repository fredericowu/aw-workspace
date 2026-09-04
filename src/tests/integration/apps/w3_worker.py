"""A real aw-workspace worker PROCESS, spawned by
``test_multiworker_app_lifecycle.py``.

Not a test module (no ``test_`` prefix, so pytest doesn't collect it). It runs
the genuine ``create_app()`` — the same lifespan, the same
``register_apps_routes``, the same ``AppLifecycle`` relay — under uvicorn, so
the multi-worker test exercises the shipped boot path rather than a
reconstruction of it. Two of these against one Postgres and one Redis IS the
``AW_WORKSPACE_WORKERS=2`` deployment.

Three things are stubbed, and only three, because each reaches outside the
test's sandbox — into the developer's checkout, the live workspace, or the
public internet:

* ``sync_on_boot`` — rewrites the real ``skills/``, ``.claude/``, ``.cursor/``
  and ``.gemini/`` trees in this repo.
* ``_reload_mcp_gateway`` — an HTTP call to the workspace's real MCP gateway.
* the marketplace-catalog fetch — ``POST /api/apps/install`` calls
  ``is_marketplace_app`` to DERIVE ``signed`` (never trusting the request
  body), and that bottoms out in ``catalog.py``'s ``httpx.get`` to GitHub.
  ``src/tests/conftest.py`` already blocks this for the pytest process,
  precisely because that fetch has hung CI twice — but a conftest cannot
  reach a spawned worker, and on a CI runner with no egress it turned the
  install POST into a 30s+ read timeout. Stubbed to the same empty catalog
  conftest returns, which is also the truthful answer here: a sideloaded
  ``package_dir`` app is not a marketplace app.

Everything the card is about — the provision/attach split, the ``apps:changed``
broadcast, the mirror, the mounts — is left completely alone.
"""
from __future__ import annotations

import sys


def main() -> None:
    port = int(sys.argv[1])

    import httpx

    import src.api.app as app_mod
    import src.apps.routes as routes_mod
    from src.apps import catalog as catalog_mod

    async def _no_sync() -> None:
        return None

    async def _no_gateway_reload(runtime, **kwargs):
        return {"ok": False, "note": "stubbed in the w3 multi-worker test"}

    def _empty_catalog(url, *args, **kwargs):
        return httpx.Response(200, text='{"apps": []}',
                              request=httpx.Request("GET", url))

    app_mod.sync_on_boot = _no_sync
    routes_mod._reload_mcp_gateway = _no_gateway_reload
    catalog_mod.httpx.get = _empty_catalog

    import uvicorn

    uvicorn.run(app_mod.create_app(), host="127.0.0.1", port=port,
                log_level="warning", access_log=False)


if __name__ == "__main__":
    main()
