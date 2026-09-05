"""The VPN **dialer** — invokes the ``aw-remote-host`` CLI through
aw-backend's exec bridge (``POST .../remote-host/exec`` + ``.../wait``) to
bring an external WireGuard tunnel up/down and route a container's egress
through it.

This is the other half of ``src/vpn/profiles.py`` (the config half). Per
``vpn-profiles-in-general.md`` §2.7 the dialer runs INSIDE the aw-remote-host
container — a Go binary this process cannot import — so this module's whole
job is to shell out to it correctly and safely, never to dial anything itself.

Mirrors ONLY the two exec verbs this needs from
``repos/aw-app-remote-host-cli/remote_host_cli_app/client.py:168-189``
(``exec_start``, ``exec_wait``) — no dependency on that app, no
reimplementation of its file-transfer/firewall surface. ``AW_BACKEND_URL`` /
``AW_WORKSPACE`` / ``AW_WORKSPACE_HOST_TOKEN`` already live in THIS process's
``os.environ`` — that app's own ``plugin.py`` docstring says core is the only
place they actually live there, published for every OTHER process sharing
this workspace's filesystem to read back from ``<home>/.env``.

Constraint that shapes everything below: **a private key must never transit
aw-backend**, which is the multi-tenant control plane and records exec job
command strings. So every dial writes the structured WireGuard fields
(``profiles.wireguard_dial_fields``) to a 0600 file under the workspace tree
first, and the exec command carries only that file's PATH — translated to
the aw-remote-host side's view of the same bind mount, discovered at runtime
rather than hardcoded (this deployment's paths have already changed once
this week).
"""
from __future__ import annotations

import json
import logging
import os
import socket
import time

import httpx

from src.apps import paths
from src.vpn.profiles import VpnProfileError, VpnProfileNotFound, VpnProfiles

log = logging.getLogger(__name__)

DEFAULT_BACKEND_URL = "http://127.0.0.1:9025"
EXEC_TIMEOUT_S = 30.0

# Process-level cache for the (expensive, one podman inspect) container
# discovery below — safe for the life of a worker process: if the container
# this process itself runs in gets recreated, the process is gone too.
_CACHE: dict = {}


class DialerError(RuntimeError):
    """The exec-bridge/CLI round trip itself failed — surfaced as a 502."""


class VpnRefused(RuntimeError):
    """The host CLI declined and touched nothing.

    ``sentence`` is the host's own refusal text, carried verbatim — composing
    a better one about a machine this process cannot see is exactly the
    failure mode this class exists to avoid.
    """

    def __init__(self, sentence: str) -> None:
        super().__init__(sentence)
        self.sentence = sentence


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise DialerError(
            f"{name} is not set in this process's environment — the dialer "
            f"only works inside an aw-workspace container that has completed "
            f"the aw-remote-host /link handshake."
        )
    return value


class _ExecClient:
    """The two exec verbs this module needs, mirroring
    ``remote_host_cli_app/client.py``'s route shapes exactly — not imported
    from there because core must not depend on an optional app's package."""

    def __init__(self) -> None:
        self._backend_url = (os.environ.get("AW_BACKEND_URL") or DEFAULT_BACKEND_URL).rstrip("/")
        self._workspace = _env("AW_WORKSPACE")
        self._token = _env("AW_WORKSPACE_HOST_TOKEN")

    def _base(self) -> str:
        return f"{self._backend_url}/api/workspaces/{self._workspace}/remote-host"

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self._token}"}

    def run(self, command: str, timeout_s: float = EXEC_TIMEOUT_S) -> dict:
        """``exec_start`` then ``exec_wait``, in one call. Never put key
        material in ``command`` — only ever a path (see the module docstring).
        """
        try:
            started = httpx.post(
                f"{self._base()}/exec", json={"command": command, "timeout_s": timeout_s},
                headers=self._headers(), timeout=15.0,
            )
        except httpx.HTTPError as exc:
            raise DialerError(f"could not reach aw-backend's exec bridge: {exc}") from exc
        if started.status_code >= 400:
            raise DialerError(f"exec_start failed: HTTP {started.status_code} {started.text}")
        job_id = started.json().get("job_id")
        if not job_id:
            raise DialerError(f"exec_start returned no job_id: {started.text}")

        try:
            waited = httpx.post(
                f"{self._base()}/exec/{job_id}/wait", json={"timeout_s": timeout_s},
                headers=self._headers(), timeout=timeout_s + 15.0,
            )
        except httpx.HTTPError as exc:
            raise DialerError(f"could not reach aw-backend's exec bridge: {exc}") from exc
        if waited.status_code >= 400:
            raise DialerError(f"exec_wait failed: HTTP {waited.status_code} {waited.text}")
        data = waited.json()
        if data.get("status") != "exited":
            raise DialerError(
                f"host command did not finish cleanly (status={data.get('status')!r}): "
                f"{command!r}"
            )
        return data


