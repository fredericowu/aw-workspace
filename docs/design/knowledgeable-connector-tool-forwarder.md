# Knowledgeable Connector as a tool forwarder — a generic forwardable-upstream registry, codegraph first

Status: **design, for discussion — nothing here is built.** Written by the
Architect agent, 2026-10-16, against card `3fb5bf3b-9510-815c-9bfd-d017b05044dc`
(target `knowledge-federated-sources`). Frederico's ask, verbatim (Telegram,
2026-10-16): *"workspace codegraph (and probably other tools in the future) —
I want, through the knowledgeable tool that the workspace uses, to be able to
alse query the workspace codegraph when it is installed/available, in other
words, the workspace app Knowledgeable Connector, would interface with the
workspace codegraph tools, acting like a request forwarder."*

Companion to `docs/design/aw-knowledgeable-multi-perspective-search.md`
(2026-10-15), whose retrieval semantics this **preserves unchanged** and whose
§4 codegraph transport this **supersedes** (see §9 — reconciled, so nobody
implements both).

## 0. Actionable summary

1. Add a **forwardable-upstream registry** to the Connector
   (`repos/aw-app-knowledgeable`): app config key `forward_upstreams`, a list
   of entries `{id, transport, app_slug, tools (allow-list + per-tool arg
   allow-list), probe, timeout_s}`. Codegraph ships as the default first entry.
