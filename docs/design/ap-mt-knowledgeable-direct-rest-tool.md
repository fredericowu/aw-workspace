# AP-MT → aw-knowledgeable direct REST tool for the retriever agents

Status: **decided — follow-up design on card `4015bf3b-9510-8118-81f8-fc8c5dc71ae2`,
2026-10-22.** Companion to
`docs/design/ap-mt-http-mcp-transport-for-api-direct-agents.md` (the
`transport=http` McpServer feature, shipped as `agents-platform-multitenant`
`80c3fac` — that feature **stands untouched**; it is simply the wrong
mechanism for this one binding) and to
`docs/design/aw-knowledgeable-multi-perspective-search.md` §4, whose P1–P5
transport decision ("ap-mt → `http://aw-knowledgeable:8090` directly over
aw-stack, own scoped read-only API key, bucket `main`") this document
implements for the three retriever agents.

Trigger: Frederico, on the card — "O ap-mt deve funcionar conectando
diretamente ao aw-knowledgeable sem passar pela workspace." The Coder then
established from code that aw-knowledgeable speaks **no MCP at all**
(`repos/aw-knowledgeable/backend/app/main.py`,
`backend/app/api/knowledge_search.py` — plain FastAPI;
repo-wide grep for `mcp.server|streamable_http|FastMCP|stdio_server` → zero
hits). The `knowledge_search` MCP tool lives only in the workspace-side
Knowledgeable Connector (`repos/aw-app-knowledgeable/knowledgeable_app/mcp/
client.py`), which itself just POSTs `/api/knowledge/search`. So "direct"
cannot be an MCP registration of any transport.

## 1. The decision

**AP-MT gets a first-class LangChain tool that POSTs
`/api/knowledge/search` directly, resolved from a new spec id
`knowledge.search`; aw-knowledgeable gets the minimal scoped read-only API
key that §4 assumed already existed — it does not.** That second half is the
load-bearing correction this document makes to §4:

> §4 says the workflow calls aw-knowledgeable "with its own scoped
> read-only API key (the scoped-api-keys mechanism already exists)".
> **Doc drift.** Read today, `backend/app/core/identity.py` accepts exactly
> two credentials: a human `aw_id_jwt` verified against aw-backend's JWKS
> (`require_identity`, identity.py:157), and the `X-Internal-Secret`
> service secret (`verify_service_secret`, identity.py:182) — which
> resolves to the service tenant and, through the deliberately "T2-less"
> `resolve_bucket_scopes` (identity.py:347–379), to **every registered
> bucket, read+write**. There is no per-key scope anywhere. The seam for
> adding one is explicitly reserved in that function's own docstring:
> "When T2 lands, only this function's body changes."

The three agents (`knowledgeable-retriever-vector/lexical/graph`,
API-direct door, `openai-gpt-5-4-nano`) then swap
`tool_specs=["mcp.knowledgeable.knowledge_search"]` →
`tool_specs=["knowledge.search"]`. Nothing else about them changes.

A side benefit worth stating: the direct binding is *better* for a nano
model than the MCP path would have been. `langchain_tools.py`'s MCP wrapper
(`_make_mcp_tool`, langchain_tools.py:105–122) is a single-string `Tool`
that makes the model serialize all arguments into one JSON string — a known
fumble point for small models. A direct `StructuredTool` gives
`knowledge_search` real typed parameters.

## 2. Where it lands

### 2a. aw-knowledgeable — the scoped read-only key (ships first)

- `backend/app/config.py`: two new settings —
  `knowledgeable_read_api_key: str = ""` and
  `knowledgeable_read_api_key_buckets: str = "main"` (comma-separated).
  Boot validator mirroring the existing one at config.py:414: key set
  without `KNOWLEDGEABLE_SERVICE_TENANT_ID` → refuse to boot (the scoped
  key resolves to the service tenant, so the same "no silent default
  tenant" guard applies).
- `backend/app/core/identity.py`:
  - `ScopedServiceIdentity(ServiceIdentity)` carrying a fixed
    `BucketScopeSet` (`{bucket: "read"}` per configured bucket).
  - `verify_scoped_read_key(request)` — `X-Api-Key` header compared with
    `hmac.compare_digest`, same non-raising contract as
    `verify_service_secret` (identity.py:182–194). A distinct header, not
    `Authorization: Bearer`, so it can never be confused with a JWT by
    `require_identity`'s bearer branch.
  - One new fallback branch in `require_identity_or_service`
    (identity.py:197–210), tried after the service secret.
  - One new branch at the top of `resolve_bucket_scopes`
    (identity.py:347): `isinstance(identity, ScopedServiceIdentity)` →
    return its fixed scope set. This is the reserved seam, verbatim.
  - `resolve_tenant_id` needs **zero changes** — `ScopedServiceIdentity`
    inherits `SERVICE_USER_ID`, which already resolves to
    `knowledgeable_service_tenant_id` (identity.py:280).
  - Enforcement comes free: `_bucket_denied` (identity.py:382) already
    gives read-scope→write = 403 and unknown-bucket = 404;
    `resolve_search_scope`'s omitted-bucket branch returns exactly the
    readable set, i.e. `["main"]`. `POST /api/knowledge/search` sits
    behind the shared `require_tenant_or_service` gate list
    (`backend/app/api/__init__.py`), so the new branch is inherited, not
    wired per-route.
- Tests: the dependency-override seam `resolve_bucket_scopes`'s docstring
  names is already there. Add: scoped key → search 200 scoped to `main`;
  scoped key → any write route 403/404; scoped key → other bucket 404;
  wrong key → 401.

### 2b. AP-MT — the tool (ships second)

- `backend/app/config.py` (env prefix `AGENTS_`, config.py:21): new
  settings `knowledgeable_base_url: str = ""` and
  `knowledgeable_api_key: str = ""` → env
  `AGENTS_KNOWLEDGEABLE_BASE_URL=http://aw-knowledgeable:8090`,
  `AGENTS_KNOWLEDGEABLE_API_KEY=<key>`.
- New `backend/app/core/tools/knowledgeable.py`: a `StructuredTool` named
  `knowledge_search` with params `query` (required), `mode` (default
  `"fused"`; valid: `vector|lexical|graph|fused`, server clamps —
  `core/knowledge_search.py:74,128`), `limit` (default 20), `sub_queries`
  (optional list), `synthesize` (default **False**), `budget_ms` (default
  20000). Forwards field-by-field, never the raw dict (the connector
  client's own rule, `knowledgeable_app/mcp/client.py:1288`). Two
  deliberate defaults, both load-bearing:
  - **`sub_queries` omitted → the tool sends `sub_queries=[query]`.**
    That hits the `_provided_plan` path
    (`core/knowledge_search.py:596–597`), skipping the planner entirely —
    the planner is an ap-mt runner-door hop measured at 13–19s per call,
    absurd inside a retrieval tool call. The agent can pass its own 2–3
    sub-queries when it wants decomposition.
  - **`synthesize=False`** — synthesis is an aw-knowledgeable→AP-MT call
    (`_synth_config`, `api/knowledge_search.py:62`), so leaving it on
    makes every retriever tool call a circular AP-MT→KG→AP-MT round trip
    paying a second LLM. The retrievers' own loop is the synthesizer.
  - httpx timeout = `budget_ms/1000 + 10s`; non-2xx surfaced as a plain
    `{"error", "status", "body"}` string to the model, never swallowed
    (the first design's `_error`-tool lesson). Note in the tool's error
    text that a 404 can mean "bucket hidden from this key", not only a
    wrong URL — aw-knowledgeable hides denials as 404 on purpose.
- `backend/app/core/tools/langchain_tools.py`: a new
  `integration_tools() -> dict[str, StructuredTool]` returning
  `{"knowledge.search": ...}` **only when both settings are set** (else
  empty — the spec then resolves to nothing, preserving today's
  fail-closed zero-tools behaviour, with one warning log).
  `tools_for_agent` consults it on exact spec match, exactly like the
  `spec in builtins` branch (langchain_tools.py:162). **It is NOT merged
  into `builtin_tools()`** — the empty-`tool_specs` default returns all
  builtins (langchain_tools.py:155–156), and a network tool with a live
  credential must never ride into that default silently.
- The three agents: `update_agent` each of
  `knowledgeable-retriever-{vector,lexical,graph}` to
  `tool_specs=["knowledge.search"]`; optionally pin each agent's prompt to
  its `mode`.

### 2c. Provisioning / rotation

Generate a ≥32-byte random key once. Canonical copy in the workspace vault
(aw-app-secrets) — same home as the playground key — named
`knowledgeable-apmt-search-key`, for bookkeeping and rotation. Then into
both deploys as env: aw-knowledgeable (`KNOWLEDGEABLE_READ_API_KEY`) and
AP-MT (`AGENTS_KNOWLEDGEABLE_API_KEY`). Rotation = set new env on both +
recreate both containers; that is the same stated cost the playground key
already accepted (`api/playground.py:86`'s docstring), and this key is on
no request-time vault path.

## 3. Rejected, and why

1. **McpServer `transport=http` row → gateway `knowledgeable-retriever`
   profile** (the original unblock). Overridden by Frederico: AP-MT must
   not depend on the workspace/gateway hop for retrieval; §4's P1–P5
   transport is direct. The shipped feature remains the right mechanism
   for any *real* HTTP MCP server.
2. **Teach aw-knowledgeable MCP** (FastMCP mount or a sidecar
   stdio/HTTP wrapper) so the shipped feature could be reused. A whole new
   protocol surface, for one consumer, on a service whose design is
   deliberately plain-REST single-worker — and the result would be *worse*
   for the nano models (single-JSON-string MCP Tool wrapper vs typed
   StructuredTool).
3. **Reuse `X-Internal-Secret`.** Zero new code, but it is read+write on
   every bucket, its rotation is coupled to the workspace connector, and
   it has a live history of being clobbered by the connector's `/settings`
   route. §4 demanded a new read-only credential for exactly these
   reasons.
4. **Per-tenant credential rows for the tool** (a McpServer-headers-style
   table). Scope creep now; see §5 — the platform-level env credential
   matches the existing `OPENAI_API_KEY` posture for builtins. Revisit if
   a second real tenant ever needs its own KG credential.
5. **stdio bridge process** — already rejected in the first design on this
   card; nothing here reopens it.

## 4. What this makes harder later

- **`knowledge_search`'s request shape now has a hard consumer outside the
  workspace.** §8 of the multi-perspective doc already accepted this cost;
  this is its first concrete instance.
- **The single-key env scheme doesn't scale to many keys.** A second
  scoped consumer forces either a second env pair or the real T2
  (per-token scopes). That is deliberate: the minimal mechanism occupies
  the reserved seam without pre-building T2.
- **`tools_for_agent` now has two registries** (builtins = in the empty
  default; integrations = explicit-spec only). Future tool authors must
  pick the right one; the distinction is documented at the function.
- **Rotation touches two containers** (recreate both). Known, stated.

## 5. One call flagged to the PO, not absorbed

With the credential as platform-level env, **any AP-MT tenant** whose agent
lists `knowledge.search` reads Frederico's bucket `main`. Decided here:
acceptable today — operator-controlled single deployment, read-only
single-bucket blast radius, same posture as the shared `OPENAI_API_KEY`.
The tightening knob, if a second real tenant lands, is a
`AGENTS_KNOWLEDGEABLE_TOOL_TENANTS` allowlist read from AP-MT's
`tenant_ctx` inside the tool factory (the tenant is bound before
`tools_for_agent` runs at `executor.py:2279` — verified in the first
design pass). Follow-up card, PO's call on timing.

## 6. Risks for the Coder

1. **Sequencing is not optional.** aw-knowledgeable auto-ships on push to
   master. Order: (1) land the aw-knowledgeable key mechanism, set env,
   recreate its container; (2) from inside the AP-MT container, verify
   reachability + auth live — `GET /api/health` (identity-free, on `app`
   directly) then an authed `POST /api/knowledge/search` with the key;
   (3) land + deploy the AP-MT tool; (4) flip the 3 agents' `tool_specs`;
   (5) one live retriever run end-to-end. Flipping agents before (2)
   proves anything reproduces the current broken state with better
   camouflage.
2. **Don't trust the gateway-era assumption of reachability.** Both are
   aw-stack services per §4, but "the same service spells differently per
   vantage point" was this card's own first-design risk. Verify
   `http://aw-knowledgeable:8090` from the AP-MT container, not from the
   workspace.
3. **Production aw-knowledgeable is single-worker, on purpose.** Three
   retriever agents each making one bounded call is fine; do not add
   client-side retries-with-fanout to the tool.
4. **The optional-config trap:** a tool gated on env ships green and
   invisible when the env is unset. Add a test that constructs the tool
   with the settings SET (and one asserting the spec resolves to zero
   tools when unset) — AP-MT tests run with
   `AGENTS_DATABASE_URL=sqlite://`.
5. **Identity tests must hit the real dependency chain once** — the
   override seam makes it easy to test `_bucket_denied` against an
   injected scope set and never exercise `verify_scoped_read_key` itself.
   One TestClient test with the real header, please.
6. **404 ambiguity**: with the scoped key, a typo'd bucket and a missing
   route both read as 404 (existence hiding is deliberate,
   identity.py:400). The tool's error string should carry status+body so
   a misconfiguration is debuggable from the agent transcript.
