"""aw-workspace-cli marketplace install <app> [--update] — install or update an app from
this workspace's marketplace catalog, via the same ``/api/apps/*`` routes the
Apps SPA uses (see ``src/apps/routes.py``)."""
from __future__ import annotations

import argparse
import os
import time

import httpx

from src.cli import local_client

COMMAND = "marketplace"
DESCRIPTION = "Install or update apps from the marketplace catalog"

_POLL_INTERVAL = 1.0

#: Tries for listing GETs and for the update POST (see ``_update_one``).
_RETRY_ATTEMPTS = 3
_RETRY_BACKOFF_S = 2.0

_SERVER_HINT = ("  check the server with: aw-workspace-cli marketplace info  /  "
                "aw-workspace-cli doctor")


def _out(msg: str = "") -> None:
    """``print`` that flushes. Block-buffered stdout (redirected to a file, or a
    pipe) left an ``update-all`` log empty for the whole run."""
    print(msg, flush=True)


def _get(path: str, timeout: float = 30.0, retry_status: bool = True):
    """Listing GET with retries; ``None`` (after printing one line) if the
    server never answered — never a traceback."""
    try:
        return local_client.request_with_retry(
            "GET", path, timeout=timeout,
            attempts=_RETRY_ATTEMPTS, backoff=_RETRY_BACKOFF_S,
            retry_status=retry_status)
    except httpx.HTTPError as exc:
        _out(f"error: the workspace server did not answer GET {path.split('?')[0]} "
             f"after {_RETRY_ATTEMPTS} tries ({local_client.describe_error(exc)})")
        _out(_SERVER_HINT)
        return None

#: Quanto esperar um install terminar, do lado do CLIENTE.
#:
#: Era 180s, mais curto que a espera do SERVIDOR, o que tornava o timeout
#: inevitável em vez de excepcional. ``CommandInstaller._run``
#: (``src/apps/commands.py``) segura um flock global em volta de TODO script
#: de instalador do workspace — apt/dpkg concorrentes corrompem — e espera
#: por ele até ``DEFAULT_TIMEOUT`` (600s) + ``INSTALLER_LOCK_MARGIN_S``
#: (120s). Isso é 720s por SCRIPT, e uma app como essentials roda uma dezena
#: deles. O cliente desistia antes do servidor sequer parar de esperar.
#:
#: Visto em 2026-10-09: ``marketplace update-all`` com 8 apps timed out em
#: todas as 8. Não é um valor que possa ficar menor que o orçamento do
#: servidor; se aquelas constantes subirem, esta precisa subir junto. Não são
#: importadas daqui de propósito — ``src.apps.commands`` puxa a stack do
#: FastAPI, e este CLI roda de um container irmão magro.
_POLL_TIMEOUT = float(os.environ.get("AW_MARKETPLACE_POLL_TIMEOUT_S", "1800"))

# Always force a fresh fetch of the merged catalog. The server caches it behind
# an in-memory TTL (``src/apps/catalog.py::get_catalog``), so a CLI install run
# right after a release can resolve the PREVIOUS version and still report
# "installed" — a silent no-op that reads as success. A refresh costs one fetch
# per configured source; a human typing this command wants the current catalog.
_CATALOG_PATH = "/api/apps/-/catalog?refresh=true"
# Refreshing hits every marketplace source over the network — the default 30s
# is tight when one of them is slow.
_CATALOG_TIMEOUT = 60.0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="aw-workspace-cli marketplace", description=DESCRIPTION,
    )
    sub = parser.add_subparsers(dest="action", required=True)

    install = sub.add_parser("install", help="Install (or update) an app from the catalog")
    install.add_argument("app", help="App id/slug as listed in the marketplace catalog")
    install.add_argument(
        "--update", action="store_true",
        help="Update an already-installed app to the catalog's current version",
    )

    sub.add_parser(
        "update-all",
        help="Update every installed app to the catalog's current version",
    )

    sub.add_parser("info", help="List installed apps and their versions")
    return parser


def run(args: list[str]) -> int:
    parser = _build_parser()
    ns = parser.parse_args(args)

    if ns.action == "install":
        return _install(ns.app, update=ns.update)
    if ns.action == "update-all":
        return _update_all()
    if ns.action == "info":
        return _info()

    parser.print_help()
    return 1


