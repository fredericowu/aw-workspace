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

from src.apps.agents import KINDS as AGENT_KINDS
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

#: Task types an app may declare in ``contributes.tasks``. Mirrors the types
#: ``aw-app-tasks``'s manager actually dispatches (``manager.py``'s run_task);
#: anything outside this set would be seeded into its table and then silently
#: skipped at fire time. ``agent_prompt`` is the one that hands the prompt to
#: an Agents Platform agent, which is what an app shipping both an agent
#: (``contributes.agents``) and the schedule that drives it needs.
CONTRIBUTED_TASK_TYPES = ("terminal", "agentic_output", "agent_prompt")


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
    def launchable_windows(self) -> list[dict[str, Any]]:
        """``windows`` minus any that only serve as a settings panel.

        An app like aw-app-git contributes one window purely to host its
        gear-icon settings form (auth method, token, ...) — nothing a user
        would "launch" from the Installed grid. Apps view categorization
        (UI vs Runnables) uses this instead of the raw ``windows`` list so
        settings-only apps land in Runnables alongside other CLI-only apps.
        """
        settings_window_ids = {p.get("window") for p in self.settings_panels}
        return [w for w in self.windows if w.get("id") not in settings_window_ids]

    @property
    def skills(self) -> list[dict[str, Any]]:
        return list(self.contributes.get("skills", []))

    @property
    def tasks(self) -> list[dict[str, Any]]:
        """``contributes.tasks`` — scheduled tasks the app seeds on install.

        Seeded, not owned: they are created once if a task of that name
        doesn't exist and are never rewritten afterwards, so the user's own
        edits always win. See ``src/apps/tasks.py``.
        """
        return list(self.contributes.get("tasks", []))

    @property
    def repos(self) -> list[dict[str, Any]]:
        """``contributes.repos`` — git checkouts the app clones into ``repos/``.

        Cloned once if ``repos/<name>`` is absent and never touched again
        afterwards — a working tree is the user's, not the app's. See
        ``src/apps/repos.py``.
        """
        return list(self.contributes.get("repos", []))

    @property
    def pip_requires(self) -> list[str]:
        """``runtime.pip_requires`` — Python deps a Tier-1 app needs importable.

        A Tier-1 app runs *in this process*, so anything it imports has to be in
        the workspace's own environment. Nine installed apps declare this key
        and, until 2026-08-14, nothing in core ever read it — the manifests were
        documentation. Apps worked only when their deps happened to already be
        present, which is why aw-app-tasks' cron scheduling (croniter) and
        aw-app-presentations' PNG export (playwright) were both dead on arrival
        in a fresh workspace while looking correctly declared.

        Tier-2 apps ship their deps in their own image; this is ignored for
        them. See ``AppRuntime._install_pip_requires``.
        """
        return [str(r) for r in (self.runtime.get("pip_requires") or []) if str(r).strip()]

    @property
    def sidecars(self) -> list[dict[str, Any]]:
        """``runtime.sidecars`` — extra containers this app's stack needs.

        A Tier-2 app is one image plus, sometimes, the companion services it
        cannot function without: aw-app-crispal is an MCP server *and* the
        WordPress + MySQL pair that serves the store's DEV clone. Declaring
        them here keeps the whole stack in one manifest with one lifecycle,
        instead of the monolith's arrangement where the app and its database
        were two hand-maintained entries in a global config file. Gated
        behind ``containers:manage``. See ``src/apps/containers.py``.
        """
        return list(self.runtime.get("sidecars", []))

    @property
    def ui_sidecar(self) -> str:
        """``runtime.ui_sidecar`` — the sidecar that serves this app's UI.

        A Tier-2 app is reverse-proxied at its own subdomain
        (``<slug>.app.<workspace>…``), which is what a ``managed_app`` window
        and the Apps grid both open. That proxy targets the app's own
        container, which is right when the app IS its UI — and wrong for
        aw-app-crispal, whose container is a headless MCP server while the
        thing a human wants to look at is the WordPress sidecar next to it.

        Naming the sidecar here re-points the proxy, and nothing else moves:
        the MCP is dialled container-to-container by name
        (``http://aw-app-crispal:9410/mcp``), never through this route.
        """
        return str(self.runtime.get("ui_sidecar", "") or "")

    @property
    def agents(self) -> dict[str, Any]:
        """``contributes.agents`` — Agents Platform objects the app seeds.

        An object of five ordered lists (``models``, ``agent_configs``,
        ``groups``, ``agents``, ``agent_flows``) with the same
        seeded-not-owned rule tasks use: created once if that slug is free,
        never rewritten, never removed on uninstall. See
        ``src/apps/agents.py``.
        """
        declared = self.contributes.get("agents")
        if not isinstance(declared, dict):
            return {}
        return {kind: list(declared.get(kind, []) or []) for kind in AGENT_KINDS}

    @property
    def reload_mcp_gateway_on_save(self) -> bool:
        """``contributes.mcp.reload_on_save`` — true for an app that ships
        its own root ``mcp.json`` (scanned directly by the MCP Gateway's
        app-scan, ADR "aw-app-mcp-tools contributes mcp.json"), where a
        config change can affect which servers/tools that file declares
        (e.g. a per-tool enable/disable toggle). ``save_app_config`` checks
        this after calling the plugin's ``on_config_saved`` hook (which is
        what actually rewrites the app's mcp.json) and, if true, calls the
        installed mcp-gateway app's ``POST /reload`` so the change takes
        effect without a full gateway restart.

        Distinct from ``contributes.mcp.provides`` (marketplace "what you
        get" tool list, see ``what_you_get``) — same ``contributes.mcp``
        object, different sibling key, unrelated purposes.

        NOTE: this is the narrow, opt-in signal. For deciding whether an
        install/uninstall/update needs a gateway rescan, use
        ``contributes_mcp`` instead — see its docstring for why an opt-in
        flag is the wrong gate there."""
        return bool(self.contributes.get("mcp", {}).get("reload_on_save", False))

    @property
    def contributes_mcp(self) -> bool:
        """Does this app put anything into the MCP Gateway's app-scan?

        The gateway discovers upstreams by scanning ``apps/<slug>/mcp.json``
        under ``AW_APP_SCAN_ROOTS``, so ANY app that ships or generates that
        file changes the gateway's world when it is installed, uninstalled
        or updated — whether or not it opted into
        ``contributes.mcp.reload_on_save``.

        Gating the install/uninstall reload on ``reload_on_save`` was the
        2026-08-12 bug: whether the gateway must rescan is the GATEWAY's
        concern, not something each app has to remember to declare.
        codegraphcontext and notion both ship an mcp.json with
        ``reload_on_save`` absent and were silently skipped.

        Manifest-only signal — apps whose mcp.json exists on disk without a
        ``contributes.mcp`` block (aw-app-browser, aw-app-code-server) are
        caught by the on-disk check in ``Reconciler._app_touches_mcp``,
        which layers a package-dir probe on top of this."""
        return bool(self.contributes.get("mcp"))

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
    def category(self) -> str:
        """UI grouping bucket (App Launcher / Workspace nav sidebar).

        Optional, forward-compatible top-level field — like ``publisher`` —
        so existing manifests keep validating unchanged. Defaults to
        ``"Apps"`` (the existing decoupled-apps grid bucket) when a manifest
        doesn't declare one.
        """
        return str(self.raw.get("category") or "Apps")

    @property
    def standalone_app(self) -> bool:
        """Whether this app has a separate process of its own to start/stop/
        restart independently of the aw-workspace process.

        No new manifest field needed — this is fully implied by the existing
        ``tier`` + ``runtime.standalone`` shape (ADR Decision 4):
          * ``tier: container`` — always its own process (a docker/podman
            container) => True.
          * ``tier: inprocess`` with a ``runtime.standalone`` block — can
            ALSO run as its own separate process => True.
          * ``tier: inprocess`` with no ``runtime.standalone`` block — routes
            register directly inside the aw-workspace process; any
            ``ctx.services``-managed subprocess it spawns (see
            ``src.apps.services.ServiceSupervisor``) is an internal
            implementation detail the app itself owns and restarts, not a
            unit of process control the framework/UI exposes => False.
        """
        if self.tier == "container":
            return True
        return bool(self.runtime.get("standalone"))

    @property
    def has_config(self) -> bool:
        """True when the app exposes a settings/config surface the gear opens.

        ``config_visible: false`` (top-level manifest field, default true) is
        an explicit override — an app can declare a real ``config_schema``
        for its own internal use (read via ``ctx.config``/the ``/config``
        route) without surfacing a Settings gear/entry for it. Not every app
        has user-facing settings (e.g. a Runnables-style CLI-only app), so
        this lets a manifest keep the schema without forcing UI on it.
        """
        if self.raw.get("config_visible") is False:
            return False
        return bool(
            self.settings_panels
            or self.config_schema.get("properties")
            or self.is_managed_app
        )

    @property
    def is_managed_app(self) -> bool:
        """True for app surfaces whose lifecycle is owned by the workspace UI.

        `body.type: managed_app` is the canonical app-authored signal. Tier-2
        container apps are treated as managed as well because the workspace
        owns their process controls even if an older manifest forgot the body
        hint.
        """
        if self.tier == "container":
            return True
        for win in self.windows:
            body = win.get("body") if isinstance(win, dict) else {}
            if isinstance(body, dict) and body.get("type") == "managed_app":
                return True
        return False

    @property
    def effective_config_schema(self) -> dict[str, Any]:
        """App schema plus framework-owned managed-app lifecycle settings."""
        schema = dict(self.config_schema or {})
        props = dict(schema.get("properties") or {})
        required = list(schema.get("required") or [])
        if self.is_managed_app:
            framework_props = {
                "auto_start": {
                    "type": "boolean",
                    "default": True,
                    "title": "Auto-start",
                    "description": "Start this app automatically when the workspace starts.",
                    "x-framework": True,
                },
                "auth_required": {
                    "type": "boolean",
                    "default": True,
                    "title": "Authentication required",
                    "description": "Only signed-in workspace users can open this app.",
                    "x-framework": True,
                },
                "public": {
                    "type": "boolean",
                    # Only a standalone app (its own process/subdomain — see
                    # standalone_app) has a public routing layer to toggle.
                    # A non-standalone managed app is always reachable
                    # through the workspace's own routing, so this reads as
                    # permanently "on" and locked instead of just vanishing
                    # (which would read as "not exposed" — misleading).
                    "default": True if not self.standalone_app else False,
                    "title": "Public",
                    "description": (
                        "This app has no standalone process/subdomain of its "
                        "own — it's always reachable through the workspace's "
                        "own routing, not independently toggleable."
                        if not self.standalone_app else
                        "Expose this app through the workspace public routing layer."
                    ),
                    "x-framework": True,
                    **({"x-disabled": True} if not self.standalone_app else {}),
                },
            }
            props = {**framework_props, **props}
        if props:
            schema["type"] = "object"
            schema["properties"] = props
            schema["required"] = required
        return schema

    def config_with_defaults(self, config: dict[str, Any] | None = None) -> dict[str, Any]:
        """Merge config_schema defaults with persisted config."""
        merged: dict[str, Any] = {}
        properties = self.effective_config_schema.get("properties") or {}
        for key, spec in properties.items():
            if isinstance(spec, dict) and "default" in spec:
                merged[key] = spec["default"]
        merged.update(dict(config or {}))
        # A locked x-disabled field (e.g. "public" on a non-standalone
        # managed app) always reads as its default — any older persisted
        # value predating this lock (or a stray PATCH) can't override it.
        for key, spec in properties.items():
            if isinstance(spec, dict) and spec.get("x-disabled"):
                merged[key] = spec["default"]
        return merged

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


