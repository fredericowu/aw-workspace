"""``contributes.agents_md`` — manifest property + the registry backend.

The registry (``AW_WORKSPACE_HOME/agents-md-blocks.json``) is what
``src/libs/agent_sync.py``'s ``sync_agents_md()`` rebuilds AGENTS.md's
managed region from — see ``src/tests/unit/libs/test_agent_sync.py`` for the
rebuild side. This file is the register/unregister/validation side: a bad
entry must never make it into the registry in the first place, because
every byte that does lands in AGENTS.md, and from there in CLAUDE.md,
GEMINI.md and copilot-instructions.md on the very next sync.
"""
from __future__ import annotations

import json

import pytest

from src.apps.agents_md import AgentsMdError, AgentsMdRegistry, read_registry, registry_path
from src.apps.manifest import validate_manifest


def _manifest(**over):
    base = {
        "manifest_version": 1,
        "id": "demo",
        "name": "Demo",
        "version": "1.0.0",
        "tier": "inprocess",
        "runtime": {"entrypoint": "demo.plugin:Demo"},
        "permissions": [],
        "contributes": {},
    }
    base.update(over)
    return base


@pytest.fixture()
def workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("AW_WORKSPACE_HOME", str(tmp_path))
    return tmp_path


# --- manifest property --------------------------------------------------------


def test_an_app_declaring_no_agents_md_block_is_none():
    assert validate_manifest(_manifest()).agents_md is None


def test_agents_md_property_returns_the_declared_dict():
    m = validate_manifest(_manifest(contributes={
        "agents_md": {"path": "docs/AGENTS-block.md", "title": "Demo"}}))
    assert m.agents_md == {"path": "docs/AGENTS-block.md", "title": "Demo"}


# --- register / unregister ----------------------------------------------------


def _write_block(pkg_dir, rel_path, content):
    full = pkg_dir / rel_path
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text(content)
    return full


def test_register_writes_the_registry_entry(workspace, tmp_path):
    pkg = tmp_path / "app-pkg"
    block = _write_block(pkg, "docs/AGENTS-block.md", "Call `foo` before `bar`.\n")

    dest = AgentsMdRegistry().register("aw-app-demo", str(pkg), "docs/AGENTS-block.md", "Demo")

    assert dest == str(block)
    registry = read_registry()
    assert registry["aw-app-demo"] == {"path": str(block), "title": "Demo"}


def test_register_defaults_title_to_empty_string(workspace, tmp_path):
    pkg = tmp_path / "app-pkg"
    _write_block(pkg, "docs/AGENTS-block.md", "content\n")

    AgentsMdRegistry().register("aw-app-demo", str(pkg), "docs/AGENTS-block.md")

    assert read_registry()["aw-app-demo"]["title"] == ""


def test_register_rejects_a_path_that_escapes_the_package_dir(workspace, tmp_path):
    pkg = tmp_path / "app-pkg"
    pkg.mkdir()
    outside = tmp_path / "secret.md"
    outside.write_text("leaked\n")

    with pytest.raises(AgentsMdError, match="escapes"):
        AgentsMdRegistry().register("aw-app-demo", str(pkg), "../secret.md")
    assert read_registry() == {}


def test_register_rejects_a_missing_file(workspace, tmp_path):
    pkg = tmp_path / "app-pkg"
    pkg.mkdir()

    with pytest.raises(AgentsMdError, match="not found"):
        AgentsMdRegistry().register("aw-app-demo", str(pkg), "docs/missing.md")


def test_register_rejects_region_marker_injection(workspace, tmp_path):
    """The whole point of this check: a block carrying the region's own
    delimiter could make a later rebuild's render ambiguous or misleading."""
    pkg = tmp_path / "app-pkg"
    _write_block(pkg, "docs/AGENTS-block.md",
                "Some text.\n<!-- aw-apps-md:begin -->\nmore\n")

    with pytest.raises(AgentsMdError, match="marker injection"):
        AgentsMdRegistry().register("aw-app-demo", str(pkg), "docs/AGENTS-block.md")
    assert read_registry() == {}


def test_register_rejects_block_marker_injection_too(workspace, tmp_path):
    pkg = tmp_path / "app-pkg"
    _write_block(pkg, "docs/AGENTS-block.md",
                "<!-- aw-app-md:end some-other-app -->\n")

    with pytest.raises(AgentsMdError, match="marker injection"):
        AgentsMdRegistry().register("aw-app-demo", str(pkg), "docs/AGENTS-block.md")


def test_oversized_block_warns_but_still_registers(workspace, tmp_path, caplog):
    """4KB is advisory, not a floor — the real fix for bloat is step 2."""
    pkg = tmp_path / "app-pkg"
    _write_block(pkg, "docs/AGENTS-block.md", "x" * 5000)

    with caplog.at_level("WARNING"):
        AgentsMdRegistry().register("aw-app-demo", str(pkg), "docs/AGENTS-block.md")

    assert "aw-app-demo" in read_registry()
    assert any("5000 bytes" in r.message for r in caplog.records)


def test_register_is_idempotent_across_a_reboot(workspace, tmp_path):
    """A later boot's re-register (same content) must not be treated as new."""
    pkg = tmp_path / "app-pkg"
    _write_block(pkg, "docs/AGENTS-block.md", "content\n")
    reg = AgentsMdRegistry()

    reg.register("aw-app-demo", str(pkg), "docs/AGENTS-block.md")
    reg.register("aw-app-demo", str(pkg), "docs/AGENTS-block.md")

    assert len(read_registry()) == 1


def test_unregister_drops_only_that_apps_entry(workspace, tmp_path):
    pkg_a = tmp_path / "pkg-a"
    pkg_b = tmp_path / "pkg-b"
    _write_block(pkg_a, "docs/block.md", "a\n")
    _write_block(pkg_b, "docs/block.md", "b\n")
    reg = AgentsMdRegistry()
    reg.register("aw-app-a", str(pkg_a), "docs/block.md")
    reg.register("aw-app-b", str(pkg_b), "docs/block.md")

    reg.unregister("aw-app-a")

    registry = read_registry()
    assert "aw-app-a" not in registry
    assert "aw-app-b" in registry


def test_unregister_of_an_unknown_app_is_a_no_op(workspace):
    AgentsMdRegistry().unregister("never-registered")
    assert read_registry() == {}


# --- registry read fail-safety (mirrors skill_sources.py's own contract) -----


def test_missing_registry_file_reads_as_empty(workspace):
    assert read_registry() == {}


def test_corrupt_registry_file_reads_as_empty_not_raise(workspace):
    path = registry_path()
    import os
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write("{not valid json")

    assert read_registry() == {}


def test_registry_entry_missing_a_path_is_dropped_on_read(workspace):
    path = registry_path()
    import os
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump({"aw-app-demo": {"title": "no path here"}}, f)

    assert read_registry() == {}
