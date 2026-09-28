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


# ---------------------------------------------------------------------------
# _workspace_api_key() self-heal
#
# Missing key + reachable server -> POST sync-env, re-read .env. Missing key
# + unreachable server -> today's plain guess. Still missing after sync-env
# -> a self-diagnosing error naming the server's own boot_id/git_head, per
# the Architect's design on card 3e95bf3b-9510-81f0-84f0-d01299c99f23.
# ---------------------------------------------------------------------------

def test_workspace_api_key_returns_key_when_present(monkeypatch):
    monkeypatch.setattr(local_client, "_read_env_value", lambda name: "existing-key")
    assert local_client._workspace_api_key() == "existing-key"


def test_workspace_api_key_raises_generic_guess_when_server_unreachable(monkeypatch):
    monkeypatch.setattr(local_client, "_read_env_value", lambda name: None)
    monkeypatch.setattr(local_client, "_probe_health", lambda timeout=3.0: None)
    sync_calls = []
    monkeypatch.setattr(local_client, "_sync_env", lambda timeout=3.0: sync_calls.append(1))

    try:
        local_client._workspace_api_key()
        assert False, "expected RuntimeError"
    except RuntimeError as exc:
        assert "is it running?" in str(exc)
    assert sync_calls == []


def test_workspace_api_key_self_heals_when_server_reachable(monkeypatch):
    reads = iter([None, "healed-key"])
    monkeypatch.setattr(local_client, "_read_env_value", lambda name: next(reads))
    monkeypatch.setattr(
        local_client, "_probe_health",
        lambda timeout=3.0: {"boot_id": "b1", "git_head": "abc123"},
    )
    sync_calls = []
    monkeypatch.setattr(local_client, "_sync_env", lambda timeout=3.0: sync_calls.append(1) or True)

    assert local_client._workspace_api_key() == "healed-key"
    assert sync_calls == [1]


def test_workspace_api_key_self_diagnosing_error_when_still_missing_after_sync(monkeypatch):
    monkeypatch.setattr(local_client, "_read_env_value", lambda name: None)
    monkeypatch.setattr(
        local_client, "_probe_health",
        lambda timeout=3.0: {"boot_id": "b1", "git_head": "abc123"},
    )
    monkeypatch.setattr(local_client, "_sync_env", lambda timeout=3.0: True)

    try:
        local_client._workspace_api_key()
        assert False, "expected RuntimeError"
    except RuntimeError as exc:
        message = str(exc)
        assert "boot_id=b1" in message
        assert "git_head=abc123" in message
        assert "still absent" in message


# ---------------------------------------------------------------------------
# request() 401 retry — exactly once, and only when sync-env actually
# changed the key on disk (a genuinely revoked caller must not hammer
# sync-env forever).
# ---------------------------------------------------------------------------

def test_request_retries_once_after_401_when_key_changed(monkeypatch):
    monkeypatch.setattr(local_client, "base_url", lambda: "http://test")
    monkeypatch.setattr(local_client, "_workspace_api_key", lambda: "stale-key")

    reads = iter(["fresh-key"])
    monkeypatch.setattr(local_client, "_read_env_value", lambda name: next(reads))
    sync_calls = []
    monkeypatch.setattr(local_client, "_sync_env", lambda timeout=3.0: sync_calls.append(1) or True)

    import httpx as httpx_mod

    calls = []

    def _fake_request(method, url, json=None, headers=None, timeout=None):
        calls.append(headers[local_client.HEADER_NAME])
        if len(calls) == 1:
            return httpx_mod.Response(401, json={"detail": "unauthorized"},
                                       request=httpx_mod.Request(method, url))
        return httpx_mod.Response(200, json={"ok": True}, request=httpx_mod.Request(method, url))

    monkeypatch.setattr(local_client.httpx, "request", _fake_request)

    status, body = local_client.request("GET", "/api/whatever")

    assert sync_calls == [1]
    assert calls == ["stale-key", "fresh-key"]
    assert status == 200
    assert body == {"ok": True}


def test_request_does_not_retry_when_key_unchanged_after_sync(monkeypatch):
    monkeypatch.setattr(local_client, "base_url", lambda: "http://test")
    monkeypatch.setattr(local_client, "_workspace_api_key", lambda: "same-key")
    monkeypatch.setattr(local_client, "_read_env_value", lambda name: "same-key")
    monkeypatch.setattr(local_client, "_sync_env", lambda timeout=3.0: True)

    import httpx as httpx_mod

    calls = []

    def _fake_request(method, url, json=None, headers=None, timeout=None):
        calls.append(headers[local_client.HEADER_NAME])
        return httpx_mod.Response(401, json={"detail": "unauthorized"},
                                   request=httpx_mod.Request(method, url))

    monkeypatch.setattr(local_client.httpx, "request", _fake_request)

    status, _ = local_client.request("GET", "/api/whatever")

    assert calls == ["same-key"]
    assert status == 401
