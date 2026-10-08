"""``cli_reference`` — the generator for the ``cli_reference/`` and
``skills/`` knowledge-base content (card
``quality:procedural-genre-absent-from-both-knowledge-indexes``).

What matters here: the command name shows up verbatim in the title/body (the
lexical-match contract the card asks for), subcommands get walked when a
module exposes ``_build_parser``, a failed capture doesn't abort the whole
run, and a skill's owner is correctly attributed per AGENTS.md's ownership
rule (app-contributed vs. native).
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
    doc = (kb_home / "knowledge_base" / "cli_reference" / "restart.md").read_text()
    assert "# aw-workspace-cli restart --help" in doc  # title, verbatim
    assert "`aw-workspace-cli restart`" in doc  # first line of the body, verbatim
    assert not result.failed


def test_subcommands_are_discovered_via_build_parser(kb_home, monkeypatch):
    def _build_parser():
        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers(dest="cmd")
        sub.add_parser("sync")
        sub.add_parser("status")
        return parser

    module = types.SimpleNamespace(_build_parser=_build_parser)
    monkeypatch.setattr(cli_reference, "discover_commands", lambda: {"agent": module})
    monkeypatch.setattr(cli_reference.subprocess, "run",
                         lambda cmd, **kw: _stub_proc(" ".join(cmd)))

    result = cli_reference.sync_cli_reference()

    names = {p.stem for p in (kb_home / "knowledge_base" / "cli_reference").iterdir()}
    assert names == {"root", "agent", "agent-sync", "agent-status"}
    assert result.written == 4


def test_a_failed_capture_is_recorded_not_raised(kb_home, monkeypatch):
    monkeypatch.setattr(cli_reference, "discover_commands",
                         lambda: {"broken": types.SimpleNamespace()})

    def _raise(cmd, **kw):
        raise OSError("no such binary")

    monkeypatch.setattr(cli_reference.subprocess, "run", _raise)

    result = cli_reference.sync_cli_reference()

    assert "aw-workspace-cli broken" in result.failed
    assert "aw-workspace-cli" in result.failed  # the root capture failed too


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
