"""commands / system_clis contribution point (F4).

Proves an app granted ``commands:install`` installs its declared CLIs THROUGH
the gated ``ctx.commands`` facade (running the app's real installer script),
that the install is idempotent (re-run on every reconcile pass), and that
uninstall reverses it by replaying the journal (running the app's revert script).

A synthetic app stands in for a real one: its "installer" touches a marker file
and its "revert" removes it — proving the mechanism without needing apt/root
(the real apt install of git/gh/essentials is proven on macbook-fred).
"""
from __future__ import annotations

import asyncio
import os
import textwrap
import threading
import time

import pytest
from fastapi import FastAPI

from src.apps.containers import ContainerError
from src.apps.journal import ActionJournal
from src.apps.runtime import AppRuntime


def _async(coro):
    return asyncio.run(coro)


def _write_cli_app(tmp_path, marker):
    slug = "clitool"
    pkg = tmp_path / slug
    (pkg / "scripts").mkdir(parents=True)
    (pkg / "aw-app.json").write_text(textwrap.dedent(f"""
    {{
      "manifest_version": 1,
      "id": "{slug}",
      "name": "{slug}",
      "version": "1.0.0",
      "tier": "inprocess",
      "runtime": {{"entrypoint": "plugin:AppPlugin"}},
      "permissions": ["commands:install"],
      "contributes": {{"system_clis": [{{"name": "toolx", "installer": "scripts/install.sh"}}]}}
    }}
    """))
    # idempotent installer: only appends a line if the marker is absent
    (pkg / "scripts" / "install.sh").write_text(
        f'#!/usr/bin/env bash\nset -e\ngrep -q done "{marker}" 2>/dev/null || echo done >> "{marker}"\n')
    (pkg / "scripts" / "uninstall.sh").write_text(
        f'#!/usr/bin/env bash\nrm -f "{marker}"\n')
    (pkg / "plugin.py").write_text(textwrap.dedent("""
        import json, os
        class AppPlugin:
            async def activate(self, ctx):
                m = json.load(open(os.path.join(ctx.package_dir, "aw-app.json")))
                for cli in m["contributes"]["system_clis"]:
                    ctx.commands.install_system_cli(
                        cli["name"], cli["installer"], uninstall="scripts/uninstall.sh")
            async def deactivate(self):
                return None
    """))
    return str(pkg)


def test_system_cli_install_runs_script_journals_and_uninstall_reverts(tmp_path, monkeypatch):
    monkeypatch.setenv("AW_WORKSPACE_HOME", str(tmp_path / "home"))
    marker = tmp_path / "marker.txt"
    pkg = _write_cli_app(tmp_path, str(marker))

    async def run():
        rt = AppRuntime(FastAPI(), journal=ActionJournal())
        await rt.load(pkg, granted_permissions=["commands:install"])

        # the installer script actually ran
        assert marker.is_file()
        assert marker.read_text().count("done") == 1

        # journaled: one install entry + a single app-level revert hook
        kinds = [(e.kind, e.target) for e in rt.journal.entries_for("clitool")]
        assert ("system_cli:install", "toolx") in kinds
        assert ("system_cli:revert-hook", "scripts/uninstall.sh") in kinds

        # uninstall reverts (runs the revert script) and leaves no journal residue
        await rt.unload("clitool")
        assert not marker.exists()
        assert rt.journal.entries_for("clitool") == []

    _async(run())


def test_system_cli_install_is_idempotent_across_reloads(tmp_path, monkeypatch):
    monkeypatch.setenv("AW_WORKSPACE_HOME", str(tmp_path / "home"))
    marker = tmp_path / "marker.txt"
    pkg = _write_cli_app(tmp_path, str(marker))

    async def run():
        rt = AppRuntime(FastAPI(), journal=ActionJournal())
        # simulate two reconcile passes (e.g. boot after workspace recreation):
        # load, unload without reverting the marker, reload → installer re-runs
        # but short-circuits, so the marker never doubles.
        await rt.load(pkg, granted_permissions=["commands:install"])
        assert marker.read_text().count("done") == 1
        # drop the app WITHOUT running revert (mimic a fresh process boot where
        # the marker persisted on the host mount) by clearing the journal first
        rt.journal.clear_app("clitool")
        await rt.unload("clitool")
        assert marker.is_file()  # not reverted
        await rt.load(pkg, granted_permissions=["commands:install"])
        assert marker.read_text().count("done") == 1  # still idempotent
        await rt.unload("clitool")

    _async(run())


