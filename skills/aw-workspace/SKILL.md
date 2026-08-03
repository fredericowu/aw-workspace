---
name: aw-workspace
description: >-
  The aw-workspace-cli CLI shipped inside repos/aw-workspace itself
  (./aw-workspace-cli at the repo root, on PATH via the repo root itself) —
  auto-discovered commands under src/cli/commands/, the marketplace
  install/update command, the update workspace|remote-host command, and the
  local-CLI auth mechanism that lets it call this workspace's own
  identity-gated API. Use whenever you're asked to add a new aw-workspace-cli
  command, or to install/update an app or trigger a workspace/remote-host
  update from a terminal inside the workspace. Note: the root-level `./aw` in
  this repo is a DEPRECATED stub that only prints a pointer here — it is not
  the monolith's `./aw` and does not run any of these commands.
---

# aw-workspace's own CLI

`repos/aw-workspace` ships a small CLI called **`aw-workspace-cli`**
(`./aw-workspace-cli` at the repo root; the repo root itself is on `PATH`
inside the image, so the bare form works too) — separate from, and much
smaller than, the monolith's `./aw` (`skills/aw/SKILL.md`). Named
`aw-workspace-cli` on purpose, not `aw`: this repo's root also contains a
monolith-style `./aw` file, and an LLM/agent with both on PATH would
otherwise conflate two very different tools. It has no service lifecycle
(the container runtime owns that) and no bootstrap venv (the image ships its
Python deps baked in via `requirements.txt`). It exists to script actions a
human would otherwise only be able to trigger from aw-console: installing/
updating apps, and updating the workspace or its remote host.

```bash
aw-workspace-cli help
aw-workspace-cli marketplace install <app>
aw-workspace-cli marketplace install <app> --update
aw-workspace-cli update workspace
aw-workspace-cli update remote-host --token <central-identity-jwt>
```

`aw-workspace-cli` works from any cwd/shell inside the container — no `./`
prefix, no `cd` into the repo first.

## `./aw` is deprecated — do not use it

The repo root also has a file named `aw` (a leftover of the pre-rename
design). Running it does nothing except print a deprecation notice pointing
here. **Never treat `./aw` in this repo as the monolith's `./aw`** — they
share a filename by historical accident only; this repo's real CLI is
`aw-workspace-cli`.

## Layout

```
aw                              # deprecated stub — prints a pointer to this skill, does nothing else
aw-workspace-cli                # the real launcher, on PATH (Dockerfile puts the repo root on PATH)
src/cli/
  local_client.py               # HTTP client for THIS workspace's own API
  commands/
    help.py
    marketplace.py               # aw-workspace-cli marketplace install <app> [--update]
    update.py                    # aw-workspace-cli update <workspace|remote-host>
```

`aw-workspace-cli` auto-discovers every module in `src/cli/commands/` via
`pkgutil.iter_modules` — same pattern as the monolith's `src/commands/`.
Adding a command is: drop `src/cli/commands/<name>.py` with

```python
COMMAND = "mycommand"          # what the user types after aw-workspace-cli
DESCRIPTION = "One-line help"  # shown by aw-workspace-cli help
def run(args: list[str]) -> int: ...
```

No registration step — it becomes runnable immediately (as long as
`aw-workspace-cli` is on `PATH`, which it is inside the built image; run it
as `./aw-workspace-cli <cmd>` in a bare checkout without the image's
`PATH`).

## `aw-workspace-cli marketplace install <app> [--update]`

Talks to **this workspace's own** `/api/apps/*` routes (`src/apps/routes.py`)
via `src/cli/local_client.py` — the same endpoints the Apps SPA calls
(`GET /api/apps/-/catalog`, `POST /api/apps/install`,
`POST /api/apps/{slug}/update`, `GET /api/apps/{slug}/install-status`).

1. Fetches the marketplace catalog, looks up `<app>` by `id`/`slug`.
2. Without `--update`: `POST /api/apps/install` with `{app_id, repo, ref,
   version}` from the catalog entry. 409 if already installed (says so —
   use `--update` instead).
