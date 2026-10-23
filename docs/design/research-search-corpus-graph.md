# research-search — corpus graph, lazy reference import, auto AI-Generate on import

Card `4025bf3b-9510-81ae-b5ea-e62a7af00bf2`. Architect design for Frederico's
three connected asks: (1) auto-queue AI Generate whenever a paper is imported,
(2) migrate the index backend from pgvector to Neo4j/GraphRAG, (3) a corpus
graph view (gray = not imported, yellow = imported; edges = bibliographic
references and shared AI-extracted themes) with lazy import of a reference and
document navigation from the graph.

Repo: `tekflox/aw-app-research-search` (checkout
`repos/aw-app-research-search`). Target: `research-search-mcp`.

## Decisions (summary)

| # | Decision |
|---|----------|
| D1 | **Keep pgvector as the one store. No Neo4j migration, no second Neo4j.** The graph is two new Postgres tables in this app's own prefix; GraphRAG *semantics* (vector seeds + graph expansion) are built over pgvector + those edge tables — the same architecture aw-knowledgeable's own `graph` retrieval arm uses. An optional later phase mirrors the document graph into aw-knowledgeable under a dedicated bucket; that mirror is the honest "Neo4j" piece, and it is additive, never the store. |
| D2 | **Auto-AI-Generate lands FIRST** — the reverse of Frederico's floated ordering, because his premise ("migrate before building the trigger") assumed pgvector was being replaced. It isn't (D1), and the trigger is what *feeds* the graph: AI Generate's references/themes legs are the edge data. Landing it first means the corpus already has edges by the time the graph view ships. |
| D3 | Edges = **references AND shared themes**, materialized into an edge table at persist time, full-rebuild-on-change (derived data, small corpus — simple over clever). Reference matching: normalized DOI → normalized-title exact → fuzzy title + first-author + year. "Shared theme" = normalized-label match OR label+description embedding cosine ≥ threshold (default 0.80, config-tunable) using the text model already loaded in this app. |
| D4 | Graph view = **new third tab** in the existing SPA, rendered with **cytoscape** — the library aw-knowledgeable's UI already uses (`repos/aw-knowledgeable/frontend-react/package.json`); no new idiom. |
| D5 | New REST surface in a **new router `api/graph.py`** (avoids touching `api/corpus.py`, which has in-flight edits): `GET /corpus/graph`, `POST /corpus/graph/rebuild`, `POST /corpus/graph/references/{ref_key}/import`. Lazy import reuses the existing `_run_index` machinery and the existing `/corpus/index/status` poll. |
| D6 | **Split into 3 sub-cards** (A trigger → B graph backend → C graph UI), dispatched sequentially, plus a 4th backlog card (D: graph-expanded retrieval + aw-knowledgeable mirror) that is deliberately optional. |

---

## D1 — Storage: keep pgvector, reject the Neo4j rip-and-replace

### What exists (read, not assumed)

* `research_search_app/store.py` — `VectorStore` on the workspace shared
  Postgres via `ctx.db` (`db:own-tables`), tables
  `app__research-search__{documents,chunks,figures,ingest_failures}`,
  nomic-embed-text-v1.5 768-dim, plus the vision twin for figures.
  Load-bearing machinery that a migration would have to rebuild:
  - `_PENDING_SHA` crash-safe two-phase stamp (`store.py:100`,
    `replace_document` `store.py:428`) — the whole Index-action job state
    machine in `api/corpus.py:168` (`queued/indexing/indexed/failed`) is
    *derived from* this invariant, deliberately, because
    `AW_WORKSPACE_WORKERS>1` makes in-process job dicts unusable.
  - Figures' **two deliberately-unmerged rankings** (`store.py:565`,
    measured text-vs-visual similarity gap 8–13x documented at
    `store.py:38-49`) — dual vector columns per row.
  - Page-exact chunk traceability (`chunk_pages`, `store.py:240`).
* AI Generate already extracts the graph's raw material per document:
  `review_references` (raw + structured jsonb incl. `doi_or_url`) and
  `review_coding` (codes/themes), written by
  `review_generate._persist_dag_response` (`review_generate.py:905`).
* aw-knowledgeable (`repos/aw-knowledgeable`) is the workspace's existing
  Neo4j GraphRAG service; this workspace talks to it only through the
  Knowledgeable Connector app's HTTP tools — by design it exposes **no bolt
  and no Cypher** to callers (`repos/aw-app-knowledgeable/README.md`).

### The decision

