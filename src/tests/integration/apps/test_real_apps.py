"""The two real apps plug into the framework end-to-end (F4).

Loads the ACTUAL ``repos/aw-app-essentials`` and ``repos/aw-app-git`` packages
through the real runtime — proving the packaged apps (relative imports, package
layout) load and drive the gated ``ctx`` facades. The apt-level CLI install is
proven separately on macbook-fred's aw-remote-host-workspace container (the
standalone scripts already work there); here the installer/revert scripts are
intercepted so the test needs neither apt nor root, while still exercising the
full framework path (manifest → import → facade → journal → reverse-replay).
"""
from __future__ import annotations

import asyncio
import os

import pytest
from fastapi import FastAPI

from src.apps.journal import ActionJournal
from src.apps.runtime import AppRuntime

REPOS = os.path.abspath(os.path.join(
    os.path.dirname(__file__), "..", "..", "..", "..", "..", ))
ESSENTIALS = os.path.join(REPOS, "aw-app-essentials")
GIT = os.path.join(REPOS, "aw-app-git")

pytestmark = pytest.mark.skipif(
    not (os.path.isdir(ESSENTIALS) and os.path.isdir(GIT)),
    reason="aw-app-essentials / aw-app-git repos not checked out alongside",
)


def _async(coro):
    return asyncio.run(coro)


def _patch_installers(rt, calls):
    def run_installer(package_dir, script):
        calls.append(("install", script))
        return f"ran {script}"

    def run_revert(package_dir, script):
        calls.append(("revert", script))
        return f"reverted {script}"

    rt.commands.run_installer = run_installer
    rt.commands.run_revert = run_revert


def test_essentials_installs_seven_clis_and_reverts(tmp_path, monkeypatch):
    monkeypatch.setenv("AW_WORKSPACE_HOME", str(tmp_path / "home"))
    calls: list = []

    async def run():
        rt = AppRuntime(FastAPI(), journal=ActionJournal())
        _patch_installers(rt, calls)
        await rt.load(ESSENTIALS, granted_permissions=["commands:install", "fs:workspace-data"])

        installed = [s for (a, s) in calls if a == "install"]
        assert len(installed) == 7  # telnet ping curl nc perl python vim
        installs = [e.target for e in rt.journal.entries_for("essentials")
                    if e.kind == "system_cli:install"]
        assert set(installs) == {"telnet", "ping", "curl", "nc", "perl", "python", "vim"}
        # single app-level revert hook journaled
        hooks = [e for e in rt.journal.entries_for("essentials")
                 if e.kind == "system_cli:revert-hook"]
        assert len(hooks) == 1 and hooks[0].target == "scripts/uninstall.sh"

        await rt.unload("essentials")
        assert ("revert", "scripts/uninstall.sh") in calls
        assert rt.journal.entries_for("essentials") == []

    _async(run())


def test_git_installs_stores_token_secret_and_routes(tmp_path, monkeypatch):
    monkeypatch.setenv("AW_WORKSPACE_HOME", str(tmp_path / "home"))
    calls: list = []

    async def run():
        import httpx

        host = FastAPI()
        rt = AppRuntime(host, journal=ActionJournal())
        _patch_installers(rt, calls)
        await rt.load(GIT, granted_permissions=[
            "commands:install", "routes:register", "secrets:own",
            "fs:workspace-data", "net:outbound"])

        # git + gh installed via the facade
        assert [s for (a, s) in calls if a == "install"] == \
            ["scripts/install_git.sh", "scripts/install_gh.sh"]

        # the settings route stores the gh token via ctx.secrets (patch the
        # app's own gh_auth so no real `gh` call happens)
        gh_auth = __import__("aw_apps.git.git_app.gh_auth",
                             fromlist=["login_with_token", "status"])
        gh_auth.login_with_token = lambda t: "logged in (stub)"
        gh_auth.status = lambda: "logged in (stub)"

        transport = httpx.ASGITransport(app=host)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            r = await c.post("/api/apps/git/settings/token",
                             json={"github_token": "ghp_roundtrip_1"})
            assert r.status_code == 200 and r.json()["logged_in"] is True
            s = await c.get("/api/apps/git/status")
            assert s.json()["has_token"] is True

        # the token round-trips through the encrypted store
        assert rt.get("git").ctx.secrets.read("github_token") == "ghp_roundtrip_1"

        await rt.unload("git")
        # secret namespace purged on uninstall
        assert rt.secret_store.keys("git") == []

    _async(run())


def test_git_denied_secrets_without_grant(tmp_path, monkeypatch):
    """aw-app-git WITHOUT secrets:own is denied at the facade (F2)."""
    monkeypatch.setenv("AW_WORKSPACE_HOME", str(tmp_path / "home"))
    calls: list = []

    async def run():
        rt = AppRuntime(FastAPI(), journal=ActionJournal())
        _patch_installers(rt, calls)
        # activate() reads ctx.secrets → PermissionError since not granted
        with pytest.raises(PermissionError):
            await rt.load(GIT, granted_permissions=[
                "commands:install", "routes:register"])  # no secrets:own
        # residue-free failed load (F1): nothing left loaded, journal cleared
        assert not rt.is_loaded("git")
        assert rt.journal.entries_for("git") == []

    _async(run())
