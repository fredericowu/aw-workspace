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

import asyncio
import json
import logging
import os
import shutil
from dataclasses import dataclass, field
from typing import Any, Callable

from src.apps import config_store
from src.apps import fetch as fetch_mod
from src.apps import mcp_template
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
                 "config": r.config, "signed": r.signed, "state": "installed"}
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
            row.signed = spec.signed
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

    def update_config(self, app_id: str, config: dict[str, Any]) -> None:
        from src.api.db import get_session
        from src.api.models import AppInstall

        with get_session() as session:
            row = session.get(AppInstall, app_id)
            if row is not None:
                row.config = config
                session.add(row)
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
        # Set while a reconcile() pass is running so the per-app install/
        # uninstall calls inside it COALESCE into one gateway reload at the
        # end instead of firing one HTTP rescan per app — a fresh workspace
        # reconciles ~20 apps at boot, and each /reload re-dials every
        # upstream. None = not in a pass (reload immediately).
        self._pending_gateway_reload: bool | None = None

    # ---- MCP gateway rescan triggers ---------------------------------------

    def _app_touches_mcp(self, manifest, package_dir: str | None) -> bool:
        """Would installing/removing this app change what the MCP Gateway's
        app-scan finds? Manifest signal (``contributes.mcp``) OR an
        ``mcp.json`` actually on disk in the package dir — aw-app-browser and
        aw-app-code-server ship the file with no ``contributes.mcp`` block,
        so the manifest alone under-reports.

        ``mcp.template.json`` counts too. It is checked separately rather than
        relying on the rendered ``mcp.json`` sitting next to it, because on a
        FIRST install the reconciler asks this before the app has activated —
        so the rendered file does not exist yet and the gateway would never be
        told to rescan (src/apps/mcp_template.py)."""
        if manifest is not None and (manifest.contributes_mcp
                                     or manifest.reload_mcp_gateway_on_save):
            return True
        if package_dir and (
            os.path.isfile(os.path.join(package_dir, "mcp.json"))
            or os.path.isfile(os.path.join(package_dir, mcp_template.TEMPLATE_NAME))
        ):
            return True
        return False

    async def _trigger_gateway_reload(self) -> None:
        """Reload now, or mark it pending when inside a reconcile() pass.
        Deferred import: routes.py imports FROM this module, so a top-level
        import here would cycle."""
        if self._pending_gateway_reload is not None:
            self._pending_gateway_reload = True
            return
        from src.apps.routes import _reload_mcp_gateway
        await _reload_mcp_gateway(self.runtime)

    def _ensure_mcp_scan_visible(self, app_id: str, package_dir: str) -> None:
        """A sideloaded app's ``package_dir`` (a dev checkout, e.g. under
        ``repos/`` — see ``POST /api/apps/install``'s ``package_dir`` branch)
        can sit anywhere on disk. The MCP Gateway's own ``scan_app_mcp_servers()``
        only ever walks ``AW_APP_SCAN_ROOTS`` (default ``fetch.apps_root()``,
        i.e. ``/opt/aw-workspace/apps``) — the ONE tree a marketplace install
        actually lands in, because ``fetch_app_repo`` always extracts straight
        into ``apps_root()/<slug>``.

        A sideloaded app whose ``package_dir`` is NOT under that root is fully
        loaded and functional (it answers its own ``/mcp`` route correctly)
        yet permanently invisible to the gateway — no amount of ``/reload`` or
        the periodic rescan watchdog helps, because the scan never looks at
        the directory the app actually lives in. Confirmed live on
        aw-windows-pilot, 2026-08-26: 21 working tools, 0 seen by the gateway,
        for as long as the app had been installed.

        A symlink at ``apps_root()/<slug>`` pointing at the real ``package_dir``
        was the first attempt here and it does NOT work: the mcp-gateway app
        is Tier-2 (its own container) and its manifest
        (``aw-mcp-gateway/aw-app.json``) bind-mounts ONLY ``$AW_APPS_ROOT`` —
        read-only, and nothing else of the host filesystem. A symlink pointing
        outside that root (e.g. into ``repos/``) is dangling from inside that
        container's mount namespace; the scan finds the link, tries to read
        through it, gets nothing, and silently moves on — confirmed live via a
        direct ``POST /reload`` that came back with ``"added": []``.

        So this COPIES the ``mcp.json`` file's bytes into
        ``apps_root()/<slug>/mcp.json`` instead — the one artifact the scan
        actually reads (``config.scan_app_mcp_servers()``), and small enough
        to refresh cheaply on every install/update. Skipped entirely (with a
        warning) when any declared server sets ``cwd_app_dir: true`` — that
        flag makes a stdio server's ``cwd`` (and therefore its relative-path
        ``command``/``args``) resolve against the REAL package_dir; a bare
        mcp.json copy would silently point it at an empty directory. That
        combination needs a real (repo=) install, not this workaround."""
        dest_dir = fetch_mod.package_dir_for(app_id)
        if os.path.abspath(dest_dir) == os.path.abspath(package_dir):
            return  # marketplace install already lives there
        src = os.path.join(package_dir, "mcp.json")
        if not os.path.isfile(src):
            return
        try:
            with open(src, encoding="utf-8") as f:
                spec = json.load(f)
        except (OSError, ValueError):
            log.exception("apps: could not read %s to check mcp-gateway scan "
                          "visibility for %s", src, app_id)
            return
        servers = spec.get("mcpServers") if isinstance(spec, dict) else None
        if isinstance(servers, dict) and any(
                isinstance(s, dict) and s.get("cwd_app_dir") for s in servers.values()):
            log.warning(
                "apps: %s's mcp.json sets cwd_app_dir — a bare copy into %s "
                "would break its relative paths, and the mcp-gateway "
                "container cannot see %s directly. This sideloaded app needs "
                "a real (repo=) install to be visible to the gateway.",
                app_id, dest_dir, package_dir)
            return
        try:
            os.makedirs(dest_dir, exist_ok=True)
            shutil.copyfile(src, os.path.join(dest_dir, "mcp.json"))
            log.info("apps: copied %s's mcp.json into %s for mcp-gateway scan "
                     "visibility (sideloaded package_dir %s is outside the "
                     "gateway container's mount)", app_id, dest_dir, package_dir)
        except OSError:
            log.exception("apps: failed to copy mcp.json for mcp-gateway scan "
                          "visibility (%s)", app_id)

    # ---- resolve a package dir for a spec (fetch unless already on disk) ----

    def _resolve_package_dir(self, spec: AppSpec) -> str:
        if spec.repo:
            return self._fetch(spec.repo, spec.ref, slug=spec.app_id)
        if spec.package_dir and os.path.isdir(spec.package_dir):
            return spec.package_dir
        raise ValueError(
            f"app {spec.app_id!r} has neither a repo to fetch nor an on-disk package_dir")

    # ---- app dependency resolution -----------------------------------------

    @staticmethod
    def _required_app_dependencies(manifest) -> list[dict[str, Any]]:
        """Required ``dependencies.apps`` entries from a manifest.

        The manifest schema keeps ``dependencies`` forward-compatible as a
        loose object, so the reconciler validates only the app-dependency shape
        it actually enforces. Missing ``required`` means required, matching
        AW's component ``depends`` behavior.
        """
        apps = manifest.dependencies.get("apps", [])
        if apps is None:
            return []
        if not isinstance(apps, list):
            raise ValueError(
                f"app {manifest.id!r} dependencies.apps must be a list")
        deps: list[dict[str, Any]] = []
        for raw in apps:
            if isinstance(raw, str):
                dep = {"id": raw}
            elif isinstance(raw, dict):
                dep = dict(raw)
            else:
                raise ValueError(
                    f"app {manifest.id!r} dependency entries must be objects or strings")
            dep_id = str(dep.get("id") or "").strip()
            if not dep_id:
                raise ValueError(
                    f"app {manifest.id!r} dependency entry is missing id")
            if dep.get("required", True) is False or dep.get("optional") is True:
                continue
            dep["id"] = dep_id
            deps.append(dep)
        return deps

    def _known_dependency_rows(self) -> dict[str, dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        if self.cloud.configured:
            try:
                rows.extend(self.cloud.list_desired())
            except Exception:
                log.exception("apps: could not read cloud registry while resolving dependencies")
        try:
            rows.extend(self.local.list())
        except Exception:
            log.exception("apps: could not read local mirror while resolving dependencies")

        out: dict[str, dict[str, Any]] = {}
        for row in rows:
            app_id = row.get("app_id") or row.get("slug")
            if app_id and app_id not in out:
                out[str(app_id)] = dict(row)
        return out

    def _catalog_dependency_row(self, app_id: str) -> dict[str, Any] | None:
        try:
            from src.apps.catalog import get_catalog
            apps = get_catalog().get("apps", [])
        except Exception:
            log.exception("apps: could not read catalog while resolving dependency %s", app_id)
            return None

        entry = next(
            (a for a in apps if (a.get("id") or a.get("slug")) == app_id),
            None,
        )
        if entry is None:
            return None
        return {
            "app_id": app_id,
            "version": entry.get("version", "") or "",
            "repo": entry.get("repo"),
            "ref": entry.get("ref") or "HEAD",
            "granted_permissions": entry.get("granted_permissions") or [],
            "config": entry.get("config") or {},
            "signed": bool(entry.get("signed", False)),
            "state": "installed",
        }

    def _dependency_spec(self, dep: dict[str, Any],
                         known_rows: dict[str, dict[str, Any]] | None = None) -> AppSpec:
        app_id = dep["id"]
        if dep.get("repo") or dep.get("package_dir"):
            return AppSpec(
                app_id=app_id,
                version=dep.get("version", "") or "",
                repo=dep.get("repo"),
                ref=dep.get("ref") or "HEAD",
                granted_permissions=list(dep.get("granted_permissions") or []),
                config=dict(dep.get("config") or {}),
                signed=bool(dep.get("signed", False)),
                package_dir=dep.get("package_dir"),
            )

        rows = known_rows if known_rows is not None else self._known_dependency_rows()
        if app_id in rows:
            return AppSpec.from_row(rows[app_id])

        catalog_row = self._catalog_dependency_row(app_id)
        if catalog_row is not None:
            return AppSpec.from_row(catalog_row)

        raise ValueError(
            f"required dependency {app_id!r} is not installed and was not found "
            "in the registry, local mirror, or marketplace catalog")

    async def _install_dependencies(self, manifest, *, write_cloud: bool,
                                    stack: tuple[str, ...]) -> list[str]:
        installed: list[str] = []
        known_rows = self._known_dependency_rows()
        for dep in self._required_app_dependencies(manifest):
            dep_id = dep["id"]
            if dep_id in stack:
                chain = " -> ".join((*stack, dep_id))
                raise ValueError(f"cyclic app dependency chain: {chain}")
            if self.runtime.is_loaded(dep_id):
                continue
            dep_spec = self._dependency_spec(dep, known_rows)
            await self.install(dep_spec, write_cloud=write_cloud,
                               _dependency_stack=(*stack, dep_id))
            installed.append(dep_id)
        return installed

    def _loaded_dependency_closure(self, roots: set[str]) -> set[str]:
        """Loaded required dependencies reachable from desired root apps.

        Used by reconcile's removal pass so an implicitly loaded dependency
        such as proxy is not immediately removed just because the desired cloud
        row only named browser.
        """
        protected = set(roots)
        changed = True
        while changed:
            changed = False
            for app_id in list(protected):
                loaded = self.runtime.get(app_id)
                if loaded is None:
                    continue
                for dep in self._required_app_dependencies(loaded.manifest):
                    dep_id = dep["id"]
                    if dep_id not in protected:
                        protected.add(dep_id)
                        changed = True
        return protected

    # ---- install / uninstall -----------------------------------------------

    async def install(self, spec: AppSpec, *, write_cloud: bool = True,
                      _dependency_stack: tuple[str, ...] = ()) -> dict[str, Any]:
        """Fetch → validate → enforce → hot-load → persist. Returns a summary."""
        # _resolve_package_dir does a synchronous tarball download + extract
        # (fetch.fetch_app_repo, httpx.stream + tarfile) when spec.repo is set
        # — offloaded to a thread so a reconcile pass fetching/upgrading an
        # app's code doesn't freeze the whole workspace's single asyncio
        # event loop for the length of that HTTP download. Reported live
        # 2026-08-06 (measured a ~15s stall of an unrelated /api/health call
        # during a two-app upgrade reconcile).
        package_dir = await asyncio.to_thread(self._resolve_package_dir, spec)
        manifest = load_manifest(package_dir)
        if spec.app_id and manifest.id != spec.app_id:
            raise ValueError(
                f"manifest id {manifest.id!r} != requested app_id {spec.app_id!r}")
        spec.app_id = manifest.id
        if not spec.version:
            spec.version = manifest.version
        # Nothing on this path carries config unless a caller supplied it: a
        # marketplace-catalog row has none, a dependency row has none, and a
        # fresh install after a delete has none — so without this the app
        # comes up on schema defaults alone and that emptiness is then
        # written back over the cloud row below, making the loss permanent.
        # Restore-on-empty only: a spec that DOES carry config is stating an
        # intent the snapshot must not override.
        if not spec.config:
            restored = config_store.load(manifest.id)
            if restored:
                log.info("apps: restored %d saved config value(s) for %s",
                         len(restored), manifest.id)
                spec.config = restored
        # Mint any x-generate secret that still has no value — AFTER the
        # restore above, so a reinstall reuses the value the app already has
        # rather than rotating a database password out from under its datadir.
        minted = manifest.generated_config(spec.config)
        if minted:
            log.info("apps: generated %s for %s", ", ".join(sorted(minted)), manifest.id)
            spec.config = {**spec.config, **minted}
        # The REQUEST is always what this version's manifest declares — never
        # the grant carried on the spec. That grant is the *effective* result
        # of the last install (written back below, and mirrored to the cloud),
        # so feeding it back in as the request pins an app to the permissions
        # of the version it was first installed at: an update that declares a
        # NEW permission never gets it, and the app degrades silently if it
        # guards on ctx.has(). Hit on aw-app-diff-tool 2026-08-12, where a
        # newly-declared fs:workspace-data stayed ungranted through
        # `POST /api/apps/{slug}/update` (which explicitly preserves the old
        # grant) — only a full uninstall+reinstall picked it up.
        #
        # Nothing narrows the manifest's set today: no consent screen exists,
        # so every caller (SPA, CLI, cloud row) passes the manifest's list or
        # nothing at all. When one does land, it needs its own field for what
        # the user DENIED — a subtractive record survives an update, whereas
        # this additive one cannot tell "user withheld it" from "the version
        # installed never asked for it".
        #
        # Trust filtering is unaffected: runtime.load still strips high-risk
        # capabilities from an unsigned app, so this widens what an app may
        # ASK for, never what it is granted.
        granted_req = list(manifest.permissions)

        deps_installed = await self._install_dependencies(
            manifest, write_cloud=write_cloud, stack=_dependency_stack or (manifest.id,))

        # runtime.load enforces the F2 grant filter (trust tier) itself and
        # returns the manifest; capture the *effective* grant for the mirror.
        #
        # A load that raises PART WAY through has usually already journaled
        # some side effects — a registered container above all. Leaving those
        # behind makes the next attempt fail with "container already
        # registered for 'x'", so the app is not merely down, it is *stuck*
        # down: install says "already installed", update says "not installed".
        # Hit for real on aw-app-crispal, 2026-08-16. unload() replays the
        # journal, which is exactly the cleanup owed here.
        try:
            await self.runtime.load(package_dir, granted_permissions=granted_req,
                                    config=spec.config, signed=spec.signed)
        except Exception:
            log.exception("apps: load of %s failed — reverting its side effects "
                          "so a retry starts clean", manifest.id)
            try:
                await self.runtime.unload(manifest.id)
            except Exception:
                log.exception("apps: could not revert the failed load of %s", manifest.id)
            raise
        loaded = self.runtime.get(manifest.id)
        effective = loaded.granted_permissions if loaded else granted_req
        spec.granted_permissions = effective

        self.local.upsert(spec, package_dir)
        # Refresh the snapshot from whatever this install actually resolved,
        # so an app configured before this mechanism existed is protected by
        # the first reconcile that loads it, not only by its next config save.
        # No-ops when spec.config is empty, so it can never blank a good file.
        config_store.save(manifest.id, spec.config)
        if write_cloud and self.cloud.configured:
            try:
                await asyncio.to_thread(
                    self.cloud.put_desired,
                    manifest.id, version=spec.version, repo=spec.repo, ref=spec.ref,
                    granted_permissions=effective, config=spec.config,
                    signed=spec.signed)
            except Exception:
                log.exception("apps: install of %s did not reach the cloud registry",
                              manifest.id)

        # An app that self-registers an mcp.json on activate() (e.g.
        # aw-app-whiteboard) only gets picked up by an ALREADY-RUNNING
        # mcp-gateway on its own next reload/restart — mcp-gateway's own
        # boot-time scan can't see a file that doesn't exist yet if this app
        # gets installed/updated afterward. Gated on _app_touches_mcp, NOT
        # on the narrower reload_on_save opt-in (see manifest.contributes_mcp).
        if self._app_touches_mcp(loaded.manifest if loaded else manifest, package_dir):
            self._ensure_mcp_scan_visible(manifest.id, package_dir)
            await self._trigger_gateway_reload()

        return {"app_id": manifest.id, "version": spec.version,
                "granted_permissions": effective, "package_dir": package_dir,
                "dependencies_installed": deps_installed}

    async def uninstall(self, app_id: str, *, remove_repo: bool = True,
                        write_cloud: bool = True,
                        purge_secrets: bool = True) -> dict[str, Any]:
        """Unload (drain + journal reverse) → drop mirror/registry rows → rm repo.

        ``purge_secrets=False`` is what the upgrade path passes: an upgrade is
        uninstall + install, and an app's ``ctx.secrets`` namespace must not be
        emptied just because its version moved.
        """
        # Probe BEFORE unloading: once the app is unloaded and its repo
        # removed, both the manifest and the on-disk mcp.json are gone and
        # there is no way left to tell whether the gateway needs a rescan.
        loaded_before = self.runtime.get(app_id)
        touched_mcp = self._app_touches_mcp(
            loaded_before.manifest if loaded_before else None,
            loaded_before.package_dir if loaded_before else None,
        )
        # Snapshot the settings BEFORE anything drops them. forget() deletes
        # the mirror row and delete_desired() deletes the cloud row, so after
        # this point the app's config exists nowhere — which is how a
        # delete+install (the routine way to force a rebuilt image) silently
        # reset aw-app-crispal to schema defaults and killed the Arvin bridge
        # for a day. install() reads this back. See config_store.py.
        if loaded_before is not None:
            config_store.save(app_id, loaded_before.config)
        else:
            for row in self.local.list():
                if row.get("app_id") == app_id:
                    config_store.save(app_id, row.get("config"))
                    break
        if self.runtime.is_loaded(app_id):
            await self.runtime.unload(app_id, purge_secrets=purge_secrets)
        self.local.forget(app_id)
        # _remove (fetch.remove_app_repo) already shutil.rmtree's whatever
        # sits at apps_root()/<slug> — for a sideloaded app that's the real
        # mcp.json COPY _ensure_mcp_scan_visible left behind (a real dir, not
        # a symlink into the original package_dir), so no special-casing is
        # needed here to clean it up.
        removed_repo = await asyncio.to_thread(self._remove, app_id) if remove_repo else False
        if write_cloud and self.cloud.configured:
            try:
                await asyncio.to_thread(self.cloud.delete_desired, app_id)
            except Exception:
                log.exception("apps: uninstall of %s did not reach the cloud registry",
                              app_id)

        # Drop the now-dead upstream from the gateway. reload() is
        # differential, so this is what turns a removed mcp.json into a
        # "removed" upstream instead of a stale entry that fails on every
        # tools/call until something else happens to reload.
        if touched_mcp:
            await self._trigger_gateway_reload()

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
                    desired = await asyncio.to_thread(self.cloud.list_desired)
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
        actual_before = set(self.runtime.loaded_slugs())

        installed: list[str] = []
        removed: list[str] = []
        upgraded: list[str] = []
        errors: list[dict[str, str]] = []

        # Coalesce every gateway reload this pass would trigger into one at
        # the end (see _pending_gateway_reload). Set even on the error paths
        # below — an app that failed halfway may still have written its
        # mcp.json before failing.
        self._pending_gateway_reload = False

        # install missing (desired but not loaded) — don't re-write the desired
        # row we're converging TO. For apps present on both sides, a version
        # bump OR a trust/grant change in the registry is an upgrade =
        # uninstall + install with the new spec (config/permissions survive —
        # they come from the desired row, which is left untouched).
        for app_id, spec in desired_active.items():
            if not self.runtime.is_loaded(app_id):
                try:
                    await self.install(spec, write_cloud=False)
                    installed.append(app_id)
                except Exception as e:  # noqa: BLE001 — one bad app must not block the rest
                    log.exception("apps: reconcile failed to install %s", app_id)
                    errors.append({"app_id": app_id, "action": "install", "error": str(e)})
                continue

            loaded = self.runtime.get(app_id)
            running_version = loaded.manifest.version if loaded else ""
            version_changed = bool(spec.version) and spec.version != running_version
            # `signed` flips when the cloud re-derives it from marketplace-
            # catalog membership (ADR Decision 4); granted_permissions can
            # change independently too (a consent-screen re-grant, or a
            # trust flip re-deriving the effective set — see
            # routes/app_installs.py's upsert_install). Neither used to be
            # detected here, so a currently-loaded app kept running with a
            # stale grant until something else forced a version bump.
            trust_changed = bool(loaded) and (
                loaded.signed != spec.signed
                or set(loaded.granted_permissions) != set(spec.granted_permissions)
            )
            if version_changed or trust_changed:
                # What we would put back. Read BEFORE the uninstall, which
                # forgets the row. NOTE this does NOT skip a re-fetch: below,
                # `previous.package_dir` is set, but `_resolve_package_dir`
                # checks `spec.repo` first, and `previous.repo` is always
                # populated from the local mirror row — so `install(previous)`
                # re-fetches over the network at the previous `ref` every
                # time. If the network can't serve that ref — the exact
                # moment a rollback is needed most — this "rollback" fails
                # too. `package_dir` is carried here but currently unused;
                # making it actually skip the re-fetch needs a dedicated
                # resolve path, not a priority-order change to
                # `_resolve_package_dir` (that method's callers, e.g. a
                # normal install, must always re-fetch to pick up updates
                # even when a stale `package_dir` already exists on disk).
                previous_row = next(
                    (r for r in self.local.list() if r.get("app_id") == app_id), None)
                previous = AppSpec.from_row(previous_row) if previous_row else None
                if previous is not None and loaded and loaded.package_dir:
                    previous.package_dir = loaded.package_dir
                try:
                    # remove_repo=False: an upgrade must not delete the
                    # running version's tree before the new one has actually
                    # landed. install() below fetches straight into this same
                    # `dest`; thanks to fetch_app_repo's atomic rename-swap
                    # (fixed together with this comment, 2026-08-19), a fetch
                    # failure now leaves `dest` untouched instead of gone.
                    await self.uninstall(app_id, write_cloud=False,
                                         purge_secrets=False, remove_repo=False)
                    await self.install(spec, write_cloud=False)
                    upgraded.append(app_id)
                except Exception as e:  # noqa: BLE001
                    log.exception("apps: reconcile failed to upgrade %s", app_id)
                    errors.append({"app_id": app_id, "action": "upgrade", "error": str(e)})
                    # An upgrade is uninstall + install, so a failed install
                    # leaves the app DOWN — not on its previous version, which
                    # is what "the update failed" sounds like it means. Put the
                    # old one back. Still re-fetches (see the note above), so
                    # this is genuinely best-effort: if the network can't
                    # serve `previous`'s ref either, this also fails — but
                    # remove_repo=False means the pre-upgrade tree was never
                    # deleted, so nothing is lost even when both attempts do.
                    if previous is not None and previous.package_dir:
                        try:
                            await self.install(previous, write_cloud=False)
                            log.warning("apps: %s rolled back to %s after a failed "
                                        "upgrade", app_id, running_version or "?")
                            errors[-1]["rolled_back_to"] = running_version or "?"
                        except Exception:
                            log.exception("apps: %s could not be rolled back and is "
                                          "now DOWN", app_id)

        # uninstall extra (loaded but not desired) — converge actual to desired;
        # leave the (absent) desired row alone.
        protected = self._loaded_dependency_closure(set(desired_active))
        for app_id in (actual_before | set(self.runtime.loaded_slugs())) - protected:
            try:
                await self.uninstall(app_id, write_cloud=False)
                removed.append(app_id)
            except Exception as e:  # noqa: BLE001
                log.exception("apps: reconcile failed to uninstall %s", app_id)
                errors.append({"app_id": app_id, "action": "uninstall", "error": str(e)})

        # Fire the single coalesced reload. Clearing the flag FIRST makes
        # _trigger_gateway_reload take its immediate path, and leaves the
        # reconciler back in "not in a pass" state even if the reload raises.
        wanted_reload = bool(self._pending_gateway_reload)
        self._pending_gateway_reload = None
        if wanted_reload:
            await self._trigger_gateway_reload()

        result = {"source": source, "desired": sorted(desired_active),
                  "installed": installed, "upgraded": upgraded, "removed": removed,
                  "errors": errors, "mcp_gateway_reloaded": wanted_reload}
        log.info("apps: reconciled (%s) — installed=%s upgraded=%s removed=%s errors=%d "
                 "mcp_reload=%s",
                 source, installed, upgraded, removed, len(errors), wanted_reload)
        return result