2. Transport is **loopback HTTP to the upstream app's own `/mcp` route**
   (`http://127.0.0.1:$AW_PORT/api/apps/codegraph/mcp`, `X-Api-Key` =
   `AW_WORKSPACE_API_KEY`, already in this Tier-1 process's env) — the exact
   URL+auth the gateway itself uses for that upstream. **Not** via
   aw-mcp-gateway: no re-entry, no zombie-cache exposure, no gateway token.
3. Surface: `knowledge_search` gains `include_code: bool` (default false) and
   optional `code_query: string`. The code answer returns in a **separate
   top-level `code` section**, never inside `chunks`/`citations` — P6
   semantics from the multi-perspective design, unchanged.
4. Availability degrades **visibly**: `code.status = "unavailable"` with a
   reason string, knowledge answer still returned; probed by a real
   `codegraph_status` **tools/call** (never `tools/list` presence), cached
   per worker (60s ok / 15s fail), also shown on the app's `GET /status`.
5. Bounds: tool + argument **allow-lists** (unknown tool/arg rejected at the
   boundary; `projectPath` is simply not allow-listed — codegraph reads
   `repos/**`, so a free `projectPath` is a filesystem-read primitive);
   per-forward timeout 10s default / 20s clamp, run **concurrently** with the
   KG call so the 30s edge envelope is unchanged; authorization is the
   **Connector's own**, caller headers logged for audit only.
6. Adding upstream #2 = **one config entry**, provided it is another installed
   app's `/mcp` route (they all self-register the same shape). A non-app
   upstream is a new transport kind = code change, deliberately.
7. Multi-tenancy: code results are composed **workspace-side** and never enter
   the multi-tenant KG service; this Connector instance is bound to exactly
   one tenant (infra design D2), so the code of this workspace flows only to
   this workspace's callers.
8. This supersedes the multi-perspective design's ap-mt→gateway-over-tunnel
   transport for P6 — Frederico's forwarder makes that unverified assumption
   unnecessary for delivering code answers at all (§9, credited).

## 1. The decision, in four sentences someone can disagree with

The Connector gets a **generic forwarding module** driven by a config-held
registry of forwardable upstreams, each an installed workspace app's own MCP
endpoint reached by loopback HTTP under the Connector's own credential —
codegraph is the first registry entry, not a special case in code. The caller
surface is one additive, default-off knob on the existing `knowledge_search`
tool (`include_code`), whose result lands in a structurally separate `code`
section beside the knowledge answer, per the decided P6 semantics (disjoint
key spaces, no fusion, mention pivot as the only bridge). Absence of codegraph
is a first-class, visible response state (`unavailable` + reason), not an
empty section and not an error, because this workspace's documented failure
mode is silent degradation. The forwarder is a *bounded* proxy: tool and
argument allow-lists, a per-call timeout under the edge cut, and no caller
influence over which index is read.

## 2. What is already decided elsewhere, and preserved

From `docs/design/aw-knowledgeable-multi-perspective-search.md` §§2–3, treated
as decided and not relitigated here:

- **P6 = code reality** (`codegraph_explore` / `codegraph_search` /
  `codegraph_callers`): the KG records intentions and decisions; codegraph
  records implementation. P6 disagreeing with P1–P5 is a finding (doc drift),
  not noise.
- **The two key spaces are disjoint** — KG identity is
  `(tenant, bucket, workspace_slug, source_path)` under `knowledge_base/`;
  codegraph identity is files/symbols under `repos/**`. They do not overlap,
  not even on file paths. Codegraph results are therefore **never RRF-fused**
  with KG chunks; the code answer stands beside the knowledge answer.
- **The mention pivot is the only honest bridge** — literal repo paths and
  backticked symbols extracted from KG chunks, looked up in codegraph,
  labeled "mentioned in", never "same as"; a non-resolving mention is itself
  a reported finding.

This card is **transport and surface**. Ranking, fusion and the pivot's
semantics stay owned by that design; §5 below only reserves a response field
(`code.mentions`) so the pivot has a place to land when the workflow (or a
later Connector iteration) implements it.

Context that makes this the right seam: `bulk_ingest.py`'s `SKIP_PREFIXES`
deliberately excludes `mapped_folders/repos/` and `mapped_folders/aw-workspace/`
from the corpus (~79% of documents) on Frederico's own 2026-10-10 call — *"we
are using codegraph for those documents, let's rely on it"* — and nothing has
routed a knowledgeable query to codegraph since. The corpus has a hole shaped
exactly like this forwarder, and re-ingesting the skipped code is explicitly
**not** proposed (the skip is structural and deliberate).

## 3. The registry — generic shape, codegraph as entry one

New module `knowledgeable_app/forwarding.py`. Registry lives in **app config**
(`forward_upstreams`, validated on read, resolved per call — same
read-fresh-every-call rule `mcp/client.py` already follows for its secret, so
a config save takes effect without a Tier-1 restart):

```json
"forward_upstreams": [
  {
    "id": "codegraph",
    "transport": "app-mcp",
    "app_slug": "codegraph",
    "tools": {
      "codegraph_explore": { "args": ["query", "maxFiles"] },
      "codegraph_search":  { "args": ["query", "limit"] },
      "codegraph_callers": { "args": ["symbol", "limit"] },
      "codegraph_status":  { "args": [] }
    },
    "probe": { "tool": "codegraph_status", "ttl_ok_s": 60, "ttl_fail_s": 15, "timeout_s": 5 },
    "timeout_s": 10
  }
]
```

Field semantics:

- `transport: "app-mcp"` — the only kind in v1: loopback HTTP to
  `http://127.0.0.1:{AW_PORT|9030}/api/apps/{app_slug}/mcp` with
  `X-Api-Key: $AW_WORKSPACE_API_KEY`. This is the exact URL shape and header
  every self-registering app writes into its own `mcp.json` for the gateway
  (`repos/aw-app-codegraph/codegraph_app/mcp/self_register.py:25-39`, and the
  identical modules in aw-app-notion, aw-app-tasks, aw-app-android-studio,
  …), so the endpoint contract is already proven by every gateway call that
  exists today. The only difference is the host: the gateway dials the
  workspace hostname from its own container; the Connector, being the
  workspace process, dials `127.0.0.1` — the sanctioned app-to-app loopback
  shape, with header auth instead of a `local_paths` carve-out because the
  `/mcp` routes already accept the API key via IdentityGuard.
- `tools` — **allow-list of tool names, each with an allow-list of argument
  names.** An unlisted tool or argument is rejected at the Connector boundary
  with a tool error naming the rejected field; nothing is ever passed through
  wholesale. `projectPath` (accepted by every codegraph tool, resolving "the
  nearest `.codegraph/` at or above that path") is deliberately absent: with
  it, a caller could point codegraph's reader at arbitrary directories —
  a filesystem-read primitive. Omitting it pins every forwarded call to the
  codegraph app's own configured index root (`/opt/aw-workspace`,
  `codegraph_app/plugin.py:55`). Allow-listing args (not deny-listing) also
  drops anything the gateway may inject into arguments, the same defensive
  rule `mcp/client.py`'s module docstring already states for
  `_gateway_caller_run_id`.
- `probe` — availability check, §6.
- `timeout_s` — per-forwarded-call ceiling, clamped to ≤20s (§7).

**What adding tool #2 later requires: one config entry** — any installed app
exposing the standard `/api/apps/<slug>/mcp` route is forwardable by listing
it with an allow-list. **That is a Tier-1 property, not a universal one**
(corrected 2026-10-27, card `4065bf3b-9510-81f6`): the route is one the app
*registers for itself*, so it needs `tier: inprocess` plus `routes:register`.
`src/apps/mcp_template.py`'s module docstring states the other half outright
— "**A Tier-2 container app runs no workspace-side code at all**" — so a
Tier-2 app can never have this route, and listing one with `transport:
app-mcp` yields a 404: an upstream listed and serving nothing. A Tier-2 app
declares its MCP surface in `apps/<slug>/mcp.json` instead, which is a
different transport kind and was the code change below.

A future upstream that is *not* an installed app's own `/mcp` route (a
Tier-2 app's declared endpoint, an external HTTP server, a stdio child) is a
new `transport` kind and a code change — deliberately: federating external
MCP servers is aw-mcp-gateway's job, and the Connector forwarding to the
outside world would duplicate it with weaker controls. The first such kind,
`app-mcp-json`, shipped on the card above and is designed in
`docs/design/knowledgeable-forwarder-declared-mcp-transport.md`: it stays
inside this constraint by resolving URL/headers/enabled-state from an
installed app's *own declaration*, never from registry fields a config
writer could mint.

## 4. Transport, concretely — the loopback verdict

**Decision: forward directly to the sibling app's `/mcp` route over
`127.0.0.1`; do not route through aw-mcp-gateway.**

The loop the card asks about — Connector → gateway → codegraph re-entering
the same gateway that is mid-call to the Connector — was checked against the
known behaviours and rejected on three grounds, one of which is the
zombie-caching trap:

1. **Gateway zombie-caching** (`Gateway.reload()` in
   `repos/aw-mcp-gateway/back/gateway/server.py` only re-dials changed specs;
   an `HttpUpstream` that connected once keeps serving its cached tool list
   even when the backend fails every real call — confirmed live 2026-08-19).
   Routing the forward through the gateway would make the Connector's code
   answer hostage to that cache: the gateway could claim codegraph healthy
   while every call fails, or serve a stale tool list after a codegraph
   update. The direct call gets the truth from the owning app every time.
2. **Credential**: the Connector holds no gateway client token and should not
   grow one; it already holds `AW_WORKSPACE_API_KEY` (it is the workspace
   process), which is exactly what the codegraph `/mcp` route accepts.
3. **Hops and the edge**: Connector → gateway → workspace core → codegraph is
   two extra network hops inside a call that is itself being proxied by the
   gateway, for zero added control.

**Re-entrancy / deadlock verdict: safe, with one hard rule.** The workspace
core is **not** single-worker — `Dockerfile:136` pins
`AW_WORKSPACE_WORKERS=5` (verified this session; the single-worker pin in
this system's notes is *production aw-knowledgeable's* uvicorn, a different
service that is not on this path). A loopback request from a handler can be
served by any of the five workers, including the caller's own, because every
handler on the path is async: the Connector's forward must be an **async
httpx call** (the pattern `mcp/client.py` already uses — per-call
`httpx.AsyncClient`), and the codegraph `/mcp` handler awaits its
`CodeGraphBridge` stdio child (`codegraph_app/mcp/bridge.py:105`) without
blocking. The hard rule for the Coders: **never a sync HTTP client inside the
handler** — a blocking loopback call stalls that worker's event loop and
every in-flight request on it, and with 5 workers it would fail
intermittently (only when the kernel hands the loopback connection to the
blocked worker), which is the worst kind of bug to chase.

Two notes that keep this honest:

- Each of the 5 workers runs its own codegraph plugin instance and its own
  `codegraph serve --mcp` child (Tier-1 `activate()` runs per worker — the
  runners app's `write_mcp_json` docstring documents this explicitly). The
  forwarded call lands on whichever worker accepts the connection and uses
  that worker's bridge; all five serve the same on-disk index
  (`.codegraph/codegraph.db`, WAL mode, concurrent reads safe per
  `codegraph_status` output). No cross-worker coordination is needed for
  reads.
- The gateway→Connector leg of the *outer* call is unchanged and still
  subject to the gateway's own behaviours; this design only takes the
  gateway out of the *inner* leg.

## 5. The caller surface

**Decision: one additive knob on the existing `knowledge_search` tool; no new
tool, no new mode value, no separate route.**

- `include_code: bool`, default `false` — absent, the request, response and
  wire shape are byte-identical to today (back-compat is a test, not a hope).
- `code_query: string`, optional — the code-side question when it should
  differ from the KG question (a natural-language ask vs a symbol name).
  Defaults to `query`.

With `include_code: true`, the Connector fans out **concurrently**
(`asyncio.gather`): the unchanged `POST /api/knowledge/search` call to
aw-knowledgeable, and one forwarded `codegraph_explore` call (the registry's
designated primary tool for this upstream). The response becomes:

```json
{
  "knowledge": { ...the aw-knowledgeable payload, byte-for-byte unchanged... },
  "code": {
    "status": "ok" | "unavailable",
    "source": "codegraph",
    "tool": "codegraph_explore",
    "result": "...codegraph's text content...",      // when ok
    "reason": "...probe/call error, verbatim...",     // when unavailable
    "mentions": [],                                    // reserved for the mention pivot — not populated in v1
    "elapsed_ms": 1234
  }
}
```

Why this shape and not the alternatives the card names:

- **Not a new `mode` value**: `mode` selects KG retrieval arms server-side
  (`_ARMS_BY_MODE`, `repos/aw-knowledgeable/backend/app/core/knowledge_search.py:556`);
  a non-KG value would entangle the server's clamp vocabulary
  (`clamp_params`) with something the server cannot serve — aw-knowledgeable
  cannot reach codegraph at all. `include_code` is Connector-side and
  orthogonal to `mode`.
- **Not inside `chunks`**: per §2, code results in `chunks` would be treated
  as fused evidence by every existing caller — exactly the fabricated
  comparability the multi-perspective design rules out. The `knowledge` /
  `code` split *is* that design's two-section output (§2's merge rules),
  surfaced one level earlier.
- **Not a standalone `code_search` Connector tool**: every gateway caller
  already has `aw__codegraph__*` directly; re-exposing them one-for-one
  through the Connector duplicates an existing surface. The Connector's value
  is *composition* — knowledge and code in one call, with one availability
  contract — plus reach for callers who see the Connector but not codegraph
  (a scoped MCP profile, a remote caller over the tunnel).

The wrapper that composes this lives in `forwarding.py` (or a thin
`mcp/compose.py`), wired into `_DISPATCH` in
`knowledgeable_app/mcp/http_handler.py:23-33` — **not** in `mcp/client.py`,
whose contract is deliberately "a 1:1 HTTP mapping onto aw-knowledgeable's
API" (its module docstring) and should stay that way.

## 6. Availability — "when it is installed/available", visibly

- **The probe is a real `tools/call` of `codegraph_status`** against the
  upstream's `/mcp` route, checking the response body for `isError` — never
  `tools/list` presence, and never route-mount presence. This is the direct
  lesson of the zombie-upstream incident: a tool list can be healthy-looking
  forever while every call fails. `codegraph_status` is cheap (index
  metadata; verified live this session: 3,847 files, answered in ~1s).
- **Cache**: in-process per worker, `{upstream_id: (ok, reason, checked_at)}`,
  TTL 60s on success, 15s on failure. Per-worker is acceptable for a cache
  (worst case 5 probes/min); anything stronger (a shared circuit breaker)
  would need cross-worker state and is explicitly out of scope — noted so the
  WORKERS>1 lesson is applied, not rediscovered.
- **Call path**: a fresh negative cache short-circuits to `unavailable`
  immediately; otherwise the forwarded call itself is the probe (its failure
  populates the negative cache with the real error).
- **The degraded contract**: with `include_code: true` and codegraph absent
  / not installed / probe-failed / call-failed / timed out, the response is
  the knowledge answer **plus** `code: {status: "unavailable", reason: ...}`
  — never a 500, never a silently missing `code` key, never an empty `result`
  that reads as "no code matches". A caller that asked for code always gets
  an explicit code-section verdict. (The uninstalled-app case surfaces as a
  connect/404 error on the loopback call and is worded as
  `"codegraph app not installed or not responding: <error>"`.)
- **Operator visibility**: `GET /api/apps/knowledgeable/status`
  (`knowledgeable_app/routes.py`, the app window's binding) gains a
  `forward_upstreams` list with each entry's `{id, available, checked_at,
  reason}`, so the degradation is visible in the UI and greppable by doctor
  tooling — not only inside tool responses.

## 7. Bounds — this is a proxy, so it is fenced

- **Tool allow-list + argument allow-list** (§3). Unknown tool → tool error
  naming it. Unknown argument → tool error naming it (not silently dropped:
  a silently-dropped `projectPath` would *look* honored). Values get length
  caps (`query` ≤ 2,000 chars) — these are forwarded into FTS/SQL on the far
  side; cheap insurance.
- **No `projectPath`, ever** (§3). The forwarder reads exactly one index:
  the codegraph app's configured root.
- **Timeouts**: per-forward default 10s, config-clamped to ≤20s; probe 5s.
  The forward runs concurrently with the KG call, so total wall clock stays
  governed by the existing `knowledge_search` envelope (connector timeout
  `min(45, max(30, budget/1000+10))` — 30s at the default budget,
  `mcp/client.py:651-656`), which already rides the 30s edge cut; the code
  leg must never extend it. On forward timeout: `code.status="unavailable",
  reason="timed out after 10s"` — the knowledge answer is not held hostage.
- **Authorization: the Connector's own.** Forwarded calls run under
  `AW_WORKSPACE_API_KEY` — workspace-scoped authority, same as every gateway
  call to the same route today. The caller's identity
  (`x-aw-caller-agent` / `x-aw-caller-run-id`, which the gateway attaches to
  every forwarded call — consumed today by `_caller_actor`,
  `knowledgeable_app/routes.py:93-105`) is **logged for audit** on every
  forward, and not used for authorization: there is no per-agent identity to
  authenticate on this seam (the same conclusion the infra design's D2
  reached for the KG secret — caller headers are self-asserted). The
  effective gate is therefore *what* can be asked (allow-lists), not *who*
  asks.

## 8. Multi-tenancy — stated, not hand-waved

aw-knowledgeable is multi-tenant; codegraph is one workspace-local index with
no tenant concept. The design is safe because the two never meet server-side:

- **This Connector instance is bound to exactly one tenant**: its shared
  service secret maps to one declared tenant in aw-knowledgeable
  (`KNOWLEDGEABLE_SERVICE_TENANT_ID`; infra design §10 D2 — never a
  caller-supplied header). The code section is composed **in the Connector**,
  inside this workspace; **nothing from codegraph is ever written to, fused
  into, or ranked by the multi-tenant KG service.** Another tenant's
  workspace runs its own Connector forwarding to its own codegraph. There is
  no cross-tenant path to close because no shared component ever holds code
  data.
- **Who may ask code questions**: anyone who can call this workspace's
  Connector tools — this workspace's gateway callers. For agents with shell
  access that adds nothing (they can read `repos/**` directly); the real
  statement is about callers who reach the Connector *without* filesystem
  access (scoped MCP profiles, remote sessions over the tunnel): for them,
  `include_code: true` is a new window onto this workspace's code structure
  and excerpts. That is accepted deliberately — it is the same trust domain
  as the workspace's own knowledge base, which those callers already read —
  and it is bounded by the allow-lists (no arbitrary paths, no arbitrary
  tools). If a future forwarded upstream is more sensitive than "this
  workspace's own code", the registry entry is where a per-upstream gate
  (e.g. `require_identity: true`) would attach; not built now, named so it
  is a decision and not an accident.

## 9. Reconciliation with the multi-perspective design — one transport, credited

The multi-perspective design (§4 "Reaching codegraph") routed P6 from the
ap-mt LangGraph workflow through the gateway's scoped HTTP endpoint over the
workspace tunnel, and flagged that reachability as *"the design's one
unverified assumption … verify before implementation starts."*

**Frederico's forwarder is the better shape, and it makes that assumption
unnecessary rather than verified** — this is his design improvement, recorded
as such. The Connector runs inside the workspace container, beside
codegraph's live index (`/opt/aw-workspace/.codegraph`, verified present and
fresh this session) and beside the gateway. Code-beside-knowledge therefore
ships **entirely in-workspace**: no tunnel, no 30s edge dependency for the
code leg, no ap-mt-held gateway token, and available to today's actual
callers (workspace agents) without any ap-mt work existing at all.

Reconciliation, explicitly, so nobody implements both transports:

- **Superseded**: multi-perspective §4's "Reaching codegraph" paragraph (the
  ap-mt → gateway scoped endpoint → codegraph path) and sequencing step 2
  ("verify ap-mt → gateway scoped-endpoint reachability"). That verification
  task is dropped, not inherited.
- **Still true and untouched**: everything else in that design — P1–P5
  transport (ap-mt → aw-knowledgeable directly over aw-stack), P6 semantics,
  no-fusion, mention pivot, eval tiers.
- **If/when the LangGraph workflow is built**, its P6 node calls **this
  forwarder surface** (the Connector's `knowledge_search` with
  `include_code: true`, or a registry-forwarded codegraph tool) through the
  one gateway upstream it would already use — not a second, parallel
  codegraph transport. An ap-mt→gateway-over-tunnel hop still exists *for
  that future workflow* (codegraph is in the workspace; ap-mt is not — no
  design removes that topology), but it is no longer load-bearing for
  delivering code answers to users, and if it fails the workflow degrades P6
  to `unavailable` exactly as that design already specifies.
- This file is the reconciliation record; per this card's git constraint
  (sibling architect active in this tree, commit only this file), the
  one-line "superseded by" pointer inside
  `aw-knowledgeable-multi-perspective-search.md` §4 is left as a follow-up
  edit for the implementing card.

## 10. Where it lands — files and seams

In `repos/aw-app-knowledgeable` (note: that repo's release cadence is its
own; the 2026-10-15 "stay out, release pending" caution in the
multi-perspective doc's §9.7 applied to *that* card's scope — this card's
deliverable is precisely a change to this app, sequenced normally):

- **`knowledgeable_app/forwarding.py` (new)** — registry parsing/validation,
  per-worker availability cache, `async forward(upstream_id, tool, args)`
  (allow-list enforcement → loopback POST `tools/call` → body/`isError`
  handling), and the `knowledge_search` composition wrapper (concurrent KG +
  code fan-out, response assembly).
- **`knowledgeable_app/mcp/http_handler.py:23-33`** — `_DISPATCH
  ["knowledge_search"]` re-pointed at the composition wrapper. No other
  dispatch rows change.
- **`knowledgeable_app/mcp/client.py:620-661`** — untouched logic; the
  `knowledge_search` entry in `TOOLS_SCHEMA` gains the `include_code` /
  `code_query` fields and the two-section response contract in its
  description. `client.py` stays a pure aw-knowledgeable HTTP mapping.
- **`knowledgeable_app/routes.py`** — `GET /status` gains the
  `forward_upstreams` availability block (§6).
- **`aw-app.json`** — `forward_upstreams` config key with the codegraph entry
  as the shipped default (so the feature path is exercised, not dormant);
  no new permissions needed (`net:outbound` is held; `AW_WORKSPACE_API_KEY`
  is ambient in a Tier-1 process). **Do not hand-bump `version`** — the
  release bot owns it, and the gateway only re-dials this upstream's changed
  tool schema on a version change (`x_app_version` in
  `aw-mcp-gateway/back/gateway/config.py:151-179` — added after this exact
  app's `search_graph` was invisible to live sessions on 0.2.0→0.3.0).
- **`tests/`** — see §13; the absence cases are mandatory, not optional.

Untouched: aw-knowledgeable (the service) — zero server changes;
aw-mcp-gateway — zero changes; aw-app-codegraph — zero changes (its `/mcp`
route already serves exactly what the forwarder needs).

## 11. Rejected alternatives

- **Connector → aw-mcp-gateway → codegraph** (the literal "request forwarder
  through the front door"): rejected — re-enters the gateway that is mid-call
  to the Connector (survivable: the gateway is a separate async container,
  not a lock — but pointless), inherits the documented HTTP-upstream
  zombie-caching at exactly the seam where this design needs ground truth,
  requires a gateway client credential the Connector doesn't have, and adds
  two hops. The runner-up; what would change the decision: if forwarding ever
  needs upstreams *outside* the workspace, those go through the gateway —
  which is the "new transport kind" code change of §3, by design.
- **In-process call into the codegraph plugin / shared `CodeGraphBridge`**:
  rejected — there is no sanctioned app→app facade (`AppContext` grants
  routes/secrets/db/services…, nothing like `ctx.apps.call()`); a raw import
  passes no capability gate, is invisible to the reconciler, and breaks
  silently when codegraph is uninstalled — the precise failure the
  availability contract exists to make visible.
- **Reading `.codegraph/codegraph.db` directly** (the Connector holds
  `fs:workspace-read`, and WAL mode makes concurrent reads safe): rejected —
  the SQLite schema is codegraph's private, version-coupled contract, and the
  query value (FTS5 ranking, traversal, result shaping) lives in the serve
  child, not the file; the Connector would be re-implementing codegraph to
  avoid one loopback HTTP call.
- **A new `mode: "code"` on `knowledge_search`** / **a standalone Connector
  `code_search` tool**: rejected in §5 (server clamp entanglement;
  duplication of `aw__codegraph__*`).
- **Hardcoding codegraph (no registry)**: rejected by the ask itself — *"and
  probably other tools in the future"* is the load-bearing clause; a special
  case answers the wrong question and guarantees a second, diverging
  forwarder when tool #2 arrives.
- **Per-caller authorization on forwarded calls**: rejected for v1 — caller
  headers are self-asserted (D2's finding), so "authorization" built on them
  would be theatre; the honest bound is the allow-list. Revisit only with a
  real identity on this seam.

## 12. What this makes harder later

- **A conditional response shape.** `knowledge_search` answers with the
  legacy payload or the `{knowledge, code}` wrapper depending on
  `include_code`. Every future consumer (and the multi-perspective workflow's
  P6 node) must handle both; collapsing to always-wrapped would be a breaking
  change for every existing caller. Chosen anyway: back-compat for the
  existing fleet outweighs shape purity.
- **The Connector now has a second upstream family.** Its failure surface is
  no longer "aw-knowledgeable reachable or not"; status/doctor/tests all gain
  a dimension per registry entry. The §6 status block is the mitigation, and
  it must be maintained as entries are added.
- **Coupling to codegraph's tool names and schemas.** An upstream rename or
  schema change turns into `code.status="unavailable"` with a tool-error
  reason (visible, but degraded) until the registry entry is updated. The
  registry being config means the fix is a config edit, not a release — but
  also means config drift can silently narrow the surface; the shipped
  default is the guard.
- **`127.0.0.1` + `X-Api-Key` as an app-to-app idiom.** This design leans on
  the same endpoint contract the gateway uses; if IdentityGuard's API-key
  acceptance on app `/mcp` routes ever changes, the forwarder breaks together
  with the gateway (loudly, at probe time) — aligned failure, but a second
  dependent on that contract.
- **Per-worker availability caches** bake in "cache only, no shared state."
  A future shared breaker/rate-limiter needs `ctx.state.kv` or similar — the
  refactor is bounded but real (the WORKERS>1 family of bugs says: declare
  it now).

## 13. Risks for the Coders — the non-obvious ways this breaks

1. **Optional-config-gated paths ship untested and crash on first real use**
   (this workspace's recorded pattern). Mandatory tests: registry present +
   upstream up (happy path); registry present + upstream absent (uninstalled
   app → explicit `unavailable`, KG answer intact); probe-fail + negative-TTL
   short-circuit; disallowed tool; disallowed arg (incl. `projectPath`
   specifically); forward timeout; `include_code` omitted → byte-identical
   legacy response. The shipped default config includes codegraph precisely
   so the forward path is not dormant in production.
2. **Async or nothing** (§4): the forward and the probe must use
   `httpx.AsyncClient`. A sync client "works in dev" and stalls a worker's
   event loop in production, intermittently, only when the kernel routes the
   loopback connection back to the blocked worker.
3. **Never gate on `tools/list`** (§6): the probe and any health check must
   be a real `tools/call` with the body inspected — the zombie-upstream
   incident false-greened on exactly the softer check.
4. **Concurrent fan-out, not serial** (§7): a serial KG-then-code sequence
   adds the code timeout to a call that already rides the 30s edge; the
   existing connector timeout math (`client.py:651-656`) must remain the
   envelope.
5. **5 workers, 5 `activate()`s** — no module-global mutable registry built
   at import/activate time; resolve config per call (the `client.py`
   `configure()` resolver pattern). Also: 5 codegraph bridge children exist;
   that's the codegraph app's existing shape, don't "fix" it from here.
6. **Tool-schema change needs a version bump to reach live sessions** — the
   gateway re-dials this upstream only when the spec-plus-app-version
   changes. The release bot owns the bump; do not hand-edit `version`, and do
   not ship with `[skip release]`.
7. **Tier-1 deploy reality**: the Connector has no restart path short of a
   core restart, and restarting core from inside a run kills the run. Land
   code, then schedule the restart deliberately.
8. **Codegraph can be mid-index or OOM-recovering** (its plugin records an
   OOM-kill signature for full indexes); slow or erroring calls are
   `unavailable` with the verbatim reason — never retried in a loop from the
   Connector (the per-call timeout is the bound; retry policy belongs to the
   caller).
9. **`code.result` size**: codegraph_explore responses run to tens of KB;
   cap forwarded result text (e.g. 32KB with an explicit `truncated: true`
   marker) so the composed response doesn't blow the tunnel's practical
   payload limits for remote callers.

## 14. Verified live vs assumed

Verified live, this session (2026-10-16):

- `/opt/aw-workspace/.codegraph/` exists and is live: `codegraph.db` + WAL +
  running daemon files (`ls`, this session).
- `codegraph_status` answered through the gateway: 3,847 files / 79,417 nodes
  indexed, latest file indexed 2026-10-16T08:46Z, WAL journal mode. Multiple
  `codegraph_explore` calls also served (this doc's citations came from
  them) — i.e. the full gateway → `/api/apps/codegraph/mcp` → bridge → child
  path, including its `X-Api-Key` auth, is proven working **end-to-end
  today**, by this very session's tool calls.
- `AW_WORKSPACE_WORKERS=5` in the workspace core `Dockerfile:136` (grep,
  this session) — the multi-worker analysis in §4 rests on this, not on an
  assumption of single-worker.
- All cited code read from disk this session: the Connector's `plugin.py`,
  `routes.py` (incl. `_caller_actor` at :93-105), `mcp/http_handler.py`,
  `mcp/client.py`, `mcp/self_register.py`; codegraph's `plugin.py`,
  `mcp/bridge.py`, `mcp/self_register.py`; the gateway's `upstream.py`
  (`_client_headers`, `call_tool` retry taxonomy) and `config.py`
  (`scan_app_mcp_servers` / `x_app_version`).

Assumed (named, with the evidence that makes each a small assumption):

- **The Connector's loopback POST to `/api/apps/codegraph/mcp` with
  `X-Api-Key` succeeds from inside the workspace process.** Not directly
  executed (this agent runs in a sibling container, not the workspace
  process). Evidence: the gateway calls the identical route with the
  identical header from a *different* container successfully (every codegraph
  call this session), and IdentityGuard's API-key acceptance is
  host-agnostic; the established app-to-app loopback pattern
  (devctl, mini-browser→proxy) proves `127.0.0.1:{AW_PORT}` reaches mounted
  app routes. First implementation step: a 5-line probe from the workspace
  container before building on it.
- **`codegraph_status` stays cheap under load** — observed ~1s once; not
  load-tested. The 5s probe timeout bounds the damage if wrong.
- **The uninstalled-codegraph error shape** (connect error vs 404 from the
  unmounted route) — asserted from the runtime's mount model, not observed;
  the test matrix in §13 covers both shapes.
