# research-search — DocGraph: gray (not-imported) cited works inside the Documents view

Status: **design, not code.** Architect, 2026-10-25. Card
`4045bf3b-9510-8188-a1a2-e0b270ff7288` (`research-search-mcp`). Amends
[`research-search-docgraph-document-nodes.md`](research-search-docgraph-document-nodes.md)
(the v0.47.0 design): §2.4's "Nodes: imported documents only. No gray nodes."
is the one sentence this doc revises. Everything else there stands.

Every file/line claim below was read from `repos/aw-app-research-search`
this session (2026-10-25, post-v0.47.0 master).

## 0. The ask, restated against the code

Frederico tested v0.47.0 on `arxiv_2602.12735`: Documents view / Depth 1
shows only the root plus the empty-state message, because none of that
paper's citations are imported. Verbatim (PT): cited documents should appear
**gray** in the graph when not yet imported, flipping **yellow** after the
user imports them — the References-view affordance, but inside the Documents
view, not behind a toggle that must be discovered.

The current behavior is exactly what v0.47.0 specified: `view=documents`
filters to `document_id IS NOT NULL` in every query
(`_DOC_FRONTIER`, `research_search_app/graph_db.py:1096`), so a paper with
an un-imported bibliography renders one lonely node. That spec sentence is
what changes; the complaint is not a bug in the implementation.

## 1. Decision

