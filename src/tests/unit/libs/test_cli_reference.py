"""``cli_reference`` — the generator for the ``docs/cli/`` /
``knowledge_base/apps/<id>/cli_reference/`` and ``skills/`` knowledge-base
content (card ``quality:procedural-genre-absent-from-both-knowledge-indexes``,
split by owner per ``feature:core-cli-reference-ownership-split``).

What matters here: the command name shows up verbatim in the title/body (the
lexical-match contract the card asks for), subcommands get walked when a
module exposes ``_build_parser``, a failed capture doesn't abort the whole
run, a command's ``__aw_app_id__`` stamp routes its capture to the right
owner's tree, stale captures for a removed command/uninstalled app are
deleted without touching a sibling ``docs/`` mirror, and a skill's owner is
correctly attributed per AGENTS.md's ownership rule (app-contributed vs.
native).
"""
from __future__ import annotations

import argparse
import types

import pytest

from src.libs import cli_reference


@pytest.fixture()
def kb_home(tmp_path, monkeypatch):
    # cli_reference.kb_dir() resolves from AW_WORKSPACE_HOME; skills_sync's
    # source_dir() (used by sync_skills_reference) resolves independently
    # from AW_WORKSPACE_CONTAINER_DIR — both need pointing at tmp_path for
    # the two trees to land as the siblings this test expects.
    monkeypatch.setenv("AW_WORKSPACE_HOME", str(tmp_path))
    monkeypatch.setenv("AW_WORKSPACE_CONTAINER_DIR", str(tmp_path))
    return tmp_path


# --- CLI --help -------------------------------------------------------------


def _stub_proc(stdout: str):
    return types.SimpleNamespace(stdout=stdout, stderr="")


def test_command_name_appears_verbatim_in_title_and_body(kb_home, monkeypatch):
    monkeypatch.setattr(cli_reference, "discover_commands",
                         lambda: {"restart": types.SimpleNamespace()})
    monkeypatch.setattr(cli_reference.subprocess, "run",
                         lambda cmd, **kw: _stub_proc("usage: aw-workspace-cli restart ..."))

    result = cli_reference.sync_cli_reference()

    assert result.written == 2  # root + "restart"
    doc = (kb_home / "docs" / "cli" / "restart.md").read_text()
    assert "# aw-workspace-cli restart --help" in doc  # title, verbatim
    assert "`aw-workspace-cli restart`" in doc  # first line of the body, verbatim
    assert not result.failed


def test_a_mapped_command_gets_its_hand_phrased_bilingual_question(kb_home, monkeypatch):
    """Card quality:procedural-genre-absent-from-both-knowledge-indexes,
    measured follow-up: indexing the raw --help closed the content gap but
    not the retrieval gap — a doc needs a real PT question above the
    --help, not just the English command name, or a conversational
    Portuguese query never finds it."""
    monkeypatch.setattr(cli_reference, "discover_commands",
                         lambda: {"update": types.SimpleNamespace(DESCRIPTION="Update the workspace")})
    monkeypatch.setattr(cli_reference.subprocess, "run",
                         lambda cmd, **kw: _stub_proc("usage: aw-workspace-cli update ..."))

    cli_reference.sync_cli_reference()

    doc = (kb_home / "docs" / "cli" / "update.md").read_text()
    assert "Como eu atualizo a aw-workspace" in doc
    assert "how do i update a running workspace" in doc.lower()
    assert "Update the workspace" in doc  # the one-sentence description


def test_an_unmapped_command_falls_back_to_a_mechanical_bilingual_question(kb_home, monkeypatch):
    monkeypatch.setattr(cli_reference, "discover_commands",
                         lambda: {"sideload-x": types.SimpleNamespace()})
    monkeypatch.setattr(cli_reference.subprocess, "run",
                         lambda cmd, **kw: _stub_proc("usage: aw-workspace-cli sideload-x ..."))

    cli_reference.sync_cli_reference()

    doc = (kb_home / "docs" / "cli" / "sideload-x.md").read_text()
    assert "Como eu uso o comando `aw-workspace-cli sideload-x`?" in doc
    assert "How do I use `aw-workspace-cli sideload-x`?" in doc


