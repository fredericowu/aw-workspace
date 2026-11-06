"""App-contributed skills registration (``contributes.skills``).

An app's ``aw-app.json`` can declare skills it teaches an agent to use — each
entry names a ``SKILL.md`` relative to the app's package dir (ADR: decoupled
apps framework). Registration **copies** (never symlinks) the skill's own
directory into the workspace's top-level skills index (``paths.skills_dir()``
— ``<workspace root>/skills/<skill_id>``, the same directory Claude Code
auto-discovers ``SKILL.md`` files from, named exactly like the workspace's
own built-in skills, no app-id prefix) — the app's package dir is immutable
by design (an update overwrites it wholesale), so a symlink would make a
user's in-place edits to their live skill vanish/break the moment the app
updates. Once copied, the workspace's own copy is the user's to edit;
re-registering with the *same* source content (every boot re-activates every
installed app) leaves an existing copy alone. Reverted (copy removed) on
uninstall via the journal, same shape as the ``commands`` bin-shim facade.

Content/version aware (see ``.aw-skill-source-hash``): with
``AW_WORKSPACE_WORKERS>1`` an ``/update`` or manual uninstall+install request
can land on a worker that never itself provisioned this app (it only ever
``attach()``ed), so that worker's in-memory Action Journal has no
``skill:register`` entry to reverse and ``unload()`` silently no-ops the
mirror deletion. A directory-existence-only no-clobber check would then keep
serving the stale copy forever once a new app version ships a changed
``SKILL.md``. ``register()`` instead hashes the app's own source skill dir on
every call and only skips re-copying when that hash still matches the one
recorded at the last copy — an actual content/version change overwrites the
destination even without ``unload()`` having run. This trades away
preserving a user's hand-edit across an app content change (accepted: an app
update winning is the point of this fix) while still leaving a copy alone
across reboots where nothing changed.

Because the destination lives at the workspace root, not under
``AW_WORKSPACE_HOME``, a core-image update could in principle touch it — but
every installed app re-``register()``s its skills on every boot (reconcile),
so the index self-heals. A user's own edits to an already-registered copy
are still never clobbered, per the check above.

Skill ids are unprefixed, so two apps declaring the same ``id`` collide on
the same destination dir. Each copy carries a hidden ``.aw-app-id`` marker
recording which app owns it — a re-``register()`` by that same app is the
normal no-clobber case above, but a *different* app claiming an
already-owned id raises :class:`SkillError` instead of silently overwriting
or silently losing the second app's skill.

The "workspace's own copy is the user's to edit" promise above holds only
while nothing else has claimed the id: once a registered skill source (see
``src/apps/skill_sources.py`` / ``src/libs/skills_sync.py``) takes over a
pushed id — marked by ``.aw-skill-source`` appearing beside ``.aw-app-id`` —
the content is server-owned instead. ``register()`` then defers rather than
copying (see below), and a manual edit to that entry is reverted on the next
sync; the skill registry connector's own ``doctor`` surfaces that drift
(``drifted[]`` in its ``materializer.py``).
"""
from __future__ import annotations

import hashlib
import logging
import os
import shutil

from src.apps import paths
from src.apps.skill_sources import SOURCE_MARKER

log = logging.getLogger(__name__)

OWNER_MARKER = ".aw-app-id"
HASH_MARKER = ".aw-skill-source-hash"
_SOURCED_HASH_EXCLUDE = frozenset({OWNER_MARKER, HASH_MARKER, SOURCE_MARKER})


