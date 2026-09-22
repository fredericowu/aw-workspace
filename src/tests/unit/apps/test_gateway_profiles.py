"""``contributes.mcp.profiles`` — an app declares the profile it references.

Until this existed a manifest could point an agent at ``/mcp/<name>``
(``mcp_servers: [{"name","server","profile"}]``) and nothing anywhere created
that profile. The gateway answers ``404 {"error":"No such config: <name>"}``
per request, the agent starts with ZERO tools, and no log on either side says
so — aw-app-marketing's ``marketing`` profile, measured live on 2026-09-21.

The validation half matters more than the rendering half. The gateway's
``normalize_config_spec`` is a NORMALIZER: it keeps the keys it knows and
drops the rest in silence. A ``tools_allowed`` typo therefore disappears, and
an absent ``tools_allow`` is what "unrestricted" means downstream — so the
profile quietly serves every tool of every upstream it lists. The gateway
cannot catch that by design; install time is the only place that can.

Run: aw-workspace-cli test src/tests/unit/apps/test_gateway_profiles.py
"""
from __future__ import annotations

import copy
import json
import os
from types import SimpleNamespace

import pytest

from src.apps import gateway_profiles
from src.apps.manifest import (GATEWAY_PROFILE_KEYS, ManifestError,
                               validate_manifest)


# The live crispal-full spec, as it exists in the gateway's gateway.json today
# — the one PR-3 will move into a manifest, so the shape this accepts is the
# shape that has to survive the move.
CRISPAL_FULL = {
    "upstreams": ["aw-crispal", "crispal-wordpress-production", "kb",
                  "agents-platform-runners"],
    "kb_index": "crispal",
    "tools_allow": ["aw-crispal__*", "crispal-wordpress-production__*", "kb__*",
                    "agents-platform-runners__list_gallery_images"],
}

BASE = {
    "manifest_version": 1,
    "id": "shop",
    "name": "Shop",
    "version": "1.0.0",
    "tier": "inprocess",
    "runtime": {"entrypoint": "shop_app.plugin:ShopPlugin"},
    "permissions": [],
    "contributes": {},
}


def _m(profiles):
    d = copy.deepcopy(BASE)
    d["contributes"]["mcp"] = {"profiles": profiles}
    return d


# ---- validation: the whitelist is the point -------------------------------

def test_a_profile_declaration_is_accepted_and_readable():
    m = validate_manifest(_m({"crispal-full": CRISPAL_FULL}))
    assert m.gateway_profiles == {"crispal-full": CRISPAL_FULL}


def test_an_unknown_key_inside_a_spec_is_rejected():
    """THE test for this feature.

    ``tools_allowed`` is one letter off ``tools_allow``. The gateway drops it
    without a word, the profile ends up with no allow-list at all, and
    ``_tool_allowed`` returns True for everything — the profile serves every
    tool of every upstream it names. Nobody sees an error anywhere.
    """
    bad = dict(CRISPAL_FULL)
    bad.pop("tools_allow")
    bad["tools_allowed"] = ["kb__*"]

    with pytest.raises(ManifestError, match="tools_allowed"):
        validate_manifest(_m({"crispal-full": bad}))


def test_the_allowed_keys_are_pinned():
    """The whitelist is copied from the gateway's own
    ``CONFIG_LIST_KEYS + CONFIG_SCALAR_KEYS``. Pinned literally here because a
    drift in EITHER direction is silent: a key this side accepts and that side
    drops is a policy that reads as enforced and does nothing."""
    assert GATEWAY_PROFILE_KEYS == {
        "upstreams", "tools_allow",
        "run_agents_allow", "run_workflows_allow",
        "run_agents_approval", "run_workflows_approval",
        "run_agents_always_allow", "run_workflows_always_allow",
        "kb_index", "presentation_namespace",
    }


