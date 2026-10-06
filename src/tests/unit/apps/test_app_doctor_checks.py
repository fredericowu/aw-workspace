"""``_app_doctor_checks`` — how an app's ``contributes.doctor`` answer is read.

The content-type gate used to be the whole story here and it silently cost a
real app its doctor entry: the OpenTelemetry Collector's ``health_check``
extension serves exactly the ``{"ok": ...}`` shape, labels it
``text/plain; charset=utf-8``, and ignores its own ``response_headers``
override — so the check read ``ok: false`` forever with ``{}`` for a detail.
These pin the replacement: parse first, judge on what comes back, and keep
"unreadable = failing" either way.
"""
from __future__ import annotations

import asyncio

from starlette.applications import Starlette
from starlette.responses import JSONResponse, PlainTextResponse, Response
from starlette.routing import Route

from src.apps.routes import _app_doctor_checks


class _Manifest:
    def __init__(self, checks):
        self.doctor_checks = checks


class _Drainable:
    def __init__(self, app):
        self.app = app


class _Loaded:
    def __init__(self, app, checks):
        self.manifest = _Manifest(checks)
        self.drainable = _Drainable(app)


class _Runtime:
    """Just enough of AppRuntime for the one function under test."""

    def __init__(self, app, checks, slug="demo"):
        self._slug = slug
        self._loaded = _Loaded(app, checks)

    def loaded_slugs(self):
        return [self._slug]

    def get(self, slug):
        return self._loaded if slug == self._slug else None


def _run(app, body_route="/healthz"):
    checks = [{"label": "forwarding", "route": body_route}]
    return asyncio.run(_app_doctor_checks(_Runtime(app, checks)))


def _app(handler, path="/healthz"):
    return Starlette(routes=[Route(path, handler)])


# ---- the regression this change exists for ---------------------------------

def test_json_body_mislabelled_as_text_plain_is_honoured():
    """The otel-collector case: right body, wrong Content-Type."""
    rows = _run(_app(lambda r: PlainTextResponse('{"ok": true, "detail": "exporting"}')))
    assert len(rows) == 1
    assert rows[0]["ok"] is True
    assert rows[0]["detail"] == {"ok": True, "detail": "exporting"}


def test_a_mislabelled_body_reporting_not_ok_still_reads_not_ok():
    """Parsing regardless of content-type must not turn into trusting it."""
    rows = _run(_app(lambda r: PlainTextResponse('{"ok": false}')))
    assert rows[0]["ok"] is False


# ---- "unreadable = failing" survives ---------------------------------------

def test_a_genuinely_unparseable_body_fails_with_the_evidence():
    """Used to be ok:false with detail {} — right verdict, no evidence."""
    rows = _run(_app(lambda r: PlainTextResponse("I am not JSON")))
    assert rows[0]["ok"] is False
    assert "unparseable body" in rows[0]["detail"]
    assert "text/plain" in rows[0]["detail"]
    assert "I am not JSON" in rows[0]["detail"]


def test_valid_json_that_is_not_an_object_fails():
    """`[1,2]`.get("ok") would raise; say so instead of leaking a TypeError."""
    rows = _run(_app(lambda r: JSONResponse([1, 2])))
    assert rows[0]["ok"] is False
    assert "expected a JSON object" in rows[0]["detail"]


def test_non_200_is_reported_as_the_status_not_the_body():
    rows = _run(_app(lambda r: JSONResponse({"ok": True}, status_code=503)))
    assert rows[0]["ok"] is False
    assert "HTTP 503" in rows[0]["detail"]


def test_an_empty_body_fails_rather_than_reading_as_ok():
    rows = _run(_app(lambda r: Response(status_code=200)))
    assert rows[0]["ok"] is False


def test_a_route_the_app_never_mounted_fails():
    rows = _run(_app(lambda r: JSONResponse({"ok": True}), path="/elsewhere"))
    assert rows[0]["ok"] is False


def test_an_app_that_mounted_no_routes_at_all_fails():
    checks = [{"label": "forwarding", "route": "/healthz"}]
    runtime = _Runtime(None, checks)
    runtime._loaded.drainable.app = None
    rows = asyncio.run(_app_doctor_checks(runtime))
    assert rows[0]["ok"] is False
    assert "mounted no routes" in rows[0]["detail"]


def test_json_with_the_right_content_type_still_works():
    """The path that already worked must keep working."""
    rows = _run(_app(lambda r: JSONResponse({"ok": True, "detail": "fine"})))
    assert rows[0]["ok"] is True


def test_an_app_declaring_no_checks_contributes_no_rows():
    assert asyncio.run(_app_doctor_checks(_Runtime(_app(lambda r: Response()), []))) == []


# ---- a check with an explicit port dials the container, not the proxy -------
# A Tier-2 app gets exactly one proxied port, and its health endpoint may not
# be on it: the connector answers on 13133 while runtime.port must stay 4318
# for core's OTLP export. See Manifest.doctor_checks.

class _Containers:
    def __init__(self, url="http://aw-app-demo:4318"):
        self._url = url

    def base_url(self, slug):
        if self._url is None:
            raise RuntimeError(f"no container registered for {slug!r}")
        return self._url


def _port_runtime(app, port, container_url="http://aw-app-demo:4318"):
    rt = _Runtime(app, [{"label": "forwarding", "route": "/healthz", "port": port}])
    rt.containers = _Containers(container_url)
    return rt


def test_a_port_check_dials_the_container_host_on_that_port(monkeypatch):
    seen: dict = {}

    class _FakeClient:
        def __init__(self, **kwargs):
            seen.update(kwargs)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, route, timeout=None):
            seen["route"] = route
            seen["timeout"] = timeout
            import httpx as _h
            return _h.Response(200, text='{"ok": true}',
                               headers={"content-type": "text/plain"})

    import httpx
    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)
    rows = asyncio.run(_app_doctor_checks(_port_runtime(None, 13133)))

    assert seen["base_url"] == "http://aw-app-demo:13133"   # port swapped, host kept
    assert "transport" not in seen                          # real HTTP, not ASGI
    assert seen["route"] == "/healthz"
    assert seen["timeout"] == 20.0                           # _DOCTOR_CHECK_TIMEOUT_S
    assert rows[0]["ok"] is True


def test_a_port_check_on_an_app_with_no_container_fails_cleanly():
    """Tier-1 app declaring a port: a FAILING check, never a skip."""
    rt = _port_runtime(_app(lambda r: JSONResponse({"ok": True})), 13133,
                       container_url=None)
    rows = asyncio.run(_app_doctor_checks(rt))
    assert rows[0]["ok"] is False
    assert "no container to dial" in rows[0]["detail"]


def test_a_port_check_still_works_when_the_app_mounted_no_routes(monkeypatch):
    """`inner is None` must not short-circuit a check that never needed it."""
    rows = []

    class _FakeClient:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, route, timeout=None):
            import httpx as _h
            return _h.Response(200, json={"ok": True})

    import httpx
    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)
    rows = asyncio.run(_app_doctor_checks(_port_runtime(None, 13133)))
    assert rows[0]["ok"] is True
    assert "mounted no routes" not in str(rows[0]["detail"])
