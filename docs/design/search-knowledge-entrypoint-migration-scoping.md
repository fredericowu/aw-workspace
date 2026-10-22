# search-knowledge as the production knowledge entry point — what the migration actually is, and how to split it

Status: **scoping analysis — recommends a PO-scoped epic; changes nothing on
the in-flight P6 card.** Architect run `c968741c8f8a41dabf9d54484128175b`,
2026-10-22, card `4015bf3b-9510-81d6-b3bb-f2f3f11d1a64`. Answers Frederico's
direction (verbatim, PT): *"o plano eh migrar pra ele, ele vai ser a porta de
entrada, ele vai ter o fluxo de gestão do conhecimento … não quero replicar a
gestão de workflow dentro do knowledgeable."* Follows
`docs/design/search-knowledge-codegraph-p6-worker.md` (master `6975c28`),
whose flagged question #2 this is the answer to.

## 0. The headline

**Frederico's direction ratifies the architecture shipped this week; it does
not reopen it.** Every transport chosen on this target survives the migration
unchanged — the retriever arms' direct REST calls, the scoped read-only key,
the `transport=http` McpServer row, the gateway-profile→Connector-forwarder
path for P6. What migrates is **who orchestrates**: planning, fan-out
composition, synthesis and source-routing move from *inside* aw-knowledgeable
(where they live as two awkward AP-MT call-outs) to the AP-MT workflow. The
work is: fork two prompts, define a result contract, measure, then walk a
long-tail caller migration — not rebuild anything.

