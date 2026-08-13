"""An install must never be a way to lose an app's configuration."""
from types import SimpleNamespace

from src.apps.routes import _config_for_install


class _Runtime:
    def __init__(self, cfg=None):
        self._cfg = cfg

    def get(self, slug):
        return SimpleNamespace(config=self._cfg) if self._cfg is not None else None


class _Reconciler:
    def __init__(self, rows=()):
        self.local = SimpleNamespace(list=lambda: list(rows))


def test_an_explicit_config_in_the_request_wins():
    out = _config_for_install("crispal", {"config": {"a": 1}},
                              _Runtime({"b": 2}), _Reconciler())
    assert out == {"a": 1}


def test_a_request_with_no_config_inherits_the_running_app_s():
    out = _config_for_install("crispal", {}, _Runtime({"b": 2}), _Reconciler())
    assert out == {"b": 2}


def test_it_falls_back_to_the_mirror_when_nothing_is_loaded():
    """The case that bit: a failed fetch left the app uninstalled, so nothing
    was loaded, and `marketplace install` (which sends no config) wiped
    ap_gallery_base/ap_token from the cloud registry too."""
    rows = [{"app_id": "crispal", "config": {"ap_token": "x"}}]
    out = _config_for_install("crispal", {}, _Runtime(None), _Reconciler(rows))
    assert out == {"ap_token": "x"}


def test_a_genuinely_new_app_starts_empty():
    assert _config_for_install("brand-new", {}, _Runtime(None), _Reconciler()) == {}


def test_a_broken_mirror_does_not_break_the_install():
    class _Boom:
        local = SimpleNamespace(list=lambda: (_ for _ in ()).throw(RuntimeError("nope")))
    assert _config_for_install("crispal", {}, _Runtime(None), _Boom()) == {}
