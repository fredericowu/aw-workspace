"""App-contributed scheduled tasks (``contributes.tasks``).

An app can declare scheduled tasks its features depend on — aw-app-crispal
ships the nightly Arvin History cleanup, which used to be a task somebody
had to remember to create by hand after installing anything.

Seed-once semantics
-------------------

A contributed task is **seeded, not owned**. On install (and on every boot,
since activation is idempotent and re-runs):

* a task with that **name** already exists  → left completely alone
* no task with that name exists            → created

That's the whole rule, and the identity key is the **name**, not a synthetic
id (Frederico, 2026-08-12: *"podemos nos guiar pelo nome … se não existir,
cria, se existir - não atualiza, deixa o que existe lá"*). The reasoning is
that a schedule is something a user tunes — they disable it, move it an hour
later, change the command — and an app re-asserting its own version on every
boot would silently undo that. Matching on name also means a task the user
created by hand before installing the app is recognised as "already there"
rather than duplicated, which is exactly what happened with the twelve tasks
migrated out of the monolith.

Consequences worth being explicit about, since both are deliberate:

* **Nothing is updated, ever.** Shipping a corrected command in a new app
  version does *not* reach an existing installation. The app must ship it
  under a new name, or the user edits theirs.
* **Nothing is removed on uninstall.** Unlike ``skills.py``, which reverts
  its copies via the journal, a seeded task belongs to the user the moment
  it exists — deleting one they had enabled and tuned would be the same
  clobbering this design exists to avoid. Uninstalling an app leaves its
  tasks behind, disabled or not, for the user to remove if they want to.

Provider indirection
--------------------

Core has no task storage of its own — ``aw-app-tasks`` owns the tables. So
this registry doesn't write anything; it hands each declaration to whichever
installed app implements the provider protocol:

    plugin.register_contributed_task(app_id: str, spec: dict) -> bool

returning True when it created the task, False when one of that name already
existed. Duck-typed on purpose: core must not import an app, and a different
tasks implementation can satisfy the same contract.

Activation order is not guaranteed — an app declaring tasks can activate
before the tasks app does. Declarations that arrive early are held in
``_pending`` and replayed when a provider shows up, and the provider's own
activation additionally sweeps every already-loaded app. Between the two,
the set converges regardless of order, on every boot.
"""
from __future__ import annotations

import logging
from typing import Any, Protocol, runtime_checkable

log = logging.getLogger(__name__)

PROVIDER_METHOD = "register_contributed_task"


@runtime_checkable
class TaskProvider(Protocol):
    """What an app must expose to receive ``contributes.tasks`` declarations."""

    def register_contributed_task(self, app_id: str, spec: dict[str, Any]) -> bool:
        ...


class TasksRegistry:
    """Runtime-owned dispatcher for the ``contributes.tasks`` surface."""

    def __init__(self) -> None:
        # app_id -> declarations that arrived before any provider was loaded.
        self._pending: dict[str, list[dict[str, Any]]] = {}

    # ---- provider lookup ----------------------------------------------------

    @staticmethod
    def find_provider(runtime) -> Any | None:
        """The loaded app implementing the provider protocol, if any."""
        for slug in runtime.loaded_slugs():
            loaded = runtime.get(slug)
            plugin = getattr(loaded, "plugin", None)
            if plugin is not None and callable(getattr(plugin, PROVIDER_METHOD, None)):
                return plugin
        return None

    # ---- registration -------------------------------------------------------

    def register(self, runtime, app_id: str, specs: list[dict[str, Any]]) -> int:
        """Seed ``app_id``'s declared tasks. Returns how many were created.

        A provider that raises on one task must not stop the others, and must
        never fail the app's activation — an app whose *features* work but
        whose seeded schedule didn't land is a much better outcome than an
        app that refuses to install.
        """
        if not specs:
            return 0
        provider = self.find_provider(runtime)
        if provider is None:
            self._pending[app_id] = list(specs)
            log.info(
                "apps: no task provider loaded yet, holding %d task(s) from %s",
                len(specs), app_id,
            )
            return 0
        return self._dispatch(provider, app_id, specs)

    def drain_pending(self, runtime) -> int:
        """Replay declarations held while no provider was loaded."""
        if not self._pending:
            return 0
        provider = self.find_provider(runtime)
        if provider is None:
            return 0
        created = 0
        for app_id, specs in list(self._pending.items()):
            created += self._dispatch(provider, app_id, specs)
            del self._pending[app_id]
        return created

    def sweep(self, runtime) -> int:
        """Seed tasks for every already-loaded app.

        Called when the provider itself activates: apps that came up before
        it were skipped (or held), and a boot where the provider happens to
        load last would otherwise seed nothing at all.
        """
        provider = self.find_provider(runtime)
        if provider is None:
            return 0
        created = self.drain_pending(runtime)
        for slug in runtime.loaded_slugs():
            loaded = runtime.get(slug)
            specs = getattr(getattr(loaded, "manifest", None), "tasks", None) or []
            if specs:
                created += self._dispatch(provider, slug, specs)
        return created

    @staticmethod
    def _dispatch(provider, app_id: str, specs: list[dict[str, Any]]) -> int:
        created = 0
        for spec in specs:
            name = str(spec.get("name") or "").strip()
            if not name:
                continue
            try:
                if provider.register_contributed_task(app_id, dict(spec)):
                    created += 1
                    log.info("apps: seeded task %r from %s", name, app_id)
                else:
                    log.debug("apps: task %r already exists, leaving it alone", name)
            except Exception:  # noqa: BLE001 — a bad seed must not fail activation
                log.exception("apps: failed to seed task %r from %s", name, app_id)
        return created
