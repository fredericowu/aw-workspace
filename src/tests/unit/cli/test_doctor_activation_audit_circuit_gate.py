"""Pins the SET+CLEAR+GATE check for activation_audit's own two self-checks
(``stale`` and the new ``repair_circuits_open``), the same discipline
``test_doctor_emulated_apps_gate.py``/``test_doctor_knowledge_mirrors_gate.py``
pin for their own sections.

Before this card (4155bf3b-9510-81fc), ``stale`` was already folded into the
real JSON ``"ok"`` tuple in ``src/apps/routes.py`` but never printed, counted,
or gated on by the TEXT form of ``aw-workspace-cli doctor`` — the exact form
`AGENTS.md` tells every agent to run first. A human/agent running the plain
``aw-workspace-cli doctor`` could see "No silent degradation found." with
exit code 0 while the JSON payload's own ``ok`` was already ``False``. This
pins both the pre-existing ``stale`` gap (now closed) and the new
``repair_circuits_open`` signal (I1's backoff/circuit-breaker,
``src/apps/activation_audit.py``) at the TEXT-mode layer, not just JSON.
"""
import inspect
import re

from src.cli import local_client
from src.cli.commands import doctor as doctor_cli


def test_the_real_ok_tuple_in_routes_py_folds_in_activation_audit_circuits():
    """Same technique as the emulated-apps/knowledge-mirrors gate tests:
    reads the real source so a regression where routes.py computes
    ``activation_audit_circuits_open`` but forgets to fold it into the real
    ``"ok"`` tuple would be caught here, not only by a fabricated-report test
    below that could not see such a regression."""
    import src.apps.routes as routes_mod
    source = inspect.getsource(routes_mod)
    match = re.search(r'"ok": \(.*?\),\n', source, re.S)
    assert match is not None, "doctor()'s ok tuple was not found in src/apps/routes.py"
    assert "activation_audit_circuits_open" in match.group(0)


def _report(*, stale=False, last_run_age_s=1.0, circuits_open=None):
    circuits_open = circuits_open or []
    return {
        "ok": not stale and not circuits_open,
        "activation_audit": {
            "last_run_age_s": last_run_age_s if not stale else None,
            "stale": stale,
            "repair_circuits_open": circuits_open,
        },
        "emulated_apps": [],
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


def test_an_open_repair_circuit_fails_the_json_exit_code(monkeypatch, capsys):
    report = _report(circuits_open=[
        {"app": "essentials", "consecutive_failures": 10, "last_error": "boom"}])
    exit_code = _run(monkeypatch, report, "--json")
    capsys.readouterr()
    assert exit_code == 1


def test_no_open_circuit_passes_the_json_exit_code(monkeypatch, capsys):
    exit_code = _run(monkeypatch, _report(), "--json")
    capsys.readouterr()
    assert exit_code == 0


def test_text_mode_exit_code_reflects_an_open_repair_circuit(monkeypatch, capsys):
    """The non-``--json`` path computes its own exit code from the sum of
    each section's problem count, independently of ``ok`` — this pins that
    ``repair_circuits_open`` is wired into THAT sum too, not only the
    ``--json`` early return, and that the row names the app and the error."""
    report = _report(circuits_open=[
        {"app": "essentials", "consecutive_failures": 10,
         "last_error": "installer 'scripts/install_yarn.sh' failed"}])
    exit_code = _run(monkeypatch, report)
    out = capsys.readouterr().out
    assert exit_code == 1
    assert "essentials" in out
    assert "10 consecutive failures" in out
    assert "install_yarn.sh" in out


def test_text_mode_stale_audit_with_no_circuits_still_fails(monkeypatch, capsys):
    """Pre-existing gap, closed by the same change: ``stale`` reaching the
    JSON ``ok`` tuple is not new, but it never reached the text-mode exit
    code sum until this card — pinned here so it cannot regress silently
    again."""
    report = _report(stale=True, last_run_age_s=None)
    exit_code = _run(monkeypatch, report)
    out = capsys.readouterr().out
    assert exit_code == 1
    assert "stale" in out.lower()


def test_text_mode_exit_code_clears_once_no_circuit_is_open(monkeypatch, capsys):
    """CLEAR, at the CLI-exit-code layer: once the app loads successfully
    again (through any path), the next doctor run must come back clean."""
    exit_code = _run(monkeypatch, _report())
    out = capsys.readouterr().out
    assert exit_code == 0
    assert "Activation audit — running and no repair circuit is open" in out
