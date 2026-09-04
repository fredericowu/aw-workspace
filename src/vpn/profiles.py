"""VPN **profile** storage for the workspace plane — phase 1: nothing dials.

Ported by function (never by file) from ``repos/aw-backend/src/api/vpn_manager.py``,
which is 51 KB and splits cleanly in two: a config half and a dialer half. Only
the config half is here — state load/save, profile CRUD, and the NordVPN client
(countries, recommendations, ``.ovpn`` download from the public CDN). See
``docs/architecture/vpn-profiles-in-general.md`` §2.3 for why each piece of the
other half stayed behind; the three that cost the most if ignored:

* ``_apply_vpn_only`` inserts a default ``REJECT`` at ``OUTPUT`` position 1.
  This feature is **fail-open** by decision (``vpn-concentrator.md`` §3.4), and
  in a single-process API server a fail-closed OUTPUT reject leaves RFC1918 and
  established connections working — so the API keeps answering and looks
  healthy — while every outbound call dies. Not ported at any phase.
* ``_setup_inbound_routing`` uses policy-routing table 200, the same table the
  GL.iNet hub owns on the bare metal.
* the poller is a background loop with nothing to poll on a surface that
  cannot dial.

There is also nothing here that shells out. ``sudo`` in this container is a
decoy: ``sudo -n`` succeeds, but root's *bounding* set is ``0x800405fb`` with
``CAP_NET_ADMIN`` clear (measured 2026-09-03), so ``wg``/``iptables`` would fail
at the kernel with an error that never mentions capabilities. Phase 2's dialer
is a Tier-2 app holding the ``tun`` host-power grant, not this process.

Storage
-------

``$AW_WORKSPACE_HOME/data/vpn/`` (resolved through ``src.apps.paths`` — never
hardcoded, because the CLI and the server resolve the home dir from different
cwds). One file per profile, ``0600``, plus ``profiles.json`` holding the
metadata index. Nord credentials go to the workspace **secret store**
(``<home>/secrets``), never into a file under ``data/vpn/`` and never into the
repo tree or the image — that last one is the exact anti-pattern this design
exists not to reproduce (``aw-backend`` ships live Nord credentials inside its
image and in git; see the design doc §7).

Two rules that ship here rather than with the dialer, because rejecting late is
how a hole gets grandfathered in (§2.4):

1. A profile is **parsed and validated**, not stored as an opaque blob.
   ``PostUp``/``PostDown``/``PreUp``/``PreDown``/``Table``/``FwMark`` and
   OpenVPN's script hooks are **rejected with a named error** — never merged,
   never silently stripped — and nothing is written to disk. ``wg-quick up``
   runs ``PostUp`` as root, so a verbatim-body ``PUT`` is an arbitrary-root-
   command endpoint the moment ``wg-quick`` exists. It does not exist yet; this
   is exactly when to close it.
2. Nothing here returns key material over HTTP. ``get_config`` returns metadata
   and a **redacted** body; there is no ``get_config_text`` equivalent.
"""
from __future__ import annotations

import fcntl
import json
import logging
import os
import re
import threading
import time
from contextlib import contextmanager

import httpx

from src.apps import paths
from src.apps.secret_store import SecretStore

log = logging.getLogger(__name__)

# Secret-store namespace for the Nord credentials. Leading underscore on
# purpose: app slugs must match ``^[a-z][a-z0-9-]{1,40}$``
# (``src/apps/manifest.py``), so this namespace can never be claimed by an
# installed app — including phase 2's own ``aw-app-vpn``.
SECRET_NS = "_core-vpn"

NORD_API = "https://api.nordvpn.com/v1"
NORD_CDN = "https://downloads.nordcdn.com/configs/files"

# Filesystem-friendly, and (for WireGuard) usable as an interface name.
_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,30}$")

# Linux caps interface names at 15 chars (IFNAMSIZ-1).
WG_IFACE_MAX = 15