# ---- health: "present" is not "works" ---------------------------------------
#
# The bug this exists for: a /usr/bin/git with an empty /usr/lib/git-core is on
# PATH and prints a version, while every https:// operation fails. The healer
# judged health with shutil.which, saw "present", and never healed — and the
# app's own installer guard made the same assumption, so neither layer could
# catch the other (2026-08-12).

from src.apps import commands as commands_mod  # noqa: E402
from src.apps.commands import CommandError, CommandInstaller, HealInFlightError  # noqa: E402


def _installer(tmp_path):
    inst = CommandInstaller()
    inst.record_system_cli("demo", "bash", str(tmp_path), "scripts/install.sh")
    return inst


def test_a_cli_that_runs_is_healthy(tmp_path):
    inst = _installer(tmp_path)
    healthy, reason = inst.check_system_cli("demo", "bash")
    assert healthy is True and reason == ""


def test_a_cli_that_is_present_but_broken_is_unhealthy(tmp_path):
    """The regression: on PATH, so the old `which` check called it healthy."""
    inst = CommandInstaller()
    inst.record_system_cli("demo", "bash", str(tmp_path), "scripts/install.sh",
                           verify="bash -c 'exit 3'")

    healthy, reason = inst.check_system_cli("demo", "bash")
    assert healthy is False
    assert "exit 3" in reason
    assert ("demo", "bash") in inst.missing_system_clis()


def test_a_missing_cli_says_so_without_running_anything(tmp_path):
    inst = CommandInstaller()
    inst.record_system_cli("demo", "definitely-not-a-real-binary", str(tmp_path),
                           "scripts/install.sh")
    healthy, reason = inst.check_system_cli("demo", "definitely-not-a-real-binary")
    assert healthy is False and reason == "not on PATH"


def test_verify_false_opts_back_down_to_a_presence_check(tmp_path):
    """For a CLI with no meaningful version flag. Explicit in the app's own
    code rather than the silent default for everything."""
    inst = CommandInstaller()
    inst.record_system_cli("demo", "bash", str(tmp_path), "scripts/install.sh",
                           verify=False)
    assert inst.check_system_cli("demo", "bash")[0] is True


def test_a_hanging_verify_does_not_wedge_the_healer(tmp_path, monkeypatch):
    inst = CommandInstaller()
    monkeypatch.setattr(CommandInstaller, "VERIFY_TIMEOUT", 0.3)
    inst.record_system_cli("demo", "bash", str(tmp_path), "scripts/install.sh",
                           verify="sleep 5")
    healthy, reason = inst.check_system_cli("demo", "bash")
    assert healthy is False and "timed out" in reason


def test_heal_failures_are_state_not_just_a_log_line(tmp_path):
    """A permanently failing heal used to be a log line repeated every pass —
    65 times in one boot — and nothing else."""
    inst = _installer(tmp_path)
    inst.record_heal_result("demo", "bash", "apt lock permission denied")
    inst.record_heal_result("demo", "bash", "apt lock permission denied")

    row = next(r for r in inst.system_cli_report() if r["cli"] == "bash")
    assert row["heal_failures"] == 2
    assert "apt lock" in row["last_heal_error"]

    inst.record_heal_result("demo", "bash", None)
    row = next(r for r in inst.system_cli_report() if r["cli"] == "bash")
    assert row["heal_failures"] == 0 and row["last_heal_error"] is None


# ---- GET /api/apps/-/doctor -------------------------------------------------
#
# Lives here, not in test_f5_endpoints.py, because that module skips unless the
# sibling aw-app-* repos happen to be checked out next to this one — and a test
# guarding a silent-degradation report must not itself be silently skipped.

