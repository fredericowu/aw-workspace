"""Reconciler tests (F3 acceptance core) — desired-vs-actual convergence.

Uses a fresh ``AppRuntime`` on a bare FastAPI host, an in-memory fake cloud
registry + local mirror (so no PG/network), and a fake ``fetch`` (a plain
directory copy) injected into the ``Reconciler`` in place of the real HTTP
tarball fetch (``src/apps/fetch.py``) — the reconciler's own convergence
logic is what's under test here, not the fetch transport (see
``test_fetch.py`` for tarball-download/extraction coverage). Proves:

* install → repo fetched, plugin hot-loaded (route answers), registry rows written;
* workspace recreation (fresh runtime, empty loaded-set, registry populated) →
  the reconciler auto-reinstalls with no manual step;
* uninstall → plugin gone, journal residue-free, repo removed, rows deleted;
* reconcile removes an app that is loaded but no longer desired.
"""
from __future__ import annotations

import asyncio
import shutil
import sys
import textwrap
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from src.apps import fetch as fetch_mod
from src.apps.reconciler import AppSpec, Reconciler
from src.apps.runtime import AppRuntime


def _async(coro):
    return asyncio.run(coro)


def _fake_fetch(repo: str, ref: str = "HEAD", *, slug: str, token=None, dest=None) -> str:
    """Stand-in for the real tarball fetch: ``repo`` is a source dir on disk."""
    dest = dest or fetch_mod.package_dir_for(slug)
    if dest != repo:
        if shutil.os.path.exists(dest):  # noqa: SIM108 - mirrors fetch.py's swap
            shutil.rmtree(dest)
        shutil.copytree(repo, dest)
    return dest


def _make_app_repo(tmp_path, slug="widget", version="1.0.0", dependencies=None,
                    reload_mcp_gateway_on_save=False):
    src = tmp_path / f"src_{slug}_{version}"
    src.mkdir()
    deps_json = ""
    if dependencies:
        import json
        deps_json = ',\n      "dependencies": ' + json.dumps({"apps": dependencies})
    mcp_json = ',\n      "mcp": {"reload_on_save": true}' if reload_mcp_gateway_on_save else ""
    (src / "aw-app.json").write_text(textwrap.dedent(f"""
    {{
      "manifest_version": 1,
      "id": "{slug}",
      "name": "{slug}",
      "version": "{version}",
      "tier": "inprocess",
      "runtime": {{"entrypoint": "plugin:AppPlugin"}},
      "permissions": ["routes:register"],
      "contributes": {{"routes": [{{"prefix": "/api/apps/{slug}"}}]{mcp_json}}}
      {deps_json}
    }}
    """))
    (src / "plugin.py").write_text(textwrap.dedent(f"""
        from fastapi import FastAPI
        class AppPlugin:
            async def activate(self, ctx):
                api = FastAPI()
                @api.get("/")
                async def root():
                    return {{"app": "{slug}", "ok": True}}
                ctx.routes.register(api)
            async def deactivate(self):
                return None
    """))
    return str(src)


def _make_app_repo_with_permissions(tmp_path, slug, version, permissions):
    """Same shape as _make_app_repo, with the manifest's permission list under
    test — the thing an update is supposed to re-read."""
    import json
    src = tmp_path / f"src_{slug}_{version}"
    src.mkdir()
    (src / "aw-app.json").write_text(json.dumps({
        "manifest_version": 1,
        "id": slug,
        "name": slug,
        "version": version,
        "tier": "inprocess",
        "runtime": {"entrypoint": "plugin:AppPlugin"},
        "permissions": permissions,
        "contributes": {"routes": [{"prefix": f"/api/apps/{slug}"}]},
    }))
    (src / "plugin.py").write_text(textwrap.dedent("""
        from fastapi import FastAPI
        class AppPlugin:
            async def activate(self, ctx):
                ctx.routes.register(FastAPI())
            async def deactivate(self):
                return None
    """))
    return str(src)


class FakeCloud:
    """In-memory stand-in for the aw-backend cloud registry."""

    def __init__(self, rows=None):
        self.rows = list(rows or [])
        self.configured = True

    def list_desired(self):
        return [dict(r) for r in self.rows]

    def put_desired(self, app_id, *, version, repo=None, ref="HEAD",
                    granted_permissions=None, config=None, instance_id="", signed=False):
        self.delete_desired(app_id)
        self.rows.append({"app_id": app_id, "version": version, "repo": repo, "ref": ref,
                          "granted_permissions": granted_permissions or [],
                          "config": config or {}, "signed": signed, "state": "installed"})

    def delete_desired(self, app_id, instance_id=""):
        self.rows = [r for r in self.rows if r["app_id"] != app_id]


class FakeMirror:
    def __init__(self):
        self.rows = {}

    def list(self):
        return [dict(r) for r in self.rows.values()]

    def upsert(self, spec, package_dir):
        self.rows[spec.app_id] = {
            "app_id": spec.app_id, "version": spec.version, "package_dir": package_dir,
            "repo": spec.repo, "ref": spec.ref,
            "granted_permissions": spec.granted_permissions, "config": spec.config,
            "signed": spec.signed, "state": "installed"}

    def forget(self, app_id):
        self.rows.pop(app_id, None)


async def _get(host, path):
    transport = httpx.ASGITransport(app=host)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        return await c.get(path)


def _reconciler(tmp_path, monkeypatch, cloud):
    monkeypatch.setenv("AW_APPS_ROOT", str(tmp_path / "apps"))
    host = FastAPI()
    rt = AppRuntime(host, guard_identity=False)
    rc = Reconciler(rt, cloud=cloud, local=FakeMirror(), fetch=_fake_fetch)
    return host, rt, rc


