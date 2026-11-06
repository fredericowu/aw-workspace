"""``aw-workspace-cli agent sync`` — the skills mirror and the AGENTS.md fan-out.

What matters here is the *exact mirror* contract: a skill deleted from the
source has to disappear from every agent dir, because a stale SKILL.md keeps
teaching agents to call tools that were uninstalled. The monolith learned
this the hard way; these tests pin it.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from src.libs import agent_sync, skills_sync


@pytest.fixture()
def workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("AW_WORKSPACE_CONTAINER_DIR", str(tmp_path))
    (tmp_path / "skills" / "aw-demo").mkdir(parents=True)
    (tmp_path / "skills" / "aw-demo" / "SKILL.md").write_text(
        "---\nname: aw-demo\ndescription: A demo skill\n---\n\nbody\n")
    (tmp_path / "AGENTS.md").write_text("# Agent instructions\n")
    (tmp_path / ".mcp.json").write_text(json.dumps({"mcpServers": {
        "aw-gateway": {"type": "http", "url": "http://gw:9200/mcp"},
        "user-own-thing": {"command": "whatever"},
    }}))
    return tmp_path


# --- skills mirror -----------------------------------------------------------


def test_skills_are_mirrored_into_every_agent_dir(workspace):
    results = skills_sync.sync_all()

    assert len(results) == 3
    for target in skills_sync.targets():
        assert (target / "aw-demo" / "SKILL.md").read_text().endswith("body\n")
    assert all(len(r.added) == 1 for r in results)


def test_second_sync_is_a_no_op(workspace):
    skills_sync.sync_all()
    results = skills_sync.sync_all()

    assert all(not r.changed for r in results)
    assert all(r.unchanged == 1 for r in results)


def test_edited_skill_is_propagated(workspace):
    skills_sync.sync_all()
    (workspace / "skills" / "aw-demo" / "SKILL.md").write_text("---\nname: aw-demo\n---\nNEW\n")

    results = skills_sync.sync_all()

    assert all(len(r.updated) == 1 for r in results)
    for target in skills_sync.targets():
        assert "NEW" in (target / "aw-demo" / "SKILL.md").read_text()


def test_deleted_skill_is_removed_from_mirrors(workspace):
    """Exact-mirror semantics — the whole reason this isn't a plain copy."""
    skills_sync.sync_all()
    for f in (workspace / "skills" / "aw-demo").iterdir():
        f.unlink()
    (workspace / "skills" / "aw-demo").rmdir()

    results = skills_sync.sync_all()

    assert all(len(r.deleted) == 1 for r in results)
    for target in skills_sync.targets():
        # The now-empty directory is pruned too, not left as a shell.
        assert not (target / "aw-demo").exists()


def test_missing_source_is_a_skip_not_a_crash(tmp_path, monkeypatch):
    monkeypatch.setenv("AW_WORKSPACE_CONTAINER_DIR", str(tmp_path))
    assert skills_sync.sync_all() == []


def test_list_skills_reads_frontmatter(workspace):
    assert skills_sync.list_skills() == [
        {"name": "aw-demo", "description": "A demo skill",
         "path": str(workspace / "skills" / "aw-demo" / "SKILL.md"),
         "rel_path": "skills/aw-demo/SKILL.md",
         "editable": True, "owner": None},
    ]


# --- skills/ is generated from two sources -----------------------------------
#
# skills/ is gitignored and rebuilt: native-skills/ is committed here, app
# skills are copied in by src/apps/skills.py. The rule that keeps the two from
# eating each other is the .aw-app-id marker — without it, the first sync after
# boot deletes every installed app's skill, and nothing reports it.


@pytest.fixture()
def two_source_workspace(tmp_path, monkeypatch):
    """``native-skills/aw-native`` committed + ``skills/aw-from-app`` app-owned."""
    monkeypatch.setenv("AW_WORKSPACE_CONTAINER_DIR", str(tmp_path))

    native = tmp_path / "native-skills" / "aw-native"
    native.mkdir(parents=True)
    (native / "SKILL.md").write_text("---\nname: aw-native\n---\nnative body\n")

    from_app = tmp_path / "skills" / "aw-from-app"
    from_app.mkdir(parents=True)
    (from_app / "SKILL.md").write_text("---\nname: aw-from-app\n---\napp body\n")
    (from_app / skills_sync.OWNER_MARKER).write_text("some-app")

    return tmp_path


