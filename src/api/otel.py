"""OpenTelemetry wiring for aw-workspace core itself — every existing
``logging.getLogger(...)`` call and every inbound HTTP request, shipped to
whatever this workspace's Observability setting currently resolves to (see
:mod:`src.api.observability`). Ported from the (disabled) reference in
``repos/agentic-workspace/src/api/app.py``, reshaped around one hard
constraint the reference didn't have to deal with: the export TARGET has to
be swappable at runtime with no restart, because ``auto`` mode can flip
between "aw-app-signoz is installed" and "it isn't" at any moment (app
install/uninstall, a settings save).

**The OTel graph itself is built exactly ONCE per process, unconditionally**
(:func:`init_otel`, called from ``create_app()`` before ``add_middleware``).
It is never torn down. Two hard reasons, not preference:

* ``opentelemetry.trace.set_tracer_provider`` is one-shot — a second call is
  a silent no-op with a warning, so a design that waits for "the app gets
  installed" to build the provider can never recover if signoz is installed
  AFTER the process has already set a no-op provider at boot.
* ``Starlette``/``FastAPI`` forbids adding middleware once the ASGI app has
  started handling requests — so the trace middleware has to go on at
  ``create_app()`` time or never.

What DOES change at runtime is only the endpoint/headers the dynamic
exporters (:class:`_DynamicSpanExporter`, :class:`_DynamicLogExporter`) send
to — see :func:`ensure_export_state`, which is the one function that
recomputes "what should auto resolve to right now" and publishes it via
:func:`current_target`. Call sites: end of the boot reconcile
(``src/apps/routes.py::reconcile_on_boot``), the reconciler's own
``install()``/``uninstall()`` (``src/apps/reconciler.py``), and the
observability settings PUT (``src/api/observability.py``) — the three
moments that can change what ``auto`` resolves to. A target of ``None``
means "export nothing", handled entirely inside the dynamic exporters (no
network call attempted) rather than by tearing anything down.

Never raises into a caller — a workspace with the opentelemetry deps not
yet installed (fresh boot before ``_sync_venv_deps`` has run) or a broken
install must still boot and serve requests with no telemetry, not crash.
"""
from __future__ import annotations

import logging
import os
import socket
import sys
import threading
from typing import Any

log = logging.getLogger(__name__)

#: Logger name prefixes whose OWN records must never reach the OTLP log
#: handler. Without this, a failing exporter logs its own failure through
#: this module's or opentelemetry's own logger, which the root handler
#: picks up and tries to export too — an amplification loop the Architect
#: design on this card called out by name.
#:
#: ``httpx`` is here for a DIFFERENT reason, found empirically against the
#: real workspace SigNoz (2026-09-03), not anticipated in the design doc:
#: httpx logs an INFO line for every request it makes — including the OTLP
#: log exporter's own successful POST to ``/v1/logs``. Without this filter
#: that confirmation line becomes the next batch's payload, whose own
#: successful POST logs another confirmation, forever — a steady ~2
#: lines/5s hum with no application traffic at all. The equivalent
#: information (method, URL, status) is already on the span
#: HTTPXClientInstrumentor produces for every httpx call, so dropping this
#: from LOGS costs nothing — it only ever duplicated the trace.
_AMPLIFICATION_PREFIXES = ("opentelemetry", __name__, "httpx")


