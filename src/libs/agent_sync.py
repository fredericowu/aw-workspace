"""Unified agent-state sync — skills, AGENTS.md and MCP servers.

Ported from the ``agentic-workspace`` monolith (``src/libs/agent_sync.py``),
trimmed to what this workspace actually has. It brings together every artifact
that decides *how an AI agent sees this workspace*, so "I changed something an
agent reads" is one command instead of three subsystems:

* **``skills/``** → mirrored into each CLI's own skills dir (:mod:`skills_sync`).
* **``AGENTS.md``** → mirrored to the per-CLI filenames each tool loads on its
  own (``CLAUDE.md``, ``GEMINI.md``, ``.github/copilot-instructions.md``).
  AGENTS.md is the source of truth; the mirrors are generated and gitignored.
* **``.mcp.json``** → the canonical MCP config (Claude Code reads this path
  natively), fanned out to ``.cursor/mcp.json``, Gemini's
  ``.gemini/settings.json``, and Codex's ``~/.codex/config.toml``.

Differences from the monolith's version, and why:

* No ``src/config/mcp.json`` → ``.mcp.json`` generation step. In this
  workspace ``.mcp.json`` IS the source: apps write their own entries into it
  at boot (aw-mcp-gateway, gated behind the ``mcp:register-gateway``
  capability — see ``src/apps/runtime.py``), so re-generating it from a
  static config would delete what the app framework just registered.
* No OpenClaw step — that subsystem doesn't exist here.

Every fan-out target degrades to a recorded skip when its CLI isn't
installed, which is the normal case on a slim BYOD image: a workspace with
only Claude Code present should report "codex: not installed", not fail.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from src.libs import skills_sync
from src.libs.skills_sync import workspace_root

log = logging.getLogger(__name__)

# Only these MCP entries are managed on Codex/Gemini — anything the user added
# by hand survives a sync untouched. Same rule as the monolith.
MANAGED_PREFIXES = ("aw-", "playwright")


def mcp_source() -> Path:
    return workspace_root() / ".mcp.json"


def mcp_targets() -> tuple[Path, ...]:
    return (workspace_root() / ".cursor" / "mcp.json",)


def agents_md_source() -> Path:
    return workspace_root() / "AGENTS.md"


def agents_md_targets() -> tuple[Path, ...]:
    return (
        workspace_root() / "CLAUDE.md",
        workspace_root() / "GEMINI.md",
        workspace_root() / ".github" / "copilot-instructions.md",
    )


@dataclass
class FileSyncResult:
    """One generated-mirror file."""

    target: str
    success: bool = True
    changed: bool = False
    error: str | None = None

    def to_dict(self) -> dict:
        return {"target": self.target, "success": self.success,
                "changed": self.changed, "error": self.error}


@dataclass
class ServerSyncResult:
    """One MCP server on a CLI that's driven per-entry (Codex, Gemini)."""

    name: str
    action: str = "noop"  # add | remove | noop | skip
    success: bool = True
    error: str | None = None

    def to_dict(self) -> dict:
        return {"name": self.name, "action": self.action,
                "success": self.success, "error": self.error}


@dataclass
class AgentSyncResult:
    skills: list = field(default_factory=list)
    agents_md: list[FileSyncResult] = field(default_factory=list)
    mcp: list[FileSyncResult] = field(default_factory=list)
    codex: list[ServerSyncResult] = field(default_factory=list)
    gemini: list[ServerSyncResult] = field(default_factory=list)


def _files_identical(a: Path, b: Path) -> bool:
    try:
        return a.read_bytes() == b.read_bytes()
    except OSError:
        return False


def _copy_file(source: Path, target: Path) -> FileSyncResult:
    """Copy ``source`` → ``target``, reporting whether anything actually moved."""
    result = FileSyncResult(target=str(target))
    if not source.exists():
        result.success = False
        result.error = f"{source} not found"
        return result
    try:
        if target.exists() and _files_identical(source, target):
            return result
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        result.changed = True
    except OSError as exc:
        result.success = False
        result.error = str(exc)
    return result


def sync_agents_md(source: Path | None = None,
                   target_paths: tuple[Path, ...] | None = None) -> list[FileSyncResult]:
    """AGENTS.md → the per-CLI filenames each tool reads on its own.

    Claude Code loads ``CLAUDE.md``, Gemini ``GEMINI.md``, Copilot
    ``.github/copilot-instructions.md``. Rather than ask a human to keep four
    copies in step, one file is authored and the rest are byte copies —
    which is why they're gitignored and carry a "do not edit" banner in
    AGENTS.md itself.
    """
    src = source or agents_md_source()
    return [_copy_file(src, t) for t in (target_paths or agents_md_targets())]


def sync_mcp(source: Path | None = None,
             target_paths: tuple[Path, ...] | None = None) -> list[FileSyncResult]:
    """``.mcp.json`` → byte-identical copies for CLIs that want their own path."""
    src = source or mcp_source()
    return [_copy_file(src, t) for t in (target_paths or mcp_targets())]


