"""App-contributed skills registration (``contributes.skills``).

An app's ``aw-app.json`` can declare skills it teaches an agent to use — each
entry names a ``SKILL.md`` relative to the app's package dir (ADR: decoupled
apps framework). Registration **copies** (never symlinks) the skill's own
directory into the shared workspace skills index
(``<AW_WORKSPACE_HOME>/skills/<app_id>__<skill_id>``) — the app's package dir
is immutable by design (an update overwrites it wholesale), so a symlink
would make a user's in-place edits to their live skill vanish/break the
moment the app updates. Once copied, the workspace's own copy is the user's
to edit; re-registering (every boot re-activates every installed app) never
overwrites an existing copy. Reverted (copy removed) on uninstall via the
journal, same shape as the ``commands`` bin-shim facade.

Known gap: ``<AW_WORKSPACE_HOME>/skills`` isn't committed/backed up anywhere
today, so a user's edits don't survive a full workspace recreation — that's a
separate, not-yet-solved persistence question, not something this registry
can fix on its own.
"""
from __future__ import annotations

import logging
import os
import shutil

from src.apps import paths

log = logging.getLogger(__name__)


class SkillError(RuntimeError):
    """Raised when a ``contributes.skills`` entry is invalid."""


def _copy_name(app_id: str, skill_id: str) -> str:
    return f"{app_id}__{skill_id}"


def resolve_skill_dir(package_dir: str, skill_path: str) -> str:
    """Validate + resolve a ``contributes.skills[].path`` entry.

    ``skill_path`` must point at a file inside the app's package dir (no
    escaping via ``..``). Returns the absolute path to that file's parent
    directory — the copy source (a whole ``skills/<id>/`` dir, so any
    reference assets next to ``SKILL.md`` come along for free).
    """
    pkg_root = os.path.abspath(package_dir)
    md_path = os.path.abspath(os.path.join(pkg_root, skill_path))
    if not md_path.startswith(pkg_root + os.sep):
        raise SkillError(f"skill path {skill_path!r} escapes the app package dir")
    if not os.path.isfile(md_path):
        raise SkillError(f"skill file not found: {skill_path!r}")
    return os.path.dirname(md_path)


class SkillsRegistry:
    """Runtime-owned backend for the ``contributes.skills`` surface (copy index)."""

    def register(self, app_id: str, skill_id: str, package_dir: str, skill_path: str) -> str:
        """Copy the app's skill dir into the shared skills index, once.

        A dir already at the destination (a prior install, or a later boot's
        re-``register()`` of an already-installed app) is left alone — never
        clobber a user's live edits. A leftover **symlink** from before this
        registry switched to copying is replaced with a real copy.

        Returns the copy's absolute path (journaled so ``unregister`` reverts it).
        """
        skill_dir = resolve_skill_dir(package_dir, skill_path)
        dest_path = os.path.join(paths.skills_dir(), _copy_name(app_id, skill_id))
        if os.path.islink(dest_path):
            os.unlink(dest_path)
        elif os.path.isdir(dest_path):
            return dest_path
        elif os.path.exists(dest_path):
            raise SkillError(f"skills index entry {dest_path!r} already exists and is not a directory")
        shutil.copytree(skill_dir, dest_path)
        return dest_path

    def unregister(self, dest_path: str) -> None:
        if not dest_path:
            return
        try:
            if os.path.islink(dest_path):
                os.unlink(dest_path)
            elif os.path.isdir(dest_path):
                shutil.rmtree(dest_path)
        except OSError:
            log.warning("apps: failed to remove skill copy %s", dest_path)
