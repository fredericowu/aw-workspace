"""App-contributed agents (``contributes.agents``).

An app can declare the Agents Platform objects its features need — the
models it runs on, the reusable config bundle, the group whose instructions
its agents share, and the agents themselves. Before this, an app that was
*about* an agent (a Telegram persona, a domain reviewer, a vertical's
coder) shipped its skill and then relied on somebody opening the Agents
Platform UI and hand-creating four rows in the right order. Nothing in the
install told them to, and nothing told them when they got it wrong.

The declaration is one object with five ordered lists::

    "contributes": {
      "agents": {
        "models":        [ { "slug": ..., "provider": ..., "model_id": ... } ],
        "agent_configs": [ { "slug": ..., "name": ... } ],
        "groups":        [ { "slug": ..., "name": ... } ],
        "agents":        [ { "slug": ..., "name": ..., "model_slug": ... } ],
        "agent_flows":   [ { "slug": ..., "name": ..., "graph": {...} } ]
      }
    }

The order of the keys is the order they are created in, and it is not
cosmetic: an Agent references a model, an agent config and a group by slug,
so all three have to exist first, and an AgentFlow's graph references agents
by slug, so it comes after them. The provider creates them in exactly this
sequence — an app only has to declare, never to sequence.

An Agents Flow is a *topology* graph, not an execution DAG: it says which
agents may hand off to which, starting from a ``source`` node (the inbound
channel). When ``enabled``, every agent appearing as a node in it gets the
flow context injected at dispatch time. A team of agents is only a team
once something says how they connect, which is why an app that ships the
agents ships the flow too.

Seed-once semantics
-------------------

Identical to ``contributes.tasks`` (see ``src/apps/tasks.py``), and for the
same reason. On install, and on every boot since activation re-runs:

* an object with that **slug** already exists → left completely alone
* no object with that slug exists            → created

The identity key is the **slug**, which is Agents Platform's own natural
key, so a slug an app declares and a slug the user typed into the UI are
the same identity — an agent the user already made by hand is recognised
rather than duplicated.

The two consequences are deliberate, and both are the same bet the tasks
surface makes — that a user's edit outranks an app's default:

* **Nothing is updated, ever.** A corrected system prompt in a new app
  version does not reach an existing installation. Ship it under a new
  slug, or the user edits theirs. This matters more here than it does for
  tasks: an agent's prompt is exactly the thing a user tunes for weeks, and
  an app re-asserting its own copy on every boot would erase that silently.
* **Nothing is removed on uninstall.** An agent that has run — with
  sessions, runs and retro scores hanging off it — belongs to the user the
  moment it exists. Uninstalling the app leaves it there to be deleted
  deliberately, in the UI, by someone who can see what else it is attached
  to.

Long prompts live in files
--------------------------

A system prompt or a group's instructions is usually much too long to sit
readably inside JSON. Either field may instead be given as
``system_prompt_file`` / ``instructions_file``, a path **relative to the
app's own package dir**, which core inlines here before the spec ever
reaches the provider. Paths are resolved and confined to the package (same
containment check ``_resolve_window`` uses in ``runtime.py``); one that
escapes, or doesn't exist, drops that single field with a warning rather
than failing the install.

Provider indirection
--------------------

Core has no agent storage and no idea how to reach Agents Platform — that
is ``aw-app-agents-platform-runners``, which already holds the base URL and
the identity token. So this registry stores nothing; it hands the whole
declaration to whichever installed app implements::

    plugin.register_contributed_agents(app_id: str, spec: dict) -> dict

returning a per-kind count of what it actually created. One call per app,
not one per object, precisely so the provider owns the ordering described
above. Duck-typed on purpose: core must not import an app, and a different
Agents Platform implementation can satisfy the same contract.

Activation order is not guaranteed — an app declaring agents can activate
before the provider does. Declarations that arrive early are held in
``_pending`` and replayed when a provider appears, and the provider's own
activation sweeps every already-loaded app. Between the two the set
converges regardless of order, on every boot.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Protocol, runtime_checkable

log = logging.getLogger(__name__)

PROVIDER_METHOD = "register_contributed_agents"

#: The five kinds, in creation order. An agent references a model, a config
#: and a group by slug; an agent flow references agents by slug — so flows
#: come last, after everything they can point at exists.
KINDS = ("models", "agent_configs", "groups", "agents", "agent_flows")

#: ``<field>_file`` sugar: a path relative to the package dir whose contents
#: become ``<field>``. Long prompts don't belong inside JSON.
FILE_FIELDS = {
    "agents": ("system_prompt",),
    "groups": ("instructions",),
}


@runtime_checkable
class AgentProvider(Protocol):
    """What an app must expose to receive ``contributes.agents`` declarations."""

    def register_contributed_agents(
        self, app_id: str, spec: dict[str, Any]
    ) -> dict[str, int]:
        ...


class AgentsRegistry:
    """Runtime-owned dispatcher for the ``contributes.agents`` surface."""

    def __init__(self) -> None:
        # app_id -> (spec, package_dir) that arrived before any provider loaded.
        self._pending: dict[str, tuple[dict[str, Any], str]] = {}

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

    def register(
        self, runtime, app_id: str, spec: dict[str, Any], package_dir: str = ""
    ) -> dict[str, int]:
        """Seed ``app_id``'s declared agents. Returns counts created per kind.

        A provider that raises must never fail the app's activation — an app
        whose features work but whose seeded agent didn't land is a much
        better outcome than an app that refuses to install.
        """
        if not _any_declared(spec):
            return {}
        provider = self.find_provider(runtime)
        if provider is None:
            self._pending[app_id] = (dict(spec), package_dir)
            log.info(
                "apps: no agent provider loaded yet, holding %s declaration(s) from %s",
                _counts(spec), app_id,
            )
            return {}
        return self._dispatch(provider, app_id, spec, package_dir)

    def drain_pending(self, runtime) -> dict[str, int]:
        """Replay declarations held while no provider was loaded."""
        if not self._pending:
            return {}
        provider = self.find_provider(runtime)
        if provider is None:
            return {}
        created: dict[str, int] = {}
        for app_id, (spec, package_dir) in list(self._pending.items()):
            _merge(created, self._dispatch(provider, app_id, spec, package_dir))
            del self._pending[app_id]
        return created

    def sweep(self, runtime) -> dict[str, int]:
        """Seed agents for every already-loaded app.

        Called when the provider itself activates: apps that came up before
        it were skipped (or held), and a boot where the provider happens to
        load last would otherwise seed nothing at all.
        """
        provider = self.find_provider(runtime)
        if provider is None:
            return {}
        created = self.drain_pending(runtime)
        for slug in runtime.loaded_slugs():
            loaded = runtime.get(slug)
            spec = getattr(getattr(loaded, "manifest", None), "agents", None) or {}
            if _any_declared(spec):
                _merge(created, self._dispatch(
                    provider, slug, spec, getattr(loaded, "package_dir", "") or ""
                ))
        return created

    # ---- dispatch -----------------------------------------------------------

    @classmethod
    def _dispatch(
        cls, provider, app_id: str, spec: dict[str, Any], package_dir: str
    ) -> dict[str, int]:
        resolved = resolve_file_fields(spec, package_dir)
        try:
            created = provider.register_contributed_agents(app_id, resolved) or {}
        except Exception:  # noqa: BLE001 — a bad seed must not fail activation
            log.exception("apps: failed to seed agents from %s", app_id)
            return {}
        if any(created.values()):
            log.info("apps: seeded %s from %s", _fmt(created), app_id)
        else:
            log.debug("apps: %s declared agents, all already existed", app_id)
        return dict(created)


# ---- helpers ----------------------------------------------------------------


def resolve_file_fields(spec: dict[str, Any], package_dir: str) -> dict[str, Any]:
    """Inline ``system_prompt_file`` / ``instructions_file`` from the package.

    Returns a copy — the manifest's own dict is never mutated, since it is
    re-read on every boot and a resolved copy would be re-resolved. A path
    that escapes the package dir, or doesn't exist, is dropped with a
    warning: a missing prompt file is worth a broken agent, not a broken
    install.
    """
    out = {kind: [dict(e) for e in spec.get(kind, []) or []] for kind in KINDS}
    if not package_dir:
        return out
    pkg_root = os.path.realpath(package_dir)
    for kind, fields in FILE_FIELDS.items():
        for entry in out.get(kind, []):
            for field in fields:
                ref = entry.pop(f"{field}_file", None)
                if not ref or entry.get(field):
                    # An inline value wins — declaring both is a mistake, but
                    # the explicit string is the more specific intent.
                    continue
                target = os.path.realpath(os.path.join(pkg_root, str(ref)))
                if not target.startswith(pkg_root + os.sep) or not os.path.isfile(target):
                    log.warning(
                        "apps: %s_file %r for %s %r not found / outside the package",
                        field, ref, kind, entry.get("slug"),
                    )
                    continue
                try:
                    with open(target, encoding="utf-8") as fh:
                        entry[field] = fh.read()
                except OSError:
                    log.exception("apps: failed to read %s_file %r", field, ref)
    return out


def _any_declared(spec: dict[str, Any] | None) -> bool:
    return bool(spec) and any(spec.get(kind) for kind in KINDS)


def _counts(spec: dict[str, Any]) -> dict[str, int]:
    return {kind: len(spec.get(kind, []) or []) for kind in KINDS if spec.get(kind)}


def _merge(into: dict[str, int], other: dict[str, int]) -> None:
    for kind, n in (other or {}).items():
        into[kind] = into.get(kind, 0) + n


def _fmt(created: dict[str, int]) -> str:
    return ", ".join(f"{n} {kind}" for kind, n in created.items() if n) or "nothing"
