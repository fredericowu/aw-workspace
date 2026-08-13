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


# --- saving is incremental ---------------------------------------------------

from src.apps.routes import _merge_config  # noqa: E402


def test_a_partial_save_keeps_the_keys_it_did_not_mention():
    """The failure this fixes: writing four new crispal keys erased the four
    already there, and the gallery went back to "connection refused"."""
    previous = {"ap_token": "a", "ap_gallery_base": "b", "wp_mcp_url": "c"}
    out = _merge_config(previous, {"remote_token": "z"})
    assert out == {"ap_token": "a", "ap_gallery_base": "b",
                   "wp_mcp_url": "c", "remote_token": "z"}


def test_an_incoming_value_overrides_the_stored_one():
    assert _merge_config({"k": "old"}, {"k": "new"}) == {"k": "new"}


def test_an_explicit_null_still_removes_a_key():
    """Merging must not make clearing a setting impossible."""
    assert _merge_config({"k": "v", "j": "w"}, {"k": None}) == {"j": "w"}


def test_removing_a_key_that_is_not_there_is_not_an_error():
    assert _merge_config({"j": "w"}, {"k": None}) == {"j": "w"}


def test_a_full_form_post_behaves_exactly_as_before():
    """A UI that sends every key it manages is unaffected by the change."""
    previous = {"a": "1", "b": "2"}
    assert _merge_config(previous, {"a": "x", "b": "y"}) == {"a": "x", "b": "y"}


def test_an_empty_save_changes_nothing():
    assert _merge_config({"a": "1"}, {}) == {"a": "1"}
