"""``contributes.agents`` — manifest validation and seed-once dispatch.

Same narrow rule the tasks surface enforces, keyed on the slug instead of
the name: an object is created only when nothing of that slug exists, and
is never updated afterwards. The extra thing to hold onto here is ORDER —
an Agent references a model, a config and a group by slug, so the four
kinds have a creation sequence and the provider is handed all of them at
once precisely so it can honour it.
"""
import pytest

from src.apps.agents import KINDS, AgentsRegistry, resolve_file_fields
from src.apps.manifest import ManifestError, validate_manifest


def _manifest(**over):
    base = {
        "manifest_version": 1,
        "id": "demo",
        "name": "Demo",
        "version": "1.0.0",
        "tier": "inprocess",
        "runtime": {"entrypoint": "demo.plugin:Demo"},
        "permissions": ["agents:contribute"],
        "contributes": {},
    }
    base.update(over)
    return base


def _agents(**kinds):
    return {"agents": kinds}


# --- manifest validation -----------------------------------------------------


def test_a_full_declaration_is_accepted():
    m = validate_manifest(_manifest(contributes=_agents(
        targets=[{"slug": "system-investigations", "name": "System Investigations"}],
        models=[{"slug": "sonnet", "provider": "anthropic",
                 "model_id": "claude-sonnet-5"}],
        agent_configs=[{"slug": "reviewer-cfg", "name": "Reviewer Config"}],
        groups=[{"slug": "reviewers", "name": "Reviewers"}],
        agents=[{"slug": "sec-reviewer", "name": "Security Reviewer",
                 "model_slug": "sonnet", "group_slug": "reviewers"}],
        agent_flows=[{"slug": "sec-flow", "name": "Security Flow",
                      "enabled": True, "graph": {"nodes": [], "edges": []}}],
    )))
    assert m.agents["targets"][0]["slug"] == "system-investigations"
    assert m.agents["models"][0]["slug"] == "sonnet"
    assert m.agents["agents"][0]["group_slug"] == "reviewers"
    assert m.agents["agent_flows"][0]["enabled"] is True


def test_a_full_declaration_with_workflows_and_evals_is_accepted():
    m = validate_manifest(_manifest(contributes=_agents(
        agents=[{"slug": "sec-reviewer", "name": "Security Reviewer"}],
        workflows=[{"slug": "wf1", "name": "WF1", "kind": "review",
                   "graph": {"nodes": [{"id": "a", "agent_slug": "sec-reviewer"}]}}],
        evals=[{"slug": "ev1", "name": "EV1", "target_kind": "agent",
               "target_slug": "sec-reviewer", "dataset": [], "metric": "judge_llm"}],
    )))
    assert m.agents["workflows"][0]["slug"] == "wf1"
    assert m.agents["evals"][0]["target_slug"] == "sec-reviewer"


def test_a_workflow_needs_a_name():
    with pytest.raises(ManifestError, match="needs a 'name'"):
        validate_manifest(_manifest(contributes=_agents(
            workflows=[{"slug": "wf1", "kind": "review", "graph": {}}])))


def test_an_eval_needs_a_name():
    with pytest.raises(ManifestError, match="needs a 'name'"):
        validate_manifest(_manifest(contributes=_agents(
            evals=[{"slug": "ev1", "target_kind": "agent", "target_slug": "x",
                   "dataset": [], "metric": "judge_llm"}])))


def test_a_target_needs_a_name():
    with pytest.raises(ManifestError, match="needs a 'name'"):
        validate_manifest(_manifest(contributes=_agents(
            targets=[{"slug": "nameless"}])))


def test_an_agent_flow_needs_a_name():
    # The flow is what turns a set of agents into a team; an unnamed one is
    # unpickable in the UI that draws it.
    with pytest.raises(ManifestError, match="name"):
        validate_manifest(_manifest(
            contributes=_agents(agent_flows=[{"slug": "sec-flow"}])))


def test_agents_require_the_capability():
    with pytest.raises(ManifestError, match="agents:contribute"):
        validate_manifest(_manifest(
            permissions=[],
            contributes=_agents(groups=[{"slug": "g", "name": "G"}]),
        ))


def test_every_entry_needs_a_slug():
    # The slug is the identity key — without one, two entries seeded on
    # different boots can't be told apart from the same one seeded twice.
    with pytest.raises(ManifestError, match="needs a 'slug'"):
        validate_manifest(_manifest(contributes=_agents(
            agents=[{"name": "No Slug"}])))