Keep pgvector/Postgres as the *only* store research-search operates. Add two
tables (migration `0008`) for the document graph. Deliver GraphRAG semantics
— retrieval that widens vector seeds through graph edges — in-app over those
tables (phase D), which is exactly how aw-knowledgeable's own `graph` mode
works (vector-seed → LINKS_TO expansion → score the widened set).

### Rejected alternatives, and why

1. **Full migration to Neo4j (vector indexes + graph in one DB).** Rebuilds
   all of `store.py` including the figures dual-embedding design and the
   `_PENDING_SHA` crash-safety that `api/corpus.py`'s poll contract depends
   on; requires re-embedding or bulk-moving every indexed document; and —
   the dominant cost — makes a Tier-1 app, whose Postgres access is free via
   `db:own-tables`, dependent on a Neo4j this workspace must operate.
   Operating Neo4j here is not hypothetical cost: credential rotation
   touches five places (memory: `rotating-production-neo4j-touches-five-
   places-not-two`), the host runs chronically near disk-full, and this
   workspace's documented failure mode is silent degradation — a new
   stateful JVM service is a new silent-degradation surface. All that buys
   retrieval nothing pgvector doesn't already do here.
2. **Share aw-knowledgeable's Neo4j over bolt.** Couples research-search to
   another service's *private* database with no API contract; aw-knowledgeable
   deliberately exposes no bolt/Cypher to anyone. Rejected outright.
3. **Use aw-knowledgeable's REST API as the primary store** (upload each
   paper as a Document, search via `knowledge_search`). Loses page-exact
   chunk traceability and figures entirely (no vision embeddings there),
   changes "My corpus" search semantics wholesale, and puts a user-facing
   app's core feature behind a single-worker production service that has its
   own load profile. Kept instead as the **optional phase-D mirror**: upload
   + `create_link` into a dedicated bucket (buckets exist —
   `docs/design/aw-knowledgeable-buckets.md`) so the corpus becomes
   discoverable from Frederico's daily `knowledge_search`, *in addition to*
   the app's own store. If Frederico wants the full replacement anyway after
   reading this trade-off, that's a scope conversation with the PO — the
   costs above are the input to it.

### Migration path for already-indexed documents

