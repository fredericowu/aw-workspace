# research-search — pgvector semantic search → Neo4j native vector index; drop the legacy pgvector tables

Status: **design, not code.** Architect, 2026-10-24. Card
`4035bf3b-9510-813a-8dc1-c511e4030719`
(`feature:research-search-pgvector-to-neo4j-migration`).

This is Frederico exercising the veto point that both prior designs named
([`research-search-graph-layer.md`](research-search-graph-layer.md) §4.1,
[`research-search-neo4j-graph.md`](research-search-neo4j-graph.md) §5.1 —
"moving vector search into Neo4j … rejected, veto point"). His instruction
(Telegram, PT-BR, verbatim on the card): *"sim precisamos mover do pgvector
pro neo4j … se [o pgvector do researcher] for só dele, temos que migrar e
deletar ele completamente"* — with the explicit carve-out that the shared
Postgres/pgvector **extension** (used by uc-phd, kb, and others) is not
touched. The card's attached investigation confirmed research-search's
vector tables are exclusively its own. So: the decision to move is made and
is not relitigated here; this design decides **how**, and what "delete
completely" safely means.

Every claim below is grounded in a file read this session (2026-10-24) or a
live inspection of the running `aw-app-neo4j` container this session.

---

## 0. Live facts this design stands on (verified 2026-10-24)

| Fact | Value | Evidence |
|---|---|---|
| Corpus | 40 documents, 2,895 chunks, 570 figures | carried from 2026-10-23 live verification, unchanged |
| Embeddings | text `nomic-ai/nomic-embed-text-v1.5` + a vision model, **both 768-dim**, fastembed/ONNX in-process | `research_search_app/store.py:77` (`VECTOR_DIM = 768`), `store.py:193` (`embed_images`), `migrations/0002` (`visual_embedding public.vector(768)`) |
| pgvector surface | `chunks.embedding`, `figures.visual_embedding`, `figures.caption_embedding` (nullable), 3 HNSW cosine indexes | `migrations/0001_create_documents_and_chunks.sql`, `0002_create_figures.sql` |
| `documents` has **no vector column** | plain relational registry: id, source, paper_id, doi, title, pdf/md paths, md_sha256, page_count, embed_model, embed_dim, starred, preferred_language, figures_indexed_at | `migrations/0001` + `0003/0004/0008` |
| Who FKs / reads `documents` | `chunks`, `figures` (CASCADE), review tables (0005–0007), `ref_targets` (0009), `graph_db.py:577,654`, `graph_identity.py:327,619` | file reads this session |
| Read paths being migrated | `VectorStore.search` (chunks ⋈ documents, excludes `_PENDING_SHA` docs, `store.py:614-633`); `VectorStore.search_figures` (two independent rankings, optional `document_id` filter, `store.py:578-610`) | file reads |
| Write path being migrated | `replace_document` (upsert doc with `_PENDING_SHA` → delete chunks → embed+insert batches → stamp real sha, `store.py:441-493`); `_index_figures` via `replace_figures` + `figures_indexed_at` gate | `store.py`, `ingest.py:53-116` |
| Public contracts on top | MCP tools `index_paper`, `get_indexed_paper`, `list_indexed_papers`, `index_status`, `search_paper_figures` (`mcp_ingest.py:46-113`); `GET /corpus/search` (`api/corpus.py:76-85`) | file reads |
| `aw-app-neo4j` | **live**, Neo4j **5.26.4 Community**, container 1536 MB / 1.0 cpu, heap 512m, pagecache 256m | `docker inspect aw-app-neo4j` this session |
| N-design state | migration `0009_graph_identity.sql` + `graph_identity.py` + `graph_db.py` (driver adapter, `connect()`/`GraphSession`, rs-prefix schema, `sync_document`/`rebuild`) exist on disk; N2 tests being edited **during this session** | `graph_db.py:169-231,472-516`; codegraph index note on `tests/test_graph_db.py` |
| Neo4j 5.26 vector indexes | native since 5.13/5.15 (GA), Community included, ≤4096 dims, cosine/euclidean, `CREATE VECTOR INDEX … IF NOT EXISTS`, `db.index.vector.queryNodes(name, k, vec) YIELD node, score`, `db.create.setNodeVectorProperty` stores float32; **no filter argument**; population is async | Neo4j 5.26 docs; consistent with the 2026-10-12 reversal's findings |

