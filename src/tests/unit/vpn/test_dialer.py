"""``src/vpn/dialer.py`` — the exec-bridge client and constraint (A) as a test.

The test that matters most here is ``test_the_exec_command_string_never_carries_the_private_key``:
the whole design of this module exists so a WireGuard private key never
transits aw-backend, which records exec job command strings. Every other
test exercises the surrounding mechanics (container discovery, path
translation, refusal handling, dial-state bookkeeping) that make that one
property hold up under a real connect/disconnect flow.
"""
from __future__ import annotations

import json
import os

import pytest

from src.apps import paths
from src.vpn import dialer
from src.vpn.profiles import VpnProfileNotFound, VpnProfiles

WG_OK = """
[Interface]
PrivateKey = aGVsbG8gd29ybGQgdGhpcyBpcyBub3QgYSBrZXk9
Address = 10.5.0.2/32

[Peer]
PublicKey = cHVibGljIGtleSB0aGF0IGlzIG5vdCByZWFsIGE9
AllowedIPs = 0.0.0.0/0
Endpoint = vpn.example.com:51820
""".strip()

PRIVATE_KEY = "aGVsbG8gd29ybGQgdGhpcyBpcyBub3QgYSBrZXk9"

FAKE_HOST_ROOT = "/srv/fake-remote-host-root"


@pytest.fixture(autouse=True)
def _clear_container_cache():
    dialer._CACHE.clear()
    yield
    dialer._CACHE.clear()


@pytest.fixture()
def env(tmp_path, monkeypatch):
    # workspace_root() and workspace_home() must agree on a common base so a
    # dial-profile path written under home is genuinely "inside" the tree
    # _translate_path checks against — same relationship as production
    # (AW_WORKSPACE_HOME defaults under the container dir).
    monkeypatch.setenv("AW_WORKSPACE_CONTAINER_DIR", str(tmp_path))
    monkeypatch.setenv("AW_WORKSPACE_HOME", str(tmp_path / ".aw-workspace"))
    monkeypatch.setenv("AW_BACKEND_URL", "http://backend.test")
    monkeypatch.setenv("AW_WORKSPACE", "acme")
    monkeypatch.setenv("AW_WORKSPACE_HOST_TOKEN", "awlk_test_token")
    from cryptography.fernet import Fernet
    monkeypatch.setenv("AW_WORKSPACE_SECRET_KEY", Fernet.generate_key().decode())
    return tmp_path


@pytest.fixture()
def profiles(env):
    return VpnProfiles()


class _FakeResponse:
    def __init__(self, status_code: int, payload: dict) -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = json.dumps(payload)

    def json(self) -> dict:
        return self._payload


def _podman_inspect_stdout(root: str, host_root: str, name: str = "/aw-remote-host-workspace") -> str:
    return json.dumps([{"Name": name, "Mounts": [{"Destination": root, "Source": host_root}]}])


def _install_exec_fake(monkeypatch, commands: list, stdout_for):
    """Fakes both exec verbs this module calls. ``commands`` records every
    command string sent to ``/exec`` (the object of the key-material test);
    ``stdout_for(command)`` decides what the "host" printed back."""
    jobs: dict[str, str] = {}
    counter = {"n": 0}

    def fake_post(url, json=None, headers=None, timeout=None):  # noqa: A002
        if url.endswith("/exec"):
            command = json["command"]
            commands.append(command)
            counter["n"] += 1
            job_id = f"job{counter['n']}"
            jobs[job_id] = stdout_for(command)
            return _FakeResponse(200, {"job_id": job_id, "pid": 1, "started": True})
        job_id = url.rsplit("/", 2)[-2]
        return _FakeResponse(200, {
            "job_id": job_id, "status": "exited", "exit_code": 0,
            "stdout": jobs.get(job_id, ""), "stderr": "",
        })

    monkeypatch.setattr(dialer.httpx, "post", fake_post)


def _happy_stdout_for(root: str, host_root: str):
    def stdout_for(command: str) -> str:
        if command.startswith("podman inspect"):
            return _podman_inspect_stdout(root, host_root)
        return json.dumps({"ok": True})
    return stdout_for


# --- connect ------------------------------------------------------------------


def test_connect_happy_path_translates_the_path_and_routes_the_own_container(monkeypatch, env, profiles):
    profiles.save_config("wg0", "wireguard", WG_OK)
    root = paths.workspace_root()
    commands: list[str] = []
    _install_exec_fake(monkeypatch, commands, _happy_stdout_for(root, FAKE_HOST_ROOT))

    result = dialer.connect(profiles, "wg0")

    assert result == {"up": {"ok": True}, "route": {"ok": True}}
    up_cmd = next(c for c in commands if "external-up" in c)
    assert FAKE_HOST_ROOT in up_cmd, "the exec command must carry the TRANSLATED (host-side) path"
    assert "--iface wg0" in up_cmd
    route_cmd = next(c for c in commands if "external-route" in c)
    assert "--container aw-remote-host-workspace" in route_cmd, "defaults to this workspace's own container"

    state = dialer.read_dial_state()
    assert state == {
        "action": "connect", "ok": True, "profile": "wg0",
        "container": "aw-remote-host-workspace", "iface": "wg0", "at": state["at"],
    }


