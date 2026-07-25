"""Proves the F2 isolation primitive: two engines, each pinned to its own
schema via ``schema_translate_map``, only ever see their own rows.

Real-Postgres only (schemas are a real-Postgres concept) — skips cleanly if
127.0.0.1:5432 isn't reachable, same pattern as aw-backend's
``src/tests/unit/api/test_workspace_provisioner.py``.
"""
from __future__ import annotations

import psycopg
import pytest
from sqlalchemy import text
from sqlmodel import Session, SQLModel, create_engine

from src.api.db import get_db_url
from src.api.models import Setting


def _postgres_reachable() -> bool:
    try:
        conn = psycopg.connect(
            "postgresql://postgres:postgres@127.0.0.1:5432/postgres",
            autocommit=True,
            connect_timeout=2,
        )
        conn.close()
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _postgres_reachable(), reason="live Postgres at 127.0.0.1:5432 not reachable"
)

_SCHEMA_A = "workspace_f2isotesta"
_SCHEMA_B = "workspace_f2isotestb"


def _engine_for(schema: str):
    return create_engine(get_db_url()).execution_options(
        schema_translate_map={None: schema}
    )


@pytest.fixture()
def two_schemas():
    base_engine = create_engine(get_db_url())
    with base_engine.begin() as conn:
        conn.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{_SCHEMA_A}"'))
        conn.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{_SCHEMA_B}"'))

    engine_a = _engine_for(_SCHEMA_A)
    engine_b = _engine_for(_SCHEMA_B)
    SQLModel.metadata.create_all(engine_a, checkfirst=True)
    SQLModel.metadata.create_all(engine_b, checkfirst=True)

    yield engine_a, engine_b

    with base_engine.begin() as conn:
        conn.execute(text(f'DROP SCHEMA IF EXISTS "{_SCHEMA_A}" CASCADE'))
        conn.execute(text(f'DROP SCHEMA IF EXISTS "{_SCHEMA_B}" CASCADE'))


class TestSchemaTranslateMapIsolation:
    def test_each_engine_only_sees_its_own_schema(self, two_schemas):
        engine_a, engine_b = two_schemas

        with Session(engine_a) as session:
            session.add(Setting(key="k", value={"from": "a"}))
            session.commit()

        with Session(engine_b) as session:
            session.add(Setting(key="k", value={"from": "b"}))
            session.commit()

        with Session(engine_a) as session:
            row = session.get(Setting, "k")
            assert row is not None
            assert row.value == {"from": "a"}

        with Session(engine_b) as session:
            row = session.get(Setting, "k")
            assert row is not None
            assert row.value == {"from": "b"}

    def test_row_count_stays_scoped_per_schema(self, two_schemas):
        engine_a, engine_b = two_schemas

        with Session(engine_a) as session:
            session.add(Setting(key="only-in-a", value={}))
            session.commit()

        with Session(engine_b) as session:
            rows = session.exec(Setting.__table__.select()).fetchall()  # type: ignore[arg-type]
            assert rows == []
