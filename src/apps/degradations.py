"""The one cheap "is anything silently off right now" check, shared by
``doctor`` and ``/api/health``.

``doctor`` has had this check since 2026-10-01 (``_autostart_not_running``,
``src/apps/routes.py``) and it is the only thing that has ever caught the
failure it exists for: an app whose config says ``auto_start: true`` with
nothing actually running. But nothing runs ``doctor`` proactively, so on
2026-10-11 a dead ``proxy:proxy-server`` took every browser-automation MCP
family down for ≥27 minutes while ``/api/health`` answered a hardcoded
``{"status": "ok"}`` throughout.

This module is that check extracted so **both** surfaces consume one source
of truth (design §C). The split of responsibilities is deliberate:

* :func:`degradations` is the primitive — ``poll()`` per registered service
  (a ``waitpid`` syscall), one Redis ``EXISTS`` per non-running service's
  lease, one Redis ``MGET`` over give-up markers. Microseconds plus ~1–2ms.
* :func:`health_report_cached` projects it into ``/api/health``'s shape
  behind a ~5s in-process cache, because uptime monitors and container
  healthchecks poll that endpoint every few seconds.

**What this is NOT** is ``doctor``. The full doctor sweep shells out to
system CLIs, probes podman, and HTTP-calls every installed app's
``contributes.doctor`` route — hundreds of milliseconds to seconds of
fan-out. Calling it from ``/api/health`` would turn monitoring into load and
make health itself a denial-of-service surface. ``doctor`` stays the superset
(CLI presence, container state, app self-check routes) and consumes this;
health consumes only this.

**Degrades open, always.** Redis away means the lease and marker lookups
fail; the answer falls back to this worker's own process table and says so
(``health_scope: "local-worker"``). Health must never fail because its own
dependency blinked.
"""
from __future__ import annotations

import logging
import os
import threading
import time

from src.apps.containers import ContainerError
from src.apps.service_lease import gave_up_markers

log = logging.getLogger(__name__)

#: Long enough that a healthcheck polling every second costs one real check
#: per five, short enough that a human watching a service recover sees it
#: within one refresh.
_CACHE_TTL_S = 5.0

#: Keys only ``doctor`` wants — ``/api/health`` is a status surface, not a
#: debugging one, and a process's last stderr lines have no business in a
#: payload an uptime monitor scrapes. ``desired`` is deliberately NOT in here:
#: "this component is off and nobody meant it to be" is exactly what a monitor
#: needs to tell apart from a component somebody switched off.
#: ``supervised_here`` is, though — it only qualifies how much the answering
#: worker's ``desired`` can be trusted, which is a debugging detail.
_DOCTOR_ONLY_KEYS = ("last_exit_code", "last_error", "component", "tier",
                     "supervised_here")

_cache_lock = threading.Lock()
_cache: dict | None = None
_cache_at: float = 0.0


def _autostart_apps(runtime) -> list[tuple[str, object]]:
    """``(slug, loaded)`` for apps that have something to start AND are
    configured to start it.

    Only apps that actually have something to start: the framework adds
    ``auto_start`` for managed apps, and an in-process app declares its own
    when it supervises a service (aw-app-proxy's proxy-server). An app with
    ``auto_start`` explicitly off is a legitimate choice, reported separately
    by ``doctor``'s ``_autostart_disabled`` — never double-reported here.
    """
    out: list[tuple[str, object]] = []
    for slug in runtime.loaded_slugs():
        loaded = runtime.get(slug)
        if loaded is None:
            continue
        props = (loaded.manifest.effective_config_schema or {}).get("properties") or {}
        if "auto_start" not in props:
            continue
        if not (loaded.config or {}).get("auto_start", True):
            continue
        out.append((slug, loaded))
    return out


