"""aw-workspace-cli update <workspace|remote-host> — trigger the same lifecycle action
aw-console's Workspace > Manage > Update button calls, straight from a
terminal inside the workspace.

Unlike ``marketplace`` (this workspace's own local API), the *update* action
lives on the cloud control plane (aw-backend ``POST /api/workspaces/{slug}
/update`` and ``/remote-host/update`` — see ``routes/workspaces.py``), gated
by a real central-identity JWT (one user, many workspaces) rather than this
workspace's local-CLI secret. There's no browser session to borrow that from
in a terminal, so the token is supplied explicitly — same shape as ``gh
auth login`` handing a CLI a token that only a human can mint.

``AW_BACKEND_URL``/``AW_WORKSPACE`` resolve through the same ``.env``
fallback ``core_restart.py`` uses (env first, then
``<AW_WORKSPACE_HOME>/.env``) — those two are readable from a sibling
agent-runner container exactly like the host token is, so the ONLY thing
this command should ever fail on is the actual gate: the missing
central-identity JWT no agent can mint. If what you actually want is "I
pushed a core fix, make it live", that's ``aw-workspace-cli restart core``,
not this — this command rewrites the host source tree from the image and
needs a human to authorize it.

See card 3d15bf3b-9510-816a-bff8-fc6698619fa4 and its Architect design:
routing this through aw-backend with the workspace's own host token
accepted as an alternative credential (widening it from the exec channel
onto control-plane lifecycle routes that also cover uninstall/reinstall)
was considered and explicitly REJECTED there — cross-repo (aw-backend
deploys are manual, so a route change there isn't live the moment this
lands), and it buys nothing today since the exec channel already grants
strictly more power. The host token stays scoped to
``remote-host/exec`` only; this command keeps the human JWT gate.
"""
from __future__ import annotations

import argparse
import os

import httpx

from src.cli.core_restart import _env

COMMAND = "update"
DESCRIPTION = "Update the workspace code or its remote host (aw-console Manage > Update)"

_TARGET_PATHS = {
    "workspace": "update",
    "remote-host": "remote-host/update",
}


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="aw-workspace-cli update",
        description=DESCRIPTION,
        epilog=(
            "restart vs. update: `restart core` reloads the process on code "
            "already on disk — what you want after a plain `git push`, since "
            "/opt/aw-workspace is a host bind mount and the commit is already "
            "there. `update workspace` instead pulls the :latest container "
            "image and syncs its repo copy over the host dir — reach for it "
            "only when you actually need a new image."
        ),
    )
    parser.add_argument("target", choices=sorted(_TARGET_PATHS), help="What to update")
    parser.add_argument(
        "--token", help="Central-identity bearer token (defaults to $AW_ID_TOKEN)",
    )
    return parser


def run(args: list[str]) -> int:
    ns = _build_parser().parse_args(args)

    backend_url = _env("AW_BACKEND_URL").rstrip("/")
    workspace = _env("AW_WORKSPACE")
    # AW_ID_TOKEN deliberately does NOT go through the .env fallback: unlike
    # the host token, nothing ever publishes it there — it's the one real
    # gate, and it must stay something only a human hands the CLI explicitly.
    token = ns.token or os.environ.get("AW_ID_TOKEN", "")

    if not backend_url or not workspace:
        print("error: AW_BACKEND_URL and AW_WORKSPACE could not be resolved from the "
              "environment or <AW_WORKSPACE_HOME>/.env — same env this workspace "
              "already uses to reach aw-backend.")
        return 1
    if not token:
        print("error: no identity token. Pass --token or set AW_ID_TOKEN to the "
              "aw_id_jwt value from an aw-console login (Workspace > Manage > Update "
              "does this via your browser session; a CLI has none, so it must be given "
              "one). If you just want to pick up a core code change you already pushed, "
              "use `aw-workspace-cli restart core` instead — it needs no identity token.")
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
