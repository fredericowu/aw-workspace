"""Materialize ``skills/`` from its two sources, then mirror it per agent CLI.

Ported from the ``agentic-workspace`` monolith (``src/libs/skills_sync.py``).

``skills/`` is **generated**, not authored. It has two upstreams:

* ``native-skills/`` — the skills this repo owns. Committed; the only skills
  a fresh clone or a brand-new deployment starts with.
* each installed app's ``contributes.skills`` — copied in by the apps
  framework (``src/apps/skills.py``), each copy carrying a ``.aw-app-id``
  marker naming its owner.

Keeping ``skills/`` out of git and generating it is what stops a workspace's
app roster from leaking into this repo's history — before this split, two
app-contributed skills sat committed here, indistinguishable from native ones
except by that marker.

It stays a **real directory of real copies**, never symlinks: app containers
bind-mount it read-only (``$AW_WORKSPACE_SKILLS``, see
``src/apps/runtime.py``), and a link pointing outside the mount resolves to
nothing on the far side — the failure mode being an agent that silently finds
no skills.

Ownership decides who may delete what. :func:`materialize` manages only the
native half: an entry carrying ``.aw-app-id`` belongs to its app, which
registers it on activate and removes it on uninstall. Without that split a
sync would delete every app's skill the moment it ran.

Sync semantics into the per-CLI mirrors are an **exact mirror**: a file that
leaves ``skills/`` leaves every target, and directories left empty are pruned.
Anything weaker leaves an uninstalled app's skill lying around teaching agents
to call tools that no longer exist.
"""
from __future__ import annotations

import filecmp
import logging
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from src.apps.paths import DEFAULT_WORKSPACE_CONTAINER_DIR
from src.apps.skills import OWNER_MARKER

log = logging.getLogger(__name__)

GENERATED_MARKER = ".generated"


def workspace_root() -> Path:
    return Path(os.path.realpath(
        os.environ.get("AW_WORKSPACE_CONTAINER_DIR", DEFAULT_WORKSPACE_CONTAINER_DIR)
    ))


def source_dir() -> Path:
    """Top-level ``skills/`` — generated, and the source the mirrors copy from."""
    return workspace_root() / "skills"


def native_source_dir() -> Path:
    """``native-skills/`` — the skills this repo owns and commits."""
    return workspace_root() / "native-skills"


def targets() -> tuple[Path, ...]:
    """Per-agent skill dirs that mirror ``skills/``.

    Adding a new agent CLI is one line here; the sync logic is identical for
    all of them. Claude Code reads ``.claude/skills``; Copilot reads the same
    directory. Cursor and Gemini each want their own.
    """
    root = workspace_root()
    return (
        root / ".claude" / "skills",
        root / ".cursor" / "skills",
        root / ".gemini" / "skills",
    )


@dataclass
class SyncResult:
    """Per-target sync stats."""

    target: str
    added: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)
    unchanged: int = 0
    error: str | None = None

    @property
    def changed(self) -> bool:
        return bool(self.added or self.updated or self.deleted)

    def to_dict(self) -> dict:
        return {
            "target": self.target,
            "added": self.added,
            "updated": self.updated,
            "deleted": self.deleted,
            "unchanged": self.unchanged,
            "error": self.error,
        }


def _iter_relative_files(root: Path) -> set[str]:
    """Relative POSIX paths of every file under ``root`` (``__pycache__`` pruned).

    The top-level ``.generated`` marker is skipped everywhere it appears, so it
    is never mirrored into a per-CLI dir and never deleted as "stale" — it
    belongs to the directory it warns about, not to any source.
    """
    if not root.exists():
        return set()
    out: set[str] = set()
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d != "__pycache__"]
        for fn in filenames:
            rel = (Path(dirpath) / fn).relative_to(root).as_posix()
            if rel == GENERATED_MARKER:
                continue
            out.add(rel)
    return out


def _files_equal(a: Path, b: Path) -> bool:
    """Content comparison — ``shallow=False`` so a same-size, same-mtime edit
    isn't mistaken for "unchanged"."""
    try:
        return filecmp.cmp(a, b, shallow=False)
    except OSError:
        return False


def _prune_empty_dirs(root: Path) -> None:
    """Bottom-up removal of empty subdirectories, so a deleted skill's folder
    disappears instead of lingering as an empty shell."""
    for dirpath, dirnames, filenames in os.walk(root, topdown=False):
        if Path(dirpath) == root:
            continue
        if not dirnames and not filenames:
            try:
                Path(dirpath).rmdir()
            except OSError:
                pass


def _sync_one(source: Path, target: Path) -> SyncResult:
    """Mirror ``source`` → ``target`` with delete-stale semantics."""
    result = SyncResult(target=str(target))
    target.mkdir(parents=True, exist_ok=True)

    src_files = _iter_relative_files(source)
    tgt_files = _iter_relative_files(target)

    for rel in sorted(src_files):
        src_path, tgt_path = source / rel, target / rel
        tgt_path.parent.mkdir(parents=True, exist_ok=True)
        if not tgt_path.exists():
            shutil.copy2(src_path, tgt_path)
            result.added.append(rel)
        elif not _files_equal(src_path, tgt_path):
            shutil.copy2(src_path, tgt_path)
            result.updated.append(rel)
        else:
            result.unchanged += 1

    for rel in sorted(tgt_files - src_files):
        try:
            (target / rel).unlink()
            result.deleted.append(rel)
        except OSError as exc:
            log.warning("skills_sync: failed to delete %s: %s", target / rel, exc)

    _prune_empty_dirs(target)
    return result