def test_install_fetches_hot_loads_and_writes_registry(tmp_path, monkeypatch):
    repo = _make_app_repo(tmp_path)
    cloud = FakeCloud()
    host, rt, rc = _reconciler(tmp_path, monkeypatch, cloud)

    async def run():
        await rc.install(AppSpec(app_id="widget", repo=repo, ref="main"))
        assert rt.is_loaded("widget")
        r = await _get(host, "/api/apps/widget/")
        assert r.status_code == 200 and r.json() == {"app": "widget", "ok": True}
        # registry + mirror both recorded
        assert [row["app_id"] for row in cloud.list_desired()] == ["widget"]
        assert "widget" in rc.local.rows
        assert (tmp_path / "apps" / "widget" / "aw-app.json").exists()

    _async(run())


def test_install_loads_required_app_dependencies_first(tmp_path, monkeypatch):
    dep_repo = _make_app_repo(tmp_path, "proxy")
    browser_repo = _make_app_repo(
        tmp_path,
        "browser",
        dependencies=[{"id": "proxy", "package_dir": dep_repo}],
    )
    cloud = FakeCloud()
    host, rt, rc = _reconciler(tmp_path, monkeypatch, cloud)

    async def run():
        summary = await rc.install(AppSpec(app_id="browser", repo=browser_repo, ref="main"))
        assert summary["dependencies_installed"] == ["proxy"]
        assert rt.loaded_slugs() == ["proxy", "browser"]

        desired_browser_only = [{
            "app_id": "browser",
            "version": "1.0.0",
            "repo": browser_repo,
            "ref": "main",
            "granted_permissions": ["routes:register"],
            "config": {},
            "state": "installed",
        }]
        result = await rc.reconcile(desired_browser_only)
        assert result["removed"] == []
        assert rt.is_loaded("proxy")
        assert rt.is_loaded("browser")

    _async(run())


def test_recreation_auto_reinstalls_from_registry(tmp_path, monkeypatch):
    """Acceptance (b): a fresh runtime with an EMPTY loaded-set but a populated
    cloud registry reinstalls the app with no manual step."""
    repo = _make_app_repo(tmp_path)
    # registry already has the app (as if a prior install / another worker wrote it)
    cloud = FakeCloud([{"app_id": "widget", "version": "1.0.0", "repo": repo,
                        "ref": "main", "granted_permissions": ["routes:register"],
                        "config": {}, "state": "installed"}])
    host, rt, rc = _reconciler(tmp_path, monkeypatch, cloud)

    async def run():
        assert rt.loaded_slugs() == []          # fresh runtime — nothing loaded
        result = await rc.reconcile()           # boot-equivalent converge
        assert result["source"] == "cloud"
        assert result["installed"] == ["widget"]
        assert rt.is_loaded("widget")
        r = await _get(host, "/api/apps/widget/")
        assert r.status_code == 200

    _async(run())


def test_local_fallback_preserves_signed_component_grants(tmp_path, monkeypatch):
    """A signed component app must keep ``ui:code`` when boot reconcile falls
    back to the local mirror; otherwise the SPA downgrades it and never imports
    the bundle.

    The fixture declares ``ui:code`` in its manifest, as the real devctl does:
    the manifest is what an app REQUESTS, and a grant can only narrow it. It
    used to declare only ``routes:register`` while the spec asked for both,
    which no real flow produces — the SPA and CLI send no grant at all, and the
    cloud row mirrors back what the workspace derived from the manifest.
    """
    repo = _make_app_repo_with_permissions(
        tmp_path, "devctl", "1.0.0", ["routes:register", "ui:code"])
    cloud = FakeCloud()
    host, rt, rc = _reconciler(tmp_path, monkeypatch, cloud)

    async def run():
        await rc.install(AppSpec(
            app_id="devctl",
            repo=repo,
            ref="main",
            granted_permissions=["routes:register", "ui:code"],
            signed=True,
        ))

        fallback_rows = rc.local.list()
        assert fallback_rows[0]["signed"] is True
        assert "ui:code" in fallback_rows[0]["granted_permissions"]

        fresh_host = FastAPI()
        fresh_rt = AppRuntime(fresh_host, guard_identity=False)
        fallback = Reconciler(
            fresh_rt,
            cloud=FakeCloud(),
            local=rc.local,
            fetch=_fake_fetch,
        )
        fallback.cloud.configured = False

        result = await fallback.reconcile()
        assert result["source"] == "local"
        loaded = fresh_rt.get("devctl")
        assert loaded is not None
        assert loaded.signed is True
        assert "ui:code" in loaded.granted_permissions

    _async(run())


def test_uninstall_is_residue_free(tmp_path, monkeypatch):
    repo = _make_app_repo(tmp_path)
    cloud = FakeCloud()
    host, rt, rc = _reconciler(tmp_path, monkeypatch, cloud)

    async def run():
        await rc.install(AppSpec(app_id="widget", repo=repo, ref="main"))
        assert (tmp_path / "apps" / "widget").exists()

        res = await rc.uninstall("widget")
        assert res["uninstalled"] is True and res["repo_removed"] is True
        assert not rt.is_loaded("widget")
        assert rt.journal.entries_for("widget") == []          # journal clean
        assert not any(n.startswith("aw_apps.widget") for n in sys.modules)
        assert cloud.list_desired() == []                       # registry row gone
        assert "widget" not in rc.local.rows                    # mirror row gone
        assert not (tmp_path / "apps" / "widget").exists()      # repo removed
        r = await _get(host, "/api/apps/widget/")
        assert r.status_code == 404

    _async(run())


def test_reconcile_removes_undesired_and_installs_missing(tmp_path, monkeypatch):
    repo_a = _make_app_repo(tmp_path, "aaa")
    repo_b = _make_app_repo(tmp_path, "bbb")
    cloud = FakeCloud()
    host, rt, rc = _reconciler(tmp_path, monkeypatch, cloud)

    async def run():
        # start with aaa loaded + in the registry
        await rc.install(AppSpec(app_id="aaa", repo=repo_a, ref="main"))
        # now the desired set is {bbb} only — aaa must be removed, bbb installed
        cloud.rows = [{"app_id": "bbb", "version": "1.0.0", "repo": repo_b, "ref": "main",
                       "granted_permissions": ["routes:register"], "config": {},
                       "state": "installed"}]
        result = await rc.reconcile()
        assert result["installed"] == ["bbb"]
        assert result["removed"] == ["aaa"]
        assert rt.is_loaded("bbb") and not rt.is_loaded("aaa")
        assert not result["errors"]

    _async(run())


