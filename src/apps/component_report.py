"""Component version self-reporting (Deploy reconciliation C2).

Architect decision, Kanban card ``4165bf3b-9510-812c-9dc5-f03e52681fb2``
(full contract on sibling card ``4165bf3b-9510-81c8`` — C1, aw-backend's
component inventory API). ``report_components`` feeds that inventory so
``kanban_check_deployed`` v2 can read this workspace's own component
versions in one call instead of needing a live adapter per component.

Auth mirrors :class:`~src.apps.registry_client.CloudRegistry` — the same
workspace host credential (``AW_BACKEND_URL`` / ``AW_WORKSPACE`` /
``AW_WORKSPACE_HOST_TOKEN``), a different aw-backend route. Inert (a
no-op) when the cloud link isn't configured, same as that client — a BYOD
workspace with no link has nothing to report to.

Best-effort by construction: this workspace's own deploy-tracking metadata
must never be the reason a boot or a reconcile pass fails. Both functions
below catch broadly and log; nothing propagates to the caller.
"""
from __future__ import annotations

import asyncio
import logging
import os

import httpx

from src.apps.runtime import AppRuntime

log = logging.getLogger(__name__)

#: Same ``source`` grammar the architect's v2 design defines for
#: ``ComponentReport.source`` — this one names the reconciler as the origin.
_SOURCE = "core-reconciler"

#: Manifest id of the Tier-1 in-process runners app — same constant value
#: ``src/api/observability.py`` uses for its own self-loopback call.
RUNNERS_APP_ID = "agents-platform-runners"


def _inventory(runtime: AppRuntime) -> list[dict]:
    """``{component, version, source}`` for core + every currently loaded
    app — the bulk-upsert body shape C1 (aw-backend) declares. Component
    names follow the same grammar ``DeployComponent`` uses on the board:
    ``"core"`` and ``"app:<slug>"``."""
    components = [{
        "component": "core",
        "version": os.environ.get("AW_WORKSPACE_VERSION", ""),
        "source": _SOURCE,
    }]
    for slug in sorted(runtime.loaded_slugs()):
        loaded = runtime.get(slug)
        if loaded is None:
            continue
        components.append({
            "component": f"app:{slug}",
            "version": loaded.manifest.version,
            "source": _SOURCE,
        })
    return components


async def report_components(runtime: AppRuntime) -> None:
    """PUT this workspace's component inventory to aw-backend's bulk
    upsert route (C1: ``PUT /api/workspaces/{slug}/components``)."""
    backend_url = os.environ.get("AW_BACKEND_URL", "").rstrip("/")
    workspace = os.environ.get("AW_WORKSPACE", "")
    token = os.environ.get("AW_WORKSPACE_HOST_TOKEN", "")
    if not (backend_url and workspace and token):
        return
    body = {"components": _inventory(runtime)}
    url = f"{backend_url}/api/workspaces/{workspace}/components"
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.put(url, json=body,
                                    headers={"Authorization": f"Bearer {token}"})
        resp.raise_for_status()
    except Exception:
        log.warning(
            "apps: could not report component versions to aw-backend's "
            "inventory (C1) — the next boot/reconcile pass retries",
            exc_info=True)


async def trigger_deploy_check(runtime: AppRuntime) -> None:
    """Fire-and-forget loopback POST into the runners app's own
    contributed ``/deploy-check`` route — same self-call shape as
    ``observability._notify_agents_platform_runners`` (loopback to this
    same process's own port, never the public app-proxy route). No-op if
    the runners app isn't installed; that route may also not exist yet
    (it ships with a sibling card) — either way this never raises."""
    if not runtime.is_loaded(RUNNERS_APP_ID):
        return
    port = os.environ.get("AW_PORT", "9030")
    url = f"http://127.0.0.1:{port}/api/apps/{RUNNERS_APP_ID}/deploy-check"
    try:
        from src.api.workspace_api_key import (
            HEADER_NAME as API_KEY_HEADER,
            get_or_create_workspace_api_key,
        )
        api_key = await asyncio.to_thread(get_or_create_workspace_api_key)
        async with httpx.AsyncClient(timeout=5.0) as client:
            await client.post(url, headers={API_KEY_HEADER: api_key})
    except Exception:
        log.warning(
            "apps: could not trigger the runners app's /deploy-check pass "
            "— its own scheduled task covers this on its normal cadence",
            exc_info=True)


async def report_components_and_trigger_check(runtime: AppRuntime) -> None:
    """Both C2 side effects together — the single call both triggers (boot,
    end-of-reconcile-when-changed) use. Each half is independently
    best-effort, so one failing never skips the other."""
    await report_components(runtime)
    await trigger_deploy_check(runtime)
