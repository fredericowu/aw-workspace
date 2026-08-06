"""aw-workspace entrypoint — slim, headless.

Binds uvicorn on ``AW_PORT`` using the ``create_app`` factory. Multi-worker
via ``AW_WORKSPACE_WORKERS`` (>1 requires the factory import string so each
worker process builds its own app/engine — same pattern as aw-backend's
``src/start/backend.py``).
"""
import os


def _sync_venv_deps():
    """Idempotently keep this process's venv in sync with requirements.txt.

    The venv is baked into the image at build time (Dockerfile), but from
    first boot onward it lives under the host bind-mount — same as this
    repo's own code, it does NOT get re-seeded on container recreation once
    the host dir is non-empty (aw-remote-host's install.sh never clobbers an
    existing host dir). A `git pull` can change requirements.txt on an
    already-bootstrapped host without ever rebuilding the image, so this
    reconciles on every boot and only re-installs when the file's hash
    changed — stdlib only (no httpx/fastapi/etc.), so it's safe to run
    before any third-party import, even one that's entirely new/missing.
    """
    import hashlib
    import subprocess
    import sys
    from pathlib import Path

    from src.apps.paths import workspace_home

    repo_root = Path(__file__).resolve().parents[2]
    req_file = repo_root / "requirements.txt"
    if not req_file.is_file():
        return

    venv_dir = Path(workspace_home()) / "venv"
    stamp_file = venv_dir / ".requirements.sha256"
    digest = hashlib.sha256(req_file.read_bytes()).hexdigest()

    if stamp_file.is_file() and stamp_file.read_text().strip() == digest:
        return

    print("aw-workspace: requirements.txt changed, syncing venv...")
    subprocess.run(
        [sys.executable, "-m", "pip", "install", "--no-cache-dir", "-r", str(req_file)],
        check=True,
    )
    venv_dir.mkdir(parents=True, exist_ok=True)
    stamp_file.write_text(digest + "\n")


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
