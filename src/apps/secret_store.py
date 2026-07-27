"""Workspace-side secure secret store (ADR contribution point ``secrets:own``) — F4.

An app's ``config_schema`` fields marked ``x-secret`` (e.g. aw-app-git's
``github_token``) are stored here rather than in plain cloud config, and exposed
to the app's plugin only through the F2-gated ``ctx.secrets`` facade. Values are
encrypted at rest with Fernet (AES-128-CBC + HMAC); the key comes from
``AW_WORKSPACE_SECRET_KEY`` (a urlsafe-base64 Fernet key) or is generated once
and persisted at ``<home>/secret.key`` (mode 600).

Scope note: this is the workspace-local secure store F4 calls for. The
**zero-knowledge** store (cloud can't read; user-held key) is a separate deferred
card (``feature:user-zero-knowledge-secret-storage``) that plugs in behind this
same facade later — the app-facing contract (``read``/``write``/``delete``) does
not change when it does.

Storage: one JSON file per app at ``<home>/secrets/<slug>.json`` mapping
``key -> ciphertext`` — namespaced by slug so uninstall purges an app's secrets
by deleting its file, and no app can address another app's namespace.
"""
from __future__ import annotations

import json
import logging
import os

from cryptography.fernet import Fernet

from src.apps import paths

log = logging.getLogger(__name__)


class SecretStoreError(RuntimeError):
    pass


def _load_or_create_key() -> bytes:
    env_key = os.environ.get("AW_WORKSPACE_SECRET_KEY")
    if env_key:
        return env_key.encode() if isinstance(env_key, str) else env_key
    key_path = os.path.join(paths.workspace_home(), "secret.key")
    if os.path.isfile(key_path):
        with open(key_path, "rb") as f:
            return f.read().strip()
    key = Fernet.generate_key()
    with open(key_path, "wb") as f:
        f.write(key)
    os.chmod(key_path, 0o600)
    log.info("apps: generated workspace secret key at %s", key_path)
    return key


class SecretStore:
    """Per-app encrypted KV store. One Fernet key per workspace."""

    def __init__(self) -> None:
        self._fernet: Fernet | None = None

    @property
    def fernet(self) -> Fernet:
        if self._fernet is None:
            self._fernet = Fernet(_load_or_create_key())
        return self._fernet

    def _path(self, app_id: str) -> str:
        return os.path.join(paths.secrets_dir(), f"{app_id}.json")

    def _read_raw(self, app_id: str) -> dict[str, str]:
        path = self._path(app_id)
        if not os.path.isfile(path):
            return {}
        with open(path, encoding="utf-8") as f:
            return json.load(f)

    def _write_raw(self, app_id: str, data: dict[str, str]) -> None:
        path = self._path(app_id)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.chmod(path, 0o600)

    def put(self, app_id: str, key: str, value: str) -> None:
        data = self._read_raw(app_id)
        data[key] = self.fernet.encrypt(value.encode()).decode()
        self._write_raw(app_id, data)

    def get(self, app_id: str, key: str) -> str | None:
        token = self._read_raw(app_id).get(key)
        if token is None:
            return None
        return self.fernet.decrypt(token.encode()).decode()

    def delete(self, app_id: str, key: str) -> bool:
        data = self._read_raw(app_id)
        if key in data:
            del data[key]
            self._write_raw(app_id, data)
            return True
        return False

    def keys(self, app_id: str) -> list[str]:
        return list(self._read_raw(app_id))

    def purge(self, app_id: str) -> bool:
        """Delete an app's whole secret namespace (uninstall). Idempotent."""
        path = self._path(app_id)
        if os.path.isfile(path):
            os.remove(path)
            log.info("apps: purged secrets for %s", app_id)
            return True
        return False
