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
    problems += _app_checks(report.get("app_checks") or [])
    _autostart(report.get("autostart") or [])
    _host_power(report.get("host_power") or {})
    problems += _mcp(report.get("mcp") or {})
    problems += _redis(report.get("redis") or {})

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


def _app_checks(rows: list) -> int:
    """Self-checks the installed apps declared (``contributes.doctor``).

    Core cannot know what "degraded" means for an app it was never built
    against — an app whose test dependencies were never provisioned looks
    perfectly installed from out here. So the app answers for itself, and this
    just renders the answer.
    """
    if not rows:
        return 0
    print()
    bad = [r for r in rows if not r.get("ok")]
    if not bad:
        print(f"App self-checks — {len(rows)} passing "
              f"({', '.join(sorted({r['app'] for r in rows}))})")
        return 0
    print(f"App self-checks — {len(bad)} of {len(rows)} reporting a problem")
    for row in bad:
        detail = row.get("detail")
        if isinstance(detail, dict):
            pending = detail.get("pending") or []
            summary = (f"{len(pending)} pending: {', '.join(map(str, pending[:6]))}"
                       + ("…" if len(pending) > 6 else "")) if pending else "not ok"
        else:
            summary = str(detail)
        print(f"  ✗ {row['app']}: {row['label']} — {summary}")
    return len(bad)


def _autostart(rows: list) -> None:
    """Apps this workspace will not bring up on its own.

    Not counted as a problem — switching an app off is legitimate. It is
    printed because ``auto_start`` is stored per-workspace and reads nowhere
    else: after a boot the app is just absent, indistinguishable from one that
    crashed, while its manifest still says it defaults to on.
    """
    print()
    if not rows:
        print("Auto-start — every managed app comes up with the workspace")
        return
    print(f"Auto-start — {len(rows)} app(s) will NOT start on boot")
    for row in rows:
        print(f"  ○ {row['app']:<14} auto_start is off ({row.get('tier', '?')}) "
              f"— someone has to start it by hand, every time")
    print("      turn one back on in Apps › <app> › Settings › Auto-start, or")
    print("      POST /api/apps/<slug>/config {\"config\": {\"auto_start\": true}}")


def _host_power(section: dict) -> None:
    """Elevated host access — what this machine granted, and who is using it.

    Not counted as a problem: an app that asked for a grant this host does not
    offer fails to LOAD, so it shows up under refused permissions or simply
    isn't running. What this adds is the part that is otherwise invisible from
    inside the workspace — that the machine is carrying an elevated grant at
    all, and whether anything still needs it.
    """
    offers = section.get("host_offers") or []
    print()
    print(f"Host power — {section.get('summary', 'unknown')}")
    if not offers:
        print("      no app on this workspace can reach a host device")
        return
    for row in section.get("apps") or []:
        print(f"  • {row['app']}: {', '.join(row['grants'])}")
    unused = section.get("unused") or []
    if unused:
        # A grant outliving the app that needed it is the residue case: the
        # machine stays elevated and nothing on it reads as different.
        print(f"  ! granted but unused by any loaded app: {', '.join(unused)}")
        print("      drop it from --host-power on the host if nothing needs it")
    if "privileged" in offers:
        print("  ! this host runs app containers WITHOUT isolation (--privileged)")


def _mcp(section: dict) -> int:
    apps = section.get("apps_contributing_tools") or []
    print()
    print(f"MCP — {len(apps)} app(s) contributing tools: {', '.join(apps) or '(none)'}")
    reachable = section.get("reachable")
    if reachable is None:
        print("      mcp-gateway app is not installed — nothing to check")
        return 0
    if not reachable:
        print(f"  ✗ gateway unreachable: {section.get('note', '')}")
        return 1
    tools = section.get("tools", 0)
    upstreams = section.get("local_upstreams") or []
    print(f"      gateway reachable — {tools} tool(s) across {len(upstreams)} upstream(s)")
    if section.get("degraded"):
        print(f"  ✗ {section.get('note', 'gateway is serving zero tools')}")
        return 1
    print("      an upstream the gateway failed to connect to serves zero tools")
    print("      until a reload; the runtime re-checks every 60s")
    return 0


def _redis(section: dict) -> int:
    print()
    if not section:
        print("Redis (redis_coord) — no report from the server")
        return 0
    url = section.get("url", "?")
    if section.get("reachable"):
        print(f"Redis (redis_coord) — reachable at {url}")
        return 0
    print(f"Redis (redis_coord) — ✗ unreachable at {url}")
    print(f"      {section.get('note', '')}")
    return 1
