# Playground onto `knowledge_search` — migration design + the sentinel fix

Status: **design, approved-pending-Frederico** — written by the Architect
against card `3f75bf3b-9510-81da-9873-e4c02c0d8c82`
(`feature:playground-migrate-to-knowledge-search`, target `aw-knowledgeable`),
2026-10-12. Incident: `ssh bare metal` in the live Playground returned a
non-answer (plus a stray Portuguese sentinel mid-English-sentence) while
`knowledge_search` answered the same question on the same corpus minutes
earlier. Every file:line below was read on this pass, not inherited.

---

## 1. The decisions

**D1 — Yes, the Playground becomes a thin client over
`POST /api/knowledge/search`, in two phases, and the tree walk survives as a
fourth selectable arm inside `knowledge_search` rather than as a parallel
pipeline.** Phase 1 (small, fixes the user-visible failure): the UI gains a
**Fused** mode, wired to `POST /api/knowledge/search`, and it becomes the
default; Tree/Semantic/Lexical keep calling `POST /api/playground/ask`
unchanged. Phase 2 (the real migration): the topic-tree walk
(`backend/app/topics/retrieve.py:90 tree_search`) is lifted into
`core/knowledge_search.py` as a `tree` arm/mode with `beam_width`,
`min_score` and `related_vias` as tree-scoped advisory knobs; Semantic maps
to `mode=vector`, Lexical to `mode=lexical` (chunk-level BM25 — strictly
better than the old node-level lexical); `/api/playground/ask` is then
retired. Its only caller is the UI (`frontend-react/src/lib/api.ts:349` —
verified, nothing else in any repo hits it).

**D2 — The sentinel reword is safe and goes in now, via `update_agent` on
the ap-mt `knowledgeable-playground` agent.** Verified: **nothing parses the
string.** `grep -r "encontrei"` across every repo, `docs/`, `native-skills/`
and `src/` finds zero occurrences of "não encontrei no grafo" — every hit is
unrelated content (a UC-PhD thesis transcript, a Crispal skill). The
Playground UI does not branch on the answer text; `playground.py` and
`core/knowledge_search.py` both treat `reply.text` as opaque. So **no
machine-readable signal needs to be added** — today the only consumer is
human eyes, and the structural signal for pipeline-level failure already
exists (`answer: null` + `clamped` notes). I'm stating that explicitly
rather than inventing a token nobody reads.