from fastapi.testclient import TestClient  # noqa: E402

from src.api.identity import require_identity  # noqa: E402
from src.apps.routes import register_apps_routes  # noqa: E402


def _doctor_client():
    app = FastAPI()
    runtime = register_apps_routes(app)
    app.dependency_overrides[require_identity] = lambda: {"sub": "test"}
    return runtime, TestClient(app)


def test_doctor_reports_a_present_but_broken_cli():
    """`status` and `/api/apps` both pass while a CLI is unusable — that gap is
    exactly what this endpoint exists to close."""
    runtime, client = _doctor_client()
    runtime.commands.record_system_cli("demo", "bash", "/tmp", "scripts/i.sh",
                                       verify="bash -c 'exit 4'")

    body = client.get("/api/apps/-/doctor").json()
    assert body["ok"] is False
    unhealthy = body["system_clis"]["unhealthy"]
    assert [u["cli"] for u in unhealthy] == ["bash"]
    assert "exit 4" in unhealthy[0]["reason"]
    # Present-but-broken is the whole point: the path is reported so the
    # difference from "not installed" is obvious.
    assert unhealthy[0]["path"]


def test_doctor_is_ok_when_nothing_is_degraded():
    _runtime, client = _doctor_client()
    assert client.get("/api/apps/-/doctor").json()["ok"] is True


# ---- mcp section: presence-check was never able to fail -------------------
#
# `mcp.apps_contributing_tools` used to be the ENTIRE mcp section: a list of
# apps that ship an mcp.json, with no check that the gateway actually serves
# anything for them. Confirmed live 2026-08-19: two gateway upstreams were
# dead, serving zero tools, and `doctor` exited 1 only because of an
# unrelated architecture self-check — the mcp section itself could not have
# failed no matter how broken the gateway was. `_mcp_gateway_status` below
# is what closes that gap; these tests exercise it directly, the same way
# `test_reload_mcp_gateway_retries_a_just_created_container_not_ready_yet`
# exercises `_reload_mcp_gateway` against a fake runtime.

from src.apps import routes as routes_mod  # noqa: E402


class _FakeMcpRuntime:
    """Minimal stand-in exposing exactly what _mcp_gateway_status touches."""

    def is_loaded(self, slug):
        return slug == "mcp-gateway"

    class containers:
        @staticmethod
        def base_url(slug):
            return "http://fake-gateway:9200"


def _fake_async_client(handler):
    class _FakeResponse:
        def __init__(self, payload):
            self._payload = payload

        def raise_for_status(self):
            pass

        def json(self):
            return self._payload

    class _FakeAsyncClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url):
            return _FakeResponse(handler())

    return _FakeAsyncClient


def test_mcp_gateway_status_not_installed():
    """Absent gateway == no container registered for it.

    Deliberately NOT ``is_loaded``: that only tracks apps with an in-process
    plugin, and mcp-gateway is ``tier: container``, so is_loaded() is False for
    a gateway that is installed and serving. Gating on it made doctor print
    "not installed — nothing to check" and return 0 against a live gateway with
    two dead upstreams (2026-08-19).
    """
    class _NoGateway:
        class containers:
            @staticmethod
            def base_url(app_id):
                raise ContainerError(f"no container registered for {app_id!r}")

    status = _async(routes_mod._mcp_gateway_status(_NoGateway(), expect_tools=True))
    assert status["reachable"] is None
    assert status["degraded"] is False


def test_mcp_gateway_status_checks_a_container_tier_gateway(monkeypatch):
    """The regression guard: a gateway with NO in-process plugin (the real
    shape) must still be checked, not skipped as "not installed"."""
    monkeypatch.setattr("httpx.AsyncClient", _fake_async_client(
        lambda: {"local_upstreams": ["kb"], "tools": 0}))

    class _ContainerOnly:
        """No is_loaded at all — mirrors a pure container-tier app."""
        class containers:
            @staticmethod
            def base_url(app_id):
                return "http://aw-app-mcp-gateway:9200"

    status = _async(routes_mod._mcp_gateway_status(_ContainerOnly(), expect_tools=True))
    assert status["reachable"] is True
    assert status["degraded"] is True, "zero tools must count as a problem"


