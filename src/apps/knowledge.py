"""App-contributed knowledge registration (``contributes.knowledge``).

An app's ``aw-app.json`` can declare one docs directory
(``contributes.knowledge = {"path": "docs"}``, relative to its package dir)
that core mirrors into the shared, searchable KB tree at
``knowledge_base/apps/<app-id>/docs/`` — reachable by ``search_knowledge_base``
exactly like this repo's own committed docs, with zero kb-app changes
(``apps/**`` sits directly under ``KB_DIR`` and its build prunes vanished
files on its own; see
``docs/design/contributes-knowledge-and-cli-reference-split.md`` §0.6/§D).

**Copy, never symlink, never bind** (design §F): ``$AW_KB_DIR`` bind-mounts
``knowledge_base/`` wholesale into the kb app's container, so a symlink
pointing at the app's own package dir resolves to nothing on the far side of
that bind — the same failure ``src/apps/skills.py`` already documents for the
skills tree. A bind per app would recreate the kb container on every app
install/uninstall (the mapped-folders cost).

**Ownership is the directory namespace, not a marker file** (design §B).
Every app's mirror only ever writes inside ``apps/<its-own-app-id>/docs/`` —
one app's sync cannot touch another's by construction, so there is no
``.aw-app-id``-style collision detector to maintain here (contrast
:class:`src.apps.skills.SkillsRegistry`, which needs one because skills land
unprefixed in one flat directory). ``apps/<id>/cli_reference/`` is a
SEPARATE writer (the ``kb-reference`` generator, ``src/libs/cli_reference.py``)
that never touches ``apps/<id>/docs/`` — the two sub-trees are
exact-mirrored independently of each other.

Content-hash gated (same ``_hash_dir`` shape as ``skills.py``) so a no-op
reboot — every installed app re-``register()``s on every boot/reconcile —
does no filesystem I/O. An actual content change (a new app version shipping
updated docs) overwrites the destination even without ``unregister()``
having run first.

**Uninstall is three-layered, deliberately not journal-only** (design §C):
the journal entry (``knowledge:register``) reverses on a normal
single-worker unload via :meth:`KnowledgeRegistry.unregister`, but the
journal is in-memory PER WORKER, so an uninstall landing on a worker that
never itself provisioned this app has no entry to reverse — the exact gap
``skills.py`` documents for ``unload()`` silently no-op'ing the skill mirror
deletion. Unlike skills, knowledge's destination is fully derivable from the
app id alone, so :meth:`KnowledgeRegistry.purge_app` runs unconditionally on
every uninstall regardless of which worker's journal has the entry, and
:func:`sweep` (called from the reconciler after a pass's roster is known) is
the third, independent backstop — deleting any ``apps/<id>/`` whose id is not
in the installed set, which also catches anything the first two missed.
"""
from __future__ import annotations

import hashlib
import logging
import os
import shutil

from src.apps import paths

log = logging.getLogger(__name__)

HASH_MARKER = ".aw-knowledge-source-hash"
README_NAME = ".generated"
README_TEXT = (
    "This directory is generated — do not hand-edit.\n\n"
    "apps/<app-id>/docs/          mirrored from that app's contributes.knowledge\n"
    "                             (src/apps/knowledge.py, re-synced on every\n"
    "                             activate; the app's own copy always wins)\n"
    "apps/<app-id>/cli_reference/ generated --help captures for that app's CLI\n"
    "                             commands (src/libs/cli_reference.py, the\n"
    "                             kb-reference scheduled task)\n\n"
    "Ownership is the directory name itself: each app only ever writes inside\n"
    "its own apps/<app-id>/. An app no longer installed has its whole\n"
    "apps/<app-id>/ removed on uninstall, with a reconciler sweep as backstop.\n"
)


class KnowledgeError(RuntimeError):
    """Raised when a ``contributes.knowledge`` entry is invalid."""


def resolve_knowledge_dir(package_dir: str, knowledge_path: str) -> str:
    """Validate + resolve a ``contributes.knowledge.path`` entry.

    Same escape check as ``skills.resolve_skill_dir`` — ``knowledge_path``
    must stay inside the app's package dir (no escaping via ``..``).
    """
    pkg_root = os.path.abspath(package_dir)
    src_dir = os.path.abspath(os.path.join(pkg_root, knowledge_path))
    if src_dir != pkg_root and not src_dir.startswith(pkg_root + os.sep):
        raise KnowledgeError(f"knowledge path {knowledge_path!r} escapes the app package dir")
    if not os.path.isdir(src_dir):
        raise KnowledgeError(f"knowledge dir not found: {knowledge_path!r}")
    return src_dir


