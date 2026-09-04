"""The engine-wide statement/lock timeouts, and the boot path's opt-out.

``src/api/db.py`` sets ``statement_timeout``/``lock_timeout`` on every pooled
connection so a query wedged behind a lock has SOME upper bound — added
2026-09-04, when a request-path stall with no bound at all was one of the
things that could freeze a worker indefinitely.

The risk that comes with it is the reason this file exists: ``create_all_tables``
is *supposed* to block. Its ``pg_advisory_lock`` is exactly how N workers
booting at the same instant are serialised (W2), and ``create_all`` takes an
ACCESS EXCLUSIVE catalog lock per table. Capping either would convert a slow
boot into a crash loop — strictly worse than the hang the caps exist to bound.
So the boot connection clears both, and must put them back before the
connection returns to the pool: neither SQLAlchemy's rollback-on-return nor
the ``with`` block resets a session GUC, so a leaked ``statement_timeout = 0``
would silently disarm the cap for whichever request picked that connection up
next.

Needs no Postgres — it drives ``create_all_tables`` against a fake connection
and asserts on the SQL it issued, which is the whole property under test.
"""
from __future__ import annotations

from contextlib import contextmanager

import pytest

from src.api import db as db_mod


class _FakeConn:
    def __init__(self) -> None:
        self.statements: list[str] = []

    def execute(self, clause, params=None):
        self.statements.append(str(clause).strip())
        return None

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def execution_options(self, **_kwargs):
        return self


class _FakeEngine:
    def __init__(self, conn: _FakeConn) -> None:
        self._conn = conn

    def connect(self):
        return self._conn


@contextmanager
def _boot(monkeypatch, create_all_raises: Exception | None = None):
    conn = _FakeConn()
    monkeypatch.setattr(db_mod, "get_engine", lambda: _FakeEngine(conn))
    monkeypatch.setattr(db_mod, "get_workspace_schema", lambda: "workspace_test")
    monkeypatch.delenv("AW_WORKSPACE_DB_URL", raising=False)

    from sqlmodel import SQLModel

    def fake_create_all(bind, checkfirst=True):
        bind.execute("CREATE TABLE …")
        if create_all_raises is not None:
            raise create_all_raises

    monkeypatch.setattr(SQLModel.metadata, "create_all", fake_create_all)
    yield conn


def test_engine_applies_statement_and_lock_timeouts(monkeypatch):
    """The caps reach libpq as connection options, not as a stray kwarg."""
    import sqlalchemy

    captured: dict = {}

    def fake_create_engine(url, **kwargs):
        captured.update(kwargs)
        return sqlalchemy.create_engine("sqlite://")

    monkeypatch.setattr(db_mod, "create_engine", fake_create_engine)
    monkeypatch.setattr(db_mod, "get_workspace_schema", lambda: "workspace_test")
    monkeypatch.setattr(db_mod, "_engine", None)
    try:
        db_mod.get_engine()
    finally:
        db_mod._engine = None  # never leave the sqlite stand-in cached

    options = captured["connect_args"]["options"]
    assert f"-c statement_timeout={db_mod._STATEMENT_TIMEOUT_MS}" in options
    assert f"-c lock_timeout={db_mod._LOCK_TIMEOUT_MS}" in options


def test_boot_clears_the_timeouts_before_taking_the_advisory_lock(monkeypatch):
    with _boot(monkeypatch) as conn:
        db_mod.create_all_tables()

    joined = conn.statements
    assert "SET statement_timeout = 0" in joined
    assert "SET lock_timeout = 0" in joined

    lock_at = next(i for i, s in enumerate(joined) if "pg_advisory_lock" in s)
    ddl_at = next(i for i, s in enumerate(joined) if s.startswith("CREATE TABLE"))
    for cleared in ("SET statement_timeout = 0", "SET lock_timeout = 0"):
        assert joined.index(cleared) < lock_at, (
            f"{cleared!r} must come before the advisory lock — the lock is "
            "designed to block while another worker holds it"
        )
    assert lock_at < ddl_at


def test_boot_restores_the_timeouts_before_returning_the_connection(monkeypatch):
    with _boot(monkeypatch) as conn:
        db_mod.create_all_tables()

    assert conn.statements[-2:] == [
        f"SET statement_timeout = {db_mod._STATEMENT_TIMEOUT_MS}",
        f"SET lock_timeout = {db_mod._LOCK_TIMEOUT_MS}",
    ], (
        "the boot connection goes back to the pool with the caps disarmed — "
        f"got {conn.statements[-2:]}"
    )


def test_timeouts_are_restored_even_when_the_ddl_fails(monkeypatch):
    """A failed boot must not poison the pool either."""
    with _boot(monkeypatch, create_all_raises=RuntimeError("boom")) as conn:
        with pytest.raises(RuntimeError, match="boom"):
            db_mod.create_all_tables()

    assert any("pg_advisory_unlock" in s for s in conn.statements)
    assert conn.statements[-2:] == [
        f"SET statement_timeout = {db_mod._STATEMENT_TIMEOUT_MS}",
        f"SET lock_timeout = {db_mod._LOCK_TIMEOUT_MS}",
    ]
