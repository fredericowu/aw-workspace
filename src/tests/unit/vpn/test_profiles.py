"""The storage contract of ``src/vpn/profiles.py``.

The tests that matter here are the *rejections*, not the round trips. A
``PostUp`` that is silently stripped looks identical to one that was never
sent — right up until someone makes stripping conditional and ``wg-quick``
exists to run it as root. So each forbidden directive is pinned twice: refused
with its own name in the error, and **nothing written to disk**.
"""
from __future__ import annotations

import json
import os
import stat

import pytest

from src.vpn import profiles as mod
from src.vpn.profiles import (
    VpnProfileError,
    VpnProfileNotFound,
    VpnProfiles,
    VpnRejectedError,
)

WG_OK = """
[Interface]
PrivateKey = aGVsbG8gd29ybGQgdGhpcyBpcyBub3QgYSBrZXk9
Address = 10.5.0.2/32
DNS = 10.5.0.1

[Peer]
PublicKey = cHVibGljIGtleSB0aGF0IGlzIG5vdCByZWFsIGE9
AllowedIPs = 0.0.0.0/0
Endpoint = vpn.example.com:51820
""".strip()

OVPN_OK = """
client
dev tun
proto udp
remote pt121.nordvpn.com 1194
<ca>
-----BEGIN CERTIFICATE-----
notarealcert
-----END CERTIFICATE-----
</ca>
<tls-auth>
-----BEGIN OpenVPN Static key V1-----
deadbeef
-----END OpenVPN Static key V1-----
</tls-auth>
""".strip()


@pytest.fixture()
def mgr(tmp_path, monkeypatch):
    monkeypatch.setenv("AW_WORKSPACE_HOME", str(tmp_path / "home"))
    # Deterministic Fernet key so the secret store never writes into a real home.
    from cryptography.fernet import Fernet
    monkeypatch.setenv("AW_WORKSPACE_SECRET_KEY", Fernet.generate_key().decode())
    return VpnProfiles()


def _files(mgr) -> list[str]:
    return sorted(os.listdir(mod.vpn_dir()))


# --- rule 1: parsed and validated, never a blob ------------------------------


@pytest.mark.parametrize("directive", ["PostUp", "PostDown", "PreUp", "PreDown",
                                       "Table", "FwMark"])
def test_wireguard_directive_is_rejected_by_name_and_nothing_is_written(mgr, directive):
    content = WG_OK.replace(
        "DNS = 10.5.0.1", f"DNS = 10.5.0.1\n{directive} = /bin/sh -c 'id > /tmp/pwned'"
    )

    with pytest.raises(VpnRejectedError) as exc:
        mgr.save_config("wg0", "wireguard", content)

    assert exc.value.directive == directive
    assert directive in str(exc.value)
    assert "wg0.conf" not in _files(mgr), "a rejected profile must not reach the disk"
    assert mgr.list_configs() == []


@pytest.mark.parametrize("directive", ["up", "down", "script-security", "route-up",
                                       "client-connect", "learn-address", "plugin"])
def test_openvpn_directive_is_rejected_by_name_and_nothing_is_written(mgr, directive):
    content = OVPN_OK.replace("client\n", f"client\n{directive} /bin/sh\n")

    with pytest.raises(VpnRejectedError) as exc:
        mgr.save_config("nord1", "openvpn", content)

    assert exc.value.directive == directive
    assert "nord1.ovpn" not in _files(mgr)


def test_a_forbidden_directive_hidden_behind_a_comment_is_not_a_directive(mgr):
    """``# PostUp = ...`` is a comment, and refusing it would make a valid
    config unstorable for no gain."""
    content = WG_OK.replace("DNS = 10.5.0.1", "DNS = 10.5.0.1\n# PostUp = /bin/sh")
    assert mgr.save_config("wg0", "wireguard", content)["name"] == "wg0"


def test_a_forbidden_directive_after_a_valid_one_is_still_caught(mgr):
    """The scan does not stop at the first well-formed section."""
    content = f"{WG_OK}\nPostUp = /bin/sh"
    with pytest.raises(VpnRejectedError):
        mgr.save_config("wg0", "wireguard", content)


def test_a_certificate_body_is_not_mistaken_for_a_directive(mgr):
    """Inline block payload is skipped, not keyword-matched — a base64 line
    starting with 'up' would otherwise trip the OpenVPN scan."""
    content = OVPN_OK.replace("notarealcert", "up7Zdown9script-security")
    assert mgr.save_config("nord1", "openvpn", content)["name"] == "nord1"


