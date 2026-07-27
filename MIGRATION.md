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
| `/api/v2/agent-sessions*` | **aw-workspace** | present but empty (no agent CLIs on the slim image yet) |
| `/ws/status` | **aw-workspace** | slim subset — terminal list push only, for now |
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
   dropped `screen`-backing + the `screen_sessions`/`agent_sessions`/
   `window_sessions` tables, the Claude `PromptDetector`, and all
   claude/codex/cursor/gemini `--resume` session-id detection — the slim image
   ships no agent CLIs, so a terminal is just a shell (or an arbitrary command).
   Kept the PTY mechanics (fork/exec, non-blocking fan-out reader, resize,
   chunked write, scrollback) verbatim so the byte contract is unchanged.

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
   present and return a safe empty/no-op shape (e.g. `/api/v2/agent-sessions` →
   `[]`) so the SPA never 404s.

5. **Mind stateful routes + worker count.** In-memory session state (PTY fds,
   subscriber queues) only lives in ONE uvicorn worker, so create-on-worker-A /
   WS-on-worker-B would miss. aw-workspace therefore runs **single-worker**
   (`AW_WORKSPACE_WORKERS=1` in the Dockerfile/compose) — fine for a single-user
   data-plane. If a future feature needs multiple workers, add a shared backing
   store (the monolith's `screen` + Postgres model) before bumping the count.
   Restart persistence is deferred for the same reason (in-memory only today).

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
   `AW_REDIS_URL`, `AW_BACKEND_URL`) and `AW_WORKSPACE_WORKERS=1`.

8. **Update this map + the KB/ADR**, and decide the monolith copy's fate: keep it
   for the single-tenant host, or deprecate it once the BYOD path fully replaces
   it. Terminal: the monolith copy stays for the single-tenant `aw.tekflox.com`
   dashboard; the BYOD SPA now uses this port.
