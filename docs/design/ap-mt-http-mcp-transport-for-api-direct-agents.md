# AP-MT: HTTP MCP transport for API-direct agents (instead of a stdio↔HTTP bridge)

**Card:** `4015bf3b-9510-8118-81f8-fc8c5dc71ae2` — "AP-MT: generic stdio↔HTTP MCP bridge for API-direct agents (multi-tenant safe)"
**Architect run:** `9057703cdba1463ea65d1c0214ca0d82`, 2026-10-22
**Status:** proposed — goes to a Coder after Frederico/dispatcher approval.

## 0. The decision

Do **not** build a stdio↔HTTP bridge child process. Add a native **`http` transport
type** to AP-MT's existing, already-tenant-scoped MCP server registry
(`McpServer` + `core/mcp_client.py`), so an API-direct agent's
`tool_specs=["mcp.<server>.<tool>"]` resolves against an HTTP MCP server (the
aw-gateway's scoped Streamable HTTP endpoint first, any HTTP MCP later) with a
plain in-process HTTP call — no child process at all.

This delivers exactly the product outcome asked for — "configure MCPs that work
over HTTP in the MCPs tab, usable by API-direct agents, multi-tenant without
leakage" — and it delivers the *multi-tenant* property **better** than the
bridge, because the leak surfaces in this area are the spawn model itself, not
the transport (see §3). The stdio bridge is the runner-up, kept in §5 with the
condition that would revive it.

The immediate unblock falls out for free: the 3 retriever agents
(`knowledgeable-retriever-vector/lexical/graph`) already carry
`tool_specs=["mcp.knowledgeable.knowledge_search"]`. They fail today only
because no `McpServer` row named `knowledgeable` exists in their tenant. After
this change, one registry row (`transport=http`, gateway profile URL + profile
token) + one Discover makes them work with **zero agent changes**.

## 1. What the code actually does today (read, not assumed)

* **API-direct door resolves MCP tools already.**
  `tools_for_agent` (`backend/app/core/tools/langchain_tools.py:143`) parses
  `mcp.<server>.<tool>` specs, loads the named `McpServer` row's
  `discovered_tools`, and wraps each as a LangChain `Tool` whose coroutine
  calls `core/mcp_client.call_tool(server, tool, args)`
  (`langchain_tools.py:105-138`). The card's "API-direct resolves ONLY from
  tool_specs, never mcp_config" is right — but `tool_specs` already reaches MCP;
  the gap is only that the registry speaks **stdio only**.

* **The registry is stdio-spawn only, per tool call, env-leaky.**
  `mcp_client.mcp_session` (`backend/app/core/mcp_client.py:54-65`) spawns
  `command+args` with `env = {**os.environ, **(row.env or {})}` — the child
  inherits the backend's **entire environment**: `ANTHROPIC_API_KEY`,
  `AGENTS_DATABASE_URL` (all tenants' data), service secrets. And
  `call_tool` (`mcp_client.py:98-117`) opens a **fresh process + MCP
  initialize per tool call**.

* **The registry is already tenant-scoped, with strict enforcement.**
  `McpServer(TenantScoped, Base)` with `tenant_id NOT NULL` and a unique index
  on `(tenant_id, name)` (`backend/app/models.py:1200-1217`). Every ORM query
  against a `TenantScoped` table is filtered by a global `do_orm_execute` +
  `with_loader_criteria` listener (`backend/app/core/tenant_filter.py`), mode
  `strict` **by default** (`backend/app/config.py:135`) — bound tenant filters,
  unbound tenant **raises** (`UnscopedTenantAccess`), i.e. fails closed.
  `/api/mcp/*` routes are gated by `require_tenant`
  (`backend/app/api/__init__.py:48,73`), which binds the tenant contextvar the
  filter reads. `backend/tests/test_tenant_isolation.py:257-258,486` already
  seeds per-tenant `McpServer` rows and scans `/api/mcp/servers` + `/api/mcp/tools`
  for cross-tenant leakage.

* **Per-agent HTTP MCP config with token injection exists on the CLI door.**
  `Agent.mcp_config` / `AgentConfig.mcp_config` hold
  `{servers: {name: {type: "streamable-http", url, headers}}}`
  (`backend/app/models.py:273,307`), rendered per agent by
  `_write_mcp_config_files` / `resolve_mcp_servers_payload`
  (`backend/app/api/agents.py:220-290`) with gateway-identity-checked token
  injection (`app/config.py:83-101`, `aw_gateway_url`/`aw_gateway_token`).

* **An "MCPs" admin page already exists in AP-MT's UI.**
  `frontend/src/routes/Mcp.tsx` (154 lines) — list/create/edit/delete/discover
  over `/api/mcp/servers`, today with stdio fields (command/args/env). Part 2
  is an extension of this page, not a new tab.

* **Deps:** `pyproject.toml:32,38` — `mcp>=1.0`, `httpx>=0.27`. The official
  SDK's `mcp.client.streamable_http.streamablehttp_client` needs mcp ≥ 1.8
  (workspace container has 1.29.0; the AP-MT image's installed version must be
  checked — see risks).

## 2. Where it lands

### 2a. Backend — HTTP transport in the registry (the core change)

1. **`backend/app/models.py` — `McpServer`** gains:
   * `transport: str` default `"stdio"` (`stdio` | `http`),
   * `url: str | None`,
   * `headers: dict[str, str]` default `{}` (bearer token lives here as
     `Authorization: Bearer …`, same shape as `Agent.mcp_config` headers).
   Columns added via the existing `_ensure_column` migration pattern in `db.py`.

2. **`backend/app/core/mcp_client.py`** — `mcp_session` branches on transport:
   * `http` → `streamablehttp_client(row.url, headers=row.headers)` from the
     official SDK, then the same `ClientSession` as today. The SDK client is
     chosen over a hand-rolled `httpx` JSON-RPC POST because "any HTTP MCP
     later" includes sessionful Streamable HTTP servers (session-id header,
     SSE responses) a bare POST can't handle. Bump the floor to `mcp>=1.8`
     (or whatever the image already has ≥ that).
   * `stdio` → existing path, **but fix the env leak in the same card**:
     spawn with a minimal env (`PATH`, `HOME`, `LANG` + `row.env`), **not**
     `{**os.environ, …}` (`mcp_client.py:60`). This is a live leak today for
     every stdio registration, bridge or no bridge.
   `discover_tools`/`call_tool` only need their detached snapshot
   (`mcp_client.py:74,103`) to copy the new fields. Keep the per-call session:
   for HTTP it's just a request cycle, and it matches the existing stateless
   shape.

3. **`backend/app/api/mcp.py`** — `McpServerIn`: `command` becomes optional,
   add `transport`/`url`/`headers`; validate `http` ⇒ `url` required,
   `stdio` ⇒ `command` required. **Mask secret values on read**: `McpServerOut`
   must return header *names* only (and mask `env` values too — today
   `GET /api/mcp/servers` echoes `env` back verbatim, which would hand any
   tenant *member* the stored tokens of their tenant; write-only semantics like
   the secrets conventions elsewhere). Update = replace; absent `headers` in a
   PUT keeps the stored ones.

4. **No change to `langchain_tools.py` for the MVP.** `tools_for_agent` and
   `_make_mcp_tool` already do the right thing once the registry can answer.

### 2b. Credential + scoping model (how "multi tenant sem vazamento" holds)

The credential is **per `(tenant, server-name)` registry row**, resolved at
call time inside the run's own `tenant_scope` — strict mode guarantees a run
can only ever resolve **its own tenant's** row for a name; another tenant's
`knowledgeable` is a different row with a different URL and token. Within a
tenant, per-agent narrowing is done the way this platform already does it:
**one gateway named-config (profile) per scope** (`tools_allow`, per
`aw-autoskill-ap-mt-scope-agent-to-one-tool`) and one registry row per profile
(e.g. `knowledgeable` → `/mcp/knowledgeable-retriever`); an agent opts in via
`tool_specs`. The tenant — not the agent — is AP-MT's isolation boundary (one
backend container, one DB, per-tenant rows), and this design doesn't pretend
otherwise; it also never puts a credential in a place scoped wider than the
tenant that owns it, and never passes it through a process environment at all.

