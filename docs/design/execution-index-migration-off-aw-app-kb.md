# Execution index outlives aw-app-kb — migration design

Architect decision for Kanban card `3fc5bf3b-9510-81c4-b8dd-ca3ed2db84ca`
("Execution index outlives aw-app-kb — design the migration off it before the
app is retired"). Written 2026-10-17. Design only — nothing here is
implemented yet.

Context: Frederico (workspace owner) stated `aw-app-kb` will be retired. The
execution index — 24,717 chunks making every past agent run semantically
searchable — lives in a Postgres **inside that app's container**. This is the
plan for moving it out before the retirement forces an improvised move. KB
searched 2026-10-17: no prior decision on retiring aw-app-kb exists; this is
new ground.

---

## 1. The decision

**Move the data to the shared `aw-remote-host-postgres` (new database
`execution_index`), as-is at 768 dims with no re-embedding — and move the
service (embedder, write routes, `search_execution_history` MCP tool) into
`aw-app-agents-platform-runners`, running as one app-supervised subprocess
inside the workspace container.**

That is the card's option B for the *data*, completed with an ownership
answer the option as stated didn't have: a table of vectors is inert without
an embedder to write it and a tool to read it, and today aw-app-kb is all
three at once. The runners app is the right new owner because it already owns
the producer (`execution_index.py`), the doctor check
(`/execution-index/doctor`), and the thing the index is *about* — runs.

Someone could disagree with any of these: that the index deserves its own
Tier-2 app rather than a subprocess of runners; that 30-day-retention data
isn't worth migrating at all (rebuild via backfill instead); or that this
should wait for aw-knowledgeable's re-embed path and go there. §4 says why
each of those loses.

## 2. Where things are today (all verified 2026-10-17)

The store:

- Table `executions` — `chunk_id` PK (`{run_id}:{seq}`), `run_id`, `seq`,
  `content`, `metadata JSONB`, `indexed_at`, `embedding vector(768)`; HNSW
  `vector_cosine_ops` index plus btree indexes on `run_id` and `indexed_at` —
  `repos/aw-app-kb/kb_app/exec_pg.py:106-128`.
- `VECTOR_DIM = 768`, `nomic-ai/nomic-embed-text-v1.5` via fastembed (ONNX,
  local, ~520 MB, zero API cost), `search_document:`/`search_query:` task
  prefixes, `_EMBED_MAX_CHARS = 1500` hard-refused (not truncated) on write —
  `kb_pg.py:55`, `exec_pg.py:188-194`.
- Postgres runs **inside the aw-app-kb container** (base image
  `pgvector/pgvector:pg17`): `repos/aw-app-kb/entrypoint.sh:30`
  (`docker-entrypoint.sh postgres &` next to the FastAPI app), data at
  `/app/persist/pgdata` = `$AW_APP_DATA` (`aw-app.json` env `PGDATA`).
- Retention: the index keeps ~30 days (`KB_EXEC_RETENTION_DAYS`, default 30)
  and caps at 80k rows; it is an **index, not an archive** — AP-MT keeps the
  full run record forever — `exec_pg.py:33-41,59-60,304`.

The writers and readers:

- Producer: `repos/aw-app-agents-platform-runners/agents_platform_runners_app/execution_index.py:405-505`
  — in-process in workspace core (the runners app is Tier-1 `inprocess`),
  POSTs `{kb_base_url}/api/kb/executions` (default `http://aw-app-kb:8000`,
  line 416) with header `X-KB-Exec-Secret`, fail-open with Redis counters per
  failure reason. Upsert by `chunk_id` + full-replace per run
  (`exec_pg.py:154-228`) makes re-sends idempotent.
- Write/status routes: `repos/aw-app-kb/kb_app/routes.py:222-224`, secret
  check at `routes.py:128-134`.
- Reader: MCP tool `search_execution_history`, defined
  `repos/aw-app-kb/kb_app/mcp_http.py:138`, handler `mcp_http.py:335`
  (chunk-level search grouped to one block per run). Listed in the kb
  manifest's `contributes.mcp.provides`.
- Doctor: the runners app's own `/execution-index/doctor`
  (`agents_platform_runners_app/routes.py:173`) reads the Redis counters, not
  the kb app — it survives the move unchanged.
- One-off backfill tool: `repos/aw-app-agents-platform-runners/scripts/backfill_execution_index.py`
  re-indexes finished runs from AP-MT through `index_run()` — it follows
  whatever target the config names, so it works against the new home for free.

The destination (live-inspected 2026-10-17):

- Container `aw-remote-host-postgres`, image `pgvector/pgvector` (digest-
  pinned), **PostgreSQL 16.14** — note: *older major* than kb's bundled PG 17.
- Published on `127.0.0.1:5432` of the workspace container (the podman host);
  attached only to the `aw-remote-host` network — **not** on
  `AW_CONTAINER_NETWORK`, so Tier-2 app containers cannot reach it. uc-phd
  states this exact constraint:
  `repos/aw-app-uc-phd/estudo_geral_extractor/pgvector_index.py:34-37`
  ("an agent runner container is a different network namespace and cannot
  reach aw-remote-host-postgres:5432").
- Already hosts per-concern databases: `aw_workspace` (core journal),
  `ux_proto`, `awserv`. A new `execution_index` database follows that
  precedent (and the ap_notion_kb db-per-concern precedent on aw-stack's
  pgvector).

The constraint that kills option A, verified as asked:

- aw-knowledgeable's embedding model is `BAAI/bge-m3`, **1024 dims** —
  `repos/aw-knowledgeable/backend/app/core/embeddings.py:88` (DEFAULT_MODEL),
  dim read from the model itself at load (`embeddings.py:320-323`), the
  `graph.py:6672` comment agrees. So the 768-dim vectors are **not portable**
  — every chunk would need re-embedding, and the re-embed path does not exist
  (open Kanban card since 2026-09-27; a model change today trips the
  dim-mismatch boot guard). On top of that, knowledgeable's store is **Neo4j**
  (vector index dimension fixed at creation, `embeddings.py:84-85`), not
  Postgres — option A was never a table move; it's a full re-ingest into a
  different engine.

## 3. The design, concretely

### 3.1 Who owns what after the move

| Piece | Today | After |
|---|---|---|
| Data | `executions` in kb container's private PG 17 | db `execution_index` in `aw-remote-host-postgres` (PG 16, pgvector) |
| Embedder + write/search/status/prune HTTP routes | kb app FastAPI (`routes.py`, `exec_pg.py`) | new **exec-index service**: a `contributes.services` subprocess of `aw-app-agents-platform-runners`, bound to `127.0.0.1:<port>` inside the workspace container |
| `search_execution_history` MCP tool | kb app MCP (`mcp_http.py:138`), gateway name `aw__kb__search_execution_history` | runners app MCP surface, gateway name `aw__agents_platform_runners__search_execution_history`; handler is a thin HTTP call to the local service's `/search` |
| Producer `index_run()` | runners app, posts to kb | unchanged home; posts to the local service (dual-write during cutover) |
| Daily `prune()` sweep | kb app `main.py` lifespan | the exec-index service's own lifespan (same no-external-scheduler rationale as `exec_pg.py:39-41`) |
| Doctor | runners `/execution-index/doctor` (Redis counters) | unchanged; add one service-liveness check (see §6) |

Why a supervised subprocess and not in-process: workspace core runs WORKERS=N
(N=10 here); the fastembed model is ~520 MB *per process that loads it*. One
subprocess holds it once, owned by one worker via the existing service lease
(`src/apps/services.py` — the F4 supervisor with respawn, lease ownership,
stop-on-uninstall; already used by aw-app-codegraphcontext, aw-app-proxy,
aw-app-google-workspace-mcp). The subprocess also trivially reaches both
sides: the producer (core, same container) and the Postgres
(127.0.0.1:5432, same container) — the exact reachability that a Tier-2
container owner would lack.

The service's code is substantially a lift of `exec_pg.py` plus the embedding
helpers of `kb_pg.py` (`_get_model`/`_embed_docs`/`_embed_query`/`_vec_str`)
plus the three routes — keep the 1500-char write refusal, the task prefixes,
and the full-replace upsert semantics byte-for-byte; they are contracts, not
incidentals (a missing `search_query:` prefix silently degrades recall with
no error anywhere). Connection via a new `EXEC_PG_URL`-style setting
(`postgresql://postgres:<pw>@127.0.0.1:5432/execution_index`), password from
app config / secret store — never hardcoded. Keep a shared-secret header on
writes (the existing `execution_index_secret` config key carries over).

### 3.2 The data move (card question 2)

**Not pg_dump/pg_restore.** The source is PG 17, the destination PG 16;
restoring a newer major's dump into an older major is the unsupported
direction. And a plain restore would also collide with dual-written rows
(COPY/INSERT without conflict handling). Instead, for one table this is
three statements:

1. Destination schema is created by the service's own `ensure_exec_schema`
   port on first boot (`CREATE EXTENSION vector` + table + HNSW + btree
   indexes — same DDL as `exec_pg.py:104-128`). **HNSW is rebuilt, never
   dumped** — Postgres never moves index data between clusters anyway; only
   the rows move.
2. Export from the kb container:
   `psql ... -c "\copy (SELECT chunk_id, run_id, seq, content, metadata, indexed_at, embedding::text FROM executions) TO STDOUT"`
   — the `vector` type round-trips as text, which is also what makes the
   PG 17 → PG 16 hop a non-event. ~24.7k rows × (~7.5 KB vector-as-text +
   ≤1.5 KB content) ≈ **~250 MB**; minutes.
3. Import into a staging table in `execution_index`, then
   `INSERT INTO executions ... SELECT ... FROM staging ON CONFLICT (chunk_id) DO NOTHING`
   — DO NOTHING, because by then dual-write is already running (§3.3) and
   any conflicting row in the destination is newer than the export.

Expected duration on 24,717 rows: COPY out + in, a few minutes; HNSW build at
default `m=16, ef_construction=64` over 24.7k × 768-dim vectors, roughly one
to five minutes single-core (bump `maintenance_work_mem` to ~512 MB for the
build). **Whole backfill under ~15 minutes, with zero search downtime** — the
kb side keeps serving reads throughout.

### 3.3 Cutover ordering while writes keep arriving (card question 3)

The lever is exactly the one the card names: `chunk_id` upsert makes every
write idempotent and re-orderable. Ordering:

1. **Ship the new home first.** Runners app release with the service, routes,
   schema bootstrap, and the MCP tool. Producer still single-writes to kb.
   Verify the service is actually running and `/status` answers (see §6 risk
   on service activation).
2. **Start dual-write.** `index_run()` gains a second target
   (`exec_index_url` config next to `kb_base_url`) and posts the same payload
   to both, with per-target failure counters (the doctor's `skipped:*` prefix
   rule picks new names up automatically). Dual-write must live *inside*
   `index_run()`, not at its call sites — the warm-watcher path also calls it.
   A failure on either target never blocks the other, and never blocks run
   delivery (keep the fail-open contract of `execution_index.py:503-505`).
3. **Backfill** per §3.2 — *after* dual-write starts, so there is no gap:
   everything newer than the export is already flowing to both sides, and
   `ON CONFLICT DO NOTHING` keeps the newer dual-written rows.
4. **Verify parity.** `/status` chunk/run counts on both sides (equal modulo
   prune timing), plus a handful of canonical queries compared side by side —
   e.g. the known-good query from
   `docs/design/apmt-execution-history-search.md:66`
   ("fs-sync delete policy approve pending deletions" → 3 specific run ids).
   Scores must match to float precision: same model, same vectors. A score
   drift means an embedding-path divergence (prefix, truncation), not noise.
5. **Flip reads.** The new gateway tool
   `aw__agents_platform_runners__search_execution_history` is live from
   step 1; both tools serve identical data during dual-write, so there is no
   flag-day for readers. Sweep references to `aw__kb__search_execution_history`
   in skills/docs (grep shows the name mostly in design docs today).
6. **Stop writing to kb** (drop `kb_base_url` from the producer's exec-index
   targets). The kb copy freezes and ages out; the retirement of aw-app-kb
   now costs the execution index nothing.

Rollback is trivial at every step before 6: the old path is never touched.

### 3.4 What breaks if retirement lands before this ships (card question 4)

- **Reads:** `search_execution_history` vanishes from the gateway with the
  app — silently; agents just stop having "have we seen this error before",
  which is this workspace's canonical failure mode (silent degradation). No
  error surfaces anywhere an agent looks.
- **Writes:** every `index_run()` POST fails connect; `skipped:kb_post:*`
  counters climb and `/execution-index/doctor` goes red within the 24 h
  window — so `aw-workspace-cli doctor` *does* catch this one. Run delivery
  is unaffected (fail-open by design).
- **Data:** PGDATA lives under `$AW_APP_DATA`
  (`.aw-workspace/data/kb/pgdata`), which the platform treats as durable
  across reinstall. If retirement preserves it, break-glass recovery = run a
  throwaway `pgvector/pgvector:pg17` container mounted on that directory and
  COPY out per §3.2. If it's purged, the index itself is rebuildable — it is
  a 30-day digest of runs AP-MT keeps forever, and
  `scripts/backfill_execution_index.py` re-indexes them — but at real cost:
  hours of CPU re-embedding (~2k+ runs × up to 12 chunks), and only runs
  still passing the `_interesting()` gate. So "retirement first" loses
  availability for days and burns a workday of compute, but is not
  permanent data loss.
- **Bigger than this card:** retiring aw-app-kb also kills
  `search_knowledge_base`, `update/delete_knowledge_base`, `search_skills`,
  `load_skill` (which agents without workspace FS access depend on to load
  their own contracts), the kb-curator skill, the KB window, and the 30-min
  sync task. Out of scope here, but the retirement needs its own card and
  must not be executed with only this migration done.

## 4. Rejected alternatives, and why

- **A — fold into aw-knowledgeable.** Rejected. 1024 ≠ 768 (verified at the
  model, `embeddings.py:88,320-323`): all 24,717 chunks need re-embedding,
  and knowledgeable's re-embed path does not exist (open card, 2026-09-27) —
  choosing A makes that unbuilt path a hard dependency. The store is Neo4j,
  not Postgres, so nothing "moves" — it's a re-ingest. And operationally
  knowledgeable is single-worker-only in prod with extraction globally
  disabled — not where to add a write-per-run workload today. Revisit only
  as a deliberate later unification *after* the re-embed path exists; this
  design deliberately leaves that door open by not touching knowledgeable
  at all.
- **B-as-stated — new Tier-2 container app owning the index.** Rejected as
  the *service* shape (the data half of B is adopted). Two reasons. First,
  reachability: `aw-remote-host-postgres` publishes only to the workspace
  container's loopback and sits on the `aw-remote-host` network — a Tier-2
  app container on `AW_CONTAINER_NETWORK` cannot reach it
  (`pgvector_index.py:34-37`); bridging via `podman network connect` does not
  survive a container recreation, which is exactly the kind of quiet breakage
  this workspace collects. Second, cost: a new app means a new repo, release
  pipeline, marketplace onboarding (two separate GitHub allowlists, release-
  bot ownership of versioning — all documented traps), to host ~400 lines.
  Runner-up: if the runners-app service route proves unworkable in practice,
  this is the fallback, with the network question solved first.
- **C — into AP-MT itself.** Rejected. Cleanest conceptual ownership, but
  AP-MT has no pgvector in its own DB and no embedding path; it lives on the
  bare-metal aw-stack host across a boundary agent containers don't reliably
  cross; and it's multitenant — parking one workspace's embeddings inside it
  inverts the tenancy design. Largest build, new failure domain for every
  agent's search. The sibling design
  `docs/design/apmt-execution-history-search.md` already draws this line:
  AP-MT grows *exact* search over its own permanent record
  (`search_run_events`); the vector digest stays workspace-side. Keep that
  split.
- **Embed in-process in the runners app (no subprocess).** Rejected:
  WORKERS=10 × ~520 MB lazily-loaded model = up to ~5 GB resident for a
  write-rarely workload. (uc-phd already does per-request embedding in-process
  for its own search — that is an existing liability, not a license.)
- **Don't migrate data at all — stand up the new home empty and re-backfill
  from AP-MT.** Genuinely viable because of the 30-day retention, and worth
  naming since it deletes §3.2 entirely. Rejected on cost/benefit: the COPY
  is <15 minutes and exact; the backfill is hours of embedding CPU inside
  the workspace container, loses sub-30-day runs that AP-MT since purged
  (none known, but unverified), and resets `indexed_at` (restarting every
  chunk's retention clock). The COPY path is strictly less risk for less
  work.
- **Keep a bundled Postgres in the new home.** Rejected: it recreates the
  exact lifetime coupling this card exists to remove, and leaves the data
  outside the shared Postgres's (nascent) backup scope.

## 5. What this makes harder later

- **A later unification with aw-knowledgeable gets no head start.** The data
  stays 768-dim nomic; going to knowledgeable later still means the full
  re-embed behind its still-missing re-embed path. This design buys
  decoupling now at the price of not advancing that at all — deliberate.
- **The workspace container's memory budget grows** by one resident
  fastembed model (~600–800 MB process). A second embedding consumer inside
  the container (the documents half, if it ever lands here too) should share
  this service rather than spawn a sibling — the service's `/search` +
  `/executions` API is the seam to extend, and that refactor becomes the
  natural follow-up, not a rewrite.
- **The index now depends on infra the app framework doesn't manage.**
  `aw-remote-host-postgres` is provisioned by the remote-host stack; its
  digest-pin and recreation quirks (documented in the remote-host notes) now
  affect the index. Mitigated by the doctor check (§6) — but a recreated
  Postgres with a lost volume is now an index outage, where before it was
  kb-app-scoped.
- **PG 16 ceiling.** The destination is PG 16; anything wanting
  PG 17+-only pgvector features inherits the shared container's upgrade
  cadence instead of owning its own.
- **The gateway tool prefix changes** (`aw__kb__…` → final flip). Docs,
  skills and agent muscle memory referencing the old name go stale; the
  sweep in §3.3 step 5 is part of the work, not optional polish.

## 6. Risks for the Coders (the non-obvious breakage)

- **Service activation is the known weak point.** The supervisor stops
  services on uninstall and respawns crashes, but "a service that was never
  started" is explicitly not detected (`src/apps/services.py` docstring: the
  activation audit is not built), and there's a live known gap around
  `activate()` boot-skip after core restart. Ship a doctor entry that fails
  when the producer is configured to write to the service but `/status`
  doesn't answer — do not trust install-time green.
- **Embedding parity is silent-fail.** Wrong task prefix, different
  truncation, or a different fastembed revision produce *working* search
  with degraded recall and no error. The §3.3-step-4 score-identity check
  (not just count parity) is the only test that catches it — make it part of
  the delivery, and reuse the kb app's exact helper code rather than
  re-typing it.
- **Dual-write placement.** `index_run()` is called from the post-run hook,
  the warm-watcher thread, and the backfill script. Put the second target
  inside `index_run()` or the backfill and watcher paths silently stay
  single-target.
- **Don't pg_dump.** PG 17 → PG 16 is the unsupported direction; §3.2's COPY
  + staging + `ON CONFLICT DO NOTHING` is the move. A plain COPY straight
  into the live table will abort on the first dual-written row's PK.
- **Counters:** never reset the existing Redis counters; add per-target
  names under the existing `skipped:` prefix so the windowed doctor picks
  them up with no code change (`routes.py:173` contract).
- **The 1500-char refusal stays a refusal** (`exec_pg.py:188-194`). Auto-
  truncating "to be helpful" recreates the bug that check exists to stop:
  upserts that report success while being semantically invisible past
  1500 chars.
- **WORKERS=N:** the service must bind one port owned by one worker's lease
  (the supervisor already handles this); the MCP handler and producer in
  every worker reach it over 127.0.0.1. Don't add any per-worker state.
- **Live verification, not CI green:** after shipping, verify from the
  running system — tool answers through the *gateway* (new tools may need a
  gateway session reload to appear), producer counters advance, doctor
  green. CI cannot see any of the seams this design crosses.

## 7. Scope: `search_knowledge_base` (the documents half) — explicitly separate

This design covers the **execution index only**. The documents half
(`documents` table, `search_knowledge_base`/`update`/`delete`,
`search_skills`, `load_skill`, the KB UI and sync task) is a **separate
migration with a different shape**: its data is rebuilt from mapped folders
and the KB output tree on every build cycle (`kb_ops` + the 30-min sync
task), so *its* move is a rebuild-at-destination, not a data migration, and
its right destination (knowledgeable once re-embed exists? a slimmed app?) is
a product decision not taken here. Nothing in this design forecloses it: the
shared Postgres has room for a `knowledge_base` db later, and the exec-index
service's embedding seam (§5) is reusable. The retirement of aw-app-kb must
not proceed on this card alone (§3.4, last bullet).

## 8. Against the stated prior (asked to argue, not ratify)

The prior — "B is the de-risking move: 768-dim vectors move as-is, the
index's lifetime decouples from the kb app, unify-with-knowledgeable stays a
later decision" — is **right on the data and adopted**. Where it was
incomplete: (1) it had no answer for who runs the embedder and serves the
tool, which is most of what aw-app-kb *is* to this index — this design
answers runners-app-owned supervised service; (2) "the shared
aw-remote-host-postgres" hides a reachability cliff — it is loopback-only on
the workspace container and off the apps network, so the obvious
implementation of B (a new container app pointing at it) does not actually
connect; (3) the PG 17 → PG 16 version skew rules out the obvious
pg_dump/pg_restore. None of these overturn B; all three would have been
discovered mid-implementation and improvised. That is what this document is
for.
