"""Decoupled-apps framework — Tier-1 in-process plugin runtime (F1).

See ``docs/knowledge_base/docs/architecture/decoupled-apps-framework.md`` (the
ADR) for the full design. F1 implements the foundation:

- ``manifest`` — parse + validate ``aw-app.json`` v1.
- ``base`` — the ``Plugin`` lifecycle contract + ``AppContext`` capability facades.
- ``journal`` — the append-only Action Journal (reversible uninstall).
- ``runtime`` — hot load/unload of an app's backend routes into the running
  FastAPI process, with in-flight-request drain on unload (NO restart).
- ``routes`` — the ``/api/apps`` management surface + boot reconcile.
"""