3. With `--update`: `POST /api/apps/{slug}/update` (404 if not installed
   yet — install it first).
4. Either way, polls `GET /api/apps/{slug}/install-status` (1s interval, 180s
   timeout) until `installed` / `no-op` / `failed`, printing the outcome.

Install/update is async server-side (the fetch + system-CLI `apt install`
step can take 30-60s) — the CLI's polling loop is what turns that into a
synchronous-feeling command.

### Auth — the local-CLI token

The SPA authenticates with a browser-issued `aw_id_jwt`
(`src/api/identity.py`'s `require_identity`), which a terminal CLI has no
way to hold. `require_identity` also accepts a **local-CLI secret**:

- Generated on first use at `<workspace_home>/cli-token` (`~/.aw-workspace/
  cli-token` by default, mode `0600`) — `src/apps/paths.py`'s
  `get_or_create_cli_token()`.
- Sent as the `X-AW-Local-Cli-Token` header (`src/apps/paths.LOCAL_CLI_HEADER`).
- `require_identity` checks this header **before** falling back to the real
  JWT check — a match returns `{"sub": "local-cli", "local_cli": True}`.

This only proves "same machine/filesystem as the server", not "is a real
user" — anyone who can read a 0600 file inside the container already has
that level of access. It is intentionally **not** wired into `update.py`
(below), which needs real per-user, cross-workspace auth.

If you add a new `aw-workspace-cli` command that needs to call this
workspace's own API, reuse `src/cli/local_client.py`'s `request(method, path,
json_body=None)` — it already attaches the token header.

## `aw-workspace-cli update <workspace|remote-host>`

Calls **aw-backend** (the cloud control plane), not this workspace:

- `aw-workspace-cli update workspace` → `POST {AW_BACKEND_URL}/api/workspaces/{AW_WORKSPACE}/update`
- `aw-workspace-cli update remote-host` → `POST {AW_BACKEND_URL}/api/workspaces/{AW_WORKSPACE}/remote-host/update`

These are the **exact same endpoints** aw-console's Workspace → Manage →
Update button calls (`repos/aw-backend/src/api/routes/workspaces.py`). They
require a real central-identity JWT via `require_identity` there
(`aw-backend/src/api/identity_guard.py`) — a console session has one from
the browser cookie; a terminal doesn't, so it must be handed one explicitly:

```bash
aw-workspace-cli update workspace --token <aw_id_jwt value>
# or
export AW_ID_TOKEN=<aw_id_jwt value>
aw-workspace-cli update workspace
```

`AW_BACKEND_URL` and `AW_WORKSPACE` come from the same env this workspace
already uses to reach aw-backend (see `.env.example`,
`src/apps/registry_client.py`) — no new config needed beyond the token.

**Do not** reuse the local-CLI token for this command. It only proves local
filesystem access to one workspace container; aw-backend's per-user,
per-workspace-role checks (`Identity.require_role`) need a real identity.

## PATH

The Dockerfile prepends `/opt/aw-workspace` (the repo root itself, where
`aw-workspace-cli` lives) onto `PATH`, alongside the existing
`/opt/aw-workspace/.aw-workspace/bin` (F4 app-shim dir — a different,
unrelated bin dir for installed apps' own shims). Both are baked in as
image-level `ENV PATH`, so every process/shell/agent session in the
container sees `aw-workspace-cli` without extra setup — no `bin/` dir or
symlink needed. (This also puts the deprecated `./aw` stub on PATH as a
bare `aw`; harmless, since it only prints a pointer to this skill.)

## Testing without a live server

`identity.py`'s local-CLI bypass and `src/apps/catalog.py`'s `get_catalog()`
can both be exercised without a running Postgres — `get_catalog()` is a
filesystem/HTTP cache, and the auth dependency is pure. A `FastAPI` +
`TestClient` app with just `require_identity` wired onto a stub route is
enough to check the token header logic when iterating on this.
