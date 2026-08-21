"""Action Journal (ADR Decision 7) — minimal F1 scope.

Every side effect an app performs through its ``AppContext`` is journaled as an
append-only entry. **Uninstall = replay the journal in reverse.** F1 journals
the one side effect the runtime supports: backend route mounts (``route:mount``).
The journal also backs the "what did this app do" audit view.

F1 keeps the journal in memory (single-worker process). Persisting it to the
workspace PG table ``app__core__actions`` is F4's scope; the shape here is
forward-compatible with that.
"""
from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from typing import Any


@dataclass
class JournalEntry:
    seq: int
    app_id: str
    kind: str  # e.g. "route:mount"
    target: str  # human-readable target, e.g. "/api/apps/aw-app-template"
    payload: dict[str, Any] = field(default_factory=dict)


class ActionJournal:
    """In-memory append-only journal, keyed for per-app reverse replay."""

    def __init__(self) -> None:
        self._entries: list[JournalEntry] = []
        self._seq = itertools.count(1)

    def record(self, app_id: str, kind: str, target: str,
               payload: dict[str, Any] | None = None) -> JournalEntry:
        entry = JournalEntry(
            seq=next(self._seq), app_id=app_id, kind=kind,
            target=target, payload=payload or {},
        )
        self._entries.append(entry)
        return entry

    def entries_for(self, app_id: str) -> list[JournalEntry]:
        """This app's entries in append order."""
        return [e for e in self._entries if e.app_id == app_id]

    def reverse_for(self, app_id: str) -> list[JournalEntry]:
        """This app's entries newest-first — the uninstall replay order."""
        return list(reversed(self.entries_for(app_id)))

    def clear_app(self, app_id: str) -> None:
        """Drop an app's entries once its uninstall replay is complete.

        No residue must remain after uninstall (ADR acceptance criterion).
        """
        self._entries = [e for e in self._entries if e.app_id != app_id]

    def all_entries(self) -> list[JournalEntry]:
        return list(self._entries)
