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


def _fake_request(token: str | None = None) -> Request:
    headers = []
    cookie = f"{identity_mod.COOKIE_NAME}={token}".encode() if token else b""
    if cookie:
        headers.append((b"cookie", cookie))
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