def test_native_skill_is_materialized_into_skills(two_source_workspace):
    skills_sync.sync_all()

    assert (two_source_workspace / "skills" / "aw-native" / "SKILL.md").exists()
    for target in skills_sync.targets():
        assert (target / "aw-native" / "SKILL.md").read_text().endswith("native body\n")


def test_app_owned_skill_survives_a_sync(two_source_workspace):
    """The regression this split could cause: skills/ is an exact mirror, and an
    app's skill has no counterpart in native-skills/. Deleting on that basis
    would wipe every installed app's skill on the first boot sync."""
    skills_sync.sync_all()

    assert (two_source_workspace / "skills" / "aw-from-app" / "SKILL.md").exists()
    # ...and it reaches the agents, exactly like a native one.
    for target in skills_sync.targets():
        assert (target / "aw-from-app" / "SKILL.md").read_text().endswith("app body\n")


def test_stale_native_skill_is_deleted_but_app_skills_are_untouched(two_source_workspace):
    skills_sync.sync_all()
    shutil.rmtree(two_source_workspace / "native-skills" / "aw-native")

    result = skills_sync.materialize()

    assert result.deleted == ["aw-native/SKILL.md"]
    assert not (two_source_workspace / "skills" / "aw-native").exists()
    assert (two_source_workspace / "skills" / "aw-from-app" / "SKILL.md").exists()


def test_generated_marker_warns_in_skills_but_is_not_mirrored(two_source_workspace):
    skills_sync.sync_all()

    marker = two_source_workspace / "skills" / skills_sync.GENERATED_MARKER
    assert "GENERATED" in marker.read_text()
    for target in skills_sync.targets():
        assert not (target / skills_sync.GENERATED_MARKER).exists()


def test_materialize_is_idempotent(two_source_workspace):
    skills_sync.materialize()
    result = skills_sync.materialize()

    assert not result.changed


def test_list_skills_marks_app_owned_entries_read_only(two_source_workspace):
    skills_sync.sync_all()

    by_name = {s["name"]: s for s in skills_sync.list_skills()}
    assert by_name["aw-native"]["editable"] is True
    assert by_name["aw-native"]["owner"] is None
    assert by_name["aw-from-app"]["editable"] is False
    assert by_name["aw-from-app"]["owner"] == "some-app"


# --- create_skill / delete_skill (Settings > Skills CRUD) --------------------
#
# Both operate on native-skills/, never on the generated skills/ merge — a
# skill written straight into skills/ with no native-skills/ backing would be
# deleted as stale on the very next materialize() (see its docstring).


def test_create_skill_writes_to_native_skills_not_the_merged_tree(tmp_path, monkeypatch):
    monkeypatch.setenv("AW_WORKSPACE_CONTAINER_DIR", str(tmp_path))

    path = skills_sync.create_skill("aw-new-thing", "Does a new thing.")

    assert path == tmp_path / "native-skills" / "aw-new-thing" / "SKILL.md"
    assert path.is_file()
    assert "name: aw-new-thing" in path.read_text()
    assert "description: Does a new thing." in path.read_text()
    assert not (tmp_path / "skills" / "aw-new-thing").exists()


def test_create_skill_rejects_invalid_name(tmp_path, monkeypatch):
    monkeypatch.setenv("AW_WORKSPACE_CONTAINER_DIR", str(tmp_path))

    with pytest.raises(ValueError):
        skills_sync.create_skill("Not Valid!")


def test_create_skill_rejects_collision_with_existing_native(tmp_path, monkeypatch):
    monkeypatch.setenv("AW_WORKSPACE_CONTAINER_DIR", str(tmp_path))
    skills_sync.create_skill("aw-dup")

    with pytest.raises(ValueError):
        skills_sync.create_skill("aw-dup")


def test_create_skill_rejects_collision_with_app_owned(two_source_workspace):
    with pytest.raises(ValueError):
        skills_sync.create_skill("aw-from-app")


def test_delete_skill_removes_the_native_directory(tmp_path, monkeypatch):
    monkeypatch.setenv("AW_WORKSPACE_CONTAINER_DIR", str(tmp_path))
    skills_sync.create_skill("aw-temp")

    assert skills_sync.delete_skill("aw-temp") is True
    assert not (tmp_path / "native-skills" / "aw-temp").exists()


def test_delete_skill_returns_false_for_an_unknown_name(tmp_path, monkeypatch):
    monkeypatch.setenv("AW_WORKSPACE_CONTAINER_DIR", str(tmp_path))

    assert skills_sync.delete_skill("does-not-exist") is False