def _install(app_id: str, update: bool) -> int:
    apps = _catalog_apps()
    if apps is None:  # listing failed (already reported) — not a missing app
        return 1
    entry = next((a for a in apps if (a.get("id") or a.get("slug")) == app_id), None)
    if entry is None:
        _out(f"error: '{app_id}' is not in the marketplace catalog")
        return 1

    if update:
        return _update_one(app_id)

    # No `signed`: the server derives trust from marketplace-catalog membership
    # (src/apps/catalog.py::is_marketplace_app) and ignores what a client
    # claims. Do NOT "fix" a trust problem by adding it back here — the field
    # was honoured once, and this payload's silence produced UNSIGNED installs
    # of catalog apps, which cost them `ui:code` and with it every frontend
    # contribution they make.
    payload = {
        "app_id": app_id,
        "repo": entry.get("repo"),
        "ref": entry.get("ref") or "HEAD",
        "version": entry.get("version") or "",
    }
    # No retry: if the first POST landed, a repeat answers 409 "already
    # installed", which is indistinguishable from a real conflict.
    try:
        status, body = local_client.request("POST", "/api/apps/install", payload)
    except httpx.HTTPError as exc:
        return _post_unanswered(app_id, "install", exc)
    if status == 409:
        _out(f"'{app_id}' is already installed — use --update to update it.")
        return 1
    if status not in (200, 202):
        _out(f"error: install failed ({status}): {body}")
        return 1

    return 0 if _poll_until_done(app_id) else 1


def _post_unanswered(app_id: str, verb: str, exc: Exception) -> int:
    _out(f"error: the workspace server did not answer the {verb} request for "
         f"'{app_id}' ({local_client.describe_error(exc)}). It may be overloaded; "
         f"the {verb} may or may not have started.")
    _out(_SERVER_HINT)
    return 1


def _update_one(app_id: str) -> int:
    """Update a single installed app; returns a process-style exit code."""
    # Retry ONLY failures that prove the POST never reached the server
    # (connect errors). A ReadTimeout is ambiguous: the server may have
    # accepted it, and a retry can land on another ASGI worker whose job
    # tracker doesn't know about it, enqueuing a second update. A POST that
    # never landed must NOT fall through to polling — install-status could
    # still report the previous run's stale "installed".
    try:
        status, body = local_client.request_with_retry(
            "POST", f"/api/apps/{app_id}/update",
            attempts=_RETRY_ATTEMPTS, backoff=_RETRY_BACKOFF_S,
            retry_on=(httpx.ConnectError, httpx.ConnectTimeout))
    except httpx.HTTPError as exc:
        return _post_unanswered(app_id, "update", exc)
    if status == 404:
        _out(f"'{app_id}' is not installed — run 'aw-workspace-cli marketplace install {app_id}' first.")
        return 1
    if status not in (200, 202):
        _out(f"error: update failed ({status}): {body}")
        return 1
    return 0 if _poll_until_done(app_id) else 1


def _installed_apps() -> list[dict] | None:
    """Every installed app's ``/api/apps`` row (slug + running version), or
    ``None`` if the listing call failed."""
    res = _get("/api/apps")
    if res is None:
        return None
    status, body = res
    if status != 200 or not isinstance(body, list):
        _out(f"error: could not list installed apps ({status}): {body}")
        return None
    return [a for a in body if isinstance(a, dict) and a.get("slug")]


def _catalog_apps() -> list[dict] | None:
    """The full marketplace catalog listing, or ``None`` on failure.

    Always fetched with ``refresh=true`` — see ``_CATALOG_PATH``."""
    res = _get(_CATALOG_PATH, timeout=_CATALOG_TIMEOUT, retry_status=False)
    if res is None:
        return None
    status, body = res
    if status != 200:
        _out(f"error: could not load the marketplace catalog ({status}): {body}")
        return None
    if not isinstance(body, dict):
        return []
    apps = body.get("apps") or []
    error = body.get("error")
    if error:
        if not apps:
            # Every catalog source failed and both caches are empty: the server
            # still answers 200, but "no apps" here is a failed lookup, not an
            # empty marketplace.
            _out(f"error: could not load the marketplace catalog: {error}")
            return None
        _out(f"warning: marketplace catalog served from a stale cache: {error}")
    return apps


def _required_dependency_ids(entry: dict) -> list[str]:
    """Required ``dependencies.apps`` ids from a catalog entry (mirrors
    ``Reconciler._required_app_dependencies`` — optional/not-required
    entries don't gate update order)."""
    deps = (entry.get("dependencies") or {}).get("apps") or []
    ids = []
    for raw in deps:
        if isinstance(raw, str):
            dep_id, required = raw.strip(), True
        elif isinstance(raw, dict):
            dep_id = str(raw.get("id") or "").strip()
            required = raw.get("required", True) is not False and raw.get("optional") is not True
        else:
            continue
        if dep_id and required:
            ids.append(dep_id)
    return ids