def _read_servers(source: Path | None = None) -> dict[str, dict]:
    src = source or mcp_source()
    try:
        data = json.loads(src.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("agent_sync: cannot read %s: %s", src, exc)
        return {}
    servers = data.get("mcpServers")
    return servers if isinstance(servers, dict) else {}


def _is_managed(name: str) -> bool:
    return any(name.startswith(p) for p in MANAGED_PREFIXES)


# --- Codex -------------------------------------------------------------------


def _codex_available() -> bool:
    return shutil.which("codex") is not None


def _codex_list() -> dict[str, dict]:
    """Currently-registered Codex MCP servers, keyed by name."""
    try:
        proc = subprocess.run(["codex", "mcp", "list", "--json"],
                              capture_output=True, text=True, timeout=30)
        if proc.returncode != 0:
            return {}
        data = json.loads(proc.stdout or "{}")
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
        return {}
    if isinstance(data, dict):
        return data
    if isinstance(data, list):
        return {e.get("name", ""): e for e in data if isinstance(e, dict)}
    return {}


def _codex_add(name: str, cfg: dict) -> ServerSyncResult:
    """Register one server with Codex, HTTP and stdio shapes alike."""
    if cfg.get("type") == "http" or cfg.get("url"):
        cmd = ["codex", "mcp", "add", name, "--url", str(cfg.get("url", ""))]
        for header, value in (cfg.get("headers") or {}).items():
            cmd += ["--header", f"{header}: {value}"]
    else:
        cmd = ["codex", "mcp", "add", name]
        for key, value in (cfg.get("env") or {}).items():
            cmd += ["--env", f"{key}={value}"]
        cmd += ["--", str(cfg.get("command", ""))] + [str(a) for a in cfg.get("args") or []]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if proc.returncode != 0:
            return ServerSyncResult(name=name, action="add", success=False,
                                    error=(proc.stderr or proc.stdout).strip()[:300])
    except (OSError, subprocess.SubprocessError) as exc:
        return ServerSyncResult(name=name, action="add", success=False, error=str(exc))
    return ServerSyncResult(name=name, action="add")


def _codex_remove(name: str) -> ServerSyncResult:
    try:
        proc = subprocess.run(["codex", "mcp", "remove", name],
                              capture_output=True, text=True, timeout=30)
        if proc.returncode != 0:
            return ServerSyncResult(name=name, action="remove", success=False,
                                    error=(proc.stderr or proc.stdout).strip()[:300])
    except (OSError, subprocess.SubprocessError) as exc:
        return ServerSyncResult(name=name, action="remove", success=False, error=str(exc))
    return ServerSyncResult(name=name, action="remove")


def sync_codex_mcp(source: Path | None = None) -> list[ServerSyncResult]:
    """Project ``.mcp.json`` into Codex, which has no native ``.mcp.json``.

    Only ``aw-*``/``playwright*`` entries are touched — a server the user
    added to Codex themselves is left exactly where it is.
    """
    if not _codex_available():
        return [ServerSyncResult(name="codex", action="skip", error="codex CLI not installed")]

    desired = {n: c for n, c in _read_servers(source).items() if _is_managed(n)}
    existing = _codex_list()
    results: list[ServerSyncResult] = []

    for name in sorted(set(existing) - set(desired)):
        if _is_managed(name):
            results.append(_codex_remove(name))
    for name in sorted(desired):
        # Re-add rather than diff: `codex mcp add` is idempotent-by-replace and
        # comparing nested shapes across CLI versions was the monolith's most
        # fragile bit.
        if name in existing:
            _codex_remove(name)
        results.append(_codex_add(name, desired[name]))
    return results


# --- Gemini ------------------------------------------------------------------


def _gemini_settings_path() -> Path:
    return workspace_root() / ".gemini" / "settings.json"


def sync_gemini_mcp(source: Path | None = None) -> list[ServerSyncResult]:
    """Write the ``mcpServers`` block of ``.gemini/settings.json`` directly.

    ``gemini mcp add`` rewrites the whole block and drops unmanaged entries,
    so the monolith settled on editing the file — same choice here. Only
    managed names are replaced; everything else in the file is preserved.
    """
    path = _gemini_settings_path()
    desired = {n: c for n, c in _read_servers(source).items() if _is_managed(n)}

    try:
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except (OSError, json.JSONDecodeError) as exc:
        return [ServerSyncResult(name="gemini", action="skip", success=False, error=str(exc))]
    if not isinstance(data, dict):
        data = {}

    current = data.get("mcpServers") if isinstance(data.get("mcpServers"), dict) else {}
    merged = {n: c for n, c in current.items() if not _is_managed(n)}
    merged.update(desired)

    results = [ServerSyncResult(name=n, action="add") for n in sorted(desired)]
    results += [ServerSyncResult(name=n, action="remove")
                for n in sorted(set(current) - set(desired)) if _is_managed(n)]

    if current == merged:
        return [ServerSyncResult(name=n, action="noop") for n in sorted(desired)] or results

    data["mcpServers"] = merged
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    except OSError as exc:
        return [ServerSyncResult(name="gemini", action="skip", success=False, error=str(exc))]
    return results


# --- entrypoint --------------------------------------------------------------


def sync_all() -> AgentSyncResult:
    """Run every fan-out. Nothing raises — each step reports its own outcome."""
    result = AgentSyncResult()
    result.skills = skills_sync.sync_all()
    result.agents_md = sync_agents_md()
    result.mcp = sync_mcp()
    result.codex = sync_codex_mcp()
    result.gemini = sync_gemini_mcp()
    return result
