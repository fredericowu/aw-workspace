"""Read-only machine/container facts for Settings > About (``GET /api/about``).

Stdlib only (psutil is not installed). Every fact is an envelope
``{state, value, unit, scope, source}`` (+ ``reason`` when not ``ok``):

* ``ok``           - measured, ``value`` set
* ``unlimited``    - a limit exists as a concept and is explicitly unset
* ``none_visible`` - nothing of this kind is exposed to this container
* ``unavailable``  - cannot be measured from here by construction
* ``unknown``      - a probe was attempted and failed (``reason`` says why)

Never a bare null and never 0 standing in for "missing". Bytes on the wire,
CPU in cores (float). Filesystem roots are injectable for tests.
"""
from __future__ import annotations

import glob
import os
import shutil
import subprocess
from typing import Any

_NO_LIMIT_V1 = 1 << 60  # cgroup v1 "no limit" sentinel is a huge number


def fact(state: str, value: Any = None, unit: str | None = None, scope: str = "",
         source: str = "", reason: str | None = None) -> dict:
    out = {"state": state, "value": value, "unit": unit, "scope": scope, "source": source}
    if reason is not None:
        out["reason"] = reason
    return out


def _read(path: str) -> str | None:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return fh.read().strip()
    except OSError:
        return None


def cpu_limit(cgroup_root: str = "/sys/fs/cgroup") -> dict:
    scope, unit = "container", "cores"
    raw = _read(f"{cgroup_root}/cpu.max")
    if raw is not None:
        src = "cgroup v2 cpu.max"
        parts = raw.split()
        try:
            if parts[0] == "max":
                return fact("unlimited", None, unit, scope, src)
            quota, period = int(parts[0]), int(parts[1]) if len(parts) > 1 else 100000
            return fact("ok", round(quota / period, 3), unit, scope, src)
        except (ValueError, IndexError, ZeroDivisionError):
            return fact("unknown", None, unit, scope, src, reason=f"unparseable cpu.max {raw!r}")
    q = _read(f"{cgroup_root}/cpu/cpu.cfs_quota_us")
    p = _read(f"{cgroup_root}/cpu/cpu.cfs_period_us")
    src = "cgroup v1 cpu.cfs_quota_us"
    if q is None:
        return fact("unknown", None, unit, scope, "cgroup", reason="no cgroup cpu limit file readable")
    try:
        if int(q) < 0:
            return fact("unlimited", None, unit, scope, src)
        return fact("ok", round(int(q) / int(p or 100000), 3), unit, scope, src)
    except ValueError:
        return fact("unknown", None, unit, scope, src, reason=f"unparseable quota {q!r}")


def _mem_pair(cgroup_root: str, v2: str, v1: str, label: str, scope: str, v1_sentinel: bool) -> dict:
    raw, src = _read(f"{cgroup_root}/{v2}"), f"cgroup v2 {v2}"
    if raw is None:
        raw, src = _read(f"{cgroup_root}/memory/{v1}"), f"cgroup v1 {v1}"
    if raw is None:
        return fact("unknown", None, "bytes", scope, "cgroup", reason=f"{label}: no cgroup file readable")
    if raw == "max":
        return fact("unlimited", None, "bytes", scope, src)
    try:
        n = int(raw)
    except ValueError:
        return fact("unknown", None, "bytes", scope, src, reason=f"unparseable {raw!r}")
    if v1_sentinel and n >= _NO_LIMIT_V1:
        return fact("unlimited", None, "bytes", scope, src)
    return fact("ok", n, "bytes", scope, src)


def container_memory(cgroup_root: str = "/sys/fs/cgroup") -> tuple[dict, dict]:
    limit = _mem_pair(cgroup_root, "memory.max", "memory.limit_in_bytes", "memory limit", "container", True)
    used = _mem_pair(cgroup_root, "memory.current", "memory.usage_in_bytes", "memory used", "container", False)
    return limit, used


def machine_cpus() -> dict:
    total = os.cpu_count()
    try:
        effective = len(os.sched_getaffinity(0))
    except (AttributeError, OSError):
        effective = total
    if not total:
        return fact("unknown", None, "cores", "kernel", "os.cpu_count", reason="cpu_count returned None")
    return fact("ok", {"total": total, "effective": effective}, "cores", "kernel", "os.cpu_count + sched_getaffinity")


