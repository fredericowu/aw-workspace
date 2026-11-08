"""The activation audit (design doc §A.2,
``docs/design/service-supervision-activation-audit.md``) — I1 (every desired
app loaded), I2 (every autostart service accounted for), and the LOUD
escalation when a repair fails or a service has already given up.

I1 runs against a REAL ``Reconciler``/``AppRuntime`` (same harness as
``test_reconciler.py``: a fake fetch that copies a local dir, an in-memory
fake cloud registry) so the repair genuinely exercises the ``install()``
path, not a mock of it. I2 runs against a REAL ``ServiceSupervisor`` (same
harness as ``test_service_restart_loop.py``) wrapped in a minimal fake
runtime, so a repair genuinely spawns and reaps a process. The escalation
tests isolate ``_fire_escalation`` from ``run_once`` deliberately — the
"does run_once decide to escalate" question and "what does firing actually
do" question are different enough to want independent failure signals.
"""
from __future__ import annotations

import asyncio
import shutil
import textwrap
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI

from src.apps import activation_audit as aa
from src.apps import service_lease
from src.apps import services as services_mod
from src.apps.reconciler import AppSpec, Reconciler
from src.apps.runtime import AppRuntime
from src.apps.services import STATE_GAVE_UP, STATE_STOPPED, ServiceSupervisor


def _async(coro):
    return asyncio.run(coro)


async def _drain_background_tasks() -> None:
    """Let any ``asyncio.create_task``-fired escalation actually run. A
    fire-and-forget task scheduled but never awaited is simply cancelled when
    ``asyncio.run`` tears the loop down — this is what makes it deterministic
    in a test instead of a race."""
    pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)


# ---------------------------------------------------------------------------
# I1 — every desired app is loaded (real Reconciler/AppRuntime)
# ---------------------------------------------------------------------------


def _fake_fetch(repo: str, ref: str = "HEAD", *, slug: str, token=None, dest=None) -> str:
    from src.apps import fetch as fetch_mod
    dest = dest or fetch_mod.package_dir_for(slug)
    if dest != repo:
        if shutil.os.path.exists(dest):
            shutil.rmtree(dest)
        shutil.copytree(repo, dest)
    return dest


def _make_app_repo(tmp_path, slug) -> str:
    src = tmp_path / f"src_{slug}"
    src.mkdir()
    (src / "aw-app.json").write_text(textwrap.dedent(f"""
    {{
      "manifest_version": 1,
      "id": "{slug}",
      "name": "{slug}",
      "version": "1.0.0",
      "tier": "inprocess",
      "runtime": {{"entrypoint": "plugin:AppPlugin"}},
      "permissions": ["routes:register"],
      "contributes": {{"routes": [{{"prefix": "/api/apps/{slug}"}}]}}
    }}
    """))
    (src / "plugin.py").write_text(textwrap.dedent("""
        from fastapi import FastAPI
        class AppPlugin:
            async def activate(self, ctx):
                api = FastAPI()
                ctx.routes.register(api)
            async def deactivate(self):
                return None
    """))
    return str(src)


class FakeCloud:
    def __init__(self, rows=None):
        self.rows = list(rows or [])
        self.configured = True

    def list_desired(self):
        return [dict(r) for r in self.rows]

    def put_desired(self, app_id, **kw):
        pass

    def delete_desired(self, app_id, instance_id=""):
        pass


class FakeMirror:
    def __init__(self):
        self.rows = {}

    def list(self):
        return [dict(r) for r in self.rows.values()]

    def upsert(self, spec, package_dir):
        self.rows[spec.app_id] = {
            "app_id": spec.app_id, "version": spec.version, "package_dir": package_dir,
            "repo": spec.repo, "ref": spec.ref,
            "granted_permissions": spec.granted_permissions, "config": spec.config,
            "signed": spec.signed, "state": "installed"}

    def forget(self, app_id):
        self.rows.pop(app_id, None)


def _reconciler(tmp_path, monkeypatch, cloud):
    monkeypatch.setenv("AW_APPS_ROOT", str(tmp_path / "apps"))
    monkeypatch.setenv("AW_WORKSPACE_CONTAINER_DIR", str(tmp_path / "root"))
    host = FastAPI()
    rt = AppRuntime(host, guard_identity=False)
    rc = Reconciler(rt, cloud=cloud, local=FakeMirror(), fetch=_fake_fetch)
    return rt, rc


