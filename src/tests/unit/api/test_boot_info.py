"""``/api/health``'s boot identity — minted once, read many times.

``mint_boot_identity`` must be called exactly once in the parent process
before ``uvicorn.run(workers=N)`` forks/spawns workers (see
``src/start/workspace.py``); every worker then only reads these back from
``os.environ``. These tests pin that contract at the ``boot_info`` layer.
"""
from __future__ import annotations

from src.api import boot_info


def test_compute_git_head_returns_head_of_this_checkout():
    head = boot_info.compute_git_head("/opt/aw-workspace")
    assert head
    assert len(head) == 40
    assert all(c in "0123456789abcdef" for c in head)


def test_compute_git_head_degrades_to_empty_string_outside_a_checkout(tmp_path):
    assert boot_info.compute_git_head(str(tmp_path)) == ""


def test_getters_are_empty_before_minting(monkeypatch):
    monkeypatch.delenv("AW_WORKSPACE_BOOT_ID", raising=False)
    monkeypatch.delenv("AW_WORKSPACE_GIT_HEAD", raising=False)
    monkeypatch.delenv("AW_WORKSPACE_STARTED_AT", raising=False)

    assert boot_info.boot_id() == ""
    assert boot_info.git_head() == ""
    assert boot_info.started_at() == 0


def test_mint_boot_identity_sets_all_three_and_getters_read_them_back(monkeypatch):
    monkeypatch.setattr(boot_info, "compute_git_head", lambda: "deadbeef")

    boot_info.mint_boot_identity()

    assert boot_info.boot_id()  # a fresh uuid4 hex, non-empty
    assert boot_info.git_head() == "deadbeef"
    assert boot_info.started_at() > 0


def test_two_mints_produce_different_boot_ids(monkeypatch):
    monkeypatch.setattr(boot_info, "compute_git_head", lambda: "")

    boot_info.mint_boot_identity()
    first = boot_info.boot_id()
    boot_info.mint_boot_identity()
    second = boot_info.boot_id()

    assert first != second


def test_started_at_survives_a_malformed_env_value(monkeypatch):
    monkeypatch.setenv("AW_WORKSPACE_STARTED_AT", "not-a-number")
    assert boot_info.started_at() == 0