---

## 1. The approach, in sentences someone can disagree with

1. **The embeddings move; the registry does not.** `chunks` and `figures`
   — the only tables containing vector data — become `rs__Chunk` /
   `rs__Figure` nodes in the shared `aw-app-neo4j` instance, under three
   native 768-dim cosine vector indexes. The `documents` table **stays in
   Postgres**: it has zero pgvector content and it is the FK anchor for
   the review tables (0005–0007), `ref_targets` (0009), and the whole
   N-design's "Postgres is system of record" spine. This narrows the
   card's literal "drop migrations 0001/0002's tables
   (documents/chunks/figures)" — **flagged for veto in §5.1**, because
   dropping `documents` would break five live tables and the graph layer
   for zero pgvector gain. After this ships, research-search has **no
   pgvector dependence at all**: no vector columns, no `public.vector`
   casts, no `pg_extension` probe. That is the "delete it completely"
   Frederico asked for, scoped to what is actually his to delete.
2. **Vectors are copied, not re-embedded.** The 40-doc corpus migrates by
   streaming existing embeddings out of Postgres into Neo4j byte-for-byte
   (same model, same dim — `documents.embed_model/embed_dim` stay as the
   provenance stamp). That makes dual-store verification meaningful:
   identical vectors in both stores means any ranking divergence is HNSW
   recall, not embedding drift.
3. **Cutover is sequenced releases, not a runtime flag**: dual-write +
   backfill first (reads still pgvector), then a verification endpoint
   compared on the live corpus, then reads flip, then — only after
   verified — a drop migration. Single-user app, fully rebuildable store;
   a config flag would be permanent surface for a one-time transition.
4. **Rebuildability survives the move.** Losing Neo4j ≠ losing the corpus:
   `documents` + the on-disk artifacts (`md_path`, figures dirs, PDFs)
   remain sufficient to re-embed everything (that is today's crash-recovery
   story too). This is what makes aw-app-neo4j's deferred backups
   acceptable for a store that now holds a core feature's data.
5. **Degradation scope grows, and we say so**: with pgvector gone,
   semantic search (`/corpus/search`, `search_paper_figures`) and ingest
   (`index_paper`) require Neo4j up. C4 ("degrade, never crash") still
   holds at the app level — boot never blocks, every failure surfaces as
   a typed degraded state — but "graph features disable" becomes "graph
   **and search/ingest** features disable". Accepted as the direct
   consequence of the instruction; mitigations in §3.6.

---

## 2. Neo4j schema (namespaced per the frozen aw-app-neo4j contract, C5)

```
(:rs__Chunk  {uid, document_id, ordinal, page_number, chunk_text,
              embedding: float[768]})
(:rs__Figure {uid, document_id, page_number, figure_index, image_path,
              caption_text, visual_embedding: float[768],
              caption_embedding: float[768]?})
```

- `uid` is a **synthetic single string key** — `"<document_id>:<ordinal>"`
  for chunks, `"<document_id>:<page>:<figure_index>"` for figures — with a
  plain uniqueness constraint `rs_chunk_uid` / `rs_figure_uid`.
  Single-property on purpose: composite node-key constraints are
  Enterprise-only; the synthetic key keeps concurrent `MERGE` convergent
  on Community (same reason `graph_db.py`'s `connect(schema=True)` applies
  the `rs__Paper.key` constraint before writes).
- Range indexes `rs_chunk_document_id`, `rs_figure_document_id` — the
  per-document delete (re-index) and the per-document figure filter.
- Vector indexes, all 768-dim cosine, created `IF NOT EXISTS`:
  `rs_chunk_embedding_vec`, `rs_figure_visual_vec`,
  `rs_figure_caption_vec`. A figure without a caption simply lacks the
  property and is absent from the caption index — the nullable-caption
  semantics of migration 0002 carry over for free.
- Embedding properties are written with `db.create.setNodeVectorProperty`
  (float32 storage, dimension-validated at write) — never a plain `SET`.
