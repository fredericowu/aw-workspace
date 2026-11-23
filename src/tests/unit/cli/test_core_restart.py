"""``aw-workspace-cli restart core`` — the ``.env`` fallback, the three
``--wait`` outcomes, the sentinel making a double dispatch a no-op, and
``core`` never colliding with a real component key.
"""
from __future__ import annotations

import subprocess

import pytest

from src.cli import core_restart
from src.cli.commands import restart as restart_cmd


# ---- .env fallback ---------------------------------------------------

def test_env_resolves_from_os_environ_first(monkeypatch):
    monkeypatch.setenv("AW_BACKEND_URL", "http://from-environ")
    assert core_restart._env("AW_BACKEND_URL") == "http://from-environ"


def test_env_falls_back_to_workspace_home_env_file(tmp_path, monkeypatch):
    monkeypatch.delenv("AW_BACKEND_URL", raising=False)
    monkeypatch.setenv("AW_WORKSPACE_HOME", str(tmp_path))
    (tmp_path / ".env").write_text("AW_BACKEND_URL=http://from-dot-env\n")

    assert core_restart._env("AW_BACKEND_URL") == "http://from-dot-env"


def test_env_returns_default_when_absent_everywhere(tmp_path, monkeypatch):
    monkeypatch.delenv("AW_WORKSPACE", raising=False)
    monkeypatch.setenv("AW_WORKSPACE_HOME", str(tmp_path))

    assert core_restart._env("AW_WORKSPACE", "fallback") == "fallback"


def test_resolve_config_raises_when_workspace_or_token_missing(tmp_path, monkeypatch):
    monkeypatch.delenv("AW_WORKSPACE", raising=False)
    monkeypatch.delenv("AW_WORKSPACE_HOST_TOKEN", raising=False)
    monkeypatch.setenv("AW_WORKSPACE_HOME", str(tmp_path))

    with pytest.raises(core_restart.NotConfigured):
        core_restart._resolve_config()


# ---- --wait outcomes ---------------------------------------------------

@pytest.fixture(autouse=True)
def _fast_polling(monkeypatch):
    monkeypatch.setattr(core_restart, "POLL_INTERVAL_S", 0)


def _health_sequence(monkeypatch, responses):
    calls = iter(responses)

    def fake_poll_health():
        try:
            return next(calls)
        except StopIteration:
            return responses[-1]

    monkeypatch.setattr(core_restart, "_poll_health", fake_poll_health)


def test_wait_succeeds_when_boot_id_changes_and_head_matches(monkeypatch):
    _health_sequence(monkeypatch, [
        {"boot_id": "before", "git_head": "old"},
        {"boot_id": "after", "git_head": "expected-sha"},
    ])

    rc = core_restart._wait_for_restart("expected-sha", "before", deadline_s=5)
    assert rc == 0


def test_wait_reports_wrong_code_when_head_mismatches(monkeypatch):
    _health_sequence(monkeypatch, [
        {"boot_id": "after", "git_head": "some-other-sha"},
    ])

    rc = core_restart._wait_for_restart("expected-sha", "before", deadline_s=5)
    assert rc == 2


def test_wait_tolerates_a_transient_head_mismatch_that_self_corrects(monkeypatch):
    _health_sequence(monkeypatch, [
        {"boot_id": "after", "git_head": "stale-sha"},
        {"boot_id": "after", "git_head": "expected-sha"},
    ])

    rc = core_restart._wait_for_restart("expected-sha", "before", deadline_s=5)
    assert rc == 0


def test_wait_reports_wrong_code_when_head_never_converges_within_grace(monkeypatch):
    _health_sequence(monkeypatch, [
        {"boot_id": "after", "git_head": "some-other-sha"},
    ])  # repeats forever — never self-corrects

    rc = core_restart._wait_for_restart("expected-sha", "before", deadline_s=5)
    assert rc == 2


def test_wait_reports_never_happened_at_deadline(monkeypatch):
    _health_sequence(monkeypatch, [
        {"boot_id": "before", "git_head": "old"},
    ])

    rc = core_restart._wait_for_restart("expected-sha", "before", deadline_s=0.05)
    assert rc == 1


# ---- exec_wait "unknown job_id" registration-race retry -----------------

def test_exec_wait_with_retry_absorbs_a_transient_unknown_job_id(monkeypatch):
    monkeypatch.setattr(core_restart, "_EXEC_WAIT_RETRY_DELAYS_S", (0, 0, 0))
    calls = []

    def fake_exec_wait(backend_url, workspace, token, job_id, timeout_s):
        calls.append(1)
        if len(calls) < 3:
            raise core_restart.RemoteHostError('unknown job_id "abc"')
        return {"stdout": "ok"}

    monkeypatch.setattr(core_restart, "_exec_wait", fake_exec_wait)

    result = core_restart._exec_wait_with_retry("url", "ws", "tok", "abc", timeout_s=5)
    assert result == {"stdout": "ok"}
    assert len(calls) == 3


