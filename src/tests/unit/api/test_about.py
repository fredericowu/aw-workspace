"""Settings > About: sysfacts collector + GET /api/about."""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api import about
from src.api.identity import require_identity
from src.libs import sysfacts


def _w(root, rel, text):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)


def test_cpu_v2_limited(tmp_path):
    _w(tmp_path, "cpu.max", "150000 100000\n")
    f = sysfacts.cpu_limit(str(tmp_path), str(tmp_path / 'noproc'))
    assert (f["state"], f["value"]) == ("ok", 1.5)


def test_cpu_v2_max_is_unlimited(tmp_path):
    _w(tmp_path, "cpu.max", "max 100000\n")
    assert sysfacts.cpu_limit(str(tmp_path), str(tmp_path / 'noproc'))["state"] == "unlimited"


def test_cpu_v1_and_v1_unlimited(tmp_path):
    _w(tmp_path, "cpu/cpu.cfs_quota_us", "200000")
    _w(tmp_path, "cpu/cpu.cfs_period_us", "100000")
    assert sysfacts.cpu_limit(str(tmp_path), str(tmp_path / 'noproc'))["value"] == 2.0
    _w(tmp_path, "cpu/cpu.cfs_quota_us", "-1")
    assert sysfacts.cpu_limit(str(tmp_path), str(tmp_path / 'noproc'))["state"] == "unlimited"


def test_cpu_missing_files_is_unknown_not_zero(tmp_path):
    f = sysfacts.cpu_limit(str(tmp_path), str(tmp_path / 'noproc'))
    assert f["state"] == "unknown" and f["value"] is None and f["reason"]


def test_memory_v2_and_max(tmp_path):
    _w(tmp_path, "memory.max", "max")
    _w(tmp_path, "memory.current", "12345")
    limit, used = sysfacts.container_memory(str(tmp_path), str(tmp_path / 'noproc'))
    assert limit["state"] == "unlimited"
    assert (used["state"], used["value"]) == ("ok", 12345)


def test_memory_v1_sentinel_is_unlimited(tmp_path):
    _w(tmp_path, "memory/memory.limit_in_bytes", str(9223372036854771712))
    _w(tmp_path, "memory/memory.usage_in_bytes", "99")
    limit, used = sysfacts.container_memory(str(tmp_path), str(tmp_path / 'noproc'))
    assert limit["state"] == "unlimited" and used["value"] == 99


def test_meminfo(tmp_path):
    _w(tmp_path, "meminfo", "MemTotal:        2048 kB\nMemAvailable:    1024 kB\n")
    f = sysfacts.machine_memory(str(tmp_path))
    assert f["value"] == {"total": 2048 * 1024, "available": 1024 * 1024}
    assert sysfacts.machine_memory(str(tmp_path / "nope"))["state"] == "unknown"


def test_storage_merges_shared_device_and_reports_failure(tmp_path):
    a = tmp_path / "a"
    a.mkdir()
    out = sysfacts.storage([("workspace", str(tmp_path)), ("state", str(a)),
                            ("container_root", str(tmp_path / "missing"))])
    assert out[0]["roles"] == ["workspace", "state"]
    assert out[0]["total"] >= out[0]["used"] >= 0
    assert out[1]["state"] == "unknown"


def test_gpu_none_visible_and_unknown(tmp_path):
    assert sysfacts.gpu(str(tmp_path))["state"] == "none_visible"
    (tmp_path / "dri").mkdir()
    (tmp_path / "dri" / "renderD128").write_text("")
    assert sysfacts.gpu(str(tmp_path))["state"] == "unknown"


def test_gpu_ok_via_nvidia_smi(tmp_path, monkeypatch):
    (tmp_path / "nvidiactl").write_text("")

    class R:
        stdout = "Tesla T4, 15360, 100\n"

    monkeypatch.setattr(sysfacts.subprocess, "run", lambda *a, **k: R())
    f = sysfacts.gpu(str(tmp_path))
    assert f["state"] == "ok" and f["value"][0]["memory_total"] == 15360 * 1048576


def test_deployment_mode():
    assert sysfacts.collect(env={})["deployment"] == "standalone"
    assert sysfacts.collect(env={"AW_BACKEND_URL": "x"})["deployment"] == "cloud_linked"


def _client(authed: bool):
    app = FastAPI()
    about.register_about_routes(app)
    if authed:
        app.dependency_overrides[require_identity] = lambda: {"sub": "u"}
    return TestClient(app)


def test_about_requires_identity():
    about._cache["value"] = None
    assert _client(False).get("/api/about").status_code == 401


def test_about_payload(monkeypatch):
    about._cache["value"] = None
    monkeypatch.setenv("AW_WORKSPACE_VERSION", "1.2.3")
    body = _client(True).get("/api/about").json()
    assert body["version"]["core"] == "1.2.3"
    for k in ("container", "machine", "storage", "gpu", "deployment"):
        assert k in body


def test_cgroup_resolved_from_proc_self_cgroup_v2(tmp_path):
    cg, proc = tmp_path / "cg", tmp_path / "proc"
    _w(proc, "self/cgroup", "0::/docker/abc\n")
    _w(cg, "memory.current", "999999")  # hierarchy root must NOT be reported
    _w(cg, "docker/abc/cpu.max", "50000 100000")
    _w(cg, "docker/abc/memory.max", str(512 * 1048576))
    _w(cg, "docker/abc/memory.current", "4096")
    assert sysfacts.cpu_limit(str(cg), str(proc))["value"] == 0.5
    limit, used = sysfacts.container_memory(str(cg), str(proc))
    assert limit["value"] == 512 * 1048576 and used["value"] == 4096


def test_cgroup_resolved_v1(tmp_path):
    cg, proc = tmp_path / "cg", tmp_path / "proc"
    _w(proc, "self/cgroup", "4:cpu,cpuacct:/docker/abc\n3:memory:/docker/abc\n")
    _w(cg, "cpu/docker/abc/cpu.cfs_quota_us", "100000")
    _w(cg, "cpu/docker/abc/cpu.cfs_period_us", "100000")
    _w(cg, "memory/docker/abc/memory.limit_in_bytes", "1048576")
    _w(cg, "memory/docker/abc/memory.usage_in_bytes", "10")
    assert sysfacts.cpu_limit(str(cg), str(proc))["value"] == 1.0
    assert sysfacts.container_memory(str(cg), str(proc))[0]["value"] == 1048576


def test_gpu_na_field_keeps_rows(tmp_path, monkeypatch):
    (tmp_path / "nvidiactl").write_text("")

    class R:
        stdout = "A100, 40960, 10\nT4, 15360, [N/A]\n"

    monkeypatch.setattr(sysfacts.subprocess, "run", lambda *a, **k: R())
    f = sysfacts.gpu(str(tmp_path))
    assert f["state"] == "ok" and len(f["value"]) == 2
    assert f["value"][1]["memory_used"] is None and f["value"][1]["memory_total"] == 15360 * 1048576


def test_about_cache_coalesces_concurrent_refreshes(monkeypatch):
    import threading
    import time as _t
    about._cache["value"] = None
    calls = []

    def slow():
        calls.append(1)
        _t.sleep(0.2)
        return {"x": 1}

    monkeypatch.setattr(about.sysfacts, "collect", slow)
    ts = [threading.Thread(target=about._facts) for _ in range(8)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert len(calls) == 1