- **No edges in v1.** `rs__Chunk`/`rs__Figure` are property-keyed on
  `document_id`, not linked to `rs__Paper`: paper nodes are minted by N2's
  `sync_document` on a different key lifecycle (`ref_targets.key`), and
  search needs no traversal. `(:rs__Chunk)-[:RS_CHUNK_OF]->(:rs__Paper)`
  is the named deferred nicety (future GraphRAG adjacency), not a blocker.
- The two rankings of `search_figures` stay **separate** (visual vs
  caption, never fused) — the 0002 header's measured 8–13× scale mismatch
  is a property of the embeddings, not of the store, and survives the move.

## 3. Where it lands — files and seams

### 3.1 New module: `research_search_app/neo4j_vectors.py`

The vector store over Neo4j. It does **not** import the neo4j driver:
it takes a `GraphSession` from `graph_db.connect()` (`graph_db.py:169` —
that module stays the only driver importer, K5 discipline intact). The C5
rs-prefix guard test extends to cover this module's Cypher. Owns:

- `ensure_vector_schema(session)` — constraints + range + vector indexes,
  idempotent, called from the write paths (same `schema=True` rationale as
  `graph_db.connect`).
- `replace_chunks(session, document_id, rows)` / `replace_figures(...)` —
  delete-by-`document_id` first, then `UNWIND` batches (≤100 rows; a
  768-float param list per row is big — never one giant statement),
  `MERGE` on `uid`, `db.create.setNodeVectorProperty` per embedding.
- `search_chunks(session, query_vec, top_k)` —
  `CALL db.index.vector.queryNodes('rs_chunk_embedding_vec', k', $vec)`.
- `search_figures(session, query_vec, document_id, top_k)` — corpus-wide:
  two `queryNodes` calls (visual, caption). With a `document_id` filter:
  **exact brute-force** over that document's figures
  (`MATCH (f:rs__Figure {document_id: $id})` +
  `vector.similarity.cosine(f.visual_embedding, $vec)`) — `queryNodes`
  takes no filter, and exact scoring over ≤~50 figures beats over-fetching
  an ANN index. Corpus-wide chunk search needs no filter (the pending-doc
  exclusion happens at metadata join, §3.3).
- `counts(session)` / `counts_for(session, document_id)` — the
  stats/verification source.
