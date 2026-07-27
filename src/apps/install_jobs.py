"""In-memory install-job tracker (async install — the BYOD-tunnel fix).

``POST /api/apps/install`` used to run the fetch + system-CLI ``apt install``
synchronously (30-60s) and hold the HTTP response open for the whole thing.
The BYOD tunnel (browser → aw-backend WorkspaceTunnelProxy → aw-workspace)
drops long-lived requests before the 200 lands, so the UI saw "Failed to
fetch" even though the install completed fine server-side. The install now
runs in a background task and this module tracks its progress per app id, so
the route can return immediately and the UI can poll
``GET /api/apps/{slug}/install-status``.

Per-process, in-memory only — a workspace restart mid-install loses the job,
same as the install itself (the ``AppInstall`` mirror row isn't written until
it finishes, so there's nothing further to lose).
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class InstallJob:
    app_id: str
    status: str = "installing"  # installing | installed | failed
    error: Optional[str] = None
    summary: Optional[dict[str, Any]] = None
    started_at: float = field(default_factory=time.time)
    finished_at: Optional[float] = None
    task: Optional["asyncio.Task"] = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "app_id": self.app_id,
            "status": self.status,
            "error": self.error,
            "summary": self.summary,
        }


class InstallJobs:
    """Tracks in-flight/finished background installs, keyed by app id."""

    def __init__(self) -> None:
        self._jobs: dict[str, InstallJob] = {}

    def get(self, app_id: str) -> Optional[InstallJob]:
        return self._jobs.get(app_id)

    def is_installing(self, app_id: str) -> bool:
        job = self._jobs.get(app_id)
        return bool(job and job.status == "installing")

    def start(self, app_id: str) -> InstallJob:
        job = InstallJob(app_id=app_id)
        self._jobs[app_id] = job
        return job

    def mark_installed(self, app_id: str, summary: dict[str, Any]) -> None:
        job = self._jobs.setdefault(app_id, InstallJob(app_id=app_id))
        job.status = "installed"
        job.summary = summary
        job.error = None
        job.finished_at = time.time()

    def mark_failed(self, app_id: str, error: str) -> None:
        job = self._jobs.setdefault(app_id, InstallJob(app_id=app_id))
        job.status = "failed"
        job.error = error
        job.finished_at = time.time()
