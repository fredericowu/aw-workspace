# AP-MT workflows: `http` gate node — outbound call + conditional stop, no LLM

**Card:** `4135bf3b-9510-81aa-9334-f286df6b6851` — "Workflow engine: new
\"http\" (lambda-style) node kind — gate/decide without an LLM hop".
**Architect run:** `e2c4f15bcb2140a39acce2dece717c02`, 2026-11-09.
**Status:** proposed — goes to a Coder.

Companion to `docs/design/ap-mt-workflow-dag-transform-nodes.md` (2026-10-23,
shipped as AP-MT `56f10a9`). This card is that design's named deferral
trigger firing — with one redirect: the deferred idea was a `tool` (MCP) node,
but there is **no routable network path from AP-MT's backend to the workspace
MCP gateway** (verified live 2026-10-22, Coder run `d0532eaaaa2740ce9dbbdc5
aaafea197`), so what ships is a direct-HTTP node instead, matching the
transport precedent in `docs/design/ap-mt-knowledgeable-direct-rest-tool.md`.

Motivator (Frederico, Telegram 2026-11-09): a cadence-dispatched workflow
today pays a full LLM agent session even when there is no work — e.g. the
Kanban sweep against an empty board. The first node should be able to call an
API ("we can call the Notion API itself") and decide whether the graph
continues, with the skip costing zero tokens, zero hops, and **not** reading
as a failure.

## 1. The decision

Add **one new dag node kind, `kind: "http"`**, beside `transform`: a
declaratively-described outbound request through a **named, server-registered
connection**, plus an optional **JMESPath `condition`** over the response
envelope.

- When `condition` evaluates **truthy**, the graph continues normally.
- When it evaluates **falsy**, the node itself still ends `ok` (it did its
  job); `run_dag` records a **graceful gate-stop**: every transitive
  dependent is `skipped` (NOT error-tainted), the run ends
  `status="success"`, and the output carries a top-level `gated` field saying
  which node stopped the graph and why.
- A request **failure** — timeout, network error, non-2xx, oversized
  response, unresolvable connection — is a node **error** (tainted), exactly
  like a `TransformError`. "Could not check" must never masquerade as
  "nothing to do"; silent degradation is this platform's documented failure
  mode.

Two halves, deliberately separable because they carry different risk:

- **(A) additive:** the node kind, the connection registry, save-time
  validation, the dispatch branch. Touches no existing graph's behaviour.
- **(B) shared-executor change:** the gate-skip rule in `run_dag` plus a new
  `node_skipped` run event. Every live dag (`search-knowledge`,
  `literature-review`) shares this code path.

## 2. Node schema (pure data, validated at save time)

```json
{"id": "check", "kind": "http", "label": "Any stale cards?",
 "connection": "notion",
 "method": "POST",
 "path": "/v1/databases/<database-id>/query",
 "body": {"filter": {"property": "Tags",
                      "multi_select": {"contains": "http-gate-demo"}}},
 "condition": "length(json.results) > `0`",
 "timeout_s": 10,
 "max_bytes": 524288}
```

- `connection` (required): a name resolved by the server-side registry (§3).
  The graph never contains a URL host or a credential.
- `method`: `GET` | `POST` only in v1.
- `path` (required): must start with `/`; rejects `://`, whitespace, `..`,
  `\\`. Defense-in-depth only — the SSRF boundary is that the host is never
  author-controlled at all (the C1 QA finding on aw-knowledgeable's
  `_callback_url_rejection` showed hostname *validation* is exactly where
  SSRF bypasses live; here there is no hostname to validate).
- `body` (optional): a static JSON object/array. **No templating from
  upstream state or user input in v1** — that is an injection surface the
  motivating case doesn't need. Named deferral: the first workflow that
  needs a dynamic query.
- `condition` (optional): JMESPath, compiled at save time. Evaluated against
  the response envelope (below); decision is `continue` if the result is
  truthy (Python truthiness), `stop` otherwise. Omitted → plain fetch node,
  never gates.
- `timeout_s`: 1–30, default 10. `max_bytes`: default 512 KiB, hard cap
  2 MiB. One request, one decision — retry/backoff/polling are explicitly
  out of scope (PO).

