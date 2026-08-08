"""``require_identity`` validates an EdDSA JWT with only the public key —
mirrors what aw-backend's own ``decode_identity_jwt`` check does, minus any
network round-trip per request.
"""
from __future__ import annotations

import asyncio
import time

import jwt as pyjwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import HTTPException
from starlette.requests import Request

from src.api import identity as identity_mod


def _pem_pair():
    key = Ed25519PrivateKey.generate()
    private_pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_pem = key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode()
    return private_pem, public_pem


def _sign(private_pem: bytes, **claims) -> str:
    now = int(time.time())
    payload = {"sub": "1", "iat": now, "exp": now + 3600, **claims}
    return pyjwt.encode(payload, private_pem, algorithm="EdDSA")


def _fake_request(token: str | None = None, *, extra_headers: dict[str, str] | None = None) -> Request:
    headers = []
    cookie = f"{identity_mod.COOKIE_NAME}={token}".encode() if token else b""
    if cookie:
        headers.append((b"cookie", cookie))
    for k, v in (extra_headers or {}).items():
        headers.append((k.lower().encode(), v.encode()))
    scope = {
        "type": "http",
        "headers": headers,
        "method": "GET",
        "path": "/",
        "query_string": b"",
    }
    return Request(scope)


class TestDecodeIdentityJwt:
    def test_accepts_valid_token(self, monkeypatch):
        private_pem, public_pem = _pem_pair()
        monkeypatch.setenv("AW_AUTH_PUBLIC_KEY", public_pem)
        token = _sign(private_pem)

        claims = identity_mod.decode_identity_jwt(token)
        assert claims is not None
        assert claims["sub"] == "1"

    def test_rejects_token_signed_by_different_key(self, monkeypatch):
        _other_private_pem, public_pem = _pem_pair()
        wrong_private_pem, _wrong_public_pem = _pem_pair()
        monkeypatch.setenv("AW_AUTH_PUBLIC_KEY", public_pem)
        token = _sign(wrong_private_pem)

        assert identity_mod.decode_identity_jwt(token) is None

    def test_rejects_expired_token(self, monkeypatch):
        private_pem, public_pem = _pem_pair()
        monkeypatch.setenv("AW_AUTH_PUBLIC_KEY", public_pem)
        now = int(time.time())
        token = pyjwt.encode(
            {"sub": "1", "iat": now - 7200, "exp": now - 3600},
            private_pem,
            algorithm="EdDSA",
        )

        assert identity_mod.decode_identity_jwt(token) is None

    def test_rejects_malformed_token(self, monkeypatch):
        _private_pem, public_pem = _pem_pair()
        monkeypatch.setenv("AW_AUTH_PUBLIC_KEY", public_pem)

        assert identity_mod.decode_identity_jwt("not-a-jwt") is None


class TestFetchPublicKeyPemRetries:
    def test_retries_transient_network_failures_and_succeeds(self, monkeypatch):
        monkeypatch.delenv("AW_AUTH_PUBLIC_KEY", raising=False)
        monkeypatch.setenv("AW_BACKEND_URL", "http://fake-backend")
        monkeypatch.setattr(identity_mod, "_FETCH_RETRY_DELAY_S", 0)

        calls = {"n": 0}

        def flaky_get(url, timeout):
            calls["n"] += 1
            if calls["n"] < 3:
                raise identity_mod.httpx.ConnectError("connection refused")
            return type("Resp", (), {
                "text": "the-pem",
                "raise_for_status": lambda self: None,
            })()

        monkeypatch.setattr(identity_mod.httpx, "get", flaky_get)

        assert identity_mod._fetch_public_key_pem() == "the-pem"
        assert calls["n"] == 3

    def test_raises_the_last_error_after_exhausting_retries(self, monkeypatch):
        monkeypatch.delenv("AW_AUTH_PUBLIC_KEY", raising=False)
        monkeypatch.setenv("AW_BACKEND_URL", "http://fake-backend")
        monkeypatch.setattr(identity_mod, "_FETCH_RETRY_DELAY_S", 0)

        def always_fails(url, timeout):
            raise identity_mod.httpx.ConnectError("connection refused")

        monkeypatch.setattr(identity_mod.httpx, "get", always_fails)

        with pytest.raises(identity_mod.httpx.ConnectError):
            identity_mod._fetch_public_key_pem()


class TestRequireIdentityDependency:
    def test_raises_401_with_no_token(self, monkeypatch):
        _private_pem, public_pem = _pem_pair()
        monkeypatch.setenv("AW_AUTH_PUBLIC_KEY", public_pem)

        with pytest.raises(HTTPException) as exc_info:
            asyncio.run(identity_mod.require_identity(_fake_request(), authorization=""))
        assert exc_info.value.status_code == 401

    def test_accepts_valid_cookie_token(self, monkeypatch):
        private_pem, public_pem = _pem_pair()
        monkeypatch.setenv("AW_AUTH_PUBLIC_KEY", public_pem)
        token = _sign(private_pem)

        claims = asyncio.run(identity_mod.require_identity(_fake_request(token), authorization=""))
        assert claims["sub"] == "1"


class TestRequireIdentityWithWorkspaceApiKey:
    """A valid X-Api-Key header authenticates framework routes the same way
    a JWT does — the CLI and external apps/MCPs both use this path."""

    def test_valid_api_key_is_accepted(self, monkeypatch):
        import src.api.workspace_api_key as api_key_mod
        monkeypatch.setattr(api_key_mod, "verify_workspace_api_key", lambda presented: presented == "good-key")

        req = _fake_request(extra_headers={api_key_mod.HEADER_NAME: "good-key"})
        claims = asyncio.run(identity_mod.require_identity(req, authorization=""))
        assert claims == {"sub": "workspace-api-key", "api_key": True}

    def test_invalid_api_key_falls_through_to_401(self, monkeypatch):
        import src.api.workspace_api_key as api_key_mod
        monkeypatch.setattr(api_key_mod, "verify_workspace_api_key", lambda presented: presented == "good-key")

        req = _fake_request(extra_headers={api_key_mod.HEADER_NAME: "wrong-key"})
        with pytest.raises(HTTPException) as exc_info:
            asyncio.run(identity_mod.require_identity(req, authorization=""))
        assert exc_info.value.status_code == 401