def test_reconcile_version_drift_triggers_upgrade_uninstall_then_install(tmp_path, monkeypatch):
    """ADR app-update-mechanism.md, Metade B (1): a desired.version bump for an
    app already loaded is an upgrade — uninstall the old version, then install
    the new one, in that order."""
    repo_v1 = _make_app_repo(tmp_path, "widget", "1.0.0")
    cloud = FakeCloud()
    host, rt, rc = _reconciler(tmp_path, monkeypatch, cloud)

    async def run():
        await rc.install(AppSpec(app_id="widget", repo=repo_v1, ref="main"))
        assert rt.get("widget").manifest.version == "1.0.0"

        repo_v2 = _make_app_repo(tmp_path, "widget", "2.0.0")
        cloud.rows = [{"app_id": "widget", "version": "2.0.0", "repo": repo_v2,
                       "ref": "main", "granted_permissions": ["routes:register"],
                       "config": {}, "state": "installed"}]

        calls: list[tuple[str, str]] = []
        real_uninstall = rc.uninstall
        real_install = rc.install

        async def spy_uninstall(app_id, **kw):
            calls.append(("uninstall", app_id))
            return await real_uninstall(app_id, **kw)

        async def spy_install(spec, **kw):
            calls.append(("install", spec.app_id))
            return await real_install(spec, **kw)

        rc.uninstall = spy_uninstall
        rc.install = spy_install

        result = await rc.reconcile()
        assert result["upgraded"] == ["widget"]
        assert result["installed"] == [] and result["removed"] == []
        assert not result["errors"]
        assert calls == [("uninstall", "widget"), ("install", "widget")]
        assert rt.get("widget").manifest.version == "2.0.0"

    _async(run())


def test_reconcile_no_version_drift_is_a_noop(tmp_path, monkeypatch):
    repo = _make_app_repo(tmp_path, "widget", "1.0.0")
    cloud = FakeCloud()
    host, rt, rc = _reconciler(tmp_path, monkeypatch, cloud)

    async def run():
        await rc.install(AppSpec(app_id="widget", repo=repo, ref="main"))

        result = await rc.reconcile()
        assert result["upgraded"] == []
        assert result["installed"] == [] and result["removed"] == []
        assert rt.get("widget").manifest.version == "1.0.0"

    _async(run())


def test_reconcile_one_bad_app_does_not_block_others(tmp_path, monkeypatch):
    repo_ok = _make_app_repo(tmp_path, "good")
    cloud = FakeCloud([
        {"app_id": "good", "version": "1.0.0", "repo": repo_ok, "ref": "main",
         "granted_permissions": ["routes:register"], "config": {}, "state": "installed"},
        {"app_id": "broken", "version": "1.0.0", "repo": "file:///no/such/repo",
         "ref": "main", "granted_permissions": [], "config": {}, "state": "installed"},
    ])
    host, rt, rc = _reconciler(tmp_path, monkeypatch, cloud)

    async def run():
        result = await rc.reconcile()
        assert "good" in result["installed"]
        assert rt.is_loaded("good")
        assert any(e["app_id"] == "broken" for e in result["errors"])

    _async(run())


def test_install_triggers_mcp_gateway_reload_when_manifest_opts_in(tmp_path, monkeypatch):
    # An app that self-registers its own mcp.json on activate() (e.g.
    # aw-app-whiteboard) only gets picked up by an ALREADY-RUNNING
    # mcp-gateway on its own next reload/restart — install()/update() must
    # trigger that reload itself, reusing the same reload_on_save flag/hook
    # config-save already uses.
    calls = []

    async def fake_reload(runtime):
        calls.append(runtime)

    monkeypatch.setattr("src.apps.routes._reload_mcp_gateway", fake_reload)

    repo = _make_app_repo(tmp_path, "widget-mcp", reload_mcp_gateway_on_save=True)
    cloud = FakeCloud()
    host, rt, rc = _reconciler(tmp_path, monkeypatch, cloud)

    _async(rc.install(AppSpec(app_id="widget-mcp", repo=repo, ref="main")))

    assert len(calls) == 1
    assert calls[0] is rt


def test_install_does_not_reload_gateway_when_manifest_opts_out(tmp_path, monkeypatch):
    calls = []

    async def fake_reload(runtime):
        calls.append(runtime)

    monkeypatch.setattr("src.apps.routes._reload_mcp_gateway", fake_reload)

    repo = _make_app_repo(tmp_path, "widget-plain", reload_mcp_gateway_on_save=False)
    cloud = FakeCloud()
    host, rt, rc = _reconciler(tmp_path, monkeypatch, cloud)

    _async(rc.install(AppSpec(app_id="widget-plain", repo=repo, ref="main")))

    assert calls == []


def test_install_does_not_block_the_event_loop_during_fetch(tmp_path, monkeypatch):
    """Regression (reported live 2026-08-06, Frederico) — measured a real
    ~15s stall of an UNRELATED /api/health call while ``reconcile()`` was
    mid-fetch for an app upgrade. ``fetch_app_repo`` (synchronous
    httpx.stream + tarfile download/extract) was called directly from
    ``Reconciler.install()``'s async body with no ``asyncio.to_thread`` —
    fixed by offloading the fetch (and the cloud-registry writes/removes)
    to a thread, same pattern as the containers.py fix from the day before."""
    import time

    repo = _make_app_repo(tmp_path)
    cloud = FakeCloud()
    PULL_DELAY = 0.3
    TICK_TOTAL = 0.2

    def slow_fetch(*args, **kwargs):
        time.sleep(PULL_DELAY)
        return _fake_fetch(*args, **kwargs)

    monkeypatch.setenv("AW_APPS_ROOT", str(tmp_path / "apps"))
    host = FastAPI()
    rt = AppRuntime(host, guard_identity=False)
    rc = Reconciler(rt, cloud=cloud, local=FakeMirror(), fetch=slow_fetch)

    async def run():
        async def other_work():
            for _ in range(4):
                await asyncio.sleep(0.05)

        start = time.monotonic()
        install_task = asyncio.create_task(
            rc.install(AppSpec(app_id="widget", repo=repo, ref="main")))
        other_task = asyncio.create_task(other_work())
        await asyncio.gather(install_task, other_task)
        elapsed = time.monotonic() - start

        assert rt.is_loaded("widget")
        assert elapsed < 0.4, (
            f"install() took {elapsed:.3f}s alongside other_work — event loop "
            f"was blocked (expected ~{max(PULL_DELAY, TICK_TOTAL)}s if truly "
            f"concurrent, ~{PULL_DELAY + TICK_TOTAL}s if serialized)"
        )

    _async(run())