def broadcast_rows(runtime) -> list[dict]:
    """``apps:changed`` fan-out that is down on THIS worker — at >1 workers.

    Reported per-worker, which is the only honest scope available: whether
    this process can hear a broadcast is a fact about this process, and
    ``/api/health`` is answered by whichever worker got the request. So a
    fleet with one deaf worker shows the row roughly 1-in-N polls. That reads
    as flaky until you know why, and it is still strictly better than the
    status quo this replaces, which was zero signal anywhere: a worker whose
    relay died kept answering ``200 ok`` while serving app config from before
    the last save (card 3fc5bf3b, measured 1-in-5 fresh).

    Silent at ``AW_WORKSPACE_WORKERS<=1``: with one worker there is nobody to
    converge with, the inline hook in ``save_app_config`` is the whole of
    correctness, and a dead broadcast costs nothing — exactly the posture
    ``src/apps/lifecycle.py`` documents.
    """
    if int(os.environ.get("AW_WORKSPACE_WORKERS", "1") or "1") <= 1:
        return []
    # Reached through the runtime's own FastAPI app (``AppRuntime.host``)
    # rather than taken as an argument, so every existing caller of
    # ``degradations()``/``health_report()`` — including the unit tests' fake
    # runtimes, which have no ``host`` — keeps working unchanged.
    state = getattr(getattr(runtime, "host", None), "state", None)
    lifecycle = getattr(state, "app_lifecycle", None)
    health = getattr(lifecycle, "broadcast_health", None)
    if not callable(health):
        return []
    report = health()
    if report.get("started") and report.get("relay_alive"):
        return []
    reason = ("never subscribed — Redis was unreachable when this worker booted"
              if not report.get("started")
              else "the relay task is down and reconnecting")
    # Shaped like a service row on purpose: ``app``/``service``/``reason``
    # survive health's projection (``_DOCTOR_ONLY_KEYS`` drops ``component``
    # and ``last_error``), so a monitor reading /api/health still gets a
    # self-describing row, while doctor gets the pid-level detail.
    return [{
        "kind": "broadcast", "app": "core", "tier": "core",
        "service": "apps:changed", "component": "core:apps:changed",
        "state": "off", "desired": "running", "autostart": True,
        "reason": reason,
        "last_error": (f"this worker (pid {os.getpid()}) will not be told when "
                       f"another worker installs, removes or reconfigures an app; "
                       f"relay reconnects so far: {report.get('relay_failures', 0)}"),
    }]


def service_relay_rows(runtime) -> list[dict]:
    """``ServiceCommandRelay`` forwarding that is down on THIS worker — at
    >1 workers. Same shape, same per-worker scope and same rationale as
    :func:`broadcast_rows` above, for a DIFFERENT ``RedisBroadcaster``
    instance: ``src/apps/service_relay.py``'s ``ServiceCommandRelay`` wraps
    its own, separate from ``AppLifecycle``'s, so a dead or stuck relay here
    is invisible to a check that only reads the other.

    Found live (card 4045bf3b-9510-814a): a forwarded restart/logs request
    for ``research-search:paper-search`` always burned the relay's full
    timeout and silently fell back to a safe local no-op, 7x over several
    minutes, while the owning worker's service lease stayed healthy
    throughout (that heartbeat runs on a separate thread, so it only proves
    the *process* was fine, not the relay). Nothing — not this service's own
    health, not ``doctor``'s autostart check, not ``/api/health`` — ever
    looked wrong, because only the forwarded command's own round-trip could
    see the gap, and nothing was watching that round-trip until now.
    """
    if int(os.environ.get("AW_WORKSPACE_WORKERS", "1") or "1") <= 1:
        return []
    state = getattr(getattr(runtime, "host", None), "state", None)
    relay = getattr(state, "service_relay", None)
    health = getattr(relay, "relay_health", None)
    if not callable(health):
        return []
    report = health()
    if report.get("started") and report.get("relay_alive"):
        return []
    reason = ("never subscribed — Redis was unreachable when this worker booted"
              if not report.get("started")
              else "the relay task is down (or stuck) and reconnecting")
    return [{
        "kind": "broadcast", "app": "core", "tier": "core",
        "service": "service-relay", "component": "core:service-relay",
        "state": "off", "desired": "running", "autostart": True,
        "reason": reason,
        "last_error": (f"this worker (pid {os.getpid()}) cannot forward a manual "
                       f"service start/stop/restart/logs request to the worker "
                       f"that actually owns the process — it falls back to a "
                       f"safe local no-op instead; relay reconnects so far: "
                       f"{report.get('relay_failures', 0)}"),
    }]