One finding that raises the fix's value: the agent is **shared**.
`config.py:300` — `knowledge_search_apmt_agent: str = ""` means "reuse
`playground_apmt_agent`", so the same prompt synthesizes for **both** the
Playground and `knowledge_search`. The reword fixes the leak in both paths
at once. Note the happy second-order effect: after the reword, the agent's
rule 3 and `_synthesis_messages`'s own trailing instruction
(`core/knowledge_search.py:445` — "If the context does not answer the
question, say so plainly") finally agree instead of fighting.

Replacement for rule 3 (drop the quoted literal entirely):

> **3. When `CONTEXT` does not support an answer, say so plainly and
> stop — in the language of the question (rule 4 applies to refusals
> too).** State that the retrieved context does not contain the answer,
> rather than filling the gap with outside knowledge, a guess, or a hedged
> partial answer built from anything not in `CONTEXT`. A
> confident-sounding completion from your own training is the one failure
> mode this agent exists to prevent.

**D3 — `effective`/`clamped` surface in the UI.** The response already
carries both (`core/knowledge_search.py:147-155, 548-555`). The UI renders
`clamped[]` as a visible notice strip under the params echo and shows
`effective` *as* the params echo. The card's own argument stands: a human
who typed `limit=5` into a form has at least as much right as a model to be
told it ran as 20 and why. The clamp messages are already written in plain
English for exactly this (`clamp_params`, `core/knowledge_search.py:111-116`).

**D4 — Synthesis budget: retrieval floor stays 20; the synthesis set
becomes a separate knob, default top-12 of the fused order, flipped only
after the goldset harness confirms it.** Reasoning in §4.

## 2. Where it lands

### Phase 1 — one coder card, ships the incident fix

- `frontend-react/src/routes/Playground.tsx` (`:247` mode state, `:261-290`
  submit): add `fused` to the mode toggle, **default**. When
  `mode === "fused"`, POST `/api/knowledge/search` with
  `{query, mode: "fused", limit, synthesize: !retrieveOnly}` (new client fn
  beside `api.ts:349`); render `answer`, `citations`, `chunks` (each chunk's
  `arms` provenance — which arm found it, at what rank — is the fused-mode
  answer to "how was this found"), `effective`, `clamped`, `usage`. The
  tree-only knob block (`:360-417`) greys out for fused exactly as it
  already does for lexical.
- UI degradation branch: `knowledge_search` degrades **inside a 200**
  (`answer: null` + a `clamped` note), unlike `/api/playground/ask`'s
  502/503 bodies (`playground.py:371-413`). The fused branch must read
  `clamped`, not the HTTP status.
- ap-mt prompt reword (D2): `update_agent(slug="knowledgeable-playground",
  system_prompt=<rule 3 replaced as above>)`. No restart, no deploy — the
  prompt is read per dispatch.

### Phase 2 — the migration, after Phase 1 is observed live

- Lift the tree walk into the pipeline: `core/knowledge_search.py` imports
  `tree_search` from `..topics.retrieve` (already core-side — the same
  import `api/search.py:108` does; no layering violation). `_ARMS_BY_MODE`
  gains `"tree": ("tree",)`; `_run_arm` gains the branch. **`fused` does NOT
  gain the tree arm yet** — whether topic-structure evidence improves the
  fused set is a goldset measurement, not a hunch.
- Stamp `chunk_text_sha256` on tree-arm rows before fusion —
  `rrf_fuse` **drops** rows without it (`core/fusion.py`), so an unstamped
  tree arm would silently return nothing. Reuse the key
  `collapse_duplicate_chunks` already computes (`core/graph.py:3827-3861`).
- Knobs: `beam_width` (1–10), `min_score`, `related_vias` travel as
  tree-scoped fields, **advisory + clamped/declared** in `knowledge_search`'s
  vocabulary — not the 400 matrix `run_search` applies
  (`api/search.py:256-280`). The 400 matrix stays with
  `/api/playground/ask` until that route dies; the two vocabularies must not
  be blended in one route.
- Traversal provenance (the shipped second-graph UI): Playground tree mode
  sends `subqueries=1, rounds=0` — one walk, one traversal, exactly today's
  semantics. The pipeline short-circuits the planner when
  `subqueries == 1` after clamping (`plan: {"planner": "verbatim"}`) — a
  planner call that may return at most the original query is pure cost.
  `include_traversal` + `mode=tree` returns `traversal` in the result.
- Retire `/api/playground/ask` + the Playground-side prompt builder
  (`playground.py:210-311`) once the UI no longer calls it. The key
  push/resolve half of `playground.py` (`:62-202`) **stays** — it is the
  credential seam `knowledge_search` itself borrows
  (`api/knowledge_search.py:53, 63`), and it is the other architect's card.

### Explicitly untouched

Credentials and scopes (the `knowledgeable-search-planner` `agent_slugs`
gap is Frederico's pending decision); key-push resilience (card
`3f75bf3b-9510-81c0-898d-ffa4ac8700a5`, another architect); core restart.

## 3. Rejected alternatives

1. **UI-only fourth toggle forever (Phase 1 as the end state).** Rejected:
   it institutionalizes the drift that caused this incident — two synthesis
   paths, two degradation vocabularies, two budget regimes, and the next
   retrieval improvement lands in one of them again.
2. **Retire tree/`beam_width`/`related_vias` outright.** Rejected: they are
   genuinely different retrieval ideas — a topic-structure-guided beam walk
   and typed-edge (Topic/Embedding/Entity) expansion — not worse versions of
   the vector/graph arms, and the traversal-provenance UI (built and QA'd
   2026-09-30, card `feature:aw-knowledgeable-retrieval-traversal-provenance`)
   has no data source without the walk.
3. **Keep `/api/playground/ask`'s route shape but swap its engine to
   `knowledge_search` internally.** Runner-up. Rejected because translating
   between the two knob vocabularies inside the route recreates a second
   validation matrix — the exact "two implementations of §12's matrix"
   failure the current code comments warn about (`playground.py:222-226`).
4. **Agent refines knobs mid-loop (hybrid).** Already rejected by
   v2-retrieval §12 on identity grounds; not reopened.

## 4. The 36,033-in / 504-out question

What the number was: 20 tree chunks × ~1,800 tokens each, serialized with
`topic_path`+`score`+`text` (`playground.py:264-273`), spent on a context
that did not contain the answer. Two separate lessons:

- **The waste was a retrieval failure, not a budget failure.** No synthesis
  budget tuning rescues a wrong 20 chunks; fused retrieval is the fix, and
  it's D1.
- **The LIMIT_FLOOR=20 rationale does not bind synthesis.** The floor exists
  because ANN retrieval below 20 returns a measurably *worse set*, not a
  prefix (`core/knowledge_search.py:41-46`). But truncating the **fused,
  rank-ordered** list for synthesis *is* taking a prefix of a well-ordered
  list — the measured pathology doesn't apply. Retrieval keeps the floor;
  synthesis gets its own `synthesis_chunks` knob (floor 5, ceiling =
  effective limit), **default 12**. Estimated ~40% input-token cut per
  answer, on top of the fused serializer already being leaner per chunk
  (`_CONTEXT_FIELDS` at `core/knowledge_search.py:417` carries no
  `topic_path`/`score`). Gate: run the goldset harness
  (`harness/knowledge_search_goldset.py`) at 12 vs 20 before the default
  flips; if answer quality drops, the default reverts to 20 — one config
  value. Haiku stays the right synthesis model either way.

## 5. What this makes harder later

- The Playground inherits `knowledge_search`'s floors — a user can no longer
  run a raw `limit=5` retrieval to *demonstrate* the small-limit recall
  collapse. The demo of why the floor exists becomes impossible from the UI.
  Accepted; the harness can still show it.
- The 25s `BUDGET_MS_CEILING` replaces the Playground's 60s
  `playground_apmt_timeout` — a deep tree walk plus synthesis that used to
  finish at 40s will now degrade to retrieval-only. Declared degradation,
  but a behaviour change a user will notice.
- One shared synthesis agent now serves two surfaces for real; if their
  answer styles ever need to diverge, the split is already provisioned
  (`knowledge_search_apmt_agent`, `config.py:300`) — set it, don't fork the
  prompt in place.
- v2-retrieval §12 becomes partially historical once `/api/playground/ask`
  dies; the doc needs a forward-pointer edit in Phase 2.

## 6. Risks for the coders

- **`rrf_fuse` silently drops unstamped rows.** A tree arm without
  `chunk_text_sha256` yields an empty fused set with no error — the classic
  silent degradation. Test must assert tree-arm rows survive fusion.
- **Lexical results are nodes in the old path, chunks in the new.** The UI's
  `isChunk` branching (`Playground.tsx:122`) partly disappears in Phase 2 —
  but only after the old modes move; in Phase 1 both shapes coexist.
- **Degradation shape differs by mode** (200+`clamped` vs 502/503) during
  the whole Phase-1 window; the UI must branch per endpoint or a fused
  failure renders as success.
- **The planner short-circuit at `subqueries==1` must still record a `plan`
  object** — `plan: null` with `rounds_run: 1` would read as "planner never
  ran" in the exact way `bug:knowledge-search-apmt-door-fails-live` just
  taught us to surface, not hide.
- **Prompt edit is live immediately** for every in-flight surface using the
  agent (both paths). Reword only rule 3; rules 1, 2, 5 were
  mutation-tested per Card E and are not to be touched.
