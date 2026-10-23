# research-search — graph layer (GraphRAG), graph viz UI, lazy reference import — without Neo4j

Status: **design, not code.** Architect, 2026-10-23. Card
`4025bf3b-9510-81c9-8daa-ec144a45a9b4`
(`architecture:research-search-graphrag-neo4j-migration-and-graph-viz`).

Frederico's instruction (Telegram, PT, condensed): change research-search
from pgvector to GraphRAG with Neo4j; visualize documents as a graph
connected by bibliographic references and by themes extracted by AI
Generate; gray = known-but-not-imported, yellow = imported; lazily
search/import a paper's references from the graph; navigate to documents
through the graph. Explicitly sequenced BEFORE auto-AI-Generate-on-import
(that feature is noted as a hook here, not designed).

Every claim below is grounded in a file read this session or a live API
call against the running app this session (2026-10-23).

---

## 0. Live facts this design stands on (verified 2026-10-23)

| Fact | Value | How verified |
|---|---|---|
| Corpus size | **40 documents, 2,895 chunks, 570 figures** | live `GET /api/apps/research-search/api/index/status` |
| App tier | **Tier-1 in-process**, `db:own-tables` on the workspace Postgres (pgvector ready, `probe_state()` green live) | `aw-app.json`, live status |
| Vector store | `store.py` — `documents`/`chunks`/`figures`/`ingest_failures`, nomic-embed-text-v1.5, 768-dim, HNSW | `research_search_app/store.py:72-92`, `migrations/0001` |
| References already extracted | `review_references` rows: `raw` + `structured` jsonb (`first_author/year/title/venue/doi_or_url`), written by the Literature Review DAG's references leg | `migrations/0007_create_review_coding.sql:42-54`, `review.py:489` |
| Themes already extracted | `review_coding` rows with `kind='theme'`, per-document, free-text `label` | `migrations/0007:21-35` |
| AI-Generate-ran signal | `review_generate_jobs.state='done'` (one row per document, Postgres-polled by design for WORKERS>1) | `migrations/0006` |
| Import machinery | `POST /corpus/index` → `_run_index` → `download_with_fallback` → `ingest.index_paper`; status polled from Postgres | `api/corpus.py:123-170` |
| Catalogue search | `GET /catalogue/search` (fans out to ~21 catalogues, normalized result shape) | `api/catalogue.py:64` |
| UI stack | plain Vite+React SPA, hash routing, tabs in `App.jsx`, existing `#/doc/<id>` route | `ui/src/App.jsx:16-30` |
| House graph-viz idiom | **cytoscape** (`GraphCanvas.tsx` + `cytoscapeStyle.ts`, with a NODE_BUDGET cap) | `repos/aw-knowledgeable/frontend-react/src/components/GraphCanvas.tsx:16` |
| Neo4j precedent | 2026-10-12 reversal: aw-knowledgeable is moving chunks/vectors/topics OUT of Neo4j to pgvector; Neo4j retained only for genuinely-traversed data | `docs/design/aw-knowledgeable-pgvector-parallel-store.md` §1–2 |
| Neo4j Community constraint | single user database per instance; backup = stop-the-container dump | same doc §6, `aw-stack/scripts/backup-neo4j.sh` |

---

## 1. The approach, in sentences someone can disagree with

1. **There is no storage migration.** pgvector stays exactly where it is,
   serving chunk and figure search unchanged. "GraphRAG" is delivered as a
   capability — a graph *layer* over data research-search already owns —
   not as a product swap.
2. **The graph layer is a Postgres projection**, not a new store: one new
   migration gives cited-paper identity (`ref_targets`) so a reference can
   be a first-class gray node; reference edges come from
   `review_references`, theme edges from `review_coding(kind='theme')` by
   normalized label match. At 40 documents (≈1,500–2,500 nodes with
   external refs), the whole graph fits in one API response.
3. **No Neo4j — and that is the conclusion the 2026-10-12 reversal
   demands**, not a contradiction of Frederico's ask. See §2. The payoff he
   asked for (graph viz, lazy import, graph navigation, graph-aware
   retrieval) is delivered in full; only the storage technology differs.
   This is the one deliberate deviation from the instruction, flagged for
   his veto: if he wants Neo4j-the-product regardless, §4.2's runner-up is
   the shape to build — the UI, API contract, and resolution design below
   survive that swap unchanged.
4. **Lazy import is the existing import flow triggered from a node**: gray
   node click → panel → `GET /catalogue/search` prefilled from the
   reference's structured fields → user picks the hit → existing
   `POST /corpus/index` → on success the resolver links the ref-target to
   the new document and the node turns yellow. Human picks the match in v1;
   auto-resolution only on exact DOI equality.
