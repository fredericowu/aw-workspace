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
import threading
import time
from typing import Any

from src.api.terminal_manager import kill_proc_tree
from src.apps import paths

log = logging.getLogger(__name__)

# scripts can be slow (apt update + install); keep a generous ceiling.
DEFAULT_TIMEOUT = float(os.environ.get("AW_APPS_CLI_INSTALL_TIMEOUT", "600"))


class CommandError(RuntimeError):
    """Raised when a command/CLI install or revert script fails."""


class HealInFlightError(CommandError):
    """Raised by ``heal()`` when a heal for the same CLI is already running
    on another thread. See ``CommandInstaller.heal``'s docstring — this is
    what actually survives a cancelled watchdog ``asyncio.Task``."""


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
        # Reentrancy guard for heal() — see its docstring. Lives on the
        # worker thread the installer subprocess actually runs on, not on
        # whatever asyncio Task happens to be awaiting it, because it is
        # exactly that Task's cancellation (watchdog.pause()) that leaves
        # this thread running unsupervised in the first place.
        self._heal_guard = threading.Lock()
        self._healing: set[tuple[str, str]] = set()

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

        Also clears any heal backoff/circuit-breaker state for this CLI — a
        fresh install/update/activate means it's being reconsidered from
        scratch, and a circuit that tripped on an old broken binary must
        not stay open forever after the owning app is reinstalled/updated.
        """
        self._system_clis[(app_id, name)] = (package_dir, installer)
        self._verify[(app_id, name)] = verify
        self._heal_state.pop((app_id, name), None)

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
            proc = self._run_subprocess(["bash", "-c", command], cwd=None,
                                         timeout=self.VERIFY_TIMEOUT)
        except subprocess.TimeoutExpired:
            return False, f"verify timed out after {self.VERIFY_TIMEOUT:.0f}s: {command}"
        except Exception as exc:  # noqa: BLE001 — a broken verify must not crash the healer
            return False, f"verify could not run ({exc}): {command}"
        if proc.returncode != 0:
            detail = (proc.stderr.strip() or proc.stdout.strip() or "").splitlines()
            return False, f"verify failed (exit {proc.returncode}): {detail[0] if detail else command}"
        return True, ""

    # Backoff/circuit-breaker for repeat heal failures. 300s matches the
    # healer's own cadence (DEFAULT_CLI_HEAL_INTERVAL_S); capped at 1h since
    # a full `npm install -g` is too expensive to retry on the watchdog's own
    # 1800s ceiling. After HEAL_MAX_CONSECUTIVE_FAILURES the circuit opens —
    # a CLI that failed that many installs in a row is not fixed by the next
    # one, and retrying forever is exactly what produced 7 concurrent
    # install_copilot.sh processes on the crispal host.
    HEAL_BACKOFF_BASE_S = 300.0
    HEAL_BACKOFF_MAX_S = 3600.0
    HEAL_MAX_CONSECUTIVE_FAILURES = 10

    def _due_for_heal(self, app_id: str, name: str) -> bool:
        state = self._heal_state.get((app_id, name))
        if state is None:
            return True
        if state["consecutive_failures"] >= self.HEAL_MAX_CONSECUTIVE_FAILURES:
            return False  # circuit open — see record_heal_result
        next_at = state.get("next_attempt_at")
        return next_at is None or time.time() >= next_at

    def missing_system_clis(self) -> list[tuple[str, str]]:
        """``(app_id, name)`` pairs for every tracked CLI that is unhealthy
        AND due for another heal attempt right now.

        Named "missing" for history; a CLI that is present but broken belongs
        here too, and is exactly the case the name used to hide. A CLI whose
        backoff window hasn't elapsed yet, or whose circuit breaker is open
        (see ``record_heal_result``), is unhealthy but not returned here —
        the healer skips it rather than re-running its installer.
        """
        return [key for key in self._system_clis
                if not self.check_system_cli(*key)[0] and self._due_for_heal(*key)]

    def heal_in_flight(self) -> bool:
        """Whether any tracked CLI currently has an installer physically
        running on a worker thread — including one abandoned by a cancelled
        watchdog Task (see ``heal``'s docstring). Best-effort, no lock: a
        caller deciding whether to start a new heal PASS only needs "is
        anything running right now", not a linearizable snapshot."""
        return bool(self._healing)

    def system_cli_report(self) -> list[dict[str, Any]]:
        """Every tracked CLI with its health and last heal outcome — the raw
        material for the doctor endpoint."""
        report: list[dict[str, Any]] = []
        for (app_id, name) in sorted(self._system_clis):
            healthy, reason = self.check_system_cli(app_id, name)
            state = self._heal_state.get((app_id, name), {})
            failures = state.get("consecutive_failures", 0)
            report.append({
                "app": app_id, "cli": name, "healthy": healthy,
                "reason": reason,
                "path": shutil.which(name),
                "heal_failures": failures,
                "last_heal_error": state.get("last_error"),
                "heal_gave_up": failures >= self.HEAL_MAX_CONSECUTIVE_FAILURES,
            })
        return report

    def record_heal_result(self, app_id: str, name: str, error: str | None) -> None:
        state = self._heal_state.setdefault(
            (app_id, name),
            {"consecutive_failures": 0, "last_error": None, "next_attempt_at": None})
        if error is None:
            state["consecutive_failures"] = 0
            state["last_error"] = None
            state["next_attempt_at"] = None
        else:
            state["consecutive_failures"] += 1
            state["last_error"] = error
            if state["consecutive_failures"] >= self.HEAL_MAX_CONSECUTIVE_FAILURES:
                # circuit open: missing_system_clis() stops offering this
                # CLI as a candidate until record_system_cli() resets it
                # (app reinstall/update/restart) — see that method's note.
                state["next_attempt_at"] = None
            else:
                backoff = min(
                    self.HEAL_BACKOFF_BASE_S * (2 ** (state["consecutive_failures"] - 1)),
                    self.HEAL_BACKOFF_MAX_S)
                state["next_attempt_at"] = time.time() + backoff

    def heal(self, app_id: str, name: str) -> str:
        """Re-run the app's own installer for one missing CLI (idempotent —
        the same script every ``install_system_cli`` call already ran).

        Reentrancy-safe across a cancelled watchdog Task: ``watchdog.pause()``
        cancels the ``asyncio.Task`` awaiting this call (via
        ``asyncio.to_thread``), but cancelling that future does not stop
        THIS thread — it keeps running the installer subprocess to
        completion, untracked, until ``resume()`` starts a new watchdog
        loop that calls ``heal()`` again for the same CLI. An
        ``asyncio.Lock`` would not help: it releases the moment the
        awaiting Task is cancelled, while this thread is still running. So
        the guard is a plain ``threading.Lock`` held here, for the duration
        of the actual subprocess call, released only when this thread's own
        work genuinely finishes — that is what a repeated pause/resume flap
        actually needs blocked, and it is what produced 7 concurrent
        ``install_copilot.sh`` processes on the crispal host.

        Raises ``HealInFlightError`` (not a real failure — must not count
        against backoff/circuit-breaker state) if a heal for this exact CLI
        is already running.
        """
        key = (app_id, name)
        with self._heal_guard:
            if key in self._healing:
                raise HealInFlightError(
                    f"heal for {name!r} ({app_id}) already in flight")
            self._healing.add(key)
        try:
            package_dir, installer = self._system_clis[key]
            return self.run_installer(package_dir, installer)
        finally:
            with self._heal_guard:
                self._healing.discard(key)

    def _run_subprocess(self, cmd: list[str], *, cwd: str | None,
                         timeout: float) -> subprocess.CompletedProcess:
        """``subprocess.run``-alike, except the whole process TREE is killed
        on timeout instead of just the direct child.

        ``subprocess.run``'s own timeout handling only kills the process it
        forked directly (``bash``) — every descendant bash forked (npm,
        node, ...) is left running, reparented onto pid 1 the instant bash
        dies, exactly the leak that exhausted PIDs on the crispal host.
        Reuses ``kill_proc_tree`` (``src/api/terminal_manager.py``) rather
        than reimplementing it — it walks ``bash``'s descendants via
        ``/proc`` while ``bash`` (``proc.pid``) is still alive, which is why
        this uses ``Popen``/``communicate`` instead of ``subprocess.run``:
        ``run`` would have already killed and reaped ``bash`` by the time we
        got a chance to look at its children.
        """
        with subprocess.Popen(
            cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        ) as proc:
            try:
                stdout, stderr = proc.communicate(timeout=timeout)
            except subprocess.TimeoutExpired:
                kill_proc_tree(proc.pid)
                raise
        return subprocess.CompletedProcess(cmd, proc.returncode, stdout, stderr)

    def _run(self, package_dir: str, script: str, *, what: str) -> str:
        path = _resolve(package_dir, script)
        try:
            proc = self._run_subprocess(["bash", path], cwd=package_dir, timeout=self.timeout)
        except subprocess.TimeoutExpired:
            raise CommandError(f"{what} {script!r} timed out after {self.timeout:.0f}s")
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
