"""Tier-2 container supervisor (ADR contribution point ``containers``, gated by
the ``containers:manage`` capability) — Phase 6.

Mirrors :class:`src.apps.services.ServiceSupervisor`, but instead of a local
subprocess it drives a **container engine** over its Docker-API socket. In BYOD
deployments that engine is the host's *rootless* podman (socket at
``/run/user/<uid>/podman/podman.sock``), which speaks the Docker API — so the
Python ``docker`` SDK talks to it unchanged. The socket path is injected via
``AW_CONTAINER_SOCKET``; without it Tier-2 is simply unavailable (``available``
is False) and the runtime refuses to load a ``tier: container`` app.

One container per app (the Tier-2 unit): deterministic name ``aw-app-<slug>``,
keyed by ``app_id``. The supervisor pulls the image if missing, applies the
manifest's ``run_flags`` (e.g. ``--shm-size=1g``) and ``resources`` (mem/cpu),
and — so the workspace can reach it — attaches it to the shared podman network
(``AW_CONTAINER_NETWORK``) and addresses it by name, mirroring how the workspace
already reaches its postgres/redis siblings.

Elevated device access (``/dev/kvm`` for a guest VM, ``/dev/net/tun`` for its
NIC) is NOT expressible as a run flag and never has been — ``--privileged`` is
rejected here whatever asks for it. It goes through ``runtime.host_power``
instead, which additionally requires a matching high-risk capability and the
BYOD host's own opt-in; see :mod:`src.apps.hostpower`. A container with no
resolved grant is created exactly as before: ``privileged=False``, no devices,
no added capabilities.

Registration is JOURNALED by the runtime so uninstall stops + removes the
container (reverse replay) — no orphan containers survive an uninstall.
"""
from __future__ import annotations

import logging
import os
import re
import shlex
import socket
from typing import Any

from src.apps import hostpower

log = logging.getLogger(__name__)


class ContainerError(RuntimeError):
    pass


_ENV_PLACEHOLDER = re.compile(r"^\$\{(config|env|app)\.([A-Za-z_][A-Za-z0-9_.]*)\}$")
#: ``${a.b|c.d}`` — try each source left to right, first non-empty wins.
_ENV_PLACEHOLDER_CHAIN = re.compile(
    r"^\$\{((?:config|env|app)\.[A-Za-z_][A-Za-z0-9_.]*"
    r"(?:\|(?:config|env|app)\.[A-Za-z_][A-Za-z0-9_.]*)+)\}$")


def app_public_url(app_id: str) -> str:
    """This app's own external URL, e.g. ``https://crispal.app.aw.workspace...``.

    Composed, not stored. A standalone app served through the tunnel edge sits
    at ``<app_id>.app.<slug>.<base_domain>``, mirroring the API's
    ``api.<slug>.<base_domain>`` (see src/api/workspace_url.py).

    This exists because the alternative — a config value someone types in —
    cannot have a sensible manifest default: the URL contains the workspace
    slug, so any literal is right for exactly one workspace and wrong
    everywhere else. aw-app-crispal shipped
    ``site_url: "http://aw-app-crispal-wordpress:10002"`` as its default,
    which is the CONTAINER hostname; when the app's config was reset to
    schema defaults (2026-08-14) WordPress began emitting every asset URL
    against a host no browser can reach, and the storefront rendered with no
    CSS and no jQuery. Derived beats stored: nothing to lose on a reinstall.
    """
    from src.api import workspace_url

    slug = os.environ.get(workspace_url.SLUG_ENV_VAR, "").strip()
    if not slug or not app_id:
        return ""
    return f"https://{app_id}.app.{slug}.{workspace_url.base_domain()}"


