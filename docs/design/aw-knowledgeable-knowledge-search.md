# `knowledge_search` — a reasoning + multi-query + RRF-fusion retrieval tool, in-app

Status: **design, for discussion — nothing here is built.** Written by the
Architect agent, 2026-10-11, against card `3f65bf3b-9510-81e2-8109-fd5d16840a85`
(target `knowledgeable-fs-sync`). Frederico's ask, verbatim on the card: one
MCP tool that "can reason the question and do a search (or multiples and
fusion)", plus a document-fetch tool, with the open question *"maybe the
workflow can be done inside the aw-knowledgeable itself, so we don't rely on
ap-mt just to hold our reasoning process."*

This document is optimised to be argued with. §1 is the recommendation and the
two decisions that are genuinely Frederico's to make; everything after it is
the evidence and the spec.

---

## 1. Recommendation, and the two things to argue about

**Recommendation: the reasoning loop lives inside `aw-knowledgeable`, as a
plain HTTP endpoint (`POST /api/knowledge/search`) that generalises the
Playground's existing retrieve→synthesize pipeline — and the model it reasons
with comes through ap-mt's OpenAI-compatible `/v1` door via the already-shipped
`core/apmt.py`, exactly like topic labelling, entity extraction and Playground
synthesis do today.** No new LLM provider key anywhere; the standing veto is
not touched (§4). The ap-mt *workflow engine* is not in the serving path at
all.

This is not a compromise position — it is the shape the repo already converged
on three separate times (`backend/app/config.py:88-224`: three
independently-configured ap-mt callers), and `knowledge_search` is the fourth
caller, not a new architecture.

**Decision A (Frederico):** synchronous tool with a hard wall-clock budget
(default 20s, degrade to retrieval-only past it), or a handle the caller polls.
I pick **synchronous + budget** (§5.3) because the point is an agent using it
mid-conversation, and because the workspace→production hop crosses the tunnel
edge that cuts requests at ~30s — a handle only becomes necessary if we later
want a "deep research" mode that thinks for minutes. Disagreeing here changes
the tool contract, so it needs to be settled before a Coder starts.

**Decision B (Frederico):** how much reasoning per search. I spec a loop with a
hard structural cap — **one planning call, ≤5 sub-queries, one synthesis call,
one optional refine round (default OFF)** (§5.2). Turning the refine round on
by default roughly doubles latency and cost per search; I recommend shipping
with it off and letting the evaluation harness (§7) prove it earns its keep.
If Frederico's instinct is "always reason harder", that's the knob, and it's
his call — but it should be flipped by measurement, not by default.

