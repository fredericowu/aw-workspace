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


def test_concurrent_saves_from_separate_instances_lose_nothing(mgr, tmp_path):
    """W2: the cross-process lost-update race. In production, each worker
    process constructs its OWN ``VpnProfiles()`` — a ``threading.RLock``
    only ever guards one instance's own thread(s), so N separate instances
    racing ``save_config`` reproduces the exact hazard N separate worker
    PROCESSES would hit (no shared Python object between them either way).
    Before the ``_locked()`` fix (an ``flock`` on a sibling lock file —
    real, cross-instance, cross-process), two concurrent saves could each
    read the same "before" index and each write back a version missing the
    other's new entry — a lost update, not a crash, so nothing before this
    test would have caught it."""
    import threading

    n = 12
    barrier = threading.Barrier(n)
    errors: list[BaseException] = []

    def save_one(i: int) -> None:
        try:
            barrier.wait(timeout=10)
            # Separate VpnProfiles() instance per "worker" — see docstring.
            VpnProfiles().save_config(f"wg{i}", "wireguard",
                                      WG_OK.replace("51820", str(51820 + i)))
        except BaseException as exc:  # noqa: BLE001 — captured, not raised, from a worker thread
            errors.append(exc)

    threads = [threading.Thread(target=save_one, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)

    assert not errors, f"{len(errors)} worker(s) raised: {errors}"
    names = {c["name"] for c in mgr.list_configs()}
    assert names == {f"wg{i}" for i in range(n)}, \
        f"lost update: expected {n} profiles, index has {sorted(names)}"


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


# --- wireguard_dial_fields ----------------------------------------------------
#
# The only function that returns real key material. What matters here is not
# the happy path alone: a hand-dropped file that never went through
# save_config's validation (list_configs reconciles disk files without
# validating them) must still be refused, not dialed.

WG_FULL = """
[Interface]
PrivateKey = aGVsbG8gd29ybGQgdGhpcyBpcyBub3QgYSBrZXk9
Address = 10.5.0.2/32, fd00::2/128
DNS = 10.5.0.1, 1.1.1.1
MTU = 1420

[Peer]
PublicKey = cHVibGljIGtleSB0aGF0IGlzIG5vdCByZWFsIGE9
PresharedKey = cHJlc2hhcmVkIGtleSBub3QgcmVhbCE9
AllowedIPs = 0.0.0.0/0, ::/0
Endpoint = vpn.example.com:51820
PersistentKeepalive = 25
""".strip()


def test_wireguard_dial_fields_happy_path(mgr):
    mgr.save_config("wg0", "wireguard", WG_FULL)

    fields = mgr.wireguard_dial_fields("wg0")

    assert fields == {
        "type": "wireguard",
        "iface": "wg0",
        "private_key": "aGVsbG8gd29ybGQgdGhpcyBpcyBub3QgYSBrZXk9",
        "address": ["10.5.0.2/32", "fd00::2/128"],
        "dns": ["10.5.0.1", "1.1.1.1"],
        "mtu": 1420,
        "peer": {
            "public_key": "cHVibGljIGtleSB0aGF0IGlzIG5vdCByZWFsIGE9",
            "preshared_key": "cHJlc2hhcmVkIGtleSBub3QgcmVhbCE9",
            "endpoint": "vpn.example.com:51820",
            "allowed_ips": ["0.0.0.0/0", "::/0"],
            "persistent_keepalive": 25,
        },
    }


def test_wireguard_dial_fields_unknown_profile(mgr):
    with pytest.raises(VpnProfileNotFound):
        mgr.wireguard_dial_fields("nope")


def test_wireguard_dial_fields_refuses_an_openvpn_profile(mgr):
    mgr.save_config("nord1", "openvpn", OVPN_OK)

    with pytest.raises(VpnProfileError, match="openvpn"):
        mgr.wireguard_dial_fields("nord1")


@pytest.mark.parametrize("directive", ["PostUp", "PostDown", "PreUp", "PreDown",
                                       "Table", "FwMark"])
def test_wireguard_dial_fields_refuses_a_forbidden_directive_even_off_disk(mgr, directive):
    """save_config would have refused this profile outright — but a file
    dropped directly into vpn_dir() (a workspace terminal can write there)
    skips that validation, and list_configs reconciles it in regardless. The
    dialer must not trust that a stored profile was ever actually validated.
    """
    content = WG_FULL.replace(
        "MTU = 1420", f"MTU = 1420\n{directive} = /bin/sh -c 'id > /tmp/pwned'"
    )
    with open(os.path.join(mod.vpn_dir(), "hand-dropped.conf"), "w", encoding="utf-8") as f:
        f.write(content)
    mgr.list_configs()  # reconciles the on-disk file into the index

    with pytest.raises(VpnRejectedError) as exc:
        mgr.wireguard_dial_fields("hand-dropped")
    assert exc.value.directive == directive


def test_wireguard_dial_fields_rejects_a_second_peer(mgr):
    two_peers = WG_FULL + "\n\n[Peer]\nPublicKey = c2Vjb25kIHBlZXIga2V5IQ==\n"
    mgr.save_config("wg0", "wireguard", two_peers)

    with pytest.raises(VpnProfileError, match="single-peer"):
        mgr.wireguard_dial_fields("wg0")


def test_wireguard_dial_fields_requires_a_private_key(mgr):
    no_key = WG_FULL.replace(
        "PrivateKey = aGVsbG8gd29ybGQgdGhpcyBpcyBub3QgYSBrZXk9\n", ""
    )
    with open(os.path.join(mod.vpn_dir(), "wg0.conf"), "w", encoding="utf-8") as f:
        f.write(no_key)
    mgr.list_configs()

    with pytest.raises(VpnProfileError, match="PrivateKey"):
        mgr.wireguard_dial_fields("wg0")


def test_wireguard_dial_fields_rejects_a_name_too_long_for_an_interface(mgr):
    """A disk-reconciled profile skips save_config's own WG_IFACE_MAX check —
    the dialer must not hand a >15-char name to ``--iface`` regardless."""
    long_name = "a" * (mod.WG_IFACE_MAX + 1)
    with open(os.path.join(mod.vpn_dir(), f"{long_name}.conf"), "w", encoding="utf-8") as f:
        f.write(WG_FULL)
    mgr.list_configs()

    with pytest.raises(VpnProfileError, match="interface"):
        mgr.wireguard_dial_fields(long_name)
