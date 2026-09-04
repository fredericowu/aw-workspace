"""``aw-workspace-cli restart core`` — reload the core process on
already-pushed code, dispatched from OUTSIDE the workspace container over
the aw-remote-host link this workspace already has (the same link
``aw-workspace-cli remote-hosts exec`` uses).

This is deliberately NOT ``aw-workspace-cli update workspace``
(``src/cli/commands/update.py``): that action pulls ``:latest`` and syncs
the image's baked repo over the host source tree, and stays gated behind a
central-identity JWT only a human can mint. This command only restarts the
container process so it picks up code that a ``git push`` already put on
the host's bind-mounted checkout — the code is already there the moment it
lands (``/opt/aw-workspace`` is a host bind mount), only the running
process is stale. That needs no new privilege: anything that can read
``<AW_WORKSPACE_HOME>/.env`` can already run arbitrary shell on the linked
host via ``remote-hosts exec``. This packages that existing capability into
one idempotent, observable verb instead of a hand-rolled shell one-liner.

Why this can't just POST to itself and wait: the process serving that
response is the one about to die. There is no response to wait for, so
completion is observed from OUTSIDE — by polling ``/api/health`` (see
``src/api/boot_info.py``) for a changed ``boot_id`` — never by waiting on
the HTTP call that triggers the restart.
"""
from __future__ import annotations

import argparse
import json
import os
import time
import uuid

import httpx

from src.api.boot_info import compute_git_head
from src.apps.paths import workspace_home_path, workspace_root
from src.cli import local_client

DEFAULT_BACKEND_URL = "http://127.0.0.1:9025"
ENV_VARS = ("AW_BACKEND_URL", "AW_WORKSPACE", "AW_WORKSPACE_HOST_TOKEN")

# The container aw-remote-host's bootstrap/workspace/install.sh creates —
# see CONTAINER_NAME there and WorkspaceContainer in its internal/ops/ops.go
# (the same name the /link "restart" verb — REJECTED for this card, see the
# design — would target). Kept in exactly one constant, with an env
# override, because a second placement driver (docker/nomad instead of this
# BYOD podman host) will need to branch on this name.
DEFAULT_CONTAINER_NAME = "aw-remote-host-workspace"
CONTAINER_NAME_ENV = "AW_REMOTE_HOST_WORKSPACE_CONTAINER"

# Matches install.sh's own IMAGE default/override — recorded in the receipt
# only for attribution (see the module docstring in the design: a restart on
# this host has been observed to come back as a recreate from :latest,
# silently activating a pending image-baked ENV change). Best-effort only;
# never blocks the restart if it can't be read.
DEFAULT_IMAGE_REF = "ghcr.io/fredericowu/aw-workspace:latest"
IMAGE_REF_ENV = "AW_WORKSPACE_IMAGE"

PREFLIGHT_TIMEOUT_S = 15.0
DEFAULT_WAIT_DEADLINE_S = 180.0
POLL_INTERVAL_S = 3.0


def _receipts_dir() -> str:
    d = os.path.join(workspace_root(), ".tmp", "core-restart")
    os.makedirs(d, exist_ok=True)
    return d


def _read_env_file_value(key: str) -> str | None:
    """Mirrors ``local_client._read_env_value`` / the remote-host-cli app's
    own ``.env`` fallback reader — same file, same three keys
    (``AW_BACKEND_URL``/``AW_WORKSPACE``/``AW_WORKSPACE_HOST_TOKEN``), kept
    here rather than imported from that app because core code must not hard
    -depend on an optional installed app's package."""
    path = os.path.join(workspace_home_path(), ".env")
    prefix = f"{key}="
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line.startswith(prefix):
                    return line[len(prefix):].strip() or None
    except FileNotFoundError:
        return None
    return None


def _env(key: str, default: str = "") -> str:
    return os.environ.get(key) or _read_env_file_value(key) or default


class NotConfigured(RuntimeError):
    pass


class RemoteHostError(RuntimeError):
    pass


def _resolve_config() -> tuple[str, str, str]:
    backend_url = _env("AW_BACKEND_URL", DEFAULT_BACKEND_URL).rstrip("/")
    workspace = _env("AW_WORKSPACE")
    token = _env("AW_WORKSPACE_HOST_TOKEN")
    if not workspace or not token:
        raise NotConfigured(
            "AW_BACKEND_URL, AW_WORKSPACE and AW_WORKSPACE_HOST_TOKEN must all be "
            "resolvable (env or <AW_WORKSPACE_HOME>/.env) — this only works once "
            "this workspace has completed the aw-remote-host /link handshake."
        )
    return backend_url, workspace, token


def _exec_start(backend_url: str, workspace: str, token: str, command: str,
                 timeout_s: float | None = None) -> dict:
    url = f"{backend_url}/api/workspaces/{workspace}/remote-host/exec"
    body: dict = {"command": command}
    if timeout_s is not None:
        body["timeout_s"] = timeout_s
    try:
        resp = httpx.request(
            "POST", url, json=body,
            headers={"Authorization": f"Bearer {token}"}, timeout=30.0,
        )
    except httpx.HTTPError as e:
        raise RemoteHostError(f"could not reach aw-backend: {e}") from e
    return _parse(resp)


