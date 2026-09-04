# aw-workspace strangler-fig migration playbook

`aw-workspace` is the **BYOD data-plane** of the [three-plane split](https://api.aw.tekflox.com)
(ADR: `docs/knowledge_base/docs/architecture/aw-three-plane-split-and-workspace-urls.md`).
It starts as a thin skeleton and **grows by porting machine-specific routes out
of the `agentic-workspace` monolith one at a time** (strangler-fig), keeping the
API/WS contract identical so the cloud SPA (`repos/aw-frontend`) never changes.

This doc is the repeatable recipe. Migration #1 (Terminal) is the worked
example; follow the same steps for tasks, agents, files, ….

## Route → plane map

Which plane owns which route family. Update this table with every migration.

| Route family | Owner plane | Notes |
|---|---|---|
| `/api/health`, `/api/auth/status`, `/api/settings/*` | **aw-workspace** | skeleton (F2/M1) |
| `/api/identity/*`, JWKS, provisioning, tunnel control | aw-backend (cloud) | control-plane, never on BYOD |
| **`/api/terminals*`, `/ws/terminal/*`** | **aw-workspace** | migration #1 — PTY shells run on the BYOD host |
| `/api/apps/code-agent-clis/agent-sessions*` | **app:code-agent-clis** | moved off the `/api/v2/agent-sessions*` core stub 2026-08-03 — this app installs the CLIs, so it's the one that can discover their on-disk sessions; also owns the "Agents" nav menu (`core.nav` slot) |
| `/ws/status` | **aw-workspace** | slim subset — terminal list push only, for now |
| **`/api/notify*`, `/ws/notifications`** | **aw-workspace** | notification engine — also reachable from Tier-1 apps via `ctx.notify` (`notifications:send`) |
| **`/api/folders*`** | **aw-workspace** | mapped folders — the monolith's `knowledge_base.map_paths` (`aw.json`) generalised into workspace state: point at ANY directory, no git repo/`repos/` prefix; apps declaring the `$AW_WORKSPACE_FOLDERS` volume get one bind per folder |
| `/api/tasks*`, `/api/plans*`, agent execution, `/api/files*` | monolith (not yet migrated) | future strangler steps |

The rest of the dashboard's routes are still served by the monolith on the
single-tenant host; they get migrated as the BYOD product needs them.

## The recipe (per route family)

1. **Copy the route module** from the monolith (`src/api/routes/<feature>.py`
   and any manager/helper it needs) into `repos/aw-workspace/src/api/`.

2. **Wire minimal deps — port only what the routes actually use.** The monolith
   modules are entangled with subsystems the slim BYOD image doesn't have
   (a global `StateManager`, GNU `screen`, the v2 session DB tables, agent-CLI
   detection, prompt detection, `codex_mcp_env`, …). Cut them. For terminal:
   dropped `screen`-backing and the Claude `PromptDetector`, so a terminal is
   just a shell (or an arbitrary command) — kept the PTY mechanics (fork/exec,
   non-blocking fan-out reader, resize, chunked write, scrollback) verbatim so
   the byte contract is unchanged. Agent-session history (the
   `screen_sessions`/`agent_sessions`/`window_sessions` tables + claude/codex/
   copilot/cursor `--resume` session-id detection) was initially dropped
   entirely for the same "no agent CLIs on the slim image" reason, then
   reinstated 2026-08-03 as **`aw-app-code-agent-clis`**'s own on-disk
   discovery + `ctx.db` soft-delete overlay (not a DB-tables port — see that
   app's `sessions.py`), once that app started actually installing the CLIs.

3. **Add the identity gate.** Guard every REST route with
   `Depends(require_identity)` (offline EdDSA/JWKS verification, mirroring
   `/api/auth/status`). WebSockets can't carry custom headers, so gate them with
   `authorize_ws(websocket)` — it reads the token from the `?token=` query param
   **or** the apex `aw_id_jwt` cookie (the browser *does* send the apex cookie on
   the WS handshake to `api.<ws>.workspace`, verified in tests). Reject with
   **accept()-then-close(code)** rather than close-before-accept (the latter is
   not surfaced as a disconnect by some clients incl. Starlette's TestClient).

4. **Preserve the API/WS contract EXACTLY** so the SPA works unchanged via its
   `apiBase`/`wsUrl` shim. Match paths, methods, request-body keys, response
   field names, and WS framing (for terminal: inbound binary = keystrokes,
   inbound text `{"type":"resize",...}` = control, all server→client frames are
   raw output bytes). If a monolith feature can't be honored yet, keep the route
   present and return a safe empty/no-op shape so the SPA never 404s — or, once
   an app can genuinely own the feature (like `aw-app-code-agent-clis` now does
   for agent-sessions), retire the core stub and point the SPA at the app's own
   route instead of leaving a permanent empty placeholder in core.

5. **Mind stateful routes + worker count.** In-memory session state (PTY fds,
   subscriber queues) only lives in ONE uvicorn worker, so create-on-worker-A /
   WS-on-worker-B would miss. That is no longer true of terminals: **W7 relays
   them over two Redis Streams per session** (`src/api/terminal_manager.py`) —
   an output stream the owner XADDs and every worker XREADs, and an input
   stream any worker XADDs and only the owner consumes. The PTY is still forked
   by, and owned by, exactly one worker, because a master fd cannot cross a
   process boundary; what crosses is the bytes. W7 replaced W5's GNU `screen`
   backing, and `screen` is no longer installed in the image.
   `AW_WORKSPACE_WORKERS` ships as **10** (Dockerfile/compose) as of W6 — every
   phase W1-W5 shipped at workers=1 with behaviour identical to today, which is
   what made the flip a two-literal change instead of a revert-under-pressure.
   If you add a new stateful route, add its shared backing store before relying
   on the count staying at 10 — and check the fallbacks, because this module
   degrades to a worker-owned PTY wherever Redis is unreachable rather than
   failing loudly.

   **Changing the Dockerfile `ENV` alone changes nothing on a running
   container** — it's only resolved once, when a container is created from
   the image, so a plain process restart keeps whatever count that container
   was created with (this bit the team on 2026-09-04: the value flip-flopped
   in git four times with no one seeing an effect, then an unrelated host
   recreate "won" 10 by surprise). To actually change the worker count of a
   container that already exists, without rebuilding/recreating it, set
   `AW_WORKSPACE_WORKERS=` in `<AW_WORKSPACE_HOME>/.env` and restart the
   process — `src/start/workspace.py`'s `main()` checks that file (via
   `src.apps.paths.read_workspace_env`) BEFORE the baked `os.environ` value,
   the opposite precedence from most `.env` fallbacks here, and writes the
   resolved number back to `os.environ` before uvicorn forks its workers so
   every worker (e.g. `src.api.app`'s own `AW_WORKSPACE_WORKERS` checks) sees
   the same number.

   **Two rules that fall out of sharing one keyspace across 10 workers**, both
   learned the expensive way and both worth copying into any new shared-state
   route rather than rediscovering:
   - *Name the incarnation, not just the entity.* Stream keys carry a
     per-incarnation `gen` (`…:term:out:<id>:<gen>`) and the owner key a
     per-incarnation token, because `restart()` reuses the session id while the
     PREVIOUS owner — possibly on another worker — is still shutting down. A
     teardown that names only the id reaches its own successor.
   - *A snapshot is not a fact.* Anything that DELETES shared state on the
     strength of an earlier `SCAN` must re-check the condition inside the
     delete (see `_prune_session_meta`'s Lua). Between a scan and the delete it
     informs, 10 workers create and destroy sessions continuously — an
     unconditional delete there destroys live state, silently.

   **The price W7 charges, and it is broader than "on deploy":** a PTY now dies
   with its owning worker — a deploy, a crash, or a worker recycle. A `screen`
   used to survive an app-process restart; **nothing does now.** Every terminal
   open when a deploy lands dies with it (accepted and authorised on W7's card),
   but so does every terminal whose worker is recycled for any other reason.
   Restoring cross-restart survival would mean moving the PTY out of the workers
   into a supervised sidecar — a different, larger change, not an extension of
   this one.

6. **Test.** Manager-level test for the real mechanics (a live PTY that actually
   runs a shell), TestClient test for the HTTP/WS contract + the identity gate.
   `pytest src/tests -v`.
   > TestClient's portal event loop does not service `loop.add_reader` fd
   > callbacks, so assert the live byte stream at the manager level, not through
   > a TestClient WebSocket.

7. **Build the multi-arch image + deploy to the BYOD.** GitHub Actions
   (`.github/workflows`, manual `workflow_dispatch`) builds
   `ghcr.io/fredericowu/aw-workspace:{latest,<sha>}` for `linux/amd64,arm64`.
   On the BYOD host (macbook-fred, podman at `/Users/aw/podman-dist/podman/bin`):
   `podman pull` the new image, then `podman rm -f aw-remote-host-workspace` and
   re-`run` it on the `aw-remote-host` network, publishing `127.0.0.1:9030`, with
   the same env (`AW_WORKSPACE`, `AW_WORKSPACE_SCHEMA`, `AW_WORKSPACE_DB_URL`,
   `AW_REDIS_URL`, `AW_BACKEND_URL`) and `AW_WORKSPACE_WORKERS=10`.

8. **Update this map + the KB/ADR**, and decide the monolith copy's fate: keep it
   for the single-tenant host, or deprecate it once the BYOD path fully replaces
   it. Terminal: the monolith copy stays for the single-tenant `aw.tekflox.com`
   dashboard; the BYOD SPA now uses this port.
