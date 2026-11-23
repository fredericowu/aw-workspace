"""Importable gateway readiness helper (graceful restart card 4215bf3b, S2)."""
from __future__ import annotations

import asyncio

import httpx

from src.apps import gateway_readiness as gr
from src.apps.containers import ContainerError


def _serve(payload, monkeypatch):
    class _Resp:
        def raise_for_status(self): pass
        def json(self): return payload

    class _Client:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *e): return False
        async def get(self, url): return _Resp()

    monkeypatch.setattr(httpx, "AsyncClient", _Client)


class _RT:
    class containers:
        @staticmethod
        def base_url(slug): return "http://gw:9200"


def _run(**kw):
    return asyncio.run(gr.mcp_gateway_status(_RT(), **kw))


def test_healthy_gateway_is_ready(monkeypatch):
    _serve({"tools": 5, "local_upstreams": ["kb"]}, monkeypatch)
    st = _run(expect_tools=True, expected={"kb": "kb"})
    assert st["not_ready"] == [] and gr.is_ready(st)
    assert st["reachable"] and st["tools"] == 5 and st["local_upstreams"] == ["kb"]


def test_dead_upstream_is_named_in_not_ready(monkeypatch):
    _serve({"tools": 5, "local_upstreams": ["kb"]}, monkeypatch)
    st = _run(expect_tools=True, expected={"kb": "kb", "x": "app-x"})
    assert [(n["kind"], n["server"], n["app"]) for n in st["not_ready"]] == [
        ("dead_upstream", "x", "app-x")]
    assert not gr.is_ready(st)


def test_stale_upstream_is_not_ready(monkeypatch):
    _serve({"tools": 5, "local_upstreams": ["kb"],
            "upstream_app_versions": {"kb": {"app": "kb", "version": "1"}}}, monkeypatch)
    st = _run(expect_tools=True, expected={"kb": "kb"}, expected_versions={"kb": "2"})
    assert st["not_ready"][0]["kind"] == "stale_upstream"


def test_zero_tools_is_not_ready(monkeypatch):
    _serve({"tools": 0, "local_upstreams": []}, monkeypatch)
    st = _run(expect_tools=True)
    assert st["not_ready"][0]["kind"] == "zero_tools"


def test_federated_upstream_never_gates(monkeypatch):
    # Only upstreams an installed app declares are expected; an extra live
    # (federated) one is reported in local_upstreams but cannot be not_ready.
    _serve({"tools": 5, "local_upstreams": ["kb", "federated-thing"]}, monkeypatch)
    st = _run(expect_tools=True, expected={"kb": "kb"})
    assert gr.is_ready(st)


def test_not_installed_counts_as_ready():
    class _No:
        class containers:
            @staticmethod
            def base_url(slug): raise ContainerError("none")
    st = asyncio.run(gr.mcp_gateway_status(_No(), expect_tools=True))
    assert st["reachable"] is None and gr.is_ready(st)


def test_unreachable_is_not_ready(monkeypatch):
    class _Boom:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): raise OSError("down")
        async def __aexit__(self, *e): return False
    monkeypatch.setattr(httpx, "AsyncClient", _Boom)
    st = _run(expect_tools=True)
    assert st["reachable"] is False and not gr.is_ready(st)


def test_gateway_readiness_collects_expectations_from_disk(monkeypatch, tmp_path):
    pkg = tmp_path / "demo"; pkg.mkdir()
    (pkg / "mcp.json").write_text('{"mcpServers": {"svc": {}, "off": {"enabled": false}}}')
    (pkg / "aw-app.json").write_text('{"version": "1.2.3"}')

    class _Loaded:
        package_dir = str(pkg)
        class manifest:
            agents = {}

    class _Rt(_RT):
        def loaded_slugs(self): return ["demo"]
        def get(self, slug): return _Loaded()

    exp = gr.collect_gateway_expectations(_Rt())
    assert exp["expect_tools"] is True
    assert exp["expected"] == {"svc": "demo"}
    assert exp["expected_versions"] == {"svc": "1.2.3"}
    _serve({"tools": 1, "local_upstreams": []}, monkeypatch)
    st = asyncio.run(gr.gateway_readiness(_Rt()))
    assert st["not_ready"][0]["server"] == "svc"