#: Required fields per ``contributes.agents`` kind, beyond the slug every
#: entry needs. Kept narrow on purpose — Agents Platform validates the rest,
#: and duplicating its schema here would mean a manifest that this workspace
#: rejects for a field the platform has since made optional.
AGENT_REQUIRED_FIELDS: dict[str, tuple[str, ...]] = {
    "models": ("provider", "model_id"),
    "agent_configs": ("name",),
    "groups": ("name",),
    "agents": ("name",),
    "agent_flows": ("name",),
}


def _validate_contributed_agents(
    contributes: dict[str, Any], permissions: list[str]
) -> None:
    """Validate ``contributes.agents`` — an object of five ordered lists.

    The slug is the identity of a seeded object (``src/apps/agents.py``), so
    a missing or blank one is rejected here rather than becoming a silent
    duplicate later: every such entry would collide on the same empty key.
    """
    declared = contributes.get("agents")
    if declared is None:
        return
    if "agents:contribute" not in permissions:
        raise ManifestError("contributes.agents requires the 'agents:contribute' permission")
    if not isinstance(declared, dict):
        raise ManifestError(
            "contributes.agents must be an object with "
            f"{', '.join(AGENT_KINDS)} lists"
        )
    unknown = set(declared) - set(AGENT_KINDS)
    if unknown:
        raise ManifestError(
            f"unknown contributes.agents key(s) {sorted(unknown)} "
            f"(expected any of {list(AGENT_KINDS)})"
        )
    for kind in AGENT_KINDS:
        entries = declared.get(kind, [])
        if not isinstance(entries, list):
            raise ManifestError(f"contributes.agents.{kind} must be a list")
        for entry in entries:
            if not isinstance(entry, dict):
                raise ManifestError(f"each contributes.agents.{kind} entry must be an object")
            slug = str(entry.get("slug") or "").strip()
            if not slug:
                raise ManifestError(f"each contributes.agents.{kind} entry needs a 'slug'")
            if not SLUG_RE.match(slug):
                raise ManifestError(
                    f"contributes.agents.{kind}[].slug {slug!r} must be lowercase "
                    "alphanumeric with dashes"
                )
            for required in AGENT_REQUIRED_FIELDS[kind]:
                if not str(entry.get(required) or "").strip():
                    raise ManifestError(
                        f"contributes.agents.{kind}[{slug!r}] needs a {required!r}"
                    )
            # ``mcp_servers`` names MCP servers by reference — the provider
            # resolves each name against the workspace's own .mcp.json at seed
            # time. It exists so a manifest never has to carry the gateway's
            # bearer token, so the one thing to enforce is that it stays a
            # list of names: anything richer is someone starting to inline the
            # credential this indirection exists to keep out of a public
            # artefact.
            refs = entry.get("mcp_servers")
            if refs is not None:
                if not isinstance(refs, list) or not all(
                    isinstance(n, str) and n.strip() for n in refs
                ):
                    raise ManifestError(
                        f"contributes.agents.{kind}[{slug!r}].mcp_servers must be "
                        "a list of MCP server names (the provider resolves each "
                        "name locally — credentials must not appear in a manifest)"
                    )


