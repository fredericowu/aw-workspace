"""``aw-workspace-cli agent sync`` — port of the monolith's ``./aw agent sync``.

The "I touched something an AI tool reads" button. One command instead of
remembering which of skills/, AGENTS.md and .mcp.json fans out where:

    aw-workspace-cli agent sync         # run every fan-out
    aw-workspace-cli agent sync --check # report drift, change nothing (CI)
    aw-workspace-cli agent status       # what would be synced, and to where

See ``src/libs/agent_sync.py`` for what each step does and why.
"""
from __future__ import annotations

import argparse

from src.libs import agent_sync

COMMAND = "agent"
DESCRIPTION = "Sync agent-facing state: skills/, AGENTS.md and .mcp.json → per-CLI paths"


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="aw-workspace-cli agent", description=DESCRIPTION)
    sub = parser.add_subparsers(dest="cmd")

    sync = sub.add_parser("sync", help="Run the unified sync (skills + AGENTS.md + MCP)")
    sync.add_argument(
        "--check", action="store_true",
        help="Report what is out of date and exit non-zero, without writing anything",
    )
    sub.add_parser("status", help="Show the source files and their fan-out targets")
    return parser


def run(args: list[str]) -> int:
    parser = _build_parser()
    ns = parser.parse_args(args)

    if ns.cmd == "status":
        return _status()
    if ns.cmd in (None, "sync"):
        return _check() if getattr(ns, "check", False) else _sync()

    parser.print_help()
    return 1


# --- printers ----------------------------------------------------------------


def _print_skills(results: list) -> int:
    if not results:
        print("Skills: nothing to sync (no skills/ directory)")
        return 0
    print("Skills:")
    rc = 0
    for r in results:
        d = r.to_dict()
        print(f"  → {d['target']}: "
              f"+{len(d['added'])} ~{len(d['updated'])} -{len(d['deleted'])} ={d['unchanged']}")
        if d.get("error"):
            rc = 1
            print(f"      ERROR: {d['error']}")
    return rc


def _print_files(label: str, results: list) -> int:
    if not results:
        print(f"{label}: nothing to sync")
        return 0
    print(f"{label}:")
    rc = 0
    for r in results:
        d = r.to_dict()
        if not d["success"]:
            rc = 1
            status = f"FAIL: {d['error']}"
        else:
            status = "updated" if d["changed"] else "ok (no change)"
        print(f"  → {d['target']}: {status}")
    return rc


def _print_servers(label: str, results: list) -> int:
    """Per-entry printer shared by Codex and Gemini — both report the same
    ``{name, action, success, error}`` shape, so one printer keeps the two
    outputs identical instead of subtly divergent."""
    if not results:
        print(f"MCP ({label}): nothing to sync")
        return 0
    rc = 0
    counts: dict[str, int] = {}
    interesting, failures = [], []
    for r in results:
        d = r.to_dict()
        counts[d["action"]] = counts.get(d["action"], 0) + 1
        if not d["success"]:
            failures.append(d)
        elif d["action"] in ("add", "remove"):
            interesting.append(d)

    summary = (f"+{counts.get('add', 0)} -{counts.get('remove', 0)} "
               f"={counts.get('noop', 0)} skip={counts.get('skip', 0)}")
    print(f"MCP ({label}): {summary}")
    for d in interesting:
        print(f"  {'+' if d['action'] == 'add' else '-'} {d['name']}")
    for d in results:
        # A skipped CLI is information, not a failure — say why.
        dd = d.to_dict()
        if dd["action"] == "skip" and dd["success"] and dd["error"]:
            print(f"  · {dd['error']}")
    for d in failures:
        rc = 1
        print(f"  FAIL {d['name']} ({d['action']}): {d['error']}")
    return rc


# --- commands ----------------------------------------------------------------


def _sync() -> int:
    result = agent_sync.sync_all()
    codes = [
        _print_skills(result.skills),
        _print_files("AGENTS.md mirrors", result.agents_md),
        _print_files("MCP (file copies)", result.mcp),
        _print_servers("Codex", result.codex),
        _print_servers("Gemini", result.gemini),
    ]
    return 0 if not any(codes) else 1


def _check() -> int:
    """Drift report with no writes — for CI, or before trusting a mirror.

    Compares the source files against their targets directly rather than
    running the sync and looking at `changed`, because a --check that mutates
    is not a check.
    """
    import filecmp

    drift: list[str] = []

    source = agent_sync.agents_md_source()
    for target in agent_sync.agents_md_targets():
        if not source.exists():
            drift.append(f"missing source: {source}")
            break
        if not target.exists():
            drift.append(f"missing mirror: {target}")
        elif not agent_sync._files_identical(source, target):
            drift.append(f"stale mirror: {target}")

    mcp_src = agent_sync.mcp_source()
    for target in agent_sync.mcp_targets():
        if mcp_src.exists() and (
                not target.exists() or not agent_sync._files_identical(mcp_src, target)):
            drift.append(f"stale MCP copy: {target}")

    from src.libs import skills_sync
    src_dir = skills_sync.source_dir()
    if src_dir.exists():
        for target in skills_sync.targets():
            cmp_src = skills_sync._iter_relative_files(src_dir)
            cmp_tgt = skills_sync._iter_relative_files(target)
            if cmp_src != cmp_tgt:
                drift.append(f"skills out of sync: {target}")
                continue
            differing = [rel for rel in cmp_src
                         if not filecmp.cmp(src_dir / rel, target / rel, shallow=False)]
            if differing:
                drift.append(f"skills content differs ({len(differing)} files): {target}")

    if not drift:
        print("agent sync: everything up to date")
        return 0
    print("agent sync: OUT OF DATE")
    for item in drift:
        print(f"  - {item}")
    print("\nRun 'aw-workspace-cli agent sync' to fix.")
    return 1


def _status() -> int:
    from src.libs import skills_sync

    print("Sources (edit these):")
    print(f"  skills/     {skills_sync.source_dir()}")
    print(f"  AGENTS.md   {agent_sync.agents_md_source()}")
    print(f"  .mcp.json   {agent_sync.mcp_source()}")
    print("\nGenerated mirrors (never hand-edit — gitignored):")
    for t in skills_sync.targets():
        print(f"  skills   → {t}")
    for t in agent_sync.agents_md_targets():
        print(f"  AGENTS   → {t}")
    for t in agent_sync.mcp_targets():
        print(f"  mcp.json → {t}")
    print(f"  mcp      → Codex (~/.codex/config.toml, via the codex CLI)")
    print(f"  mcp      → Gemini ({agent_sync._gemini_settings_path()})")
    return 0
