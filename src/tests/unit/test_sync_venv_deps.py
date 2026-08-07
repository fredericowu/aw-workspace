"""Unit tests for src.start.workspace._sync_venv_deps — the boot-time venv
reconciler that keeps $AW_WORKSPACE_HOME/venv in sync with requirements.txt.

Hermetic: subprocess.run is faked, so no real venv/pip/network is touched.
The one behaviour that MUST hold across all changes — never delete/recreate an
existing venv (apps install their own deps into it) — is asserted directly.
"""
import hashlib
import subprocess

from src.start.workspace import _sync_venv_deps


def _fake_run_factory(calls):
    """subprocess.run stub: records the sub-verb (`venv` / `pip`) and, for a
    `python -m venv <dir>` call, creates <dir>/bin/python so a follow-up run
    sees the venv as present."""
    def fake_run(cmd, check=True, **kw):
        calls.append(cmd)
        if len(cmd) >= 4 and cmd[1:3] == ["-m", "venv"]:
            binp = cmd[3] + "/bin"
            import os
            os.makedirs(binp, exist_ok=True)
            open(binp + "/python", "w").close()

        class _R:
            returncode = 0
        return _R()
    return fake_run


def _digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_creates_venv_when_missing(tmp_path, monkeypatch):
    req = tmp_path / "requirements.txt"
    req.write_text("httpx==0.27\n")
    venv = tmp_path / "venv"
    calls = []
    monkeypatch.setattr(subprocess, "run", _fake_run_factory(calls))

    _sync_venv_deps(venv_dir=venv, req_file=req)

    assert [c[2] for c in calls] == ["venv", "pip"]        # build then install
    assert (venv / "bin" / "python").exists()
    assert (venv / ".requirements.sha256").read_text().strip() == _digest(req)


def test_noop_when_already_synced(tmp_path, monkeypatch):
    req = tmp_path / "requirements.txt"
    req.write_text("httpx==0.27\n")
    venv = tmp_path / "venv"
    (venv / "bin").mkdir(parents=True)
    (venv / "bin" / "python").touch()
    (venv / ".requirements.sha256").write_text(_digest(req) + "\n")
    calls = []
    monkeypatch.setattr(subprocess, "run", _fake_run_factory(calls))

    _sync_venv_deps(venv_dir=venv, req_file=req)

    assert calls == []                                      # fast path, nothing run


def test_stamp_without_venv_still_builds(tmp_path, monkeypatch):
    """The broken layout this fix targets: a stamp file but no real venv must
    NOT be treated as synced."""
    req = tmp_path / "requirements.txt"
    req.write_text("httpx==0.27\n")
    venv = tmp_path / "venv"
    venv.mkdir()
    (venv / ".requirements.sha256").write_text(_digest(req) + "\n")  # stamp, no bin/python
    calls = []
    monkeypatch.setattr(subprocess, "run", _fake_run_factory(calls))

    _sync_venv_deps(venv_dir=venv, req_file=req)

    assert "venv" in [c[2] for c in calls]                  # rebuilt despite stamp


def test_reqs_changed_never_recreates_venv(tmp_path, monkeypatch):
    """Requirements bump on an existing venv => pip only, never `-m venv`, and
    an app-installed file must survive (the core no-wipe guarantee)."""
    req = tmp_path / "requirements.txt"
    req.write_text("httpx==0.27\n")
    venv = tmp_path / "venv"
    (venv / "bin").mkdir(parents=True)
    (venv / "bin" / "python").touch()
    (venv / ".requirements.sha256").write_text(_digest(req) + "\n")
    app_file = venv / "lib" / "site-packages" / "app_installed.txt"
    app_file.parent.mkdir(parents=True)
    app_file.write_text("owned by an installed app")

    req.write_text("httpx==0.28\nnewpkg==1.0\n")            # change the hash
    calls = []
    monkeypatch.setattr(subprocess, "run", _fake_run_factory(calls))

    _sync_venv_deps(venv_dir=venv, req_file=req)

    assert [c[2] for c in calls] == ["pip"]                 # no rebuild
    assert app_file.exists()                                # app deps preserved
    assert (venv / ".requirements.sha256").read_text().strip() == _digest(req)


def test_missing_requirements_is_noop(tmp_path, monkeypatch):
    venv = tmp_path / "venv"
    calls = []
    monkeypatch.setattr(subprocess, "run", _fake_run_factory(calls))

    _sync_venv_deps(venv_dir=venv, req_file=tmp_path / "does-not-exist.txt")

    assert calls == []
    assert not venv.exists()
