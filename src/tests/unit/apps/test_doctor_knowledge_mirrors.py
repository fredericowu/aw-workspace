"""``doctor``'s SET+CLEAR+GATE check for the ``contributes.knowledge`` mirror
(card 4145bf3b-9510-81df-be86-d2e2e09acfeb): on 2026-11-08 all ~10 declared
app knowledge mirrors vanished for ~23h with zero signal anywhere doctor or
an agent would look. This exact degradation-signal shape has already
produced two silent failures on an earlier card:

* SET with no CLEAR path — the row went permanently red on a healthy
  workspace (see ``test_restoring_the_mirror_clears_the_row`` below).
* a row reaching ``/api/health`` without flipping ``doctor``'s own ``ok``
  field (``test_doctor_knowledge_mirrors_gate.py`` pins that one — this
  file only covers the row-detection half).

These tests pin the three states that matter (missing, hash-mismatched,
healthy) and mutation-test the hash path: a corrupted marker is reported,
and restoring it clears the row again rather than staying red.
"""
import os
from types import SimpleNamespace

import pytest

from src.apps import paths
from src.apps.knowledge import HASH_MARKER, KnowledgeRegistry
from src.apps.routes import _knowledge_mirror_problems


def _app(slug, package_dir, *, knowledge=None):
    return SimpleNamespace(
        package_dir=str(package_dir),
        manifest=SimpleNamespace(id=slug, knowledge=knowledge),
    )


class _Runtime:
    def __init__(self, apps):
        self._apps = apps

    def loaded_slugs(self):
        return list(self._apps)

    def get(self, slug):
        return self._apps.get(slug)


@pytest.fixture
def kb_apps_dir(tmp_path, monkeypatch):
    d = tmp_path / "knowledge_base" / "apps"
    d.mkdir(parents=True)
    monkeypatch.setattr(paths, "knowledge_apps_dir", lambda: str(d))
    return d


def _write_source(pkg_dir, *, text="hello"):
    docs = pkg_dir / "docs"
    docs.mkdir(parents=True)
    (docs / "readme.md").write_text(text)
    return docs


def _mirror(kb_apps_dir, slug, src_docs):
    """Copy ``src_docs`` into ``kb_apps_dir/<slug>/docs`` and stamp the hash
    marker, the exact shape ``KnowledgeRegistry.register`` leaves behind."""
    dest = kb_apps_dir / slug / "docs"
    dest.mkdir(parents=True)
    for name in os.listdir(src_docs):
        (dest / name).write_bytes((src_docs / name).read_bytes())
    source_hash = KnowledgeRegistry._hash_dir(str(src_docs))
    (dest / HASH_MARKER).write_text(source_hash)
    return dest, source_hash


def test_an_app_with_no_contributes_knowledge_is_skipped(tmp_path, kb_apps_dir):
    rt = _Runtime({"kb": _app("kb", tmp_path / "kb_pkg", knowledge=None)})
    assert _knowledge_mirror_problems(rt) == []


def test_a_fresh_matching_mirror_is_healthy(tmp_path, kb_apps_dir):
    """The current (2026-11-10) real state of this workspace: every
    declared mirror present with a matching hash."""
    pkg = tmp_path / "notion_pkg"
    src_docs = _write_source(pkg)
    _mirror(kb_apps_dir, "notion", src_docs)
    rt = _Runtime({"notion": _app("notion", pkg, knowledge={"path": "docs"})})
    assert _knowledge_mirror_problems(rt) == []


def test_a_missing_mirror_is_reported(tmp_path, kb_apps_dir):
    """SET: the exact 2026-11-08 incident shape — the app is loaded and
    declares ``contributes.knowledge``, but ``apps/<id>/docs/`` does not
    exist."""
    pkg = tmp_path / "ssh_pkg"
    _write_source(pkg)
    rt = _Runtime({"ssh": _app("ssh", pkg, knowledge={"path": "docs"})})
    assert _knowledge_mirror_problems(rt) == [{"app": "ssh", "reason": "mirror missing"}]


def test_a_corrupted_hash_marker_is_reported(tmp_path, kb_apps_dir):
    """A mirror whose ``.aw-knowledge-source-hash`` no longer matches the
    real source content (stale, hand-edited, or corrupted) must not read
    as healthy just because the directory exists."""
    pkg = tmp_path / "secrets_pkg"
    src_docs = _write_source(pkg)
    dest, _ = _mirror(kb_apps_dir, "secrets", src_docs)
    (dest / HASH_MARKER).write_text("not-a-real-hash")
    rt = _Runtime({"secrets": _app("secrets", pkg, knowledge={"path": "docs"})})
    assert _knowledge_mirror_problems(rt) == [
        {"app": "secrets", "reason": "mirror content stale (hash mismatch)"},
    ]


def test_restoring_the_mirror_clears_the_row(tmp_path, kb_apps_dir):
    """CLEAR: fixing the corruption above makes the row disappear again —
    pinning that this check has a clear path, not just a set path. The
    earlier card on this same SET+CLEAR+GATE shape went permanently red on
    a healthy workspace for lacking exactly this."""
    pkg = tmp_path / "kb_pkg"
    src_docs = _write_source(pkg)
    dest, source_hash = _mirror(kb_apps_dir, "kb", src_docs)
    (dest / HASH_MARKER).write_text("corrupted")
    rt = _Runtime({"kb": _app("kb", pkg, knowledge={"path": "docs"})})
    assert _knowledge_mirror_problems(rt) != []

    (dest / HASH_MARKER).write_text(source_hash)
    assert _knowledge_mirror_problems(rt) == []


def test_an_app_declaring_knowledge_but_missing_source_dir_is_reported(tmp_path, kb_apps_dir):
    """The source side can rot too — ``resolve_knowledge_dir`` raises when
    the declared path no longer exists in the installed package."""
    pkg = tmp_path / "broken_pkg"
    pkg.mkdir()
    rt = _Runtime({"broken": _app("broken", pkg, knowledge={"path": "docs"})})
    problems = _knowledge_mirror_problems(rt)
    assert len(problems) == 1
    assert problems[0]["app"] == "broken"
    assert "source dir invalid" in problems[0]["reason"]


def test_a_slug_that_failed_to_load_does_not_break_the_report(kb_apps_dir):
    rt = _Runtime({"ghost": None})
    assert _knowledge_mirror_problems(rt) == []