**And the current P6 card proceeds unchanged.** Every piece it builds
(Connector `code_only`, the McpServer row, `knowledgeable-retriever-code`,
the fourth worker node) is a building block of the end state. The only edit:
the workflow-description update in that design's §2d should describe the
workflow as *transitional* ("observability mirror → production entry point,
see this doc"), not as a mirror forever.

## 1. What happens to `/api/knowledge/search` and the two frozen agents (Q1)

**The route is not deprecated — it is demoted to the retrieval primitive,
permanently.** The structural fact: the retrieval engine — the three arms and
RRF fusion — is **in-process code** in aw-knowledgeable
(`core/knowledge_search.py`, `core/fusion.py`), sitting on pgvector/FTS/Neo4j
access that AP-MT does not have and must not grow (that would be the exact
duplication Frederico refuses, mirrored). The workflow's own workers *are
callers of this route* (`mode=<arm>`, `sub_queries=<plan>`,
`synthesize=false`). The entry point can move; the engine cannot.

What the route loses over the migration is its **two orchestration
features** — which are precisely its two call-outs *into AP-MT*, i.e. the
circular dependency Frederico wants gone:

1. **The internal planner invocation** — fires only when a caller omits
   `sub_queries` (`_provided_plan`, `core/knowledge_search.py:257` vs the
   agent call path), costing the measured 13–19s runner-door hop. End state:
   the route defaults `sub_queries=[query]` (the `_fallback_plan` shape) and
   never calls an agent. Planning is the workflow orchestrator's job.
2. **`synthesize=true`** — today the **default** (`api/knowledge_search.py:37`),
   each such call paying the `_synth_config()` AP-MT callback (`:62`). End
   state: default flips to `false`, then the parameter is deprecated and the
   callback deleted. Synthesis is the workflow synthesizer's job.

After both: aw-knowledgeable makes **zero** calls into AP-MT. The
single-worker production service stops being latency-hostage to an agent
platform; the AP-MT⇄KG circularity (synthesis callback, planner hop) that two
design docs had to engineer around disappears structurally.

**The frozen agents** (`knowledgeable-search-planner`,
`knowledgeable-playground`): frozen *while* the internal pipeline still
serves callers; **dead weight after** — once no caller reaches the route
without `sub_queries` and `synthesize` is gone, nothing invokes them, and
they are retired (deleted, not kept in sync with anything). Their prompts
live on as the *initial copies* of the workflow-owned forks (§2).

## 2. The workflow-owned planner: the rejection inverts, on a schedule (Q2)

The P6 design rejected a workflow-owned planner because the workflow was a
mirror and the prod prompt was load-bearing elsewhere. Frederico's answer
flips the premise, so the decision flips — **but as fork-not-edit, and not on
the current card**:

- **During the migration window** (both pipelines live), the freeze stands
  verbatim: nobody edits `knowledgeable-search-planner` or
  `knowledgeable-playground`, because production `/api/knowledge/search`
  still depends on them.
- **The epic's first build step forks them**: new workflow-owned agents
  (e.g. `sk-planner`, `sk-synthesizer`), prompts cloned from the frozen ones,
  and the workflow's orchestrator/synthesizer nodes re-pointed
  (`update_workflow`, graph `orchestrator.agent` / `synthesizer.agent`). From
  that moment the workflow evolves freely and the frozen originals decay
  toward retirement with the internal pipeline.
- **No sync machinery.** Divergence is the *point*, not drift: the forked
  planner is where source routing grows — emitting, beside `sub_queries`, a
  routing block (`{code: {answerable, code_query}}`, later per-source flags
  for whatever `forward_upstreams` gains). Keeping the fork synced to a
  prompt scheduled for deletion would be maintaining dead weight.
- **The P6 self-gating worker is the v1 gate and stays.** When the forked
  planner emits a code-routing flag, the worker's prompt gains one line —
  "obey the PLAN's code flag when present; judge yourself when absent" — a
  graceful handover, not a rework. The gate's *location* (worker vs planner)
  is an epic decision; the worker ships now either way.

## 3. What "migrate to it" means for existing callers (Q3)

Callers of `knowledge_search` today: every workspace agent and skill via the
Connector MCP tool (the mandatory KB-search step in every dispatch), AP-MT's
own three retrievers via direct REST, and possibly the aw-knowledgeable UI
(grep of `frontend-react/src` for the route found no caller — unverified,
the epic's inventory step must settle it).

**Be honest about the cost shape.** Today's one-hop MCP call with
`synthesize=false` and provided/defaulted `sub_queries` is in-process
retrieval — no LLM at all. The workflow is minimum five agent completions
(plan + 3 arms + synthesis; six with the P6 worker) orchestrated per query.
The arms are parallel API-direct nano calls, so this is seconds-and-cents,
not the 13–19s runner-door scale — but it is strictly more than the
primitive, **and the workspace's highest-volume search caller is the
mandatory KB search on every agent dispatch**. Migrating *that* to a
six-agent workflow would multiply platform cost by every agent turn in the
workspace.

So "porta de entrada" has to mean, concretely:

- **The workflow is the canonical entry point for *answered* knowledge
  queries** — anything that today sets `synthesize=true` (the REST default!)
  or wants multi-perspective composition (P6 included). These callers were
  already paying LLM synthesis; the delta is the planner + arm agents.
- **The primitive remains the sanctioned surface for raw retrieval** —
  `synthesize=false` chunk fetches, which is what the workflow's own workers
  do and what the mandatory per-dispatch KB search should stay on. This is
  not a grandfathered legacy path; it is the engine's API, used *by* the
  entry point.
- **The invocation pipe for workspace callers already exists**: the runners
  app exposes `workflow_search_knowledge` through the gateway today. The epic
  must decide the facade question — whether the Connector's `knowledge_search`
  MCP tool transparently dispatches the workflow when `synthesize=true`
  (keeping one tool name for the fleet, at the cost of a
  Connector→AP-MT→gateway→Connector re-entrant loop when P6 fires — bounded
  and async, but it must be stated), or whether synthesized search becomes an
  explicitly different tool and callers migrate by edit. I lean facade
  (10,000 call sites don't exist, but every agent prompt and skill that says
  "call knowledge_search" does); the latency measurement decides it.
- **Auth moves nothing**: workspace→gateway→runners→AP-MT and
  AP-MT→aw-knowledgeable (scoped key) are both live today.

**One non-negotiable before any cutover: measure.** The workflow was built as
an observability mirror — use it as its own yardstick. Fixed query set,
side-by-side: internal pipeline (today's default path) vs workflow run —
wall-clock, dollar cost, answer quality. No caller migrates until those
numbers exist. This is epic phase 0 and it needs zero new code.

## 4. Card or epic? (Q4)

**Epic, PO-scoped — plainly bigger than a card, and not mine to slice alone.**
It changes a production REST contract's default behavior
(`synthesize: bool = True` today), retires two production agents, decides the
fate of the fleet-wide `knowledge_search` tool every agent prompt references,
and needs acceptance criteria (latency/cost/quality thresholds) that are
product calls. The PO should scope it; suggested phase structure, each phase
a card:

- **P0 — measure.** The side-by-side above. No code.
- **P1 — fork.** `sk-planner`/`sk-synthesizer` cloned from the frozen
  prompts; workflow re-pointed. Freeze on the originals continues.
- **P2 — contract + routing.** The workflow's result contract (today it
  returns `{plan, workers[], synthesis}` — `orchestrators/__init__.py:201` —
  which is an observability shape, not an API); planner-owned source routing
  absorbing the P6 gate; the facade-vs-new-tool decision from §3.
- **P3 — demote the primitive.** Flip `synthesize` default, deprecate it,
  default `sub_queries=[query]`, delete `_synth_config` and the planner
  invocation path. aw-knowledgeable makes zero AP-MT calls.
- **P4 — retire.** Delete the frozen agents; update
  `aw-knowledgeable-multi-perspective-search.md` and the forwarder doc's §9
  pointers (their transport decisions all still hold — only "who orchestrates"
  paragraphs change).

**What the Coder on the current card keeps vs holds: keep everything, hold
nothing.** All four build steps of `search-knowledge-codegraph-p6-worker.md`
§2 proceed as written; the one wording change is §0's "transitional" note in
the workflow description. Blocking approved P6 work on an epic that starts
with a measurement phase would invert the dependency — the epic *needs* the
P6 worker in place to measure the full multi-perspective shape.

## 5. What this direction makes harder later (named now, decided in the epic)

- **Two search planes during the window.** Until P3 lands, a question can be
  answered by the internal pipeline or the workflow with different plans and
  different synthesis — support burden ("which path produced this answer?")
  that the run-tree observability mitigates but doesn't erase.
- **AP-MT becomes availability-critical for knowledge.** Today, if AP-MT is
  down, `knowledge_search(synthesize=false)` still works end-to-end. After
  migration, synthesized search dies with AP-MT. The primitive remaining
  callable is the designed degradation path; the facade (if chosen) should
  fall back to it explicitly rather than erroring.
- **The workflow result contract becomes a public API.** Freezing
  `{plan, workers[], synthesis}` or versioning it is a P2 decision with
  long shadows — the current shape leaks node ids and worker labels that
  were chosen for observability, not for consumers.
- **The re-entrant loop** (Connector facade → AP-MT → gateway → Connector
  `code_only` for P6) puts the Connector on both ends of one logical call.
  Async and bounded (different tools, 5 workers), but it is the kind of
  topology that makes a future incident harder to read; P2 should draw it.
