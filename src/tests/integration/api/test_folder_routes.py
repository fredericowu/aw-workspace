"""``/api/folders*`` — the HTTP contract, the identity gate, and the
"mapping a folder actually reaches the running apps" guarantee.

The registry itself is stubbed (no Postgres in this suite); what's exercised
here is the routing layer's own behaviour: status codes, error shapes, and the
remap hand-off to the apps runtime.
"""
from __future__ import annotations

import time
from types import SimpleNamespace

import jwt as pyjwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import FastAPI
from starlette.testclient import TestClient

from src.api import folders as folders_mod
from src.api.folders import register_folder_routes


def _pem_pair():
    key = Ed25519PrivateKey.generate()
    private_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    public_pem = key.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode()
    return private_pem, public_pem


class _FakeRuntime:
    def __init__(self) -> None:
        self.remaps = 0

    async def remap_folders(self):
        self.remaps += 1
        return ["kb"]


@pytest.fixture()
def ctx(monkeypatch, tmp_path):
    private_pem, public_pem = _pem_pair()
    monkeypatch.setenv("AW_AUTH_PUBLIC_KEY", public_pem)
    token = pyjwt.encode(
        {"sub": "u1", "exp": int(time.time()) + 3600}, private_pem, algorithm="EdDSA"
    )

    store: list[dict] = []
    monkeypatch.setattr(folders_mod, "list_folders", lambda: sorted(store, key=lambda f: f["name"]))

    def fake_add(path, name=None, mode="ro"):
        entry = folders_mod.validate(path, name, mode)
        store[:] = [f for f in store if f["name"] != entry["name"]] + [entry]
        return entry

    def fake_remove(name):
        before = len(store)
        store[:] = [f for f in store if f["name"] != name]
        return len(store) != before

    monkeypatch.setattr(folders_mod, "add_folder", fake_add)
    monkeypatch.setattr(folders_mod, "remove_folder", fake_remove)

    app = FastAPI()
    app.state.app_runtime = _FakeRuntime()
    register_folder_routes(app)
    client = TestClient(app)
    client.cookies.set("aw_id_jwt", token)
    return client, app, tmp_path


def test_requires_identity(ctx):
    client, _, _ = ctx
    client.cookies.clear()
    assert client.get("/api/folders").status_code == 401


def test_map_list_and_unmap_round_trip(ctx):
    client, app, tmp_path = ctx
    docs = tmp_path / "docs"
    docs.mkdir()

    created = client.post("/api/folders", json={"path": str(docs)})
    assert created.status_code == 200
    assert created.json()["folder"]["name"] == "docs"
    assert created.json()["folder"]["exists"] is True
    # The mapping reached the running apps, not just the registry.
    assert created.json()["remapped_apps"] == ["kb"]
    assert app.state.app_runtime.remaps == 1

    listed = client.get("/api/folders").json()["folders"]
    assert [f["name"] for f in listed] == ["docs"]

    removed = client.delete("/api/folders/docs")
    assert removed.status_code == 200
    assert client.get("/api/folders").json()["folders"] == []


def test_maps_a_plain_directory_that_is_not_a_git_repo(ctx):
    """The regression this whole feature exists for."""
    client, _, tmp_path = ctx
    plain = tmp_path / "datasets" / "2026"
    plain.mkdir(parents=True)

    res = client.post("/api/folders", json={"path": str(plain), "name": "data", "mode": "rw"})

    assert res.status_code == 200
    assert res.json()["folder"] == {
        "name": "data", "path": str(plain), "mode": "rw",
        "exists": True, "in_workspace": False,
    }


def test_rejects_a_relative_path_with_400(ctx):
    client, _, _ = ctx
    res = client.post("/api/folders", json={"path": "docs"})
    assert res.status_code == 400
    assert "absolute" in res.json()["detail"]


def test_unmapping_something_unmapped_is_404(ctx):
    client, _, _ = ctx
    assert client.delete("/api/folders/nope").status_code == 404


def test_browse_lists_subdirectories(ctx):
    client, _, tmp_path = ctx
    (tmp_path / "alpha").mkdir()

    res = client.get("/api/folders/-/browse", params={"path": str(tmp_path)})

    assert res.status_code == 200
    assert [e["name"] for e in res.json()["entries"]] == ["alpha"]


def test_browse_rejects_a_bad_path_with_400(ctx):
    client, _, tmp_path = ctx
    res = client.get("/api/folders/-/browse", params={"path": str(tmp_path / "missing")})
    assert res.status_code == 400


def test_a_failing_remap_does_not_fail_the_mapping(ctx, monkeypatch):
    """Registering the folder is the user's request; re-mounting it is a
    convenience. A container that won't come back must not lose the mapping."""
    client, app, tmp_path = ctx
    docs = tmp_path / "docs"
    docs.mkdir()

    async def boom():
        raise RuntimeError("podman is down")

    app.state.app_runtime.remap_folders = boom

    res = client.post("/api/folders", json={"path": str(docs)})

    assert res.status_code == 200
    assert res.json()["remapped_apps"] == []
    assert [f["name"] for f in client.get("/api/folders").json()["folders"]] == ["docs"]