def _write_readme() -> None:
    path = os.path.join(paths.knowledge_apps_dir(), README_NAME)
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(README_TEXT)
    except OSError:
        log.warning("apps: could not write %s", path)


class KnowledgeRegistry:
    """Runtime-owned backend for the ``contributes.knowledge`` surface (copy mirror)."""

    def register(self, app_id: str, package_dir: str, knowledge_path: str) -> str:
        """Exact-mirror the app's docs dir into ``knowledge_base/apps/<app_id>/docs/``.

        Hash-gated: a no-op reboot (source content unchanged since the last
        copy) does no I/O. A changed source (app update shipping new docs)
        overwrites the destination even without ``unregister`` having run —
        see module docstring.

        Returns the destination's absolute path (journaled so ``unregister``
        reverts it).
        """
        src_dir = resolve_knowledge_dir(package_dir, knowledge_path)
        _write_readme()
        dest_path = os.path.join(paths.knowledge_apps_dir(), app_id, "docs")
        source_hash = self._hash_dir(src_dir)
        if os.path.islink(dest_path):
            os.unlink(dest_path)
        elif os.path.isdir(dest_path):
            if self._read_hash(dest_path) == source_hash:
                return dest_path
            shutil.rmtree(dest_path)
        elif os.path.exists(dest_path):
            raise KnowledgeError(
                f"knowledge mirror {dest_path!r} already exists and is not a directory")
        shutil.copytree(src_dir, dest_path)
        with open(os.path.join(dest_path, HASH_MARKER), "w") as f:
            f.write(source_hash)
        return dest_path

    @staticmethod
    def _read_hash(dest_path: str) -> str:
        try:
            with open(os.path.join(dest_path, HASH_MARKER)) as f:
                return f.read().strip()
        except OSError:
            return ""

    @staticmethod
    def _hash_dir(dir_path: str) -> str:
        """Stable content hash of a knowledge source dir (path + bytes, sorted)."""
        hasher = hashlib.sha256()
        for root, dirs, files in os.walk(dir_path):
            dirs.sort()
            for name in sorted(files):
                full = os.path.join(root, name)
                hasher.update(os.path.relpath(full, dir_path).encode())
                with open(full, "rb") as f:
                    hasher.update(f.read())
        return hasher.hexdigest()

    def unregister(self, dest_path: str) -> None:
        """Reverse a single ``knowledge:register`` journal entry."""
        if not dest_path:
            return
        try:
            if os.path.islink(dest_path):
                os.unlink(dest_path)
            elif os.path.isdir(dest_path):
                shutil.rmtree(dest_path)
        except OSError:
            log.warning("apps: failed to remove knowledge mirror %s", dest_path)

    def purge_app(self, app_id: str) -> None:
        """Unconditionally remove the WHOLE ``apps/<app_id>/`` dir.

        Journal-independent (see module docstring) — the uninstall path
        calls this regardless of whether this worker's in-memory journal
        has a ``knowledge:register`` entry to reverse. Removes all of
        ``apps/<app_id>/``, not just ``docs/``, so a stale
        ``cli_reference/`` capture from the same app doesn't survive its
        uninstall either.
        """
        dest_path = os.path.join(paths.knowledge_apps_dir(), app_id)
        try:
            if os.path.islink(dest_path):
                os.unlink(dest_path)
            elif os.path.isdir(dest_path):
                shutil.rmtree(dest_path)
        except OSError:
            log.warning("apps: failed to purge knowledge namespace for %s", app_id)


def sweep(installed_app_ids: set[str]) -> list[str]:
    """Delete any ``apps/<id>/`` whose id is not in ``installed_app_ids``.

    The reconciler's roster-sweep backstop (design §B/§C) — call once per
    reconcile pass, after the pass's final installed set is known, so
    anything a missed uninstall/journal gap left behind self-heals on the
    next boot/reconcile instead of lingering forever. Returns the removed
    app ids, for the caller's log line.
    """
    root = paths.knowledge_apps_dir()
    _write_readme()
    removed: list[str] = []
    try:
        entries = os.listdir(root)
    except OSError:
        return removed
    for name in entries:
        if name == README_NAME or name in installed_app_ids:
            continue
        path = os.path.join(root, name)
        if os.path.islink(path) or not os.path.isdir(path):
            continue
        try:
            shutil.rmtree(path)
            removed.append(name)
        except OSError:
            log.warning("apps: knowledge sweep failed to remove %s", path)
    return removed
