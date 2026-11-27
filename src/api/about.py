"""``GET /api/about`` - read-only facts for Settings > About.

Version block mirrors what ``/api/health`` already reports (that route is a
polled public contract and is NOT extended). Authenticated: machine facts are
operator-only. The collector is blocking file/subprocess IO, so it runs off the
loop and is cached briefly.
"""
from __future__ import annotations

import asyncio
import os
import time

from fastapi import Depends, FastAPI

from src.api import boot_info
from src.api.identity import require_identity
from src.libs import sysfacts

_TTL = 5.0
_cache: dict = {"at": 0.0, "value": None}


def _facts() -> dict:
    now = time.monotonic()
    if _cache["value"] is None or now - _cache["at"] > _TTL:
        _cache["value"] = sysfacts.collect()
        _cache["at"] = now
    return _cache["value"]


def register_about_routes(app: FastAPI) -> None:
    @app.get("/api/about")
    async def about(_claims: dict = Depends(require_identity)) -> dict:
        facts = await asyncio.to_thread(_facts)
        return {
            "version": {
                "core": os.environ.get("AW_WORKSPACE_VERSION", ""),
                "git_head": boot_info.git_head(),
                "started_at": boot_info.started_at(),
            },
            "workspace": os.environ.get("AW_WORKSPACE", ""),
            **facts,
        }
