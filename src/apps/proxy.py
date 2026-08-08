"""Reverse-proxy ASGI app for Tier-2 (container) apps — Phase 6.

A ``tier: container`` app has no in-process plugin; its backend lives in the
container. The runtime mounts this ASGI app at ``/api/apps/<slug>`` (behind the
same :class:`~src.apps.runtime.IdentityGuard` as Tier-1 sub-apps), and it
forwards every request to the container's ``base_url`` — both plain HTTP and
WebSocket (the aw-app-browser CDP endpoint needs WS).

Kept deliberately small: HTTP is buffered-forwarded via ``httpx``; WebSocket is
bridged via the ``websockets`` client with two pump tasks. Starlette strips the
mount prefix before we see the scope, so ``scope["path"]`` is already the
container-relative path.
"""
from __future__ import annotations

import asyncio
import logging

from starlette.types import Receive, Scope, Send

log = logging.getLogger(__name__)

# Hop-by-hop headers that must not be forwarded verbatim (per RFC 7230).
_HOP_BY_HOP = {
    b"connection", b"keep-alive", b"proxy-authenticate", b"proxy-authorization",
    b"te", b"trailers", b"transfer-encoding", b"upgrade", b"host",
}
# httpx transparently gunzips (or br/deflate-decodes) a compressed upstream
# response before we ever see `resp.content` — so forwarding the upstream's
# own `content-encoding`/`content-length` headers verbatim lies to the
# downstream browser: it receives PLAIN bytes labeled as compressed (and at
# the wrong length), tries to gunzip already-decompressed data, and renders
# garbage. Confirmed live 2026-08-08 against aw-app-code-server (code-server
# gzips its index.html) — the page loaded (200) but rendered as mojibake.
# Dropping both lets Starlette/uvicorn recompute a correct content-length
# for the actual (decompressed) body we send.
_RESPONSE_HEADERS_TO_STRIP = _HOP_BY_HOP | {b"content-encoding", b"content-length"}
_INTERNAL_HEADERS = {
    b"x-aw-identity-sub",
    b"x-aw-identity-email",
}


class ContainerReverseProxy:
    """Forward HTTP + WebSocket to a Tier-2 container's ``base_url``."""

    def __init__(self, base_url: str) -> None:
        self.base_url = base_url.rstrip("/")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            await self._http(scope, receive, send)
        elif scope["type"] == "websocket":
            await self._ws(scope, receive, send)
        # lifespan and anything else: ignore (the host app owns lifespan).

    # ---- HTTP -----------------------------------------------------------

    def _target(self, scope: Scope, ws: bool = False) -> str:
        # Starlette Mount signals the mount prefix via root_path (ASGI spec) and
        # may leave scope["path"] as the FULL path — strip the prefix so the
        # container sees a container-relative path.
        path = scope.get("path", "") or "/"
        root = scope.get("root_path", "") or ""
        if root and path.startswith(root):
            path = path[len(root):] or "/"
        qs = scope.get("query_string", b"").decode()
        base = self.base_url
        if ws:
            base = "ws://" + base.split("://", 1)[-1]
        return base + path + (f"?{qs}" if qs else "")

    async def _http(self, scope: Scope, receive: Receive, send: Send) -> None:
        import httpx

        body = await _read_http_body(receive)
        headers = [(k.decode("latin-1"), v.decode("latin-1"))
                   for k, v in scope.get("headers", [])
                   if k.lower() not in _HOP_BY_HOP and k.lower() not in _INTERNAL_HEADERS
                   and k.lower() != b"accept-encoding"]
        # Force an Accept-Encoding httpx can ALWAYS fully decode, instead of
        # forwarding the browser's own value verbatim. httpx silently drops
        # any content-encoding it doesn't have a decoder for (SUPPORTED_
        # DECODERS only has br/zstd if the optional `brotli`/`zstandard`
        # packages are installed — neither is in this image) and falls back
        # to IdentityDecoder with NO error — resp.content then ends up as
        # the raw, still-compressed bytes. Confirmed live 2026-08-08: a
        # browser's real multi-algorithm Accept-Encoding let code-server
        # pick `br`; httpx silently no-op'd; this proxy's own content-
        # encoding-stripping (below) then told Caddy the (still br-
        # compressed) body was plain, so Caddy gzip-compressed already-
        # compressed bytes — a valid outer gzip wrapper around garbage.
        # gzip/deflate are always decodable (Python's stdlib zlib, no extra
        # package needed), so pinning to those guarantees resp.content is
        # genuinely the decompressed body every time.
        headers.append(("accept-encoding", "gzip, deflate"))
        identity = scope.get("aw_identity") or {}
        if identity:
            sub = identity.get("sub") or identity.get("user_id") or ""
            email = identity.get("email") or ""
            if sub:
                headers.append(("x-aw-identity-sub", str(sub)))
            if email:
                headers.append(("x-aw-identity-email", str(email)))
        url = self._target(scope)
        try:
            async with httpx.AsyncClient(timeout=None) as client:
                resp = await client.request(
                    scope["method"], url, headers=headers, content=body)
        except Exception as e:  # container down / unreachable → 502, don't crash host
            log.warning("apps: proxy to %s failed: %s", url, e)
            await _send_502(send)
            return

        out_headers = [(k.encode("latin-1"), v.encode("latin-1"))
                       for k, v in resp.headers.items()
                       if k.lower().encode() not in _RESPONSE_HEADERS_TO_STRIP]
        await send({"type": "http.response.start", "status": resp.status_code,
                    "headers": out_headers})
        await send({"type": "http.response.body", "body": resp.content})

    # ---- WebSocket ------------------------------------------------------

    async def _ws(self, scope: Scope, receive: Receive, send: Send) -> None:
        import websockets

        # Consume the client's connect event before dialling upstream.
        await receive()
        url = self._target(scope, ws=True)
        try:
            upstream = await websockets.connect(url, open_timeout=10, max_size=None)
        except Exception as e:
            log.warning("apps: ws proxy to %s failed: %s", url, e)
            await send({"type": "websocket.close", "code": 1011})
            return
        await send({"type": "websocket.accept"})

        async def client_to_upstream() -> None:
            while True:
                event = await receive()
                etype = event["type"]
                if etype == "websocket.receive":
                    data = event.get("bytes")
                    if data is not None:
                        await upstream.send(data)
                    else:
                        await upstream.send(event.get("text", ""))
                elif etype == "websocket.disconnect":
                    await upstream.close()
                    return

        async def upstream_to_client() -> None:
            async for message in upstream:
                if isinstance(message, bytes):
                    await send({"type": "websocket.send", "bytes": message})
                else:
                    await send({"type": "websocket.send", "text": message})
            await send({"type": "websocket.close", "code": 1000})

        c2u = asyncio.ensure_future(client_to_upstream())
        u2c = asyncio.ensure_future(upstream_to_client())
        try:
            done, pending = await asyncio.wait(
                {c2u, u2c}, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
        finally:
            await upstream.close()


async def _read_http_body(receive: Receive) -> bytes:
    chunks: list[bytes] = []
    while True:
        event = await receive()
        if event["type"] != "http.request":
            break
        chunks.append(event.get("body", b"") or b"")
        if not event.get("more_body", False):
            break
    return b"".join(chunks)


async def _send_502(send: Send) -> None:
    body = b'{"detail":"bad gateway: app container unreachable"}'
    await send({"type": "http.response.start", "status": 502,
                "headers": [(b"content-type", b"application/json"),
                            (b"content-length", str(len(body)).encode())]})
    await send({"type": "http.response.body", "body": body})
