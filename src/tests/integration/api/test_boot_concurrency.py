"""W2 VERIFY: booting N workers simultaneously must produce exactly the
same state as booting 1 — the exact scenario ``src/api/app.py``'s
``create_app()`` hits for real on every ``AW_WORKSPACE_WORKERS>1`` deploy
(``create_all_tables()`` runs once per worker, before the lifespan and
before any event loop exists).

Real-Postgres only — skips cleanly if 127.0.0.1:5432 isn't reachable, same
pattern as ``test_workspace_api_key`` / ``test_app_lifespan_order``.
"""
from __future__ import annotations

import threading

import psycopg
import pytest
from sqlalchemy import text


def _postgres_reachable() -> bool:
    try:
        psycopg.connect(
            "postgresql://postgres:postgres@127.0.0.1:5432/postgres",
            autocommit=True, connect_timeout=2,
        ).close()
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _postgres_reachable(), reason="live Postgres at 127.0.0.1:5432 not reachable"
)

_SCHEMA = "workspace_bootconcurrencytest"
N_WORKERS = 4


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("AW_WORKSPACE_SCHEMA", _SCHEMA)
    monkeypatch.setenv("AW_WORKSPACE_DB_URL",
                        "postgresql://postgres:postgres@127.0.0.1:5432/awserv")
    monkeypatch.setenv("AW_WORKSPACE_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("AW_WORKSPACE", "bootconcurrencytest")
    import src.api.db as dbmod
    monkeypatch.setattr(dbmod, "_engine", None)
    # Warm the engine single-threaded first — get_engine()'s own lazy-init
    # is not itself the thing under test (a real deploy never races THAT:
    # each worker is its own process with its own `_engine` global). Racing
    # N threads through a not-yet-created module-global cache would add an
    # unrelated flake to a test that's supposed to isolate the advisory
    # lock + the atomic key insert.
    dbmod.get_engine()

    yield

    from src.api.db import get_engine
    with get_engine().begin() as conn:
        conn.execute(text(f'DROP SCHEMA IF EXISTS "{_SCHEMA}" CASCADE'))
    monkeypatch.setattr(dbmod, "_engine", None)


def test_n_workers_booting_concurrently_converge_to_one_state(env):
    """N threads independently run ``create_all_tables()`` (the DDL race)
    then ``get_or_create_workspace_api_key()`` (the key race) at the same
    moment — mirroring what N real uvicorn worker processes do at
    ``create_app()``/lifespan time. Asserts the three things the card asks
    for: (a) no DDL error, (b) all N workers resolve the SAME workspace API
    key, (c) the env file parses cleanly (no interleaved/truncated write).
    """
    from src.api.db import create_all_tables
    from src.api.workspace_api_key import get_or_create_workspace_api_key, _env_path

    errors: list[BaseException] = []
    keys: list[str] = []
    barrier = threading.Barrier(N_WORKERS)

    def boot_one() -> None:
        try:
            barrier.wait(timeout=10)
            create_all_tables()
            keys.append(get_or_create_workspace_api_key())
        except BaseException as exc:  # noqa: BLE001 — captured, not raised, from a worker thread
            errors.append(exc)

    threads = [threading.Thread(target=boot_one) for _ in range(N_WORKERS)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    # (a) no DDL error
    assert not errors, f"{len(errors)} worker(s) raised: {errors}"

    # (b) all N workers resolved the SAME workspace API key
    assert len(keys) == N_WORKERS
    assert len(set(keys)) == 1, f"workers diverged on the API key: {set(keys)}"

    # (c) the env file parses cleanly — no interleaved/truncated write
    with open(_env_path()) as f:
        lines = f.read().splitlines()
    seen_names = set()
    for line in lines:
        assert "=" in line, f"env file line is not KEY=VALUE: {line!r}"
        name, _, _value = line.partition("=")
        assert name not in seen_names, f"duplicate key {name!r} in env file — a torn write"
        seen_names.add(name)
    assert f"AW_WORKSPACE_API_KEY={keys[0]}" in lines
