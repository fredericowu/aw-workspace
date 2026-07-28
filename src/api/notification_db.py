"""PostgreSQL-backed notification queue — SQLModel ORM.

Strangler-fig port of the monolith's ``src/api/notification_db.py``: identical
public API (``add``/``get_pending``/``get_recent``/``mark_delivered``/
``mark_read``/``dismiss``/``has_notification``/``cleanup_old``), rebound to
this workspace's own schema-isolated engine (``src.api.db``) instead of the
monolith's ``src.api.pg_db``.

Each public method opens its own ``Session`` and commits inside that scope so
the session is always returned to the pool cleanly.
"""

from __future__ import annotations

import logging
import time

from sqlmodel import select

from src.api.db import get_session
from src.api.models import Notification

log = logging.getLogger("notifications")


class NotificationDB:
    """Thin ORM wrapper around this workspace's ``notifications`` table."""

    def __init__(self) -> None:
        # Kick out stale notifications from previous runs on startup. Best-effort:
        # this runs at NotificationManager construction time, which normally
        # follows create_all_tables() — but must not crash app boot if the DB
        # isn't reachable/migrated yet (mirrors reconcile_on_boot's contract).
        try:
            self.cleanup_old()
        except Exception:
            log.exception("notifications: startup cleanup_old failed")

    # ------------------------------------------------------------------
    # Write operations

    def add(
        self,
        message: str,
        level: str = "info",
        title: str = "",
        source: str = "",
        url: str = "",
        external_id: str = "",
        external_status: str = "",
        supersedes: bool = True,
    ) -> dict:
        """Insert a notification and return it as a plain dict.

        If *supersedes* is ``True`` and *external_id* is set, any pending
        notifications for the same ``(source, external_id)`` are marked
        ``'superseded'`` so they won't display. Use ``supersedes=False``
        for events like comments where every entry matters.
        """
        now = time.time()

        with get_session() as session:
            superseded_ids: list[int] = []

            if supersedes and external_id:
                stmt = (
                    select(Notification)
                    .where(
                        Notification.source == source,
                        Notification.external_id == external_id,
                        Notification.status.in_(["new", "delivered"]),
                    )
                )
                old_rows = session.exec(stmt).all()
                superseded_ids = [n.id for n in old_rows]
                for n in old_rows:
                    n.status = "superseded"
                    session.add(n)

            notif = Notification(
                message=message,
                level=level,
                title=title,
                source=source,
                url=url,
                external_id=external_id,
                external_status=external_status,
                status="new",
                created_at=now,
            )
            session.add(notif)
            session.commit()
            session.refresh(notif)

            result = notif.model_dump()
            result["superseded_ids"] = superseded_ids
            return result

    def mark_delivered(self, notif_id: int) -> None:
        """Set status to ``'delivered'`` and record ``delivered_at``."""
        with get_session() as session:
            notif = session.get(Notification, notif_id)
            if notif:
                notif.status = "delivered"
                notif.delivered_at = time.time()
                session.add(notif)
                session.commit()

    def mark_read(self, notif_id: int) -> None:
        """Set status to ``'read'`` and record ``read_at``."""
        with get_session() as session:
            notif = session.get(Notification, notif_id)
            if notif:
                notif.status = "read"
                notif.read_at = time.time()
                session.add(notif)
                session.commit()

    def dismiss(self, notif_id: int) -> None:
        """Alias for :meth:`mark_read`."""
        self.mark_read(notif_id)

    def cleanup_old(self, days: int = 7) -> None:
        """Delete notifications older than *days* days."""
        cutoff = time.time() - (days * 86400)
        with get_session() as session:
            stmt = select(Notification).where(Notification.created_at < cutoff)
            old = session.exec(stmt).all()
            for n in old:
                session.delete(n)
            session.commit()

    # ------------------------------------------------------------------
    # Read operations

    def get_pending(self) -> list[dict]:
        """Return notifications with status ``'new'`` or ``'delivered'``, oldest first."""
        with get_session() as session:
            stmt = (
                select(Notification)
                .where(Notification.status.in_(["new", "delivered"]))
                .order_by(Notification.created_at)
            )
            return [n.model_dump() for n in session.exec(stmt).all()]

    def get_recent(self, limit: int = 50) -> list[dict]:
        """Return the last *limit* notifications regardless of status."""
        with get_session() as session:
            stmt = (
                select(Notification)
                .order_by(Notification.created_at.desc())
                .limit(limit)
            )
            return [n.model_dump() for n in session.exec(stmt).all()]

    def has_notification(
        self,
        source: str,
        external_id: str,
        external_status: str,
    ) -> bool:
        """Return ``True`` if a matching notification already exists."""
        with get_session() as session:
            stmt = (
                select(Notification)
                .where(
                    Notification.source == source,
                    Notification.external_id == external_id,
                    Notification.external_status == external_status,
                )
                .limit(1)
            )
            return session.exec(stmt).first() is not None