def test_the_allowed_keys_match_the_installed_gateway_when_there_is_one():
    """Cross-check against the real thing WITHOUT importing it: the gateway is
    a container app whose package dir may not be here at all (and its module
    imports deps this process doesn't have), so the constants are read out of
    the source with ast and the check skips when it isn't installed. This
    never gates on the PUBLISHED gateway having shipped the scan yet."""
    import ast

    from src.apps.paths import DEFAULT_WORKSPACE_CONTAINER_DIR

    src = os.path.join(
        os.environ.get("AW_WORKSPACE_CONTAINER_DIR", DEFAULT_WORKSPACE_CONTAINER_DIR),
        "apps", "mcp-gateway", "back", "gateway", "config.py")
    if not os.path.isfile(src):
        pytest.skip("mcp-gateway app is not installed in this workspace")

    with open(src, encoding="utf-8") as f:
        tree = ast.parse(f.read())
    found: dict[str, set] = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if (isinstance(target, ast.Name)
                    and target.id in ("CONFIG_LIST_KEYS", "CONFIG_SCALAR_KEYS")):
                found[target.id] = set(ast.literal_eval(node.value))

    assert set(found) == {"CONFIG_LIST_KEYS", "CONFIG_SCALAR_KEYS"}, found
    assert GATEWAY_PROFILE_KEYS == found["CONFIG_LIST_KEYS"] | found["CONFIG_SCALAR_KEYS"]


@pytest.mark.parametrize("name", ["has space", "with/slash", "with.dot", "", "a b"])
def test_a_name_the_gateway_could_not_serve_is_rejected(name):
    """The name is a URL path segment (``/mcp/<name>``). One this side lets
    through and ``valid_config_name`` rejects is a profile that is written,
    scanned, dropped and 404s forever."""
    with pytest.raises(ManifestError, match="must be letters"):
        validate_manifest(_m({name: {"upstreams": ["kb"]}}))


@pytest.mark.parametrize("name", ["marketing", "crispal-full", "crispal_full", "A1"])
def test_the_names_the_gateway_accepts_are_accepted(name):
    assert validate_manifest(_m({name: {"upstreams": ["kb"]}})).gateway_profiles


def test_profiles_must_be_an_object():
    with pytest.raises(ManifestError, match="must be an object"):
        validate_manifest(_m([{"name": "marketing"}]))


def test_a_spec_must_be_an_object():
    with pytest.raises(ManifestError, match="must be an object"):
        validate_manifest(_m({"marketing": ["kb"]}))


@pytest.mark.parametrize("value", [{"kb": True}, [""], [3], 7, None])
def test_a_list_key_must_hold_non_empty_strings(value):
    with pytest.raises(ManifestError, match="upstreams must be a list"):
        validate_manifest(_m({"marketing": {"upstreams": value}}))


def test_a_list_key_may_be_a_bare_string():
    """``normalize_config_spec`` wraps a str into a list, so rejecting it here
    would refuse a manifest the gateway handles fine."""
    m = validate_manifest(_m({"marketing": {"upstreams": "kb"}}))
    assert m.gateway_profiles["marketing"]["upstreams"] == "kb"


@pytest.mark.parametrize("value", [["crispal"], "", 1, None])
def test_a_scalar_key_must_hold_a_non_empty_string(value):
    with pytest.raises(ManifestError, match="kb_index must be a"):
        validate_manifest(_m({"marketing": {"kb_index": value}}))


def test_an_app_declaring_no_profiles_is_untouched():
    """Every app that ships a contributes.mcp block for other reasons —
    ``provides``, ``reload_on_save`` — must keep validating unchanged."""
    d = copy.deepcopy(BASE)
    d["contributes"]["mcp"] = {"reload_on_save": True, "provides": ["a tool"]}
    assert validate_manifest(d).gateway_profiles == {}


def test_a_boolean_contributes_mcp_still_validates():
    """``contributes.mcp: true`` is a shape in the wild (src/apps/base.py)."""
    d = copy.deepcopy(BASE)
    d["contributes"]["mcp"] = True
    assert validate_manifest(d).gateway_profiles == {}


def test_no_permission_is_required():
    """A profile only NARROWS what the gateway's root /mcp already serves to
    anyone holding the token, so there is no escalation to authorize. If this
    ever starts failing on a permission, that decision was made by accident."""
    m = validate_manifest(_m({"marketing": CRISPAL_FULL}))
    assert m.permissions == []


# ---- rendering ------------------------------------------------------------

def _fake_manifest(profiles, app_id="shop"):
    return SimpleNamespace(id=app_id, gateway_profiles=profiles)


def _read(pkg):
    with open(gateway_profiles.output_path(pkg), encoding="utf-8") as f:
        return json.load(f)


def test_render_writes_the_profiles_verbatim(tmp_path):
    pkg = str(tmp_path)

    assert gateway_profiles.render(pkg, _fake_manifest({"crispal-full": CRISPAL_FULL}))

    assert _read(pkg) == {"profiles": {"crispal-full": CRISPAL_FULL}}