def test_subcommands_are_discovered_via_build_parser(kb_home, monkeypatch):
    def _build_parser():
        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers(dest="cmd")
        sub.add_parser("sync", help="Run the unified sync")
        sub.add_parser("status")  # no help= — must not crash the walk
        return parser

    module = types.SimpleNamespace(_build_parser=_build_parser)
    monkeypatch.setattr(cli_reference, "discover_commands", lambda: {"agent": module})
    monkeypatch.setattr(cli_reference.subprocess, "run",
                         lambda cmd, **kw: _stub_proc(" ".join(cmd)))

    result = cli_reference.sync_cli_reference()

    names = {p.stem for p in (kb_home / "docs" / "cli").iterdir()}
    assert names == {"root", "agent", "agent-sync", "agent-status"}
    assert result.written == 4

    sync_doc = (kb_home / "docs" / "cli" / "agent-sync.md").read_text()
    assert "Run the unified sync" in sync_doc  # from add_parser's own help= text
    status_doc = (kb_home / "docs" / "cli" / "agent-status.md").read_text()
    assert "How do I use `aw-workspace-cli agent status`?" in status_doc  # mechanical fallback


def test_a_failed_capture_is_recorded_not_raised(kb_home, monkeypatch):
    monkeypatch.setattr(cli_reference, "discover_commands",
                         lambda: {"broken": types.SimpleNamespace()})

    def _raise(cmd, **kw):
        raise OSError("no such binary")

    monkeypatch.setattr(cli_reference.subprocess, "run", _raise)

    result = cli_reference.sync_cli_reference()

    assert "aw-workspace-cli broken" in result.failed
    assert "aw-workspace-cli" in result.failed  # the root capture failed too


# --- ownership split ---------------------------------------------------------


def _app_owned_module(app_id: str, **kwargs):
    """A command module stamped the way ``discovery._load_app_command``
    stamps a real app-contributed one."""
    module = types.SimpleNamespace(**kwargs)
    module.__aw_app_id__ = app_id
    return module


def test_built_in_command_lands_under_committed_docs_cli(kb_home, monkeypatch):
    monkeypatch.setattr(cli_reference, "discover_commands",
                         lambda: {"restart": types.SimpleNamespace()})
    monkeypatch.setattr(cli_reference.subprocess, "run",
                         lambda cmd, **kw: _stub_proc("usage: ..."))

    cli_reference.sync_cli_reference()

    assert (kb_home / "docs" / "cli" / "restart.md").is_file()
    assert not (kb_home / "knowledge_base" / "apps").exists()


def test_app_owned_command_routes_to_its_own_apps_cli_reference_dir(kb_home, monkeypatch):
    module = _app_owned_module("demo-app", DESCRIPTION="Do the thing")
    monkeypatch.setattr(cli_reference, "discover_commands", lambda: {"demo": module})
    monkeypatch.setattr(cli_reference.subprocess, "run",
                         lambda cmd, **kw: _stub_proc("usage: aw-workspace-cli demo ..."))

    result = cli_reference.sync_cli_reference()

    app_doc = kb_home / "knowledge_base" / "apps" / "demo-app" / "cli_reference" / "demo.md"
    assert app_doc.is_file()
    assert "# aw-workspace-cli demo --help" in app_doc.read_text()
    assert not (kb_home / "docs" / "cli" / "demo.md").exists()  # never doubles into core's tree
    assert result.written == 2  # root (core) + demo (app)


