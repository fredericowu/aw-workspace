# research-search — theme-convergence job: cluster per-paper claim sentences into minted canonical themes, cross-language, reversible

Status: **design, not code.** Architect, 2026-10-24, run
`c82f6022580c4964b26b47ffc64a7b15`. Card
`4035bf3b-9510-8117-812b-fc96926c95a4`. Scope of record is the PO comment on
that card (2026-10-24, run `40a6a55267a8497c8e6873c5aeeb6164`), which
overrides the card body in two places (the live-data premise, and the
cross-language deferral). Repo `aw-app-research-search`, target
`research-search-mcp`.

Builds on, and does not reopen, `research-search-neo4j-graph.md` §4's four
identity guarantees, delivered by N1 (`research_search_app/graph_identity.py`,
`migrations/0009_graph_identity.sql`) and N2
(`research_search_app/graph_db.py`). This is the job §4 named and deferred.

## 0. The measured fact the mechanism is built on

Run live against the 23 themes in `app__research-search__themes` this design
session (2026-10-24), with `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2`
(fastembed ONNX, 384-dim, 0.22 GB, already in the installed fastembed's
supported list):

* The PO's four-way "empirical validation and tradeoffs" must-link cluster has
  pairwise cosines **0.394 to 0.701**. The weakest true pair is 0.394.
* A false cross-topic pair scores **0.544**: `Empirical validation, gains, and
  remaining limitations` (EN, arxiv_2508.0) vs `Construção, fundamentação e
  validação do QCS` (PT, the peer-education canary corpus).
