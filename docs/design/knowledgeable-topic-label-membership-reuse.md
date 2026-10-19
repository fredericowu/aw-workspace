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

---

## MEASURED AFTER IMPLEMENTATION — the membership premise is FALSIFIED

Implemented and deployed 2026-10-17 by `coder-opus`
(`aw-knowledgeable@119cb44`, deploy run 38986099888, green). Both changes are
live. Measured on bucket `main` the same night, and **§"Where it lands"
change 1 does not deliver its acceptance criteria.** Change 2 does.

### What the live builds say

```
00:39:40  42287 chunks  labels {reused: 61, reused_by: {cosine: 61, membership: 0},
                                llm: 48, keywords: 47, llm_attempted: 48}   <- first build, expected: old tree has no document_ids
01:39:46  42314 chunks  labels {reused: 52, reused_by: {cosine: 51, membership: 1},
                                llm: 48, keywords: 56, llm_attempted: 48}   <- STEADY STATE. target was <=10 / >=140 / 0
```

Acceptance: 1 ✗ (48 calls, not ≤10), 2 ✗ (56 keyword labels, not 0),
3 ✓ (60 min apart, see below), 4 ✗ (the floor alone caps the rate at 1
build/h — against the card's measured 1.30 builds/h that is ~−23%, not −85%;
in the hour actually observed it suppressed 2 of 3 builds, ~−67%).

### Why — the root cause, measured two ways

A read-only re-cluster of the live bucket, compared against the stored tree
(`/tmp/diag.py`, no writes, no LLM):

| input | best centroid cosine (median) | best doc jaccard (median) | cosine≥0.98 | jaccard≥0.9 |
|---|---|---|---|---|
| same corpus, same seed (0) | **1.000** | **1.000** | 145/145 | 145/145 |
| same corpus, seed 1 | 0.975 | **0.340** | 62/145 | **4**/145 |

The second row reproduces production's own profile (49-68 cosine matches per
build) from a *reseed alone*, with the corpus byte-identical. So:

1. **The clustering is perfectly deterministic** given identical input — when
   it lands in the same local optimum, cosine already reuses *everything*
   (145/145) and the membership key adds nothing.
2. **When it lands in a different local optimum, membership collapses far
   harder than the centroid** — 0.340 median jaccard against 0.975 median
   cosine. §"What the live system actually does today" asserted the opposite
   ("their *membership* is byte-identical even when the centroid wobbles");
   that is false on this corpus. Leaves hold a median of 105-124 documents,
   so a boundary shift reassigns tens of them, and jaccard ≥ 0.9 over 124
   documents tolerates only ~12 moving.
3. Therefore **no threshold choice rescues the design**: jaccard ≥ 0.7 ∧
   cosine ≥ 0.90 is 9/145, jaccard ≥ 0.5 is 35/145. "reused ≥ 140" is
   unreachable by tuning.
4. The actual variable is the clustering's **initialisation**: the reservoir
   sample depends on how many rows have been seen, so +27 chunks reseeds the
   seed centroids and lands k-means in a different partition. A corpus delta
   behaves exactly like a reseed. The lever that would make *either* key work
   is a warm start from the previous tree's centroids — which this spec put
   explicitly out of scope ("the clustering itself ... untouched"), so it is
   a new design decision, not a coder fix.

### What change 2 (the floor) actually did — verified live

```
00:36:28Z  claim stamps topic_build_last_built_at   -> build at 00:39:40
00:36:54Z  real document arrival, bucket dirty
01:00:22Z  second real document arrival (dirty_at overwritten)
01:24:33Z  read-only replica of the claim's WHERE:
             floor=3600s -> claimable: []          <- deferred, pending stays true
             floor=0s    -> claimable: ['main']    <- what today's code would have done
01:36:28Z  floor expires -> claim                  -> build at 01:39:46
```
Two arrivals that would each have paid a full tree (~00:42 and ~01:05)
collapsed into one build 60 min after the previous one. Nothing was lost:
`pending_topic_build` stayed true throughout.

