"""aw-workspace-cli marketplace update-all — only outdated installed apps are
updated, and updates are ordered so a dependency runs before its dependents.
Plus: every catalog read forces a server-side cache refresh."""
from __future__ import annotations

from src.cli.commands import marketplace as marketplace_mod


def test_catalog_path_forces_a_refresh():
    assert marketplace_mod._CATALOG_PATH == "/api/apps/-/catalog?refresh=true"


def test_catalog_reads_always_bypass_the_server_ttl_cache(monkeypatch):
    """Every command that resolves a catalog entry must ask for a fresh fetch —
    reading the TTL cache resolves a pre-release version and then reports a
    successful install of it."""
    paths = []

    def fake_request(method, path, json_body=None, timeout=30.0):
        paths.append(path)
        if path == "/api/apps":
            return 200, [{"slug": "git", "version": "1.0.0"}]
        return 200, {"apps": [{"id": "git", "version": "2.0.0", "dependencies": {}}]}

    monkeypatch.setattr(marketplace_mod.local_client, "request", fake_request)

    marketplace_mod._catalog_apps()
    marketplace_mod._catalog_apps()

    catalog_reads = [p for p in paths if p.startswith("/api/apps/-/catalog")]
    assert len(catalog_reads) == 2
    assert all(p == "/api/apps/-/catalog?refresh=true" for p in catalog_reads)


def test_catalog_read_uses_the_longer_refresh_timeout(monkeypatch):
    """A forced refresh re-fetches every marketplace source over the network,
    so it must not run under the client's default 30s timeout."""
    seen = {}

    def fake_request(method, path, json_body=None, timeout=30.0):
        seen["timeout"] = timeout
        return 200, {"apps": []}

    monkeypatch.setattr(marketplace_mod.local_client, "request", fake_request)

    marketplace_mod._catalog_apps()
    assert seen["timeout"] == marketplace_mod._CATALOG_TIMEOUT > 30.0


def test_install_fails_when_the_catalog_fetch_fails(monkeypatch, capsys):
    monkeypatch.setattr(
        marketplace_mod.local_client, "request", lambda *a, **k: (500, "boom")
    )
    assert marketplace_mod._install("git", update=False) == 1
    assert "could not load the marketplace catalog" in capsys.readouterr().out


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
        if path == marketplace_mod._CATALOG_PATH:
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
        if path == marketplace_mod._CATALOG_PATH:
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


# --- transient server slowness (2026-11-19: host load ~14, ReadTimeout) -------

import httpx
import pytest


@pytest.fixture
def fast(monkeypatch):
    monkeypatch.setattr(marketplace_mod, "_POLL_INTERVAL", 0)
    monkeypatch.setattr(marketplace_mod, "_RETRY_BACKOFF_S", 0)
    monkeypatch.setattr(marketplace_mod.local_client.time, "sleep", lambda s: None)


def test_poll_survives_readtimeouts_and_completes(monkeypatch, fast, capsys):
    answers = iter([httpx.ReadTimeout(""), httpx.ReadTimeout(""),
                    (200, {"status": "installing"}), (200, {"status": "installed"})])

    def fake_request(method, path, json_body=None, timeout=30.0):
        a = next(answers)
        if isinstance(a, Exception):
            raise a
        return a

    monkeypatch.setattr(marketplace_mod.local_client, "request", fake_request)
    assert marketplace_mod._poll_until_done("git") is True
    out = capsys.readouterr().out
    assert out.count("server slow to answer") == 1  # one line per streak, not per poll


def test_poll_gives_up_only_at_the_deadline(monkeypatch, fast, capsys):
    def boom(*a, **k):
        raise httpx.ReadTimeout("")

    monkeypatch.setattr(marketplace_mod.local_client, "request", boom)
    monkeypatch.setattr(marketplace_mod, "_POLL_TIMEOUT", 0.05)
    assert marketplace_mod._poll_until_done("git") is False
    assert "continua no servidor" in capsys.readouterr().out