def _collect(runtime) -> tuple[list[dict], bool]:
    """``(rows, redis_reachable)`` — the whole check, once.

    Canonical row shape; each consumer drops what it does not want::

        {"kind": "service", "app": "proxy", "tier": "inprocess",
         "service": "proxy-server", "component": "proxy:proxy-server",
         "state": "gave_up", "desired": "running", "supervised_here": True,
         "autostart": True, "reason": "crash-loop",
         "last_exit_code": 1, "last_error": "..."}

    ``state`` is the supervisor's own, and ``off`` when it has none to offer
    (a Tier-2 container row reads the same way): ``off`` | ``backing_off`` |
    ``gave_up`` | ``stopped``. ``backing_off`` means the restart loop is
    already on it; ``gave_up`` means it is not, and will not be, without a
    human (``src/apps/services.py``).

    ``desired`` is the other half of that, and the half a consumer must not
    skip: ``state == "stopped"`` with ``desired == "running"`` is a service
    nobody stopped and nothing is bringing back — broken, not switched off.
    Services only; a container row has no supervisor intent to report.
    """
    rows: list[dict] = []
    for slug, loaded in _autostart_apps(runtime):
        tier = loaded.manifest.tier
        for app_id, service_id in runtime.services.registered():
            if app_id != slug:
                continue
            status = runtime.services.status(app_id, service_id)
            if status.get("running"):
                continue
            row = {
                "kind": "service", "app": slug, "tier": tier,
                "service": service_id, "component": f"{app_id}:{service_id}",
                "state": status.get("state") or "off", "autostart": True,
                # The supervisor's INTENT, which is what separates "someone
                # switched this off" from "this is broken" — never the absence
                # of a crash, which is what doctor used to read it from and
                # which says nothing at all (see the label in
                # ``src/cli/commands/doctor.py``).
                "desired": status.get("desired"),
                "supervised_here": bool(status.get("supervised_here")),
                "last_exit_code": status.get("last_exit_code"),
                "last_error": status.get("last_error"),
            }
            if status.get("give_up_reason"):
                row["reason"] = status["give_up_reason"]
            rows.append(row)
        for app_id, _container in runtime.containers.registered():
            if app_id != slug:
                continue
            try:
                status = runtime.containers.status(app_id)
            except ContainerError:
                # Not "fine": the engine cannot currently reach the
                # container, which is the same silent gap with a different
                # exception instead of a clean running: False.
                status = {"running": False}
            if status.get("running"):
                continue
            rows.append({
                "kind": "container", "app": slug, "tier": tier,
                "service": None, "component": app_id,
                "state": "off", "autostart": True,
            })

    # A give-up is decided by whichever worker owned the process and lives in
    # that worker's memory plus a durable Redis marker. Any OTHER worker —
    # including whichever one serves this request — has only the marker, so
    # without this a crash-looped service reads as a plain "off" and the
    # ERROR explaining it sits in another process's logs.
    pairs = [(r["app"], r["service"]) for r in rows if r["kind"] == "service"]
    markers, redis_ok = gave_up_markers(pairs)
    for row in rows:
        marker = markers.get((row["app"], row.get("service")))
        if not marker:
            continue
        row["state"] = "gave_up"
        row.setdefault("reason", marker.get("reason") or "unknown")
        if row.get("last_exit_code") is None and marker.get("exit_code") is not None:
            row["last_exit_code"] = marker["exit_code"]
        if not row.get("last_error") and marker.get("detail"):
            row["last_error"] = marker["detail"]

    # Appended after the give-up pass above, which keys on (app, service) and
    # has nothing to say about a broadcast row.
    rows.extend(broadcast_rows(runtime))
    rows.extend(service_relay_rows(runtime))
    return rows, redis_ok


def degradations(runtime) -> list[dict]:
    """Every component that is supposed to be running and is not — the
    canonical rows (see :func:`_collect`). ``doctor``'s source."""
    if runtime is None:
        return []
    return _collect(runtime)[0]


def health_report(runtime) -> dict:
    """``/api/health``'s fragment: ``{"status", "degraded"[, "health_scope"]}``.

    Fails open to ``status: "ok"`` on an unexpected error rather than
    propagating it: a 500 from the health endpoint makes a container
    orchestrator restart core — the exact restart that caused the outage this
    whole mechanism exists to end.
    """
    if runtime is None:
        return {"status": "ok", "degraded": []}
    try:
        rows, redis_ok = _collect(runtime)
    except Exception:
        log.exception("apps: the degradation check failed — reporting health as ok "
                      "rather than failing the endpoint")
        return {"status": "ok", "degraded": []}
    for row in rows:
        for key in _DOCTOR_ONLY_KEYS:
            row.pop(key, None)
    out = {"status": "degraded" if rows else "ok", "degraded": rows}
    if not redis_ok:
        # Honest about the blind spot rather than silently narrower: with
        # Redis away this worker cannot see another worker's leases or
        # give-up markers, so a service running elsewhere can appear here as
        # degraded and a give-up decided elsewhere is invisible.
        out["health_scope"] = "local-worker"
    return out


def health_report_cached(runtime, *, ttl: float = _CACHE_TTL_S) -> dict:
    """:func:`health_report` behind a short process-wide cache."""
    global _cache, _cache_at
    now = time.monotonic()
    with _cache_lock:
        if _cache is not None and (now - _cache_at) < ttl:
            return _cache
    report = health_report(runtime)
    with _cache_lock:
        _cache = report
        _cache_at = time.monotonic()
    return report


def invalidate_cache() -> None:
    """Drop the cached answer — called by anything that deliberately changes
    a service's state, so a human who just clicked Start does not read a
    5s-stale "degraded" back."""
    global _cache, _cache_at
    with _cache_lock:
        _cache = None
        _cache_at = 0.0
