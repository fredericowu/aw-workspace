"""AW_WORKSPACE_DB_URL / AWSERV_DB_URL precedence (see src/api/db.py's
get_db_url) — the BYOD workspace-host runtime (aw-remote-host) sets the
former to point at the user's own local Postgres."""
from __future__ import annotations

from src.api.db import get_db_url


def test_prefers_aw_workspace_db_url_over_awserv_db_url(monkeypatch):
    monkeypatch.setenv("AWSERV_DB_URL", "postgresql://postgres:postgres@127.0.0.1:5432/awserv")
    monkeypatch.setenv("AW_WORKSPACE_DB_URL", "postgresql://postgres:secret@127.0.0.1:5432/aw_workspace")

    assert get_db_url() == "postgresql+psycopg://postgres:secret@127.0.0.1:5432/aw_workspace"


def test_falls_back_to_awserv_db_url_when_alias_unset(monkeypatch):
    monkeypatch.delenv("AW_WORKSPACE_DB_URL", raising=False)
    monkeypatch.setenv("AWSERV_DB_URL", "postgresql://postgres:postgres@127.0.0.1:5432/awserv")

    assert get_db_url() == "postgresql+psycopg://postgres:postgres@127.0.0.1:5432/awserv"


def test_falls_back_to_default_when_neither_set(monkeypatch):
    monkeypatch.delenv("AW_WORKSPACE_DB_URL", raising=False)
    monkeypatch.delenv("AWSERV_DB_URL", raising=False)

    assert get_db_url() == "postgresql+psycopg://postgres:postgres@127.0.0.1:5432/awserv"
