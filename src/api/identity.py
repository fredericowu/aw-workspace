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

import httpx
import jwt as pyjwt
from fastapi import Header, HTTPException, Request, WebSocket

COOKIE_NAME = "aw_id_jwt"
JWT_ALGORITHM = "EdDSA"

_public_key_pem: str | None = None
_lock = threading.Lock()


def _fetch_public_key_pem() -> str:
    backend_url = os.environ.get("AW_BACKEND_URL", "").rstrip("/")
    if not backend_url:
        raise RuntimeError(
            "neither AW_AUTH_PUBLIC_KEY nor AW_BACKEND_URL is set — cannot "
            "verify identity JWTs"
        )
    resp = httpx.get(f"{backend_url}/api/identity/public-key", timeout=10.0)
    resp.raise_for_status()
    return resp.text


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


def _local_cli_authorized(request: Request) -> bool:
    """True if the request carries this workspace's own ``./aw`` CLI secret
    (see ``src/apps/paths.py``'s ``get_or_create_cli_token``) — lets the CLI
    call identity-gated routes without a browser-issued ``aw_id_jwt``."""
    from src.apps.paths import LOCAL_CLI_HEADER, get_or_create_cli_token

    presented = request.headers.get(LOCAL_CLI_HEADER)
    return bool(presented) and presented == get_or_create_cli_token()


async def require_identity(request: Request, authorization: str = Header(default="")) -> dict:
    """FastAPI dependency — returns the verified JWT claims dict or 401s."""
    if _local_cli_authorized(request):
        return {"sub": "local-cli", "local_cli": True}

    token = _extract_token(request, authorization)
    if not token:
        raise HTTPException(status_code=401, detail="unauthorized")

    claims = decode_identity_jwt(token)
    if not claims:
        raise HTTPException(status_code=401, detail="unauthorized")

    return claims


def authorize_ws(websocket: WebSocket) -> dict | None:
    """Verify the identity JWT for a WebSocket handshake, returning claims or None.

    A browser cannot set custom headers on a WebSocket, so the token is taken
    from (in order): the ``?token=`` query param (short-lived, explicit), then
    the apex ``aw_id_jwt`` cookie (sent automatically to ``api.<ws>.workspace``
    since the cookie lives on the shared apex domain). Returns the verified
    claims dict, or ``None`` if no valid token is present — the caller closes
    the socket. Never raises.
    """
    token = websocket.query_params.get("token") or websocket.cookies.get(COOKIE_NAME, "")
    if not token:
        return None
    return decode_identity_jwt(token)
