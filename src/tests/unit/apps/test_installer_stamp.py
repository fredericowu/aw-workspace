"""Installer stamps (src/apps/commands.py): skip a re-run of an installer that
already succeeded on this container layer; invalidate on any change."""
from __future__ import annotations

import os
import time

import pytest

from src.apps import commands as cmds_mod, paths
from src.apps.commands import CommandInstaller, DEFAULT_STAMP_DIR


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("AW_WORKSPACE_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("AW_INSTALLER_STAMP_DIR", str(tmp_path / "stamps"))
    return tmp_path


@pytest.fixture
def pkg(tmp_path):
    d = tmp_path / "pkg"
    d.mkdir()
    (d / "i.sh").write_text("#!/bin/bash\necho ran >> \"$(dirname \"$0\")/ran.log\"\n")
    return d


def _runs(pkg):
    f = pkg / "ran.log"
    return len(f.read_text().splitlines()) if f.exists() else 0


def _install(c, pkg, version="1.0.0"):
    """What SystemCliFacade.install_system_cli does on the provision path."""
    if c.installer_stamp_hit("app", str(pkg), "i.sh", version):
        return "skipped"
    c.run_installer(str(pkg), "i.sh", app_id="app", version=version)
    return "ran"


def test_hit_skips_then_script_version_ttl_and_healer_rerun(pkg, monkeypatch):
    c = CommandInstaller(lock_dir=str(pkg / "locks"))
    assert _install(c, pkg) == "ran"
    assert _install(c, pkg) == "skipped"
    assert _runs(pkg) == 1

    # changed version
    assert _install(c, pkg, "1.0.1") == "ran"
    # changed script bytes
    (pkg / "i.sh").write_text((pkg / "i.sh").read_text() + "# edit\n")
    assert _install(c, pkg, "1.0.1") == "ran"
    assert _install(c, pkg, "1.0.1") == "skipped"
    # expired TTL
    real = time.time
    monkeypatch.setattr(cmds_mod.time, "time", lambda: real() + 8 * 24 * 3600)
    assert _install(c, pkg, "1.0.1") == "ran"
    monkeypatch.setattr(cmds_mod.time, "time", real)
    assert _install(c, pkg, "1.0.1") == "skipped"
    # healer: unhealthy CLI deletes the stamp
    c.record_system_cli("app", "definitely-not-a-binary-xyz", str(pkg), "i.sh")
    assert c.check_system_cli("app", "definitely-not-a-binary-xyz")[0] is False
    assert _install(c, pkg, "1.0.1") == "ran"


def test_changed_installer_env_reruns(pkg, monkeypatch):
    c = CommandInstaller(lock_dir=str(pkg / "locks"))
    assert _install(c, pkg) == "ran"
    monkeypatch.setenv("AW_WORKSPACE_HOME", str(pkg / "other-home"))
    assert _install(c, pkg) == "ran"  # AW_BIN_DIR moved


def test_failed_installer_writes_no_stamp(pkg):
    (pkg / "i.sh").write_text("exit 3\n")
    c = CommandInstaller(lock_dir=str(pkg / "locks"))
    with pytest.raises(cmds_mod.CommandError):
        c.run_installer(str(pkg), "i.sh", app_id="app", version="1")
    assert not c.installer_stamp_hit("app", str(pkg), "i.sh", "1")


def test_unwritable_stamp_dir_just_means_no_skip(pkg, monkeypatch):
    monkeypatch.setenv("AW_INSTALLER_STAMP_DIR", "/proc/nope/stamps")
    c = CommandInstaller(lock_dir=str(pkg / "locks"))
    assert _install(c, pkg) == "ran"
    assert _install(c, pkg) == "ran"


def test_default_stamp_dir_is_in_the_container_layer(monkeypatch, tmp_path):
    """The lifetime bug unit tests with a tmp dir cannot see: the default must
    not sit on a bind mount that survives a container recreate."""
    monkeypatch.delenv("AW_INSTALLER_STAMP_DIR")
    monkeypatch.setenv("HOME", str(tmp_path / "h"))
    d = os.path.abspath(cmds_mod.stamp_dir())
    assert d == DEFAULT_STAMP_DIR
    for forbidden in (paths.workspace_home(), os.environ["HOME"],
                      "/opt/aw-workspace/.aw-workspace"):
        assert not d.startswith(os.path.abspath(forbidden) + os.sep)
