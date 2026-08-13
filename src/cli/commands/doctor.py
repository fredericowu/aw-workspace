"""``aw-workspace-cli doctor`` — what is silently degraded right now.

``status`` answers "is the server up and how many components are running",
which a workspace can pass while being badly broken: CLIs present but
non-functional, an app whose entire frontend was refused, an MCP gateway
serving none of an app's tools. All three happened on 2026-08-12 and none of
them appeared anywhere a human or an agent would look — only in a container
log, repeated every heal pass, seen by no one.

So this command exists to be run BEFORE concluding that something is fine,
by a person or by an agent. It exits non-zero when anything is degraded, so
it also works as a check in a script.
"""
from __future__ import annotations

import argparse
import json

from src.cli import local_client

COMMAND = "doctor"
DESCRIPTION = "Report anything silently degraded (CLIs, permissions, MCP tools)"


def run(args: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="aw-workspace-cli doctor", description=DESCRIPTION,
    )
    parser.add_argument("--json", action="store_true", help="Print the raw payload")
    ns = parser.parse_args(args)

    status, body = local_client.request("GET", "/api/apps/-/doctor")
    if status != 200:
        print(f"error: could not reach the workspace API ({status}): {body}")
        return 2

    report = body if isinstance(body, dict) else json.loads(body)
    if ns.json:
        print(json.dumps(report, indent=2))
        return 0 if report.get("ok") else 1

    problems = 0
    problems += _system_clis(report.get("system_clis") or {})
    problems += _permissions(report.get("permissions") or [])
    _mcp(report.get("mcp") or {})

    print()
    if problems:
        print(f"{problems} problem(s) found — see above.")
        return 1
    print("No silent degradation found.")
    return 0


def _system_clis(section: dict) -> int:
    total = section.get("total", 0)
    unhealthy = section.get("unhealthy") or []
    print()
    print(f"System CLIs — {total - len(unhealthy)}/{total} healthy")
    for row in unhealthy:
        # The reason matters more than the name: "not on PATH" is a missing
        # install, while a failing verify is the case a `which` check misses.
        print(f"  ✗ {row['cli']:<12} ({row['app']}) {row.get('reason', '')}")
        if row.get("path"):
            print(f"      present at {row['path']} but not working")
        if row.get("heal_failures"):
            print(f"      self-heal has failed {row['heal_failures']}x: "
                  f"{row.get('last_heal_error') or 'unknown error'}")
    return len(unhealthy)


def _permissions(rows: list) -> int:
    if not rows:
        print()
        print("Permissions — every loaded app got what its manifest asked for")
        return 0
    print()
    print("Permissions — refused capabilities")
    for row in rows:
        print(f"  ✗ {row['app']}: {', '.join(row['refused'])}")
        print(f"      {row.get('reason', '')}")
        if "ui:code" in row.get("refused", []):
            # Worth calling out by name: this is the one whose symptom looks
            # nothing like its cause — the window chrome still draws.
            print("      ui:code refused means this app contributes NO frontend: "
                  "no window body, no nav row, no titlebar actions")
    return len(rows)


def _mcp(section: dict) -> None:
    apps = section.get("apps_contributing_tools") or []
    print()
    print(f"MCP — {len(apps)} app(s) contributing tools: {', '.join(apps) or '(none)'}")
    print("      an upstream the gateway failed to connect to serves zero tools")
    print("      until a reload; the runtime re-checks every 60s")
