"""Marketplace catalog tests — multi-source raw GET, merge/dedup, persistent
cache fallback on total fetch failure."""
from __future__ import annotations

import json

import httpx
import pytest

from src.apps import catalog as catalog_mod


@pytest.fixture(autouse=True)
def _isolated_catalog(tmp_path, monkeypatch):
    monkeypatch.setenv("AW_WORKSPACE_HOME", str(tmp_path / "home"))
    # ...and from the sources the RUNNING workspace has configured. db_sources()
    # reads Postgres, so without this these tests assert against whatever the
    # live registry happens to hold: on a workspace pointed at
    # tekflox/aw-marketplace-private, six of them failed while asserting the
    # public default. A unit test about "the default when nothing is
    # configured" must supply that condition, not inherit it — and the failure
    # was invisible outside the workspace container, which has the DB.
    monkeypatch.setattr(catalog_mod, "db_sources", lambda: [])
    catalog_mod.clear_cache()
    yield
    catalog_mod.clear_cache()


def _resp(payload, status=200, url="https://example.com"):
    return httpx.Response(status, text=json.dumps(payload), request=httpx.Request("GET", url))


_api = catalog_mod._api_url_for


def test_default_source_uses_legacy_repo_ref(monkeypatch):
    seen = []

    def fake_get(url, headers=None, timeout=None):
        seen.append((url, (headers or {}).get("Accept")))
        return _resp({"apps": [{"id": "git", "name": "Git"}]})

    monkeypatch.setattr(catalog_mod.httpx, "get", fake_get)

    result = catalog_mod.get_catalog(force=True)
    # The API is tried first (s-maxage=60) and succeeds, so raw is never hit.
    assert seen == [(
        "https://api.github.com/repos/tekflox/aw-marketplace/contents/apps.json?ref=master",
        catalog_mod.GITHUB_RAW_MEDIA_TYPE,
    )]
    assert [a["id"] for a in result["apps"]] == ["git"]
    assert result["apps"][0]["_source"] == "tekflox/aw-marketplace@master"


def test_api_url_only_built_for_owner_repo_specs():
    assert _api("acme/store@main") == (
        "https://api.github.com/repos/acme/store/contents/apps.json?ref=main"
    )
    assert _api("acme/store") == (
        "https://api.github.com/repos/acme/store/contents/apps.json?ref=master"
    )
    # A full-URL source points at an arbitrary host with no GitHub API.
    assert _api("https://example.com/custom/apps.json") is None
    assert _api("http://example.com/apps.json") is None
    # Malformed specs must not produce a plausible-looking API URL.
    assert _api("not-a-repo") is None
    assert _api("acme/store/extra@main") is None
    assert _api("") is None


def test_api_failure_falls_back_to_raw(monkeypatch):
    """A rate-limited (or otherwise failing) API must never make the catalog
    less available than the raw URL alone was."""
    seen = []

    def fake_get(url, headers=None, timeout=None):
        seen.append(url)
        if "api.github.com" in url:
            return httpx.Response(
                403, text="rate limit exceeded", request=httpx.Request("GET", url),
            )
        return _resp({"apps": [{"id": "git", "name": "Git"}]})

    monkeypatch.setattr(catalog_mod.httpx, "get", fake_get)

    result = catalog_mod.get_catalog(force=True)
    assert [a["id"] for a in result["apps"]] == ["git"]
    assert result["failed_sources"] == []
    assert seen == [
        "https://api.github.com/repos/tekflox/aw-marketplace/contents/apps.json?ref=master",
        "https://raw.githubusercontent.com/tekflox/aw-marketplace/master/apps.json",
    ]


def test_source_fetch_still_raises_when_api_and_raw_both_fail(monkeypatch):
    """The 'test this source' route relies on a bad credential reporting
    itself — the fallback must not swallow a genuine failure."""
    def fake_get(url, headers=None, timeout=None):
        return httpx.Response(404, text="nope", request=httpx.Request("GET", url))

    monkeypatch.setattr(catalog_mod.httpx, "get", fake_get)

    with pytest.raises(httpx.HTTPStatusError):
        catalog_mod.fetch_source("acme/store@main")


def test_per_app_manifests_stay_on_raw(monkeypatch):
    """One API call per source per refresh is the whole rate-limit budget:
    N manifest fetches over the API would exhaust an unauthenticated hourly
    limit in a couple of refreshes."""
    seen = []

    def fake_get(url, headers=None, timeout=None):
        seen.append(url)
        if "contents/apps.json" in url:
            return _resp({"apps": [{"id": "git", "name": "Git", "repo": "acme/git"}]})
        return _resp({"manifest_version": 1, "id": "git", "name": "Git", "version": "1.0.0"})

    monkeypatch.setattr(catalog_mod.httpx, "get", fake_get)

    catalog_mod.get_catalog(force=True)
    manifest_urls = [u for u in seen if u.endswith("aw-app.json")]
    assert manifest_urls == ["https://raw.githubusercontent.com/acme/git/master/aw-app.json"]
    assert not any("api.github.com" in u for u in manifest_urls)


