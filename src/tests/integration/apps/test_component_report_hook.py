"""Deploy reconciliation C2 — the reconcile()-end hook (card 4165bf3b-9510-81f4).

Exercises the real ``Reconciler.reconcile()`` (not a fake) to prove the two
things the architect's design calls out explicitly:

* the component self-report + /deploy-check nudge fire only when a pass
  actually installed/upgraded something, never on a no-op convergence tick;
* a failure inside that self-report never fails the reconcile pass itself.

Unit coverage of ``component_report.py`` in isolation lives in
``src/tests/unit/apps/test_component_report.py``.
"""
from __future__ import annotations

from src.apps import component_report

from .test_reconciler import FakeCloud, _async, _make_app_repo, _reconciler


def test_reconcile_triggers_component_report_only_when_something_changed(tmp_path, monkeypatch):
    repo = _make_app_repo(tmp_path)
    cloud = FakeCloud()
    host, rt, rc = _reconciler(tmp_path, monkeypatch, cloud)

    calls = []

    async def _fake_report(runtime):
        calls.append(runtime)

    monkeypatch.setattr(component_report, "report_components_and_trigger_check",
                        _fake_report)

    async def run():
        # First pass: installs "widget" — installed is non-empty, hook fires.
        result1 = await rc.reconcile([{
            "app_id": "widget", "version": "1.0.0", "repo": repo, "ref": "main",
            "granted_permissions": ["routes:register"], "config": {},
        }])
        assert result1["installed"] == ["widget"]
        assert len(calls) == 1

        # Second pass against the SAME desired state: nothing to install or
        # upgrade (present_unchanged instead) — hook must not fire again.
        result2 = await rc.reconcile([{
            "app_id": "widget", "version": "1.0.0", "repo": repo, "ref": "main",
            "granted_permissions": ["routes:register"], "config": {},
        }])
        assert not result2.get("installed")
        assert not result2.get("upgraded")
        assert len(calls) == 1

    _async(run())


def test_reconcile_succeeds_even_when_component_report_raises(tmp_path, monkeypatch):
    """The architect's own acceptance test, at the reconcile() layer: a
    failed self-report must never fail the reconcile pass."""
    repo = _make_app_repo(tmp_path)
    cloud = FakeCloud()
    host, rt, rc = _reconciler(tmp_path, monkeypatch, cloud)

    async def _raising_report(runtime):
        raise ConnectionError("aw-backend unreachable")

    monkeypatch.setattr(component_report, "report_components_and_trigger_check",
                        _raising_report)

    async def run():
        result = await rc.reconcile([{
            "app_id": "widget", "version": "1.0.0", "repo": repo, "ref": "main",
            "granted_permissions": ["routes:register"], "config": {},
        }])
        # Must still report the install cleanly — the raise above must not
        # propagate out of reconcile() or corrupt its result.
        assert result["installed"] == ["widget"]
        assert rt.is_loaded("widget")

    _async(run())
