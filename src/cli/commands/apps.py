"""``aw-workspace-cli apps`` — inspect the installed apps.

Port of the read side of the monolith's ``./aw info``/component listing, in
this workspace's own vocabulary. Installing/updating is a separate verb
already: ``aw-workspace-cli marketplace install|update-all|info``.
"""
from __future__ import annotations

import argparse
import json

from src.cli import local_client

COMMAND = "apps"
DESCRIPTION = "List installed apps (add --json for the raw payload)"


def run(args: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="aw-workspace-cli apps", description=DESCRIPTION,
    )
    parser.add_argument("slug", nargs="?", help="Show one app's full record")
    parser.add_argument("--json", action="store_true", dest="as_json",
                        help="Print the raw JSON instead of a table")
    ns = parser.parse_args(args)

    status, body = local_client.request("GET", "/api/apps")
    if status != 200:
        print(f"error: could not list apps ({status}): {body}")
        return 1
    apps = body if isinstance(body, list) else (body or {}).get("apps", [])

    if ns.slug:
        match = next((a for a in apps if a.get("slug") == ns.slug), None)
        if match is None:
            print(f"error: {ns.slug!r} is not installed")
            return 1
        print(json.dumps(match, indent=2))
        return 0

    if ns.as_json:
        print(json.dumps(apps, indent=2))
        return 0

    if not apps:
        print("No apps installed.")
        return 0
    width = max(len(a.get("slug", "")) for a in apps)
    ver_w = max(len(str(a.get("version", ""))) for a in apps)
    for a in sorted(apps, key=lambda x: x.get("slug", "")):
        print(f"  {a.get('slug', '').ljust(width)}  "
              f"{str(a.get('version', '')).ljust(ver_w)}  "
              f"{a.get('tier', ''):<9}  {a.get('category', '')}")
    return 0
