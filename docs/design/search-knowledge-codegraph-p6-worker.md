# search-knowledge workflow: the P6 code-reality worker — concrete now, generic already exists

Status: **proposed — goes to a Coder.** Architect run `751708dcca194f749646bc10ea02c090`,
2026-10-22, card `4015bf3b-9510-81d6-b3bb-f2f3f11d1a64` (target
`search-knowledge-workflow`). Companion to the two docs shipped on this target
today — `docs/design/ap-mt-http-mcp-transport-for-api-direct-agents.md`
(`transport=http` McpServer registry, shipped `80c3fac`) and
`docs/design/ap-mt-knowledgeable-direct-rest-tool.md` (`knowledge.search`
direct REST tool, shipped `04467f7`) — and to
`docs/design/knowledgeable-connector-tool-forwarder.md`, whose §9
reconciliation this document is the "if/when the workflow is built" moment of.

## 0. Ground truth that changes the card's premises (verified live, 2026-10-22)

The card asked three "check before designing" questions. Answers, from code
and a live call, not from the docs:

1. **The Connector forwarder is BUILT and LIVE, not "design only".**
   `repos/aw-app-knowledgeable/knowledgeable_app/forwarding.py` (commits
   `1bf649e`, `3c5e1bf`, with `tests/test_forwarding.py`); the installed copy
   `/opt/aw-workspace/apps/knowledgeable` is v0.71.0 and carries it. Live
   probe this run: `knowledge_search(include_code=true, code_query="fusion
   rrf core/fusion.py")` through the gateway returned the composed
   `{knowledge, code}` shape with `code.status: "ok"`, `source: "codegraph"`,
   `elapsed_ms: 1555`. The P6 transport exists end-to-end **today**.
2. **`include_code` does NOT live on aw-knowledgeable's REST API** — the card
   mis-attributed it. Repo-wide grep of `repos/aw-knowledgeable` for
   `include_code`: zero hits. It is Connector-side only
   (`knowledgeable_app/forwarding.py::compose_knowledge_search`, dispatched
   from `mcp/http_handler.py:32`). Consequence: **"just flip `include_code=true`
   on AP-MT's `knowledge.search` tool" is impossible** — that tool
   (`agents-platform-multitenant/backend/app/core/tools/knowledgeable.py`)
   POSTs `/api/knowledge/search` directly, and aw-knowledgeable cannot reach
   codegraph at all (codegraph is inside the workspace; aw-knowledgeable is
   on aw-stack — forwarder doc §5). Path (b) from the card is off the table
   for structural, not build-status, reasons.
