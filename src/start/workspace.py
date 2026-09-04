"""aw-workspace entrypoint — slim, headless.

Binds uvicorn on ``AW_PORT`` using the ``create_app`` factory. Multi-worker
via ``AW_WORKSPACE_WORKERS`` (>1 requires the factory import string so each
worker process builds its own app/engine — same pattern as aw-backend's
``src/start/backend.py``).
"""
import os


def _sync_venv_deps(venv_dir=None, req_file=None):
    """Ensure the workspace venv exists on the persistent mount and matches
    requirements.txt — idempotent, boot-time, stdlib-only.

    The venv lives at ``$AW_WORKSPACE_HOME/venv`` (i.e.
    ``/opt/aw-workspace/.aw-workspace/venv``) — under the persistent host
    bind-mount / named volume, so it survives container recreation AND is
    shared with sibling app / CLI-agent containers (which PYTHONPATH-import
    its site-packages; see the Dockerfile venv comment and execute.py).

    The image bakes a venv there at build time, but a persistent mount
    SHADOWS the baked copy: a bind-mount always, a named volume once it's
    non-empty — and aw-remote-host's install.sh never clobbers an already-
    seeded host dir. A host first seeded by an older image (or an older
    layout that only wrote the ``.requirements.sha256`` stamp) therefore ends
    up with a stamp but NO real venv. So this boot reconciler — not the image
    — is the source of truth:

      * no real venv on the mount (no ``bin/python``)  -> create it, then
        ``pip install -r requirements.txt`` INTO it.
      * a real venv present                            -> NEVER delete or
        recreate it. Apps install their own deps into this same venv; wiping
        it would break them. Only ``pip install -r`` on top (adds/upgrades the
        listed packages, leaves everything else untouched) and only when
        requirements.txt's hash changed.

    The "already synced" fast-path is gated on BOTH the stamp matching AND
    ``bin/python`` existing, so a stamp-without-venv (the broken layout above)
    still triggers a build. Cross-platform (Linux + macOS hosts) — it only
    ever runs inside the Linux workspace container, and derives the venv's
    interpreter path rather than hardcoding a Python minor version.

    Runs before any third-party import, so it must stay stdlib-only. The
    ``venv_dir`` / ``req_file`` params exist for unit tests; production calls
    it with no args.
    """
    import hashlib
    import subprocess
    import sys
    from pathlib import Path

    from src.apps.paths import workspace_home

    if req_file is None:
        req_file = Path(__file__).resolve().parents[2] / "requirements.txt"
    else:
        req_file = Path(req_file)
    if not req_file.is_file():
        return

    venv_dir = Path(workspace_home()) / "venv" if venv_dir is None else Path(venv_dir)
    venv_python = venv_dir / "bin" / "python"
    stamp_file = venv_dir / ".requirements.sha256"
    digest = hashlib.sha256(req_file.read_bytes()).hexdigest()

    has_venv = venv_python.exists()
    in_sync = (
        has_venv
        and stamp_file.is_file()
        and stamp_file.read_text().strip() == digest
    )
    if in_sync:
        return

    if not has_venv:
        # Build the venv on the persistent mount. Use the CURRENT interpreter
        # as the base — when the venv is missing, PATH's venv/bin was skipped
        # so this is the image's base python (never the venv's own, which
        # doesn't exist yet). `-m venv` populates venv_dir in place and does
        # NOT remove unrelated files already there (e.g. a stale stamp).
        print(f"aw-workspace: no venv at {venv_dir} — creating it", flush=True)
        venv_dir.mkdir(parents=True, exist_ok=True)
        subprocess.run([sys.executable, "-m", "venv", str(venv_dir)], check=True)

    # Install / refresh requirements INTO THE VENV explicitly (not
    # sys.executable, which may be the base interpreter). Non-destructive on
    # an existing venv: pip adds/upgrades the listed packages and leaves
    # app-installed packages in place.
    print("aw-workspace: syncing venv with requirements.txt", flush=True)
    subprocess.run(
        [str(venv_python), "-m", "pip", "install", "--no-cache-dir", "-r", str(req_file)],
        check=True,
    )
    stamp_file.write_text(digest + "\n")


def _reexec_into_venv():
    """Re-exec this process under the workspace venv's interpreter.

    PID 1 boots under the image's BASE python (see the Dockerfile CMD) so the
    workspace can always start and repair/build the venv even when
    ``$AW_WORKSPACE_HOME/venv`` is missing or its ``bin/python`` is a dangling
    symlink. Once :func:`_sync_venv_deps` has ensured the venv exists, hand off
    to it so uvicorn and every third-party import resolve from the shared venv
    rather than the base image's site-packages. Idempotent via a sentinel env
    var so the exec'd child never loops; a no-op when already running under
    the venv interpreter (e.g. an older image whose CMD is still `python`).

    "Already under the venv" is decided by ``sys.prefix``, NOT by comparing the
    interpreter paths. ``python -m venv`` links ``venv/bin/python3`` straight at
    the base interpreter, so ``realpath`` collapses both sides to
    ``/usr/local/bin/python3`` and a path comparison reports "already there"
    while ``sys.path`` still points at the base image's site-packages. The
    re-exec was then skipped and boot died on ``import uvicorn`` — which is
    exactly what every fresh BYOD provision hit (2026-08-17, bare-metal): a
    seeded venv that is complete and correct, and a process that never enters
    it. ``sys.prefix`` is what actually distinguishes the two environments.
    """
    import os
    import sys

    from src.apps.paths import workspace_home

    if os.environ.get("AW_VENV_REEXEC") == "1":
        return

    venv_dir = os.path.join(workspace_home(), "venv")
    venv_python = os.path.join(venv_dir, "bin", "python")
    # exists() follows symlinks, so this also covers the dangling-link case the
    # base-python CMD exists for: nothing to hand off to, keep booting.
    if not os.path.exists(venv_python):
        return
    if os.path.realpath(sys.prefix) == os.path.realpath(venv_dir):
        return

    os.environ["AW_VENV_REEXEC"] = "1"
    os.execv(venv_python, [venv_python, "-m", "src.start.workspace", *sys.argv[1:]])