def test_i1_repairs_a_desired_app_that_is_not_loaded(tmp_path, monkeypatch):
    repo = _make_app_repo(tmp_path, "aa-widget")
    cloud = FakeCloud([{"app_id": "aa-widget", "repo": repo, "ref": "main",
                        "version": "1.0.0", "state": "installed"}])
    rt, rc = _reconciler(tmp_path, monkeypatch, cloud)
    assert not rt.is_loaded("aa-widget")

    result = _async(aa.run_once(rc, rt))

    assert rt.is_loaded("aa-widget")
    assert result["repairs"] == [{"kind": "app", "app": "aa-widget", "action": "install"}]
    assert result["errors"] == []
    assert result["apps_missing"] == 1


def test_i1_a_failing_repair_is_recorded_not_raised(tmp_path, monkeypatch):
    cloud = FakeCloud([{"app_id": "aa-ghost", "repo": str(tmp_path / "nope"),
                        "ref": "main", "version": "1.0.0", "state": "installed"}])
    rt, rc = _reconciler(tmp_path, monkeypatch, cloud)

    result = _async(aa.run_once(rc, rt))

    assert not rt.is_loaded("aa-ghost")
    assert result["repairs"] == []
    assert len(result["errors"]) == 1
    assert result["errors"][0]["app"] == "aa-ghost"
    assert result["errors"][0]["action"] == "install"


def test_i1_repair_cap_bounds_damage(tmp_path, monkeypatch):
    """The bound on how much a bug in this module (or a genuinely bad boot)
    can do per run — everything past the cap is named, never acted on."""
    monkeypatch.setattr(aa, "_MAX_REPAIRS_PER_RUN", 2)
    rows = []
    repos = {}
    for i in range(4):
        slug = f"aa-widget{i}"
        repo = _make_app_repo(tmp_path, slug)
        repos[slug] = repo
        rows.append({"app_id": slug, "repo": repo, "ref": "main",
                    "version": "1.0.0", "state": "installed"})
    cloud = FakeCloud(rows)
    rt, rc = _reconciler(tmp_path, monkeypatch, cloud)

    result = _async(aa.run_once(rc, rt))

    installed = [a for a in repos if rt.is_loaded(a)]
    assert len(installed) == 2, "the repair cap was not respected"
    assert len(result["repairs"]) == 2
    capped = [e for e in result["errors"] if "repair cap reached" in e.get("error", "")]
    assert len(capped) == 2


def test_i1_quiet_pass_stays_quiet(tmp_path, monkeypatch, caplog):
    repo = _make_app_repo(tmp_path, "aa-widget")
    cloud = FakeCloud([{"app_id": "aa-widget", "repo": repo, "ref": "main",
                        "version": "1.0.0", "state": "installed"}])
    rt, rc = _reconciler(tmp_path, monkeypatch, cloud)
    _async(rc.install(AppSpec(app_id="aa-widget", repo=repo, ref="main")))
    assert rt.is_loaded("aa-widget")

    import logging
    caplog.set_level(logging.INFO, logger="src.apps.activation_audit")
    result = _async(aa.run_once(rc, rt))

    assert result["repairs"] == []
    assert result["errors"] == []
    assert result["escalated"] is False
    assert "0 repair(s), 0 error(s)" in caplog.text


def test_provisioning_in_flight_skips_the_run_entirely(tmp_path, monkeypatch):
    repo = _make_app_repo(tmp_path, "aa-widget")
    cloud = FakeCloud([{"app_id": "aa-widget", "repo": repo, "ref": "main",
                        "version": "1.0.0", "state": "installed"}])
    rt, rc = _reconciler(tmp_path, monkeypatch, cloud)
    rc._provision_depth = 1  # simulate "mid our own _provisioning() pass"

    result = _async(aa.run_once(rc, rt))

    assert result == {"skipped": "provisioning-in-flight"}
    assert not rt.is_loaded("aa-widget"), "the audit must not have acted at all"


# ---------------------------------------------------------------------------
# I2 — every autostart service is accounted for (real ServiceSupervisor)
# ---------------------------------------------------------------------------


def _app(slug, *, tier="inprocess"):
    return SimpleNamespace(
        config={"auto_start": True},
        manifest=SimpleNamespace(
            id=slug, tier=tier,
            effective_config_schema={
                "type": "object",
                "properties": {"auto_start": {"type": "boolean", "default": True}}},
        ),
    )


class _AuditRuntime:
    """Just enough of ``AppRuntime`` for ``degradations()`` + the audit's I2
    loop: a REAL ``ServiceSupervisor`` (so a repair genuinely spawns/reaps a
    process) plus the loaded-app bookkeeping ``_autostart_apps`` reads.
    ``get("tasks")`` returns ``None`` by default — the escalation-source
    tests attach their own fake tasks app onto it.
    """

    def __init__(self, apps):
        self._apps = apps
        self.services = ServiceSupervisor()
        self.containers = SimpleNamespace(registered=lambda: [])

    def loaded_slugs(self):
        return list(self._apps)

    def get(self, slug):
        return self._apps.get(slug)

    def is_loaded(self, slug):
        return slug in self._apps


