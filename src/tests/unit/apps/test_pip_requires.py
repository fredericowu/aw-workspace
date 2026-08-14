"""``runtime.pip_requires`` — Python deps a Tier-1 app needs importable.

Nine installed apps declared this key and, until 2026-08-14, nothing in core
ever read it: `grep -rn pip_requires src/` returned nothing. The manifests were
documentation. Apps worked only when their deps happened to already be present
— aw-app-tasks' cron scheduling (croniter) and aw-app-presentations' PNG export
(playwright) were both dead in a fresh workspace while looking correctly
declared, and each reported a missing *package* rather than a missing *install
step*, which sends the reader to the wrong place.

A Tier-1 app is imported into the workspace process, so it cannot install its
own deps before its module is importable — core has to do it, before the
import.
"""
from __future__ import annotations

import os

import pytest

from src.apps.manifest import validate_manifest


def _manifest(pip_requires=None, tier="inprocess", **runtime):
    rt = {"entrypoint": "plugin:AppPlugin", **runtime}
    if pip_requires is not None:
        rt["pip_requires"] = pip_requires
    if tier == "container":
        rt.setdefault("image", "ghcr.io/x/y:latest")
        rt.setdefault("port", 9999)
    return {
        "manifest_version": 1, "id": "demo", "name": "Demo", "version": "1.0.0",
        "tier": tier, "runtime": rt, "permissions": [],
    }


# ---- manifest parsing -------------------------------------------------------

def test_pip_requires_is_parsed_off_the_manifest():
    m = validate_manifest(_manifest(["croniter>=2.0", "httpx>=0.24"]))
    assert m.pip_requires == ["croniter>=2.0", "httpx>=0.24"]


def test_absent_pip_requires_is_an_empty_list_not_none():
    assert validate_manifest(_manifest()).pip_requires == []


@pytest.mark.parametrize("declared", [[], ["", "   "], [""]])
def test_blank_entries_are_dropped(declared):
    """A stray empty string would become a bare `pip install` with no
    arguments, which exits 0 and looks like success."""
    assert validate_manifest(_manifest(declared)).pip_requires == []


# ---- install behaviour ------------------------------------------------------

class _Runtime:
    """The two methods under test, lifted onto a bare object so the test does
    not have to construct a whole AppRuntime (which needs a FastAPI host)."""

    def __init__(self):
        from src.apps.runtime import AppRuntime
        self._install_pip_requires = AppRuntime._install_pip_requires.__get__(self)


@pytest.fixture()
def rt(tmp_path, monkeypatch):
    monkeypatch.setattr("src.apps.paths.workspace_home", lambda: str(tmp_path))
    return _Runtime()


@pytest.fixture()
def calls(monkeypatch):
    seen = []

    class _Proc:
        returncode = 0
        stdout = ""
        stderr = ""

    def _run(cmd, **kw):
        seen.append(cmd)
        return _Proc()

    monkeypatch.setattr("subprocess.run", _run)
    return seen


def test_declared_requirements_are_installed(rt, calls):
    rt._install_pip_requires(validate_manifest(_manifest(["croniter>=2.0"])))

    assert len(calls) == 1
    assert calls[0][1:4] == ["-m", "pip", "install"]
    assert "croniter>=2.0" in calls[0]


def test_nothing_declared_runs_no_subprocess(rt, calls):
    rt._install_pip_requires(validate_manifest(_manifest()))
    assert calls == []


def test_second_load_does_not_reinstall(rt, calls):
    """This runs on every app load, i.e. every boot — a pip subprocess per app
    per boot would be paid forever."""
    m = validate_manifest(_manifest(["croniter>=2.0"]))
    rt._install_pip_requires(m)
    rt._install_pip_requires(m)
    rt._install_pip_requires(m)

    assert len(calls) == 1


def test_a_changed_requirement_list_reinstalls(rt, calls):
    rt._install_pip_requires(validate_manifest(_manifest(["croniter>=2.0"])))
    rt._install_pip_requires(validate_manifest(_manifest(["croniter>=2.0", "httpx"])))

    assert len(calls) == 2


def test_a_changed_list_does_not_leave_the_stale_marker_behind(rt, calls, tmp_path):
    rt._install_pip_requires(validate_manifest(_manifest(["croniter>=2.0"])))
    rt._install_pip_requires(validate_manifest(_manifest(["croniter>=2.0", "httpx"])))

    markers = os.listdir(tmp_path / "pip-requires")
    assert len(markers) == 1, f"stale markers accumulated: {markers}"


def test_reordering_the_same_requirements_is_not_a_change(rt, calls):
    rt._install_pip_requires(validate_manifest(_manifest(["a", "b"])))
    rt._install_pip_requires(validate_manifest(_manifest(["b", "a"])))

    assert len(calls) == 1


def test_a_failed_install_is_not_marked_satisfied(rt, monkeypatch):
    """Offline hosts exist. The next load must retry rather than record a lie —
    and must not raise, because most apps still work without an optional dep;
    the import that genuinely needs it raises a better error by itself."""
    class _Fail:
        returncode = 1
        stdout = ""
        stderr = "No matching distribution"

    attempts = []
    monkeypatch.setattr("subprocess.run", lambda cmd, **kw: attempts.append(cmd) or _Fail())

    m = validate_manifest(_manifest(["nope-does-not-exist"]))
    rt._install_pip_requires(m)   # must not raise
    rt._install_pip_requires(m)

    assert len(attempts) == 2


def test_a_raising_subprocess_is_swallowed(rt, monkeypatch):
    def _boom(cmd, **kw):
        raise OSError("pip vanished")

    monkeypatch.setattr("subprocess.run", _boom)

    rt._install_pip_requires(validate_manifest(_manifest(["x"])))   # must not raise


def test_two_apps_do_not_share_a_marker(rt, calls, tmp_path):
    a = validate_manifest({**_manifest(["x"]), "id": "app-a"})
    b = validate_manifest({**_manifest(["x"]), "id": "app-b"})
    rt._install_pip_requires(a)
    rt._install_pip_requires(b)

    assert len(calls) == 2, "the second app was skipped by the first app's marker"