Immediate unblock, concretely:
* gateway named-config `knowledgeable-retriever` with
  `tools_allow=[knowledge_search]` (exists or created per the autoskill),
* `POST /api/mcp/servers` in the retrievers' tenant:
  `{name: "knowledgeable", transport: "http", url: "<gateway-from-APMT>/mcp/knowledgeable-retriever", headers: {Authorization: "Bearer <profile token>"}}`,
* `POST /api/mcp/servers/knowledgeable/discover`,
* done — `tool_specs=["mcp.knowledgeable.knowledge_search"]` is already set on
  all three agents.

### 2c. Part 2 — the "MCPs" admin surface

Extend `frontend/src/routes/Mcp.tsx` + `frontend/src/lib/api.ts::McpServer`:
* transport selector; `stdio` shows command/args/env, `http` shows url +
  headers (token field write-only, shown as `••• (set)` after save).
* After Discover, each discovered tool row shows its **copyable spec string**
  `mcp.<server>.<tool>` — that *is* the "generated registration" for the
  API-direct door; no command+args+env generation exists in this design
  because there is no process. Optional (PO call): an "attach to agent…"
  dropdown that PATCHes the chosen agent's `tool_specs`.
* UI must surface the known footgun: an agent with `tool_specs=[]` has **all 6
  builtins including `run_command`** (`langchain_tools.py:150-156`); the first
  explicit spec removes the builtins. When attaching a first MCP tool to an
  empty-spec agent, warn that builtins must now be listed explicitly if wanted.

