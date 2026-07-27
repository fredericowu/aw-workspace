"""Commands / system-CLI install backend (ADR contribution point ``commands`` /
``system_clis``, gated by the F2 ``commands:install`` capability) — F4.

Two related surfaces, both journaled so uninstall reverts them:

* **command shims** — an app declares ``contributes.commands`` (``<slug>-*``);
  ``install_shim`` drops a tiny wrapper into the persistent ``bin`` dir (on PATH,
  survives restart) that execs the app-provided ``exec`` path. ``remove_shim``
  reverts it.
* **system CLIs** — an app declares ``contributes.system_clis`` with an
  ``installer`` script; ``run_installer`` runs it (installing e.g. ``git``/``gh``/
  ``vim`` INTO the workspace). The install scripts are idempotent, so the
  reconciler safely re-runs them on every boot / workspace recreation.
  ``run_revert`` runs the app's uninstall script on uninstall.

Both the installer and the revert script are run from the app's package dir so
their relative paths resolve; the app never gets a raw shell handle — it calls
the gated ``ctx.commands`` facade, which routes here.
"""
from __future__ import annotations

import logging
import os
import subprocess

from src.apps import paths

log = logging.getLogger(__name__)

# scripts can be slow (apt update + install); keep a generous ceiling.
DEFAULT_TIMEOUT = float(os.environ.get("AW_APPS_CLI_INSTALL_TIMEOUT", "600"))


class CommandError(RuntimeError):
    """Raised when a command/CLI install or revert script fails."""


def _resolve(package_dir: str, script: str) -> str:
    path = script if os.path.isabs(script) else os.path.join(package_dir, script)
    path = os.path.abspath(path)
    if not path.startswith(os.path.abspath(package_dir) + os.sep):
        raise CommandError(f"script {script!r} escapes the app package dir")
    if not os.path.isfile(path):
        raise CommandError(f"script not found: {script!r}")
    return path


class CommandInstaller:
    """Runtime-owned backend for the ``commands`` / ``system_clis`` surface."""

    def __init__(self, timeout: float = DEFAULT_TIMEOUT) -> None:
        self.timeout = timeout

    # ---- system CLIs (installer scripts) --------------------------------

    def run_installer(self, package_dir: str, script: str) -> str:
        return self._run(package_dir, script, what="installer")

    def run_revert(self, package_dir: str, script: str) -> str:
        return self._run(package_dir, script, what="revert")

    def _run(self, package_dir: str, script: str, *, what: str) -> str:
        path = _resolve(package_dir, script)
        proc = subprocess.run(
            ["bash", path], cwd=package_dir, capture_output=True, text=True,
            timeout=self.timeout, check=False,
        )
        if proc.returncode != 0:
            raise CommandError(
                f"{what} {script!r} failed (exit {proc.returncode}): "
                f"{proc.stderr.strip() or proc.stdout.strip()}"
            )
        return proc.stdout.strip()

    # ---- command shims (persistent bin dir) -----------------------------

    def install_shim(self, name: str, package_dir: str, exec_path: str) -> str:
        """Write ``<bin>/<name>`` execing the app-provided ``exec_path``.

        Returns the shim's absolute path (journaled so ``remove_shim`` reverts).
        """
        target = _resolve(package_dir, exec_path)
        shim_path = os.path.join(paths.bin_dir(), name)
        script = (
            "#!/usr/bin/env bash\n"
            "# aw-apps command shim (F4) — auto-generated; do not edit.\n"
            f'exec "{target}" "$@"\n'
        )
        with open(shim_path, "w", encoding="utf-8") as f:
            f.write(script)
        os.chmod(shim_path, 0o755)
        log.info("apps: installed command shim %s -> %s", shim_path, target)
        return shim_path

    def remove_shim(self, shim_path: str) -> bool:
        if shim_path and os.path.isfile(shim_path):
            os.remove(shim_path)
            log.info("apps: removed command shim %s", shim_path)
            return True
        return False
