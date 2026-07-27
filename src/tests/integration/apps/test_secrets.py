"""secrets:own contribution point (F4) — the F4 acceptance secret round-trip.

An app granted ``secrets:own`` writes and reads its own secret through the
gated ``ctx.secrets`` facade (backed by the encrypted workspace-side store);
the value round-trips (write → read) and is stored encrypted at rest. An app
WITHOUT ``secrets:own`` is DENIED (F2) and the denial is journaled. Uninstall
purges the app's whole secret namespace — no residue.
"""
from __future__ import annotations

import asyncio
import json
import os
import textwrap

from fastapi import FastAPI

from src.apps.journal import ActionJournal
from src.apps.runtime import AppRuntime


def _async(coro):
    return asyncio.run(coro)


def _write_app(tmp_path, slug, plugin_src, permissions):
    pkg = tmp_path / slug
    pkg.mkdir()
    perms = json.dumps(permissions)
    (pkg / "aw-app.json").write_text(textwrap.dedent(f"""
    {{
      "manifest_version": 1,
      "id": "{slug}",
      "name": "{slug}",
      "version": "1.0.0",
      "tier": "inprocess",
      "runtime": {{"entrypoint": "plugin:AppPlugin"}},
      "permissions": {perms}
    }}
    """))
    (pkg / "plugin.py").write_text(textwrap.dedent(plugin_src))
    return str(pkg)


def test_secret_round_trips_and_is_encrypted_at_rest(tmp_path, monkeypatch):
    monkeypatch.setenv("AW_WORKSPACE_HOME", str(tmp_path / "home"))
    plugin = """
        class AppPlugin:
            async def activate(self, ctx):
                ctx.secrets.write("github_token", "ghp_secret_value_123")
                ctx.roundtrip = ctx.secrets.read("github_token")
            async def deactivate(self):
                return None
    """
    pkg = _write_app(tmp_path, "vault", plugin, ["secrets:own"])

    async def run():
        rt = AppRuntime(FastAPI(), journal=ActionJournal())
        await rt.load(pkg, granted_permissions=["secrets:own"])

        # write → read round-trip returned the exact value
        assert rt.get("vault").ctx.roundtrip == "ghp_secret_value_123"

        # ...and it re-reads on a fresh facade call (persisted)
        assert rt.get("vault").ctx.secrets.read("github_token") == "ghp_secret_value_123"

        # journaled
        kinds = [(e.kind, e.target) for e in rt.journal.entries_for("vault")]
        assert ("secret:write", "github_token") in kinds

        # encrypted at rest — the plaintext never appears in the store file
        store_file = os.path.join(str(tmp_path / "home"), "secrets", "vault.json")
        assert os.path.isfile(store_file)
        assert "ghp_secret_value_123" not in open(store_file).read()

        # uninstall purges the namespace — no residue
        await rt.unload("vault")
        assert not os.path.exists(store_file)

    _async(run())


def test_app_without_secrets_own_is_denied_and_journaled(tmp_path, monkeypatch):
    monkeypatch.setenv("AW_WORKSPACE_HOME", str(tmp_path / "home"))
    plugin = """
        class AppPlugin:
            async def activate(self, ctx):
                ctx.denied = None
                try:
                    ctx.secrets.write("k", "v")
                except PermissionError as e:
                    ctx.denied = str(e)
            async def deactivate(self):
                return None
    """
    # granted routes:register only — NOT secrets:own
    pkg = _write_app(tmp_path, "nosec", plugin, ["secrets:own"])  # declared…

    async def run():
        rt = AppRuntime(FastAPI(), journal=ActionJournal())
        # …but only routes:register is actually granted at load time
        await rt.load(pkg, granted_permissions=[])
        ctx = rt.get("nosec").ctx
        assert ctx.denied and "secrets:own" in ctx.denied
        kinds = [(e.kind, e.target) for e in rt.journal.entries_for("nosec")]
        assert ("capability:denied", "secrets:own") in kinds
        # nothing was written
        assert rt.secret_store.keys("nosec") == []
        await rt.unload("nosec")

    _async(run())
