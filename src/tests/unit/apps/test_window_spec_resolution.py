"""``${config.x}`` expansion in a declarative window's ``spec_data``
(``AppRuntime._resolve_window`` / ``_expand_window_node``).

Added alongside aw-app-signoz-connector's own SigNoz-UI window (card
3f25bf3b...): an `iframe` widget's `src` needed a way to carry a managed
config value with no backend route of its own to resolve it. The one rule
that matters more than the feature itself — named risk #1 by the design —
is that `spec_data` is served straight to the SPA with no further auth
check, so an `x-secret: true` config field (e.g. `query_api_key`) must
NEVER be expanded here. Every test below exists to keep that true.

Run: aw-workspace-cli test src/tests/unit/apps/test_window_spec_resolution.py
"""
from __future__ import annotations

import json

import pytest

from src.apps.manifest import validate_manifest
from src.apps.runtime import AppRuntime, _expand_window_node, _window_secret_config_keys


BASE_MANIFEST = {
    "manifest_version": 1,
    "id": "signoz-connector",
    "name": "SigNoz Connector",
    "version": "0.3.0",
    "tier": "container",
    "runtime": {"image": "otel/opentelemetry-collector-contrib:0.160.0", "port": 4318},
    "permissions": ["containers:manage"],
    "contributes": {
        "windows": [{"id": "signoz-connector.ui", "title": "SigNoz",
                     "body": {"type": "declarative", "spec": "windows/signoz_ui.json"}}],
    },
    "config_schema": {
        "type": "object",
        "properties": {
            "web_ui_url": {"type": "string", "default": ""},
            "query_api_key": {"type": "string", "default": "", "x-secret": True},
        },
    },
}


def _manifest(**config_schema_overrides):
    import copy
    raw = copy.deepcopy(BASE_MANIFEST)
    if config_schema_overrides:
        raw["config_schema"]["properties"].update(config_schema_overrides)
    return validate_manifest(raw)


class FakeApp:
    def __init__(self, manifest, package_dir, config):
        self.manifest = manifest
        self.package_dir = package_dir
        self.config = config


def _write_spec(tmp_path, spec: dict) -> str:
    pkg = tmp_path / "pkg"
    windows = pkg / "windows"
    windows.mkdir(parents=True)
    (windows / "signoz_ui.json").write_text(json.dumps(spec))
    return str(pkg)


class TestSecretKeyDiscovery:
    def test_x_secret_fields_are_collected(self):
        manifest = _manifest()
        assert _window_secret_config_keys(manifest) == {"query_api_key"}

    def test_a_schema_with_no_secret_fields_yields_an_empty_set(self):
        raw = {**BASE_MANIFEST, "config_schema": {
            "type": "object", "properties": {"web_ui_url": {"type": "string"}}}}
        manifest = validate_manifest(raw)
        assert _window_secret_config_keys(manifest) == set()


class TestExpandWindowNode:
    def test_a_plain_config_value_is_substituted(self):
        node = {"src": "${config.web_ui_url}"}
        out = _expand_window_node(node, {"web_ui_url": "https://signoz.example/ui"},
                                   "signoz-connector", set())
        assert out["src"] == "https://signoz.example/ui"

    def test_a_secret_key_is_left_unresolved_not_substituted(self):
        node = {"headers": {"SIGNOZ-API-KEY": "${config.query_api_key}"}}
        out = _expand_window_node(node, {"query_api_key": "super-secret-value"},
                                   "signoz-connector", {"query_api_key"})
        assert out["headers"]["SIGNOZ-API-KEY"] == "${config.query_api_key}"
        assert "super-secret-value" not in json.dumps(out)

    def test_a_secret_embedded_inside_a_larger_string_is_also_refused(self):
        node = "https://example/callback?key=${config.query_api_key}"
        out = _expand_window_node(node, {"query_api_key": "super-secret-value"},
                                   "signoz-connector", {"query_api_key"})
        assert "super-secret-value" not in out
        assert "${config.query_api_key}" in out

    def test_an_unresolved_non_secret_placeholder_is_left_as_is(self):
        node = {"src": "${config.web_ui_url}"}
        out = _expand_window_node(node, {}, "signoz-connector", set())
        assert out["src"] == "${config.web_ui_url}"

    def test_recurses_through_lists_and_nested_dicts(self):
        node = {"regions": [{"widgets": [{"src": "${config.web_ui_url}"}]}]}
        out = _expand_window_node(node, {"web_ui_url": "https://x"}, "app", set())
        assert out["regions"][0]["widgets"][0]["src"] == "https://x"

    def test_non_string_leaves_pass_through_unchanged(self):
        node = {"enabled": True, "count": 3, "nothing": None}
        out = _expand_window_node(node, {}, "app", set())
        assert out == node


class TestResolveWindowIntegration:
    """``_resolve_window`` doesn't touch ``self`` — calling it unbound (first
    arg ignored) avoids standing up a real ``AppRuntime``/FastAPI host just
    to exercise the spec-load + expansion it wires together."""

    def test_web_ui_url_is_expanded_in_the_loaded_spec(self, tmp_path):
        manifest = _manifest()
        pkg = _write_spec(tmp_path, {"regions": [{"widgets": [
            {"type": "iframe", "src": "${config.web_ui_url}"}]}]})
        app = FakeApp(manifest, pkg, {"web_ui_url": "https://signoz-mcp.aw.tekflox.com"})
        entry = manifest.windows[0]

        resolved = AppRuntime._resolve_window(None, app, entry)

        widget = resolved["body"]["spec_data"]["regions"][0]["widgets"][0]
        assert widget["src"] == "https://signoz-mcp.aw.tekflox.com"

    def test_query_api_key_never_reaches_the_resolved_spec(self, tmp_path):
        manifest = _manifest()
        pkg = _write_spec(tmp_path, {"regions": [{"widgets": [
            {"type": "iframe", "src": "${config.query_api_key}"}]}]})
        app = FakeApp(manifest, pkg, {"query_api_key": "do-not-leak-me"})
        entry = manifest.windows[0]

        resolved = AppRuntime._resolve_window(None, app, entry)

        rendered = json.dumps(resolved)
        assert "do-not-leak-me" not in rendered
        assert "${config.query_api_key}" in rendered