def _own_hostname() -> str:
    """Podman/Docker both default a container's hostname to its own short ID
    when none is set — this container's own is exactly that."""
    return socket.gethostname().strip()


def _discover_workspace_container(exec_client: _ExecClient) -> dict:
    """Resolve THIS process's own container as the aw-remote-host side sees
    it — never hardcoded (a stale name risks moving a different workload's
    egress). Asking the aw-remote-host side to ``podman inspect`` OUR OWN
    hostname resolves it unambiguously, no IP or name-pattern guessing
    needed — then the returned bind mounts are checked against
    ``paths.workspace_root()`` as a second, independent confirmation this is
    really the right container before anything is trusted from it.
    """
    if "container" in _CACHE:
        return _CACHE["container"]

    own_id = _own_hostname()
    result = exec_client.run(f"podman inspect {own_id}")
    stdout = (result.get("stdout") or "").strip()
    try:
        parsed = json.loads(stdout) if stdout else None
    except json.JSONDecodeError as exc:
        raise DialerError(
            f"could not resolve this workspace's own container on the "
            f"aw-remote-host side (podman inspect {own_id} did not return "
            f"JSON): {result.get('stderr') or stdout}"
        ) from exc
    if not parsed:
        raise DialerError(
            f"podman inspect {own_id} on the aw-remote-host side returned no "
            f"container — refusing to guess a name, a wrong guess moves a "
            f"different workload's egress"
        )
    info = parsed[0]
    name = (info.get("Name") or "").lstrip("/")
    mounts = info.get("Mounts") or []
    root = paths.workspace_root()
    workspace_mount = next((m for m in mounts if m.get("Destination") == root), None)
    if not name or not workspace_mount or not workspace_mount.get("Source"):
        raise DialerError(
            f"podman inspect {own_id} on the aw-remote-host side did not carry "
            f"a bind mount at {root!r} — cannot confirm this is really this "
            f"workspace's own container"
        )
    info = {"name": name, "host_root": workspace_mount["Source"]}
    _CACHE["container"] = info
    return info


def _translate_path(local_path: str, container_info: dict) -> str:
    """This container's own path -> the same file's path as seen from the
    aw-remote-host side, via the bind mount confirmed during discovery above
    — never a hardcoded prefix pair, because this deployment's paths have
    already changed once this week."""
    root = paths.workspace_root()
    if local_path != root and not local_path.startswith(root + os.sep):
        raise DialerError(
            f"{local_path!r} is outside the workspace tree {root!r} — "
            f"refusing to translate a path the aw-remote-host side would not "
            f"actually see"
        )
    rel = os.path.relpath(local_path, root)
    host_root = container_info["host_root"]
    return host_root if rel == "." else os.path.join(host_root, rel)


def _dial_dir() -> str:
    d = os.path.join(paths.workspace_home(), "data", "vpn", "dial")
    os.makedirs(d, mode=0o700, exist_ok=True)
    return d


def _write_dial_profile(fields: dict) -> str:
    """Structured WireGuard fields -> a 0600 file under the workspace tree.
    The ONLY thing that ever reaches the exec command string is this file's
    (translated) PATH — never its content."""
    path = os.path.join(_dial_dir(), f"{fields['iface']}.json")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(fields, f)
    return path