def test_mcp_gateway_status_unreachable_counts_as_degraded(monkeypatch):
    class _Boom:
        def __init__(self, *a, **k):
            raise ConnectionRefusedError("gateway not listening")

    monkeypatch.setattr("httpx.AsyncClient", _Boom)
    status = _async(routes_mod._mcp_gateway_status(_FakeMcpRuntime(), expect_tools=True))
    assert status["reachable"] is False
    assert status["degraded"] is True


def test_mcp_gateway_status_flags_a_specific_dead_upstream(monkeypatch):
    """The actual 2026-08-19 shape: the gateway is reachable and its aggregate
    tool count is well above zero (everything else works), but one declared
    upstream never registered. A bare ``tools == 0`` check would miss this
    entirely — only naming the missing upstream catches it."""
    monkeypatch.setattr("httpx.AsyncClient", _fake_async_client(
        lambda: {"local_upstreams": ["kb", "notion"], "tools": 120}))

    status = _async(routes_mod._mcp_gateway_status(
        _FakeMcpRuntime(), expect_tools=True,
        expected={"kb": "kb", "notion": "notion", "arvin": "aw-crispal"}))

    assert status["reachable"] is True
    assert status["degraded"] is True
    assert status["dead_upstreams"] == [{"server": "arvin", "app": "aw-crispal"}]


def test_mcp_gateway_status_ok_when_every_declared_upstream_is_live(monkeypatch):
    monkeypatch.setattr("httpx.AsyncClient", _fake_async_client(
        lambda: {"local_upstreams": ["kb"], "tools": 40}))

    status = _async(routes_mod._mcp_gateway_status(
        _FakeMcpRuntime(), expect_tools=True, expected={"kb": "kb"}))

    assert status["degraded"] is False
    assert status["dead_upstreams"] == []


def test_mcp_gateway_status_zero_tools_while_apps_expect_them_is_degraded(monkeypatch):
    monkeypatch.setattr("httpx.AsyncClient",
                         _fake_async_client(lambda: {"tools": 0, "local_upstreams": []}))
    status = _async(routes_mod._mcp_gateway_status(_FakeMcpRuntime(), expect_tools=True))
    assert status["reachable"] is True
    assert status["degraded"] is True


def test_mcp_gateway_status_healthy_is_not_degraded(monkeypatch):
    monkeypatch.setattr("httpx.AsyncClient",
                         _fake_async_client(lambda: {"tools": 209, "local_upstreams": ["crispal", "kanban"]}))
    status = _async(routes_mod._mcp_gateway_status(_FakeMcpRuntime(), expect_tools=True))
    assert status["reachable"] is True
    assert status["degraded"] is False
    assert status["tools"] == 209


# ---- dead_profiles: a referenced /mcp/<name> the gateway does not serve ----
#
# A manifest can point an agent at a scoped profile
# (mcp_servers: [{"name","server","profile"}]) and nothing creates it. The
# gateway answers 404 {"error":"No such config: <name>"} per request and the
# agent starts with ZERO tools, silently — aw-app-marketing's `marketing`
# profile, measured live 2026-09-21 while the gateway served 385 tools and
# every other agent worked. No total-count check can see that; only comparing
# the references against /healthz's `configs` can.

def test_mcp_gateway_status_flags_a_referenced_profile_that_is_not_served(monkeypatch):
    """The live marketing shape: gateway healthy, one profile referenced by an
    installed app and absent from `configs`."""
    monkeypatch.setattr("httpx.AsyncClient", _fake_async_client(
        lambda: {"local_upstreams": ["kb"], "tools": 385,
                 "configs": ["crispal-full"]}))

    status = _async(routes_mod._mcp_gateway_status(
        _FakeMcpRuntime(), expect_tools=True,
        expected_profiles={"crispal-full": "crispal", "marketing": "marketing"}))

    assert status["degraded"] is True
    assert status["dead_profiles"] == [{"profile": "marketing", "app": "marketing"}]
    assert "marketing" in status["note"]