### Consequence the floor introduces, now visible

Builds an hour apart accumulate a bigger delta than builds 15 min apart, so
per-build reuse gets *worse* (52 vs the 49-68 band) even as total spend falls.
Cost is clearly better; per-build label quality is slightly worse.

### Left deployed deliberately

The membership arm is strictly additive (it can only admit a pair that already
has the same members AND a close centroid), so it is inert rather than wrong,
and `document_ids` + `labels.reused_by` are the instrumentation the next
iteration needs to measure anything at all. Reverting it is a one-line change
if the Architect prefers that; the decision is theirs, not the coder's.

---

## ARCHITECT DECISION after the falsification (2026-10-17)

### Decision 1 — the membership arm STAYS deployed

It is inert, not wrong: it can only admit a pair that already shares members
AND a close centroid, and the live build confirms it admits ~1/156. Reverting
would churn 11 mutation-checked tests to remove a no-op, and `document_ids` +
`labels.reused_by` are the only measurement surface any further iteration has
— including the one below, whose acceptance is read off `reused_by`. Under a
warm-started clustering the arm also stops being dead weight: a same-partition
build has jaccard ≈ 1.0, so membership corroborates the cosine key instead of
failing beside it. This holds even if decision 2's gate fails: storage cost is
measured and trivial (32,876 ids tree-wide, biggest set 2,395, overflow 0).

### Decision 2 — one more attempt, through the clustering INIT, gated by a
### measurement before any production code

**The approach, falsifiably:** warm-start `StreamingLeafClustering` from the
stored tree's level-0 centroids instead of the reservoir k-means, keeping all
three full passes; spend leftover labeller budget upgrading reused
keyword-source labels; bound entrenchment with a cold-build clock. The
measured basis: the diag table above shows the clustering is *deterministic
given its init* — same init ⇒ 145/145 at cosine ≥ 0.98, and the entire
production reuse failure reproduces from a reseed alone. Pinning the init is
therefore the one lever that makes the already-shipped 0.98 key hold. The
premise this stands on, and the thing that can falsify it, is step 0 below.

**Why this is NOT rejected alternative 1(d).** 1(d) rejected incremental
*assignment*: new chunks bolted onto frozen clusters, no re-clustering, drift
unbounded, `leaf_k` frozen. Warm start keeps every pass: pass 2 still refines
over the FULL corpus every build (`build.py:560-565`), pass 3 still reassigns
every chunk, `leaf_k` still follows corpus size, empty clusters are still
dropped at `finish()`. The only change is where Lloyd starts. The residual —
real — risk is local-optimum *entrenchment*: a warm init exploits history
instead of exploring, so a corpus that genuinely reorganises keeps the old
partition shape until something forces a fresh look. That is what the cold
clock bounds, rather than the design denying the risk.

### Where it lands

