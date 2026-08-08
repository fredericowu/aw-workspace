"""Mapped folders — point the workspace at ANY directory, no git repo required.

Until now everything a workspace could hand to an app was **repo-bound**: the
only shareable location was ``paths.repos_dir()`` (``/opt/aw-workspace/repos``),
exposed to Tier-2 containers through the single ``$AW_WORKSPACE_REPOS`` volume
placeholder, and the kb app could only resolve a *bare repo name* under it. A
plain directory that isn't a checkout under ``repos/`` — ``docs/``, a nested
subdirectory of one repo, a host path bind-mounted into the workspace — had no
way in.

That's the "amarração por repositório" this module removes, restoring what the
``agentic-workspace`` monolith did with ``knowledge_base.map_paths`` in
``aw.json``: an arbitrary list of paths, each mapped by name, no git anywhere in
the resolution path (Frederico, 2026-08-08: *"preciso poder mapear pastas …
sem amarracao por repositório"*; the 2026-08-05 ``$AW_WORKSPACE_REPOS`` volume
was the half-step — "eu quero apontar uma pasta e ele mapear" — that only
covered folders that happened to live under ``repos/``).

Model
-----

A mapped folder is ``{name, path, mode}``:

* ``name`` — the stable handle everything else addresses it by
  (``[A-Za-z0-9][A-Za-z0-9._-]*``, unique). Defaults to the path's basename.
* ``path`` — an **absolute** path as seen by the workspace process. Anything:
  inside the workspace tree, a nested subdir, or a host path that only the
  container engine can see (see ``exists`` below).
* ``mode`` — ``ro`` (default) or ``rw``. ``ro`` is the safe default because the
  common case is "let something *read* my folder" (indexing, docs, KB).

Storage is the workspace's own ``settings`` table (key ``mapped_folders``) —
per-workspace schema, durable across container recreation, and readable by the
apps runtime in-process (no extra file to keep in sync with the DB).

``exists`` is reported, never enforced: a path can legitimately be invisible to
*this* process yet perfectly valid as a Tier-2 bind source, because the
container engine runs on the host and app containers get host paths (see
``AppRuntime._container_host_bind_path``). Refusing to register those would
re-introduce a different flavour of the same "we decide what you may point at"
constraint this module exists to delete.

Consumers
---------

* ``AppRuntime._container_volumes`` expands a ``$AW_WORKSPACE_FOLDERS`` volume
  into **one bind per mapped folder** at ``<target>/<name>``.
* ``aw-workspace-cli folders`` (``src/cli/commands/folders.py``).
* The REST API below, for the SPA / any app UI.
"""
from __future__ import annotations

import logging
import os
import re

from fastapi import Body, Depends, FastAPI, HTTPException

from src.api.db import get_session
from src.api.identity import require_identity
from src.api.models import Setting
from src.apps.paths import DEFAULT_WORKSPACE_CONTAINER_DIR

log = logging.getLogger(__name__)

SETTING_KEY = "mapped_folders"

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
VALID_MODES = ("ro", "rw")

# Names that would collide with something the container-side layout already
# means, or that can't be a directory entry at all.
RESERVED_NAMES = frozenset({".", "..", "-"})


class FolderError(ValueError):
    """Invalid mapped-folder input — surfaced as a 400 by the routes."""


def workspace_root() -> str:
    return os.path.realpath(
        os.environ.get("AW_WORKSPACE_CONTAINER_DIR", DEFAULT_WORKSPACE_CONTAINER_DIR)
    )


# --- registry ----------------------------------------------------------------


def _normalise(entry: dict) -> dict:
    """One stored row → the canonical shape, tolerating older/partial rows."""
    path = str(entry.get("path") or "")
    return {
        "name": str(entry.get("name") or os.path.basename(path.rstrip("/")) or "folder"),
        "path": path,
        "mode": entry.get("mode") if entry.get("mode") in VALID_MODES else "ro",
    }


def list_folders() -> list[dict]:
    """Every mapped folder, sorted by name. Storage-shaped (no ``exists``)."""
    with get_session() as session:
        row = session.get(Setting, SETTING_KEY)
        raw = (row.value or {}).get("folders", []) if row else []
    folders = [_normalise(e) for e in raw if isinstance(e, dict) and e.get("path")]
    return sorted(folders, key=lambda f: f["name"])


def _save(folders: list[dict]) -> None:
    value = {"folders": [_normalise(f) for f in folders]}
    with get_session() as session:
        row = session.get(Setting, SETTING_KEY)
        if row is None:
            session.add(Setting(key=SETTING_KEY, value=value))
        else:
            row.value = value
            session.add(row)
        session.commit()


def describe(folder: dict) -> dict:
    """Registry row + the liveness fields a caller actually wants to see.

    ``exists`` is best-effort from *this* process — see the module docstring on
    why a False here is informational, not a failure.
    """
    path = folder["path"]
    out = dict(folder)
    try:
        out["exists"] = os.path.isdir(path)
    except OSError:
        out["exists"] = False
    out["in_workspace"] = os.path.realpath(path).startswith(workspace_root())
    return out


