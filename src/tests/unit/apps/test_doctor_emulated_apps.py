"""``doctor``'s ``emulated_apps`` section (card
4155bf3b-9510-8127-9303-d3eeb4922daf): a running Tier-2 container whose
image architecture doesn't match the engine's own must be a visible row, not
silent — auto-falling back to an emulated pull is deliberate, running
several times slower with nobody able to tell why is this workspace's named
failure mode.

Pins the row-detection half (SET + CLEAR); the GATE half — that a non-empty
row set actually flips ``doctor``'s own exit code — is pinned separately in
``tests/unit/cli/test_doctor_emulated_apps_gate.py``, mirroring the split
already used for ``knowledge_mirrors``.
"""
from types import SimpleNamespace

from src.apps.routes import _emulated_apps


class _Containers:
    def __init__(self, rows):
        self._rows = rows

    def registered(self):
        return [(row["app"], SimpleNamespace(image=row["image"])) for row in self._rows]

    def status(self, app_id: str):
        row = next(r for r in self._rows if r["app"] == app_id)
        if row.get("raises"):
            raise RuntimeError("engine unreachable")
        return {
            "emulated": row["emulated"],
            "image_arch": row.get("image_arch"),
            "emulated_detail": row.get("detail"),
        }


def _runtime(rows):
    return SimpleNamespace(containers=_Containers(rows))


def test_no_running_containers_reports_nothing():
    assert _emulated_apps(_runtime([])) == []


def test_a_native_arch_container_is_not_reported():
    rows = [{"app": "kb", "image": "ghcr.io/tekflox/aw-app-kb:latest",
             "emulated": False, "image_arch": "arm64"}]
    assert _emulated_apps(_runtime(rows)) == []


def test_an_emulated_container_is_reported():
    """SET: the exact failure mode — an arm64 host running an amd64-only
    image under qemu, after the pull fallback kicked in."""
    rows = [{"app": "kb", "image": "ghcr.io/tekflox/aw-app-kb:latest",
             "emulated": True, "image_arch": "amd64",
             "detail": "running emulated: image is amd64, host is arm64"}]
    assert _emulated_apps(_runtime(rows)) == [{
        "app": "kb",
        "image": "ghcr.io/tekflox/aw-app-kb:latest",
        "image_arch": "amd64",
        "detail": "running emulated: image is amd64, host is arm64",
    }]


def test_clearing_emulation_drops_the_row():
    """CLEAR: once a native image is published and the app restarts onto
    it, the next doctor call must stop reporting the row — no persisted
    marker survives the transition because none is kept."""
    rows = [{"app": "kb", "image": "ghcr.io/tekflox/aw-app-kb:latest",
             "emulated": True, "image_arch": "amd64", "detail": "x"}]
    assert _emulated_apps(_runtime(rows)) != []

    rows[0]["emulated"] = False
    assert _emulated_apps(_runtime(rows)) == []


def test_an_unreadable_container_does_not_break_the_rest_of_the_report():
    rows = [
        {"app": "broken", "image": "ghcr.io/example/broken:latest", "raises": True},
        {"app": "kb", "image": "ghcr.io/tekflox/aw-app-kb:latest",
         "emulated": True, "image_arch": "amd64", "detail": "x"},
    ]
    problems = _emulated_apps(_runtime(rows))
    assert [p["app"] for p in problems] == ["kb"]


def test_no_container_supervisor_reports_nothing():
    assert _emulated_apps(SimpleNamespace(containers=None)) == []
