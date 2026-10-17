"""An app's settings must survive an uninstall — see src/apps/config_store.py."""
import json
import os

import pytest

from src.apps import config_store


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setattr(config_store.paths, "workspace_home", lambda: str(tmp_path))
    # The generation token deliberately resolves through the no-makedirs
    # variant (it is read on nearly every /api/apps/* request), so both have
    # to be redirected or the token lands in the LIVE workspace's tree.
    monkeypatch.setattr(config_store.paths, "workspace_home_path", lambda: str(tmp_path))
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


# --- the config-generation token (card 3fc5bf3b) -----------------------------


def test_no_generation_until_something_bumps_it():
    """``None`` means "no config has ever been saved in this workspace", which
    is nothing for a worker to converge to — the middleware skips on it."""
    assert config_store.read_generation() is None
    assert config_store.generation_mtime_ns() is None


def test_each_bump_writes_a_different_token():
    first = config_store.bump_generation()
    second = config_store.bump_generation()
    assert first and second and first != second
    assert config_store.read_generation()[1] == second


def test_the_token_is_not_world_readable():
    # It sits in the same 0700 dir as the snapshots, which can hold API keys.
    config_store.bump_generation()
    assert oct(os.stat(config_store.generation_path()).st_mode)[-3:] == "600"


def test_mtime_moves_when_the_token_does():
    config_store.bump_generation()
    before = config_store.generation_mtime_ns()
    os.utime(config_store.generation_path(), ns=(before - 10**9, before - 10**9))
    # Backwards on purpose: a restore or a copy can do this, which is why
    # consumers compare with `!=` rather than `>`.
    assert config_store.generation_mtime_ns() != before
    config_store.bump_generation()
    assert config_store.generation_mtime_ns() != before - 10**9


def test_the_token_is_not_mistaken_for_an_app_snapshot():
    """``list_saved`` drives nothing less than which apps have a snapshot to
    restore from — a ``.generation`` entry showing up there would invent an
    app called ``.generation``."""
    config_store.save("crispal", {"a": 1})
    config_store.bump_generation()
    assert config_store.list_saved() == ["crispal"]
    assert config_store.load(".generation") == {}


def test_a_bump_that_cannot_write_is_reported_not_raised(monkeypatch):
    """A config save must not fail because its floor could not be stamped —
    the broadcast is still the primary path."""
    monkeypatch.setattr(config_store.os, "replace",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("read-only")))
    assert config_store.bump_generation() is None
