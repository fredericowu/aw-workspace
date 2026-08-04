"""db:own-tables contribution point (F4) — PG-gated.

An app granted ``db:own-tables`` creates + uses a table under its enforced
``app__<slug>__`` prefix, in this workspace's own schema (F2 isolation). The
table SURVIVES unload/uninstall (2026-08-04 decision — reconcile()'s upgrade
path is uninstall+install for a plain version bump, so an automatic drop
wiped an app's data on every routine update). A name outside the prefix is
rejected.

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


def test_app_owns_table_prefix_enforced_and_survives_unload(tmp_path, workspace_schema):
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

        # unload (routine update's uninstall+install, or a real uninstall)
        # does NOT drop it — only an explicit future admin action would.
        await rt.unload("dbapp")
        with get_engine().begin() as conn:
            still_there = conn.execute(text(
                "SELECT to_regclass(:q)"),
                {"q": f'"{workspace_schema}".app__dbapp__notes'}).scalar()
        assert still_there is not None

    _async(run())


def _write_migrating_app(tmp_path, migration_files: dict):
    """Same dbapp shape, plus a migrations/ dir declared in the manifest and
    populated with the given {filename: sql} files."""
    slug = "migapp"
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
      "permissions": ["db:own-tables"],
      "migrations": {{"dir": "migrations"}}
    }}
    """))
    (pkg / "plugin.py").write_text(textwrap.dedent("""
    class AppPlugin:
        async def activate(self, ctx):
            ctx.db.create("app__migapp__items", "id serial primary key")
        async def deactivate(self):
            return None
    """))
    mig_dir = pkg / "migrations"
    mig_dir.mkdir()
    for filename, sql in migration_files.items():
        (mig_dir / filename).write_text(sql)
    return str(pkg)


def test_migrations_apply_once_and_survive_update(tmp_path, workspace_schema):
    pkg = _write_migrating_app(tmp_path, {
        "0001_add_label.sql":
            'ALTER TABLE "workspace_f4db".app__migapp__items ADD COLUMN label TEXT',
    })

    async def run():
        from src.api.db import get_engine

        rt = AppRuntime(FastAPI(), journal=ActionJournal())
        await rt.load(pkg, granted_permissions=["db:own-tables"])

        # the migration ran — new column exists
        with get_engine().begin() as conn:
            cols = {
                row[0] for row in conn.execute(text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema = :s AND table_name = 'app__migapp__items'"),
                    {"s": "workspace_f4db"})
            }
        assert "label" in cols

        # simulate an update: unload + re-load with a SECOND migration added.
        # The first must NOT re-run (idempotent — it would error re-adding
        # the same column); only the new one should apply.
        await rt.unload("migapp")
        (mig_path := __import__("pathlib").Path(pkg) / "migrations" / "0002_add_note.sql") \
            .write_text('ALTER TABLE "workspace_f4db".app__migapp__items ADD COLUMN note TEXT')
        await rt.load(pkg, granted_permissions=["db:own-tables"])

        with get_engine().begin() as conn:
            cols2 = {
                row[0] for row in conn.execute(text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema = :s AND table_name = 'app__migapp__items'"),
                    {"s": "workspace_f4db"})
            }
        assert "label" in cols2 and "note" in cols2

        # tracking table recorded both, exactly once each
        with get_engine().begin() as conn:
            rows = conn.execute(text(
                'SELECT filename FROM "workspace_f4db"."_app_migrations" '
                "WHERE app_id = 'migapp' ORDER BY filename")).fetchall()
        assert [r[0] for r in rows] == ["0001_add_label.sql", "0002_add_note.sql"]

    _async(run())
