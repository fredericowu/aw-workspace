# Multi-perspective graph search — a LangGraph workflow over the knowledge graph and the workspace codegraph

Status: **design, for discussion — nothing here is built.** Written by the
Architect agent, 2026-10-15, against card `3fa5bf3b-9510-81f7-bca5-e4b192394030`
(target `aw-knowledgeable-fs-sync`). Frederico's ask, verbatim: *"I want to
test the knowledge on the graphrag and the workflow that will search in the
knowledge graph from different perspectives and I want to integrate the
workflow (langgraph) with the workspace codegraph."*

Companion to `docs/design/aw-knowledgeable-knowledge-search.md` (2026-10-11),
which this composes and does not replace.

---

## 0. The fact that reframes everything: there is no entity layer

`extraction_enabled = False` in production
(`repos/aw-knowledgeable/backend/app/config.py:160`, gated at
`ingest/extraction.py:61-66`). The `(:Entity)` layer the schema defines has no
other writer, and the config comment itself names the stake: the ~$18k
phase-2 full-corpus backfill needs a human-owned durable OFF. That is
Frederico's standing decision and this design does not touch it.

So the production graph is **Documents + Chunks + Topics + links**
(`LINKS_TO` / `RELATED_TO` via topic and embedding edges). Every perspective
in §2 marked **[now]** works on exactly that data — verified against the arm
implementations, not the schema. Every perspective marked **[entity-gated]**
is listed so its absence is a named decision, not a silent assumption; §7 is
the bounded option for Frederico to accept or refuse.

One more structural fact that decides §3: the corpus in bucket `main` is the
`knowledge_base/` tree (fs-sync design §identity:
`(tenant, bucket, workspace_slug, source_path)`, source paths like
`notion/kanban/...` and `authored/...`). Codegraph indexes `repos/**` source
files into its own SQLite store (`repos/aw-app-codegraph/README.md` — one
index at the workspace root). **The two key spaces do not overlap at all** —
not even on file paths. Any "join" that claims identity between them is
fabricated.

## 1. The decision, in four sentences someone can disagree with

A **LangGraph `StateGraph` workflow in agents-platform-multitenant** (which
already ships `langgraph>=0.2.50` and already runs LangGraph as its native
agent loop — `backend/app/core/agent_loop.py:1-6,98`) fans one question out to
**six perspectives**: five against aw-knowledgeable (each a differently-
parameterised call to the existing `POST /api/knowledge/search` /
`GET /api/search` surfaces — the workflow is their *sixth caller*, not a
fourth search path) and one against the workspace codegraph via the
mcp-gateway. KG perspectives are fused by RRF on `chunk_text_sha256` with
per-perspective provenance kept; codegraph results are **never fused** —
there is no reliable join between the two graphs, so the code answer stands
beside the knowledge answer, bridged only by literal mentions (§3). The
deliverable that answers "test the knowledge" is §6: an offline recall
harness extending the existing `harness/knowledge_search_recall.py` pattern,
plus a 20-question live judged set whose A/B baseline is plain
`knowledge_search` — if multi-perspective doesn't beat that baseline, the
workflow should not ship.

## 2. What a "perspective" is — the concrete list

A perspective is a **different retrieval function**, not a different phrasing.
`knowledge_search` already does phrasing diversity internally (its planner
decomposes into sub-queries, `core/knowledge_search.py:279-351`) and already
fuses three *arms*. The workflow adds lens diversity ON TOP: each perspective
below differs in matching function, retrieval unit, ranking dimension, or
corpus — and each row in the output records which perspectives found it and
at what rank.

### Available now (Documents + Chunks + Topics)

