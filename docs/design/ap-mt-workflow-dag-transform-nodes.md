# AP-MT workflows: a generic deterministic node kind ("transform") and a DAG graph shape

**Card:** `4025bf3b-9510-818f-ab3a-d6626fb539b6` — "AP-MT workflows: vocabulário
genérico de passo determinístico no JSON (sem código por workflow)".
**Motivator:** search-knowledge run `6a93966f` took 17+ min because each
retrieval worker (`openai-gpt-5-4-nano`, ~13 tok/s) is instructed to re-emit
the raw `knowledge_search` JSON verbatim (agent system prompt, step 5:
"Your entire reply is the tool's raw JSON result, verbatim") — the only way
today for structured data to reach the next node's `{prev}`.
**Constraint (Frederico, Telegram 23/10, verbatim in the card):** the workflow
JSON must contain everything the workflow needs; no per-workflow Python in
AP-MT; deterministic steps expressed in a definition language; the workflow
exportable, AP-MT only interprets. Plus: dedup of documents/chunks at each
retrieval step AND again before synthesize.

## 1. The decision

Add **one new node-level field `kind` (`"agent"` default, `"transform"` new)**
and **one new graph shape, `dag`** (nodes with `needs: [ids]`), to the
existing orchestrator dispatcher. A transform node runs **in-process, no LLM,
no runner**, executing a short list of `steps` — a fixed vocabulary of **three
ops** (`get`, `parse_json`, `dedup`) whose addressing language is
**JMESPath**. Agent nodes gain a structured output envelope: the API-direct
agent loop captures full tool results (`AgentResult.tool_results`), so a
transform can pick a previous node's raw `tool_result` and hand it to the next
node's `{prev}` with zero LLM regeneration.

The five existing graph shapes and the `{input}`/`{prev}` templating are
untouched (requirement 4: additive). search-knowledge is rewritten as a `dag`
— pure data, exportable, no slug special-casing anywhere in the engine.

Why a new graph shape at all: requirement 1 demands a transform node "usable
in ANY workflow, interpreted uniformly". The fixed `orchestrator_worker`
shape (`orchestrators/__init__.py:163-201`) has no position where a second
node can sit inside a retrieval arm — dedup-per-arm forces either per-shape
hooks (`workers[i].post`, `graph.pre_synthesis` — two new ad-hoc mechanisms,
invisible as nodes, no per-node run events) or a shape where nodes sequence
freely. The DAG is the uniform answer, and it is what the React Flow editor
natively renders anyway (`frontend/src/lib/graphFlow.tsx` builds nodes+edges
for every shape already).

## 2. Where it lands (real files)

AP-MT repo (`repos/agents-platform-multitenant`):

1. **`backend/app/core/agent_loop.py`** — `AgentResult` (line 19) gains
   `tool_results: list[dict] = field(default_factory=list)`; the
   `on_tool_end` branch (line ~153) appends
   `{"name": ..., "content": <full text content>}` alongside the existing
   1500-char-truncated `tool_result` *event* (events stay truncated —
   `run_events.payload` is plain `json`, don't bloat rows). Content
   extraction must unwrap ToolMessage / content-block lists properly —
   today's `_safe_str(output)` repr would break `parse_json` (see §6).
   Cap per-result capture at 512 KB.
2. **`backend/app/core/executor.py:2282-2298, 2704`** — thread
   `res.tool_results` into `run_agent`'s return dict (`"tool_results"` key,
   `[]` on the CLI-subshell path — passthrough is an API-direct-agent
   feature; CLI harness tools are invisible to the executor, documented
   limitation).
3. **`backend/app/core/orchestrators/transforms.py`** (new, ~120 lines) —
   `run_steps(initial_value, steps) -> value`, the whole op vocabulary.
   Pure functions, no I/O. `jmespath` added to `pyproject.toml` (pure
   Python, ~tiny, spec at jmespath.org).
