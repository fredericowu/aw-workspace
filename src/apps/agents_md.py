"""``contributes.agents_md`` — one app-owned Markdown block for AGENTS.md.

Step 1 of the two-step effort in
``docs/design/apps-contribute-agents-md-blocks.md``. An app declares a single
Markdown file via ``contributes.agents_md: {"path": ..., "title": ...}``; this
module validates that entry and records it in a small registry at
``AW_WORKSPACE_HOME/agents-md-blocks.json`` — the ``skill_sources.py``
precedent, adapted from "a list of dirs" to "one block per app".

This module does NOT touch AGENTS.md itself. ``src/libs/agent_sync.py``'s
``sync_agents_md()`` is the only writer of AGENTS.md's managed region, reading
this registry fresh on every sync and rebuilding the whole region from it —
see that module for why (exact-mirror semantics, fail-safe parsing).

Registry mutation happens under the same ``locks/agents-md.lock`` flock the
rebuild uses, so a register/unregister can never race a rebuild reading the
registry mid-write.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any

from src.apps import fs_lock, paths

log = logging.getLogger(__name__)

#: Marker prefixes a contributed block must never contain — they are the
#: region/block delimiters ``agent_sync.py`` parses, so a block carrying one
#: could make a later render ambiguous or misleading. Checked at register
#: time, not at every sync: see module docstring + the design doc.
_FORBIDDEN_MARKERS = ("<!-- aw-app-md", "<!-- aw-apps-md")

#: Every byte of a block lands in every agent's context on every run — warn,
#: don't block, past this size. The real fix for bloat is step 2 (decoupling).
SIZE_WARNING_BYTES = 4096

#: How long to wait for the shared agents-md lock before giving up.
LOCK_TIMEOUT_S = 30.0


class AgentsMdError(RuntimeError):
    """Raised when a ``contributes.agents_md`` entry is invalid."""


def _lock_path() -> str:
    return fs_lock.lock_path("agents-md")


def resolve_agents_md_path(package_dir: str, agents_md_path: str) -> str:
    """Validate + resolve a ``contributes.agents_md.path`` entry.

    Same escape check as ``skills.resolve_skill_dir``: ``agents_md_path`` must
    point at a file inside the app's package dir (no escaping via ``..``).
    Returns the absolute path to that file.
    """
    pkg_root = os.path.abspath(package_dir)
    md_path = os.path.abspath(os.path.join(pkg_root, agents_md_path))
    if not md_path.startswith(pkg_root + os.sep):
        raise AgentsMdError(f"agents_md path {agents_md_path!r} escapes the app package dir")
    if not os.path.isfile(md_path):
        raise AgentsMdError(f"agents_md file not found: {agents_md_path!r}")
    return md_path


def _check_block_safety(app_id: str, abs_path: str) -> None:
    """Marker-injection is a hard reject; an oversized block is a warning.

    Read once, at register time — the sync-time render reads the file fresh
    again later, but this is what stops a bad block from ever entering the
    registry in the first place.
    """
    try:
        with open(abs_path, "rb") as f:
            raw = f.read()
    except OSError as exc:
        raise AgentsMdError(f"could not read agents_md block for {app_id!r}: {exc}") from exc
    text = raw.decode("utf-8", errors="replace")
    for marker in _FORBIDDEN_MARKERS:
        if marker in text:
            raise AgentsMdError(
                f"agents_md block for {app_id!r} contains {marker!r} — marker "
                "injection would break the AGENTS.md managed-region parse"
            )
    if len(raw) > SIZE_WARNING_BYTES:
        log.warning(
            "apps: agents_md block for %s is %d bytes (over the %d-byte advisory "
            "limit) — every byte lands in every agent's context on every run",
            app_id, len(raw), SIZE_WARNING_BYTES,
        )


def registry_path() -> str:
    """Where the recorded blocks live — durable, tenant-scoped state."""
    return os.path.join(paths.workspace_home(), "agents-md-blocks.json")


def read_registry() -> dict[str, dict[str, Any]]:
    """``{app_id: {"path": <abs path>, "title": ...}}`` for every app that
    has registered a block.

    A missing or corrupt file reads as empty rather than raising: this is
    consulted from the sync path, and the sync path failing outright is worse
    than it running with no app-contributed blocks.
    """
    path = registry_path()
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("agents_md: unreadable registry %s: %s", path, exc)
        return {}
    if not isinstance(data, dict):
        return {}
    return {
        str(app_id): entry
        for app_id, entry in data.items()
        if isinstance(entry, dict) and isinstance(entry.get("path"), str)
    }


def _write_registry(registry: dict[str, dict[str, Any]]) -> None:
    path = registry_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(registry, fh, indent=2, sort_keys=True)
    os.replace(tmp, path)


class AgentsMdRegistry:
    """Runtime-owned backend for the ``contributes.agents_md`` surface."""

    def register(self, app_id: str, package_dir: str, agents_md_path: str,
                 title: str | None = None) -> str:
        """Validate the entry and record it in the registry.

        Returns the resolved absolute path (journaled so ``unregister`` can
        drop exactly this app's entry; removal is otherwise registry-based,
        not payload-based — see ``src/apps/runtime.py``).
        """
        abs_path = resolve_agents_md_path(package_dir, agents_md_path)
        _check_block_safety(app_id, abs_path)
        fd = fs_lock.acquire(_lock_path(), timeout=LOCK_TIMEOUT_S)
        if fd is None:
            raise AgentsMdError(
                f"could not acquire the agents-md lock within {LOCK_TIMEOUT_S}s")
        try:
            registry = read_registry()
            registry[app_id] = {"path": abs_path, "title": title or ""}
            _write_registry(registry)
        finally:
            fs_lock.release(fd)
        return abs_path

    def unregister(self, app_id: str) -> None:
        """Drop ``app_id``'s block from the registry (uninstall replay)."""
        fd = fs_lock.acquire(_lock_path(), timeout=LOCK_TIMEOUT_S)
        if fd is None:
            log.warning("apps: could not acquire the agents-md lock to unregister %s", app_id)
            return
        try:
            registry = read_registry()
            if registry.pop(app_id, None) is not None:
                _write_registry(registry)
        finally:
            fs_lock.release(fd)
