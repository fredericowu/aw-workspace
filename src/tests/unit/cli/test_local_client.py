"""``base_url()`` — where the CLI reaches its own workspace server.

Loopback must win whenever it's actually listening, even though the
server-published external tunnel URL is *also* present in every workspace's
``.env`` unconditionally. Preferring the external URL whenever present (the
old behavior) routed a CLI invoked co-located with its own server over the
public tunnel edge instead of the loopback sitting right there — and that
edge measured ~30% connect timeouts in production (fredericowu, 2026-09-26),
turning `marketplace update-all` into an intermittent hang.
"""
from __future__ import annotations

from src.cli import local_client


def test_explicit_override_wins_over_everything(monkeypatch):
    monkeypatch.setenv("AW_LOCAL_API_URL", "http://override:1234")
    monkeypatch.setattr(local_client, "_tcp_reachable", lambda host, port, timeout=0.3: False)
    assert local_client.base_url() == "http://override:1234"


def test_prefers_loopback_when_reachable_even_if_external_is_set(monkeypatch):
    monkeypatch.delenv("AW_LOCAL_API_URL", raising=False)
    monkeypatch.setenv("AW_WORKSPACE_API_URL", "https://api.example.workspace.aw.tekflox.com")
    monkeypatch.setenv("AW_PORT", "9030")
    monkeypatch.setattr(local_client, "_tcp_reachable", lambda host, port, timeout=0.3: True)
    assert local_client.base_url() == "http://127.0.0.1:9030"


def test_falls_back_to_external_when_loopback_is_not_listening(monkeypatch):
    monkeypatch.delenv("AW_LOCAL_API_URL", raising=False)
    monkeypatch.setenv("AW_WORKSPACE_API_URL", "https://api.example.workspace.aw.tekflox.com")
    monkeypatch.setattr(local_client, "_tcp_reachable", lambda host, port, timeout=0.3: False)
    assert local_client.base_url() == "https://api.example.workspace.aw.tekflox.com"


def test_falls_back_to_loopback_when_neither_override_nor_external_nor_reachable(monkeypatch):
    monkeypatch.delenv("AW_LOCAL_API_URL", raising=False)
    monkeypatch.delenv("AW_WORKSPACE_API_URL", raising=False)
    monkeypatch.setenv("AW_PORT", "9030")
    monkeypatch.setattr(local_client, "_tcp_reachable", lambda host, port, timeout=0.3: False)
    monkeypatch.setattr(local_client, "_read_env_value", lambda name: None)
    assert local_client.base_url() == "http://127.0.0.1:9030"
