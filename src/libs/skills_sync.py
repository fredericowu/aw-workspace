"""Materialize ``skills/`` from its two sources, then mirror it per agent CLI.

Ported from the ``agentic-workspace`` monolith (``src/libs/skills_sync.py``).

``skills/`` is **generated**, not authored. It has three upstreams:

* ``native-skills/`` — the skills this repo owns. Committed; the only skills
  a fresh clone or a brand-new deployment starts with.
* each installed app's ``contributes.skills`` — copied in by the apps
  framework (``src/apps/skills.py``), each copy carrying a ``.aw-app-id``
  marker naming its owner.
* each installed app's registered **skill source dirs** — scanned here on
  every sync, each copy carrying ``.aw-skill-source`` on top of ``.aw-app-id``
  (``src/apps/skill_sources.py``). This is the half that can hold skills which
  did not exist at install time: an app declares "also look here" from
  ``Plugin.list_skill_sources()``, and the directory's *contents* are re-read
  every sync. ``aw-autoskill`` writing a new skill into tenant storage each
  night is the case it exists for — a copy-at-activate push can never see one.

Keeping ``skills/`` out of git and generating it is what stops a workspace's
app roster from leaking into this repo's history — before this split, two
app-contributed skills sat committed here, indistinguishable from native ones
except by that marker.

It stays a **real directory of real copies**, never symlinks: app containers
bind-mount it read-only (``$AW_WORKSPACE_SKILLS``, see
``src/apps/runtime.py``), and a link pointing outside the mount resolves to
nothing on the far side — the failure mode being an agent that silently finds
no skills.

Ownership decides who may delete what. :func:`materialize`'s native pass
manages only the native half: an entry carrying ``.aw-app-id`` belongs to its
app, which registers it on activate and removes it on uninstall. Without that
split a sync would delete every app's skill the moment it ran. The sourced
pass is scoped the same way in the other direction — it only ever touches
entries carrying ``.aw-skill-source``, so a pushed skill stays the uninstall
journal's to remove and a native one stays the native pass's.

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
from src.apps import skill_sources
from src.apps.skill_sources import SOURCE_MARKER

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


def _sourced_entries(merged: Path) -> dict[str, str]:
    """``{entry name: owning app id}`` for entries pulled from a source dir.

    Read off :data:`SOURCE_MARKER`, which only entries materialized by
    :func:`_materialize_app_sources` carry. Entries an app *pushed* via
    ``contributes.skills`` have ``.aw-app-id`` but not this one, which is
    exactly what keeps them out of the delete pass below — removing those is
    the uninstall journal's job, not ours.
    """
    if not merged.exists():
        return {}
    out: dict[str, str] = {}
    for entry in merged.iterdir():
        marker = entry / SOURCE_MARKER
        if entry.is_dir() and marker.is_file():
            try:
                out[entry.name] = marker.read_text(encoding="utf-8").strip()
            except OSError:
                continue
    return out


def _materialize_app_sources(dest: Path, result: SyncResult) -> None:
    """Pull each registered app source dir into ``skills/``.

    Runs after the native pass and is scoped to entries carrying
    :data:`SOURCE_MARKER`, so it can neither see nor delete a native skill or
    an app's pushed one.

    An app absent from the registry has been uninstalled (or never reported),
    so its entries go — the exact-mirror rule. An app whose hook answered
    ``ok: False`` never reaches the registry write at all, so its last known
    dirs are still listed here and its skills survive the outage.
    """
    registry = skill_sources.read_registry()
    existing = _sourced_entries(dest)

    seen: set[str] = set()
    for app_id, entry in sorted(registry.items()):
        for raw_dir in entry.get("dirs", []):
            src_root = Path(raw_dir)
            if not src_root.is_dir():
                log.warning("skills_sync: %s lists a missing skill source %s", app_id, src_root)
                continue
            for skill_dir in sorted(p for p in src_root.iterdir() if p.is_dir()):
                if not (skill_dir / "SKILL.md").is_file():
                    continue
                name = skill_dir.name
                owner = existing.get(name)
                if name in seen or (owner is not None and owner != app_id):
                    # Same collision rule as the push half: first writer keeps
                    # the id, the loser is reported rather than silently losing
                    # its skill to whoever synced last.
                    log.warning("skills_sync: skill id %r claimed by more than one source; "
                                "keeping %s", name, owner or "the first")
                    continue
                seen.add(name)
                _copy_source_skill(skill_dir, dest / name, app_id, result)

    for name, app_id in sorted(existing.items()):
        if name in seen:
            continue
        try:
            shutil.rmtree(dest / name)
            result.deleted.append(f"{name}/")
        except OSError as exc:
            log.warning("skills_sync: failed to delete sourced skill %s: %s", name, exc)


def _copy_source_skill(src: Path, dest: Path, app_id: str, result: SyncResult) -> None:
    """Mirror one skill dir in, then stamp both ownership markers."""
    dest.mkdir(parents=True, exist_ok=True)
    src_files = _iter_relative_files(src)
    for rel in sorted(src_files):
        src_path, dest_path = src / rel, dest / rel
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        if not dest_path.exists():
            shutil.copy2(src_path, dest_path)
            result.added.append(f"{dest.name}/{rel}")
        elif not _files_equal(src_path, dest_path):
            shutil.copy2(src_path, dest_path)
            result.updated.append(f"{dest.name}/{rel}")
        else:
            result.unchanged += 1

    for rel in sorted(_iter_relative_files(dest) - src_files):
        if rel in (OWNER_MARKER, SOURCE_MARKER):
            continue
        try:
            (dest / rel).unlink()
            result.deleted.append(f"{dest.name}/{rel}")
        except OSError as exc:
            log.warning("skills_sync: failed to delete %s: %s", dest / rel, exc)

    # .aw-app-id so every existing owner check (collision detection, the
    # native delete pass's reserved set) treats this like any app-owned entry;
    # SOURCE_MARKER on top so _sourced_entries can tell pulled from pushed.
    try:
        (dest / OWNER_MARKER).write_text(app_id, encoding="utf-8")
        (dest / SOURCE_MARKER).write_text(app_id, encoding="utf-8")
    except OSError as exc:
        log.warning("skills_sync: could not mark %s as owned by %s: %s", dest, app_id, exc)


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

    _materialize_app_sources(dest, result)
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