def test_poll_failed_state_unchanged(monkeypatch, fast, capsys):
    monkeypatch.setattr(marketplace_mod.local_client, "request",
                        lambda *a, **k: (200, {"status": "failed", "error": "x"}))
    assert marketplace_mod._poll_until_done("git") is False
    assert "failed — x" in capsys.readouterr().out


def test_listing_get_retries_then_succeeds(monkeypatch, fast):
    calls = []

    def fake_request(method, path, json_body=None, timeout=30.0):
        calls.append(path)
        if len(calls) < 3:
            raise httpx.ReadTimeout("")
        return 200, [{"slug": "git", "version": "1"}]

    monkeypatch.setattr(marketplace_mod.local_client, "request", fake_request)
    assert marketplace_mod._installed_apps() == [{"slug": "git", "version": "1"}]
    assert len(calls) == 3


def test_listing_get_persistent_timeout_is_a_clean_failure(monkeypatch, fast, capsys):
    def boom(*a, **k):
        raise httpx.ReadTimeout("")

    monkeypatch.setattr(marketplace_mod.local_client, "request", boom)
    assert marketplace_mod._installed_apps() is None
    assert marketplace_mod._catalog_apps() is None
    assert marketplace_mod._update_all() == 1
    assert marketplace_mod._info() == 1
    out = capsys.readouterr().out
    assert "did not answer" in out and "ReadTimeout" in out


def test_update_post_unanswered_returns_1_without_polling(monkeypatch, fast, capsys):
    paths = []

    def fake_request(method, path, json_body=None, timeout=30.0):
        paths.append((method, path))
        raise httpx.ConnectError("")

    monkeypatch.setattr(marketplace_mod.local_client, "request", fake_request)
    assert marketplace_mod._update_one("git") == 1
    assert all(m == "POST" for m, _ in paths)  # never polled a possibly-stale status
    assert len(paths) == marketplace_mod._RETRY_ATTEMPTS  # connect failures prove it never landed
    assert "did not answer the update request" in capsys.readouterr().out


def test_update_post_readtimeout_is_not_retried(monkeypatch, fast, capsys):
    paths = []

    def fake_request(method, path, json_body=None, timeout=30.0):
        paths.append((method, path))
        raise httpx.ReadTimeout("")

    monkeypatch.setattr(marketplace_mod.local_client, "request", fake_request)
    assert marketplace_mod._update_one("git") == 1
    assert len(paths) == 1  # ambiguous: may have been accepted, retry could double-enqueue
    assert "did not answer the update request" in capsys.readouterr().out


def test_install_catalog_timeout_does_not_claim_app_missing(monkeypatch, fast, capsys):
    def boom(*a, **k):
        raise httpx.ReadTimeout("")

    monkeypatch.setattr(marketplace_mod.local_client, "request", boom)
    assert marketplace_mod._install("git", update=True) == 1
    out = capsys.readouterr().out
    assert "did not answer" in out and "not in the marketplace catalog" not in out


def test_install_post_unanswered_is_not_retried(monkeypatch, fast, capsys):
    paths = []

    def fake_request(method, path, json_body=None, timeout=30.0):
        paths.append((method, path))
        if method == "GET":
            return 200, {"apps": [{"id": "git", "repo": "r", "version": "2"}]}
        raise httpx.ReadTimeout("")

    monkeypatch.setattr(marketplace_mod.local_client, "request", fake_request)
    assert marketplace_mod._install("git", update=False) == 1
    assert paths.count(("POST", "/api/apps/install")) == 1
    assert "did not answer the install request" in capsys.readouterr().out


