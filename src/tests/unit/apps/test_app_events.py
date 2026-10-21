from __future__ import annotations

from src.apps import app_events


def test_app_event_credentials_are_app_scoped(monkeypatch):
    monkeypatch.setattr(app_events, "get_or_create_workspace_api_key", lambda: "workspace-secret")
    alpha = app_events.app_event_token("alpha")
    beta = app_events.app_event_token("beta")
    assert alpha != beta
    assert app_events.verify_app_event_token("alpha", alpha)
    assert not app_events.verify_app_event_token("beta", alpha)


def test_payload_size_is_utf8_encoded_bytes():
    assert app_events.payload_size({"value": "é"}) > len('{"value":"é"}')
