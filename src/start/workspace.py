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
    """
    import os
    import sys

    from src.apps.paths import workspace_home

    if os.environ.get("AW_VENV_REEXEC") == "1":
        return

    venv_python = os.path.join(workspace_home(), "venv", "bin", "python")
    if not os.path.exists(venv_python):
        return
    if os.path.realpath(venv_python) == os.path.realpath(sys.executable):
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


def main():
    _sync_venv_deps()
    _reexec_into_venv()  # everything below runs under the shared venv interpreter
    _put_app_bin_dir_on_path()

    port = int(os.environ.get("AW_PORT", "9030"))
    workers = int(os.environ.get("AW_WORKSPACE_WORKERS", "1"))
    workspace = os.environ.get("AW_WORKSPACE", "")

    print(f"aw-workspace starting  :{port}  workspace={workspace}  workers={workers}")

    import uvicorn

    if workers > 1:
        uvicorn.run(
            "src.api.app:create_app",
            factory=True,
            host="0.0.0.0",
            port=port,
            workers=workers,
            log_level="info",
        )
    else:
        from src.api.app import create_app
        uvicorn.run(create_app(), host="0.0.0.0", port=port, log_level="info")


if __name__ == "__main__":
    main()