VALID_TYPES = ("wireguard", "openvpn")

# WireGuard keys that hand wg-quick a shell command or move the routing table
# out from under us. ``PreUp``/``PreDown`` are not named in the design doc but
# are the same exec vector as ``PostUp``/``PostDown`` — rejecting three of four
# hooks is not closing the hole.
WG_FORBIDDEN = ("postup", "postdown", "preup", "predown", "table", "fwmark")

# OpenVPN directives that execute something, or that re-enable executing
# something. ``script-security`` is the gate the rest need, so it is refused
# alongside them rather than treated as harmless on its own.
OVPN_FORBIDDEN = (
    "script-security", "up", "down", "route-up", "route-pre-down", "ipchange",
    "client-connect", "client-disconnect", "learn-address", "tls-verify",
    "auth-user-pass-verify", "up-restart", "plugin", "setenv-safe",
)

# Lines whose *value* is key material. Redacted on every read path.
WG_SECRET_KEYS = ("privatekey", "presharedkey")
# Inline OpenVPN blocks that carry key material (``<ca>``/``<cert>`` are public
# certificates and stay readable — they are what makes a redacted profile still
# identifiable).
OVPN_SECRET_BLOCKS = ("key", "tls-auth", "tls-crypt", "tls-crypt-v2")

REDACTED = "***REDACTED***"


class VpnProfileError(ValueError):
    """Invalid input — surfaced as a 400."""


class VpnRejectedError(VpnProfileError):
    """A profile was refused because it carries a directive we never store.

    Carries the offending directive so the API can name it: a rejection the
    user cannot act on is barely better than a silent strip.
    """

    def __init__(self, directive: str, message: str) -> None:
        super().__init__(message)
        self.directive = directive


class VpnProfileNotFound(LookupError):
    """No such profile — surfaced as a 404."""


def vpn_dir() -> str:
    """``$AW_WORKSPACE_HOME/data/vpn`` — resolved, never assumed.

    Same shape ``AppRuntime`` gives a Tier-2 app its durable storage
    (``src/apps/runtime.py``: ``<home>/data/<app id>``), so phase 2's
    ``aw-app-vpn`` reads the profiles this writes without a second convention.
    """
    d = os.path.join(paths.workspace_home(), "data", "vpn")
    os.makedirs(d, mode=0o700, exist_ok=True)
    return d


def _state_path() -> str:
    return os.path.join(vpn_dir(), "profiles.json")


def safe_name(name: str) -> str:
    name = (name or "").strip()
    if not _NAME_RE.match(name):
        raise VpnProfileError(
            f"invalid profile name {name!r}: must match {_NAME_RE.pattern}"
        )
    return name


# --- validation ---------------------------------------------------------------
#
# Everything below runs BEFORE anything touches the filesystem. A rejected
# profile must leave no trace on disk — that is the test that matters.


def _strip_comment(line: str) -> str:
    """Drop a trailing comment. ``#`` and ``;`` both start one in wg/OpenVPN
    configs, and a directive hidden behind one is not a directive."""
    for marker in ("#", ";"):
        idx = line.find(marker)
        if idx != -1:
            line = line[:idx]
    return line.strip()


def _validate_wireguard(content: str) -> dict:
    """Parse a WireGuard config, refuse the dangerous keys, return metadata."""
    endpoint = None
    seen_section = False
    for lineno, raw in enumerate(content.splitlines(), start=1):
        line = _strip_comment(raw)
        if not line:
            continue
        if line.startswith("["):
            seen_section = True
            continue
        if "=" not in line:
            raise VpnProfileError(
                f"line {lineno}: not a 'Key = Value' pair ({raw.strip()!r})"
            )
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if key.lower() in WG_FORBIDDEN:
            raise VpnRejectedError(
                key,
                f"rejected: '{key}' (line {lineno}) is not allowed in a stored "
                f"WireGuard profile. wg-quick runs PostUp/PostDown/PreUp/PreDown "
                f"as root, and Table/FwMark redirect routing outside the tunnel "
                f"this workspace would set up. Remove the line and re-submit — "
                f"it is not stripped for you, and nothing was saved.",
            )
        if key.lower() == "endpoint" and not endpoint:
            endpoint = value
    if not seen_section:
        raise VpnProfileError(
            "not a WireGuard config: no [Interface] section found"
        )
    return {"endpoint": endpoint}