def test_blank_slug_is_rejected():
    with pytest.raises(ManifestError, match="needs a 'slug'"):
        validate_manifest(_manifest(contributes=_agents(
            groups=[{"slug": "   ", "name": "G"}])))


def test_slug_must_look_like_a_slug():
    with pytest.raises(ManifestError, match="must be lowercase"):
        validate_manifest(_manifest(contributes=_agents(
            groups=[{"slug": "Not A Slug", "name": "G"}])))


def test_a_model_needs_its_provider_and_model_id():
    with pytest.raises(ManifestError, match="needs a 'model_id'"):
        validate_manifest(_manifest(contributes=_agents(
            models=[{"slug": "sonnet", "provider": "anthropic"}])))


def test_an_agent_needs_a_name():
    with pytest.raises(ManifestError, match="needs a 'name'"):
        validate_manifest(_manifest(contributes=_agents(
            agents=[{"slug": "nameless"}])))


def test_unknown_kind_is_rejected():
    # Catches the plural/singular slip ("group" vs "groups") at install time
    # instead of silently seeding nothing.
    with pytest.raises(ManifestError, match="unknown contributes.agents key"):
        validate_manifest(_manifest(contributes=_agents(
            group=[{"slug": "g", "name": "G"}])))


def test_a_list_where_an_object_belongs_is_rejected():
    with pytest.raises(ManifestError, match="must be an object"):
        validate_manifest(_manifest(contributes={"agents": [{"slug": "x"}]}))


def test_an_app_declaring_no_agents_has_an_empty_dict():
    assert validate_manifest(_manifest()).agents == {}


# --- file-backed prompts -----------------------------------------------------


def test_system_prompt_file_is_inlined_from_the_package(tmp_path):
    (tmp_path / "prompts").mkdir()
    (tmp_path / "prompts" / "reviewer.md").write_text("You review code.")
    spec = {"agents": [{"slug": "rev", "name": "Rev",
                        "system_prompt_file": "prompts/reviewer.md"}]}
    out = resolve_file_fields(spec, str(tmp_path))
    assert out["agents"][0]["system_prompt"] == "You review code."
    assert "system_prompt_file" not in out["agents"][0]


def test_a_prompt_file_outside_the_package_is_refused(tmp_path):
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (tmp_path / "secrets.txt").write_text("not yours")
    spec = {"agents": [{"slug": "rev", "name": "Rev",
                        "system_prompt_file": "../secrets.txt"}]}
    out = resolve_file_fields(spec, str(pkg))
    assert "system_prompt" not in out["agents"][0]


def test_a_missing_prompt_file_does_not_raise(tmp_path):
    spec = {"agents": [{"slug": "rev", "name": "Rev",
                        "system_prompt_file": "nope.md"}]}
    assert resolve_file_fields(spec, str(tmp_path))["agents"][0].get("system_prompt") is None


def test_a_dataset_file_is_parsed_not_rstripped(tmp_path):
    """dataset is JSON, not text — the fingerprint-stability fix a prompt
    file needs (rstrip the trailing newline) doesn't apply; the equivalent
    for JSON is comparing the PARSED value, which survives re-serialization
    whitespace/ordering noise that rstrip() alone would not."""
    (tmp_path / "eval.json").write_text('[\n  {"input": "hi", "expected": "hey"}\n]\n')
    spec = {"evals": [{"slug": "e1", "dataset_file": "eval.json"}]}

    out = resolve_file_fields(spec, str(tmp_path))

    assert out["evals"][0]["dataset"] == [{"input": "hi", "expected": "hey"}]
    assert "dataset_file" not in out["evals"][0]


def test_an_invalid_json_dataset_file_is_dropped_with_a_warning(tmp_path):
    (tmp_path / "eval.json").write_text("{not json")
    spec = {"evals": [{"slug": "e1", "dataset_file": "eval.json"}]}

    out = resolve_file_fields(spec, str(tmp_path))

    assert "dataset" not in out["evals"][0]


def test_resolution_never_mutates_the_manifest_dict(tmp_path):
    # The manifest is re-read every boot; a resolved copy written back would
    # be re-resolved against an already-inlined prompt.
    (tmp_path / "p.md").write_text("hi")
    spec = {"agents": [{"slug": "rev", "name": "Rev", "system_prompt_file": "p.md"}]}
    resolve_file_fields(spec, str(tmp_path))
    assert spec["agents"][0]["system_prompt_file"] == "p.md"


# --- seed-once dispatch ------------------------------------------------------