def test_multi_source_merge_and_dedup(monkeypatch):
    monkeypatch.setenv("AW_MARKETPLACE_SOURCES", "acme/store@main,acme/extra")

    payloads = {
        "https://api.github.com/repos/acme/store/contents/apps.json?ref=main": {
            "apps": [{"id": "a", "name": "A from store"}, {"id": "shared", "name": "Shared (store wins)"}],
        },
        "https://api.github.com/repos/acme/extra/contents/apps.json?ref=master": {
            "apps": [{"id": "b", "name": "B from extra"}, {"id": "shared", "name": "Shared (extra)"}],
        },
    }

    def fake_get(url, headers=None, timeout=None):
        return _resp(payloads[url])

    monkeypatch.setattr(catalog_mod.httpx, "get", fake_get)

    result = catalog_mod.get_catalog(force=True)
    ids = [a["id"] for a in result["apps"]]
    assert ids == ["a", "shared", "b"]
    shared = next(a for a in result["apps"] if a["id"] == "shared")
    assert shared["name"] == "Shared (store wins)"  # first source wins
    assert result["sources"] == ["acme/store@main", "acme/extra"]


def test_explicit_raw_url_source_used_as_is(monkeypatch):
    monkeypatch.setenv("AW_MARKETPLACE_SOURCES", "https://example.com/custom/apps.json")
    seen_urls = []

    def fake_get(url, headers=None, timeout=None):
        seen_urls.append(url)
        return _resp({"apps": [{"id": "x", "name": "X"}]})

    monkeypatch.setattr(catalog_mod.httpx, "get", fake_get)

    catalog_mod.get_catalog(force=True)
    assert seen_urls == ["https://example.com/custom/apps.json"]


def test_one_source_failing_does_not_break_the_others(monkeypatch):
    monkeypatch.setenv("AW_MARKETPLACE_SOURCES", "acme/broken,acme/ok")

    def fake_get(url, headers=None, timeout=None):
        if "broken" in url:
            raise httpx.ConnectError("boom", request=httpx.Request("GET", url))
        return _resp({"apps": [{"id": "ok-app", "name": "OK"}]})

    monkeypatch.setattr(catalog_mod.httpx, "get", fake_get)

    result = catalog_mod.get_catalog(force=True)
    assert [a["id"] for a in result["apps"]] == ["ok-app"]
    assert result["failed_sources"] == ["acme/broken"]
    assert result["sources"] == ["acme/ok"]


def test_fetch_failure_serves_stale_in_memory_cache(monkeypatch):
    calls = {"n": 0}

    def fake_get(url, headers=None, timeout=None):
        calls["n"] += 1
        if calls["n"] == 1:
            return _resp({"apps": [{"id": "git", "name": "Git"}]})
        raise httpx.ConnectError("down", request=httpx.Request("GET", url))

    monkeypatch.setattr(catalog_mod.httpx, "get", fake_get)

    first = catalog_mod.get_catalog(force=True)
    assert first["apps"][0]["id"] == "git"
    assert "stale" not in first

    second = catalog_mod.get_catalog(force=True)
    assert second["apps"][0]["id"] == "git"
    assert second["stale"] is True


def test_fetch_failure_with_no_memory_cache_serves_disk_cache(monkeypatch):
    def ok_get(url, headers=None, timeout=None):
        return _resp({"apps": [{"id": "git", "name": "Git"}]})

    monkeypatch.setattr(catalog_mod.httpx, "get", ok_get)
    catalog_mod.get_catalog(force=True)  # populates disk cache
    catalog_mod.clear_cache()  # drop in-memory cache only

    def failing_get(url, headers=None, timeout=None):
        raise httpx.ConnectError("down", request=httpx.Request("GET", url))

    monkeypatch.setattr(catalog_mod.httpx, "get", failing_get)

    result = catalog_mod.get_catalog(force=True)
    assert result["apps"][0]["id"] == "git"
    assert result["stale"] is True


def test_total_cold_failure_returns_empty_with_error(monkeypatch):
    def failing_get(url, headers=None, timeout=None):
        raise httpx.ConnectError("down", request=httpx.Request("GET", url))

    monkeypatch.setattr(catalog_mod.httpx, "get", failing_get)

    result = catalog_mod.get_catalog(force=True)
    assert result["apps"] == []
    assert "error" in result


def test_token_sent_as_bearer_header_when_configured(monkeypatch):
    monkeypatch.setenv("AW_APP_GIT_TOKEN", "secret-token")
    seen = []

    def fake_get(url, headers=None, timeout=None):
        seen.append((url, headers))
        return _resp({"apps": []})

    monkeypatch.setattr(catalog_mod.httpx, "get", fake_get)

    catalog_mod.get_catalog(force=True)
    url, headers = seen[0]
    # The credential has to reach api.github.com too, or a private marketplace
    # would 404 on every API fetch and silently fall back to raw forever.
    assert "api.github.com" in url
    assert headers["Authorization"] == "Bearer secret-token"