## 3. Alternatives considered and rejected

1. **The generic stdio↔HTTP bridge child process (the card's nominal ask).**
   Rejected as the primary because, measured against its own stated goal
   (multi-tenant leak-safety), it is strictly worse than not spawning:
   * It adds **no isolation boundary**. The spawn point is the shared AP-MT
     backend container — every tenant's bridge processes share one container,
     UID, filesystem and network identity. "Per-spawn env" is process-level
     hygiene inside an unchanged boundary; the HTTP call the bridge would make
     is the same HTTP call the backend can make itself.
   * The registry's spawn mechanism is the actual hazard: `/api/mcp/servers`
     is tenant-member-writable and `command` is arbitrary → **arbitrary code
     execution in the shared backend container**, with (today) the full
     platform env (`mcp_client.py:60`). A sanctioned bridge binary doesn't
     close that; pinning `command` to a vetted binary does — and a vetted
     binary whose only job is HTTP forwarding *is* Option B with an extra
     fork/exec, an extra Python interpreter startup, and an extra
     serialization hop **per tool call** (`call_tool` opens a fresh session
     every call).
   * Latency: fork/exec + interpreter + MCP initialize + HTTP per retrieval
     call, on a workflow whose whole point is being cheap/fast on nano models.
2. **Adopt `mcp-remote` (npm) as the bridge command.** Same structural
   objections as 1, plus a Node runtime dependency in the backend image. It
   remains the right answer **if** an external stdio-only MCP *client* outside
   AP-MT's backend ever needs to reach the gateway — use the ecosystem
   standard then; don't write our own (§5).
3. **Resolve the agent's own `mcp_config.servers` in `tools_for_agent`**
   (unify the doors; no registry involvement). Attractive — same per-agent
   credential the CLI door uses — but rejected for this card: (a) the stored
   URLs are written for the **runner-container vantage point**
   (`aw-app-mcp-gateway:9200`, bridge IPs) and are routinely unreachable from
   the AP-MT backend container, so "reuse" silently breaks per deployment
   topology; (b) it bypasses the discovery/caching the ReAct loop needs
   (`discovered_tools`); (c) it makes every agent-edit a credential-edit with
   no admin surface — the opposite of part 2. Kept as a plausible future
   unification; nothing here precludes it.
4. **The rejected `repos/aw-mcp-gateway/connector/` reverse-`/link` design** —
   per the card, out of scope: it solves the opposite direction (local stdio
   MCP registering itself *behind* a remote gateway). Not re-litigated.
5. **Hand-rolled `httpx` JSON-RPC POST instead of the SDK client** (mirroring
   aw-mcp-gateway's own `HttpUpstream`). Runner-up implementation detail;
   rejected because sessionful Streamable HTTP servers (session-id, SSE) are
   explicitly in the "any HTTP MCP later" scope. Falls back in if the AP-MT
   image's `mcp` version can't be raised to ≥1.8 for some reason.

## 4. What this makes harder later

* **Two MCP config surfaces persist** (registry for the API-direct door;
  `Agent.mcp_config` for the CLI door). A later unification (alt 3) now has
  two credential stores to merge instead of one.
* **Headers/tokens plaintext in the shared Postgres** — parity with
  `Agent.mcp_config` today, but this table will accumulate more tokens. A
  future encrypted-at-rest pass (`core/secret_crypto.py` Fernet exists for
  exactly this) must migrate this table too. Masking on read (§2a.3) is the
  minimum shipped now.
* **Per-call HTTP sessions** — no connection pooling or session reuse. Fine at
  current volume; a high-QPS agent later wants a pooled client keyed by row id.
* **No in-house stdio bridge exists** if an external stdio-only consumer shows
  up — the decision there is "adopt `mcp-remote`", recorded here so it isn't
  re-invented.

## 5. Risks for the Coder (the non-obvious ways this breaks)

1. **Gateway reachability from the AP-MT container is unproven.** The same
   gateway spells differently per vantage (`http://aw-app-mcp-gateway:9200`
   inside the workspace network; `http://172.18.0.1:9200` from outside; the
   codex path uses `settings.aw_gateway_url`, seen as `<host>:9123` in tests).
   **Verify with a curl of `/healthz` from inside the
   `agents-platform-multitenant` container before seeding the retriever row**,
   and store that spelling. Avoid the public tunnel URL (30s edge cut).
2. **`mcp` SDK version in the AP-MT image.** Floor is `>=1.0`;
   `streamablehttp_client` needs ≥1.8. Check `pip show mcp` in the running
   container, bump `pyproject.toml` accordingly; if pinned low by something
   else, use alt 5's httpx fallback.
3. **Strict tenancy is an env-var lever.** `AGENTS_TENANT_ENFORCEMENT=off`
   disables the entire filter (`tenant_filter.py` docstring). The isolation
   claim in §2b holds only under `warn`(bound)/`strict`. Verify the prod
   container doesn't set `off`, and add an http-transport case to
   `test_tenant_isolation.py` (two tenants, same server *name*, different
   URLs → each run resolves its own; headers never appear in `GET` responses).
4. **`_make_mcp_tool` is a single-JSON-string-arg `Tool`**
   (`langchain_tools.py:105-122`) — a nano model must emit `{"query": "…"}`
   as a JSON string. It works, but if retrieval quality is poor, the fix is
   building `StructuredTool`s from the already-stored
   `discovered_tools[].input_schema` — scoped out here, flag to PO if needed.
5. **`discover_tools` swallows errors into a fake tool**
   (`mcp_client.py:94-95` returns `[{"name": "_error", …}]`) — a wrong
   URL/token at Discover time looks like a one-tool server in the UI. Surface
   `_error` rows as failures in `Mcp.tsx`.
6. **Don't regress the stdio path while fixing its env**: some stdio servers
   legitimately need vars from the platform env; the minimal-env change must
   keep `row.env` as the explicit opt-in channel and should be verified
   against any existing `source="file"` registrations synced from `.mcp.json`
   (`sync_mcp_servers_from_file`, `mcp_client.py:28`).
7. **Out-of-scope but must be decided by PO/Frederico:** tenant-member-writable
   *stdio* registrations are RCE-in-shared-container by design (§3.1). This
   card doesn't fix that; recommend gating `transport=stdio` creation to the
   ops tenant or an admin role in a follow-up card.