def test_exec_wait_with_retry_reraises_a_different_error_immediately(monkeypatch):
    calls = []

    def fake_exec_wait(backend_url, workspace, token, job_id, timeout_s):
        calls.append(1)
        raise core_restart.RemoteHostError("some other failure")

    monkeypatch.setattr(core_restart, "_exec_wait", fake_exec_wait)

    with pytest.raises(core_restart.RemoteHostError, match="some other failure"):
        core_restart._exec_wait_with_retry("url", "ws", "tok", "abc", timeout_s=5)
    assert len(calls) == 1


def test_exec_wait_with_retry_gives_up_after_exhausting_retries(monkeypatch):
    monkeypatch.setattr(core_restart, "_EXEC_WAIT_RETRY_DELAYS_S", (0, 0))

    def fake_exec_wait(backend_url, workspace, token, job_id, timeout_s):
        raise core_restart.RemoteHostError('unknown job_id "abc"')

    monkeypatch.setattr(core_restart, "_exec_wait", fake_exec_wait)

    with pytest.raises(core_restart.RemoteHostError, match="unknown job_id"):
        core_restart._exec_wait_with_retry("url", "ws", "tok", "abc", timeout_s=5)


# ---- sentinel idempotency ------------------------------------------------

def test_restart_script_is_a_noop_on_second_dispatch(tmp_path):
    sentinel = str(tmp_path / "sentinel")
    log = str(tmp_path / "log")
    script = core_restart._build_restart_script(sentinel, log, "not-a-real-container")

    subprocess.run(["sh", "-c", script], check=False)
    lines_after_first = (tmp_path / "log").read_text().splitlines()
    assert lines_after_first  # sanity: the first dispatch actually ran something

    subprocess.run(["sh", "-c", script], check=False)
    lines_after_second = (tmp_path / "log").read_text().splitlines()

    assert lines_after_second == lines_after_first


def test_restart_script_touches_sentinel_before_running(tmp_path):
    sentinel = str(tmp_path / "sentinel")
    log = str(tmp_path / "log")
    script = core_restart._build_restart_script(sentinel, log, "not-a-real-container")

    subprocess.run(["sh", "-c", script], check=False)

    assert (tmp_path / "sentinel").exists()
    assert (tmp_path / "log").exists()


# ---- core vs. a real component key ---------------------------------------

def test_restart_core_dispatches_to_core_restart(monkeypatch):
    monkeypatch.setattr(restart_cmd, "components", lambda: [])
    called = {}
    # restart_cmd imports the core_restart module itself (`from src.cli import
    # core_restart`), so patching the attribute here patches the same object
    # restart_cmd.run() calls through.
    def fake_run(args):
        called["args"] = args
        return 0

    monkeypatch.setattr(core_restart, "run", fake_run)

    assert restart_cmd.run(["core", "--wait"]) == 0
    assert called["args"] == ["--wait"]


def test_collision_between_core_target_and_a_real_component_fails_loudly(monkeypatch):
    monkeypatch.setattr(
        restart_cmd, "components",
        lambda: [{"component": "core", "key": "docker:aw-core"}],
    )

    with pytest.raises(AssertionError):
        restart_cmd._assert_no_component_collision()


def test_no_collision_when_no_component_is_named_core(monkeypatch):
    monkeypatch.setattr(
        restart_cmd, "components",
        lambda: [{"component": "kb", "key": "docker:aw-kb"}],
    )

    restart_cmd._assert_no_component_collision()  # must not raise


# ---- --request-id / --json (graceful restart card 4215bf3b, slice S2) -----

import json as _json
import os as _os


@pytest.fixture
def stubbed_run(tmp_path, monkeypatch):
    """Everything network/host-facing stubbed; receipts go to tmp_path."""
    monkeypatch.setattr(core_restart, "workspace_root", lambda: str(tmp_path))
    monkeypatch.setattr(core_restart, "_resolve_config", lambda: ("http://b", "ws", "tok"))
    monkeypatch.setattr(core_restart, "compute_git_head", lambda: "head1")
    monkeypatch.setattr(core_restart, "_poll_health", lambda: {"boot_id": "boot-A"})
    calls = {"preflight": 0, "dispatch": []}

    def preflight(*a, **k):
        calls["preflight"] += 1
        return True, ["aw-remote-host-workspace"], "sha256:img"

    def dispatch(backend_url, workspace, token, request_id, container_name):
        calls["dispatch"].append(request_id)

    monkeypatch.setattr(core_restart, "_preflight", preflight)
    monkeypatch.setattr(core_restart, "_dispatch_restart", dispatch)
    return calls


def test_json_prints_exactly_one_object_and_nothing_else(stubbed_run, capsys):
    code = core_restart.run(["--request-id", "req-1", "--json"])
    out = capsys.readouterr().out
    assert code == 0
    obj = _json.loads(out)  # a single object: any extra line would fail to parse
    assert obj["ok"] is True and obj["status"] == "dispatched"
    assert obj["request_id"] == "req-1"
    assert obj["expected_head"] == "head1" and obj["boot_id_before"] == "boot-A"
    assert stubbed_run["dispatch"] == ["req-1"]


