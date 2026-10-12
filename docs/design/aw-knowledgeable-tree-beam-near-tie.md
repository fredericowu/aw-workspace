# Tree mode: the beam_width default, and declaring near-tie prunes

Status: **design, ready for a Coder** — written by the Architect against card
`3f75bf3b-9510-8102-af01-f466111c0a38`
(`retrieval:tree-beam-width-near-tie-prune-silently-wrong`, target
`aw-knowledgeable`), 2026-10-12. Every number in §2 was measured on the live
container this morning (harness output preserved at
`/tmp/beam_measure_main.json` on the bare-metal host); every file:line was
read on this pass.

Incident being designed against: at `beam_width=3`, `me.md`'s leaf topic
scored 0.51865876 — 4th of ~18 siblings — and lost the last beam slot to a
sibling at 0.51866596. A margin of **0.0000072** decided between the right
answer and "I found no information". The document never reached chunk-level
scoring. The topic tree itself was ruled out (rebuild demonstrably ran; at
`beam_width=10` the same query returns `me.md` at rank 1).

---

## 1. The decisions

**D1 — `beam_width` defaults to 5.** Changed in BOTH places the 3 lives,
because the Playground sends its value explicitly and a config-only change
would not reach it:

- `backend/app/core/graph.py:5977` — `_setting("TOPIC_SEARCH_BEAM_WIDTH", 3)`
  → default `5` (still env-overridable, still per-request overridable 1–10).
- `frontend-react/src/routes/Playground.tsx:249` — `useState(3)` → `useState(5)`.
  The UI always sends `beam_width` on tree mode (`:285`), so this literal IS
  the Playground default, not the config value.

**D2 — every scored beam cut declares how close it was, and a cut within
epsilon of the boundary is called out in plain English.** New always-present
`near_ties: [str]` field on every `strategy: "tree"` envelope (empty list =
every cut was clear), with a structured twin in the traversal levels, and the
Playground's Retrieved panel renders it. Epsilon: **0.01 absolute cosine**,
as `TOPIC_BEAM_NEAR_TIE_EPSILON`. Details in §3.

D2 is the load-bearing one. D1 makes the incident's shape rarer (the
incident build had `me.md` 4th of 18 — a beam of 5 keeps it, and the card's
own live-repro verification passes at default knobs); D2 makes the residual
honest at every width. Both ship.

---

## 2. The measured numbers behind D1

