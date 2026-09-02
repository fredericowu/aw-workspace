"""Marketplace source registry — validation and the credential host binding.

The host binding is the security-critical part: a user can add an arbitrary
URL as a marketplace, so a credential must never be attached to a request
for a host it wasn't issued for.
"""
import json

import pytest

from src.api import marketplace as mp
from src.api.models import MarketplaceSource


# --- validate ----------------------------------------------------------------


def test_derives_id_and_defaults_auth_host_from_owner_repo():
    entry = mp.validate({"url": "tekflox/aw-marketplace-private@master",
                         "auth_type": "github_pat"})
    assert entry["id"] == "tekflox-aw-marketplace-private"
    # An owner/repo spec resolves to GitHub, so that's what the token binds to.
    assert entry["auth_host"] == "github.com"


def test_derives_auth_host_from_full_url():
    entry = mp.validate({"url": "https://catalog.acme.internal/apps.json",
                         "auth_type": "bearer"})
    assert entry["auth_host"] == "catalog.acme.internal"


def test_auth_host_is_cleared_when_auth_is_off():
    entry = mp.validate({"url": "acme/store", "auth_type": "none",
                         "auth_host": "github.com"})
    assert entry["auth_host"] == ""


def test_rejects_a_spec_that_is_neither_owner_repo_nor_url():
    with pytest.raises(mp.SourceError):
        mp.validate({"url": "not a repo spec at all"})


def test_rejects_a_bad_explicit_id():
    with pytest.raises(mp.SourceError):
        mp.validate({"url": "acme/store", "id": "Has Spaces"})


def test_url_is_required():
    with pytest.raises(mp.SourceError):
        mp.validate({"url": "  "})


# --- credential host binding -------------------------------------------------


@pytest.fixture
def bound_source(monkeypatch):
    """A github.com-bound source with a stored token, no DB or disk needed."""
    row = MarketplaceSource(
        id="private", name="Private", url="tekflox/aw-marketplace-private",
        enabled=True, priority=10, auth_type="github_pat", auth_host="github.com",
    )
    monkeypatch.setattr(mp, "get_source", lambda sid: row if sid == "private" else None)
    monkeypatch.setattr(mp._secrets, "get",
                        lambda ns, key: "ghp_secret" if key == "private" else None)
    return row


def test_credential_is_attached_for_the_bound_host(bound_source):
    header = mp.credential_for(
        "private", "https://raw.githubusercontent.com/tekflox/x/master/apps.json")
    assert header == ("Authorization", "token ghp_secret")


def test_credential_is_withheld_from_a_different_host(bound_source):
    # The whole point: adding https://evil.example/apps.json must not leak
    # a token bound to github.com.
    assert mp.credential_for("private", "https://evil.example/apps.json") is None


def test_credential_is_withheld_from_a_lookalike_host(bound_source):
    assert mp.credential_for("private", "https://github.com.evil.example/apps.json") is None


def test_no_credential_when_auth_is_off(monkeypatch):
    row = MarketplaceSource(id="pub", url="acme/store", auth_type="none", auth_host="")
    monkeypatch.setattr(mp, "get_source", lambda sid: row)
    assert mp.credential_for("pub", "https://raw.githubusercontent.com/a/b/c") is None


def test_no_credential_for_an_unknown_source(monkeypatch):
    monkeypatch.setattr(mp, "get_source", lambda sid: None)
    assert mp.credential_for("nope", "https://github.com/x") is None


def test_bearer_auth_type_uses_bearer_scheme(monkeypatch):
    row = MarketplaceSource(id="acme", url="https://catalog.acme.internal/apps.json",
                            auth_type="bearer", auth_host="catalog.acme.internal")
    monkeypatch.setattr(mp, "get_source", lambda sid: row)
    monkeypatch.setattr(mp._secrets, "get", lambda ns, key: "tok")
    assert mp.credential_for("acme", "https://catalog.acme.internal/apps.json") == (
        "Authorization", "Bearer tok")


# --- host_of -----------------------------------------------------------------


@pytest.mark.parametrize("spec,expected", [
    ("owner/repo", "github.com"),
    ("owner/repo@main", "github.com"),
    ("https://catalog.acme.internal/apps.json", "catalog.acme.internal"),
    ("https://CATALOG.Acme.Internal/apps.json", "catalog.acme.internal"),
])
def test_host_of(spec, expected):
    assert mp.host_of(spec) == expected


# --- describe never leaks the token ------------------------------------------


def test_describe_reports_only_whether_a_credential_exists(bound_source):
    out = mp.describe(bound_source)
    assert out["has_credential"] is True
    assert "ghp_secret" not in str(out)
    assert "credential" not in out


# --- registry credentials (private app images) -------------------------------


@pytest.mark.parametrize("image,expected", [
    ("ghcr.io/tekflox/aw-app-crispal:latest", "ghcr.io"),
    ("tekflox/aw-app-crispal:latest", "docker.io"),   # Hub namespace, not a host
    ("aw-app-crispal", "docker.io"),
    ("localhost:5000/foo:1", "localhost:5000"),
    ("registry.internal:5000/foo", "registry.internal:5000"),
])
def test_registry_host_parsing(image, expected):
    from src.apps.containers import _registry_host
    assert _registry_host(image) == expected


def test_registry_credential_reuses_a_github_marketplace_token(monkeypatch):
    row = MarketplaceSource(id="private", url="tekflox/aw-marketplace-private",
                            enabled=True, auth_type="github_pat", auth_host="github.com")
    monkeypatch.setattr(mp, "list_sources", lambda enabled_only=False: [row])
    monkeypatch.setattr(mp._secrets, "get", lambda ns, key: "ghp_secret")
    assert mp.registry_credential("ghcr.io") == ("x-access-token", "ghp_secret")