4. **`backend/app/core/orchestrators/__init__.py`** —
   - `derive_kind_from_graph` (line 15): a `nodes` graph where any node has
     `needs` or `kind == "transform"` resolves to `"dag"` (checked before the
     `concurrency` disambiguation at line 34).
   - new `run_dag(graph, user_input, run_id, run_agent, *, root_run_id)`:
     topo-order over `needs`; ready nodes run concurrently
     (`asyncio.gather` per wave is enough — no need for a scheduler); state
     is `{node_id: envelope}`. Agent nodes reuse `_run_node` (line 75)
     unchanged — hop + token charging stays as is. Transform nodes run
     in-process: emit `node_start`/`node_end` with `node_id` (so
     `graphFlow.tsx`'s event replay lights them up for free), **no hop
     charge, no token charge** (DAG is finite and acyclic; validation
     rejects cycles). `node_end` payload: `{items_in, items_out, dropped,
     ms}` — the dedup effect becomes observable per run.
   - dispatch map (line 51) gains `"dag": run_dag`.
   - Budget/failure semantics copied from the existing orchestrators: a
     node hitting `BudgetExceeded` marks its dependents skipped; result is
     stamped via `_stamp_limit` (line 113).
5. **`backend/app/api/workflows.py:61,85`** — create/update currently
   validate nothing about the graph. Add validation for `dag` graphs:
   unique node ids, every `needs` id exists, acyclic, every transform step
   has a known `op`, every JMESPath compiles (`jmespath.compile`). Reject
   with 400 + message (the editor surfaces it).
6. **Frontend** — `frontend/src/routes/WorkflowEdit.tsx` and
   `frontend/src/lib/graphFlow.tsx` (§5).
7. **Data-only migration** (§4): `update_workflow` on `search-knowledge` +
   `update_agent` on the four `knowledgeable-retriever-*` system prompts.
   No DB migration — `Workflow.graph` is already a JSON column
   (`backend/app/models.py:361`).

Untouched: the five existing orchestrators, `_node_input` templating
(`orchestrators/__init__.py:65-72`), sub-workflow nodes (`agent:
"workflow:<slug>"`, `executor.py` `child_agent`) — they work inside a dag
node unchanged.

## 3. The vocabulary (the "definition language" decision)

A transform node:

```json
{"id": "dedup-vector", "kind": "transform", "needs": ["retrieve-vector"],
 "steps": [ {"op": "...", ...}, ... ]}
```

**Initial value:** the envelope-value of its single `needs` entry — for an
agent node, the structured dict `{text, tool_results, run_id, tokens_*}`;
for a transform node, its output value. With multiple `needs`, the initial
value is the **list of those values in `needs` order** (deterministic). This
removes absolute `nodes."retrieve-vector"` addressing — and with it the
JMESPath quoted-dash footgun — from every path in today's cases.

**Three ops**, each taking the previous step's value:

| op | fields | semantics |
|---|---|---|
| `get` | `path` (JMESPath) | project/select/flatten — `tool_results[].content`, `[].chunks[]`, `[].{id: chunk_id, text: text}` |
| `parse_json` | `on_error: "skip"\|"fail"` (default `fail`) | string → JSON; on a list, parse each element; `skip` drops unparseable items and records a count in `node_end` |
| `dedup` | `by` (JMESPath, relative to item; optional) | value must be a list; keep **first** occurrence per key; without `by`, key = sha256 of the item's canonical JSON; items whose key evaluates to null are kept |

**Agent-node templating in a dag:** `{input}` as today; `{prev}` = the
rendered value of the node's single `needs` entry (string as-is, anything
else compact-JSON) — so existing templates read naturally; `{node:<id>}` for
multi-dependency agent nodes. Rendering to string happens only at template
substitution; between nodes the value stays structured.

### Why JMESPath + 3 fixed ops, and not the alternatives

- **jq** (`jq` PyPI binding): a full language — more power than the two
  cases need, a C-extension dependency in the image, arbitrary program
  strings inside JSON (escaping pain, hard to validate in the editor, hard
  to reason about termination). Rejected as speculative generality.
- **JSONLogic**: built for boolean rule evaluation; list restructuring and
  projection are contortions (`map`/`var` chains), dedup doesn't exist — we
  would ship custom ops *anyway*, plus a worse addressing syntax.
- **CEL** (`cel-python`): expression-only (no list restructuring to a new
  shape without `map` gymnastics), the Python implementation is immature,
  and again no dedup.
- **Hand-rolled dotted paths, no library**: we would re-implement JMESPath's
  hard 20% (projections, flattening, quoted keys, slices) ad hoc, with
  private semantics nobody can look up. Rejected — a workflow JSON should be
  readable against a public spec, which is exactly Frederico's
  "linguagem de definição" ask.

JMESPath is the narrowest thing that is still a *standard*: pure-Python,
stable ~a decade, side-effect-free, statically compilable (validation at
save time), and it covers select/project/flatten completely. The three ops
carry only the semantics JMESPath lacks (parsing, dedup). This is the
smallest vocabulary that expresses both of today's cases; nothing else is
included.

## 4. search-knowledge rewritten (proof of concept)

Two data changes, zero Python specific to this workflow:

**(a) The four retriever agents' system prompts** (via `update_agent`): step
5 becomes *"After the tool call, reply with exactly: OK"*. The nano model
then emits ~2 tokens instead of re-streaming ~60 KB of JSON at 13 tok/s —
this is where the 17 minutes actually go away. The transform reads
`tool_results`, ignores `text`.

**(b) The workflow graph** (via `update_workflow`), kind `dag`:

