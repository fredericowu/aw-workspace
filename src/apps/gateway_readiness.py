"""Gateway readiness: what the mcp-gateway itself reports serving.

Extracted from ``src/apps/routes.py`` (``_mcp_gateway_status`` and its
expectation-building) so code outside the doctor route, notably the runners
app's ``/healthy`` probe (graceful-restart card 4215bf3b, slice S2), can call
the exact same logic ``aw-workspace-cli doctor`` uses instead of re-deriving it.

Public surface (all importable, no FastAPI app needed):

* :func:`mcp_gateway_status` - the full doctor-section computation.
* :func:`collect_gateway_expectations` - the ``expected*`` maps from a runtime.
* :func:`gateway_readiness` - the two combined; the one call a probe needs.

Return contract of :func:`mcp_gateway_status` / :func:`gateway_readiness`
(superset of the doctor ``mcp`` section; existing keys unchanged):

* ``reachable``: ``True`` | ``False`` | ``None`` (``None`` = gateway app not
  installed, which callers treat as ready).
* ``tools``: int, ``local_upstreams``: list[str] (local upstreams only;
  federated/remote upstreams are never listed here, so they can never gate).
* ``not_ready``: list of ``{"kind", "server"|"profile"|None, "app", "reason"}``
  with ``kind`` in ``dead_upstream | dead_profile | stale_upstream |
  zero_tools | warm_redis``. Empty list == nothing the doctor would flag.
  Present only when ``reachable`` is ``True``; absent otherwise, so use
  :func:`is_ready`.
"""
from __future__ import annotations

import json
import logging
import os

from src.apps.containers import ContainerError

log = logging.getLogger(__name__)


def referenced_gateway_profiles(runtime) -> dict[str, str]:
    """Scoped gateway profiles installed apps expect — name -> owning app slug.

    Read from ``contributes.agents[*].mcp_servers[*].profile``, the only place
    a profile reference exists (``manifest._validate_mcp_references``). An
    agent-config pointing at ``/mcp/<name>`` for a profile the gateway does
    not serve gets ``404 {"error":"No such config: <name>"}`` on every request
    and the agent runs with ZERO tools — with nothing in any log on this side,
    because from here it is a perfectly valid URL. Naming the profile is the
    whole value: a total tool count cannot see it, since every other agent on
    the same gateway keeps working.
    """
    out: dict[str, str] = {}
    for slug in runtime.loaded_slugs():
        loaded = runtime.get(slug)
        if loaded is None:
            continue
        for entries in (loaded.manifest.agents or {}).values():
            for entry in entries or []:
                if not isinstance(entry, dict):
                    continue
                for ref in entry.get("mcp_servers") or []:
                    if not isinstance(ref, dict):
                        continue
                    name = str(ref.get("profile") or "").strip()
                    if name:
                        out.setdefault(name, slug)
    return out