class SkillError(RuntimeError):
    """Raised when a ``contributes.skills`` entry is invalid."""


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
        """Copy the app's skill dir into the shared skills index.

        A dir already at the destination *owned by this same app* (a prior
        install, or a later boot's re-``register()`` of an already-installed
        app) is left alone when its content hasn't changed since the last
        copy — never clobber a user's live edits across a no-op reboot. But
        if the app's own source skill dir now hashes differently (a version
        bump shipped a new ``SKILL.md``), the destination is overwritten even
        though a directory already exists there — this is what makes an
        ``/update`` self-heal a stale mirror without depending on
        ``unload()`` having run first (see module docstring). A dir owned by
        a *different* app is a real id collision — raised as
        :class:`SkillError` rather than silently overwritten or silently
        skipped. A leftover **symlink** from before this registry switched to
        copying is replaced with a real copy.

        A dest already carrying ``.aw-skill-source`` has been claimed by a
        registered skill source (``src/libs/skills_sync.py``'s pull pass,
        gated by that source's ``takeover`` flag) — this call **defers**
        instead: no copy, no exception, just the dest path back as if it had
        succeeded. Raising :class:`SkillError` here is what used to turn the
        accidental mass-adoption already live in production (§17.7: 50 of 56
        pushed entries silently re-owned by the skill registry) into a
        ``SkillError`` on every single boot. A real content mismatch between
        what this app ships and what the sourced entry actually serves is
        still surfaced — as a WARNING naming both hashes, never as an
        exception, since the sourced copy wins either way. Provenance is
        rewritten back to the real pushing app on every call (idempotent —
        the pull pass only stamps ``.aw-app-id`` when it's absent, so the two
        writers never fight over it).

        Returns the copy's absolute path (journaled so ``unregister`` reverts it).
        """
        skill_dir = resolve_skill_dir(package_dir, skill_path)
        dest_path = os.path.join(paths.skills_dir(), skill_id)
        source_hash = self._hash_dir(skill_dir)
        if os.path.isdir(dest_path) and os.path.isfile(os.path.join(dest_path, SOURCE_MARKER)):
            served_hash = self._hash_dir(dest_path, exclude=_SOURCED_HASH_EXCLUDE)
            if served_hash != source_hash:
                log.warning(
                    "apps: skill %r is sourced from a registered skill source; "
                    "%s's own copy (hash %s) differs from what's actually served "
                    "(hash %s) — the served copy wins",
                    skill_id, app_id, source_hash, served_hash,
                )
            try:
                with open(os.path.join(dest_path, OWNER_MARKER), "w") as f:
                    f.write(app_id)
            except OSError:
                log.warning("apps: could not record provenance for sourced skill %r", skill_id)
            return dest_path
        if os.path.islink(dest_path):
            os.unlink(dest_path)
        elif os.path.isdir(dest_path):
            owner = self._read_owner(dest_path)
            if owner and owner != app_id:
                raise SkillError(
                    f"skill id {skill_id!r} already registered by app {owner!r}, "
                    f"cannot also register it for {app_id!r} — rename one of them"
                )
            if self._read_hash(dest_path) == source_hash:
                return dest_path
            shutil.rmtree(dest_path)
        elif os.path.exists(dest_path):
            raise SkillError(f"skills index entry {dest_path!r} already exists and is not a directory")
        shutil.copytree(skill_dir, dest_path)
        with open(os.path.join(dest_path, OWNER_MARKER), "w") as f:
            f.write(app_id)
        with open(os.path.join(dest_path, HASH_MARKER), "w") as f:
            f.write(source_hash)
        return dest_path

    @staticmethod
    def _read_owner(dest_path: str) -> str:
        try:
            with open(os.path.join(dest_path, OWNER_MARKER)) as f:
                return f.read().strip()
        except OSError:
            return ""

    @staticmethod
    def _read_hash(dest_path: str) -> str:
        try:
            with open(os.path.join(dest_path, HASH_MARKER)) as f:
                return f.read().strip()
        except OSError:
            return ""

    @staticmethod
    def _hash_dir(dir_path: str, *, exclude: frozenset[str] = frozenset()) -> str:
        """Stable content hash of a skill source dir (path + bytes, sorted).

        ``exclude`` skips named files — used to keep this registry's own
        ownership markers out of a comparison against a pristine app-package
        source dir that never had them.
        """
        hasher = hashlib.sha256()
        for root, dirs, files in os.walk(dir_path):
            dirs.sort()
            for name in sorted(files):
                if name in exclude:
                    continue
                full = os.path.join(root, name)
                hasher.update(os.path.relpath(full, dir_path).encode())
                with open(full, "rb") as f:
                    hasher.update(f.read())
        return hasher.hexdigest()

    def unregister(self, dest_path: str) -> None:
        if not dest_path:
            return
        try:
            if os.path.isdir(dest_path) and os.path.isfile(os.path.join(dest_path, SOURCE_MARKER)):
                # Sourced — a registered skill source (or another
                # takeover-flagged source) owns this content now; only
                # skills_sync.py's own pull-pass delete loop may remove it.
                # Checked fresh on disk, not via the in-memory Action
                # Journal, so this holds even on a worker that never itself
                # provisioned the app (AW_WORKSPACE_WORKERS>1).
                return
            if os.path.islink(dest_path):
                os.unlink(dest_path)
            elif os.path.isdir(dest_path):
                shutil.rmtree(dest_path)
        except OSError:
            log.warning("apps: failed to remove skill copy %s", dest_path)