def test_mcp_gateway_status_ok_when_every_referenced_profile_is_served(monkeypatch):
    monkeypatch.setattr("httpx.AsyncClient", _fake_async_client(
        lambda: {"local_upstreams": ["kb"], "tools": 385,
                 "configs": ["crispal-full"]}))

    status = _async(routes_mod._mcp_gateway_status(
        _FakeMcpRuntime(), expect_tools=True,
        expected_profiles={"crispal-full": "crispal"}))

    assert status["dead_profiles"] == []
    assert status["degraded"] is False


def test_mcp_gateway_status_missing_configs_key_is_unknown_not_all_dead(monkeypatch):
    """Same rule as warm_redis: the fleet is not version-locked, so a gateway
    that does not publish `configs` must read as unknown — never as "every
    profile this workspace references is dead"."""
    monkeypatch.setattr("httpx.AsyncClient", _fake_async_client(
        lambda: {"local_upstreams": ["kb"], "tools": 385}))

    status = _async(routes_mod._mcp_gateway_status(
        _FakeMcpRuntime(), expect_tools=True,
        expected_profiles={"marketing": "marketing"}))

    assert status["configs"] is None
    assert status["dead_profiles"] == []
    assert status["degraded"] is False


def test_mcp_gateway_status_an_empty_configs_list_is_a_real_answer(monkeypatch):
    """Distinct from the case above: the gateway answered, and it serves no
    profile at all — so every reference IS dead. This is the state a fresh
    workspace is in before anything creates the profiles."""
    monkeypatch.setattr("httpx.AsyncClient", _fake_async_client(
        lambda: {"local_upstreams": ["kb"], "tools": 385, "configs": []}))

    status = _async(routes_mod._mcp_gateway_status(
        _FakeMcpRuntime(), expect_tools=True,
        expected_profiles={"crispal-full": "crispal"}))

    assert status["degraded"] is True
    assert status["dead_profiles"] == [{"profile": "crispal-full", "app": "crispal"}]


def test_mcp_gateway_status_without_expected_profiles_is_unchanged(monkeypatch):
    """Every existing caller passes nothing — no new finding may appear."""
    monkeypatch.setattr("httpx.AsyncClient", _fake_async_client(
        lambda: {"local_upstreams": ["kb"], "tools": 40, "configs": []}))

    status = _async(routes_mod._mcp_gateway_status(_FakeMcpRuntime(), expect_tools=True))

    assert status["dead_profiles"] == []
    assert status["degraded"] is False


def test_doctor_prints_the_dead_profile_by_name(capsys):
    """The verdict is worthless if the output does not say WHICH profile —
    the whole failure is one agent among many silently losing its tools."""
    from src.cli.commands.doctor import _mcp

    rc = _mcp({
        "apps_contributing_tools": ["crispal"],
        "reachable": True, "tools": 385, "local_upstreams": ["kb"],
        "configs": ["crispal-full"],
        "dead_profiles": [{"profile": "marketing", "app": "marketing"}],
        "warm_redis": {"ok": True, "source": "probed"},
        "degraded": True,
        "note": "1 scoped profile(s) referenced by an installed app but NOT served",
    })

    out = capsys.readouterr().out
    assert rc == 1
    assert "marketing" in out
    assert "/mcp/marketing" in out


def test_doctor_says_nothing_about_profiles_when_they_are_all_live(capsys):
    from src.cli.commands.doctor import _mcp

    rc = _mcp({
        "apps_contributing_tools": ["crispal"],
        "reachable": True, "tools": 385, "local_upstreams": ["kb"],
        "configs": ["crispal-full"], "dead_profiles": [],
        "warm_redis": {"ok": True, "source": "probed"},
        "degraded": False, "note": "gateway reachable",
    })

    assert rc == 0
    assert "profile" not in capsys.readouterr().out