@pytest.fixture()
def fake_redis(monkeypatch):
    """No live Redis in these I2 tests — the gave_up-marker lookup inside
    ``degradations()`` and the escalation debounce/marker calls all go
    through ``service_lease._get_shared_client``, one shared fake."""
    from src.tests.unit.apps.test_service_lease import FakeRedis
    fake = FakeRedis()
    monkeypatch.setattr(service_lease, "_get_shared_client", lambda *a, **k: fake)
    return fake


@pytest.fixture()
def fast_backoff(monkeypatch):
    monkeypatch.setattr(services_mod, "_BACKOFF_BASE_S", 0.02)
    monkeypatch.setattr(services_mod, "_BACKOFF_CAP_S", 0.1)


def _wait(predicate, timeout=10.0, interval=0.02):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


def test_i2_repairs_a_stranded_service(tmp_path, fake_redis):
    """The exact shape ``_arm_lease_retry`` can leave behind: ``desired ==
    running`` but ``state == stopped`` because the lease-wait budget ran out
    (the "honest pair... doctor reports as broken" the restart loop's own
    docstring names) — this is what the audit exists to converge, not just
    describe."""
    rt = _AuditRuntime({"proxy": _app("proxy")})
    rt.services.register("proxy", "proxy-server", "sleep 60", str(tmp_path),
                         autostart=False)
    svc = rt.services._require("proxy", "proxy-server")
    svc.desired = services_mod.DESIRED_RUNNING
    svc.state = STATE_STOPPED
    assert svc.proc is None

    class _FakeReconciler:
        is_provisioning = False

        async def _resolve_desired_active(self):
            return {}, "local"

    result = _async(aa.run_once(_FakeReconciler(), rt))

    assert _wait(lambda: rt.services.status("proxy", "proxy-server")["running"] is True)
    assert result["repairs"] == [
        {"kind": "service", "app": "proxy", "service": "proxy-server",
         "action": "start", "result": "running"}]


def test_i2_never_touches_a_gave_up_service(tmp_path, fake_redis, fast_backoff):
    rt = _AuditRuntime({"proxy": _app("proxy")})
    rt.services.register("proxy", "proxy-server", "sh -c 'exit 7'", str(tmp_path),
                         autostart=False)
    rt.services.start("proxy", "proxy-server")
    svc = rt.services._require("proxy", "proxy-server")
    assert _wait(lambda: svc.state == STATE_GAVE_UP)
    spawned_at = svc.last_spawn_monotonic

    class _FakeReconciler:
        is_provisioning = False

        async def _resolve_desired_active(self):
            return {}, "local"

    async def run():
        result = await aa.run_once(_FakeReconciler(), rt)
        await _drain_background_tasks()
        return result

    result = _async(run())

    time.sleep(0.3)  # well past the (shrunk) backoff window — a spawn would show
    assert svc.last_spawn_monotonic == spawned_at, "a gave_up service was repaired"
    assert svc.state == STATE_GAVE_UP
    assert result["repairs"] == []
    # The LOUD path: a gave_up service IS a reason to escalate, even though
    # it must never be directly repaired.
    assert result["escalated"] is True


def test_i2_never_touches_a_deliberately_stopped_service(tmp_path, fake_redis):
    rt = _AuditRuntime({"proxy": _app("proxy")})
    rt.services.register("proxy", "proxy-server", "sleep 60", str(tmp_path),
                         autostart=False)
    rt.services.start("proxy", "proxy-server")
    rt.services.stop("proxy", "proxy-server")
    svc = rt.services._require("proxy", "proxy-server")
    assert svc.proc is None

    class _FakeReconciler:
        is_provisioning = False

        async def _resolve_desired_active(self):
            return {}, "local"

    result = _async(aa.run_once(_FakeReconciler(), rt))

    time.sleep(0.2)
    assert svc.proc is None, "a deliberate stop was undone by the audit"
    assert result["repairs"] == []
    assert result["escalated"] is False


