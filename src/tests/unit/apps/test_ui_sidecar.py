"""``runtime.ui_sidecar`` — which container the app's own route proxies to."""
from __future__ import annotations

import pytest

from src.apps.containers import ContainerSupervisor
from src.apps.manifest import ManifestError, validate_manifest

SIDECARS = [
    {"name": "db", "image": "mysql:8.0"},
    {"name": "wordpress", "image": "wordpress:latest", "port": 10002},
]


def _manifest(ui_sidecar=None, sidecars=None):
    runtime = {
        "image": "ghcr.io/x/y:latest",
        "port": 9410,
        "sidecars": SIDECARS if sidecars is None else sidecars,
    }
    if ui_sidecar is not None:
        runtime["ui_sidecar"] = ui_sidecar
    return {
        "manifest_version": 1, "id": "crispal", "name": "Crispal", "version": "1.0.0",
        "tier": "container", "runtime": runtime,
        "permissions": ["containers:manage"],
    }


def test_not_declaring_one_leaves_the_app_proxying_itself():
    assert validate_manifest(_manifest()).ui_sidecar == ""


def test_a_declared_ui_sidecar_is_parsed():
    assert validate_manifest(_manifest("wordpress")).ui_sidecar == "wordpress"


def test_naming_a_sidecar_that_does_not_exist_is_rejected():
    with pytest.raises(ManifestError, match="names no declared sidecar"):
        validate_manifest(_manifest("nope"))


def test_a_portless_ui_sidecar_is_rejected():
    """Otherwise the proxy is mounted at port 0 — a 502 per request, from a
    manifest that reads as correct."""
    with pytest.raises(ManifestError, match="must declare a 'port'"):
        validate_manifest(_manifest("db"))


@pytest.mark.parametrize("bad", ["", "   ", 3, True])
def test_a_malformed_ui_sidecar_is_rejected(bad):
    with pytest.raises(ManifestError):
        validate_manifest(_manifest(bad))


def test_the_proxy_target_is_the_sidecars_address_not_the_apps():
    """The whole point: the route resolves to the UI container, and the app's
    own container keeps its port for everything that dials it directly."""
    sup = ContainerSupervisor(socket="/dev/null", network="awnet", client=object())
    sup.register("crispal", "ghcr.io/x/y", 9410)
    key = sup.register_sidecar("crispal", "wordpress",
                               image="wordpress:latest", port=10002)
    assert sup.base_url("crispal") == "http://aw-app-crispal:9410"
    assert sup.base_url(key) == "http://aw-app-crispal-wordpress:10002"
