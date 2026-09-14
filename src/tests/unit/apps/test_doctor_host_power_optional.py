"""``doctor``'s ``host_power.unmet_optional`` — the only place an app's
``runtime.host_power_optional`` request that this host didn't grant is
visible at all. Unlike ``runtime.host_power``, the app still loads (see
``hostpower.resolve_optional``), so without this section a host with no
GPU degrades in a way nobody could tell apart from "GPU acceleration was
never requested" — exactly the silent degradation this workspace's own
``doctor`` exists to rule out.
"""
from types import SimpleNamespace

from src.apps.routes import _unmet_optional_host_power


def _app(slug, *, host_power_optional=()):
    return SimpleNamespace(
        manifest=SimpleNamespace(id=slug, host_power_optional=list(host_power_optional)),
    )


class _Runtime:
    def __init__(self, apps):
        self._apps = apps

    def loaded_slugs(self):
        return list(self._apps)

    def get(self, slug):
        return self._apps.get(slug)


def test_unmet_request_is_reported():
    rt = _Runtime({"browser": _app("browser", host_power_optional=["gpu"])})
    assert _unmet_optional_host_power(rt, host_offers=()) == [
        {"app": "browser", "grant": "gpu"}
    ]


def test_met_request_is_not_reported():
    rt = _Runtime({"browser": _app("browser", host_power_optional=["gpu"])})
    assert _unmet_optional_host_power(rt, host_offers=("gpu",)) == []


def test_an_app_with_no_optional_request_is_skipped():
    rt = _Runtime({"browser": _app("browser")})
    assert _unmet_optional_host_power(rt, host_offers=()) == []


def test_host_privileged_covers_an_optional_request_too():
    """Same ceiling resolve_optional() applies at load time: a host that
    granted `privileged` satisfies any narrower optional request."""
    rt = _Runtime({"browser": _app("browser", host_power_optional=["gpu"])})
    assert _unmet_optional_host_power(rt, host_offers=("privileged",)) == []


def test_a_slug_that_failed_to_load_does_not_break_the_report():
    rt = _Runtime({
        "ghost": None,
        "browser": _app("browser", host_power_optional=["gpu"]),
    })
    assert _unmet_optional_host_power(rt, host_offers=()) == [
        {"app": "browser", "grant": "gpu"}
    ]
