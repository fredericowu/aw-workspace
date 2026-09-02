"""Observability settings — where THIS workspace sends OTLP telemetry.

Three explicit states, not "blank field = auto" (Frederico, 2026-08-29
Notion thread ``signoz``: a blank endpoint is ambiguous once ``aw-app-signoz``
is installed — does blank mean "don't export" or "use the local one"?):

* ``off`` (default) — nothing is exported.
* ``local`` — only selectable while ``aw-app-signoz`` is installed in this
  workspace. Endpoint + auth are resolved automatically from the local
  instance (:func:`src.apps.containers.app_public_url` + this workspace's
  own API key) — the user types nothing.
* ``custom`` — endpoint + API key filled in by hand, to point at any OTLP
  collector (including the ``aw-app-signoz`` of a *different* workspace of
  the same owner).

Storage is the generic ``settings`` KV table (``src.api.models.Setting``),
same mechanism as the workspace API key and mapped folders — see
``src/api/folders.py`` for the module shape this mirrors.

If ``aw-app-signoz`` is uninstalled while mode is ``local``, :func:`resolve`
downgrades the stored mode to ``off`` and returns a warning instead of
silently keeping a mode that no longer resolves to anything (Frederico's
explicit ask: if the app gets uninstalled while in Local mode, fall back to
Off and warn — never fail silently. The downgrade is
lazy — it happens the next time the config is read (GET, or the resolver
used by aw-backend), not via an uninstall hook, since none exists for this
today; documented as the chosen trade-off rather than built as a new hook.
"""
from __future__ import annotations

import asyncio
import logging
import os

import httpx
from fastapi import Depends, FastAPI, HTTPException

from src.api.db import get_session
from src.api.identity import require_identity
from src.api.models import Setting
from src.api.workspace_api_key import HEADER_NAME as API_KEY_HEADER, get_or_create_workspace_api_key
from src.apps.containers import app_public_url

log = logging.getLogger(__name__)

SETTING_KEY = "observability"

#: The aw-app-signoz manifest id (``aw-app.json`` -> ``"id": "signoz"``) —
#: what "Local (auto)" resolves against.
SIGNOZ_APP_ID = "signoz"

MODES = ("off", "local", "custom")


class ObservabilityError(ValueError):
    """Invalid observability settings input — surfaced as a 400 by the routes."""


def _load(session=None) -> dict:
    if session is not None:
        row = session.get(Setting, SETTING_KEY)
        return dict(row.value) if row else {}
    with get_session() as s:
        row = s.get(Setting, SETTING_KEY)
        return dict(row.value) if row else {}


def _save(value: dict) -> None:
    with get_session() as session:
        row = session.get(Setting, SETTING_KEY)
        if row is None:
            session.add(Setting(key=SETTING_KEY, value=value))
        else:
            row.value = value
            session.add(row)
        session.commit()


def _custom(stored: dict) -> dict:
    custom = stored.get("custom") or {}
    return {
        "endpoint": str(custom.get("endpoint") or ""),
        "api_key": str(custom.get("api_key") or ""),
    }


def signoz_installed(runtime) -> bool:
    """Whether ``aw-app-signoz`` is currently loaded in this process.

    ``runtime`` is the host's :class:`src.apps.runtime.AppRuntime`
    (``app.state.app_runtime``) — the same object ``src/api/folders.py``
    reaches for container remaps. Missing/None runtime (pure unit tests
    with no apps subsystem wired) reads as "not installed", which is the
    safe default for a feature that requires the app to be present."""
    if runtime is None or not hasattr(runtime, "is_loaded"):
        return False
    return bool(runtime.is_loaded(SIGNOZ_APP_ID))


def local_target() -> dict:
    """The endpoint + key "Local (auto)" resolves to, derived not stored —
    same reasoning as ``app_public_url``'s own docstring: a workspace slug
    baked into a stored value would be wrong the moment it's copied
    anywhere else, so it's recomputed every time instead."""
    return {
        "endpoint": app_public_url(SIGNOZ_APP_ID),
        "api_key": get_or_create_workspace_api_key(),
    }


def resolve(runtime) -> dict:
    """The effective config a caller (Settings UI, aw-backend) actually
    wants: current mode, whether Local is even choosable, and — unless
    Desligado — the endpoint/key that mode resolves to right now.

    Downgrades a stale ``local`` mode to ``off`` and persists that, per the
    module docstring, whenever the app has disappeared since it was set."""
    stored = _load()
    mode = stored.get("mode") or "off"
    if mode not in MODES:
        mode = "off"
    local_available = signoz_installed(runtime)

    warning = None
    if mode == "local" and not local_available:
        mode = "off"
        stored["mode"] = "off"
        _save(stored)
        warning = (
            "aw-app-signoz was uninstalled — Observability mode reset to "
            "Desligado (was Local)."
        )
        log.warning("observability: local mode stale (app not installed), reset to off")

    resolved = None
    if mode == "local":
        resolved = {**local_target(), "source": "local"}
    elif mode == "custom":
        custom = _custom(stored)
        if custom["endpoint"]:
            resolved = {**custom, "source": "custom"}

    return {
        "mode": mode,
        "local_available": local_available,
        "custom": _custom(stored),
        "resolved": resolved,
        "warning": warning,
    }