```json
{
  "nodes": [
    {"id": "plan", "agent": "knowledgeable-search-planner",
     "label": "Plan",
     "input_template": "<existing planner template>\n\nQUESTION: {input}"},

    {"id": "retrieve-vector", "agent": "knowledgeable-retriever-vector",
     "needs": ["plan"],
     "input_template": "ORIGINAL QUESTION: {input}\n\nPLAN (JSON from the search planner — parse sub_queries from it, tolerant of extra items or surrounding prose):\n{prev}"},
    {"id": "dedup-vector", "kind": "transform", "needs": ["retrieve-vector"],
     "steps": [
       {"op": "get", "path": "tool_results[].content"},
       {"op": "parse_json", "on_error": "skip"},
       {"op": "get", "path": "[].chunks[]"},
       {"op": "dedup", "by": "chunk_id"},
       {"op": "get", "path": "[].{id: chunk_id, doc: document_path, heading: heading_path, text: text, score: rrf_score}"}
     ]},

    { "...": "retrieve-lexical / retrieve-graph / retrieve-code + their dedup-* twins, identical but for the agent slug and id" },

    {"id": "collect", "kind": "transform",
     "needs": ["dedup-vector", "dedup-lexical", "dedup-graph", "dedup-code"],
     "steps": [
       {"op": "get", "path": "[]"},
       {"op": "dedup", "by": "id"}
     ]},

    {"id": "synthesize", "agent": "knowledgeable-playground",
     "needs": ["collect"],
     "input_template": "<<<CONTEXT>>>\n{prev}\n<<<END CONTEXT>>>\n\nQUESTION: {input}\n\nAnswer only from the context above, citing chunk ids like [1]. If the context does not answer the question, say so plainly."}
  ]
}
```

Notes:
- The four arms run concurrently because the DAG's dependencies allow it —
  parallelism falls out of `needs`, no `concurrency` flag.
- Each arm makes exactly one `knowledge_search` call today (confirmed in the
  live agent prompt), so per-arm dedup is near-idempotent *now* — it is kept
  because Frederico asked for it explicitly and it becomes load-bearing the
  moment an arm calls per-sub-query. `collect` is where cross-arm dupes
  (same chunk from vector and lexical) actually drop.
- The projection step trims each chunk to the five fields the synthesizer
  cites — the synthesizer input shrinks relative to today's raw re-emission.
- Dedup requirements (per-arm AND pre-synthesize) land as **visible nodes**
  with `items_in/items_out` in their `node_end` events — QA can assert the
  dedup effect from the run timeline.

## 5. Editor (WorkflowEdit.tsx / React Flow) implications

- `frontend/src/routes/WorkflowEdit.tsx:20` — `Topology` union gains
  `"dag"`; `topologyOf` (line 60) detects it (any node with `needs`/`kind:
  "transform"`); `TOPOLOGY_TEMPLATES` (line 37) gains a 3-node starter
  (agent → transform → agent); `listNodeRefs` (line 156) and the add/remove
  node paths (lines 193, 208) handle the flat `nodes` list (same as
  sequential — only `needs` is new).
- `frontend/src/lib/graphFlow.tsx:56-95` — a `dag` branch: edges directly
  from `needs` (this is React Flow's native model — simpler than the
  existing synthetic edges), layered x-position by dependency depth,
  transform nodes get a distinct style/badge (e.g. "ƒ") and no agent line.
  Run-time status needs **zero work**: `deriveNodeStateFromEvents` (line
  103) already colors any node that emits `node_start`/`node_end` with a
  `node_id`, which transforms do.
- Node inspector (WorkflowEdit.tsx:~630-665): for a transform node, show a
  `steps` JSON textarea instead of the agent selector. v1 keeps `needs`
  editing in the JSON textarea (positions aren't persisted anyway —
  line 226); drag-to-connect editing of `needs` is a later nicety, not part
  of this card.
- `RunDetail` reuses `graphFlow` — live dag runs render with no extra work.

## 6. Risks for the Coders (the non-obvious breakage)

1. **Tool-result extraction is the landmine.** `on_tool_end`'s `output` for
   MCP tools arrives as a ToolMessage or a list of content blocks, not a
   plain string — today's `_safe_str(output)` (`agent_loop.py:155-160`,
   fine for a truncated event) would feed `parse_json` a Python repr and
   the whole passthrough dies downstream with "skip dropped everything".
   Extract `.content`, flatten text blocks, test against the real
   `knowledge_search` result shape.
2. **Dedup keep-first is order-sensitive.** Chunk order inside an arm =
   tool-call order = service ranking (fine); cross-arm order in `collect` =
   `needs` list order. Deterministic, but reordering `needs` in the JSON
   silently changes which duplicate survives (whose `score` the synthesizer
   sees). Document it in the op's docstring; don't "fix" it with sorting.