# ---- warm_redis: a missing key is unknown, not degraded --------------------
#
# aw-mcp-gateway v0.27.0 added a `warm_redis` block to /healthz: {ok, url,
# source, reachable, tokens_seen_24h, tokens_unresolved_24h}. An unresolvable
# warm-token Redis silently breaks schedule_wakeup/ask_human/mark_flow_done/
# supervise/callback dispatch for every warm session (confirmed live
# 2026-09-19 against crispal's hosted workspace) with otherwise zero doctor
# signal. But the hosted fleet is not version-locked — plenty of installs
# will lack this key for a while — so a MISSING key must never read as
# degraded, only an explicitly-present `warm_redis.ok == False`.

def test_mcp_gateway_status_missing_warm_redis_key_is_unknown_not_degraded(monkeypatch):
    """A pre-0.27.0 gateway simply doesn't have the key — must not be treated
    as broken just because this workspace now knows to look for it."""
    monkeypatch.setattr("httpx.AsyncClient", _fake_async_client(
        lambda: {"tools": 40, "local_upstreams": ["kb"]}))

    status = _async(routes_mod._mcp_gateway_status(_FakeMcpRuntime(), expect_tools=True))

    assert status["warm_redis"] is None
    assert status["degraded"] is False


def test_mcp_gateway_status_warm_redis_ok_is_not_degraded(monkeypatch):
    monkeypatch.setattr("httpx.AsyncClient", _fake_async_client(
        lambda: {"tools": 40, "local_upstreams": ["kb"],
                 "warm_redis": {"ok": True, "source": "probed", "reachable": True}}))

    status = _async(routes_mod._mcp_gateway_status(_FakeMcpRuntime(), expect_tools=True))

    assert status["warm_redis"] == {"ok": True, "source": "probed", "reachable": True}
    assert status["degraded"] is False


def test_mcp_gateway_status_warm_redis_explicitly_not_ok_is_degraded(monkeypatch):
    """The actual crispal shape: gateway reachable, tools flowing fine, but
    the warm-token Redis it resolved doesn't answer — a total outage for
    every caller-identity-dependent tool that a bare tools>0 check misses."""
    monkeypatch.setattr("httpx.AsyncClient", _fake_async_client(
        lambda: {"tools": 40, "local_upstreams": ["kb"],
                 "warm_redis": {"ok": False, "source": "none", "reachable": False}}))

    status = _async(routes_mod._mcp_gateway_status(_FakeMcpRuntime(), expect_tools=True))

    assert status["degraded"] is True
    assert "warm-token Redis" in status["note"]


def test_an_explicit_verify_is_the_sole_authority(tmp_path):
    """nvm is a shell function sourced from ~/.nvm/nvm.sh — `which` can never
    find it. A PATH precondition would call it broken forever while the healer
    re-ran a perfectly good installer on every pass."""
    inst = CommandInstaller()
    marker = tmp_path / "nvm.sh"
    marker.write_text("# nvm\n")
    inst.record_system_cli("essentials", "nvm", str(tmp_path), "scripts/install_nvm.sh",
                           verify=f'test -s "{marker}"')

    assert inst.check_system_cli("essentials", "nvm") == (True, "")
    assert ("essentials", "nvm") not in inst.missing_system_clis()

    marker.unlink()
    healthy, reason = inst.check_system_cli("essentials", "nvm")
    assert healthy is False and "verify failed" in reason


# ---- system-CLI healer PID leak (crispal RODADA 9) --------------------------
#
# What actually happened: watchdog.pause() (a lease flap under load) cancels
# the asyncio Task awaiting CommandInstaller.heal() via asyncio.to_thread, but
# cancelling that future does NOT stop the worker thread — it keeps running
# the installer subprocess to completion, untracked. resume() then starts a
# new watchdog loop that heals the SAME still-broken CLI again. Repeat that
# enough times under sustained load and you get 7 concurrent
# install_copilot.sh processes on the host, which is what exhausted PIDs on
# the crispal bare-metal box. Per the Architect's risk notes: test the guard
# with threading.Event, not timing, and mutation-test D1/D2/D3 separately.