def test_delete_skill_refuses_an_app_owned_entry(two_source_workspace):
    skills_sync.sync_all()

    with pytest.raises(ValueError):
        skills_sync.delete_skill("aw-from-app")
    assert (two_source_workspace / "skills" / "aw-from-app").exists()


# --- AGENTS.md ---------------------------------------------------------------
#
# sync_agents_md() rebuilds the managed region from the agents_md registry
# BEFORE the per-CLI fan-out — see src/apps/agents_md.py. No app has
# registered a block in the plain `workspace` fixture, so the region
# rebuilds to its empty form (banner + no blocks), appended at EOF the first
# time since the fixture's AGENTS.md starts with no markers at all.


def test_agents_md_is_mirrored_to_every_cli_filename(workspace):
    results = agent_sync.sync_agents_md()

    assert all(r.success and r.changed for r in results)
    claude = (workspace / "CLAUDE.md").read_text()
    assert claude.startswith("# Agent instructions\n")
    assert agent_sync.REGION_BEGIN in claude
    assert agent_sync.REGION_END in claude
    assert (workspace / "GEMINI.md").read_text() == claude
    assert (workspace / ".github" / "copilot-instructions.md").exists()


def test_agents_md_resync_reports_no_change(workspace):
    agent_sync.sync_agents_md()
    assert all(not r.changed for r in agent_sync.sync_agents_md())


def test_agents_md_missing_source_fails_cleanly(workspace):
    (workspace / "AGENTS.md").unlink()
    results = agent_sync.sync_agents_md()
    assert all(not r.success and "not found" in r.error for r in results)


# --- AGENTS.md managed-region rebuild -----------------------------------------


def _register_block(workspace, app_id, content, title=None):
    from src.apps.agents_md import AgentsMdRegistry

    pkg = workspace / "pkg" / app_id
    pkg.mkdir(parents=True)
    block = pkg / "AGENTS-block.md"
    block.write_text(content)
    AgentsMdRegistry().register(app_id, str(pkg), "AGENTS-block.md", title)
    return block


def test_region_absent_is_appended_at_eof(workspace):
    before = (workspace / "AGENTS.md").read_text()
    agent_sync.sync_agents_md()

    after = (workspace / "AGENTS.md").read_text()
    assert after.startswith(before)
    assert after.count(agent_sync.REGION_BEGIN) == 1
    assert after.count(agent_sync.REGION_END) == 1


def test_a_registered_blocks_content_appears_in_the_rebuilt_region(workspace):
    _register_block(workspace, "aw-app-demo", "Call `foo` before `bar`.\n", "Demo")

    agent_sync.sync_agents_md()

    agents_md = (workspace / "AGENTS.md").read_text()
    assert "## Demo (app: aw-app-demo)" in agents_md
    assert "Call `foo` before `bar`." in agents_md
    # ...and it reaches the mirrors like everything else in the file.
    assert "Call `foo` before `bar`." in (workspace / "CLAUDE.md").read_text()


def test_unregistering_an_app_removes_its_block_on_the_next_sync(workspace):
    from src.apps.agents_md import AgentsMdRegistry

    _register_block(workspace, "aw-app-demo", "Demo content.\n", "Demo")
    agent_sync.sync_agents_md()
    assert "Demo content." in (workspace / "AGENTS.md").read_text()

    AgentsMdRegistry().unregister("aw-app-demo")
    agent_sync.sync_agents_md()

    agents_md = (workspace / "AGENTS.md").read_text()
    assert "Demo content." not in agents_md
    assert agent_sync.REGION_BEGIN in agents_md  # the (now empty) region stays


def test_rebuild_is_a_no_op_once_the_region_matches_the_registry(workspace):
    _register_block(workspace, "aw-app-demo", "Demo content.\n")
    agent_sync.sync_agents_md()

    results = agent_sync.sync_agents_md()

    assert all(r.success and not r.changed for r in results)


def test_two_blocks_are_rendered_sorted_by_app_id(workspace):
    _register_block(workspace, "aw-app-zebra", "Z content.\n")
    _register_block(workspace, "aw-app-alpha", "A content.\n")

    agent_sync.sync_agents_md()

    agents_md = (workspace / "AGENTS.md").read_text()
    assert agents_md.index("A content.") < agents_md.index("Z content.")


