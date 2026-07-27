"""F1 proof-of-concept plugin.

Ships one backend route. On ``activate`` it builds its OWN FastAPI sub-app and
registers it through ``ctx.routes`` — the runtime mounts it at
``/api/apps/hello`` with no restart, and unmounts it on uninstall.
"""
from __future__ import annotations

from fastapi import FastAPI


class HelloPlugin:
    async def activate(self, ctx):
        api = FastAPI(title="hello-app")

        @api.get("/")
        async def root():
            return {"app": "hello", "ok": True, "version": ctx.version}

        @api.get("/greet/{name}")
        async def greet(name: str):
            return {"message": f"Hello, {name}!", "app": "hello"}

        ctx.routes.register(api)

    async def deactivate(self):
        return None