| # | Perspective | Mechanism (existing, cited) | What it surfaces that the others cannot |
|---|---|---|---|
| P1 | **Semantic** | `knowledge_search mode=vector, synthesize=false` (`core/graph.py:5304 vector_search`) | Paraphrased content — answers phrased unlike the question. |
| P2 | **Lexical / identifier** | `knowledge_search mode=lexical` (`core/graph.py:5597 chunk_lexical_search`, BM25 over `Chunk.text`) | Exact names, error strings, config keys, slugs — embeddings blur identifiers; BM25 does not. |
| P3 | **Structural neighborhood** | `knowledge_search mode=graph, radius=1–2` (`core/graph.py:6032 graph_expand_search` — vector seeds, BFS over `LINKS_TO`/`RELATED_TO` via topic+embedding edges, exact re-score) | Documents *adjacent* to the semantic hits that match the query text not at all — the related decision, the follow-up card. Works today: the `entity` via in `_GRAPH_ARM_VIAS` (`graph.py:6029`) is simply empty. |
| P4 | **Taxonomic / topic-tree** | `search_graph mode=tree` with anchor + beam (connector README; §12 knobs `beam_width`, `anchor`, `anchor_depth`) | Coverage: *what else lives under this theme* — siblings under the same Topic, for "what do we know about X" rather than "answer X". |
| P5 | **Temporal / recency** | Same `knowledge_search` call + `filters.date_from/date_to` and `documents_created_at` re-rank (`core/knowledge_search.py:598-606`) | The *current* state of a decision — catches a superseded doc outranking its replacement on pure similarity. |
| P6 | **Code reality** | `codegraph_explore` / `codegraph_search` / `codegraph_callers` (aw-app-codegraph, separate SQLite+FTS5 graph over `repos/**`) | What the code actually does *now*. The KG records intentions and decisions; codegraph records implementation. P6 disagreeing with P1–P5 is a **finding** (doc drift), not noise. |