def test_same_request_id_twice_dispatches_once(stubbed_run, capsys):
    core_restart.run(["--request-id", "req-2", "--json"])
    capsys.readouterr()
    code = core_restart.run(["--request-id", "req-2", "--json"])
    obj = _json.loads(capsys.readouterr().out)
    assert code == 0
    assert obj["status"] == "already_dispatched"
    assert obj["boot_id_before"] == "boot-A"
    assert stubbed_run["dispatch"] == ["req-2"]
    assert stubbed_run["preflight"] == 1


def test_failed_dispatch_frees_the_request_id_for_retry(stubbed_run, monkeypatch, capsys, tmp_path):
    def boom(*a, **k):
        raise core_restart.RemoteHostError("bad request", status_code=400)
    monkeypatch.setattr(core_restart, "_dispatch_restart", boom)
    code = core_restart.run(["--request-id", "req-3", "--json"])
    obj = _json.loads(capsys.readouterr().out)
    assert code == 1 and obj["ok"] is False and obj["status"] == "dispatch_failed"
    assert not _os.path.exists(tmp_path / ".tmp" / "core-restart" / "req-3.json")


@pytest.mark.parametrize("bad", ["../x", "a b", "a;rm", "", "x" * 65, "abc\n"])
def test_invalid_request_id_is_refused_before_anything_runs(stubbed_run, capsys, bad):
    code = core_restart.run(["--request-id", bad, "--json"])
    obj = _json.loads(capsys.readouterr().out)
    assert code == 1 and obj["status"] == "invalid_request_id"
    assert stubbed_run["preflight"] == 0 and stubbed_run["dispatch"] == []


def test_json_error_when_not_configured(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(core_restart, "workspace_root", lambda: str(tmp_path))
    def nc():
        raise core_restart.NotConfigured("nope")
    monkeypatch.setattr(core_restart, "_resolve_config", nc)
    code = core_restart.run(["--json"])
    obj = _json.loads(capsys.readouterr().out)
    assert code == 1 and obj["status"] == "not_configured" and obj["error"] == "nope"


def test_json_wait_reports_boot_change(stubbed_run, monkeypatch, capsys):
    seq = iter([{"boot_id": "boot-A"}, {"boot_id": "boot-B", "git_head": "head1"}])
    state = {"n": 0}
    def health():
        state["n"] += 1
        return {"boot_id": "boot-A"} if state["n"] <= 2 else {"boot_id": "boot-B", "git_head": "head1"}
    monkeypatch.setattr(core_restart, "_poll_health", health)
    monkeypatch.setattr(core_restart.time, "sleep", lambda s: None)
    code = core_restart.run(["--request-id", "req-4", "--json", "--wait"])
    obj = _json.loads(capsys.readouterr().out)
    assert code == 0 and obj["status"] == "succeeded"
    assert obj["wait"]["boot_id_after"] == "boot-B"


def test_without_json_output_is_human_text(stubbed_run, capsys):
    core_restart.run([])
    assert "restart core: dispatched" in capsys.readouterr().out


def test_replay_during_inflight_dispatch_is_not_reported_as_dispatched(stubbed_run, monkeypatch, capsys):
    seen = {}
    def inflight(backend_url, workspace, token, request_id, container_name):
        core_restart.run(["--request-id", request_id, "--json"])  # second caller mid-dispatch
        seen["out"] = _json.loads(capsys.readouterr().out)
    monkeypatch.setattr(core_restart, "_dispatch_restart", inflight)
    assert core_restart.run(["--request-id", "req-5", "--json"]) == 0
    assert seen["out"]["status"] == "dispatch_pending" and seen["out"]["ok"] is False
    capsys.readouterr()
    core_restart.run(["--request-id", "req-5", "--json"])
    assert _json.loads(capsys.readouterr().out)["status"] == "already_dispatched"


def test_unacknowledged_dispatch_keeps_receipt_and_baseline(stubbed_run, monkeypatch, capsys, tmp_path):
    def timeout(*a, **k):
        raise core_restart.RemoteHostError("HTTP 502", status_code=502)
    monkeypatch.setattr(core_restart, "_dispatch_restart", timeout)
    code = core_restart.run(["--request-id", "req-6", "--json"])
    obj = _json.loads(capsys.readouterr().out)
    assert code == 1 and obj["status"] == "dispatch_unconfirmed" and obj["boot_id_before"] == "boot-A"
    assert _os.path.exists(tmp_path / ".tmp" / "core-restart" / "req-6.json")
    # replay keeps the ORIGINAL baseline, never re-dispatches, never reports success
    monkeypatch.setattr(core_restart, "_poll_health", lambda: {"boot_id": "boot-B", "git_head": "head1"})
    core_restart.run(["--request-id", "req-6", "--json"])
    obj = _json.loads(capsys.readouterr().out)
    assert obj["status"] == "dispatch_unconfirmed" and obj["ok"] is False
    code = core_restart.run(["--request-id", "req-6", "--json", "--wait"])
    obj = _json.loads(capsys.readouterr().out)
    assert code == 0 and obj["wait"]["boot_id_after"] == "boot-B"
    assert obj["boot_id_before"] == "boot-A"
