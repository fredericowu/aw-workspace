---
name: aw-workspace
description: >-
  The aw-workspace-cli CLI shipped inside repos/aw-workspace itself
  (./aw-workspace-cli at the repo root, on PATH via the repo root itself) —
  auto-discovered commands under src/cli/commands/, the marketplace
  install/update command, the update workspace|remote-host command, and the
  workspace API key auth mechanism that lets it call this workspace's own
  identity-gated API. Use whenever you're asked to add a new aw-workspace-cli
  command, or to install/update an app or trigger a workspace/remote-host
  update from a terminal inside the workspace. Note: the root-level `./aw` in
  this repo is a DEPRECATED stub that only prints a pointer here — it is not
  the monolith's `./aw` and does not run any of these commands.
last_updated: 2026-08-17
last_update_note: >-
  Corrected the "Auth" section — it described the old file-based
  X-AW-Local-Cli-Token mechanism, which was removed 2026-08-08 and replaced
  by the AW_WORKSPACE_API_KEY / X-Api-Key shared secret
  (src/api/workspace_api_key.py). Found by aw-autoskill scanning 4 sessions
  that hand-rolled curl+X-Api-Key boilerplate instead of using
  local_client.request().
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
aw-workspace-cli status                       # health + components + mapped folders
aw-workspace-cli apps [<slug>] [--json]
aw-workspace-cli start|stop|restart <app>     # a component, by bare app slug
aw-workspace-cli logs <app> [-f]
aw-workspace-cli folders list|add|rm|browse   # map ANY folder — see below
aw-workspace-cli test [pytest args...]
aw-workspace-cli marketplace install <app> [--update]
aw-workspace-cli update workspace
aw-workspace-cli update remote-host --token <central-identity-jwt>
```

Tier-2 manifests may expose non-HTTP listeners with `runtime.publish` entries
(`container`, optional `host`, and `protocol` `tcp|udp`). Port ranges such as
`10000-10100` are expanded one-to-one and limited to 1001 ports. These bindings
are additional to `runtime.port`, which remains the authenticated HTTP reverse-
proxy target. Publishing requires `tier=container` and `containers:manage`.

Most of these are ports of the monolith's `./aw` verbs (`status`, `start`,
`stop`, `restart`, `logs`, `test`) re-pointed at this workspace's own API, so
muscle memory from `agentic-workspace` carries over. Lifecycle commands take a
bare app slug (`kb`), not the legacy `docker:aw-kb` component key.

### Mapped folders

`folders` is the no-repository-binding way to make a directory available to
apps — the successor to the monolith's `knowledge_base.map_paths` in
`aw.json`:

```bash
aw-workspace-cli folders add /opt/aw-workspace/docs        # name defaults to "docs"
aw-workspace-cli folders add /srv/datasets --name data --mode rw
aw-workspace-cli folders browse /opt/aw-workspace          # find a path to map
aw-workspace-cli folders list
aw-workspace-cli folders rm docs
```

The folder does **not** have to be a git checkout, and does not have to live
under `repos/`. Any app whose manifest declares a `$AW_WORKSPACE_FOLDERS`
volume gets every mapped folder bind-mounted at `<target>/<name>`, and adding
or removing one re-mounts it into those already-running containers (see
`src/api/folders.py` and `AppRuntime.remap_folders`).

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
    apps.py                      # aw-workspace-cli apps
    folders.py                   # aw-workspace-cli folders list|add|rm|browse
    logs.py                      # aw-workspace-cli logs <component> [-f]
    marketplace.py               # aw-workspace-cli marketplace install <app> [--update]
    start.py / stop.py / restart.py   # thin wrappers over src/cli/lifecycle.py
    status.py                    # aw-workspace-cli status
    test.py                      # aw-workspace-cli test
    update.py                    # aw-workspace-cli update <workspace|remote-host>
  lifecycle.py                   # shared component resolution for start/stop/restart/logs
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

### Auth — the workspace API key

*Updated 2026-08-17: the old file-based `X-AW-Local-Cli-Token` /
`get_or_create_cli_token()` mechanism this section used to describe was
removed (deployed 2026-08-08) — nothing in `src/` references it anymore.
Verified against `src/api/workspace_api_key.py` and `src/cli/local_client.py`
directly, not from memory.*

The SPA authenticates with a browser-issued `aw_id_jwt`
(`src/api/identity.py`'s `require_identity`), which a terminal CLI (or a
sibling process like an external MCP server) has no way to hold. Instead
there's a single **workspace-wide API key**:

- Minted on first use (`src/api/workspace_api_key.py`'s
  `get_or_create_workspace_api_key()`), stored in the `settings` KV table
  (survives container recreation as long as Postgres does), and mirrored to
  `<AW_WORKSPACE_HOME>/.env` as `AW_WORKSPACE_API_KEY` so any process with no
  DB access — `aw-workspace-cli`, an agent-runner container, an external
  MCP — can read it straight from the file.
- Sent as the `X-Api-Key` header (`src/api/workspace_api_key.HEADER_NAME`).
- `require_identity` checks this header **before** falling back to the real
  JWT check (`_workspace_api_key_authorized`, checked via constant-time
  compare) — a match authenticates the request without a browser session.
- This is the same key an agent-runner container uses to reach the workspace
  server through the tunnel edge's `X-Api-Key` carve-out when loopback isn't
  reachable — see `runner-workspace-reachability` in auto-memory.

This proves "holds the shared workspace secret", not "is a specific real
user" — anyone who can read `.env` inside the container already has that
level of access. It is intentionally **not** wired into `update.py` (below),
which needs real per-user, cross-workspace auth.

If you add a new `aw-workspace-cli` command — or any ad hoc script — that
needs to call this workspace's own API, reuse `src/cli/local_client.py`'s
`request(method, path, json_body=None)`; it already resolves the right base
URL (loopback vs. the tunnel URL) and attaches the `X-Api-Key` header. Don't
hand-roll a `curl -H "X-Api-Key: ..."` by grepping `.env` yourself — that
duplicates exactly what `local_client.request()` already does correctly,
including the loopback/tunnel fallback.

## `aw-workspace-cli restart core` vs. `update workspace`

Two different problems that got conflated in a 2026-09-04 incident (card
`3d15bf3b-9510-816a-bff8-fc6698619fa4`), and now have two different verbs:

- **"I pushed a fix to this repo's core code, make it live."**
  `/opt/aw-workspace` is a host bind mount — the moment a commit lands on
  the linked BYOD host (`git push`, since the host and the container share
  this tree), the code is already there. Only the **process** is stale.
  That's `aw-workspace-cli restart core` — no identity token, agent
  -triggerable from a sibling runner container.
- **"I want a new container image."** That's
  `aw-workspace-cli update workspace` below — it pulls `:latest` and syncs
  the image's baked repo copy OVER the host source tree, rewriting it. Human
  -only, JWT-gated, and NOT what you want after an ordinary code push (if
  the image hasn't been rebuilt from the commit you're chasing, this can
  silently overwrite newer files with older ones).

### `restart core`

```bash
aw-workspace-cli restart core            # dispatch, don't wait
aw-workspace-cli restart core --wait     # dispatch and poll /api/health
```

Mechanism (`src/cli/core_restart.py`), dispatched from **outside** the
workspace container over the aw-remote-host link this workspace already
has (same link `remote-hosts exec` uses) — the process serving the restart
can't usefully wait on its own response, since it's the process about to
die:

1. Resolve `AW_BACKEND_URL` / `AW_WORKSPACE` / `AW_WORKSPACE_HOST_TOKEN`
   (env first, then `<AW_WORKSPACE_HOME>/.env` — same fallback
   `aw-app-remote-host-cli`'s client uses, so this also works from a
   sibling agent-runner container, not just from inside the workspace).
2. Capture `expected_head` (`git rev-parse HEAD` of this checkout) and
   `boot_id_before` (current `/api/health`).
3. Pre-flight: confirm the target container
   (`aw-remote-host-workspace`, `CONTAINER_NAME_ENV` override
   `AW_REMOTE_HOST_WORKSPACE_CONTAINER`) actually exists on the linked
   host — refuses to restart a container it can't confirm by name — and
   best-effort captures the currently-pulled `:latest` image digest for
   the receipt (a restart on this host has been observed to come back as a
   **recreate** from `:latest`, silently activating a pending image-baked
   env change like `AW_WORKSPACE_WORKERS`; this doesn't prevent that, only
   makes it attributable).
4. Write a receipt to `.tmp/core-restart/<request_id>.json`.
5. Dispatch the restart **async** (`exec_start`, never `exec_run`/
   `exec_wait` for the mutating step — those are documented-flaky for a
   job that ran fine, and the job kills the very channel a wait would sit
   on). The host-side command is `[ -e <sentinel> ] && exit 0; touch
   <sentinel>; { podman restart <container>; echo EXIT=$?; } >>
   <log> 2>&1` — idempotent, because exec has been proven to execute the
   same command TWICE during a link reconnect, and a double
   `podman restart` would kill the freshly-booted process seconds after
   boot. Sentinel/log live at a plain `/tmp` host path, never under
   `/opt/aw-workspace` — that path is a bind mount of the HOST's own dir,
   invisible to a script running ON the host.

**`--wait` poll contract**: poll `/api/health` (below) until `boot_id`
changes AND `git_head == expected_head`, to a ~180s deadline. Three
distinguishable outcomes, three exit codes — never collapse them:

| outcome | exit | meaning |
|---|---|---|
| `boot_id` unchanged at deadline | 1 | the restart never happened |
| `boot_id` changed, `git_head` mismatches | 2 | came back on the WRONG code |
| `boot_id` changed, `git_head` matches | 0 | success |

The poller is disposable — all durable state is the receipt plus
`/api/health` itself, so killing `--wait` loses nothing but the exit code.

This grants **no new privilege**: anything that can read
`<AW_WORKSPACE_HOME>/.env` can already run arbitrary shell on the linked
host via `remote-hosts exec`. This only packages that into one idempotent,
observable verb instead of a hand-rolled one-liner.

### `/api/health`'s boot identity

`src/api/boot_info.py` — `boot_id` (random uuid4), `git_head` (`git
rev-parse HEAD` of the tree the process started from), `started_at` (epoch
seconds), alongside the existing `status`/`workspace`/`version`. Minted
**once**, in the parent process, before `uvicorn.run(workers=N)` in
`src/start/workspace.py` — `AW_WORKSPACE_WORKERS=10` is live on this
deployment, and if each worker minted its own `boot_id` a poller could
never converge on one value. `git_head` in particular is captured at
process **start**, never read live per request — reading it live would
report the current bind-mounted worktree even on a stale process, exactly
the lie this field exists to catch.

This is now a public contract other services poll (`restart core --wait`
above; aw-console and aw-backend's `_wait_for_workspace_version` also read
`/api/health`) — add fields here, never repurpose or remove
`status`/`workspace`/`version`/`boot_id`/`git_head`/`started_at`.

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

`AW_BACKEND_URL` and `AW_WORKSPACE` resolve through the same env-then-`.env`
fallback `restart core` uses (`src/cli/core_restart._env`) — no new config
needed beyond the token, and this command now fails on the actual gate (the
missing JWT) instead of on env vars that were readable all along. The JWT
itself is deliberately NOT read from `.env` — nothing ever publishes it
there, and it must stay something only a human hands the CLI explicitly.
Widening this route to also accept the workspace's own host token was
considered and **rejected** in the `restart core` design (see the card
above): cross-repo, aw-backend deploys are manual, and it would widen a
data-plane credential onto control-plane lifecycle routes that also cover
uninstall/reinstall — the host token stays scoped to `remote-host/exec`.

**Do not** reuse the workspace API key for this command. It only proves
possession of one workspace's shared secret; aw-backend's per-user,
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

`identity.py`'s `X-Api-Key` bypass and `src/apps/catalog.py`'s `get_catalog()`
can both be exercised without a running Postgres — `get_catalog()` is a
filesystem/HTTP cache, and the auth dependency is pure. A `FastAPI` +
`TestClient` app with just `require_identity` wired onto a stub route is
enough to check the header logic when iterating on this.
