"""Doctor check for ``~/.claude.json`` (card 41d5bf3b, design
claude-json-private-seed-v1).

Concurrent Claude CLIs rewrite that file in place; an unparseable one kills
every new run with zero tokens. The check is read-only (never restores),
red only when 2 reads ~200ms apart both fail, and a missing file is not red.
All tests use a tmp HOME - never the live file.
"""
import json
import os

import pytest

from src.apps import routes
from src.cli.commands import doctor


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".claude" / "backups").mkdir(parents=True)
    return tmp_path


def _backup(home, name, text, mtime):
    p = home / ".claude" / "backups" / f".claude.json.backup.{name}"
    p.write_text(text)
    os.utime(p, (mtime, mtime))
    return p


def test_valid_file_is_green(home):
    (home / ".claude.json").write_text('{"userID": "u"}')
    out = routes._claude_config_status(retry_delay_s=0)
    assert out["present"] is True and out["corrupt"] is False
    assert doctor._claude_config(out) == 0


def test_missing_file_is_not_red(home):
    out = routes._claude_config_status(retry_delay_s=0)
    assert out["present"] is False and out["corrupt"] is False
    assert doctor._claude_config(out) == 0


def test_unparseable_is_red_and_names_newest_valid_backup(home, capsys):
    (home / ".claude.json").write_text('{"a": 1}trailing garbage')
    _backup(home, "1", '{"v": 1}', 1000)
    new = _backup(home, "2", '{"v": 2}', 2000)
    _backup(home, "3", '{"v":', 3000)  # newest but corrupt: skipped
    out = routes._claude_config_status(retry_delay_s=0)
    assert out["corrupt"] is True
    assert out["newest_valid_backup"] == str(new)
    assert doctor._claude_config(out) == 1
    assert str(new) in capsys.readouterr().out


def test_red_with_no_valid_backup_says_none(home, capsys):
    (home / ".claude.json").write_text("")
    out = routes._claude_config_status(retry_delay_s=0)
    assert out["corrupt"] is True and out["newest_valid_backup"] is None
    assert doctor._claude_config(out) == 1
    assert "no valid backup" in capsys.readouterr().out


def test_torn_read_that_heals_on_second_read_is_green(home, monkeypatch):
    f = home / ".claude.json"
    f.write_text('{"a":')
    calls = {"n": 0}
    real = routes._claude_json_parses

    def flaky(path):
        calls["n"] += 1
        if calls["n"] == 1:
            return False
        return real(path)

    f.write_text('{"a": 1}')
    monkeypatch.setattr(routes, "_claude_json_parses", flaky)
    out = routes._claude_config_status(retry_delay_s=0)
    assert out["corrupt"] is False and calls["n"] == 2


def test_clears_when_file_is_fixed(home):
    f = home / ".claude.json"
    f.write_text("{")
    assert routes._claude_config_status(retry_delay_s=0)["corrupt"] is True
    f.write_text("{}")
    assert routes._claude_config_status(retry_delay_s=0)["corrupt"] is False


def test_status_does_not_modify_files(home):
    f = home / ".claude.json"
    f.write_text("{")
    _backup(home, "1", "{}", 1000)
    before = sorted((p.name, p.read_bytes()) for p in home.rglob("*") if p.is_file())
    routes._claude_config_status(retry_delay_s=0)
    after = sorted((p.name, p.read_bytes()) for p in home.rglob("*") if p.is_file())
    assert before == after


def test_cli_run_exits_nonzero_on_corrupt_and_zero_on_green(monkeypatch, capsys):
    """End to end through ``doctor.run``: a report whose only problem is a
    corrupt claude config must exit 1 and print the backup path."""
    def run_with(section):
        monkeypatch.setattr(doctor.local_client, "request",
                            lambda *a, **k: (200, {"ok": not section["corrupt"],
                                                   "claude_config": section}))
        return doctor.run([])

    bad = {"present": True, "corrupt": True, "path": "/h/.claude.json",
           "newest_valid_backup": "/h/.claude/backups/.claude.json.backup.9", "reason": "x"}
    assert run_with(bad) == 1
    assert "/h/.claude/backups/.claude.json.backup.9" in capsys.readouterr().out
    good = dict(bad, corrupt=False, newest_valid_backup=None)
    assert run_with(good) == 0


def test_dangling_backup_does_not_hide_a_valid_one(home):
    (home / ".claude.json").write_text("{")
    (home / ".claude" / "backups" / ".claude.json.backup.1").symlink_to(home / "gone")
    ok = _backup(home, "2", "{}", 1000)
    out = routes._claude_config_status(retry_delay_s=0)
    assert out["newest_valid_backup"] == str(ok)


def test_check_runs_off_the_event_loop():
    import inspect
    src = inspect.getsource(routes)
    assert "await asyncio.to_thread(_claude_config_status)" in src