def expand_env(env: dict[str, Any] | None, config: dict[str, Any] | None,
               app_id: str = "") -> dict[str, str]:
    """Resolve ``runtime.env`` placeholders against an app's config.

    A container app's ``config_schema`` was previously write-only in
    practice: the user could fill in a field and nothing carried it into the
    container, because ``runtime.env`` was passed through verbatim. Every
    such app had to be started by hand with the right ``-e`` flags — which
    is exactly what a manifest is supposed to remove.

    Two placeholder forms, both whole-value (never interpolated into a larger
    string, so a literal ``$`` in a value is never mangled):

    * ``${config.<key>}`` — the app's own saved config value. This is the
      one that makes a ``config_schema`` field actually do something.
    * ``${env.<VAR>}``    — a variable from the workspace process, for
      values the workspace owns and the user shouldn't retype (its backend
      URL, its host token).
    * ``${app.url}``      — this app's own external URL, composed from the
      workspace slug and base domain (see :func:`app_public_url`).

    Sources can be chained with ``|`` and the first non-empty one wins:
    ``${config.site_url|app.url}`` lets a user override the derived URL
    without forcing every install to store one.

    **An unresolved placeholder drops the variable entirely** rather than
    passing an empty string. That matters: images legitimately set their own
    ``ENV`` defaults, and injecting ``FOO=""`` would silently override a
    working default with nothing. Absent means "not configured", which is
    what the image's own fallback is for.
    """
    out: dict[str, str] = {}
    for key, raw in (env or {}).items():
        if not isinstance(raw, str):
            out[key] = str(raw)
            continue
        value = expand_value(raw, config, app_id)
        if value is None:
            log.debug("apps: env %s unresolved (%s) — leaving it unset", key, raw)
            continue
        out[key] = value
    return out


def expand_value(raw: str, config: dict[str, Any] | None,
                 app_id: str = "") -> str | None:
    """Resolve ONE placeholder-shaped string. ``None`` means unresolved.

    Split out of :func:`expand_env` so the same placeholder grammar can be
    applied outside ``runtime.env`` — see ``src/apps/mcp_template.py``, which
    walks an app's ``mcp.template.json`` with it. Keeping one implementation
    matters more than the indirection costs: an app author who learns
    ``${config.x|env.Y}`` in a manifest should not find a second, subtly
    different dialect one file over.

    A string with no placeholder is returned unchanged (never ``None``) —
    only a placeholder whose every source is empty resolves to ``None``.
    """
    config = config or {}

    def resolve(kind: str, name: str) -> Any:
        if kind == "config":
            return config.get(name)
        if kind == "env":
            return os.environ.get(name)
        if kind == "app":
            return app_public_url(app_id) if name == "url" else None
        return None

    stripped = raw.strip()
    chain = _ENV_PLACEHOLDER_CHAIN.match(stripped)
    if chain:
        sources = [s.split(".", 1) for s in chain.group(1).split("|")]
    else:
        match = _ENV_PLACEHOLDER.match(stripped)
        if not match:
            return raw
        sources = [list(match.groups())]

    for kind, name in sources:
        value = resolve(kind, name)
        if value is not None and value != "":
            return str(value)
    return None


def _registry_host(image: str) -> str:
    """Registry an image reference points at, ``docker.io`` when implicit.

    An image ref's first path segment is the registry only if it looks like
    a host (contains a dot or a port, or is ``localhost``) — ``tekflox/foo``
    is a Docker Hub namespace, ``ghcr.io/tekflox/foo`` is not.
    """
    head = image.split("/", 1)[0]
    if "." in head or ":" in head or head == "localhost":
        return head
    return "docker.io"


def _registry_auth(image: str) -> dict | None:
    """Credentials for pulling ``image``, or None to pull anonymously.

    A private app image (published alongside a private marketplace) can't be
    pulled anonymously, and an unauthenticated pull fails in a way that looks
    like "registry unreachable" — the app then silently starts from a stale
    cached image, or not at all. See ``marketplace.registry_credential``.
    """
    try:
        from src.api.marketplace import registry_credential

        cred = registry_credential(_registry_host(image))
    except Exception as e:  # noqa: BLE001 — never block a public pull on this
        log.debug("apps: registry credential lookup failed for %s (%s)", image, e)
        return None
    if not cred:
        return None
    log.info("apps: using a marketplace credential to pull %s", image)
    return {"username": cred[0], "password": cred[1]}


