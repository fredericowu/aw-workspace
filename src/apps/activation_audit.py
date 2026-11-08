"""The activation audit (design doc §A.2,
``docs/design/service-supervision-activation-audit.md``) — the structural
half of "a silent activate-skip is impossible".

§B/§C/§D of that doc (the ``ServiceSupervisor`` restart loop, its backoff and
sticky give-up, and the ``/api/health`` degraded surface) are already shipped
(``ba82486``, ``f333bc8``) and are NOT touched here. What they cannot catch is
the 2026-10-11 incident itself: a service whose ``activate()`` never ran has
no ``Popen`` for the restart loop to observe in the first place. Liveness and
activation ACCOUNTING are different problems; this module is the second one.

Two checks, both asserting END-STATE rather than trusting that the right code
path ran:

* **I1** — every app in ``desired_active`` is loaded
  (``AppRuntime.is_loaded``). A miss gets exactly one repair attempt through
  the full ``install()`` path (never a bare ``runtime.load`` — see the
  design's "Risks for the Coders": a partial repair can hit journaled
  residue that only ``install()``'s revert-on-failure cleans up).
* **I2** — every autostart service is running locally, running on another
  worker's lease, or in an explicit exempt state (``gave_up``, or
  ``desired=stopped``). Anything else gets a lease-arbitrated
  ``ServiceSupervisor.start()`` — a race with another worker resolves to the
  existing logged no-op, by construction.

Desired-state for I2 comes SOLELY from :func:`src.apps.degradations.degradations`
— the same choke point ``doctor`` and ``/api/health`` already read — not a
second, parallel computation that could drift from it. I1's desired-state
comes from :meth:`Reconciler._resolve_desired_active`, the identical
cloud-or-local resolution a reconcile pass itself starts from, for the same
reason.

Runs (a) at the end of every reconcile pass (``Reconciler.reconcile``), and
(b) on its own ~60s leader-gated watchdog tick (``src/apps/routes.py``).
Skips entirely while THIS worker is mid-``_provisioning()``
(:attr:`Reconciler.is_provisioning`) — the one correctness requirement named
in the design: an upgrade is uninstall-then-install, and the audit firing
between the two halves would see a desired app as "not loaded" and
repair-install it mid-upgrade. Capped at :data:`_MAX_REPAIRS_PER_RUN` repairs
(I1+I2 combined) per run; anything past the cap is named in ``errors``, never
acted on — the bound on how much damage a bug in this very module can do.

**The LOUD escalation, and the failure mode it exists to not repeat.** A
service reaching ``gave_up``, or a repair attempt itself failing, used to
only ever reach a human through the existing 4-hourly degradation-watch task
(``agentic_output``, dispatches its agent only on a notable exit code — i.e.
only in the bad case, which is exactly the untested path). That dispatch
mechanism failed silently for 5 days as recently as the day this module was
written (``system-analyst``, card ``40f5bf3b-9510-8106``) — so routing the
loud signal SOLELY through it would have reproduced, one layer up, the exact
silent-failure class this whole design exists to kill. The fix chosen here:
a failed escalation attempt is ITSELF recorded as a degradation
(:func:`src.apps.service_lease.set_escalation_failed`, read back by
:func:`src.apps.degradations.escalation_rows`) — the same pull surface
(``doctor``, ``/api/health``) every other degradation in this workspace
already uses, rather than a second, novel kind of silence. Debounced to one
attempt per 15 minutes workspace-wide (:func:`acquire_escalation_debounce`)
so a boot storm of give-ups costs one page, not N, and fired from a detached
``asyncio.create_task`` so a slow/hanging agents-platform round trip
(documented up to 30 minutes) can never stall the audit tick that found it.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import TYPE_CHECKING, Any

from src.apps.degradations import degradations
from src.apps.service_lease import (_get_shared_client, acquire_escalation_debounce,
                                    clear_escalation_failed, set_escalation_failed)
from src.libs.redis_coord import _key_prefix

if TYPE_CHECKING:  # pragma: no cover
    from src.apps.reconciler import Reconciler
    from src.apps.runtime import AppRuntime

log = logging.getLogger(__name__)

#: I1 + I2 repairs, combined, in ONE run. The bound on how much a bug in this
#: module (or a genuinely bad boot) can do per ~60s tick — everything past it
#: is named in ``errors`` and left for the next tick, never acted on twice as
#: hard. Not per-app-id: a workspace with a bad day should degrade to "the
#: audit catches up over a few minutes", not "the audit tries to fix
#: everything at once and makes the concurrent-repair failure mode worse".
_MAX_REPAIRS_PER_RUN = 5

#: One escalation attempt per this many seconds, workspace-wide — see the
#: module docstring's "LOUD escalation" section.
_ESCALATION_DEBOUNCE_S = 900.0

#: Soft-resolved by NAME (never a hardcoded id — a task id is per-workspace,
#: created once by a human or a prior card), so an environment missing the
#: task, or the tasks app entirely, is a one-line WARNING and a no-op rather
#: than an error. Overridable because the exact wording of the existing task
#: name is this workspace's own choice, not a framework constant.
_ESCALATION_TASK_NAME_ENV = "AW_ACTIVATION_AUDIT_ESCALATION_TASK_NAME"
_DEFAULT_ESCALATION_TASK_NAME = "Workspace doctor — degradation watch"

_LAST_RUN_KEY = f"{_key_prefix()}activation-audit:last-run"

#: This worker's own clock, used when Redis is away — degrades the staleness
#: check to "has THIS process run it lately" instead of "has ANY worker",
#: same shape as every other degrade-open fallback in this subsystem.
_last_run_at_inprocess: float | None = None


def _stamp_last_run() -> None:
    global _last_run_at_inprocess
    now = time.time()
    _last_run_at_inprocess = now
    try:
        _get_shared_client().set(_LAST_RUN_KEY, str(now))
    except Exception:
        log.warning("activation_audit: could not persist the last-run stamp to "
                    "Redis — doctor's staleness check falls back to this "
                    "worker's own memory", exc_info=True)


def last_run_age_s() -> float | None:
    """Seconds since the audit last completed a run, or ``None`` if it has
    never run at all (yet) — read by ``doctor`` so the audit itself cannot
    rot silently the way the doctor-watch task once did (disabled 2026-09-09,
    noticed 2026-10-11)."""
    try:
        raw = _get_shared_client().get(_LAST_RUN_KEY)
        if raw:
            return max(0.0, time.time() - float(raw))
    except Exception:
        log.warning("activation_audit: could not read the last-run stamp from "
                    "Redis — falling back to this worker's own memory",
                    exc_info=True)
    if _last_run_at_inprocess is None:
        return None
    return max(0.0, time.time() - _last_run_at_inprocess)


async def _fire_escalation(runtime: "AppRuntime", reasons: list[str]) -> None:
    """Best-effort "run now" of the configured degradation-watch task.

    Always called via ``asyncio.create_task`` — never awaited by
    :func:`run_once` itself — because ``agents_platform_client``'s own
    documented timeout runs up to 30 minutes, and an audit tick that blocks
    on that is worse than the problem it is trying to report. Every
    exception is swallowed HERE, not propagated: this function is the
    fire-and-forget boundary.
    """
    task_name = os.environ.get(_ESCALATION_TASK_NAME_ENV) or _DEFAULT_ESCALATION_TASK_NAME
    reason_text = "; ".join(reasons)
    tasks_app = runtime.get("tasks")
    plugin = getattr(tasks_app, "plugin", None) if tasks_app is not None else None
    manager = getattr(plugin, "manager", None)
    store = getattr(plugin, "store", None)
    if manager is None or store is None:
        log.warning(
            "activation_audit: the tasks app is not installed/loaded — cannot "
            "run-now the degradation-watch escalation for: %s. The existing "
            "4-hourly schedule remains the only floor.", reason_text)
        return
    try:
        task = next((t for t in store.list() if t.get("name") == task_name), None)
    except Exception:
        log.warning("activation_audit: could not read the tasks store to resolve "
                    "the escalation task", exc_info=True)
        return
    if task is None:
        log.warning(
            "activation_audit: no task named %r — cannot run-now the "
            "degradation-watch escalation for: %s. The existing 4-hourly "
            "schedule remains the only floor. Set %s to override the name.",
            task_name, reason_text, _ESCALATION_TASK_NAME_ENV)
        return
    log.warning("activation_audit: escalating — running %r now (reasons: %s)",
               task_name, reason_text)
    try:
        run = await manager.run_task(task["id"], trigger="manual")
    except Exception as e:  # noqa: BLE001 — this IS the fire-and-forget boundary
        set_escalation_failed(reason="dispatch-raised", detail=str(e)[:500])
        log.error("activation_audit: the escalation dispatch raised — this "
                  "failure is now itself a degradation on doctor and "
                  "/api/health", exc_info=True)
        return
    if run.get("status") == "ok":
        clear_escalation_failed()
    else:
        detail = str(run.get("error") or "task run did not complete ok")[:500]
        set_escalation_failed(reason="task-run-error", detail=detail)
        log.error("activation_audit: the escalation dispatch did not succeed "
                  "(%s) — this failure is now itself a degradation on doctor "
                  "and /api/health", detail)


async def run_once(reconciler: "Reconciler", runtime: "AppRuntime") -> dict[str, Any]:
    """One audit pass: I1 + I2 + the LOUD escalation. Safe to call repeatedly
    and concurrently with itself is NOT assumed — callers (the watchdog tick,
    the end-of-reconcile-pass hook) are each individually reentrancy-guarded;
    this function itself does not guard against overlap.
    """
    if reconciler.is_provisioning:
        return {"skipped": "provisioning-in-flight"}

    repairs: list[dict] = []
    errors: list[dict] = []
    repair_budget = _MAX_REPAIRS_PER_RUN

    # ---- I1: every desired app is loaded --------------------------------
    desired_active, source = await reconciler._resolve_desired_active()
    apps_missing = [app_id for app_id in desired_active if not runtime.is_loaded(app_id)]
    for app_id in apps_missing:
        if repair_budget <= 0:
            errors.append({"kind": "app", "app": app_id,
                          "error": "repair cap reached this run — named, not acted on"})
            continue
        repair_budget -= 1
        try:
            await reconciler.install(desired_active[app_id], write_cloud=False,
                                     sync_skill_mirrors=False)
            repairs.append({"kind": "app", "app": app_id, "action": "install"})
            log.warning("activation_audit: I1 repaired %s — it was desired and "
                       "not loaded; installed", app_id)
        except Exception as e:  # noqa: BLE001 — one bad app must not sink the run
            errors.append({"kind": "app", "app": app_id, "action": "install",
                          "error": str(e)})
            log.exception("activation_audit: I1 repair of %s failed", app_id)

    # ---- I2: every autostart service is accounted for --------------------
    rows = degradations(runtime)
    service_rows = [r for r in rows if r.get("kind") == "service"]
    gave_up_rows = [r for r in service_rows if r.get("state") == "gave_up"]
    for row in service_rows:
        # Exempt states (design §A.2): gave_up and a deliberate stop are
        # reported, never acted on — the audit must not become the
        # unconditional respawner the restart loop's own give-up exists to
        # prevent.
        if row.get("state") == "gave_up" or row.get("desired") == "stopped":
            continue
        app_id, service_id = row["app"], row["service"]
        if repair_budget <= 0:
            errors.append({"kind": "service", "app": app_id, "service": service_id,
                          "error": "repair cap reached this run — named, not acted on"})
            continue
        repair_budget -= 1
        try:
            # manual=False: the respawn path's own entry point. Preserves
            # the crash-loop budget (consecutive_fast_exits) instead of
            # resetting it the way a deliberate human start would — an audit
            # tick is not a human saying "I just fixed this", and resetting
            # the counter every ~60s would mean a service crash-looping
            # slower than that NEVER reaches its sticky give-up.
            status = await asyncio.to_thread(
                runtime.services.start, app_id, service_id, manual=False)
            repairs.append({"kind": "service", "app": app_id, "service": service_id,
                          "action": "start", "result": status.get("state")})
            log.warning("activation_audit: I2 repaired %s/%s — desired=running "
                       "but state=%s; issued a lease-arbitrated start",
                       app_id, service_id, row.get("state"))
        except Exception as e:  # noqa: BLE001
            errors.append({"kind": "service", "app": app_id, "service": service_id,
                          "action": "start", "error": str(e)})
            log.exception("activation_audit: I2 repair of %s/%s failed",
                          app_id, service_id)

    _stamp_last_run()

    # ---- LOUD escalation ---------------------------------------------------
    reasons: list[str] = [
        f"{r['app']}/{r['service']} gave up ({r.get('reason', '?')})"
        for r in gave_up_rows
    ]
    for e in errors:
        if e.get("action") not in ("install", "start"):
            continue
        label = f"{e['app']}/{e['service']}" if e.get("service") else e["app"]
        reasons.append(f"{label} repair failed: {e.get('error', '?')}")
    for e in errors:
        if "repair cap reached" not in e.get("error", ""):
            continue
        label = f"{e['app']}/{e['service']}" if e.get("service") else e["app"]
        reasons.append(f"{label}: repair cap reached this run")

    escalated: Any = False
    if reasons:
        if acquire_escalation_debounce(_ESCALATION_DEBOUNCE_S):
            asyncio.create_task(_fire_escalation(runtime, reasons))
            escalated = True
        else:
            escalated = "debounced"

    summary = {
        "source": source,
        "apps_checked": len(desired_active), "apps_missing": len(apps_missing),
        "services_checked": len(service_rows), "repairs": repairs, "errors": errors,
        "escalated": escalated,
    }
    # One line every run, including on zero findings — the staleness of THIS
    # line is what doctor checks for, so it must never be made conditional.
    log.info("activation audit: %d app(s), %d service(s), %d repair(s), %d error(s)",
             summary["apps_checked"], summary["services_checked"],
             len(repairs), len(errors))
    return summary
