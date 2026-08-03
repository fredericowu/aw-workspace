"""Authoritative capability catalog + trust tier (ADR Decision 4) — F2.

This is the single source of truth for the permission strings an ``aw-app.json``
may declare, their risk tier, and the trust/signing rule that gates high-risk
capabilities. Both the manifest validator (:mod:`src.apps.manifest`) and the
runtime enforcement (:mod:`src.apps.base` / :mod:`src.apps.runtime`) consume it,
and aw-backend keeps a byte-identical mirror (``src/api/app_capabilities.py``)
so the cloud registry grants the same set the workspace enforces.

Capability strings come in two shapes:

* **exact** — a fixed string (``routes:register``).
* **parameterised** — a prefix + an argument (``config:extend:<app>``,
  ``ui:slots:<slot-id>``); the argument is app/slot-specific.

Trust tier (ADR Decision 4 / 3b): a handful of capabilities are **high-risk**
(``ui:code``, ``containers:manage``, ``config:extend:*``) and may only be
granted to a **signed/marketplace** app. A side-loaded (unsigned) app requesting
one is refused the grant — not merely warned. ``ui:code`` in particular is
hard-gated per the frontend-plugin decision.
"""
from __future__ import annotations

# --- catalog -----------------------------------------------------------------

# risk: "low" | "high". high-risk caps require a signed/marketplace app.
CATALOG: dict[str, dict[str, str]] = {
    "routes:register":   {"risk": "low",  "desc": "mount /api/apps/<id>/* routes"},
    "routes:local":      {"risk": "low",  "desc": "declared local_paths bypass identity auth for in-workspace localhost callers"},
    "db:own-tables":     {"risk": "low",  "desc": "create/use app-owned workspace tables"},
    "commands:install":  {"risk": "low",  "desc": "install commands/CLIs that survive restart"},
    "service:manage":    {"risk": "low",  "desc": "register a start/stop background service"},
    "watchdog:tasks":    {"risk": "low",  "desc": "register in-process periodic (watchdog) tasks"},
    "net:outbound":      {"risk": "low",  "desc": "make outbound HTTP from Tier-1 code"},
    "fs:workspace-data": {"risk": "low",  "desc": "read/write under the app's own data dir"},
    "secrets:own":       {"risk": "low",  "desc": "request the app's own secrets"},
    "notifications:send": {"risk": "low", "desc": "fire a notification via the workspace notification engine"},
    "containers:manage": {"risk": "high", "desc": "run/manage sidecar containers"},
    "ui:code":           {"risk": "high", "desc": "load the app's JS bundle into the SPA context"},
    "mcp:register-gateway": {"risk": "high", "desc": "mount the workspace's root .mcp.json (read/write, single file) to register this app's own MCP endpoint"},
}

# parameterised capabilities: prefix -> risk. The argument after the prefix is
# app/slot-specific and validated only for non-emptiness.
PREFIX_CATALOG: dict[str, str] = {
    "config:extend:": "high",   # write config INTO another app's extension point
    "ui:slots:":      "low",    # render into a specific named slot (per-slot grant)
}

EXACT_PERMISSIONS = frozenset(CATALOG)
PREFIX_PERMISSIONS = tuple(PREFIX_CATALOG)


def is_valid_capability(cap: str) -> bool:
    """True if ``cap`` is a known exact or parameterised capability string."""
    if not isinstance(cap, str) or not cap:
        return False
    if cap in EXACT_PERMISSIONS:
        return True
    return any(cap.startswith(p) and len(cap) > len(p) for p in PREFIX_PERMISSIONS)


def is_high_risk(cap: str) -> bool:
    """True if ``cap`` may only be granted to a signed/marketplace app."""
    if cap in CATALOG:
        return CATALOG[cap]["risk"] == "high"
    for prefix, risk in PREFIX_CATALOG.items():
        if cap.startswith(prefix) and len(cap) > len(prefix):
            return risk == "high"
    return False


def check_app_signature(app_id: str, version: str, source: str | None = None) -> bool:
    """Trust/signing hook (STUB — full marketplace hash-pinning is F8).

    F2 has no signing infra yet: this returns ``False`` (untrusted) for
    everything. The install path passes an explicit ``signed`` flag instead; F8
    replaces this with verification of the installed package hash against what
    the marketplace catalog published (ADR Decision 4).
    """
    return False


def filter_grants(requested: list[str], *, signed: bool) -> tuple[list[str], list[str]]:
    """Split *valid* requested capabilities into (granted, refused) by trust.

    Unknown capabilities are NOT handled here — validate them first
    (:func:`src.apps.manifest.validate_manifest` / the install path). Order is
    preserved; each capability appears in exactly one list.

    Signature/trust gating is DISABLED (Frederico decision 2026-08-01): F2 has
    no real signing infra yet (``check_app_signature`` above is a permanent
    stub until F8), so refusing high-risk caps for "unsigned" only blocked
    every app uniformly, including first-party marketplace catalog apps —
    not a meaningful security boundary yet. Every requested capability is
    granted; nothing is ever refused. Left commented rather than deleted so
    re-enabling this once F8 lands is a one-line uncomment, not a rewrite.
    """
    # for cap in requested:
    #     if is_high_risk(cap) and not signed:
    #         refused.append(cap)
    #     else:
    #         granted.append(cap)
    return list(requested), []
