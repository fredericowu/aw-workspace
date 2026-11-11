"""Workspace-wide pytest fixtures.

No test in this suite should reach a real network by default. The
marketplace-catalog fetch (``src/apps/catalog.py``) has hung CI twice from
two DIFFERENT call sites — ``test_fetch.py`` via ``auth_headers_for_repo``
(commit a385181) and ``test_update_endpoint.py`` via ``is_marketplace_app``
(card 3cf5bf3b-9510-81e4-bd1e-e0117c3e9086) — because every caller
(``get_catalog``, ``fetch_source``, ``is_marketplace_app``,
``auth_headers_for_repo``, ``list_tags``, ``_fetch_app_manifest``) bottoms
out in ``catalog.py``'s own ``httpx.get``, and a fix that only patches one
caller's re-exported name leaves every other call site exposed. Blocking the
actual network primitive here covers all of them at once, for every test
file, without anyone having to remember to stub catalog access again.

``test_catalog.py``, ``test_fetch.py`` and ``test_f5_endpoints.py``
intentionally exercise real catalog-fetch logic and already monkeypatch
their own layer inside their own tests/fixtures (``catalog_mod.httpx.get``,
``catalog_mod._fetch_source``, ``catalog_mod.auth_headers_for_repo``). Those
assignments run after this fixture (conftest fixtures are instantiated
before same-scope fixtures local to a test module) and simply overwrite it
for the duration of that test — no per-file opt-out needed.
"""
from __future__ import annotations

import httpx
import pytest

from src.apps import catalog as catalog_mod
from src.apps import fs_lock


@pytest.fixture(autouse=True)
def _no_marketplace_catalog_network(monkeypatch):
    def _fake_get(url, *args, **kwargs):
        return httpx.Response(200, text='{"apps": []}', request=httpx.Request("GET", url))

    monkeypatch.setattr(catalog_mod.httpx, "get", _fake_get)


@pytest.fixture(autouse=True)
def _isolated_app_lock_dir(tmp_path_factory, monkeypatch):
    """No test may touch the LIVE provisioning/installer locks.

    ``AppLifecycle`` and ``CommandInstaller`` default their lock dir to
    ``<AW_WORKSPACE_HOME>/locks`` (``src/apps/fs_lock.py``), and this suite
    runs inside the same container as a real workspace server — so a test
    constructing either with no explicit ``lock_dir`` would contend with live
    provisioning: blocking behind a real cold install, or worse, making one
    wait on a test. Same class of mistake as the live-Redis-keyspace one,
    caught before it shipped rather than after.
    """
    monkeypatch.setenv(fs_lock.LOCK_DIR_ENV,
                       str(tmp_path_factory.mktemp("aw-locks")))


@pytest.fixture(autouse=True)
def _no_live_cloud_registry(monkeypatch):
    """No test may write to the REAL aw-backend ``app_installs`` rows.

    ``Reconciler.__init__`` defaults ``self.cloud`` to a bare
    ``CloudRegistry()`` (``src/apps/registry_client.py``), which reads
    ``AW_BACKEND_URL``/``AW_WORKSPACE``/``AW_WORKSPACE_HOST_TOKEN`` straight
    from the process environment. This suite runs inside the same container
    as a real workspace server, where those three vars are already set for
    real — so any test exercising ``register_apps_routes()``'s route wiring
    without injecting a fake ``Reconciler.cloud`` (e.g.
    ``save_app_config``'s unconditional ``reconciler.cloud.put_desired(...)``
    whenever ``reconciler.cloud.configured`` is True) silently PUTs its
    fixture app id into this workspace's real desired-app-state. That is
    exactly how ``hookapp``/``nohookapp``/``mcpapp``/``orderapp``/``plainapp``
    leaked into live state on 2026-09-18 (card 3df5bf3b).

    ``CloudRegistry.configured`` is ``bool(backend_url and workspace and
    token)`` — ANY one of the three missing is already enough to make it
    False, so only ``AW_BACKEND_URL``/``AW_WORKSPACE_HOST_TOKEN`` are blanked
    here. ``AW_WORKSPACE`` is deliberately left alone: it is also the
    workspace-slug Redis-key-prefix (``src/libs/redis_coord.py``'s
    ``_key_prefix``) several unrelated test modules depend on inheriting
    from the ambient environment or setting once at import time (e.g.
    ``test_w7_terminal_multiworker.py``) — blanking it here broke that
    file's own namespace isolation for every test that doesn't re-set it
    itself, with no gain in protection since the other two already suffice.

    A test that genuinely wants the real ``CloudRegistry`` client re-sets
    these vars itself via ``monkeypatch.setenv``, matching this file's
    existing documented convention for ``_no_marketplace_catalog_network``.
    """
    for name in ("AW_BACKEND_URL", "AW_WORKSPACE_HOST_TOKEN"):
        monkeypatch.delenv(name, raising=False)
