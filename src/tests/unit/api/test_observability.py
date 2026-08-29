"""Observability settings — mode validation, local-mode gating on
aw-app-signoz being installed, and the downgrade-to-off-on-uninstall path.

Storage is stubbed at the module-function level (``_load``/``_save``), same
approach ``src/tests/unit/api/test_folders.py`` uses for the registry — no
Postgres in this suite.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

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


@pytest.fixture(autouse=True)
def _local_target(monkeypatch):
    monkeypatch.setattr(observability, "app_public_url",
                         lambda app_id: f"https://{app_id}.app.ws.example.com")
    monkeypatch.setattr(observability, "get_or_create_workspace_api_key",
                         lambda: "the-workspace-key")


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
    mock_client.post.return_value = httpx.Response(200, json={"pushed": True})
    mock_client.__aenter__.return_value = mock_client

    with patch("src.api.observability.httpx.AsyncClient", return_value=mock_client):
        await observability._notify_agents_platform_runners()

    mock_client.post.assert_awaited_once_with(
        "http://127.0.0.1:9030/api/apps/agents-platform-runners/register-observability",
        headers={"X-Api-Key": "the-workspace-key"},
    )


@pytest.mark.asyncio
async def test_notify_swallows_a_connection_failure(monkeypatch):
    mock_client = AsyncMock()
    mock_client.post.side_effect = httpx.ConnectError("connection refused")
    mock_client.__aenter__.return_value = mock_client

    with patch("src.api.observability.httpx.AsyncClient", return_value=mock_client):
        await observability._notify_agents_platform_runners()  # must not raise


@pytest.mark.asyncio
async def test_notify_swallows_an_app_not_installed_404(monkeypatch):
    request = httpx.Request("POST", "http://127.0.0.1:9030/api/apps/agents-platform-runners/register-observability")
    mock_client = AsyncMock()
    mock_client.post.return_value = httpx.Response(404, request=request)
    mock_client.__aenter__.return_value = mock_client

    with patch("src.api.observability.httpx.AsyncClient", return_value=mock_client):
        await observability._notify_agents_platform_runners()  # must not raise
