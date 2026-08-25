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


@pytest.fixture(autouse=True)
def _reset_provider():
    """``_provider`` is module-global state — a test that registers one and
    doesn't clean up would silently change how every OTHER test's "agents"
    namespace calls behave, including ones in this same file that never
    mention a provider at all."""
    seeded_state.set_provider(None)
    yield
    seeded_state.set_provider(None)


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


def test_the_first_pass_must_record_a_baseline_to_ever_reconcile():
    """The bootstrap deadlock this surface shipped with, caught on reinstall.

    With no baseline, ``updatable_fields`` correctly returns nothing. If the
    caller then treats "no changes" as "nothing to do" and skips recording,
    the baseline is never written — so there are never any changes, so it is
    never recorded. The surface looks healthy and silently never reconciles.
    """
    live = dict(SPEC)
    assert seeded_state.updatable_fields("app", "tasks", "demo", SPEC, live) == {}

    seeded_state.record("app", "tasks", "demo", SPEC)  # what the caller must do anyway

    assert seeded_state.updatable_fields(
        "app", "tasks", "demo", {**SPEC, "prompt": "v2"}, live) == {"prompt": "v2"}


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


def test_a_new_manifest_field_fills_an_empty_platform_default():
    """A later app version may start wiring an optional reference.

    The row is known to be app-seeded because a baseline exists, and ``None``
    is the platform's untouched default.  This is the Call Agent migration:
    v0.35 added ``agent_config_slug`` so its existing phone agent gained MCP
    tools without changing slug or losing conversation continuity.
    """
    old = {"slug": "phone", "name": "Phone"}
    seeded_state.record("app", "tasks", "phone", old)
    live = {**old, "agent_config_slug": None}

    changes = seeded_state.updatable_fields(
        "app", "tasks", "phone",
        {**old, "agent_config_slug": "agent-config-call-agent-tools"}, live)

    assert changes == {"agent_config_slug": "agent-config-call-agent-tools"}


def test_a_new_manifest_field_does_not_replace_a_nonempty_live_value():
    old = {"slug": "phone", "name": "Phone"}
    seeded_state.record("app", "tasks", "phone", old)
    live = {**old, "agent_config_slug": "users-own-config"}

    changes = seeded_state.updatable_fields(
        "app", "tasks", "phone",
        {**old, "agent_config_slug": "app-default"}, live)

    assert changes == {}


# --- the caller, where the bootstrap deadlock actually lived ------------------


class _FakeTaskProvider:
    """Minimal stand-in for aw-app-tasks' provider surface."""

    def __init__(self, live):
        self.live = dict(live)
        self.writes = []

    def register_contributed_task(self, app_id, spec):
        return False  # always "already exists" — the reconcile path

    def read_contributed_task(self, name):
        return dict(self.live) if self.live.get("name") == name else None

    def update_contributed_task(self, name, changes):
        self.live.update(changes)
        self.writes.append(dict(changes))
        return True


def test_task_reconcile_records_on_the_first_pass_then_updates():
    """Two passes, because one is exactly what the shipped bug looked like.

    Pass 1 has no baseline and must still record one. Pass 2 is the first that
    can compute a change — if pass 1 returned early without recording, this
    stays empty forever and the whole surface silently never reconciles.
    """
    from src.apps.tasks import TasksRegistry

    provider = _FakeTaskProvider(SPEC)

    TasksRegistry._dispatch(provider, "app", [dict(SPEC)])
    assert provider.writes == []  # nothing to change yet, correctly

    TasksRegistry._dispatch(provider, "app", [{**SPEC, "prompt": "corrected"}])

    assert provider.writes == [{"prompt": "corrected"}]
    assert provider.live["prompt"] == "corrected"


def test_task_reconcile_never_touches_enabled_across_passes():
    provider = _FakeTaskProvider({**SPEC, "enabled": True})

    from src.apps.tasks import TasksRegistry

    TasksRegistry._dispatch(provider, "app", [dict(SPEC)])
    TasksRegistry._dispatch(provider, "app", [{**SPEC, "enabled": False, "prompt": "v2"}])

    assert provider.live["enabled"] is True
    assert provider.live["prompt"] == "v2"


