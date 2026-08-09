"""Mirror ``skills/`` into each agent CLI's own skills directory.

Ported from the ``agentic-workspace`` monolith (``src/libs/skills_sync.py``).
``skills/`` at the workspace root is the single source of truth — it's what
this repo ships, what installed apps contribute into (see
``src/apps/paths.py::skills_dir``), and what a human edits. Every per-agent
directory below is a **generated mirror**, gitignored, never hand-edited.

Sync semantics are an **exact mirror**: a file deleted from ``skills/`` is
deleted from every target, and directories left empty are pruned. Anything
weaker leaves an uninstalled app's skill lying around teaching agents to call
tools that no longer exist.
"""
from __future__ import annotations

import filecmp
import logging
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from src.apps.paths import DEFAULT_WORKSPACE_CONTAINER_DIR

log = logging.getLogger(__name__)


def workspace_root() -> Path:
    return Path(os.path.realpath(
        os.environ.get("AW_WORKSPACE_CONTAINER_DIR", DEFAULT_WORKSPACE_CONTAINER_DIR)
    ))


def source_dir() -> Path:
    """Top-level ``skills/`` — the source of truth."""
    return workspace_root() / "skills"


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
    """Relative POSIX paths of every file under ``root`` (``__pycache__`` pruned)."""
    if not root.exists():
        return set()
    out: set[str] = set()
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d != "__pycache__"]
        for fn in filenames:
            out.add((Path(dirpath) / fn).relative_to(root).as_posix())
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


def sync_all(source: Path | None = None,
             target_dirs: tuple[Path, ...] | None = None) -> list[SyncResult]:
    """Mirror the source skills dir into every target. Per-target stats back.

    A failing target is recorded and skipped, not raised: one unwritable
    agent dir must not stop the others from getting the update.
    """
    src = source or source_dir()
    tgts = target_dirs or targets()

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
