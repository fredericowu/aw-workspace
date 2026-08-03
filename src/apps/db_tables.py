"""App-owned workspace tables (ADR contribution point ``db:own-tables``, gated by
the F2 ``db:own-tables`` capability) — F4.

An app granted ``db:own-tables`` may create and use tables in the workspace
Postgres, but only under the enforced name prefix ``app__<slug>__`` (ADR Decision
8) — the facade validates every name, so an app can never address core or another
app's tables. Tables are created in this workspace's own schema (the F2 isolation
primitive — see ``src.api.db``); raw DDL/DML is schema-qualified explicitly here
because ``schema_translate_map`` only rewrites SQLAlchemy ``Table`` metadata, not
text SQL.

Uninstall **drops** the app's tables (task F4 acceptance). The journal records
each ``db:table`` create so the runtime knows which to drop on reverse replay.
"""
from __future__ import annotations

import logging
import re

from sqlalchemy import text

from src.api.db import get_engine, get_workspace_schema

log = logging.getLogger(__name__)

_IDENT_RE = re.compile(r"^[a-z_][a-z0-9_-]*$")


class DbTableError(RuntimeError):
    pass


def prefix_for(app_id: str) -> str:
    return f"app__{app_id}__"


def _validate(app_id: str, name: str) -> str:
    prefix = prefix_for(app_id)
    if not name.startswith(prefix):
        raise DbTableError(f"table name {name!r} must be prefixed with {prefix!r}")
    if not _IDENT_RE.match(name):
        raise DbTableError(f"invalid table name {name!r}")
    return name


class DbTables:
    """Runtime-owned backend for apps' own workspace tables."""

    def _qualified(self, name: str) -> str:
        return f'"{get_workspace_schema()}"."{name}"'

    def create(self, app_id: str, name: str, columns_sql: str) -> str:
        """``CREATE TABLE IF NOT EXISTS`` in this workspace's schema. Idempotent."""
        _validate(app_id, name)
        ddl = f"CREATE TABLE IF NOT EXISTS {self._qualified(name)} ({columns_sql})"
        with get_engine().begin() as conn:
            conn.execute(text(ddl))
        log.info("apps: created table %s for %s", name, app_id)
        return name

    def execute(self, app_id: str, name: str, sql: str, params: dict | None = None):
        """Run a statement that references the app's own ``{table}`` placeholder.

        ``sql`` must use ``{table}`` where the (prefix-validated, schema-qualified)
        table name goes — the app never spells the schema itself.
        """
        _validate(app_id, name)
        stmt = sql.replace("{table}", self._qualified(name))
        with get_engine().begin() as conn:
            return conn.execute(text(stmt), params or {}).fetchall() \
                if stmt.strip().lower().startswith("select") \
                else conn.execute(text(stmt), params or {})

    def drop(self, app_id: str, name: str) -> None:
        """Drop an app's table (uninstall). Idempotent."""
        _validate(app_id, name)
        with get_engine().begin() as conn:
            conn.execute(text(f"DROP TABLE IF EXISTS {self._qualified(name)}"))
        log.info("apps: dropped table %s for %s", name, app_id)