**What would change my mind on the central question (in-app vs ap-mt
workflow):** (a) if per-search run lineage in the ap-mt `/workflows` UI — one
visible tree per search, per-step artefacts — turns out to be something
Frederico actually wants to look at per query, the workflow engine is the only
place that exists; (b) if a future "deep research" mode wants minutes of
agentic tool-looping rather than a bounded pipeline, that mode belongs in ap-mt
(it's what the runner door is for) and should be a *separate* workflow calling
this same endpoint as one of its tools, not a reason to move the bounded loop
out. Neither applies to the mid-conversation search this card asks for.

---

## 2. The central question, tested: what ap-mt's workflow engine gives vs costs

Frederico leaned in-app ("so we don't rely on ap-mt just to hold our reasoning
process"). Taking the other side seriously:

| ap-mt workflow gives for free | Why it doesn't pay for its cost here |
|---|---|
| Run lineage / `/workflows` UI | Each LLM call through the `/v1` door still lands as a run on an ap-mt agent slug (`knowledgeable-labeller` etc. already show up this way), so cost and usage attribution survive. What's lost is only the per-search *tree* view — see "what would change my mind", §1. |
| Cost rollup | Same: the `/v1` door bills per agent slug per call. The envelope additionally returns `usage` per search (the Playground already does: `api/playground.py:419-422`). |
| Retries, supervision | `core/apmt.py` already has retry with backoff and a tested unfixable-error classifier (`apmt.py:155-179`); supervision of a <30s request is meaningless. |
| Model routing | The "model" in `core/apmt.py` **is an agent slug** (`ApmtConfig.model`), so swapping the reasoning model is config, not code — routing already exists at exactly the granularity needed (`config.py:113,207` pattern). |

What the workflow engine *costs*, measured, not estimated (2026-10-08, this
corpus, ap-mt both doors, recorded in the extraction-cost memory and the
extraction cards):

- **Runner door (what workflow steps run on): ~$0.1022/call, ~100s latency,
  ~33k tokens of CLI-harness prompt inflation per call.**
- **Provider-API door (`/v1/chat/completions`, what `core/apmt.py` speaks):
  ~$0.001/call, 11–19s on extraction-sized prompts, no harness.**

A 3-hop reasoning workflow per search would be ~5 minutes and ~$0.30 *per
query* — two orders of magnitude off both budgets, and unusable
mid-conversation. On top of that, a workflow-hosted loop makes **every
retrieval** depend on ap-mt being up; in-app, an ap-mt outage degrades to
retrieval-without-synthesis, exactly the declared-degradation contract the
Playground already ships (`api/playground.py:371-413` — 503/502 still carry the
full `retrieval` envelope). And the reasoning prompts live in the repo that
owns the data and its tests, not in a workflow definition in another service's
database.

Frederico's instinct was right. The only honest caveat is that it was right
*because* the `/v1` door exists — in a world where ap-mt only had the runner
door, the veto plus latency would have forced a real dilemma.

## 3. His "I guess this is done already" — checked

Partially true, and the true part is load-bearing:

- **Done:** the MCP surface exists — 7 tools in
  `repos/aw-app-knowledgeable/knowledgeable_app/mcp/client.py` (dispatch at
  `mcp/http_handler.py::_DISPATCH`), with `search_graph` (`client.py:341`)
  already forwarding every §12/§15 knob. The retrieve→synthesize pipeline
  exists end to end as `POST /api/playground/ask` (`api/playground.py:342`):
  in-process `run_search()` call, `<<<CONTEXT>>>` framing, one
  `apmt.chat_complete`, closed-book agent, declared degradation. The in-process
  seam `run_search()` (`api/search.py:222-248`) was explicitly built to be
  called by "anything that already runs inside a request" — this design is its
  second customer.
- **Not done:** multi-query planning, any fusion at all, a lexical arm that
  returns *chunks* (today's `mode=lexical` returns nodes for the link picker —
  wrong unit for fusion, §5.4), a document-fetch MCP tool (the HTTP endpoint
  exists, `api/documents.py:102`, but streams file bytes — wrong shape for an
  agent), and any evaluation harness.

So: "change stuff around" is roughly right for the skeleton, and wrong for the
four pieces above, which are the actual work.

## 4. Which model reasons, and where the key comes from

**The standing veto stays intact, by construction.** The credential is an
**ap-mt ApiKey row** (never a provider key); it is **pushed** into an
in-memory slot by the workspace connector every 300s and never pulled or
persisted (`aw-app-knowledgeable/knowledgeable_app/playground_key_push.py`,
production side `api/playground.py::set_pushed_key`);
`backend/tests/test_no_llm_provider_transport.py` walks every file in
`backend/app` and fails on any provider transport, in code or comment. The
2026-10-03 closure (commit `7207670`) renamed the whole vocabulary
(`core/apmt.py`, `no_apmt_key`) precisely so this question wouldn't reopen.
`knowledge_search` adds nothing to that surface — it is caller #4 of
`core/apmt.py`.

Concretely:

- **Planner call** — new config `knowledge_search_planner_apmt_agent`
  (default a new ap-mt agent `knowledgeable-search-planner` on a nano-class
  provider-API model: structured JSON out, ~1k tokens, the $0.001/11s door).
- **Synthesis call** — reuse the Playground's agent
  (`settings.playground_apmt_agent`): its closed-book prompt is the one that
  was mutation-tested against leaking outside knowledge, and `knowledge_search`
  wants exactly that contract. A separate `knowledge_search_apmt_agent` config
  key exists from day one but defaults to the Playground agent.
- **Key plumbing:** reuse the already-pushed Playground key slot for both
  calls. Two ops facts for the Coder, both bitten before: (1) ap-mt ApiKey rows
  are **scoped to agent slugs** — the new planner slug must be added to the
  pushed key's `agent_slugs` or every call 401s; (2) every restart empties the
  in-memory slot until the connector's ≤300s re-push — in that window
  `knowledge_search` must behave like the Playground does (`paused_reason`
  surfaced, retrieval still returned), never silently degrade.

## 5. The design

### 5.1 Where it lands

Logic in the service, MCP layers stay thin — deliberately, because Frederico
also said *"aw-knowledgeable itself will run a mcp-gateway that will expose its
mcp"*: when that self-hosted MCP server exists, it wraps the same endpoint and
inherits the whole pipeline for free.

| Piece | File (new unless noted) |
|---|---|
| Orchestration (plan → fan-out → fuse → synthesize) | `repos/aw-knowledgeable/backend/app/core/knowledge_search.py` |
| RRF fusion, pure functions | `repos/aw-knowledgeable/backend/app/core/fusion.py` |
| Chunk-level lexical arm (Neo4j full-text) | `core/graph.py` — new `chunk_lexical_search()` next to `vector_search` (`graph.py:4383`) + full-text index in `ensure_schema` |
| Route | `backend/app/api/knowledge_search.py` — `POST /api/knowledge/search` |
| Doc-fetch route (text, not bytes) | `api/documents.py` — new `GET /api/documents/{id}/text` beside `/content` (`documents.py:102`) |
| MCP tools `knowledge_search`, `get_document` | `repos/aw-app-knowledgeable/knowledgeable_app/mcp/client.py` (+ `TOOLS_SCHEMA` at `client.py:403`, `_DISPATCH` in `http_handler.py`) |
| Config | `backend/app/config.py` — `knowledge_search_*` block, same shape as the three existing apmt caller blocks |
| Eval harness | `repos/aw-knowledgeable/backend/eval/` (§7) |

**Bucket scoping — inside the strict contract, not around it:** the route
binds `resolve_search_scope` (`core/identity.py:440`), the same dependency
`GET /api/search` and `GET /api/documents` use: an omitted `bucket` spans
every bucket the token can read, an explicit one narrows. No `DEFAULT_BUCKET`
anywhere (that literal is retired; the implicit resolvers at
`identity.py:380/491` 400 with candidates on ambiguity). **The scope is fixed
for the whole search and the planner never sees or chooses buckets** — a
permission boundary must not become a model decision. `collection` and
`anchor` (§15 axes) pass through to every sub-query unchanged.

### 5.2 The reasoning loop, with its stopping condition

"Reason the question" means, concretely: **query decomposition + mode
assignment, once, by a cheap structured-output call — not an open-ended
agentic loop.** The loop is bounded by structure, not by vibes:

```
plan (LLM #1) → fan-out (N parallel run_search/chunk_lexical, no LLM)
             → RRF fuse (no LLM) → synthesize (LLM #2)
             → [optional refine round: repeat fan-out+fuse once] → done
```

1. **Plan** — one `apmt.chat_complete` on the planner agent, JSON out (parsed
   with the existing `apmt.json_object`, `apmt.py:333`). Input: the question +
   the arm vocabulary. Output: 2–5 sub-queries, each
   `{q, mode: semantic|tree|lexical_chunks}`. The prompt instructs it to:
   produce the original question for the `tree` arm; a conceptual rephrasing
   (and a PT↔EN translation — the 2026-10-08 A/B measured that the bilingual
   surface moves results on this corpus) for the `semantic` arm; and any
   literal identifiers (command names, file names, error strings) as
   `lexical_chunks` queries. **Deterministic fallback:** invalid JSON after
   one retry, or a dead apmt → fixed fan-out `{q × semantic, tree,
   lexical_chunks}` with `planner: "fallback"` declared in the envelope. A
   search must never fail because planning failed.
2. **Fan-out** — all sub-queries in parallel (`asyncio.gather`), each through
   the in-process `run_search()` / `chunk_lexical_search()`, **every one at
   `limit = max(20, 2×final_limit)`** and truncated only after fusion. This is
   not a tuning choice: `search(limit=5)` is NOT `search(limit=20)[:5]` on
   this store — ANN recall collapses at small limits and the envelope declares
   nothing (open backlog card
   `correctness:ann-recall-collapses-at-small-limit`, measured 2026-10-07).
   Asking small inside a multi-query plan would silently throw away the best
   results of every arm. The floor is a named constant
   (`SUBQUERY_LIMIT_FLOOR = 20`) citing that card, so fixing the card later
   doesn't orphan a magic number.
3. **Fuse** — §5.4.
4. **Synthesize** — one `apmt.chat_complete` on the synthesis agent over the
   fused top-`limit`, reusing the Playground's `_context_block` framing
   (`playground.py:276-295`) — same delimiters, same closed-book contract,
   same "the graph does not say" honest-empty answer.
5. **Refine round (default OFF, `max_rounds = 1`)** — if enabled
   (`max_rounds = 2`) the synthesis prompt may return a structured
   `{insufficient_evidence: true, missing: [...]}` instead of an answer, which
   triggers **exactly one** more fan-out with the missing terms as new
   sub-queries, re-fused with the first round's pool, then a final synthesis.

**The stopping condition, explicitly:** the pipeline halts when any of these
is hit, whichever comes first — (a) structural: `rounds == max_rounds`
(default 1, hard ceiling 2 — not configurable above 2); (b) budget:
wall-clock `budget_ms` (default 20 000) checked before each LLM call — if the
next call doesn't fit, return fused retrieval with `degraded: "budget"` and no
answer; (c) token cap: `max_search_tokens` (default 8 000 across both/all
calls), same degradation. An unbounded reasoning loop per search is a cost and
latency bug, so the bound is part of the contract, not a tuning detail — the
envelope always reports `rounds`, `llm_calls`, `usage`, and which (if any)
stop fired.

### 5.3 Sync vs handle

Synchronous. Expected shape: planner ~2–4s (nano, small prompt) + fan-out
~1–2s (parallel) + synthesis ~5–12s ≈ **9–18s typical**, inside the 20s
budget, inside the ~30s tunnel cut that applies to the workspace→production
hop (the connector's `base_url` is config; the production deployment is
reached over the tunnel edge, which cuts at ~30s — the same cut the topics CLI
had to design around). The budget degradation in §5.2 is what makes
synchronous safe rather than hopeful: the tool's worst case is a fused
retrieval envelope, never a hang or an opaque 504.

Rejected: returning a handle + poll tool. It doubles the MCP surface, makes
the common case (an agent mid-conversation) strictly worse, and its only
beneficiary — minutes-long deep research — is better served later as an ap-mt
workflow calling this endpoint repeatedly (runner-up design, see §1).

### 5.4 Fusion: RRF over ranks, chunks as the unit, one new arm as prerequisite

**Prior work this starts from, not re-derives:** the retrieval A/B on this
corpus (2026-10-08, kb-pgvector vs graph, three operational questions; re-run
2026-10-10) concluded the structural fix is **hybrid lexical+vector** — "a
literal command name is a keyword match BM25 never loses and cosine never
reliably wins" (procedural-genre card, Coder's measured comment) — and the
repo's own design already names the only sound merge:
`aw-knowledgeable-v2-retrieval.md:1216-1220` — *"Cosine scores are not
calibrated across models… The sound merge is RRF over ranks. This is the trap:
merging by score looks like it works"* — and at `:1275-1277` explicitly says
hybrid dense+sparse is the reason to build RRF. It was never implemented.
This is where it gets implemented.

- **Formula:** standard RRF, `score(c) = Σ_arms 1/(K + rank_arm(c))`, `K = 60`
  (the literature default; a config knob, not a tuned value). Ranks only —
  cosine [0,1], Lucene BM25 and tree beam scores are three incomparable
  scales, and §10's trap applies verbatim.
- **Fusion unit: the chunk**, keyed by the dedup identity that already exists
  (`chunk_text_sha256`, shipped by the dedupe card in `core/graph.py`) so the
  same text reached by two arms *fuses* (rank evidence accumulates) rather
  than occupying two slots.
- **The prerequisite arm:** today `mode=lexical` returns *nodes* (link-picker
  shape, `api/search.py` dispatch; `_CONTEXT_FIELDS` comment at
  `playground.py:262` confirms the unit mismatch). Fusing nodes with chunks
  would force a fake mapping. So this design adds `chunk_lexical_search()`:
  a Neo4j **full-text index on `(:Chunk).text`** (Lucene/BM25 — none exists
  today; `grep` finds only the vector index) queried via
  `db.index.fulltext.queryNodes`, which — like its vector sibling at
  `graph.py:1246-1288` — takes no filter argument, so it gets the same
  over-fetch + tenant/bucket/collection post-filter + declared-starvation
  pattern `vector_search` already implements. `search_nodes` and the existing
  `mode=lexical` stay untouched for the link picker.
- **Empty arms:** contribute nothing to RRF (correct by construction), but the
  envelope declares per-arm result counts — a retriever that returned nothing
  must be visible, not silent (the house rule §5 of v2-retrieval exists for
  exactly this).
- **`min_score`:** applies within the semantic/tree arms before fusion if the
  caller sets it; never across arms (incomparable scales again).

### 5.5 Tool signatures

**`knowledge_search`** (MCP) → `POST /api/knowledge/search`:

```jsonc
{
  "question":     "string, required",
  "bucket":       "string, optional — omitted = every bucket this token reads",
  "collection":   "string, optional — §15 source-path prefix",
  "anchor":       "string, optional — §15 anchor node",
  "limit":        "int, default 10 — final fused results fed to synthesis",
  "retrieve_only":"bool, default false — skip synthesis, return fused retrieval",
  "max_rounds":   "int 1..2, default 1 — Decision B's knob",
  "budget_ms":    "int, default 20000, server-capped at 25000"
}
```

Response (success and both degraded shapes share it, Playground-style):

```jsonc
{
  "answer": "string | null",
  "results": [ { "chunk_id", "document_id", "document_label", "text",
                 "topic_path", "rrf_score", "arms": [{"arm", "rank", "score"}] } ],
  "plan": { "planner": "llm|fallback", "sub_queries": [{"q","mode","arm_count"}] },
  "rounds": 1, "llm_calls": 2,
  "degraded": "null | budget | no_apmt_key | synthesis_failed",
  "usage": { "input_tokens": 0, "output_tokens": 0 },
  "scope": { "buckets": ["..."], "collection": null, "anchor": null }
}
```

Full provenance by default — which arm found what, at which rank — because the
evaluation harness (§7) and any human arguing with a bad answer both need it,
and it costs only bytes.

**`get_document`** (MCP) → `GET /api/documents/{id}/text`:

```jsonc
{ "document_id": "string, required",
  "max_chars":   "int, default 40000",
  "offset":      "int, default 0" }
```

Returns `{ label, bucket, collection, source_path, content_type, text,
truncated, total_chars, offset }` — the **extracted text**, never file bytes
(an MCP tool returning a PDF byte stream is useless to an agent; the existing
`/content` endpoint (`documents.py:102`) keeps serving browsers). Text comes
from `extract_text(storage_path)` — re-runnable by design
(v2-retrieval: "no re-embed ever needs a re-upload") — with a size-capped disk
cache beside the stored file; ordered-chunk concatenation is the fallback if
extraction fails at request time, declared as `"source": "chunks"`. Same 404
collapse rules as `/content` (miss, cross-tenant, non-document → 404, K1).

## 6. The PhD-thesis use case, read

Frederico's "have a look at my phd thesis presentation" resolves to the
thesis-proposal work (card `3e65bf3b-9510-81d1-9b25-fed50611f6d3`,
presentation `phd-thesis-pitch`): **graph-grounded agentic memory** — agents
that retrieve from, reason over, and write back into a structural knowledge
graph, with the Architect's 2026-09-25 validation locating the novelty at the
multi-agent seam. Two consequences for this design:

1. `knowledge_search` is not a convenience wrapper; it is **the retrieval half
   of the thesis instrument**. The envelope's full provenance (per-arm ranks,
   plan, rounds, degradations) is what makes a search *study-able*, which is
   why §5.5 makes it non-optional rather than a debug flag.
2. The evaluation corpus is real and small: the Estudo Geral set (18
   documents, `aw-app-uc-phd`), already present in the graph via the
   kb-mapped-folders ingest (the topic-tree card's "PhD melanoma branch" is
   this corpus). §7 evaluates against it plus the operational-questions set
   the A/B already used.

(Caveat, honestly: I read the thesis-proposal card and its Architect comment,
not the rendered slides — `aw_presentation` has no content-fetch tool. If the
pitch has moved beyond that card, §6 is the section to correct.)

## 7. Evaluation harness — so it can't silently regress

Reuse the A/B's method, made repeatable:

- **Gold set:** `backend/eval/goldset.json` — ~25 questions, each with
  expected document id(s) (and chunk substring where one chunk is *the*
  answer). Composition: the 3 operational questions from the 2026-10-08 A/B
  (verbatim — they are the measured baseline), ~15 questions over the Estudo
  Geral 18-doc corpus (PT and EN), ~7 over the Notion/kanban genre.
- **Runner:** `python -m backend.eval.retrieval` — **a loopback client of the
  live server** (`POST /api/knowledge/search` with `retrieve_only=true`),
  never an importer of app modules. This is the repo's standing rule from the
  topics-CLI regression: keys live in the uvicorn process's memory, so any
  `python -m` that imports instead of calling gets a silently keyless run that
  reports success.
- **Metrics:** recall@5/@10 per arm *and* fused — the acceptance claim is
  falsifiable: **fused ≥ best single arm on recall@10, per genre**. If RRF
  loses to its best arm, the fusion (or the planner) is broken and the harness
  says so. Plus end-to-end: with synthesis on, an LLM-judge pass
  (`answer grounded in retrieved chunks? question answered?`) through the same
  apmt door — reported, not gating, until it proves stable.
- **Regression:** baseline JSON committed; the runner diffs against it and
  exits non-zero on a drop beyond a stated tolerance. Wire-able as a scheduled
  task later; a committed baseline + a one-command runner is the part that
  must exist on day one.

## 8. Rejected alternatives (and what would un-reject them)

1. **Host the loop in ap-mt's workflow engine** — the card's central question.
   Rejected on measured cost/latency (runner door ~$0.10 / ~100s per hop vs
   `/v1` door ~$0.001 / ~11–19s), on the hard cross-service dependency it puts
   on every retrieval, and on prompts living outside the data's repo. §1 names
   the two things that would change this.
2. **Agentic tool-loop ("give an agent the search tools and let it iterate")**
   — rejected as the *default* path: it is unbounded by construction, which is
   the exact cost/latency bug the card warns about, and it's what callers can
   already do today by calling `search_graph` repeatedly. The bounded pipeline
   is the product; the agentic loop stays available to any agent that wants it.
3. **HyDE (hypothetical-document expansion) in the planner** — deferred, not
   rejected: every measured gap on this corpus (the A/B, the procedural-genre
   re-measure) pointed at the *lexical* arm and bilingual phrasing, not at
   semantic paraphrase depth. Add it the day the eval harness shows a recall
   gap HyDE addresses; it's one more planner output field.
4. **Score-normalised fusion (min-max / z-score) instead of RRF** — rejected
   by the repo's own prior analysis (v2-retrieval.md:1216-1220): uncalibrated
   scales, and "merging by score looks like it works" is precisely the failure
   that needs a measured harness to detect. RRF is rank-only and immune.
5. **Letting the planner choose buckets/scope** — rejected: §7bis made the
   bucket a permission boundary; a model must not widen or narrow one.
6. **Handle/async tool shape** — rejected for v1 (§5.3); the deep-research
   mode that wants it should arrive as an ap-mt workflow over this endpoint.
7. **A second approval-protocol/key path for the planner** — rejected: reuse
   the pushed Playground key slot; one push loop per key, one key for the
   search path, slugs added to its scope.

## 9. What this makes harder later

1. **A per-search run-lineage tree in ap-mt's UI.** In-app orchestration means
   ap-mt sees two unrelated agent calls, not one search. If that view is ever
   wanted, the fix is a `search_id` tag passed through to both calls'
   metadata — cheap, but someone has to want it first.
2. **The 20s budget bakes in "answers are shallow".** A genuinely deep mode
   can't be retrofitted into this synchronous contract; it will be a second
   surface (workflow + handle), and some future reader will ask why there are
   two. §1/§8.6 are the recorded answer.
3. **The full-text index is new operational surface.** Lucene index on every
   chunk: build time on the 180k-chunk corpus, disk, and the same
   post-filter-dilution class of bugs the vector path fights (§2 of
   v2-retrieval) — now in two places. The declared-starvation pattern must be
   copied, not approximated.
4. **Reusing the Playground synthesis agent couples two features' prompts.**
   The day `knowledge_search` wants a different answer style, set
   `knowledge_search_apmt_agent` to a new slug — the config key exists from
   day one so that day is a config change, but until then a Playground prompt
   edit silently changes `knowledge_search` answers too.
5. **RRF K=60 and the 20-floor are constants chosen by literature/bug-report,
   not by this corpus.** The harness (§7) is what keeps them honest; shipping
   the tool without the harness makes them folklore.

## 10. Risks for the Coders (the non-obvious ones)

1. **The small-limit recall bug will eat your fusion silently.** Any sub-query
   under `limit=20` returns a *different set*, not a prefix — and the envelope
   says nothing (`partial: false`). The floor in §5.2 is load-bearing; the
   backlog card (`correctness:ann-recall-collapses-at-small-limit`) stays open
   independently.
2. **Restart window:** every production restart empties the pushed-key slot
   for ≤300s. `llm_enabled`-style honesty applies: in that window the tool
   must return retrieval + `degraded: "no_apmt_key"`, and the eval runner must
   check the degradation field, never just HTTP 200 — `status: "built"` lying
   while keyless is the exact regression topics already paid for.
3. **ApiKey agent-scoping:** the new planner slug 401s until it's in the
   pushed key's `agent_slugs` (ap-mt `api/openai_compat.py::require_api_key` +
   `_check_scope`). This is an ap-mt-side config step a green unit-test suite
   will not catch.
