"""src/api/otel.py — the export-target logic (ensure_export_state, the
amplification filter, the dynamic exporters). ``ensure_export_state`` and the
filter don't touch opentelemetry at all; the dynamic exporters' ``export()``
does (even on the no-op path — the import happens before the empty-target
check), so this suite depends on the opentelemetry deps from requirements.txt
being installed, same as the rest of this module in production.

Real-workspace findings (2026-09-03) this suite pins down so they can't
regress silently: ensure_export_state must route auto/local through the
INTERNAL container URL (not the public tunnel edge the Settings UI and the
AP-MT push use), and the amplification filter must catch httpx's own
per-request log line, not just opentelemetry's.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from src.api import otel


def _runtime(base_url="http://aw-app-signoz:8080"):
    containers = SimpleNamespace(base_url=lambda app_id: base_url)
    return SimpleNamespace(containers=containers)


def _resolved(source, endpoint, api_key="k"):
    return {"mode": source, "local_available": True,
            "resolved": {"endpoint": endpoint, "api_key": api_key, "source": source},
            "custom": {"endpoint": "", "api_key": ""}, "warning": None}


def _none_resolved():
    return {"mode": "off", "local_available": False, "resolved": None,
            "custom": {"endpoint": "", "api_key": ""}, "warning": None}


def setup_function(_):
    otel._set_target(None)


# --- ensure_export_state -----------------------------------------------------


def test_auto_mode_routes_through_the_internal_container_url_not_the_public_one():
    with patch("src.api.observability.resolve",
               return_value=_resolved("auto", "https://signoz.app.ws.example.com")):
        target = otel.ensure_export_state(_runtime())

    assert target == {"endpoint": "http://aw-app-signoz:8080", "headers": {"X-Api-Key": "k"}}


def test_local_mode_also_routes_through_the_internal_container_url():
    with patch("src.api.observability.resolve",
               return_value=_resolved("local", "https://signoz.app.ws.example.com")):
        target = otel.ensure_export_state(_runtime())

    assert target["endpoint"] == "http://aw-app-signoz:8080"


def test_custom_mode_keeps_the_endpoint_as_given():
    """A custom endpoint may point at a DIFFERENT workspace's signoz — this
    process has no container-network path to it, only the public one."""
    with patch("src.api.observability.resolve",
               return_value=_resolved("custom", "https://other.example.com/")):
        target = otel.ensure_export_state(_runtime())

    assert target["endpoint"] == "https://other.example.com"


def test_internal_lookup_failure_disables_export_rather_than_falling_back_to_public():
    """Real-workspace finding (2026-09-03): CI ran pytest with no real
    AppRuntime, ``base_url()`` raised, and the old code fell back to the
    PUBLIC tunnel endpoint — which has a 30s cutoff and stalled CI for 25+
    minutes. The public endpoint was never this consumer's intended target
    (see the module/function docstrings), so a failed internal lookup must
    disable export (``None``), not silently redirect to it."""
    def _boom(app_id):
        raise RuntimeError("no container registered")
    runtime = SimpleNamespace(containers=SimpleNamespace(base_url=_boom))
    with patch("src.api.observability.resolve",
               return_value=_resolved("auto", "https://signoz.app.ws.example.com")):
        target = otel.ensure_export_state(runtime)

    assert target is None
    assert otel.current_target() is None


def test_nothing_resolved_publishes_no_target():
    with patch("src.api.observability.resolve", return_value=_none_resolved()):
        target = otel.ensure_export_state(_runtime())

    assert target is None
    assert otel.current_target() is None


def test_never_raises_even_if_resolve_itself_blows_up():
    with patch("src.api.observability.resolve", side_effect=RuntimeError("db is down")):
        assert otel.ensure_export_state(_runtime()) is None


def test_no_runtime_falls_back_to_the_public_endpoint():
    """No AppRuntime (e.g. a bare unit-test host) — nothing to ask for a
    container URL, so the public one resolve() already gave us stands."""
    with patch("src.api.observability.resolve",
               return_value=_resolved("auto", "https://signoz.app.ws.example.com")):
        target = otel.ensure_export_state(None)

    assert target["endpoint"] == "https://signoz.app.ws.example.com"


# --- _build_resource -----------------------------------------------------


def test_build_resource_carries_this_processs_own_pid():
    """Multi-worker: a trace/span alone can't tell which of the N worker
    processes handled it without this — see finding_key
    observability:aw-workspace-core-worker-pid-attribute."""
    import os

    resource = otel._build_resource()

    assert resource.attributes["worker.pid"] == os.getpid()


# --- amplification filter -----------------------------------------------------


def test_amplification_filter_blocks_httpx_own_request_log():
    """The real loop found live: httpx logs its own POST to /v1/logs, which
    without this filter becomes the next batch's payload forever."""
    import logging
    f = otel._NoAmplification()
    record = logging.LogRecord("httpx", logging.INFO, __file__, 1,
                                'HTTP Request: POST .../v1/logs "HTTP/1.1 200 OK"', None, None)
    assert f.filter(record) is False


def test_amplification_filter_blocks_opentelemetrys_own_logger():
    import logging
    f = otel._NoAmplification()
    record = logging.LogRecord("opentelemetry.exporter.otlp.proto.http._log_exporter",
                                logging.WARNING, __file__, 1, "retrying", None, None)
    assert f.filter(record) is False