# ---- MCP gateway reload triggers (install / uninstall / reconcile) --------
#
# The 2026-08-12 bug: the reload was gated on contributes.mcp.reload_on_save,
# an opt-in flag, so codegraphcontext/notion (mcp.json, flag absent) and every
# uninstall were silently skipped. The gate is now "does this app change what
# the gateway's app-scan finds" — see Reconciler._app_touches_mcp.

def _patch_reload(monkeypatch, sink):
    async def fake_reload(runtime, **kwargs):
        sink.append(runtime)
    monkeypatch.setattr("src.apps.routes._reload_mcp_gateway", fake_reload)


def test_install_reloads_gateway_for_mcp_json_on_disk_without_the_optin_flag(
        tmp_path, monkeypatch):
    """aw-app-browser / aw-app-code-server ship an mcp.json with no
    contributes.mcp block at all — the manifest alone under-reports them."""
    calls = []
    _patch_reload(monkeypatch, calls)

    repo = _make_app_repo(tmp_path, "widget-diskmcp", reload_mcp_gateway_on_save=False)
    (Path(repo) / "mcp.json").write_text('{"mcpServers": {"widget": {"type": "http"}}}')
    cloud = FakeCloud()
    host, rt, rc = _reconciler(tmp_path, monkeypatch, cloud)

    _async(rc.install(AppSpec(app_id="widget-diskmcp", repo=repo, ref="main")))

    assert len(calls) == 1


def test_uninstall_reloads_gateway_so_the_dead_upstream_gets_dropped(
        tmp_path, monkeypatch):
    calls = []
    _patch_reload(monkeypatch, calls)

    repo = _make_app_repo(tmp_path, "widget-mcp", reload_mcp_gateway_on_save=True)
    cloud = FakeCloud()
    host, rt, rc = _reconciler(tmp_path, monkeypatch, cloud)
    _async(rc.install(AppSpec(app_id="widget-mcp", repo=repo, ref="main")))
    calls.clear()

    _async(rc.uninstall("widget-mcp"))

    assert len(calls) == 1


def test_uninstall_does_not_reload_gateway_for_a_plain_app(tmp_path, monkeypatch):
    calls = []
    _patch_reload(monkeypatch, calls)

    repo = _make_app_repo(tmp_path, "widget-plain", reload_mcp_gateway_on_save=False)
    cloud = FakeCloud()
    host, rt, rc = _reconciler(tmp_path, monkeypatch, cloud)
    _async(rc.install(AppSpec(app_id="widget-plain", repo=repo, ref="main")))
    calls.clear()

    _async(rc.uninstall("widget-plain"))

    assert calls == []


# ---- Plugin.on_workspace_mcp_changed fan-out ------------------------------
#
# The gateway reload tells the GATEWAY the surface moved. These tests cover the
# other half: telling every loaded PLUGIN, so an app whose own long-lived
# clients were built against the old surface can invalidate them. The fan-out
# lives inside _trigger_gateway_reload precisely so it inherits that method's
# coalescing — a boot reconcile over ~50 apps must notify once, not fifty
# times.


def _make_subscriber_repo(tmp_path, slug, *, body="ctx.mcp_changed.append(1)"):
    """A plain (non-MCP) app that implements the hook. Non-MCP on purpose: it
    must be notified about OTHER apps moving the surface, which is the whole
    point — its own install triggers nothing."""
    src = tmp_path / f"src_{slug}"
    src.mkdir(exist_ok=True)
    (src / "aw-app.json").write_text(textwrap.dedent(f"""
    {{
      "manifest_version": 1,
      "id": "{slug}",
      "name": "{slug}",
      "version": "1.0.0",
      "tier": "inprocess",
      "runtime": {{"entrypoint": "plugin:AppPlugin"}},
      "permissions": [],
      "contributes": {{}}
    }}
    """))
    (src / "plugin.py").write_text(textwrap.dedent(f"""
        class AppPlugin:
            async def activate(self, ctx):
                ctx.mcp_changed = []
            async def deactivate(self):
                return None
            async def on_workspace_mcp_changed(self, ctx):
                {body}
    """))
    return str(src)


def _with_subscriber(tmp_path, monkeypatch, calls, slug="watcher", **kw):
    _patch_reload(monkeypatch, calls)
    cloud = FakeCloud()
    host, rt, rc = _reconciler(tmp_path, monkeypatch, cloud)
    _async(rc.install(AppSpec(
        app_id=slug, repo=_make_subscriber_repo(tmp_path, slug, **kw), ref="main")))
    # The subscriber is plain, so installing IT moved nothing.
    assert calls == []
    assert rt.get(slug).ctx.mcp_changed == []
    return host, rt, rc, cloud


def test_another_apps_install_notifies_every_loaded_plugin(tmp_path, monkeypatch):
    """The card's own title case: app B is installed, and app A — which never
    changed — is the one that has to hear about it."""
    calls = []
    host, rt, rc, cloud = _with_subscriber(tmp_path, monkeypatch, calls)

    repo = _make_app_repo(tmp_path, "widget-mcp", reload_mcp_gateway_on_save=True)
    _async(rc.install(AppSpec(app_id="widget-mcp", repo=repo, ref="main")))

    assert rt.get("watcher").ctx.mcp_changed == [1]


