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

**System-CLI drift healing** — reconcile-on-boot only reinstalls an app that
isn't currently loaded (``src/apps/reconciler.py``); once an app is loaded, the
runtime never again checks that the CLIs it installed are still on disk. If
something outside the app's own lifecycle removes a binary (a package purge
run by hand, an unrelated apt operation, a base-image layer rebuilt under a
long-lived container without a full recreation — this is what happened to
``gh`` in aw-app-git, found 2026-08-03), the app is stuck reporting "installed"
with a dead CLI until it's manually uninstalled/reinstalled or the whole
workspace is recreated. ``record_system_cli``/``missing_system_clis``/``heal``
back a generic, per-app-code-free fix: every ``install_system_cli`` call
auto-registers itself here, and ``AppRuntime.start_system_cli_healer`` (one
runtime-owned periodic task, not gated by any app's ``watchdog:tasks``
permission) re-runs an app's own installer script whenever a CLI stops being
healthy — the installer IS the app's heal logic, so no app needs to write or
register anything extra.

**Present is not healthy.** Health used to mean ``shutil.which(name) is not
None``. That is a proxy, and it lied: a ``/usr/bin/git`` with an EMPTY
``/usr/lib/git-core`` (no package behind it) is on PATH and prints a version
while every ``https://`` operation dies with "git: 'remote-https' is not a git
command". The healer saw "present", never healed, and nvm — which installs
itself by cloning over HTTPS — took node, npm, npx, yarn and pnpm down with
it, surfacing as four unrelated failures in a different app entirely
(2026-08-12).

