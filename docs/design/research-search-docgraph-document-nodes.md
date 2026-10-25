# research-search — DocGraph v2: document nodes with typed edges (author/theme/coding), constrained to the PDF column

Status: **design, not code.** Architect, 2026-10-25. Card
`4045bf3b-9510-810e-b28c-da995f6e26c9`
(`research-search-mcp`). Amends, does not supersede,
[`research-search-neo4j-graph.md`](research-search-neo4j-graph.md): the N1
identity layer, the N2 Neo4j projection and serving store, and the N3
component all stay; this changes what the Document Viewer graph *shows* and
*where it sits*, per Frederico's live feedback on v0.45.0 (verbatim, PT, on
the card).

Every file/line claim below was read from
`repos/aw-app-research-search` this session (2026-10-25).

---

## 0. The two asks, restated against the code

1. **Layout.** `DocGraph` is mounted *after* the `.viewer-split` grid
   (`ui/src/views/DocumentViewer.jsx:230-243`), so it spans the full page
   width below both the PDF pane and the review-fields pane. Frederico wants
   it inside the PDF column, directly below the PDF.
2. **Semantics.** Nodes today are `ref_targets` identities, mostly gray
   external cited works, and `nodeLabel()` renders `first_author` first
   (`ui/src/views/DocGraph.jsx:34-37`), which is why the screenshot reads as
   "a graph of authors". Frederico wants nodes to be **other corpus
   documents**, with edges typed **author** (shared author), **theme**
   (shared theme), **coding** (shared code).

## 1. Prior-architecture question, answered (don't re-litigate)

The card asked whether the earlier corpus-graph backend (`graph_edges`
table, `kind IN ('reference','shared_theme')`, `GET /corpus/graph`, Graph
tab) is live and should be re-pointed at. **It is not live and never was.**
That design (`research-search-corpus-graph.md`, cards A-D) was superseded
same-day by `research-search-graph-layer.md` (G1-G4), which was itself
superseded on 2026-10-24 by `research-search-neo4j-graph.md` (N1-N5) when
Frederico overrode the "no Neo4j" call. Both supersessions happened at
design stage: the repo's migrations are `0009_graph_identity.sql` (N1) and
`0010_theme_converge.sql` (TC1); **no `graph_edges` table, no
`/corpus/graph` route, no Graph tab exists in code.** There is nothing to
re-point at. The live stack is the one to extend:

| Piece | Where | What it holds |
|---|---|---|
| Identity (system of record) | Postgres: `ref_targets`, `themes`, `review_coding.theme_id` (`migrations/0009_graph_identity.sql`) | cited-work identity, merge-safe theme identity |
| Codes + themes per paper | `review_coding` (`migrations/0007_create_review_coding.sql`), `kind IN ('code','theme')` | lit-review DAG output; codes are per-paper **by design**, only themes have cross-paper identity |
| Theme convergence | `theme_merges`, `converge_jobs` (0010); `_canonical_theme` walk (`research_search_app/graph_db.py:296-338`) | canonical theme mapping, already followed by every sync |
| Projection + serving | Neo4j: `rs__Paper`, `rs__Theme`, `RS_CITES`, `RS_HAS_THEME` (`graph_db.py:85-98`, `sync_document` at `graph_db.py:448`) | rebuildable from Postgres, `POST /api/graph/rebuild` |
| Read | `GET /api/graph/neighborhood` (`api/graph.py`, cypher at `graph_db.py:748-826`) + N5 MCP tool `graph_neighborhood` (`mcp_ingest.py`) | §3.5 payload N3 renders |

## 2. Decision

**Extend the existing Neo4j projection with two new node types and add a
`view=documents` mode to `GET /api/graph/neighborhood`. No migration. No new
endpoint. The UI moves inside the PDF pane and defaults to the documents
view, keeping the current references view behind a toggle.**

### 2.1 "coding" resolved

"coding" = the `kind='code'` rows of `review_coding`, the qualitative codes
the literature-review DAG extracts alongside themes (0007's CHECK
constraint is the whole vocabulary: `code` | `theme`). There is exactly one
coding concept in this app, so no clarification round-trip with Frederico is
needed. A **coding edge** = two documents sharing a code by normalized
label (`graph_identity.normalize_label`, `graph_identity.py:156`).

