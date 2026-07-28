"""App-contributed skills registration (``contributes.skills``).

An app's ``aw-app.json`` can declare skills it teaches an agent to use — each
entry names a ``SKILL.md`` relative to the app's package dir (ADR: decoupled
apps framework). Registration symlinks the skill's own directory into the
shared workspace skills index (``<AW_WORKSPACE_HOME>/skills/<app_id>__<skill_id>``)
— no content is copied, so an app update is reflected immediately through the
symlink target. Reverted (symlink removed) on uninstall via the journal, same
shape as the ``commands`` bin-shim facade.
"""
from __future__ import annotations

import logging
import os

from src.apps import paths

log = logging.getLogger(__name__)


class SkillError(RuntimeError):
    """Raised when a ``contributes.skills`` entry is invalid."""


def _link_name(app_id: str, skill_id: str) -> str:
    return f"{app_id}__{skill_id}"


def resolve_skill_dir(package_dir: str, skill_path: str) -> str:
    """Validate + resolve a ``contributes.skills[].path`` entry.

    ``skill_path`` must point at a file inside the app's package dir (no
    escaping via ``..``). Returns the absolute path to that file's parent
    directory — the symlink target (a whole ``skills/<id>/`` dir, so any
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
    """Runtime-owned backend for the ``contributes.skills`` surface (symlink index)."""

    def register(self, app_id: str, skill_id: str, package_dir: str, skill_path: str) -> str:
        """Symlink the app's skill dir into the shared skills index.

        Returns the symlink's absolute path (journaled so ``unregister`` reverts it).
        """
        skill_dir = resolve_skill_dir(package_dir, skill_path)
        link_path = os.path.join(paths.skills_dir(), _link_name(app_id, skill_id))
        if os.path.islink(link_path):
            os.unlink(link_path)
        elif os.path.exists(link_path):
            raise SkillError(f"skills index entry {link_path!r} already exists and is not a symlink")
        os.symlink(skill_dir, link_path, target_is_directory=True)
        return link_path

    def unregister(self, link_path: str) -> None:
        if link_path and os.path.islink(link_path):
            try:
                os.unlink(link_path)
            except OSError:
                log.warning("apps: failed to remove skill symlink %s", link_path)