def test_another_apps_uninstall_notifies_every_loaded_plugin(tmp_path, monkeypatch):
    calls = []
    host, rt, rc, cloud = _with_subscriber(tmp_path, monkeypatch, calls)
    repo = _make_app_repo(tmp_path, "widget-mcp", reload_mcp_gateway_on_save=True)
    _async(rc.install(AppSpec(app_id="widget-mcp", repo=repo, ref="main")))
    rt.get("watcher").ctx.mcp_changed.clear()

    _async(rc.uninstall("widget-mcp"))

    assert rt.get("watcher").ctx.mcp_changed == [1]


def test_a_plain_apps_install_notifies_nobody(tmp_path, monkeypatch):
    """Gated on _app_touches_mcp, same as the reload it rides on. Installing a
    pure-UI app must not condemn anything — a subscriber's response is not
    free (for the warm pool it costs a CLI cold start on the next turn)."""
    calls = []
    host, rt, rc, cloud = _with_subscriber(tmp_path, monkeypatch, calls)

    repo = _make_app_repo(tmp_path, "widget-plain", reload_mcp_gateway_on_save=False)
    _async(rc.install(AppSpec(app_id="widget-plain", repo=repo, ref="main")))

    assert calls == []
    assert rt.get("watcher").ctx.mcp_changed == []


def test_a_reconcile_pass_coalesces_the_notification_to_exactly_one(
        tmp_path, monkeypatch):
    """Three MCP apps installed in ONE pass → one reload and ONE notification.
    Without the coalescing (i.e. if the fan-out sat in _trigger_gateway_reload's
    three CALLERS instead of inside it) a boot over ~50 apps would fire ~50
    times, and every warm container would be condemned repeatedly for a single
    logical change."""
    calls = []
    host, rt, rc, cloud = _with_subscriber(tmp_path, monkeypatch, calls)

    cloud.rows = [
        {"app_id": "watcher", "version": "1.0.0",
         "repo": _make_subscriber_repo(tmp_path, "watcher"), "ref": "main",
         "granted_permissions": [], "config": {}, "state": "installed"},
    ] + [
        {"app_id": f"mcp{i}", "version": "1.0.0",
         "repo": _make_app_repo(tmp_path, f"mcp{i}", reload_mcp_gateway_on_save=True),
         "ref": "main", "granted_permissions": ["routes:register"], "config": {},
         "state": "installed"}
        for i in range(3)
    ]
    result = _async(rc.reconcile())

    assert sorted(result["installed"]) == ["mcp0", "mcp1", "mcp2"]
    assert not result["errors"]
    assert len(calls) == 1
    assert rt.get("watcher").ctx.mcp_changed == [1]


def test_a_plugin_that_raises_does_not_break_the_install(tmp_path, monkeypatch):
    """The fan-out is awaited on the install critical path, so one bad
    subscriber must not fail an unrelated app's install — nor stop the walk
    before the subscribers after it."""
    calls = []
    _patch_reload(monkeypatch, calls)
    cloud = FakeCloud()
    host, rt, rc = _reconciler(tmp_path, monkeypatch, cloud)
    # Installed FIRST, so it is walked first: loaded_slugs() is install order.
    _async(rc.install(AppSpec(
        app_id="badsub", ref="main",
        repo=_make_subscriber_repo(tmp_path, "badsub", body='raise RuntimeError("boom")'))))
    _async(rc.install(AppSpec(
        app_id="goodsub", repo=_make_subscriber_repo(tmp_path, "goodsub"), ref="main")))

    repo = _make_app_repo(tmp_path, "widget-mcp", reload_mcp_gateway_on_save=True)
    res = _async(rc.install(AppSpec(app_id="widget-mcp", repo=repo, ref="main")))

    assert res["app_id"] == "widget-mcp"
    assert rt.is_loaded("widget-mcp")
    assert rt.get("goodsub").ctx.mcp_changed == [1]


def test_a_plugin_without_the_hook_is_skipped(tmp_path, monkeypatch):
    """Duck-typed, like on_config_reloaded's call site: every app predating
    this hook has no such method, and a Tier-2 app has no plugin at all."""
    calls = []
    _patch_reload(monkeypatch, calls)
    cloud = FakeCloud()
    host, rt, rc = _reconciler(tmp_path, monkeypatch, cloud)
    _async(rc.install(AppSpec(
        app_id="oldapp", repo=_make_app_repo(tmp_path, "oldapp"), ref="main")))

    repo = _make_app_repo(tmp_path, "widget-mcp", reload_mcp_gateway_on_save=True)
    _async(rc.install(AppSpec(app_id="widget-mcp", repo=repo, ref="main")))

    assert len(calls) == 1  # got as far as the reload, no AttributeError


# ---- sideloaded package_dir outside the gateway's scan root (2026-08-26) --
#
# A sideloaded app's package_dir (POST /api/apps/install {"package_dir": ...},
# a dev checkout anywhere on disk — e.g. under repos/) is never copied into
# AW_APPS_ROOT the way a repo= install is (fetch_app_repo always extracts
# straight into apps_root()/<slug>). The gateway's own scan_app_mcp_servers()
# only ever walks AW_APP_SCAN_ROOTS == apps_root(), so a sideloaded app that
# contributes.mcp was loaded, functional, and answering its own /mcp route —
# yet permanently invisible to the gateway, no matter how many times reload()
# or the periodic rescan watchdog fired. Confirmed live on aw-windows-pilot.
#
# A symlink at apps_root()/<slug> was the first fix attempted and does NOT
# work against the real deployment: mcp-gateway is its own Tier-2 container
# whose manifest bind-mounts ONLY $AW_APPS_ROOT (read-only) — a link pointing
# outside it (e.g. into repos/) is dangling from inside that container, so
# _ensure_mcp_scan_visible copies the mcp.json bytes instead.

