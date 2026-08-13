"""``ctx.routes.register`` refuses routes core already serves.

The bug this guards is silent shadowing: core registers
``GET /api/apps/{slug}/ui/{path:path}`` (component-mode ESM bundles) and
matches it BEFORE an app's ``Mount``, so an app route under ``/ui/`` is
unreachable while every sibling route answers fine. It cannot be reproduced
with ``TestClient(build_routes(...))`` — that mounts the sub-app alone — so
without this check the only way to find it is a real install.
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI

from src.apps.base import RoutesFacade


def _app_with(*paths: str) -> FastAPI:
    app = FastAPI()
    for path in paths:
        app.get(path)(lambda: {"ok": True})
    return app


@pytest.mark.parametrize("path", [
    "/ui/hosts",
    "/ui/anything/deeper",
    "/config",
    "/install-status",
    "/versions",
    "/settings",
    "/update",
])
def test_reserved_paths_are_detected(path):
    assert RoutesFacade._reserved_conflicts(_app_with(path)) == [path]


@pytest.mark.parametrize("path", [
    "/panel/hosts",
    "/hosts",
    "/tunnels",
    "/ws/bridge/{id}",
    # Near-misses that must NOT trip: a longer segment starting with the same
    # letters is a different path, and refusing it would be a false positive
    # that blocks a legitimate install.
    "/uibuilder",
    "/configuration-wizard",
    "/settings-export",
])
def test_ordinary_paths_are_allowed(path):
    assert RoutesFacade._reserved_conflicts(_app_with(path)) == []


def test_conflicts_are_reported_together():
    """One install should surface every offending path, not just the first —
    fixing them one round-trip at a time is the slow version of this bug."""
    found = RoutesFacade._reserved_conflicts(_app_with("/ui/a", "/hosts", "/config"))
    assert sorted(found) == ["/config", "/ui/a"]
