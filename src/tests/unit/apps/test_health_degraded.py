"""``/api/health`` stopped lying (design §C).

Until 2026-10-11 it answered a hardcoded ``{"status": "ok"}``, and it did so
for the whole ≥27 minutes that a dead ``proxy:proxy-server`` had every
browser-automation MCP family down. The only component that could name the
cause was ``aw-workspace-cli doctor``, which nothing runs proactively.

The shape of the fix matters as much as the fix:

* it consumes the SAME primitive ``doctor`` does (``degradations()``), never
  doctor itself — that sweep shells out to system CLIs, probes podman and
  HTTP-calls every app's self-check route, which on an endpoint uptime
  monitors poll every few seconds turns monitoring into load;
* a degraded workspace still answers **200**, because a 5xx makes a
  container orchestrator restart core — the exact restart that started the
  incident;
* it fails **open**: if the check itself breaks, health says ok rather than
  failing the endpoint.
"""
from types import SimpleNamespace

from src.apps import degradations as degmod
from src.apps.containers import ContainerError
from src.apps.degradations import (degradations, health_report,
                                   health_report_cached, invalidate_cache)


def _app(slug, *, config, schema_has_autostart=True, tier="container"):
    props = {"auto_start": {"type": "boolean", "default": True}} if schema_has_autostart else {}
    return SimpleNamespace(
        config=config,
        manifest=SimpleNamespace(
            id=slug, tier=tier,
            effective_config_schema={"type": "object", "properties": props},
        ),
    )


class _Services:
    def __init__(self, rows=None):
        self._rows = rows or {}

    def registered(self):
        return list(self._rows)

    def status(self, app_id, service_id):
        return self._rows[(app_id, service_id)]


class _Containers:
    def __init__(self, rows=None, errors=None):
        self._rows = rows or {}
        self._errors = errors or set()

    def registered(self):
        return [(app_id, SimpleNamespace()) for app_id in self._rows] + \
               [(app_id, SimpleNamespace()) for app_id in self._errors]

    def status(self, app_id):
        if app_id in self._errors:
            raise ContainerError("boom")
        return self._rows[app_id]


class _Runtime:
    def __init__(self, apps, services=None, containers=None):
        self._apps = apps
        self.services = services or _Services()
        self.containers = containers or _Containers()

    def loaded_slugs(self):
        return list(self._apps)

    def get(self, slug):
        return self._apps.get(slug)


def _no_redis(monkeypatch, markers=None, reachable=True):
    """Pin the give-up marker lookup instead of touching the live Redis these
    unit tests share with the rest of the workspace."""
    monkeypatch.setattr(degmod, "gave_up_markers",
                        lambda pairs, **kw: (markers or {}, reachable))


def test_a_healthy_workspace_reports_ok(monkeypatch):
    _no_redis(monkeypatch)
    rt = _Runtime(
        {"proxy": _app("proxy", config={"auto_start": True}, tier="inprocess")},
        services=_Services({("proxy", "proxy-server"): {"running": True}}),
    )
    assert health_report(rt) == {"status": "ok", "degraded": []}


def test_a_dead_autostart_service_makes_the_workspace_degraded(monkeypatch):
    """The incident's own signature, in the payload an uptime monitor reads."""
    _no_redis(monkeypatch)
    rt = _Runtime(
        {"proxy": _app("proxy", config={"auto_start": True}, tier="inprocess")},
        services=_Services({
            ("proxy", "proxy-server"): {"running": False, "state": "off",
                                        "last_exit_code": 1, "last_error": "boom",
                                        "desired": "running",
                                        "supervised_here": True},
        }),
    )
    report = health_report(rt)
    assert report["status"] == "degraded"
    assert report["degraded"] == [{
        "kind": "service", "app": "proxy", "service": "proxy-server",
        "state": "off", "autostart": True, "desired": "running",
    }]
    # A process's last stderr lines are a debugging detail doctor wants and a
    # scraped status payload does not. `desired` is the opposite call: "off and
    # nobody meant it to be" is exactly what a monitor needs to tell apart
    # from a component somebody switched off, so it stays.
    assert "last_error" not in report["degraded"][0]
    assert "supervised_here" not in report["degraded"][0]


