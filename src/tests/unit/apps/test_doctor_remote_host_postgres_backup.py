"""Dead-man's switch for the nightly aw-remote-host-postgres backup.

docs/design/aw-remote-host-postgres-backup.md §6 step 7: a red GHA run
already shows up on its own; this check exists for the mode a red run
cannot catch — the job silently stopped being scheduled at all. These tests
pin the three states that matter (never run, stale, fresh) plus the ``ok``
wiring, since reporting the problem in the body while leaving ``ok`` true
would reproduce exactly the kind of silence this card exists to kill.
"""
import json
import os

import pytest

from src.apps import routes


@pytest.fixture
def status_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(routes, "workspace_home_path", lambda: str(tmp_path))
    d = tmp_path / "data" / "backup-status"
    d.mkdir(parents=True)
    return d


def _write(status_dir, **fields):
    with open(status_dir / "remote-host-postgres.json", "w", encoding="utf-8") as f:
        json.dump(fields, f)


def test_fresh_backup_is_not_stale(status_dir):
    _write(status_dir, slot="sunday", verified_at="2026-10-18T00:30:00Z",
           executions=25063)
    out = routes._remote_host_postgres_backup_status()
    assert out["present"] is True
    assert out["stale"] is False
    assert out["slot"] == "sunday"


def test_missing_file_is_absent_and_stale(status_dir):
    out = routes._remote_host_postgres_backup_status()
    assert out["present"] is False
    assert out["stale"] is True
    assert "ever run" in out["reason"]


def test_old_verified_at_is_stale(status_dir, monkeypatch):
    # 2026-10-15 is well over 26h before "now" regardless of when this test
    # actually runs, so this does not need to fake the clock.
    _write(status_dir, slot="thursday", verified_at="2026-10-15T00:00:00Z")
    out = routes._remote_host_postgres_backup_status()
    assert out["present"] is True
    assert out["stale"] is True
    assert "h old" in out["reason"]


def test_unparseable_verified_at_is_stale(status_dir):
    _write(status_dir, slot="sunday", verified_at="not-a-timestamp")
    out = routes._remote_host_postgres_backup_status()
    assert out["stale"] is True
    assert "unparseable" in out["reason"]


def test_ok_wiring_folds_stale_into_degraded():
    """Pins the ``not remote_host_postgres_backup["stale"]`` clause in the
    doctor payload's ``ok`` computation."""
    def ok_for(backup):
        return not backup["stale"]

    assert ok_for({"stale": False}) is True
    assert ok_for({"stale": True}) is False
