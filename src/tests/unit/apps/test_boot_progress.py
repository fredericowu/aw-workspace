import asyncio

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
        p = BootProgress("b", lock_dir=str(tmp_path))
        await p.begin(1, 0)
        await p.finish(False)
    asyncio.run(go())
    assert read_progress(str(tmp_path))["phase"] == "failed"