def test_install_copies_a_sideloaded_apps_mcp_json_into_the_gateway_scan_root(
        tmp_path, monkeypatch):
    calls = []
    _patch_reload(monkeypatch, calls)

    # Lives OUTSIDE AW_APPS_ROOT on purpose — e.g. repos/aw-app-widget-mcp,
    # not fetched by the reconciler at all.
    package_dir = _make_app_repo(tmp_path, "widget-sideload-mcp",
                                 reload_mcp_gateway_on_save=True)
    mcp_json = '{"mcpServers": {"widget-sideload-mcp": {"type": "http", "url": "http://x"}}}'
    (Path(package_dir) / "mcp.json").write_text(mcp_json)
    cloud = FakeCloud()
    host, rt, rc = _reconciler(tmp_path, monkeypatch, cloud)

    _async(rc.install(AppSpec(app_id="widget-sideload-mcp", package_dir=package_dir)))

    assert len(calls) == 1
    copied = Path(fetch_mod.package_dir_for("widget-sideload-mcp"))
    assert not copied.is_symlink()  # a real file, not a link the gateway container can't see
    assert (copied / "mcp.json").read_text() == mcp_json
    # The real dev checkout is untouched — only its mcp.json got mirrored.
    assert not (copied / "aw-app.json").exists()
    assert (Path(package_dir) / "aw-app.json").is_file()


def test_install_does_not_copy_mcp_json_for_a_normal_marketplace_app(tmp_path, monkeypatch):
    """package_dir already IS apps_root()/<slug> for a repo= install — no
    separate copy to make, _ensure_mcp_scan_visible must no-op."""
    calls = []
    _patch_reload(monkeypatch, calls)

    repo = _make_app_repo(tmp_path, "widget-mcp2", reload_mcp_gateway_on_save=True)
    (Path(repo) / "mcp.json").write_text('{"mcpServers": {"widget-mcp2": {"type": "http"}}}')
    cloud = FakeCloud()
    host, rt, rc = _reconciler(tmp_path, monkeypatch, cloud)

    _async(rc.install(AppSpec(app_id="widget-mcp2", repo=repo, ref="main")))

    installed = Path(fetch_mod.package_dir_for("widget-mcp2"))
    assert (installed / "aw-app.json").is_file()  # the real fetched app, not a bare copy


def test_install_skips_the_copy_when_mcp_json_needs_its_own_package_dir(
        tmp_path, monkeypatch):
    """cwd_app_dir ties a stdio server's cwd (and its relative command/args)
    to the REAL package_dir — a bare mcp.json copy elsewhere would silently
    point it at an empty directory, so this must refuse instead."""
    calls = []
    _patch_reload(monkeypatch, calls)

    package_dir = _make_app_repo(tmp_path, "widget-sideload-stdio",
                                 reload_mcp_gateway_on_save=True)
    (Path(package_dir) / "mcp.json").write_text(
        '{"mcpServers": {"widget-sideload-stdio": '
        '{"type": "stdio", "command": "./run.sh", "cwd_app_dir": true}}}')
    cloud = FakeCloud()
    host, rt, rc = _reconciler(tmp_path, monkeypatch, cloud)

    _async(rc.install(AppSpec(app_id="widget-sideload-stdio", package_dir=package_dir)))

    assert len(calls) == 1  # the reload still fires — this is a visibility gap, not an install failure
    copied = Path(fetch_mod.package_dir_for("widget-sideload-stdio"))
    assert not copied.exists()


def test_uninstall_removes_the_copied_mcp_json_without_touching_the_real_repo(
        tmp_path, monkeypatch):
    calls = []
    _patch_reload(monkeypatch, calls)

    package_dir = _make_app_repo(tmp_path, "widget-sideload-mcp2",
                                 reload_mcp_gateway_on_save=True)
    (Path(package_dir) / "mcp.json").write_text(
        '{"mcpServers": {"widget-sideload-mcp2": {"type": "http", "url": "http://x"}}}')
    cloud = FakeCloud()
    host, rt, rc = _reconciler(tmp_path, monkeypatch, cloud)
    _async(rc.install(AppSpec(app_id="widget-sideload-mcp2", package_dir=package_dir)))
    copied = Path(fetch_mod.package_dir_for("widget-sideload-mcp2"))
    assert copied.is_dir()

    _async(rc.uninstall("widget-sideload-mcp2"))

    assert not copied.exists()
    assert Path(package_dir).is_dir()  # the real dev checkout survives


def test_reconcile_coalesces_many_app_installs_into_one_gateway_reload(
        tmp_path, monkeypatch):
    """Boot reconciles ~20 apps; each /reload re-dials every upstream, so the
    per-app calls must collapse to one at the end of the pass."""
    calls = []
    _patch_reload(monkeypatch, calls)

    repos = [_make_app_repo(tmp_path, f"widget-m{i}", reload_mcp_gateway_on_save=True)
             for i in range(3)]
    cloud = FakeCloud()
    host, rt, rc = _reconciler(tmp_path, monkeypatch, cloud)

    summary = _async(rc.reconcile([
        {"app_id": f"widget-m{i}", "repo": repos[i], "ref": "main"} for i in range(3)
    ]))

    assert sorted(summary["installed"]) == ["widget-m0", "widget-m1", "widget-m2"]
    assert len(calls) == 1
    assert summary["mcp_gateway_reloaded"] is True


def test_reconcile_does_not_reload_gateway_when_nothing_mcp_changed(
        tmp_path, monkeypatch):
    calls = []
    _patch_reload(monkeypatch, calls)

    repo = _make_app_repo(tmp_path, "widget-plain", reload_mcp_gateway_on_save=False)
    cloud = FakeCloud()
    host, rt, rc = _reconciler(tmp_path, monkeypatch, cloud)

    summary = _async(rc.reconcile([{"app_id": "widget-plain", "repo": repo, "ref": "main"}]))

    assert calls == []
    assert summary["mcp_gateway_reloaded"] is False