4. **`db.index.fulltext.queryNodes` takes no filters** — same as the vector
   index. Copy `vector_search`'s over-fetch window + in-scope count +
   `starving_filter` declaration (`graph.py:4383-4434`); do not invent a third
   escalation mechanism (that rule is already written down twice).
5. **Fuse on `chunk_text_sha256`, not `chunk_id`** — otherwise the duplicate
   contents the dedupe card measured (3 document_ids, byte-identical text,
   identical score) re-inflate across arms and the top of every fused list is
   the same paragraph three times.
6. **Don't validate §12 knobs twice.** `run_search()` owns the validation
   matrix; the orchestrator passes through and lets 400s propagate verbatim,
   same rule the connector (`client.py:347-350`) and the Playground already
   follow.
7. **Tunnel cut ≈30s:** test the synchronous path through a real browser/the
   gateway, not just in-container curl — in-container never crosses the hop
   (the BYOD-tunnel memory's lesson generalises).

## 11. Addendum (2026-10-23): `also_in` compaction — a `documents` map, not a repeat per entry

Card `aw-knowledgeable-knowledge_search-repete-metadado-completo-do-documento-
em-cada-entrada-de-also_in-formato-compacto`. §5.5's `results`/`retrieve_only`
naming above predates the shipped shape (the real response key is `chunks`,
not `results`; `synthesize` not `retrieve_only`; plus `effective`, `clamped`,
`rounds_run` — never corrected here, corrected now alongside this change).

**The waste, measured on a live payload** (prod, same question as AP-MT run
`6a93966f`, `X-Internal-Secret`, `synthesize=false`, `o200k_base` — the
tokenizer the retrieval workers actually use): 20 fused chunks, 55 `also_in`
entries across 18 chunks, 9 distinct documents — the worst single chunk had 7
entries, 6 of them the *same* document varying only `chunk_index`. Each entry
repeated `document_id` + a ~140-char `document_label` slug + `document_path`
in full. Before: 48,736 B / 15,015 tok compact JSON (17,914 tok in the
indent=2 text the Connector hands agents). The chunk text itself — the actual
content — was only 5,002 tok, 33% of the payload.

**The fix, in `core/knowledge_search.py` (`_compact_chunks`, ~line 516),
applied only when the response is assembled (~line 701), never upstream:**
- A chunk's `also_in` (`[{document_id, document_label, chunk_index}, ...]`)
  becomes `{document_id: [chunk_index, ...]}` — label/path dropped from each
  entry, order preserved (score-descending, same order the entries already
  carried).
- A new top-level `documents: {document_id: {label, path}}` map resolves
  every document referenced by any chunk's own fields or by any `also_in`
  entry, exactly once.
- `citations` (`_citations`) drops `document_label` — resolve it via
  `documents`.
- `chunk_text_sha256` — fusion's internal dedupe key — no longer appears on
  a response chunk row; it stays internal to `collapse_duplicate_chunks`/
  `rrf_fuse`.
- A chunk row's own `document_id`/`document_label`/`document_path`/
  `heading_path`/`text`/`score`/`rrf_score`/`arms` are unchanged — only
  `also_in` and `chunk_text_sha256` change shape.
- An `also_in` entry with no `document_id` groups under the explicit
  `ALSO_IN_UNKNOWN_DOCUMENT` sentinel — never a JSON-null key, never
  silently dropped (collapse-never-drop, §5, applied to this compaction too).
- `_compact_chunks` is pure and does not mutate `fused` — synthesis
  (`_synthesis_messages`) still reads `fused` directly, and its
  `_CONTEXT_FIELDS` still wants `document_label` on the chunk row unchanged.

**Measured after:** 38,877 B / 11,973 tok compact (**-20.3%**); 14,388 tok
indent=2 (**-19.7%**). The search-knowledge workflow makes 4 retrieval calls
per run — roughly 14k fewer tokens per run from this alone.

**Scope, deliberately narrow:** `collapse_duplicate_chunks`/`_also_in_entry`
(`core/graph.py`, shared by `run_search`, `anchor_search`, and topics) are
untouched — those surfaces (frontend, playground) keep the fat `also_in`
shape. Compacting at the source would contaminate `/api/search` and topics,
outside this card's scope.

**Rejected variants:** also stripping a chunk's own `document_label`/
`document_path` out to the `documents` map (an extra -3.1pp) — rejected
because the dag/transform epic's JMESPath projections run per-chunk
post-flatten and cannot join against a sibling map; dropping `arms` by
default (measured -33.3% alone) — rejected, it is the documented provenance
contract (§5.5), kept as a named opt-out follow-up
(`include_provenance=false`) if the dag epic's live A/B still shows context
pressure.

**What this makes harder:** two `also_in` shapes now coexist in the codebase
(this compact one vs. `run_search`/topics' fat one) until someone unifies
them; without `chunk_text_sha256` on the response, cross-call textual dedup
needs to recompute the sha256 of the normalized text rather than reuse the
one already shipped.

---

*Prior work consumed, not re-derived: the 2026-10-08/10-10 retrieval A/B and
its hybrid-retrieval conclusion (procedural-genre card
`quality:procedural-genre-absent-from-both-knowledge-indexes`); RRF-over-ranks
as the only sound merge (`aw-knowledgeable-v2-retrieval.md` §10); the
small-limit recall collapse (backlog card, measured 2026-10-07); the LLM-key
veto closure and push-only key design (cards of 2026-09-30/10-03, enforced by
`test_no_llm_provider_transport.py`); the runner-vs-API-door cost measurement
(2026-10-08); the dedupe card's `chunk_text_sha256` identity; strict bucket
resolution (`core/identity.py`).*
