"""Skills CRUD + open-in-code-server — backs Settings > General > Skills.

Ported from the ``agentic-workspace`` monolith's ``src/api/routes/skills.py``,
adapted to this workspace's split source: the monolith treats ``skills/`` as
the one source of truth, but here ``skills/`` is *generated*
(``src.libs.skills_sync.materialize()``) from ``native-skills/`` (this repo's
own, committed) plus whatever installed apps contribute or push, each marked
with ``.aw-app-id``. Writing straight into ``skills/`` would get silently
reverted by the next sync — its delete pass treats anything unmarked and
absent from ``native-skills/`` as stale — so create/delete here only ever
touch ``native-skills/`` (see ``skills_sync.create_skill`` /
``skills_sync.delete_skill``), and both end with a full
``agent_sync.sync_all()`` so the merged tree and every per-agent mirror
(``.claude/skills`` etc.) reflect the edit immediately rather than waiting on
the next scheduled sync.

The frontend's "Sync now" button hits ``POST /api/agent/sync`` directly (see
``src/api/agent_routes.py``) — no separate alias needed here.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from urllib.parse import quote

from fastapi import Body, Depends, FastAPI, HTTPException

from src.api.identity import require_identity
from src.libs import agent_sync, skills_sync

log = logging.getLogger(__name__)

# Path inside the code-server container where this workspace's whole tree is
# bind-mounted, at the SAME absolute path the workspace itself uses on the
# host (see aw-app-code-server's aw-app.json — $AW_WORKSPACE_ROOT at
# /opt/aw-workspace, read-write). A workspace-relative skill path is
# therefore an identity translation, not a remap — no host/container path
# juggling needed here, unlike the monolith's single-process ProcessManager
# version this was ported from.
CODE_SERVER_WORKSPACE = os.environ.get(
    "AW_CODE_SERVER_WORKSPACE", "/opt/aw-workspace"
).rstrip("/")


def register_skills_routes(app: FastAPI) -> None:
    @app.get("/api/skills")
    async def list_skills_route(identity: dict = Depends(require_identity)):
        return {"skills": skills_sync.list_skills()}

    @app.post("/api/skills")
    async def create_skill_route(
        payload: dict = Body(...), identity: dict = Depends(require_identity)
    ):
        name = (payload.get("name") or "").strip()
        description = (payload.get("description") or "").strip()
        if not name:
            raise HTTPException(status_code=400, detail="name is required")
        try:
            path = await asyncio.to_thread(skills_sync.create_skill, name, description)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

        # Sync immediately so the new skill is visible to agents (and shows
        # up as `editable` in the next GET /api/skills) without waiting for
        # a scheduled resync.
        result = await asyncio.to_thread(agent_sync.sync_all)
        return {
            "name": name,
            "path": str(path),
            "sync": [r.to_dict() for r in result.skills],
        }

    @app.delete("/api/skills/{name}")
    async def delete_skill_route(name: str, identity: dict = Depends(require_identity)):
        try:
            ok = await asyncio.to_thread(skills_sync.delete_skill, name)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        if not ok:
            raise HTTPException(status_code=404, detail=f"skill {name!r} not found")

        result = await asyncio.to_thread(agent_sync.sync_all)
        return {"deleted": name, "sync": [r.to_dict() for r in result.skills]}

    @app.post("/api/skills/{name}/open")
    async def open_skill_route(name: str, identity: dict = Depends(require_identity)):
        """Return a URL that opens ``skills/<name>/SKILL.md`` in code-server.

        code-server (the aw-app-code-server container) has ``auto_start:
        true`` and is expected already running
        — this workspace's app framework keeps it up, unlike the monolith's
        ProcessManager-driven start-on-demand this route was ported from
        (see aw-app-code-server's README: "auto-start is the framework's own
        auto_start: true config", no separate start step needed here).
        """
        skills = skills_sync.list_skills()
        match = next((s for s in skills if s["name"] == name), None)
        if match is None:
            raise HTTPException(status_code=404, detail=f"skill {name!r} not found")

        container_dir = f"{CODE_SERVER_WORKSPACE}/skills/{name}"
        container_file = f"{container_dir}/SKILL.md"
        # `vscode-remote:` (not `file://`) is what code-server's frontend
        # needs to actually load file content instead of a phantom empty
        # buffer — see aw-app-code-server/mcp_server/server.py, ported from
        # the same comment in the monolith's routes/skills.py.
        file_uri = f"vscode-remote:{container_file}"
        payload = json.dumps([["openFile", file_uri]], separators=(",", ":"))
        url = (
            f"/api/apps/code-server/?folder={quote(container_dir, safe='/')}"
            f"&payload={quote(payload, safe='')}"
        )

        return {
            "name": name,
            "url": url,
            "container_dir": container_dir,
            "container_file": container_file,
            "rel_path": match["rel_path"],
        }
