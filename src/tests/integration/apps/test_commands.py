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

from fastapi import FastAPI

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

from src.apps.commands import CommandInstaller  # noqa: E402


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
    class _NoGateway:
        def is_loaded(self, slug):
            return False

    status = _async(routes_mod._mcp_gateway_status(_NoGateway(), expect_tools=True))
    assert status["reachable"] is None
    assert status["degraded"] is False


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