def test_reconciler_leaves_no_pending_reload_state_after_a_pass(tmp_path, monkeypatch):
    """_pending_gateway_reload must return to None, or every later install()
    would only ever mark a flag nobody flushes."""
    calls = []
    _patch_reload(monkeypatch, calls)

    repo = _make_app_repo(tmp_path, "widget-mcp", reload_mcp_gateway_on_save=True)
    cloud = FakeCloud()
    host, rt, rc = _reconciler(tmp_path, monkeypatch, cloud)

    _async(rc.reconcile([{"app_id": "widget-mcp", "repo": repo, "ref": "main"}]))
    assert rc._pending_gateway_reload is None

    calls.clear()
    repo2 = _make_app_repo(tmp_path, "widget-mcp2", reload_mcp_gateway_on_save=True)
    _async(rc.install(AppSpec(app_id="widget-mcp2", repo=repo2, ref="main")))
    assert len(calls) == 1


def test_update_grants_a_permission_the_new_manifest_added(tmp_path, monkeypatch):
    """An update must re-read the NEW version's manifest for what to request.

    Regression (aw-app-diff-tool, 2026-08-12): the request was taken from the
    spec's carried grant — the *effective* result of the previous install — so
    a permission first declared by the new version was never granted, and the
    app silently degraded wherever it guarded on ctx.has().
    """
    v1 = _make_app_repo_with_permissions(tmp_path, "widget", "1.0.0", ["routes:register"])
    cloud = FakeCloud()
    host, rt, rc = _reconciler(tmp_path, monkeypatch, cloud)

    _async(rc.install(AppSpec(app_id="widget", version="1.0.0", repo=v1, signed=True)))
    assert rt.get("widget").granted_permissions == ["routes:register"]

    v2 = _make_app_repo_with_permissions(
        tmp_path, "widget", "2.0.0", ["routes:register", "fs:workspace-data"])
    # Exactly what POST /api/apps/{slug}/update builds: the previous effective
    # grant carried forward onto the new version's spec.
    _async(rc.uninstall("widget", write_cloud=False))
    _async(rc.install(AppSpec(
        app_id="widget", version="2.0.0", repo=v2,
        granted_permissions=["routes:register"], signed=True)))

    assert "fs:workspace-data" in rt.get("widget").granted_permissions


def test_update_drops_a_permission_the_new_manifest_removed(tmp_path, monkeypatch):
    """The manifest is the whole request, so a withdrawn permission goes away
    instead of being carried forever by the stored grant."""
    v1 = _make_app_repo_with_permissions(
        tmp_path, "widget", "1.0.0", ["routes:register", "fs:workspace-data"])
    cloud = FakeCloud()
    host, rt, rc = _reconciler(tmp_path, monkeypatch, cloud)
    _async(rc.install(AppSpec(app_id="widget", version="1.0.0", repo=v1, signed=True)))

    v2 = _make_app_repo_with_permissions(tmp_path, "widget", "2.0.0", ["routes:register"])
    _async(rc.uninstall("widget", write_cloud=False))
    _async(rc.install(AppSpec(
        app_id="widget", version="2.0.0", repo=v2,
        granted_permissions=["routes:register", "fs:workspace-data"], signed=True)))

    assert rt.get("widget").granted_permissions == ["routes:register"]


def test_trust_filter_still_strips_high_risk_from_an_unsigned_app(tmp_path, monkeypatch):
    """Re-reading the manifest widens what an app may ASK for, never what it
    is granted — an unsigned app still loses ui:code."""
    repo = _make_app_repo_with_permissions(
        tmp_path, "widget", "1.0.0", ["routes:register", "ui:code"])
    cloud = FakeCloud()
    host, rt, rc = _reconciler(tmp_path, monkeypatch, cloud)

    _async(rc.install(AppSpec(app_id="widget", version="1.0.0", repo=repo, signed=False)))

    granted = rt.get("widget").granted_permissions
    assert "routes:register" in granted
    assert "ui:code" not in granted


# ---------------------------------------------------------------------------
# A failed update must not leave the app down — both halves of that, measured
# on aw-app-crispal on 2026-08-16.
# ---------------------------------------------------------------------------

def test_a_failed_load_reverts_its_own_side_effects(tmp_path, monkeypatch):
    """A load that raises part way has usually already journaled something —
    a registered container above all. Left behind, the next attempt fails with
    "container already registered", so the app is not merely down, it is STUCK
    down: install says "already installed", update says "not installed"."""
    repo = _make_app_repo(tmp_path)
    cloud = FakeCloud()
    host, rt, rc = _reconciler(tmp_path, monkeypatch, cloud)

    reverted = []
    real_unload = rt.unload

    async def boom(*a, **kw):
        raise RuntimeError("load blew up half way")

    async def spy_unload(slug):
        reverted.append(slug)
        return await real_unload(slug)

    monkeypatch.setattr(rt, "load", boom)
    monkeypatch.setattr(rt, "unload", spy_unload)

    async def run():
        with pytest.raises(RuntimeError, match="blew up"):
            await rc.install(AppSpec(app_id="widget", repo=repo, ref="main"))
        assert reverted == ["widget"], "the failed load left its side effects behind"

    _async(run())


def test_a_failed_upgrade_rolls_back_to_the_running_version(tmp_path, monkeypatch):
    """An upgrade is uninstall + install, so a failed install leaves NOTHING
    running — not the previous version, which is what "the update failed"
    sounds like it means."""
    repo = _make_app_repo(tmp_path, version="1.0.0")
    cloud = FakeCloud()
    host, rt, rc = _reconciler(tmp_path, monkeypatch, cloud)

    async def run():
        await rc.install(AppSpec(app_id="widget", repo=repo, ref="main"))
        assert rt.is_loaded("widget")

        # the catalog now wants 2.0.0, and installing it fails
        cloud.put_desired("widget", version="2.0.0", repo=repo, ref="main")
        real_install = rc.install
        calls = {"n": 0}

        async def flaky_install(spec, **kw):
            calls["n"] += 1
            if spec.version == "2.0.0":
                raise RuntimeError("the new version does not come up")
            return await real_install(spec, **kw)

        monkeypatch.setattr(rc, "install", flaky_install)
        summary = await rc.reconcile()

        assert rt.is_loaded("widget"), "the app was left down after a failed upgrade"
        err = next(e for e in summary["errors"] if e["app_id"] == "widget")
        assert err["action"] == "upgrade"
        assert err.get("rolled_back_to") == "1.0.0", err

    _async(run())


