"""./aw update <workspace|remote-host> — trigger the same lifecycle action
aw-console's Workspace > Manage > Update button calls, straight from a
terminal inside the workspace.

Unlike ``marketplace`` (this workspace's own local API), the *update* action
lives on the cloud control plane (aw-backend ``POST /api/workspaces/{slug}
/update`` and ``/remote-host/update`` — see ``routes/workspaces.py``), gated
by a real central-identity JWT (one user, many workspaces) rather than this
workspace's local-CLI secret. There's no browser session to borrow that from
in a terminal, so the token is supplied explicitly — same shape as ``gh
auth login`` handing a CLI a token that only a human can mint.
"""
from __future__ import annotations

import argparse
import os

import httpx

COMMAND = "update"
DESCRIPTION = "Update the workspace code or its remote host (aw-console Manage > Update)"

_TARGET_PATHS = {
    "workspace": "update",
    "remote-host": "remote-host/update",
}


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="./aw update", description=DESCRIPTION)
    parser.add_argument("target", choices=sorted(_TARGET_PATHS), help="What to update")
    parser.add_argument(
        "--token", help="Central-identity bearer token (defaults to $AW_ID_TOKEN)",
    )
    return parser


def run(args: list[str]) -> int:
    ns = _build_parser().parse_args(args)

    backend_url = os.environ.get("AW_BACKEND_URL", "").rstrip("/")
    workspace = os.environ.get("AW_WORKSPACE", "")
    token = ns.token or os.environ.get("AW_ID_TOKEN", "")

    if not backend_url or not workspace:
        print("error: AW_BACKEND_URL and AW_WORKSPACE must be set — same env this "
              "workspace already uses to reach aw-backend.")
        return 1
    if not token:
        print("error: no identity token. Pass --token or set AW_ID_TOKEN to the "
              "aw_id_jwt value from an aw-console login (Workspace > Manage > Update "
              "does this via your browser session; a CLI has none, so it must be given one).")
        return 1

    url = f"{backend_url}/api/workspaces/{workspace}/{_TARGET_PATHS[ns.target]}"
    try:
        resp = httpx.post(url, headers={"Authorization": f"Bearer {token}"}, timeout=120.0)
    except httpx.HTTPError as e:
        print(f"error: could not reach aw-backend: {e}")
        return 1

    try:
        body = resp.json()
    except ValueError:
        body = resp.text

    if resp.status_code >= 400:
        print(f"error: {ns.target} update failed ({resp.status_code}): {body}")
        return 1

    print(f"{ns.target}: update triggered — {body}")
    return 0
