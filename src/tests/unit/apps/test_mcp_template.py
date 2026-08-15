"""An app's MCP credential must survive an update — see src/apps/mcp_template.py.

The regression these guard is silent by construction: the gateway keeps
listing an upstream whose token went stale, it just serves zero tools.
"""
import json
import os

import pytest

from src.apps import mcp_template


TEMPLATE = {
    "mcpServers": {
        "home-assistant": {
            "enabled": True,
            "type": "http",
            "url": "http://aw-app-home-assistant:8123/api/mcp",
            "headers": {"Authorization": "Bearer ${config.mcp_token}"},
        }
    }
}


def write_template(pkg, doc=None):
    path = os.path.join(pkg, mcp_template.TEMPLATE_NAME)
    with open(path, "w") as f:
        json.dump(doc if doc is not None else TEMPLATE, f)
    return path


def read_output(pkg):
    with open(os.path.join(pkg, mcp_template.OUTPUT_NAME)) as f:
        return json.load(f)


def test_no_template_is_a_no_op(tmp_path):
    """Every app that writes mcp.json directly must keep working untouched."""
    pkg = str(tmp_path)
    with open(os.path.join(pkg, "mcp.json"), "w") as f:
        json.dump({"mcpServers": {"x": {"enabled": True}}}, f)

    assert mcp_template.render(pkg, {"mcp_token": "t"}, "app") is None
    # The app's own file is left exactly as it was.
    assert read_output(pkg) == {"mcpServers": {"x": {"enabled": True}}}


def test_config_value_is_rendered_into_the_output(tmp_path):
    pkg = str(tmp_path)
    write_template(pkg)

    mcp_template.render(pkg, {"mcp_token": "sekrit"}, "home-assistant")

    srv = read_output(pkg)["mcpServers"]["home-assistant"]
    assert srv["headers"]["Authorization"] == "Bearer sekrit"
    assert srv["enabled"] is True
    # Non-placeholder strings pass through untouched.
    assert srv["url"] == "http://aw-app-home-assistant:8123/api/mcp"


def test_an_update_regenerates_the_credential(tmp_path):
    """The actual bug: an update replaces the package dir, wiping mcp.json.

    Config lives outside the package (config_store), so the next activation
    must put the token back with nobody re-pasting it.
    """
    pkg = str(tmp_path)
    write_template(pkg)
    config = {"mcp_token": "sekrit"}
    mcp_template.render(pkg, config, "home-assistant")

    # Simulate `update`: package dir replaced by the new version's contents.
    os.unlink(os.path.join(pkg, mcp_template.OUTPUT_NAME))
    write_template(pkg)
    assert not os.path.exists(os.path.join(pkg, mcp_template.OUTPUT_NAME))

    mcp_template.render(pkg, config, "home-assistant")

    srv = read_output(pkg)["mcpServers"]["home-assistant"]
    assert srv["headers"]["Authorization"] == "Bearer sekrit"
    assert srv["enabled"] is True


def test_unconfigured_token_disables_the_server(tmp_path):
    """A literal ${config.x} in an auth header connects and 401s, serving zero
    tools — indistinguishable from a broken app. Report it as off instead."""
    pkg = str(tmp_path)
    write_template(pkg)

    mcp_template.render(pkg, {}, "home-assistant")

    srv = read_output(pkg)["mcpServers"]["home-assistant"]
    assert srv["enabled"] is False


def test_empty_string_token_also_disables(tmp_path):
    """A cleared settings field is 'not configured', not 'configured as ""'."""
    pkg = str(tmp_path)
    write_template(pkg)

    mcp_template.render(pkg, {"mcp_token": ""}, "home-assistant")

    assert read_output(pkg)["mcpServers"]["home-assistant"]["enabled"] is False


def test_one_unconfigured_server_does_not_disable_its_siblings(tmp_path):
    pkg = str(tmp_path)
    write_template(pkg, {
        "mcpServers": {
            "needs-token": {
                "enabled": True,
                "headers": {"Authorization": "Bearer ${config.mcp_token}"},
            },
            "no-credential": {"enabled": True, "url": "http://x:1/mcp"},
        }
    })

    mcp_template.render(pkg, {}, "app")

    servers = read_output(pkg)["mcpServers"]
    assert servers["needs-token"]["enabled"] is False
    assert servers["no-credential"]["enabled"] is True


def test_env_and_chained_sources_resolve(tmp_path, monkeypatch):
    """Same placeholder grammar as runtime.env, including `|` fallback."""
    monkeypatch.setenv("SOME_WORKSPACE_VAR", "from-env")
    pkg = str(tmp_path)
    write_template(pkg, {
        "mcpServers": {
            "s": {
                "enabled": True,
                "headers": {
                    "A": "${env.SOME_WORKSPACE_VAR}",
                    "B": "${config.missing|env.SOME_WORKSPACE_VAR}",
                    "C": "${config.present|env.SOME_WORKSPACE_VAR}",
                },
            }
        }
    })

    mcp_template.render(pkg, {"present": "from-config"}, "app")

    headers = read_output(pkg)["mcpServers"]["s"]["headers"]
    assert headers == {"A": "from-env", "B": "from-env", "C": "from-config"}


def test_nested_lists_are_walked(tmp_path):
    pkg = str(tmp_path)
    write_template(pkg, {
        "mcpServers": {
            "s": {"enabled": True, "args": ["--token", "${config.mcp_token}", "--x"]}
        }
    })

    mcp_template.render(pkg, {"mcp_token": "sekrit"}, "app")

    assert read_output(pkg)["mcpServers"]["s"]["args"] == ["--token", "sekrit", "--x"]


def test_rendered_file_is_not_world_readable(tmp_path):
    """It holds the credential the template existed to keep out of git."""
    pkg = str(tmp_path)
    write_template(pkg)

    mcp_template.render(pkg, {"mcp_token": "sekrit"}, "app")

    mode = oct(os.stat(os.path.join(pkg, mcp_template.OUTPUT_NAME)).st_mode)[-3:]
    assert mode == "600"


def test_a_broken_template_leaves_the_previous_output_alone(tmp_path):
    """Don't take a working upstream down because a new template is malformed."""
    pkg = str(tmp_path)
    write_template(pkg)
    mcp_template.render(pkg, {"mcp_token": "sekrit"}, "app")
    good = read_output(pkg)

    with open(os.path.join(pkg, mcp_template.TEMPLATE_NAME), "w") as f:
        f.write("{ not json")

    assert mcp_template.render(pkg, {"mcp_token": "sekrit"}, "app") is None
    assert read_output(pkg) == good


def test_render_is_idempotent(tmp_path):
    """It runs on every activation; nothing may drift pass over pass."""
    pkg = str(tmp_path)
    write_template(pkg)

    mcp_template.render(pkg, {"mcp_token": "sekrit"}, "app")
    first = read_output(pkg)
    mcp_template.render(pkg, {"mcp_token": "sekrit"}, "app")

    assert read_output(pkg) == first


@pytest.mark.parametrize("name", ["mcpServers", "servers"])
def test_a_template_without_the_expected_shape_still_renders(tmp_path, name):
    """Expansion must not depend on the doc having an mcpServers key — only
    the disable-on-unresolved step does."""
    pkg = str(tmp_path)
    write_template(pkg, {name: {"s": {"token": "${config.mcp_token}"}}})

    mcp_template.render(pkg, {"mcp_token": "sekrit"}, "app")

    assert read_output(pkg)[name]["s"]["token"] == "sekrit"