**`view=documents` additionally returns the root document's un-imported
cited works as gray leaf nodes, appended after the imported neighborhood
and never expanded through. Read-path only: no projection change, no
migration, no new endpoint, no new query param. The UI already handles
gray nodes generically; the Documents view inherits the gray-node side
panel (N4's import surface) for free.**

Three facts make this small:

1. The gray nodes **already exist in Neo4j**: `_MERGE_REFS` projects every
   `ref_targets` row as an `rs__Paper` (`graph_db.py:507-530` region;
   `imported` true only for corpus documents, per `_MERGE_ROOT`,
   `graph_db.py:496-505`). The references view serves them today. This is
   purely a read-time filter change.
2. The DocGraph tap handler already branches on `d.imported`, not on the
   view (`ui/src/views/DocGraph.jsx:138-151`): yellow → re-root, gray →
   side panel. The pulse states, failed-import tooltip and
   `import_state` plumbing (`import_states`, `graph_db.py:1160`) are
   view-agnostic too.
3. `docGraphStyle.js:44-59` styles by `node[?imported]` /
   `import_state`, shared by both views. Gray renders gray with zero
   style work.

### 1.1 Which gray nodes, exactly

**Un-imported `RS_CITES` targets of the ROOT document only**, at every
depth. Directed: `(root)-[:RS_CITES]->(b)` with `b.document_id IS NULL`.

* This is the verbatim ask ("os documentos citados" — by the paper being
  read). The affordance follows the open PDF's bibliography.
* Grays of *other* imported nodes in the neighborhood are deliberately NOT
  included (runner-up, §4.1): at depth 2-3 that multiplies every
  neighbor's ~30-entry bibliography into the view and drowns the corpus
  structure the Documents view exists to show. The workflow for "go
  deeper" is already designed: import a gray, it turns yellow, click it to
  re-root, and its own grays appear.
* Co-citation still shows: `_DOC_EDGES` runs over ALL kept keys, so when a
  root's gray citation is also cited by another visible imported document,
  that second `reference` edge renders too (see §2.1 — no change needed
  for this, the existing directed `RS_CITES` branch covers it).

### 1.2 Depth and expansion semantics

Depth continues to count logical hops over **imported** documents exactly
as shipped (`documents_neighborhood`'s frontier loop,
`graph_db.py:1241-1249`). Gray nodes are attached **after** expansion
completes, as leaves. They are never members of a frontier, so the
gray-intermediated co-citation leak the v2 design guarded against (§2.4
there) cannot re-enter: nothing traverses *through* a gray node.

### 1.3 Cap and ordering

Imported nodes keep absolute priority for the `NODE_CAP` (500,
`graph_db.py:102`) budget: sort and truncate imported rows exactly as
today (`graph_db.py:1254-1257`), then fill the remaining budget with gray
rows ordered by citing-degree desc then key (degree = how many corpus
documents cite the work — the most co-cited un-imported works are the ones
most worth importing, so they survive truncation first).
`stats.truncated` must reflect the combined totals (imported total + gray
total vs kept). Grays never evict a corpus document from the view.

## 2. Where it lands

### 2.1 `research_search_app/graph_db.py`

* **New cypher `_DOC_GRAY`** (beside `_DOC_NODES`, `graph_db.py:1103`):

  ```
  MATCH (root:rs__Paper)-[:RS_CITES]->(b:rs__Paper)
  WHERE root.key = $root_key AND b.document_id IS NULL
  RETURN DISTINCT b.key AS key, b.document_id AS document_id,
         b.title AS title, b.year AS year,
         b.first_author AS first_author, b.doi AS doi,
         coalesce(b.imported, false) AS imported,
         coalesce(b.has_review, false) AS has_review,
         COUNT { (b)<-[:RS_CITES]-(:rs__Paper) } AS degree
  ```

  Same field list as `_DOC_NODES` so `_node_payload`
  (`graph_db.py:1203`) consumes both unchanged. One extra round trip
  (the module's own comment already blesses round-trip count over
  single-trip aesthetics, `graph_db.py:1067-1068`).

* **`documents_neighborhood` (`graph_db.py:1215`)**: after `kept`/`keys`
  are computed, run `_DOC_GRAY` with the root key, drop any key already in
  `keys` (cannot happen by construction — gray means `document_id IS
  NULL` — but cheap and explicit), sort per §1.3, append up to
  `cap - len(kept)`, extend `keys`. `import_states` and `_DOC_EDGES`
  already run over `keys` afterwards — gray nodes get their
  `import_state` join and their `reference` edges with **no further
  query changes** (`_DOC_EDGES`'s first branch is directed
  `(a)-[:RS_CITES]->(b)` over `$keys`, `graph_db.py:1125-1127`, which is
  precisely imported→gray; grays have no `RS_HAS_THEME/AUTHOR/CODE` edges
  — only `sync_document` writes those, on imported docs — so the other
  three union branches return nothing for them).
* **`stats`**: `truncated` = (imported_total + gray_total) > len(kept
  combined). Keep the shape `{nodes, truncated}`; no new stats keys.
* **Payload shape is otherwise unchanged** — nodes already carry
  `imported`/`import_state`/`import_error`. The only consumer of
  `view=documents` is DocGraph.jsx (the N5 MCP tool is NOT shipped —
  `mcp_ingest.py` has no `graph_neighborhood` tool; card
  `4035bf3b-9510-81eb` is Backlog), so growing this view's node set is
  contract-safe. `view=references` stays byte-identical, fixture-pinned.

### 2.2 `ui/src/views/DocGraph.jsx`

Small, mostly copy:

1. **Stale comments**: lines 16-23 ("imported corpus documents only") and
   146-149 ("Only reachable in the references view") describe the old
   semantics — rewrite them; the code beneath them needs no change.
2. **Empty state** (`DocGraph.jsx:328-335`): condition stays
   `nodes.length <= 1`, but it now means "no imported neighbors AND no
   extracted references" — reword to say that and point at the fix (run
   AI Generate / check the review pane's references), instead of the
   current corpus-sharing sentence which is now misleading.
3. **Node-state legend**: the Documents view now mixes yellow and gray
   for the first time; add two node swatches (imported / not imported —
   click to view vs click to import) beside the edge legend in
   `.doc-graph-header`, driven by the same `theme.css` variables
   `docGraphStyle.js` reads. The references view can share it.
4. **Panel**: unchanged. The Import button stays disabled with "Coming in
   N4" (`DocGraph.jsx:367-372`). Live import + gray→yellow push is N4's
   card — see §3.
5. **Fixture**: extend `fixtures/graphNeighborhoodDocuments.sample.json`
   with gray nodes + their reference edges (incl. one gray cited by two
   imported docs, to pin the co-citation edge).

### 2.3 Tests (`tests/test_graph_db.py`, fake-graph harness at :1019)

Extend the existing `documents_neighborhood` fakes with a `_DOC_GRAY`
answer and pin:

* root with zero imported neighbors + un-imported citations → root
  (yellow) + grays, NOT the empty-ish payload (this is Frederico's exact
  repro);
* grays are leaves: depth 2 does not pull in anything reachable only
  through a gray (frontier never contains a gray key);
* cap: imported nodes all kept before any gray; `truncated` true when
  grays are clipped;
* gray co-cited by a non-root imported doc gets both reference edges;
* `view=references` default response byte-identical (existing pin stays).

## 3. Scope boundary — what this does NOT deliver

**A working Import button.** The gray→yellow flip today happens when the
user imports the paper through the Corpus tab (or MCP `index_paper`): the
ingest hook's `sync_document` sets `imported=true` on the same key
(`_MERGE_ROOT` assigns, not coalesces, `graph_db.py:507` comment), and the
next DocGraph refetch shows it yellow. Click-to-actually-import from the
panel — catalogue search, download, dispatch, websocket live-flip — is N4
(`import_jobs` is in place since 0009; nothing writes it yet). Folding N4
into this card would be silent scope growth; instead this card makes N4's
click surface visible in the default view, which is an argument for the PO
to **schedule N4 next** — flag that in the delivery report back to
Frederico.

## 4. Rejected alternatives

1. **Grays of every kept imported node (not just the root).** Runner-up.
   Depth 2-3 would union every neighbor's bibliography (~30 grays each)
   into the canvas, drowning the corpus structure; the re-root workflow
   covers the same need incrementally. One-line change later
   (`$root_key` → `$keys`) if Frederico asks for denser.
2. **Default the UI to the references view instead.** Loses the typed
   author/theme/coding edges he asked for two cards ago; the complaint is
   about the Documents view's blind spot, not a preference for the old
   view.
3. **A `grays=true|false` query param.** No second consumer exists to
   protect (N5 unshipped, references default untouched); a param nobody
   passes is dead contract surface. If an agent consumer later wants a
   corpus-only documents view, add the param then, defaulted to the
   UI's behavior.
4. **Merge the two views into one (drop the toggle).** The references
   view still shows strictly more (every cited work regardless of root,
   author-name grouping) and is the pinned byte-compatible default for
   agent callers; removal is a separate PO call, not this card.
5. **Wire Import to `POST /corpus/index` now as a stopgap.** That route
   needs `source` + `paper_id`; a `ref_targets` row has neither (a DOI at
   best, no catalogue identity) — resolving that mapping IS the N4
   search-then-download design (`research-search-neo4j-graph.md` §3.5).
   A half-stopgap that only works for arXiv-DOI grays would ship an
   affordance that fails unpredictably by node.

## 5. What this makes harder later

* **"Documents view" is no longer "imported-only"** — any future consumer
  (N5 MCP tool, exports) must filter on `imported` if it wants corpus-only
  semantics; the v2 doc's cleaner invariant is gone. Mitigated by the
  per-node `imported` flag being always present.
* **Root-only gray semantics** bakes in an asymmetry (visible imported
  neighbors don't show their own grays). If §4.1's runner-up lands later,
  node counts jump and depth 2-3 may need a gray sub-cap then.
* **Degree-ordered gray truncation** means which grays survive the cap
  shifts as the corpus grows (more citers → different co-citation
  ranking). Fine for a viewer; an agent consumer wanting stable pagination
  would need a different ordering key.

## 6. Risks for the Coder

1. **The combined sort must not disturb the root pin** — append grays
   after the imported slice; do not re-sort the concatenation
   (`graph_db.py:1255-1256`'s key puts root first; a naive combined
   re-sort by degree can push the root behind a hub).
2. **`truncated` lies if computed per-slice** — compute from combined
   totals or a root with 600 citations reports `truncated: false` after
   clipping grays.
3. The references-view default must stay byte-identical — the existing
   fixture pin in `tests/test_graph_db.py` is the guard; don't touch
   `_neighborhood_cypher`.
4. **Verification on the live repro needs data**: gray nodes only exist
   after the paper's references were extracted (AI Generate), resolved
   (`POST /api/graph/resolve`) and synced. Before concluding the feature
   works/fails on `arxiv_2602.12735`, check the References view shows
   gray nodes for it; if it doesn't, run resolve + `POST
   /api/graph/rebuild` first — that's data plumbing, not this change.
5. Edit `repos/aw-app-research-search` (source), never
   `apps/research-search` (installed copy). Rebuild `ui/dist` (committed,
   check-dist gate enforces it). No version hand-bump (release bot owns
   it). Shared tree: check `git status` before and after; if another
   session is active on this repo, use a worktree.
6. After deploy, the change is read-path only — **no rebuild required**
   for the feature itself (grays are already projected), which also means
   it can be verified immediately on the live corpus.
