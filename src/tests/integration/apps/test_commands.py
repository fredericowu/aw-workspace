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
