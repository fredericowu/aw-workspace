"""db:own-tables contribution point (F4) — PG-gated.

An app granted ``db:own-tables`` creates + uses a table under its enforced
``app__<slug>__`` prefix, in this workspace's own schema (F2 isolation), and
the table is DROPPED on uninstall. A name outside the prefix is rejected.

Real-Postgres only (schemas are a real-Postgres concept) — skips cleanly if
127.0.0.1:5432 isn't reachable, same pattern as ``test_isolation``.
"""
from __future__ import annotations

import asyncio
import textwrap

import psycopg
import pytest
from fastapi import FastAPI
from sqlalchemy import text

from src.apps.journal import ActionJournal
from src.apps.runtime import AppRuntime


def _postgres_reachable() -> bool:
    try:
        psycopg.connect(
            "postgresql://postgres:postgres@127.0.0.1:5432/postgres",
            autocommit=True, connect_timeout=2,
        ).close()
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _postgres_reachable(), reason="live Postgres at 127.0.0.1:5432 not reachable"
)


def _async(coro):
    return asyncio.run(coro)


_PLUGIN = """
    class AppPlugin:
        async def activate(self, ctx):
            t = "app__dbapp__notes"
            ctx.db.create(t, "id serial primary key, body text")
            ctx.db.execute(t, "INSERT INTO {table} (body) VALUES (:b)", {"b": "hello"})
            rows = ctx.db.execute(t, "SELECT body FROM {table}")
            ctx.rows = [r[0] for r in rows]
            try:
                ctx.db.create("wrong_prefix", "id int")
            except Exception as e:
                ctx.prefix_error = type(e).__name__
        async def deactivate(self):
            return None
"""


def _write_db_app(tmp_path):
    slug = "dbapp"
    pkg = tmp_path / slug
    pkg.mkdir()
    (pkg / "aw-app.json").write_text(textwrap.dedent(f"""
    {{
      "manifest_version": 1,
      "id": "{slug}",
      "name": "{slug}",
      "version": "1.0.0",
      "tier": "inprocess",
      "runtime": {{"entrypoint": "plugin:AppPlugin"}},
      "permissions": ["db:own-tables"]
    }}
    """))
    (pkg / "plugin.py").write_text(textwrap.dedent(_PLUGIN))
    return str(pkg)


@pytest.fixture()
def workspace_schema(monkeypatch):
    schema = "workspace_f4db"
    monkeypatch.setenv("AW_WORKSPACE_SCHEMA", schema)
    monkeypatch.setenv("AW_WORKSPACE_DB_URL",
                       "postgresql://postgres:postgres@127.0.0.1:5432/awserv")
    # rebuild the cached engine against this schema
    import src.api.db as dbmod
    monkeypatch.setattr(dbmod, "_engine", None)
    from src.api.db import get_engine
    with get_engine().begin() as conn:
        conn.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{schema}"'))
    yield schema
    with get_engine().begin() as conn:
        conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
    monkeypatch.setattr(dbmod, "_engine", None)


def test_app_owns_table_prefix_enforced_and_dropped_on_uninstall(tmp_path, workspace_schema):
    pkg = _write_db_app(tmp_path)

    async def run():
        from src.api.db import get_engine

        rt = AppRuntime(FastAPI(), journal=ActionJournal())
        await rt.load(pkg, granted_permissions=["db:own-tables"])
        ctx = rt.get("dbapp").ctx

        # create + insert + read round-trip
        assert ctx.rows == ["hello"]
        # prefix enforced
        assert ctx.prefix_error == "DbTableError"
        # table really exists in this workspace's schema
        with get_engine().begin() as conn:
            exists = conn.execute(text(
                "SELECT to_regclass(:q)"),
                {"q": f'"{workspace_schema}".app__dbapp__notes'}).scalar()
        assert exists is not None
        # journaled
        kinds = [(e.kind, e.target) for e in rt.journal.entries_for("dbapp")]
        assert ("db:table", "app__dbapp__notes") in kinds

        # uninstall drops it
        await rt.unload("dbapp")
        with get_engine().begin() as conn:
            gone = conn.execute(text(
                "SELECT to_regclass(:q)"),
                {"q": f'"{workspace_schema}".app__dbapp__notes'}).scalar()
        assert gone is None

    _async(run())
