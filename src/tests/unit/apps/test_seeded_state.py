"""Reconciling seeded tasks/agents without clobbering what a human tuned.

The surface was create-if-absent and never updated, which meant a manifest
could read like the source of truth while the live row quietly disagreed —
an app had no way to correct its own mistake. Making it update is easy; making
it update *safely* is the whole design, and these pin the three ways it could
go wrong:

* re-asserting ``enabled``/``schedules`` would switch off every task the user
  turned on, on every boot, silently — every manifest ships ``enabled: false``;
* overwriting a field the user edited in the UI is the clobbering the old
  design existed to prevent;
* a row that shares a name but was never seeded by us is not ours to touch.
"""
from __future__ import annotations

import pytest

from src.apps import seeded_state


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("AW_WORKSPACE_HOME", str(tmp_path / ".aw-workspace"))


SPEC = {
    "name": "demo",
    "prompt": "original prompt",
    "command": "echo hi",
    "enabled": False,
    "schedules": [{"kind": "daily", "time": "03:00"}],
}


def test_untouched_field_is_updatable():
    seeded_state.record("app", "tasks", "demo", SPEC)
    live = dict(SPEC)

    changes = seeded_state.updatable_fields(
        "app", "tasks", "demo", {**SPEC, "prompt": "corrected prompt"}, live)

    assert changes == {"prompt": "corrected prompt"}


def test_a_new_manifest_cannot_flip_enabled_or_reschedule():
    """The case divergence detection does NOT cover, which is why USER_OWNED exists.

    A user who *edited* these is already protected by the hand-edit check. The
    gap is the untouched row: live still equals what we seeded, so the diff
    looks safe, and a v2 manifest shipping ``enabled: true`` or a new time
    would silently start firing a schedule the user never opted into — or move
    one they were relying on.
    """
    seeded_state.record("app", "tasks", "demo", SPEC)
    live = dict(SPEC)  # untouched: matches the seeded values exactly

    changes = seeded_state.updatable_fields("app", "tasks", "demo", {
        **SPEC,
        "enabled": True,
        "schedules": [{"kind": "daily", "time": "07:00"}],
        "prompt": "corrected prompt",
    }, live)

    assert changes == {"prompt": "corrected prompt"}


def test_a_user_toggled_task_is_protected_too():
    """Belt and braces: the same fields, once edited, are caught by both rules."""
    seeded_state.record("app", "tasks", "demo", SPEC)
    live = {**SPEC, "enabled": True}

    changes = seeded_state.updatable_fields("app", "tasks", "demo", SPEC, live)

    assert "enabled" not in changes


def test_a_hand_edited_field_is_left_alone():
    seeded_state.record("app", "tasks", "demo", SPEC)
    live = {**SPEC, "prompt": "the user rewrote this in the UI"}

    changes = seeded_state.updatable_fields(
        "app", "tasks", "demo", {**SPEC, "prompt": "app's newer prompt"}, live)

    assert changes == {}


def test_a_row_we_never_seeded_is_not_ours():
    """Matching is by name, so a task the user made by hand can collide.

    With no record of seeding it, the safe reading is 'not ours' — the same
    reason the create path treats a name match as already-there.
    """
    live = dict(SPEC)

    changes = seeded_state.updatable_fields(
        "app", "tasks", "demo", {**SPEC, "prompt": "new"}, live)

    assert changes == {}


def test_an_unchanged_manifest_produces_no_write():
    """Steady state on every boot — must not churn the store."""
    seeded_state.record("app", "tasks", "demo", SPEC)

    assert seeded_state.updatable_fields("app", "tasks", "demo", SPEC, dict(SPEC)) == {}


def test_recording_again_moves_the_baseline():
    """After a successful reconcile the new value becomes what we own."""
    seeded_state.record("app", "tasks", "demo", SPEC)
    corrected = {**SPEC, "prompt": "v2"}
    seeded_state.record("app", "tasks", "demo", corrected)

    # Live now matches v2; a v3 manifest is still updatable...
    assert seeded_state.updatable_fields(
        "app", "tasks", "demo", {**SPEC, "prompt": "v3"}, dict(corrected)
    ) == {"prompt": "v3"}
    # ...but a user edit on top of v2 is not.
    assert seeded_state.updatable_fields(
        "app", "tasks", "demo", {**SPEC, "prompt": "v3"},
        {**corrected, "prompt": "user text"}
    ) == {}


def test_forget_drops_the_app_entirely():
    seeded_state.record("app", "tasks", "demo", SPEC)
    seeded_state.forget("app")

    assert seeded_state.updatable_fields(
        "app", "tasks", "demo", {**SPEC, "prompt": "new"}, dict(SPEC)) == {}


def test_a_corrupt_state_file_reads_as_empty():
    """Consulted on every boot, so it degrades to 'nothing is ours'."""
    seeded_state.record("app", "tasks", "demo", SPEC)
    with open(seeded_state.state_path(), "w", encoding="utf-8") as fh:
        fh.write("{not json")

    assert seeded_state.updatable_fields(
        "app", "tasks", "demo", {**SPEC, "prompt": "new"}, dict(SPEC)) == {}


def test_a_field_absent_from_live_is_skipped():
    """A platform that doesn't return a field can't be diffed against it."""
    seeded_state.record("app", "tasks", "demo", SPEC)
    live = {k: v for k, v in SPEC.items() if k != "command"}

    changes = seeded_state.updatable_fields(
        "app", "tasks", "demo", {**SPEC, "command": "echo bye"}, live)

    assert changes == {}