5. **Graph-aware retrieval** (the RAG in GraphRAG) is a sibling section on
   corpus search: after the pgvector top-k, expand one hop over
   reference+theme edges and return neighbor documents' best chunks under a
   separate `related` key — never fused into the distance ranking (the same
   two-rankings discipline `store.py` already enforces for figures).
6. **Auto-AI-Generate-on-import is not built**, but its hook is named: the
   success path of `_run_index` (`api/corpus.py:143-153`, right after
   `ingest.index_paper` returns ok) is where a future card enqueues a
   review-generate job; the graph payload already carries `has_review` per
   node (from `review_generate_jobs.state='done'`), so the viz needs
   nothing more when that lands.

---

## 2. Why not Neo4j — the reversal applies here with more force, not less

The card requires this addressed head-on. The 2026-10-12 decision
(`architecture:knowledgeable-move-out-of-neo4j-connector-owns-work`,
`docs/design/aw-knowledgeable-pgvector-parallel-store.md`) moved
aw-knowledgeable's 33,408 chunks + embeddings + topic tree out of Neo4j
into pgvector, keeping Neo4j only for the entity/human graphs. Its stated
taxonomy: **"everything whose access pattern is an index lookup or a
one-hop join moves; everything that is actually traversed stays."** The
drivers were concrete: Neo4j Community's vector index takes no filter
argument (forcing an over-fetch/escalation machine), the page-cache ceiling
at scale, and the stop-the-container backup window that grows with data.

Test research-search's graph against that taxonomy:

- **Graph viz** = "give me the whole corpus graph" — one query over three
  tables. No traversal.
- **Lazy import / navigation** = single-row lookups and one-hop joins.
- **Graph-aware retrieval** = top-k vector hits + one-hop edge expansion —
  a `JOIN`, not a BFS.

Nothing here is deeper than one hop. By the reversal's own rule this is
*"a table pretending to be a graph"* — and unlike aw-knowledgeable, there
is no 33k-chunk store to relieve, no entity graph, no `EXISTS{}` visibility
predicates, no radius-2 BFS. aw-knowledgeable kept Neo4j for data it
genuinely traverses; research-search has no such data. Proposing a new
Neo4j consumer for ~2k one-hop nodes eleven days after that reversal would
be re-litigating it in the wrong direction.

The instance question the card asks dies with the store decision, but for
the record both options are independently bad:

- **Sharing `aw-neo4j`**: Neo4j Community supports **one** user database.
  research-search nodes would live inside aw-knowledgeable's single
  database, breaking that service's "one module is the only place touching
  a store driver" discipline (its K5 guard), coupling two apps' lifecycles,
  and adding a sixth place to the already-painful five-place Neo4j
  credential rotation.
- **A dedicated instance**: a new always-on container (~1 G+ RAM on a host
  at 86% disk), a new stop-the-container backup script, a new credential —
  to serve a graph that today fits in a 200 KB JSON response.

