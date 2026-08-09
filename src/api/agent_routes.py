"""``POST /api/agent/sync`` — the HTTP face of ``aw-workspace-cli agent sync``.

Ported from the monolith's ``src/api/routes/agent.py``, which backs both its
CLI and the Settings → Skills "Sync now" button. Same idea here: the SPA (or
any app UI) can re-run the fan-out without shelling into a terminal.

Also owns the **boot sync**, which the monolith does NOT have — and should.
In this workspace an installed app copies its own ``contributes.skills`` into
``skills/`` on every activate (``AppRuntime._register_skills``), so the moment
the reconciler finishes on boot the per-agent mirrors are already behind.
Syncing right after that closes the window with no user action; the monolith
never needed it because nothing there writes into ``skills/`` on its own.

(The monolith's AGENTS.md claims awserv runs a boot sync and a ~½s
``watchfiles`` re-sync on any ``skills/`` change. Neither exists in its code
as of this port — the only real triggers are this endpoint, the skills CRUD
routes and a settings save. The stale claim is not carried over.)
"""
from __future__ import annotations

import asyncio
import logging

from fastapi import Depends, FastAPI

from src.api.identity import require_identity
from src.libs import agent_sync

log = logging.getLogger(__name__)


def _result_payload(result) -> dict:
    return {
        "skills": [r.to_dict() for r in result.skills],
        "agents_md": [r.to_dict() for r in result.agents_md],
        "mcp": [r.to_dict() for r in result.mcp],
        "codex": [r.to_dict() for r in result.codex],
        "gemini": [r.to_dict() for r in result.gemini],
    }


def register_agent_routes(app: FastAPI) -> None:
    @app.post("/api/agent/sync")
    async def sync_now(identity: dict = Depends(require_identity)):
        # Offloaded to a thread: the sync is filesystem-bound and may shell out
        # to the codex CLI, neither of which belongs on the single event loop
        # that also serves every terminal WebSocket.
        result = await asyncio.to_thread(agent_sync.sync_all)
        payload = _result_payload(result)
        # The monolith's UI reads the skills half as `results`; keep that alias
        # so a frontend written against either shape works.
        payload["results"] = payload["skills"]
        return payload


async def sync_on_boot() -> None:
    """Bring the per-agent mirrors up to date once, at startup.

    Never raises: a workspace must boot even if one agent directory is
    unwritable. A failure here means stale mirrors, not a dead workspace.
    """
    try:
        result = await asyncio.to_thread(agent_sync.sync_all)
    except Exception:  # noqa: BLE001 — mirrors are a convenience, not a dependency
        log.exception("agent sync: boot sync failed; per-agent mirrors may be stale")
        return

    changed = sum(1 for r in result.skills if r.changed)
    changed += sum(1 for r in (result.agents_md + result.mcp) if r.changed)
    if changed:
        log.info("agent sync: boot sync updated %d target(s)", changed)
    else:
        log.debug("agent sync: boot sync — everything already up to date")