**State value / condition input envelope:**
`{"status_code": 200, "json": <parsed body or null>, "text_head": "<first
500 chars, only when json is null>"}`. Stored in dag `state[nid]` so
downstream transforms/agents can consume it like any other node value.

## 3. Connection registry — in code for v1, one entry

New module `backend/app/core/orchestrators/http_connections.py`:

```python
CONNECTIONS: dict[str, Connection]   # name -> (base_url, resolve_headers)
```

v1 ships exactly one entry, `"notion"`: `base_url =
notion_direct.NOTION_API`'s host (`https://api.notion.com`), headers =
`Authorization: Bearer <token>` + `Notion-Version` imported from
`core/notion_direct.py:62-63` (don't restate the version string). The token
resolution **reuses** the `WorkspaceNotionToken` lookup that
`kanban_writer.tenant_notion_client()` already performs
(`backend/app/core/kanban_writer.py:170-233`) — factor the row-select +
`secret_crypto.decrypt` into a shared helper (e.g.
`resolve_tenant_notion_token(tenant_id)`) that both call sites use, rather
than copying it. No row / undecryptable row → the http node **errors** with
"connection 'notion' is not configured for this tenant" (loud, per §1; note
this is deliberately NOT kanban_writer's silent proxy fallback — there is no
proxy to fall back to here).

Why in-code and not a DB table: the only live credential is already a
derived, reconciled row (`WorkspaceNotionToken`'s docstring: nothing in this
service's UI may edit it), and a generic connections CRUD needs its own
encryption/reconcile/UI story for exactly one consumer today. Same call as
`ap-mt-knowledgeable-direct-rest-tool.md` §3 item 4 ("per-tenant credential
rows — scope creep now"). The node schema is registry-shape-agnostic:
`connection: "<name>"` resolves identically whether names come from a dict
or a table, so a DB-backed registry is a pure fast-follow. **Named trigger:
the second connection.**

**PO's rejected alternative stays rejected:** a fixed set of built-in check
kinds (`kanban_count`, …) is a cabinet of one-offs needing an AP-MT deploy
per new check. Here, new *checks* ship as workflow edits (path/body/
condition are data); only new *providers* need code.

## 4. Execution — `_run_http_node`

Mirrors `_run_transform_node`
(`backend/app/core/orchestrators/__init__.py:369-392`):

1. `node_start` event `{label, kind: "http"}` with `node_id` — graphFlow's
   replay lights the node for free (`frontend/src/lib/graphFlow.tsx:148-171`).
2. `httpx.AsyncClient(follow_redirects=False, timeout=timeout_s)` — redirects
   are refused so a 3xx can never re-point the `Authorization` header at
   another host; stream the response and abort past `max_bytes`.
3. Non-2xx (3xx included) → `HttpNodeError(method, path, status_code)`.
4. Parse JSON best-effort → envelope; evaluate `condition` when present.
5. `node_end` event `{label, method, path, status_code, ms, bytes,
   decision: "continue"|"stop"|null, condition}` — this is the "states what
   it fetched and decided" evidence the PO requires on the timeline.
6. `state[nid] = envelope`; return the decision to `_run_one`.

**No hop charge, no token charge** — same justification as `transform` (the
dag is finite and acyclic by save-time validation, so outbound calls per run
are bounded by graph size; the hop budget exists to bound LLM dispatch, and
there are no tokens, which is the entire point of the card).

`HttpNodeError` is handled in `_run_one` exactly like `TransformError`
(`__init__.py:446-452`): node `error`, tainted, structured `error` event.
Error text carries method + path + status + a ≤200-char body snippet —
**never** headers, never the token.

## 5. Gate semantics in `run_dag` — the (B) half

The one shared-semantics change, kept minimal:

- New local `gated: set[str]` in `run_dag` (`__init__.py:395`). In
  `_run_one`'s http branch, `decision == "stop"` → `gated.add(nid)`;
  `details[nid] = {"decision": "stop", "status_code": ...}`. The gate node's
  own status stays `"ok"` and it counts in `completed`.
- The skip rule (`__init__.py:430`) grows one clause:
  `any(status.get(d) in {"skipped", "error"} or d in gated for d in needs)`.
  Gate-skips are **not** added to `tainted`, so the failed-sinks rollup
  (`__init__.py:516-521`) is untouched and the run stays `success` — the
  exact inverse of the search-knowledge failed-sink case (card
  `4055bf3b-9510-8179`), and the same graceful-stop precedent as
  `limit_reached` (`executor.py:3465-3480`) and `NodeError`'s own docstring.
  Transitive propagation needs no new code (dependents of a skipped node
  already skip).
- **Every** skip — error-, budget-, and now gate-caused — publishes a new
  `node_skipped` event `{label, reason: "dependency <id> errored|skipped|
  gated"}` with `node_id`. Today a skipped node publishes nothing and renders
  as idle/pending forever, which fails the PO's "downstream agent node shows
  SKIPPED" acceptance. Additive event kind: `deriveNodeStateFromEvents`
  (`graphFlow.tsx:158-171`) and the RunDetail replay switch ignore unknown
  kinds, so old runs and old frontends degrade to today's behaviour; the
  frontend change is one new branch mapping `node_skipped` → a muted
  "skipped" state.
- Run-level: when `gated` is non-empty, `out["gated"] = {"node": "<id>",
  "message": "gate '<label>' stopped the graph: condition evaluated false"}`
  plus a bus `log` event with the same sentence. `run_workflow`'s status
  logic (`executor.py:3476-3480`) reads `final.get("error")` only — `gated`
  does not trip it, so `Run.status = "success"`.

**Zero-cost evidence is structural, not asserted:** a gated Run A has zero
child runs, so the rollup at `executor.py:3484-3489` records
`tokens_in = tokens_out = 0` on the Run row, and the terminal
`node_end __workflow__` event carries the same zeros plus a `budget`
snapshot with 0 hops. That is the measured number the card demands.

## 6. Other seams that must learn `http`

- `derive_kind_from_graph` (`__init__.py:65`): the dag predicate becomes
  `n.get("needs") or n.get("kind") in {"transform", "http"}`. Without this a
  single-node http graph derives `sequential` and crashes in `_run_node`
  (`KeyError: 'agent'`).
- `_validate_dag_graph` (`backend/app/api/workflows.py:52-75`): a
  `kind == "http"` branch calling a new `validate_http_node(node)` (sibling
  of `validate_steps`, living in `http_connections.py`): connection name in
  registry, method whitelist, path shape, `jmespath.compile(condition)`,
  timeout/max_bytes ranges, body is a JSON object/array. The
  `elif not n.get("agent")` fall-through at `:74` must exempt `http` as it
  exempts `transform`.
- `_rendered` (`__init__.py:486`): `kind != "transform"` becomes
  `kind not in {"transform", "http"}` so an http sink renders its structured
  envelope instead of `""`.
- Tenant threading: **do not rely on the tenant contextvar.** `run_workflow`
  has `wf.tenant_id` in hand (`executor.py:3313-3352`); thread it explicitly
  as an additive kwarg through `dispatch` → `run_dag` → `_run_http_node`.
  Background paths silently losing the tenant is this repo's most-bitten
  trap (`kanban_writer.py:170` docstring says so in those words).
- `WorkflowEdit.tsx` needs **nothing** for v1: `EditableGraphNode` carries an
  index signature and save re-parses the raw JSON text
  (`frontend/src/routes/WorkflowEdit.tsx:18`, `:356`), so the new node
  fields round-trip a UI save intact. Authoring UI is the PO's fast-follow.

## 7. Demo workflow + deterministic Run B

Created **as data** via `create_workflow`/`update_workflow` (how both live
dags were built; zero UI work). Slug `kanban-gate-demo`:

- `check` — http node as in §2: queries the tenant's Kanban database
  (database id from the tenant's `NotionSyncConfig.kanban_database_id`,
  baked into the workflow JSON as author data; verify the exact property
  name/type with `get_kanban_properties` before baking the filter) for cards
  tagged `http-gate-demo`; condition `` length(json.results) > `0` ``.
- `work` — agent node, `needs: ["check"]`, cheapest agent available (e.g.
  `echo-coder`), `input_template`: "The gate passed. Reply with one line."

**Run A (condition false):** no card carries the tag — gate lights up,
`node_end` shows what it fetched and `decision: "stop"`, `work` shows
skipped, Run row reads `success` with `tokens_in = tokens_out = 0`.
**Run B (condition true, forced deterministically):** tag any scratch card
with `http-gate-demo` (one `set_kanban_property` call), click Run again —
gate passes, `work` actually runs. Untag afterwards. No waiting for a real
stale card.

Evidence bar (PO): screenshots of both run graphs from the same workflow +
the Run record's zero token counts for Run A, measured not asserted.

## 8. Step-0 confirmations (cheap; blockers if false, not workarounds)

1. **Outbound reach:** one `curl https://api.notion.com/v1/users/me` (any
   status is fine, it proves routing) from the AP-MT backend container.
   Near-certain, but the existing board writes may always have used the
   workspace proxy, so it has never been *proven* from this container.
2. **Credential:** a `workspace_notion_tokens` row exists for this tenant
   (`GET /api/runners/notion-token` fingerprint, or one SELECT). If absent,
   the aw-app-notion settings push populates it — raise as a blocker, don't
   work around.

## 9. Rejected alternatives (so they aren't re-litigated)

1. **Built-in check kinds** (`kanban_count`, `http_get`, …) — rejected by
   the PO; a deploy per new check violates the "no per-workflow Python"
   principle the transform design established.
2. **Edge condition** (`when:` on `needs`) instead of a gate node — invisible
   on the run timeline; the S2 knowledgeable-nl-adapter §17 knob 3 finding is
   kept even though its rejection rationale (keyword heuristic not worth a
   shared-executor change) doesn't apply here.
3. **MCP `tool` node** (the transform doc's original deferral) —
   structurally blocked: no network path AP-MT → workspace gateway; also
   heavier (MCP session per run, auth config in graph JSON).
4. **Separate pure `gate` node kind** (http fetches, a second node decides) —
   the runner-up. More composable (could gate on transform/agent output),
   but splits Frederico's evidence ("what it fetched and decided") across
   two timeline nodes and doubles the authoring surface. The run_dag
   mechanism deliberately keys the `gated` set by node id, not by node kind,
   so a future standalone gate kind reuses it unchanged.
5. **A new `Run.status` value** (`gated`/`noop`) — touches every Run-status
   consumer (UI chips, retro scoring, sweeper, github_sync) for a state the
   `limit_reached`-on-success precedent already models.
6. **DB-backed connections table in v1** — §3; named trigger: the second
   connection.
7. **Request templating from state/input** — §2; named trigger: the first
   workflow needing a dynamic query.

## 10. What this makes harder later

- A second connection/provider requires an AP-MT deploy until the DB
  registry fast-follow lands.
- A true two-way branch (else-arm running a *different* subtree on `stop`)
  still doesn't exist; when it comes it needs edge grammar and will have to
  reconcile with the visible-gate rule rather than replace it.
- `node_skipped` becomes a third per-node event kind every future timeline/
  graph feature must respect.
- The `success + output.gated` convention means anything that
  programmatically consumes workflow results — **especially the future Plan
  cadence integration, which is this card's own follow-up** — must check
  `output.gated`, not just `status`, to tell "ran" from "gated". Write that
  on the follow-up card when it's opened.

## 11. Risks for the Coders

- `derive_kind_from_graph` omission is a *silent* misroute (sequential), not
  an error — cover it with an explicit test.
- Non-2xx must stay a node error. Letting the condition evaluate over an
  error response (`json: null` → falsy → "stop") would mask an outage as a
  healthy empty board — the exact failure the PO called worse than the cost
  problem.
- Never put headers or the token in any event, log line, or error string.
- Regression pins for the (B) half: existing dag tests green unmodified;
  a sink skipped **via gate** does not set `out["error"]`; a sink skipped
  via a *tainted* upstream still does; budget-skip still reads success.
- Tests use `httpx.MockTransport` (no new dependency), beside
  `test_orchestrator_dag.py` / `test_orchestrator_transforms.py` /
  `test_workflows_dag_validation.py`.
- Suggested sequencing: **PR1 = (A)** node kind + registry + validation +
  dispatch (no `condition` yet), **PR2 = (B)** condition + gated-skip +
  `node_skipped` + the one-branch frontend change, **PR3 = demo as data +
  live Run A/B evidence** (no code). One combined PR is acceptable if the
  commits stay separable — the split is about reviewability of the
  shared-executor diff, not shipping cadence.