def test_render_does_not_expand_placeholders(tmp_path):
    """Deliberately NOT mcp_template: a profile carries no credential, so the
    literal string is the value. Expanding here would invent a way for one to
    hold a secret."""
    pkg = str(tmp_path)
    spec = {"upstreams": ["kb"], "kb_index": "${config.tenant}"}

    gateway_profiles.render(pkg, _fake_manifest({"p": spec}))

    assert _read(pkg)["profiles"]["p"]["kb_index"] == "${config.tenant}"


def test_an_app_with_no_profiles_writes_nothing(tmp_path):
    pkg = str(tmp_path)

    assert gateway_profiles.render(pkg, _fake_manifest({})) is False

    assert not os.path.exists(gateway_profiles.output_path(pkg))


def test_a_version_that_drops_its_profiles_removes_the_file(tmp_path):
    """An update replaces the package dir, but a generated file left in it
    would keep being scanned — the app would go on serving a scope its current
    manifest no longer declares."""
    pkg = str(tmp_path)
    gateway_profiles.render(pkg, _fake_manifest({"old": {"upstreams": ["kb"]}}))
    assert os.path.exists(gateway_profiles.output_path(pkg))

    assert gateway_profiles.render(pkg, _fake_manifest({})) is False

    assert not os.path.exists(gateway_profiles.output_path(pkg))


def test_render_is_idempotent(tmp_path):
    """It runs on every activation; nothing may drift pass over pass."""
    pkg = str(tmp_path)
    m = _fake_manifest({"crispal-full": CRISPAL_FULL})

    gateway_profiles.render(pkg, m)
    first = _read(pkg)
    gateway_profiles.render(pkg, m)

    assert _read(pkg) == first


def test_the_rendered_file_is_readable_by_the_gateway(tmp_path):
    """0600 (what mcp_template uses for a file holding a token) would be
    unreadable from the gateway's own container, which scans this as another
    user. A profile holds no credential."""
    pkg = str(tmp_path)

    gateway_profiles.render(pkg, _fake_manifest({"p": {"upstreams": ["kb"]}}))

    mode = oct(os.stat(gateway_profiles.output_path(pkg)).st_mode)[-3:]
    assert mode == "644"


def test_a_failed_write_does_not_leave_a_partial_file(tmp_path):
    pkg = str(tmp_path)

    class _Unserializable:
        pass

    with pytest.raises(TypeError):
        gateway_profiles.render(pkg, _fake_manifest({"p": _Unserializable()}))

    assert not os.path.exists(gateway_profiles.output_path(pkg))
    assert [p for p in os.listdir(pkg) if p.startswith(".gateway-profiles-")] == []


# ---- the reload trigger ---------------------------------------------------
#
# Risk 7 of the design: AppRuntime.start_mcp_gateway_rescan POSTs /reload
# every few minutes, so a profile converges even with this trigger broken.
# Excellent in production, useless as a test — so the predicate is called
# directly rather than waiting for a watchdog that would hide the bug.

def test_an_app_that_only_declares_profiles_triggers_a_gateway_reload():
    """No mcp.json, no mcp.template.json, nothing on disk yet (a FIRST install
    asks this before the app has activated). Without this the file lands in
    the package dir and is never applied."""
    from src.apps.reconciler import Reconciler

    manifest = SimpleNamespace(
        contributes_mcp=False, reload_mcp_gateway_on_save=False,
        gateway_profiles={"marketing": {"upstreams": ["kb"]}})

    assert Reconciler._app_touches_mcp(None, manifest, None) is True


def test_an_app_with_neither_profiles_nor_mcp_json_does_not(tmp_path):
    from src.apps.reconciler import Reconciler

    manifest = SimpleNamespace(
        contributes_mcp=False, reload_mcp_gateway_on_save=False,
        gateway_profiles={})

    assert Reconciler._app_touches_mcp(None, manifest, str(tmp_path)) is False


def test_a_rendered_file_on_disk_triggers_one_too(tmp_path):
    """The uninstall direction: the manifest may be unreadable by then, and
    removing the package dir removes profiles the gateway is still serving."""
    from src.apps.reconciler import Reconciler

    gateway_profiles.render(str(tmp_path), _fake_manifest({"p": {"upstreams": ["kb"]}}))

    assert Reconciler._app_touches_mcp(None, None, str(tmp_path)) is True