* The PO's named PT↔EN must-link pair (`Arquitetura e pipeline técnico do
  GraphRAG` ↔ `hybrid LLM-plus-graph retrieval architecture`) scores **0.567**,
  so the multilingual model does carry PT↔EN at sentence length.
* The two PT peer-education papers link to each other at 0.565 to 0.648, which
  is a *true* cross-paper cluster inside the canary corpus. "Canary stays
  isolated" means isolated from the tech corpus, not from its own sibling.

Consequence, and the one falsifiable claim of this design: **no cosine
threshold separates true from false pairs on this data** (a threshold above
0.544 breaks a must-link cluster; one below 0.394 admits a canary merge). So
similarity cannot be the gate. It is only the *proposer*, run generously, and
the LLM confirm is the entire decision. This is the PO's decision 2 with a
measured reason attached, and it kills every "auto-merge above a high
threshold, LLM only for the gray zone" variant.

## 1. The approach, in sentences someone can disagree with

1. **One pipeline: embed → propose → LLM-partition → gate → apply → rebuild.**
   Embed every *active* theme's display `label` (never `norm_label`, see §5)
   with the multilingual model; build candidate components over pairs with
   cosine ≥ 0.35 (below the weakest measured must-link, with margin); hand
   each component that spans ≥2 documents to the LLM with member labels plus
   their documents' titles; the LLM partitions the component into clusters
   that denote the *same specific theme* (singletons allowed, splitting the
   component is expected), and mints a short topical label per cluster.
2. **Apply = mint a new canonical row, point members at it.** Per the PO: a
   new `themes` row (fresh uuid4, `merged_into NULL`), every member's
   `merged_into` set to it. No winner election among existing labels, ever.
   The schema permits this today: `merged_into` is a plain self-FK and the
   partial unique index only constrains `merged_into IS NULL` rows
   (`migrations/0009_graph_identity.sql`, themes block). Confirmed by reading
   the index definition; the coder proves it by executing one.
3. **Every merge is an audit row** (`theme_merges`): members, per-pair
   similarity stats, the embedding model name, the LLM's rationale and the
   agent/model that produced it, timestamps. The audit row is also the
   un-merge handle. An unexplainable merge is undiagnosable (PO decision 2).
4. **Un-merge is a first-class v1 endpoint**, idempotent, restricted to
   merges whose canonical is still active, and it is *demonstrated live* as
   part of delivery, not asserted from code reading.
5. **On-demand only**: `POST /api/graph/converge` (202 + DB-backed job row +
   poll, the `review_generate_jobs` pattern), an MCP tool on the
   `research-search-index` server, and a small affordance in the existing
   `ui/src/views/DocGraph.jsx`. No cron, no per-save hook.
6. **Postgres first, graph second, degrade-never-crash.** The merge is
   durable once the Postgres rows are written; the job then replays the graph
   via the existing `graph_db.rebuild()`. If Neo4j is down the job still
   succeeds and reports the graph as stale (contract rule C4; §7.1 of the
   N-design). A rebuild later heals it, which is the entire reason Postgres
   is the system of record.

## 2. Where it lands — files and seams

### 2.1 Migration `0010_theme_merges.sql` (number provisional)

`research-search-neo4j-vector-store.md` §3.5 reserves 0010/0011 for the
V1/V3 vector cutover, but neither file exists in `migrations/` yet
(`ls migrations/` tops out at 0009). Whichever card lands first takes the
number; the loser renumbers, same precedent as 0008→0009 (named in 0009's own
header). Two tables, both under the enforced `app__research-search__` prefix,
and **no `:word` sequence anywhere in the file, comments included**
(`tests/test_migrations_bind_params.py` guards; 0009's header records the
release this cost).

* **`app__research-search__theme_merges`** — the audit and revert handle:
  `merge_id uuid PK`; `canonical_theme_id uuid NOT NULL REFERENCES themes
  (theme_id)`; `members jsonb NOT NULL` (list of `{theme_id, label,
  norm_label, document_ids, sim_to_canonical_centroid}` snapshotted at merge
  time); `metrics jsonb` (`{embed_model, threshold, mean_pairwise,
  min_pairwise}`); `rationale text NOT NULL` (the LLM's, verbatim);
  `llm_model text`; `job_id uuid`; `created_at timestamptz`; `reverted_at
  timestamptz NULL`. No FK from `members` (jsonb snapshot is provenance, not
  state).
* **`app__research-search__converge_jobs`** — the 202/poll row, WORKERS>1
  honest for the same reason `review_generate_jobs` (0006) is: the worker
  polled is generally not the worker running the job. `id uuid PK`, `state`
  (`running|done|failed` with heartbeat-staleness takeover copied from
  `review_generate.py:602-607`), `params jsonb` (`threshold`, `dry_run`),
  `report jsonb` (proposals with scores and rationales, applied merge_ids,
  skip reasons, graph sync outcome), `error text`, `created_at`,
  `heartbeat_at`, `updated_at`. One job may run at a time, corpus-global
  (claim via the same conditional-UPDATE single-flight as
  `review_generate.py`).

### 2.2 New module `research_search_app/theme_converge.py`

Pure functions + a store over `ctx.db`, house shape per
`graph_identity.py`/`review_generate.py`. Owns:

* **Embedding**: its own lazy singleton for
  `paraphrase-multilingual-MiniLM-L12-v2` via fastembed, same
  `FASTEMBED_CACHE_PATH` mechanism as `store.py:140-152`. It must NOT touch
  `store.py`'s nomic models or pgvector: convergence embeddings are
  **ephemeral, in-memory, recomputed per run** (23 to ~200 labels, well under
  a second), deliberately not persisted, so this card has zero coupling to
  the V1-V3 pgvector→Neo4j vector migration.
* **Proposing**: cosine matrix over active themes' `label` text; union-find
  components over pairs ≥ `converge_threshold` (config, default **0.35**);
  drop components whose members span one document.
* **LLM confirm**: one AP-MT `/v1/chat/completions` call per component (at
  23 themes that is 1 to 3 calls) through the existing transport
  (`review_generate.call_llm_apmt` + `_call_llm_apmt_with_remint`, reused,
  not reimplemented), model `agent/research-theme-convergence` by default,
  config-overridable via `converge_llm_model`. Prompt carries member labels
  with their documents' titles; response is a strict JSON partition:
  `clusters: [{member_theme_ids, minted_label, rationale}]`, singletons
  listed as such. Malformed JSON fails the whole component (logged, reported,
  nothing applied), never a partial parse.
* **Gate** (what "auto-apply above the gate" means operationally): a cluster
  is applied iff the LLM returned it as one theme, it spans ≥2 distinct
  documents, and the minted label survives the collision rule below.
* **Apply**, idempotent and crash-safe without transactions.
  `DbTables.execute` gives no multi-statement transaction
  (`graph_identity.py:51-55`), so the order is: (1) insert the canonical
  `themes` row; (2) insert the `theme_merges` audit row; (3) per-member
  `UPDATE themes SET merged_into = :canonical WHERE theme_id = :member AND
  merged_into IS NULL`. Every step idempotent; the job *starts* by sweeping
  audit rows whose member set is not fully merged and completing them, so a
  crash between (2) and (3) is healed by the next run rather than producing
  a second canonical.
* **Collision rule for the minted label**: `normalize_label(minted)` must
  collide with NO existing `norm_label`, active or merged. On collision,
  re-ask the LLM once with the exclusion list in the prompt; still colliding,
  skip the cluster with a reported reason. This sidesteps both the partial
  unique index violation and the "minted label equals a member's label"
  case, which would otherwise be winner-election through the back door.
* **Un-merge** (`revert_merge(merge_id)`): 409 if the canonical's own
  `merged_into` is not NULL ("revert the later merge first" keeps the
  graph-returns-to-prior-edges guarantee honest); else (1) per-member
  `SET merged_into = NULL WHERE merged_into = :canonical`; (2) re-stamp any
  `review_coding` row stamped directly on the canonical by a post-merge
  ingest (its label's `norm_label` maps back to a member from the audit
  snapshot; a row matching no member keeps the canonical, which then stays
  active instead of being deleted); (3) delete the canonical row only when
  nothing references it (no coding rows, no `merged_into` pointers); (4) set
  `reverted_at`; (5) rebuild. Each step idempotent, re-runnable.
* **Graph sync**: after apply or revert, call `graph_db.rebuild()` (patient
  connect, `attempts=5`, exactly `api/graph.py:_rebuild`). Rebuild rather
  than per-document sync because `_DROP_ORPHAN_THEMES` only runs there, and
  merged-away theme nodes would otherwise linger as `health()` drift. At 40
  documents a rebuild is seconds; the job report carries the outcome, and a
  `GraphUnavailable` here degrades the report, never fails the job.

### 2.3 `graph_db.py` — pay the named debt

`postgres_counts` (`graph_db.py:640-646`) says in its own docstring that its
`has_theme`/`themes` counts over-count once merges exist and that "the fix
belongs to the card that ships the job". That card is this one: both counts
must map `review_coding.theme_id` through `_canonical_theme` (Python-side,
reusing its cache; a recursive CTE is unavailable through `DbTables`, sharp
edge 1) before `COUNT(DISTINCT ...)`. Without this, the first applied merge
makes `GET /api/graph/health` cry drift forever and `doctor` goes
permanently yellow. `_theme_rows` (`graph_db.py:375-399`) already collapses
onto canonical identity and needs nothing.

### 2.4 Routes — new `research_search_app/api/converge.py`

A separate router, not additions to `api/graph.py`: N4 (`/graph/import`,
`/ws/graph`) is specced into that file and a separate module is what the
`api/__init__.py` convention exists for. Registered under the same `/graph`
prefix family:

* `POST /graph/converge` `{threshold?, dry_run?}` → 202 `{job_id}`; echoes
  the running job if one exists (single-flight).
* `GET /graph/converge/{job_id}` → state + report. Dry-run reports proposals
  and writes nothing else.
* `GET /graph/merges` → audit list (applied and reverted), newest first.
* `POST /graph/merges/{merge_id}/revert` → the un-merge; 404 unknown, 409
  per the rule above, 200 with the revert report.

202 + poll is forced, not chosen: the LLM confirm via AP-MT takes 15-45 s
per call (`review_generate.py:85,146`) and the tunnel cuts synchronous edges
at 30 s (the reason `POST /corpus/index` is already a 202).

### 2.5 Contributed agent + credential scope

`aw-app.json` `contributes.agents` gains `research-theme-convergence`,
shaped exactly like `research-review-generator` (JSON-only system prompt, no
tools, low temperature, an existing contributed model slug). Two things the
coder must not miss:

* `apmt_credential.py:133` mints the scoped ApiKey with
  `agent_slugs=[AGENT_SLUG, WORKFLOW_SLUG]` — the new slug must be added
  there, and the existing remint-on-reject path
  (`_call_llm_apmt_with_remint`) is what heals keys minted before the change
  (a stale key 401/403s on the new slug, triggering a remint that includes
  it). Verify that live, don't assume it.
* Contributed agents are reconciled at marketplace install/update, so the
  agent ships with the version bump; no manual seeding.

### 2.6 MCP tools — `research_search_app/mcp_ingest.py`

Three entries in `_TOOLS_SCHEMA`, thin wrappers over the same functions the
routes call (the `generate_review`/`generate_review_status` precedent,
`mcp_ingest.py:198-240`): `converge_themes` (dispatch, `threshold`/`dry_run`
args), `converge_themes_status` (poll by job id, DB-backed), and
`revert_theme_merge` (by `merge_id`; also returns the merge list when called
with no id, so the id is discoverable without a fourth tool). Names collide
with nothing on the gateway today (checked against the live tool list; the
`search_graph` collision precedent is why this is checked at design time).

### 2.7 UI — `ui/src/views/DocGraph.jsx`

The N3 view exists and is the "callable from the DocGraph area" surface the
PO named: a "Converge themes" action that POSTs and polls, a job-state line,
and a merges list with per-row revert. No modals (app iframes have none),
no approval queue (deliberately not built, PO decision 5). Smallest possible
surface; the job report JSON is the real UX.

### 2.8 Untouched

`store.py` (both nomic models, pgvector), `graph_identity.py` (all three
resolution passes already follow merge chains: `_active_theme`
`graph_identity.py:344`, `ensure_theme`'s refusal to mint on a broken chain
`:389`), the Neo4j schema and every Cypher statement in `graph_db.py` (the
apply path writes Postgres and replays; no new labels, so
`tests/test_graph_prefix_guard.py` is unaffected), `review.py`'s
`replace_ai_coding` (codes stay per-paper), the AI Generate prompt (P2's
card), the citation side.

## 3. What was rejected, and why

* **Near-duplicate label merge (normalize / string similarity / fuzzy
  match).** The card body's premise. Merges zero of the 23 live themes; they
  are per-paper claim sentences sharing no content words. Measured by the PO
  against the live tables; re-confirmed by the cosine spread in §0.
* **Similarity threshold as the gate (auto-merge above τ, LLM for the gray
  zone).** No τ exists: 0.544 false pair vs 0.394 true pair (§0). The LLM
  confirms everything that gets applied, full stop.
* **Electing a winner label among members.** PO decision 1; makes the node
  label a lie about the other members. Also re-derivable here: the four
  empirical-cluster sentences are each paper-specific claims, none describes
  the cluster.
* **LLM-only clustering (skip embeddings, send all labels in one prompt).**
  Viable at 23 labels and tempting, but it discards the persisted similarity
  score the PO requires with each merge, has no principled way to bound the
  prompt as the corpus grows, and makes "why did these two end up in one
  prompt" unanswerable. The embedding prefilter is cheap (sub-second, local,
  ONNX) and is what makes the audit row diagnosable.
* **Persisting theme-label embeddings (pgvector column or Neo4j vector
  index).** ~200 sentence embeddings recompute in under a second; persisting
  them would couple this card to the in-flight V1-V3 vector-store migration
  for zero gain and give drift a new place to live.
* **Reusing `nomic-embed-text-v1.5`.** English-leaning; cross-language is in
  v1 by PO override and the PT↔EN pair is the acceptance criterion. The
  multilingual MiniLM measured 0.567 on exactly that pair. Second model costs
  0.22 GB in the existing fastembed cache and no new pip dependency.
* **Fixing `normalize_text`'s accent-stripping now.** Its docstring says the
  fix needs a backfill, and the PO routed the defect to the P2 card
  (companion card `4035bf3b-9510-8128-a968-cd172da904f4` "also carries the
  normalize_text accent-stripping defect"). This design routes around it
  instead: nothing in the pipeline reads or compares `norm_label` (see §5
  risk 1), so the defect cannot drop a PT row here.
* **Direct LLM call to a provider.** Standing directive: apps' LLM calls go
  through AP-MT `/v1` with the self-minted scoped ApiKey. The transport,
  retry, remint and failed-run-as-HTTP-200 parsing all already exist in
  `review_generate.py`; a second transport would be a second place for the
  same bugs.
* **Cron / per-save trigger, approval-queue UI, retrieval feedback.** All
  explicitly out of scope by the PO; not re-litigated.
* **A `kind='dag'` workflow for the confirm call.** The literature-review
  DAG exists for a 6-9 node multi-model pipeline. This is one structured
  completion per component; `agent/<slug>` is the cheapest thing that works
  and the model remains swappable via config.

## 4. What this makes harder later

* **English minted labels bake an anglophone lens into the graph.** The
  minted label is specced EN-default (rationale: the graph is the
  cross-corpus lens and the only cross-paper cluster today is PT↔EN). P2's
  controlled vocabulary will inherit this choice; if Frederico wants PT or
  bilingual labels, it is a prompt-line change but a vocabulary migration
  later.
* **`merged_into` chains deepen with every re-run** (a canonical can become
  a member of a later merge). `_canonical_theme`/`_active_theme` walk chains
  correctly, but revert is restricted to chain heads, so unwinding an old
  merge means unwinding everything after it, newest first. Fine at this
  scale; a "flatten chains" maintenance pass is the future card if it ever
  isn't.
* **The audit row snapshots state as jsonb.** Provenance, not live state: a
  hand-edited theme row can disagree with the snapshot, and revert trusts
  the snapshot for re-stamping. Accepted; the alternative (normalized member
  table with FKs) buys consistency nobody reads at the cost of revert logic
  that can be invalidated by the very merge it is reverting.
* **`postgres_counts` goes Python-side for canonicalization**, O(membership
  rows) per health call. Trivial now; the named upgrade is pushing the merge
  map into SQL once `DbTables` grows a way to run a recursive CTE.
* **One-LLM-call-per-component caps out** somewhere around low-thousands of
  themes per component. The escape hatch (chunk a component across calls,
  merge-confirm across chunks) is named here so the 10x-corpus card doesn't
  start from zero.
* **A second embedding model in the image/cache** (third counting the vision
  model): 0.22 GB more disk, one more first-call download. The host disk
  runs hot (90%+); the model is small but it is not free.

## 5. Risks for the Coders — where this breaks non-obviously

1. **`norm_label` is a trap for exactly the rows that matter.**
   `normalize_text` strips accented characters outright
   (`graph_identity.py:91,153`): `técnico` → `t cnico`. Ten of 23 live
   themes are PT. The pipeline must embed and prompt with the display
   `label` and must not prefilter, join, or dedupe on `norm_label` anywhere.
   The one place `normalize_label` is legitimately used is the minted
   label's collision check (§2.2), where lossy-but-deterministic is
   acceptable because both sides of every future comparison go through the
   same function.
2. **No transactions, by platform design.** Each `ctx.db.execute` is its own
   `engine.begin()` (`graph_identity.py:51-55`). The apply/revert orders in
   §2.2 are load-bearing; reordering them "for clarity" reintroduces the
   partial-unique-index violation (canonical inserted after members point at
   it fails the FK; members merged before the canonical exists fails the
   FK the other way) or strands half-applied merges with no recovery sweep.
3. **AP-MT `/v1` answers a FAILED run with HTTP 200** and the failure as
   assistant text (`review_generate.py:456-463`, `_APMT_RUN_FAILED_RE`).
   Reuse the existing parsing; a naive `choices[0]` read will happily apply
   merges from an error message. Likewise the key-scope change (§2.5): an
   ApiKey minted before this card 403s on the new agent slug, and only the
   remint path heals it — test with a stale key, not just a fresh install.
4. **Migration file rules bite twice**: no `:word` anywhere including
   comments (SQLAlchemy `text()` eats it, psql does not, failure is logged
   not fatal, the app then serves routes whose tables do not exist), and the
   0010 number may be taken by vector-V1 at rebase time.
5. **The canary is a *proposal-time* false positive, by measurement.** At
   threshold 0.35 the 0.544 QCS↔empirical-validation pair WILL be proposed;
   the test that matters asserts the LLM gate rejects it and the two
   peer-education papers never join a tech cluster. A test that only checks
   the proposer never sees the failure mode. Conversely the two PT
   peer-education papers linking to *each other* is correct behaviour, not a
   canary breach — assert isolation from the tech corpus, not total
   isolation.
6. **Acceptance is measured after the P0 coverage backfill** (card
   `4035bf3b-9510-8123-ad01-d3a9b3b7c0aa`): today only 7 of 40 documents are
   themed, so "≥8 theme nodes with ≥2 documents" is not reachable on the
   current rows. Build and test against the 7-document set (the PT↔EN pair
   and the canary are both in it); the ≥8 gate runs post-P0 with a fresh
   converge run. Do not serialize the build on P0.
7. **First converge call downloads 0.22 GB** (no installer pre-warm exists,
   `installer.py` has none for the existing models either). The 202 pattern
   absorbs it, but the job's first heartbeat window must tolerate the
   download, and the report should name the download when it happens so a
   slow first run is diagnosable.
8. **Dry-run must be genuinely dry.** It still runs embeddings and the LLM
   (that is its point) but writes only the job row. The temptation is to
   reuse the apply path with a flag threaded through; keep the write step
   separate instead so the flag cannot be dropped by a refactor.

## 6. Acceptance (the PO's, restated as the job's own report)

After P0 backfill + one converge run: ≥8 theme nodes carrying ≥2 distinct
documents; `GET /api/graph/neighborhood?root=arxiv_2405.2&depth=2` reaches
another corpus document through a theme node with no citation edge;
arxiv_2501.0's `Arquitetura e pipeline técnico do GraphRAG` and
arxiv_2507.0's `hybrid LLM-plus-graph retrieval architecture` share one
canonical `theme_id`; the two peer-education crossref papers share no
canonical theme with any arxiv paper; and one live merge is reverted via
`POST /graph/merges/{id}/revert` with the neighborhood payload returning to
its pre-merge edge set. `GET /api/graph/health` reports zero drift after
every one of those steps (§2.3 is what makes that true).

## 7. Decomposition (TC1-TC3)

| Card | Scope | Depends on |
|---|---|---|
| **TC1 — convergence backend** | migration (`theme_merges`, `converge_jobs`), `theme_converge.py` (embed/propose/confirm/gate/apply + recovery sweep), `api/converge.py` (converge + status), contributed agent + credential scope, rebuild-on-apply, `postgres_counts` merge-map fix, tests incl. canary-rejection and collision rule | nothing (N1/N2 merged) |
| **TC2 — un-merge, live** | revert endpoint + `GET /graph/merges`, revert semantics of §2.2, **live end-to-end demo on the real corpus** (merge → neighborhood before/after → revert → neighborhood restored → health drift 0), posted as evidence on the card | TC1 |
| **TC3 — MCP tools + DocGraph affordance** | `converge_themes`/`converge_themes_status`/`revert_theme_merge` in `mcp_ingest.py`, DocGraph.jsx action + merges list + revert | TC1 (TC2 for revert surface) |

TC1 and the P0 coverage backfill card are independent and parallel-safe; the
acceptance *measurement* is gated on P0, the build is not.