def _validate_openvpn(content: str) -> dict:
    """Parse an OpenVPN config, refuse the script hooks, return metadata.

    Directives inside an inline block (``<ca>``…``</ca>``) are payload, not
    directives, so the block body is skipped rather than scanned — otherwise a
    certificate's base64 could trip a keyword match.
    """
    endpoint = None
    in_block: str | None = None
    for lineno, raw in enumerate(content.splitlines(), start=1):
        line = _strip_comment(raw)
        if not line:
            continue
        if in_block:
            if line.lower() == f"</{in_block}>":
                in_block = None
            continue
        if line.startswith("<") and line.endswith(">") and not line.startswith("</"):
            in_block = line[1:-1].strip().lower()
            continue
        parts = line.split()
        directive = parts[0].lower()
        if directive in OVPN_FORBIDDEN:
            raise VpnRejectedError(
                parts[0],
                f"rejected: '{parts[0]}' (line {lineno}) is not allowed in a "
                f"stored OpenVPN profile — up/down and the other script hooks "
                f"execute a command, and script-security is what re-enables "
                f"them. Remove the line and re-submit — it is not stripped for "
                f"you, and nothing was saved.",
            )
        if directive == "remote" and not endpoint and len(parts) > 1:
            endpoint = " ".join(parts[1:3])
    if in_block:
        raise VpnProfileError(f"unterminated inline <{in_block}> block")
    if not endpoint:
        raise VpnProfileError(
            "not an OpenVPN config: no 'remote <host> [port]' directive found"
        )
    return {"endpoint": endpoint}


def validate(ctype: str, content: str) -> dict:
    """Validate ``content`` for ``ctype``. Raises; returns parsed metadata."""
    if ctype not in VALID_TYPES:
        raise VpnProfileError(
            f"unsupported VPN type {ctype!r} (expected one of {', '.join(VALID_TYPES)})"
        )
    if not (content or "").strip():
        raise VpnProfileError("content is empty")
    if ctype == "wireguard":
        return _validate_wireguard(content)
    return _validate_openvpn(content)


# --- redaction ----------------------------------------------------------------


def redact(ctype: str, content: str) -> str:
    """The body as it is safe to return over HTTP: structure kept, keys gone.

    ``vpn-concentrator.md`` §3.6 says the control plane never hands out VPN key
    material. This surface *stores* keys, which is the only place honouring that
    rule costs anything — so it is honoured here rather than assumed.
    """
    out: list[str] = []
    in_block: str | None = None
    for raw in content.splitlines():
        if ctype == "wireguard":
            key, sep, _ = raw.partition("=")
            if sep and key.strip().lower() in WG_SECRET_KEYS:
                out.append(f"{key}= {REDACTED}")
                continue
            out.append(raw)
            continue

        stripped = raw.strip()
        if in_block:
            if stripped.lower() == f"</{in_block}>":
                in_block = None
                out.append(raw)
            continue
        if (stripped.startswith("<") and stripped.endswith(">")
                and not stripped.startswith("</")):
            name = stripped[1:-1].strip().lower()
            out.append(raw)
            if name in OVPN_SECRET_BLOCKS:
                in_block = name
                out.append(REDACTED)
            continue
        out.append(raw)
    return "\n".join(out)


# --- manager ------------------------------------------------------------------


