"""Render an app's ``gateway-profiles.json`` from its manifest.

Why this exists
---------------

A manifest has always been able to *reference* one of the MCP Gateway's
scoped profiles — ``contributes.agents[*].mcp_servers[*]`` accepts
``{"name","server","profile"}`` (``manifest._validate_mcp_references``) — and
had no way to *define* one. The profile itself existed only in the gateway's
own ``gateway.json``, hand-written through ``PUT /admin/configs``: in no
repository, reviewed by nobody, not reproducible on a fresh workspace, and
erased by any reset of that data dir.

A reference to a profile nobody created is not an error anywhere. The gateway
answers ``404 {"error":"No such config: <name>"}`` per request and the agent
starts with **zero tools**, silently — live on 2026-09-21 for
``aw-app-marketing``'s ``marketing`` profile. Whoever references, declares.

The design
----------

An app declares ``contributes.mcp.profiles`` (``{name: spec}``) and this
module writes it, verbatim, to ``apps/<id>/gateway-profiles.json`` next to
the ``mcp.json`` the gateway already scans — the same transport
``mcp_template.py`` uses for upstreams, for the same reason: the contract
between an app and the gateway is a file name, not a schema the gateway has
to learn.

Two deliberate differences from ``mcp_template.py``:

* **No ``${config.x}`` / ``${env.X}`` expansion.** A profile carries no
  credential — its keys are fixed, scalar or lists of globs (see
  ``manifest.GATEWAY_PROFILE_KEYS``). This is a literal copy, not a template,
  and importing ``expand_value`` here would invent a way for one to hold a
  secret.
* **The output is deleted when the manifest declares none.** An app that
  drops its profiles in a new version must not leave the old ones live: the
  package dir is replaced on update, but a stale generated file in it would
  keep being scanned and keep serving the scope that was removed.

Nothing else records ownership. An uninstalled app's package dir is gone, so
its profiles are gone from the next scan — a pure function of what is on
disk, with no ``seeded_state`` to drift out of sync with it.
"""
from __future__ import annotations

import contextlib
import json
import logging
import os
import tempfile
from typing import Any

log = logging.getLogger(__name__)

OUTPUT_NAME = "gateway-profiles.json"


def output_path(package_dir: str) -> str:
    return os.path.join(package_dir, OUTPUT_NAME)


def render(package_dir: str, manifest: Any) -> bool:
    """Write ``gateway-profiles.json``, or remove it. Returns whether it exists.

    ``manifest`` is an ``AppManifest`` — only ``gateway_profiles`` and ``id``
    are read.
    """
    profiles = manifest.gateway_profiles
    path = output_path(package_dir)

    if not profiles:
        if os.path.exists(path):
            with contextlib.suppress(OSError):
                os.unlink(path)
            log.info("apps: %s declares no gateway profiles — removed %s",
                     manifest.id, OUTPUT_NAME)
        return False

    _write(path, {"profiles": profiles})
    log.info("apps: %s rendered %s (%s)", manifest.id, OUTPUT_NAME,
             ", ".join(sorted(profiles)))
    return True


def _write(path: str, doc: dict) -> None:
    """Atomic write. World-readable, unlike ``mcp_template``'s 0600: a profile
    holds no credential, and the gateway reads this file as another user from
    its own container."""
    d = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".gateway-profiles-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, indent=2)
            fh.write("\n")
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except Exception:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
