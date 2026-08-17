"""Host power grants — the elevated device/capability access a Tier-2 app
container may be given, and never without the BYOD host's own opt-in.

Until this existed, ``src.apps.containers`` rejected ``--privileged``
outright and understood no device flags at all, so a whole class of app was
impossible to build here: anything that runs a *guest* rather than a process.
A Windows VM needs ``/dev/kvm`` or it falls back to software emulation and is
unusably slow; it needs ``/dev/net/tun`` for the guest's own NIC; an Android
guest needs the binder devices. None of that can be faked from userspace.

The flat rejection was still the right default, because the alternative most
projects reach for — let the manifest ask for ``--privileged`` and hand it
over — means installing an app is enough to dissolve the isolation boundary
around everything else on that machine. So the grant is split across three
legs that have to line up independently, none of which the app controls
alone:

1. **the host opted in** — ``aw-remote-host bootstrap-workspace
   --host-power=kvm,tun`` (or ``=all``) records the set and passes the
   *effective* one in as ``AW_HOST_POWER``. The host probes what it can
   actually deliver first: there is no ``/dev/kvm`` on macOS, and rootless
   podman cannot pass through a device the invoking user can't open, so a
   request is not the same as a grant.
2. **the app asked** — ``runtime.host_power: ["kvm", "tun"]`` in its manifest.
3. **the app was granted the capability** — ``host:device-kvm`` &co, all
   high-risk, so marketplace-signed apps only (ADR Decision 4/3b).

Missing any leg **fails the install**, loudly, naming the leg. It does not
start the container without the grant: a Windows VM that boots into software
emulation reads as "the app is broken and slow", and the cause (a host that
never opted in) is invisible from there. Silent degradation is this
workspace's documented failure mode — see ``aw-workspace-cli doctor``.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Iterable

log = logging.getLogger(__name__)

#: Env var the host (``aw-remote-host``'s ``bootstrap/workspace/install.sh``)
#: sets on the workspace container: a comma-separated list of the grants it
#: verified it can actually deliver. Absent/empty = this host opted into
#: nothing, which is the default on every host.
ENV_VAR = "AW_HOST_POWER"

#: Accepted in a host's ``--host-power`` list and in ``runtime.host_power``:
#: expands to every granular grant, and pointedly NOT to ``privileged``.
ALL_KEYWORD = "all"

#: grant name -> what it means and what it costs.
#:
#: ``devices`` are passed through as-is; ``caps`` are Linux capabilities added
#: on top, because a device node alone is often not enough (``/dev/net/tun``
#: without ``NET_ADMIN`` opens and then fails to configure the interface).
GRANTS: dict[str, dict[str, Any]] = {
    "kvm": {
        "capability": "host:device-kvm",
        "devices": ("/dev/kvm",),
        "caps": (),
        "desc": "hardware virtualisation — a QEMU/KVM guest (aw-app-windows)",
    },
    "tun": {
        "capability": "host:device-tun",
        "devices": ("/dev/net/tun",),
        "caps": ("NET_ADMIN",),
        "desc": "TAP/TUN networking — a guest VM's or a VPN's own virtual NIC",
    },
    "fuse": {
        "capability": "host:device-fuse",
        "devices": ("/dev/fuse",),
        "caps": ("SYS_ADMIN",),
        "desc": "FUSE mounts — nested container storage, AppImages",
    },
    "binder": {
        "capability": "host:device-binder",
        "devices": ("/dev/binder", "/dev/hwbinder", "/dev/vndbinder"),
        "caps": (),
        "desc": "Android binder IPC — a redroid/Android guest",
    },
    "privileged": {
        "capability": "host:privileged",
        "devices": (),
        "caps": (),
        "desc": "full --privileged: every device, every capability, no isolation",
    },
}

#: What ``all`` expands to, in this order.
#:
#: ``privileged`` is excluded on purpose. "All the devices my host can offer"
#: and "dissolve the container boundary" are different decisions with
#: different blast radii, and a convenience keyword must not silently make
#: the second one for you — ``privileged`` has to be typed.
GRANULAR: tuple[str, ...] = ("kvm", "tun", "fuse", "binder")

#: Every capability string this module can require. Kept here so the
#: capability catalog and this catalog cannot drift (see
#: ``src/tests/.../test_manifest_schema_matches_catalog.py``).
CAPABILITIES: tuple[str, ...] = tuple(g["capability"] for g in GRANTS.values())


class HostPowerError(RuntimeError):
    """A host-power grant was asked for that cannot be honoured."""


def expand(names: Iterable[str] | None) -> tuple[str, ...]:
    """Normalise a grant list: expand ``all``, dedupe, order deterministically.

    Order follows ``GRANTS`` insertion order rather than the caller's, so the
    same set always renders the same way — the string ends up in a container
    label, a ``status`` line and a console badge, and a set that reorders
    itself between runs looks like a change that didn't happen.
    """
    if not names:
        return ()
    requested: set[str] = set()
    for raw in names:
        name = str(raw or "").strip().lower()
        if not name:
            continue
        if name == ALL_KEYWORD:
            requested.update(GRANULAR)
            continue
        if name not in GRANTS:
            raise HostPowerError(
                f"unknown host power grant {name!r} — known: "
                f"{ALL_KEYWORD}, {', '.join(GRANTS)}"
            )
        requested.add(name)
    return tuple(name for name in GRANTS if name in requested)


def parse_list(value: str | None) -> tuple[str, ...]:
    """Parse a comma-separated grant list (the ``AW_HOST_POWER`` wire format)."""
    if not value:
        return ()
    return expand(part for part in str(value).replace(" ", "").split(","))


def host_grants(env: dict[str, str] | None = None) -> tuple[str, ...]:
    """What THIS host opted into, from ``AW_HOST_POWER``.

    An unparseable value is treated as "nothing granted" rather than raising:
    this is read on every Tier-2 app load, and a typo in one host's service
    definition must not make every app on it unloadable. The warning is the
    signal, and ``doctor`` surfaces it where someone looks.
    """
    source = os.environ if env is None else env
    raw = (source.get(ENV_VAR) or "").strip()
    if not raw:
        return ()
    try:
        return parse_list(raw)
    except HostPowerError as exc:
        log.warning("apps: ignoring malformed %s=%r (%s)", ENV_VAR, raw, exc)
        return ()


def required_capabilities(grants: Iterable[str]) -> tuple[str, ...]:
    """The capability strings an app must hold to receive ``grants``."""
    return tuple(GRANTS[name]["capability"] for name in expand(grants))


def resolve(
    app_id: str,
    requested: Iterable[str] | None,
    permissions: Iterable[str] | None,
    env: dict[str, str] | None = None,
) -> tuple[str, ...]:
    """Check all three legs for ``app_id`` and return the granted set.

    Raises :class:`HostPowerError` naming the leg that failed — the message
    goes straight into the install error the user sees, so it says what to do
    about it, not just what went wrong.
    """
    wanted = expand(requested)
    if not wanted:
        return ()

    held = set(permissions or ())
    missing_caps = [
        cap for cap in required_capabilities(wanted) if cap not in held
    ]
    if missing_caps:
        raise HostPowerError(
            f"{app_id} declares runtime.host_power but is missing the matching "
            f"permission(s): {', '.join(sorted(missing_caps))}"
        )

    available = set(host_grants(env))
    # A host that granted `privileged` granted every device and capability
    # there is, so it satisfies any narrower request by definition. Without
    # this, granting the MOST powerful thing refused an app that asked for
    # less — "I enabled everything and the app still won't install" (found
    # live on bare-metal, 2026-08-17).
    #
    # The app still only receives what it ASKED for: an app wanting kvm+tun
    # gets kvm+tun, not --privileged. The host's grant is a ceiling, not a
    # floor, and there is no reason to hand out more isolation loss than the
    # manifest declared it needs.
    if "privileged" in available:
        available.update(GRANULAR)

    missing_host = [name for name in wanted if name not in available]
    if missing_host:
        offered = ", ".join(sorted(available)) or "nothing"
        raise HostPowerError(
            f"{app_id} needs host power [{', '.join(missing_host)}], which this "
            f"host has not granted (it offers: {offered}). Re-run "
            f"`aw-remote-host bootstrap-workspace --with-workspace "
            f"--host-power={','.join(wanted)}` on the host machine, then retry "
            f"the install."
        )
    return wanted


def docker_kwargs(grants: Iterable[str]) -> dict[str, Any]:
    """Map granted names onto ``docker`` SDK ``containers.run`` kwargs.

    ``privileged`` short-circuits the rest: it already implies every device
    and capability, and also listing them would be noise in ``inspect``
    output that reads as a tighter grant than what is actually in force.
    """
    resolved = expand(grants)
    if not resolved:
        return {}
    if "privileged" in resolved:
        return {"privileged": True}

    devices: list[str] = []
    caps: list[str] = []
    for name in resolved:
        spec = GRANTS[name]
        for dev in spec["devices"]:
            # rwm = read/write/mknod, what a device passthrough normally means.
            entry = f"{dev}:{dev}:rwm"
            if entry not in devices:
                devices.append(entry)
        for cap in spec["caps"]:
            if cap not in caps:
                caps.append(cap)

    kwargs: dict[str, Any] = {}
    if devices:
        kwargs["devices"] = devices
    if caps:
        kwargs["cap_add"] = caps
    return kwargs


def describe(grants: Iterable[str]) -> str:
    """Human-readable one-liner for ``status`` / ``doctor`` / a log line."""
    resolved = expand(grants)
    if not resolved:
        return "standard (no elevated host access)"
    if "privileged" in resolved:
        others = [n for n in resolved if n != "privileged"]
        suffix = f" (+{', '.join(others)})" if others else ""
        return f"PRIVILEGED{suffix} — no container isolation"
    return ", ".join(resolved)
