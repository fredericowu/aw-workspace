"""doctor's ``stale_upstreams`` — the gateway is still dialled at an app
version that is no longer installed.

``aw-mcp-gateway`` decides which upstreams to re-dial on ``/reload`` by
comparing specs. An app update that changes an upstream's TOOL LIST without
changing its ``mcp.json`` spec used to be bucketed ``unchanged`` and never
re-dialled, so the gateway kept serving the tool list it cached when it first
dialled. A tool could ship, deploy green, pass CI, and be invisible to every
live session at once — 2026-08-30 (agents-platform-runners 0.96.0 -> 0.99.0,
``list_warm_containers``) and again 2026-09-29 (knowledgeable 0.2.0 -> 0.3.0,
``search_graph``).

The gateway now carries the owning app's version in upstream identity and
re-dials on it, but that self-heal needs an observable or nobody can tell it
is working: from inside a session the symptom is indistinguishable from the
session-cache lesson (``verify-new-gateway-tools-in-same-session``), which is
what sent the 09-29 diagnosis the wrong way. This is that observable — it
compares the version the gateway reports having DIALED each upstream at
against the version in ``apps/<slug>/aw-app.json`` on disk.

Run: aw-workspace-cli test src/tests/unit/apps/test_doctor_stale_upstream_versions.py
"""
from __future__ import annotations

import asyncio

import httpx
import pytest

from src.apps.routes import _mcp_gateway_status


class _Containers:
    def base_url(self, slug):
        return "http://gateway.test:9200"


class _Runtime:
    containers = _Containers()


def _gateway_serving(payload: dict, monkeypatch):
    """Stand in for the gateway's ``GET /healthz``. ``_mcp_gateway_status``
    does ``import httpx`` inside the function, so patching the module
    attribute is what reaches it."""
    class _Resp:
        def raise_for_status(self):
            return None

        def json(self):
            return payload

    class _Client:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, url):
            return _Resp()

    monkeypatch.setattr(httpx, "AsyncClient", _Client)


BASE = {"tools": 42, "local_upstreams": ["svc"], "configs": [],
        "warm_redis": {"ok": True, "source": "env"}}


def test_a_superseded_dialed_version_is_reported_and_degrades(monkeypatch):
    _gateway_serving({**BASE,
                      "upstream_app_versions": {"svc": {"app": "demo", "version": "0.96.0"}}},
                     monkeypatch)

    status = asyncio.run(_mcp_gateway_status(
        _Runtime(), expect_tools=True, expected={"svc": "demo"},
        expected_versions={"svc": "0.99.0"}))

    assert status["stale_upstreams"] == [
        {"server": "svc", "app": "demo", "dialed": "0.96.0", "installed": "0.99.0"}]
    assert status["degraded"] is True
    assert "0.96.0" in status["note"] and "0.99.0" in status["note"]


def test_a_matching_version_is_not_reported(monkeypatch):
    _gateway_serving({**BASE,
                      "upstream_app_versions": {"svc": {"app": "demo", "version": "0.99.0"}}},
                     monkeypatch)

    status = asyncio.run(_mcp_gateway_status(
        _Runtime(), expect_tools=True, expected={"svc": "demo"},
        expected_versions={"svc": "0.99.0"}))

    assert status["stale_upstreams"] == []
    assert status["degraded"] is False


def test_a_gateway_that_predates_the_field_reads_as_unknown_not_stale(monkeypatch):
    """The hosted fleet is not version-locked, so an older gateway is a normal
    case — a MISSING key must never be reported as every upstream being stale.
    Same tolerance ``warm_redis`` already has."""
    _gateway_serving(dict(BASE), monkeypatch)

    status = asyncio.run(_mcp_gateway_status(
        _Runtime(), expect_tools=True, expected={"svc": "demo"},
        expected_versions={"svc": "0.99.0"}))

    assert status["upstream_app_versions"] is None
    assert status["stale_upstreams"] == []
    assert status["degraded"] is False


@pytest.mark.parametrize("dialed,installed", [
    (None, "0.99.0"),   # the gateway could not read the app's manifest
    ("0.96.0", None),   # this side could not read it either
])
def test_an_unknown_version_on_either_side_is_not_a_mismatch(dialed, installed, monkeypatch):
    """Degrade to "unknown", never to a wrong answer: a null version proves
    nothing about whether the gateway is stale, and reporting it as a mismatch
    would make doctor exit non-zero for an unreadable manifest."""
    _gateway_serving({**BASE,
                      "upstream_app_versions": {"svc": {"app": "demo", "version": dialed}}},
                     monkeypatch)

    status = asyncio.run(_mcp_gateway_status(
        _Runtime(), expect_tools=True, expected={"svc": "demo"},
        expected_versions={"svc": installed} if installed else {}))

    assert status["stale_upstreams"] == []
    assert status["degraded"] is False


def test_an_upstream_with_no_owning_app_is_skipped(monkeypatch):
    """A hand-authored (``mcp.custom.json``) upstream has no installed app and
    nothing on disk to compare against — the gateway omits it from
    ``upstream_app_versions`` and this must not invent a finding for it."""
    _gateway_serving({**BASE, "local_upstreams": ["svc", "hand-authored"],
                      "upstream_app_versions": {"svc": {"app": "demo", "version": "0.99.0"}}},
                     monkeypatch)

    status = asyncio.run(_mcp_gateway_status(
        _Runtime(), expect_tools=True, expected={"svc": "demo"},
        expected_versions={"svc": "0.99.0"}))

    assert status["stale_upstreams"] == []
    assert status["degraded"] is False


def test_a_dead_upstream_still_wins_the_summary_note(monkeypatch):
    """Both findings are reported; the note names the more severe one. A
    stale-but-live upstream serves old tools, a dead one serves none."""
    _gateway_serving({**BASE, "local_upstreams": [],
                      "upstream_app_versions": {}},
                     monkeypatch)

    status = asyncio.run(_mcp_gateway_status(
        _Runtime(), expect_tools=True, expected={"svc": "demo"},
        expected_versions={"svc": "0.99.0"}))

    assert status["degraded"] is True
    assert "not live in the gateway" in status["note"]


def test_the_cli_prints_the_mismatch_and_exits_non_zero(capsys):
    """The output line is the deliverable — a finding that only exists in a
    JSON payload nobody reads is the silent-degradation failure again."""
    from src.cli.commands.doctor import _mcp

    rc = _mcp({
        "apps_contributing_tools": ["demo"],
        "reachable": True, "tools": 42, "local_upstreams": ["svc"],
        "dead_profiles": [], "warm_redis": {"ok": True, "source": "env"},
        "upstream_app_versions": {"svc": {"app": "demo", "version": "0.96.0"}},
        "stale_upstreams": [
            {"server": "svc", "app": "demo", "dialed": "0.96.0", "installed": "0.99.0"}],
        "degraded": True,
        "note": "1 upstream(s) still dialled at a superseded app version",
    })

    out = capsys.readouterr().out
    assert "gateway dialed svc at 0.96.0 but 0.99.0 is installed" in out
    assert "tools invisible until re-dial" in out
    assert rc == 1


def test_the_cli_says_unknown_for_a_gateway_without_the_field(capsys):
    from src.cli.commands.doctor import _mcp

    rc = _mcp({
        "apps_contributing_tools": ["demo"],
        "reachable": True, "tools": 42, "local_upstreams": ["svc"],
        "dead_profiles": [], "warm_redis": {"ok": True, "source": "env"},
        "degraded": False, "note": "",
    })

    out = capsys.readouterr().out
    assert "upstream app versions: unknown" in out
    assert rc == 0
