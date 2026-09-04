"""``upsert_workspace_env`` (``src/apps/paths.py``) — the single writer for
``<home>/.env``, shared by the workspace API key and the external API URL.

W2: at ``AW_WORKSPACE_WORKERS>1`` multiple workers can call this at the
exact same moment during boot. Before the fix this was a plain
read-modify-write with an ``O_TRUNC`` write — no serialization across
processes, so two concurrent callers could interleave (a reader sees a
half-written file) or the later writer could silently drop an earlier
writer's key (both read the same "before" content).
"""
from __future__ import annotations

import os
import threading

import pytest

from src.apps.paths import env_file, upsert_workspace_env


@pytest.fixture()
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("AW_WORKSPACE_HOME", str(tmp_path / "home"))
    os.makedirs(tmp_path / "home", exist_ok=True)
    return tmp_path / "home"


def test_upsert_creates_and_updates_a_key(home):
    upsert_workspace_env("FOO", "1")
    with open(env_file()) as f:
        assert f.read().splitlines() == ["FOO=1"]

    upsert_workspace_env("FOO", "2")
    with open(env_file()) as f:
        assert f.read().splitlines() == ["FOO=2"]


def test_upsert_preserves_other_lines(home):
    upsert_workspace_env("FOO", "1")
    upsert_workspace_env("BAR", "2")
    with open(env_file()) as f:
        lines = set(f.read().splitlines())
    assert lines == {"FOO=1", "BAR=2"}


def test_file_is_0600(home):
    upsert_workspace_env("FOO", "1")
    mode = os.stat(env_file()).st_mode & 0o777
    assert mode == 0o600


def test_concurrent_upserts_of_distinct_keys_lose_nothing(home):
    """N threads, each opening the file fresh (mirrors N separate worker
    PROCESSES — ``fcntl.flock`` is keyed on the open file description, not
    on any Python-level object shared between callers), each upserting a
    DIFFERENT key at the same moment. Before the fix, this reliably lost
    updates: every thread's read-modify-write cycle races every other's."""
    n = 20
    barrier = threading.Barrier(n)
    errors: list[BaseException] = []

    def upsert_one(i: int) -> None:
        try:
            barrier.wait(timeout=10)
            upsert_workspace_env(f"KEY_{i}", str(i))
        except BaseException as exc:  # noqa: BLE001 — captured, not raised, from a worker thread
            errors.append(exc)

    threads = [threading.Thread(target=upsert_one, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)

    assert not errors, f"{len(errors)} worker(s) raised: {errors}"

    with open(env_file()) as f:
        lines = f.read().splitlines()

    seen = {}
    for line in lines:
        assert "=" in line, f"env file line is not KEY=VALUE (torn write): {line!r}"
        name, _, value = line.partition("=")
        assert name not in seen, f"duplicate key {name!r} in env file (torn/interleaved write)"
        seen[name] = value

    assert seen == {f"KEY_{i}": str(i) for i in range(n)}, \
        f"lost update: expected {n} keys, file has {sorted(seen)}"


def test_concurrent_upserts_of_the_same_key_end_with_one_line(home):
    """Racing writers of the SAME key must never produce two lines for it —
    the pre-fix read-then-append-if-not-found logic could otherwise have
    two concurrent callers both decide "not found" and both append."""
    n = 10
    barrier = threading.Barrier(n)

    def upsert_same(i: int) -> None:
        barrier.wait(timeout=10)
        upsert_workspace_env("SHARED", str(i))

    threads = [threading.Thread(target=upsert_same, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)

    with open(env_file()) as f:
        lines = [l for l in f.read().splitlines() if l.startswith("SHARED=")]
    assert len(lines) == 1, f"expected exactly one SHARED= line, got {lines}"