def test_the_supervisors_own_state_reaches_the_payload(monkeypatch):
    """"off" and "gave up" are not the same operational fact: one may clear
    itself within seconds, the other never will without a human."""
    _no_redis(monkeypatch)
    rt = _Runtime(
        {"proxy": _app("proxy", config={"auto_start": True}, tier="inprocess")},
        services=_Services({
            ("proxy", "proxy-server"): {
                "running": False, "state": "gave_up",
                "give_up_reason": "crash-loop",
            },
        }),
    )
    row = health_report(rt)["degraded"][0]
    assert row["state"] == "gave_up"
    assert row["reason"] == "crash-loop"


def test_a_give_up_decided_on_another_worker_is_still_visible(monkeypatch):
    """A give-up is decided by whichever worker owned the process and lives
    in its memory; every other worker has only the durable Redis marker. The
    request that lands on one of those must not report a bare "off"."""
    _no_redis(monkeypatch, markers={
        ("proxy", "proxy-server"): {"reason": "orphan-suspected", "exit_code": 1},
    })
    rt = _Runtime(
        {"proxy": _app("proxy", config={"auto_start": True}, tier="inprocess")},
        services=_Services({("proxy", "proxy-server"): {"running": False}}),
    )
    row = health_report(rt)["degraded"][0]
    assert row["state"] == "gave_up"
    assert row["reason"] == "orphan-suspected"


def test_redis_away_degrades_open_and_says_so(monkeypatch):
    """Health must never fail because its own dependency blinked — but it
    must not pretend the answer is fleet-wide either."""
    _no_redis(monkeypatch, reachable=False)
    rt = _Runtime(
        {"proxy": _app("proxy", config={"auto_start": True}, tier="inprocess")},
        services=_Services({("proxy", "proxy-server"): {"running": False}}),
    )
    report = health_report(rt)
    assert report["status"] == "degraded"
    assert report["health_scope"] == "local-worker"


def test_a_stopped_container_is_degraded_too(monkeypatch):
    _no_redis(monkeypatch)
    rt = _Runtime(
        {"browser": _app("browser", config={"auto_start": True})},
        containers=_Containers({"browser": {"running": False}}),
    )
    assert health_report(rt)["degraded"] == [{
        "kind": "container", "app": "browser", "service": None,
        "state": "off", "autostart": True,
    }]


def test_a_container_the_engine_cannot_reach_counts_as_degraded(monkeypatch):
    _no_redis(monkeypatch)
    rt = _Runtime(
        {"browser": _app("browser", config={"auto_start": True})},
        containers=_Containers(errors={"browser"}),
    )
    assert health_report(rt)["status"] == "degraded"


def test_an_app_the_user_switched_off_is_not_degraded(monkeypatch):
    """Turning an app off is a legitimate choice; reporting it as degradation
    would make every workspace with one disabled app alarm forever (doctor
    reports it separately, as information rather than a problem)."""
    _no_redis(monkeypatch)
    rt = _Runtime(
        {"browser": _app("browser", config={"auto_start": False})},
        containers=_Containers({"browser": {"running": False}}),
    )
    assert health_report(rt)["status"] == "ok"


# --- the apps:changed fan-out, reported per worker (card 3fc5bf3b) -----------


def _runtime_with_lifecycle(health, apps=None):
    """A runtime whose FastAPI app carries an ``app_lifecycle``, the way
    ``register_apps_routes`` wires the real one."""
    rt = _Runtime(apps or {})
    rt.host = SimpleNamespace(
        state=SimpleNamespace(app_lifecycle=SimpleNamespace(
            broadcast_health=lambda: health)))
    return rt


def test_a_dead_relay_is_reported_at_more_than_one_worker(monkeypatch):
    """What had NO signal anywhere: a worker whose relay died kept answering
    200 ok while serving app config from before the last save."""
    _no_redis(monkeypatch)
    monkeypatch.setenv("AW_WORKSPACE_WORKERS", "5")
    rt = _runtime_with_lifecycle(
        {"started": True, "relay_alive": False, "relay_failures": 3})

    report = health_report(rt)
    assert report["status"] == "degraded"
    row = report["degraded"][0]
    # Health's projection drops `component`/`last_error`, so the row has to be
    # self-describing without them — a monitor gets kind+app+service+reason.
    assert row["kind"] == "broadcast"
    assert (row["app"], row["service"]) == ("core", "apps:changed")
    assert row["state"] == "off" and row["desired"] == "running"
    assert "reconnecting" in row["reason"]
    # ...and doctor's projection keeps the per-worker detail.
    assert "pid" in degradations(rt)[0]["last_error"]