async def mcp_gateway_status(runtime: AppRuntime, *, expect_tools: bool,
                              expected: dict[str, str] | None = None,
                              expected_profiles: dict[str, str] | None = None,
                              expected_versions: dict[str, str] | None = None) -> dict:
    """What the gateway itself reports serving, for doctor's ``mcp`` section.

    Before this, ``mcp.apps_contributing_tools`` only listed apps that ship an
    ``mcp.json`` — a pure presence check that can never fail, so ``doctor``
    could exit 0 while the gateway itself was unreachable or serving zero
    tools (confirmed live 2026-08-19: two upstreams were dead and doctor's
    exit code owed entirely to an unrelated architecture self-check). This
    calls the gateway's own unauthenticated ``GET /healthz`` — cheap, no
    admin token needed — and folds unreachable/zero-tools/dead-upstreams into
    ``degraded`` so it actually counts as a problem.

    ``expected`` maps upstream/server name -> owning app slug, i.e. the
    ``mcpServers`` keys read straight out of each app's own ``mcp.json`` —
    NOT a guess normalized from the app's slug (a gateway upstream is named
    after that key, which does not match the app slug 1:1; see
    ``aw-mcp-gateway``'s ``config.scan_app_mcp_servers``). Reading the real
    name from the source of truth lets this flag a SPECIFIC dead upstream by
    name, which a bare ``tools == 0`` aggregate count cannot: the 2026-08-19
    incident had exactly two dead upstreams while everything else kept the
    gateway's total tool count well above zero. ``expect_tools`` alone (no
    ``expected``) still catches the coarser total-collapse case.

    ``expected_profiles`` is the same idea one level up, for scoped profiles
    (``/mcp/<name>``) instead of upstreams: profile name -> the app slug whose
    ``contributes.agents[*].mcp_servers[*].profile`` names it (see
    ``_referenced_gateway_profiles``), compared against ``configs`` on the
    same ``/healthz``. A referenced profile the gateway does not serve is a
    404 per request and an agent with zero tools, and nothing else in this
    workspace reports it — aw-app-marketing's ``marketing`` profile has been
    in exactly that state since it shipped (measured live 2026-09-21).
    Like ``warm_redis``, a **missing** ``configs`` key reads as unknown rather
    than as "every profile is dead": it is only absent on a gateway old enough
    not to publish it, and the fleet is not version-locked.

    ``expected_versions`` maps upstream/server name -> the version in that
    app's ``aw-app.json`` **on disk right now**, compared against the version
    the gateway reports having actually DIALED that upstream at
    (``upstream_app_versions`` on ``/healthz``). A mismatch means the gateway
    is still serving the tool list it cached before the app was updated, so a
    tool that shipped, deployed green and passed CI is invisible to every live
    session at once — and nothing else in this workspace reports it. From
    inside a session it is indistinguishable from the session-cache lesson
    (``verify-new-gateway-tools-in-same-session``), which is what sent the
    2026-09-29 diagnosis the wrong way. Same missing-key tolerance as
    ``warm_redis``: a gateway that predates the field reads as unknown, never
    as stale.

    Also folds in the gateway's own ``warm_redis`` block (v0.27.0+): an
    unresolvable/unreachable warm-token Redis breaks ``schedule_wakeup``,
    ``ask_human``, ``mark_flow_done``, ``supervise`` and callback dispatch for
    every warm session in the workspace, with otherwise zero doctor signal
    (confirmed live 2026-09-19 against crispal's hosted workspace). A
    **missing** ``warm_redis`` key — an older gateway that predates this
    field, and the hosted fleet is not version-locked — reads as unknown, not
    degraded; only an explicitly-present ``warm_redis.ok == False`` counts.
    """
    # NOT ``runtime.is_loaded``: that tracks apps with an in-process plugin, and
    # mcp-gateway is ``tier: container`` — it has none, so is_loaded() is False
    # for a gateway that is installed, running and serving 600+ tools. Gating on
    # it reintroduced exactly the bug this function exists to kill: doctor
    # printed "mcp-gateway app is not installed — nothing to check" and returned
    # 0 while two upstreams were dead (seen live 2026-08-19, right after the
    # first fix shipped). The container registry is the predicate that answers
    # "is this app actually here" for both tiers.
    import httpx
    try:
        base_url = runtime.containers.base_url("mcp-gateway")
    except ContainerError:
        return {"reachable": None, "degraded": False,
                "note": "mcp-gateway app is not installed"}
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"{base_url}/healthz")
            resp.raise_for_status()
            payload = resp.json()
    except Exception as e:  # noqa: BLE001 — the failure itself IS the finding
        log.warning("apps: doctor could not reach mcp-gateway /healthz: %s", e)
        return {"reachable": False, "degraded": True,
                "note": f"gateway unreachable at {base_url}/healthz: {e}"}
    tools = payload.get("tools", 0)
    upstreams = payload.get("local_upstreams") or []
    live = set(upstreams)
    dead = sorted(
        ({"server": name, "app": app} for name, app in (expected or {}).items()
         if name not in live),
        key=lambda d: (d["app"], d["server"]),
    )
    # ``configs`` absent == a gateway that predates publishing it; absent is
    # unknown, never "all dead". An empty LIST is a real answer (no profile is
    # served) and does flag every reference.
    live_configs = payload.get("configs")
    dead_profiles = sorted(
        ({"profile": name, "app": app}
         for name, app in (expected_profiles or {}).items()
         if name not in set(live_configs)),
        key=lambda d: (d["app"], d["profile"]),
    ) if isinstance(live_configs, list) else []
    zero_tools = expect_tools and tools == 0
    # ``warm_redis`` only exists from aw-mcp-gateway v0.27.0 onward (the block
    # on /healthz: {ok, url, source, reachable, tokens_seen_24h,
    # tokens_unresolved_24h}). The hosted fleet is not version-locked, so a
    # gateway that predates it is a normal, common case — a MISSING key must
    # read as unknown, never as degraded. Only an explicitly-present
    # ``warm_redis.ok == False`` (an unresolvable/unreachable warm-token
    # Redis, which silently breaks schedule_wakeup/ask_human/mark_flow_done/
    # supervise/callback dispatch for every warm session) counts.
    warm_redis = payload.get("warm_redis")
    warm_redis_bad = isinstance(warm_redis, dict) and warm_redis.get("ok") is False
    # ``upstream_app_versions`` only exists from aw-mcp-gateway v0.37.0 onward
    # ({name: {app, version}} on /healthz). A MISSING key reads as unknown —
    # the fleet is not version-locked and a gateway that predates the field is
    # a normal case — and so does a null ``version`` on either side (the app's
    # manifest was unreadable when the gateway scanned it). Only two versions
    # that are both known and different count, because only that combination
    # actually proves the gateway is serving a pre-update tool list.
    dialed_versions = payload.get("upstream_app_versions")
    stale = sorted(
        ({"server": name,
          "app": row.get("app") or (expected or {}).get(name),
          "dialed": row.get("version"),
          "installed": (expected_versions or {}).get(name)}
         for name, row in (dialed_versions or {}).items()
         if isinstance(row, dict) and row.get("version")
         and (expected_versions or {}).get(name)
         and row.get("version") != expected_versions[name]),
        key=lambda d: d["server"],
    ) if isinstance(dialed_versions, dict) else []
    degraded = (bool(dead) or bool(dead_profiles) or zero_tools or warm_redis_bad
                or bool(stale))
    if dead:
        note = ("%d upstream(s) declared but not live in the gateway: %s" %
                (len(dead), ", ".join(f"{d['server']} ({d['app']})" for d in dead)))
    elif dead_profiles:
        note = ("%d scoped profile(s) referenced by an installed app but NOT "
                "served by the gateway: %s — every agent scoped to one gets "
                "404 per request and starts with zero tools" %
                (len(dead_profiles),
                 ", ".join(f"{d['profile']} ({d['app']})" for d in dead_profiles)))
    elif warm_redis_bad:
        note = ("warm-token Redis unresolved/unreachable (source=%s) — "
                 "schedule_wakeup, list_wakeups, ask_human, mark_flow_done, "
                 "supervise and callback dispatch will fail for warm sessions"
                 % warm_redis.get("source", "unknown"))
    elif stale:
        note = ("%d upstream(s) still dialled at a superseded app version: %s "
                "— tools added by those updates are invisible to every live "
                "session until the gateway re-dials" %
                (len(stale),
                 ", ".join("%s at %s but %s is installed"
                           % (d["server"], d["dialed"], d["installed"]) for d in stale)))
    elif zero_tools:
        note = ("gateway reachable but serving ZERO tools despite apps "
                 "declaring mcp.json — at least one upstream is dead")
    elif expected:
        note = "gateway reachable; every declared upstream is live"
    else:
        note = ("gateway reachable; compare local_upstreams against apps_contributing_tools "
                 "for per-app drift (names don't match 1:1 — see docstring)")
    not_ready = _not_ready(dead=dead, dead_profiles=dead_profiles, stale=stale,
                           zero_tools=zero_tools, warm_redis_bad=warm_redis_bad)
    return {
        "reachable": True,
        "not_ready": not_ready,
        "tools": tools,
        "local_upstreams": upstreams,
        "dead_upstreams": dead,
        "configs": live_configs if isinstance(live_configs, list) else None,
        "dead_profiles": dead_profiles,
        "warm_redis": warm_redis,
        # None (not {}) when the gateway predates the field, so the CLI can
        # print "unknown" instead of implying every version matched.
        "upstream_app_versions": dialed_versions if isinstance(dialed_versions, dict) else None,
        "stale_upstreams": stale,
        "degraded": degraded,
        "note": note,
    }


