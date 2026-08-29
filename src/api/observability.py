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
explicit ask: "se o app for desinstalado depois enquanto em modo Local, cair
pra Desligado e avisar, não falhar silenciosamente"). The downgrade is
lazy — it happens the next time the config is read (GET, or the resolver
used by aw-backend), not via an uninstall hook, since none exists for this
today; documented as the chosen trade-off rather than built as a new hook.
"""
from __future__ import annotations

import logging

from fastapi import Depends, FastAPI, HTTPException

from src.api.db import get_session
from src.api.identity import require_identity
from src.api.models import Setting
from src.api.workspace_api_key import get_or_create_workspace_api_key
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
        return resolve(runtime)

    @app.put("/api/settings/observability")
    async def put_observability(body: dict,
                                identity: dict = Depends(require_identity)):
        runtime = getattr(app.state, "app_runtime", None)
        custom = body.get("custom") or {}
        try:
            return update(
                mode=str(body.get("mode") or ""),
                custom_endpoint=custom.get("endpoint"),
                custom_api_key=custom.get("api_key"),
                runtime=runtime,
            )
        except ObservabilityError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
