"""``runtime.env`` placeholder expansion — a container app's own config.

Without this a container app's ``config_schema`` is decorative: the user
fills a field in and nothing carries it into the container.
"""
import pytest

from src.apps.containers import expand_env


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
