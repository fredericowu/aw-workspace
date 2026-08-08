"""Shared helpers for the component-lifecycle CLI commands.

``start`` / ``stop`` / ``restart`` are three separate command modules (the
discovery contract in ``src/cli/discovery.py`` is one ``COMMAND`` per file, and
the monolith's ``./aw`` exposed them as three verbs too), but they differ only
in the verb — everything else, including the "which component did you mean?"
resolution, lives here.

Component keys are the legacy SPA vocabulary (``docker:aw-<app>`` for a Tier-2
container, ``service:<app>:<id>`` for a Tier-1 managed service — see
``src/api/components.py``). Nobody wants to type those, so every command here
accepts a bare app slug and resolves it, only asking the user to disambiguate
when a slug genuinely matches more than one component.
"""
from __future__ import annotations

from src.cli import local_client


def components() -> list[dict]:
    status, body = local_client.request("GET", "/api/components")
    if status != 200:
        print(f"error: could not list components ({status}): {body}")
        return []
    if isinstance(body, dict):
        return body.get("components", [])
    return body if isinstance(body, list) else []


def resolve_key(target: str) -> str | None:
    """Map user input to a component key: exact key, app slug, or substring."""
    rows = components()
    if not rows:
        return None

    for row in rows:
        if row.get("key") == target:
            return row["key"]
    matches = [r for r in rows if r.get("component") == target]
    if len(matches) == 1:
        return matches[0]["key"]
    if not matches:
        matches = [r for r in rows if target in str(r.get("key", ""))]

    if len(matches) == 1:
        return matches[0]["key"]
    if not matches:
        print(f"error: no component matching {target!r}")
        print(f"known: {', '.join(sorted(r.get('component', r['key']) for r in rows))}")
        return None
    print(f"error: {target!r} is ambiguous — matches {', '.join(m['key'] for m in matches)}")
    return None


def act(verb: str, target: str) -> int:
    key = resolve_key(target)
    if key is None:
        return 1
    status, body = local_client.request("POST", f"/api/components/{key}/{verb}", timeout=180.0)
    if status != 200:
        detail = body.get("error") if isinstance(body, dict) else body
        print(f"error: {verb} {key} failed ({status}): {detail}")
        return 1
    state = body.get("status", verb + "ed") if isinstance(body, dict) else verb
    print(f"{key}: {state}")
    return 0
