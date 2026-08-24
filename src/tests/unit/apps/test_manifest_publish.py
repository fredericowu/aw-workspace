import pytest

from src.apps.manifest import ManifestError, validate_manifest


def _manifest(publish, *, tier="container", permissions=None):
    return {
        "manifest_version": 1,
        "id": "phone",
        "name": "Phone",
        "version": "1.0.0",
        "tier": tier,
        "runtime": ({"image": "phone:test", "port": 8000, "publish": publish}
                    if tier == "container" else
                    {"entrypoint": "phone.plugin:Plugin", "publish": publish}),
        "permissions": permissions if permissions is not None else ["containers:manage"],
        "contributes": {},
    }


def test_publish_accepts_udp_and_equal_ranges():
    manifest = validate_manifest(_manifest([
        {"container": 5060, "host": 5060, "protocol": "udp"},
        {"container": "10000-10100", "host": "10000-10100", "protocol": "udp"},
    ]))
    assert len(manifest.runtime["publish"]) == 2


@pytest.mark.parametrize("publish", [
    [{"container": "10000-10002", "host": "20000-20001", "protocol": "udp"}],
    [{"container": 5060, "protocol": "sctp"}],
    [{"container": 0}],
])
def test_publish_rejects_invalid_bindings(publish):
    with pytest.raises(ManifestError):
        validate_manifest(_manifest(publish))


def test_publish_requires_container_permission_and_tier():
    with pytest.raises(ManifestError):
        validate_manifest(_manifest([{"container": 5060}], permissions=[]))
    with pytest.raises(ManifestError):
        validate_manifest(_manifest([{"container": 5060}], tier="inprocess",
                                    permissions=["containers:manage"]))
