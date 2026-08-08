"""``aw-workspace-cli status`` — port of the monolith's ``./aw status``.

One screen answering "is this workspace healthy, and what's running in it":
the server's own health, every component (Tier-2 app containers + Tier-1
managed services), and the mapped folders — the three things you'd otherwise
hit three different endpoints for.
"""
from __future__ import annotations

import argparse

from src.cli import local_client
from src.cli.lifecycle import components

COMMAND = "status"
DESCRIPTION = "Show workspace health, components and mapped folders"


def run(args: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="aw-workspace-cli status", description=DESCRIPTION,
    )
    parser.add_argument("--quiet", "-q", action="store_true",
                        help="Only print the health line and a running/total count")
    ns = parser.parse_args(args)

    rc = _health()
    rows = components()
    running = sum(1 for r in rows if r.get("running"))

    if ns.quiet:
        print(f"components: {running}/{len(rows)} running")
        return rc

    print()
    _components(rows, running)
    print()
    _folders()
    return rc


def _health() -> int:
    # /api/health is the one unauthenticated route — reachable even when the
    # API key or identity plumbing is what's broken, which is exactly when
    # you're running `status`.
    try:
        status, body = local_client.request("GET", "/api/health", timeout=10.0)
    except Exception as exc:  # noqa: BLE001 — an unreachable server IS the status
        print(f"workspace: UNREACHABLE at {local_client.base_url()} ({exc})")
        return 1
    if status != 200 or not isinstance(body, dict):
        print(f"workspace: unhealthy ({status}): {body}")
        return 1
    print(f"workspace: {body.get('status', '?')}  "
          f"slug={body.get('workspace') or '?'}  "
          f"version={body.get('version') or 'dev'}")
    print(f"api:       {local_client.base_url()}")
    return 0


def _components(rows: list[dict], running: int) -> None:
    print(f"COMPONENTS ({running}/{len(rows)} running)")
    if not rows:
        print("  (none registered)")
        return
    width = max(len(str(r.get("component", r.get("key", "")))) for r in rows)
    for row in sorted(rows, key=lambda r: str(r.get("component", ""))):
        name = str(row.get("component", row.get("key", "")))
        mark = "●" if row.get("running") else "○"
        port = f":{row['port']}" if row.get("port") else ""
        print(f"  {mark} {name.ljust(width)}  {str(row.get('status', '?')):<8}{port}")


def _folders() -> None:
    status, body = local_client.request("GET", "/api/folders")
    if status == 404:
        # Distinguish "nothing mapped" from "this server predates the feature" —
        # otherwise an old core silently reads as an empty, working registry.
        print("MAPPED FOLDERS (unavailable)")
        print("  (this workspace's server is older than the mapped-folders feature)")
        return
    folders = (body or {}).get("folders", []) if status == 200 and isinstance(body, dict) else []
    print(f"MAPPED FOLDERS ({len(folders)})")
    if not folders:
        print("  (none — map one with: aw-workspace-cli folders add /absolute/path)")
        return
    width = max(len(f["name"]) for f in folders)
    for f in folders:
        mark = " " if f.get("exists") else "!"
        print(f"  {mark} {f['name'].ljust(width)}  {f['mode']:<4}  {f['path']}")