def machine_memory(proc_root: str = "/proc") -> dict:
    raw = _read(f"{proc_root}/meminfo")
    src = "/proc/meminfo"
    if raw is None:
        return fact("unknown", None, "bytes", "kernel", src, reason="meminfo unreadable")
    vals: dict[str, int] = {}
    for line in raw.splitlines():
        k, _, rest = line.partition(":")
        bits = rest.split()
        if bits and bits[0].isdigit():
            vals[k] = int(bits[0]) * (1024 if len(bits) > 1 and bits[1].lower() == "kb" else 1)
    if "MemTotal" not in vals:
        return fact("unknown", None, "bytes", "kernel", src, reason="MemTotal missing")
    return fact("ok", {"total": vals["MemTotal"],
                       "available": vals.get("MemAvailable", vals.get("MemFree"))},
                "bytes", "kernel", src)


def storage(paths: list[tuple[str, str]]) -> list[dict]:
    """``paths`` = [(role, path)]. Entries on the same device are merged."""
    merged: dict[Any, dict] = {}
    out: list[dict] = []
    for role, path in paths:
        try:
            dev = os.stat(path).st_dev
            du = shutil.disk_usage(path)
        except OSError as exc:
            out.append({"roles": [role], "path": path,
                        "state": "unknown", "reason": f"{type(exc).__name__}: {exc.strerror or exc}"})
            continue
        if dev in merged:
            merged[dev]["roles"].append(role)
            continue
        entry = {"roles": [role], "path": path, "state": "ok", "unit": "bytes", "scope": "volume",
                 "source": "shutil.disk_usage", "total": du.total, "used": du.used, "free": du.free}
        merged[dev] = entry
        out.append(entry)
    return out


def gpu(dev_root: str = "/dev") -> dict:
    scope = "container"
    nvidia = os.path.exists(f"{dev_root}/nvidiactl")
    render = glob.glob(f"{dev_root}/dri/renderD*")
    if not nvidia and not render:
        return fact("none_visible", [], None, scope, "device nodes",
                    reason="no GPU is exposed to this container")
    if nvidia:
        try:
            res = subprocess.run(
                ["nvidia-smi", "--query-gpu=name,memory.total,memory.used",
                 "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=2, check=True)
            devices = []
            for line in res.stdout.strip().splitlines():
                name, total, used = [p.strip() for p in line.split(",")]
                devices.append({"name": name, "memory_total": int(total) * 1048576,
                                "memory_used": int(used) * 1048576})
            if devices:
                return fact("ok", devices, "bytes", scope, "nvidia-smi")
        except (OSError, subprocess.SubprocessError, ValueError) as exc:
            return fact("unknown", None, None, scope, "nvidia-smi",
                        reason=f"GPU device node present but nvidia-smi failed: {type(exc).__name__}")
    return fact("unknown", None, None, scope, "device nodes",
                reason="GPU device node present but cannot be described from inside the container")


def collect(*, cgroup_root: str = "/sys/fs/cgroup", proc_root: str = "/proc",
            dev_root: str = "/dev", env: dict | None = None) -> dict:
    env = os.environ if env is None else env
    mem_limit, mem_used = container_memory(cgroup_root)
    paths = []
    for role, key in (("workspace", "AW_WORKSPACE_CONTAINER_DIR"), ("state", "AW_WORKSPACE_HOME")):
        if env.get(key):
            paths.append((role, env[key]))
    paths.append(("container_root", "/"))
    return {
        "container": {"cpu_limit": cpu_limit(cgroup_root), "memory_limit": mem_limit,
                      "memory_used": mem_used},
        "machine": {"cpus": machine_cpus(), "memory": machine_memory(proc_root)},
        "storage": storage(paths),
        "gpu": gpu(dev_root),
        "gpu_passthrough_configured": bool(env.get("AW_HOST_GPU_DEVICES")),
        "deployment": "cloud_linked" if env.get("AW_BACKEND_URL") else "standalone",
    }