3. **`derive_kind_from_graph` ambiguity.** A `nodes` graph is sequential
   *unless* a node carries `needs`/`kind` — someone adding `needs` to an
   existing sequential workflow flips its execution model. The backend
   validation message on save and the editor topology label are the guard;
   there is no way to make shape-sniffing unambiguous without breaking the
   existing convention that the graph, not the stored `kind`, is truth
   (`orchestrators/__init__.py:46-50`).
4. **Don't widen `run_events`.** The 1500-char truncation on `tool_result`
   events stays; full content lives only in the in-memory envelope.
   `run_events.payload` is `json`, not `jsonb` — fat rows hurt every query.
5. **Transform failures**: a bad path yields `null` mid-chain, not an
   exception — `dedup` on `null` must fail loudly with the step index, or
   debugging a dag becomes guesswork. Emit the failing step index + op in
   the node `error` event.
6. **Tests that won't catch it:** unit tests on `transforms.py` pass
   trivially; the integration truth is a live rerun. Acceptance = rerun the
   same question as run `6a93966f` through the rewritten workflow
   (`AGENTS_DATABASE_URL='sqlite://' pytest` for the suite per
   `aw-autoskill-ap-mt-test-verify`, then the live A/B) and compare
   wall-clock + verify `collect`'s `items_in > items_out` on a question
   with overlapping arms. Deploy caveat: green CI ≠ live — verify in the
   running container (`aw-autoskill-verify-deploy-in-container`).
7. **CLI-runner agents have empty `tool_results`** — a transform after a
   CLI-backed node silently gets `[]`. Validation can't know the agent's
   runtime at save time; the `node_end` `items_in: 0` is the observable
   symptom. Documented limitation, not a bug to chase.

## 7. Rejected alternatives (so they aren't re-litigated)

- **Per-shape hooks** (`workers[i].post` ops + `graph.pre_synthesis` on
  orchestrator_worker): smallest diff, but two new ad-hoc mechanisms instead
  of one node kind, invisible in the run graph, and violates requirement 1
  ("a node kind, usable in any workflow, interpreted uniformly").
- **Composing from existing shapes via sub-workflows** (`workflow:` nodes:
  sequential → parallel sub-workflow → per-arm sequential sub-workflows):
  works today with zero engine changes, but the structured envelope is
  json.dumps-flattened at every boundary (`executor.py` child_agent), and
  one logical workflow becomes 6 DB objects — the opposite of "one
  exportable JSON".
- **A `tool` node kind** (deterministic MCP tool call from JSON — would
  delete the nano LLM from the arms entirely, planner-output parsing moved
  into a transform): the *bigger* latency win, but new executor
  infrastructure (MCP session per workflow run, auth config in graph JSON)
  beyond this card's two cases. **Deferred with a named trigger:** if after
  this ships the arms' only remaining LLM work (tolerant plan parsing +
  "OK") is measured > ~5 s per arm, or a retriever misparses the plan in
  practice, add `kind: "tool"` next — the dag + envelope groundwork here is
  exactly what it needs.
- **Python/expression snippets in JSON** (sandboxed eval): unexportable
  semantics, a sandbox to own forever. Frederico's "a menos que consigamos
  colocar esses códigos no próprio json" is satisfied by ops-as-data.
- **jq / JSONLogic / CEL** as the step language — §3.

## 8. What this makes harder later

- **A sixth graph shape to maintain.** The dag can express the other five;
  consolidating them onto it is a plausible future the platform now has to
  either do or forever carry both. Explicitly out of scope here.
- **JMESPath becomes a stored-data commitment.** Workflow JSONs in the DB
  (and exported ones in the wild) embed its syntax; switching languages
  later means a data migration, not a refactor.
- **Envelopes grow executor memory** (~500 KB per search-knowledge run with
  the 512 KB/result cap; bounded, but a future 50-node dag with fat tool
  results needs the cap enforced, not assumed).
- **Shape-sniffing gains one more rule** (§6.3) — each new shape makes
  `derive_kind_from_graph` more delicate.

## 9. Sequencing for the Coders

1. `AgentResult.tool_results` + executor threading (items 1–2 of §2) — has
   value alone (observability) and unblocks everything.
2. `transforms.py` + unit tests (pure functions, sqlite pytest).
3. `run_dag` + dispatch + `derive_kind_from_graph` + backend validation.
4. Frontend dag topology (render + JSON edit + inspector).
5. Data migration: 4 agent prompts + workflow JSON (MCP `update_agent` /
   `update_workflow` calls — reversible, old graph kept in the card).
6. Live A/B vs run `6a93966f`'s question; verify in-container.

Steps 1–3 are backend-only and deployable before 4; the old workflow keeps
running unchanged until step 5 flips the data.