class _NoAmplification(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return not record.name.startswith(_AMPLIFICATION_PREFIXES)


# --- export target: the one thing that changes at runtime -------------------

_state_lock = threading.Lock()
_target: dict[str, Any] | None = None
_initialized = False


def current_target() -> dict[str, Any] | None:
    """``{"endpoint": ..., "headers": {...}}`` the dynamic exporters should
    send to right now, or ``None`` to export nothing. Set by
    :func:`ensure_export_state`."""
    with _state_lock:
        return _target


def _set_target(new_target: dict[str, Any] | None) -> None:
    global _target
    with _state_lock:
        changed = _target != new_target
        _target = new_target
    if not changed:
        return
    # Once per actual state CHANGE, never once per export attempt — a dead
    # endpoint must not become a log line per request (Architect risk #4).
    if new_target:
        log.info("otel: exporting to %s", new_target["endpoint"])
    else:
        log.info("otel: export disabled (no observability target)")


def ensure_export_state(runtime: Any = None) -> dict[str, Any] | None:
    """Recompute the effective OTLP target and publish it for the dynamic
    exporters to pick up on their next export. Idempotent and cheap (one
    settings-table read via :func:`src.api.observability.resolve`) — safe to
    call as often as needed. Never raises.

    ``runtime`` is a :class:`src.apps.runtime.AppRuntime` (or ``None``,
    which reads as "aw-app-signoz not installed", same convention as
    ``observability.signoz_installed``).

    ``resolve()``'s ``resolved.endpoint`` is deliberately the PUBLIC tunnel
    URL — that value also feeds the Settings UI and the push to
    agents-platform-multitenant, an external system with no path onto this
    workspace's podman network. This process is a different consumer: when
    the source is ``auto``/``local`` (this workspace's OWN aw-app-signoz),
    it swaps in the internal container URL
    (``runtime.containers.base_url("signoz")``, e.g.
    ``http://aw-app-signoz:8080``) instead — same auth (the app's own nginx
    still gates on the X-Api-Key header, per the card), but without the
    public tunnel's 30s cutoff and the container:aw-sandbox netns hop
    (Architect finding #2). ``custom`` mode is left as given: it may point
    at a *different* workspace's signoz, which this process only has a
    public path to.
    """
    try:
        from src.api.observability import SIGNOZ_APP_ID, resolve
        from src.api.workspace_api_key import HEADER_NAME

        result = resolve(runtime)
        resolved = result.get("resolved")
        target = None
        if resolved and resolved.get("endpoint"):
            endpoint = str(resolved["endpoint"]).rstrip("/")
            if resolved.get("source") in ("auto", "local") and runtime is not None:
                try:
                    endpoint = runtime.containers.base_url(SIGNOZ_APP_ID).rstrip("/")
                except Exception:
                    log.warning(
                        "otel: could not resolve aw-app-signoz's internal container "
                        "URL, falling back to the public endpoint", exc_info=True)
            headers = {}
            api_key = resolved.get("api_key")
            if api_key:
                headers[HEADER_NAME] = api_key
            target = {"endpoint": endpoint, "headers": headers}
        _set_target(target)
        return target
    except Exception:
        log.exception("otel: ensure_export_state failed")
        return None


# --- dynamic exporters: same provider, swappable endpoint -------------------


class _DynamicSpanExporter:
    """A ``SpanExporter`` that re-resolves its real target on every export
    instead of being constructed with one fixed endpoint — the real
    ``OTLPSpanExporter`` is cached and only rebuilt when the target actually
    changes. Exports as a silent no-op (``SUCCESS``, no network call) while
    ``current_target()`` is ``None``."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._key: tuple | None = None
        self._real = None

    def _get(self, target: dict[str, Any]):
        key = (target["endpoint"], tuple(sorted(target["headers"].items())))
        with self._lock:
            if key != self._key:
                from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
                    OTLPSpanExporter,
                )

                if self._real is not None:
                    try:
                        self._real.shutdown()
                    except Exception:
                        pass
                self._real = OTLPSpanExporter(
                    endpoint=f"{target['endpoint']}/v1/traces", headers=target["headers"])
                self._key = key
            return self._real

    def export(self, spans):
        from opentelemetry.sdk.trace.export import SpanExportResult

        target = current_target()
        if not target:
            return SpanExportResult.SUCCESS
        try:
            return self._get(target).export(spans)
        except Exception:
            # No log here on purpose — see the module docstring and
            # _set_target: a dead endpoint must not become a log line per
            # export batch. The one-time state-change log already covers it.
            return SpanExportResult.FAILURE

    def shutdown(self) -> None:
        with self._lock:
            if self._real is not None:
                self._real.shutdown()

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        with self._lock:
            real = self._real
        return real.force_flush(timeout_millis) if real is not None else True


class _DynamicLogExporter:
    """Same shape as :class:`_DynamicSpanExporter`, for the OTLP log path."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._key: tuple | None = None
        self._real = None

    def _get(self, target: dict[str, Any]):
        key = (target["endpoint"], tuple(sorted(target["headers"].items())))
        with self._lock:
            if key != self._key:
                from opentelemetry.exporter.otlp.proto.http._log_exporter import (
                    OTLPLogExporter,
                )

                if self._real is not None:
                    try:
                        self._real.shutdown()
                    except Exception:
                        pass
                self._real = OTLPLogExporter(
                    endpoint=f"{target['endpoint']}/v1/logs", headers=target["headers"])
                self._key = key
            return self._real

    def export(self, batch):
        from opentelemetry.sdk._logs.export import LogExportResult

        target = current_target()
        if not target:
            return LogExportResult.SUCCESS
        try:
            return self._get(target).export(batch)
        except Exception:
            return LogExportResult.FAILURE

    def shutdown(self) -> None:
        with self._lock:
            if self._real is not None:
                self._real.shutdown()

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return True


# --- the graph itself: built once, incondicional ----------------------------


def _build_resource():
    from opentelemetry.sdk.resources import Resource

    return Resource.create({
        "service.name": "aw-workspace",
        "service.version": os.environ.get("AW_WORKSPACE_VERSION", ""),
        "service.instance.id": socket.gethostname(),
        "deployment.environment": os.environ.get("AW_ENV", "production"),
        "workspace.slug": os.environ.get("AW_WORKSPACE", ""),
    })


def _init_process_once() -> None:
    from opentelemetry import trace
    from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
    from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor
    from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor

    resource = _build_resource()

    tracer_provider = TracerProvider(resource=resource)
    tracer_provider.add_span_processor(BatchSpanProcessor(_DynamicSpanExporter()))
    trace.set_tracer_provider(tracer_provider)

    logger_provider = LoggerProvider(resource=resource)
    logger_provider.add_log_record_processor(BatchLogRecordProcessor(_DynamicLogExporter()))

    # The root logger has NO handler at all today, so `logging.lastResort`
    # is what currently prints WARNING+ to stderr (Architect finding #1).
    # Both handlers below must be present together: the OTLP handler alone
    # would give the root logger a real handler and silently turn
    # `lastResort` off, muting docker logs the moment this installs.
    root = logging.getLogger()
    root.setLevel(logging.INFO)

    stderr_handler = logging.StreamHandler(sys.stderr)
    stderr_handler.setLevel(logging.WARNING)
    root.addHandler(stderr_handler)

    otlp_handler = LoggingHandler(level=logging.INFO, logger_provider=logger_provider)
    otlp_handler.addFilter(_NoAmplification())
    root.addHandler(otlp_handler)

    # httpx only — this codebase never imports `requests` for its own calls,
    # and deliberately NOT instrumenting it is what keeps the OTLP exporters
    # above (built on `requests`, per opentelemetry-exporter-otlp-proto-http)
    # from tracing their own outbound POST, which would otherwise become a
    # trace-export loop against a live SigNoz (Architect risk #3).
    HTTPXClientInstrumentor().instrument()


def _add_middleware(app) -> None:
    from opentelemetry.instrumentation.asgi import OpenTelemetryMiddleware

    app.add_middleware(OpenTelemetryMiddleware)


def init_otel(app) -> None:
    """Build the process-global OTel graph the first time this runs, and
    attach the ASGI trace middleware to THIS ``app`` instance every time.
    Call unconditionally from ``create_app()``, before ``add_middleware``.
    See the module docstring for why this can never be deferred until an
    app is actually installed."""
    global _initialized
    try:
        if not _initialized:
            _init_process_once()
            _initialized = True
        _add_middleware(app)
    except Exception:
        log.warning("otel: init failed — this process will serve with no telemetry",
                    exc_info=True)
