"""An app's settings must survive an uninstall — see src/apps/config_store.py."""
import json
import os

import pytest

from src.apps import config_store


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setattr(config_store.paths, "workspace_home", lambda: str(tmp_path))
    return tmp_path


def test_save_then_load_round_trips():
    config_store.save("crispal", {"openai_api_key": "sk-x", "db_dsn": "postgres://"})
    assert config_store.load("crispal") == {
        "openai_api_key": "sk-x", "db_dsn": "postgres://"}


def test_load_is_empty_when_nothing_was_saved():
    assert config_store.load("never-installed") == {}


def test_saving_an_empty_config_does_not_clear_an_existing_snapshot():
    # The regression this guards: a reinstall boots on schema defaults, and if
    # that emptiness overwrote the snapshot the good values would be gone one
    # pass before anyone noticed.
    config_store.save("crispal", {"openai_api_key": "sk-x"})
    config_store.save("crispal", {})
    config_store.save("crispal", None)
    assert config_store.load("crispal") == {"openai_api_key": "sk-x"}


def test_snapshot_is_not_world_readable(_home):
    config_store.save("crispal", {"openai_api_key": "sk-secret"})
    assert oct(os.stat(config_store.path("crispal")).st_mode)[-3:] == "600"


def test_snapshot_is_plain_readable_json(_home):
    # Recovering a setting by hand should be `cat`, not a query.
    config_store.save("crispal", {"a": 1})
    with open(config_store.path("crispal")) as f:
        assert json.load(f) == {"a": 1}


def test_purge_removes_it_and_is_idempotent():
    config_store.save("crispal", {"a": 1})
    assert config_store.purge("crispal") is True
    assert config_store.purge("crispal") is False
    assert config_store.load("crispal") == {}


def test_list_saved_names_every_app_with_a_snapshot():
    config_store.save("crispal", {"a": 1})
    config_store.save("kb", {"b": 2})
    assert config_store.list_saved() == ["crispal", "kb"]


def test_a_corrupt_snapshot_reads_as_empty_rather_than_raising(_home):
    config_store.save("crispal", {"a": 1})
    with open(config_store.path("crispal"), "w") as f:
        f.write("{not json")
    assert config_store.load("crispal") == {}
