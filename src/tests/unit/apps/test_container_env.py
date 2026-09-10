"""``runtime.env`` placeholder expansion — a container app's own config.

Without this a container app's ``config_schema`` is decorative: the user
fills a field in and nothing carries it into the container.
"""
import os

import pytest

from src.apps.containers import app_data_owner, expand_env


def test_config_placeholder_is_resolved():
    env = expand_env({"CRISPAL_DB_DSN": "${config.db_dsn}"},
                     {"db_dsn": "host=db port=5432"})
    assert env == {"CRISPAL_DB_DSN": "host=db port=5432"}


def test_env_placeholder_reads_the_workspace_process(monkeypatch):
    monkeypatch.setenv("AW_BACKEND_URL", "https://backend.example")
    env = expand_env({"BACKEND": "${env.AW_BACKEND_URL}"}, {})
    assert env == {"BACKEND": "https://backend.example"}


def test_literal_values_pass_through_untouched():
    env = expand_env({"PORT": "9410", "MODE": "prod"}, {})
    assert env == {"PORT": "9410", "MODE": "prod"}


def test_non_string_values_are_stringified():
    assert expand_env({"PORT": 9410, "DEBUG": True}, {}) == {"PORT": "9410", "DEBUG": "True"}


@pytest.mark.parametrize("config", [{}, {"db_dsn": ""}, {"db_dsn": None}])
def test_unresolved_placeholder_drops_the_variable(config):
    """Absent, not empty. An image sets its own ENV defaults, and injecting
    FOO="" would override a working default with nothing."""
    assert expand_env({"CRISPAL_DB_DSN": "${config.db_dsn}"}, config) == {}


def test_unset_env_placeholder_drops_the_variable(monkeypatch):
    monkeypatch.delenv("SOME_UNSET_VAR", raising=False)
    assert expand_env({"X": "${env.SOME_UNSET_VAR}"}, {}) == {}


def test_a_dollar_inside_a_value_is_not_mangled():
    """Placeholders are whole-value only, so a literal $ is safe — passwords
    and DSNs contain them."""
    env = expand_env({"PW": "p$$w0rd${notaplaceholder}"}, {})
    assert env == {"PW": "p$$w0rd${notaplaceholder}"}


def test_partial_interpolation_is_not_attempted():
    # "prefix-${config.x}" is left alone rather than half-substituted: a
    # surprising half-value is worse than an obviously-literal one.
    env = expand_env({"X": "prefix-${config.db_dsn}"}, {"db_dsn": "v"})
    assert env == {"X": "prefix-${config.db_dsn}"}


def test_unknown_placeholder_namespace_is_left_literal():
    assert expand_env({"X": "${secret.foo}"}, {}) == {"X": "${secret.foo}"}


def test_mixed_manifest_resolves_each_key_independently(monkeypatch):
    monkeypatch.setenv("AW_WORKSPACE", "aw")
    env = expand_env(
        {
            "CRISPAL_PORT": "9410",
            "REMOTE_AGENT_URL": "${config.remote_agent_url}",
            "CRISPAL_DB_DSN": "${config.db_dsn}",
            "WS": "${env.AW_WORKSPACE}",
        },
        {"remote_agent_url": "https://ap.example"},
    )
    assert env == {
        "CRISPAL_PORT": "9410",
        "REMOTE_AGENT_URL": "https://ap.example",
        "WS": "aw",
    }


# --- ${app.url} and source chaining ------------------------------------------
# A URL containing the workspace slug can have no sensible manifest default, so
# it is derived rather than stored — see containers.app_public_url.


def test_app_url_is_composed_from_the_workspace_slug(monkeypatch):
    monkeypatch.setenv("AW_WORKSPACE", "aw")
    monkeypatch.setenv("AW_WORKSPACE_BASE_DOMAIN", "workspace.aw.tekflox.com")
    assert expand_env({"SITE": "${app.url}"}, {}, "crispal") == {
        "SITE": "https://crispal.app.aw.workspace.aw.tekflox.com"}


def test_app_url_is_dropped_when_the_workspace_slug_is_unknown(monkeypatch):
    monkeypatch.delenv("AW_WORKSPACE", raising=False)
    assert expand_env({"SITE": "${app.url}"}, {}, "crispal") == {}


def test_an_explicit_config_value_wins_over_the_derived_url(monkeypatch):
    monkeypatch.setenv("AW_WORKSPACE", "aw")
    monkeypatch.setenv("AW_WORKSPACE_BASE_DOMAIN", "workspace.aw.tekflox.com")
    env = expand_env({"SITE": "${config.site_url|app.url}"},
                     {"site_url": "https://sapatariacrispal.com"}, "crispal")
    assert env == {"SITE": "https://sapatariacrispal.com"}