def test_amplification_filter_blocks_this_modules_own_logger():
    import logging
    f = otel._NoAmplification()
    record = logging.LogRecord("src.api.otel", logging.INFO, __file__, 1, "x", None, None)
    assert f.filter(record) is False


def test_amplification_filter_allows_real_application_loggers():
    import logging
    f = otel._NoAmplification()
    record = logging.LogRecord("src.apps.reconciler", logging.INFO, __file__, 1,
                                "apps: reconciled", None, None)
    assert f.filter(record) is True


# --- current_target / _set_target --------------------------------------------


def test_set_target_logs_only_on_an_actual_change(monkeypatch):
    calls = []
    monkeypatch.setattr(otel.log, "info", lambda *a, **k: calls.append(a))

    otel._set_target({"endpoint": "http://x", "headers": {}})
    otel._set_target({"endpoint": "http://x", "headers": {}})  # same target again
    otel._set_target({"endpoint": "http://x", "headers": {}})

    assert len(calls) == 1


# --- dynamic exporters: no-op while nothing is configured --------------------


def test_dynamic_span_exporter_is_a_silent_no_op_with_no_target():
    from opentelemetry.sdk.trace.export import SpanExportResult

    otel._set_target(None)
    exporter = otel._DynamicSpanExporter()
    assert exporter.export([]) == SpanExportResult.SUCCESS


def test_dynamic_log_exporter_is_a_silent_no_op_with_no_target():
    from opentelemetry.sdk._logs.export import LogExportResult

    otel._set_target(None)
    exporter = otel._DynamicLogExporter()
    assert exporter.export([]) == LogExportResult.SUCCESS


# --- the aw-app-signoz-connector hop ----------------------------------------
#
# When the connector app is installed it is a FORWARDER, so it becomes this
# process's next hop instead of the final destination (design §3.3). These
# pin the three things that are easy to get wrong: that it wins over the
# auto/local swap, that it applies to `custom` too, and that a workspace
# which resolved nothing still exports nothing.


def _connector_runtime(loaded=("signoz-connector",),
                       base_url="http://aw-app-signoz-connector:4318"):
    """A runtime that reports the given app ids as loaded.

    Note the default ``_runtime()`` above deliberately has NO ``is_loaded``
    at all, which ``connector_installed`` reads as "no connector" — that is
    what keeps every pre-existing test in this file on the old code path.
    """
    def _base_url(app_id):
        return base_url if app_id == "signoz-connector" else "http://aw-app-signoz:8080"

    return SimpleNamespace(
        is_loaded=lambda app_id: app_id in loaded,
        containers=SimpleNamespace(base_url=_base_url),
    )


def test_connector_installed_routes_through_its_internal_container_url():
    with patch("src.api.observability.resolve",
               return_value=_resolved("custom", "https://central.example.com")):
        target = otel.ensure_export_state(_connector_runtime())

    assert target["endpoint"] == "http://aw-app-signoz-connector:4318"


def test_connector_hop_carries_no_auth_header():
    """The resolved api_key authenticates to the FINAL destination, and the
    connector is what presents it upstream. Its own receiver is
    internal-only with no gate, so core must not ship the credential to a
    hop that never checks it."""
    with patch("src.api.observability.resolve",
               return_value=_resolved("custom", "https://central.example.com",
                                      api_key="the-central-ingest-token")):
        target = otel.ensure_export_state(_connector_runtime())

    assert target["headers"] == {}


def test_connector_takes_precedence_over_the_auto_local_signoz_swap():
    """Both apps installed. The connector is the forwarder, so it wins —
    otherwise a workspace with both would export straight past it and the
    connector's queue/doctor guarantees would silently not apply."""
    runtime = _connector_runtime(loaded=("signoz-connector", "signoz"))
    with patch("src.api.observability.resolve",
               return_value=_resolved("auto", "https://signoz.app.ws.example.com")):
        target = otel.ensure_export_state(runtime)

    assert target["endpoint"] == "http://aw-app-signoz-connector:4318"


def test_connector_internal_lookup_failure_exports_nothing_not_the_public_url():
    """Same rule as the signoz branch, and for the same recorded reason: the
    public endpoint was never this consumer's target and is known to hang
    (25+ minute CI stall, 2026-09-03), so a failed internal lookup disables
    export instead of redirecting to it."""
    def _boom(app_id):
        raise RuntimeError("no container registered")

    runtime = SimpleNamespace(is_loaded=lambda app_id: app_id == "signoz-connector",
                              containers=SimpleNamespace(base_url=_boom))
    with patch("src.api.observability.resolve",
               return_value=_resolved("custom", "https://central.example.com")):
        target = otel.ensure_export_state(runtime)

    assert target is None
    assert otel.current_target() is None


def test_connector_installed_but_nothing_resolved_still_exports_nothing():
    """§8.8 of the design doc: "no runtime → export nothing, never fall back
    to the public URL" — and nothing resolved is the same class. Exporting
    into the connector here would fill its disk-backed queue with telemetry
    that has no destination to drain to."""
    with patch("src.api.observability.resolve", return_value=_none_resolved()):
        target = otel.ensure_export_state(_connector_runtime())

    assert target is None
    assert otel.current_target() is None
