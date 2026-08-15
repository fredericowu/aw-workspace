"""Durable per-app config snapshots that survive an uninstall.

Why this exists
---------------

An app's settings live in two places, and ``uninstall`` used to destroy both:

* the workspace's own ``AppInstall`` row (``Reconciler.local.forget``)
* the cloud registry row (``RegistryClient.delete_desired``) — the source of
  truth

So a delete + install — the routine way to force a rebuilt container image
onto an app whose code is baked in, and what an agent reaches for when
updating one — silently reset every setting a human had typed. What came
back was not the app's config but the *schema defaults*: every key with a
``default`` survived, every key without one vanished.

Found 2026-08-15 on aw-app-crispal, where it had been true since 14/08:
``openai_api_key``, ``db_dsn``, ``wp_mcp_token``, ``awserv_url``, ``ap_url``,
``ap_token``, ``ap_gallery_base``, ``ap_inject_secret`` and the four
``remote_*`` settings driving the Arvin device bridge were all gone. Nothing
reported it. The Android automation had been dead for a day and read exactly
like a code regression.

Frederico, 2026-08-15: *"eu quero que no delete, as configurações sejam
mantidas … o delete de uma app deve deixar o data/ dela e o settings"*.

The design
----------

The app's **data** already survives: ``$AW_APP_DATA`` resolves to
``workspace_home()/data/<app_id>``, which uninstall never touches (only the
installed *package* dir is removed — see ``runtime.py``'s ``$AW_APP_DATA``
docstring). Config is what had no such home, so it gets one right next to it:

    <workspace_home>/app-config/<app_id>.json

Same durable tree as ``bin/``, ``secrets/``, ``data/`` — a plain file, so
recovering a setting is ``cat``, not a database query. It is written on
every config save and once more at uninstall, and read back on install when
the incoming spec carries no config of its own.

Deliberately restore-on-empty rather than restore-always: an install that
*does* carry config (a cloud row, an explicit API payload) is stating an
intent, and a snapshot must not override it. Only the empty case — which is
precisely the "nobody remembered to pass it" case — falls back here.

These files can hold API keys, so they are written 0600 under a 0700
directory, like ``secrets/``.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
from typing import Any

from src.apps import paths

log = logging.getLogger(__name__)


def config_dir() -> str:
    d = os.path.join(paths.workspace_home(), "app-config")
    os.makedirs(d, mode=0o700, exist_ok=True)
    return d


def path(app_id: str) -> str:
    return os.path.join(config_dir(), f"{app_id}.json")


def save(app_id: str, config: dict[str, Any] | None) -> str | None:
    """Snapshot ``config`` for ``app_id``. Returns the file path, or None.

    An empty config writes nothing and — importantly — does NOT clear an
    existing snapshot. Otherwise the very reinstall this exists to survive
    would overwrite the good file with the empty config it booted with,
    one pass before anyone noticed.
    """
    if not config:
        return None
    target = path(app_id)
    try:
        fd, tmp = tempfile.mkstemp(dir=config_dir(), prefix=f".{app_id}.", suffix=".tmp")
        with os.fdopen(fd, "w") as f:
            json.dump(config, f, indent=2, sort_keys=True)
        os.chmod(tmp, 0o600)
        os.replace(tmp, target)  # atomic: a crash mid-write cannot truncate it
        return target
    except Exception:  # noqa: BLE001 — never fail an install/uninstall over this
        log.exception("apps: could not snapshot config for %s", app_id)
        return None


def load(app_id: str) -> dict[str, Any]:
    """The saved config for ``app_id``, or ``{}`` when there is none."""
    try:
        with open(path(app_id)) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception:  # noqa: BLE001 — a corrupt snapshot must not block a boot
        log.exception("apps: could not read the config snapshot for %s", app_id)
        return {}


def purge(app_id: str) -> bool:
    """Delete the snapshot. Only ever called when a caller asks explicitly —
    uninstall must not, that is the whole point of this module."""
    try:
        os.remove(path(app_id))
        return True
    except FileNotFoundError:
        return False


def list_saved() -> list[str]:
    try:
        return sorted(
            f[:-5] for f in os.listdir(config_dir())
            if f.endswith(".json") and not f.startswith(".")
        )
    except FileNotFoundError:
        return []