class FakeProvider:
    """Stands in for aw-app-agents-platform-runners: create-if-absent by slug."""

    def __init__(self, existing=None):
        self.store = {k: dict(v) for k, v in (existing or {}).items()}
        self.order = []

    def register_contributed_agents(self, app_id, spec):
        created = {}
        # Driven by KINDS, not a copy of it: the real provider seeds every
        # kind the contract defines, so a kind added to the contract must
        # show up here without anyone remembering to edit this double.
        for kind in KINDS:
            for entry in spec.get(kind) or []:
                self.order.append((kind, entry["slug"]))
                bucket = self.store.setdefault(kind, {})
                if entry["slug"] in bucket:
                    continue
                bucket[entry["slug"]] = dict(entry)
                created[kind] = created.get(kind, 0) + 1
        return created


class FakeLoaded:
    def __init__(self, manifest_agents, plugin=None, package_dir=""):
        self.manifest = type("M", (), {"agents": manifest_agents})()
        self.plugin = plugin
        self.package_dir = package_dir


class FakeRuntime:
    def __init__(self, apps):
        self._apps = apps

    def loaded_slugs(self):
        return list(self._apps)

    def get(self, slug):
        return self._apps.get(slug)


SPEC = {
    "targets": [{"slug": "system-investigations", "name": "System Investigations"}],
    "models": [{"slug": "sonnet", "provider": "anthropic", "model_id": "claude-sonnet-5"}],
    "agent_configs": [{"slug": "rev-cfg", "name": "Reviewer Config"}],
    "groups": [{"slug": "reviewers", "name": "Reviewers"}],
    "agents": [{"slug": "sec-reviewer", "name": "Security Reviewer",
                "model_slug": "sonnet", "agent_config_slug": "rev-cfg",
                "group_slug": "reviewers"}],
    "agent_flows": [{"slug": "sec-flow", "name": "Security Flow",
                     "graph": {"nodes": [{"id": "a", "type": "agent",
                                          "agent_slug": "sec-reviewer"}]}}],
}


def test_creates_every_declared_object():
    provider = FakeProvider()
    rt = FakeRuntime({"runners": FakeLoaded({}, provider)})
    created = AgentsRegistry().register(rt, "sec", SPEC)
    assert created == {"targets": 1, "models": 1, "agent_configs": 1,
                       "groups": 1, "agents": 1, "agent_flows": 1}


def test_the_agent_is_created_after_what_it_references():
    # A wrong order doesn't error — it produces an agent pointing at three
    # slugs that don't exist yet, or a flow whose graph names agents that
    # aren't there. So the order itself is the assertion. ``targets`` has no
    # such reference in either direction, so it goes first.
    provider = FakeProvider()
    rt = FakeRuntime({"runners": FakeLoaded({}, provider)})
    AgentsRegistry().register(rt, "sec", SPEC)
    assert [kind for kind, _ in provider.order] == [
        "targets", "models", "agent_configs", "groups", "agents", "agent_flows"]


def test_an_existing_slug_is_left_untouched():
    provider = FakeProvider(existing={
        "agents": {"sec-reviewer": {"slug": "sec-reviewer",
                                    "system_prompt": "user's own tuned prompt"}},
    })
    rt = FakeRuntime({"runners": FakeLoaded({}, provider)})
    created = AgentsRegistry().register(rt, "sec", SPEC)
    assert "agents" not in created
    # Untouched means untouched — weeks of prompt tuning survive the boot.
    assert provider.store["agents"]["sec-reviewer"]["system_prompt"] == "user's own tuned prompt"


def test_seeding_twice_creates_one_set():
    provider = FakeProvider()
    rt = FakeRuntime({"runners": FakeLoaded({}, provider)})
    reg = AgentsRegistry()
    assert reg.register(rt, "sec", SPEC)["agents"] == 1
    assert reg.register(rt, "sec", SPEC) == {}   # every boot re-runs this
    assert len(provider.store["agents"]) == 1


def test_declarations_are_held_until_a_provider_appears():
    reg = AgentsRegistry()
    assert reg.register(FakeRuntime({"sec": FakeLoaded(SPEC)}), "sec", SPEC) == {}

    provider = FakeProvider()
    later = FakeRuntime({"sec": FakeLoaded(SPEC), "runners": FakeLoaded({}, provider)})
    assert reg.drain_pending(later)["agents"] == 1


def test_sweep_seeds_apps_that_loaded_before_the_provider():
    provider = FakeProvider()
    rt = FakeRuntime({"sec": FakeLoaded(SPEC), "runners": FakeLoaded({}, provider)})
    assert AgentsRegistry().sweep(rt)["agents"] == 1