# ---- boot ordering (BOOT_PRIORITY) ---------------------------------------
#
# The 2026-09-10 incident: a cold recreate left mcp-gateway — the whole
# workspace's MCP surface — unstarted 11 minutes in, and a human had to run
# `aw-workspace-cli start mcp-gateway` by hand. The install loop is serial and
# was unordered, and `LocalMirror.list` has no ORDER BY, so the gateway's
# position in a 450s+ pass was effectively random from one boot to the next.


def _desired_row(slug, repo):
    return {"app_id": slug, "version": "1.0.0", "repo": repo, "ref": "main",
            "granted_permissions": ["routes:register"], "config": {},
            "state": "installed"}


def test_reconcile_installs_the_boot_priority_app_first_then_alphabetically(
        tmp_path, monkeypatch):
    """mcp-gateway goes first; everything else falls in behind it by app_id."""
    _patch_reload(monkeypatch, [])
    slugs = ["zeta", "mcp-gateway", "alpha", "middle"]
    repos = {s: _make_app_repo(tmp_path, s) for s in slugs}
    # Deliberately neither alphabetical nor gateway-first — with no ORDER BY
    # upstream this permutation is exactly as legitimate an input as any other.
    desired = [_desired_row(s, repos[s])
               for s in ["zeta", "middle", "mcp-gateway", "alpha"]]
    host, rt, rc = _reconciler(tmp_path, monkeypatch, FakeCloud())

    async def run():
        result = await rc.reconcile(desired)
        assert result["errors"] == []
        assert result["installed"] == ["mcp-gateway", "alpha", "middle", "zeta"]
        # ...and the runtime really did load them in that order, not just the
        # summary list.
        assert rt.loaded_slugs() == ["mcp-gateway", "alpha", "middle", "zeta"]

    _async(run())


def test_reconcile_install_order_is_identical_across_shuffled_desired_lists(
        tmp_path, monkeypatch):
    """The bug was NONDETERMINISM, not just a missing priority: the same
    workspace with the same apps could put mcp-gateway at position 3 one boot
    and position 40 the next. Two different permutations of one desired set
    must now converge to the same install order."""
    _patch_reload(monkeypatch, [])
    slugs = ["delta", "mcp-gateway", "bravo", "echo", "charlie"]
    repos = {s: _make_app_repo(tmp_path, s) for s in slugs}
    first = ["echo", "mcp-gateway", "bravo", "delta", "charlie"]
    second = ["charlie", "delta", "bravo", "echo", "mcp-gateway"]

    async def install_order(root, order):
        _, rt, rc = _reconciler(root, monkeypatch, FakeCloud())
        result = await rc.reconcile([_desired_row(s, repos[s]) for s in order])
        assert result["errors"] == []
        return result["installed"]

    async def run():
        a = await install_order(tmp_path / "ws_a", first)
        b = await install_order(tmp_path / "ws_b", second)
        assert a == b == ["mcp-gateway", "bravo", "charlie", "delta", "echo"]

    _async(run())


def test_boot_starts_the_gateway_rescan_watchdog_while_the_pass_is_still_running(
        tmp_path, monkeypatch):
    """The half an ordering test cannot prove.

    Sorting the loop only helps if something re-dials the gateway while the
    other 40-odd apps trickle up behind it. The 60s rescan watchdog is that
    something, and it used to be started only AFTER the whole 450s+ pass
    returned — so it sat idle through the entire window it exists for.

    Asserting it is registered once ``reconcile_on_boot`` has returned proves
    nothing (that was already true before the fix). The claim under test is
    that it starts MID-pass, so this holds the reconcile open and asserts the
    registration lands while it is demonstrably still in flight.
    """
    from types import SimpleNamespace

    from src.apps import routes as routes_mod
    from src.apps.runtime import _MCP_RESCAN_TASK_ID, _SYSTEM_APP_ID

    reloads = []
    _patch_reload(monkeypatch, reloads)
    monkeypatch.setattr("src.api.otel.ensure_export_state", lambda _rt: None)

    gw_repo = _make_app_repo(tmp_path, "mcp-gateway")
    host, rt, rc = _reconciler(tmp_path, monkeypatch, FakeCloud())

    async def run():
        gateway_up = asyncio.Event()
        release = asyncio.Event()

        class SlowPass:
            """Stands in for the real boot pass: brings mcp-gateway up first
            (as BOOT_PRIORITY now guarantees), then keeps running for a long
            time — the other 46 apps."""

            async def reconcile(self):
                await rc.install(AppSpec(app_id="mcp-gateway", repo=gw_repo, ref="main"),
                                 write_cloud=False)
                gateway_up.set()
                await release.wait()
                return {"installed": ["mcp-gateway"]}

        app = SimpleNamespace(state=SimpleNamespace(
            app_reconciler=SlowPass(), app_runtime=rt))

        task = asyncio.ensure_future(routes_mod.reconcile_on_boot(app))
        await asyncio.wait_for(gateway_up.wait(), timeout=10)

        registered = False
        for _ in range(1000):  # the waiter polls is_loaded on a 2s cadence
            if _MCP_RESCAN_TASK_ID in rt.watchdog.task_ids_for(_SYSTEM_APP_ID):
                registered = True
                break
            await asyncio.sleep(0.01)

        assert registered, "rescan watchdog never started during the reconcile pass"
        # Without this the test would pass just as happily against the old
        # after-the-pass behaviour.
        assert not task.done(), ("the reconcile pass had already finished — this "
                                 "test proves nothing unless it is still in flight")
        # Gated on a real /reload, not merely on is_loaded: is_loaded means the
        # container was created, and a watchdog whose first ticks fail backs off
        # to ~8 minutes (src/apps/watchdog.py), i.e. worse than doing nothing.
        assert reloads, "the watchdog started without probing that the gateway answers"

        release.set()
        await asyncio.wait_for(task, timeout=10)

    _async(run())