def test_the_chain_falls_through_an_empty_config_value(monkeypatch):
    monkeypatch.setenv("AW_WORKSPACE", "aw")
    monkeypatch.setenv("AW_WORKSPACE_BASE_DOMAIN", "workspace.aw.tekflox.com")
    env = expand_env({"SITE": "${config.site_url|app.url}"}, {"site_url": ""}, "crispal")
    assert env == {"SITE": "https://crispal.app.aw.workspace.aw.tekflox.com"}


def test_a_chain_with_nothing_resolvable_drops_the_variable(monkeypatch):
    monkeypatch.delenv("AW_WORKSPACE", raising=False)
    assert expand_env({"SITE": "${config.nope|app.url}"}, {}, "crispal") == {}


# --- ${data.uid} / ${data.gid} ------------------------------------------------
# The host uid owning an app's $AW_APP_DATA is rewritten by the recursive chown
# the workspace redeploy applies to its whole bind mount, so a manifest cannot
# name it as a literal — see containers.app_data_owner.


@pytest.fixture()
def app_data(tmp_path, monkeypatch):
    """A workspace home whose ``data/<app_id>`` dir exists, like a reinstall."""
    monkeypatch.setenv("AW_WORKSPACE_HOME", str(tmp_path))
    (tmp_path / "data" / "blender").mkdir(parents=True)
    return tmp_path


def test_data_uid_and_gid_resolve_to_the_app_data_dirs_owner(app_data):
    (app_data / "data" / "blender" / "prefs").write_text("")
    st = os.stat(app_data / "data" / "blender" / "prefs")
    assert expand_env({"PUID": "${data.uid}", "PGID": "${data.gid}"}, {}, "blender") == {
        "PUID": str(st.st_uid), "PGID": str(st.st_gid)}


def _fake_dir(tmp_path, monkeypatch, *, dir_owner, child_owners):
    """A data dir whose mount point and contents have DIFFERENT owners.

    chown needs root, so ownership is faked at the stat boundary rather than
    on disk — the real bug is entirely about which path gets stat'ed.
    """
    monkeypatch.setenv("AW_WORKSPACE_HOME", str(tmp_path))
    data_dir = tmp_path / "data" / "blender"
    data_dir.mkdir(parents=True)
    for i, _owner in enumerate(child_owners):
        (data_dir / f"entry{i}").write_text("")

    owners = {str(data_dir): dir_owner}
    owners.update({str(data_dir / f"entry{i}"): o for i, o in enumerate(child_owners)})

    class FakeStat:
        def __init__(self, uid, gid):
            self.st_uid, self.st_gid = uid, gid

    real_stat, real_scandir = os.stat, os.scandir

    def fake_stat(path, *a, **kw):
        return FakeStat(*owners[str(path)]) if str(path) in owners else real_stat(path, *a, **kw)

    def fake_scandir(path, *a, **kw):
        # Sorted, unlike the real scandir: readdir order is arbitrary, which
        # would let a first-found implementation pass or fail by luck.
        with real_scandir(path, *a, **kw) as it:
            paths = sorted(e.path for e in it)

        class Wrapper:
            def __enter__(self): return self
            def __exit__(self, *e): return False
            def __iter__(self):
                for p in paths:
                    yield type("E", (), {
                        "path": p,
                        "stat": lambda s=None, follow_symlinks=True, p=p: fake_stat(p),
                    })()
        return Wrapper()

    monkeypatch.setattr(os, "stat", fake_stat)
    monkeypatch.setattr(os, "scandir", fake_scandir)


def test_the_mount_points_own_owner_is_ignored_in_favour_of_the_contents(
        tmp_path, monkeypatch):
    """The 2026-09-10 regression, caught in live verification and not by the
    original tests.

    A linuxserver image chowns its /config mount point to PUID:PGID on every
    boot, so the top-level directory reports back the uid the container was
    last started with. Statting it re-derives the stale value forever: after
    the sweep left blender's 418 entries at 1001, the mount point had already
    been chowned back to 1000, so PUID resolved to 1000 and every file the
    desktop needed stayed unreadable.
    """
    _fake_dir(tmp_path, monkeypatch, dir_owner=(1000, 1000),
              child_owners=[(1001, 1001)] * 4)
    assert app_data_owner("blender") == (1001, 1001)


def test_a_minority_of_boot_chowned_entries_does_not_win(tmp_path, monkeypatch):
    """A boot chowns the few paths it manages (/config/.config, /config/.XDG)
    to abc and leaves the rest — so first-found would pick the wrong uid
    depending on readdir order. The majority owner is the one to match."""
    _fake_dir(tmp_path, monkeypatch, dir_owner=(1000, 1000),
              child_owners=[(1000, 1000), (1001, 1001), (1001, 1001), (1001, 1001)])
    assert app_data_owner("blender") == (1001, 1001)


