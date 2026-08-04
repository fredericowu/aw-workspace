"""Per-app SQL migrations (ADR contribution point ``db:own-tables``, F4
follow-up — 2026-08-04).

An app's own bootstrap ``ctx.db.create(...)`` calls (``CREATE TABLE IF NOT
EXISTS``) cover the INITIAL shape of its tables and are safe to re-run on
every load — but they can never express schema EVOLUTION (adding a column,
an index, backfilling a value) without either wiping and recreating the
table (which ``db_tables.py`` no longer does — see its module docstring) or
hand-rolling ad-hoc ``ALTER TABLE ... IF NOT EXISTS`` checks in application
code. Migrations are the real mechanism for that.

An app opts in by declaring a ``migrations.dir`` in its ``aw-app.json``
(relative to the package root, e.g. ``"migrations": {"dir": "migrations"}``)
and dropping numbered ``.sql`` files in that directory
(``0001_add_status_column.sql``, ``0002_backfill_status.sql``, ...). Files
are applied in filename-sorted order, at most once per (app_id, filename) —
tracked in the CORE-owned ``_app_migrations`` table (deliberately NOT
``app__<slug>__``-prefixed: it must survive independently of whatever the
app's own tables look like). Applied by the runtime on every ``load()``
(covers both install and update) after the app's ``plugin.activate()`` has
run, so a migration can safely ``ALTER`` a table the app's own bootstrap
code just ensured exists.

A migration is intentionally NOT sandboxed per-app the way ``ctx.db.execute``
is (no ``{table}`` placeholder enforcement, no name-prefix check) — the SQL
file is written by the app's own developer and ships in the app's own repo,
same trust level as the rest of its Python code. It runs in the workspace's
own schema via ``search_path``-qualified DDL, same isolation boundary as
every other ``db:own-tables`` operation.
"""
from __future__ import annotations

import logging
import os

from sqlalchemy import text

from src.api.db import get_engine, get_workspace_schema

log = logging.getLogger(__name__)

_TRACKING_TABLE = "_app_migrations"


def _ensure_tracking_table() -> None:
    schema = get_workspace_schema()
    with get_engine().begin() as conn:
        conn.execute(text(
            f'CREATE TABLE IF NOT EXISTS "{schema}"."{_TRACKING_TABLE}" ('
            "app_id TEXT NOT NULL, "
            "filename TEXT NOT NULL, "
            "applied_at TIMESTAMPTZ NOT NULL DEFAULT now(), "
            "PRIMARY KEY (app_id, filename))"
        ))


def migrations_dir_for(package_dir: str, manifest_migrations: dict) -> str | None:
    """Resolve the app's migrations directory from its manifest, or None if
    it doesn't declare one. ``{"dir": "migrations"}`` is the documented
    shape; an empty/absent ``migrations`` (every existing app today) means
    "no migrations" — this function is a no-op for them."""
    if not manifest_migrations:
        return None
    rel = manifest_migrations.get("dir") or "migrations"
    return os.path.join(package_dir, rel)


def pending_migrations(app_id: str, migrations_dir: str) -> list[str]:
    """Filenames (sorted) not yet recorded as applied for this app. Returns
    ``[]`` without touching the DB if the directory doesn't exist or is
    empty — apps that declare a dir but haven't shipped any files yet pay
    no tracking-table cost."""
    if not os.path.isdir(migrations_dir):
        return []
    files = sorted(f for f in os.listdir(migrations_dir) if f.endswith(".sql"))
    if not files:
        return []
    _ensure_tracking_table()
    schema = get_workspace_schema()
    with get_engine().begin() as conn:
        applied = {
            row[0] for row in conn.execute(
                text(f'SELECT filename FROM "{schema}"."{_TRACKING_TABLE}" '
                     "WHERE app_id = :app_id"),
                {"app_id": app_id},
            )
        }
    return [f for f in files if f not in applied]


def apply_migrations(app_id: str, migrations_dir: str) -> list[str]:
    """Apply pending ``.sql`` files in filename order. Each file runs in its
    own transaction, recorded immediately after it commits — a failure stops
    the run (a later migration may depend on an earlier one) but leaves
    everything already-applied intact. Returns the filenames actually run
    this call (empty if nothing was pending)."""
    pending = pending_migrations(app_id, migrations_dir)
    schema = get_workspace_schema()
    applied: list[str] = []
    for filename in pending:
        path = os.path.join(migrations_dir, filename)
        with open(path, "r", encoding="utf-8") as f:
            sql = f.read()
        with get_engine().begin() as conn:
            if sql.strip():
                conn.execute(text(sql))
            conn.execute(
                text(f'INSERT INTO "{schema}"."{_TRACKING_TABLE}" (app_id, filename) '
                     "VALUES (:app_id, :filename)"),
                {"app_id": app_id, "filename": filename},
            )
        applied.append(filename)
        log.info("apps: applied migration %s for %s", filename, app_id)
    return applied
