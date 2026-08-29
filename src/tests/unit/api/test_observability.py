"""Observability settings — mode validation, local-mode gating on
aw-app-signoz being installed, and the downgrade-to-off-on-uninstall path.

Storage is stubbed at the module-function level (``_load``/``_save``), same
approach ``src/tests/unit/api/test_folders.py`` uses for the registry — no
Postgres in this suite.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from src.api import observability


def _store(monkeypatch, initial=None):
    state = {"value": dict(initial or {})}
    monkeypatch.setattr(observability, "_load", lambda session=None: dict(state["value"]))

    def fake_save(value):
        state["value"] = dict(value)

    monkeypatch.setattr(observability, "_save", fake_save)
    return state


def _runtime(installed):
    return SimpleNamespace(is_loaded=lambda slug: installed)


def _response(status, json_body=None):
    # A bare httpx.Response has no ._request, and raise_for_status() refuses
    # to run at all without one (even for a 2xx) — a real httpx.Client always
    # attaches one, so a hand-built mock response needs to do it explicitly.
    request = httpx.Request(
        "POST", "http://127.0.0.1:9030/api/apps/agents-platform-runners/register-observability")
    return httpx.Response(status, json=json_body, request=request)


@pytest.fixture(autouse=True)
def _local_target(monkeypatch):
    monkeypatch.setattr(observability, "app_public_url",
                         lambda app_id: f"https://{app_id}.app.ws.example.com")
    monkeypatch.setattr(observability, "get_or_create_workspace_api_key",
                         lambda: "the-workspace-key")
    # Real backoff would slow down every retry test for no reason — the
    # retry COUNT is what these tests exercise, not the sleep duration.
    monkeypatch.setattr(observability, "NOTIFY_RETRY_BACKOFF_S", 0)


# --- signoz_installed ---------------------------------------------------------


def test_signoz_installed_true_when_runtime_reports_loaded():
    assert observability.signoz_installed(_runtime(True)) is True


def test_signoz_installed_false_when_not_loaded():
    assert observability.signoz_installed(_runtime(False)) is False


def test_signoz_installed_false_without_a_runtime():
    assert observability.signoz_installed(None) is False


# --- resolve -------------------------------------------------------------------


def test_defaults_to_off(monkeypatch):
    _store(monkeypatch)
    result = observability.resolve(_runtime(True))
    assert result["mode"] == "off"
    assert result["resolved"] is None
    assert result["warning"] is None


def test_local_resolves_to_the_derived_endpoint_and_key(monkeypatch):
    _store(monkeypatch, {"mode": "local"})
    result = observability.resolve(_runtime(True))
    assert result["mode"] == "local"
    assert result["local_available"] is True
    assert result["resolved"] == {
        "endpoint": "https://signoz.app.ws.example.com",
        "api_key": "the-workspace-key",
        "source": "local",
    }


def test_local_mode_downgrades_to_off_when_app_is_gone(monkeypatch):
    state = _store(monkeypatch, {"mode": "local"})

    result = observability.resolve(_runtime(False))

    assert result["mode"] == "off"
    assert result["resolved"] is None
    assert result["warning"] and "uninstalled" in result["warning"]
    # The downgrade was persisted, not just reported once.
    assert state["value"]["mode"] == "off"


def test_custom_resolves_to_the_stored_endpoint_and_key(monkeypatch):
    _store(monkeypatch, {
        "mode": "custom",
        "custom": {"endpoint": "https://other.example.com", "api_key": "k"},
    })
    result = observability.resolve(_runtime(False))
    assert result["resolved"] == {
        "endpoint": "https://other.example.com", "api_key": "k", "source": "custom",
    }


def test_custom_with_no_endpoint_yet_resolves_to_nothing(monkeypatch):
    _store(monkeypatch, {"mode": "custom"})
    result = observability.resolve(_runtime(False))
    assert result["mode"] == "custom"
    assert result["resolved"] is None


# --- update ----------------------------------------------------------------


def test_update_to_off_always_succeeds(monkeypatch):
    state = _store(monkeypatch, {"mode": "local"})
    observability.update("off", None, None, _runtime(False))
    assert state["value"]["mode"] == "off"


def test_update_to_local_requires_the_app_installed(monkeypatch):
    _store(monkeypatch)
    with pytest.raises(observability.ObservabilityError, match="installed"):
        observability.update("local", None, None, _runtime(False))


def test_update_to_local_succeeds_when_installed(monkeypatch):
    state = _store(monkeypatch)
    observability.update("local", None, None, _runtime(True))
    assert state["value"]["mode"] == "local"


def test_update_to_custom_requires_an_endpoint(monkeypatch):
    _store(monkeypatch)
    with pytest.raises(observability.ObservabilityError, match="endpoint"):
        observability.update("custom", "", "key", _runtime(False))


def test_update_to_custom_requires_an_http_url(monkeypatch):
    _store(monkeypatch)
    with pytest.raises(observability.ObservabilityError, match="http"):
        observability.update("custom", "not-a-url", "key", _runtime(False))


def test_update_to_custom_persists_endpoint_and_key(monkeypatch):
    state = _store(monkeypatch)
    observability.update("custom", "https://other.example.com/", "k", _runtime(False))
    assert state["value"]["custom"] == {
        "endpoint": "https://other.example.com/", "api_key": "k",
    }


def test_update_rejects_an_unknown_mode(monkeypatch):
    _store(monkeypatch)
    with pytest.raises(observability.ObservabilityError, match="mode must be"):
        observability.update("bogus", None, None, _runtime(False))


# --- _notify_agents_platform_runners --------------------------------------------


@pytest.mark.asyncio
async def test_notify_posts_to_the_local_register_observability_route(monkeypatch):
    monkeypatch.setenv("AW_PORT", "9030")
    mock_client = AsyncMock()
    mock_client.post.return_value = _response(200, {"pushed": True})
    mock_client.__aenter__.return_value = mock_client

    with patch("src.api.observability.httpx.AsyncClient", return_value=mock_client):
        result = await observability._notify_agents_platform_runners()

    mock_client.post.assert_awaited_once_with(
        "http://127.0.0.1:9030/api/apps/agents-platform-runners/register-observability",
        headers={"X-Api-Key": "the-workspace-key"},
    )
    assert result == {"ok": True, "reason": None}


@pytest.mark.asyncio
async def test_notify_swallows_a_connection_failure(monkeypatch):
    mock_client = AsyncMock()
    mock_client.post.side_effect = httpx.ConnectError("connection refused")
    mock_client.__aenter__.return_value = mock_client

    with patch("src.api.observability.httpx.AsyncClient", return_value=mock_client):
        result = await observability._notify_agents_platform_runners()  # must not raise

    assert result["ok"] is False
    assert "connection refused" in result["reason"]


@pytest.mark.asyncio
async def test_notify_swallows_an_app_not_installed_404(monkeypatch):
    request = httpx.Request("POST", "http://127.0.0.1:9030/api/apps/agents-platform-runners/register-observability")
    mock_client = AsyncMock()
    mock_client.post.return_value = httpx.Response(404, request=request)
    mock_client.__aenter__.return_value = mock_client

    with patch("src.api.observability.httpx.AsyncClient", return_value=mock_client):
        result = await observability._notify_agents_platform_runners()  # must not raise

    assert result["ok"] is False


@pytest.mark.asyncio
async def test_notify_retries_then_succeeds(monkeypatch):
    mock_client = AsyncMock()
    mock_client.post.side_effect = [
        httpx.ConnectError("connection refused"),
        _response(200, {"pushed": True, "mode": "local"}),
    ]
    mock_client.__aenter__.return_value = mock_client
    log_mock = MagicMock()
    monkeypatch.setattr(observability, "log", log_mock)

    with patch("src.api.observability.httpx.AsyncClient", return_value=mock_client):
        result = await observability._notify_agents_platform_runners()

    assert result == {"ok": True, "reason": None}
    assert mock_client.post.await_count == 2
    # The failed first attempt is still logged — success doesn't erase it.
    log_mock.warning.assert_called_once()
    log_mock.error.assert_not_called()


@pytest.mark.asyncio
async def test_notify_treats_a_pushed_false_body_as_a_failure_not_a_success(monkeypatch):
    """The exact gap QA reported: /register-observability answers 200 even
    when the remote leg to AP-MT itself failed (AP-MT down, timeout, no
    token) — checking only the HTTP status would read that as success."""
    mock_client = AsyncMock()
    mock_client.post.return_value = _response(
        200, {"pushed": False, "reason": "agents_platform_token not configured"})
    mock_client.__aenter__.return_value = mock_client
    log_mock = MagicMock()
    monkeypatch.setattr(observability, "log", log_mock)

    with patch("src.api.observability.httpx.AsyncClient", return_value=mock_client):
        result = await observability._notify_agents_platform_runners()

    assert result == {"ok": False, "reason": "agents_platform_token not configured"}
    assert mock_client.post.await_count == observability.NOTIFY_MAX_ATTEMPTS
    log_mock.error.assert_called_once()


@pytest.mark.asyncio
async def test_notify_exhausted_retries_logs_a_warning_per_attempt_and_one_final_error(monkeypatch):
    mock_client = AsyncMock()
    mock_client.post.side_effect = httpx.ConnectError("connection refused")
    mock_client.__aenter__.return_value = mock_client
    log_mock = MagicMock()
    monkeypatch.setattr(observability, "log", log_mock)

    with patch("src.api.observability.httpx.AsyncClient", return_value=mock_client):
        result = await observability._notify_agents_platform_runners()

    assert result["ok"] is False
    assert mock_client.post.await_count == observability.NOTIFY_MAX_ATTEMPTS
    assert log_mock.warning.call_count == observability.NOTIFY_MAX_ATTEMPTS
    log_mock.error.assert_called_once()
    # The error carries context, not just "it failed" — attempt count and the
    # last reason, so a human reading logs doesn't have to reconstruct it.
    error_args = log_mock.error.call_args[0]
    assert str(observability.NOTIFY_MAX_ATTEMPTS) in [str(a) for a in error_args]