Method: one-off harness (same family as `harness/topic_coverage_measure.py`),
`docker cp` + `docker exec` into the live `aw-knowledgeable` container per
the live-measurement recipe. Bucket `main`, tenant `aw-workspace-default` —
**33,032 chunks, full coverage, tree levels {2 roots, 8 mid, 129 leaves}**.
8 queries (the `fruits` repro plus 7 real ones spanning the corpus), `k=20`
(the Playground's limit), 3 timed reps per width, query embedding computed
once and excluded (identical at every width).

| | bw=3 | bw=5 | bw=10 |
|---|---|---|---|
| walk latency, median across queries (range) | **~0.78s** (0.53–1.17) | **~1.3s** (0.93–2.04) | **~2.7s** (1.84–3.80) |
| topics scored per walk | 33–102 | 84–126 | **139 = the whole tree** |
| leaf chunks fetched + re-scored | 376–1138 | 861–2082 | 2003–3826 |
| synthesis context (chars of the k=20 results) | ~14–25k | ~14–26k | ~13–25k |
| flat-top-3 docs recalled (proxy, 8 queries × 3) | 10/24 | 13/24 | 17/24 |
| repro: `me.md` for "what fruits do I like" | rank 1 *on today's build*; lost by 7.2e-6 on the incident build | rank 1 (4th-of-18 incident shape also survives) | rank 1, `me2.md` rank 2 |

Three facts that reframe the cost question the card raised:

1. **Widening the beam costs latency and Neo4j reads, not tokens.** Context
   chars are flat across widths because `limit` caps what reaches the
   synthesizer; the walk itself makes no LLM calls. The §5 GraphRAG cost
   curve recorded in `api/topics.py`'s docstring is about **build** cost
   (one LLM label per topic) — it does not apply to search width. Measured,
   not asserted: the token column above.
2. **At bw=10 the hierarchy stops pruning on this corpus.** 139 topics
   scored = the entire tree; bw=10 is "score every topic, then keep the
   best 10 leaves" — flat search with extra steps, at 3.4× bw=3's latency,
   minus the ANN index that real flat search uses. If no pruning is what a
   caller wants, Semantic mode already does it properly.
3. **bw=10's residual misses are not beam losses.** `ssh bare metal`
   recalls 1/3 and the wireless-video control 0/3 even with the whole tree
   scored — those are topic-organization misses (centroid/leaf granularity),
   and no width buys them back. Width fixes exactly one failure class:
   near-boundary prunes. That class is what D2 makes visible.

So 5, not 10: +~0.5s median over bw=3 buys the measured recall step
(10→13/24) and covers the incident shape, while 10's further step
(13→17/24) costs 2× the walk and abandons the pruning that is tree mode's
reason to exist. And 5, not 3: the incident is a live, user-visible wrong
answer at 3, and 3's cut margins are the tightest measured (census below).
Production is single-worker and the walk is async Neo4j IO plus in-loop
numpy cosines over ≤~2k vectors at bw=5 — low-millisecond CPU per request;
acceptable.

### The cut-margin census (what epsilon had to be chosen from)

Pooled over every level cut at bw=3 across the 8 live queries (14 cuts):
min 0.0026, p25 0.0032, median 0.0097, p75 0.0129, max 0.044. Cuts within
0.005: 4/14. Within 0.01: 8/14.

The decisive datum is the incident tie itself: the **same semantic tie**
measured 0.0000072 on the incident build and 0.0069 on today's rebuild —
hourly rebuilds move a boundary pair by ~1e-2. An epsilon in the
"float-noise" range (1e-4 and below) would have missed today's incarnation
of the exact tie that caused the incident. Hence **0.01**: it catches the
known real case across both of its observed builds. It will fire on roughly
half of all level cuts on this corpus — that is the truth about this corpus
(margins ARE mostly thin), and it is acceptable because the declaration is
a factual provenance line in a panel, not an alarm (§3.4).

---

## 3. The near-tie declaration (D2) — exact shape

The chunk stage already declares its near-ties: `_NEAR_MISSES = 3`
(`backend/app/topics/retrieve.py:47`) reports the chunks just outside
top-k. The topic cuts are the only scored boundary in the pipeline with no
such declaration. D2 extends the existing idiom one stage up — no new
concept.

**3.1 — `topics/retrieve.py` (`tree_search`, the level loop at :148-168).**
Each `levels[]` entry gains two fields, computed from scores already in
hand (zero new Neo4j calls — `documents_beneath` for pruned topics is
already batched at :176-183):

```
"cut_margin": float | null   # last-kept score − first-pruned score; null when nothing was pruned at this level
"near_tie":   bool           # cut_margin is not null and ≤ epsilon
```

**3.2 — the envelope (`api/search.py`, tree branch at :532-568).** New
top-level field `near_ties: [str]`, present on **every** `strategy: "tree"`
response (empty list when every cut was clear — same "always present, often
empty" contract as `knowledge_search`'s `clamped`). One string per
near-tie level, in `clamped`'s plain-English register (statement of what
happened, with the numbers):

> `"level 1 beam cut was a near-tie: kept 'Apps & marketplace' (score 0.518666) over pruned 'Monolith to apps migration' (score 0.518659, 7 documents beneath) — margin 0.000007 is within epsilon 0.01, so a slightly different query or the next tree rebuild could change which branch this answer came from"`

Build the strings in `retrieve.py` (it owns the labels, scores and
`documents_beneath`); `api/search.py` only attaches. The flat-fallback
branch (:505-530) sets `near_ties: []` too — a schema that appears and
disappears by strategy is how the `not_applied` family of bugs starts.
Agents and the UI read the structured `traversal.levels[].near_tie` twin,
never the prose (same split the flow platform uses for `kind`/`outcome`
enums vs `message`).

**3.3 — epsilon.** `TOPIC_BEAM_NEAR_TIE_EPSILON`, default `0.01`, absolute
cosine distance. NOTE for the Coder: `graph.py`'s `_setting()` (:3814) is
int-only and raises on `"0.01"` — this needs its own small float reader
next to it, not a shoehorn.

**3.4 — Playground rendering.** Two touches in
`frontend-react/src/routes/Playground.tsx`:

- **Retrieved panel** (:478-495): render `retrieval.near_ties` as one
  `text-warn` line each, directly under the existing `not_applied` chips —
  the §12 spot for "what the backend actually did".
- **Traversal graph**: a pruned topic whose level entry says `near_tie`
  gets the warn accent on its existing dashed/provisional node styling
  (`graph/cytoscapeStyle.ts` data-selector, driven by the new field on the
  entry). Craft details are the UX Coder's; the contract is only "a
  near-tie prune must be visually distinct from an ordinary prune".

**3.5 — MCP surface.** `search_graph` (aw-app-knowledgeable) is a verbatim
pass-through — agents see `near_ties` with no connector change. Its schema
text should mention the field; that is a one-line follow-up card in the
connector repo, same family as the recent "mode=tree schema text" docs card.

**3.6 — survival through the Phase-2 migration.** The approved
`aw-knowledgeable-playground-on-knowledge-search.md` design lifts
`tree_search` into `core/knowledge_search.py` as a `tree` arm. `near_ties`
is computed inside `tree_search`'s own return value, so it rides the lift
unchanged; in the fused envelope it stays a **separate field** from
`clamped` — `clamped` means "I altered your request", `near_ties` means "I
ran your request and one of my cuts was razor-thin". Same honesty family,
different claims; merging them would make `clamped` unparseable as a list
of overrides.

---

## 4. Do `min_score` and `radius` share the silent-cliff shape?

- **`min_score` — same shape in principle, already half-declared, not fixed
  here.** It is a hard threshold on the same continuous score, so a chunk
  at `min_score − 1e-7` dies as silently-close as the incident's topic did.
  But its casualties are already visible: `dropped_below_min_score` (count,
  always on the envelope when `min_score` is set — `api/search.py:554`) and
  the dropped chunks by name in the traversal (`:561-566`). What is missing
  is only the "within epsilon of the boundary" call-out; the same idiom from
  §3 extends to it naturally **later**. Noted per the card, deliberately not
  fixed in this design.
- **`radius` — not the same shape.** It is a discrete hop bound (0–2,
  clamped and declared in `knowledge_search`'s `effective`/`clamped`).
  There is no score and therefore no margin; "how near was the first
  excluded node" has no free answer (it would take a new query, where the
  topic margins of §3 cost nothing). Nothing to declare.
- **`limit`/k — already solved.** The chunk stage's `near_misses` IS this
  declaration for the final cut. D2 completes the set: after it, every
  scored boundary in tree mode says how close it was.

---

## 5. Rejected alternatives

- **Default 10.** Scores the whole tree (no pruning — §2.2) at 2× bw=5's
  latency; its extra recall comes from degenerating into flat search, which
  Semantic mode already provides with an index. Runner-up if the corpus's
  branching collapses further: revisit when the tree gains a level or the
  bucket triples.
- **Keep 3 and ship only the declaration.** Fails the card's own
  verification: the live repro must stop answering "I found no information"
  at default knobs, and at 3 the incident build loses `me.md` again
  whenever it ranks 4th. Visibility without the recall step leaves the
  defect, labelled.
- **Relative epsilon (margin / best-score) or a percentile.** The measured
  boundary movement across rebuilds (~1e-2) is what the declaration must
  catch, and it is absolute; a relative form adds a second number to
  explain with no measured benefit. `min_score` is already absolute — house
  consistency.
- **Adaptive beam ("expand while the margin is under epsilon").** On this
  corpus half the cuts are under 0.01 — the beam would balloon toward
  whole-tree on most queries, i.e. default-10 by the back door, with an
  unbounded worst case. Rejected; a *declared* expansion could become an
  explicit knob later if near_ties data shows it earning its cost.
- **Auto-including the first-pruned branch's chunks when a near-tie fires.**
  Changes retrieval semantics silently — precisely the behavior class this
  card exists to end. The declaration tells the user/agent to widen; the
  widening stays theirs.
- **Deterministic tie-breaking (e.g. by topic size).** Reorders who wins a
  coin-flip; the flip stays invisible. Orthogonal to both decisions.
- **UI stops sending `beam_width` and defers to the server default.**
  Cleaner ownership, but the slider then lies until first touch, and
  Phase 2 retires `/api/playground/ask` anyway — not worth a UX contract
  change on a doomed endpoint. The two-places-to-change wart is accepted
  and dies with Phase 2.
- **Changing the Playground default mode to Semantic** — out of scope by
  the card, and it would bury the defect instead of fixing it.

---

## 6. What this makes harder later / risks for the Coder

- **`near_ties` becomes API surface.** Agents will read it; its wording
  acquires the same compatibility weight `clamped` already carries. The
  structured `near_tie`/`cut_margin` twins exist precisely so nothing ever
  needs to parse the prose — keep it that way in tests.
- **The default lives in two places until Phase 2.** `graph.py:5977` and
  `Playground.tsx:249` can drift; the Phase-2 migration (UI → thin client
  over `knowledge_search`) removes the UI literal. Until then, a comment at
  each pointing at the other.
- **Tests to update, and the one that lies:**
  `backend/tests/test_topic_search.py::test_tree_search_beam_width_is_a_setting`
  monkeypatches around the default 3 — it and any sibling asserting 3 must
  move to 5. Do NOT write a live-corpus test asserting `me.md`'s rank: the
  hourly rebuild moves centroids, so such a test is flaky by construction
  (the two measured margins for the same tie differ by 1000×). Pin new
  behavior to synthetic trees (the `three_topic_bucket` idiom); the live
  repro is the card's manual verification step.
- **Flat fallback must carry `near_ties: []`** — an absent-vs-empty split
  by strategy is a schema trap for every consumer.
- **`cut_margin: null` when a level prunes nothing** (frontier ≤ beam —
  common at the 2-root level). Null is not a near-tie; don't fire on it.
- **`_setting()` is int-only** (`graph.py:3814`) — epsilon needs a float
  reader; forcing it through `_setting` raises on `"0.01"` at import time
  of the first query.
- **Single-worker production is load-bearing and untouched** — D1/D2 add
  no LLM calls, no per-process state, no background work.
- Observation from the measurement run, for whoever next touches the
  harnesses: bucket `kb-mapped-folders` in tenant `aw-workspace-default`
  now holds **0 chunks** — the corpus lives in bucket `main` (33,032
  chunks). `harness/topic_coverage_measure.py`'s `MEASURE_BUCKET` default
  silently measures an empty bucket; worth a one-line default change on
  the next harness touch.