def _validate_contributed_repos(
    contributes: dict[str, Any], permissions: list[str]
) -> None:
    """Validate ``contributes.repos`` — a list of ``{name, url}`` checkouts.

    ``name`` is both the identity and the directory under ``repos/``, so it
    is constrained to a plain directory name here: an app must not be able to
    pick where in the filesystem core writes (``src/apps/repos.py`` refuses
    the same shapes again at clone time, since a manifest is not the only way
    a spec can reach it).
    """
    declared = contributes.get("repos")
    if declared is None:
        return
    if "repos:contribute" not in permissions:
        raise ManifestError("contributes.repos requires the 'repos:contribute' permission")
    if not isinstance(declared, list):
        raise ManifestError("contributes.repos must be a list")
    for entry in declared:
        if not isinstance(entry, dict):
            raise ManifestError("each contributes.repos entry must be an object")
        name = str(entry.get("name") or "").strip()
        url = str(entry.get("url") or "").strip()
        if not name or not url:
            raise ManifestError("each contributes.repos entry needs a 'name' and a 'url'")
        if "/" in name or "\\" in name or name in (".", ".."):
            raise ManifestError(
                f"contributes.repos[].name {name!r} must be a plain directory name"
            )
        depth = entry.get("depth")
        if depth is not None and (
            not isinstance(depth, int) or isinstance(depth, bool) or depth <= 0
        ):
            raise ManifestError("contributes.repos[].depth must be a positive integer")


