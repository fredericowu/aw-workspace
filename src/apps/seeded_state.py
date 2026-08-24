"""What a contribution surface last seeded, so it can update without clobbering.

``contributes.agents`` and ``contributes.tasks`` were create-if-absent and
**never updated**. The reason was sound: a seeded task or agent belongs to the
user the moment it exists, and an app re-asserting its own version on every
boot would silently undo a schedule they tuned or a prompt they fixed in the
UI.

The cost was equally real, and it bit on 2026-08-22: a corrected prompt
shipped in a new app version never reached the live row, so the manifest read
like the source of truth while the running system quietly disagreed. The app
had no way to fix its own mistake short of a new slug.

This module is the middle: remember the value each field had **when we seeded
it**. On the next pass,

* live value still equals what we seeded → nobody touched it, so the app's
  new value wins and we re-record;
* live value differs → a human changed it. Leave it, say so, never overwrite.

That is `kubectl apply`'s last-applied-configuration, scoped to the fields an
app legitimately owns.

**Field ownership is the other half of the fix**, and it covers a case the
hash check alone does not. A user who *edited* a field is already protected
above. The gap is the row nobody touched: live still equals what we seeded, so
the diff looks safe — and a v2 manifest shipping ``enabled: true`` or a
different time would then start firing a schedule the user never opted into,
or move one they were relying on. So ``enabled`` and ``schedules`` are
*operational* state, the user's to decide, seeded at creation and never
re-asserted on update. Content (a prompt, a command, a description) stays the
app's to correct.

Hashes, not values: this file is read on every boot and a prompt is
kilobytes. The hash is only ever compared against another hash, never
reversed.

One namespace lives elsewhere: "agents" is tenant state, not workspace state
-----------------------------------------------------------------------------

Everything above is true for ``contributes.tasks`` (namespace ``"tasks"``),
where the seeded OBJECT itself is genuinely per-workspace — aw-app-tasks has
no shared backing store. ``contributes.agents`` (namespace ``"agents"``) is
different: the object it seeds (an Agent, a Workflow, ...) lives once in
Agents Platform, shared by every workspace of a tenant. Tracking its
baseline in this file anyway meant two workspaces of the same tenant
installing the same builtin object raced over a file each of them thought
was authoritative — the loser's every later boot read its own up-to-date
manifest as a live-vs-seeded divergence and logged it as a hand-edit that
never happened. Permanent, silent capture (Kanban
``ap-mt:seeded-state-tenant-scoped``).

So for ``kind == "agents"``, :func:`record` and :func:`updatable_fields`
delegate to whatever provider called :func:`set_provider` — Agents
Platform's own ``seeded_objects`` table — instead of touching this file.
``"tasks"`` is untouched: it still reads and writes locally, because there
the local file already was the right place. On a workspace's first boot
after this landed, a remote miss for an object this file DOES have a local
record for is treated as "migrate it", not "nobody ever seeded this" — see
:func:`_remote_read`. Skipping that migration would reclassify every field
an app already owned as hand-edited, silently, forever.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from typing import Any

from src.apps import paths

log = logging.getLogger(__name__)

#: Fields the *user* owns once the object exists. Seeded at creation, never
#: re-asserted on update — not even when the live value still matches what we
#: seeded, which is the one case the hand-edit check cannot catch.
USER_OWNED = frozenset({"enabled", "schedules"})

#: The one namespace whose seeded object is per-TENANT rather than
#: per-workspace — see the module docstring's "One namespace lives
#: elsewhere" section.
REMOTE_NAMESPACE = "agents"

#: The Agents Platform provider registered via :func:`set_provider`, or
#: ``None`` before any provider has loaded (record/updatable_fields then
#: fall back to local-file behaviour even for ``"agents"``, same as before
#: this namespace existed — a missing provider is not a reason to lose data).
_provider: Any | None = None


def set_provider(provider: Any | None) -> None:
    """Register the provider :func:`record`/:func:`updatable_fields` delegate
    to for the ``"agents"`` namespace. Called by ``src/apps/agents.py``
    every time it resolves one — there is no un-registration path, since a
    provider disappearing mid-run isn't a real scenario this needs to
    handle (apps don't unload without a restart).
    """
    global _provider
    _provider = provider


def _remote_capable() -> bool:
    """Whether the registered provider actually implements the tenant-state
    pair. A provider that only implements the older ``register_contributed_
    agents``/``read_contributed_agent``/``update_contributed_agent`` surface
    (aw-app-agents-platform-runners before this landed) has neither method —
    :func:`record`/:func:`updatable_fields` must fall back to the local
    file for it exactly as they did before ``"agents"`` went remote, or a
    workspace on new aw-workspace core paired with an old runners app would
    silently record nothing at all, anywhere.
    """
    return (_provider is not None
            and callable(getattr(_provider, "read_state", None))
            and callable(getattr(_provider, "write_state", None)))


def _remote_read(app_id: str, key: str) -> dict[str, str] | None:
    """The remote fingerprints for ``key`` ("<obj kind>:<slug>"), migrating
    a local baseline onto the provider the first time this workspace sees
    no remote row where its own file has one. See the module docstring."""
    read = getattr(_provider, "read_state", None)
    if read is None:
        return None
    obj_kind, _, slug = key.partition(":")
    try:
        row = read(obj_kind, slug)
    except Exception:  # noqa: BLE001 — a bad read must never crash a reconcile
        log.exception("seeded_state: failed to read remote state for %s %r", app_id, key)
        return None
    if row:
        return row.get("fingerprints") or {}
    local = _read().get(app_id, {}).get(REMOTE_NAMESPACE, {}).get(key)
    return local or None


def _remote_write(app_id: str, key: str, fingerprints: dict[str, str],
                  app_version: str) -> None:
    write = getattr(_provider, "write_state", None)
    if write is None:
        return
    obj_kind, _, slug = key.partition(":")
    try:
        write(app_id, obj_kind, slug, app_version, fingerprints)
    except Exception:  # noqa: BLE001 — a bad write must never crash a reconcile
        log.exception("seeded_state: failed to write remote state for %s %r", app_id, key)


def state_path() -> str:
    return os.path.join(paths.workspace_home(), "seeded-state.json")


def _read() -> dict[str, Any]:
    try:
        with open(state_path(), encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("seeded_state: unreadable %s: %s", state_path(), exc)
        return {}
    return data if isinstance(data, dict) else {}


def _write(state: dict[str, Any]) -> None:
    path = state_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2, sort_keys=True)
    os.replace(tmp, path)


def fingerprint(value: Any) -> str:
    """Stable hash of one field's value, order-independent for dicts."""
    blob = json.dumps(value, sort_keys=True, default=str, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def record(app_id: str, kind: str, key: str, spec: dict[str, Any], *,
          app_version: str = "") -> None:
    """Remember every field we just seeded for ``kind``/``key``.

    ``app_version`` is only meaningful (and only sent anywhere) for the
    ``"agents"`` namespace — the tenant-shared table uses it to arbitrate
    between two workspaces racing to record a baseline; see
    :func:`_remote_write`. Local-file namespaces ignore it: nothing else
    installs the same object, so there is nothing to race.
    """
    fingerprints = {field: fingerprint(value) for field, value in spec.items()}
    if kind == REMOTE_NAMESPACE and _remote_capable():
        _remote_write(app_id, key, fingerprints, app_version)
        return
    state = _read()
    state.setdefault(app_id, {}).setdefault(kind, {})[key] = fingerprints
    _write(state)


def forget(app_id: str) -> None:
    """Drop an app's record — it was uninstalled."""
    state = _read()
    if state.pop(app_id, None) is not None:
        _write(state)


def updatable_fields(app_id: str, kind: str, key: str,
                     spec: dict[str, Any], live: dict[str, Any]) -> dict[str, Any]:
    """The subset of ``spec`` it is safe to write over ``live``.

    A field is included only when all three hold:

    * it is not :data:`USER_OWNED`;
    * we have a record of seeding it (no record → we never owned it, so this
      is a row the user made by hand that happens to share a name, and
      matching on name is exactly how the surface recognises that);
    * the live value still hashes to what we seeded (nobody edited it).

    Returns ``{}`` when there is nothing safe to change, which is the common
    steady state and must stay cheap.
    """
    if kind == REMOTE_NAMESPACE and _remote_capable():
        seeded = _remote_read(app_id, key)
    else:
        seeded = _read().get(app_id, {}).get(kind, {}).get(key)
    if not seeded:
        return {}

    out: dict[str, Any] = {}
    for field, new_value in spec.items():
        if field in USER_OWNED:
            continue
        was = seeded.get(field)
        if was is None:
            continue
        if field not in live:
            continue
        if fingerprint(live.get(field)) != was:
            log.info("seeded_state: %s %s/%s field %r was edited by hand — leaving it",
                     app_id, kind, key, field)
            continue
        if fingerprint(new_value) == was:
            continue  # unchanged in the manifest too
        out[field] = new_value
    return out
