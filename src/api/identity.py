"""Offline verification of the central-identity EdDSA JWT (F2).

No credentials DB here — identity is central (aw-backend, F1b). This module
only verifies signature + expiry against aw-backend's PUBLIC key, mirroring
``aw-backend/src/api/identity_guard.py``'s ``decode_identity_jwt`` check but
without ever touching aw-backend over the network per request:

* ``AW_AUTH_PUBLIC_KEY`` (PEM string) — set directly, no boot-time fetch.
* Otherwise, fetched once from ``{AW_BACKEND_URL}/api/identity/public-key``
  on first use and cached in-process for the life of the worker.
"""
from __future__ import annotations

import os
import threading
import time

import httpx
import jwt as pyjwt
from fastapi import Header, HTTPException, Request, WebSocket

COOKIE_NAME = "aw_id_jwt"
JWT_ALGORITHM = "EdDSA"

_public_key_pem: str | None = None
_lock = threading.Lock()

# A container that just (re)started can hit a network hiccup reaching
# aw-backend on this VERY FIRST identity-gated request (DNS/routing still
# settling right after boot). Without a retry, decode_identity_jwt's broad
# except swallows that network failure and reports it identically to "bad
# token" — a perfectly valid, unexpired JWT gets 401'd once, which the SPA's
# apiFetch treated as a real logout with zero grace (found live 2026-08-05,
# recurring "login expired" reports that correlated with app
# install/update/reinstall cycles, which recreate this container). Retrying
# here fixes it at the source instead of only papering over it client-side.
_FETCH_RETRIES = 3
_FETCH_RETRY_DELAY_S = 0.5


def _fetch_public_key_pem() -> str:
    backend_url = os.environ.get("AW_BACKEND_URL", "").rstrip("/")
    if not backend_url:
        raise RuntimeError(
            "neither AW_AUTH_PUBLIC_KEY nor AW_BACKEND_URL is set — cannot "
            "verify identity JWTs"
        )
    last_exc: Exception | None = None
    for attempt in range(_FETCH_RETRIES):
        try:
            resp = httpx.get(f"{backend_url}/api/identity/public-key", timeout=10.0)
            resp.raise_for_status()
            return resp.text
        except Exception as e:  # noqa: BLE001 — retried below, re-raised after last attempt
            last_exc = e
            if attempt < _FETCH_RETRIES - 1:
                time.sleep(_FETCH_RETRY_DELAY_S * (attempt + 1))
    raise last_exc


def get_public_key_pem() -> str:
    """Return the aw-backend Ed25519 public key PEM, caching it in-process."""
    global _public_key_pem
    env_key = os.environ.get("AW_AUTH_PUBLIC_KEY")
    if env_key:
        return env_key
    if _public_key_pem is None:
        with _lock:
            if _public_key_pem is None:
                _public_key_pem = _fetch_public_key_pem()
    return _public_key_pem


def decode_identity_jwt(token: str) -> dict | None:
    """Verify signature + expiry using the PUBLIC key only. No DB lookup —
    the caller decides what ``sub``/``memberships`` mean for this workspace."""
    try:
        return pyjwt.decode(token, get_public_key_pem(), algorithms=[JWT_ALGORITHM])
    except Exception:
        return None


def _extract_token(request: Request, authorization: str) -> str:
    if authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    return request.cookies.get(COOKIE_NAME, "")


def _workspace_api_key_authorized(request: Request) -> bool:
    """True if the request carries a valid workspace-wide API key (see
    ``src.api.workspace_api_key``) — lets another app/MCP, and this
    workspace's own ``aw-workspace-cli``, call framework routes the same way
    ``_default_verify_http`` lets it call app routes."""
    from src.api.workspace_api_key import HEADER_NAME, verify_workspace_api_key

    presented = request.headers.get(HEADER_NAME, "")
    return verify_workspace_api_key(presented)


async def require_identity(request: Request, authorization: str = Header(default="")) -> dict:
    """FastAPI dependency — returns the verified JWT claims dict or 401s."""
    if _workspace_api_key_authorized(request):
        return {"sub": "workspace-api-key", "api_key": True}

    token = _extract_token(request, authorization)
    if not token:
        raise HTTPException(status_code=401, detail="unauthorized")

    claims = decode_identity_jwt(token)
    if not claims:
        raise HTTPException(status_code=401, detail="unauthorized")

    return claims


def authorize_ws(websocket: WebSocket) -> dict | None:
    """Verify a WebSocket handshake, returning claims or None.

    Checked in order: the workspace-wide ``X-Api-Key`` header (mirrors
    ``_workspace_api_key_authorized`` above — a non-browser caller that CAN
    set upgrade-request headers, e.g. this workspace's own CLI or an
    automation tool driving a browser over CDP, authenticates the same way
    it already does for HTTP); then the ``?token=`` query param (short-lived,
    explicit); then the apex ``aw_id_jwt`` cookie (sent automatically to
    ``api.<ws>.workspace`` since the cookie lives on the shared apex domain
    — this is the path a real browser tab's plain ``new WebSocket()`` relies
    on, since it can't set custom headers). Returns the verified claims
    dict, or ``None`` if nothing valid is present — the caller closes the
    socket. Never raises.
    """
    if _workspace_api_key_authorized(websocket):
        return {"sub": "workspace-api-key", "api_key": True}
    token = websocket.query_params.get("token") or websocket.cookies.get(COOKIE_NAME, "")
    if not token:
        return None
    return decode_identity_jwt(token)
