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


class Notification(SQLModel, table=True):
    """Persistent notification queue — one row per notification event.

    Strangler-fig port of the monolith's ``src/api/db_models.py::Notification``
    (identical columns) — see ``src/api/notifications.py`` / ``notification_db.py``.
    """

    __tablename__ = "notifications"  # type: ignore[assignment]

    id: Optional[int] = Field(default=None, primary_key=True)
    message: str
    level: str = Field(default="info")
    title: str = Field(default="")
    source: str = Field(default="")
    url: str = Field(default="")
    external_id: str = Field(default="")
    external_status: str = Field(default="")
    status: str = Field(default="new")
    created_at: Optional[float] = Field(default=None)
    delivered_at: Optional[float] = Field(default=None)
    read_at: Optional[float] = Field(default=None)


class AppInstall(SQLModel, table=True):
    """A locally-installed decoupled app (F1 minimal registry).

    The cloud (aw-backend ``app_installs``) is the source of truth for the
    reconciler (F3); F1 persists the install here too so the workspace can
    reload its apps on boot without a round-trip. One row per installed app.
    """

    __tablename__ = "app_installs"  # type: ignore[assignment]

    slug: str = Field(primary_key=True)
    version: str
    package_dir: str
    # Where the reconciler (re)fetches the package (F3). ``repo`` None means the
    # app was installed straight from an on-disk ``package_dir`` (e.g. the bundled
    # PoC) with no git source to re-clone.
    repo: Optional[str] = Field(default=None)
    ref: str = Field(default="HEAD")
    granted_permissions: list[str] = Field(default_factory=list, sa_column=Column(JSONB))
    config: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSONB))
    signed: bool = Field(default=False)
    enabled: bool = True