def _exec_wait(backend_url: str, workspace: str, token: str, job_id: str,
               timeout_s: float) -> dict:
    url = f"{backend_url}/api/workspaces/{workspace}/remote-host/exec/{job_id}/wait"
    try:
        resp = httpx.request(
            "POST", url, json={"timeout_s": timeout_s},
            headers={"Authorization": f"Bearer {token}"}, timeout=timeout_s + 15.0,
        )
    except httpx.HTTPError as e:
        raise RemoteHostError(f"could not reach aw-backend: {e}") from e
    return _parse(resp)


# exec_wait called immediately after exec_start has been observed, live, to
# race a job-registration window on the host-side relay: the very next wait
# call sometimes lands before the host has finished registering the job it
# just started, and comes back "unknown job_id" for a job that in fact ran
# fine (same shape as the documented exec_status/exec_wait race — see
# native-skills/aw-workspace/SKILL.md). A short, bounded retry absorbs that
# window; it must NOT retry the actual restart dispatch (see
# _dispatch_restart's docstring for why), only this read-only wait.
_EXEC_WAIT_RETRY_DELAYS_S = (0.5, 1.0, 2.0)


def _exec_wait_with_retry(backend_url: str, workspace: str, token: str, job_id: str,
                           timeout_s: float) -> dict:
    last_error: RemoteHostError | None = None
    for attempt, delay in enumerate((0.0, *_EXEC_WAIT_RETRY_DELAYS_S)):
        if delay:
            time.sleep(delay)
        try:
            return _exec_wait(backend_url, workspace, token, job_id, timeout_s)
        except RemoteHostError as e:
            if "unknown job_id" not in str(e):
                raise
            last_error = e
    raise last_error


def _parse(resp: httpx.Response) -> dict:
    try:
        data = resp.json()
    except ValueError:
        data = {}
    if resp.status_code >= 400:
        raise RemoteHostError(
            (isinstance(data, dict) and (data.get("error") or data.get("detail")))
            or f"HTTP {resp.status_code}"
        )
    return data if isinstance(data, dict) else {}


def _preflight(backend_url: str, workspace: str, token: str, container_name: str,
               image_ref: str) -> tuple[bool, list[str], str]:
    """Synchronously confirm ``container_name`` exists on the linked host,
    and best-effort capture the currently-pulled ``:latest`` image digest —
    ONE round trip, since both are read-only/idempotent (unlike the restart
    itself, a double-execution here is harmless). Returns
    ``(container_exists, known_container_names, image_digest_or_empty)``.
    """
    marker = "---AW-CORE-RESTART-DIGEST---"
    command = (
        f"podman ps -a --format '{{{{.Names}}}}'; echo '{marker}'; "
        f"podman image inspect {image_ref} --format '{{{{.Id}}}}' 2>/dev/null || true"
    )
    started = _exec_start(backend_url, workspace, token, command, timeout_s=PREFLIGHT_TIMEOUT_S)
    job_id = started.get("job_id")
    if not job_id:
        raise RemoteHostError(f"exec_start returned no job_id: {started}")
    result = _exec_wait_with_retry(backend_url, workspace, token, job_id, timeout_s=PREFLIGHT_TIMEOUT_S)
    stdout = result.get("stdout", "")
    if marker in stdout:
        names_part, _, digest_part = stdout.partition(marker)
    else:
        names_part, digest_part = stdout, ""
    names = [n.strip() for n in names_part.splitlines() if n.strip()]
    digest = digest_part.strip()
    return container_name in names, names, digest


def _dispatch_restart(backend_url: str, workspace: str, token: str,
                       request_id: str, container_name: str) -> None:
    """Fire-and-forget the actual restart — exec_start only, never
    exec_run/exec_wait: those are documented-flaky (a job that ran fine can
    still report an "unknown job_id"), and waiting on the very channel the
    restart is about to kill is a race, not a safety measure.

    The host-side script is idempotent, guarded by a sentinel file keyed on
    ``request_id`` — exec has been proven to execute the same command TWICE
    during a link reconnect, and a double ``podman restart`` would kill the
    freshly-booted process seconds after boot. The sentinel and log live at
    a plain host path (``/tmp``), never under ``/opt/aw-workspace`` — that
    path is a bind mount of the HOST's own dir, invisible to a script
    running ON the host itself.
    """
    sentinel, log = _host_side_paths(request_id)
    script = _build_restart_script(sentinel, log, container_name)
    _exec_start(backend_url, workspace, token, script)


def _host_side_paths(request_id: str) -> tuple[str, str]:
    """Plain host paths — never under ``/opt/aw-workspace``, which is a bind
    mount of the HOST's own dir and therefore invisible to a script running
    ON the host itself."""
    return (
        f"/tmp/aw-core-restart-{request_id}.sentinel",
        f"/tmp/aw-core-restart-{request_id}.log",
    )