def test_heal_second_call_for_same_cli_returns_immediately_while_first_is_in_flight(tmp_path):
    """D1 — the actual fix. A threading.Lock held on the worker thread for
    the duration of the installer call, not an asyncio.Lock (which would be
    released the instant the awaiting Task is cancelled while this thread
    keeps running — see heal()'s docstring)."""
    inst = _installer(tmp_path)
    calls = []
    started = threading.Event()
    release = threading.Event()

    def fake_run_installer(package_dir, installer):
        calls.append(1)
        started.set()
        assert release.wait(timeout=5), "test setup: release was never signalled"
        return "ok"

    inst.run_installer = fake_run_installer  # instance-level monkeypatch

    t = threading.Thread(target=inst.heal, args=("demo", "bash"))
    t.start()
    assert started.wait(timeout=5), "first heal() never started"

    # Simulates resume() re-triggering the same CLI before the first
    # (possibly abandoned) heal has finished.
    with pytest.raises(HealInFlightError):
        inst.heal("demo", "bash")
    assert len(calls) == 1, "a second heal() must not start a duplicate installer"

    release.set()
    t.join(timeout=5)
    assert not t.is_alive()
    assert len(calls) == 1


def test_heal_pass_is_skipped_while_a_previous_pass_thread_is_still_in_flight(tmp_path, monkeypatch):
    """D1 — the whole-pass guard in AppRuntime._heal_system_clis. Simulates
    the abandoned-thread state left behind by a cancelled watchdog Task
    directly (via CommandInstaller's own in-flight set), rather than racing
    real threads, since what matters is that the runtime reads that state
    correctly — heal_in_flight() is exercised on its own above."""
    monkeypatch.setenv("AW_WORKSPACE_HOME", str(tmp_path / "home"))
    rt = AppRuntime(FastAPI(), journal=ActionJournal())
    rt.commands.record_system_cli("demo", "bash", str(tmp_path), "scripts/i.sh",
                                   verify="bash -c 'exit 1'")
    rt.commands._healing.add(("demo", "bash"))  # abandoned heal, still "running"

    calls = []
    monkeypatch.setattr(rt.commands, "heal", lambda *a: calls.append(a))

    _async(rt._heal_system_clis())
    assert calls == [], "a new heal pass must not start while a previous thread is still in flight"


def test_heal_in_flight_skip_is_not_recorded_as_a_failure(tmp_path, monkeypatch):
    """HealInFlightError is not a real failure — recording it would let a
    lease flap accelerate the very backoff/circuit-breaker (D2) that exists
    to protect against wasted retries."""
    monkeypatch.setenv("AW_WORKSPACE_HOME", str(tmp_path / "home"))
    rt = AppRuntime(FastAPI(), journal=ActionJournal())
    rt.commands.record_system_cli("demo", "bash", str(tmp_path), "scripts/i.sh",
                                   verify="bash -c 'exit 1'")

    def raise_in_flight(*a):
        raise HealInFlightError("busy")

    monkeypatch.setattr(rt.commands, "heal", raise_in_flight)
    recorded = []
    monkeypatch.setattr(rt.commands, "record_heal_result", lambda *a: recorded.append(a))

    _async(rt._heal_system_clis())
    assert recorded == []


# ---- healer backoff + circuit breaker (D2) -----------------------------------


def _unhealthy_installer(tmp_path):
    inst = CommandInstaller()
    inst.record_system_cli("demo", "bash", str(tmp_path), "scripts/install.sh",
                           verify="bash -c 'exit 1'")
    return inst


def test_missing_system_clis_skips_a_cli_still_within_its_backoff_window(tmp_path, monkeypatch):
    inst = _unhealthy_installer(tmp_path)
    now = [1_000_000.0]
    monkeypatch.setattr(commands_mod.time, "time", lambda: now[0])

    assert ("demo", "bash") in inst.missing_system_clis()
    inst.record_heal_result("demo", "bash", "install failed")
    assert ("demo", "bash") not in inst.missing_system_clis(), \
        "right after a failure the CLI is inside its own backoff window"

    now[0] += 301  # past the first 300s backoff step
    assert ("demo", "bash") in inst.missing_system_clis()