def test_connect_lets_an_explicit_container_override_the_default(monkeypatch, env, profiles):
    profiles.save_config("wg0", "wireguard", WG_OK)
    root = paths.workspace_root()
    commands: list[str] = []
    _install_exec_fake(monkeypatch, commands, _happy_stdout_for(root, FAKE_HOST_ROOT))

    dialer.connect(profiles, "wg0", container="some-other-container")

    route_cmd = next(c for c in commands if "external-route" in c)
    assert "--container some-other-container" in route_cmd


def test_the_exec_command_string_never_carries_the_private_key(monkeypatch, env, profiles):
    """Constraint (A) as a test: a WireGuard private key must never transit
    aw-backend, which records exec job command strings."""
    profiles.save_config("wg0", "wireguard", WG_OK)
    root = paths.workspace_root()
    commands: list[str] = []
    _install_exec_fake(monkeypatch, commands, _happy_stdout_for(root, FAKE_HOST_ROOT))

    dialer.connect(profiles, "wg0")

    assert commands, "the fake never observed any exec call"
    for command in commands:
        assert PRIVATE_KEY not in command


def test_connect_surfaces_a_host_refusal_verbatim(monkeypatch, env, profiles):
    profiles.save_config("wg0", "wireguard", WG_OK)
    root = paths.workspace_root()
    refusal = "table 200 is already owned by the GL.iNet hub"

    def stdout_for(command: str) -> str:
        if command.startswith("podman inspect"):
            return _podman_inspect_stdout(root, FAKE_HOST_ROOT)
        if "external-up" in command:
            return json.dumps({"refused": True, "refusal": refusal})
        return json.dumps({"ok": True})

    commands: list[str] = []
    _install_exec_fake(monkeypatch, commands, stdout_for)

    with pytest.raises(dialer.VpnRefused) as exc:
        dialer.connect(profiles, "wg0")

    assert exc.value.sentence == refusal, "the host's own sentence, not a composed one"
    state = dialer.read_dial_state()
    assert state["action"] == "connect" and state["ok"] is False


def test_connect_fails_clearly_when_the_container_cannot_be_confirmed(monkeypatch, env, profiles):
    """No bind mount at the workspace root -> refuse to guess a name rather
    than risk moving a different workload's egress."""
    profiles.save_config("wg0", "wireguard", WG_OK)

    def stdout_for(command: str) -> str:
        if command.startswith("podman inspect"):
            return json.dumps([{"Name": "/something-else", "Mounts": []}])
        return json.dumps({"ok": True})

    commands: list[str] = []
    _install_exec_fake(monkeypatch, commands, stdout_for)

    with pytest.raises(dialer.DialerError, match="bind mount"):
        dialer.connect(profiles, "wg0")


def test_connect_propagates_an_unknown_profile_without_touching_the_host(monkeypatch, env, profiles):
    commands: list[str] = []
    _install_exec_fake(monkeypatch, commands, lambda c: json.dumps({"ok": True}))

    with pytest.raises(VpnProfileNotFound):
        dialer.connect(profiles, "nope")

    assert commands == [], "a profile lookup failure must never reach the exec bridge"


# --- disconnect -----------------------------------------------------------


def test_disconnect_targets_the_iface_from_the_last_successful_connect(monkeypatch, env):
    dialer._write_dial_state({
        "action": "connect", "ok": True, "profile": "wg0",
        "container": "aw-remote-host-workspace", "iface": "wg0", "at": "then",
    })
    dial_path = dialer._write_dial_profile({"iface": "wg0", "type": "wireguard"})
    assert os.path.exists(dial_path)

    commands: list[str] = []
    _install_exec_fake(monkeypatch, commands, lambda c: json.dumps({"ok": True}))

    result = dialer.disconnect()

    assert result == {"unroute": {"ok": True}, "down": {"ok": True}}
    down_cmd = next(c for c in commands if "external-down" in c)
    assert "--iface wg0" in down_cmd
    assert not os.path.exists(dial_path), "the dial-profile file (key material) must not linger"
    state = dialer.read_dial_state()
    assert state["action"] == "disconnect" and state["ok"] is True


def test_disconnect_without_a_prior_connect_omits_iface(monkeypatch, env):
    commands: list[str] = []
    _install_exec_fake(monkeypatch, commands, lambda c: json.dumps({"ok": True}))

    dialer.disconnect()

    down_cmd = next(c for c in commands if "external-down" in c)
    assert "--iface" not in down_cmd


