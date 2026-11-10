"""Pins the GATE half of card 4145bf3b's SET+CLEAR+GATE check: a non-empty
``knowledge_mirrors`` row set must flip ``aw-workspace-cli doctor``'s own
EXIT CODE, not merely appear in the output body.

Two prior failures on this exact degradation-signal shape make that
distinction load-bearing rather than pedantic:

* a doctor row that reaches the JSON body without flipping ``ok`` reads as
  healthy to anything gating on the exit code (CI, an alert, a scheduled
  check) even though a human reading the text output would see it — the
  §A.2 escalation-dispatch bug fixed in PR #19 / d3353c3;
* ``cmd | tail`` returns ``tail``'s exit code, not ``doctor``'s — these
  tests call ``doctor.run()`` directly and read its return value, never a
  pipeline, to avoid reproducing that exact mistake here.

The payload shape mirrors ``/api/apps/-/doctor``'s real response, minimised
to the fields each CLI-side renderer actually reads (see
``src/cli/commands/doctor.py``), with ``ok`` wired the same way
``src/apps/routes.py``'s ``doctor()`` route wires it:
``not knowledge_mirrors`` folded into the overall boolean.
"""
import inspect
import re

from src.cli import local_client
from src.cli.commands import doctor as doctor_cli


def test_the_real_ok_tuple_in_routes_py_folds_in_knowledge_mirror_problems():
    """The CLI-level tests below fabricate their own ``report`` dict, so a
    regression where ``src/apps/routes.py``'s ``doctor()`` route computes
    ``knowledge_mirror_problems`` but forgets to fold it into the real
    ``"ok"`` tuple — exactly the §A.2 escalation-dispatch shape fixed in PR
    #19 / d3353c3, where a row reached the payload without flipping ``ok``
    — would pass every test below without ever touching production code.
    This reads the real source and pins the fold directly, the same
    technique used by this repo's own CheckHint for that earlier bug
    (``degraded:doctor-json-ok-excludes-state-facade-bypass``)."""
    import src.apps.routes as routes_mod
    source = inspect.getsource(routes_mod)
    match = re.search(r'"ok": \(.*?\),\n', source, re.S)
    assert match is not None, "doctor()'s ok tuple was not found in src/apps/routes.py"
    assert "knowledge_mirror_problems" in match.group(0)


def _report(knowledge_mirrors):
    return {
        "ok": not knowledge_mirrors,
        "knowledge_mirrors": knowledge_mirrors,
        "system_clis": {"total": 0, "unhealthy": []},
        "permissions": [],
        "app_checks": [],
        "autostart": [],
        "autostart_not_running": [],
        "pending_migrations": [],
        "host_power": {"host_offers": [], "summary": "none", "apps": [],
                        "unused": [], "unmet_optional": []},
        "mcp": {"apps_contributing_tools": [], "degraded": False, "reachable": None},
        "redis": {},
        "vault": {"configured": False},
        "state_facade": {},
        "remote_host_postgres_backup": {"present": True, "stale": False,
                                         "age_hours": 1.0, "slot": "x"},
    }


def _run(monkeypatch, report, *extra_args):
    monkeypatch.setattr(local_client, "request", lambda *a, **k: (200, report))
    return doctor_cli.run(list(extra_args))


def test_a_broken_mirror_fails_the_json_exit_code(monkeypatch, capsys):
    report = _report([{"app": "ssh", "reason": "mirror missing"}])
    exit_code = _run(monkeypatch, report, "--json")
    capsys.readouterr()
    assert exit_code == 1


def test_a_healthy_mirror_set_passes_the_json_exit_code(monkeypatch, capsys):
    report = _report([])
    exit_code = _run(monkeypatch, report, "--json")
    capsys.readouterr()
    assert exit_code == 0


def test_text_mode_exit_code_also_reflects_the_broken_mirror(monkeypatch, capsys):
    """The non-``--json`` path computes its own exit code from the sum of
    each section's problem count, independently of ``ok`` — this pins that
    ``knowledge_mirrors`` is wired into THAT sum too, not only the
    ``--json`` early return, and that the row text actually names the app
    and the reason."""
    report = _report([{"app": "ssh", "reason": "mirror missing"}])
    exit_code = _run(monkeypatch, report)
    out = capsys.readouterr().out
    assert exit_code == 1
    assert "ssh" in out
    assert "mirror missing" in out


def test_text_mode_exit_code_clears_once_the_mirror_is_healthy(monkeypatch, capsys):
    """CLEAR, at the CLI-exit-code layer: restoring health must bring the
    exit code back to 0 — the earlier card's bug was a check that could
    SET but never CLEAR, going permanently red on an otherwise healthy
    workspace."""
    report = _report([])
    exit_code = _run(monkeypatch, report)
    out = capsys.readouterr().out
    assert exit_code == 0
    assert "every declared contributes.knowledge mirror is present and current" in out