_MARKER_TEXT = """\
This directory is GENERATED by `aw-workspace-cli agent sync` and is gitignored.

Do not edit anything here — the next sync overwrites it. Edit the source:

  * a skill this repo owns  -> native-skills/<name>/
  * a skill an app provides -> that app's own repo (see .aw-app-id in the
    skill's directory for which app owns it)
"""


def _app_owned_entries(merged: Path) -> set[str]:
    """Top-level names in ``skills/`` that an installed app owns.

    Ownership is read off the ``.aw-app-id`` marker the apps framework writes
    beside each copy (``src/apps/skills.py``) — the same marker it uses to
    detect id collisions. Anything without one is native, and therefore ours
    to overwrite or delete.
    """
    if not merged.exists():
        return set()
    return {
        entry.name for entry in merged.iterdir()
        if entry.is_dir() and (entry / OWNER_MARKER).is_file()
    }


def materialize(native: Path | None = None, merged: Path | None = None) -> SyncResult:
    """Copy ``native-skills/`` into ``skills/``, leaving app-owned entries alone.

    This is the half of ``skills/`` that git carries. The app-owned half is
    written by ``src/apps/skills.py`` on activate and removed on uninstall, so
    the delete pass here is scoped to native entries only — a stale native
    skill goes, an app's skill is never touched.
    """
    src = native or native_source_dir()
    dest = merged or source_dir()
    result = SyncResult(target=str(dest))

    if not src.exists():
        log.warning("skills_sync: %s does not exist; skills/ keeps only app-owned entries", src)
        return result

    dest.mkdir(parents=True, exist_ok=True)
    reserved = _app_owned_entries(dest)

    src_files = _iter_relative_files(src)
    dest_files = {
        rel for rel in _iter_relative_files(dest)
        if rel.split("/", 1)[0] not in reserved
    }

    for rel in sorted(src_files):
        src_path, dest_path = src / rel, dest / rel
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        if not dest_path.exists():
            shutil.copy2(src_path, dest_path)
            result.added.append(rel)
        elif not _files_equal(src_path, dest_path):
            shutil.copy2(src_path, dest_path)
            result.updated.append(rel)
        else:
            result.unchanged += 1

    for rel in sorted(dest_files - src_files):
        try:
            (dest / rel).unlink()
            result.deleted.append(rel)
        except OSError as exc:
            log.warning("skills_sync: failed to delete %s: %s", dest / rel, exc)

    _prune_empty_dirs(dest)
    try:
        (dest / GENERATED_MARKER).write_text(_MARKER_TEXT, encoding="utf-8")
    except OSError as exc:
        log.warning("skills_sync: could not write %s: %s", dest / GENERATED_MARKER, exc)
    return result


def sync_all(source: Path | None = None,
             target_dirs: tuple[Path, ...] | None = None) -> list[SyncResult]:
    """Mirror the source skills dir into every target. Per-target stats back.

    A failing target is recorded and skipped, not raised: one unwritable
    agent dir must not stop the others from getting the update.

    ``skills/`` is regenerated from ``native-skills/`` first, so a caller that
    only ever runs this one function still gets the native skills on a fresh
    deployment, where ``skills/`` starts out empty (it is gitignored).
    """
    src = source or source_dir()
    tgts = target_dirs or targets()

    if source is None:
        materialize(merged=src)

    if not src.exists():
        log.warning("skills_sync: source %s does not exist; skipping", src)
        return []

    results: list[SyncResult] = []
    for target in tgts:
        try:
            r = _sync_one(src, target)
        except Exception as exc:  # noqa: BLE001 — one bad target isn't fatal
            log.exception("skills_sync: failed to sync to %s", target)
            r = SyncResult(target=str(target), error=str(exc))
        results.append(r)
    return results


def list_skills(source: Path | None = None) -> list[dict]:
    """``[{name, description, path}]`` for every ``skills/<name>/SKILL.md``."""
    src = source or source_dir()
    if not src.exists():
        return []
    out = []
    for entry in sorted(src.iterdir()):
        skill_md = entry / "SKILL.md"
        if not entry.is_dir() or not skill_md.is_file():
            continue
        meta = _parse_frontmatter(skill_md)
        out.append({
            "name": meta.get("name", entry.name),
            "description": meta.get("description", ""),
            "path": str(skill_md),
        })
    return out


def _parse_frontmatter(path: Path) -> dict:
    """Minimal YAML frontmatter reader — only the flat ``key: value`` pairs
    a SKILL.md header actually uses."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    if not text.startswith("---"):
        return {}
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}
    meta = {}
    for line in parts[1].strip().splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            meta[k.strip()] = v.strip()
    return meta
