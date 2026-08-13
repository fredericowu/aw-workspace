"""``runtime.sidecars`` — companion containers of a Tier-2 app."""
from __future__ import annotations

import pytest

from src.apps.containers import ContainerSupervisor
from src.apps.manifest import ManifestError, validate_manifest


def _manifest(sidecars, permissions=("containers:manage",)):
    return {
        "manifest_version": 1, "id": "crispal", "name": "Crispal", "version": "1.0.0",
        "tier": "container",
        "runtime": {"image": "ghcr.io/x/y:latest", "port": 9410, "sidecars": sidecars},
        "permissions": list(permissions),
    }


# ---- manifest validation ----------------------------------------------------

def test_sidecars_are_parsed_off_the_manifest():
    m = validate_manifest(_manifest([
        {"name": "db", "image": "mysql:8.0"},
        {"name": "wordpress", "image": "wordpress:latest", "port": 10002},
    ]))
    assert [(s["name"], s.get("port")) for s in m.sidecars] == [
        ("db", None), ("wordpress", 10002)]


def test_sidecars_require_containers_manage():
    with pytest.raises(ManifestError, match="containers:manage"):
        validate_manifest(_manifest([{"name": "db", "image": "mysql:8.0"}], permissions=[]))


@pytest.mark.parametrize("entry", [
    {"image": "mysql:8.0"},                        # no name
    {"name": "db"},                                # no image
    {"name": "Db", "image": "mysql:8.0"},          # name is a DNS label
    {"name": "a/b", "image": "mysql:8.0"},
    {"name": "db", "image": "mysql:8.0", "port": 0},
    {"name": "db", "image": "mysql:8.0", "port": "3306"},
])
def test_a_malformed_sidecar_is_rejected(entry):
    with pytest.raises(ManifestError):
        validate_manifest(_manifest([entry]))


def test_duplicate_sidecar_names_are_rejected():
    with pytest.raises(ManifestError, match="duplicate"):
        validate_manifest(_manifest([
            {"name": "db", "image": "mysql:8.0"},
            {"name": "db", "image": "postgres:16"},
        ]))


def test_no_sidecars_declared_is_an_empty_list():
    m = validate_manifest({
        "manifest_version": 1, "id": "demo", "name": "Demo", "version": "1.0.0",
        "tier": "inprocess", "runtime": {"entrypoint": "m:C"}, "permissions": [],
    })
    assert m.sidecars == []


# ---- supervisor registry ----------------------------------------------------

class _FakeClient:
    """Minimal docker-client stand-in; records what would have been run."""

    def __init__(self):
        self.started: list[dict] = []
        self.removed: list[str] = []
        self.containers = self
        self.images = self

    def pull(self, image, **kw):
        return None

    def get(self, name):
        from docker.errors import NotFound
        raise NotFound(name)

    def run(self, image, **kwargs):
        self.started.append({"image": image, **kwargs})
        return type("C", (), {"id": f"id-{kwargs['name']}"})()


def _supervisor():
    return ContainerSupervisor(socket="/dev/null", network=None, client=_FakeClient())


def test_a_sidecar_is_named_after_its_app_and_role():
    sup = _supervisor()
    key = sup.register_sidecar("crispal", "db", image="mysql:8.0")
    assert key == "crispal:db"
    assert sup._require(key).name == "aw-app-crispal-db"


def test_a_sidecar_may_expose_no_port_at_all():
    """A database is dialled by its siblings, never reverse-proxied."""
    sup = _supervisor()
    sup.register_sidecar("crispal", "db", image="mysql:8.0")
    assert sup._require("crispal:db").port == 0


def test_the_apps_own_container_still_requires_a_port():
    from src.apps.containers import ContainerError
    sup = _supervisor()
    with pytest.raises(ContainerError):
        sup.register("crispal", "ghcr.io/x/y", 0)


def test_a_portless_sidecar_publishes_nothing(monkeypatch):
    sup = _supervisor()
    sup.register_sidecar("crispal", "db", image="mysql:8.0")
    sup.start("crispal:db")
    assert "ports" not in sup._client.started[0]


def test_sidecar_keys_are_scoped_to_their_app():
    sup = _supervisor()
    sup.register_sidecar("crispal", "db", image="mysql:8.0")
    sup.register_sidecar("crispal", "wordpress", image="wordpress:latest", port=10002)
    sup.register_sidecar("other", "db", image="mysql:8.0")
    assert sup.sidecar_keys("crispal") == ["crispal:db", "crispal:wordpress"]


def test_uninstall_takes_the_sidecars_down_too():
    """An orphaned database outliving its app is exactly the residue to avoid."""
    sup = _supervisor()
    sup.register("crispal", "ghcr.io/x/y", 9410)
    sup.register_sidecar("crispal", "db", image="mysql:8.0")
    sup.register_sidecar("crispal", "wordpress", image="wordpress:latest", port=10002)
    sup.stop_all_for("crispal")
    assert sup.registered() == []