def test_i2_a_healthy_running_service_is_untouched(tmp_path, fake_redis):
    rt = _AuditRuntime({"proxy": _app("proxy")})
    rt.services.register("proxy", "proxy-server", "sleep 60", str(tmp_path),
                         autostart=False)
    started = rt.services.start("proxy", "proxy-server")
    pid = started["pid"]

    class _FakeReconciler:
        is_provisioning = False

        async def _resolve_desired_active(self):
            return {}, "local"

    result = _async(aa.run_once(_FakeReconciler(), rt))

    assert rt.services.status("proxy", "proxy-server")["pid"] == pid, \
        "a healthy service was restarted unnecessarily"
    assert result["repairs"] == []
    rt.services.stop("proxy", "proxy-server")


# ---------------------------------------------------------------------------
# The LOUD escalation
# ---------------------------------------------------------------------------


class _FakeTaskStore:
    def __init__(self, tasks):
        self._tasks = tasks

    def list(self):
        return list(self._tasks)


class _FakeTaskManager:
    def __init__(self, outcome):
        """``outcome`` is either a run dict to return, or an Exception
        instance/class to raise."""
        self.outcome = outcome
        self.calls = []

    async def run_task(self, task_id, *, trigger="manual"):
        self.calls.append(task_id)
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


def _tasks_runtime(manager, tasks):
    plugin = SimpleNamespace(manager=manager, store=_FakeTaskStore(tasks))
    tasks_app = SimpleNamespace(plugin=plugin)
    return SimpleNamespace(get=lambda slug: tasks_app if slug == "tasks" else None)


def test_fire_escalation_tasks_app_absent_is_a_noop(caplog):
    rt = SimpleNamespace(get=lambda slug: None)
    _async(aa._fire_escalation(rt, ["x gave up"]))  # must not raise
    assert "not installed/loaded" in caplog.text


def test_fire_escalation_no_matching_task_is_a_noop(caplog):
    manager = _FakeTaskManager({"status": "ok"})
    rt = _tasks_runtime(manager, tasks=[{"id": "t1", "name": "something else"}])
    _async(aa._fire_escalation(rt, ["x gave up"]))
    assert manager.calls == []
    assert "no task named" in caplog.text


def test_fire_escalation_dispatch_raising_is_itself_a_degradation(monkeypatch):
    recorded = {}
    monkeypatch.setattr(aa, "set_escalation_failed",
                        lambda **kw: recorded.update(kw) or True)
    monkeypatch.setattr(aa, "clear_escalation_failed",
                        lambda **kw: pytest.fail("must not clear on a raising dispatch"))
    manager = _FakeTaskManager(RuntimeError("agents-platform unreachable"))
    rt = _tasks_runtime(manager, tasks=[{"id": "t1", "name": aa._DEFAULT_ESCALATION_TASK_NAME}])

    _async(aa._fire_escalation(rt, ["proxy/proxy-server gave up (crash-loop)"]))

    assert manager.calls == ["t1"]
    assert recorded["reason"] == "dispatch-raised"
    assert "agents-platform unreachable" in recorded["detail"]


def test_fire_escalation_task_run_error_is_itself_a_degradation(monkeypatch):
    recorded = {}
    monkeypatch.setattr(aa, "set_escalation_failed",
                        lambda **kw: recorded.update(kw) or True)
    manager = _FakeTaskManager({"status": "error", "error": "no agent slug configured"})
    rt = _tasks_runtime(manager, tasks=[{"id": "t1", "name": aa._DEFAULT_ESCALATION_TASK_NAME}])

    _async(aa._fire_escalation(rt, ["proxy/proxy-server gave up (crash-loop)"]))

    assert recorded["reason"] == "task-run-error"
    assert "no agent slug configured" in recorded["detail"]


def test_fire_escalation_success_clears_a_prior_failure_marker(monkeypatch):
    cleared = {"n": 0}
    monkeypatch.setattr(aa, "set_escalation_failed",
                        lambda **kw: pytest.fail("must not mark a success as a failure"))
    monkeypatch.setattr(aa, "clear_escalation_failed", lambda **kw: cleared.__setitem__("n", 1))
    manager = _FakeTaskManager({"status": "ok"})
    rt = _tasks_runtime(manager, tasks=[{"id": "t1", "name": aa._DEFAULT_ESCALATION_TASK_NAME}])

    _async(aa._fire_escalation(rt, ["proxy/proxy-server gave up (crash-loop)"]))

    assert cleared["n"] == 1


