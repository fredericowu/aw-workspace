"""Unit tests for the F5b redis_coord bootstrap helpers — no live Redis
needed, these only check env-var resolution, key-prefix scoping, and the
`get_redis_pool()` per-URL cache. Cross-process behavior of the four
coordination primitives themselves is covered by
src/tests/integration/libs/test_redis_coord.py and
test_redis_poll_queue.py (require a real Redis, skip cleanly otherwise).
"""
from __future__ import annotations

import importlib

import pytest


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("AW_WORKSPACE_REDIS_URL", raising=False)
    monkeypatch.delenv("AW_WORKSPACE", raising=False)
    import src.libs.redis_coord as redis_coord
    importlib.reload(redis_coord)
    yield redis_coord
    importlib.reload(redis_coord)


class TestGetWorkspaceRedisUrl:
    def test_falls_back_to_default_when_unset(self, _clean_env):
        assert _clean_env.get_workspace_redis_url() == _clean_env.DEFAULT_REDIS_URL

    def test_uses_env_var_when_set(self, _clean_env, monkeypatch):
        monkeypatch.setenv("AW_WORKSPACE_REDIS_URL", "redis://10.0.0.5:6379/2")
        assert _clean_env.get_workspace_redis_url() == "redis://10.0.0.5:6379/2"


class TestGetWorkspaceSlug:
    def test_falls_back_to_default_when_unset(self, _clean_env):
        assert _clean_env.get_workspace_slug() == _clean_env.DEFAULT_WORKSPACE

    def test_uses_env_var_when_set(self, _clean_env, monkeypatch):
        monkeypatch.setenv("AW_WORKSPACE", "acme")
        assert _clean_env.get_workspace_slug() == "acme"


class TestKeyPrefixesAreWorkspaceScoped:
    def test_prefixes_embed_the_workspace_slug(self, _clean_env, monkeypatch):
        monkeypatch.setenv("AW_WORKSPACE", "acme")
        assert _clean_env._bcast_prefix() == "aw:ws:acme:bcast:"
        assert _clean_env._leader_prefix() == "aw:ws:acme:leader:"
        assert _clean_env._cooldown_prefix() == "aw:ws:acme:cooldown:"
        assert _clean_env._poll_stream_prefix() == "aw:ws:acme:mdpoll:"
        assert _clean_env._poll_epoch_key() == "aw:ws:acme:mdpoll:epoch"

    def test_different_workspaces_never_share_a_prefix(self, _clean_env, monkeypatch):
        monkeypatch.setenv("AW_WORKSPACE", "acme")
        acme_prefix = _clean_env._bcast_prefix()
        monkeypatch.setenv("AW_WORKSPACE", "widgets-inc")
        widgets_prefix = _clean_env._bcast_prefix()
        assert acme_prefix != widgets_prefix


class TestGetRedisPool:
    def test_same_url_returns_the_same_cached_client(self, _clean_env):
        a = _clean_env.get_redis_pool("redis://127.0.0.1:6379/0")
        b = _clean_env.get_redis_pool("redis://127.0.0.1:6379/0")
        assert a is b

    def test_different_urls_return_different_clients(self, _clean_env):
        a = _clean_env.get_redis_pool("redis://127.0.0.1:6379/0")
        b = _clean_env.get_redis_pool("redis://127.0.0.1:6379/1")
        assert a is not b

    def test_defaults_to_the_workspace_redis_url(self, _clean_env, monkeypatch):
        monkeypatch.setenv("AW_WORKSPACE_REDIS_URL", "redis://127.0.0.1:6379/3")
        pool = _clean_env.get_redis_pool()
        assert pool is _clean_env.get_redis_pool("redis://127.0.0.1:6379/3")
