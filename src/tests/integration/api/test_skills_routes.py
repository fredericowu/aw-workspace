"""``/api/skills`` CRUD + open-in-code-server — backs Settings > General >
Skills. Real-Postgres only — skips cleanly if 127.0.0.1:5432 isn't reachable,
same pattern as ``test_settings_route_order.py``.

Covers the split-source rule these routes exist to enforce: create/delete
touch ``native-skills/`` only, never the generated ``skills/`` merge, and an
app-owned entry (``.aw-app-id`` marker, no ``native-skills/`` backing) is
listed read-only and refuses delete.
"""
from __future__ import annotations

import os

import pytest
import psycopg
from fastapi.testclient import TestClient
from sqlalchemy import text


def _postgres_reachable() -> bool:
    try:
        psycopg.connect(
            "postgresql://postgres:postgres@127.0.0.1:5432/postgres",
            autocommit=True, connect_timeout=2,
        ).close()
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _postgres_reachable(), reason="live Postgres at 127.0.0.1:5432 not reachable"
)

_SCHEMA = "workspace_skillsroutestest"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("AW_WORKSPACE_SCHEMA", _SCHEMA)
    monkeypatch.setenv("AW_WORKSPACE_DB_URL",
                        "postgresql://postgres:postgres@127.0.0.1:5432/awserv")
    monkeypatch.setenv("AW_WORKSPACE_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("AW_WORKSPACE_CONTAINER_DIR", str(tmp_path / "root"))

    native = tmp_path / "root" / "native-skills" / "aw-existing"
    native.mkdir(parents=True)
    (native / "SKILL.md").write_text(
        "---\nname: aw-existing\ndescription: An existing native skill.\n---\n\nbody\n"
    )
    app_owned = tmp_path / "root" / "skills" / "aw-from-app"
    app_owned.mkdir(parents=True)
    (app_owned / "SKILL.md").write_text("---\nname: aw-from-app\n---\napp body\n")
    (app_owned / ".aw-app-id").write_text("some-app")

    import src.api.db as dbmod
    monkeypatch.setattr(dbmod, "_engine", None)

    import src.api.app as app_mod

    async def noop_reconcile(app):
        return None

    monkeypatch.setattr(app_mod, "reconcile_on_boot", noop_reconcile)

    with TestClient(app_mod.create_app()) as c:
        yield c

    from src.api.db import get_engine
    with get_engine().begin() as conn:
        conn.execute(text(f'DROP SCHEMA IF EXISTS "{_SCHEMA}" CASCADE'))
    monkeypatch.setattr(dbmod, "_engine", None)


def _auth_headers():
    return {"X-Api-Key": os.environ.get("AW_WORKSPACE_API_KEY", "")}


def test_unauthenticated_request_is_401(client):
    assert client.get("/api/skills").status_code == 401


def test_list_skills_includes_native_and_app_owned_with_correct_editable_flag(client):
    res = client.get("/api/skills", headers=_auth_headers())
    assert res.status_code == 200
    by_name = {s["name"]: s for s in res.json()["skills"]}

    assert by_name["aw-existing"]["editable"] is True
    assert by_name["aw-existing"]["owner"] is None
    assert by_name["aw-from-app"]["editable"] is False
    assert by_name["aw-from-app"]["owner"] == "some-app"


def test_create_then_delete_round_trip(client):
    create = client.post(
        "/api/skills", json={"name": "aw-new", "description": "A new one."},
        headers=_auth_headers(),
    )
    assert create.status_code == 200, create.text

    listed = client.get("/api/skills", headers=_auth_headers()).json()["skills"]
    entry = next(s for s in listed if s["name"] == "aw-new")
    assert entry["editable"] is True
    assert entry["owner"] is None

    delete = client.delete("/api/skills/aw-new", headers=_auth_headers())
    assert delete.status_code == 200

    listed_after = client.get("/api/skills", headers=_auth_headers()).json()["skills"]
    assert "aw-new" not in [s["name"] for s in listed_after]


def test_create_rejects_invalid_name(client):
    res = client.post("/api/skills", json={"name": "Not Valid!"}, headers=_auth_headers())
    assert res.status_code == 400


def test_create_rejects_collision_with_an_app_owned_skill(client):
    res = client.post("/api/skills", json={"name": "aw-from-app"}, headers=_auth_headers())
    assert res.status_code == 400


def test_delete_unknown_skill_is_404(client):
    res = client.delete("/api/skills/does-not-exist", headers=_auth_headers())
    assert res.status_code == 404


def test_delete_refuses_an_app_owned_skill(client):
    res = client.delete("/api/skills/aw-from-app", headers=_auth_headers())
    assert res.status_code == 400


def test_open_returns_a_code_server_url(client):
    res = client.post("/api/skills/aw-existing/open", headers=_auth_headers())
    assert res.status_code == 200
    body = res.json()
    assert body["url"].startswith("/api/apps/code-server/?folder=")
    assert "skills/aw-existing" in body["url"]
    assert "payload=" in body["url"]


def test_open_unknown_skill_is_404(client):
    res = client.post("/api/skills/does-not-exist/open", headers=_auth_headers())
    assert res.status_code == 404
