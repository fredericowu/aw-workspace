"""Which scoped gateway profiles this workspace's apps expect to exist.

``_referenced_gateway_profiles`` is the input to doctor's ``dead_profiles``
check. It reads ``contributes.agents[*].mcp_servers[*].profile`` — the only
place a manifest can name a ``/mcp/<name>`` profile — so a reference to one
nobody created is visible BEFORE an agent runs on it and gets 404 per request
with zero tools and no log entry anywhere.

There is an existing probe for the same 404 (``probe_scoped_profiles`` in
aw-app-agents-platform-runners' ``agent_provisioner.py``). It writes
``log.warning`` on every heal pass, which is the non-fix this workspace's
AGENTS.md names explicitly: repeated forever, read by nobody. It is left
alone; this is what actually surfaces the condition.

Run: aw-workspace-cli test src/tests/unit/apps/test_doctor_dead_profiles.py
"""
from __future__ import annotations

import json
import os
from types import SimpleNamespace

import pytest

from src.apps.paths import DEFAULT_WORKSPACE_CONTAINER_DIR
from src.apps.routes import _referenced_gateway_profiles


def _app(slug, agents):
    return SimpleNamespace(manifest=SimpleNamespace(id=slug, agents=agents))


class _Runtime:
    def __init__(self, apps):
        self._apps = apps

    def loaded_slugs(self):
        return list(self._apps)

    def get(self, slug):
        return self._apps.get(slug)


def test_a_scoped_agent_config_names_its_profile():
    rt = _Runtime({"marketing": _app("marketing", {"agent_configs": [
        {"slug": "agent-config-marketing",
         "mcp_servers": [{"name": "marketing", "server": "aw-gateway",
                          "profile": "marketing"}]},
    ]})})

    assert _referenced_gateway_profiles(rt) == {"marketing": "marketing"}


def test_a_plain_string_reference_names_no_profile():
    """``"aw-gateway"`` is the whole gateway, not a profile — reporting it
    would make every unscoped app a permanent false positive."""
    rt = _Runtime({"kb": _app("kb", {"agents": [
        {"slug": "kb-agent", "mcp_servers": ["aw-gateway"]},
    ]})})

    assert _referenced_gateway_profiles(rt) == {}


def test_a_reference_without_a_profile_key_names_no_profile():
    rt = _Runtime({"kb": _app("kb", {"agent_configs": [
        {"slug": "c", "mcp_servers": [{"name": "kb", "server": "aw-gateway"}]},
    ]})})

    assert _referenced_gateway_profiles(rt) == {}


def test_every_agent_kind_is_searched():
    """``agents`` and ``agent_configs`` both carry ``mcp_servers``; scanning
    one list and not the other would half-report."""
    rt = _Runtime({"x": _app("x", {
        "agent_configs": [{"slug": "c", "mcp_servers": [{"profile": "from-config"}]}],
        "agents": [{"slug": "a", "mcp_servers": [{"profile": "from-agent"}]}],
    })})

    assert _referenced_gateway_profiles(rt) == {
        "from-config": "x", "from-agent": "x"}


def test_an_app_contributing_no_agents_is_skipped():
    rt = _Runtime({"git": _app("git", {})})
    assert _referenced_gateway_profiles(rt) == {}


def test_a_slug_that_failed_to_load_does_not_break_the_report():
    rt = _Runtime({"ghost": None, "marketing": _app("marketing", {"agent_configs": [
        {"slug": "c", "mcp_servers": [{"profile": "marketing"}]},
    ]})})

    assert _referenced_gateway_profiles(rt) == {"marketing": "marketing"}


def test_the_real_marketing_manifest_references_the_marketing_profile():
    """The live case, read from the app's own manifest rather than restated:
    ``agent-config-marketing`` points at ``/mcp/marketing``, and on
    2026-09-21 that endpoint answered
    ``404 {"error":"No such config: marketing"}``.

    Skips where the repo isn't checked out — this pins the shape the check
    has to catch, not the presence of one clone.
    """
    root = os.environ.get("AW_WORKSPACE_CONTAINER_DIR", DEFAULT_WORKSPACE_CONTAINER_DIR)
    path = os.path.join(root, "repos", "aw-app-marketing", "aw-app.json")
    if not os.path.isfile(path):
        pytest.skip("repos/aw-app-marketing is not checked out here")

    with open(path, encoding="utf-8") as f:
        manifest = json.load(f)
    agents = manifest.get("contributes", {}).get("agents", {})
    rt = _Runtime({"marketing": _app("marketing", agents)})

    assert _referenced_gateway_profiles(rt) == {"marketing": "marketing"}
