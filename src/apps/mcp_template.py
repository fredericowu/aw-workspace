"""Render an app's ``mcp.json`` from a template + its saved config.

Why this exists
---------------

MCP Gateway discovers upstreams by scanning ``apps/<slug>/mcp.json`` — a
plain file inside the installed *package* dir. That works for an upstream
with nothing secret in it (aw-app-browser points at a CDP endpoint and needs
no credential), and breaks for every upstream that needs one:

* The package dir is **overwritten wholesale on every app update**, so a
  token hand-written into ``mcp.json`` survives exactly until the next
  version bump — and then the app's tools silently vanish from the gateway,
  with the upstream still listed and simply serving nothing.
* An app repo is public. A per-install token has nowhere to live in it, so
  the only options were "commit a credential" or "tell the user to edit a
  file after every update".
* A Tier-1 app can dodge this by writing its own ``mcp.json`` on activate
  (aw-app-notion does exactly that, from its secret store). **A Tier-2
  container app runs no workspace-side code at all**, so it had no such
  escape. That asymmetry is what this module removes.

Found 2026-08-15 while porting Home Assistant out of the monolith: HA's
``/api/mcp`` needs a long-lived token, the app is Tier-2, and the honest
instruction in its README was "re-paste the token after every update".

The design
----------

An app ships **``mcp.template.json``** instead of ``mcp.json``. On every
activation — and again on every config save — the runtime expands it into
``mcp.json`` next to it, using the same ``${config.x}`` / ``${env.X}`` /
``${app.url}`` grammar a manifest's ``runtime.env`` already speaks (see
``containers.expand_value``; one dialect, not two).

Because the template is versioned and the output is generated, an update
that replaces the package dir replaces the *template* — and the very next
activation regenerates ``mcp.json`` from config that lives somewhere else
entirely (``config_store``: ``<workspace_home>/app-config/<app_id>.json``,
which uninstall deliberately keeps). So the credential survives an
update, an uninstall/install, and a workspace redeploy without anyone
re-pasting anything.

**An unresolved placeholder disables that server rather than shipping a
broken one.** A gateway upstream configured with the literal string
``${config.mcp_token}`` in an Authorization header doesn't fail loudly; it
connects, gets 401, and serves zero tools — the exact silent-degradation
shape this workspace already loses time to. Better for the app to report
"not configured yet" and for ``doctor`` to be able to see it.
"""
from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import tempfile
from typing import Any

from src.apps.containers import expand_value

log = logging.getLogger(__name__)

TEMPLATE_NAME = "mcp.template.json"
OUTPUT_NAME = "mcp.json"

#: One ``${source.name}`` occurrence, anywhere inside a larger string.
#:
#: ``runtime.env`` placeholders are deliberately whole-value — an env var is
#: either a placeholder or a literal, and partial substitution there would
#: mangle a legitimate ``$`` in a value. A template is the opposite case: the
#: single most common thing an MCP upstream needs is
#: ``"Authorization": "Bearer ${config.mcp_token}"``, where the credential is
#: by definition embedded in a larger string. So this file interpolates, and
#: delegates each individual occurrence to the shared resolver so the two
#: dialects can never drift on what a source means.
_EMBEDDED = re.compile(r"\$\{(?:config|env|app)\.[A-Za-z_][A-Za-z0-9_.]*"
                       r"(?:\|(?:config|env|app)\.[A-Za-z_][A-Za-z0-9_.]*)*\}")


def template_path(package_dir: str) -> str:
    return os.path.join(package_dir, TEMPLATE_NAME)


def output_path(package_dir: str) -> str:
    return os.path.join(package_dir, OUTPUT_NAME)


def has_template(package_dir: str) -> bool:
    return os.path.isfile(template_path(package_dir))


def _expand(node: Any, config: dict[str, Any], app_id: str,
            unresolved: list[str], where: str = "") -> Any:
    """Walk the template, expanding every string leaf.

    Records the dotted path of each placeholder that resolved to nothing in
    ``unresolved`` so the caller can decide what to disable.
    """
    if isinstance(node, dict):
        return {k: _expand(v, config, app_id, unresolved, f"{where}.{k}" if where else k)
                for k, v in node.items()}
    if isinstance(node, list):
        return [_expand(v, config, app_id, unresolved, f"{where}[{i}]")
                for i, v in enumerate(node)]
    if isinstance(node, str):
        occurrences = _EMBEDDED.findall(node)
        if not occurrences:
            return node
        out = node
        for token in occurrences:
            value = expand_value(token, config, app_id)
            if value is None:
                unresolved.append(where)
                return node
            out = out.replace(token, value)
        return out
    return node


def _server_of(path: str) -> str | None:
    """``mcpServers.home-assistant.headers.Authorization`` -> ``home-assistant``."""
    parts = path.split(".")
    if len(parts) >= 2 and parts[0] == "mcpServers":
        return parts[1]
    return None


def render(package_dir: str, config: dict[str, Any] | None,
           app_id: str = "") -> dict | None:
    """Expand ``mcp.template.json`` and write ``mcp.json``. Returns the doc.

    No-op returning ``None`` when the app ships no template — every existing
    app that writes ``mcp.json`` directly keeps working untouched.
    """
    src = template_path(package_dir)
    if not os.path.isfile(src):
        return None

    try:
        with open(src, encoding="utf-8") as fh:
            doc = json.load(fh)
    except (OSError, ValueError):
        log.exception("apps: %s has an unreadable %s — leaving %s alone",
                      app_id, TEMPLATE_NAME, OUTPUT_NAME)
        return None

    unresolved: list[str] = []
    rendered = _expand(doc, config or {}, app_id, unresolved)

    # A server with an unresolved placeholder is not configured yet. Serving
    # it anyway means a connected upstream with zero tools, which reads as a
    # broken app rather than a blank field.
    servers = rendered.get("mcpServers")
    if isinstance(servers, dict):
        for path in unresolved:
            name = _server_of(path)
            if name and isinstance(servers.get(name), dict):
                if servers[name].get("enabled"):
                    log.warning(
                        "apps: %s MCP upstream %r disabled — %s is not configured",
                        app_id, name, path)
                servers[name]["enabled"] = False
        if not unresolved:
            log.info("apps: %s rendered %s (%d upstream(s))",
                     app_id, OUTPUT_NAME, len(servers))

    _write(output_path(package_dir), rendered)
    return rendered


def _write(path: str, doc: dict) -> None:
    """Atomic write, 0600 — a rendered file can hold the very credential the
    template existed to keep out of git."""
    d = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".mcp-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, indent=2)
            fh.write("\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except Exception:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
