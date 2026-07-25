"""aw-workspace FastAPI application factory — F2 skeleton.

Full runtime (apps/terminal/presentation/gateway/redis) is F5 — this only
proves the two F2 primitives wire together: a schema-isolated engine
(``src.api.db``) and offline EdDSA identity verification
(``src.api.identity``).
"""
from __future__ import annotations

import logging
import os

from fastapi import Depends, FastAPI

from src.api.db import create_all_tables, get_session, get_workspace_schema
from src.api.identity import require_identity
from src.api.models import Setting

log = logging.getLogger(__name__)


def create_app() -> FastAPI:
    create_all_tables()

    app = FastAPI(title="aw-workspace", version="0.1.0")

    @app.get("/api/health")
    async def health():
        return {"status": "ok", "workspace": os.environ.get("AW_WORKSPACE", "")}

    @app.get("/api/settings/{key}")
    async def get_setting(key: str, identity: dict = Depends(require_identity)):
        with get_session() as session:
            row = session.get(Setting, key)
            return {"key": key, "value": row.value if row else None}

    @app.put("/api/settings/{key}")
    async def put_setting(key: str, value: dict, identity: dict = Depends(require_identity)):
        with get_session() as session:
            row = session.get(Setting, key)
            if row is None:
                row = Setting(key=key, value=value)
            else:
                row.value = value
            session.add(row)
            session.commit()
        return {"key": key, "value": value, "schema": get_workspace_schema()}

    return app
