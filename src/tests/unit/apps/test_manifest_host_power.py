"""``runtime.host_power`` at manifest-validation time.

Validation covers the two legs that are true wherever the manifest is read
(shape, and the matching capability being declared). The third — does THIS
host offer the grant — is checked at load instead, because a manifest is
validated when it is authored and released too, and failing on a laptop with
no /dev/kvm would make the app unreleasable rather than uninstallable-here.
"""
import pytest

from src.apps.manifest import ManifestError, _validate_host_power, validate_manifest


def _tier2(**runtime):
    """A minimal valid Tier-2 manifest, plus whatever runtime keys are given."""
    return {
        "manifest_version": 1,
        "id": "aw-app-windows",
        "name": "Windows",
        "version": "0.1.0",
        "tier": "container",
        "runtime": {"image": "dockurr/windows", "port": 8006, **runtime},
        "permissions": ["containers:manage"],
    }


class TestShape:
    def test_absent_is_fine(self):
        _validate_host_power({}, "container", [])

    def test_empty_list_is_fine(self):
        _validate_host_power({"host_power": []}, "container", [])

    def test_must_be_a_list_of_strings(self):
        with pytest.raises(ManifestError, match="must be a list"):
            _validate_host_power({"host_power": "kvm"}, "container", [])
        with pytest.raises(ManifestError, match="must be a list"):
            _validate_host_power({"host_power": [1]}, "container", [])

    def test_unknown_grant_is_refused_with_the_known_set(self):
        with pytest.raises(ManifestError, match="unknown host power grant"):
            _validate_host_power({"host_power": ["gpu"]}, "container", [])


class TestCapabilityLeg:
    def test_grant_without_its_capability_is_refused(self):
        with pytest.raises(ManifestError, match="host:device-kvm"):
            _validate_host_power({"host_power": ["kvm"]}, "container",
                                 ["containers:manage"])

    def test_grant_with_its_capability_passes(self):
        _validate_host_power({"host_power": ["kvm"]}, "container",
                             ["containers:manage", "host:device-kvm"])

    def test_all_requires_every_granular_capability(self):
        from src.apps.hostpower import GRANULAR, required_capabilities
        with pytest.raises(ManifestError):
            _validate_host_power({"host_power": ["all"]}, "container",
                                 ["host:device-kvm"])
        _validate_host_power({"host_power": ["all"]}, "container",
                             list(required_capabilities(GRANULAR)))

    def test_whole_manifest_accepts_a_correct_declaration(self):
        m = validate_manifest(_tier2(host_power=["kvm", "tun"]) | {
            "permissions": ["containers:manage", "host:device-kvm", "host:device-tun"],
        })
        assert m.host_power == ["kvm", "tun"]

    def test_whole_manifest_rejects_an_undeclared_capability(self):
        with pytest.raises(ManifestError, match="host:device-tun"):
            validate_manifest(_tier2(host_power=["kvm", "tun"]) | {
                "permissions": ["containers:manage", "host:device-kvm"],
            })


class TestTierLeg:
    def test_tier1_cannot_be_elevated(self):
        """A Tier-1 app runs inside the workspace process and already has
        exactly the workspace's own access. Accepting the key there would be a
        manifest entry that reads as a privilege and changes nothing."""
        with pytest.raises(ManifestError, match="only applies to tier=container"):
            _validate_host_power({"host_power": ["kvm"]}, "inprocess",
                                 ["host:device-kvm"])


class TestSidecarsAreRefusedNotIgnored:
    def test_sidecar_host_power_raises(self):
        """Not wired for sidecars yet. Tolerating the key would start a
        companion container without the device it asked for, with a manifest
        that reads as correct — the exact trap this feature exists to close."""
        with pytest.raises(ManifestError, match="sidecars.*not supported"):
            _validate_host_power(
                {"sidecars": [{"name": "qemu", "image": "x", "host_power": ["kvm"]}]},
                "container", [])


class TestManifestProperty:
    def test_property_expands_all(self):
        from src.apps.hostpower import GRANULAR, required_capabilities
        m = validate_manifest(_tier2(host_power=["all"]) | {
            "permissions": ["containers:manage", *required_capabilities(GRANULAR)],
        })
        assert m.host_power == list(GRANULAR)

    def test_property_is_empty_when_undeclared(self):
        assert validate_manifest(_tier2()).host_power == []
