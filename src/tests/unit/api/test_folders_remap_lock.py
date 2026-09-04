"""``_locked_remap`` (``src/api/folders.py``) — the cross-process mutex
around the actual container-recreate call.

W2: ``_RemapCoalescer`` only collapses a burst of folder mutations WITHIN
one process — at ``AW_WORKSPACE_WORKERS>1`` each worker has its own
``_coalescer`` (module-level, one per process), so a burst spread across
workers used to still fire one concurrent ``remap_folders()`` call per
worker. These tests prove ``_locked_remap`` serializes concurrent callers
instead of letting them run at once — the exact failure the docstring on
``_RemapCoalescer`` says it exists to prevent.
"""
from __future__ import annotations

import asyncio

import pytest

from src.api.folders import _locked_remap


@pytest.mark.asyncio
async def test_concurrent_remaps_never_overlap(tmp_path, monkeypatch):
    monkeypatch.setenv("AW_WORKSPACE_HOME", str(tmp_path / "home"))

    active = 0
    max_active = 0
    calls = []

    async def fake_run():
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        calls.append("start")
        await asyncio.sleep(0.05)
        active -= 1
        calls.append("end")
        return ["some-app"]

    results = await asyncio.gather(*[_locked_remap(fake_run) for _ in range(5)])

    assert max_active == 1, "two remaps ran concurrently — the lock did not serialize them"
    assert calls == ["start", "end"] * 5
    assert results == [["some-app"]] * 5


@pytest.mark.asyncio
async def test_a_failed_remap_releases_the_lock_for_the_next_caller(tmp_path, monkeypatch):
    monkeypatch.setenv("AW_WORKSPACE_HOME", str(tmp_path / "home"))

    async def failing_run():
        raise RuntimeError("boom")

    async def ok_run():
        return ["ok"]

    with pytest.raises(RuntimeError):
        await _locked_remap(failing_run)

    # A prior failure must not leave the lock held forever.
    result = await asyncio.wait_for(_locked_remap(ok_run), timeout=2.0)
    assert result == ["ok"]
