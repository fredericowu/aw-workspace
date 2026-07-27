"""Apps reconciler (ADR Decision 5 — the core of F3).

One mechanism drives everything: compute **desired** state (the cloud registry's
``app_installs`` rows for this workspace — source of truth) vs **actual** state
(the plugins currently loaded in this process), then converge — install the
missing, uninstall the extra. This single reconcile IS both:

* **auto-reinstall on workspace recreation** — a fresh runtime boots with an
  empty loaded-set; the registry still lists the user's apps, so reconcile
  fetches + hot-loads each one with no manual step; and
* **"Install My Apps"** — the endpoint just re-runs reconcile against the same
  registry after the user's rows have been written.

The install flow is: fetch the app repo (``fetch``) → validate its manifest (F1)
→ resolve + enforce granted permissions (F2) → **hot-load** into the runtime (F1,
no restart) → write the local mirror + cloud registry rows. Uninstall reverses
it: unload (F1 drain + Action-Journal reverse-replay) → drop the mirror/registry
rows → remove the cloned repo.

Everything effectful (fetch/remove, cloud I/O, local-mirror persistence) is
injected so the reconciler is unit-testable without a network or a live cloud —
the defaults wire the real git fetch, the ``CloudRegistry`` HTTP client, and the
workspace's ``AppInstall`` PG mirror.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Any, Callable

from src.apps import fetch as fetch_mod
from src.apps.manifest import load_manifest
from src.apps.registry_client import CloudRegistry
from src.apps.runtime import AppRuntime

log = logging.getLogger(__name__)


@dataclass
class AppSpec:
    """Normalized desired-state for one app (from a cloud or local row)."""

    app_id: str
    version: str = ""
    repo: str | None = None
    ref: str = "HEAD"
    granted_permissions: list[str] = field(default_factory=list)
    config: dict[str, Any] = field(default_factory=dict)
    signed: bool = False
    package_dir: str | None = None  # already-on-disk source (skip fetch)
    state: str = "installed"

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> "AppSpec":
        app_id = row.get("app_id") or row.get("slug") or ""
        return cls(
            app_id=app_id,
            version=row.get("version", "") or "",
            repo=row.get("repo"),
            ref=row.get("ref") or "HEAD",
            granted_permissions=list(row.get("granted_permissions") or []),
            config=dict(row.get("config") or {}),
            signed=bool(row.get("signed", False)),
            package_dir=row.get("package_dir"),
            state=row.get("state", "installed") or "installed",
        )


# --- local mirror (workspace ``AppInstall`` PG table) ------------------------


class LocalMirror:
    """The workspace's own ``AppInstall`` rows — a cache of what's loaded so the
    process can boot its apps without a cloud round-trip. The cloud registry is
    the source of truth; this mirrors it."""

    def list(self) -> list[dict[str, Any]]:
        from sqlmodel import select

        from src.api.db import get_session
        from src.api.models import AppInstall

        with get_session() as session:
            rows = list(session.exec(select(AppInstall).where(AppInstall.enabled == True)))  # noqa: E712
            return [
                {"app_id": r.slug, "version": r.version, "package_dir": r.package_dir,
                 "repo": r.repo, "ref": r.ref, "granted_permissions": r.granted_permissions,
                 "config": r.config, "state": "installed"}
                for r in rows
            ]

    def upsert(self, spec: "AppSpec", package_dir: str) -> None:
        from src.api.db import get_session
        from src.api.models import AppInstall

        with get_session() as session:
            row = session.get(AppInstall, spec.app_id)
            if row is None:
                row = AppInstall(slug=spec.app_id, version=spec.version,
                                 package_dir=package_dir)
            row.version = spec.version
            row.package_dir = package_dir
            row.repo = spec.repo
            row.ref = spec.ref
            row.granted_permissions = spec.granted_permissions
            row.config = spec.config
            row.enabled = True
            session.add(row)
            session.commit()

    def forget(self, app_id: str) -> None:
        from src.api.db import get_session
        from src.api.models import AppInstall

        with get_session() as session:
            row = session.get(AppInstall, app_id)
            if row is not None:
                session.delete(row)
                session.commit()


class Reconciler:
    """Converges the running app set to the cloud registry's desired state."""

    def __init__(self, runtime: AppRuntime, *, cloud: CloudRegistry | None = None,
                 local: LocalMirror | None = None,
                 fetch: Callable[..., str] = fetch_mod.fetch_app_repo,
                 remove: Callable[[str], bool] = fetch_mod.remove_app_repo) -> None:
        self.runtime = runtime
        self.cloud = cloud if cloud is not None else CloudRegistry()
        self.local = local if local is not None else LocalMirror()
        self._fetch = fetch
        self._remove = remove

    # ---- resolve a package dir for a spec (fetch unless already on disk) ----

    def _resolve_package_dir(self, spec: AppSpec) -> str:
        if spec.repo:
            return self._fetch(spec.repo, spec.ref, slug=spec.app_id)
        if spec.package_dir and os.path.isdir(spec.package_dir):
            return spec.package_dir
        raise ValueError(
            f"app {spec.app_id!r} has neither a repo to fetch nor an on-disk package_dir")

    # ---- install / uninstall -----------------------------------------------

    async def install(self, spec: AppSpec, *, write_cloud: bool = True) -> dict[str, Any]:
        """Fetch → validate → enforce → hot-load → persist. Returns a summary."""
        package_dir = self._resolve_package_dir(spec)
        manifest = load_manifest(package_dir)
        if spec.app_id and manifest.id != spec.app_id:
            raise ValueError(
                f"manifest id {manifest.id!r} != requested app_id {spec.app_id!r}")
        spec.app_id = manifest.id
        if not spec.version:
            spec.version = manifest.version
        granted_req = spec.granted_permissions or list(manifest.permissions)

        # runtime.load enforces the F2 grant filter (trust tier) itself and
        # returns the manifest; capture the *effective* grant for the mirror.
        await self.runtime.load(package_dir, granted_permissions=granted_req,
                                config=spec.config, signed=spec.signed)
        loaded = self.runtime.get(manifest.id)
        effective = loaded.granted_permissions if loaded else granted_req
        spec.granted_permissions = effective

        self.local.upsert(spec, package_dir)
        if write_cloud and self.cloud.configured:
            try:
                self.cloud.put_desired(
                    manifest.id, version=spec.version, repo=spec.repo, ref=spec.ref,
                    granted_permissions=effective, config=spec.config,
                    signed=spec.signed)
            except Exception:
                log.exception("apps: install of %s did not reach the cloud registry",
                              manifest.id)

        return {"app_id": manifest.id, "version": spec.version,
                "granted_permissions": effective, "package_dir": package_dir}

    async def uninstall(self, app_id: str, *, remove_repo: bool = True,
                        write_cloud: bool = True) -> dict[str, Any]:
        """Unload (drain + journal reverse) → drop mirror/registry rows → rm repo."""
        if self.runtime.is_loaded(app_id):
            await self.runtime.unload(app_id)
        self.local.forget(app_id)
        removed_repo = self._remove(app_id) if remove_repo else False
        if write_cloud and self.cloud.configured:
            try:
                self.cloud.delete_desired(app_id)
            except Exception:
                log.exception("apps: uninstall of %s did not reach the cloud registry",
                              app_id)
        return {"app_id": app_id, "uninstalled": True, "repo_removed": removed_repo}

    # ---- the reconcile itself ----------------------------------------------

    async def reconcile(self, desired: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        """Converge actual (loaded) → desired (registry). Install missing,
        uninstall extra. ``desired`` defaults to the cloud registry, falling
        back to the local mirror when the cloud isn't configured/reachable."""
        source = "provided"
        if desired is None:
            if self.cloud.configured:
                try:
                    desired = self.cloud.list_desired()
                    source = "cloud"
                except Exception:
                    log.exception("apps: reconcile could not read the cloud registry; "
                                  "falling back to the local mirror")
                    desired = self.local.list()
                    source = "local-fallback"
            else:
                desired = self.local.list()
                source = "local"

        specs = [AppSpec.from_row(r) for r in desired]
        desired_active = {s.app_id: s for s in specs if s.state != "disabled"}
        actual = set(self.runtime.loaded_slugs())

        installed: list[str] = []
        removed: list[str] = []
        errors: list[dict[str, str]] = []

        # install missing (desired but not loaded) — don't re-write the desired
        # row we're converging TO.
        for app_id, spec in desired_active.items():
            if app_id in actual:
                continue
            try:
                await self.install(spec, write_cloud=False)
                installed.append(app_id)
            except Exception as e:  # noqa: BLE001 — one bad app must not block the rest
                log.exception("apps: reconcile failed to install %s", app_id)
                errors.append({"app_id": app_id, "action": "install", "error": str(e)})

        # uninstall extra (loaded but not desired) — converge actual to desired;
        # leave the (absent) desired row alone.
        for app_id in actual - set(desired_active):
            try:
                await self.uninstall(app_id, write_cloud=False)
                removed.append(app_id)
            except Exception as e:  # noqa: BLE001
                log.exception("apps: reconcile failed to uninstall %s", app_id)
                errors.append({"app_id": app_id, "action": "uninstall", "error": str(e)})

        result = {"source": source, "desired": sorted(desired_active),
                  "installed": installed, "removed": removed, "errors": errors}
        log.info("apps: reconciled (%s) — installed=%s removed=%s errors=%d",
                 source, installed, removed, len(errors))
        return result
