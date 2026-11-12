"""Deploy reconciliation C2 — component self-report (card 4165bf3b-9510-81f4).

Pure-unit coverage of ``src/apps/component_report.py`` against a fake
``AppRuntime``: payload shape, the "unconfigured cloud link = inert no-op"
rule mirrored from ``CloudRegistry``, and that neither half ever raises —
the acceptance test the architect's design names explicitly ("a failed
POST never fails reconcile"). The reconcile()-level half of that same
acceptance test (the hook not blowing up the real pass) lives in
``src/tests/integration/apps/test_component_report_hook.py``.
"""
from __future__ import annotations

import asyncio

from src.apps import component_report


class _LoadedApp:
    def __init__(self, version):
        class _Manifest:
            pass
        self.manifest = _Manifest()
        self.manifest.version = version


class _FakeRuntime:
    def __init__(self, apps=None, runners_loaded=False):
        self._apps = apps or {}
        self._runners_loaded = runners_loaded

    def loaded_slugs(self):
        return list(self._apps)

    def get(self, slug):
        return self._apps.get(slug)

    def is_loaded(self, slug):
        if slug == component_report.RUNNERS_APP_ID:
            return self._runners_loaded
        return slug in self._apps


def _async(coro):
    return asyncio.run(coro)


def test_inventory_includes_core_and_every_loaded_app(monkeypatch):
    monkeypatch.setenv("AW_WORKSPACE_VERSION", "1.2.3")
    rt = _FakeRuntime({"kb": _LoadedApp("0.9.0"), "aw-app-notion": _LoadedApp("2.0.0")})

    inv = component_report._inventory(rt)

    assert {"component": "core", "version": "1.2.3",
            "source": "core-reconciler"} in inv
    assert {"component": "app:kb", "version": "0.9.0",
            "source": "core-reconciler"} in inv
    assert {"component": "app:aw-app-notion", "version": "2.0.0",
            "source": "core-reconciler"} in inv
    assert len(inv) == 3


def test_report_components_noop_when_cloud_link_not_configured(monkeypatch):
    monkeypatch.delenv("AW_BACKEND_URL", raising=False)
    monkeypatch.delenv("AW_WORKSPACE", raising=False)
    monkeypatch.delenv("AW_WORKSPACE_HOST_TOKEN", raising=False)

    called = {"n": 0}

    class _ExplodingClient:
        async def __aenter__(self):
            called["n"] += 1
            raise AssertionError("httpx must not be touched with no cloud link")

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(component_report.httpx, "AsyncClient",
                        lambda *a, **kw: _ExplodingClient())

    _async(component_report.report_components(_FakeRuntime()))
    assert called["n"] == 0


def test_report_components_puts_the_declared_c1_payload_shape(monkeypatch):
    monkeypatch.setenv("AW_BACKEND_URL", "https://backend.example")
    monkeypatch.setenv("AW_WORKSPACE", "acme")
    monkeypatch.setenv("AW_WORKSPACE_HOST_TOKEN", "awlk_test_token")
    monkeypatch.setenv("AW_WORKSPACE_VERSION", "9.9.9")

    captured = {}

    class _Resp:
        def raise_for_status(self):
            return None

    class _FakeClient:
        async def put(self, url, json=None, headers=None):
            captured["url"] = url
            captured["json"] = json
            captured["headers"] = headers
            return _Resp()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(component_report.httpx, "AsyncClient",
                        lambda *a, **kw: _FakeClient())

    rt = _FakeRuntime({"widget": _LoadedApp("1.0.0")})
    _async(component_report.report_components(rt))

    assert captured["url"] == "https://backend.example/api/workspaces/acme/components"
    assert captured["headers"] == {"Authorization": "Bearer awlk_test_token"}
    assert captured["json"] == {"components": [
        {"component": "core", "version": "9.9.9", "source": "core-reconciler"},
        {"component": "app:widget", "version": "1.0.0", "source": "core-reconciler"},
    ]}


def test_report_components_swallows_a_failed_put(monkeypatch):
    """The architect's own acceptance test: a failed POST never fails
    reconcile. At this layer that means ``report_components`` itself must
    never let the exception escape."""
    monkeypatch.setenv("AW_BACKEND_URL", "https://backend.example")
    monkeypatch.setenv("AW_WORKSPACE", "acme")
    monkeypatch.setenv("AW_WORKSPACE_HOST_TOKEN", "awlk_test_token")

    class _FakeClient:
        async def put(self, *a, **kw):
            raise ConnectionError("backend unreachable")

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(component_report.httpx, "AsyncClient",
                        lambda *a, **kw: _FakeClient())

    # Must not raise.
    _async(component_report.report_components(_FakeRuntime()))


def test_trigger_deploy_check_noop_when_runners_app_not_installed(monkeypatch):
    def _must_not_dial_out(*a, **kw):
        raise AssertionError("must not dial out when the runners app isn't loaded")

    monkeypatch.setattr(component_report.httpx, "AsyncClient", _must_not_dial_out)
    # Must not raise — is_loaded()==False short-circuits before AsyncClient
    # is ever constructed.
    _async(component_report.trigger_deploy_check(_FakeRuntime(runners_loaded=False)))


def test_trigger_deploy_check_swallows_a_failed_post(monkeypatch):
    # Patched at the source module, not on component_report — the import
    # inside trigger_deploy_check() re-looks-up the name from there at
    # call time, so this is what actually takes effect.
    monkeypatch.setattr(
        "src.api.workspace_api_key.get_or_create_workspace_api_key",
        lambda: "fake-key")

    class _FakeClient:
        async def post(self, *a, **kw):
            raise ConnectionError("runners app unreachable")

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(component_report.httpx, "AsyncClient",
                        lambda *a, **kw: _FakeClient())

    # Must not raise even though the loopback POST fails.
    _async(component_report.trigger_deploy_check(_FakeRuntime(runners_loaded=True)))
