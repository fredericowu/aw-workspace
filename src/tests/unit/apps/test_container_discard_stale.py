"""Removing a container podman still believes is running.

THE INCIDENT (2026-09-11). podman keeps container metadata in the graphroot
(which survives its host container being recreated) and runtime state in the
runroot (which does not), and it detects "new boot" from the HOST kernel's
boot_id — unchanged by a container restart. So after aw-remote-host is
recreated, every nested container is recorded as running with a pid that no
longer exists, and `remove(force=True)` fails trying to stop it:

    cannot remove container ... as it could not be stopped:
    conmon exited prematurely, exit code could not be retrieved

start() removes the stale container so the name is free; when that raise
propagated, no app could be started at all. 19 app containers had to be
reconciled and removed by hand, one batch at a time.
"""

import pytest

from src.apps.containers import _discard


class FakeContainer:
    """Refuses the first forced remove exactly the way podman does for a
    container whose process is gone but whose record says running."""

    def __init__(self, *, refuse_removes=0, stop_raises=False):
        self.refuse_removes = refuse_removes
        self.stop_raises = stop_raises
        self.calls: list[str] = []

    def remove(self, force=False):
        self.calls.append("remove")
        if self.refuse_removes > 0:
            self.refuse_removes -= 1
            raise RuntimeError(
                "cannot remove container abc as it could not be stopped: "
                "conmon exited prematurely, exit code could not be retrieved")

    def stop(self, timeout=None):
        self.calls.append("stop")
        if self.stop_raises:
            raise RuntimeError("container abc is not running")


def test_a_healthy_container_is_removed_without_ceremony():
    c = FakeContainer()
    _discard(c, "aw-app-kb")
    assert c.calls == ["remove"], "no stop for a container that removes cleanly"


def test_a_stale_container_is_reconciled_then_removed():
    """THE REGRESSION. A stop first corrects podman's bookkeeping — the
    process is already gone, so this only fixes the record — after which the
    remove succeeds."""
    c = FakeContainer(refuse_removes=1)
    _discard(c, "aw-app-kb")
    assert c.calls == ["remove", "stop", "remove"]


def test_a_stop_that_errors_does_not_stop_the_repair():
    """podman errors on that stop too — loudly, and harmlessly: the process it
    is being asked to kill is already dead. Treating that as fatal would
    reintroduce the bug one line further down."""
    c = FakeContainer(refuse_removes=1, stop_raises=True)
    _discard(c, "aw-app-kb")
    assert c.calls == ["remove", "stop", "remove"]


def test_a_container_that_truly_cannot_go_still_raises():
    """Silently continuing would leave start() to fail on a taken name with a
    far more confusing error than this one."""
    c = FakeContainer(refuse_removes=2)
    with pytest.raises(RuntimeError, match="conmon exited prematurely"):
        _discard(c, "aw-app-kb")
    assert c.calls == ["remove", "stop", "remove"]


# ── the call site ─────────────────────────────────────────────────────────
#
# Testing _discard alone leaves the bug reachable: start()'s stale cleanup
# could go back to a bare remove(force=True) and every test above would still
# pass while no app could be started on a host that had just been recreated.

class _FakeClient:
    def __init__(self, existing):
        self._existing = existing
        self.containers = self
        self.images = self

    def get(self, name):
        if self._existing is None:
            from docker.errors import NotFound
            raise NotFound(name)
        return self._existing

    def pull(self, *a, **kw):
        return None

    def run(self, *a, **kw):
        class _C:
            id = "new"
        return _C()


def _supervisor(monkeypatch, existing):
    from src.apps import containers as mod

    sup = mod.ContainerSupervisor.__new__(mod.ContainerSupervisor)
    client = _FakeClient(existing)
    monkeypatch.setattr(sup, "_docker_for_create", lambda: client, raising=False)
    monkeypatch.setattr(sup, "_docker_for_pull", lambda: client, raising=False)
    return sup, mod


def test_start_repairs_a_stale_container_instead_of_raising(monkeypatch):
    """A host that was just recreated has every nested container recorded as
    running with a dead pid. Before this, start()'s cleanup raised on the
    first one and no app could come up at all — 19 of them needed removing by
    hand on 2026-09-11."""
    stale = FakeContainer(refuse_removes=1)
    sup, mod = _supervisor(monkeypatch, stale)
    called: list[str] = []
    real = mod._discard
    monkeypatch.setattr(mod, "_discard", lambda c, n: (called.append(n), real(c, n))[1])

    c = mod._Container(app_id="kb", image="img", port=1, run_flags=None,
                       resources=None, env=None, network=None)
    monkeypatch.setattr(sup, "_require", lambda app_id: c, raising=False)
    try:
        sup.start("kb")
    except Exception:
        pass  # the rest of start() is out of scope; the cleanup is what matters

    assert called == ["aw-app-kb"], "start() must route its stale cleanup through _discard"
    assert stale.calls == ["remove", "stop", "remove"]