Codes deliberately have no Postgres identity table (0009's comment: "codes
are per-paper by design; only themes have cross-paper identity"). This
design does NOT change that: codes get a **projection-only** identity, a
`rs__Code` node keyed on `norm_label`, derivable and rebuildable, no merge
support, no new table. If code convergence is ever wanted, that is the
themes playbook re-run, and a PO call.

### 2.2 "author" resolved, with its honest limitation

Nothing in this app stores a document's author list. `documents` (0001) has
no author column; `index_paper` accepts only optional `title`/`doi`
(`mcp_ingest.py:63,316`); `paper.authors` exists only on transient search
results (`render.py:72`). The only author fact in the system is
`ref_targets.first_author`, harvested from citing bibliographies and
already projected onto `rs__Paper` nodes (`_MERGE_ROOT`,
`graph_db.py:420-430`).

**v1 author edges therefore match on normalized FIRST author**
(`graph_identity.normalize_first_author`, `graph_identity.py:179`, the same
function the identity key already uses, so "Vaswani, A." and "A. Vaswani"
agree). Consequences to state plainly in the handoff and to Frederico:

* two documents sharing a *non-first* co-author get no edge;
* a document that no corpus bibliography cites has no `ref_targets` row,
  hence no `first_author`, hence no author edges.

Fixing that for real means ingesting author lists (new column + `index_paper`
param + backfill via catalogue/LLM), a separately scoped feature. That is a
**PO decision, not silently absorbed here** (runner-up recorded in §5).

### 2.3 Projection changes (`research_search_app/graph_db.py`)

New labels/types, all `rs`-prefixed (the C5 guard test
`tests/test_graph_prefix_guard.py` enforces the prefix):

* `rs__Author {norm_name}` unique, `display_name` coalesced first-write-wins.
  `(p:rs__Paper)-[:RS_HAS_AUTHOR {document_id}]->(a:rs__Author)`, written in
  `sync_document` only when the synced document's own target row has a
  non-empty `first_author` (same `own_target` lookup that feeds
  `_MERGE_ROOT`).
* `rs__Code {norm_label}` unique, `label` coalesced.
  `(p:rs__Paper)-[:RS_HAS_CODE {document_id, source}]->(c:rs__Code)` from
  `review_coding` rows `kind='code'`, mirroring `_theme_rows`/`_MERGE_THEMES`
  exactly (`graph_db.py:383-404, 445-460`), including the prune
  (`_PRUNE_HAS_THEME` sibling) and orphan drop (`_DROP_ORPHAN_THEMES`
  sibling).
* `SCHEMA_STATEMENTS` (`graph_db.py:236-243`): uniqueness constraints on
  `rs__Author.norm_name` and `rs__Code.norm_label` (concurrent-MERGE
  correctness, same reason as `rs_paper_key`).
* `_WIPE` (`graph_db.py:558`) must grow both labels, and `rebuild()` must
  drop orphan authors/codes too. **A label forgotten in `_WIPE` survives
  every rebuild as a zombie.**

### 2.4 Read: `view=documents` on the same endpoint

`GET /api/graph/neighborhood?root=<id>&depth=1..3&view=references|documents`,
default `references` = byte-identical to today (N5's MCP tool and any agent
caller stay unbroken; optionally expose `view` on the MCP tool too, additive).

`view=documents` semantics:

* **Nodes:** imported documents only (`document_id IS NOT NULL`). Root = the
  open document. No gray nodes.
* **Logical edges (4 kinds):** `reference` (an `RS_CITES` between two
  imported papers, kept deliberately, see §5), `theme` (2-hop via
  `rs__Theme`, exists today at `graph_db.py:800-807`), `author` (2-hop via
  `rs__Author`), `coding` (2-hop via `rs__Code`).
* **Depth** counts *logical* hops over that union. Do NOT translate this to
  one variable-length Cypher pattern over physical hops
  (`*1..{2*depth}` through theme/author/code nodes): a 2-physical-hop
  `RS_CITES` chain through a **gray** paper would then surface at depth 1,
  silently reintroducing co-citation-through-gray links nobody asked for.
  Instead: **iterative frontier expansion in Python**, one Cypher round per
  depth level (frontier keys as a parameter, return the 4-way union of
  imported neighbors), then one final edges-among-kept query. At most 4
  round trips, corpus is ~40 documents; exactness beats the single-trip
  aesthetic here. Keep the cap (`NODE_CAP`, degree-ordered, root pinned
  first) and the truncation stat.
* **Payload:** same node shape as §3.5 (all nodes will have
  `imported: true`; keep `import_state` present for UI uniformity). Edges:
  `{source, target, kind}` plus detail arrays, `themes` (exists), `authors`
  (`[{norm_name, display_name}]`), `codes` (`[{norm_label, label}]`).
  Undirected 2-hop kinds dedupe with the existing `a.key < b.key` trick;
  the null-placeholder union row (load-bearing, `graph_db.py:809-820`)
  is needed in the new edges query too, or a root with no edges vanishes.

### 2.5 `health()` symmetry (the trap that goes permanently yellow)

`_COUNTS` (`graph_db.py:607-612`) and `postgres_counts`
(`graph_db.py:630-680`) MUST be extended in lockstep: `authors`,
`has_author`, `codes`, `has_code`. Postgres expectations: authors = distinct
`normalize_first_author(first_author)` over `ref_targets` rows with
`document_id IS NOT NULL` and non-empty first_author; codes = distinct
`normalize_label(label)` over `review_coding WHERE kind='code'`; edge counts
= distinct (document, normalized key) pairs. Normalize in Python, same as
`postgres_counts` already does for themes (`DbTables.execute` only fetches
SELECT-first statements, `store.py:93-99`, so no SQL function games). Miss
this and `GET /api/graph/health` reports drift forever and `doctor` goes
yellow on a healthy graph; the module's own docstrings document this exact
failure class.

### 2.6 UI (`ui/src/views/DocumentViewer.jsx`, `DocGraph.jsx`, `docGraphStyle.js`, `api.js`, `theme.css`)

1. **Move the mount**: `<DocGraph key={documentId} documentId={documentId}/>`
   from after `.viewer-split` (`DocumentViewer.jsx:243`) to inside
   `.viewer-pane-pdf`, after the iframe (`DocumentViewer.jsx:231-233`). The
   grid column (`minmax(0, 1fr)`, `theme.css:295-300`) then constrains the
   width, and the graph sits directly below the PDF. No new wrapper CSS
   beyond, at most, a smaller `margin-top` on `.doc-graph`.
2. **Resize correctness**: the canvas now lives in a column whose width
   changes with the viewport and which, below the 331px-rule breakpoint, is
   `display: none` when `data-active-pane='review'`
   (`theme.css:331-338`). cytoscape sized against a hidden/0-width container
   renders blank. Add a `ResizeObserver` on the container calling
   `cy.resize()` and re-`fit()` when the size transitions from 0. (On
   narrow screens the graph hides with the PDF pane; that matches "logo
   abaixo dele" and is accepted.)
3. **Default to documents view**: `api.js graphNeighborhood(documentId,
   depth, view)` (`api.js:252-255`); `DocGraph` fetches `view=documents` by
   default and gains a small `Documents | References` toggle next to the
   depth toggle (reuse the `.graph-depth-toggle` button idiom). The
   references view keeps today's behavior INCLUDING the gray-node side panel,
   which is N4's (lazy import) designed click surface; removing it would
   orphan N4. In documents view, node tap = navigate (`#/doc/<id>`), no
   panel.
4. **Edge styling + legend** (`docGraphStyle.js:63-79`): `reference` stays
   solid+arrow, `theme` stays dashed; add `author` and `coding` with
   distinct line style/color from `theme.css` variables (works in both
   themes). Four kinds are not distinguishable without a legend: add a
   compact inline legend in `.doc-graph-header`. Tooltips: theme one exists
   (`DocGraph.jsx:86-90`); add the same for `coding` (code labels) and
   `author` (shared author name).
5. **Labels**: in documents view nodes are corpus documents, so prefer
   `title` over `first_author` in `nodeLabel()` (the current
   first-author-first order is exactly what read as "authors" in the
   screenshot). Header text: "Document graph".
6. **Fixture**: add a `view=documents` sample beside
   `fixtures/graphNeighborhood.sample.json` and build/test against it.

## 3. What stays untouched

N1 identity layer and hooks; theme-convergence (TC1/TC2), whose canonical
mapping already feeds `_theme_rows` and therefore theme edges for free;
`/corpus/*`; the N5 MCP tool's default output; `review_coding` schema;
`import_jobs`/N4 design (preserved via the references-view toggle). **No
Postgres migration in this change.**

## 4. Deployment note

The new projections exist only after a sync. After install/update, run
`POST /api/graph/rebuild` once to materialize `rs__Author`/`rs__Code` for
the existing corpus; until then the documents view shows theme/reference
edges only. Say this in the delivery report. (Version bump belongs to the
release bot, never by hand.)

## 5. Rejected alternatives

1. **Re-point N3 at the old `graph_edges`/`GET /corpus/graph` backend.**
   It does not exist in code (§1); both prior designs died before
   implementation. Building it now would stand up a second, Postgres-side
   serving path in direct conflict with Frederico's standing 2026-10-24
   override that Neo4j IS the graph store.
2. **Compute doc-to-doc edges in SQL at read time, skip Neo4j.** Same
   conflict with the override, and it would fork the serving layer: one UI,
   two stores, two degradation behaviors.
3. **Full co-author edges now.** No author data exists to build them from
   (§2.2). Scoping an authors-ingestion feature inside a layout-fix card
   would be silent scope growth; it goes to the PO as a named follow-up.
   Runner-up if the PO wants it: `documents.authors jsonb` + optional
   `index_paper` param + LLM backfill from `md_path` front matter
   (title_backfill precedent shows front matter is hostile; expect an LLM,
   not a regex).
4. **A separate `GET /api/graph/doc-neighborhood` endpoint.** The param
   shares root/depth/cap/503-degrade semantics and the N5 wrapper for free;
   a second route duplicates all of it.
5. **Variable-length Cypher over physical hops for the documents view.**
   Leaks gray-intermediated co-citation into depth 1 (§2.4); exactness wins
   at this corpus scale.
6. **Replace the references view outright (no toggle).** Orphans N4's
   gray-node import affordance, which is designed and carded. The toggle
   costs two buttons.
7. **Postgres identity table for codes (themes playbook).** Contradicts
   N1's deliberate "codes are per-paper" decision for zero current benefit;
   the projection-only `norm_label` identity delivers the edges and is
   rebuild-safe.

## 6. What this makes harder later

* **Namespace surface grows**: every future rename/wipe/health change now
  has 4 node labels and 4 relationship types to keep in sync; `_WIPE`,
  `rebuild()` orphan drops, `_COUNTS`, `postgres_counts` and the guard test
  are the five places a new label must land together.
* **`norm_label`-keyed code identity**: renaming a code's spelling changes
  its node identity (edges re-key on next sync/rebuild). Fine while codes
  are projection-only; if codes ever get convergence/merging, they need the
  full minted-id treatment and this projection changes shape.
* **first_author-only author edges** bake in semantics that will broaden if
  author lists are ever ingested: edges will appear that didn't exist, and
  any user mental model of "author edge = first author" breaks. Additive,
  but worth a release note when it happens.
* **Depth-iterative read** departs from the one-round-trip cypher style; at
  a much larger corpus the N round trips and the per-frontier `IN` lists
  would need revisiting (batching or a server-side alternative).

## 7. Risks for the Coder (non-obvious breakage)

1. `health()` drift symmetry (§2.5) — the test to write first.
2. The null-placeholder union row (`graph_db.py:809-820`) — reproduce it in
   the new edges query or freshly imported papers lose their root.
3. `DbTables.execute` fetches results only for SELECT-first statements
   (`store.py:93-99`); all normalization walks stay in Python.
4. Cypher cannot parameterize labels or var-length bounds; the only string
   interpolation allowed is the clamped int depth (existing pattern,
   pinned by `tests/test_graph_db.py`).
5. cytoscape zero-size container after the move (§2.6.2); test the narrow
   breakpoint with the review pane active.
6. `a.key < b.key` dedupe on every undirected 2-hop kind, or each edge
   arrives twice.
7. Edit `repos/aw-app-research-search` (the source), never
   `apps/research-search` (installed copy). No version hand-bump.
8. The N5 MCP tool and any agent flows consume today's default payload:
   `view` must default to `references` and the default response must stay
   byte-compatible (fixture-pin it).
