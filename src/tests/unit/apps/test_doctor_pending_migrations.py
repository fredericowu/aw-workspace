"""``doctor``'s defence-in-depth signal for a half-migrated loaded app (card
core-app-migration-failures-are-silently-swallowed-instead-of-failing-the-
installreload): a currently-loaded app whose ``_app_migrations`` ledger is
behind its manifest's migrations dir is exactly the shape a swallowed
migration failure used to leave running, undetected, in production.
"""
from types import SimpleNamespace

from src.apps import migrations as migrations_mod
from src.apps.routes import _app_pending_migrations


def _app(slug, *, package_dir="/pkg", migrations=None):
    return SimpleNamespace(
        package_dir=package_dir,
        manifest=SimpleNamespace(id=slug, migrations=migrations or {}),
    )


class _Runtime:
    def __init__(self, apps):
        self._apps = apps

    def loaded_slugs(self):
        return list(self._apps)

    def get(self, slug):
        return self._apps.get(slug)


def test_an_app_with_no_migrations_dir_is_skipped(monkeypatch):
    monkeypatch.setattr(migrations_mod, "pending_migrations", lambda *a: (_ for _ in ()).throw(
        AssertionError("must not be reached — app declares no migrations dir")))
    rt = _Runtime({"kb": _app("kb", migrations={})})
    assert _app_pending_migrations(rt) == []


def test_an_app_fully_migrated_is_not_reported(monkeypatch):
    monkeypatch.setattr(migrations_mod, "pending_migrations", lambda app_id, d: [])
    rt = _Runtime({"research-search": _app("research-search", migrations={"dir": "migrations"})})
    assert _app_pending_migrations(rt) == []


def test_an_app_behind_its_ledger_is_reported(monkeypatch):
    """The exact research-search v0.35.0 incident shape: loaded, serving
    routes, but ``_app_migrations`` stopped one file short."""
    monkeypatch.setattr(migrations_mod, "pending_migrations",
                         lambda app_id, d: ["0009_graph_identity.sql"])
    rt = _Runtime({"research-search": _app("research-search", migrations={"dir": "migrations"})})
    assert _app_pending_migrations(rt) == [
        {"app": "research-search", "pending": ["0009_graph_identity.sql"]},
    ]


def test_a_slug_that_failed_to_load_does_not_break_the_report():
    rt = _Runtime({"ghost": None})
    assert _app_pending_migrations(rt) == []