def _validate_sidecars(runtime: dict[str, Any], permissions: list[str]) -> None:
    """Validate ``runtime.sidecars`` — companion containers of a Tier-2 app.

    Each needs a ``name`` (namespaced into ``aw-app-<app>-<name>``, so the
    same plain-directory-name constraint applies for a predictable DNS name)
    and an ``image``. ``port`` is optional: a database sidecar has nothing to
    expose to the workspace, only to its siblings on the podman network.
    """
    declared = runtime.get("sidecars")
    if declared is None:
        return
    if "containers:manage" not in permissions:
        raise ManifestError("runtime.sidecars requires the 'containers:manage' permission")
    if not isinstance(declared, list):
        raise ManifestError("runtime.sidecars must be a list")
    seen: set[str] = set()
    for entry in declared:
        if not isinstance(entry, dict):
            raise ManifestError("each runtime.sidecars entry must be an object")
        name = str(entry.get("name") or "").strip()
        if not name or not SLUG_RE.match(name):
            raise ManifestError(
                "each runtime.sidecars entry needs a lowercase alphanumeric-with-dashes 'name'"
            )
        if name in seen:
            raise ManifestError(f"duplicate runtime.sidecars name {name!r}")
        seen.add(name)
        if not str(entry.get("image") or "").strip():
            raise ManifestError(f"runtime.sidecars[{name!r}] needs an 'image'")
        port = entry.get("port")
        if port is not None and (
            not isinstance(port, int) or isinstance(port, bool) or port <= 0
        ):
            raise ManifestError(
                f"runtime.sidecars[{name!r}].port must be a positive integer"
            )

    ui_sidecar = runtime.get("ui_sidecar")
    if ui_sidecar is None:
        return
    if not isinstance(ui_sidecar, str) or not ui_sidecar.strip():
        raise ManifestError("runtime.ui_sidecar must be a non-empty string")
    ui_sidecar = ui_sidecar.strip()
    if ui_sidecar not in seen:
        raise ManifestError(
            f"runtime.ui_sidecar {ui_sidecar!r} names no declared sidecar "
            f"(have {sorted(seen)})"
        )
    # The proxy has to have somewhere to connect TO. A sidecar with no port
    # is one nothing outside its own network reaches (a database), and
    # pointing the app's public subdomain at it would mount a proxy to
    # port 0 — a 502 on every request, with a manifest that read as correct.
    for entry in declared:
        if str(entry.get("name") or "").strip() == ui_sidecar and not entry.get("port"):
            raise ManifestError(
                f"runtime.ui_sidecar {ui_sidecar!r} must declare a 'port' — "
                f"it is what the app's reverse proxy connects to"
            )


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

    tasks = contributes.get("tasks", [])
    if tasks and "tasks:contribute" not in permissions:
        raise ManifestError("contributes.tasks requires the 'tasks:contribute' permission")
    if not isinstance(tasks, list):
        raise ManifestError("contributes.tasks must be a list")
    for task in tasks:
        if not isinstance(task, dict) or not task.get("name"):
            raise ManifestError("each contributes.tasks entry needs a 'name'")
        # The name IS the identity of a contributed task (see
        # ``src/apps/tasks.py``) — a blank or whitespace-only one would make
        # every such task collide on the same empty key.
        if not str(task["name"]).strip():
            raise ManifestError("contributes.tasks[].name must not be blank")
        task_type = task.get("type", "terminal")
        if task_type not in CONTRIBUTED_TASK_TYPES:
            raise ManifestError(
                f"contributes.tasks[].type must be one of "
                f"{', '.join(repr(t) for t in CONTRIBUTED_TASK_TYPES)} "
                f"(got {task_type!r})"
            )
        if task_type == "agentic_output" and not task.get("command"):
            raise ManifestError("an 'agentic_output' task needs a 'command'")
        if task_type == "terminal" and not task.get("prompt"):
            raise ManifestError("a 'terminal' task needs a 'prompt'")
        # An 'agent_prompt' task dispatches to an Agents Platform agent, so it
        # needs both the prompt to send and the slug to send it to. Validating
        # the slug's presence here (rather than letting the tasks app create a
        # task that can never fire) keeps the failure at install time, where
        # there is someone to read it — an unroutable schedule that only fails
        # at 03:00 is exactly the silent degradation this workspace is prone to.
        if task_type == "agent_prompt":
            if not task.get("prompt"):
                raise ManifestError("an 'agent_prompt' task needs a 'prompt'")
        # 'agentic_output' dispatches to an agent too — only on a notable exit
        # code, but through the identical path (manager.py::_run_agentic_output
        # bails with "no agent_slug configured" *before* running the command).
        # This check used to cover 'agent_prompt' alone, so aw-app-crispal's
        # "Arvin History Cleanup" seeded a row with agent_slug NULL and every
        # fire died on that guard, never running the cleanup (2026-08-15).
        # aw-app-tasks rejects the same shape over its REST API — a task the
        # user cannot create by hand must not be reachable by seeding either.
        if task_type in ("agent_prompt", "agentic_output"):
            if not str(task.get("agent_slug") or "").strip():
                raise ManifestError(f"an '{task_type}' task needs an 'agent_slug'")
        if not isinstance(task.get("schedules", []), list):
            raise ManifestError("contributes.tasks[].schedules must be a list")

    _validate_contributed_agents(contributes, permissions)
    _validate_contributed_repos(contributes, permissions)
    _validate_sidecars(runtime, permissions)

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
