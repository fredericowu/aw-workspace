"""``doctor``'s verdict on a component that is configured ON and is not
running — the label, not the detection.

Detection has been covered since 2026-10-01
(``src/tests/unit/apps/test_doctor_autostart.py``). The label was wrong: it
read "deliberate or not" off ``state == "stopped"``, i.e. off the mere
absence of a crash, which says nothing about intent. On 2026-10-11 a
``proxy:proxy-server`` whose start had been silently declined and never
retried was reported as "deliberately stopped (not a crash) — nothing will
restart it", and ``doctor`` is the one tool in this workspace that catches
silent degradation. Telling an operator an outage was on purpose is worse
than saying nothing.

So the label is pinned here against ``desired`` — the supervisor's intent —
and against the one thing that qualifies it: ``desired`` is per-worker memory
(``supervised_here``), and at ``AW_WORKSPACE_WORKERS=5`` four out of five
answers come from a worker that never ran the process.
"""
from src.cli.commands import doctor

_DELIBERATE = "deliberately stopped"
_BROKEN = "BROKEN, NOT deliberate"
_UNEXPLAINED = "never run it"


def _row(**over):
    row = {"app": "proxy", "tier": "inprocess", "component": "proxy:proxy-server",
           "state": "stopped", "desired": "stopped", "supervised_here": True}
    row.update(over)
    return row


def _render(capsys, *rows):
    problems = doctor._autostart_not_running(list(rows))
    return problems, capsys.readouterr().out


def test_a_service_nobody_stopped_is_reported_as_broken(capsys):
    """``desired == running`` and not running: the supervisor still wants this
    up, so nobody stopped it. This is the exact state the ghost-lease bug
    left ``proxy:proxy-server`` in."""
    problems, out = _render(capsys, _row(desired="running"))

    assert problems == 1
    assert _BROKEN in out
    assert _DELIBERATE not in out, \
        "doctor told the operator a live outage was intentional"
    assert "restart it and read the logs" in out


def test_a_service_someone_stopped_is_still_reported_as_deliberate(capsys):
    """The inverse must keep working, or every switched-off component starts
    reading as an incident and the section becomes noise."""
    problems, out = _render(capsys, _row(desired="stopped", supervised_here=True))

    assert problems == 1
    assert _DELIBERATE in out
    assert _BROKEN not in out


def test_a_worker_that_never_ran_it_does_not_claim_intent(capsys):
    """``desired`` lives in the memory of whichever worker ran the process.
    A worker that never did has an untouched default, not a record — so it
    reports the component as off-and-unexplained rather than dressing its own
    default up as somebody's decision."""
    problems, out = _render(capsys, _row(desired="stopped", supervised_here=False))

    assert problems == 1
    assert _UNEXPLAINED in out
    assert _DELIBERATE not in out
    assert _BROKEN not in out


def test_the_supervisors_own_verdict_still_wins_over_desired(capsys):
    """``backing_off`` and ``gave_up`` both imply ``desired == running``, and
    both say something more specific and more actionable than "broken" —
    recovery is in flight, or it is definitively not."""
    _, backing = _render(capsys, _row(state="backing_off", desired="running"))
    assert "retrying this with backoff" in backing
    assert _BROKEN not in backing

    _, gave_up = _render(
        capsys, _row(state="gave_up", desired="running", reason="crash-loop"))
    assert "GAVE UP on it (crash-loop)" in gave_up
    assert _BROKEN not in gave_up


def test_a_container_row_gets_no_intent_label(capsys):
    """Only a managed service has a supervisor with an intent to report; a
    Tier-2 container row carries no ``desired`` and must not be guessed at."""
    problems, out = _render(
        capsys, {"app": "browser", "tier": "container", "component": "browser",
                 "state": "off"})

    assert problems == 1
    assert _DELIBERATE not in out
    assert _BROKEN not in out
    assert _UNEXPLAINED not in out


def test_nothing_degraded_prints_nothing(capsys):
    problems, out = _render(capsys)
    assert problems == 0
    assert out == ""
