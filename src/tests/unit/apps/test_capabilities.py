"""Unit tests for the capability catalog + trust tier (F2)."""
from __future__ import annotations

import pytest

from src.apps.capabilities import (
    filter_grants,
    is_high_risk,
    is_valid_capability,
)


@pytest.mark.parametrize("cap", [
    "routes:register", "db:own-tables", "commands:install", "service:manage",
    "net:outbound", "fs:workspace-data", "secrets:own", "containers:manage",
    "ui:code", "config:extend:kanban", "ui:slots:core.toolbar",
])
def test_known_capabilities_are_valid(cap):
    assert is_valid_capability(cap)


@pytest.mark.parametrize("cap", [
    "do:anything", "routes", "config:extend:", "ui:slots:", "", "ROUTES:REGISTER",
])
def test_unknown_or_malformed_capabilities_are_invalid(cap):
    assert not is_valid_capability(cap)


@pytest.mark.parametrize("cap", ["ui:code", "containers:manage", "config:extend:kanban"])
def test_high_risk_caps(cap):
    assert is_high_risk(cap)


@pytest.mark.parametrize("cap", ["routes:register", "secrets:own", "ui:slots:core.toolbar"])
def test_low_risk_caps(cap):
    assert not is_high_risk(cap)


def test_unsigned_app_loses_high_risk_caps():
    """Re-enabled 2026-08-04 — see filter_grants' docstring. ``signed`` is now
    computed automatically (marketplace-catalog membership, never
    client-supplied), so refusing high-risk caps for "unsigned" is a real
    boundary again, not a uniform block on every app."""
    granted, refused = filter_grants(
        ["routes:register", "ui:code", "config:extend:kanban"], signed=False)
    assert granted == ["routes:register"]
    assert refused == ["ui:code", "config:extend:kanban"]


def test_signed_app_keeps_high_risk_caps():
    granted, refused = filter_grants(["routes:register", "ui:code"], signed=True)
    assert granted == ["routes:register", "ui:code"]
    assert refused == []


class TestWorkspaceReadCapability:
    """`$AW_WORKSPACE_REPOS` mounts the user's ENTIRE checkout tree — every
    repo, private ones included, plus whatever a stray .env in one of them
    holds. It was gated by nothing until 2026-08-16, while $AW_APP_DATA,
    $AW_KB_DIR and $AW_WORKSPACE_FOLDERS all demanded a capability for far
    less. An app could mount the lot with a manifest claiming it touched no
    filesystem at all."""

    def test_capability_exists(self):
        from src.apps.capabilities import CATALOG, is_valid_capability
        assert is_valid_capability("fs:workspace-read")
        assert CATALOG["fs:workspace-read"]["risk"] == "low"

    def test_aw_backend_mirror_is_byte_identical(self):
        """The cloud registry grants from aw-backend's copy. If it drifts, an
        app requesting the capability is refused the grant with no clue why —
        and the refusal surfaces as a missing feature, not as an error."""
        import os
        import re
        import src.apps.capabilities as caps
        core = os.path.abspath(caps.__file__)
        root = core.split(os.sep + "src" + os.sep)[0]
        mirror = os.path.join(root, "repos", "aw-backend",
                              "src", "api", "app_capabilities.py")
        if not os.path.isfile(mirror):
            import pytest
            pytest.skip("aw-backend not checked out here")
        def catalog(path):
            src = open(path).read()
            m = re.search(r"^CATALOG.*?^\}", src, re.S | re.M)
            return m.group(0)
        assert catalog(core) == catalog(mirror)


class TestRequiresWorkspace:
    """An app had no way to state that it needs a core feature: `dependencies`
    only expresses other apps. So an app built against last week's capability
    installed cleanly on last month's workspace and failed deep in a request,
    reading as an app bug rather than a version mismatch."""

    def test_older_workspace_is_refused(self, monkeypatch):
        from src.apps.manifest import ManifestError, _validate_requires_workspace
        monkeypatch.setenv("AW_WORKSPACE_VERSION", "v0.1.63")
        import pytest
        with pytest.raises(ManifestError, match="needs aw-workspace"):
            _validate_requires_workspace({"requires_workspace": "0.2.0"})

    def test_equal_or_newer_passes(self, monkeypatch):
        from src.apps.manifest import _validate_requires_workspace
        monkeypatch.setenv("AW_WORKSPACE_VERSION", "v0.1.63")
        _validate_requires_workspace({"requires_workspace": "0.1.63"})
        _validate_requires_workspace({"requires_workspace": "0.1.60"})

    def test_unstamped_workspace_does_not_enforce(self, monkeypatch):
        """A dev checkout doesn't stamp a version. Refusing every install
        there would be worse than the problem being solved."""
        from src.apps.manifest import _validate_requires_workspace
        monkeypatch.delenv("AW_WORKSPACE_VERSION", raising=False)
        _validate_requires_workspace({"requires_workspace": "99.0.0"})

    def test_absent_field_is_fine(self):
        from src.apps.manifest import _validate_requires_workspace
        _validate_requires_workspace({})

    def test_garbage_is_refused_at_validation_time(self):
        from src.apps.manifest import ManifestError, _validate_requires_workspace
        import pytest
        with pytest.raises(ManifestError):
            _validate_requires_workspace({"requires_workspace": "not-a-version"})

    def test_version_compare_is_numeric_not_lexical(self):
        """'0.1.7' > '0.1.63' as strings. That mistake silently lets an app
        install on a workspace too old for it."""
        from src.apps.manifest import _parse_version
        assert _parse_version("0.1.7") < _parse_version("0.1.63")


class TestManifestSchemaTracksTheCatalog:
    """A THIRD place the capability list lives: the app-manifest JSON schema in
    aw-marketplace, whose `permissions` pattern is a hardcoded regex.

    It went stale twice over without anyone noticing — missing `repos:contribute`
    (shipped long ago) and then `fs:workspace-read`. The symptom is an app's
    release failing with

        'fs:workspace-read' does not match '^(routes:register|db:own-tables|...)$'

    which reads as a bad manifest rather than a stale schema, in a repo nobody
    was looking at. This test is the tripwire.
    """

    def _schema_pattern(self):
        import json
        import os
        import pytest
        import src.apps.capabilities as caps
        root = os.path.abspath(caps.__file__).split(os.sep + "src" + os.sep)[0]
        path = os.path.join(root, "repos", "aw-marketplace",
                            "schemas", "aw-app.schema.json")
        if not os.path.isfile(path):
            pytest.skip("aw-marketplace not checked out here")
        with open(path) as f:
            schema = json.load(f)
        return schema["properties"]["permissions"]["items"]["pattern"]

    def test_every_catalog_capability_is_accepted_by_the_schema(self):
        import re
        from src.apps.capabilities import CATALOG
        pattern = re.compile(self._schema_pattern())
        missing = [c for c in CATALOG if not pattern.fullmatch(c)]
        assert not missing, (
            f"the manifest schema would reject {missing} — regenerate it from "
            f"this catalog, or an app declaring one cannot be released"
        )

    def test_parameterised_capabilities_are_accepted(self):
        import re
        pattern = re.compile(self._schema_pattern())
        for cap in ("ui:slots:core.nav.workspace", "config:extend:some-app"):
            assert pattern.fullmatch(cap), cap

    def test_schema_rejects_something_that_is_not_a_capability(self):
        """A pattern so loose it accepts anything is not a check."""
        import re
        pattern = re.compile(self._schema_pattern())
        assert not pattern.fullmatch("fs:read-everything")
        assert not pattern.fullmatch("")
