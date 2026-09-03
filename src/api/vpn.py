"""``/api/vpn*`` — VPN profile manager on the WORKSPACE plane (phase 1).

Backs Settings NEW › General › VPNs. Upload, list, inspect, edit and delete VPN
config profiles, plus NordVPN credential storage and server lookup/import.
**Nothing dials.** There is no start, no stop, no ``wg-quick``, no ``openvpn``,
no iptables and no poller — see ``src/vpn/profiles.py``'s docstring and
``docs/architecture/vpn-profiles-in-general.md`` §2.3 for what stayed behind in
``aw-backend``'s copy and why. ``GET /api/vpn/status`` says so honestly rather
than fabricating an up/down.

Why these routes are here and not on ``aw-backend``, where 17 ``/api/vpn/*``
routes already exist: ``apiBase.js:176-183`` rewrites every relative ``/api/*``
fetch from a workspace SPA host to ``api.<slug>.workspace.<apex>`` — this
process. The legacy routes are on the wrong side of that rewrite, which is
``bug:networking-tab-calls-wrong-api-plane``; building them here is what makes
the SPA's own fetch correct. That claim is falsifiable and is asserted in
``src/tests/integration/api/test_vpn_routes.py``: the routes must be registered
on the CORE app object. The aw-backend copy stays exactly as it is — different
plane, different lifetime, not migrated, not called, not deleted.

Registered through ``register_vpn_routes(app, mgr)`` like every other surface
here (``src/api/app.py``) — core has no ``include_router`` sprawl.
"""
from __future__ import annotations

import asyncio
import logging

import httpx
from fastapi import Body, Depends, FastAPI, File, Form, HTTPException, UploadFile

from src.api.identity import require_identity
from src.vpn.profiles import (
    VpnProfileError,
    VpnProfileNotFound,
    VpnProfiles,
    VpnRejectedError,
)

log = logging.getLogger(__name__)

# Bigger than any real VPN profile (a fat OpenVPN config with inline certs is
# ~10 KB), small enough that an upload can't be used to fill the workspace's
# durable storage.
MAX_UPLOAD_BYTES = 256 * 1024


def _http_error(exc: Exception) -> HTTPException:
    """Map a manager error to its status code, keeping the named directive.

    A rejection has to tell the user *which* line was refused: a 400 saying
    "invalid config" is indistinguishable from a silent strip from the outside,
    which is the failure mode the reject-don't-strip rule exists to prevent.
    """
    if isinstance(exc, VpnRejectedError):
        return HTTPException(
            status_code=400,
            detail={"error": "rejected_directive", "directive": exc.directive,
                    "message": str(exc)},
        )
    if isinstance(exc, VpnProfileNotFound):
        return HTTPException(status_code=404, detail=str(exc))
    return HTTPException(status_code=400, detail=str(exc))


