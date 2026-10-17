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
import uuid
from typing import Any

from src.apps import paths

log = logging.getLogger(__name__)

#: The config-generation token (see :func:`bump_generation`). A dotfile so
#: :func:`list_saved`'s ``not f.startswith(".")`` already excludes it, and it
#: carries no ``.json`` suffix either — it is not an app's snapshot.
GENERATION_NAME = ".generation"


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


# ---- the config-generation token (the cross-worker propagation floor) -------
#
# Why a file next to the snapshots. A config save has to reach every one of
# AW_WORKSPACE_WORKERS uvicorn processes, and the push that does that
# (``apps:changed`` over Redis — ``src/apps/lifecycle.py``) has two ways to
# die silently, both of which have actually happened: a wedged pending-
# broadcast flag and a relay task killed by a Redis blip. Both are fixed, and
# a fix for the push is still a fix for *a* push.
#
# So this is the floor underneath it: one token, bumped at the save site, read
# at the request boundary (``src/api/app.py``'s generation middleware). Every
# worker is a process in ONE container sharing ONE filesystem, so the file is
# visible to all of them with no transport at all — which is the point: with
# Redis dead, propagation degrades to "slightly later, on the next
# /api/apps/* request this worker serves", never to "wrong answers until a
# restart". See docs/design/app-config-cross-worker-propagation.md.


def generation_path() -> str:
    """The token's path, resolved WITHOUT :func:`config_dir`'s ``makedirs``.

    This is read on (almost) every ``/api/apps/*`` request, and a ``mkdir``
    syscall per request to create a directory that exists is exactly the kind
    of cost a floor is not allowed to have."""
    return os.path.join(paths.workspace_home_path(), "app-config", GENERATION_NAME)


def bump_generation() -> str | None:
    """Write a fresh token, atomically. Returns it, or ``None`` on failure.

    Called from ``routes.save_app_config`` only, deliberately **not** from
    :func:`save` — that one also runs on the uninstall-snapshot path, where
    there is no new config for anyone to converge to.

    Same ``mkstemp`` + ``os.replace`` as :func:`save` for the same reason: a
    reader must see either the old token or the new one, never a half-written
    file. Never raises — a save must not fail because its floor could not be
    stamped (the broadcast is still the primary path).
    """
    token = uuid.uuid4().hex
    try:
        fd, tmp = tempfile.mkstemp(dir=config_dir(), prefix=f"{GENERATION_NAME}.",
                                   suffix=".tmp")
        with os.fdopen(fd, "w") as f:
            f.write(token)
        os.chmod(tmp, 0o600)
        os.replace(tmp, generation_path())
        return token
    except Exception:  # noqa: BLE001 — never fail a config save over this
        log.exception("apps: could not bump the config generation token")
        return None


def generation_mtime_ns() -> int | None:
    """``st_mtime_ns`` of the token, or ``None`` when there is no token.

    One ``os.stat`` — the per-request half of the floor. ``None`` means no
    config has ever been saved in this workspace (a fresh install), which is
    nothing to converge to, so callers skip.
    """
    try:
        return os.stat(generation_path()).st_mtime_ns
    except OSError:
        return None


def read_generation() -> tuple[int, str] | None:
    """``(st_mtime_ns, token)``, or ``None`` when there is no readable token.

    Paid only when :func:`generation_mtime_ns` says the mtime moved. The token
    is what callers must compare on — ``st_mtime_ns`` is the cheap *hint* that
    something may have happened, and it can move without the content changing
    (a restore, a copy) or move backwards (which is why its consumers compare
    with ``!=``, never ``>``).
    """
    try:
        st = os.stat(generation_path())
        with open(generation_path()) as f:
            return st.st_mtime_ns, f.read().strip()
    except OSError:
        return None


def list_saved() -> list[str]:
    try:
        return sorted(
            f[:-5] for f in os.listdir(config_dir())
            if f.endswith(".json") and not f.startswith(".")
        )
    except FileNotFoundError:
        return []