def test_core_capture_is_checksum_gated_against_spurious_rewrites(kb_home, monkeypatch):
    monkeypatch.setattr(cli_reference, "discover_commands",
                         lambda: {"restart": types.SimpleNamespace()})
    monkeypatch.setattr(cli_reference.subprocess, "run",
                         lambda cmd, **kw: _stub_proc("usage: aw-workspace-cli restart ..."))
    cli_reference.sync_cli_reference()
    doc_path = kb_home / "docs" / "cli" / "restart.md"
    first_mtime = doc_path.stat().st_mtime_ns

    cli_reference.sync_cli_reference()  # identical --help text again

    assert doc_path.stat().st_mtime_ns == first_mtime  # untouched: no spurious write

    monkeypatch.setattr(cli_reference.subprocess, "run",
                         lambda cmd, **kw: _stub_proc("usage: aw-workspace-cli restart --changed"))

    cli_reference.sync_cli_reference()

    assert doc_path.stat().st_mtime_ns != first_mtime  # real change does land
    assert "--changed" in doc_path.read_text()


def test_core_stale_capture_is_deleted_when_command_disappears(kb_home, monkeypatch):
    monkeypatch.setattr(cli_reference, "discover_commands",
                         lambda: {"restart": types.SimpleNamespace()})
    monkeypatch.setattr(cli_reference.subprocess, "run",
                         lambda cmd, **kw: _stub_proc("usage: ..."))
    cli_reference.sync_cli_reference()
    assert (kb_home / "docs" / "cli" / "restart.md").exists()

    monkeypatch.setattr(cli_reference, "discover_commands", lambda: {})  # command removed
    cli_reference.sync_cli_reference()

    assert not (kb_home / "docs" / "cli" / "restart.md").exists()
    assert (kb_home / "docs" / "cli" / "root.md").exists()  # root is always regenerated


def test_uninstalled_apps_cli_reference_dir_is_removed_but_its_docs_mirror_is_not(kb_home, monkeypatch):
    module = _app_owned_module("demo-app")
    monkeypatch.setattr(cli_reference, "discover_commands", lambda: {"demo": module})
    monkeypatch.setattr(cli_reference.subprocess, "run",
                         lambda cmd, **kw: _stub_proc("usage: ..."))
    cli_reference.sync_cli_reference()
    app_cli_dir = kb_home / "knowledge_base" / "apps" / "demo-app" / "cli_reference"
    assert app_cli_dir.is_dir()

    # contributes.knowledge's own docs mirror, living beside cli_reference/
    # under the same apps/<id>/ namespace — a different registry's tree.
    docs_mirror = kb_home / "knowledge_base" / "apps" / "demo-app" / "docs"
    docs_mirror.mkdir(parents=True)
    (docs_mirror / "readme.md").write_text("hello")

    monkeypatch.setattr(cli_reference, "discover_commands", lambda: {})  # app uninstalled
    cli_reference.sync_cli_reference()

    assert not app_cli_dir.exists()
    assert (docs_mirror / "readme.md").exists()  # not this generator's tree to touch


# --- skills/*/SKILL.md as content -------------------------------------------


def test_app_owned_skill_is_attributed_to_its_app(kb_home):
    skill_dir = kb_home / "skills" / "aw-demo"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("---\nname: aw-demo\n---\n\nbody\n")
    (skill_dir / ".aw-app-id").write_text("demo-app\n")

    result = cli_reference.sync_skills_reference()

    doc = (kb_home / "knowledge_base" / "skills" / "aw-demo.md").read_text()
    assert "Owner: app: demo-app" in doc
    assert "body" in doc
    assert result.written == 1


def test_native_skill_is_attributed_to_native_skills(kb_home):
    skill_dir = kb_home / "skills" / "aw-workspace"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("---\nname: aw-workspace\n---\n\nbody\n")

    cli_reference.sync_skills_reference()

    doc = (kb_home / "knowledge_base" / "skills" / "aw-workspace.md").read_text()
    assert "Owner: native-skills/" in doc


def test_no_skills_directory_is_a_noop(kb_home):
    result = cli_reference.sync_skills_reference()

    assert result.written == 0
    assert not result.failed