Why this is not six paraphrases: P1 vs P2 differ in matching function
(cosine vs BM25 — uncalibrated scales, fused by rank only, per
`chunk_lexical_search`'s own docstring); P3 and P4 differ in retrieval unit
(graph neighborhood / taxonomy position vs text match); P5 differs in ranking
dimension (time); P6 differs in corpus and graph entirely.

### Entity-gated (NOT available today — do not build against these)

| # | Perspective | Needs |
|---|---|---|
| E1 | **Entity-pivot** — all documents mentioning entity X across buckets, beyond lexical match of its name | `(:Entity)` populated |
| E2 | **Relation-path** — how A connects to B through assertions, multi-hop | entities + assertions |
| E3 | **Contradiction scan** — conflicting assertions about the same entity | entities + assertions |

These are what "GraphRAG" usually means in the literature. Today they would
return nothing. §7 is the bounded way to find out what they are worth before
paying for them.

### Merge, dedupe, rank, disagreement

- **KG perspectives (P1–P5):** every arm already returns the same row shape
  carrying `chunk_text_sha256` and `document_id`. The workflow fuses the five
  per-perspective ranked lists with plain RRF (k=60) on `chunk_text_sha256` —
  deliberately the same algorithm `fusion.rrf_fuse` uses inside
  `knowledge_search`, kept trivial so the two implementations cannot
  meaningfully drift. Dedupe is by sha; each surviving row keeps
  `{perspective: rank}` provenance.
- **P6 is not fused.** Symbols/files are a different unit from chunks;
  forcing them into one ranked list fabricates comparability (the same
  argument `chunk_lexical_search`'s docstring makes about nodes vs chunks).
  The output has two sections: `knowledge` and `code`.
- **Disagreement is an output field, not a judgement call.** The synthesis
  node receives top-N per perspective with provenance and must produce
  `answer`, `citations`, and `disagreements[]`. Two named rules: a temporal
  conflict (P5's newest contradicts P1's best-ranked) is reported with both
  dates, never silently resolved to either; a doc-vs-code conflict (P6
  contradicts P1–P5) is always surfaced as a drift finding. Synthesis never
  drops a perspective's top hit without naming it.

## 3. The join between two graphs with no shared identity: **there is no join**

Decision: **no identity join — two answers side by side, bridged only by
literal mentions.**

- KG identity: `(tenant, bucket, workspace_slug, source_path)`, paths
  relative to `knowledge_base/` (fs-sync design). Codegraph identity: files
  and symbols under `repos/**`. The key spaces are disjoint; even exact
  path-equality has no rows today.
- The one honest bridge is the **mention pivot**: KG chunks constantly cite
  repo paths and symbols literally (`path/to/file.py:123`, backticked
  symbols — house citation style). The workflow extracts those literal
  mentions from the top fused KG chunks (regex: repo-relative path with
  optional `:line`; backticked identifiers) and looks them up in codegraph.
  Results are labeled **"mentioned in"** — a provenance edge, never "same
  as". A mention that doesn't resolve in codegraph is itself reported (the
  doc cites code that no longer exists — drift again).
- Likewise in reverse: P6's symbol/file names are offered to P2 (lexical) as
  additional query terms, so code hits can pull in the cards that discussed
  them.

**Rejected:** symbol-name fuzzy matching and embedding-similarity linking
(both silently mismatch — the card's own red line); a shared entity
vocabulary (requires the absent entity layer; if §7's pilot ever runs over
repo-citing documents, entities become the natural future join vocabulary —
that is the door the entity layer opens, noted, not assumed).

## 4. Where it runs, and what it composes vs replaces

**Placement: ap-mt, as a registered native workflow
(`backend/app/core/orchestrators/`), built as a LangGraph `StateGraph`.**

- LangGraph is already ap-mt's engine for API-direct agents
  (`agent_loop.py` imports `create_react_agent`; `pyproject.toml:27`
  `langgraph>=0.2.50`). This introduces **no second orchestration model** —
  it is the platform's own, used for a deterministic graph instead of a
  ReAct loop.
- **Nothing stateful goes into aw-knowledgeable.** Production aw-knowledgeable
  must stay single-worker (three ap-mt keys in per-process globals); its own
  design deliberately made `knowledge_search` a *bounded, non-agentic* loop.
  Putting a LangGraph app inside it would contradict both. The workflow is
  stateless per invocation (no checkpointer); bounded like
  `knowledge_search` itself.
- **Reaching the KG:** ap-mt and aw-knowledgeable are both aw-stack services;
  the workflow calls `http://aw-knowledgeable:8090` with its **own scoped
  read-only API key** (the scoped-api-keys mechanism already exists), scope
  = read on bucket `main`. New credential, named in §8.
- **Reaching codegraph:** codegraph lives inside the workspace (Tier-1 app,
  index next to the source, served via aw-mcp-gateway). The workflow calls
  the gateway's scoped HTTP endpoint for `aw__codegraph__*` over the
  workspace edge. **This is the design's one unverified assumption** — that
  an ap-mt-held gateway token can reach a scoped endpoint through the tunnel
  (30s edge cut applies; codegraph calls are local-SQLite fast, so they fit).
  If it doesn't hold, P6 degrades exactly as designed: the code section
  reports `unavailable`, the knowledge answer still returns. Verify before
  implementation starts.

**Composes (never replaces):**

| Existing surface | Role in the workflow |
|---|---|
| `knowledge_search` (`POST /api/knowledge/search`) | The KG retrieval primitive — called once per perspective P1/P2/P3/P5 with `synthesize=false`, `limit>=20` |
| `search_graph mode=tree` | P4 only |
| `codegraph_explore` / `codegraph_search` | P6 + mention-pivot lookups |
| ap-mt `/v1` door, mapping agents on `openai-gpt-4o-mini` | Router + synthesis LLM calls |
| `knowledgeable-search-planner` | **Untouched** — keeps serving `knowledge_search`'s internal decomposition |

**One additive change to aw-knowledgeable** (small, and the only one):
a `plan: false` request field on `POST /api/knowledge/search` that maps to
the already-existing `planner_cfg=None` path
(`core/knowledge_search.py:316-317` → `_fallback_plan`, deterministic
single-sub-query). Reason: the planner sits behind a 13.6–19s runner door
(`knowledge-search-runner-door.md` §1); without this flag the workflow's 4
KG calls each pay their own planner call — 4× the latency and runner load
for decomposition the workflow's router node already did once. The code
path exists; the change is plumbing a request field to it, plus a clamp
note and a test.

**Replaces: nothing.** `knowledge_search` stays the right tool for a single
agent mid-conversation; the workflow is for the deliberate, multi-lens,
testable question. The workflow's output cites which primitive produced
every row, so it cannot quietly become a fourth opaque search path.

## 5. Cost and latency envelope

Per invocation, defaults:

- **LLM calls: ≤3** — one router (classify the question, pick applicable
  perspectives, produce per-perspective phrasings; `openai-gpt-4o-mini`),
  one synthesis (bigger model, Frederico's choice), optional judge only in
  eval runs. Retrieval calls are not LLM calls once `plan=false` lands.
- **Retrieval: ≤6 calls**, concurrency capped at **3** against
  aw-knowledgeable — it is single-worker; five concurrent fan-outs from the
  workflow would be a self-inflicted load spike. Each KG call carries
  `budget_ms<=20000` (server clamps at 25000).
- **Wall-clock budget: 60s default**, enforced in the graph. Degradation
  order when exhausted: drop synthesis (return fused evidence + provenance,
  same pattern `knowledge_search` uses), then drop P4/P6, never P1/P2.
- **Cost accounting: from token counts only.** `cost_usd` reads $0 for every
  OpenAI run in ap-mt (known reporting gap, carded) — the eval harness and
  any per-query budget derive dollars from `tokens_in/out` × price table,
  never from the field.
- Rough per-query envelope at defaults: ~5–15k tokens on gpt-4o-mini
  (router) + one synthesis call over ~30 fused chunks. Cheap enough that the
  eval set (§6) runs for cents; the eval must print measured numbers rather
  than trusting this estimate.

## 6. How "testing the knowledge" is measured — the part that ships first

The real question on the card: **does this beat `knowledge_search` alone?**
Two tiers, both producing a stored baseline and a nonzero exit on regression
— the exact pattern already in the repo
(`harness/knowledge_search_recall.py` + `knowledge_search_goldset.py` +
`knowledge_search_recall_baseline.json`, and its QA history: a goldset was
once falsified by mutation testing for being unable to move; the new
questions must be engineered the same way, so a broken fusion *shows*).

**Tier A — offline, deterministic, CI-able.** Extends the seeded-corpus
harness with question classes engineered so a single perspective misses:

- *recency traps* — superseded doc engineered to outrank its replacement on
  similarity (P5 must win);
- *identifier-vs-paraphrase pairs* (P2 vs P1);
- *neighborhood-only answers* — answer doc shares no query terms but links
  to the seed (P3);
- *coverage questions* — "what do we know about T" where the key facts are
  spread across one topic's siblings (P4).

Metric: recall@10 per perspective and for the workflow's fused output. The
bar, same shape as the existing harness's: **workflow-fused ≥
`knowledge_search mode=fused` on every class, strictly > on the engineered
classes.** If it isn't, the harness says so and the workflow has no reason
to exist — that is the falsifiable core of the whole card. P6 is excluded
from Tier A by design (needs a live workspace; not reproducible offline).

**Tier B — live, judged, read-only against bucket `main`.** A fixed set of
~20 questions, versioned in the repo, four categories: decisions ("what did
we decide about X and why"), current-state ("what is the state of Y" — P5
class), cross-graph ("where is X implemented and what have we decided about
it" — the P6/mention-pivot class), and **negative controls** (answer is
genuinely not in the KG — measures honest refusal; a system that always
answers fails these). Scoring: LLM-judge on groundedness (every claim
carries a citation), coverage against a hand-written key-fact list per
question, and conflict-surfacing (did it report the planted disagreement).
Run as A/B: workflow vs plain `knowledge_search(mode=fused,
synthesize=true)` on the same questions. Regression signal: scores stored
per question; any question dropping below its baseline fails the run.

Tier B's baseline run against plain `knowledge_search` can be executed
**before any workflow code exists** — recommended first implementation step,
since it is the control arm and doubles as the acceptance test for whether
the workflow is worth building at all.

## 7. The extraction option — bounded, measured, Frederico's call

Not proposed: flipping `extraction_enabled` (durable human-owned OFF; ~$18k
full-corpus backfill; standing decision). Proposed as an explicit option:

**A pilot of ~50 documents through the existing harness path** —
`harness/extraction_recall.py` / `extraction_golden.py` already run the real
extractor against a *throwaway* Neo4j; the same mechanism runs the pilot
docs (picked from `notion/kanban/done/` — the most entity-dense, decision-
bearing subtree) without touching production or the flag, since the global
flag has no per-bucket scoping anyway. Output: measured tokens/doc →
honest $/doc → extrapolated corpus cost, plus E1–E3 run against the pilot
slice and scored on the Tier B questions. Then Frederico decides with
numbers whether the entity layer buys enough to fund any wider run. Until
then, E1–E3 stay out of the shipped perspective list entirely.

## 8. What this design makes harder later

- **A second external consumer of aw-knowledgeable's API.** The workflow
  couples ap-mt to `knowledge_search`'s request/response shape; API changes
  now have a consumer outside the workspace. Plus one new credential (the
  scoped read-only key) to rotate.
- **P6 ties eval completeness to workspace uptime.** Tier A deliberately
  excludes it; the cross-graph question class only runs live.
- **Workflow-level RRF is a second fusion site.** Kept deliberately trivial
  (plain RRF, k=60) and documented as such — if `fusion.rrf_fuse` ever gets
  weighting, the two drift unless someone remembers this paragraph.
- **`plan=false` is new API surface** on aw-knowledgeable — one more knob in
  `clamp_params`' vocabulary to test and document.
- **The no-join decision caps cross-graph answer quality** at what mention
  pivoting + synthesis prose can do. If a real join is ever wanted, the path
  is the entity layer over repo-citing documents (§7 pilot is the first
  step), which reopens the extraction-cost conversation — by design, not by
  accident.

## 9. Risks for the Coders — the non-obvious ways this breaks

1. **`limit<20` returns a worse SET, not a prefix** — enforced server-side
   now (`clamp_params`, `LIMIT_FLOOR`), but `search_graph` (P4) is the raw
   surface: always ask ≥20 and truncate yourself. Same trap family: tree
   mode's beam has a documented near-tie cliff
   (`aw-knowledgeable-tree-beam-near-tie.md`) — use its settings, don't
   tune beam_width by feel.
2. **Do not serialize KG calls, and do not exceed concurrency 3** — single
   worker on the other end; serialized planner-door calls (before
   `plan=false` lands) would stack 13.6–19s each.
3. **The planner door failure mode is `clamped`, not an exception** —
   `knowledge_search` never raises for a degraded LLM door; the workflow
   must read `clamped[]`/`plan.planner == "fallback"` and surface it, or
   evals will silently measure deterministic fan-out while reporting
   planner-assisted numbers (this exact bug happened once:
   `bug:knowledge-search-apmt-door-fails-live`).
4. **Mention-pivot regex over chunk text will hit stale citations** — a doc
   citing `file.py:123` from six months ago resolves to the wrong line or
   nothing. Resolve to *file+symbol*, treat line numbers as hints, and
   report non-resolving mentions rather than dropping them.
5. **The tunnel edge cuts at 30s and strips content-encoding** — the
   gateway-side P6 call must be one small request, not a batch; and the
   gateway's scoped-endpoint reachability from ap-mt is the §4 assumption
   to verify *first*, not after the graph is built.
6. **Eval cost reporting:** tokens × price table, never `cost_usd` (reads
   $0 for OpenAI in ap-mt). The harness should fail loudly if it finds
   itself about to report a $0 total.
7. **Stay out of `repos/aw-app-knowledgeable`** (release pending) — the
   `plan=false` field needs no connector change to ship (the workflow calls
   aw-knowledgeable directly); the connector's MCP schema can pick it up in
   a later release.

## 10. Rejected alternatives, and why

- **Build the workflow inside aw-knowledgeable** (the shape the previous
  knowledge_search design chose for *its* loop): rejected here because (a)
  aw-knowledgeable cannot reach codegraph at all — the integration Frederico
  explicitly asked for would be impossible from there; (b) single-worker
  constraint makes it the worst home for new orchestration; (c) its own
  design doctrine is "bounded, NOT agentic" — a LangGraph dependency inside
  it breaks that deliberately chosen shape.
- **Build it in the workspace (runners app or a new Tier-1 app):** closest
  runner-up — it reaches both graphs natively. Rejected because Tier-1 apps
  run in-process in workspace core, so `langgraph`+`langchain-core` land in
  the shared core venv for one feature; a new Tier-2 app costs the full
  marketplace ceremony (two allowlists, public repo, release pipeline) for
  one workflow; and eval reproducibility then depends on the whole workspace
  being up. Falls back into play **if** the §4 gateway-reachability
  assumption fails.
- **A fourth standalone search tool** (new retrieval path with its own
  planner): rejected outright — the card's regression warning is correct;
  `knowledge_search` already owns decomposition and arm fusion, and this
  design keeps it that way.
- **Fuzzy KG↔codegraph linker** (symbol-name or embedding matching):
  rejected — silently mismatches, and the key spaces are disjoint today, so
  every "match" would be an inference presented as an identity.
- **Perspectives as N parallel agent runs** (`run_agents_parallel`):
  rejected for the serving path — agentic fan-out is non-deterministic and
  unevaluable per §6, and burns runner capacity per perspective. The
  platform's agent machinery stays what it is: the thing that *calls* this
  workflow.

## 11. Sequencing (for the PO/Coders, after Frederico's answers)

1. Tier B question set + baseline run against plain `knowledge_search`
   (no new code; produces the control numbers).
2. Verify ap-mt → gateway scoped-endpoint reachability (the §4 assumption).
3. `plan=false` in aw-knowledgeable + tests (small, independent).
4. The LangGraph workflow in ap-mt: router → P1/P2/P3/P5 (+P4) → RRF →
   mention-pivot → P6 → synthesis, with the §5 budget.
5. Tier A harness extension + baseline JSON.
6. Tier B A/B run; ship/no-ship decision on the §6 bar.
7. (Optional, Frederico's call) §7 extraction pilot.

**Open questions for Frederico** (the two things only he can answer):
whether the §7 bounded extraction pilot (~50 docs, throwaway Neo4j, measured
$/doc) is authorized; and whether the P1–P6 list above matches what he meant
by "different perspectives" before anything is built against it.
