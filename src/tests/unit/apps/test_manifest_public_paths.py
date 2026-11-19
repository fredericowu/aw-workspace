"""``contributes.routes[].public_paths`` validation (routes:public)."""
from __future__ import annotations

import pytest

from src.apps.manifest import (
    PUBLIC_PATHS_APPROVED_KEY,
    ManifestError,
    validate_manifest,
)


def _m(public_paths, permissions=("routes:register", "routes:public")):
    return {
        "manifest_version": 1,
        "id": "shopify",
        "name": "Shopify",
        "version": "1.0.0",
        "tier": "inprocess",
        "runtime": {"entrypoint": "x.plugin:P"},
        "permissions": list(permissions),
        "contributes": {"routes": [
            {"prefix": "/api/apps/shopify", "public_paths": public_paths},
        ]},
    }


def test_exact_path_under_the_app_prefix_is_accepted():
    m = validate_manifest(_m(["/api/apps/shopify/webhooks"]))
    assert m.public_paths == ["/api/apps/shopify/webhooks"]


def test_public_paths_require_the_routes_public_permission():
    with pytest.raises(ManifestError, match="routes:public"):
        validate_manifest(_m(["/api/apps/shopify/webhooks"],
                             permissions=["routes:register"]))


@pytest.mark.parametrize("path", [
    "/api/apps/shopify",             # the mount root = the whole app
    "/api/apps/shopify/",            # same, with a slash
    "/api/apps/other/webhooks",      # another app's mount
    "/api/apps/shopifyx/webhooks",   # prefix-confusable sibling id
    "/webhooks",                     # mount-relative, not a full path
    "/api/apps/shopify/*",           # wildcard
    "/api/apps/shopify/webhooks/",   # trailing slash
    "/api/apps/shopify//webhooks",   # empty segment
    "/api/apps/shopify/../mcp",      # traversal
    "/api/apps/shopify/./webhooks",
    "/api/apps/shopify/webhooks?x=1",
    "/api/apps/shopify/web hooks",
    "",
    None,
    42,
])
def test_anything_wider_or_odd_is_refused(path):
    with pytest.raises(ManifestError, match="public_paths"):
        validate_manifest(_m([path]))


def test_owner_approval_flag_is_offered_only_when_paths_are_declared():
    with_paths = validate_manifest(_m(["/api/apps/shopify/webhooks"]))
    props = with_paths.effective_config_schema["properties"]
    assert props[PUBLIC_PATHS_APPROVED_KEY]["default"] is False
    assert "/api/apps/shopify/webhooks" in props[PUBLIC_PATHS_APPROVED_KEY]["description"]
    assert with_paths.config_with_defaults({})[PUBLIC_PATHS_APPROVED_KEY] is False

    without = validate_manifest(_m([]))
    assert PUBLIC_PATHS_APPROVED_KEY not in (
        without.effective_config_schema.get("properties") or {})


def test_an_app_schema_cannot_preapprove_its_own_public_paths():
    data = _m(["/api/apps/shopify/webhooks"])
    data["config_schema"] = {"type": "object", "properties": {
        PUBLIC_PATHS_APPROVED_KEY: {"type": "boolean", "default": True}}}
    m = validate_manifest(data)
    assert m.effective_config_schema["properties"][PUBLIC_PATHS_APPROVED_KEY]["default"] is False
    assert m.config_with_defaults({})[PUBLIC_PATHS_APPROVED_KEY] is False


@pytest.mark.parametrize("value", [42, "/api/apps/shopify/webhooks",
                                   {"/api/apps/shopify/webhooks": 1}])
def test_public_paths_must_be_a_list(value):
    with pytest.raises(ManifestError, match="public_paths"):
        validate_manifest(_m(value))