**The trigger that reopens this** (named so it isn't re-derived): corpus
grows past ~10k imported documents, or a retrieval feature genuinely needs
≥3-hop traversal (e.g. citation-path queries) that recursive CTEs serve
poorly, or Frederico overrides on reading this. Until then, Postgres.

---

## 3. Where it lands — files and seams

### 3.1 Migration `0008_create_ref_targets.sql`

Same header rules as 0007 (no vector types → nothing `public.`-qualified;
hyphenated names double-quoted):

- **`app__research-search__ref_targets`** — one row per *cited work*,
  deduped across citing documents:
  - `key text PRIMARY KEY` — deterministic: normalized DOI when present
    (lowercase, strip `https://doi.org/` prefixes), else
    `sha256(norm_title|year|norm_first_author)[:40]` where `norm_*` is
    lowercase, whitespace-collapsed, punctuation-stripped. Store
    `norm_version integer NOT NULL DEFAULT 1` beside it so a future better
    normalizer can migrate keys instead of orphaning them.
  - `doi text`, `title text`, `year integer`, `first_author text`,
    `venue text` — best-effort display fields from `structured`.
  - `document_id text REFERENCES documents(id) ON DELETE SET NULL` —
    **NULL = gray node; set = resolved (yellow, merges with the document
    node)**.
  - `created_at`, `resolved_at timestamptz`.
- **`review_references` gains `target_key text`** (nullable, indexed; no FK
  `ON DELETE CASCADE` back-pressure — a re-resolution pass may rewrite
  keys).

### 3.2 Resolution module — new `research_search_app/graph.py`

Pure functions + a `GraphStore` over `ctx.db` (the `store.py`/`review.py`
house shape — tests use the same fake-db double):

- `resolve_references(db, document_id)` — for each `review_references` row
  of that document: compute `key` from `structured`, upsert `ref_targets`,
  stamp `target_key`. Match to an imported document: exact normalized-DOI
  equality against `documents.doi`, else exact normalized-title equality
  against `documents.title`. Rows whose `structured` is empty (no title, no
  DOI) get `target_key NULL` and are simply absent from the graph — an
  unparseable citation string is not a node.
- `resolve_new_document(db, document_id)` — inverse direction, called after
  a successful import: match the new document's DOI/title against
  unresolved `ref_targets`, set `document_id`/`resolved_at` on hits. This
  is what flips gray → yellow after a lazy import (and retroactively, when
  a manually imported paper happens to be something the corpus already
  cites).
- **Hook sites** (both already in the request/worker path, both idempotent
  re-runs):
  - `review_generate.py:980` — immediately after `replace_ai_references`.
  - `api/corpus.py` `_run_index` success path (after `ingest.index_paper`
    returns `ok`, ~line 150) — call `resolve_new_document`. **This same
    seam is the future auto-AI-Generate hook** (flagged, not built).
  - Reference CRUD in `api/review.py` (human add/edit/delete of a
    reference) re-resolves that document.
- One-shot backfill for the 40 existing documents:
  `POST /api/graph/resolve` (admin-ish, idempotent, loops
  `resolve_references` over all documents then `resolve_new_document`) —
  cheap at this scale, also the recovery tool if resolution ever drifts.

### 3.3 Graph read API — new `research_search_app/api/graph.py`

`GET /api/graph` → one payload, assembled in SQL (aggregates, not N+1):

```
{
  "nodes": [
    {"id": "<document_id>", "kind": "document", "title", "starred",
     "has_review": bool,            // review_generate_jobs.state='done'
     "ref_count": int},
    {"id": "ref:<key>", "kind": "external", "title", "year",
     "first_author", "doi", "cited_by": int}   // unresolved ref_targets only
  ],
  "edges": [
    {"source": "<doc>", "target": "<doc>|ref:<key>", "kind": "reference"},
    {"source": "<doc>", "target": "<doc>", "kind": "theme",
     "labels": ["..."], "weight": int}
  ],
  "stats": {"documents": n, "external": n, "reference_edges": n, "theme_edges": n}
}
```

- Theme edges computed at read: pair documents sharing
  `lower(btrim(label))` on `review_coding` rows with `kind='theme'`,
  `weight` = count of shared labels, aggregated in one self-join `GROUP BY`
  doc-pair. No materialized edge table in v1 (40 docs; revisit if the
  self-join ever shows up in timings).
- Query params: `include_external=true|false` (default true),
  `min_cited_by` (default 1 — i.e. everything; the UI exposes a filter
  because 40 docs × ~30–80 refs each can mean 1–3k gray nodes), and a
  server-side **node cap of 1,000** prioritized by `cited_by DESC` with a
  `truncated: true` flag in `stats` — no silent cap (house rule).
- Resolved ref_targets do **not** emit an `external` node — the edge points
  at the document node directly. One work, one node.

### 3.4 Graph UI — new `ui/src/views/Graph.jsx`, third tab

- `App.jsx`: add `{ id: 'graph', label: 'Graph' }` to `TABS`
  (`ui/src/App.jsx:16-19`); route exactly like Corpus/Catalogues.
- **Rendering: cytoscape** (plain `cytoscape` npm dep, no React wrapper,
  no layout plugins — built-in `cose` layout). This is the workspace idiom
  (`aw-knowledgeable/frontend-react/src/components/GraphCanvas.tsx`), it
  handles 1–3k nodes with canvas rendering, and its style/selector model
  maps 1:1 onto the node/edge kinds above. Mount in a `ref` div +
  `useEffect`, destroy on unmount — the same lifecycle GraphCanvas uses.
  Rejected: hand-rolled SVG/d3-force (re-implements pan/zoom/hit-testing
  cytoscape gives for free), react-force-graph (new idiom, WebGL dep).
- Styling via `theme.css` variables so light/dark keep working: document
  nodes **yellow** (`#eac54f`-family), external nodes **gray**
  (`#9ca3af`-family), reference edges solid directed, theme edges dashed
  undirected with `weight`-scaled width. Starred docs get a distinct
  border; `has_review` shown in the detail panel (and is the property the
  future auto-queue will read). Legend in-view, filter toggle for external
  nodes / `min_cited_by` slider.
- **Click behavior**: yellow node → `window.location.hash = '#/doc/<id>'`
  (existing DocumentViewer route, zero new routing); gray node → in-view
  side panel (not a modal) with title/author/year/DOI/raw citation + the
  lazy-import flow (§3.5). Tab state (filters, camera) lifted to `App.jsx`
  `graphState` exactly like `corpusState` — the view unmounts on `#/doc/`
  navigation and must survive the round trip.

### 3.5 Lazy reference import — from the gray-node panel

v1 is deliberately human-in-the-loop (a bibliography entry is fuzzy; a
wrong auto-match silently poisons the corpus):

1. Panel opens with a search preview: `GET /catalogue/search?q=<title>`
   (or DOI when present) — the existing endpoint, existing normalized
   result shape (`api/catalogue.py:47-65`).
2. Results listed with source badges; a hit whose normalized DOI **equals**
   the target's DOI is auto-highlighted as "exact match" and sorted first.
3. User clicks Import → existing `POST /corpus/index {source, paper_id,
   doi, title}` → poll existing `GET /corpus/index/status` (the Corpus
   tab's state machine, reused) → on `indexed`, `resolve_new_document` has
   already linked the target server-side; the panel refetches `/api/graph`
   and the node flips yellow in place.
4. Failures surface the `ingest_failures` reason in the panel (the
   machinery already records it — `api/corpus.py:123-153`).

Deferred (named, not built): trigram/embedding fuzzy matching for
no-DOI references; "import all references" bulk action; background
auto-import. Each is additive on `ref_targets` + this panel.

### 3.6 Graph-aware retrieval — the RAG payoff

`GET /corpus/search` (and `search_paper_text`, `store.py:635`) gain
`graph_expand: bool = false`. When set: take the distinct documents of the
pgvector top-k, expand one hop over reference + theme edges to imported
neighbors (SQL join on `review_references.target_key → ref_targets.document_id`
plus the theme pair query), and return each neighbor's single best chunk
for the query under a **separate `related` key** — with `via:
"reference"|"theme"` provenance per entry. Never merged into the main
ranked list (the figures two-rankings rule, same reasoning: different
retrieval semantics must not share one ORDER BY). MCP exposure: a
`graph_expand` parameter on the existing corpus-search surface when that
tool lands — no new tool minted by this design.

---

## 4. What I rejected, and why

1. **Full Neo4j replacement (vectors included)** — Frederico's literal
   phrasing. Re-embeds or copies 2,895 chunks into the exact store shape
   the 2026-10-12 reversal is actively migrating 33k chunks out of
   (filterless community vector index, stop-the-container backups), for an
   app whose vector search works today. Rejected on the precedent's own
   measured grounds.
2. **Hybrid: pgvector stays, dedicated Neo4j for the graph layer only.**
   The honest runner-up, and the shape to build **if Frederico overrides
   §2 on reading this** — the API contract, resolution logic, and UI are
   store-agnostic and survive the swap. Rejected for v1 because every
   access pattern is ≤1 hop over ≤3k nodes: a new container, credential,
   and backup surface for a projection SQL already serves. The reopen
   triggers are named at the end of §2.
3. **Sharing `aw-neo4j`.** Community Edition is single-database;
   research-search would write into aw-knowledgeable's store, violating
   its one-module store discipline and entangling credential rotation
   (five places today) and backup windows across two products. Worst
   option; rejected unconditionally.
4. **Materialized theme-edge / graph-edge tables.** Precomputation to make
   reads cheap that are already cheap; adds a staleness invariant (edges
   vs. review rows) for nothing. The read-time self-join is the v1; the
   table is the documented fallback if `/api/graph` timings ever say so.
5. **Auto-import on gray-node click (no human pick).** A `structured`
   blob is LLM-extracted best-effort; title collisions and versioned
   preprints make silent auto-import a corpus-poisoning risk. DOI-exact
   auto-highlight keeps the happy path one click without the risk.
6. **A new `graph_nodes` universal table replacing `documents` +
   `ref_targets`** (one node table, GraphRAG-textbook style). Forces every
   existing query through a remodel of the app's primary table for zero
   new capability; `ref_targets` + the existing tables express the same
   graph.

---

## 5. What this makes harder later

- **A real multi-hop future costs a second remodel.** If citation-path
  traversal (≥3 hops) becomes a product need, recursive CTEs will carry it
  to a point, then the §2 trigger fires and the graph layer moves — the
  projection design means moving *tables*, not untangling retrieval.
- **`ref_targets.key` bakes in the v1 normalizer.** A better resolver
  (fuzzy, embedding-based) must rewrite keys; `norm_version` makes that a
  migration instead of a guess, but it is still a data migration.
- **Theme edges inherit AI Generate's label churn.** Re-running AI Generate
  replaces `source='ai'` theme rows (`review.py:382-392`), so theme edges
  can appear/vanish between runs. Accepted: the edges are honest about
  their provenance; pinning them would mean freezing labels the user can
  regenerate at will.
- **The whole-graph payload has a ceiling.** One response with a 1,000-node
  cap is right for 40 docs; at ~thousands of documents the UI needs a
  neighborhood/paging endpoint (the aw-knowledgeable focus+depth shape).
  Named so nobody bolts pagination on in a panic.
- **`graph_expand` becomes API surface** the moment the UI or an agent
  scripts against it; retiring or renaming it later is a breaking change.

---

## 6. Risks for the Coders

1. **`DbTables.execute` sharp edges** (documented in `store.py:93-99`,
   `store.py:388-396`): only `SELECT ...` gets `fetchall()`'d; UPDATE
   returns a `CursorResult` — check `rowcount`, not truthiness. No
   multi-statement transaction across calls — `resolve_references` must be
   idempotent per-row (upsert, re-stamp), never assume all-or-nothing.
2. **Migration file rules flip vs. query rules** (`migrations/0001` header):
   nothing in 0008 is a vector type so nothing is `public.`-qualified, but
   every table name is double-quoted (hyphenated prefix).
3. **WORKERS>1**: resolution runs in whichever worker handled the request;
   all state lives in Postgres (the `review_generate_jobs` precedent —
   `migrations/0006:7`). No module-level caches of the graph; `/api/graph`
   reads fresh every call.
4. **`/api/graph` must aggregate in SQL.** A per-document loop over 40 docs
   is survivable today and a trap tomorrow; the theme self-join and the
   ref-edge join are each one statement.
5. **UI ships a committed `ui/dist`** — adding the `cytoscape` dep means
   `npm install` (use the HOME-cache workaround for the documented EACCES)
   + `vite build` + committing dist, and the check-dist gate must pass. No
   invented Tailwind classes — this app styles via `theme.css` only.
6. **cytoscape lifecycle**: create in `useEffect`, `cy.destroy()` on
   cleanup, never re-create on every render (GraphCanvas.tsx is the
   reference implementation, including its node-budget idea).
7. **Don't touch the DAG legs.** `review_generate.py`'s pipeline is live
   and has its own in-flight design cards (language match/selector). The
   only edit there is the one-line resolution hook after line 980 —
   anything more is scope creep into someone else's seam.
8. **Reference CRUD already exists** (`api/review.py`); human-added
   references must flow through resolution the same as AI ones — kind is
   provenance, not graph eligibility.
9. **Shared checkout discipline**: multiple agents are active on this tree;
   re-check `git status` immediately before committing.

---

## 7. Decomposition — four cards, explicitly, not one mega-diff

| Card | Scope | Depends on | Size |
|---|---|---|---|
| **G1 — graph data layer** | migration 0008, `graph.py` (resolution both directions + GraphStore), hooks at `review_generate.py:980` / `_run_index` / review CRUD, `POST /api/graph/resolve` backfill, `GET /api/graph`, tests (fake-db + throwaway pg) | — | 1–1.5 d |
| **G2 — graph view UI** | Graph tab, cytoscape canvas, gray/yellow + legend + filters, yellow→`#/doc/<id>` nav, gray→detail panel (read-only), `graphState` lift, dist rebuild | G1 | 1–1.5 d |
| **G3 — lazy import from the graph** | gray-node panel search (`/catalogue/search` reuse), DOI exact-match highlight, import via existing `/corpus/index` + status poll, node flip on resolve, failure surfacing | G1+G2 | ~1 d |
| **G4 — graph-aware retrieval** | `graph_expand` on `/corpus/search` + `search_paper_text`, `related` sibling section with `via` provenance, tests | G1 (not G2/G3) | 0.5–1 d |

Sequence: G1 → (G2 ∥ G4) → G3. Each card is independently reviewable and
independently shippable; G4 can land before or after the UI cards.

**v1 ships:** all four cards above.
**Deferred, named:** fuzzy/embedding reference resolution; bulk "import all
references"; background auto-import; materialized edge tables; neighborhood
endpoint; **auto-AI-Generate-on-import** (its hook seam and its `has_review`
graph property are already in G1 — the future card only adds the enqueue at
`_run_index`'s success path and whatever queue semantics the PO scopes).

**LLM cost ≈ $0** (resolution is string normalization; no new embedding, no
extraction). New infra: none. New dependency: `cytoscape` (UI only).