def test_a_tie_resolves_to_the_lowest_uid_not_an_arbitrary_one(tmp_path, monkeypatch):
    """Stability matters more than which side wins: an arbitrary winner would
    flip PUID between restarts, recreating the drift it exists to prevent."""
    _fake_dir(tmp_path, monkeypatch, dir_owner=(0, 0),
              child_owners=[(1001, 1001), (1000, 1000)])
    assert app_data_owner("blender") == (1000, 1000)
    _fake_dir(tmp_path / "b", monkeypatch, dir_owner=(0, 0),
              child_owners=[(1000, 1000), (1001, 1001)])
    assert app_data_owner("blender") == (1000, 1000)


def test_an_empty_data_dir_falls_back_to_its_own_owner(app_data):
    """A first install has no contents yet, so there is nothing to take a
    majority of — the directory itself is then the best signal."""
    assert app_data_owner("blender") == (
        os.stat(app_data / "data" / "blender").st_uid,
        os.stat(app_data / "data" / "blender").st_gid)


def test_data_uid_falls_back_to_an_ancestor_before_the_dir_exists(tmp_path, monkeypatch):
    """expand_env runs BEFORE _container_volumes makedirs the app's own data
    dir, so a first install must still resolve. The ancestors sit in the same
    swept mount and carry the same ownership.

    Asserts the probe SEQUENCE rather than the returned uid: every candidate
    path here is owned by the test process, so comparing uids alone passes
    even if the walk is deleted.
    """
    monkeypatch.setenv("AW_WORKSPACE_HOME", str(tmp_path))
    probed: list[str] = []
    real_stat = os.stat

    def spy(path, *args, **kwargs):
        if str(path).startswith(str(tmp_path)):
            probed.append(str(path))
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(os, "stat", spy)
    assert app_data_owner("never-installed") == (
        real_stat(tmp_path).st_uid, real_stat(tmp_path).st_gid)
    assert probed == [
        str(tmp_path / "data" / "never-installed"),
        str(tmp_path / "data"),
        str(tmp_path),
    ]


def test_data_uid_falls_back_to_the_process_uid_when_nothing_can_be_stat_ed(
        tmp_path, monkeypatch):
    """Never drop PUID: a linuxserver image with no PUID runs as 911, which
    owns nothing — a worse failure than a stale guess."""
    monkeypatch.setenv("AW_WORKSPACE_HOME", str(tmp_path / "does-not-exist"))
    assert app_data_owner("blender") == (os.getuid(), os.getgid())
    assert expand_env({"PUID": "${data.uid}"}, {}, "blender") == {"PUID": str(os.getuid())}


def test_expansion_does_not_create_the_workspace_home(tmp_path, monkeypatch):
    """Resolving a placeholder must not have filesystem side effects — it runs
    on every container start, including for apps that own no data dir."""
    home = tmp_path / "not-yet"
    monkeypatch.setenv("AW_WORKSPACE_HOME", str(home))
    expand_env({"PUID": "${data.uid}"}, {}, "blender")
    assert not home.exists()


def test_a_typo_d_data_key_drops_the_variable(app_data):
    """The 911 hazard, made explicit: ${data.owner} is a well-formed
    placeholder in an unknown key, so it resolves to nothing and PUID never
    reaches the container. Guarded by this test, not by review."""
    assert expand_env({"PUID": "${data.owner}"}, {}, "blender") == {}


def test_a_typo_d_data_namespace_is_left_literal(app_data):
    assert expand_env({"PUID": "${dat.uid}"}, {}, "blender") == {"PUID": "${dat.uid}"}


def test_a_config_override_wins_over_the_derived_owner(app_data):
    """Chaining works here too, so a host that genuinely needs a fixed uid can
    pin one without every install storing it."""
    assert expand_env({"PUID": "${config.puid|data.uid}"}, {"puid": "1234"},
                      "blender") == {"PUID": "1234"}


def test_the_chain_falls_through_to_the_derived_owner(app_data):
    st = os.stat(app_data / "data" / "blender")
    assert expand_env({"PUID": "${config.puid|data.uid}"}, {"puid": ""},
                      "blender") == {"PUID": str(st.st_uid)}


def test_a_root_owned_data_dir_still_yields_a_value(app_data, monkeypatch):
    """uid 0 is falsy — it must not be mistaken for "unresolved" and dropped."""
    monkeypatch.setattr(
        "src.apps.containers.app_data_owner", lambda app_id: (0, 0))
    assert expand_env({"PUID": "${data.uid}"}, {}, "blender") == {"PUID": "0"}
