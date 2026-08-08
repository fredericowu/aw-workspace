"""``aw-workspace-cli folders`` — argument handling and what the user sees.

The HTTP layer is stubbed; these pin the CLI's own contract (exit codes, the
"not visible from here" warning, remap reporting) so a UI/API change can't
quietly turn a failure into a silent success at the terminal.
"""
from __future__ import annotations

import pytest

from src.cli.commands import folders as folders_cmd


@pytest.fixture()
def calls(monkeypatch):
    recorded: list[tuple] = []
    responses: dict[tuple[str, str], tuple[int, object]] = {}

    def fake_request(method, path, json_body=None, timeout=30.0):
        recorded.append((method, path, json_body))
        for (m, p), response in responses.items():
            if m == method and path.startswith(p):
                return response
        return 200, {"folders": []}

    monkeypatch.setattr(folders_cmd.local_client, "request", fake_request)
    return recorded, responses


def test_list_reports_nothing_mapped(calls, capsys):
    assert folders_cmd.run(["list"]) == 0
    assert "No folders mapped." in capsys.readouterr().out


def test_bare_invocation_lists(calls, capsys):
    """`folders` with no subcommand is the same as `folders list` — the thing
    you type when you just want to see what's mapped."""
    assert folders_cmd.run([]) == 0
    assert "No folders mapped." in capsys.readouterr().out


def test_list_renders_each_mapping(calls, capsys):
    _, responses = calls
    responses[("GET", "/api/folders")] = (200, {"folders": [
        {"name": "docs", "path": "/opt/aw-workspace/docs", "mode": "ro", "exists": True},
        {"name": "data", "path": "/srv/data", "mode": "rw", "exists": False},
    ]})

    assert folders_cmd.run(["list"]) == 0

    out = capsys.readouterr().out
    assert "docs  ro    /opt/aw-workspace/docs" in out
    assert "not visible from the workspace process" in out


def test_add_posts_name_and_mode(calls, capsys):
    recorded, responses = calls
    responses[("POST", "/api/folders")] = (200, {
        "folder": {"name": "data", "path": "/srv/data", "mode": "rw", "exists": True},
        "remapped_apps": ["kb"],
    })

    assert folders_cmd.run(["add", "/srv/data", "--name", "data", "--mode", "rw"]) == 0

    assert recorded[0] == ("POST", "/api/folders",
                           {"path": "/srv/data", "mode": "rw", "name": "data"})
    out = capsys.readouterr().out
    assert "Mapped data -> /srv/data (rw)" in out
    assert "Remounted into: kb" in out


def test_add_defaults_to_read_only_and_omits_the_name(calls):
    recorded, responses = calls
    responses[("POST", "/api/folders")] = (200, {"folder": {
        "name": "docs", "path": "/x/docs", "mode": "ro", "exists": True}})

    folders_cmd.run(["add", "/x/docs"])

    assert recorded[0][2] == {"path": "/x/docs", "mode": "ro"}


def test_add_warns_when_the_path_is_not_visible(calls, capsys):
    _, responses = calls
    responses[("POST", "/api/folders")] = (200, {"folder": {
        "name": "ghost", "path": "/nope", "mode": "ro", "exists": False}})

    assert folders_cmd.run(["add", "/nope"]) == 0
    assert "warning:" in capsys.readouterr().out


def test_add_surfaces_the_server_error_and_fails(calls, capsys):
    _, responses = calls
    responses[("POST", "/api/folders")] = (400, {"detail": "path must be absolute"})

    assert folders_cmd.run(["add", "docs"]) == 1
    assert "path must be absolute" in capsys.readouterr().out


def test_rm_reports_an_unknown_name(calls, capsys):
    _, responses = calls
    responses[("DELETE", "/api/folders/")] = (404, {"detail": "nope"})

    assert folders_cmd.run(["rm", "nope"]) == 1
    assert "no mapped folder named 'nope'" in capsys.readouterr().out


def test_browse_prints_child_paths(calls, capsys):
    _, responses = calls
    responses[("GET", "/api/folders/-/browse")] = (200, {
        "path": "/opt/aw-workspace",
        "entries": [{"name": "docs", "path": "/opt/aw-workspace/docs"}],
    })

    assert folders_cmd.run(["browse", "/opt/aw-workspace"]) == 0
    assert "/opt/aw-workspace/docs" in capsys.readouterr().out