def test_registry_credential_is_none_for_other_registries(monkeypatch):
    row = MarketplaceSource(id="private", url="tekflox/x", enabled=True,
                            auth_type="github_pat", auth_host="github.com")
    monkeypatch.setattr(mp, "list_sources", lambda enabled_only=False: [row])
    monkeypatch.setattr(mp._secrets, "get", lambda ns, key: "ghp_secret")
    # A GitHub PAT is not a Docker Hub credential — don't send it there.
    assert mp.registry_credential("docker.io") is None


def test_registry_credential_is_none_without_a_github_source(monkeypatch):
    row = MarketplaceSource(id="acme", url="https://acme.internal/apps.json",
                            enabled=True, auth_type="bearer", auth_host="acme.internal")
    monkeypatch.setattr(mp, "list_sources", lambda enabled_only=False: [row])
    monkeypatch.setattr(mp._secrets, "get", lambda ns, key: "tok")
    assert mp.registry_credential("ghcr.io") is None


# --- local mirror + boot recovery --------------------------------------------
#
# Incident 2026-09-02: Postgres lost the marketplace_sources table entirely
# (unlike app_installs, which has a cloud-registry boot reconciler). The
# credential survived on disk (SecretStore is already file-backed) but the
# row referencing it did not, so Crispal's private catalog + private GHCR
# image both went dark until someone manually decrypted the credential and
# re-POSTed the source. These tests cover the fix: a local mirror of the
# row metadata, and a boot-time reconcile that restores a mirrored row
# Postgres has forgotten, but only when it can still actually authenticate.


@pytest.fixture
def mirror_home(tmp_path, monkeypatch):
    monkeypatch.setattr(mp.paths, "workspace_home", lambda: str(tmp_path))
    return tmp_path


def test_write_mirror_persists_non_secret_fields(mirror_home, monkeypatch):
    row = MarketplaceSource(id="private", name="Private", url="tekflox/aw-marketplace-private",
                            enabled=True, priority=50, auth_type="github_pat",
                            auth_host="github.com", created_at=1.0)
    monkeypatch.setattr(mp, "list_sources", lambda enabled_only=False: [row])
    monkeypatch.setattr(mp._secrets, "get", lambda ns, key: "ghp_secret")

    mp._write_mirror()

    with open(mirror_home / "marketplace_sources.json") as f:
        data = json.load(f)
    assert data["sources"] == [{
        "id": "private", "name": "Private", "url": "tekflox/aw-marketplace-private",
        "enabled": True, "priority": 50, "auth_type": "github_pat",
        "auth_host": "github.com", "created_at": 1.0,
    }]  # no credential and no has_credential — never persisted to disk


def test_reconcile_restores_a_mirrored_source_missing_from_postgres(mirror_home, monkeypatch):
    with open(mirror_home / "marketplace_sources.json", "w") as f:
        json.dump({"sources": [{
            "id": "tekflox-private", "name": "Crispal private catalog",
            "url": "tekflox/aw-marketplace-private@master", "enabled": True,
            "priority": 50, "auth_type": "github_pat", "auth_host": "github.com",
        }]}, f)
    monkeypatch.setattr(mp, "get_source", lambda sid: None)  # gone from Postgres
    monkeypatch.setattr(mp._secrets, "get", lambda ns, key: "ghp_recovered")  # survived
    calls = []
    monkeypatch.setattr(mp, "upsert_source", lambda entry, credential=None: calls.append(entry))

    mp.reconcile_sources_on_boot()

    assert len(calls) == 1
    assert calls[0]["id"] == "tekflox-private"
    assert calls[0]["url"] == "tekflox/aw-marketplace-private@master"


def test_reconcile_skips_a_source_already_in_postgres(mirror_home, monkeypatch):
    with open(mirror_home / "marketplace_sources.json", "w") as f:
        json.dump({"sources": [{"id": "already-there", "url": "a/b", "auth_type": "none"}]}, f)
    monkeypatch.setattr(mp, "get_source", lambda sid: object())  # still present
    calls = []
    monkeypatch.setattr(mp, "upsert_source", lambda entry, credential=None: calls.append(entry))

    mp.reconcile_sources_on_boot()

    assert calls == []


def test_reconcile_does_not_restore_when_the_credential_is_also_gone(mirror_home, monkeypatch):
    with open(mirror_home / "marketplace_sources.json", "w") as f:
        json.dump({"sources": [{"id": "private", "url": "a/b", "auth_type": "github_pat"}]}, f)
    monkeypatch.setattr(mp, "get_source", lambda sid: None)
    monkeypatch.setattr(mp._secrets, "get", lambda ns, key: None)  # credential also lost
    calls = []
    monkeypatch.setattr(mp, "upsert_source", lambda entry, credential=None: calls.append(entry))

    mp.reconcile_sources_on_boot()

    assert calls == []  # nothing to authenticate with — don't half-restore it


def test_reconcile_restores_an_unauthenticated_source_with_no_credential_needed(mirror_home, monkeypatch):
    with open(mirror_home / "marketplace_sources.json", "w") as f:
        json.dump({"sources": [{"id": "public", "url": "acme/store", "auth_type": "none"}]}, f)
    monkeypatch.setattr(mp, "get_source", lambda sid: None)
    monkeypatch.setattr(mp._secrets, "get", lambda ns, key: None)
    calls = []
    monkeypatch.setattr(mp, "upsert_source", lambda entry, credential=None: calls.append(entry))

    mp.reconcile_sources_on_boot()

    assert len(calls) == 1  # auth_type "none" never needed a credential to begin with


def test_reconcile_is_a_noop_when_no_mirror_file_exists(mirror_home):
    mp.reconcile_sources_on_boot()  # must not raise
