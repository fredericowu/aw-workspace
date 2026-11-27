import asyncio

import pytest

@pytest.fixture(autouse=True)
def _boot(monkeypatch):
    monkeypatch.setenv("AW_WORKSPACE_BOOT_ID", "b1")


from src.apps.boot_progress import BootProgress, read_progress


def test_progress_file_counts_and_phases(tmp_path):
    frames = []

    async def on_change(snap):
        frames.append((snap["phase"], snap["loaded"], list(snap["in_flight"])))

    async def go():
        p = BootProgress("b1", lock_dir=str(tmp_path), on_change=on_change)
        assert read_progress(str(tmp_path))["phase"] == "idle"
        await p.begin(total=3, loaded=1)
        await p.app_started("a")
        mid = read_progress(str(tmp_path))  # a different "worker" reads the file
        await p.app_finished("a")
        await p.finish(True)
        return mid

    mid = asyncio.run(go())
    assert (mid["phase"], mid["total"], mid["loaded"], mid["in_flight"]) == \
        ("reconciling", 3, 1, ["a"])
    final = read_progress(str(tmp_path))
    assert (final["phase"], final["loaded"], final["in_flight"]) == ("done", 2, [])
    assert [f[0] for f in frames] == ["reconciling"] * 3 + ["done"]


def test_failed_phase(tmp_path):
    async def go():
        p = BootProgress("b1", lock_dir=str(tmp_path))
        await p.begin(1, 0)
        await p.finish(False)
    asyncio.run(go())
    assert read_progress(str(tmp_path))["phase"] == "failed"


def test_previous_boot_snapshot_is_idle(tmp_path, monkeypatch):
    async def go():
        p = BootProgress("b1", lock_dir=str(tmp_path))
        await p.begin(2, 0)
        await p.finish(True)
    asyncio.run(go())
    assert read_progress(str(tmp_path))["phase"] == "done"
    monkeypatch.setenv("AW_WORKSPACE_BOOT_ID", "b2")
    assert read_progress(str(tmp_path))["phase"] == "idle"


def test_failed_app_is_not_counted_and_bad_lock_dir_is_swallowed(tmp_path):
    async def go():
        p = BootProgress("b1", lock_dir=str(tmp_path))
        await p.begin(2, 0)
        await p.app_started("a")
        await p.app_finished("a", ok=False)
        return p.snapshot()
    snap = asyncio.run(go())
    assert (snap["loaded"], snap["in_flight"]) == (0, [])

    blocker = tmp_path / "file"
    blocker.write_text("x")

    async def bad():
        p = BootProgress("b1", lock_dir=str(blocker / "sub"))
        await p.begin(1, 0)  # must not raise
        await p.finish(False)
    asyncio.run(bad())
