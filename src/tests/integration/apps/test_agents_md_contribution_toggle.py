"""The ``agents_md_contribution_enabled`` toggle (design doc
``docs/design/agents-md-step2-migration-and-toggle.md`` §C) — a plain
``config_store`` value, injected into ``effective_config_schema`` whenever a
manifest declares ``contributes.agents_md``, and consulted by
``agent_sync._render_region()`` on every rebuild.

This pins design-doc test #3 ("Risks for the Coders"): a toggle flip through
``POST /api/apps/{slug}/config`` rebuilds AGENTS.md's managed region
immediately — and a save that does NOT touch the toggle must not.

Fully sandboxed: both ``AW_WORKSPACE_HOME`` *and* ``AW_WORKSPACE_CONTAINER_DIR``
point at a tmp dir, so this never reads or writes this repo's own real
AGENTS.md (``agent_sync.sync_agents_md()`` otherwise resolves straight off
``AW_WORKSPACE_CONTAINER_DIR``, and this suite runs inside a live workspace
container that ships a real one).
"""
from __future__ import annotations

import asyncio
import textwrap

from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api.identity import require_identity
from src.apps.routes import register_apps_routes


def _async(coro):
    return asyncio.run(coro)


class _FakeLocalReconciler:
    def __init__(self):
        self.saved: list[tuple[str, dict]] = []

    def update_config(self, app_id, config):
        self.saved.append((app_id, dict(config)))


def _client():
    app = FastAPI()
    runtime = register_apps_routes(app)
    app.state.app_reconciler.local = _FakeLocalReconciler()
    app.dependency_overrides[require_identity] = lambda: {"sub": "test"}
    return app, runtime, TestClient(app)


def _write_app(tmp_path, slug):
    pkg = tmp_path / slug
    pkg.mkdir()
    (pkg / "docs").mkdir()
    (pkg / "docs" / "AGENTS-block.md").write_text("Demo contribution content.\n")
    (pkg / "aw-app.json").write_text(textwrap.dedent(f"""
    {{
      "manifest_version": 1,
      "id": "{slug}",
      "name": "{slug}",
      "version": "1.0.0",
      "tier": "inprocess",
      "runtime": {{"entrypoint": "plugin:AppPlugin"}},
      "permissions": [],
      "contributes": {{
        "agents_md": {{"path": "docs/AGENTS-block.md", "title": "Demo"}}
      }}
    }}
    """))
    (pkg / "plugin.py").write_text(textwrap.dedent("""
        class AppPlugin:
            async def activate(self, ctx):
                return None
            async def deactivate(self):
                return None
    """))
    return str(pkg)


def _sandbox(tmp_path, monkeypatch):
    monkeypatch.setenv("AW_WORKSPACE_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("AW_WORKSPACE_CONTAINER_DIR", str(tmp_path))
    (tmp_path / "AGENTS.md").write_text(
        "# Agent instructions\n\n"
        "<!-- aw-apps-md:begin -->\n"
        "<!-- GENERATED SECTION -->\n"
        "<!-- aw-apps-md:end -->\n"
    )


def test_toggle_flip_through_the_save_endpoint_rebuilds_the_region(tmp_path, monkeypatch):
    _sandbox(tmp_path, monkeypatch)
    pkg = _write_app(tmp_path, "demoapp")
    app, runtime, client = _client()
    _async(runtime.load(pkg, granted_permissions=[]))

    agents_md_path = tmp_path / "AGENTS.md"
    assert "Demo contribution content." in agents_md_path.read_text()

    # A save that does not touch the toggle must not resync — "and only then".
    before = agents_md_path.read_text()
    res = client.post("/api/apps/demoapp/config", json={"config": {"unrelated": "x"}})
    assert res.status_code == 200
    assert agents_md_path.read_text() == before

    # Flipping the toggle off removes the block immediately, via the save
    # endpoint alone — no separate `agent sync` CLI invocation needed.
    res = client.post("/api/apps/demoapp/config",
                      json={"config": {"agents_md_contribution_enabled": False}})
    assert res.status_code == 200
    assert "Demo contribution content." not in agents_md_path.read_text()

    # ...and flipping it back on brings the block back.
    res = client.post("/api/apps/demoapp/config",
                      json={"config": {"agents_md_contribution_enabled": True}})
    assert res.status_code == 200
    assert "Demo contribution content." in agents_md_path.read_text()