def test_disconnect_surfaces_a_host_refusal_verbatim(monkeypatch, env):
    refusal = "the dead-man switch is still armed"

    def stdout_for(command: str) -> str:
        if "external-unroute" in command:
            return json.dumps({"refused": True, "refusal": refusal})
        return json.dumps({"ok": True})

    commands: list[str] = []
    _install_exec_fake(monkeypatch, commands, stdout_for)

    with pytest.raises(dialer.VpnRefused) as exc:
        dialer.disconnect()
    assert exc.value.sentence == refusal


# --- read_dial_state / read-only ------------------------------------------


def test_read_dial_state_defaults_to_empty(env):
    assert dialer.read_dial_state() == {}


def test_exec_client_requires_workspace_env_vars(monkeypatch, env):
    monkeypatch.delenv("AW_WORKSPACE", raising=False)
    with pytest.raises(dialer.DialerError, match="AW_WORKSPACE"):
        dialer._ExecClient()


# --- configured / query_status / status ---------------------------------
#
# The dead-man's switch (internal/vpn/deadman.go) reverts a tunnel
# autonomously and without telling anyone — so read_dial_state() alone is
# not a safe answer to "is the VPN on". These tests pin the two failure
# modes that matter: a live "down" must override a recorded "connect, ok",
# and an unavailable query verb must yield "unknown", never a stale
# "connected" and never a fabricated "disconnected".


def test_configured_reflects_env_vars(monkeypatch, env):
    assert dialer.configured() is True
    monkeypatch.delenv("AW_WORKSPACE_HOST_TOKEN", raising=False)
    assert dialer.configured() is False


def test_query_status_is_unknown_when_not_configured(monkeypatch):
    monkeypatch.delenv("AW_WORKSPACE", raising=False)
    monkeypatch.delenv("AW_WORKSPACE_HOST_TOKEN", raising=False)
    assert dialer.query_status() == {"state": "unknown"}


def test_query_status_degrades_to_unknown_when_the_verb_is_not_recognized(monkeypatch, env):
    """The Go side has not shipped external-status yet (or any future CLI
    that doesn't recognize it) — a plain non-zero exit, same as any other
    unknown subcommand. Must degrade cleanly, not raise."""
    def fake_run(self, command, timeout_s=dialer.EXEC_TIMEOUT_S):
        return {"status": "exited", "exit_code": 2, "stdout": "", "stderr": "unknown command"}

    monkeypatch.setattr(dialer._ExecClient, "run", fake_run)

    assert dialer.query_status() == {"state": "unknown"}


def test_query_status_reports_connected_with_the_live_payload(monkeypatch, env):
    def stdout_for(command):
        assert "external-status" in command
        return json.dumps({
            "iface": "wg0", "up": True, "container": "aw-remote-host-workspace",
            "container_egress_ip": "203.0.113.9", "deadman_armed": True,
            "since": "2026-09-05T12:00:00Z",
        })

    commands: list[str] = []
    _install_exec_fake(monkeypatch, commands, stdout_for)

    result = dialer.query_status()

    assert result["state"] == "connected"
    assert result["up"] is True
    assert result["container_egress_ip"] == "203.0.113.9"


def test_query_status_takes_no_iface_flag(monkeypatch, env):
    """The Go contract for external-status specifies only the output shape,
    not an input flag — there is one external tunnel at a time."""
    commands: list[str] = []
    _install_exec_fake(monkeypatch, commands, lambda c: json.dumps({"up": True}))

    dialer.query_status()

    status_cmd = next(c for c in commands if "external-status" in c)
    assert status_cmd == "aw-remote-host vpn external-status --json"


def test_query_status_reports_disconnected_when_the_host_says_down(monkeypatch, env):
    _install_exec_fake(monkeypatch, [], lambda c: json.dumps({"iface": "wg0", "up": False}))
    assert dialer.query_status()["state"] == "disconnected"


def test_status_lets_a_live_down_override_a_recorded_connect(monkeypatch, env):
    """The exact scenario the dead-man's switch creates: the last thing this
    process asked for succeeded, but the tunnel has since reverted. The live
    measurement must win."""
    dialer._write_dial_state({
        "action": "connect", "ok": True, "profile": "wg0", "iface": "wg0",
        "container": "aw-remote-host-workspace", "at": "then",
    })
    _install_exec_fake(monkeypatch, [], lambda c: json.dumps({"iface": "wg0", "up": False}))

    result = dialer.status()

    assert result["state"] == "disconnected"
    assert result["connected"] is False
    assert result["active"] is None


