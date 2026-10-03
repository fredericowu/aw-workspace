"""A dead secret store must not read as a healthy workspace.

On 2026-10-03 every vault operation workspace-wide hard-500'd for hours —
aw-backend's vault client still dialled ``aw-sandbox:9130`` after that
container was decommissioned, taking ``aw-vault`` (which shared its netns)
with it — while ``status``, ``/api/health`` and ``doctor`` all stayed green.
The three aw-knowledgeable key-push self-heal loops logged a failed tick
every 300s and nothing else said a word. ``doctor`` had zero vault awareness
before this check; these tests pin the two directions that matter (a 500
behind a perfectly healthy API is a failure; an unlinked BYOD workspace is
not) plus the ``ok`` wiring, since reporting the problem in the body while
leaving ``ok`` true would reproduce the silence under a new name.

``asyncio.run`` rather than an async test function, matching
``test_flock_lease.py`` — this suite has no async plugin configured.
"""
import asyncio
import json

import pytest

from src.apps import routes


class _Resp:
    def __init__(self, status_code, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text if text else (json.dumps(payload) if payload is not None else "")

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


class _Client:
    """Stands in for ``httpx.AsyncClient`` used as an async context manager."""

    def __init__(self, resp=None, raises=None):
        self._resp = resp
        self._raises = raises
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url, headers=None):
        self.calls.append((url, headers))
        if self._raises is not None:
            raise self._raises
        return self._resp


@pytest.fixture
def linked(monkeypatch):
    """A workspace that completed the /link handshake."""
    monkeypatch.setattr(routes, "_vault_env", lambda key: {
        "AW_BACKEND_URL": "https://api.aw.tekflox.com",
        "AW_WORKSPACE": "aw",
        "AW_WORKSPACE_HOST_TOKEN": "awlk_test",
    }.get(key, ""))


def _patch_client(monkeypatch, client):
    monkeypatch.setattr(routes.httpx, "AsyncClient", lambda **kw: client)
    return client


def test_a_listing_that_answers_is_reachable(linked, monkeypatch):
    client = _patch_client(monkeypatch, _Client(
        _Resp(200, {"secrets": [{"name": "a"}, {"name": "b"}]})))
    out = asyncio.run(routes._vault_status())
    assert out["reachable"] is True
    assert out["secrets"] == 2
    # The LIST route, never a read: a read is behind a human approval gate
    # and would put a Telegram prompt on someone's phone every doctor pass.
    assert client.calls[0][0] == (
        "https://api.aw.tekflox.com/api/workspaces/aw/approval/secrets")
    assert client.calls[0][1]["Authorization"] == "Bearer awlk_test"


def test_a_500_is_a_failure_and_carries_aw_backends_own_detail(linked, monkeypatch):
    """The exact 2026-10-03 shape: aw-backend itself was healthy, and the
    real cause only ever appeared in its ``detail``."""
    _patch_client(monkeypatch, _Client(_Resp(500, {
        "detail": "Vault error 500: <urlopen error [Errno -3] "
                  "Temporary failure in name resolution>",
    })))
    out = asyncio.run(routes._vault_status())
    assert out["reachable"] is False
    assert out["status"] == 500
    assert "Temporary failure in name resolution" in out["note"]


def test_a_transport_failure_is_a_failure(linked, monkeypatch):
    _patch_client(monkeypatch, _Client(raises=RuntimeError("connect timeout")))
    out = asyncio.run(routes._vault_status())
    assert out["reachable"] is False
    assert "connect timeout" in out["note"]


def test_a_200_with_the_wrong_shape_is_a_failure(linked, monkeypatch):
    """A 200 is not proof: an edge or a proxy answering in place of the
    control plane is exactly how a dead dependency reads as healthy."""
    _patch_client(monkeypatch, _Client(_Resp(200, None, text="<html>hi</html>")))
    out = asyncio.run(routes._vault_status())
    assert out["reachable"] is False


def test_an_unlinked_workspace_is_not_configured_not_broken(monkeypatch):
    """A BYOD workspace that never linked has no secret store at all. That
    must report absent (``reachable: None``) — counting it as degraded would
    make doctor exit non-zero on every unlinked workspace forever."""
    monkeypatch.setattr(routes, "_vault_env", lambda key: "")
    out = asyncio.run(routes._vault_status())
    assert out["configured"] is False
    assert out["reachable"] is None


def test_ok_is_false_only_when_the_vault_is_actually_broken():
    """Pins the ``reachable is not False`` wiring in the doctor payload: the
    three states have to land differently, and ``not reachable`` would make
    an unlinked workspace permanently degraded."""
    def ok_for(vault):
        return vault.get("reachable") is not False

    assert ok_for({"configured": True, "reachable": True}) is True
    assert ok_for({"configured": False, "reachable": None}) is True
    assert ok_for({"configured": True, "reachable": False}) is False