None needed for the store (it doesn't change). The graph backfills from data
that already exists: `POST /corpus/graph/rebuild` recomputes edges from
`review_references`/`review_coding`, and phase A's backfill endpoint queues
AI Generate for documents that never ran it, which then populates their
references/themes and (via the persist hook) their edges.

## D2 — Sequencing: trigger first, graph second

Frederico floated "maybe migration before auto-generate". The reason to do
that would be building the trigger against a store about to be replaced;
with D1 the store is not being replaced, so the premise falls away. The
positive reason to invert: every paper imported from the moment A ships gets
references+themes extracted automatically — exactly the edge data B and C
need — so the graph is born populated instead of empty. A is also the
smallest, most independently verifiable piece.

Order: **A (trigger) → B (graph backend) → C (graph UI)**, sequential
dispatches (same working tree; parallel coders on one repo is a known
corruption risk), then D later.

---

## Phase A — auto AI-Generate on import (sub-card, coder-sonnet)

**Trigger seam.** There are exactly two import completion points, both ending
in `ingest.index_paper(...)` returning `{"ok": True, ...}`:

* `api/corpus.py:144` — `_run_index` background task (web Index action; also
  what phase B's lazy import reuses, so it inherits the trigger for free).
* `mcp_ingest.py` — the `index_paper` MCP tool handler.

Add one helper (new module `research_search_app/auto_generate.py`, keeping
both call sites one-line): `maybe_queue_generate(ctx, document_id, md_path)`
→ checks config flag `auto_generate_on_import` (default **true**), then calls
`review_generate_mod.start_generate_job(ctx.db, document_id=..., md_path=...,
field_keys=None, overwrite=False, config=ctx.config,
get_secret=secret_reader(ctx), set_secret=secret_writer(ctx),
delete_secret=secret_deleter(ctx))` — the same call
`api/review.py:277` makes. Constraints:

* **Never let the trigger fail the import.** `LLMNotConfiguredError`,
  `FileNotFoundError`, anything — catch, log, done. The paper being indexed
  is the contract; the review is a bonus.
* `overwrite=False` (fill-only-empty) and `claim_job`'s DB-backed
  per-document claim (`review_generate.py:806`) already make re-index
  triggers idempotent and worker-safe. No new job infrastructure.
* Do not trigger on `ok: False` paths, and don't trigger from inside
  `ingest.index_paper` itself (it has no `ctx`/config/secrets — keep it a
  pure ingest function).

**Backfill.** `POST /corpus/review/generate/backfill` (in `api/review.py`):
starts jobs for up to N (body param, default 3) indexed documents that have
no AI-generated review data yet (no `review_generate_jobs` row with
state done, or no `source='ai'` rows), returns
`{started: [...], remaining: int}`. Frederico clicks it a few times; no queue
infra. Phase C surfaces a button for it.

**Explicit non-goal:** no global concurrency cap across workers. Imports are
click-driven (one at a time); the backfill batches at N per call. If bulk
import ever ships, revisit with a DB-counted cap.

## Phase B — graph backend (sub-card, coder-opus)

**Migration `0008_create_graph_tables.sql`** (same header rules as 0007 —
quoted hyphenated names, nothing `public.`-qualified):

* `app__research-search__graph_external_refs` — one row per *deduplicated*
  not-yet-imported work: `ref_key text PK`, `doi`, `title`, `first_author`,
  `year int`, `venue`, `raw` (a representative citation), `cited_by_count
  int`, timestamps. `ref_key` = normalized DOI when present, else
  `t_<sha1(normalized title)>`. Dedup across citing documents is the point:
  two papers citing the same unimported work share one gray node.
* `app__research-search__graph_edges` — `id bigserial PK`,
  `src_document_id text NOT NULL REFERENCES documents ON DELETE CASCADE`,
  `dst_document_id text NULL REFERENCES documents ON DELETE CASCADE`,
  `dst_ref_key text NULL REFERENCES graph_external_refs ON DELETE CASCADE`,
  `kind text CHECK (kind IN ('reference','shared_theme'))`, `weight real
  NOT NULL DEFAULT 1.0`, `detail jsonb NOT NULL DEFAULT '{}'`, timestamps;
  CHECK exactly one of dst_document_id/dst_ref_key is non-null; partial
  unique indexes on (src, dst_document_id, kind) and (src, dst_ref_key,
  kind). `shared_theme` edges are document↔document only and undirected —
  canonicalize `src < dst`.

**Edge builder** (new `research_search_app/graph.py`), matching in Python —
no pg_trgm (CREATE EXTENSION may need privileges the app migrations don't
have), corpus is O(10²) docs / O(10³) refs:

* Normalize DOI: lowercase, strip `https://doi.org/` etc. from `doi_or_url`
  (the field genuinely holds either a DOI or a URL — handle both, a non-DOI
  URL falls through to title matching).
* Reference → document resolution ladder: DOI exact vs `documents.doi` →
  normalized-title exact vs `documents.title` → fuzzy (difflib/rapidfuzz
  ratio ≥ ~0.90, calibrate on real rows) + first-author surname + year ±1.
  `detail` records which rung matched.
* Shared-theme edges: pull `review_coding` rows `kind='theme'`; connect two
  documents when normalized labels are equal, OR when
  `embed_docs(label + ": " + description)` cosine ≥ `theme_edge_threshold`
  (config, default 0.80). `weight` = similarity; `detail` = the two labels.
  Embedding runs off the event loop (`asyncio.to_thread`), same rule as
  every other `embed_*` caller.
* `rebuild(db)` = full recompute, delete-then-insert per kind (derived data;
  last-writer-wins under concurrency is acceptable). Hooks:
  1. end of `_persist_dag_response` when `written` is non-empty
     (`review_generate.py:905`) — new references/themes landed;
  2. after a successful `index_paper` in `_run_index` — a newly imported
     document may *be* an existing gray node: re-resolve, flip edges from
     `dst_ref_key` to `dst_document_id` (gray→yellow is a data event, not a
     UI trick).
  Important instability to design around: `replace_ai_references` deletes and
  reinserts rows on every generate — never key anything on
  `review_references.id`; `ref_key` is content-derived for exactly this
  reason.

**Routes** (new `api/graph.py` router, registered beside the others in
`routes.py`):

* `GET /corpus/graph` → `{nodes: [{id, kind: "document"|"external", title,
  imported, starred, has_review, year, doi}], edges: [{source, target, kind,
  weight, detail}], rebuilt_at}`. Whole graph — no focus/depth until the
  corpus outgrows it.
* `POST /corpus/graph/rebuild` → 202, background task (model cold-load can
  be ~15s and the tunnel edge cuts at 30s).
* `POST /corpus/graph/references/{ref_key}/import` → resolve the external
  ref to a concrete (source, paper_id): `upstream_client.search_papers`
  with title (or DOI) as query, accept the best hit whose normalized title
  matches ≥ ~0.85; on success kick the **same** `_run_index` background task
  and return 202 `{source, paper_id, document_id}` (client polls the
  existing `GET /corpus/index/status`); on no confident hit return 404
  `{reason, suggested_query}` so the UI can offer "open in Search tab". This
  is plumbing over existing `search_papers`/`download_with_fallback`/
  `index_paper` — no new fetch logic. Because it reuses `_run_index`, a
  lazy-imported paper auto-queues AI Generate (phase A) and then joins the
  graph with its own edges — the flywheel Frederico is actually asking for.

MCP tools (`get_corpus_graph`, `import_reference`) are a named follow-up,
not in B's scope — the third in-process MCP server (`mcp_ingest.py`) can
wire the same functions later without touching `graph.py`.

## Phase C — graph UI (sub-card, coder-sonnet)

* Third tab in `ui/src/App.jsx` `TABS` (`{id: 'graph', label: 'Graph'}`),
  new `ui/src/views/GraphView.jsx`. Lift nothing into App state beyond what
  Corpus already does; refetch on tab entry.
* **cytoscape** (plain, no react wrapper — mount into a ref'd div), the
  workspace's existing graph idiom. Layout: built-in `cose`. No other new
  deps.
* Node styling via existing `theme.css` variables: document nodes amber/
  yellow, external refs gray, starred gets a ring/badge; label = short
  title. Edges: `reference` solid with arrow (citing → cited);
  `shared_theme` dashed, no arrow, tooltip/click shows the matched theme
  labels from `detail`.
* Interactions: click yellow node → `window.location.hash = '#/doc/<id>'`
  (existing DocumentViewer route). Click gray node → side panel with the
  citation (`raw`), structured fields, cited-by count, and an **Import**
  button → `POST .../references/{ref_key}/import` → poll
  `/corpus/index/status` (the exact pattern `Catalogues.jsx`'s Index action
  already implements — copy it) → on `indexed`, refetch the graph; the node
  flips color because the backend flipped the edge rows. On 404, show the
  reason + a button that jumps to the Search tab prefilled with
  `suggested_query`. Also a "Rebuild graph" affordance and the phase-A
  backfill button ("Generate reviews for N documents missing them").
* Rebuild `ui/dist` and commit it — this app serves the committed bundle.

## Phase D — future card, not dispatched now (backlog)

1. Graph-expanded corpus search: `GET /corpus/search` gains an `expand`
   mode — seed top-k chunks, collect their documents, widen one hop through
   `graph_edges`, pull each neighbor's best chunk, return a fused list
   (fuse by **rank**, never raw distance — `store.py:38`'s documented rule).
   This is the retrieval half of "GraphRAG" and needs no Neo4j.
2. aw-knowledgeable mirror: push Document nodes + reference/theme links into
   a dedicated bucket (e.g. `research-search`) via the Knowledgeable
   Connector (`upload_document`/`create_node`/`create_link`), app→app over
   loopback. Makes the corpus answerable from Frederico's main
   `knowledge_search` beside the rest of his knowledge. Prereqs to verify at
   build time: bucket parameter on the upload path; connector installed;
   idempotent re-push keyed on document_id.

## Risks for the Coders

1. **In-flight working tree.** `repos/aw-app-research-search` has uncommitted
   changes right now (pdf_path-containment fix in `ingest.py`/`paths.py`/
   `api/corpus.py`/tests, and `aw-app.json`). Leave them intact, stage only
   your own files, never hand-bump `aw-app.json` (release bot owns versions).
2. `DbTables.execute` opens one transaction per call — delete-then-insert
   rebuilds are not atomic; a concurrent `GET /corpus/graph` can see a
   half-rebuilt edge set for a moment. Accepted for derived data; don't try
   to fix it with module-level locks (WORKERS>1 makes them per-process lies).
3. `start_generate_job` raises `LLMNotConfiguredError` when the anthropic
   transport has no key — the auto-trigger must swallow it or every import
   on an unconfigured workspace fails.
4. `structured.doi_or_url` lies about being a DOI — it's frequently a bare
   URL or empty. The matching ladder, not DOI alone, is the contract.
5. Theme labels are generated per document with no shared vocabulary —
   exact-match-only would produce a near-empty theme graph; embedding
   similarity is what makes these edges exist. Calibrate the threshold on
   the real corpus and put the number in config, not in code.
6. The job-status state machine in `api/corpus.py` treats "no documents row,
   no failure row" as `queued` forever — the lazy-import endpoint must
   record a failure row on *every* non-started path (same discipline
   `_run_index` already documents at `api/corpus.py:149-154`).
7. Tests: follow the existing fake-`db` double pattern in `tests/`; the
   matching ladder and theme-similarity edge builder are pure functions —
   unit-test them without Postgres or the model (inject precomputed
   vectors).