def test_backoff_grows_exponentially_and_caps_at_one_hour(tmp_path, monkeypatch):
    inst = _unhealthy_installer(tmp_path)
    now = [0.0]
    monkeypatch.setattr(commands_mod.time, "time", lambda: now[0])

    delays = []
    for _ in range(6):
        inst.record_heal_result("demo", "bash", "still broken")
        delays.append(inst._heal_state[("demo", "bash")]["next_attempt_at"] - now[0])

    assert delays[:5] == [300, 600, 1200, 2400, 3600]
    assert delays[5] == 3600, "capped at 1h — a full npm install is too expensive to retry faster"


def test_circuit_breaker_opens_after_max_consecutive_failures_and_resets_on_reinstall(tmp_path, monkeypatch):
    inst = _unhealthy_installer(tmp_path)
    now = [0.0]
    monkeypatch.setattr(commands_mod.time, "time", lambda: now[0])

    for _ in range(CommandInstaller.HEAL_MAX_CONSECUTIVE_FAILURES):
        inst.record_heal_result("demo", "bash", "still broken")
        now[0] += 3600  # always past whatever the backoff window is

    row = next(r for r in inst.system_cli_report() if r["cli"] == "bash")
    assert row["heal_gave_up"] is True
    assert ("demo", "bash") not in inst.missing_system_clis(), \
        "a CLI that failed 10 installs in a row is not fixed by the 11th"

    # A reinstall/update/activate (record_system_cli called again) resets
    # the circuit — it does not stay open forever after the app changes.
    inst.record_system_cli("demo", "bash", str(tmp_path), "scripts/install.sh",
                           verify="bash -c 'exit 1'")
    assert ("demo", "bash") in inst.missing_system_clis()
    row = next(r for r in inst.system_cli_report() if r["cli"] == "bash")
    assert row["heal_gave_up"] is False


# ---- kill the process TREE on timeout (D3) -----------------------------------
#
# subprocess.run's own timeout handling kills only the direct child it forked
# (`bash`) — everything bash forked (npm, node, ...) is left running,
# reparented onto pid 1 the instant bash dies. Reuses kill_proc_tree
# (src/api/terminal_manager.py, referenced in runtime.py) rather than a second
# implementation.


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _assert_pid_dies(pid: int, deadline_s: float = 5.0) -> None:
    """Poll to a deadline instead of a fixed sleep — a fixed sleep flakes
    under CPU contention (e.g. other work running in this same shared
    container), which is exactly the kind of load that triggers the lease
    flap this whole fix exists for."""
    deadline = time.monotonic() + deadline_s
    while time.monotonic() < deadline and _pid_alive(pid):
        time.sleep(0.05)
    assert not _pid_alive(pid), f"pid {pid} was still alive after {deadline_s}s"


def test_run_kills_the_whole_process_tree_on_timeout(tmp_path):
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    pidfile = tmp_path / "child.pid"
    script = pkg / "install.sh"
    script.write_text(
        "#!/usr/bin/env bash\n"
        "sleep 30 &\n"
        f'echo $! > "{pidfile}"\n'
        "wait\n"
    )
    script.chmod(0o755)

    inst = CommandInstaller(timeout=1.0)
    with pytest.raises(CommandError, match="timed out"):
        inst.run_installer(str(pkg), "install.sh")

    assert pidfile.exists(), "installer never got far enough to fork its child"
    child_pid = int(pidfile.read_text().strip())
    _assert_pid_dies(child_pid)


def test_check_system_cli_kills_the_process_tree_on_a_hanging_verify(tmp_path, monkeypatch):
    """Same problem, same fix, at the other subprocess.run call site the
    Architect flagged (check_system_cli)."""
    inst = CommandInstaller()
    monkeypatch.setattr(CommandInstaller, "VERIFY_TIMEOUT", 1.0)
    pidfile = tmp_path / "verify_child.pid"
    inst.record_system_cli(
        "demo", "bash", str(tmp_path), "scripts/install.sh",
        verify=f'sleep 30 & echo $! > "{pidfile}"; wait')

    healthy, reason = inst.check_system_cli("demo", "bash")
    assert healthy is False and "timed out" in reason

    assert pidfile.exists()
    child_pid = int(pidfile.read_text().strip())
    _assert_pid_dies(child_pid)