def test_catalog_entry_enriched_with_publisher_resource_estimate_and_what_you_get(monkeypatch):
    manifest_payload = {
        "manifest_version": 1,
        "id": "git",
        "name": "Git",
        "version": "0.1.0",
        "tier": "inprocess",
        "runtime": {"entrypoint": "git_app.plugin:GitAppPlugin"},
        "permissions": [],
        "contributes": {
            "windows": [{"id": "git.main", "title": "Git & GitHub CLI"}],
            "nav": [{"id": "git.nav", "label": "Git", "opens": "git.main"}],
            "system_clis": [{"name": "git", "installer": "x.sh"}, {"name": "gh", "installer": "y.sh"}],
        },
    }

    def fake_get(url, headers=None, timeout=None):
        if url.endswith("apps.json"):
            return _resp({"apps": [{"id": "git", "name": "Git", "repo": "tekflox/aw-app-git", "ref": "main"}]})
        assert url == "https://raw.githubusercontent.com/tekflox/aw-app-git/main/aw-app.json"
        return _resp(manifest_payload)

    monkeypatch.setattr(catalog_mod.httpx, "get", fake_get)

    result = catalog_mod.get_catalog(force=True)
    app = result["apps"][0]
    assert app["publisher"] == "TekFlox"
    assert app["resource_estimate"] == {"cpu": "low", "memory": "-", "disk": "-"}
    assert app["what_you_get"] == {
        "mcp_tools": [], "ui_screens": ["Git & GitHub CLI", "Git"], "commands": ["git", "gh"],
        "skills": [],
    }
    assert app["dependencies"] == {}


def test_catalog_entry_manifest_fetch_failure_leaves_entry_unenriched(monkeypatch):
    def fake_get(url, headers=None, timeout=None):
        if url.endswith("apps.json"):
            return _resp({"apps": [{"id": "git", "name": "Git", "repo": "tekflox/aw-app-git", "ref": "main"}]})
        raise httpx.ConnectError("down", request=httpx.Request("GET", url))

    monkeypatch.setattr(catalog_mod.httpx, "get", fake_get)

    result = catalog_mod.get_catalog(force=True)
    app = result["apps"][0]
    assert "publisher" not in app
    assert "what_you_get" not in app


def test_ttl_cache_avoids_refetch_until_forced(monkeypatch):
    calls = {"n": 0}

    def fake_get(url, headers=None, timeout=None):
        calls["n"] += 1
        return _resp({"apps": [{"id": "git", "name": "Git"}]})

    monkeypatch.setattr(catalog_mod.httpx, "get", fake_get)

    catalog_mod.get_catalog(force=True)
    catalog_mod.get_catalog(force=False)
    assert calls["n"] == 1

    catalog_mod.get_catalog(force=True)
    assert calls["n"] == 2


class TestIsMarketplaceApp:
    """Catalog membership is the local half of the ``signed`` trust flag
    (ADR Decision 4) — see routes.py's install path, where taking it from the
    request body instead silently stripped ui:code from a CLI install."""

    @pytest.fixture(autouse=True)
    def _catalog(self, monkeypatch):
        def fake_get(url, headers=None, timeout=None):
            if "aw-marketplace-private" in url:
                return _resp({"apps": [{"id": "crispal", "repo": "tekflox/aw-app-crispal"}]})
            return _resp({"apps": [{"id": "diff-tool", "repo": "tekflox/aw-app-diff-tool"}]})

        monkeypatch.setenv(
            "AW_MARKETPLACE_SOURCES",
            "tekflox/aw-marketplace@master,tekflox/aw-marketplace-private@master")
        monkeypatch.setattr(catalog_mod.httpx, "get", fake_get)
        catalog_mod.get_catalog(force=True)

    def test_published_id_and_repo_pair_is_signed(self):
        assert catalog_mod.is_marketplace_app("diff-tool", "tekflox/aw-app-diff-tool") is True

    def test_repo_match_is_case_and_whitespace_insensitive(self):
        assert catalog_mod.is_marketplace_app("diff-tool", " TekFlox/aw-app-Diff-Tool ") is True

    def test_id_collision_under_a_different_repo_is_not_signed(self):
        """A side-loaded app reusing a catalog app's id must not inherit its
        trust — this is the whole reason both fields are compared."""
        assert catalog_mod.is_marketplace_app("diff-tool", "attacker/aw-app-diff-tool") is False

    def test_unknown_app_is_not_signed(self):
        assert catalog_mod.is_marketplace_app("nope", "tekflox/aw-app-nope") is False

    def test_sideload_without_a_repo_is_not_signed(self):
        assert catalog_mod.is_marketplace_app("diff-tool", None) is False
        assert catalog_mod.is_marketplace_app("", "tekflox/aw-app-diff-tool") is False

    def test_a_private_source_does_not_confer_trust(self):
        """Only the official catalog counts — the cloud registry reads that one
        repo alone, and disagreeing would make reconcile see permanent trust
        drift and reinstall the app on every pass."""
        assert catalog_mod.is_marketplace_app("crispal", "tekflox/aw-app-crispal") is False