class FakeStateProvider(FakeProvider):
    """FakeProvider plus the read/write pair _reconcile uses, plus the
    tenant-state pair seeded_state delegates to — enough to trace an
    app's manifest version all the way into a write_state call."""

    def __init__(self, existing=None):
        super().__init__(existing)
        self.state: dict[str, dict] = {}
        self.write_state_calls: list[tuple] = []

    def read_contributed_agent(self, kind, slug):
        return self.store.get(kind, {}).get(slug)

    def update_contributed_agent(self, kind, slug, changes):
        self.store[kind][slug].update(changes)
        return True

    def read_state(self, kind, slug):
        return self.state.get(f"{kind}:{slug}")

    def write_state(self, app_id, kind, slug, app_version, fingerprints):
        self.write_state_calls.append((app_id, kind, slug, app_version, fingerprints))
        self.state[f"{kind}:{slug}"] = {"app_version": app_version, "fingerprints": fingerprints}


def test_the_apps_own_manifest_version_reaches_the_tenant_state_write():
    from src.apps import seeded_state

    provider = FakeStateProvider()
    manifest = type("M", (), {"agents": SPEC, "version": "2.3.1"})()
    rt = FakeRuntime({"runners": FakeLoaded({}, provider),
                      "sec": FakeLoaded(SPEC)})
    rt._apps["sec"].manifest = manifest

    try:
        AgentsRegistry().sweep(rt)
        versions = {call[3] for call in provider.write_state_calls}
        assert versions == {"2.3.1"}
    finally:
        seeded_state.set_provider(None)


def test_failed_reconcile_does_not_advance_the_remote_baseline():
    """A transient PATCH failure must remain retryable on the next boot."""
    from src.apps import seeded_state

    old = {"agents": [{"slug": "phone", "name": "Phone",
                       "agent_config_slug": None}]}
    new = {"agents": [{"slug": "phone", "name": "Phone",
                       "agent_config_slug": "tools"}]}
    provider = FakeStateProvider(existing={"agents": {"phone": old["agents"][0]}})
    seeded_state.set_provider(provider)
    seeded_state.record("call-agent", "agents", "agents:phone",
                        old["agents"][0], app_version="1.0.0")
    before = dict(provider.state["agents:phone"])
    provider.update_contributed_agent = lambda kind, slug, changes: False

    try:
        AgentsRegistry._reconcile(provider, "call-agent", new, "2.0.0")
        assert provider.state["agents:phone"] == before
    finally:
        seeded_state.set_provider(None)


def test_a_raising_provider_does_not_fail_the_activation():
    class Broken:
        def register_contributed_agents(self, app_id, spec):
            raise RuntimeError("agents-platform unreachable")

    rt = FakeRuntime({"runners": FakeLoaded({}, Broken())})
    assert AgentsRegistry().register(rt, "sec", SPEC) == {}


def test_an_empty_declaration_never_reaches_the_provider():
    provider = FakeProvider()
    rt = FakeRuntime({"runners": FakeLoaded({}, provider)})
    assert AgentsRegistry().register(rt, "sec", {"models": [], "agents": []}) == {}
    assert provider.order == []


def test_kinds_orders_workflows_and_evals_between_agents_and_flows():
    # Workflows reference agents by slug (comes after); evals target either
    # an agent or a workflow by slug (comes after both); agent_flows
    # reference agents by slug too, so it still goes last.
    assert KINDS == ("targets", "models", "agent_configs", "groups", "agents",
                     "workflows", "evals", "agent_flows")


def test_a_workflow_and_eval_are_seeded_in_order():
    spec = {**SPEC,
            "workflows": [{"slug": "wf1", "name": "WF1", "kind": "review",
                          "graph": {"nodes": [{"id": "a", "agent_slug": "sec-reviewer"}]}}],
            "evals": [{"slug": "ev1", "name": "EV1", "target_kind": "agent",
                      "target_slug": "sec-reviewer", "dataset": [], "metric": "judge_llm"}]}
    provider = FakeProvider()
    rt = FakeRuntime({"runners": FakeLoaded({}, provider)})
    created = AgentsRegistry().register(rt, "sec", spec)
    assert created["workflows"] == 1
    assert created["evals"] == 1
    assert [kind for kind, _ in provider.order] == [
        "targets", "models", "agent_configs", "groups", "agents", "workflows", "evals", "agent_flows"]
