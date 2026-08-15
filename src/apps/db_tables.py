"""App-owned workspace tables (ADR contribution point ``db:own-tables``, gated by
the F2 ``db:own-tables`` capability) — F4.

An app granted ``db:own-tables`` may create and use tables in the workspace
Postgres, but only under the enforced name prefix ``app__<slug>__`` (ADR Decision
8) — the facade validates every name, so an app can never address core or another
app's tables. Tables are created in this workspace's own schema (the F2 isolation
primitive — see ``src.api.db``); raw DDL/DML is schema-qualified explicitly here
because ``schema_translate_map`` only rewrites SQLAlchemy ``Table`` metadata, not
text SQL.

Tables are NEVER dropped automatically (2026-08-04 decision, reversing the
original F4 acceptance criteria) — ``reconcile()``'s upgrade path is
uninstall+install for a plain version bump, so an unconditional drop-on-unload
wiped an app's data on every routine update, not just a genuine uninstall.
``create()``'s ``CREATE TABLE IF NOT EXISTS`` is idempotent-safe against
existing data across reloads; schema evolution beyond the initial create is
the ``migrations/`` mechanism's job (see ``src/apps/migrations.py``), applied
at both install and update. ``drop()`` below still exists for a future
explicit "reset this app's data" admin action, but nothing in the runtime
calls it automatically anymore.
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

    def execute_multi(self, app_id: str, sql: str, names: list[str],
                      params: dict | None = None):
        """Like ``execute`` but for a statement spanning several of the app's
        own tables — ``{table:app__<slug>__foo}`` per reference.

        ``execute`` above validates exactly one table name, which makes a join
        (or a VIEW defined over several tables) inexpressible: an app with more
        than one table would have to stitch rows together in Python and lose
        every set-based guarantee the database gives it. Each name in ``names``
        goes through the same ``_validate`` prefix check, so the multi-table
        form widens what an app can *say*, never which tables it can reach.
        """
        for name in names:
            _validate(app_id, name)
        stmt = sql
        for name in names:
            stmt = stmt.replace("{table:%s}" % name, self._qualified(name))
        if "{table:" in stmt:
            raise DbTableError(
                "unresolved {table:...} placeholder — every table referenced "
                "must be listed in names")
        with get_engine().begin() as conn:
            result = conn.execute(text(stmt), params or {})
            return result.fetchall() if result.returns_rows else result

    def session(self, app_id: str, metadata=None):
        """A SQLAlchemy ``Session`` on this workspace's engine, for apps that
        model their tables declaratively instead of in SQL strings.

        The engine already carries ``schema_translate_map={None: <workspace
        schema>}`` (see ``src.api.db``), so schema-less ORM models land in this
        workspace's schema exactly like core's own — the F2 isolation primitive
        holds without the app doing anything. ``metadata`` (an app's
        ``Base.metadata``) is prefix-validated up front, which is what keeps
        Decision 8 true on this path: an app cannot map a model onto a core or
        foreign table and reach it through the ORM.

        Callers own the session lifecycle (``with ctx.db.session(md) as s:``).
        """
        if metadata is not None:
            for name in metadata.tables:
                _validate(app_id, name)
        from sqlalchemy.orm import Session as _Session
        return _Session(get_engine())

    def drop(self, app_id: str, name: str) -> None:
        """Drop an app's table. NOT called automatically by the runtime
        anymore (see module docstring) — reserved for a future explicit
        "reset this app's data" admin action. Idempotent."""
        _validate(app_id, name)
        with get_engine().begin() as conn:
            conn.execute(text(f"DROP TABLE IF EXISTS {self._qualified(name)}"))
        log.info("apps: dropped table %s for %s", name, app_id)