def _not_ready(*, dead, dead_profiles, stale, zero_tools, warm_redis_bad) -> list[dict]:
    out: list[dict] = []
    for d in dead:
        out.append({"kind": "dead_upstream", "server": d["server"], "app": d["app"],
                    "reason": "declared by the app but not live in the gateway"})
    for d in dead_profiles:
        out.append({"kind": "dead_profile", "profile": d["profile"], "app": d["app"],
                    "reason": "scoped profile referenced but not served"})
    for d in stale:
        out.append({"kind": "stale_upstream", "server": d["server"], "app": d["app"],
                    "reason": "dialled at %s but %s is installed" % (d["dialed"], d["installed"])})
    if zero_tools and not dead:
        out.append({"kind": "zero_tools", "server": None, "app": None,
                    "reason": "gateway serving zero tools while apps declare mcp.json"})
    if warm_redis_bad:
        out.append({"kind": "warm_redis", "server": None, "app": None,
                    "reason": "warm-token Redis unresolved/unreachable"})
    return out


def is_ready(status: dict) -> bool:
    """Gateway counts as ready: not installed (``reachable is None``), or
    reachable with an empty ``not_ready``. Unreachable is never ready."""
    reach = status.get("reachable")
    if reach is None:
        return True
    return bool(reach) and not status.get("not_ready")