def test_update_all_still_stops_at_first_incomplete(monkeypatch, fast, capsys):
    updated = []

    def fake_request(method, path, json_body=None, timeout=30.0):
        if path == "/api/apps":
            return 200, [{"slug": "a", "version": "1"}, {"slug": "b", "version": "1"}]
        if path == marketplace_mod._CATALOG_PATH:
            return 200, {"apps": [{"id": "a", "version": "2"}, {"id": "b", "version": "2"}]}
        if path.endswith("/update"):
            updated.append(path)
            return 202, {}
        return 200, {"status": "failed", "error": "nope"}

    monkeypatch.setattr(marketplace_mod.local_client, "request", fake_request)
    assert marketplace_mod._update_all() == 1
    assert updated == ["/api/apps/a/update"]


def test_catalog_refresh_not_retried_on_5xx(monkeypatch):
    """refresh=true + 504 -> exactly one request: a retry could start a second
    refresh that overwrites the shared catalog cache."""
    from src.cli.commands import marketplace as marketplace_mod

    calls = []

    def fake_request(method, path, json_body=None, timeout=30.0):
        calls.append(path)
        return 504, "gateway timeout"

    monkeypatch.setattr(marketplace_mod.local_client, "request", fake_request)
    monkeypatch.setattr(marketplace_mod, "_RETRY_BACKOFF_S", 0)
    monkeypatch.setattr(marketplace_mod.local_client.time, "sleep", lambda s: None)

    assert marketplace_mod._catalog_apps() is None
    assert calls == ["/api/apps/-/catalog?refresh=true"]


def test_plain_listing_get_still_retried_on_5xx(monkeypatch):
    from src.cli.commands import marketplace as marketplace_mod

    answers = iter([(504, "gateway timeout"), (200, [])])
    calls = []

    def fake_request(method, path, json_body=None, timeout=30.0):
        calls.append(path)
        return next(answers)

    monkeypatch.setattr(marketplace_mod.local_client, "request", fake_request)
    monkeypatch.setattr(marketplace_mod, "_RETRY_BACKOFF_S", 0)
    monkeypatch.setattr(marketplace_mod.local_client.time, "sleep", lambda s: None)

    assert marketplace_mod._get("/api/apps") == (200, [])
    assert len(calls) == 2


def _catalog_returns(monkeypatch, body, installed=None):
    def fake_request(method, path, json_body=None, timeout=30.0):
        if path == "/api/apps":
            return 200, installed if installed is not None else [{"slug": "git", "version": "1"}]
        return 200, body

    monkeypatch.setattr(marketplace_mod.local_client, "request", fake_request)


def test_catalog_error_with_no_apps_is_a_failed_lookup(monkeypatch, capsys):
    _catalog_returns(monkeypatch, {"apps": [], "error": "all sources unreachable"})
    assert marketplace_mod._catalog_apps() is None
    assert "all sources unreachable" in capsys.readouterr().out


def test_update_all_exits_nonzero_on_catalog_error(monkeypatch, capsys):
    _catalog_returns(monkeypatch, {"apps": [], "error": "all sources unreachable"})
    assert marketplace_mod._update_all() == 1
    out = capsys.readouterr().out
    assert "all sources unreachable" in out and "up to date" not in out


def test_install_exits_nonzero_on_catalog_error(monkeypatch, capsys):
    _catalog_returns(monkeypatch, {"apps": [], "error": "all sources unreachable"})
    assert marketplace_mod._install("git", update=False) == 1
    out = capsys.readouterr().out
    assert "all sources unreachable" in out and "not in the marketplace catalog" not in out


def test_catalog_error_with_apps_is_only_a_warning(monkeypatch, capsys):
    apps = [{"id": "git", "version": "1"}]
    _catalog_returns(monkeypatch, {"apps": apps, "error": "refresh failed"})
    assert marketplace_mod._catalog_apps() == apps
    assert "warning" in capsys.readouterr().out
    assert marketplace_mod._update_all() == 0


def test_empty_catalog_without_error_is_unchanged(monkeypatch):
    _catalog_returns(monkeypatch, {"apps": []})
    assert marketplace_mod._catalog_apps() == []
    assert marketplace_mod._update_all() == 0
