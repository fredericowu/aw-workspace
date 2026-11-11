"""Pins the GATE half of card 4155bf3b's SET+CLEAR+GATE check: a non-empty
``emulated_apps`` row set must flip ``aw-workspace-cli doctor``'s own EXIT
CODE, not merely appear in the output body — the same distinction
``test_doctor_knowledge_mirrors_gate.py`` pins for that section, for the
same reason: a row that reaches the JSON body without flipping ``ok`` reads
as healthy to anything gating on the exit code (CI, an alert, a scheduled
check) even though a human reading the text output would see it.

The payload shape mirrors ``/api/apps/-/doctor``'s real response, minimised
to the fields each CLI-side renderer actually reads (see
``src/cli/commands/doctor.py``), with ``ok`` wired the same way
``src/apps/routes.py``'s ``doctor()`` route wires it: ``not emulated_apps``
folded into the overall boolean.
"""
import inspect
import re

from src.cli import local_client
from src.cli.commands import doctor as doctor_cli


def test_the_real_ok_tuple_in_routes_py_folds_in_emulated_apps():
    """The CLI-level tests below fabricate their own ``report`` dict, so a
    regression where ``src/apps/routes.py``'s ``doctor()`` route computes
    ``emulated_apps`` but forgets to fold it into the real ``"ok"`` tuple
    would pass every test below without ever touching production code.
    Reads the real source and pins the fold directly, the same technique
    the knowledge-mirrors gate test uses for its own section."""
    import src.apps.routes as routes_mod
    source = inspect.getsource(routes_mod)
    match = re.search(r'"ok": \(.*?\),\n', source, re.S)
    assert match is not None, "doctor()'s ok tuple was not found in src/apps/routes.py"
    assert "emulated_apps" in match.group(0)


def _report(emulated_apps):
    return {
        "ok": not emulated_apps,
        "emulated_apps": emulated_apps,
        "knowledge_mirrors": [],
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


def test_an_emulated_app_fails_the_json_exit_code(monkeypatch, capsys):
    report = _report([{"app": "kb", "image": "ghcr.io/tekflox/aw-app-kb:latest",
                        "image_arch": "amd64", "detail": "running emulated"}])
    exit_code = _run(monkeypatch, report, "--json")
    capsys.readouterr()
    assert exit_code == 1


def test_no_emulated_apps_passes_the_json_exit_code(monkeypatch, capsys):
    report = _report([])
    exit_code = _run(monkeypatch, report, "--json")
    capsys.readouterr()
    assert exit_code == 0


def test_text_mode_exit_code_reflects_the_emulated_app(monkeypatch, capsys):
    """The non-``--json`` path computes its own exit code from the sum of
    each section's problem count, independently of ``ok`` — this pins that
    ``emulated_apps`` is wired into THAT sum too, not only the ``--json``
    early return, and that the row text actually names the app and the
    detail."""
    report = _report([{"app": "kb", "image": "ghcr.io/tekflox/aw-app-kb:latest",
                        "image_arch": "amd64", "detail": "running emulated: image is amd64"}])
    exit_code = _run(monkeypatch, report)
    out = capsys.readouterr().out
    assert exit_code == 1
    assert "kb" in out
    assert "running emulated: image is amd64" in out


def test_text_mode_exit_code_clears_once_no_app_is_emulated(monkeypatch, capsys):
    """CLEAR, at the CLI-exit-code layer: a native image publishing and the
    app restarting onto it must bring the exit code back to 0."""
    report = _report([])
    exit_code = _run(monkeypatch, report)
    out = capsys.readouterr().out
    assert exit_code == 0
    assert "every running Tier-2 container matches the host architecture" in out