def _put_app_bin_dir_on_path():
    """Prepend the F4 app-shim dir (paths.bin_dir()) to PATH.

    paths.py/base.py/commands.py all claim shims there are "on PATH", but
    nothing ever actually put it on PATH — set it once, process-wide, before
    uvicorn starts so it's inherited by every subprocess this process spawns
    (terminal PTYs, apt installs, ...).
    """
    from src.apps.paths import bin_dir

    d = bin_dir()
    path = os.environ.get("PATH", "")
    if d not in path.split(os.pathsep):
        os.environ["PATH"] = f"{d}{os.pathsep}{path}" if path else d


def _resolve_workers() -> int:
    """AW_WORKSPACE_WORKERS ships baked into the image (Dockerfile ENV) as the
    default — that only changes on an image rebuild + container recreate; a
    plain process restart inherits the same baked value forever. Let
    ``<workspace_home>/.env`` override it at runtime instead, via
    ``src.apps.paths.read_workspace_env`` — the same fallback idea
    AW_BACKEND_URL/AW_WORKSPACE already use in ``src.cli.core_restart``, but
    with the order FLIPPED: ``.env`` checked first, ``os.environ`` (the image
    default) second. Unlike those two vars, ``os.environ`` here is *always*
    populated by the Dockerfile, so an env-first check would make the
    ``.env`` override a permanent no-op."""
    from src.apps.paths import read_workspace_env

    return int(read_workspace_env("AW_WORKSPACE_WORKERS") or os.environ.get("AW_WORKSPACE_WORKERS", "1"))


def _uvicorn_log_config():
    """Build a uvicorn ``log_config`` dict that also attaches a root-logger handler.

    uvicorn.run(..., log_level="info") only configures uvicorn's OWN loggers
    (``uvicorn``, ``uvicorn.error``, ``uvicorn.access``) via
    ``Config.configure_logging()`` — it never touches the root logger. Without
    this, every other module's ``logging.getLogger(__name__).info/warning(...)``
    call has no handler anywhere in its propagation chain and is silently
    dropped (INFO) or swallowed by Python's bare ``logging.lastResort``
    handler at WARNING+ with no useful format. Confirmed live (2026-09-04):
    terminal_manager.py's diagnostic logger.info/warning calls never reached
    podman logs while uvicorn's own access-log lines for the same requests
    did.

    This must be wired in via ``log_config=`` rather than a bare
    ``logging.basicConfig()`` call before ``uvicorn.run()`` — that would only
    configure the parent process. With ``AW_WORKSPACE_WORKERS>1``, uvicorn
    spawns each worker as a genuinely separate process
    (``multiprocessing.get_context("spawn")``, see
    ``uvicorn._subprocess.subprocess_started``) that never re-runs this
    module's ``main()``; it only calls ``Config.configure_logging()`` on
    itself, which applies whatever ``log_config`` dict was handed to
    ``uvicorn.run()`` via ``logging.config.dictConfig(...)``. Routing our root
    handler through that same mechanism is what makes it reach every worker
    — where requests (and terminal_manager.py's logging) actually happen —
    not just the parent supervisor.

    uvicorn's own loggers ship with ``propagate=False`` in its default
    dictConfig, so adding a root handler here does not double-print uvicorn's
    access/error lines.
    """
    from copy import deepcopy

    from uvicorn.config import LOGGING_CONFIG

    log_config = deepcopy(LOGGING_CONFIG)
    log_config["formatters"]["root"] = {
        "format": "%(asctime)s %(levelname)s %(name)s: %(message)s",
    }
    log_config["handlers"]["root"] = {
        "formatter": "root",
        "class": "logging.StreamHandler",
        "stream": "ext://sys.stdout",
    }
    log_config["root"] = {"handlers": ["root"], "level": "INFO"}
    return log_config


def main():
    _sync_venv_deps()
    _reexec_into_venv()  # everything below runs under the shared venv interpreter
    _put_app_bin_dir_on_path()

    # Mint boot_id/git_head/started_at into os.environ HERE — in the parent,
    # before uvicorn.run(workers=N) below forks/spawns workers — so every
    # worker inherits the same values via env instead of minting its own.
    # See src/api/boot_info.py.
    from src.api.boot_info import mint_boot_identity
    mint_boot_identity()

    port = int(os.environ.get("AW_PORT", "9030"))

    # Written back to os.environ so every downstream reader in this process
    # (and any uvicorn worker forked from it) — e.g. src.api.app's own
    # AW_WORKSPACE_WORKERS checks — sees the same resolved number rather
    # than the stale baked-in one. See _resolve_workers for why .env takes
    # precedence over os.environ here.
    workers = _resolve_workers()
    os.environ["AW_WORKSPACE_WORKERS"] = str(workers)

    workspace = os.environ.get("AW_WORKSPACE", "")

    print(f"aw-workspace starting  :{port}  workspace={workspace}  workers={workers}")

    import uvicorn

    log_config = _uvicorn_log_config()

    if workers > 1:
        uvicorn.run(
            "src.api.app:create_app",
            factory=True,
            host="0.0.0.0",
            port=port,
            workers=workers,
            log_level="info",
            log_config=log_config,
        )
    else:
        from src.api.app import create_app
        uvicorn.run(create_app(), host="0.0.0.0", port=port, log_level="info", log_config=log_config)


if __name__ == "__main__":
    main()
