"""An app switched off in config must not read as an app that is fine.

``auto_start`` lives only in this workspace's stored config, so code review
tells you the opposite of the truth — the manifest says ``default: true``
while the app never comes up. aw-app-browser sat like that on 2026-08-23 and
the cookie sync that depended on it looked like broken code for a day.
"""
from types import SimpleNamespace

from src.apps.routes import _autostart_disabled


def _app(slug, *, config, schema_has_autostart=True, tier="container"):
    props = {"auto_start": {"type": "boolean", "default": True}} if schema_has_autostart else {}
    return SimpleNamespace(
        config=config,
        manifest=SimpleNamespace(
            id=slug, tier=tier,
            effective_config_schema={"type": "object", "properties": props},
        ),
    )


class _Runtime:
    def __init__(self, apps):
        self._apps = apps

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
