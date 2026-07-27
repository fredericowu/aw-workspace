"""Cloud registry client (ADR Decision 5 — F3).

The cloud (aw-backend ``app_installs``) is the **source of truth** for which
apps a workspace should be running. This is the workspace's poll-side client to
it: the reconciler lists the desired install rows here, and the workspace's own
install/uninstall flow writes them back so an install survives recreation.

Auth: the workspace authenticates to aw-backend with its own ``awlk_`` host
credential (``AW_WORKSPACE_HOST_TOKEN`` — the durable credential the
aw-remote-host ``/link`` handshake minted for this workspace), which aw-backend
accepts only for THIS workspace's app-installs (see ``app_installs.py``'s
``require_workspace_actor``). ``AW_BACKEND_URL`` + ``AW_WORKSPACE`` (slug) locate
the endpoint — the same env this runtime already uses for identity-JWT
verification.

When the cloud is not configured/reachable the client is *inert* by design:
``configured`` is False and the reconciler falls back to the local mirror, so a
BYOD workspace with no cloud link still boots its previously-installed apps.
"""
from __future__ import annotations

import logging
import os

import httpx

log = logging.getLogger(__name__)


class CloudRegistry:
    """HTTP client to aw-backend's ``/api/workspaces/{slug}/app-installs``."""

    def __init__(self, backend_url: str | None = None, workspace: str | None = None,
                 token: str | None = None, timeout: float = 15.0) -> None:
        self.backend_url = (backend_url or os.environ.get("AW_BACKEND_URL", "")).rstrip("/")
        self.workspace = workspace or os.environ.get("AW_WORKSPACE", "")
        self.token = token or os.environ.get("AW_WORKSPACE_HOST_TOKEN", "")
        self.timeout = timeout

    @property
    def configured(self) -> bool:
        return bool(self.backend_url and self.workspace and self.token)

    def _base(self) -> str:
        return f"{self.backend_url}/api/workspaces/{self.workspace}/app-installs"

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}

    def list_desired(self) -> list[dict]:
        """Return the desired install rows for this workspace (empty if unset)."""
        if not self.configured:
            return []
        resp = httpx.get(self._base(), headers=self._headers(), timeout=self.timeout)
        resp.raise_for_status()
        return resp.json().get("app_installs", [])

    def put_desired(self, app_id: str, *, version: str, repo: str | None = None,
                    ref: str = "HEAD", granted_permissions: list[str] | None = None,
                    config: dict | None = None, instance_id: str = "",
                    signed: bool = False) -> dict:
        """Upsert the desired state for one app (the workspace's install flow)."""
        if not self.configured:
            return {}
        body: dict = {"version": version, "ref": ref, "instance_id": instance_id,
                      "signed": signed}
        if repo is not None:
            body["repo"] = repo
        if granted_permissions is not None:
            body["granted_permissions"] = granted_permissions
        if config is not None:
            body["config"] = config
        resp = httpx.put(f"{self._base()}/{app_id}", json=body,
                         headers=self._headers(), timeout=self.timeout)
        resp.raise_for_status()
        return resp.json()

    def delete_desired(self, app_id: str, instance_id: str = "") -> None:
        """Remove the desired state for one app (the workspace's uninstall flow)."""
        if not self.configured:
            return
        resp = httpx.delete(f"{self._base()}/{app_id}",
                            params={"instance_id": instance_id},
                            headers=self._headers(), timeout=self.timeout)
        if resp.status_code not in (200, 404):
            resp.raise_for_status()
