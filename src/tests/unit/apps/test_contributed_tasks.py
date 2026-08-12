"""``contributes.tasks`` — manifest validation and seed-once dispatch.

The rule under test is deliberately narrow: a contributed task is created
only when no task of that NAME exists, and is never updated afterwards.
"""
import pytest

from src.apps.manifest import ManifestError, validate_manifest
from src.apps.tasks import TasksRegistry


def _manifest(**over):
    base = {
        "manifest_version": 1,
        "id": "demo",
        "name": "Demo",
        "version": "1.0.0",
        "tier": "inprocess",
        "runtime": {"entrypoint": "demo.plugin:Demo"},
        "permissions": ["tasks:contribute"],
        "contributes": {},
    }
    base.update(over)
    return base


# --- manifest validation -----------------------------------------------------


def test_valid_agentic_output_task_is_accepted():
    m = validate_manifest(_manifest(contributes={"tasks": [{
        "name": "Nightly cleanup", "type": "agentic_output",
        "command": "echo hi", "schedules": [{"kind": "cron", "expr": "0 3 * * *"}],
    }]}))
    assert m.tasks[0]["name"] == "Nightly cleanup"


def test_tasks_require_the_capability():
    with pytest.raises(ManifestError, match="tasks:contribute"):
        validate_manifest(_manifest(
            permissions=[],
            contributes={"tasks": [{"name": "X", "type": "agentic_output",
                                    "command": "echo"}]},
        ))


def test_task_needs_a_name():
    with pytest.raises(ManifestError, match="needs a 'name'"):
        validate_manifest(_manifest(contributes={"tasks": [{"command": "echo"}]}))


def test_blank_name_is_rejected():
    # The name is the identity key — a blank one would collide with itself.
    with pytest.raises(ManifestError, match="must not be blank"):
        validate_manifest(_manifest(contributes={"tasks": [
            {"name": "   ", "type": "agentic_output", "command": "echo"}]}))


def test_agentic_output_needs_a_command():
    with pytest.raises(ManifestError, match="needs a 'command'"):
        validate_manifest(_manifest(contributes={"tasks": [
            {"name": "X", "type": "agentic_output"}]}))


def test_terminal_needs_a_prompt():
    with pytest.raises(ManifestError, match="needs a 'prompt'"):
        validate_manifest(_manifest(contributes={"tasks": [
            {"name": "X", "type": "terminal"}]}))


def test_unknown_type_is_rejected():
    with pytest.raises(ManifestError, match="must be 'terminal' or 'agentic_output'"):
        validate_manifest(_manifest(contributes={"tasks": [
            {"name": "X", "type": "cron", "command": "echo"}]}))


def test_an_app_declaring_no_tasks_has_an_empty_list():
    assert validate_manifest(_manifest()).tasks == []


# --- seed-once dispatch ------------------------------------------------------


class FakeProvider:
    """Stands in for aw-app-tasks: create-if-absent, matched by name."""

    def __init__(self, existing=()):
        self.tasks = {n: {"name": n, "enabled": True} for n in existing}
        self.calls = []

    def register_contributed_task(self, app_id, spec):
        self.calls.append((app_id, spec["name"]))
        if spec["name"] in self.tasks:
            return False
        self.tasks[spec["name"]] = dict(spec)
        return True


class FakeLoaded:
    def __init__(self, manifest_tasks, plugin=None):
        self.manifest = type("M", (), {"tasks": manifest_tasks})()
        self.plugin = plugin


class FakeRuntime:
    def __init__(self, apps):
        self._apps = apps

    def loaded_slugs(self):
        return list(self._apps)

    def get(self, slug):
        return self._apps.get(slug)


SPEC = {"name": "Arvin History Cleanup", "type": "agentic_output", "command": "x"}


def test_creates_a_task_that_does_not_exist():
    provider = FakeProvider()
    rt = FakeRuntime({"tasks": FakeLoaded([], provider)})
    assert TasksRegistry().register(rt, "crispal", [SPEC]) == 1
    assert "Arvin History Cleanup" in provider.tasks


def test_existing_task_of_the_same_name_is_left_untouched():
    provider = FakeProvider(existing=["Arvin History Cleanup"])
    before = dict(provider.tasks["Arvin History Cleanup"])
    rt = FakeRuntime({"tasks": FakeLoaded([], provider)})
    assert TasksRegistry().register(rt, "crispal", [SPEC]) == 0
    # Untouched means untouched — the user's enabled flag survives.
    assert provider.tasks["Arvin History Cleanup"] == before


def test_seeding_twice_creates_only_one_task():
    provider = FakeProvider()
    rt = FakeRuntime({"tasks": FakeLoaded([], provider)})
    reg = TasksRegistry()
    assert reg.register(rt, "crispal", [SPEC]) == 1
    assert reg.register(rt, "crispal", [SPEC]) == 0   # every boot re-runs this
    assert len(provider.tasks) == 1


def test_declarations_are_held_until_a_provider_appears():
    # Activation order isn't guaranteed: the contributing app can load first.
    reg = TasksRegistry()
    empty = FakeRuntime({"crispal": FakeLoaded([SPEC])})
    assert reg.register(empty, "crispal", [SPEC]) == 0

    provider = FakeProvider()
    later = FakeRuntime({"crispal": FakeLoaded([SPEC]),
                         "tasks": FakeLoaded([], provider)})
    assert reg.drain_pending(later) == 1
    assert "Arvin History Cleanup" in provider.tasks


def test_sweep_seeds_apps_that_loaded_before_the_provider():
    provider = FakeProvider()
    rt = FakeRuntime({"crispal": FakeLoaded([SPEC]),
                      "tasks": FakeLoaded([], provider)})
    assert TasksRegistry().sweep(rt) == 1


def test_a_raising_provider_does_not_stop_the_other_tasks():
    class Broken(FakeProvider):
        def register_contributed_task(self, app_id, spec):
            if spec["name"] == "bad":
                raise RuntimeError("boom")
            return super().register_contributed_task(app_id, spec)

    provider = Broken()
    rt = FakeRuntime({"tasks": FakeLoaded([], provider)})
    specs = [{"name": "bad", "type": "agentic_output", "command": "x"}, SPEC]
    assert TasksRegistry().register(rt, "crispal", specs) == 1
    assert "Arvin History Cleanup" in provider.tasks
