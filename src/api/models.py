"""SQLModel tables for this workspace's own schema.

Declared WITHOUT a schema — ``db.get_engine()``'s ``schema_translate_map``
is what routes them into ``AW_WORKSPACE_SCHEMA`` at execution time. Adding
``__table_args__ = {"schema": ...}`` here would defeat that and hard-code
one workspace's schema into every process.

``Setting`` is the only table F2 needs — a generic KV store to prove the
translate-map round-trip. Runtime tables (apps/app_configs/runs/...) are
F5's scope.
"""
from __future__ import annotations

from typing import Any, Optional

from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Column, Field, SQLModel


class Setting(SQLModel, table=True):
    __tablename__ = "settings"  # type: ignore[assignment]

    key: str = Field(primary_key=True)
    value: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSONB))