def test_a_block_whose_source_file_vanished_is_dropped_with_a_warning(workspace, caplog):
    block = _register_block(workspace, "aw-app-demo", "Demo content.\n")
    block.unlink()

    with caplog.at_level("WARNING"):
        results = agent_sync.sync_agents_md()

    assert all(r.success for r in results)
    assert "Demo content." not in (workspace / "AGENTS.md").read_text()
    assert any("aw-app-demo" in r.message for r in caplog.records)


def test_malformed_region_refuses_the_rewrite_and_leaves_the_file_untouched(workspace):
    """The core safety property: a bad rewrite here is instantly
    workspace-wide (AGENTS.md mirrors into CLAUDE.md/GEMINI.md/copilot).
    A duplicate begin marker must never be guessed at or truncated away."""
    agents_md_path = workspace / "AGENTS.md"
    before = (agents_md_path.read_text() + f"\n{agent_sync.REGION_BEGIN}\n"
             f"one\n{agent_sync.REGION_END}\n\n{agent_sync.REGION_BEGIN}\n"
             f"two\n{agent_sync.REGION_END}\n")
    agents_md_path.write_text(before)

    results = agent_sync.sync_agents_md()

    assert all(not r.success for r in results)
    assert agents_md_path.read_text() == before  # byte-identical — never truncated


def test_end_before_begin_is_also_refused(workspace):
    agents_md_path = workspace / "AGENTS.md"
    before = (agents_md_path.read_text() +
             f"\n{agent_sync.REGION_END}\n{agent_sync.REGION_BEGIN}\n")
    agents_md_path.write_text(before)

    results = agent_sync.sync_agents_md()

    assert all(not r.success for r in results)
    assert agents_md_path.read_text() == before


# --- agents_md_contribution_enabled toggle ------------------------------------
#
# The toggle (`src/apps/manifest.py:effective_config_schema`) is a plain
# config_store value, gated inside `_render_region` — not a registry field.
# These pin design doc "Risks for the Coders" tests 1, 2 and 4.


def test_render_region_skips_a_disabled_app_and_includes_an_absent_key_app(workspace):
    from src.apps import config_store

    _register_block(workspace, "aw-app-demo", "Demo content.\n")
    _register_block(workspace, "aw-app-other", "Other content.\n")
    config_store.save("aw-app-demo", {"agents_md_contribution_enabled": False})

    agent_sync.sync_agents_md()

    agents_md = (workspace / "AGENTS.md").read_text()
    assert "Demo content." not in agents_md
    assert "Other content." in agents_md  # no config saved for it — default True


def test_region_drift_check_is_clean_for_a_disabled_and_synced_state(workspace):
    from src.apps import config_store

    _register_block(workspace, "aw-app-demo", "Demo content.\n")
    config_store.save("aw-app-demo", {"agents_md_contribution_enabled": False})
    agent_sync.sync_agents_md()

    assert agent_sync.agents_md_region_drift() is None


def test_toggle_flip_does_not_touch_the_registry_file(workspace):
    """Pure-mirror invariant: the registry records what the manifest
    declared, never whether the block is currently shown."""
    from src.apps import agents_md, config_store

    _register_block(workspace, "aw-app-demo", "Demo content.\n")
    before = Path(agents_md.registry_path()).read_bytes()

    config_store.save("aw-app-demo", {"agents_md_contribution_enabled": False})
    agent_sync.sync_agents_md()

    after = Path(agents_md.registry_path()).read_bytes()
    assert after == before


def test_region_drift_check_is_read_only(workspace):
    """``agent sync --check``'s primitive: must report drift without writing."""
    _register_block(workspace, "aw-app-demo", "Demo content.\n")
    before = (workspace / "AGENTS.md").read_text()

    drift = agent_sync.agents_md_region_drift()

    assert drift is not None
    assert (workspace / "AGENTS.md").read_text() == before  # unchanged — check never writes


def test_region_drift_check_is_clean_once_synced(workspace):
    _register_block(workspace, "aw-app-demo", "Demo content.\n")
    agent_sync.sync_agents_md()

    assert agent_sync.agents_md_region_drift() is None


def test_region_drift_check_reports_a_malformed_region(workspace):
    agents_md_path = workspace / "AGENTS.md"
    agents_md_path.write_text(
        agents_md_path.read_text() + f"\n{agent_sync.REGION_BEGIN}\n")

    drift = agent_sync.agents_md_region_drift()

    assert drift is not None and "malformed" in drift