# --- "agents" namespace delegates to a tenant-scoped provider ---------------


class _FakeStateProvider:
    """Stands in for aw-app-agents-platform-runners' read_state/write_state —
    a tenant-shared store, not a per-workspace file."""

    def __init__(self):
        self.store: dict[str, dict] = {}
        self.writes: list[tuple] = []

    def read_state(self, kind, slug):
        return self.store.get(f"{kind}:{slug}")

    def write_state(self, app_id, kind, slug, app_version, fingerprints):
        self.writes.append((app_id, kind, slug, app_version, fingerprints))
        self.store[f"{kind}:{slug}"] = {
            "app_id": app_id, "app_version": app_version, "fingerprints": fingerprints,
        }


class _OldProvider:
    """A provider from before read_state/write_state existed — has neither
    method, same as agent_provisioner.py before this landed."""


def test_agents_namespace_records_remotely_not_locally():
    provider = _FakeStateProvider()
    seeded_state.set_provider(provider)

    seeded_state.record("app", "agents", "agents:coder-sonnet", SPEC, app_version="1.0.0")

    assert provider.writes  # went to the provider...
    assert seeded_state._read() == {}  # ...never touched the local file


def test_agents_namespace_reconciles_against_the_remote_baseline():
    provider = _FakeStateProvider()
    seeded_state.set_provider(provider)

    seeded_state.record("app", "agents", "agents:coder-sonnet", SPEC, app_version="1.0.0")
    live = dict(SPEC)

    changes = seeded_state.updatable_fields(
        "app", "agents", "agents:coder-sonnet", {**SPEC, "prompt": "corrected"}, live)

    assert changes == {"prompt": "corrected"}


def test_a_local_baseline_migrates_onto_a_first_seen_provider():
    """The sharpest trap in the whole card: a workspace upgrading from the
    local file must not start from an empty remote baseline, or every field
    it already owned gets reclassified as hand-edited on this very pass."""
    seeded_state.record("app", "agents", "agents:coder-sonnet", SPEC)  # pre-upgrade: local file

    provider = _FakeStateProvider()
    seeded_state.set_provider(provider)  # post-upgrade boot: provider now available, remote empty
    live = dict(SPEC)

    changes = seeded_state.updatable_fields(
        "app", "agents", "agents:coder-sonnet", {**SPEC, "prompt": "corrected"}, live)

    assert changes == {"prompt": "corrected"}  # not {} — the local baseline was honoured


def test_an_old_provider_without_state_methods_falls_back_to_the_local_file():
    """aw-workspace core can ship this before aw-app-agents-platform-runners
    does — a provider with neither method must not silently write nowhere."""
    seeded_state.set_provider(_OldProvider())

    seeded_state.record("app", "agents", "agents:coder-sonnet", SPEC)
    live = dict(SPEC)

    changes = seeded_state.updatable_fields(
        "app", "agents", "agents:coder-sonnet", {**SPEC, "prompt": "corrected"}, live)

    assert changes == {"prompt": "corrected"}


def test_a_prompt_file_trailing_newline_is_not_a_divergence(tmp_path):
    """A one-byte phantom divergence that would disable reconcile app-wide.

    Prompt files end with a newline; the stored value does not. Left in, the
    very first comparison classifies every system_prompt_file agent as
    hand-edited — so the app's own corrections are silently never applied,
    which is the exact failure this whole surface exists to fix.
    """
    from src.apps.agents import resolve_file_fields

    pkg = tmp_path / "pkg"
    (pkg / "prompts").mkdir(parents=True)
    (pkg / "prompts" / "p.md").write_text("You are an agent.\n")

    out = resolve_file_fields(
        {"agents": [{"slug": "a", "system_prompt_file": "prompts/p.md"}]}, str(pkg))

    assert out["agents"][0]["system_prompt"] == "You are an agent."
