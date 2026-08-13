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