3. **The workflow is an `orchestrator_worker` graph with static workers and
   frozen endpoints.** `run_orchestrator_worker`
   (`agents-platform-multitenant/backend/app/core/orchestrators/__init__.py:163-201`):
   plan node → all workers in parallel (each gets `{input}` + `{prev}`=plan)
   → worker outputs concatenated as `[worker-id]\n<text>` parts → synthesizer.
   There is no conditional execution and no deterministic non-agent node kind
   (the workflow's own description records this). The planner
   (`knowledgeable-search-planner`) and synthesizer (`knowledgeable-playground`)
   are **live production dependencies of the real `knowledge_search`** — their
   prompts/models must not be edited for this card.

## 1. The decision

**Add a fourth, self-gating worker — `knowledgeable-retriever-code` — to the
`search-knowledge` workflow. It decides per-run whether the question is
code-answerable; when yes it makes exactly one MCP call to the Connector's
`knowledge_search` with `include_code=true` and a new `code_only=true` knob,
bound through the just-shipped `transport=http` McpServer registry pointing at
the gateway profile that already exists for this workflow. When the question
is not code-shaped, or codegraph is unavailable at any layer, the worker's
output says so explicitly and the other three arms proceed unchanged.**

**On the generic ask: do not build the "apps contribute knowledge sources →
agents-platform-runners auto-registers AP-MT McpServer rows" pipeline now.**
The generic layer already exists, one level down and in the right place: the
Connector's `forward_upstreams` registry (built, config-driven). A new
workspace app becomes a knowledge source by shipping its `/mcp` route and
adding **one config entry** there — and it is then reachable by this workflow
through the **same single McpServer row** this card seeds, with zero AP-MT
changes. §5 names the trigger that would justify the AP-MT-level pipeline.

Why the worker gates itself (the "code-answerable" decision point): the
planner's prompt is frozen (live prod dep), so it cannot be taught to flag
code-shaped sub-queries; the executor has no deterministic node kind, so a
regex/heuristic node would be new executor infrastructure for N=1. The only
seam we own is the new agent's own prompt — and the judgment ("is this a
where-is-X / what-calls-Y / how-is-Z-implemented question?") is exactly the
kind of cheap single-shot classification a small LLM does well. The worker
runs on every workflow run (workers are static), but a skipped run is one
nano/mini completion with no tool call — cents, not seconds.

## 2. Where it lands

### 2a. Connector — `code_only` knob (the one code change; ships first)

`repos/aw-app-knowledgeable`:

- `knowledgeable_app/forwarding.py::compose_knowledge_search` — new optional
  arg `code_only: bool` (default false), honored only when `include_code` is
  true: skip the `client.knowledge_search_data(args)` leg of the
  `asyncio.gather` and return `{"knowledge": null, "code": {...}}`. Everything
  else (allow-lists, probe cache, 32KB result cap, `unavailable` contract) is
  the existing `forward()` path, untouched.
- `knowledgeable_app/mcp/client.py` `TOOLS_SCHEMA` — the `knowledge_search`
  entry gains `code_only` beside `include_code`/`code_query` (client.py:1587
  area), documented as "only meaningful with include_code".
- `tests/test_forwarding.py` — add: `code_only` happy path (no KG call made —
  assert via a KG-client stub that would fail the test if called);
  `code_only` + upstream down → `{"knowledge": null, "code": {"status":
  "unavailable", ...}}`; `code_only` without `include_code` → ignored,
  byte-identical legacy response.

Why `code_only` is necessary rather than nice: without it, every code-worker
call drags a full **fused** KG search into production aw-knowledgeable —
which is deliberately single-worker and is already serving this same
workflow's three arm calls concurrently — and, worse, the Connector's
`knowledge_search` schema has **no `sub_queries` pass-through** (grep: zero
hits in `client.py`; the REST param landed in aw-knowledgeable `447cb66` but
the Connector was never updated), so that redundant KG leg would invoke
aw-knowledgeable's internal planner — the measured 13–19s runner-door hop —
inside a tool call the worker's MCP client has to sit through. `code_only`
removes both problems and keeps the single-surface principle: it is still
`knowledge_search`, still the forwarder, still the arg allow-list — not the
standalone `code_search` tool the forwarder design §11 rejected.

(Adjacent, not this card: exposing `sub_queries` pass-through on the
Connector schema for parity with the REST API is a two-line change that
benefits every Connector caller. Worth its own small card.)

### 2b. AP-MT — one registry row (data, not code)

Using `transport=http` (`80c3fac`) exactly as its design's "immediate
unblock" described, in the retrievers' tenant:

```
POST /api/mcp/servers
{name: "knowledgeable-ws", transport: "http",
 url: "<gateway-as-reachable-from-AP-MT>/mcp/knowledgeable-retriever",
 headers: {Authorization: "Bearer <existing profile token>"}}
POST /api/mcp/servers/knowledgeable-ws/discover
```

The gateway named-config profile `knowledgeable-retriever`
(`tools_allow=[knowledge_search]`) **already exists with a minted token** —
it sits unused in the three retriever agents' legacy `mcp_config` (CLI-door
field, ignored by the API-direct door). Reuse profile and token; `code_only`
/`include_code` are arguments of the allowed tool, so no profile change.
The URL spelling must be the one reachable **from the AP-MT backend
container** — verify before seeding (§6.1); not the public tunnel (30s edge).

Zero AP-MT code changes: `tools_for_agent` already resolves
`mcp.knowledgeable-ws.knowledge_search` once the row can answer discovery.

### 2c. The agent (data)

New `knowledgeable-retriever-code`, same shape as the three existing
retrievers (`disable_harness_tools=true`,
`tool_specs=["mcp.knowledgeable-ws.knowledge_search"]`), **model
`openai-gpt-5-4-mini`, not nano** — this worker makes a judgment call the
others don't, and the MCP tool wrapper is a single-JSON-string `Tool`
(`langchain_tools.py:105-122`), a known fumble point for nano. Prompt
contract (Coder owns wording):