# --- MCP ---------------------------------------------------------------------


def test_mcp_is_copied_byte_for_byte(workspace):
    results = agent_sync.sync_mcp()

    assert all(r.success for r in results)
    assert (workspace / ".cursor" / "mcp.json").read_bytes() == \
        (workspace / ".mcp.json").read_bytes()


def test_gemini_manages_only_aw_prefixed_servers(workspace):
    """A server the user added to Gemini themselves must survive a sync."""
    settings = workspace / ".gemini" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text(json.dumps({
        "theme": "dark",
        "mcpServers": {"my-own": {"command": "mine"}},
    }))

    agent_sync.sync_gemini_mcp()

    data = json.loads(settings.read_text())
    assert data["theme"] == "dark"                    # unrelated keys preserved
    assert "my-own" in data["mcpServers"]             # unmanaged entry preserved
    assert "aw-gateway" in data["mcpServers"]         # managed entry added
    assert "user-own-thing" not in data["mcpServers"]  # not ours to push


def test_codex_skips_cleanly_when_the_cli_is_absent(workspace, monkeypatch):
    monkeypatch.setattr(agent_sync.shutil, "which", lambda _: None)

    results = agent_sync.sync_codex_mcp()

    assert len(results) == 1
    assert results[0].action == "skip"
    assert "not installed" in results[0].error


def test_codex_add_never_passes_the_unsupported_header_flag(workspace, monkeypatch):
    """`codex mcp add` has no --header (codex-cli 0.147.0). Passing it aborted
    the add, so aw-gateway — the only entry with an Authorization header —
    never reached Codex on any sync."""
    calls = []
    monkeypatch.setattr(agent_sync.subprocess, "run",
                        lambda cmd, **kw: calls.append(cmd) or _ok())
    monkeypatch.setattr(agent_sync, "_codex_write_http_headers", lambda *a: None)

    agent_sync._codex_add("aw-gateway", {
        "type": "http", "url": "http://gw:9200/mcp",
        "headers": {"Authorization": "Bearer tok"},
    })

    assert "--header" not in calls[0]
    assert calls[0][:5] == ["codex", "mcp", "add", "aw-gateway", "--url"]


def test_codex_headers_land_in_config_as_an_http_headers_table(tmp_path, monkeypatch):
    """The headers still have to arrive — via the sub-table codex DOES read."""
    codex_home = tmp_path / ".codex"
    codex_home.mkdir()
    cfg = codex_home / "config.toml"
    cfg.write_text('[mcp_servers.aw-gateway]\nurl = "http://gw:9200/mcp"\n')
    monkeypatch.setenv("CODEX_HOME", str(codex_home))

    agent_sync._codex_write_http_headers("aw-gateway", {"Authorization": "Bearer tok"})

    body = cfg.read_text()
    assert "[mcp_servers.aw-gateway.http_headers]" in body
    assert 'Authorization = "Bearer tok"' in body


def test_codex_header_table_is_rewritten_not_duplicated(tmp_path, monkeypatch):
    """A duplicate table would make config.toml unparseable and take every
    other MCP server down with it, so a re-sync must replace, not append."""
    codex_home = tmp_path / ".codex"
    codex_home.mkdir()
    cfg = codex_home / "config.toml"
    cfg.write_text('[mcp_servers.aw-gateway]\nurl = "http://gw:9200/mcp"\n')
    monkeypatch.setenv("CODEX_HOME", str(codex_home))

    agent_sync._codex_write_http_headers("aw-gateway", {"Authorization": "Bearer old"})
    agent_sync._codex_write_http_headers("aw-gateway", {"Authorization": "Bearer new"})

    body = cfg.read_text()
    assert body.count("[mcp_servers.aw-gateway.http_headers]") == 1
    assert "Bearer old" not in body
    assert 'Authorization = "Bearer new"' in body
    # The entry codex mcp add wrote must survive the rewrite.
    assert 'url = "http://gw:9200/mcp"' in body


def _ok():
    class _P:
        returncode = 0
        stdout = ""
        stderr = ""
    return _P()


def test_sync_all_runs_every_step(workspace, monkeypatch):
    monkeypatch.setattr(agent_sync.shutil, "which", lambda _: None)

    result = agent_sync.sync_all()

    assert len(result.skills) == 3
    assert len(result.agents_md) == 3
    assert len(result.mcp) == 1
    assert result.codex and result.gemini