def validate(path: str, name: str | None = None, mode: str = "ro") -> dict:
    """Validate + canonicalise a mapping request. Raises ``FolderError``."""
    path = (path or "").strip()
    if not path:
        raise FolderError("path is required")
    if not os.path.isabs(path):
        raise FolderError(
            f"path must be absolute (got {path!r}) — mapped folders are resolved "
            f"identically from the server, the CLI and app containers, so a "
            f"cwd-relative path would mean three different directories"
        )
    path = os.path.normpath(path).rstrip("/") or "/"

    name = (name or "").strip() or os.path.basename(path) or "root"
    if name in RESERVED_NAMES or not NAME_RE.match(name):
        raise FolderError(
            f"invalid name {name!r} — use letters, digits, '.', '_' or '-' "
            f"(must start with a letter or digit)"
        )

    mode = (mode or "ro").strip()
    if mode not in VALID_MODES:
        raise FolderError(f"mode must be one of {', '.join(VALID_MODES)} (got {mode!r})")

    return {"name": name, "path": path, "mode": mode}


def add_folder(path: str, name: str | None = None, mode: str = "ro") -> dict:
    """Map ``path`` under ``name``. Re-mapping an existing name updates it."""
    entry = validate(path, name, mode)
    folders = [f for f in list_folders() if f["name"] != entry["name"]]
    folders.append(entry)
    _save(folders)
    log.info("folders: mapped %s -> %s (%s)", entry["name"], entry["path"], entry["mode"])
    return entry


def remove_folder(name: str) -> bool:
    folders = list_folders()
    kept = [f for f in folders if f["name"] != name]
    if len(kept) == len(folders):
        return False
    _save(kept)
    log.info("folders: unmapped %s", name)
    return True


def browse(path: str | None = None) -> dict:
    """Immediate subdirectories of ``path`` — backs a "point at a folder"
    picker so the user doesn't have to type an absolute path from memory.

    Defaults to the workspace root. Read-only and directory-only: it never
    lists file contents, only names, so it exposes strictly less than the
    terminal this workspace already offers its owner.
    """
    target = os.path.normpath(path or workspace_root())
    if not os.path.isabs(target):
        raise FolderError("path must be absolute")
    if not os.path.isdir(target):
        raise FolderError(f"not a directory: {target}")
    try:
        names = sorted(
            n for n in os.listdir(target)
            if not n.startswith(".") and os.path.isdir(os.path.join(target, n))
        )
    except PermissionError as exc:
        raise FolderError(f"cannot read {target}: {exc}") from exc
    parent = os.path.dirname(target.rstrip("/")) or "/"
    return {
        "path": target,
        "parent": None if target == "/" else parent,
        "entries": [{"name": n, "path": os.path.join(target, n)} for n in names],
    }


# --- routes ------------------------------------------------------------------


def register_folder_routes(app: FastAPI) -> None:
    """Mount ``/api/folders*`` — all identity-gated, like every other route."""

    @app.get("/api/folders")
    async def get_folders(identity: dict = Depends(require_identity)):
        return {"folders": [describe(f) for f in list_folders()]}

    @app.get("/api/folders/-/browse")
    async def browse_folders(path: str | None = None,
                             identity: dict = Depends(require_identity)):
        try:
            return browse(path)
        except FolderError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/folders")
    async def post_folder(body: dict = Body(...),
                          identity: dict = Depends(require_identity)):
        try:
            entry = add_folder(
                path=str(body.get("path") or ""),
                name=body.get("name"),
                mode=str(body.get("mode") or "ro"),
            )
        except FolderError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        remapped = await _remap_apps(app)
        return {"folder": describe(entry), "remapped_apps": remapped}

    @app.delete("/api/folders/{name}")
    async def delete_folder(name: str, identity: dict = Depends(require_identity)):
        if not remove_folder(name):
            raise HTTPException(status_code=404, detail=f"no mapped folder named {name!r}")
        remapped = await _remap_apps(app)
        return {"removed": name, "remapped_apps": remapped}


async def _remap_apps(app: FastAPI) -> list[str]:
    """Push the new folder set into every running container app that asked for it.

    Tier-2 binds are fixed at container *creation*, so a folder added after an
    app started is invisible to it until the container is recreated. Doing that
    here is what makes the feature actually end-to-end — otherwise "map a
    folder" silently means "map a folder, then go restart the app yourself".
    """
    runtime = getattr(app.state, "app_runtime", None)
    if runtime is None or not hasattr(runtime, "remap_folders"):
        return []
    try:
        return await runtime.remap_folders()
    except Exception:  # noqa: BLE001 — a failed remap must not fail the mapping
        log.exception("folders: remapping container apps failed")
        return []