def test_run_once_fires_a_detached_escalation_task_on_gave_up(monkeypatch, fake_redis):
    """``run_once`` itself must not block on the escalation — it schedules a
    detached task and returns. Draining pending tasks afterwards is what
    makes the dispatch observable in a test."""
    rt = SimpleNamespace(
        loaded_slugs=lambda: [],
        get=lambda slug: None,
        services=SimpleNamespace(),
        is_loaded=lambda slug: True,
    )
    monkeypatch.setattr(aa, "degradations", lambda runtime: [
        {"kind": "service", "app": "proxy", "service": "proxy-server",
         "state": "gave_up", "desired": "running", "reason": "crash-loop"},
    ])
    fired = {}

    async def _fake_fire(runtime, reasons):
        fired["reasons"] = reasons

    monkeypatch.setattr(aa, "_fire_escalation", _fake_fire)

    class _FakeReconciler:
        is_provisioning = False

        async def _resolve_desired_active(self):
            return {}, "local"

    async def run():
        result = await aa.run_once(_FakeReconciler(), rt)
        await _drain_background_tasks()
        return result

    result = _async(run())

    assert result["escalated"] is True
    assert "proxy/proxy-server gave up (crash-loop)" in fired["reasons"][0]


def test_run_once_debounces_a_second_escalation(monkeypatch, fake_redis):
    rt = SimpleNamespace(
        loaded_slugs=lambda: [], get=lambda slug: None,
        services=SimpleNamespace(), is_loaded=lambda slug: True,
    )
    monkeypatch.setattr(aa, "degradations", lambda runtime: [
        {"kind": "service", "app": "proxy", "service": "proxy-server",
         "state": "gave_up", "desired": "running", "reason": "crash-loop"},
    ])
    calls = {"n": 0}

    async def _fake_fire(runtime, reasons):
        calls["n"] += 1

    monkeypatch.setattr(aa, "_fire_escalation", _fake_fire)

    class _FakeReconciler:
        is_provisioning = False

        async def _resolve_desired_active(self):
            return {}, "local"

    async def run():
        first = await aa.run_once(_FakeReconciler(), rt)
        await _drain_background_tasks()
        second = await aa.run_once(_FakeReconciler(), rt)
        await _drain_background_tasks()
        return first, second

    first, second = _async(run())

    assert first["escalated"] is True
    assert second["escalated"] == "debounced"
    assert calls["n"] == 1, "the debounce window did not prevent a second dispatch"


def test_run_once_clears_a_stale_marker_once_the_condition_resolves(fake_redis):
    """The defect caught in review: a marker set by a PAST failed dispatch
    must not outlive the condition that caused it. Once a run finds nothing
    to escalate (``reasons == []``), the marker has to go — otherwise
    ``escalation_rows()`` reports a PERMANENT degradation on a healthy
    workspace forever, which is the same failure this module exists to
    prevent, just inverted (always red instead of never red)."""
    service_lease.set_escalation_failed(reason="dispatch-raised", detail="boom")
    assert service_lease.escalation_failed_marker() is not None

    rt = SimpleNamespace(
        loaded_slugs=lambda: [], get=lambda slug: None,
        services=SimpleNamespace(), is_loaded=lambda slug: True,
    )

    class _FakeReconciler:
        is_provisioning = False

        async def _resolve_desired_active(self):
            return {}, "local"

    result = _async(aa.run_once(_FakeReconciler(), rt))

    assert result["escalated"] is False
    assert service_lease.escalation_failed_marker() is None
    from src.apps.degradations import degradations
    assert degradations(rt) == []


def test_run_once_with_no_marker_and_nothing_to_escalate_stays_quiet(fake_redis):
    """The companion case: a quiet tick with no marker set must not become
    noisy — the unconditional clear is a no-op, not a new write every run."""
    assert service_lease.escalation_failed_marker() is None
    rt = SimpleNamespace(
        loaded_slugs=lambda: [], get=lambda slug: None,
        services=SimpleNamespace(), is_loaded=lambda slug: True,
    )

    class _FakeReconciler:
        is_provisioning = False

        async def _resolve_desired_active(self):
            return {}, "local"

    result = _async(aa.run_once(_FakeReconciler(), rt))

    assert result["escalated"] is False
    assert service_lease.escalation_failed_marker() is None


# ---------------------------------------------------------------------------
# degradations() surfaces a failed escalation on the SAME choke point
# ---------------------------------------------------------------------------


def test_a_failed_escalation_is_itself_visible_on_degradations(fake_redis):
    from src.apps.degradations import degradations

    rt = SimpleNamespace(loaded_slugs=lambda: [], get=lambda slug: None)
    assert degradations(rt) == []

    service_lease.set_escalation_failed(reason="dispatch-raised", detail="boom")
    rows = degradations(rt)
    assert len(rows) == 1
    assert rows[0]["kind"] == "escalation"
    assert rows[0]["reason"] == "dispatch-raised"
    assert rows[0]["last_error"] == "boom"

    service_lease.clear_escalation_failed()
    assert degradations(rt) == []
