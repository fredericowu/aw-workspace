"""``aw-workspace-cli logs <component>`` — port of the monolith's ``./aw logs``.

The workspace only exposes component logs over the SPA's WebSocket
(``/ws/logs/<key>``, see ``src/api/components.py``): the server sends a
backlog chunk on connect and then streams. This command speaks that same WS so
there's no second, CLI-only log route to keep in sync — it prints the backlog
and exits, or keeps streaming with ``-f``.
"""
from __future__ import annotations

import argparse
import asyncio

from src.api.workspace_api_key import HEADER_NAME
from src.cli import local_client
from src.cli.lifecycle import resolve_key

COMMAND = "logs"
DESCRIPTION = "Show (or follow) a component's logs"

# How long to wait for the backlog burst before deciding the server is done
# talking. The WS stays open by design, so a non-follow read needs an
# idle-based end condition rather than an EOF.
_IDLE_TIMEOUT = 2.0


def run(args: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="aw-workspace-cli logs", description=DESCRIPTION,
    )
    parser.add_argument("component", help="App slug or component key")
    parser.add_argument("-f", "--follow", action="store_true",
                        help="Keep streaming until interrupted")
    ns = parser.parse_args(args)

    key = resolve_key(ns.component)
    if key is None:
        return 1
    return asyncio.run(_stream(key, ns.follow))


def _ws_url(key: str) -> str:
    base = local_client.base_url()
    scheme = "wss" if base.startswith("https://") else "ws"
    host = base.split("://", 1)[-1].rstrip("/")
    return f"{scheme}://{host}/ws/logs/{key}"


async def _stream(key: str, follow: bool) -> int:
    try:
        import websockets
    except ImportError:
        print("error: the 'websockets' package is required for log streaming")
        return 1

    # The WS handshake carries the workspace API key as a header — the same
    # credential every other CLI call uses (authorize_ws accepts it).
    headers = {HEADER_NAME: local_client._workspace_api_key()}
    try:
        async with websockets.connect(_ws_url(key), additional_headers=headers) as ws:
            while True:
                try:
                    chunk = await asyncio.wait_for(
                        ws.recv(), timeout=None if follow else _IDLE_TIMEOUT)
                except asyncio.TimeoutError:
                    return 0
                if isinstance(chunk, bytes):
                    chunk = chunk.decode("utf-8", errors="replace")
                print(chunk, end="")
    except KeyboardInterrupt:
        return 0
    except Exception as exc:  # noqa: BLE001 — surface the reason, don't traceback
        print(f"error: could not stream logs for {key}: {exc}")
        return 1