def update(mode: str, custom_endpoint: str | None, custom_api_key: str | None,
           runtime) -> dict:
    """Validate + persist a new mode. Raises :class:`ObservabilityError`."""
    mode = (mode or "").strip()
    if mode not in MODES:
        raise ObservabilityError(f"mode must be one of {', '.join(MODES)} (got {mode!r})")

    if mode == "local" and not signoz_installed(runtime):
        raise ObservabilityError(
            "mode 'local' requires aw-app-signoz to be installed in this workspace"
        )

    stored = _load()
    stored["mode"] = mode

    if mode == "custom":
        endpoint = (custom_endpoint or "").strip()
        if not endpoint:
            raise ObservabilityError("custom mode requires a non-empty endpoint")
        if not (endpoint.startswith("http://") or endpoint.startswith("https://")):
            raise ObservabilityError("custom endpoint must be an http(s) URL")
        stored["custom"] = {
            "endpoint": endpoint,
            "api_key": (custom_api_key or "").strip(),
        }

    _save(stored)
    log.info("observability: mode set to %s", mode)
    return resolve(runtime)


# --- push to agents-platform-multitenant --------------------------------------

#: aw-app-agents-platform-runners' manifest id — where the mode change is
#: pushed to (that app owns the actual AP-MT round trip, see its own
#: observability_push.py). Best-effort only: this workspace's setting is the
#: source of truth regardless of whether the push lands.
RUNNERS_APP_ID = "agents-platform-runners"

#: A few attempts with a short backoff, all inside this PUT's own request
#: cycle — NOT the polling watchdog this card removed (no background timer,
#: no reconnect loop). Worst case (3 attempts, 5s httpx timeout each, backoff
#: between the first two) stays well under any reasonable request timeout.
NOTIFY_MAX_ATTEMPTS = 3
NOTIFY_RETRY_BACKOFF_S = 0.5


async def _notify_agents_platform_runners() -> dict:
    """Tell aw-app-agents-platform-runners a mode change just saved, so it can
    push the new target to agents-platform-multitenant immediately instead of
    waiting on a poll. Retries a few times with a short backoff before giving
    up — never raised into the caller, since the settings save already
    succeeded and is the one thing that must not be undone by this.

    A 200 from ``/register-observability`` does not mean the push itself
    landed — that route always returns 200 and reports the outcome in its
    body (``{"pushed": bool, "reason": str}``, see
    ``observability_push.push_once``), because the remote leg to AP-MT can
    fail (AP-MT down, timeout, ``agents_platform_token`` not configured)
    independently of the local call succeeding. Checking only
    ``raise_for_status()`` here would treat that failure as success, which is
    exactly the silent-failure gap this retry exists to close: every failed
    attempt is logged, and the final outcome is returned so the PUT response
    can surface it instead of swallowing it."""
    port = os.environ.get("AW_PORT", "9030")
    url = f"http://127.0.0.1:{port}/api/apps/{RUNNERS_APP_ID}/register-observability"
    reason = "unknown error"
    for attempt in range(1, NOTIFY_MAX_ATTEMPTS + 1):
        try:
            # to_thread: reads/mints the key through the SYNCHRONOUS
            # get_session — inline it would block the one event-loop thread.
            api_key = await asyncio.to_thread(get_or_create_workspace_api_key)
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.post(url, headers={API_KEY_HEADER: api_key})
            resp.raise_for_status()
            body = resp.json()
            if body.get("pushed"):
                return {"ok": True, "reason": None}
            reason = body.get("reason") or "push did not succeed"
        except Exception as exc:  # noqa: BLE001 — any leg failure is a retry candidate
            reason = str(exc)

        log.warning("observability: push attempt %d/%d to %s failed: %s",
                    attempt, NOTIFY_MAX_ATTEMPTS, RUNNERS_APP_ID, reason)
        if attempt < NOTIFY_MAX_ATTEMPTS:
            await asyncio.sleep(NOTIFY_RETRY_BACKOFF_S)

    log.error("observability: giving up notifying %s after %d attempts — last error: %s",
              RUNNERS_APP_ID, NOTIFY_MAX_ATTEMPTS, reason)
    return {"ok": False, "reason": reason}


# --- routes ------------------------------------------------------------------


def register_observability_routes(app: FastAPI) -> None:
    """Mount ``/api/settings/observability`` — identity-gated like every
    other settings route. Deliberately its own routes rather than the raw
    ``GET/PUT /api/settings/{key}`` — resolving "Local (auto)" needs the
    app-runtime lookup + downgrade-on-uninstall logic above, which the
    generic key/value routes have no way to run."""

    @app.get("/api/settings/observability")
    async def get_observability(identity: dict = Depends(require_identity)):
        runtime = getattr(app.state, "app_runtime", None)
        # to_thread: resolve()/update() read and write the settings row via
        # the SYNCHRONOUS get_session, and this process has ONE event-loop
        # thread serving every request (AW_WORKSPACE_WORKERS=1) — an inline
        # call stalls all of them for the DB round-trip.
        return await asyncio.to_thread(resolve, runtime)

    @app.put("/api/settings/observability")
    async def put_observability(body: dict,
                                identity: dict = Depends(require_identity)):
        runtime = getattr(app.state, "app_runtime", None)
        custom = body.get("custom") or {}
        try:
            result = await asyncio.to_thread(
                update,
                mode=str(body.get("mode") or ""),
                custom_endpoint=custom.get("endpoint"),
                custom_api_key=custom.get("api_key"),
                runtime=runtime,
            )
        except ObservabilityError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        push = await _notify_agents_platform_runners()
        return {**result, "push": push}
