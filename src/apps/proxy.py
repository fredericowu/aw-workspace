"""Reverse-proxy ASGI app for Tier-2 (container) apps — Phase 6.

A ``tier: container`` app has no in-process plugin; its backend lives in the
container. The runtime mounts this ASGI app at ``/api/apps/<slug>`` (behind the
same :class:`~src.apps.runtime.IdentityGuard` as Tier-1 sub-apps), and it
forwards every request to the container's ``base_url`` — both plain HTTP and
WebSocket (the aw-app-browser CDP endpoint needs WS).

Kept deliberately small: HTTP is a byte-for-byte passthrough via ``httpx``
(the RAW, undecoded response — see ``_http``'s docstring for why); WebSocket
is bridged via the ``websockets`` client with two pump tasks. Starlette
strips the mount prefix before we see the scope, so ``scope["path"]`` is
already the container-relative path.

The single exception to "passthrough" is a ``Cache-Control: no-store`` added
to HTML/JSON responses that arrived with no caching directive at all — see
``_NO_STORE_CONTENT_TYPES``. It's additive only (an app that sets its own
``Cache-Control`` keeps it) and exists because heuristic browser caching
otherwise pins an app to a previous deploy.
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
_INTERNAL_HEADERS = {
    b"x-aw-identity-sub",
    b"x-aw-identity-email",
}

# Content types we refuse to let a browser cache on its own recognisance.
#
# App backends are FastAPI + `StaticFiles(html=True)` (the aw-app-template
# pattern every app is scaffolded from), and NEITHER sets `Cache-Control`.
# With no `Cache-Control` and no `Expires`, a browser falls back to HEURISTIC
# freshness (RFC 9111 4.2.2) — it invents a lifetime, typically 10% of the
# time since `Last-Modified`, and serves from cache WITHOUT revalidating. The
# ETag `StaticFiles` does send is useless in that window because nothing ever
# asks.
#
# Two distinct failure modes, both reported live 2026-08-08 (Frederico, on
# iPad Safari — which applies heuristics especially aggressively):
#
# 1. HTML — `index.html` is the only unhashed file Vite emits, and it names
#    the hashed bundle. A stale index.html therefore pins the ENTIRE app to
#    the previous build, no matter how many times the new one is deployed.
# 2. JSON — API GETs come back with no validator at all (not even an ETag),
#    so a cached response can outlive the state it describes. This is the
#    nastier one: the freshly-loaded app asks for data and the browser
#    answers from memory, which reads as "the server ignored my change".
#
# Only applied when the upstream said NOTHING about caching — an app that
# sets its own `Cache-Control` is making a deliberate choice and keeps it.
# Hashed assets (JS/CSS/fonts/images) are untouched: their filenames are
# their cache keys, so caching them hard is the whole point.
_NO_STORE_CONTENT_TYPES = (b"text/html", b"application/json")
_NO_STORE_VALUE = b"no-store, must-revalidate"


def _cache_control_override(headers) -> bytes | None:
    """``no-store`` for uncacheable-by-nature payloads, else ``None``.

    ``headers`` is the outgoing list of ``(name, value)`` byte pairs.
    """
    content_type = b""
    for name, value in headers:
        lowered = name.lower()
        if lowered == b"cache-control":
            return None  # upstream was explicit — respect it
        if lowered == b"content-type":
            content_type = value.lower()
    if any(content_type.startswith(t) for t in _NO_STORE_CONTENT_TYPES):
        return _NO_STORE_VALUE
    return None


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
        """Byte-for-byte passthrough — same philosophy as the monolith's Caddy
        talking straight to the container: don't decode/re-encode anything,
        just relay exactly what the upstream sent.

        Earlier versions of this method used ``client.request(...)`` and
        forwarded ``resp.content`` (httpx's transparently DECOMPRESSED body)
        alongside the upstream's own ``content-encoding`` header — two
        separate bugs came out of that, both confirmed live 2026-08-08
        against aw-app-code-server (code-server gzips its responses):

        1. ``resp.content`` is decompressed, but the forwarded headers still
           claimed ``content-encoding: gzip`` — the browser received PLAIN
           bytes labeled compressed and rendered garbage trying to gunzip
           them.
        2. httpx only decompresses encodings it has a registered decoder
           for — ``br``/``zstd`` decoders only exist if the optional
           ``brotli``/``zstandard`` packages are installed (neither is in
           this image). For those, httpx's ``_get_content_decoder()`` hits a
           ``KeyError``, silently swallows it, and falls back to
           ``IdentityDecoder`` — so ``resp.content`` was the raw, STILL-
           compressed bytes with no error raised at all. A real browser's
           Accept-Encoding lists ``br``/``zstd`` right alongside gzip, so
           this bit even after "fixing" (1) by stripping the header: this
           proxy would tell Caddy the (still-brotli-compressed) body was
           plain, and Caddy's own ``encode gzip`` would gzip already-
           compressed bytes — a valid outer gzip wrapper around garbage.

        Both problems are really the same root mistake: trying to be
        smart (decode, then re-describe) about a body this proxy has no
        need to understand. ``client.send(..., stream=True)`` +
        ``resp.aiter_raw()`` reads the response WITHOUT decompressing it, so
        the bytes we forward and the ``content-encoding``/``content-length``
        headers describing them are always consistent — whatever the
        upstream sent, whatever encoding it picked, no decoder required, no
        optional package needed. Caddy (which sits in front of this proxy
        and does its own ``encode gzip zstd``) then does exactly what it
        already does for every other route: skip re-encoding a response
        that's already encoded, or freshly encode one that isn't.
        """
        import httpx

        body = await _read_http_body(receive)
        headers = [(k.decode("latin-1"), v.decode("latin-1"))
                   for k, v in scope.get("headers", [])
                   if k.lower() not in _HOP_BY_HOP and k.lower() not in _INTERNAL_HEADERS]
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
                req = client.build_request(
                    scope["method"], url, headers=headers, content=body)
                resp = await client.send(req, stream=True)
                raw_body = b"".join([chunk async for chunk in resp.aiter_raw()])
                await resp.aclose()
        except Exception as e:  # container down / unreachable → 502, don't crash host
            log.warning("apps: proxy to %s failed: %s", url, e)
            await _send_502(send)
            return

        out_headers = [(k.encode("latin-1"), v.encode("latin-1"))
                       for k, v in resp.headers.items()
                       if k.lower().encode() not in _HOP_BY_HOP]
        # The one place this proxy is NOT a pure passthrough — and only ever
        # additive, never a rewrite (see _cache_control_override).
        override = _cache_control_override(out_headers)
        if override is not None:
            out_headers.append((b"cache-control", override))
        await send({"type": "http.response.start", "status": resp.status_code,
                    "headers": out_headers})
        await send({"type": "http.response.body", "body": raw_body})

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