def _build_restart_script(sentinel: str, log: str, container_name: str) -> str:
    """The idempotent, sentinel-guarded host-side restart script. Pure
    string-building, split out from :func:`_dispatch_restart` so its
    idempotency can be exercised directly with a real ``sh -c`` in tests,
    without touching the network."""
    return (
        f"[ -e {sentinel} ] && exit 0; touch {sentinel}; "
        f"{{ podman restart {container_name}; echo EXIT=$?; }} >> {log} 2>&1"
    )


def _write_receipt(request_id: str, expected_head: str, boot_id_before: str,
                    image_digest: str, container_name: str) -> str:
    path = os.path.join(_receipts_dir(), f"{request_id}.json")
    payload = {
        "request_id": request_id,
        "requested_at": int(time.time()),
        "requested_by": os.environ.get("USER") or os.environ.get("AW_AGENT_SLUG") or "unknown",
        "expected_head": expected_head,
        "boot_id_before": boot_id_before,
        "image_digest_before": image_digest,
        "container_name": container_name,
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
        f.write("\n")
    return path


def _poll_health() -> dict:
    status, body = local_client.request("GET", "/api/health", timeout=10.0)
    if status != 200 or not isinstance(body, dict):
        return {}
    return body


def _wait_for_restart(expected_head: str, boot_id_before: str, deadline_s: float) -> int:
    """Poll ``/api/health`` until ``boot_id`` changes and ``git_head``
    matches. Disposable: all durable state is the receipt plus
    ``/api/health`` itself, so killing this loop loses nothing but the exit
    code. Three distinguishable outcomes, three distinct exit codes."""
    deadline = time.monotonic() + deadline_s
    last_health: dict = {}
    while time.monotonic() < deadline:
        last_health = _poll_health()
        new_boot_id = last_health.get("boot_id", "")
        if new_boot_id and new_boot_id != boot_id_before:
            if last_health.get("git_head") == expected_head:
                print(f"restart core: succeeded — boot_id {boot_id_before!r} -> "
                      f"{new_boot_id!r}, git_head {expected_head!r}")
                return 0
            print(f"restart core: came back on the WRONG code — expected "
                  f"git_head {expected_head!r}, got {last_health.get('git_head')!r}")
            return 2
        time.sleep(POLL_INTERVAL_S)
    print(f"restart core: the restart never happened — boot_id is still "
          f"{boot_id_before!r} after {deadline_s:.0f}s")
    return 1


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="aw-workspace-cli restart core",
        description="Restart the aw-workspace core process on already-pushed code",
    )
    parser.add_argument("--wait", action="store_true",
                         help="Block and poll /api/health until the restart is confirmed")
    parser.add_argument("--timeout", type=float, default=DEFAULT_WAIT_DEADLINE_S,
                         help=f"--wait deadline in seconds (default {DEFAULT_WAIT_DEADLINE_S:.0f})")
    return parser


def run(args: list[str]) -> int:
    ns = _build_parser().parse_args(args)

    try:
        backend_url, workspace, token = _resolve_config()
    except NotConfigured as e:
        print(f"error: {e}")
        return 1

    container_name = _env(CONTAINER_NAME_ENV, DEFAULT_CONTAINER_NAME)
    image_ref = _env(IMAGE_REF_ENV, DEFAULT_IMAGE_REF)

    expected_head = compute_git_head()
    health_before = _poll_health()
    boot_id_before = health_before.get("boot_id", "")

    try:
        exists, known_names, image_digest = _preflight(
            backend_url, workspace, token, container_name, image_ref,
        )
    except RemoteHostError as e:
        print(f"error: could not verify the target container on the linked host: {e}")
        return 1

    if not exists:
        print(f"error: no container named {container_name!r} on the linked host — "
              f"refusing to restart the wrong one. Known containers: "
              f"{', '.join(known_names) or '(none)'}")
        return 1

    request_id = uuid.uuid4().hex[:12]
    receipt_path = _write_receipt(
        request_id, expected_head, boot_id_before, image_digest, container_name,
    )

    try:
        _dispatch_restart(backend_url, workspace, token, request_id, container_name)
    except RemoteHostError as e:
        print(f"error: could not dispatch the restart: {e}")
        return 1

    print(f"restart core: dispatched (request_id={request_id})")
    print(f"  expected_head:   {expected_head}")
    print(f"  boot_id_before:  {boot_id_before!r}")
    print(f"  image_digest:    {image_digest or '(unavailable)'}")
    print(f"  receipt:         {receipt_path}")
    print("  WARNING: this kills every live terminal/PTY in the workspace container.")
    print(f"  poll:            aw-workspace-cli restart core --wait "
          f"# or: curl {local_client.base_url()}/api/health")

    if not ns.wait:
        return 0

    return _wait_for_restart(expected_head, boot_id_before, ns.timeout)