1. Receive ORIGINAL QUESTION + PLAN (same `input_template` as the other
   workers — the planner's literal-identifier sub-queries are useful signal).
2. Decide: is this answerable from the codebase itself (where is X defined,
   what calls Y, how is Z implemented, which file/config does W)? If no —
   reply exactly `CODE PERSPECTIVE SKIPPED: <one-line reason>` and stop. No
   tool call.
3. If yes — call `knowledge_search` exactly once:
   `{"query": <original question>, "include_code": true, "code_only": true,
   "code_query": <the most literal symbol/file-shaped formulation>}`.
4. Reply with the tool's raw JSON verbatim (the retriever family rule); on
   tool error, the error verbatim.

The skip sentence is deliberately prose the frozen synthesizer can read as
"no evidence from this arm" without any prompt change on its side.

### 2d. The workflow (data)

`update_workflow(slug="search-knowledge")`: append worker node
`{id: "retrieve-code", agent: "knowledgeable-retriever-code", label:
"Retrieve (code reality)", input_template: <same as the other three>}`.
The executor needs nothing: the fourth worker joins the `asyncio.gather`,
its part lands in `{prev}` for the synthesizer as `[retrieve-code]\n<text>`,
and the `[worker #N of M]` suffix adjusts itself. Also update the workflow
`description`'s fidelity-gap paragraph: the mirror now includes a P6
perspective that production `knowledge_search` only produces on caller
opt-in (`include_code=true`) — the auto-decide is this workflow's addition.

### 2e. Sequencing (the Coder's order of operations)

1. Land + release the Connector `code_only` change (release bot owns the
   version bump; the gateway only re-dials a changed tool schema on a version
   change). Tier-1: schedule the core restart deliberately — never from
   inside the run.
2. Verify live through the gateway, same probe as this design's §0 call but
   with `code_only=true` — assert `knowledge` is null and `code.status` ok.
3. From inside the AP-MT container: verify the gateway spelling
   (`curl <candidate>/healthz`), then seed the McpServer row + Discover.
4. Create the agent; one direct `run_agent` smoke test with a code question
   and one with a non-code question (expect the skip sentence).
5. `update_workflow`; one full `run_workflow` with a code-shaped question
   (e.g. "where is RRF fusion implemented?") and one general-knowledge
   question; confirm per-node events show the gate behaving.

## 3. Alternatives considered and rejected

1. **Flip `include_code=true` on the existing `knowledge.search` REST tool**
   (the card's hoped-for cheap path). Impossible, not just rejected: the
   parameter doesn't exist on aw-knowledgeable's REST surface and can't — the
   service cannot reach codegraph (§0.2). Composition is Connector-side by
   design (multi-tenant KG must never hold one workspace's code — forwarder
   doc §8).
2. **A gateway profile exposing codegraph tools directly**
   (`tools_allow=[codegraph_explore]`, worker calls codegraph with no
   Connector hop). Runner-up — one hop fewer, simplest possible tool call.
   Rejected: gateway profiles filter at tool level only, so `projectPath`
   (deliberately stripped by the forwarder's per-tool **argument**
   allow-list) would cross the boundary attached to a token stored in AP-MT's
   Postgres; and it creates exactly the "second, parallel codegraph
   transport" the forwarder reconciliation (§9) exists to prevent — two
   consumers to update on every codegraph schema change instead of one
   registry. Would revive only if the Connector hop proves a real latency
   problem (it measured 1.5s live; it won't).
3. **Teach the planner to flag code-shaped sub-queries.** The right place in
   a from-scratch design, but `knowledgeable-search-planner` is a live
   production dependency of the real `knowledge_search` — editing its prompt
   to serve the mirror inverts the dependency. A second, workflow-owned
   planner agent would fix that at the cost of a drifting fork of a prod
   prompt. Revisit if/when the workflow stops being an observability mirror
   and becomes a production entry point in its own right.
4. **A deterministic gate (regex/keyword heuristic) before the worker.** The
   executor has no non-agent node kind; building conditional node
   infrastructure for one gate is the expensive general case of a problem the
   agent prompt solves for cents. Also: "is this code-answerable" is
   genuinely fuzzy ("why does login time out?" may be either) — a worker that
   can read the plan beats a regex.
5. **The generic AP-MT-level pipeline now** ("apps contribute knowledge
   sources; runners notifies AP-MT; McpServer rows appear"). Rejected for
   now — §5.

## 4. What this makes harder later

- **The mirror diverges further from production.** The workflow now exercises
  a perspective production `knowledge_search` only produces on explicit
  opt-in, and the opt-in *decision* lives in an agent prompt here. If
  Frederico wants production `knowledge_search` to auto-decide
  `include_code` the same way, that's a separate (flagged, §5) product call —
  and its implementation could not reuse this worker's prompt mechanism
  (production has no agent in the loop).
- **`{knowledge: null}` is a third response shape** for `knowledge_search`
  consumers (legacy / composed / code-only). The forwarder design already
  accepted conditional shape (§12); this widens it by one branch.
- **The gating policy is a prompt, not code** — not unit-testable; its
  quality is only measurable by runs (the workflow's whole purpose is
  per-node observability, which is the mitigation: a wrong gate decision is
  visible in the run tree, not silent).
- **A second live consumer of the `knowledgeable-retriever` profile token**
  (the gateway named-config). Rotation now means: gateway profile + AP-MT
  McpServer row (plaintext in shared Postgres until the registry's
  encrypted-at-rest pass lands — named in the transport doc §4).

## 5. The generic-vs-specific recommendation, and two calls flagged for Frederico

**Recommendation: build the concrete P6 worker now (this doc); do not build
the generic registration pipeline.** The reasoning, falsifiably:

- **The extensibility Frederico described already shipped, at the workspace
  layer.** "Apps contribute knowledge sources" = a `forward_upstreams` config
  entry naming any installed app's standard `/api/apps/<slug>/mcp` route
  (every MCP-contributing app self-registers that exact shape). All such
  sources surface through the **one** `knowledge_search` tool on the **one**
  gateway profile this card already wires to AP-MT. Source #2 at the
  workspace layer = one config entry + zero AP-MT work. The AP-MT-level
  pipeline would duplicate that registry one level up, for N=1.
- **The only "source #2" that actually arrived chose the opposite transport.**
  aw-knowledgeable itself — this very week, on this very target — got a
  bespoke direct REST tool precisely because Frederico ruled retrieval must
  not depend on the workspace hop. An auto-registration pipeline would have
  manufactured a workspace-mediated MCP registration for it: generic
  plumbing producing the wrong answer for its first real customer is the
  strongest available evidence it's premature. P6 goes through the workspace
  only because the code physically lives there — it is the exception
  (best-effort, visibly degradable), not the pattern.
- **What would make the generic pipeline worth building** (the test for the
  future): (a) a second knowledge source that must cross the
  workspace↔AP-MT boundary *and* can't ride the Connector surface, or
  (b) a second real AP-MT tenant needing per-tenant source sets, or (c) the
  runners app needing to reconcile source lifecycle (app uninstalled → row
  must die, or it serves zombie tools — the gateway's own documented failure
  mode, which an auto-registrar would have to solve, not inherit). Until one
  of those is real, auto-registration is a credential-writing cross-system
  daemon with no second customer.

**Flagged to Frederico (product calls, not absorbed):**

1. **Should production `knowledge_search` also auto-decide `include_code`?**
   Today the composed code perspective is caller-opt-in; this workflow adds
   auto-decide only inside the mirror. Making the Connector itself classify
   every query would put an LLM call (or heuristic) on the production
   retrieval path — latency and cost on every search. My recommendation: no —
   keep opt-in at the surface, let callers (and this workflow) own the
   decision; but that's a product posture, so it's yours.
2. **The workflow's charter.** Its description still says
   "diagnostic/observability mirror — NOT a new production entry point."
   Adding P6 nudges it toward being the better pipeline (it now does
   something the one-shot tool doesn't decide for you). If it's becoming a
   real entry point, that re-opens alternative 3 (a workflow-owned planner)
   and the fidelity-gap paragraph — say so and I'll re-scope.

## 6. Risks for the Coder

1. **Gateway reachability from the AP-MT container is still unproven** — the
   transport doc's risk #1, inherited verbatim. Curl `/healthz` from inside
   `agents-platform-multitenant` before seeding the row; store that spelling;
   not the tunnel URL.
2. **`mcp` SDK ≥ 1.8 in the AP-MT image** for `streamablehttp_client` —
   check `pip show mcp` in the running container (floor in `pyproject.toml`
   is `>=1.0`).
3. **`discover_tools` swallows a bad URL/token into a fake `_error` tool**
   (`mcp_client.py:94-95`) — after Discover, assert `knowledge_search` is in
   `discovered_tools`, don't trust a non-empty list.
4. **The single-JSON-string tool wrapper**: the worker must emit
   `{"query": ..., "include_code": true, "code_only": true, "code_query": ...}`
   as one JSON string. If mini still fumbles booleans-in-string, the recorded
   fix is building `StructuredTool`s from `discovered_tools[].input_schema`
   (transport doc risk #4) — flag, don't hack around.
5. **`code_only` without `include_code` must stay byte-identical legacy** —
   the back-compat test the forwarder design already demands, extended by one
   arg. The optional-config trap (paths gated on a flag shipping untested)
   is this workspace's recorded pattern; the §2a tests are mandatory.
6. **Tier-1 deploy reality**: the Connector change reaches live sessions only
   after a release-bot version bump (gateway re-dials on `x_app_version`
   change) + a core restart. Do not hand-bump the version; do not restart
   core from inside your own run.
7. **Don't touch the frozen agents.** `knowledgeable-search-planner` and
   `knowledgeable-playground` are live production dependencies of the real
   `knowledge_search`. The only agent you create or edit is
   `knowledgeable-retriever-code`.
8. **Result size**: the forwarder caps the code text at 32KB; four worker
   parts can near ~100KB into the synthesizer. If synthesis quality drops,
   trim at the worker (its prompt can ask codegraph for fewer files via
   `maxFiles` — allow-listed in the registry) rather than touching the cap.

## 7. Addendum 2026-10-23 — §2b/§2c's MCP transport is dead; `include_code` re-homed to the Knowledge Sources fan-out

Architect, card `4025bf3b-9510-810e` (the repoint card), run
`c2f2134e2138432982e43192a05488a5`. Records the decision the Coder on that
card correctly escalated instead of making alone.

**What happened since §2 was written.** The `knowledgeable-ws` McpServer row
never completed Discover and never can under current topology: there is no
network path from AP-MT's backend (bare-metal docker engine) to
`aw-app-mcp-gateway:9200` (inside the nested `aw-remote-host` podman network,
no published port) — proven live 2026-10-22, memory
`ap-mt-cannot-reach-workspace-mcp-gateway-network`. The Coder (run
`a4f1fb62daae48d`) repointed `knowledgeable-retriever-code` to the same
transport the other three workers use — `tool_specs=["knowledge.search"]`,
`mcp_config` byte-identical — and verified live (runs `a34360666eda…`:
correct self-gate skip; `de39739fb874…`: correct gate-open + successful
call). Connectivity parity achieved; the dead `knowledgeable-ws` reference is
gone.

**Consequence, verified by codegraph trace + empirical run output:** the
`knowledge.search` integration tool
(`agents-platform-multitenant/backend/app/core/tools/knowledgeable.py:21`,
resolved via `langchain_tools.py:144`) carries no
`include_code`/`code_only`/`code_query`, and aw-knowledgeable's
`/api/knowledge/search` (`backend/app/api/knowledge_search.py:28`) has no
such fields either. So the P6 worker currently produces a **KG-only
retrieval under the code-reality label** — `code.status: "ok"` has never
been reachable through AP-MT, by any transport that exists today.

**Decision:**

1. **Do NOT extend the REST primitive with code args** (the Coder's option
   a). Impossible as stated — §0.2/§3.1 stand: aw-knowledgeable cannot reach
   codegraph, composition is Connector-side by design. And now also
   direction-violating: `/api/knowledge/search` is being demoted to a pure
   retrieval primitive (`search-knowledge-entrypoint-migration-scoping.md`
   §1); widening its schema with cross-boundary orchestration args would
   re-grow exactly what is being removed.
2. **Do NOT fix the AP-MT↔workspace-gateway network gap for this** (option
   b). The gap is a deliberate isolation boundary (publishing the gateway
   port is the hole the bare-metal skill warns against; the tunnel's 30s
   edge was rejected in the transport design); `McpServer` rows are
   tenant-wide with no `workspace` column, so (b) cannot satisfy Frederico's
   per-workspace routing requirement anyway; and it would build a second,
   parallel transport to the one already under Frederico's review.
3. **The code-reality perspective is delivered by
   `knowledgeable-knowledge-sources-registry.md` C1+C2+C4** — AP-MT calls a
   new `knowledge.sources` direct REST tool → aw-knowledgeable
   `POST /api/knowledge/sources/query` → per-workspace Connector callback
   `POST /source-query` → `forwarding.forward()` → codegraph. That design's
   §4e already names this card as the tactical bridge it supersedes. Pending
   Frederico's approval and PO slicing; nothing further ships on the repoint
   card.

**Interim state, accepted consciously (option c until C4):** the worker's
prompt still instructs `include_code: true, code_only: true` — arguments the
`StructuredTool` signature drops harmlessly (verified: run `de39739f…`
succeeded; its output has no `code` key). Left as-is deliberately: C4
rewrites the prompt wholesale, and an interim edit would trade a verified
live row for a cosmetically-honest unverified one. Until C4, a non-skipped
code-arm answer is a KG search with a code-shaped query, not codegraph
truth — C4's workflow-description update (§2d here, plus the "transitional"
note from the migration doc §0) is where that becomes visible in the
artifact itself. §2a's `code_only` Connector knob (shipped, v0.71) stays:
workspace-local composed callers use it today, and it remains correct
independent of this transport.
