"""Extra skill-source directories an app contributes at sync time.

``contributes.skills`` is a **push**: on activate the apps framework copies
each declared ``SKILL.md`` dir into ``skills/`` once (``src/apps/skills.py``).
That is right for skills an app ships in its own package — they only change
when the app updates.

It is wrong for skills that appear *after* install. ``aw-autoskill`` writes a
new skill into the tenant's own storage every night; a push at activate can
never see them. This module is the **pull** half: a Tier-1 plugin may
implement :meth:`Plugin.list_skill_sources` and name directories that
``materialize()`` should scan on every sync.

Why a registry file instead of calling the plugin from ``materialize()``:
``aw-workspace-cli agent sync`` runs in its own process with no apps loaded,
and it must produce the same ``skills/`` as the server's boot sync. So the
hook runs where plugins live (right after ``activate``) and records its answer
here; ``materialize`` reads the recorded *directories* and scans their
*contents* fresh every time. Content stays dynamic, discovery stays available
to both processes, and — the load-bearing part — the destructive delete pass
never depends on a plugin (or a network call behind one) being reachable at
the moment it runs.

The hook's return value carries three states, because the delete pass is
destructive and "I have nothing" must not be confused with "I could not
answer":

* ``{"ok": True, "dirs": [...]}`` — authoritative. Entries under these dirs
  are synced in, and entries this app previously sourced that are no longer
  there get deleted.
* ``{"ok": False}`` — unavailable. The recorded entry is left as it was and
  ``materialize`` skips this app's delete pass entirely, so a transient
  failure cannot wipe a tenant's accumulated skills out of every mirror.
* ``None`` / not implemented — the app contributes no sources at all.

Entries materialized this way carry :data:`SOURCE_MARKER` naming the app, on
top of the ``.aw-app-id`` marker every app-owned entry carries. The extra
marker is what keeps the two halves apart: ``materialize`` may delete a stale
*sourced* entry, but must never touch a *pushed* one — that one is the
uninstall journal's to remove.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any

from src.apps import paths

log = logging.getLogger(__name__)

#: Written beside a materialized entry, naming the app whose source dir it
#: came from. Distinct from ``.aw-app-id`` on purpose — see module docstring.
SOURCE_MARKER = ".aw-skill-source"


def registry_path() -> str:
    """Where the recorded sources live — durable, tenant-scoped state."""
    return os.path.join(paths.workspace_home(), "skill-sources.json")


def read_registry() -> dict[str, dict[str, Any]]:
    """``{app_id: {"dirs": [...]}}`` for every app that reported sources.

    A missing or corrupt file reads as empty rather than raising: this is
    consulted from the sync path, and the sync path failing outright is worse
    than it running with the native half only.
    """
    path = registry_path()
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("skill_sources: unreadable registry %s: %s", path, exc)
        return {}
    if not isinstance(data, dict):
        return {}
    return {
        str(app_id): entry
        for app_id, entry in data.items()
        if isinstance(entry, dict) and isinstance(entry.get("dirs"), list)
    }


def _write_registry(registry: dict[str, dict[str, Any]]) -> None:
    path = registry_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(registry, fh, indent=2, sort_keys=True)
    os.replace(tmp, path)


def forget(app_id: str) -> None:
    """Drop an app's recorded sources (uninstall / deactivate).

    Its materialized entries are then unowned by any live source, and the next
    ``materialize`` deletes them — the exact-mirror rule that stops an
    uninstalled app's skills from teaching agents about tools that are gone.
    """
    registry = read_registry()
    if registry.pop(app_id, None) is not None:
        _write_registry(registry)


def record(app_id: str, result: Any) -> None:
    """Persist one app's :meth:`list_skill_sources` answer.

    ``ok: False`` deliberately writes nothing — the previous answer stands, so
    an app that is briefly unable to enumerate its skills does not lose them.
    """
    if result is None:
        forget(app_id)
        return
    if not isinstance(result, dict):
        log.warning("skill_sources: %s returned %r, expected a dict", app_id, type(result))
        return
    if not result.get("ok", False):
        log.info("skill_sources: %s reported unavailable; keeping its last known sources", app_id)
        return

    dirs = [str(d) for d in (result.get("dirs") or []) if isinstance(d, (str, os.PathLike))]
    registry = read_registry()
    registry[app_id] = {"dirs": dirs}
    _write_registry(registry)


async def refresh(app_id: str, plugin: Any, ctx: Any) -> None:
    """Call an app's hook, if it has one, and record the answer.

    Never raises: a plugin that throws here must not fail the app's load. The
    cost of a broken hook is that app's skills going stale, not the workspace
    failing to come up.
    """
    hook = getattr(plugin, "list_skill_sources", None)
    if hook is None:
        return
    try:
        result = await hook(ctx)
    except Exception as exc:  # noqa: BLE001 - a bad hook must not break load
        log.warning("skill_sources: %s list_skill_sources() failed: %s", app_id, exc)
        return
    record(app_id, result)
