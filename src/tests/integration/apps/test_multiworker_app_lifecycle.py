"""W3 VERIFY: two app instances sharing one Redis + one Postgres.

The card's acceptance criterion, verbatim: *"two app instances sharing one
Redis + one Postgres; install an app through instance A and prove instance B
serves ``/api/apps/<id>/…`` without a restart; uninstall through A and prove B
stops serving it."*

Anything smaller does not test the bug. A single process cannot: the whole
defect is that ``AppRuntime._attach_mount`` appends to ONE FastAPI object, and
two ``create_app()`` instances inside one interpreter would also share
``sys.modules``, which is exactly what ``_import_plugin`` namespaces per app —
so "two workers" has to mean two OS processes. These are spawned as real
uvicorn servers running the real ``create_app()`` (see ``w3_worker.py``).

Requires a reachable Postgres AND a reachable Redis; skips cleanly without
either, same posture as ``test_boot_concurrency.py``. Note that means it can
skip silently on CI — see the knowledge base entry
"core integration tests need a throwaway postgres". The evidence attached to
the W3 card was produced with both live.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time

import httpx
import pytest

#: A real client, NOT the module-level ``httpx.get``. The workspace-wide
#: autouse fixture in ``src/tests/conftest.py`` monkeypatches ``httpx.get``
#: to return a canned 200 for every test (it blocks the marketplace-catalog
#: fetch, and does so at the network primitive so no call site can slip
#: past). That is right for the suite and wrong here — this test's entire
#: job is to make REAL requests to two REAL servers, and going through the
#: patched function made ``wait_healthy`` "succeed" against a port with
#: nothing listening on it. ``Client.get`` is untouched by that fixture.
HTTP = httpx.Client(trust_env=False)

# .../src/tests/integration/apps/ -> the repo root that `src` is a package of.
REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", ".."))

_PG_URL = os.environ.get(
    "AW_TEST_DB_URL", "postgresql://postgres:postgres@127.0.0.1:5432/awserv")
_SCHEMA = "workspace_w3multiworkertest"
_SLUG = "w3probe"
_BOOT_TIMEOUT = 90.0
_CONVERGE_TIMEOUT = 90.0

PLUGIN = '''
import os

from fastapi import FastAPI


class AppPlugin:
    """Records the PID that activated it, so the test can prove BOTH worker
    processes really imported and activated this app rather than one of them
    merely proxying to the other."""

    async def activate(self, ctx):
        sub = FastAPI()
        pid = os.getpid()

        @sub.get("/ping")
        async def ping():
            return {"pong": ctx.app_id, "pid": pid, "provision": ctx.provision}

        ctx.routes.register(sub)
        with open(os.path.join(os.environ["W3_MARKER_DIR"],
                               f"activated-{pid}"), "w") as f:
            f.write("provision=%s\\n" % ctx.provision)

    async def deactivate(self):
        pass
'''


def _reachable(host: str, port: int) -> bool:
    s = socket.socket()
    s.settimeout(2)
    try:
        s.connect((host, port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def _postgres_reachable() -> bool:
    try:
        import psycopg

        psycopg.connect(_PG_URL, autocommit=True, connect_timeout=3).close()
        return True
    except Exception:
        return False


def _redis_reachable() -> bool:
    try:
        import redis

        from src.libs.redis_coord import get_workspace_redis_url

        redis.from_url(get_workspace_redis_url(),
                       socket_connect_timeout=2).ping()
        return True
    except Exception:
        return False


pytestmark = [
    pytest.mark.skipif(not _postgres_reachable(),
                       reason=f"live Postgres not reachable at {_PG_URL}"),
    pytest.mark.skipif(not _redis_reachable(),
                       reason="live Redis not reachable (AW_REDIS_URL / "
                              "AW_WORKSPACE_REDIS_URL)"),
]


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _build_app(root) -> str:
    pkg = root / _SLUG
    pkg.mkdir(parents=True)
    (pkg / "aw-app.json").write_text(json.dumps({
        "manifest_version": 1,
        "id": _SLUG,
        "name": "W3 probe",
        "version": "1.0.0",
        "tier": "inprocess",
        "runtime": {"entrypoint": "plugin:AppPlugin"},
        "permissions": ["routes:register"],
    }))
    (pkg / "plugin.py").write_text(PLUGIN)
    return str(pkg)


class _Worker:
    def __init__(self, name: str, env: dict[str, str]) -> None:
        self.name = name
        self.port = _free_port()
        self.env = env
        self.proc: subprocess.Popen | None = None
        self.log = env["W3_MARKER_DIR"] + f"/{name}.log"

    def start(self) -> None:
        self._fh = open(self.log, "w")
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "src.tests.integration.apps.w3_worker",
             str(self.port)],
            cwd=REPO_ROOT, env={**os.environ, **self.env},
            stdout=self._fh, stderr=subprocess.STDOUT,
        )

    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def wait_healthy(self, timeout: float = _BOOT_TIMEOUT) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.proc is not None and self.proc.poll() is not None:
                raise AssertionError(
                    f"{self.name} exited with {self.proc.returncode}:\n{self.tail()}")
            try:
                if HTTP.get(self.base() + "/api/health", timeout=3).status_code == 200:
                    return
            except Exception:
                pass
            time.sleep(0.5)
        raise AssertionError(f"{self.name} never became healthy:\n{self.tail()}")

    def tail(self, n: int = 60) -> str:
        try:
            with open(self.log) as f:
                return "".join(f.readlines()[-n:])
        except OSError:
            return "(no log)"

    def stop(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=20)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=10)
        if getattr(self, "_fh", None) is not None:
            self._fh.close()


@pytest.fixture()
def cluster(tmp_path, monkeypatch):
    """Two worker processes on ONE Postgres schema, ONE Redis, ONE
    AW_WORKSPACE_HOME — i.e. AW_WORKSPACE_WORKERS=2, spelled out."""
    from src.libs.redis_coord import get_workspace_redis_url

    home = tmp_path / "home"
    home.mkdir()
    # The test process itself resolves the shared env file through
    # paths.workspace_home(); monkeypatch so it is restored at teardown.
    monkeypatch.setenv("AW_WORKSPACE_HOME", str(home))
    markers = tmp_path / "markers"
    markers.mkdir()

    shared = {
        "AW_WORKSPACE_SCHEMA": _SCHEMA,
        "AW_WORKSPACE_DB_URL": _PG_URL,
        "AW_WORKSPACE_HOME": str(home),
        "AW_WORKSPACE": "w3multiworkertest",
        # Declared so the boot path takes the multi-worker branch: exactly one
        # of these two runs the side-effecting boot reconcile and the other
        # takes `attach_on_boot`. They share a parent (this pytest process),
        # which is the claim key — see _is_boot_provisioner.
        "AW_WORKSPACE_WORKERS": "2",
        "AW_REDIS_URL": get_workspace_redis_url(),
        "W3_MARKER_DIR": str(markers),
        # No cloud registry in the test: the LOCAL mirror (shared Postgres) is
        # the desired state, which is exactly the "shared vs per-process"
        # asymmetry the card is about.
        "AW_BACKEND_URL": "",
        "AW_WORKSPACE_LINK_KEY": "",
        # PREPENDED, never replaced: this workspace reaches its site-packages
        # through PYTHONPATH, so overwriting it outright is how a spawned
        # process ends up with "No module named fastapi" (the same trap
        # `aw-workspace-cli test` hits with pytest — see the
        # aw-autoskill-core-pytest-direct skill).
        "PYTHONPATH": os.pathsep.join(
            [REPO_ROOT, *([p] if (p := os.environ.get("PYTHONPATH")) else [])]),
    }
    a = _Worker("A", shared)
    b = _Worker("B", shared)
    try:
        a.start()
        b.start()
        a.wait_healthy()
        b.wait_healthy()
        yield a, b, markers
    finally:
        a.stop()
        b.stop()
        _drop_schema()


def _drop_schema() -> None:
    import psycopg

    with psycopg.connect(_PG_URL, autocommit=True, connect_timeout=5) as conn:
        conn.execute(f'DROP SCHEMA IF EXISTS "{_SCHEMA}" CASCADE')


def _api_key() -> str:
    """Both workers publish the SAME key into the shared env file (W2).

    Resolved through ``workspace_api_key._env_path`` rather than hardcoded, so
    it follows ``AW_WORKSPACE_HOME`` wherever W2's flock-guarded writer puts
    it. That resolution reads ``os.environ``, which the ``cluster`` fixture
    points at the same home the workers got — via ``monkeypatch``, so it is
    undone at teardown. Setting it directly leaked a deleted tmp dir into
    every later test in the session and broke three of them.
    """
    from src.api.workspace_api_key import _env_path

    path = _env_path()
    for line in open(path):
        if line.startswith("AW_WORKSPACE_API_KEY="):
            return line.split("=", 1)[1].strip()
    raise AssertionError(f"no AW_WORKSPACE_API_KEY in {path}")


def _poll(fn, timeout: float, what: str):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            ok, last = fn()
            if ok:
                return last, time.time() - (deadline - timeout)
        except Exception as e:  # noqa: BLE001 — polling, the last state is the report
            last = e
        time.sleep(0.5)
    raise AssertionError(f"timed out after {timeout}s waiting for {what}; last={last!r}")


def test_install_on_A_is_served_by_B_and_uninstall_on_A_stops_B(cluster, tmp_path):
    a, b, markers = cluster
    pkg = _build_app(tmp_path / "pkgs")
    key = _api_key()
    h = {"X-Api-Key": key}

    # Precondition: NEITHER worker serves the app yet.
    for w in (a, b):
        r = HTTP.get(f"{w.base()}/api/apps/{_SLUG}/ping", headers=h, timeout=10)
        assert r.status_code == 404, f"{w.name} already serves {_SLUG}: {r.status_code}"

    # --- install through instance A only ---------------------------------
    r = HTTP.post(f"{a.base()}/api/apps/install", headers=h, timeout=30,
                   json={"package_dir": pkg})
    assert r.status_code == 202, r.text

    def _a_installed():
        s = HTTP.get(f"{a.base()}/api/apps/{_SLUG}/install-status",
                      headers=h, timeout=10).json()
        return s.get("status") == "installed", s

    status, _ = _poll(_a_installed, 120.0, "A to finish installing")
    print(f"\n[A install-status] {status}")

    # The install-job state is reachable from the OTHER worker (card
    # constraint 2) — this poll never touched A.
    s_from_b = HTTP.get(f"{b.base()}/api/apps/{_SLUG}/install-status",
                         headers=h, timeout=10)
    print(f"[B install-status] {s_from_b.status_code} {s_from_b.text}")
    assert s_from_b.status_code == 200, s_from_b.text
    assert s_from_b.json()["status"] == "installed"

    # --- THE ASSERTION: B serves the app's routes, with no restart -------
    def _b_serves():
        r = HTTP.get(f"{b.base()}/api/apps/{_SLUG}/ping", headers=h, timeout=10)
        return r.status_code == 200, (r.status_code, r.text)

    served, waited = _poll(_b_serves, _CONVERGE_TIMEOUT,
                           "B to serve the app installed through A")
    print(f"[B /api/apps/{_SLUG}/ping] {served} (converged in ~{waited:.1f}s)")

    body_b = HTTP.get(f"{b.base()}/api/apps/{_SLUG}/ping", headers=h, timeout=10).json()
    body_a = HTTP.get(f"{a.base()}/api/apps/{_SLUG}/ping", headers=h, timeout=10).json()
    print(f"[A body] {body_a}\n[B body] {body_b}")

    assert body_a["pong"] == body_b["pong"] == _SLUG
    # Two genuinely different processes each activated the plugin...
    assert body_a["pid"] != body_b["pid"], (
        "both responses came from one process — this did not test two workers")
    assert {a.proc.pid, b.proc.pid} == {body_a["pid"], body_b["pid"]}
    # ...and exactly one of them did it as the PROVISIONING half.
    assert body_a["provision"] is True, "A took the request, so A provisioned"
    assert body_b["provision"] is False, (
        "B ran the side-effecting half too — the split failed")

    markers_written = sorted(p.name for p in markers.iterdir() if p.name.startswith("activated-"))
    print(f"[activation markers] {markers_written}")
    assert len(markers_written) == 2, markers_written

    # B is genuinely serving it, not answering out of a stale list
    listed = HTTP.get(f"{b.base()}/api/apps", headers=h, timeout=10).json()
    entry = next(x for x in listed if x["slug"] == _SLUG)
    print(f"[B /api/apps entry] {entry['slug']} routes={entry['routes']} "
          f"version={entry['version']}")
    assert entry["routes"] is True

    # --- uninstall through instance A only -------------------------------
    r = HTTP.delete(f"{a.base()}/api/apps/{_SLUG}", headers=h, timeout=60)
    assert r.status_code == 200, r.text
    print(f"[A uninstall] {r.json()}")

    r_a = HTTP.get(f"{a.base()}/api/apps/{_SLUG}/ping", headers=h, timeout=10)
    assert r_a.status_code == 404, f"A still serves it: {r_a.status_code}"

    def _b_stopped():
        r = HTTP.get(f"{b.base()}/api/apps/{_SLUG}/ping", headers=h, timeout=10)
        return r.status_code == 404, r.status_code

    stopped, waited = _poll(_b_stopped, _CONVERGE_TIMEOUT,
                            "B to stop serving the app uninstalled through A")
    print(f"[B after uninstall] {stopped} (converged in ~{waited:.1f}s)")

    listed = HTTP.get(f"{b.base()}/api/apps", headers=h, timeout=10).json()
    assert not any(x["slug"] == _SLUG for x in listed), listed
    print("[B /api/apps] no longer lists the app\n")
