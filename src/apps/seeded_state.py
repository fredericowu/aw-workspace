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


def record(app_id: str, kind: str, key: str, spec: dict[str, Any]) -> None:
    """Remember every field we just seeded for ``kind``/``key``."""
    state = _read()
    state.setdefault(app_id, {}).setdefault(kind, {})[key] = {
        field: fingerprint(value) for field, value in spec.items()
    }
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
