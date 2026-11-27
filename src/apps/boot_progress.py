"""Boot-reconcile progress, published as an atomic JSON file.

The boot leader writes it beside the per-boot done marker
(``boot_reconcile_coord._write_marker`` pattern: tmp + fsync + ``os.replace``),
so ``GET /api/apps/-/boot-status`` can be answered by ANY worker — each worker
has its own loaded set in memory, only the file is shared.

Shape::

    {"boot_id", "phase": "reconciling|done|failed", "total", "loaded",
     "in_flight": [app_id, ...], "started_at", "updated_at"}

Everything here is best-effort: a progress write must never fail a reconcile.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from typing import Any, Awaitable, Callable, Optional

from src.apps import fs_lock

log = logging.getLogger(__name__)

PROGRESS_FILE = "boot-reconcile-progress.json"


def progress_path(lock_dir: str | None = None) -> str:
    return os.path.join(lock_dir or fs_lock.lock_dir(), PROGRESS_FILE)


def read_progress(lock_dir: str | None = None) -> dict[str, Any]:
    """The last published progress, or ``{"phase": "idle"}`` when no boot pass
    has written one (fresh dir, or the file is mid-replace/unreadable)."""
    try:
        with open(progress_path(lock_dir), encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and data.get("phase"):
            return data
    except (OSError, ValueError):
        pass
    return {"phase": "idle", "total": 0, "loaded": 0, "in_flight": []}


class BootProgress:
    """Tracks one boot pass and publishes every change to the file and,
    through ``on_change``, to the WebSocket layer."""

    def __init__(self, boot_id: str, lock_dir: str | None = None,
                 on_change: Optional[Callable[[dict], Awaitable[None]]] = None):
        self.boot_id = boot_id
        self._lock_dir = lock_dir
        self.on_change = on_change
        self._lock = threading.Lock()
        self._state: dict[str, Any] = {
            "boot_id": boot_id, "phase": "reconciling", "total": 0,
            "loaded": 0, "in_flight": [], "started_at": time.time(),
            "updated_at": time.time()}

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {**self._state, "in_flight": list(self._state["in_flight"])}

    def _write(self) -> None:
        path = progress_path(self._lock_dir)
        tmp = f"{path}.tmp.{os.getpid()}.{threading.get_ident()}"
        try:
            with self._lock:
                self._state["updated_at"] = time.time()
                payload = json.dumps(self._state)
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(payload)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
        except Exception:  # noqa: BLE001 — best-effort by contract
            log.warning("apps: could not write boot progress file", exc_info=True)

    async def _publish(self) -> None:
        self._write()
        if self.on_change is not None:
            try:
                await self.on_change(self.snapshot())
            except Exception:  # noqa: BLE001
                log.warning("apps: boot progress notify failed", exc_info=True)

    async def begin(self, total: int, loaded: int) -> None:
        with self._lock:
            self._state.update(total=total, loaded=loaded, phase="reconciling")
        await self._publish()

    async def app_started(self, app_id: str) -> None:
        with self._lock:
            if app_id not in self._state["in_flight"]:
                self._state["in_flight"].append(app_id)
        await self._publish()

    async def app_finished(self, app_id: str) -> None:
        with self._lock:
            if app_id in self._state["in_flight"]:
                self._state["in_flight"].remove(app_id)
            self._state["loaded"] = min(self._state["total"] or 10**9,
                                        self._state["loaded"] + 1)
        await self._publish()

    async def finish(self, ok: bool) -> None:
        with self._lock:
            self._state["phase"] = "done" if ok else "failed"
            self._state["in_flight"] = []
        await self._publish()
