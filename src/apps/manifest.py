"""``aw-app.json`` v1 manifest loader + validator (ADR Decision 2).

Validated at install AND at load. This is a hand-written validator (no
jsonschema dependency on the slim image) that covers the v1 fields F1 needs;
unknown extra keys are tolerated forward-compatibly, malformed known keys are
rejected with a precise message.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import Any

# slug rule (ADR Decision 8): the namespace key for routes/tables/commands/...
SLUG_RE = re.compile(r"^[a-z][a-z0-9-]{1,40}$")
# semver-ish: MAJOR.MINOR.PATCH with optional pre-release/build
SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$")

TIERS = {"inprocess", "container"}

# Capability strings the consent UI knows about (ADR Decision 4). Two are
# parameterised prefixes rather than exact matches.
_EXACT_PERMISSIONS = {
    "routes:register",
    "db:own-tables",
    "commands:install",
    "service:manage",
    "containers:manage",
    "net:outbound",
    "fs:workspace-data",
    "secrets:own",
    "ui:code",
}
_PREFIX_PERMISSIONS = ("config:extend:", "ui:slots:")


class ManifestError(ValueError):
    """Raised when an ``aw-app.json`` fails v1 validation."""


@dataclass
class Manifest:
    """A validated ``aw-app.json`` v1."""

    id: str
    name: str
    version: str
    tier: str
    manifest_version: int = 1
    description: str = ""
    runtime: dict[str, Any] = field(default_factory=dict)
    permissions: list[str] = field(default_factory=list)
    contributes: dict[str, Any] = field(default_factory=dict)
    config_schema: dict[str, Any] = field(default_factory=dict)
    dependencies: dict[str, Any] = field(default_factory=dict)
    migrations: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def entrypoint(self) -> str:
        """``module:ClassName`` for the Tier-1 plugin class (inprocess only)."""
        return str(self.runtime.get("entrypoint", ""))

    @property
    def windows(self) -> list[dict[str, Any]]:
        return list(self.contributes.get("windows", []))

    @property
    def nav(self) -> list[dict[str, Any]]:
        return list(self.contributes.get("nav", []))


def _require_permission(perm: str) -> bool:
    if perm in _EXACT_PERMISSIONS:
        return True
    return any(perm.startswith(p) and len(perm) > len(p) for p in _PREFIX_PERMISSIONS)


def validate_manifest(data: dict[str, Any]) -> Manifest:
    """Validate a parsed manifest dict against the v1 schema; return a Manifest.

    Raises :class:`ManifestError` with a precise message on the first problem.
    """
    if not isinstance(data, dict):
        raise ManifestError("manifest must be a JSON object")

    mv = data.get("manifest_version")
    if mv != 1:
        raise ManifestError(f"manifest_version must be 1, got {mv!r}")

    slug = data.get("id")
    if not isinstance(slug, str) or not SLUG_RE.match(slug):
        raise ManifestError(
            f"id must match {SLUG_RE.pattern} (got {slug!r})"
        )

    name = data.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ManifestError("name is required and must be a non-empty string")

    version = data.get("version")
    if not isinstance(version, str) or not SEMVER_RE.match(version):
        raise ManifestError(f"version must be semver MAJOR.MINOR.PATCH (got {version!r})")

    tier = data.get("tier")
    if tier not in TIERS:
        raise ManifestError(f"tier must be one of {sorted(TIERS)} (got {tier!r})")

    runtime = data.get("runtime", {})
    if not isinstance(runtime, dict):
        raise ManifestError("runtime must be an object")
    if tier == "inprocess" and not str(runtime.get("entrypoint", "")).strip():
        raise ManifestError("inprocess apps require runtime.entrypoint (\"module:Class\")")
    if tier == "inprocess" and ":" not in str(runtime.get("entrypoint", "")):
        raise ManifestError("runtime.entrypoint must be \"module:ClassName\"")

    permissions = data.get("permissions", [])
    if not isinstance(permissions, list) or not all(isinstance(p, str) for p in permissions):
        raise ManifestError("permissions must be a list of strings")
    for perm in permissions:
        if not _require_permission(perm):
            raise ManifestError(f"unknown permission {perm!r}")

    contributes = data.get("contributes", {})
    if not isinstance(contributes, dict):
        raise ManifestError("contributes must be an object")

    # Any app that mounts routes must declare the capability (ADR Decision 4).
    if contributes.get("routes") and "routes:register" not in permissions:
        raise ManifestError("contributes.routes requires the 'routes:register' permission")

    for win in contributes.get("windows", []):
        if not isinstance(win, dict) or not win.get("id"):
            raise ManifestError("each contributes.windows entry needs an 'id'")
        if not str(win.get("id", "")).startswith(f"{slug}."):
            raise ManifestError(
                f"window id {win.get('id')!r} must be namespaced under '{slug}.'"
            )

    config_schema = data.get("config_schema", {})
    if not isinstance(config_schema, dict):
        raise ManifestError("config_schema must be an object")

    return Manifest(
        id=slug,
        name=name,
        version=version,
        tier=tier,
        manifest_version=mv,
        description=str(data.get("description", "")),
        runtime=runtime,
        permissions=permissions,
        contributes=contributes,
        config_schema=config_schema,
        dependencies=data.get("dependencies", {}) or {},
        migrations=data.get("migrations", {}) or {},
        raw=data,
    )


def load_manifest(package_dir: str) -> Manifest:
    """Read + validate ``<package_dir>/aw-app.json``."""
    path = os.path.join(package_dir, "aw-app.json")
    if not os.path.isfile(path):
        raise ManifestError(f"no aw-app.json at {package_dir}")
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        raise ManifestError(f"aw-app.json is not valid JSON: {e}") from e
    return validate_manifest(data)
