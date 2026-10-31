"""Core migration engine (src/apps/migrations.py) + the loud-failure contract
in ``AppRuntime._apply_migrations`` (src/apps/runtime.py).

Card: core-app-migration-failures-are-silently-swallowed-instead-of-failing-
the-installreload. Two regressions this guards against:

1. A migration failure used to be ``log.exception``'d and swallowed, so an
   app's install/reload reported green while serving routes against a
   schema it never actually migrated to. ``_apply_migrations`` must now
   raise.
2. The raw ``.sql`` file body used to run through SQLAlchemy ``text()``,
   which scans the WHOLE file — comments included — for ``:word`` and
   treats every match as a missing bind parameter. A migration whose header
   comment describes a Python slice like ``items[:40]`` then failed with
   "a value is required for bind parameter '40'" even though the file has
   no real bind params and applies fine via ``psql``. ``exec_driver_sql``
   passes the string straight to the DBAPI with no such scanning.

No live Postgres needed: ``get_engine``/``get_workspace_schema`` are
monkeypatched to a minimal fake engine that only implements what
``apply_migrations``/``pending_migrations`` actually call.
"""
from __future__ import annotations

import types

import pytest
from sqlalchemy import text

from src.apps import migrations as migrations_mod
from src.apps.runtime import AppRuntime


class _FakeConn:
    """Records every call; returns no rows for the SELECT in
    ``pending_migrations`` so every file on disk is treated as pending."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, object]] = []

    def execute(self, stmt, params=None):
        self.calls.append(("execute", str(stmt), params))
        if "SELECT filename" in str(stmt):
            return []
        return None

    def exec_driver_sql(self, sql):
        self.calls.append(("exec_driver_sql", sql, None))
        return None


class _FakeBeginCtx:
    def __init__(self, conn: _FakeConn) -> None:
        self._conn = conn

    def __enter__(self) -> _FakeConn:
        return self._conn

    def __exit__(self, *exc) -> bool:
        return False


class _FakeEngine:
    """One shared connection across every ``with get_engine().begin()``
    block, same as a real engine reusing its pool within one call."""

    def __init__(self, conn: _FakeConn) -> None:
        self._conn = conn

    def begin(self) -> _FakeBeginCtx:
        return _FakeBeginCtx(self._conn)


@pytest.fixture
def fake_conn(monkeypatch) -> _FakeConn:
    conn = _FakeConn()
    monkeypatch.setattr(migrations_mod, "get_engine", lambda: _FakeEngine(conn))
    monkeypatch.setattr(migrations_mod, "get_workspace_schema", lambda: "testschema")
    return conn


def _write_migration(tmp_path, filename: str, sql: str) -> str:
    migrations_dir = tmp_path / "migrations"
    migrations_dir.mkdir(exist_ok=True)
    (migrations_dir / filename).write_text(sql)
    return str(migrations_dir)


# The exact pattern from the incident: a header comment describing a Python
# slice, which SQLAlchemy's text() misreads as a bind parameter named "40".
_COLON_COMMENT_SQL = """-- header: the first items[:40] of a list, not SQL
CREATE TABLE IF NOT EXISTS demo (id INTEGER, body TEXT);
INSERT INTO demo (id, body) VALUES (1, 'hello');
"""


def test_apply_migrations_does_not_choke_on_a_colon_comment(fake_conn, tmp_path):
    """The exec_driver_sql fix: a file containing ``[:40]`` in a comment must
    not raise a missing-bind-parameter error — the whole false-positive
    class this card asked to eliminate, not just detect."""
    migrations_dir = _write_migration(tmp_path, "0001_demo.sql", _COLON_COMMENT_SQL)

    applied = migrations_mod.apply_migrations("demoapp", migrations_dir)

    assert applied == ["0001_demo.sql"]
    # The raw file body must go through exec_driver_sql, not execute(text(...)) —
    # that's the actual mechanism that avoids the bind-param scan.
    file_body_calls = [c for c in fake_conn.calls if c[0] == "exec_driver_sql"]
    assert len(file_body_calls) == 1
    assert file_body_calls[0][1] == _COLON_COMMENT_SQL
    # Real bind params (the tracking-table insert) must still be used as such.
    insert_calls = [c for c in fake_conn.calls if "INSERT INTO" in c[1] and c[0] == "execute"]
    assert len(insert_calls) == 1
    assert insert_calls[0][2] == {"app_id": "demoapp", "filename": "0001_demo.sql"}


def test_apply_migrations_propagates_a_real_sql_failure(monkeypatch, tmp_path):
    """Distinct from the colon-comment false positive above (which the
    exec_driver_sql fix eliminates entirely): a migration file with a
    genuine SQL error must still fail — and that failure must come straight
    out of ``apply_migrations`` with nothing between it and the caller.
    ``_apply_migrations`` (runtime.py) is what used to swallow this."""
    migrations_dir = _write_migration(
        tmp_path, "0001_bad.sql", "ALTER TABLE this_table_does_not_exist ADD COLUMN x int;")

    conn = _FakeConn()
    monkeypatch.setattr(migrations_mod, "get_engine", lambda: _FakeEngine(conn))
    monkeypatch.setattr(migrations_mod, "get_workspace_schema", lambda: "testschema")

    def _exec_driver_sql(self, sql):
        raise RuntimeError('relation "this_table_does_not_exist" does not exist')

    monkeypatch.setattr(_FakeConn, "exec_driver_sql", _exec_driver_sql)

    with pytest.raises(RuntimeError, match="does not exist"):
        migrations_mod.apply_migrations("demoapp", migrations_dir)


def test_text_still_raises_on_the_same_sql_demonstrating_the_bug(tmp_path):
    """Sanity check on the mechanism itself, independent of this app's code:
    SQLAlchemy's ``text()`` really does choke on the exact pattern above,
    which is why swapping it for ``exec_driver_sql`` is the real fix and not
    just a cosmetic change."""
    from sqlalchemy import create_engine
    from sqlalchemy.exc import StatementError

    engine = create_engine("sqlite://")
    with pytest.raises(StatementError, match="bind parameter '40'"):
        with engine.begin() as conn:
            conn.execute(text(_COLON_COMMENT_SQL))


def test_apply_runtime_migrations_raises_instead_of_swallowing(monkeypatch, tmp_path):
    """``AppRuntime._apply_migrations`` must propagate a migration failure —
    it used to ``log.exception`` and return normally, which is what let an
    app's install/reload report green against a half-migrated schema."""

    def _boom(app_id, migrations_dir):
        raise RuntimeError("a value is required for bind parameter '40'")

    monkeypatch.setattr(migrations_mod, "apply_migrations", _boom)

    manifest = types.SimpleNamespace(id="demoapp", migrations={"dir": "migrations"})

    # _apply_migrations never touches `self` — exercising it as an unbound
    # function call keeps this test free of AppRuntime's own setup.
    with pytest.raises(RuntimeError, match="bind parameter '40'"):
        AppRuntime._apply_migrations(None, manifest, str(tmp_path))


def test_apply_runtime_migrations_is_a_noop_when_app_declares_no_migrations(tmp_path):
    manifest = types.SimpleNamespace(id="demoapp", migrations={})
    # Must not raise and must not try to reach the DB at all.
    AppRuntime._apply_migrations(None, manifest, str(tmp_path))
