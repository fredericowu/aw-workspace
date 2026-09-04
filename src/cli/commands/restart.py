"""``aw-workspace-cli restart <component>`` — port of the monolith's ``./aw restart``.

Takes a bare app slug (``kb``) or a full component key
(``docker:aw-kb``); see ``src/cli/lifecycle.py``.

``restart core`` is a RESERVED target, not a component — it restarts the
aw-workspace core process itself, from OUTSIDE the container this CLI's own
server may be running in, over the aw-remote-host link. There is no
``core`` entry in ``/api/components`` (the list ``lifecycle.act`` resolves
against) to POST a restart to, and even if there were, a process cannot
usefully POST its own restart and wait on the response — the response can
never arrive. See ``src/cli/core_restart.py``.

Card 3d15bf3b-9510-816a-bff8-fc6698619fa4's Architect design also
considered — and explicitly REJECTED — a second path here: an aw-backend
``POST /api/workspaces/{slug}/restart`` route authorized by this
workspace's own host token. Cross-repo (aw-backend deploys are manual, so
a route change there isn't live the moment this lands), and it would widen
a data-plane credential onto control-plane lifecycle routes that also
cover uninstall/reinstall — for no gain, since the exec channel `core`
already uses grants strictly more power. Do not re-add it here; see the
card's REJECTED section for the full reasoning.
"""
from __future__ import annotations

import argparse

from src.cli import core_restart
from src.cli.lifecycle import act, components

COMMAND = "restart"
DESCRIPTION = "Restart a workspace component (app container or managed service), or the core process itself"

CORE_TARGET = "core"


def run(args: list[str]) -> int:
    if args and args[0] == CORE_TARGET:
        _assert_no_component_collision()
        return core_restart.run(args[1:])

    parser = argparse.ArgumentParser(
        prog="aw-workspace-cli restart", description=DESCRIPTION,
    )
    parser.add_argument("component", help="App slug or component key")
    ns = parser.parse_args(args)
    return act("restart", ns.component)


def _assert_no_component_collision() -> None:
    """``core`` is intercepted above before ``lifecycle.resolve_key`` ever
    runs, so a real component slug/key that happened to equal ``core``
    would be silently unreachable via ``restart core`` instead of failing
    loudly. Fail loudly instead — this is a naming collision bug, not a
    runtime condition to handle gracefully."""
    for row in components():
        if row.get("component") == CORE_TARGET or row.get("key") == CORE_TARGET:
            raise AssertionError(
                f"a real component ({row!r}) resolves to {CORE_TARGET!r} — "
                "'restart core' is reserved for the core-process restart path "
                "and must never collide with a component slug/key"
            )
