"""``_resolve_workers`` (``src/start/workspace.py``) — AW_WORKSPACE_WORKERS
resolution.

The regression this guards: AW_WORKSPACE_WORKERS ships baked into the image
(Dockerfile ``ENV``), so ``os.environ`` for it is populated on every boot
regardless of what an operator wants. Before this fix the only way to change
the worker count was an image rebuild + container recreate — a plain process
restart always inherited the baked value. ``<workspace_home>/.env`` is meant
to let a restart alone pick up a new count, which only works if ``.env`` is
checked BEFORE ``os.environ`` — the reverse of the order most ``.env``
fallbacks in this codebase use (see ``src.cli.core_restart``), because those
other vars are typically absent from ``os.environ`` until something sets
them explicitly.
"""
from __future__ import annotations

import os

import pytest

from src.apps.paths import upsert_workspace_env
from src.start.workspace import _resolve_workers


@pytest.fixture()
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("AW_WORKSPACE_HOME", str(tmp_path / "home"))
    os.makedirs(tmp_path / "home", exist_ok=True)
    monkeypatch.delenv("AW_WORKSPACE_WORKERS", raising=False)
    return tmp_path / "home"


def test_no_env_and_no_dotenv_falls_back_to_one(home):
    assert _resolve_workers() == 1


def test_os_environ_used_when_dotenv_is_absent(home, monkeypatch):
    """The Dockerfile-baked value, with nothing overriding it."""
    monkeypatch.setenv("AW_WORKSPACE_WORKERS", "10")
    assert _resolve_workers() == 10


def test_dotenv_overrides_os_environ(home, monkeypatch):
    """The regression: an operator edits .env without rebuilding the image
    or recreating the container, and a plain restart must pick it up even
    though the Dockerfile ENV keeps os.environ populated too."""
    monkeypatch.setenv("AW_WORKSPACE_WORKERS", "10")
    upsert_workspace_env("AW_WORKSPACE_WORKERS", "3")
    assert _resolve_workers() == 3
