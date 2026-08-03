"""aw-workspace-cli marketplace install <app> [--update] — install or update an app from
this workspace's marketplace catalog, via the same ``/api/apps/*`` routes the
Apps SPA uses (see ``src/apps/routes.py``)."""
from __future__ import annotations

import argparse
import time

from src.cli import local_client

COMMAND = "marketplace"
DESCRIPTION = "Install or update apps from the marketplace catalog"

_POLL_INTERVAL = 1.0
_POLL_TIMEOUT = 180.0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="aw-workspace-cli marketplace", description=DESCRIPTION,
    )
    sub = parser.add_subparsers(dest="action", required=True)

    install = sub.add_parser("install", help="Install (or update) an app from the catalog")
    install.add_argument("app", help="App id/slug as listed in the marketplace catalog")
    install.add_argument(
        "--update", action="store_true",
        help="Update an already-installed app to the catalog's current version",
    )
    return parser


def run(args: list[str]) -> int:
    parser = _build_parser()
    ns = parser.parse_args(args)

    if ns.action == "install":
        return _install(ns.app, update=ns.update)

    parser.print_help()
    return 1


def _catalog_entry(app_id: str) -> dict | None:
    status, body = local_client.request("GET", "/api/apps/-/catalog")
    if status != 200:
        print(f"error: could not load the marketplace catalog ({status}): {body}")
        return None
    apps = (body or {}).get("apps", []) if isinstance(body, dict) else []
    return next((a for a in apps if (a.get("id") or a.get("slug")) == app_id), None)


def _install(app_id: str, update: bool) -> int:
    entry = _catalog_entry(app_id)
    if entry is None:
        print(f"error: '{app_id}' is not in the marketplace catalog")
        return 1

    if update:
        status, body = local_client.request("POST", f"/api/apps/{app_id}/update")
        if status == 404:
            print(f"'{app_id}' is not installed — run 'aw-workspace-cli marketplace install {app_id}' first.")
            return 1
        if status not in (200, 202):
            print(f"error: update failed ({status}): {body}")
            return 1
        return 0 if _poll_until_done(app_id) else 1

    payload = {
        "app_id": app_id,
        "repo": entry.get("repo"),
        "ref": entry.get("ref") or "HEAD",
        "version": entry.get("version") or "",
    }
    status, body = local_client.request("POST", "/api/apps/install", payload)
    if status == 409:
        print(f"'{app_id}' is already installed — use --update to update it.")
        return 1
    if status not in (200, 202):
        print(f"error: install failed ({status}): {body}")
        return 1

    return 0 if _poll_until_done(app_id) else 1


def _poll_until_done(app_id: str) -> bool:
    print(f"{app_id}: installing…")
    deadline = time.monotonic() + _POLL_TIMEOUT
    while time.monotonic() < deadline:
        status, body = local_client.request("GET", f"/api/apps/{app_id}/install-status")
        if status == 200 and isinstance(body, dict):
            state = body.get("status")
            if state == "no-op":
                print(f"{app_id}: already up to date")
                return True
            if state == "installed":
                print(f"{app_id}: installed")
                return True
            if state == "failed":
                print(f"{app_id}: failed — {body.get('error')}")
                return False
        time.sleep(_POLL_INTERVAL)
    print(f"{app_id}: timed out waiting for the install job to finish")
    return False