def _parse_run_flags(run_flags: list[str] | None) -> dict:
    """Map a manifest's ``run_flags_needed`` CLI flags to ``docker`` SDK kwargs.

    Only the small, safe subset apps actually need is understood; ``--privileged``
    is rejected outright (Tier-2 trust rule). An unknown flag raises rather than
    being silently dropped, so a manifest can't quietly ask for something the
    supervisor won't honor.

    ``--privileged`` stays rejected here even now that ``runtime.host_power``
    exists, because this channel carries none of that one's checks: a run flag
    is not matched against a capability and not matched against the host's
    opt-in, so honouring it would be a way around both.
    """
    kwargs: dict = {}
    for flag in run_flags or []:
        name, _, value = flag.partition("=")
        if name in ("--privileged",):
            raise ContainerError(
                "run flag --privileged is not allowed for Tier-2 apps — declare "
                "runtime.host_power: [\"privileged\"] plus the 'host:privileged' "
                "permission instead, which also requires the host's own opt-in")
        if name == "--shm-size":
            if not value:
                raise ContainerError("--shm-size requires a value (e.g. --shm-size=1g)")
            kwargs["shm_size"] = value
        else:
            raise ContainerError(f"unsupported run flag {name!r}")
    return kwargs


def _resource_kwargs(resources: dict | None) -> dict:
    """Map ``runtime.resources`` (``{"cpus": 0.5, "mem_mb": 512}``) to hard limits."""
    kwargs: dict = {}
    if not resources:
        return kwargs
    mem_mb = resources.get("mem_mb")
    if mem_mb:
        kwargs["mem_limit"] = f"{int(mem_mb)}m"
    cpus = resources.get("cpus")
    if cpus:
        kwargs["nano_cpus"] = int(float(cpus) * 1_000_000_000)
    return kwargs


def _port_range(value: int | str, label: str) -> list[int]:
    """Expand one port or an inclusive ``start-end`` range."""
    raw = str(value).strip()
    if "-" in raw:
        left, right = raw.split("-", 1)
    else:
        left = right = raw
    try:
        start, end = int(left), int(right)
    except ValueError as exc:
        raise ContainerError(f"{label} must be a port or port range") from exc
    if not (1 <= start <= end <= 65535):
        raise ContainerError(f"{label} must be within 1..65535")
    if end - start > 1000:
        raise ContainerError(f"{label} range may contain at most 1001 ports")
    return list(range(start, end + 1))


def _publish_ports(publish: list[dict] | None) -> dict[str, int]:
    """Translate manifest ``runtime.publish`` into docker SDK port bindings."""
    ports: dict[str, int] = {}
    occupied: set[tuple[int, str]] = set()
    for entry in publish or []:
        protocol = str(entry.get("protocol") or "tcp").lower()
        if protocol not in {"tcp", "udp"}:
            raise ContainerError("published port protocol must be tcp or udp")
        inside = _port_range(entry.get("container"), "container port")
        outside = _port_range(entry.get("host", entry.get("container")), "host port")
        if len(inside) != len(outside):
            raise ContainerError("container and host port ranges must have equal length")
        for container_port, host_port in zip(inside, outside):
            binding = (host_port, protocol)
            if binding in occupied:
                raise ContainerError(f"duplicate host port {host_port}/{protocol}")
            occupied.add(binding)
            ports[f"{container_port}/{protocol}"] = host_port
    return ports


class _Container:
    def __init__(self, app_id: str, image: str, port: int,
                 run_flags: list[str] | None, resources: dict | None,
                 env: dict | None, network: str | None,
                 volumes: dict[str, dict] | None = None,
                 container_name: str | None = None,
                 host_power: tuple[str, ...] | None = None,
                 publish: list[dict] | None = None) -> None:
        self.app_id = app_id
        self.image = image
        # 0 for a sidecar with nothing to expose — a database is dialled by
        # its siblings on the podman network, never reverse-proxied.
        self.port = int(port or 0)
        self.run_flags = list(run_flags or [])
        self.resources = dict(resources or {})
        self.env = dict(env or {})
        self.network = network
        self.volumes = dict(volumes or {})
        # Already resolved against the app's permissions AND this host's
        # opt-in by the caller (src.apps.runtime) — this is the granted set,
        # not the requested one, so nothing here re-decides policy.
        self.host_power = tuple(host_power or ())
        self.publish = list(publish or [])
        self._name = container_name
        self.container_id: str | None = None

    @property
    def name(self) -> str:
        # A sidecar passes its own name (``aw-app-crispal-db``); the app's
        # own container derives it, and its registry key has no ":" in it.
        return self._name or f"aw-app-{self.app_id}"