def collect_gateway_expectations(runtime) -> dict:
    """Keyword args for :func:`mcp_gateway_status`, derived from the apps on
    disk: ``expect_tools``, ``expected``, ``expected_profiles``,
    ``expected_versions``."""
    mcp_apps = sorted(
        slug for slug in runtime.loaded_slugs()
        if os.path.isfile(os.path.join(runtime.get(slug).package_dir, "mcp.json"))
    ) if runtime.loaded_slugs() else []

    # Server name -> owning app slug, read from each app's own mcp.json
    # (not guessed from the slug — see _mcp_gateway_status's docstring)
    # so a specific dead upstream can be named, not just inferred from a
    # zero total.
    mcp_expected: dict[str, str] = {}
    # Server name -> the owning app's version ON DISK right now, for the
    # version-skew check: the gateway reports which version it actually
    # dialled each upstream at, and a difference means it is still serving
    # that upstream's pre-update tool list.
    mcp_expected_versions: dict[str, str] = {}
    for slug in mcp_apps:
        loaded = runtime.get(slug)
        try:
            with open(os.path.join(loaded.package_dir, "mcp.json"), encoding="utf-8") as f:
                doc = json.load(f)
        except (OSError, ValueError):
            continue
        # Read from aw-app.json rather than loaded.manifest.version so
        # both sides of the comparison read the SAME file: the gateway
        # mounts this exact directory read-only and takes its own dialled
        # version from it. An in-memory manifest is a second source that
        # can differ from disk for reasons that have nothing to do with
        # the gateway being stale.
        try:
            with open(os.path.join(loaded.package_dir, "aw-app.json"), encoding="utf-8") as f:
                installed_version = str((json.load(f) or {}).get("version") or "").strip()
        except (OSError, ValueError):
            installed_version = ""
        for name, spec in (doc.get("mcpServers") or {}).items():
            if isinstance(spec, dict) and spec.get("enabled") is False:
                continue
            mcp_expected[name] = slug
            if installed_version:
                mcp_expected_versions[name] = installed_version

    return {
        "expect_tools": bool(mcp_apps),
        "expected": mcp_expected,
        "expected_profiles": referenced_gateway_profiles(runtime),
        "expected_versions": mcp_expected_versions,
    }


async def gateway_readiness(runtime) -> dict:
    """One-call readiness for a probe: expectations + status."""
    return await mcp_gateway_status(runtime, **collect_gateway_expectations(runtime))
