"""``aw-workspace-cli sideload <dir>`` — install an app straight from a local
directory, no GitHub repo and no marketplace catalog entry.

This is the inner development loop for a new app. Without it the only way to
exercise an app against the real runtime (real Postgres, real migrations, the
real core router in front of your routes) is the whole publish chain: create a
repo, allowlist it, release, merge a catalog sync PR. Two of the nastiest bug
classes this framework has — a route shadowed by a core prefix, and an
``activate()`` that reads a table before its migration ran — are invisible to
``TestClient`` and only appear on a real install, so making a real install
cheap is the fix, not better mocks.

    aw-workspace-cli sideload repos/aw-app-foo        # install (or reinstall)
    aw-workspace-cli sideload repos/aw-app-foo --keep # fail if already installed

``--update`` does not exist on purpose: ``POST /api/apps/<slug>/update``
resolves the version from the catalog, which a sideloaded app is not in
("<slug> not found in catalog"). Redeploy is uninstall + install, which this
does by default. App-owned tables SURVIVE uninstall by design, so seeded rows
are still there afterwards.

A sideloaded app is NOT ``signed`` — trust is derived from catalog membership
(``src/apps/catalog.py::is_marketplace_app``) and a local directory has no
repo to match. High-risk capabilities (``ui:code``, ``containers:manage``,
``config:extend:<app>``) are therefore refused, so a component-mode frontend
will not load. A **declarative** window still renders fine, which is enough to
exercise a settings panel. Publish the app when you need the real thing.
"""
from __future__ import annotations

import argparse
import json
import os
import time

from src.cli import local_client

COMMAND = "sideload"
DESCRIPTION = "Install an app from a local directory (dev loop, no marketplace)"

_POLL_INTERVAL = 1.0
_POLL_TIMEOUT = 300.0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="aw-workspace-cli sideload", description=DESCRIPTION,
        epilog="Redeploy after an edit: just run it again.",
    )
    parser.add_argument("path", help="Path to the app package dir (holds aw-app.json)")
    parser.add_argument(
        "--keep", action="store_true",
        help="Do not uninstall first — fail if the app is already installed",
    )
    return parser


def run(args: list[str]) -> int:
    ns = _build_parser().parse_args(args)

    package_dir = os.path.abspath(os.path.expanduser(ns.path))
    manifest_path = os.path.join(package_dir, "aw-app.json")
    if not os.path.isfile(manifest_path):
        print(f"error: no aw-app.json in {package_dir}")
        return 1
    try:
        with open(manifest_path, encoding="utf-8") as f:
            manifest = json.load(f)
    except (OSError, ValueError) as e:
        print(f"error: could not read {manifest_path}: {e}")
        return 1

    app_id = manifest.get("id")
    if not app_id:
        print(f"error: {manifest_path} has no 'id'")
        return 1
    permissions = manifest.get("permissions") or []

    if not ns.keep:
        status, _ = local_client.request("DELETE", f"/api/apps/{app_id}")
        if status in (200, 202):
            print(f"{app_id}: uninstalled previous install")

    status, body = local_client.request("POST", "/api/apps/install", {
        "package_dir": package_dir,
        "granted_permissions": permissions,
    })
    if status == 409:
        print(f"'{app_id}' is already installed — drop --keep to reinstall it.")
        return 1
    if status not in (200, 202):
        print(f"error: install failed ({status}): {body}")
        return 1

    print(f"{app_id}: installing from {package_dir}…")
    deadline = time.time() + _POLL_TIMEOUT
    while time.time() < deadline:
        status, body = local_client.request("GET", f"/api/apps/{app_id}/install-status")
        state = (body or {}).get("status") if isinstance(body, dict) else None
        if state == "installed":
            refused = _refused_capabilities(permissions)
            print(f"{app_id}: installed")
            if refused:
                # Say it plainly. A silently-missing frontend reads as an app
                # bug and has cost real debugging time more than once.
                print(f"{app_id}: refused (unsigned sideload): {', '.join(refused)}")
                if "ui:code" in refused:
                    print(f"{app_id}: component-mode frontend will NOT load; "
                          f"declarative windows still work.")
            return 0
        if state == "failed":
            print(f"error: {(body or {}).get('error')}")
            return 1
        time.sleep(_POLL_INTERVAL)

    print(f"error: timed out waiting for {app_id} to install")
    return 1


def _refused_capabilities(requested: list[str]) -> list[str]:
    from src.apps.capabilities import is_high_risk
    return [c for c in requested if is_high_risk(c)]