1. **`backend/app/topics/cluster.py`** — new `seed_centroids_from(prior, k)`
   beside `seed_centroids(k)` (`:402`). `prior` = the stored level-0
   centroids. `k > len(prior)`: top up with k-means++ farthest-point picks
   from the reservoir (pass 1 is unchanged — the reservoir is still sampled,
   it just stops being the *init*). `k < len(prior)`: keep the `k` largest by
   stored `chunk_count`. **Weights contract:** assign the reservoir to the
   warm centroids and seed `_weights` from those counts — same semantics as
   the cold path; the invariant at `:356-359` ("the first refined page cannot
   erase the seed") is the thing a wrong weights-init silently breaks.
2. **`backend/app/topics/build.py`** — move the `read_bucket_topic_labels()`
   call (`:691`) above the init (`:556`); ONE read serves both warm start and
   the reuse pass. Warm path when a stored tree exists, the embedding
   dimension matches (same guard `_reuse_labels` already has, `:401-403`),
   and the cold clock hasn't expired; cold otherwise. The build report stamps
   `init: "warm" | "cold"` — without it the next measurement can't tell which
   path ran.
3. **Budget upgrade pass** (`build.py:716-737`) — today a topic whose reused
   label has `source == "keywords"` is skipped by the budget loop forever
   (`:735`), which is why `keywords == 0` is unreachable even with perfect
   reuse. After `wanted` is filled with non-reused topics, spend the
   LEFTOVER budget on reused-but-keyword-source topics, same order (highest
   level first, largest first). This is also what re-absorbs each cold
   build's relabel debt over the following builds instead of in one storm.
4. **Cold clock** — `TOPIC_BUILD_COLD_INTERVAL_SECONDS` (default 604800, 7
   days), checked and stamped (`topic_build_last_cold_at` on `(:Bucket)`)
   inside `build_bucket_topic_tree`, NOT inside `claim_dirty_bucket` — the
   claim's lock-then-recheck duplication hazard (see "What this makes harder
   later" above) is not getting a third condition. Plus an explicit
   `init=cold` override on `POST /api/topics/build` for the operator.
5. **Untouched:** the floor (works, stays at 3600 — its measured side effect,
   worse per-build reuse from bigger deltas, dissolves under warm start
   because reuse no longer depends on delta size through the init), the
   connector, the extractor, `max_llm_calls`, the delete-then-replace
   invariant, the membership arm.

### Step 0 — THE GATE, before any production change

Extend `/tmp/diag.py` (bare-metal-privileged / inside the container,
read-only, ~2 min): warm-start from the stored level-0 centroids, run
refine + assign over the live corpus, compare against the stored tree.
**Proceed only if cosine ≥ 0.98 matches ≥ 135/145 leaves.** If the gate
fails, STOP and report back — and the fallback is pre-decided, no third
design round: the card closes `partial` on the floor's measured saving,
decision 1 unchanged. This is the same discipline that caught the membership
premise, applied *before* the build cost this time.

### Acceptance criteria (replacing the falsified 1/2/4; 3 already passes)

1. **Gate:** warm re-cluster of live `main` matches ≥ 135/145 leaves at
   cosine ≥ 0.98 before any code ships.
2. **`labels.reused ≥ 140`** of 156 on a warm single-document rebuild.
3. **`labels.keywords == 0` within 4 warm builds of deploy** (the upgrade
   pass drains the standing ~56-topic keyword debt at ≤ 48/build while also
   covering genuinely-new topics).
4. **`llm_attempted ≤ 10` once the keyword debt is drained.**
5. Report carries `init: warm|cold`; at most one `cold` build per
   `TOPIC_BUILD_COLD_INTERVAL_SECONDS` per bucket.

### Rejected alternatives (this round)

- **Max-label-age relabelling over a loosened cosine threshold** (the
  runner-up). It concedes topic-identity instability and papers over it: at
  seed+1 the median best cosine is 0.975, so even a 0.95 bar leaves ~half
  the tree unreused, and every label it does carry lands on a partition
  sharing a median 34% of its members — the exact defect
  `label_reuse_cosine()`'s docstring exists to prevent, now at scale.
  `reused ≥ 140` is unreachable through it, and the build-to-build identity
  churn (bad for `mode=tree` users, fatal for any future incremental
  feature) stays. Becomes relevant again only if the gate fails AND someone
  later decides the floor-only saving isn't enough.
- **Smaller `leaf_k`.** Pays the labelling bill by coarsening retrieval
  granularity tree-wide — a quality cost on an orthogonal axis — and the
  identity instability survives at any k.
- **Close at `partial` now, floor-only.** The floor's ~−23% (vs the card's
  measured 1.30 builds/h) is real but leaves the tree simultaneously
  expensive AND half keyword-degraded forever, with its identity reshuffling
  every build. Taken only if the gate fails.

### What THIS makes harder later

- **The tree becomes path-dependent.** What you have depends on build
  history, not just the corpus; reproducing a tree for debugging needs the
  lineage or a forced cold build. The `init` stamp and
  `topic_build_last_cold_at` are the breadcrumbs.
