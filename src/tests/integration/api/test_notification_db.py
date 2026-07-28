"""``NotificationDB`` CRUD against a real schema-isolated Postgres table.

Real-Postgres only — skips cleanly if 127.0.0.1:5432 isn't reachable, same
pattern as ``test_isolation`` / ``test_db_tables``.
"""
from __future__ import annotations

import psycopg
import pytest
from sqlalchemy import text


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

_SCHEMA = "workspace_notifdbtest"


@pytest.fixture()
def db(monkeypatch):
    monkeypatch.setenv("AW_WORKSPACE_SCHEMA", _SCHEMA)
    monkeypatch.setenv("AW_WORKSPACE_DB_URL",
                       "postgresql://postgres:postgres@127.0.0.1:5432/awserv")
    import src.api.db as dbmod
    monkeypatch.setattr(dbmod, "_engine", None)
    from src.api.db import create_all_tables, get_engine
    create_all_tables()

    from src.api.notification_db import NotificationDB
    yield NotificationDB()

    with get_engine().begin() as conn:
        conn.execute(text(f'DROP SCHEMA IF EXISTS "{_SCHEMA}" CASCADE'))
    monkeypatch.setattr(dbmod, "_engine", None)


def test_add_and_get_pending(db):
    notif = db.add(message="hello", level="info", source="test")
    assert notif["status"] == "new"
    pending = db.get_pending()
    assert any(n["id"] == notif["id"] for n in pending)


def test_mark_delivered_then_read_moves_out_of_pending(db):
    notif = db.add(message="hi", source="test")
    db.mark_delivered(notif["id"])
    assert any(n["id"] == notif["id"] and n["status"] == "delivered" for n in db.get_pending())
    db.mark_read(notif["id"])
    assert all(n["id"] != notif["id"] for n in db.get_pending())


def test_dismiss_is_alias_for_mark_read(db):
    notif = db.add(message="dismiss me", source="test")
    db.dismiss(notif["id"])
    assert all(n["id"] != notif["id"] for n in db.get_pending())


def test_has_notification_dedup(db):
    db.add(message="a", source="ci", external_id="pr-1", external_status="open")
    assert db.has_notification("ci", "pr-1", "open") is True
    assert db.has_notification("ci", "pr-1", "closed") is False


def test_supersedes_marks_old_pending_as_superseded(db):
    first = db.add(message="first", source="ci", external_id="pr-2", external_status="open")
    second = db.add(message="second", source="ci", external_id="pr-2", external_status="merged")

    ids_pending = {n["id"] for n in db.get_pending()}
    assert second["id"] in ids_pending
    assert first["id"] not in ids_pending

    recent = {n["id"]: n["status"] for n in db.get_recent()}
    assert recent[first["id"]] == "superseded"


def test_get_recent_respects_limit(db):
    for i in range(3):
        db.add(message=f"m{i}", source="test")
    assert len(db.get_recent(limit=2)) == 2
