"""AW_BIN_DIR reaches installer scripts (src/apps/commands.py).

The gap this closes, measured on the aw host 2026-10-08: app install
scripts hardcoded ``/usr/local/bin``, which lives in the workspace
CONTAINER's writable layer and not on the host bind mount. Every workspace
Update recreates that container, so terraform/node/go/yarn/pnpm/brew and
the four agent CLIs were wiped and re-downloaded on the next boot — 18
minutes in, 25 of 60 apps loaded, Apps panel empty. Worse, each installer
runs behind ``_run``'s global flock, so every avoidable download is on the
critical path of every app queued behind it.

The scripts' own "already installed at $AW_BIN_DIR, skip" guards could
never fire, because the binary could not still be there. Exporting
``AW_BIN_DIR`` as the persistent ``paths.bin_dir()`` is what makes those
guards real.
"""
from __future__ import annotations

import os
import textwrap

import pytest

from src.apps import paths
from src.apps.commands import CommandInstaller


@pytest.fixture(autouse=True)
def workspace_home(tmp_path, monkeypatch):
    """Point AW_WORKSPACE_HOME at a temp dir. Without this these tests
    resolve the REAL /opt/aw-workspace/.aw-workspace — unwritable here, and
    on a live host it would be the running workspace's own state."""
    home = tmp_path / "ws-home"
    monkeypatch.setenv("AW_WORKSPACE_HOME", str(home))
    return str(home)


def test_installer_env_points_at_the_persistent_bin_dir():
    env = CommandInstaller()._installer_env()

    assert env["AW_BIN_DIR"] == paths.bin_dir()
    # The whole point: it must be under AW_WORKSPACE_HOME (host-mounted,
    # survives a container recreate), never the container-local
    # /usr/local/bin this replaces.
    assert env["AW_BIN_DIR"].startswith(paths.workspace_home())
    assert env["AW_BIN_DIR"] != "/usr/local/bin"


def test_installer_env_keeps_the_rest_of_the_environment():
    """A script still needs PATH, HOME, proxy vars and the rest — this adds
    one variable, it does not replace the environment."""
    env = CommandInstaller()._installer_env()

    for key in ("PATH", "HOME"):
        if key in os.environ:
            assert env[key] == os.environ[key]


def test_the_bin_dir_exists_so_a_script_can_write_into_it_immediately():
    """``paths.bin_dir()`` creates it. An installer that assumes it can
    ``cp`` straight in must not be the thing that discovers it is missing."""
    assert os.path.isdir(CommandInstaller()._installer_env()["AW_BIN_DIR"])


def test_a_script_actually_receives_it(tmp_path):
    """End to end through the real ``_run``: the value has to arrive in the
    script's own environment, not merely be computed. Uses a throwaway lock
    dir so this never contends with a live install."""
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    out = tmp_path / "seen"
    (pkg / "installer.sh").write_text(textwrap.dedent(f"""
        #!/usr/bin/env bash
        set -euo pipefail
        # Exactly the line the app scripts now carry.
        AW_BIN_DIR="${{AW_BIN_DIR:-/usr/local/bin}}"
        printf '%s' "$AW_BIN_DIR" > {out}
    """).strip())

    installer = CommandInstaller(lock_dir=str(tmp_path / "locks"))
    installer.run_installer(str(pkg), "installer.sh")

    assert out.read_text() == paths.bin_dir(), (
        "the installer script did not receive AW_BIN_DIR — the ${VAR:-default} "
        "form in the app scripts would silently fall back to /usr/local/bin, "
        "which is the container-local dir this whole change exists to stop using"
    )


def test_a_script_without_the_override_is_unaffected(tmp_path):
    """Backward compatibility, and the reason the app-side change is
    ``${AW_BIN_DIR:-/usr/local/bin}`` rather than a bare rename: a script
    from an older release that still hardcodes the path must keep working
    against a workspace that exports the variable."""
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    out = tmp_path / "seen"
    (pkg / "installer.sh").write_text(textwrap.dedent(f"""
        #!/usr/bin/env bash
        set -euo pipefail
        AW_BIN_DIR="/usr/local/bin"
        printf '%s' "$AW_BIN_DIR" > {out}
    """).strip())

    installer = CommandInstaller(lock_dir=str(tmp_path / "locks"))
    installer.run_installer(str(pkg), "installer.sh")

    assert out.read_text() == "/usr/local/bin"