class ContainerSupervisor:
    """Runtime-owned registry + lifecycle for apps' Tier-2 containers."""

    def __init__(self, socket: str | None = None, network: str | None = None,
                 client: object | None = None) -> None:
        # AW_CONTAINER_SOCKET is set by the workspace bootstrap when the host
        # podman socket is mounted in; absent → Tier-2 unavailable.
        self._socket = socket if socket is not None else os.environ.get("AW_CONTAINER_SOCKET")
        # Attach app containers to the workspace's own podman network so they're
        # reachable by name (aardvark-dns) — same as postgres/redis. When unset,
        # fall back to publishing the port on the proxy host (127.0.0.1 default).
        self._network = network if network is not None else (os.environ.get("AW_CONTAINER_NETWORK") or None)
        self._proxy_host = os.environ.get("AW_CONTAINER_PROXY_HOST", "127.0.0.1")
        self._client = client  # injectable for tests; else built lazily
        self._containers: dict[str, _Container] = {}

    @property
    def available(self) -> bool:
        """True when a container engine socket is configured (or a client injected)."""
        return bool(self._socket) or self._client is not None

    def _docker(self):
        if self._client is None:
            if not self._socket:
                raise ContainerError(
                    "no container engine socket configured (set AW_CONTAINER_SOCKET)")
            import docker  # lazy — slim image only imports it for Tier-2 apps
            self._client = docker.DockerClient(base_url="unix://" + self._socket)
        return self._client

    def docker(self):
        """Return the underlying Docker-compatible client for read-only helpers."""
        return self._docker()

    def registered(self) -> list[tuple[str, _Container]]:
        """Registered Tier-2 app containers, in stable app-id order."""
        return sorted(self._containers.items(), key=lambda item: item[0])

    # ---- registry -------------------------------------------------------

    def register(self, app_id: str, image: str, port: int,
                 run_flags: list[str] | None = None, resources: dict | None = None,
                 env: dict | None = None, autostart: bool = False,
                 volumes: dict[str, dict] | None = None,
                 container_name: str | None = None,
                 host_power: tuple[str, ...] | None = None,
                 publish: list[dict] | None = None) -> None:
        if app_id in self._containers:
            raise ContainerError(f"container already registered for {app_id!r}")
        if not image:
            raise ContainerError(f"app {app_id!r} tier=container requires runtime.image")
        # A sidecar (registered as "<app>:<name>", see register_sidecar) may
        # legitimately expose nothing; the app's own container may not.
        if not port and ":" not in app_id:
            raise ContainerError(f"app {app_id!r} tier=container requires runtime.port")
        # Validate run flags up front so a bad manifest fails at register, not run.
        _parse_run_flags(run_flags)
        # Same for the grant names. The caller has already decided WHETHER this
        # app may have them; this only rejects a name that maps to no grant, so
        # a typo fails here instead of at start() — or worse, silently, if some
        # later refactor stops passing them to docker at all.
        host_power = hostpower.expand(host_power)
        _publish_ports(publish)
        c = _Container(app_id, image, port, run_flags, resources, env, self._network,
                       volumes, container_name, host_power, publish)
        self._containers[app_id] = c
        log.info("apps: registered container %s (image=%s port=%s network=%s)",
                 c.name, image, port, self._network)
        if c.host_power:
            # Loud on purpose: this is the one line in the log that says a
            # container on this machine is no longer fully contained.
            log.warning("apps: %s granted elevated host power: %s",
                        c.name, hostpower.describe(c.host_power))
        if autostart:
            self.start(app_id)

    @staticmethod
    def sidecar_key(app_id: str, name: str) -> str:
        """Registry key for a sidecar — ``"<app_id>:<name>"``.

        Namespaced so a sidecar can never collide with (or be mistaken for) an
        app's own registration, and so ``stop_all_for`` can find every
        companion of an app by prefix on uninstall.
        """
        return f"{app_id}:{name}"

    def register_sidecar(self, app_id: str, name: str, image: str,
                         port: int | None = None, run_flags: list[str] | None = None,
                         resources: dict | None = None, env: dict | None = None,
                         volumes: dict[str, dict] | None = None) -> str:
        """Register a companion container of ``app_id``. Returns its key.

        The container is named ``aw-app-<app_id>-<name>``, which is also the
        hostname siblings resolve it by on the shared podman network — so a
        WordPress sidecar reaches its database at ``aw-app-crispal-db``
        without anything having to discover an IP.
        """
        key = self.sidecar_key(app_id, name)
        self.register(key, image, port or 0, run_flags=run_flags, resources=resources,
                      env=env, volumes=volumes,
                      container_name=f"aw-app-{app_id}-{name}")
        return key

    def sidecar_keys(self, app_id: str) -> list[str]:
        """Registered sidecar keys of ``app_id``, in declaration-stable order."""
        prefix = f"{app_id}:"
        return sorted(k for k in self._containers if k.startswith(prefix))

    def set_volumes(self, app_id: str, volumes: dict[str, dict]) -> None:
        """Replace the bind set the NEXT ``start()`` will create the container with.

        Binds are fixed at container creation, so changing what an app can see
        (today: the user's mapped folders — see ``AppRuntime.remap_folders``)
        means recreating it. ``register()`` deliberately refuses to overwrite an
        existing registration, and re-registering would be the wrong verb
        anyway: nothing about the app's identity, image or ports is changing —
        only this one field.
        """
        self._require(app_id).volumes = volumes

    def update_env(self, app_id: str, env: dict[str, str]) -> bool:
        """Point a registered container at a new environment. True if changed.

        A container's environment is fixed at creation, so this only updates
        the registered spec — the caller restarts to make it take effect.
        Split that way because the caller already knows whether the app
        should be running at all (``auto_start``), and a config save must not
        silently start a container the user had stopped.
        """
        c = self._require(app_id)
        if c.env == env:
            return False
        c.env = dict(env)
        return True

    def start(self, app_id: str) -> dict:
        c = self._require(app_id)
        client = self._docker()
        from docker.errors import ImageNotFound, NotFound

        # Remove any stale container from a previous run so the name is free.
        try:
            stale = client.containers.get(c.name)
            stale.remove(force=True)
        except NotFound:
            pass

        # Always try to pull first — app images are tagged mutably (``:latest``
        # or a moving release tag), so a locally-cached image under that tag
        # can be stale even though it's "present" (observed: a fresh release
        # silently kept serving the previous build's assets because the old
        # tag already resolved locally and was never re-fetched). Only fall
        # back to whatever's cached when the registry itself is unreachable
        # (offline/BYOD-friendly), matching the catalog's own stale-is-better-
        # than-broken fallback (see ``src.apps.catalog.get_catalog``).
        try:
            log.info("apps: pulling image %s for %s", c.image, c.name)
            client.images.pull(c.image, auth_config=_registry_auth(c.image))
        except Exception:  # noqa: BLE001 — registry unreachable, fall back to cache
            try:
                client.images.get(c.image)
            except ImageNotFound:
                raise
            log.warning(
                "apps: pull failed for %s, starting %s from cached image", c.image, c.name
            )

        kwargs: dict = {
            "name": c.name,
            "detach": True,
            "environment": c.env,
            "volumes": c.volumes,
            # Default stays exactly as it was: no privilege, no devices, no
            # added capabilities. Only an app that cleared all three
            # host_power legs overrides this, below.
            "privileged": False,
            # Root cause of "it should come back up on its own but doesn't":
            # without a restart policy, podman/docker never restarts this
            # container on its own — only the NEXT aw-workspace process boot
            # (reconcile_on_boot -> this same start()) would bring it back.
            # A crash, OOM-kill, or the container engine itself restarting
            # independently of the aw-workspace process left it dead
            # indefinitely. "unless-stopped" survives all of those while still
            # respecting an explicit stop() (the app framework's own
            # auto_start=false / uninstall path calls stop(), which force-
            # removes the container outright — no conflict with the policy).
            "restart_policy": {"Name": "unless-stopped"},
        }
        kwargs.update(_parse_run_flags(c.run_flags))
        kwargs.update(_resource_kwargs(c.resources))
        kwargs.update(hostpower.docker_kwargs(c.host_power))
        published = _publish_ports(c.publish)
        if published:
            kwargs["ports"] = published
        # This workspace's own slug (AW_WORKSPACE, set by whatever launched
        # this process) — apps that need to namespace something by workspace
        # identity (e.g. aw-mcp-gateway prefixing its published tool names)
        # read it from here instead of each needing its own plumbing.
        # Unconditional (unlike AW_WORKSPACE_HOST below) since it's identity
        # metadata, not a network detail.
        workspace_slug = os.environ.get("AW_WORKSPACE", "")
        if workspace_slug:
            kwargs["environment"]["AW_WORKSPACE_SLUG"] = workspace_slug
        if c.network:
            kwargs["network"] = c.network
            # The container needs to call BACK into the workspace process itself
            # (e.g. aw-app-browser's Chrome tunneling through the in-process
            # aw-app-proxy CONNECT proxy on :9124) — 127.0.0.1 inside the app
            # container is its OWN loopback, never the workspace's. On the
            # shared podman network the workspace is reachable by its container
            # name via aardvark-dns (same way it already reaches postgres/redis
            # — see the rootless-podman-tier2 ADR), and podman sets a
            # container's hostname to its name by default, so gethostname()
            # gives that same resolvable name from inside the workspace.
            kwargs["environment"]["AW_WORKSPACE_HOST"] = socket.gethostname()
            # The reverse direction: an app that needs to publish its OWN
            # address for something else to dial back in (e.g. aw-mcp-gateway
            # writing its own entry into the host's .mcp.json, ADR "container
            # apps can register themselves") can't use 127.0.0.1 either — that
            # resolves inside its own netns, not from whatever process reads
            # the file. c.name (`aw-app-{app_id}`) is exactly what siblings on
            # this shared network already resolve it by (aardvark-dns), same
            # mechanism as AW_WORKSPACE_HOST above, just the other direction.
            kwargs["environment"]["AW_APP_SELF_HOST"] = c.name
        elif c.port and not published:
            # No shared network → publish the port so the proxy host can reach it.
            kwargs["ports"] = {f"{c.port}/tcp": c.port}

        container = client.containers.run(c.image, **kwargs)
        c.container_id = getattr(container, "id", None)
        log.info("apps: started container %s id=%s", c.name, c.container_id)
        return self.status(app_id)

    def stop(self, app_id: str) -> dict:
        c = self._require(app_id)
        client = self._docker()
        from docker.errors import NotFound
        try:
            obj = client.containers.get(c.name)
            obj.remove(force=True)
            log.info("apps: stopped+removed container %s", c.name)
        except NotFound:
            pass
        c.container_id = None
        return {"container": c.name, "running": False}

    def status(self, app_id: str) -> dict:
        c = self._require(app_id)
        client = self._docker()
        from docker.errors import NotFound
        running = False
        state: str | None = None
        try:
            obj = client.containers.get(c.name)
            reload_fn = getattr(obj, "reload", None)
            if callable(reload_fn):
                reload_fn()
            state = getattr(obj, "status", None)
            running = state == "running"
        except NotFound:
            pass
        return {
            "container": c.name,
            "running": running,
            "status": state,
            "image": c.image,
            "port": c.port,
            "url": self.base_url(app_id),
        }

    def base_url(self, app_id: str) -> str:
        """The URL the reverse-proxy targets to reach this app's container."""
        c = self._require(app_id)
        host = c.name if c.network else self._proxy_host
        return f"http://{host}:{c.port}"

    def stop_all_for(self, app_id: str) -> None:
        """Stop + remove + drop the app's container AND every sidecar of it.

        Sidecars go first: the app is the thing that talks to them, and
        leaving an orphaned database running after its app is gone is exactly
        the residue the uninstall replay exists to prevent.
        """
        for key in self.sidecar_keys(app_id):
            try:
                self.stop(key)
            except Exception:
                log.exception("apps: failed to stop sidecar %s on uninstall", key)
            self._containers.pop(key, None)
        if app_id not in self._containers:
            return
        try:
            self.stop(app_id)
        except Exception:
            log.exception("apps: failed to stop container for %s on uninstall", app_id)
        self._containers.pop(app_id, None)

    def forget_all_for(self, app_id: str) -> None:
        """Drop the app's + its sidecars' registrations WITHOUT stopping (W3).

        A Tier-2 container is external to every worker — podman owns it — so
        it must be stopped exactly once, by the worker doing the provisioning
        half. What every OTHER worker holds is this purely in-process registry
        (plus a lazily-built docker client), and letting go of it is the whole
        of its detach. Issuing N ``podman stop`` calls at one container would
        turn a clean uninstall into a race whose losers log "no such
        container". See src/apps/lifecycle.py.
        """
        for key in [*self.sidecar_keys(app_id), app_id]:
            self._containers.pop(key, None)

    def _require(self, app_id: str) -> _Container:
        c = self._containers.get(app_id)
        if c is None:
            raise ContainerError(f"no container registered for {app_id!r}")
        return c