def _dependency_order(outdated: dict[str, dict]) -> list[str]:
    """Order ``outdated`` (slug -> catalog entry) so every app comes after
    its dependencies, via Kahn's algorithm: repeatedly take the apps with no
    unresolved dependency left in the set, then remove them and repeat. A
    dependency that isn't itself outdated doesn't gate order here — it's
    already at its current version, and the server installs it on demand if
    it's ever missing (``Reconciler._install_dependencies``)."""
    remaining = {
        slug: [d for d in _required_dependency_ids(entry) if d in outdated]
        for slug, entry in outdated.items()
    }
    ordered: list[str] = []
    placed: set[str] = set()
    while remaining:
        ready = sorted(slug for slug, deps in remaining.items() if all(d in placed for d in deps))
        if not ready:
            cycle = ", ".join(sorted(remaining))
            _out(f"warning: cyclic dependency among outdated apps ({cycle}) — updating anyway")
            ready = sorted(remaining)
        for slug in ready:
            ordered.append(slug)
            placed.add(slug)
            del remaining[slug]
    return ordered


def _update_all() -> int:
    installed = _installed_apps()
    if installed is None:
        return 1
    if not installed:
        _out("no apps installed — nothing to update")
        return 0

    catalog = _catalog_apps()
    if catalog is None:
        return 1
    catalog_by_id = {
        (a.get("id") or a.get("slug")): a for a in catalog if isinstance(a, dict)
    }

    outdated: dict[str, dict] = {}
    for app in installed:
        slug = app["slug"]
        entry = catalog_by_id.get(slug)
        if entry is None:
            continue  # not in any configured marketplace source — nothing to compare against
        catalog_version = entry.get("version") or ""
        if catalog_version and catalog_version != app.get("version"):
            outdated[slug] = entry

    if not outdated:
        _out(f"all {len(installed)} installed app(s) already up to date")
        return 0

    order = _dependency_order(outdated)
    _out(f"updating {len(order)} outdated app(s) in dependency order: {', '.join(order)}")

    # Sequencial E com parada na primeira que não concluir. Os installers de
    # TODAS as apps serializam num único flock global
    # (``CommandInstaller._run``), então disparar a próxima enquanto a
    # anterior ainda roda não paraleliza nada: só empilha na fila, deixa cada
    # uma mais lenta e garante o timeout da seguinte. Foi exatamente a
    # cascata vista em 2026-10-09, com as 8 apps "falhando" em sequência por
    # causa da primeira. A ordem é de dependência, então seguir em frente
    # depois de uma incompleta é pior ainda — instala o dependente antes da
    # dependência.
    done = 0
    stopped_at: str | None = None
    for i, slug in enumerate(order):
        if _update_one(slug) == 0:
            done += 1
            continue
        stopped_at = slug
        remaining = order[i + 1:]
        if remaining:
            _out(f"parando aqui — {len(remaining)} app(s) não tentada(s): "
                  f"{', '.join(remaining)}")
            _out("  os installers de todas as apps compartilham um lock, "
                  "então enfileirar as próximas agora só deixaria tudo mais "
                  "lento. Rode de novo quando esta terminar.")
        break

    if stopped_at is not None:
        _out(f"done — {done} de {len(order)} ok, parou em: {stopped_at}")
        return 1
    _out(f"done — all {len(order)} app(s) up to date")
    return 0


def _info() -> int:
    installed = _installed_apps()
    if installed is None:
        return 1
    if not installed:
        _out("no apps installed")
        return 0

    for app in sorted(installed, key=lambda a: a["slug"]):
        _out(f"{app['slug']} {app.get('version') or 'unknown'}")
    return 0


def _poll_until_done(app_id: str) -> bool:
    _out(f"{app_id}: installing…")
    deadline = time.monotonic() + _POLL_TIMEOUT
    flaky = False
    while time.monotonic() < deadline:
        try:
            status, body = local_client.request("GET", f"/api/apps/{app_id}/install-status")
        except httpx.HTTPError as exc:
            # Transient: the job lives on the server and keeps running while a
            # loaded server is slow to answer. Keep waiting until the deadline.
            if not flaky:
                _out(f"{app_id}: server slow to answer "
                     f"({local_client.describe_error(exc)}) — still waiting")
                flaky = True
            time.sleep(_POLL_INTERVAL)
            continue
        flaky = False
        if status == 200 and isinstance(body, dict):
            state = body.get("status")
            if state == "no-op":
                _out(f"{app_id}: already up to date")
                return True
            if state == "installed":
                _out(f"{app_id}: installed")
                return True
            if state == "failed":
                _out(f"{app_id}: failed — {body.get('error')}")
                return False
        time.sleep(_POLL_INTERVAL)
    # NÃO é "falhou". O job vive no servidor e segue rodando depois que
    # paramos de olhar — dizer o contrário fez um update-all inteiro parecer
    # oito falhas quando na verdade era uma fila.
    _out(f"{app_id}: ainda instalando depois de {_POLL_TIMEOUT:.0f}s — o job "
          f"continua no servidor, parei só de acompanhar.")
    _out(f"  acompanhe com: aw-workspace-cli marketplace info")
    return False
