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
