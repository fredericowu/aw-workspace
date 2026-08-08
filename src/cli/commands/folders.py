"""``aw-workspace-cli folders`` — map any directory into the workspace by name.

The CLI face of ``src/api/folders.py``. Deliberately mirrors the shape the
monolith's ``./aw`` gave ``knowledge_base.map_paths`` in ``aw.json`` (a list of
plain paths, no git involved), but as first-class workspace state instead of a
hand-edited config file:

    aw-workspace-cli folders list
    aw-workspace-cli folders add /opt/aw-workspace/docs
    aw-workspace-cli folders add /srv/datasets --name data --mode rw
    aw-workspace-cli folders browse /opt/aw-workspace
    aw-workspace-cli folders rm docs

Every mutation goes through the same identity-gated REST routes the SPA uses,
so the CLI can't drift from the UI, and adding/removing a folder re-mounts it
into the running apps that declared ``$AW_WORKSPACE_FOLDERS`` before returning.
"""
from __future__ import annotations

import argparse

from src.cli import local_client

COMMAND = "folders"
DESCRIPTION = "Map any folder into the workspace by name (no git repo needed)"


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="aw-workspace-cli folders", description=DESCRIPTION,
    )
    sub = parser.add_subparsers(dest="action")

    sub.add_parser("list", help="List every mapped folder")

    add = sub.add_parser("add", help="Map a folder (re-mapping a name updates it)")
    add.add_argument("path", help="Absolute path to the directory to map")
    add.add_argument("--name", help="Handle to address it by (default: the basename)")
    add.add_argument("--mode", choices=("ro", "rw"), default="ro",
                     help="Mount mode handed to app containers (default: ro)")

    rm = sub.add_parser("rm", help="Unmap a folder by name")
    rm.add_argument("name")

    browse = sub.add_parser("browse", help="List subdirectories, to find a path to map")
    browse.add_argument("path", nargs="?", help="Directory to list (default: workspace root)")

    return parser


def run(args: list[str]) -> int:
    parser = _build_parser()
    ns = parser.parse_args(args)

    if ns.action in (None, "list"):
        return _list()
    if ns.action == "add":
        return _add(ns.path, ns.name, ns.mode)
    if ns.action == "rm":
        return _rm(ns.name)
    if ns.action == "browse":
        return _browse(ns.path)

    parser.print_help()
    return 1


def _folders() -> tuple[int, list[dict]]:
    status, body = local_client.request("GET", "/api/folders")
    if status != 200:
        print(f"error: could not list mapped folders ({status}): {body}")
        return status, []
    return 200, (body or {}).get("folders", []) if isinstance(body, dict) else []


def _list() -> int:
    status, folders = _folders()
    if status != 200:
        return 1
    if not folders:
        print("No folders mapped.")
        print("Map one with: aw-workspace-cli folders add /absolute/path")
        return 0

    name_w = max(len(f["name"]) for f in folders)
    print(f"{'NAME'.ljust(name_w)}  MODE  PATH")
    for f in folders:
        # A missing path is worth flagging but not an error: it may exist only
        # on the container-engine host (see folders.describe()).
        note = "" if f.get("exists") else "   (not visible from the workspace process)"
        print(f"{f['name'].ljust(name_w)}  {f['mode']:<4}  {f['path']}{note}")
    return 0


def _add(path: str, name: str | None, mode: str) -> int:
    payload = {"path": path, "mode": mode}
    if name:
        payload["name"] = name
    status, body = local_client.request("POST", "/api/folders", json_body=payload)
    if status != 200:
        detail = body.get("detail") if isinstance(body, dict) else body
        print(f"error: could not map {path} ({status}): {detail}")
        return 1

    folder = (body or {}).get("folder", {})
    print(f"Mapped {folder.get('name')} -> {folder.get('path')} ({folder.get('mode')})")
    if not folder.get("exists"):
        print("warning: that path is not visible from the workspace process. It will "
              "still be handed to app containers as a host path — check the spelling "
              "if that isn't what you meant.")
    _report_remap(body)
    return 0


def _rm(name: str) -> int:
    status, body = local_client.request("DELETE", f"/api/folders/{name}")
    if status == 404:
        print(f"error: no mapped folder named {name!r}")
        return 1
    if status != 200:
        print(f"error: could not unmap {name} ({status}): {body}")
        return 1
    print(f"Unmapped {name}")
    _report_remap(body)
    return 0


def _report_remap(body: object) -> None:
    remapped = (body or {}).get("remapped_apps", []) if isinstance(body, dict) else []
    if remapped:
        print(f"Remounted into: {', '.join(remapped)}")


def _browse(path: str | None) -> int:
    query = f"?path={path}" if path else ""
    status, body = local_client.request("GET", f"/api/folders/-/browse{query}")
    if status != 200:
        detail = body.get("detail") if isinstance(body, dict) else body
        print(f"error: could not browse ({status}): {detail}")
        return 1
    print(body.get("path", ""))
    for entry in body.get("entries", []):
        print(f"  {entry['path']}")
    if not body.get("entries"):
        print("  (no subdirectories)")
    return 0