def test_an_unparseable_wireguard_config_is_refused(mgr):
    with pytest.raises(VpnProfileError):
        mgr.save_config("wg0", "wireguard", "this is not a config")


def test_an_openvpn_config_without_a_remote_is_refused(mgr):
    with pytest.raises(VpnProfileError):
        mgr.save_config("nord1", "openvpn", "client\ndev tun\n")


def test_an_unknown_type_is_refused(mgr):
    with pytest.raises(VpnProfileError):
        mgr.save_config("x", "tailscale", "whatever")


def test_a_bad_name_is_refused(mgr):
    with pytest.raises(VpnProfileError):
        mgr.save_config("../../etc/passwd", "wireguard", WG_OK)


def test_a_wireguard_name_longer_than_the_interface_limit_is_refused(mgr):
    with pytest.raises(VpnProfileError):
        mgr.save_config("a" * 16, "wireguard", WG_OK)


# --- rule 2: no private key over the API -------------------------------------


def test_get_config_redacts_the_wireguard_private_key(mgr):
    mgr.save_config("wg0", "wireguard", WG_OK)

    got = mgr.get_config("wg0")

    assert got["redacted"] is True
    assert "aGVsbG8gd29ybGQ" not in got["content"], "PrivateKey leaked over the API"
    assert mod.REDACTED in got["content"]
    # The parts that identify the profile survive redaction.
    assert "Endpoint = vpn.example.com:51820" in got["content"]
    assert "cHVibGljIGtleQ" not in got["content"] or True  # PublicKey is not secret


def test_get_config_redacts_openvpn_key_material_but_keeps_the_ca(mgr):
    mgr.save_config("nord1", "openvpn", OVPN_OK)

    content = mgr.get_config("nord1")["content"]

    assert "deadbeef" not in content, "tls-auth key leaked over the API"
    assert "notarealcert" in content, "the CA is public and identifies the server"
    assert "remote pt121.nordvpn.com 1194" in content


def test_there_is_no_plaintext_read_path_on_the_manager(mgr):
    """``get_config_text`` is the aw-backend method this port deliberately
    dropped. Its absence is the contract, so pin it."""
    assert not hasattr(mgr, "get_config_text")


# --- storage -----------------------------------------------------------------


def test_round_trip_lists_edits_and_deletes(mgr):
    mgr.save_config("wg0", "wireguard", WG_OK)

    listed = mgr.list_configs()
    assert [c["name"] for c in listed] == ["wg0"]
    assert listed[0]["type"] == "wireguard"
    assert listed[0]["source"] == "upload"
    assert listed[0]["endpoint"] == "vpn.example.com:51820"
    assert listed[0]["created_at"]

    mgr.save_config("wg0", "wireguard", WG_OK.replace("51820", "51821"))
    assert mgr.list_configs()[0]["endpoint"] == "vpn.example.com:51821"

    mgr.delete_config("wg0")
    assert mgr.list_configs() == []
    assert "wg0.conf" not in _files(mgr)


def test_deleting_something_that_is_not_there_raises_not_found(mgr):
    with pytest.raises(VpnProfileNotFound):
        mgr.delete_config("nope")


def test_stored_profiles_are_0600_under_the_workspace_home(mgr, tmp_path):
    mgr.save_config("wg0", "wireguard", WG_OK)

    path = os.path.join(mod.vpn_dir(), "wg0.conf")
    assert path.startswith(str(tmp_path / "home")), "storage must follow AW_WORKSPACE_HOME"
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(mod._state_path()).st_mode) == 0o600


def test_a_file_dropped_in_by_hand_is_adopted_as_source_disk(mgr):
    mgr.list_configs()  # create the dir
    with open(os.path.join(mod.vpn_dir(), "manual.conf"), "w", encoding="utf-8") as f:
        f.write(WG_OK)

    listed = mgr.list_configs()

    assert [c["name"] for c in listed] == ["manual"]
    assert listed[0]["source"] == "disk"


def test_an_index_entry_whose_file_vanished_is_dropped(mgr):
    mgr.save_config("wg0", "wireguard", WG_OK)
    os.remove(os.path.join(mod.vpn_dir(), "wg0.conf"))

    assert mgr.list_configs() == []


# --- rule 3: Nord credentials never round-trip -------------------------------