class VpnProfiles:
    """Profile CRUD + the Nord client. No lifecycle, no tunnel, no shell.

    One instance per PROCESS, but ``AW_WORKSPACE_WORKERS`` may be >1 as of
    W1/W2, so this class no longer relies on ``threading.RLock`` alone to
    guard ``profiles.json`` — that only ever guarded this one process's own
    threads, not a sibling worker's. ``_locked()`` below adds an ``flock``
    on a sibling lock file, a real cross-process mutex, around every
    read-modify-write of the index. ``_save_state``'s tmp-then-``os.replace``
    already made a single write atomic (a reader never sees a torn file);
    what was missing was serializing the READ before it, so two concurrent
    ``save_config``/``delete_config`` calls on different workers don't both
    read the same "before" index and then each write back a version missing
    the other's change (a lost update — self-hiding, since ``list_configs``
    reconciles against disk and the orphaned profile reappears as
    ``source: disk``).
    """

    def __init__(self, secrets: SecretStore | None = None) -> None:
        self._lock = threading.RLock()
        self._secrets = secrets or SecretStore()

    @contextmanager
    def _locked(self):
        """Intra-process ``RLock`` plus a cross-process ``flock`` — the
        combination covers both this process's own threads and every
        sibling worker process racing the same ``profiles.json``."""
        with self._lock:
            lock_fd = os.open(f"{_state_path()}.lock", os.O_CREAT | os.O_RDWR, 0o600)
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX)
                yield
            finally:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
                os.close(lock_fd)

    # -- state ----------------------------------------------------------------

    def _load_state(self) -> dict:
        try:
            with open(_state_path(), encoding="utf-8") as f:
                data = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            data = {}
        configs = data.get("configs")
        return {"configs": configs if isinstance(configs, dict) else {}}

    def _save_state(self, state: dict) -> None:
        path = _state_path()
        tmp = f"{path}.tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"configs": state.get("configs", {})}, f, indent=2, sort_keys=True)
            f.write("\n")
        os.replace(tmp, path)

    def _profile_path(self, name: str, ctype: str) -> str:
        ext = ".conf" if ctype == "wireguard" else ".ovpn"
        return os.path.join(vpn_dir(), f"{safe_name(name)}{ext}")

    # -- profile CRUD ---------------------------------------------------------

    def list_configs(self) -> list[dict]:
        """Every stored profile, reconciled against what is actually on disk.

        Files dropped into ``data/vpn/`` by hand appear as ``source: disk``;
        index entries whose file is gone are dropped. Same reconcile the
        aw-backend original does, and for the same reason — the directory is
        writable by the workspace's own terminal.
        """
        with self._locked():
            state = self._load_state()
            configs = state["configs"]

            on_disk: dict[str, str] = {}
            for fn in os.listdir(vpn_dir()):
                if fn.startswith(".") or fn == "profiles.json":
                    continue
                base, dot, ext = fn.rpartition(".")
                if not dot:
                    continue
                ctype = {"conf": "wireguard", "ovpn": "openvpn"}.get(ext)
                if not ctype:
                    continue
                if base not in configs:
                    configs[base] = {"type": ctype, "source": "disk",
                                     "created_at": _mtime(os.path.join(vpn_dir(), fn))}
                on_disk[base] = ctype

            for name in list(configs):
                if name not in on_disk:
                    configs.pop(name, None)

            self._save_state(state)
            return [{"name": name, **meta} for name, meta in sorted(configs.items())]

    def get_config(self, name: str) -> dict:
        """Metadata plus a **redacted** body. Deliberately not the real one."""
        with self._locked():
            meta = self._load_state()["configs"].get(name)
            if not meta:
                raise VpnProfileNotFound(f"no profile named {name!r}")
            path = self._profile_path(name, meta["type"])
            try:
                with open(path, encoding="utf-8") as f:
                    content = f.read()
            except FileNotFoundError as exc:
                raise VpnProfileNotFound(f"no profile named {name!r}") from exc
            return {
                "name": name,
                **meta,
                "content": redact(meta["type"], content),
                "redacted": True,
            }

    def save_config(self, name: str, ctype: str, content: str,
                    source: str = "upload", extra: dict | None = None) -> dict:
        """Validate, then store. A rejected profile never reaches the disk."""
        name = safe_name(name)
        if ctype == "wireguard" and len(name) > WG_IFACE_MAX:
            raise VpnProfileError(
                f"WireGuard profile name max {WG_IFACE_MAX} chars (Linux interface limit)"
            )
        parsed = validate(ctype, content)

        with self._locked():
            path = self._profile_path(name, ctype)
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(content)
                if not content.endswith("\n"):
                    f.write("\n")

            state = self._load_state()
            existing = state["configs"].get(name) or {}
            meta = {
                "type": ctype,
                "source": source,
                "endpoint": parsed.get("endpoint"),
                "created_at": existing.get("created_at") or _now(),
                "updated_at": _now(),
                **(extra or {}),
            }
            state["configs"][name] = meta
            self._save_state(state)
            log.info("vpn: stored profile %s (%s, %s)", name, ctype, source)
            return {"name": name, **meta}

    def delete_config(self, name: str) -> None:
        with self._locked():
            state = self._load_state()
            meta = state["configs"].pop(name, None)
            if meta is None:
                raise VpnProfileNotFound(f"no profile named {name!r}")
            path = self._profile_path(name, meta["type"])
            if os.path.exists(path):
                os.remove(path)
            self._save_state(state)
            log.info("vpn: deleted profile %s", name)

    # -- status ---------------------------------------------------------------

    def status(self) -> dict:
        """What is true: profiles are stored here and **nothing dials**.

        Never a fabricated up/down. The aw-backend copy of this endpoint
        answers from a poller that has never run in a container with no ``wg``
        binary — an inert route that reads as a live one. This one says what it
        is, and the UI repeats it.
        """
        try:
            profiles = len(self._load_state()["configs"])
        except OSError:
            profiles = 0
        return {
            "phase": 1,
            "state": "no_tunnel_host",
            "connected": False,
            "active": None,
            "can_dial": False,
            "profiles": profiles,
            "detail": (
                "Profiles are stored and validated on the workspace, but no "
                "tunnel host is configured — nothing dials from here. The "
                "dialer is a separate app holding the 'tun' host-power grant "
                "(phase 2)."
            ),
        }

    # -- Nord credentials -----------------------------------------------------
    #
    # Presence in, presence out. The obvious CRUD shape (GET returns what PUT
    # accepted) would ship the service password to the browser on every render.

    def nord_credentials_state(self) -> dict:
        user = self._secrets.get(SECRET_NS, "service_username") or ""
        return {
            "configured": bool(user and self._secrets.get(SECRET_NS, "service_password")),
            "username_hint": _mask(user),
            "username_length": len(user),
            "has_access_token": bool(self._secrets.get(SECRET_NS, "access_token")),
        }

    def set_nord_credentials(self, service_username: str, service_password: str) -> dict:
        if not (service_username or "").strip() or not (service_password or "").strip():
            raise VpnProfileError(
                "service_username and service_password are both required "
                "(these are the NordVPN *service* credentials from Account → "
                "Manual setup, not your account e-mail and password)"
            )
        self._secrets.put(SECRET_NS, "service_username", service_username.strip())
        self._secrets.put(SECRET_NS, "service_password", service_password.strip())
        return self.nord_credentials_state()

    def set_nord_access_token(self, token: str) -> dict:
        """Store the long-lived token and exchange it for service credentials.

        An empty token clears all three, which is the only way to remove them —
        there is no read path that could show you what you are removing.
        """
        token = (token or "").strip()
        if not token:
            for key in ("access_token", "service_username", "service_password"):
                self._secrets.delete(SECRET_NS, key)
            return self.nord_credentials_state()
        self._secrets.put(SECRET_NS, "access_token", token)
        user, password = self._fetch_nord_service_credentials(token)
        self._secrets.put(SECRET_NS, "service_username", user)
        self._secrets.put(SECRET_NS, "service_password", password)
        return self.nord_credentials_state()

    @staticmethod
    def _fetch_nord_service_credentials(token: str) -> tuple[str, str]:
        """``/v1/users/services/credentials`` with HTTP Basic ``token:<token>``."""
        res = httpx.get(
            f"{NORD_API}/users/services/credentials",
            auth=("token", token),
            headers={"Accept": "application/json"},
            timeout=15.0,
        )
        res.raise_for_status()
        payload = res.json()
        user = payload.get("username") or ""
        password = payload.get("password") or ""
        if not user or not password:
            raise VpnProfileError("Nord API returned no service credentials for that token")
        return user, password

    # -- Nord browse + import -------------------------------------------------
    #
    # Nord's public API is unversioned and third-party: when it changes, this
    # returns an empty list rather than an error, and the screen looks broken
    # for a reason nothing here can report. Noted in the design doc §4.

    def nord_countries(self) -> list[dict]:
        res = httpx.get(f"{NORD_API}/servers/countries", timeout=10.0)
        res.raise_for_status()
        return [
            {"id": c.get("id"), "name": c.get("name"), "code": c.get("code"),
             "cities": [{"id": ct.get("id"), "name": ct.get("name")}
                        for ct in c.get("cities", [])]}
            for c in res.json()
        ]

    def nord_recommendations(self, country_id: int | None = None,
                             city_id: int | None = None,
                             limit: int = 10) -> list[dict]:
        # Nord takes filters in bracket notation, which httpx will not build
        # from a dict without escaping the brackets — hand them over as params
        # keyed exactly as the API expects.
        params: dict[str, object] = {"limit": limit}
        if country_id:
            params["filters[country_id]"] = country_id
        if city_id:
            params["filters[country_city_id]"] = city_id
        res = httpx.get(f"{NORD_API}/servers/recommendations", params=params, timeout=10.0)
        res.raise_for_status()
        out = []
        for s in res.json():
            location = (s.get("locations") or [{}])[0].get("country", {}) or {}
            out.append({
                "name": s.get("name"),
                "hostname": s.get("hostname"),
                "load": s.get("load"),
                "station": s.get("station"),
                "country": location.get("name"),
                "city": (location.get("city") or {}).get("name"),
            })
        return out

    def nord_import(self, hostname: str, protocol: str = "udp",
                    name: str | None = None) -> dict:
        """Download a Nord ``.ovpn`` from the public CDN and store it.

        The download goes through the same ``save_config`` as a hand-uploaded
        file, so a Nord config carrying a script hook is refused too. Nord's own
        configs do not carry one today; trusting that they never will is how the
        validation gets a special case that outlives its reason.
        """
        if protocol not in ("udp", "tcp"):
            raise VpnProfileError("protocol must be udp or tcp")
        hostname = (hostname or "").strip()
        if not hostname or "/" in hostname or ".." in hostname:
            raise VpnProfileError(f"invalid Nord hostname {hostname!r}")
        url = f"{NORD_CDN}/ovpn_{protocol}/servers/{hostname}.{protocol}.ovpn"
        res = httpx.get(url, timeout=15.0)
        res.raise_for_status()

        short = hostname.split(".")[0]
        return self.save_config(
            name or f"{short}-{protocol}",
            "openvpn",
            res.text,
            source="nord",
            extra={"nord_hostname": hostname, "nord_protocol": protocol},
        )


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _mtime(path: str) -> str:
    try:
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(os.path.getmtime(path)))
    except OSError:
        return _now()


def _mask(value: str) -> str:
    """Enough to recognise a username you already know, useless to anyone else."""
    if not value:
        return ""
    if len(value) <= 4:
        return "*" * len(value)
    return f"{value[:2]}{'*' * (len(value) - 4)}{value[-2:]}"