def test_a_relay_that_never_subscribed_is_reported_too(monkeypatch):
    """``AppLifecycle.start`` returning False used to be a lone log.warning —
    a worker that boots with Redis briefly away then runs forever without any
    cross-worker convergence at all."""
    _no_redis(monkeypatch)
    monkeypatch.setenv("AW_WORKSPACE_WORKERS", "5")
    rt = _runtime_with_lifecycle(
        {"started": False, "relay_alive": False, "relay_failures": 0})
    assert "never subscribed" in health_report(rt)["degraded"][0]["reason"]


def test_a_healthy_relay_reports_nothing(monkeypatch):
    _no_redis(monkeypatch)
    monkeypatch.setenv("AW_WORKSPACE_WORKERS", "5")
    rt = _runtime_with_lifecycle(
        {"started": True, "relay_alive": True, "relay_failures": 2})
    assert health_report(rt) == {"status": "ok", "degraded": []}


def test_a_single_worker_never_reports_the_fan_out(monkeypatch):
    """The single-worker golden rule: with one worker there is nobody to
    converge with, the inline hook in ``save_app_config`` is the whole of
    correctness, and a dead broadcast costs nothing."""
    _no_redis(monkeypatch)
    monkeypatch.setenv("AW_WORKSPACE_WORKERS", "1")
    rt = _runtime_with_lifecycle(
        {"started": False, "relay_alive": False, "relay_failures": 0})
    assert health_report(rt) == {"status": "ok", "degraded": []}


def test_a_runtime_with_no_lifecycle_wired_reports_nothing(monkeypatch):
    """Every CLI/unit-test runtime is built without one (``AppRuntime`` alone,
    no ``register_apps_routes``), and must not become degraded by omission."""
    _no_redis(monkeypatch)
    monkeypatch.setenv("AW_WORKSPACE_WORKERS", "5")
    assert health_report(_Runtime({})) == {"status": "ok", "degraded": []}


def test_the_fan_out_row_stays_out_of_doctors_autostart_section(monkeypatch):
    """``_autostart_not_running`` is printed by a service-shaped formatter
    ("auto_start is on but X is off"); the fan-out row would read as an app
    nobody has. It is printed from doctor's ``redis`` section instead."""
    from src.apps.routes import _autostart_not_running

    _no_redis(monkeypatch)
    monkeypatch.setenv("AW_WORKSPACE_WORKERS", "5")
    rt = _runtime_with_lifecycle(
        {"started": True, "relay_alive": False, "relay_failures": 1})
    assert len(degradations(rt)) == 1
    assert _autostart_not_running(rt) == []


# --- the service-command relay, reported per worker (card 4045bf3b-9510-814a)


def _runtime_with_service_relay(health, apps=None):
    """A runtime whose FastAPI app carries a ``service_relay``, the way
    ``register_apps_routes`` wires the real one."""
    rt = _Runtime(apps or {})
    rt.host = SimpleNamespace(
        state=SimpleNamespace(service_relay=SimpleNamespace(
            relay_health=lambda: health)))
    return rt


def test_a_dead_service_relay_is_reported_at_more_than_one_worker(monkeypatch):
    """The exact gap card 4045bf3b-9510-814a found: a forwarded restart for
    research-search:paper-search always burned the full relay timeout and
    silently fell back to a local no-op — this is what makes that visible."""
    _no_redis(monkeypatch)
    monkeypatch.setenv("AW_WORKSPACE_WORKERS", "5")
    rt = _runtime_with_service_relay(
        {"started": True, "relay_alive": False, "relay_failures": 2})

    report = health_report(rt)
    assert report["status"] == "degraded"
    row = report["degraded"][0]
    assert row["kind"] == "broadcast"
    assert (row["app"], row["service"]) == ("core", "service-relay")
    assert row["state"] == "off" and row["desired"] == "running"
    assert "reconnecting" in row["reason"]
    assert "pid" in degradations(rt)[0]["last_error"]


def test_a_service_relay_that_never_subscribed_is_reported_too(monkeypatch):
    _no_redis(monkeypatch)
    monkeypatch.setenv("AW_WORKSPACE_WORKERS", "5")
    rt = _runtime_with_service_relay(
        {"started": False, "relay_alive": False, "relay_failures": 0})
    assert "never subscribed" in health_report(rt)["degraded"][0]["reason"]