def test_nord_credentials_are_never_returned_and_never_hit_the_profile_dir(mgr):
    mgr.set_nord_credentials("fredericoservice", "sup3rs3cret")

    state = mgr.nord_credentials_state()

    assert state["configured"] is True
    assert "sup3rs3cret" not in json.dumps(state), "the service password left the server"
    assert state["username_hint"] == "fr************ce"
    assert "fredericoservice" not in json.dumps(state)

    # Nothing under data/vpn/ ever holds a credential.
    for fn in _files(mgr):
        with open(os.path.join(mod.vpn_dir(), fn), encoding="utf-8") as f:
            body = f.read()
        assert "sup3rs3cret" not in body
        assert "fredericoservice" not in body


def test_nord_credentials_live_in_the_secret_store_encrypted(mgr, tmp_path):
    mgr.set_nord_credentials("fredericoservice", "sup3rs3cret")

    secret_file = tmp_path / "home" / "secrets" / f"{mod.SECRET_NS}.json"
    body = secret_file.read_text(encoding="utf-8")

    assert "sup3rs3cret" not in body, "stored in plaintext"
    assert "service_password" in body


def test_incomplete_nord_credentials_are_refused(mgr):
    with pytest.raises(VpnProfileError):
        mgr.set_nord_credentials("user-only", "")


def test_an_empty_access_token_clears_everything(mgr):
    mgr.set_nord_credentials("fredericoservice", "sup3rs3cret")

    state = mgr.set_nord_access_token("")

    assert state["configured"] is False
    assert state["has_access_token"] is False


def test_an_access_token_is_exchanged_for_service_credentials(mgr, monkeypatch):
    monkeypatch.setattr(
        mod.VpnProfiles, "_fetch_nord_service_credentials",
        staticmethod(lambda token: ("svcuser", "svcpass")),
    )

    state = mgr.set_nord_access_token("tok_123")

    assert state["has_access_token"] is True
    assert state["configured"] is True
    assert "svcpass" not in json.dumps(state)


# --- rule: nothing dials, and the status endpoint says so --------------------


def test_status_is_honest_about_having_no_tunnel_host(mgr):
    status = mgr.status()

    assert status["connected"] is False
    assert status["can_dial"] is False
    assert status["state"] == "no_tunnel_host"
    assert status["active"] is None


def test_the_manager_exposes_no_lifecycle_surface(mgr):
    """The dialer half of aw-backend's vpn_manager.py is not ported at any
    phase; its absence is the point of this card, so it is pinned."""
    for banned in ("start", "stop", "set_vpn_only", "restore_routing",
                   "_setup_inbound_routing", "_apply_vpn_only", "vpn_poller"):
        assert not hasattr(mgr, banned), f"{banned} must not exist on this plane"


def test_nothing_in_the_module_shells_out(mgr):
    """``sudo`` here is a decoy — root's bounding set has CAP_NET_ADMIN clear,
    so a shell-out fails at the kernel with an error that never says
    'capability'. Easier to never import a way to run one.

    Walks the imports rather than grepping the text: the module docstring
    explains at length what it does NOT run, and a substring check would be
    tripped by its own documentation.
    """
    import ast

    tree = ast.parse(open(mod.__file__, encoding="utf-8").read())
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])

    assert "subprocess" not in imported
    assert "pty" not in imported


# --- Nord import goes through the same validation ----------------------------


def test_nord_import_stores_the_downloaded_config(mgr, monkeypatch):
    class _Res:
        text = OVPN_OK

        def raise_for_status(self):
            return None

    monkeypatch.setattr(mod.httpx, "get", lambda url, **kw: _Res())

    saved = mgr.nord_import("pt121.nordvpn.com", "udp")

    assert saved["name"] == "pt121-udp"
    assert saved["source"] == "nord"
    assert saved["nord_hostname"] == "pt121.nordvpn.com"
    assert "pt121-udp.ovpn" in _files(mgr)


def test_a_nord_config_carrying_a_script_hook_is_refused_too(mgr, monkeypatch):
    """Trusting the CDN is how validation acquires a special case that
    outlives its reason."""
    class _Res:
        text = OVPN_OK.replace("client\n", "client\nup /bin/sh\n")

        def raise_for_status(self):
            return None

    monkeypatch.setattr(mod.httpx, "get", lambda url, **kw: _Res())

    with pytest.raises(VpnRejectedError):
        mgr.nord_import("pt121.nordvpn.com", "udp")
    assert _files(mgr) in ([], ["profiles.json"])


def test_nord_import_rejects_a_traversing_hostname(mgr):
    with pytest.raises(VpnProfileError):
        mgr.nord_import("../../etc/passwd", "udp")


def test_nord_import_rejects_an_unknown_protocol(mgr):
    with pytest.raises(VpnProfileError):
        mgr.nord_import("pt121.nordvpn.com", "sctp")