- **Score→distance conversion, pinned here once**: Neo4j cosine score ∈
  [0,1] is `(cos+1)/2`; pgvector `<=>` is `1−cos`. Every public contract
  (`search_paper_figures`'s `distance`, `/corpus/search`) keeps
  lower-is-closer cosine distance: `distance = 2·(1−score)`. A unit test
  pins this with known vectors — it is the one place the migration could
  silently invert ranking semantics.

### 3.2 Write path — `store.py.replace_document` / `ingest._index_figures`

Shape preserved, chunk/figure destination swapped:

1. Upsert `documents` row with `_PENDING_SHA` (Postgres — unchanged,
   `store.py:460-476`).
2. `neo4j_vectors.replace_chunks(...)` — delete doc's chunks, write
   batches (embed per batch as today, `store.py:483-487`).
3. Stamp real `md_sha256` (Postgres — unchanged).
4. Figures: same, with `figures_indexed_at` stamped in Postgres only after
   `replace_figures` returns (the 0002 crash gate, unchanged).

The `_PENDING_SHA` machinery is **kept, not rebuilt**: it lives in the
Postgres `documents` row, and `DbTables.execute`'s no-cross-call-
transactions constraint is irrelevant to Neo4j writes (each batch is its
own bolt transaction). A crash mid-write still leaves a pending sha and the
resume check still reprocesses. If `graph_db.connect` raises
`GraphUnavailable`, `index_paper` returns the **existing retryable shape**
`{"ok": False, "unavailable": True, "error": ...}` (`ingest.py:63-67` —
the pdf-venv precedent): says nothing about the document, never recorded
to `ingest_failures`, caller retries when Neo4j is back.

### 3.3 Read paths — `store.py.search` / `search_figures` + their wrappers

- `search_paper_text`/`VectorStore.search`: embed query (fastembed,
  unchanged, off-event-loop rules unchanged) → `neo4j_vectors.search_chunks`
  with k′ = k + margin → join metadata from Postgres `documents` by
  `document_id` (one `WHERE id = ANY` query) → drop hits whose doc is
  missing or `_PENDING_SHA`-stamped → trim to k. The join was already
  there (`store.py:626-633`); it just moves from SQL to assembly, and it
  is what keeps the half-indexed-document exclusion working.
- `search_paper_figures`: per §3.1; response shape (`visual`/`caption`
  lists, `distance` field) byte-compatible with today
  (`mcp_ingest.py:109-113` consumers unaffected).
- `index_status` / `corpus` probe (`api/corpus.py:93`): the
  `pg_extension` probe is replaced by (a) Postgres `documents` table probe
  and (b) bolt reachability via `graph_db.connect(attempts=1)` — two
  **distinct** degraded states (`db_unavailable` vs
  `vector_store_unavailable`), both surfaced in `GET /status` so `doctor`
  sees them (silent degradation is this workspace's failure mode).
- `get_indexed_paper` / `list_indexed_papers`: chunk/figure counts come
  from new denormalized `documents.chunks_count` / `figures_count`
  columns stamped at ingest (migration, §3.5) — corpus **listing** keeps
  working with Neo4j down; only search/ingest degrade.

### 3.4 Backfill + verification — `POST /api/corpus/vector-backfill`, `/vector-verify`

- **Backfill** (one-time, idempotent, re-runnable): stream
  `chunks`/`figures` rows from Postgres (`embedding::text` parsed — no
  re-embedding), batch into Neo4j via §3.1's replace functions, then wait
  for the vector indexes to be ONLINE (`SHOW INDEXES` poll — population is
  async; verifying against a still-populating index reads as missing
  rows). Reports per-document counts.
- **Verify**: (a) per-document chunk/figure/caption counts equal in both
  stores; (b) a fixed probe set (each document's title + a handful of
  fixed phrases, ~50 queries) run through **both** stores, overlap@10
  compared. Same vectors both sides ⇒ divergence is HNSW recall only;
  threshold overlap@10 ≥ 9/10 mean, any query < 7/10 listed for
  inspection. Output is a JSON report the cutover card attaches as
  evidence. Both endpoints exist only while both stores do (V1–V2 window).

### 3.5 Postgres migration `0010_vector_cutover_prep.sql` and `0011_drop_pgvector_tables.sql`

- `0010` (ships with V1): `ALTER TABLE documents ADD COLUMN IF NOT EXISTS
  chunks_count integer`, `figures_count integer` (backfilled from the live
  tables in the same file — plain SQL, no vector types, nothing
  `public.`-qualified).
- `0011` (ships with V3, **only after V2's verification evidence is on the
  card**): `DROP TABLE IF EXISTS chunks`, `DROP TABLE IF EXISTS figures`
  (quoted full names). **Not dropped**: `documents` (§1.1),
  `ingest_failures` (lives in 0001 but holds no vectors and is live refusal
  machinery). The shared Postgres instance and the `vector` extension are
  untouched — they are uc-phd's and kb's infrastructure (the card's own
  carve-out).
- Both files obey the house migration rules: hyphenated names
  double-quoted; **no `:word` sequence anywhere, comments included**
  (SQLAlchemy `text()` eats it and the failure is logged, not fatal —
  recorded trap).

### 3.6 Degradation + recovery runbook (what replaces "pgvector is always there")

- Neo4j down → search endpoints return the typed 503 the corpus tab
  already renders for a degraded store; `index_paper` returns
  `unavailable: true`; boot and corpus listing unaffected. Nothing
  crash-loops (C4; aw-knowledgeable's 2026-10-21 boot-loop is the recorded
  anti-pattern).
- Neo4j data lost (no backups by contract, deferred with a named trigger)
  → **re-index from disk**: `documents` + `md_path`/figures dirs survive;
  a re-run of the ingest path per document rebuilds nodes and vectors.
  Cost at 40 docs: re-embedding 2,895 chunks + 570 figures on CPU, order
  of tens of minutes — acceptable, documented, and the reason §1.4 holds.
  (The backfill endpoint cannot restore embeddings after 0011 drops the
  source tables — re-indexing is the recovery path from then on.)

---

## 4. Resource sizing (the card's explicit question)

Measured live: container limit 1536 MB, heap 512m, pagecache 256m,
Neo4j 5.26.4 Community. The vector payload is 2,895 chunk + 570 visual +
≤570 caption vectors ≈ 4,000 × 768 dims × 4 B (float32 via
`setNodeVectorProperty`) ≈ **13 MB raw**, perhaps 3–4× with HNSW graph
overhead — well under 50 MB against 256m of pagecache, plus chunk-text
properties (~4 MB). **No resizing needed.** Named trigger to revisit heap/
pagecache (and whether vectors deserve their own instance): corpus
≥ ~100k chunks, or an embedding-dimension bump. The N2 identity-graph
workload (≤3k tiny nodes) is unaffected by sharing.

## 5. What I rejected, and why

1. **Dropping `documents` too** (the card's literal list). It contains no
   vector data, and five live surfaces FK or read it: `review_notes`,
   `review_generate_jobs`, `review_coding`, `ref_targets` (0009,
   `ON DELETE SET NULL` to it), `import_jobs` (N-design), plus
   `graph_db.py`/`graph_identity.py` reads. Dropping it moves the system
   of record into a store whose backups are deferred and whose rebuild
   depends on… the registry being dropped. **Flagged for veto**: if
   Frederico's intent was "nothing of the old store survives", the answer
   is that `documents` stopped being a pgvector table the moment its
   vector children left — it is the app's relational spine.
2. **Re-embedding into Neo4j instead of copying vectors.** Minutes of CPU
   and a verification that can no longer distinguish store behavior from
   embedding drift. Copy, verify, keep `embed_model`/`embed_dim` stamps.
3. **A second, vector-dedicated Neo4j instance.** A new container,
   credential, and silent-degradation surface to avoid sharing an instance
   that §4 shows is ~3% utilized by this workload. The aw-app-neo4j
   contract's namespacing (C5) exists precisely so consumers and workloads
   share one instance.
4. **Runtime dual-read config flag** for cutover. Permanent config surface
   for a one-time transition in a single-user app with a rebuildable
   store; release-sequenced V1→V3 gives the same safety with evidence
   gates instead of a flag.
5. **Keeping pgvector (the two prior designs' D1).** Overridden by the
   owner, explicitly, after the trade-offs were put in front of him twice.
   What that override accepts, so nobody rediscovers it as a bug: ANN
   queries lose filter pushdown (§3.1 works around it; fine at this
   scale), and semantic search/ingest acquire a hard runtime dependency on
   Neo4j (§3.6). Not relitigated.
6. **`RS_CHUNK_OF` edges to `rs__Paper` in v1.** Paper nodes are minted by
   N2's sync on `ref_targets.key`; coupling chunk writes to that lifecycle
   buys search nothing today. Deferred, named (§2).
7. **Lexical/full-text migration.** Out of scope — there is none to
   migrate (`search_paper_text` is vector-only; no tsvector columns
   exist). Noted so nobody "ports" a feature that doesn't exist.

## 6. What this makes harder later

- **Search and ingest now ride Neo4j's availability.** The app's
  flagship MCP tools degrade whenever aw-app-neo4j is down or rotating
  (credential rotation on the graph instance now interrupts search, not
  just the graph view). Typed degraded states + doctor surfacing are the
  mitigation; the exposure is real and accepted.
- **Embeddings live in a store with deferred backups.** Recovery = re-index
  from disk (tens of minutes, §3.6), never a restore. If the corpus grows
  10×, that recovery window grows with it — the backup-trigger
  conversation on aw-app-neo4j's card gets more urgent as this corpus
  grows.
- **Filterless ANN is now baked in.** Any future "search within these N
  documents" feature pays over-fetch or brute-force; pgvector's `WHERE`
  pushdown is gone. At ≥100k chunks this becomes the first thing to
  re-measure (§4's trigger).
- **Divergence from uc-phd.** `store.py` was deliberately mirrored from
  uc-phd (same `_PENDING_SHA`, same shapes); they now diverge permanently,
  and fixes stop porting 1:1 between the two apps.
- **A 4,000-node vector workload shares the identity graph's instance** —
  one noisy workload can now affect the other; today both are tiny, and §4
  names the revisit trigger.

## 7. Risks for the Coders

1. **Index population is async.** After backfill or index creation, poll
   `SHOW INDEXES` for ONLINE before verifying or serving — a
   still-populating index silently returns partial results.
2. **Use `db.create.setNodeVectorProperty`**, not `SET` — float32 +
   dimension validation; a node whose vector is the wrong shape is
   silently absent from the index (no error at query time).
3. **Pin the score→distance conversion** (`2·(1−score)`) with a
   known-vector test — this is the one silent-ranking-inversion hazard.
4. **Community edition constraint limits**: single-property uniqueness
   only (node keys are Enterprise) — hence the synthetic `uid` (§2);
   don't "improve" it into a composite constraint.
5. **`_PENDING_SHA` semantics must survive**: `api/corpus.py`'s status
   poll and the search exclusion both key off `documents.md_sha256` in
   Postgres — don't move or shadow it in Neo4j.
6. **Migration SQL colon rule**: no `:word` anywhere in 0010/0011,
   comments included — the runner feeds files through SQLAlchemy `text()`
   and the failure is logged, not fatal.
7. **Batch the UNWIND writes** (≤100 rows/statement; 768 floats per row)
   and keep each batch its own transaction — mirrors `INSERT_BATCH`.
8. **Local tests**: throwaway Neo4j needs one combined `NEO4J_AUTH` var,
   and its bridge IP must be re-read after any restart (recorded harness
   trap); unit tests fake `GraphSession` the way `test_graph_db.py`
   already does.
9. **N2 is in flight on this shared checkout right now** (its tests were
   being edited during this design session). V1 **rebases on N2's merged
   `graph_db.py`** — do not copy or fork the adapter; `git status` before
   every commit.
10. **aw-app-neo4j first-boot credential lock-in** (recorded): the
    password is fixed at the instance's first boot — backfill failures
    that look like auth are config drift, not code.
11. **Don't drop `ingest_failures`** (it's in 0001 but vector-free and
    live), and 0011 must not touch anything outside the two named tables.

## 8. Decomposition and sequencing (the card's question 4)

**Own sub-sequence (V-cards) in the same target/epic, strictly after N2
merges.** It does not fold into N1–N4: N3 (UI) and N4 (agentic import)
never touch vectors, and N2's adapter needs **zero changes** for this —
`connect()`/`GraphSession` are workload-agnostic; V extends them from a
new module. The shared-instance questions the card raises (second workload,
namespacing) are answered: C5 prefixes cover `rs__Chunk`/`rs__Figure`
naturally, and §4 shows sizing is a non-issue.

| Card | Scope | Depends on | Gate to the next |
|---|---|---|---|
| **V1 — Neo4j vector store + dual-write + backfill** | `neo4j_vectors.py` (schema, replace/search/counts, distance conversion + pinned test), migration 0010 (count columns), ingest dual-writes (pgvector + Neo4j), `POST /api/corpus/vector-backfill`, guard-test extension | **N2 merged** (adapter + config + dependency already land there) | backfill run on live corpus; counts match |
| **V2 — verify + read cutover** | `POST /api/corpus/vector-verify`, flip `search`/`search_figures`/`corpus_search`/`index_status` reads to Neo4j, typed degraded states in `/status`, remove pgvector dual-write | V1 | verification JSON posted on the card (counts + overlap@10 ≥ threshold); Frederico/QA sees it |
| **V3 — delete** | migration 0011 (drop `chunks`+`figures` only), remove `vec_literal`/`public.vector` paths/`pg_extension` probe, docs + recovery runbook, test updates | V2's evidence on the card | — |

Deferred, named: `RS_CHUNK_OF` edges to `rs__Paper` (GraphRAG adjacency);
vector-workload split to its own instance (trigger §4); retrofit of
`search_paper_text` as an MCP tool (pre-existing deferral, unchanged).
