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

from src.apps.capabilities import is_valid_capability

# slug rule (ADR Decision 8): the namespace key for routes/tables/commands/...
SLUG_RE = re.compile(r"^[a-z][a-z0-9-]{1,40}$")
# semver-ish: MAJOR.MINOR.PATCH with optional pre-release/build
SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$")

TIERS = {"inprocess", "container"}

# Marketplace metadata (F6b): optional, defaulted at validation time so
# existing manifests keep working unchanged.
DEFAULT_PUBLISHER = "TekFlox"
# Only CPU is a traffic-light level (green/amber/red in the UI). Memory and
# disk are FREE-FORM estimates of *server* usage — a size string like "~200 MB"
# / "1 GB", or "-" for something that doesn't stay resident (e.g. the git app
# just installs a CLI; nothing runs in memory). See manifest schema notes.
RESOURCE_LEVELS = {"low", "medium", "high"}
DEFAULT_RESOURCE_ESTIMATE = {"cpu": "low", "memory": "-", "disk": "-"}


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
    publisher: str = DEFAULT_PUBLISHER
    resource_estimate: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_RESOURCE_ESTIMATE))
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

    @property
    def settings_panels(self) -> list[dict[str, Any]]:
        return list(self.contributes.get("settings_panels", []))

    @property
    def skills(self) -> list[dict[str, Any]]:
        return list(self.contributes.get("skills", []))

    @property
    def frontend(self) -> dict[str, Any]:
        """First-class frontend code plugin (ADR Decision 3b), or ``{}``."""
        fe = self.contributes.get("frontend")
        return dict(fe) if isinstance(fe, dict) else {}

    @property
    def icon(self) -> str:
        """Icon hint — first nav entry's icon, or the top-level ``icon`` field."""
        for entry in self.nav:
            if entry.get("icon"):
                return str(entry["icon"])
        return str(self.raw.get("icon", ""))

    @property
    def has_config(self) -> bool:
        """True when the app exposes a settings/config surface the gear opens."""
        return bool(self.settings_panels or self.config_schema.get("properties"))

    @property
    def requires_ui_refresh(self) -> bool:
        """Whether installing/updating/configuring this app changes something
        the SPA only picks up on a fresh page load — new `contributes.nav`
        entries, slot contributions, or a `contributes.frontend` bundle (ADR
        Decision 3b). `GET /api/apps/-/contributions` IS refetched live, but
        nothing currently re-mounts nav/slots from a stale fetch without a
        real reload. Apps that only add backend routes/settings panels (no
        nav/frontend contribution) should leave this false — the default —
        since those show up in the Installed list without a reload already.
        Not schema-validated (optional, forward-compatible like `publisher`);
        read straight off the raw manifest dict."""
        return bool(self.raw.get("requires_ui_refresh", False))

    @property
    def what_you_get(self) -> dict[str, list[str]]:
        """Marketplace detail-view summary — MCP tools / UI screens / runnable
        commands, derived from ``contributes`` (empty where nothing is declared)."""
        mcp_tools: list[str] = []
        for tool in self.contributes.get("mcp", {}).get("provides", []):
            if isinstance(tool, dict) and tool.get("name"):
                mcp_tools.append(str(tool["name"]))
            elif isinstance(tool, str) and tool:
                mcp_tools.append(tool)

        ui_screens: list[str] = []
        for win in self.windows:
            label = win.get("title") or win.get("id")
            if label and label not in ui_screens:
                ui_screens.append(str(label))
        for entry in self.nav:
            label = entry.get("label")
            if label and label not in ui_screens:
                ui_screens.append(str(label))

        commands: list[str] = []
        for cli in self.contributes.get("system_clis", []):
            if isinstance(cli, dict) and cli.get("name"):
                commands.append(str(cli["name"]))
        for cmd in self.contributes.get("commands", []):
            if isinstance(cmd, dict) and cmd.get("name"):
                commands.append(str(cmd["name"]))
            elif isinstance(cmd, str) and cmd:
                commands.append(cmd)

        skills: list[str] = []
        for skill in self.skills:
            if isinstance(skill, dict) and skill.get("id"):
                skills.append(str(skill["id"]))

        return {"mcp_tools": mcp_tools, "ui_screens": ui_screens, "commands": commands,
                "skills": skills}


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
    if tier == "container":
        if not str(runtime.get("image", "")).strip():
            raise ManifestError("container apps require runtime.image")
        if not isinstance(runtime.get("port"), int) or runtime.get("port") <= 0:
            raise ManifestError("container apps require a positive integer runtime.port")

    # Dual-mode standalone block (ADR "Apps Own Their Front + Back Routes"
    # Decision 4) — optional, but if present must have the right shape.
    standalone = runtime.get("standalone")
    if standalone is not None:
        if not isinstance(standalone, dict):
            raise ManifestError("runtime.standalone must be an object")
        module = standalone.get("module")
        if not isinstance(module, str) or not module.strip():
            raise ManifestError("runtime.standalone.module must be a non-empty string")
        port = standalone.get("default_port")
        if not isinstance(port, int) or isinstance(port, bool) or port <= 0:
            raise ManifestError("runtime.standalone.default_port must be a positive integer")

    permissions = data.get("permissions", [])
    if not isinstance(permissions, list) or not all(isinstance(p, str) for p in permissions):
        raise ManifestError("permissions must be a list of strings")
    for perm in permissions:
        if not is_valid_capability(perm):
            raise ManifestError(f"unknown permission {perm!r}")
    if tier == "container" and "containers:manage" not in permissions:
        raise ManifestError("container apps require the 'containers:manage' permission")

    contributes = data.get("contributes", {})
    if not isinstance(contributes, dict):
        raise ManifestError("contributes must be an object")

    # Any app that mounts routes must declare the capability (ADR Decision 4).
    if contributes.get("routes") and "routes:register" not in permissions:
        raise ManifestError("contributes.routes requires the 'routes:register' permission")

    # local_paths (agent-driven localhost bypass) requires 'routes:local'
    # (ADR "Apps Own Their Front + Back Routes" Decision 2).
    routes = contributes.get("routes", [])
    if not isinstance(routes, list):
        raise ManifestError("contributes.routes must be a list")
    has_local_paths = any(
        isinstance(r, dict) and r.get("local_paths") for r in routes
    )
    if has_local_paths and "routes:local" not in permissions:
        raise ManifestError(
            "contributes.routes[].local_paths requires the 'routes:local' permission"
        )

    for win in contributes.get("windows", []):
        if not isinstance(win, dict) or not win.get("id"):
            raise ManifestError("each contributes.windows entry needs an 'id'")
        if not str(win.get("id", "")).startswith(f"{slug}."):
            raise ManifestError(
                f"window id {win.get('id')!r} must be namespaced under '{slug}.'"
            )

    for skill in contributes.get("skills", []):
        if not isinstance(skill, dict) or not skill.get("id") or not skill.get("path"):
            raise ManifestError("each contributes.skills entry needs an 'id' and a 'path'")

    config_schema = data.get("config_schema", {})
    if not isinstance(config_schema, dict):
        raise ManifestError("config_schema must be an object")

    publisher = data.get("publisher", DEFAULT_PUBLISHER)
    if not isinstance(publisher, str) or not publisher.strip():
        raise ManifestError("publisher must be a non-empty string")

    resource_estimate_in = data.get("resource_estimate", {})
    if not isinstance(resource_estimate_in, dict):
        raise ManifestError("resource_estimate must be an object")
    resource_estimate = {**DEFAULT_RESOURCE_ESTIMATE, **resource_estimate_in}
    # CPU is a traffic-light level; memory/disk are free-form size estimates
    # (or "-"). Normalize memory/disk to a string so the UI can render them raw.
    if resource_estimate.get("cpu") not in RESOURCE_LEVELS:
        raise ManifestError(
            f"resource_estimate.cpu must be one of {sorted(RESOURCE_LEVELS)} "
            f"(got {resource_estimate.get('cpu')!r})"
        )
    for key in ("memory", "disk"):
        val = resource_estimate.get(key)
        if not isinstance(val, str) or not val.strip():
            raise ManifestError(
                f"resource_estimate.{key} must be a non-empty string "
                f'(a size estimate like "~200 MB" or "-"; got {val!r})'
            )

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
        publisher=publisher,
        resource_estimate=resource_estimate,
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
