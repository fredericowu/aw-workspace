"""An app switched off in config must not read as an app that is fine.

``auto_start`` lives only in this workspace's stored config, so code review
tells you the opposite of the truth — the manifest says ``default: true``
while the app never comes up. aw-app-browser sat like that on 2026-08-23 and
the cookie sync that depended on it looked like broken code for a day.

The second half of this file covers the inverse and more dangerous gap:
``auto_start: true`` with nothing actually running. ``doctor`` never
cross-checked that against real process/container state, so this exact
condition recurred at least 15 times (2026-09-06 through 2026-09-29, across
aw-app-proxy, codegraphcontext and whatsapp) with ``doctor`` printing "No
silent degradation found" every single time — only ``status``'s COMPONENTS
table ever caught it. See finding_key
``aw-workspace-multiworker:proxy-app-auto-start-not-running``.
"""
from types import SimpleNamespace

from src.apps.containers import ContainerError
from src.apps.routes import _autostart_disabled, _autostart_not_running


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
        self._rows = rows or {}  # (app_id, service_id) -> status dict

    def registered(self):
        return list(self._rows)

    def status(self, app_id, service_id):
        return self._rows[(app_id, service_id)]


class _Containers:
    def __init__(self, rows=None, errors=None):
        self._rows = rows or {}  # app_id -> status dict
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


def test_an_app_with_autostart_off_is_reported():
    rt = _Runtime({"browser": _app("browser", config={"auto_start": False})})
    assert _autostart_disabled(rt) == [{"app": "browser", "tier": "container"}]


def test_an_app_that_starts_normally_is_not_reported():
    rt = _Runtime({"browser": _app("browser", config={"auto_start": True})})
    assert _autostart_disabled(rt) == []


def test_an_absent_key_means_the_schema_default_wins():
    """The manifest defaults auto_start to true, so a config that never
    mentions it starts on boot — reporting that would make the section noise
    in every healthy workspace."""
    rt = _Runtime({"browser": _app("browser", config={})})
    assert _autostart_disabled(rt) == []


def test_an_app_with_nothing_to_start_is_skipped():
    """Most in-process apps have no auto_start in their schema at all — they
    are part of the server process. Only apps that supervise a container or a
    service (aw-app-proxy's proxy-server) declare one."""
    rt = _Runtime({"kb": _app("kb", config={"auto_start": False},
                              schema_has_autostart=False, tier="inprocess")})
    assert _autostart_disabled(rt) == []


def test_a_slug_that_failed_to_load_does_not_break_the_report():
    rt = _Runtime({"ghost": None, "browser": _app("browser", config={"auto_start": False})})
    assert _autostart_disabled(rt) == [{"app": "browser", "tier": "container"}]


def test_an_auto_start_service_that_is_running_is_not_reported():
    rt = _Runtime(
        {"proxy": _app("proxy", config={"auto_start": True}, tier="inprocess")},
        services=_Services({("proxy", "proxy-server"): {"running": True}}),
    )
    assert _autostart_not_running(rt) == []


def test_an_auto_start_service_that_is_off_is_reported():
    """The exact 2026-10-01 incident: aw-app-proxy's proxy-server was left
    stopped after a manual test, auto_start stayed true, and nothing but a
    direct curl to :9124 ever noticed."""
    rt = _Runtime(
        {"proxy": _app("proxy", config={"auto_start": True}, tier="inprocess")},
        services=_Services({
            ("proxy", "proxy-server"): {
                "running": False, "last_exit_code": 0, "last_error": None,
            },
        }),
    )
    assert _autostart_not_running(rt) == [{
        "app": "proxy", "tier": "inprocess", "component": "proxy:proxy-server",
        "last_exit_code": 0, "last_error": None,
    }]


def test_an_auto_start_container_that_is_off_is_reported():
    rt = _Runtime(
        {"browser": _app("browser", config={"auto_start": True})},
        containers=_Containers({"browser": {"running": False}}),
    )
    assert _autostart_not_running(rt) == [
        {"app": "browser", "tier": "container", "component": "browser"},
    ]


def test_a_container_status_error_counts_as_not_running():
    """``containers.status`` raises ``ContainerError`` for a container the
    engine can't currently reach — that is NOT "fine", it's the same silent
    gap with a different exception instead of a clean ``running: False``."""
    rt = _Runtime(
        {"browser": _app("browser", config={"auto_start": True})},
        containers=_Containers(errors={"browser"}),
    )
    assert _autostart_not_running(rt) == [
        {"app": "browser", "tier": "container", "component": "browser"},
    ]


def test_an_app_with_autostart_off_is_not_reported_as_not_running():
    """Already covered by ``_autostart_disabled`` — don't double-report the
    same off app under two different sections."""
    rt = _Runtime(
        {"browser": _app("browser", config={"auto_start": False})},
        containers=_Containers({"browser": {"running": False}}),
    )
    assert _autostart_not_running(rt) == []


def test_an_app_with_nothing_to_start_is_not_checked_for_running():
    rt = _Runtime(
        {"kb": _app("kb", config={"auto_start": True},
                    schema_has_autostart=False, tier="inprocess")},
        containers=_Containers({"kb": {"running": False}}),
    )
    assert _autostart_not_running(rt) == []
