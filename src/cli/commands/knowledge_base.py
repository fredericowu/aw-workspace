"""aw-workspace-cli knowledge-base — delegates to the kb app's own CLI.

Mirrors the monolith's ``./aw knowledge-base`` command, but every flag,
parser, and behavior is defined in the ``kb`` app itself
(``kb_app.kb_ops``, repo tekflox/aw-app-kb) — this file does not
duplicate any of that. It just execs ``python3 -m kb_app.kb_ops <args>``
inside the running ``aw-app-kb`` container (via the Docker-API socket
ContainerSupervisor also uses — see src/apps/containers.py) and streams
its stdout/stderr straight through, forwarding local stdin for
``--update`` (which reads file content from stdin).

Usage:
    aw-workspace-cli knowledge-base --build
    aw-workspace-cli knowledge-base --search "how to configure redir"
    aw-workspace-cli knowledge-base --update <path>   # content from stdin
    aw-workspace-cli knowledge-base --map-path <path>

Requires the Knowledge Base app installed and running (Apps > Knowledge
Base, or `aw-workspace-cli marketplace install kb`).
"""
from __future__ import annotations

import os
import sys

COMMAND = "knowledge-base"
DESCRIPTION = "Manage the knowledge base (build, search, map) — runs inside the kb app"

APP_CONTAINER = "aw-app-kb"


def _demux_stream(recv) -> None:
    """Strip Docker's exec stream-multiplexing frame headers and write payload
    straight to stdout. Only needed on the raw ``socket=True`` path (used for
    --update, to also write stdin) — ``api.exec_start(stream=True)`` without
    ``socket=True`` already demuxes this for the non-stdin path below.

    Frame format (tty=False): 8-byte header — 1 byte stream type, 3 reserved,
    4-byte big-endian payload length — then that many payload bytes. Buffered
    since a single ``recv()`` call has no reason to land on a frame boundary.
    """
    import struct

    buf = b""
    while True:
        chunk = recv(4096)
        if not chunk:
            break
        buf += chunk
        while len(buf) >= 8:
            size = struct.unpack(">I", buf[4:8])[0]
            if len(buf) < 8 + size:
                break
            sys.stdout.buffer.write(buf[8:8 + size])
            buf = buf[8 + size:]
    sys.stdout.flush()


def run(args: list[str]) -> int:
    args = list(args or [])

    socket = os.environ.get("AW_CONTAINER_SOCKET")
    if not socket:
        print("aw-workspace-cli knowledge-base: no container engine available "
              "(AW_CONTAINER_SOCKET not set) — Tier-2 apps aren't supported here.")
        return 1

    import docker
    import docker.errors

    client = docker.DockerClient(base_url="unix://" + socket)
    try:
        container = client.containers.get(APP_CONTAINER)
    except docker.errors.NotFound:
        print(f"aw-workspace-cli knowledge-base: '{APP_CONTAINER}' isn't running — "
              f"install/start the Knowledge Base app first "
              f"(aw-workspace-cli marketplace install kb).")
        return 1

    # --update reads file content from stdin (see kb_ops.py's _update) — only
    # attach an interactive stdin pipe for that flag, other commands don't
    # read stdin and forwarding it unconditionally would hang exec_start()
    # waiting for EOF on a terminal that never sends one.
    wants_stdin = "--update" in args
    cmd = ["python3", "-m", "kb_app.kb_ops", *args]

    api = client.api
    exec_id = api.exec_create(
        container.id, cmd, stdin=wants_stdin, stdout=True, stderr=True, tty=False,
    )["Id"]

    if wants_stdin:
        sock = api.exec_start(exec_id, stream=False, socket=True)
        try:
            raw = sock._sock if hasattr(sock, "_sock") else sock
            raw.sendall(sys.stdin.buffer.read())
            raw.shutdown(1)  # SHUT_WR — signal EOF, still read the reply below
            _demux_stream(raw.recv)
        finally:
            sock.close()
        sys.stdout.flush()
    else:
        stream = api.exec_start(exec_id, stream=True)
        for chunk in stream:
            sys.stdout.buffer.write(chunk)
            sys.stdout.flush()

    return api.exec_inspect(exec_id)["ExitCode"] or 0
