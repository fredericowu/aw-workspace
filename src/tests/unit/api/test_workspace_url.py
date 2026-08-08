"""Unit tests for src.api.workspace_url — dynamic composition of this
workspace's external API base URL from the slug + a layered base domain."""
import pytest

import src.api.workspace_url as wu


@pytest.fixture
def clean(monkeypatch):
    for var in ("AW_WORKSPACE", "AW_WORKSPACE_BASE_DOMAIN", "AW_BACKEND_URL",
                wu.ENV_VAR_NAME):
        monkeypatch.delenv(var, raising=False)
    return monkeypatch


class TestBaseDomain:
    def test_static_default_when_nothing_configured(self, clean):
        assert wu.base_domain() == "workspace.aw.tekflox.com"

    def test_explicit_override_wins(self, clean):
        clean.setenv("AW_WORKSPACE_BASE_DOMAIN", "workspace.example.dev")
        clean.setenv("AW_BACKEND_URL", "https://api.aw.tekflox.com")  # ignored
        assert wu.base_domain() == "workspace.example.dev"

    def test_derived_from_backend_url_apex(self, clean):
        clean.setenv("AW_BACKEND_URL", "https://api.aw.tekflox.com")
        assert wu.base_domain() == "workspace.aw.tekflox.com"

    def test_derived_tracks_a_different_deploy_domain(self, clean):
        clean.setenv("AW_BACKEND_URL", "https://api.staging.tekflox.com")
        assert wu.base_domain() == "workspace.staging.tekflox.com"

    def test_backend_without_api_prefix_used_as_apex(self, clean):
        clean.setenv("AW_BACKEND_URL", "https://aw.tekflox.com")
        assert wu.base_domain() == "workspace.aw.tekflox.com"

    def test_malformed_backend_url_falls_back_to_static(self, clean):
        clean.setenv("AW_BACKEND_URL", "not a url")
        assert wu.base_domain() == "workspace.aw.tekflox.com"


class TestWorkspaceApiBaseUrl:
    def test_composes_full_host_from_slug(self, clean):
        clean.setenv("AW_WORKSPACE", "acme")
        assert wu.workspace_api_base_url() == "https://api.acme.workspace.aw.tekflox.com"

    def test_slug_is_dynamic(self, clean):
        clean.setenv("AW_WORKSPACE", "widgets-inc")
        clean.setenv("AW_WORKSPACE_BASE_DOMAIN", "workspace.example.dev")
        assert wu.workspace_api_base_url() == "https://api.widgets-inc.workspace.example.dev"

    def test_none_when_slug_unset(self, clean):
        assert wu.workspace_api_base_url() is None


class TestPublish:
    def test_publishes_to_env_and_dotenv(self, clean, monkeypatch):
        writes = {}
        monkeypatch.setattr(wu, "upsert_workspace_env",
                            lambda name, value: writes.__setitem__(name, value))
        clean.setenv("AW_WORKSPACE", "acme")

        url = wu.publish_workspace_api_url()

        assert url == "https://api.acme.workspace.aw.tekflox.com"
        assert writes == {wu.ENV_VAR_NAME: url}
        import os
        assert os.environ[wu.ENV_VAR_NAME] == url

    def test_publish_noop_without_slug(self, clean, monkeypatch):
        called = []
        monkeypatch.setattr(wu, "upsert_workspace_env",
                            lambda name, value: called.append(name))
        assert wu.publish_workspace_api_url() is None
        assert called == []
