"""aw-workspace-cli marketplace update-all — only outdated installed apps are
updated, and updates are ordered so a dependency runs before its dependents."""
from __future__ import annotations

from src.cli.commands import marketplace as marketplace_mod


def test_dependency_order_places_deps_before_dependents():
    outdated = {
        "browser": {"dependencies": {"apps": ["proxy"]}},
        "proxy": {"dependencies": {}},
        "notes": {"dependencies": {"apps": [{"id": "proxy", "required": True}]}},
    }
    order = marketplace_mod._dependency_order(outdated)
    assert order.index("proxy") < order.index("browser")
    assert order.index("proxy") < order.index("notes")


def test_dependency_order_ignores_optional_and_out_of_set_dependencies():
    outdated = {
        # depends on an app that isn't itself outdated — shouldn't gate order
        "a": {"dependencies": {"apps": ["already-current"]}},
        # optional dependency on b — shouldn't force b before c
        "c": {"dependencies": {"apps": [{"id": "b", "optional": True}]}},
        "b": {"dependencies": {}},
    }
    order = marketplace_mod._dependency_order(outdated)
    assert set(order) == {"a", "c", "b"}


def test_dependency_order_breaks_cycles_instead_of_hanging(capsys):
    outdated = {
        "a": {"dependencies": {"apps": ["b"]}},
        "b": {"dependencies": {"apps": ["a"]}},
    }
    order = marketplace_mod._dependency_order(outdated)
    assert set(order) == {"a", "b"}
    assert "cyclic dependency" in capsys.readouterr().out


def test_update_all_only_updates_outdated_apps(monkeypatch):
    calls = []

    def fake_request(method, path, json_body=None, timeout=30.0):
        if path == "/api/apps":
            return 200, [
                {"slug": "git", "version": "1.0.0"},
                {"slug": "notes", "version": "2.0.0"},
            ]
        if path == "/api/apps/-/catalog":
            return 200, {"apps": [
                {"id": "git", "version": "1.0.0", "dependencies": {}},
                {"id": "notes", "version": "2.1.0", "dependencies": {}},
            ]}
        calls.append((method, path))
        return 200, {"status": "installed", "version": "2.1.0"}

    monkeypatch.setattr(marketplace_mod.local_client, "request", fake_request)
    monkeypatch.setattr(marketplace_mod, "_POLL_INTERVAL", 0)

    assert marketplace_mod._update_all() == 0
    update_calls = [c for c in calls if c[0] == "POST" and c[1].endswith("/update")]
    assert update_calls == [("POST", "/api/apps/notes/update")]


def test_update_all_reports_when_everything_is_current(monkeypatch, capsys):
    def fake_request(method, path, json_body=None, timeout=30.0):
        if path == "/api/apps":
            return 200, [{"slug": "git", "version": "1.0.0"}]
        if path == "/api/apps/-/catalog":
            return 200, {"apps": [{"id": "git", "version": "1.0.0", "dependencies": {}}]}
        raise AssertionError(f"unexpected request: {method} {path}")

    monkeypatch.setattr(marketplace_mod.local_client, "request", fake_request)

    assert marketplace_mod._update_all() == 0
    assert "already up to date" in capsys.readouterr().out


def test_info_lists_installed_apps_sorted_by_slug(monkeypatch, capsys):
    def fake_request(method, path, json_body=None, timeout=30.0):
        if path == "/api/apps":
            return 200, [
                {"slug": "notes", "version": "2.0.0"},
                {"slug": "git", "version": "1.0.0"},
            ]
        raise AssertionError(f"unexpected request: {method} {path}")

    monkeypatch.setattr(marketplace_mod.local_client, "request", fake_request)

    assert marketplace_mod._info() == 0
    out = capsys.readouterr().out.splitlines()
    assert out == ["git 1.0.0", "notes 2.0.0"]


def test_info_reports_when_nothing_installed(monkeypatch, capsys):
    monkeypatch.setattr(
        marketplace_mod.local_client, "request", lambda *a, **k: (200, [])
    )

    assert marketplace_mod._info() == 0
    assert "no apps installed" in capsys.readouterr().out