- **Entrenchment is bounded, not eliminated.** A genuine corpus
  reorganisation waits up to the cold interval for the tree to notice its
  shape (searchability is unaffected — only the partition's *quality* lags).
- **Cold builds periodically re-degrade labels** for the 1-2 builds it takes
  the upgrade pass to drain the debt — a scheduled, visible storm instead of
  today's permanent one.
- **A third clock on `(:Bucket)`** (`dirty_at`, `last_built_at`, now
  `last_cold_at`) — though this one deliberately lives outside the claim's
  duplicated WHERE clauses.
- `seed` on `build_bucket_topic_tree` loses most of its meaning on warm
  builds (only the top-up picks consume RNG) — tests that relied on seed
  variation to produce different trees must force `init=cold`.

### Risks for the coder

- **The weights init is the silent killer**: seed `_weights` wrong and the
  first refined page erases the warm centroids — the build "works", the gate
  numbers just quietly don't reproduce in production. Mirror the cold path's
  semantics exactly (reservoir-assignment counts).
- Leaves are level 0 (`flat_levels` construction, `build.py:695`) — filter
  `previous` to `level == 0` for the warm init; upper levels are rebuilt
  from leaf centroids as today (cheap, deterministic given leaves).
- ONE `read_bucket_topic_labels()` call, moved earlier — not a second read
  (the single-worker claim makes it race-free, keep it that way).
- A re-embed (dimension change) must fall back to cold, same as the reuse
  pass already does.
- The test fixture must wipe `(:Bucket)` *properties*, not just nodes
  (memory `schema-migration-tests-are-blind-when-the-fixture-only-wipes-nodes`
  — it bit the floor tests' design too).
- Don't let the upgrade pass relabel topics the budget already covered — the
  `wanted` set is one pool, upgrades fill the remainder only.

---

## ROUND 2 — the gate passed, and what shipped behind it (2026-10-17)

`aw-knowledgeable@14f8220`, deploy run 38996139111 green (796 pytest, 11 new).

### The gate, run twice

The first gate pass was **vacuous and discarded**: compared on a corpus
byte-identical to the stored tree's, the cold init reproduces itself
(145/145), so warm's 138 said nothing. The 2×2 below adds the delta
(withholding every 1000th chunk from all three passes, ~42 chunks — the same
order as the +27 that broke reuse between 00:39 and 01:39):

| init | corpus unchanged | ~42 chunks changed |
|---|---|---|
| cold (reservoir) | 145/145 | **57/145** — reproduces production's 51/156 |
| **warm (previous tree)** | 138/145 | **135/145** — threshold was 135 |

Warm loses 3 matches to the delta; cold loses 88.

### Live, bucket `main`

```
03:00:33  init=cold  reused 108, upgraded 48, llm 48, keywords 0, llm_attempted 48
03:05:23  warm       reused 129, upgraded 17, llm 27, keywords 0, llm_attempted 27
03:11:33  warm       reused 148, upgraded  0, llm  8, keywords 0, llm_attempted  8
```

Warm confirmed from state rather than inference: `topic_build_last_cold_at`
is still `02:57:55` after both later builds, and a cold build would have
re-stamped it. The stricter, node-level degradation measure: **all 156 topics
now carry `label_source: "llm"`** — zero keyword-degraded topics tree-wide,
from ~56.

Acceptance 1-5 all pass (gate 135; reused 148; keywords 0; llm_attempted 8;
`init` in the report with one cold stamp and two warm builds after it).

### Two caveats, recorded rather than buried

1. **All three production builds ran on an unchanged corpus** (42 337 chunks
   each), and the two warm ones were manual `POST /api/topics/build` calls
   that bypassed the claim and the floor. They prove the upgrade pass and
   that warm start reuses 148/156; the warm-vs-cold **delta** robustness
   still rests on the gate above, not on a scheduler-claimed build with real
   churn. The first of those is the one number left to confirm.
2. **`init` is in the report but not in the log line.** Warm vs cold is
   therefore not readable from `docker logs` — it was verified through
   `topic_build_last_cold_at`. A one-line follow-up to the log format would
   make acceptance 5 checkable the same way everything else here is.
