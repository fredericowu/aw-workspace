"""Per-workspace SQLAlchemy engine — the F2 isolation primitive.

The engine is built with ``execution_options(schema_translate_map={None:
AW_WORKSPACE_SCHEMA})``: every model is declared WITHOUT a schema (the
default ``None`` bucket), and SQLAlchemy rewrites every unqualified table
reference to the configured schema at execution time. A process only ever
sees the one schema its own engine is pinned to — there is no code path
that can address another workspace's tables from here.

The schema itself is provisioned centrally by aw-backend's
``workspace_provisioner.provision_schema()`` (``CREATE SCHEMA workspace_<slug>``,
F1). This module never creates the schema — only tables inside it, via
``create_all_tables()``.
"""
from __future__ import annotations

import logging
import os

from sqlalchemy.engine import Engine
from sqlmodel import Session, SQLModel, create_engine

_log = logging.getLogger(__name__)

_DEFAULT_URL = "postgresql+psycopg://postgres:postgres@127.0.0.1:5432/awserv"


def get_db_url() -> str:
    """Return the SQLAlchemy URL for the shared Postgres server.

    ``AW_WORKSPACE_DB_URL`` wins over ``AWSERV_DB_URL`` when both are set —
    the BYOD workspace-host runtime (see aw-remote-host) sets the former to
    point at the user's own local Postgres, while ``AWSERV_DB_URL`` remains
    the control-plane convention other callers may still have set in the
    shared environment. Accepts either var with or without the
    ``+psycopg`` driver suffix — normalises to ``postgresql+psycopg://…``
    either way (same convention as aw-backend's ``pg_db.get_db_url``).
    """
    url = os.environ.get("AW_WORKSPACE_DB_URL") or os.environ.get("AWSERV_DB_URL", _DEFAULT_URL)
    if url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url[len("postgresql://"):]
    return url


def get_workspace_schema() -> str:
    schema = os.environ.get("AW_WORKSPACE_SCHEMA")
    if not schema:
        raise RuntimeError("AW_WORKSPACE_SCHEMA is not set")
    return schema


_engine: Engine | None = None


def get_engine() -> Engine:
    """Shared Engine, scoped to this process's workspace schema.

    ``execution_options(schema_translate_map=...)`` is what enforces
    isolation — every unqualified (schema-less) table this process touches
    is rewritten to ``AW_WORKSPACE_SCHEMA`` at execution time.
    """
    global _engine
    if _engine is None:
        pool_size = int(os.environ.get("AW_WORKSPACE_DB_POOL_SIZE", "5"))
        engine = create_engine(
            get_db_url(),
            pool_pre_ping=True,
            pool_size=pool_size,
            max_overflow=pool_size * 2,
        )
        _engine = engine.execution_options(
            schema_translate_map={None: get_workspace_schema()}
        )
    return _engine


def create_all_tables() -> None:
    """Create this workspace's tables (idempotent) inside its own schema.

    Imports ``src.api.models`` to register the schema-less SQLModel
    classes, then lets ``schema_translate_map`` route ``CREATE TABLE`` to
    ``AW_WORKSPACE_SCHEMA``.

    Schema provisioning: in the central multi-tenant model the schema is
    created ahead of time by aw-backend's F1 ``provision_schema`` (the
    control-plane owns tenant provisioning). In the BYOD model the
    workspace's Postgres is the user's own local DB — the cloud
    control-plane cannot reach it to ``CREATE SCHEMA`` — so the runtime
    self-provisions its schema. We scope that to BYOD by keying on
    ``AW_WORKSPACE_DB_URL`` (the var the aw-remote-host bootstrap sets;
    see ``get_db_url``), which keeps the central invariant intact: with
    only ``AWSERV_DB_URL`` set, the schema must already exist.
    """
    from sqlalchemy import text

    from src.api import models  # noqa: F401 — side-effect: registers models

    engine = get_engine()
    schema = get_workspace_schema()

    if os.environ.get("AW_WORKSPACE_DB_URL"):
        # BYOD host — the runtime owns its local DB, so provision the schema.
        with engine.begin() as conn:
            conn.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{schema}"'))
        _log.info("db: ensured BYOD schema %s exists", schema)

    SQLModel.metadata.create_all(engine, checkfirst=True)
    _log.info("db: schema %s up to date", schema)


def get_session() -> Session:
    """Return a fresh ``sqlmodel.Session`` bound to this workspace's engine."""
    return Session(get_engine())