def register_vpn_routes(app: FastAPI, mgr: VpnProfiles | None = None) -> None:
    """Mount ``/api/vpn*`` — all identity-gated, like every other route here."""
    mgr = mgr or VpnProfiles()
    app.state.vpn_profiles = mgr

    # to_thread throughout: every manager call does blocking file I/O or a
    # blocking httpx call, and this process runs ONE uvicorn worker
    # (AW_WORKSPACE_WORKERS=1) — an inline call freezes every other in-flight
    # request, including ones touching no VPN state at all. Same reasoning as
    # src/api/folders.py.

    @app.get("/api/vpn/configs")
    async def list_configs(identity: dict = Depends(require_identity)):
        return {"configs": await asyncio.to_thread(mgr.list_configs)}

    # Registered BEFORE the ``/{name}`` routes: Starlette matches in
    # registration order, not by specificity, so a literal path that arrives
    # after a same-shaped catch-all is shadowed forever (the incident behind
    # src/tests/integration/api/test_settings_route_order.py). ``/status`` and
    # ``/nord/*`` sit on a different segment and are safe, but
    # ``/configs/upload`` collides with ``/configs/{name}`` exactly.
    @app.post("/api/vpn/configs/upload")
    async def upload_config(
        type: str = Form(...),
        name: str | None = Form(None),
        file: UploadFile = File(...),
        identity: dict = Depends(require_identity),
    ):
        raw = await file.read()
        if len(raw) > MAX_UPLOAD_BYTES:
            raise HTTPException(
                status_code=400,
                detail=f"file too large ({len(raw)} bytes, max {MAX_UPLOAD_BYTES})",
            )
        try:
            content = raw.decode()
        except UnicodeDecodeError as exc:
            raise HTTPException(
                status_code=400, detail="file is not UTF-8 text — not a VPN config"
            ) from exc
        cfg_name = name or (
            file.filename.rsplit(".", 1)[0] if file.filename else "uploaded"
        )
        try:
            return await asyncio.to_thread(
                mgr.save_config, cfg_name, type, content, "upload"
            )
        except (VpnProfileError, VpnProfileNotFound) as exc:
            raise _http_error(exc) from exc

    @app.get("/api/vpn/configs/{name}")
    async def get_config(name: str, identity: dict = Depends(require_identity)):
        """Metadata + a REDACTED body. No endpoint here returns a private key —
        see ``vpn-concentrator.md`` §3.6."""
        try:
            return await asyncio.to_thread(mgr.get_config, name)
        except (VpnProfileError, VpnProfileNotFound) as exc:
            raise _http_error(exc) from exc

    @app.put("/api/vpn/configs/{name}")
    async def put_config(name: str, payload: dict = Body(...),
                         identity: dict = Depends(require_identity)):
        ctype = payload.get("type")
        content = payload.get("content")
        if not ctype or content is None:
            raise HTTPException(status_code=400, detail="type and content are required")
        try:
            return await asyncio.to_thread(mgr.save_config, name, ctype, content, "upload")
        except (VpnProfileError, VpnProfileNotFound) as exc:
            raise _http_error(exc) from exc

    @app.delete("/api/vpn/configs/{name}")
    async def delete_config(name: str, identity: dict = Depends(require_identity)):
        try:
            await asyncio.to_thread(mgr.delete_config, name)
        except (VpnProfileError, VpnProfileNotFound) as exc:
            raise _http_error(exc) from exc
        return {"removed": name}

    @app.get("/api/vpn/status")
    async def status(identity: dict = Depends(require_identity)):
        return await asyncio.to_thread(mgr.status)

    # -- NordVPN --------------------------------------------------------------

    @app.get("/api/vpn/nord/credentials")
    async def get_nord_credentials(identity: dict = Depends(require_identity)):
        """Presence and username *shape* only — never the password or token.

        The obvious CRUD shape (GET returns what PUT accepted) would leak the
        service password to the browser on every render.
        """
        return await asyncio.to_thread(mgr.nord_credentials_state)

    @app.put("/api/vpn/nord/credentials")
    async def put_nord_credentials(payload: dict = Body(...),
                                   identity: dict = Depends(require_identity)):
        try:
            if "access_token" in payload:
                return await asyncio.to_thread(
                    mgr.set_nord_access_token, payload.get("access_token") or ""
                )
            return await asyncio.to_thread(
                mgr.set_nord_credentials,
                payload.get("service_username") or "",
                payload.get("service_password") or "",
            )
        except VpnProfileError as exc:
            raise _http_error(exc) from exc
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=502, detail=f"NordVPN API error: {exc}") from exc

    @app.get("/api/vpn/nord/countries")
    async def nord_countries(identity: dict = Depends(require_identity)):
        try:
            return {"countries": await asyncio.to_thread(mgr.nord_countries)}
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=502, detail=f"NordVPN API error: {exc}") from exc

    @app.get("/api/vpn/nord/recommendations")
    async def nord_recommendations(country_id: int | None = None,
                                   city_id: int | None = None,
                                   limit: int = 10,
                                   identity: dict = Depends(require_identity)):
        try:
            servers = await asyncio.to_thread(
                mgr.nord_recommendations, country_id, city_id, limit
            )
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=502, detail=f"NordVPN API error: {exc}") from exc
        return {"servers": servers}

    @app.post("/api/vpn/nord/import")
    async def nord_import(payload: dict = Body(...),
                          identity: dict = Depends(require_identity)):
        hostname = payload.get("hostname")
        if not hostname:
            raise HTTPException(status_code=400, detail="hostname is required")
        try:
            return await asyncio.to_thread(
                mgr.nord_import,
                hostname,
                payload.get("protocol") or "udp",
                payload.get("name"),
            )
        except (VpnProfileError, VpnProfileNotFound) as exc:
            raise _http_error(exc) from exc
        except httpx.HTTPError as exc:
            raise HTTPException(
                status_code=502, detail=f"NordVPN config download failed: {exc}"
            ) from exc
