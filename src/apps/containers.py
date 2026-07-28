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
already reaches its postgres/redis siblings. No ``--privileged``, ever.

Registration is JOURNALED by the runtime so uninstall stops + removes the
container (reverse replay) — no orphan containers survive an uninstall.
"""
from __future__ import annotations

import logging
import os
import shlex

log = logging.getLogger(__name__)


class ContainerError(RuntimeError):
    pass


def _parse_run_flags(run_flags: list[str] | None) -> dict:
    """Map a manifest's ``run_flags_needed`` CLI flags to ``docker`` SDK kwargs.

    Only the small, safe subset apps actually need is understood; ``--privileged``
    is rejected outright (Tier-2 trust rule). An unknown flag raises rather than
    being silently dropped, so a manifest can't quietly ask for something the
    supervisor won't honor.
    """
    kwargs: dict = {}
    for flag in run_flags or []:
        name, _, value = flag.partition("=")
        if name in ("--privileged",):
            raise ContainerError("run flag --privileged is not allowed for Tier-2 apps")
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


class _Container:
    def __init__(self, app_id: str, image: str, port: int,
                 run_flags: list[str] | None, resources: dict | None,
                 env: dict | None, network: str | None) -> None:
        self.app_id = app_id
        self.image = image
        self.port = int(port)
        self.run_flags = list(run_flags or [])
        self.resources = dict(resources or {})
        self.env = dict(env or {})
        self.network = network
        self.container_id: str | None = None

    @property
    def name(self) -> str:
        return f"aw-app-{self.app_id}"


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

    # ---- registry -------------------------------------------------------

    def register(self, app_id: str, image: str, port: int,
                 run_flags: list[str] | None = None, resources: dict | None = None,
                 env: dict | None = None, autostart: bool = False) -> None:
        if app_id in self._containers:
            raise ContainerError(f"container already registered for {app_id!r}")
        if not image:
            raise ContainerError(f"app {app_id!r} tier=container requires runtime.image")
        if not port:
            raise ContainerError(f"app {app_id!r} tier=container requires runtime.port")
        # Validate run flags up front so a bad manifest fails at register, not run.
        _parse_run_flags(run_flags)
        c = _Container(app_id, image, port, run_flags, resources, env, self._network)
        self._containers[app_id] = c
        log.info("apps: registered container %s (image=%s port=%s network=%s)",
                 c.name, image, port, self._network)
        if autostart:
            self.start(app_id)

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

        # Pull the image if it isn't present locally.
        try:
            client.images.get(c.image)
        except ImageNotFound:
            log.info("apps: pulling image %s for %s", c.image, c.name)
            client.images.pull(c.image)

        kwargs: dict = {
            "name": c.name,
            "detach": True,
            "environment": c.env,
            # never --privileged; drop nothing extra but don't grant caps either
            "privileged": False,
        }
        kwargs.update(_parse_run_flags(c.run_flags))
        kwargs.update(_resource_kwargs(c.resources))
        if c.network:
            kwargs["network"] = c.network
        else:
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
        """Stop + remove + drop the app's container (uninstall reverse replay)."""
        if app_id not in self._containers:
            return
        try:
            self.stop(app_id)
        except Exception:
            log.exception("apps: failed to stop container for %s on uninstall", app_id)
        self._containers.pop(app_id, None)

    def _require(self, app_id: str) -> _Container:
        c = self._containers.get(app_id)
        if c is None:
            raise ContainerError(f"no container registered for {app_id!r}")
        return c
