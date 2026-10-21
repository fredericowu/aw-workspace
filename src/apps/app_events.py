"""Manifest-driven app-event contract and Tier-2 caller credentials.

The notification WebSocket remains the transport.  This module only owns the
declarative hook contract and the narrow credential a container uses to emit
events as itself.  Tokens are deterministic HMACs of the workspace API key:
they need no second secret store, are scoped to one app id, and rotate when the
workspace key rotates.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
from typing import Any

from src.api.workspace_api_key import get_or_create_workspace_api_key

APP_EVENT_HEADER = "X-AW-App-Event-Token"
APP_EVENT_TOKEN_ENV = "AW_APP_EVENT_TOKEN"
APP_EVENT_URL_ENV = "AW_APP_EVENT_URL"
APP_EVENT_PROTOCOL = 1
MAX_EVENT_PAYLOAD_BYTES = 8192
EVENT_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
PAYLOAD_FIELD_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
ALLOWED_ACTIONS = frozenset({"open_window"})


def app_event_token(app_id: str) -> str:
    key = get_or_create_workspace_api_key().encode("utf-8")
    digest = hmac.new(key, f"aw-app-event/v1:{app_id}".encode("utf-8"), hashlib.sha256)
    return f"awev1_{digest.hexdigest()}"


def verify_app_event_token(app_id: str, presented: str) -> bool:
    return bool(presented) and hmac.compare_digest(presented, app_event_token(app_id))


def app_event_url(app_id: str) -> str:
    from src.apps.containers import workspace_host

    port = int(os.environ.get("AW_PORT", "9030"))
    return f"http://{workspace_host()}:{port}/api/apps/{app_id}/events"


def payload_size(data: dict[str, Any]) -> int:
    return len(json.dumps(data, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))


def validate_event_hooks(contributes: dict[str, Any], app_id: str,
                         permissions: list[str]) -> None:
    hooks = contributes.get("event_hooks", [])
    if not isinstance(hooks, list):
        raise ValueError("contributes.event_hooks must be a list")
    if hooks and "notifications:send" not in permissions:
        raise ValueError(
            "contributes.event_hooks requires the 'notifications:send' permission")

    seen: set[str] = set()
    for hook in hooks:
        if not isinstance(hook, dict):
            raise ValueError("each contributes.event_hooks entry must be an object")
        event = hook.get("event")
        if not isinstance(event, str) or not EVENT_RE.fullmatch(event):
            raise ValueError(
                "contributes.event_hooks[].event must match "
                f"{EVENT_RE.pattern} (got {event!r})")
        if event in seen:
            raise ValueError(f"duplicate contributes.event_hooks event {event!r}")
        seen.add(event)

        action = hook.get("action")
        if not isinstance(action, dict):
            raise ValueError("contributes.event_hooks[].action must be an object")
        action_type = action.get("type")
        if action_type not in ALLOWED_ACTIONS:
            raise ValueError(
                "contributes.event_hooks[].action.type must be 'open_window' "
                f"(got {action_type!r})")
        unknown = set(action) - {"type", "window", "url_field"}
        if unknown:
            raise ValueError(
                "contributes.event_hooks[].action has unsupported fields: "
                + ", ".join(sorted(unknown)))
        window = action.get("window")
        if not isinstance(window, str) or not window.startswith(f"{app_id}."):
            raise ValueError(
                "contributes.event_hooks[].action.window must be namespaced "
                f"under '{app_id}.'")
        url_field = action.get("url_field")
        if url_field is not None and (
            not isinstance(url_field, str) or not PAYLOAD_FIELD_RE.fullmatch(url_field)
        ):
            raise ValueError(
                "contributes.event_hooks[].action.url_field must be a snake_case payload field")

