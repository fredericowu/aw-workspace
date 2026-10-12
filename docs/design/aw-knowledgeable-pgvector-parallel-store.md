# aw-knowledgeable — parallel pgvector store, built by the Connector, compared before any cutover

Status: **design, not code.** Architect, 2026-10-12. Card
`3f75bf3b-9510-816c-b67a-f13324067528`
(`architecture:knowledgeable-move-out-of-neo4j-connector-owns-work`).

Frederico's instruction, verbatim and complete: *"wire everything and let the
Knowledgeable Connector do the work, move stuff out from neo4j and let it
work, put the data in a way we can compare, don't simply wipe it."*

Every claim below is grounded in a file read this session or a command run
against the live system this session (bare metal, 2026-10-12 ~19:40 UTC);
live numbers are stamped as such. Three facts in the dispatch were re-verified
and **two had moved** — see §0.

---

## 0. Live facts this design stands on (verified 2026-10-12)

| Fact | Value | How verified |
|---|---|---|
| Corpus bucket `main` | **33,408 chunks / 2,073 docs**, every chunk has `embedding` | cypher-shell on `aw-stack-aw-neo4j-1` |
| Entity graph | **NOT 0 anymore**: 4,468 `:Entity`, 10,808 `MENTIONS`, 7,036 `ASSERTS`, 3,413 `RELATED_TO` — but only **225 of 2,073** `main` docs have extraction (~11%, the pilot) | cypher-shell, per-bucket counts |
| Neo4j store size | 3.4 G `/data` | `docker exec du` |
| aw-stack central Postgres (`aw-stack-aw-postgres-1`) | `postgres:16-alpine` — **pgvector not available at all** (`pg_available_extensions` has no `vector` row) | psql live |
| The actual pgvector | **`aw-pgvector`** service in `repos/aw-stack/docker-compose.yml:269-328` — `pgvector/pgvector:pg17`, `vector` 0.8.3 installed, **PGPORT 5433**, db `knowledge_base` (aw-app-kb's, 64 MB), on `aw-stack-net`, published on **no** host interface | psql + `docker inspect` live |
| Reachability workspace → `aw-pgvector:5433` | **TCP connects** (also `aw-knowledgeable:8090`, `aw-stack-aw-neo4j-1:7687`) | socket test from an agent container in this workspace |
| Host headroom | 12 cores, load ~4–5, 31 G RAM available, **disk 86% (60 G free)** | `nproc`/`uptime`/`free`/`df` |
| Embedding cost | local ONNX `BAAI/bge-m3`, 1024-dim, **serial, ~0.91 s/chunk** (`backend/app/core/embeddings.py:283-286`) — no API, no key | code + measured docstring |
| `MEASURE_BUCKET` trap | `harness/topic_coverage_measure.py:34` defaults to `kb-mapped-folders` = **0 chunks** | grep + cypher count |
| aw-knowledgeable HEAD | `93e8452` — key pull-through **is implemented service-side** (the pull-through doc's "decided, not implemented" status line is stale); acceptance still pending | `git log` + `backend/tests/test_key_pull.py` |

The dispatch's "entity graph still 0" is stale — the extraction pilot ran.
Nothing in this design migrates the entity graph, so the conclusion is
unchanged, but §2 plans **around a partially populated graph**, not an empty
one.

---

## 1. The approach, in sentences someone can disagree with

1. A **parallel chunk/vector/topic store** is created in Postgres — a new
   database `knowledgeable` on the existing **`aw-pgvector`** aw-stack
   service (the instance that already runs `pgvector/pgvector:pg17` and is
   already inside the nightly `pg_dumpall`). Not on the central
   `aw-stack-aw-postgres-1`, which cannot host vectors at all (§0).
2. The **Knowledgeable Connector owns the new store's build**: a new
   `knowledgeable-mirror` CLI tick (same two-door shape as §13 bulk-ingest)
   pulls chunks **with their existing bge-m3 embeddings**, documents, and the
   topic tree from a new read-only export API on aw-knowledgeable, and writes
   them to pgvector. **No re-embedding** — the vectors are copied, so the new
   store holds byte-identical vectors to the old one.
3. aw-knowledgeable's retrieval gains a per-request **`store=neo4j|pgvector`**
   switch on the three chunk-backed arms (vector, chunk-lexical, tree).
   Query embedding, fusion, envelopes, knobs — everything else — is the same
   code either way. The graph arm and entity visibility keep reading Neo4j in
   **both** configurations.
4. The comparison runs the production query sets against both stores through
   the same pipeline in the same process, on bucket `main`, and the result is
   a measured table in the style of `aw-knowledgeable-tree-beam-near-tie.md`
   §2. **Cutover is a separate later card, decided on that table** — and when
   it happens, the Neo4j chunk store is removed completely ("don't leave a
   fallback behind"), with the entity graph and the human graph staying.
5. Neo4j is **not wiped, not paused, not degraded**: the export API is
   GET-only, the live ingest path is untouched, and a tagged S3 dump is taken
   at T0 through the already-existing stop-dump-restart job.

The deliberate consequence: **this card's implementation does not move the
embedding compute.** That is Phase B, after the comparison — §4 says exactly
what it would look like and why doing it first is the wrong order.

---

## 2. What moves and what stays — per store, with the shape named

| Data | Shape in truth | Today | During comparison | End state after a pgvector-wins cutover |
|---|---|---|---|---|
| **Chunks + embeddings** (33,408 × ~8 KB in Neo4j: 1024 doubles + text) | **A table pretending to be a graph.** `(:Chunk)-[:PART_OF]->(:Document)` is a foreign key; retrieval is `db.index.vector.queryNodes` / Lucene BM25 + exactly one hop (`graph.py:1289-1303`, `1363-1421`) | Neo4j | **Mirrored to pg** (`chunks` table, `embedding vector(1024)`, HNSW cosine + tsvector index) | pg only; Neo4j `:Chunk` nodes deleted |
| **Documents / metadata** | A table. Also currently carries the ingest queue (`processing_status`, `claim_lock` — `graph.py:2533-2546`) | Neo4j | Mirrored to pg (`documents` table); queue columns **stay in Neo4j** (live ingest untouched) | pg; the queue moves with the pipeline (Phase B) |
| **Topic tree** (163 topics, 156 `PARENT_OF`, 34,116 `COVERS`) | **A parent-pointer table.** The beam descent is one query per level + numpy cosine in Python (`topics/retrieve.py:144-243`); at `beam_width=10` it scores all 139–163 topics — the tree-beam doc itself calls that "flat search with extra steps" | Neo4j | Mirrored to pg (`topics` with `parent_id` + `centroid vector(1024)`, `topic_covers`) whenever the build stamp changes | pg; build output lands in pg (Phase B) |
| **Entity graph** (`:Entity`, `MENTIONS`, `ASSERTS`, `RELATED_TO`) | **Genuinely graph-shaped** — the graph arm BFS (`graph.py:5369-5427`, radius ≤ 2 over `LINKS_TO`+`RELATED_TO`) and the `EXISTS{}` visibility predicates (`graph.py:447-480`) are real traversals; shared `(:Entity)` is the structural cross-bucket connector (§11.5 rule 2) | Neo4j | **Stays.** It is 11% populated (pilot), Phase-2 full extraction is **not authorized**, and migrating a partial pilot buys nothing. The graph arm reads Neo4j under both `store=` values — same arm on both sides keeps the comparison fair | **Stays in Neo4j.** This is what Neo4j is still for |
| **Human graph** (`LINKS_TO`, `:Collection` spine, `create_node`/`create_link`, `get_graph`) | Graph-shaped by nature and by UI (focus+depth neighbourhood walks) | Neo4j | Stays | **Stays in Neo4j** |
| **Identity projection** (`tenants` 2-column table) | A table, already moved | aw-stack central pg (`aw-knowledgeable-sqlite-exit.md`) | Untouched | Untouched |
| **Document bytes** | Files | service volume `documents-data` | Untouched | Untouched |

So "move stuff out from Neo4j" lands as: **everything whose access pattern is
an index lookup or a one-hop join moves; everything that is actually
traversed stays.** Neo4j shrinks from a 3.4 G store dominated by embeddings
to a genuine knowledge graph (~7 k nodes of entities, links, collections,
documents-as-graph-endpoints), which also shrinks the daily backup's
stop-the-container window (`repos/aw-stack/scripts/backup-neo4j.sh:16-22`
names the window as growing with data) and relieves the 1 g page-cache that
v2 §2 measured embeddings exhausting at ~269 k chunks.

### 2.1 Which parts of the inherited design die with the reversal

The reversal is **given** (Frederico, this card). Stating the casualties so
there are not two half-architectures:

**Dies now:**
- `aw-knowledgeable-infra.md` locked decision 2 ("native Neo4j with its own
  vector index — no reuse of aw-app-kb's pgvector") — the reversal itself.
- infra §2 **"the vector index is the leak"** and everything downstream of
  it on the pg path: the over-fetch factor, the recall floor, the
  ANN→exact escalation with starvation logging
  (`v2-retrieval` §2, `graph.py:1271-1350`). pgvector pre-filters
  `tenant`+`bucket` in the WHERE clause natively — the entire mechanism
  exists because `db.index.vector.queryNodes` on 5.26.4-community takes no
  filter argument. (It keeps running on the Neo4j path while that path
  serves; it is deleted at cutover, not before.)
- **K4 in its over-fetch form** (infra §2 acceptance test): its "expected
  over-fetch row count" assertion has no referent in SQL. Replaced by a
  pg-side tenant/bucket isolation test (seed A and B, assert the WHERE
  scoping) plus the static-guard extension below.
- infra §7 harder-later #8 (over-fetch caps tenant size before small-tenant
  recall degrades) and §8 risk 1 — the defect class they warn about does not
  exist on the pg path.
- `v2-retrieval` §2's **"Rejected: pgvector-on-Postgres"** entry. Its revisit
  clause (absent `vector.similarity.cosine` **and** measurable ANN dilution)
  is **not** what fired — Frederico reversed the premise. Honesty note its
  author deserves: the rejection's core argument ("retrieval goes vector hit
  → graph neighbourhood → topic path **inside one query**") was never
  realized in code. The tree walk is one round-trip per level with scoring in
  Python; the graph arm is per-hop Cypher; `RELATED_TO` is precomputed and
  read one hop at a time. The split this design makes costs the same
  round-trip shape retrieval already has.
- The 3.9 KB/chunk page-cache arithmetic and the ~269 k-chunk ceiling
  (v2 §2) as a *constraint on the chunk store* — moot in pg.

**Weakened (survives with a new obligation):**
- infra §2's "one module (`core/graph.py`) is the **only** place touching a
  store driver" becomes "one gate contract, **two** store seams". The new pg
  store module is held to the same discipline: named statements only, every
  statement carries `tenant` and `bucket`, and the K5 static guard grows a
  SQL twin (no `session.execute(`/raw SQL outside the pg store module; no
  statement without both scope columns). The SQLAlchemy side actually gets
  back the `do_orm_execute`-style rewriting hook infra §2 said Cypher lacks —
  but the house pattern here is the template catalogue + guard test, and two
  enforcement idioms for one contract is worse than one idiom twice.
- Tenant offboarding and bucket moves now sweep **two stores**
  (`scripts/move_bucket.py` gains a pg sibling at cutover).
- infra §1 (Neo4j topology in aw-stack) survives — Neo4j stays — but its
  §1 "backups already have a home there" argument now cuts the other way:
  the vectors land in an instance covered by the **online, zero-downtime**
  `pg_dumpall` nightly (`backup-postgres.sh` `TARGETS` already includes
  `aw-pgvector`), while Neo4j keeps the stop-the-container dump.
- D2 (`infra` §10, connector Tier-1, "entire dependency surface is httpx"):
  survives for this card — the mirror runs in the CLI subprocess using
  psycopg **already present in core's venv** (core's own DB layer), so
  `pip_requires` does not grow. But D2's own runner-up clause ("switch to
  Tier-2 if the connector … grows a background worker") is **the live
  question for Phase B** — see §4.

**Survives untouched:** standalone-service decision; RAGA direction;
embedding model choice (bge-m3, Amendment 5); chunking; topic build
semantics (Amendments 1–2: distribution-cosine edges, mutual top-N); bucket
semantics (permission boundary, registry node, `main`); the §11 entity
design including per-bucket derivation as a security rule; RRF fusion keyed
on `chunk_text_sha256`; §12 knobs/envelope contracts (`near_ties`,
`not_applied`, params echo); §13 driver + fs-sync + git backend; identity
Phase A; key pull-through.

---

## 3. Where it lands — files and seams

### 3.1 aw-stack (`repos/aw-stack`)
- New database `knowledgeable` + role `knowledgeable` on the **`aw-pgvector`**
  service (not the central pg). Created the way sqlite-exit §4 created its db:
  by hand/psql at provision, not by a compose rebuild. `CREATE EXTENSION
  vector` in that database.
- Password: generated at provision (`openssl rand -hex 24`), delivered to its
  two consumers through their **existing** credential channels — the service's
  `.env` (exactly like `IDENTITY_DATABASE_URL`) and the workspace secret
  store (below). Never in compose, never in a repo. Note for the rotation
  future: the in-flight Redis credential-sync card
  (`infra:redis-credential-automatic-sync`) is building the generic
  distribute-on-deploy machinery; this URL is a candidate for it later, not a
  dependency on it now. **This design does not touch Redis at all**, so it
  does not collide with ACL Phase 1/2 or the sync redesign.
- One line in `backup-postgres.sh`'s header note is already true: aw-pgvector
  dumps must restore into a pgvector-capable image. No backup work needed —
  the new database is covered on its first nightly.

### 3.2 aw-knowledgeable (`repos/aw-knowledgeable`)
- **Export API** (new `backend/app/api/export.py`), service-secret gated
  (`require_service`, same gate as the key-push routes), GET-only, paginated
  by `external_id` ascending (the house pattern —
  `read_bucket_chunks_pages`, `graph.py:5677`):
  - `GET /api/export/documents?bucket=&after=&limit=` — id, label, type,
    `content_hash`, `source_path`, `uploaded_at`, `processing_status`.
  - `GET /api/export/chunks?bucket=&after=&limit=` — id, document_id,
    chunk_index, heading_path, text, **embedding**, embedding_model,
    embedding_dim. ~500/page (the `CHUNK_BATCH_SIZE` precedent); 33,408
    chunks × ~8 KB ≈ 70 pages ≈ 260 MB total across the backfill — paged,
    never one response.
  - `GET /api/export/topics?bucket=` — the whole tree (163 topics +
    `PARENT_OF` + paged `COVERS`) plus a **build stamp** so the mirror only
    refetches when a rebuild happened.
- **pg store module** (new `backend/app/core/pgstore.py`): lazy singleton
  sync SQLAlchemy engine on a new `VECTOR_DATABASE_URL` setting
  (same shape as `tenant_store.py:39-60`; `run_in_threadpool` at call sites —
  the `fastapi-sync-dependency-breaks-contextvars` rule). Owns the named SQL
  statements for: vector top-k (`ORDER BY embedding <=> :q` with
  `tenant`/`bucket` in WHERE), lexical top-k (`tsvector` +
  `ts_rank_cd`), topic roots/children/leaf-chunks. Boot-time dimension
  assertion mirroring `graph.py:404-412`. Empty `VECTOR_DATABASE_URL`
  disables the whole path (the `test_key_pull.py` precedent: no URL, no
  network call) — **and the comparison card must include one test with the
  setting ON against a throwaway pg**, because optional-config-gated paths
  ship untested and crash on first real use (that memory is paid for).
- **`store=` switch**: `api/search.py` + `core/knowledge_search.py` accept
  `store: neo4j|pgvector` (default `neo4j`; 400 if `pgvector` requested while
  unconfigured — request-shape error per the §12 validation matrix). Dispatch
  inside `run_search`/`_run_arm`: vector, chunk-lexical and tree arms go to
  `pgstore`; the graph arm, `related_vias` decoration, entity reads, and
  `search_nodes` stay on `graph.py` regardless. The envelope echoes
  `store` in `params` (never a silent downgrade — house rule).
- Chunk-stage re-scoring, beam logic, near-tie declaration, RRF fusion:
  **unchanged code**, fed by either store.

### 3.3 Connector (`repos/aw-app-knowledgeable`)
- New engine module `knowledgeable_app/mirror.py` + CLI
  `commands/knowledgeable_mirror.py` (`run|status`), same two-door shape as
  §13 — **but unlike `knowledgeable-ingest`, the work runs in the CLI
  subprocess itself**, not via HTTP back to core: the bulk-ingest tick is
  bounded by `local_client`'s 30 s default timeout
  (`src/cli/local_client.py:154-155`), which a 70-page backfill cannot and
  must not fit. The subprocess talks straight to the export API (httpx,
  `X-Internal-Secret`) and to pg (psycopg from core's venv).
- Secrets: `service_secret` it already holds; new vault secret
  `knowledgeable-pgvector-url` read per tick via the `key_vault.py` pattern
  (`auto_approve_for: agent:aw-app-knowledgeable`). Never written to disk,
  never in argv.
- State: watermark + per-entity sync journal in a new
  `app__knowledgeable__mirror_state` table via `ctx.db`… **no** — the CLI
  subprocess has no `ctx`. The watermark lives in the **pg store itself**
  (`mirror_state` table in the `knowledgeable` database, beside the data it
  describes), which also makes the mirror self-contained and restart-safe.
  Single-flight: `ctx.state.lease` is also core-side, so the mirror takes a
  **pg advisory lock** in its own database for the tick — one mechanism, same
  database, no second lease system.
- Scheduling: one new contributed task (`contributes.tasks`,
  `agentic_output`, `*/15 * * * *`, `notify_exit_codes: [1]`), shipped
  `enabled: false` like the ingest tick, turned on by a human. Steady-state
  tick: list changed docs since watermark, pull their chunks, upsert;
  refetch topics when the build stamp moved. Backfill is the same code with
  an empty watermark, run manually first (`aw-workspace-cli
  knowledgeable-mirror run --backfill`), expected ~10–30 min dominated by
  transfer + HNSW inserts.
- Deletions mirror as deletions (doc gone from export listing → rows deleted
  in pg) — the mirror is a replica, not an archive; Neo4j remains the
  durable original throughout this phase.

---

## 4. Where the work runs — and whether it fixes what we think it fixes

Honest answers, because this is the part most likely to be wrong:

**The ~1.5 docs/min ceiling is NOT fixed by this card.** It is the
arithmetic of serial CPU embedding — `drain_once` never sleeps while work
remains (`worker.py:231-236`); 0.91 s/chunk × ~40 chunks/doc ≈ 40 s/doc
(`embeddings.py:283-286`, `embeddings.py:374-385` strictly serial). That
cost is **location-independent**: the connector runs on the same 12-core
host (load 4–5 today). Moving the work moves the bill, not the price. What
would actually raise throughput — ONNX intra-op threads or a small worker
pool — can be done in either process and is deliberately out of this card.

**The single-worker pin is NOT lifted by this card** — the open sweep card
(`3f75bf3b-9510-81d4-baa8-dbfeb3902874`) stays correct. What this design
does is make that card's end-state reachable: after Phase B the service
process contains **no pipeline** (no drain loop, no extraction breaker —
they move out with the work), leaving only pull-capable key slots, and the
sweep card's audit can then conclude on a much shorter inventory. Until
then, nothing here changes `--workers 1`.

**Can the connector carry the load?** Split the load in two:
- **The mirror (this card): yes, trivially.** It is I/O-bound (HTTP pages
  in, SQL upserts out), runs in the scheduled CLI subprocess —
  `agentic_output.run_command` spawns it with **no timeout**
  (`repos/aw-app-tasks/tasks_app/agentic_output.py:84-88`) so a long
  backfill is legal, and per-tick deltas are seconds. Zero model, zero LLM,
  zero meaningful CPU.
- **The pipeline (Phase B): not as Tier-1 in-process, and only awkwardly as
  a subprocess.** Sustained bge-m3 inference needs the ~520 MB+ model on
  disk, ~2 G RAM while running, and real CPU — inside workspace core's
  process that is disqualifying, and as a fresh-interpreter-per-tick
  subprocess it pays a model load every tick and competes with everything
  else in the workspace container. The right Phase-B home is the one D2's
  runner-up clause already named: **the connector goes Tier-2** (own
  container, model baked into the image — exactly `aw-app-kb`'s shape,
  which is Tier-2 *for precisely this reason*: "kb is Tier-2 because it
  owns pgvector/Postgres deps and heavy indexing", infra §10), or a
  dedicated worker service lands beside aw-knowledgeable. That is a Phase-B
  decision made on the comparison evidence plus a PO call on where the
  compute should live; it is **named here so nobody discovers it
  mid-implementation**.

**Correction to the card's reading (clause 1), for the PO:** "let the
Knowledgeable Connector do the work" is honored in this card as *the
connector owns building and maintaining the new store*. The embedding /
extraction / topic-label **compute** does not move yet, for three reasons:
moving it first costs ~8.5 h of serial CPU re-embedding 33,408 chunks for
vectors we can copy byte-identically in minutes; it forces the Tier-2
conversion before any evidence exists that the new store wins; and it
confounds the comparison (a new pipeline + new store measured together
cannot attribute a regression to either). If Frederico means "the connector
must do the embedding from day one", the same seams hold — the mirror
becomes backfill-only and Phase B starts immediately — but the order above
is the recommendation.

---

## 5. The comparison — the deliverable Frederico named

**What is compared:** the same retrieval pipeline, same process, same query
embedding, same fusion, same knobs — differing only in `store=`. The graph
arm reads Neo4j on both sides. This isolates exactly the decision being
made: *which chunk/topic store serves retrieval better*.

**Corpus:** bucket `main`, tenant `aw-workspace-default`. The run asserts
chunk-count parity between stores first (mirror watermark current, counts
within the live-ingest delta) and records both counts in the report. The
harness sets **`MEASURE_BUCKET=main` explicitly** — the default
(`harness/topic_coverage_measure.py:34`) points at `kb-mapped-folders`,
which holds **0 chunks**; measuring it "proves" victory over an empty
store. The measurement card also flips that default to `main` (or makes it
required) so the trap dies.

**Query sets and metrics:**
1. The tree-beam doc's **8-query live set**, k=20, 3 timed reps —
   **flat-top-3 recall** exactly as measured there (baseline already on
   file: 10/24 → 13/24 → 17/24 at beam 3/5/10 on Neo4j). Direct
   comparability with an existing measured table is why this set leads.
2. **Known-answer probes** (`me.md`-style): ~12 additional queries with a
   hand-named expected document, split across memory/notion/docs sources —
   recall@10 and rank-of-expected per arm (`vector`, `lexical`, `tree`,
   `fused` via `knowledge_search` at the fixed profile `radius=1, limit=20,
   subqueries=3, rounds=0`, `synthesize=false`).
3. **Latency** p50/p95 per arm per store, query embedding excluded
   (computed once, identical by construction).
4. **Operational row:** store size on disk, HNSW build time, and — Neo4j
   side only — how often the ANN→exact escalation fired (the pg side has no
   such mechanism to need).

**Mechanics:** the `aw-autoskill-aw-knowledgeable-live-measurement-harness`
recipe, unchanged: write the script (family of `topic_coverage_measure.py`,
calling `run_search`/`knowledge_search` in-process with both `store`
values), `docker cp` into the live `aw-knowledgeable` container (the
production image COPYs only `scripts/` — `harness/` is not in the image),
`docker exec python` it, `--out` JSON. Output lands as a measured table
appended to this doc and as a comment on the comparison card.

**Fairness notes the harness must record in its envelope:** pgvector HNSW
knobs (`ef_search`, default 40; `m`/`ef_construction` at build), Neo4j's
beam/overfetch settings, and the embedding model/dim read from the data —
an unrecorded knob is how a comparison gets re-litigated. Lexical arms are
**different scorers by design** (Lucene BM25 vs `ts_rank_cd`); compare
recall and fused-rank outcomes, never raw scores.

**Decision rule (proposed, PO owns the final):** pgvector cuts over only if
it is **non-inferior on recall** (no metric worse by more than 1 answer out
of 24 / 1 out of 12 probes) **and** materially better on at least one of:
tree/vector latency, operational simplicity (escalation machinery deleted),
or ingest-path independence. Anything murkier goes back to Frederico with
the table, not a recommendation hidden in prose.

---

## 6. Nothing gets wiped — the preservation story, stated

- **Neo4j keeps serving and keeps ingesting** throughout. The export API is
  read-only; the mirror holds no locks on Neo4j; default `store=` stays
  `neo4j` until a cutover card says otherwise.
- **T0 tagged dump:** one explicit run of the existing
  `repos/aw-stack/scripts/backup-neo4j.sh` with `SLOT=pre-pgvector-parallel`
  (+ `KEEP_ARTIFACT=true`), scheduled off-hours. Neo4j Community has no
  online backup — this script stops `aw-stack-aw-neo4j-1` for the dump; at
  3.4 G the window is minutes, and it is the same window the 03:30 daily
  already takes. That artifact is the frozen "old side" of record.
- **Daily dumps continue** unchanged (03:30 UTC, S3 `tekflox-aw-backup`).
- The new pg database enters the **online** `pg_dumpall` nightly
  automatically.
- At a future cutover: Neo4j chunk/topic data is deleted **completely** in
  that card (no dormant fallback code path left — Frederico's standing
  rule), preceded by one final tagged dump as the historical artifact. The
  entity graph, human graph, and collections stay in Neo4j — Neo4j is not
  being decommissioned, it is being returned to the job that needs a graph.

---

## 7. What I rejected, and why

1. **Connector re-embeds the corpus as the way to build the new store**
   (the maximal reading of clause 1). ~8.5 h serial CPU on a loaded host for
   vectors that can be copied byte-identically; requires porting
   chunker+embedder+model into the connector before any comparison exists;
   confounds pipeline change with store change. It becomes Phase B's
   forward-path, not the backfill. Runner-up — and the first thing to
   revisit if Frederico corrects my reading.
2. **Service dual-writes pgvector at ingest time** (no connector role).
   Fewer moving parts, but it couples the live ingest path to a second
   store (violates "live retrieval must not degrade" on the write side),
   keeps all work in the service this card exists to slim down, and gives
   the connector no ownership — the opposite of the instruction.
3. **Vectors on the central `aw-stack-aw-postgres-1`.** Impossible as-is
   (`postgres:16-alpine`, no pgvector binary); swapping that image for a
   pgvector one to consolidate instances is a bigger infra change than
   reusing the instance that exists for exactly this.
4. **A Redis-backed work queue / store for the mirror.** The aw-stack Redis
   is mid-ACL-rollout (`default` still nopass), a separate card is
   redesigning its credential distribution, and the key pull-through design
   already documented why a Redis hop is dominated here
   (`knowledgeable-apmt-key-pull-through.md` §4.1). The mirror's state is
   two tables in the database it already writes; Redis adds a dependency
   and a credential for nothing.
5. **Tier-2 conversion of the connector now.** Needed only when the compute
   moves (Phase B). Doing it now re-opens install/marketplace/release
   failure classes (four are documented) for a card that ships an I/O
   mirror.
6. **Migrating the entity graph to pg** (as `JOIN`able tables). It is the
   one store with real traversal and real `EXISTS{}` visibility semantics,
   it is 11% populated, and full extraction is explicitly unauthorized —
   migrating a pilot is motion without progress.
7. **A new standalone "knowledgeable-worker" service for the mirror.**
   Right shape for Phase B's compute, overkill for an I/O tick the
   connector's existing scheduled-CLI door already handles.

---

## 8. What this makes harder later

- **Two stores, one contract.** Every tenancy/bucket rule now has two
  enforcement sites (template catalogue + SQL statements, K5 + its SQL
  twin). A rule added on one side and forgotten on the other is the new
  drift mode; the guard tests are the mitigation, not a cure.
- **The mirror is a replica with lag.** Until cutover, `store=pgvector`
  answers are up to one tick (15 min) stale plus the topic-build stamp
  lag. Fine for comparison; named here so nobody flips the default and
  calls the staleness a bug.
- **Schema evolution forks.** A change to chunk shape (new property, new
  dim) now lands in Neo4j, the export API, and the pg schema. The
  `embedding_dim` boot assertions on both sides turn drift into a refusal
  instead of corruption, but the three-way edit is a real tax until
  cutover deletes one side.
- **aw-pgvector becomes load-bearing for a second product.** Today it
  serves aw-app-kb's 64 MB; after this it also serves knowledgeable
  retrieval. It has no resource caps in compose and port 5433 hardcoded by
  every consumer. Acceptable at this scale; revisit caps if `store=pgvector`
  becomes the default.
- **The comparison freezes vocabulary.** `store=` becomes API surface the
  moment the harness uses it; retiring it at cutover is a (small) breaking
  change to whoever scripted against it.
- **Phase B is pre-shaped.** By naming Tier-2-at-Phase-B, this doc
  constrains the next architect; if compute placement changes (a GPU box, a
  shared embedding service), the named runner-up should be re-opened rather
  than silently obeyed.

---

## 9. Risks for the Coders

1. **The 30 s trap.** `knowledgeable-ingest`'s thin-client shape
   (`commands/knowledgeable_ingest.py` → `local_client` → engine in core)
   dies at 30 s (`local_client.py:154-155`) and the server keeps working
   after the client gives up. The mirror must NOT copy that shape — work in
   the subprocess, straight to export API + pg. The one long-silent-command
   watchdog (600 s) applies to *agent* runs, not scheduled tasks, but keep
   the backfill chatty in its journal anyway.
2. **Don't hold a store open across the export pagination.** 70 pages ×
   ~4 MB with an HNSW insert per row: commit per page, resume by
   watermark. An aborted backfill must resume, not restart — the journal is
   in pg beside the data (§3.3).
3. **Build the HNSW index AFTER the backfill**, then `ANALYZE`. Inserting
   33 k rows through a live HNSW index is many times slower than
   index-after-load; this is the difference between ~10 min and hours.
4. **Dimension is read, never written as a literal** — mirror carries
   `embedding_model`/`embedding_dim` per chunk and refuses on mix
   (`graph.py:404-412` is the precedent); the pg column is `vector(1024)`
   fixed at creation, same failure mode as Neo4j's index.
5. **Lexical parity is a false goal.** BM25 vs ts_rank_cd will disagree on
   scores and often on order. Tests assert recall and fused outcomes, not
   score equality. A coder "fixing" the pg lexical arm to match Lucene
   scores is scope creep on a comparison this design wants honest.
6. **The export route gate.** `require_service` only — a workspace API key
   must not be enough to bulk-export a tenant's corpus (same reasoning as
   the key routes' "never a human", `api/ingest.py:48-50`). And the
   connector's `/keys/{slot}` precedent applies to the connector's side of
   nothing here — the mirror *calls out*, it serves no new route.
7. **Tests that touch pg need a throwaway pgvector container** (image
   `pgvector/pgvector:pg17`, connect via the container's **bridge IP**, not
   loopback — the published-port-loopback trap is documented), and
   aw-workspace's conftest monkeypatches `httpx.get` globally — connector
   tests must use the client object, not module-level httpx.
8. **In-flight collisions:** the pull-through acceptance on aw-knowledgeable
   (`93e8452`) is pending — don't touch `playground.py`/`extraction.py`/
   `label.py` key paths; the Redis ACL + credential-sync cards own
   everything Redis — this design uses none of it; the fs-sync design owns
   `bulk_ingest.py` refactors — the mirror is a **sibling module**, shared
   helpers only by extraction, never by editing `bulk_ingest.py`'s tick.
9. **Shared checkout discipline:** several agents are active on
   aw-workspace master; re-check `git status` immediately before committing
   and stop rather than race.

---

## 10. Phasing, cards, and estimate

| Card | Repo | Content | Size |
|---|---|---|---|
| P1 provision | aw-stack | db `knowledgeable` + role on aw-pgvector, `CREATE EXTENSION vector`, secret into vault + service `.env` | ~½ day |
| P2 export + pg read path | aw-knowledgeable | export API (3 routes), `pgstore.py`, `store=` switch on search/knowledge_search, dim assertions, tests (throwaway pg + throwaway Neo4j harnesses both exist) | 2–3 days — the big one |
| P3 mirror | aw-app-knowledgeable | `mirror.py` + CLI + contributed task + vault secret + advisory-lock single-flight + backfill | 1–1.5 days |
| P4 comparison | harness via live-measurement recipe | script, backfill verification, measured table, `MEASURE_BUCKET` default fix, report on card | ½–1 day |

Sequence P1 → (P2 ∥ P3 against a stub) → P3 backfill → P4. Wall-clock ≈ one
week at normal dev-team cadence. **LLM cost ≈ $0** (no extraction, no
labelling, no re-embedding — the pipeline spends agent tokens only;
comparable cards have run 10–50 M tokens each). Disk: < 1 G on the
aw-pgvector volume (host has 60 G free). Neo4j downtime: zero beyond the
T0 dump window the daily backup already takes. Phase B (pipeline move +
Tier-2 + queue migration + cutover + complete removal of the Neo4j chunk
store) is **not estimated here** — it is scoped by its own card after the
P4 table exists.