def test_status_reports_connected_with_the_profile_name_at_the_top_level(monkeypatch, env):
    """The defect this pins: a live "up" tunnel must surface connected=True
    and the profile name directly — not nested, not defaulted to False."""
    dialer._write_dial_state({
        "action": "connect", "ok": True, "profile": "wg0", "iface": "wg0",
        "container": "aw-remote-host-workspace", "at": "then",
    })

    def stdout_for(command):
        return json.dumps({
            "iface": "wg0", "up": True, "container": "aw-remote-host-workspace",
            "container_egress_ip": "203.0.113.9", "deadman_armed": True,
            "since": "2026-09-05T12:00:00Z",
        })

    _install_exec_fake(monkeypatch, [], stdout_for)

    result = dialer.status()

    assert result["state"] == "connected"
    assert result["connected"] is True
    assert result["active"] == "wg0"
    assert result["container"] == "aw-remote-host-workspace"
    assert result["egress_ip"] == "203.0.113.9"
    assert result["deadman_armed"] is True


def test_status_never_sends_an_iface_flag_to_external_status(monkeypatch, env):
    """external-status takes no --iface argument in the Go contract — only
    the output shape was specified, so the Go side has no reason to accept
    one. Passing it anyway means the CLI exits non-zero on an unrecognized
    flag and every "connected" answer silently degrades to "unknown"."""
    dialer._write_dial_state({
        "action": "connect", "ok": True, "profile": "wg0", "iface": "wg0",
        "container": "aw-remote-host-workspace", "at": "then",
    })
    commands: list[str] = []
    _install_exec_fake(monkeypatch, commands, lambda c: json.dumps({"up": True}))

    dialer.status()

    status_cmd = next(c for c in commands if "external-status" in c)
    assert "--iface" not in status_cmd


def test_status_egress_ip_never_falls_back_to_the_host_address(monkeypatch, env):
    """host_egress_ip is, by the feature's own invariant, the address that
    did NOT change — the real, un-tunneled ISP address. Showing it as the
    VPN egress would be confidently wrong, not merely incomplete: a user
    would act on it."""
    dialer._write_dial_state({
        "action": "connect", "ok": True, "profile": "wg0", "iface": "wg0",
        "container": "aw-remote-host-workspace", "at": "then",
    })
    _install_exec_fake(monkeypatch, [], lambda c: json.dumps({
        "up": True, "container_egress_ip": None, "host_egress_ip": "198.51.100.7",
    }))

    result = dialer.status()

    assert result["egress_ip"] is None
    assert result["egress_ip"] != "198.51.100.7"


def test_status_is_unknown_rather_than_stale_when_the_query_verb_is_unavailable(monkeypatch, env):
    """A recorded "connect, ok" must NOT leak through as "connected" just
    because the live verb couldn't be reached — that is precisely the stale
    answer the dead-man's switch makes dangerous."""
    dialer._write_dial_state({
        "action": "connect", "ok": True, "profile": "wg0", "iface": "wg0",
        "container": "aw-remote-host-workspace", "at": "then",
    })

    def fake_run(self, command, timeout_s=dialer.EXEC_TIMEOUT_S):
        return {"status": "exited", "exit_code": 2, "stdout": "", "stderr": "unknown command"}

    monkeypatch.setattr(dialer._ExecClient, "run", fake_run)

    result = dialer.status()

    assert result["state"] == "unknown"
    assert result["connected"] is False


def test_status_is_unknown_with_no_prior_dial_and_no_query_verb(monkeypatch, env):
    def fake_run(self, command, timeout_s=dialer.EXEC_TIMEOUT_S):
        return {"status": "exited", "exit_code": 2, "stdout": "", "stderr": "unknown command"}

    monkeypatch.setattr(dialer._ExecClient, "run", fake_run)

    result = dialer.status()

    assert result["state"] == "unknown"
    assert result["connected"] is False
    assert result["active"] is None


def test_status_reports_connecting_while_a_connect_is_in_flight(monkeypatch, env, profiles):
    """A connect() in progress writes "connecting" before it does anything
    else — a concurrent status() poll (a different worker, a different
    request) must not report "unknown" or a stale "disconnected" for that
    window."""
    profiles.save_config("wg0", "wireguard", WG_OK)
    root = paths.workspace_root()

    def stdout_for(command):
        if command.startswith("podman inspect"):
            return _podman_inspect_stdout(root, FAKE_HOST_ROOT)
        if "external-status" in command:
            # the host hasn't caught up yet — still reports the old state
            return json.dumps({"iface": "wg0", "up": False})
        return json.dumps({"ok": True})

    commands: list[str] = []
    _install_exec_fake(monkeypatch, commands, stdout_for)

    # Simulate the in-flight window by writing the "connecting" marker
    # connect() itself would write, without running the whole flow.
    dialer._write_dial_state({"action": "connecting", "profile": "wg0", "at": "now"})

    result = dialer.status()

    assert result["state"] == "connecting"
    assert result["active"] == "wg0"
