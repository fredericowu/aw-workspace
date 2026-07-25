"""aw-workspace entrypoint — slim, headless.

Binds uvicorn on ``AW_PORT`` using the ``create_app`` factory. Multi-worker
via ``AW_WORKSPACE_WORKERS`` (>1 requires the factory import string so each
worker process builds its own app/engine — same pattern as aw-backend's
``src/start/backend.py``).
"""
import os


def main():
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
