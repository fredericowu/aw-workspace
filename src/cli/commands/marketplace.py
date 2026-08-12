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

    sub.add_parser(
        "update-all",
        help="Update every installed app to the catalog's current version",
    )

    sub.add_parser("info", help="List installed apps and their versions")
    return parser


def run(args: list[str]) -> int:
    parser = _build_parser()
    ns = parser.parse_args(args)

    if ns.action == "install":
        return _install(ns.app, update=ns.update)
    if ns.action == "update-all":
        return _update_all()
    if ns.action == "info":
        return _info()

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
        return _update_one(app_id)

    # No `signed`: the server derives trust from marketplace-catalog membership
    # (src/apps/catalog.py::is_marketplace_app) and ignores what a client
    # claims. Do NOT "fix" a trust problem by adding it back here — the field
    # was honoured once, and this payload's silence produced UNSIGNED installs
    # of catalog apps, which cost them `ui:code` and with it every frontend
    # contribution they make.
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


def _update_one(app_id: str) -> int:
    """Update a single installed app; returns a process-style exit code."""
    status, body = local_client.request("POST", f"/api/apps/{app_id}/update")
    if status == 404:
        print(f"'{app_id}' is not installed — run 'aw-workspace-cli marketplace install {app_id}' first.")
        return 1
    if status not in (200, 202):
        print(f"error: update failed ({status}): {body}")
        return 1
    return 0 if _poll_until_done(app_id) else 1


def _installed_apps() -> list[dict] | None:
    """Every installed app's ``/api/apps`` row (slug + running version), or
    ``None`` if the listing call failed."""
    status, body = local_client.request("GET", "/api/apps")
    if status != 200 or not isinstance(body, list):
        print(f"error: could not list installed apps ({status}): {body}")
        return None
    return [a for a in body if isinstance(a, dict) and a.get("slug")]


def _catalog_apps() -> list[dict] | None:
    """The full marketplace catalog listing, or ``None`` on failure."""
    status, body = local_client.request("GET", "/api/apps/-/catalog")
    if status != 200:
        print(f"error: could not load the marketplace catalog ({status}): {body}")
        return None
    return (body or {}).get("apps", []) if isinstance(body, dict) else []


def _required_dependency_ids(entry: dict) -> list[str]:
    """Required ``dependencies.apps`` ids from a catalog entry (mirrors
    ``Reconciler._required_app_dependencies`` — optional/not-required
    entries don't gate update order)."""
    deps = (entry.get("dependencies") or {}).get("apps") or []
    ids = []
    for raw in deps:
        if isinstance(raw, str):
            dep_id, required = raw.strip(), True
        elif isinstance(raw, dict):
            dep_id = str(raw.get("id") or "").strip()
            required = raw.get("required", True) is not False and raw.get("optional") is not True
        else:
            continue
        if dep_id and required:
            ids.append(dep_id)
    return ids


def _dependency_order(outdated: dict[str, dict]) -> list[str]:
    """Order ``outdated`` (slug -> catalog entry) so every app comes after
    its dependencies, via Kahn's algorithm: repeatedly take the apps with no
    unresolved dependency left in the set, then remove them and repeat. A
    dependency that isn't itself outdated doesn't gate order here — it's
    already at its current version, and the server installs it on demand if
    it's ever missing (``Reconciler._install_dependencies``)."""
    remaining = {
        slug: [d for d in _required_dependency_ids(entry) if d in outdated]
        for slug, entry in outdated.items()
    }
    ordered: list[str] = []
    placed: set[str] = set()
    while remaining:
        ready = sorted(slug for slug, deps in remaining.items() if all(d in placed for d in deps))
        if not ready:
            cycle = ", ".join(sorted(remaining))
            print(f"warning: cyclic dependency among outdated apps ({cycle}) — updating anyway")
            ready = sorted(remaining)
        for slug in ready:
            ordered.append(slug)
            placed.add(slug)
            del remaining[slug]
    return ordered


def _update_all() -> int:
    installed = _installed_apps()
    if installed is None:
        return 1
    if not installed:
        print("no apps installed — nothing to update")
        return 0

    catalog = _catalog_apps()
    if catalog is None:
        return 1
    catalog_by_id = {
        (a.get("id") or a.get("slug")): a for a in catalog if isinstance(a, dict)
    }

    outdated: dict[str, dict] = {}
    for app in installed:
        slug = app["slug"]
        entry = catalog_by_id.get(slug)
        if entry is None:
            continue  # not in any configured marketplace source — nothing to compare against
        catalog_version = entry.get("version") or ""
        if catalog_version and catalog_version != app.get("version"):
            outdated[slug] = entry

    if not outdated:
        print(f"all {len(installed)} installed app(s) already up to date")
        return 0

    order = _dependency_order(outdated)
    print(f"updating {len(order)} outdated app(s) in dependency order: {', '.join(order)}")
    failed = [slug for slug in order if _update_one(slug) != 0]

    if failed:
        print(f"done — {len(order) - len(failed)} ok, {len(failed)} failed: {', '.join(failed)}")
        return 1
    print(f"done — all {len(order)} app(s) up to date")
    return 0


def _info() -> int:
    installed = _installed_apps()
    if installed is None:
        return 1
    if not installed:
        print("no apps installed")
        return 0

    for app in sorted(installed, key=lambda a: a["slug"]):
        print(f"{app['slug']} {app.get('version') or 'unknown'}")
    return 0


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