def _remove_dial_profile(iface: str) -> None:
    """Best-effort cleanup once a tunnel is torn down — the file holds key
    material and has no reason to linger past its own disconnect."""
    try:
        os.remove(os.path.join(_dial_dir(), f"{iface}.json"))
    except FileNotFoundError:
        pass


def _dial_state_path() -> str:
    return os.path.join(paths.workspace_home(), "data", "vpn", "dial_state.json")


def _write_dial_state(state: dict) -> None:
    path = _dial_state_path()
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    tmp = f"{path}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, sort_keys=True)
    os.replace(tmp, path)


def read_dial_state() -> dict:
    """What THIS process last asked the aw-remote-host CLI to do, and
    whether it reported success — never a live re-check of the tunnel. There
    is no query verb in the CLI contract (external-up/down/route/unroute are
    all one-shot actions), so ``GET /api/vpn/status`` must report exactly
    this and nothing more confident: a dead-man revert or a host restart
    would not be reflected here.
    """
    try:
        with open(_dial_state_path(), encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _run_cli(exec_client: _ExecClient, cli_args: list[str]) -> dict:
    """Run one ``aw-remote-host`` CLI verb, parse its single JSON stdout
    line, and turn a refusal into ``VpnRefused`` so callers repeat the host's
    own sentence verbatim instead of composing a new one."""
    command = "aw-remote-host " + " ".join(cli_args)
    result = exec_client.run(command)
    stdout = (result.get("stdout") or "").strip()
    try:
        payload = json.loads(stdout.splitlines()[-1]) if stdout else {}
    except (json.JSONDecodeError, IndexError):
        payload = {}
    if payload.get("refused"):
        raise VpnRefused(payload.get("refusal") or "the host declined and touched nothing")
    if result.get("exit_code") != 0:
        raise DialerError(
            f"{' '.join(cli_args)} exited {result.get('exit_code')}: "
            f"{result.get('stderr') or stdout}"
        )
    return payload


def connect(profiles: VpnProfiles, name: str, container: str | None = None) -> dict:
    """``external-up``, then ``external-route`` — two steps, not one:
    ``ops_vpn_external_route.go``'s header explains that conflating dial and
    route makes a run log unreadable. ``container`` defaults to this
    workspace's own container (Frederico's stated first test: connect, and
    the workspace itself exits through it)."""
    exec_client = _ExecClient()
    try:
        fields = profiles.wireguard_dial_fields(name)
        container_info = _discover_workspace_container(exec_client)
        local_path = _write_dial_profile(fields)
        host_path = _translate_path(local_path, container_info)

        up_result = _run_cli(exec_client, [
            "vpn", "external-up", "--profile-json", host_path,
            "--iface", fields["iface"], "--json",
        ])

        route_container = container or container_info["name"]
        route_result = _run_cli(exec_client, [
            "vpn", "external-route", "--container", route_container, "--json",
        ])
    except (VpnRefused, DialerError, VpnProfileError, VpnProfileNotFound) as exc:
        _write_dial_state({"action": "connect", "ok": False, "profile": name, "at": _now(),
                           "error": str(exc)})
        raise

    _write_dial_state({
        "action": "connect", "ok": True, "profile": name, "container": route_container,
        "iface": fields["iface"], "at": _now(),
    })
    return {"up": up_result, "route": route_result}


def disconnect() -> dict:
    """``external-unroute``, then ``external-down`` — the reverse order of
    ``connect``, for the same reason: unroute first so nothing is still
    pointed at a tunnel that is about to disappear."""
    exec_client = _ExecClient()
    state = read_dial_state()
    iface = state.get("iface") if state.get("action") == "connect" and state.get("ok") else None

    try:
        unroute_result = _run_cli(exec_client, ["vpn", "external-unroute", "--json"])
        down_args = ["vpn", "external-down", "--json"]
        if iface:
            down_args += ["--iface", iface]
        down_result = _run_cli(exec_client, down_args)
    except (VpnRefused, DialerError) as exc:
        _write_dial_state({"action": "disconnect", "ok": False, "at": _now(), "error": str(exc)})
        raise

    if iface:
        _remove_dial_profile(iface)
    _write_dial_state({"action": "disconnect", "ok": True, "at": _now()})
    return {"unroute": unroute_result, "down": down_result}
