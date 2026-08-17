"""``_reexec_into_venv`` — the hand-off from the base interpreter to the venv.

PID 1 boots under the image's base python on purpose (see the Dockerfile CMD)
so it can repair a missing venv. That only works if it then actually re-execs
into the venv. When it silently doesn't, boot dies on ``import uvicorn`` with a
venv sitting right there, complete and correct — which is what every fresh BYOD
provision hit on 2026-08-17.
"""
import os
import sys

import pytest

from src.start import workspace as ws


@pytest.fixture()
def venv(tmp_path, monkeypatch):
    """A venv whose bin/python is a symlink to the CURRENT interpreter.

    That is what ``python -m venv`` produces, and it is the shape that broke
    the old check: both sides of a path comparison resolve to the same real
    interpreter, so "am I already in the venv?" answered yes from outside it.
    """
    home = tmp_path / ".aw-workspace"
    bindir = home / "venv" / "bin"
    bindir.mkdir(parents=True)
    (bindir / "python").symlink_to(os.path.realpath(sys.executable))
    monkeypatch.setattr("src.apps.paths.workspace_home", lambda: str(home))
    monkeypatch.delenv("AW_VENV_REEXEC", raising=False)
    return home


def _capture_execv(monkeypatch):
    calls = []
    monkeypatch.setattr(os, "execv", lambda path, argv: calls.append((path, argv)))
    return calls


def test_reexecs_when_venv_python_is_a_symlink_to_the_base_interpreter(venv, monkeypatch):
    """The regression. `python -m venv` links bin/python3 straight at the base
    interpreter, so realpath(venv_python) == realpath(sys.executable) and the
    old guard returned early — leaving sys.path on the base image's
    site-packages and boot dying on `import uvicorn`."""
    calls = _capture_execv(monkeypatch)
    # sys.prefix is the base env, i.e. we are NOT in the venv.
    monkeypatch.setattr(sys, "prefix", "/usr/local")

    ws._reexec_into_venv()

    assert len(calls) == 1, "should have handed off to the venv interpreter"
    path, argv = calls[0]
    assert path == str(venv / "venv" / "bin" / "python")
    assert argv[1:3] == ["-m", "src.start.workspace"]
    assert os.environ["AW_VENV_REEXEC"] == "1"


def test_no_reexec_when_already_running_under_the_venv(venv, monkeypatch):
    """The exec'd child must not loop, and an older image whose CMD is already
    `python` (PATH-resolved to the venv) must be left alone."""
    calls = _capture_execv(monkeypatch)
    monkeypatch.setattr(sys, "prefix", str(venv / "venv"))

    ws._reexec_into_venv()

    assert calls == []


def test_sentinel_env_var_stops_a_loop(venv, monkeypatch):
    calls = _capture_execv(monkeypatch)
    monkeypatch.setattr(sys, "prefix", "/usr/local")
    monkeypatch.setenv("AW_VENV_REEXEC", "1")

    ws._reexec_into_venv()

    assert calls == []


def test_no_venv_keeps_booting_under_the_base_interpreter(tmp_path, monkeypatch):
    """The whole reason the Dockerfile CMD names the base python absolutely:
    with no venv to enter, PID 1 must still come up so it can build one."""
    calls = _capture_execv(monkeypatch)
    monkeypatch.setattr("src.apps.paths.workspace_home", lambda: str(tmp_path))
    monkeypatch.delenv("AW_VENV_REEXEC", raising=False)

    ws._reexec_into_venv()

    assert calls == []


def test_dangling_venv_python_keeps_booting(tmp_path, monkeypatch):
    """A mount whose venv survived but whose interpreter symlink points at a
    python from a since-replaced base image. Handing off would exec nothing."""
    bindir = tmp_path / "venv" / "bin"
    bindir.mkdir(parents=True)
    (bindir / "python").symlink_to("/nonexistent/python3")
    monkeypatch.setattr("src.apps.paths.workspace_home", lambda: str(tmp_path))
    monkeypatch.delenv("AW_VENV_REEXEC", raising=False)
    calls = _capture_execv(monkeypatch)

    ws._reexec_into_venv()

    assert calls == []
