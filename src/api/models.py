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


class GuestUser(SQLModel, table=True):
    """A username/password login scoped to a fixed list of this workspace's apps.

    Backs Settings > General > Users. Ported from the monolith/aw-backend
    ``db_models.py::GuestUser``, but stored in THIS workspace's own schema
    rather than the control plane's: the Settings SPA reaches its API at
    ``api.<slug>.workspace.<apex>`` (see ``apiBase.js``'s fetch rewrite), so
    a control-plane-only implementation is unreachable from the tab that
    manages it — which is exactly why the Users tab 404'd.

    ``allowed_apps`` is JSONB here, not the reference's generic ``JSON`` —
    every other JSON column in this module is JSONB and the engine is always
    Postgres (``src.api.db``), so there's no portability reason to differ.

    **A row here does not yet grant access to anything.** Only the admin-side
    CRUD is implemented; there is no guest login in this workspace runtime —
    see ``src/api/guest_users.py``'s module docstring for why that half needs
    a control-plane decision rather than a port.
    """

    __tablename__ = "guest_users"  # type: ignore[assignment]

    id: Optional[int] = Field(default=None, primary_key=True)
    username: str = Field(unique=True, index=True)
    password_hash: str
    allowed_apps: list[str] = Field(default_factory=list, sa_column=Column(JSONB))
    created_at: Optional[float] = Field(default=None)


class MarketplaceSource(SQLModel, table=True):
    """One marketplace the catalog is merged from — user-managed, private-capable.

    Replaces the ``AW_MARKETPLACE_SOURCES`` env var as the source of truth
    (the env var stays as a seed/fallback, see ``src/apps/catalog.py``): a
    user adds a marketplace from Settings, which has to survive a container
    recreation and be editable without an ops-side env change.

    **No credential is stored on this row.** ``auth_type``/``auth_host``
    describe *how* to authenticate; the token itself lives encrypted in the
    workspace secret store (``src/apps/secret_store.py``, namespace
    ``_marketplace``, key = this row's ``id``). Keeping it out of the table
    means the plain ``GET /api/marketplace/sources`` response — and any
    future dump of this table — can never leak it.

    ``auth_host`` is the anti-exfiltration control. A single global token
    sent to every configured source means adding ``https://evil.example/
    apps.json`` hands that host your GitHub PAT. The credential is therefore
    bound to the host it belongs to and the header is only attached when the
    request's host matches — same model as ``.netrc``/``git credential``.
    """

    __tablename__ = "marketplace_sources"  # type: ignore[assignment]

    id: str = Field(primary_key=True)
    name: str = Field(default="")
    # Either ``owner/repo``/``owner/repo@ref`` or a full http(s) URL to the
    # catalog JSON — the two shapes ``catalog._raw_url_for`` already accepts.
    url: str
    enabled: bool = Field(default=True)
    # Merge order (ascending). Ties are broken by ``id`` so the merge is
    # deterministic; first source wins on duplicate app ids.
    priority: int = Field(default=100)
    # "none" | "github_pat" | "bearer"
    auth_type: str = Field(default="none")
    # Host the credential may be sent to, e.g. "github.com". Empty when
    # ``auth_type`` is "none".
    auth_host: str = Field(default="")
    created_at: Optional[float] = Field(default=None)