Worse, the app's own installer guard made the SAME assumption, so neither
layer could catch the other. So health is now a command that has to succeed:
``verify`` from the manifest entry, or ``<name> --version`` by default. An app
whose CLI has no meaningful version flag passes ``verify=False`` to opt back
down to a presence check, which keeps that decision explicit and visible in
its manifest instead of being the silent default for everything.
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
from typing import Any

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
        # (app_id, cli_name) -> (package_dir, installer_script) for every
        # install_system_cli call, so the healer can re-run the right script.
        self._system_clis: dict[tuple[str, str], tuple[str, str]] = {}
        # (app_id, cli_name) -> verify spec: a shell command to run, or False
        # to fall back to a presence check. See the module docstring.
        self._verify: dict[tuple[str, str], str | bool | None] = {}
        # (app_id, cli_name) -> last heal outcome. A heal that keeps failing
        # used to be a log line repeated every pass and nothing else — 65
        # times in a single boot, seen by no one. Now it is state something
        # can report (`aw-workspace-cli doctor`, GET /api/apps/-/doctor).
        self._heal_state: dict[tuple[str, str], dict[str, Any]] = {}

    # ---- system CLIs (installer scripts) --------------------------------

    def run_installer(self, package_dir: str, script: str) -> str:
        return self._run(package_dir, script, what="installer")

    def run_revert(self, package_dir: str, script: str) -> str:
        return self._run(package_dir, script, what="revert")

    def record_system_cli(self, app_id: str, name: str, package_dir: str,
                           installer: str,
                           verify: str | bool | None = None) -> None:
        """Track a CLI an app installed so the healer can re-check/re-run it
        later. Called by ``CommandsFacade.install_system_cli`` — apps never
        call this directly.

        ``verify``: a shell command proving the CLI WORKS, ``None`` for the
        default (``<name> --version``), or ``False`` for presence-only.
        """
        self._system_clis[(app_id, name)] = (package_dir, installer)
        self._verify[(app_id, name)] = verify

    def forget_system_clis_for(self, app_id: str) -> None:
        """Drop everything tracked for an app on uninstall — an uninstalled
        app's CLI is gone on purpose, not drift to heal."""
        for key in [k for k in self._system_clis if k[0] == app_id]:
            del self._system_clis[key]

    VERIFY_TIMEOUT = 20.0

    def check_system_cli(self, app_id: str, name: str) -> tuple[bool, str]:
        """``(healthy, reason)`` for one tracked CLI. Reason is "" when healthy.

        An explicit ``verify`` command is the SOLE authority — no PATH check
        first. Not every CLI is a binary: ``nvm`` is a shell function sourced
        from ``~/.nvm/nvm.sh``, so ``which`` can never find it, and a PATH
        precondition would report it broken forever while the healer re-ran a
        perfectly good installer on every pass.

        Without an explicit verify, presence comes first — a missing binary
        needs no subprocess to diagnose — and then ``<name> --version``, which
        is what makes this a health check rather than the ``which`` proxy it
        replaces.
        """
        verify = self._verify.get((app_id, name))
        explicit = isinstance(verify, str) and verify.strip()

        if not explicit:
            if shutil.which(name) is None:
                return False, "not on PATH"
            if verify is False:
                return True, ""

        command = verify if explicit else f"{name} --version"
        try:
            proc = subprocess.run(
                ["bash", "-c", command], capture_output=True, text=True,
                timeout=self.VERIFY_TIMEOUT, check=False,
            )
        except subprocess.TimeoutExpired:
            return False, f"verify timed out after {self.VERIFY_TIMEOUT:.0f}s: {command}"
        except Exception as exc:  # noqa: BLE001 — a broken verify must not crash the healer
            return False, f"verify could not run ({exc}): {command}"
        if proc.returncode != 0:
            detail = (proc.stderr.strip() or proc.stdout.strip() or "").splitlines()
            return False, f"verify failed (exit {proc.returncode}): {detail[0] if detail else command}"
        return True, ""

    def missing_system_clis(self) -> list[tuple[str, str]]:
        """``(app_id, name)`` pairs for every tracked CLI that is not healthy.

        Named "missing" for history; a CLI that is present but broken belongs
        here too, and is exactly the case the name used to hide.
        """
        return [key for key in self._system_clis if not self.check_system_cli(*key)[0]]

    def system_cli_report(self) -> list[dict[str, Any]]:
        """Every tracked CLI with its health and last heal outcome — the raw
        material for the doctor endpoint."""
        report: list[dict[str, Any]] = []
        for (app_id, name) in sorted(self._system_clis):
            healthy, reason = self.check_system_cli(app_id, name)
            state = self._heal_state.get((app_id, name), {})
            report.append({
                "app": app_id, "cli": name, "healthy": healthy,
                "reason": reason,
                "path": shutil.which(name),
                "heal_failures": state.get("consecutive_failures", 0),
                "last_heal_error": state.get("last_error"),
            })
        return report

    def record_heal_result(self, app_id: str, name: str, error: str | None) -> None:
        state = self._heal_state.setdefault(
            (app_id, name), {"consecutive_failures": 0, "last_error": None})
        if error is None:
            state["consecutive_failures"] = 0
            state["last_error"] = None
        else:
            state["consecutive_failures"] += 1
            state["last_error"] = error

    def heal(self, app_id: str, name: str) -> str:
        """Re-run the app's own installer for one missing CLI (idempotent —
        the same script every ``install_system_cli`` call already ran)."""
        package_dir, installer = self._system_clis[(app_id, name)]
        return self.run_installer(package_dir, installer)

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

    @staticmethod
    def shim_path(name: str) -> str:
        """Where :meth:`install_shim` would put ``name``'s shim.

        Split out for W3's attach path: a worker that is only converging must
        not WRITE the shim (one shared bin dir, N writers), but still journals
        the ``command:install`` entry so its own unload stays symmetric — and
        that entry carries the path. See src/apps/lifecycle.py.
        """
        return os.path.join(paths.bin_dir(), name)

    def install_shim(self, name: str, package_dir: str, exec_path: str) -> str:
        """Write ``<bin>/<name>`` execing the app-provided ``exec_path``.

        Returns the shim's absolute path (journaled so ``remove_shim`` reverts).
        """
        target = _resolve(package_dir, exec_path)
        shim_path = self.shim_path(name)
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
