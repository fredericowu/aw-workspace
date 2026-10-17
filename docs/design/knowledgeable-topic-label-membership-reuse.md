# aw-knowledgeable: membership-keyed label reuse + per-bucket rebuild floor

**Card:** `3fb5bf3b-9510-8144-a7a9-f68f81043f30` (efficiency:topic-rebuild-full-tree-per-single-document)
**Status:** specified 2026-10-17, implementation assigned to `coder-opus` (Frederico's explicit call).
**Repo:** `/opt/aw-workspace/repos/aw-knowledgeable`.

## The decision

Two changes, both bucket-state-only (single-worker safe), neither touching the
connector or the extractor:

1. **Fix the label-reuse pass so it actually reuses** — add *document
   membership* as a reuse key beside the existing centroid-cosine key. This is
   candidate 3 from the card ("cache de labels por cluster"), built on the
   discovery that the cache **already exists** (`_reuse_labels`,
   `backend/app/topics/build.py:248`) and is ineffective because its only key —
   centroid cosine ≥ 0.98 — is unstable under re-clustering. This kills the
   per-rebuild cost: a single-document change relabels only the clusters that
   document actually touched.

2. **A per-bucket minimum interval between builds** (candidate 2, as a guard,
   not as the fix) — `TOPIC_BUILD_MIN_INTERVAL_SECONDS`, default 3600, enforced
   inside `claim_dirty_bucket`'s existing Cypher. This kills the rebuild
   *frequency*: 15-minute re-sync revision churn collapses into at most one
   build per bucket per hour, with no coupling to the connector's own tick.

Neither alone is sufficient: the throttle without the reuse fix still pays 48
LLM calls per changed document (just less often); the reuse fix without the
throttle still spins a ~3-minute full re-cluster every 15 minutes for nothing.

## What the live system actually does today (measured, not inferred)

The card's measurement (2026-10-16, 07:02→21:38Z): 19 rebuilds, 912 labeller
calls (48 per rebuild, always 48), 1,303,321 input tokens, 11/19 rebuilds
triggered by a single document. On top of that, pulled live from the container
log on 2026-10-17 (`docker logs aw-knowledgeable | grep "topic tree built for
bucket main"`), the per-build label breakdown for bucket `main` — 156 topics,
145 leaves, 3 levels, ~42k chunks:

```
15:38 42226 chunks  labels {reused: 68, llm: 48, keywords: 40, llm_attempted: 48}
15:53 42238 chunks  labels {reused: 55, llm: 48, keywords: 53, llm_attempted: 48}
16:38 42259 chunks  labels {reused: 49, llm: 48, keywords: 59, llm_attempted: 48}
18:53 42269 chunks  labels {reused: 50, llm: 48, keywords: 58, llm_attempted: 48}
21:08 42270 chunks  labels {reused: 57, llm: 48, keywords: 51, llm_attempted: 48}
21:38 42276 chunks  labels {reused: 56, llm: 48, keywords: 52, llm_attempted: 48}
```

Read the 21:08 → 21:38 pair: the corpus grew by **6 chunks** and still only
56/156 labels matched at cosine ≥ 0.98. The budget (48, `max_llm_calls()`,
`backend/app/topics/label.py:259`) saturates every build, and ~50 topics per
build **degrade to keyword labels** — which topics get LLM labels churns
build-to-build. So the current steady state is simultaneously expensive AND
degraded. `label.py:278`'s own comment — "What keeps the steady state cheap is
NOT this bound but the reuse pass" — describes an intent the 0.98 centroid key
does not deliver on this corpus.

Why the centroid key fails: every build re-runs
`StreamingLeafClustering` from scratch (`build.py:361`), and although the page
order is deterministic (`core/graph.py::read_bucket_chunks_pages` contract),
any insertion shifts the reservoir sample and the minibatch refinement, so
most of the 145 leaf centroids wobble past the 0.02-cosine tolerance even when
their *membership* is byte-identical. Membership (which documents' chunks are
in the cluster) is the stable identity the key needs.

## Where it lands

### Change 1 — membership-keyed reuse

**`backend/app/topics/build.py`**

- Topic row construction (`:577-604`): add a `document_ids` property to each
  topic — for a leaf, `sorted(set(leaves[i].document_ids))` (the aggregate
  already collects them, `:213`); for an upper level, the union over its
  member leaves (the `leaf_sets` table at `:479-484` already maps every upper
  topic to leaf indices). Cap: if the set exceeds
  `TOPIC_MEMBERSHIP_MAX_DOCS` (new env, default 4096), store an empty list and
  `document_ids_overflow: true` — such topics fall back to the cosine-only
  path. On bucket `main` (2,393 documents total) no topic hits the cap.
- `_reuse_labels` (`:248`): widen the **candidate set**, keep the greedy
  one-to-one same-level matching exactly as is. A (new, old) same-level pair
  is a candidate when:
  - `cosine ≥ label_reuse_cosine()` (today's rule, unchanged, still 0.98), **or**
  - `doc_jaccard ≥ TOPIC_LABEL_REUSE_JACCARD` (new env, default 0.9) **and**
    `cosine ≥ TOPIC_LABEL_REUSE_COSINE_FLOOR` (new env, default 0.90).
  Ranking stays cosine-descending (today's order); the jaccard rule only
  admits more candidates. Jaccard is computed at **document** level, not chunk
  level — chunk external ids are `{doc}-c{index:05d}` and re-chunking a
  revised document renumbers them, so document ids are the stable unit.
- Why the 0.90 cosine floor on the jaccard path: membership-identical is not
  content-identical. A re-synced document whose text was entirely rewritten
  keeps its doc id; a leaf dominated by that one document must relabel. The
  floor is what catches a content swing while tolerating clustering wobble —
  it is the same concern `label_reuse_cosine()`'s docstring (`:165-187`)
  protects, answered with strictly stronger evidence (same members AND close
  centroid) than 0.95-cosine-alone ever was.
- Report (`:684-691`): split the reuse count by key —
  `labels.reused_by = {"cosine": n, "membership": m}` — so the fix's effect is
  observable per build, in the same report §5 already requires.

**`backend/app/core/graph.py`**

- `read_bucket_topic_labels` template (`:1948`) and function (`:6778`): also
  return `t.document_ids AS document_ids`. Old topics written before this
  change carry none — the jaccard path is inert on the first post-deploy
  build (cosine-only, today's behaviour, full cost once) and live from the
  second. No migration, no backfill.
- `write_topics` (`:2043`) needs no change — `SET t += topic.properties`
  carries the new list property as-is.

### Change 2 — per-bucket rebuild floor

**`backend/app/topics/scheduler.py`**

- New `min_interval_seconds()` beside `debounce_seconds()` (`:58`), env
  `TOPIC_BUILD_MIN_INTERVAL_SECONDS`, default 3600. Passed into the claim.

**`backend/app/core/graph.py`**

- `claim_dirty_bucket` template (`:1022`) and function (`:3969`): add, in
  **both** WHERE clauses (the lock-then-recheck shape must re-test everything
  after the lock, same as `pending_topic_build` today):
  `AND (b.topic_build_last_built_at IS NULL OR b.topic_build_last_built_at <=
  datetime() - duration(...))`; and stamp
  `b.topic_build_last_built_at = datetime()` in the same SET that clears
  `pending_topic_build` (`:1031`). Stamping at claim, not after the build,
  keeps the one-write-path property and matches the existing "a failed build
  does not retry blind" semantics (`scheduler.py:90-99`).
- Blocked-by-floor is **not** a lost rebuild: the claim simply doesn't match,
  `pending_topic_build` stays true, and the next poll after the floor expires
  claims it (≤ 30 s of slack from `TOPIC_BUILD_SCHEDULER_POLL_SECONDS`).
  First-ever build of a bucket (`last_built_at` null) is never delayed.
- Debounce semantics are untouched: `mark_bucket_dirty`
  (`ingest/worker.py:187` → template `graph.py:994`) keeps overwriting
  `topic_build_dirty_at`, bursts still collapse.

### Untouched

The connector and its 15-min tick / hash debounce (constraint: not fused — the
floor lives entirely on `(:Bucket)` state, source-agnostic). The extractor
(`extraction_enabled=False`, nothing here is built on it). The clustering
itself, the tree shape, the delete-then-replace invariant, `max_llm_calls`,
the ingest worker beyond the line it already has.

## Acceptance criteria — in labeller calls per changed document

Steady state means: second build after deploy onward (the first build
populates `document_ids` at today's full cost, expected and one-time).

1. **≤ 10 labeller calls for a single-document change** (today: 48). On live
   bucket `main`, a rebuild whose trigger was one changed document reports
   `labels.llm_attempted ≤ 10` and `labels.reused ≥ 140` of 156.
2. **Zero degraded labels in steady state** (today: 40-59 per build).
   `labels.keywords == 0` on that same single-document rebuild — the freed
   budget covers every genuinely-changed topic.
3. **≥ 1 h between builds of one bucket** (today: as little as 15 min).
   Bursts of `knowledgeable-labeller` runs in AP-MT (`GET /api/runs`) spaced
   ≥ `TOPIC_BUILD_MIN_INTERVAL_SECONDS` per bucket.
4. Net effect on the measured workload shape: 912 calls / 1.3M tokens per
   14.6 h window → ≤ ~14 build slots × ≤ 10 calls ≈ **≥ 85% fewer calls**, and
   that is the conservative bound (it assumes every slot fires and every slot
   has a changed cluster).

### How to verify live

From the bare-metal host (`bare-metal-privileged`):
`docker logs aw-knowledgeable | grep "topic tree built for bucket main"` —
the `labels {...}` dict in that line is the entire before/after. Trigger a
one-document change (any re-sync revision does it), wait out floor + debounce,
read the next line. Cross-check call counts against AP-MT's run list for
`knowledgeable-labeller` in the same window. The
`aw-autoskill-aw-knowledgeable-live-measurement-harness` skill has the
docker-cp pattern if a scripted before/after is wanted.

## Tests (contract, not implementation)

`backend/tests/test_topics.py` (the reuse pass already has tests here):
- identical document membership + centroid wobbled into (0.90, 0.98) → label
  reused via the membership key;
- identical membership but cosine < 0.90 (content rewritten) → NOT reused;
- membership jaccard below threshold and cosine < 0.98 → NOT reused;
- old topics with no `document_ids` property (pre-deploy tree) → no crash,
  cosine-only behaviour;
- report carries `reused_by` split.

`backend/tests/test_topic_scheduler.py` (claim tests live here):
- dirty + debounce elapsed + `last_built_at` recent → not claimed;
- same, after the floor elapses → claimed;
- `last_built_at` null → claimed immediately;
- the claim stamps `last_built_at`.

Memory `schema-migration-tests-are-blind-when-the-fixture-only-wipes-nodes`
applies: check the fixture these tests use wipes `(:Bucket)` properties too,
or the floor tests will pass against stale state.

## Rejected alternatives

**1 — Partial/incremental rebuild** (card candidate 1). Rejected on four
grounds. (a) No stable cluster identity exists to hang it on: topic ids are
positional, ordered by size (`build.py:458-464`) — creating a durable identity
is exactly the membership key above, at which point the *cheap* consumer of it
is the labeller, not a second build path. (b) It forks the delete-then-replace
invariant (`graph.py:1955` comment block; `api/topics.py`'s known interleaving
gap) into a mutate-in-place variant — the hazard `claim_dirty_bucket` was
built to contain. (c) It solves the wrong cost: clustering is ~3 min of local
CPU and zero LLM tokens; the measured spend is labels, and membership reuse
captures that saving with one codepath. (d) Incremental assignment without
periodic full re-clustering degrades the tree (centroid drift; `leaf_k` is a
function of corpus size, `cluster.py::leaf_cluster_count`) — we'd be buying a
quality regression with design budget.

**2 alone — throttle as the fix.** Rejected: at one build per hour you still
pay a full tree per changed document, forever, and still carry ~50
keyword-degraded labels per build. It caps the bleeding; it doesn't close the
wound.

**4 — batch per re-sync via the connector's `dirty_since`** (card candidate
4). Rejected: it fuses the connector's hash-anchored 15-min debounce with the
topic scheduler across an app boundary — two independent tick systems whose
state would need shared semantics (the cost the card's constraints demanded
be named) — and it only helps churn that *arrives via the connector*. The
bucket-side floor collapses revision churn from every source (connector,
direct API uploads, fs-sync, kanban mirror) with zero coupling.

**Raising the debounce** (the 2026-10-13 card
`knowledgeable:topic-build-debounce-too-short-for-mirror-churn`). The floor
supersedes it: a longer debounce only delays a build while arrivals continue
(and a steady 15-min drip always finds a quiet 300 s window eventually); the
floor bounds the *rate* regardless of arrival pattern. That card's finding is
resolved at this seam; no debounce change needed.

**Runner-up, severable:** if the freshness delay of the floor turns out to
matter (it delays a bucket's tree by up to ~65 min after a quiet period),
change 1 stands alone — ship membership reuse, drop the floor, and the cost
per changed document is still ≤ 10 calls, just paid every 15 min instead of
hourly.

## What this makes harder later

- **Labels can now go stale under slow drift.** Today ~100 topics/build get
  accidentally relabelled; that accidental freshness disappears. A topic whose
  membership is constant while its content drifts gradually (many small
  revisions, each within the 0.90 floor) keeps its label indefinitely. If this
  bites, the door back open is a max-label-age relabel (budget the oldest
  labels first) — noted, not specced.
- **`document_ids` is a second bulky property on `(:Topic)`** (beside the
  1024-float centroid). Fine at 2,393 docs; a 100k-document bucket pushes
  root topics to the cap and silently back onto cosine-only reuse. The
  escape is a minhash signature instead of the raw list — a property-format
  migration someone will have to do if corpora grow ~40×.
- **A second clock on `(:Bucket)`.** Anyone extending `claim_dirty_bucket`
  must now keep *two* conditions duplicated across the lock-then-recheck
  WHERE clauses; forgetting the second copy reopens the race the shape
  exists to close.
- **Tree freshness is bounded below by the floor** — a genuinely important
  document landing 5 min after a build waits ~55 min for its topics (vector
  and lexical search see it immediately; only `mode=tree` lags). Operators
  can lower `TOPIC_BUILD_MIN_INTERVAL_SECONDS` per deployment.

## Risks for the coder (non-obvious breakage)

- **Blanket topic reads.** `write_topics` does `SET t += properties`, and some
  read paths may return whole topic nodes rather than named fields — verify
  `get_graph` / `list_bucket_topics` / search templates don't start shipping
  doc-id lists (or worse, make frontend payloads balloon). Return named
  fields only.
- **The greedy ranking stays cosine-descending.** Do not rank by jaccard:
  membership sets overlap heavily between a parent and its children is
  impossible (same-level matching already guards this), but between two sibling
  leaves after a split, jaccard-first would pair the label onto the wrong
  half. Cosine within the admitted candidates picks the nearer centroid, and
  the one-old-claimed-once rule (`:303-315`) already prevents duplicates.
- **`document_ids` must be deduplicated and sorted** before write —
  `_TopicAggregate.document_ids` appends per *chunk* (`build.py:213`), so the
  raw list carries one entry per chunk, ~42k strings tree-wide if written raw.
- **First build after deploy is full-cost and keyword-heavy.** Expected. Don't
  "fix" it with a backfill; the second build proves the design.
- **Single-worker constraint holds by construction** — all state is on
  `(:Bucket)`/`(:Topic)`; nothing per-process. Don't add any.
- Out of scope, explicitly: `cost_usd=0` for OpenAI runs (own memory/card);
  the `processed_at` overwrite-on-resync observability gap; the connector's
  tick; `collapse_to_main.py`'s own `mark_bucket_dirty` calls (one-shot
  script).