def test_a_healthy_service_relay_reports_nothing(monkeypatch):
    _no_redis(monkeypatch)
    monkeypatch.setenv("AW_WORKSPACE_WORKERS", "5")
    rt = _runtime_with_service_relay(
        {"started": True, "relay_alive": True, "relay_failures": 1})
    assert health_report(rt) == {"status": "ok", "degraded": []}


def test_a_single_worker_never_reports_the_service_relay(monkeypatch):
    _no_redis(monkeypatch)
    monkeypatch.setenv("AW_WORKSPACE_WORKERS", "1")
    rt = _runtime_with_service_relay(
        {"started": False, "relay_alive": False, "relay_failures": 0})
    assert health_report(rt) == {"status": "ok", "degraded": []}


def test_a_runtime_with_no_service_relay_wired_reports_nothing(monkeypatch):
    """Every CLI/unit-test runtime built without ``register_apps_routes``
    must not become degraded by omission, same as the apps:changed fan-out."""
    _no_redis(monkeypatch)
    monkeypatch.setenv("AW_WORKSPACE_WORKERS", "5")
    assert health_report(_Runtime({})) == {"status": "ok", "degraded": []}


def test_a_dead_broadcast_and_a_dead_service_relay_are_both_reported(monkeypatch):
    """Two DIFFERENT RedisBroadcaster instances (AppLifecycle's and
    ServiceCommandRelay's own) can each be down independently — one must not
    mask the other."""
    _no_redis(monkeypatch)
    monkeypatch.setenv("AW_WORKSPACE_WORKERS", "5")
    rt = _Runtime({})
    rt.host = SimpleNamespace(state=SimpleNamespace(
        app_lifecycle=SimpleNamespace(
            broadcast_health=lambda: {"started": True, "relay_alive": False,
                                       "relay_failures": 1}),
        service_relay=SimpleNamespace(
            relay_health=lambda: {"started": True, "relay_alive": False,
                                   "relay_failures": 4}),
    ))
    report = health_report(rt)
    assert report["status"] == "degraded"
    services = {row["service"] for row in report["degraded"]}
    assert services == {"apps:changed", "service-relay"}


def test_no_runtime_yet_is_ok():
    """``/api/health`` answers before the apps framework has attached
    anything — and must, since that is when a boot is watched most closely."""
    assert health_report(None) == {"status": "ok", "degraded": []}


def test_a_broken_check_fails_open(monkeypatch):
    class _Exploding:
        def loaded_slugs(self):
            raise RuntimeError("boom")
    assert health_report(_Exploding()) == {"status": "ok", "degraded": []}


def test_the_cache_absorbs_a_polling_monitor(monkeypatch):
    """Container healthchecks and uptime monitors hit this every few
    seconds; the check is cheap but not free (a poll() per service plus two
    Redis round-trips), so it is computed at most once per TTL."""
    _no_redis(monkeypatch)
    calls = {"n": 0}

    class _Counting(_Runtime):
        def loaded_slugs(self):
            calls["n"] += 1
            return super().loaded_slugs()

    rt = _Counting({"proxy": _app("proxy", config={"auto_start": True}, tier="inprocess")},
                   services=_Services({("proxy", "proxy-server"): {"running": True}}))
    invalidate_cache()
    for _ in range(5):
        assert health_report_cached(rt)["status"] == "ok"
    assert calls["n"] == 1

    # A deliberate start/stop invalidates it, so a human who just clicked
    # Start is not handed a 5s-stale answer.
    invalidate_cache()
    assert health_report_cached(rt)["status"] == "ok"
    assert calls["n"] == 2
    invalidate_cache()


def test_doctor_and_health_read_the_same_check(monkeypatch):
    """One source of truth: the doctor rows and the health rows are two
    projections of the same call, so they can never disagree about whether
    something is running."""
    _no_redis(monkeypatch)
    rt = _Runtime(
        {"proxy": _app("proxy", config={"auto_start": True}, tier="inprocess")},
        services=_Services({
            ("proxy", "proxy-server"): {"running": False, "state": "backing_off",
                                        "last_exit_code": 1, "last_error": "boom"},
        }),
    )
    doctor_rows = degradations(rt)
    health_rows = health_report(rt)["degraded"]
    assert len(doctor_rows) == len(health_rows) == 1
    # doctor keeps the debugging detail; health keeps the status.
    assert doctor_rows[0]["component"] == "proxy:proxy-server"
    assert doctor_rows[0]["last_error"] == "boom"
    assert doctor_rows[0]["state"] == health_rows[0]["state"] == "backing_off"
