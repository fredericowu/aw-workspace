# research-search — Neo4j graph layer: theme-linked papers, Document Viewer graph, agentic lazy import

Status: **design, not code.** Architect, 2026-10-24. Card
`4025bf3b-9510-81c9-8daa-ec144a45a9b4`
(`architecture:research-search-graphrag-neo4j-migration-and-graph-viz`).

**Supersedes** [`research-search-graph-layer.md`](research-search-graph-layer.md)
(commit `79294f1`) and its G1–G4 coder cards. Frederico overrode that
design's "no Neo4j" decision on the card (2026-10-24T09:34, Telegram, PT,
verbatim):

> "Eu quero que papéis se liguem a papéis baseados em temas. Depois a gente
> vai ter uma task que observa os temas e tenta encontrar coisas comuns pra
> convergir temas e ligações. Eu quero o neo4j, uma instância que será
> tier2 da workspace. Podemos criar uma app separada chamada neo4j, daí
> outras apps podem reutilizala. E a gente põe o research app como
> dependente dela. Pode começar pela app do neo4j e especificar as demais
> coisas enquanto isso. Só faz em paralelo o que não gerar retrabalho."

This design also folds in the two 2026-10-23T21:21 card comments the
superseded design missed: the graph renders **below the PDF inside Document
Viewer, rooted on the open document** (no standalone Graph tab), and lazy
import runs as an **agentic flow calling the MCP tool chain, with results
pushed to the UI over a websocket** (gray→yellow live), not polling.

Every claim below is grounded in a file read this session (2026-10-24) or
carried over from the 2026-10-23 live verification (40 documents, 2,895
chunks, 570 figures).

---

## 0. What survives from the superseded design, and what changes

| Piece | 79294f1 (superseded) | This design |
|---|---|---|
| Graph backing store | Postgres projection (`ref_targets` + read-time joins) | **Neo4j**, via the new `aw-app-neo4j` Tier-2 dependency app (card `4035bf3b-9510-811b-951e-c074bd6fd2b1`, designed in parallel) |
| Chunk/figure vector search | pgvector (`store.py`) | **Unchanged — pgvector stays.** The override asks for the graph store and theme links; it does not ask to move embeddings, and moving them re-opens the exact filterless-vector-index problem the 2026-10-12 reversal measured. Flagged in §6 for veto. |
| Reference identity (`ref_targets`), bidirectional DOI/title resolution, hook sites | Designed | **Survives verbatim** — identity minting stays in Postgres (§3.1–3.2) |
| Theme edges | Read-time label self-join, derived only | **First-class**: minted `Theme` identity + `(:Paper)-[:HAS_THEME]->(:Theme)`; paper↔paper theme linking is a real 2-hop traversal (§3.3) |
| Graph UI | Top-level cytoscape Graph tab | **Below the PDF in `DocumentViewer.jsx`, rooted on the open document** (§3.6) |
| Lazy import | Synchronous panel + `/corpus/index` status poll | **Agentic flow calls the MCP tool chain; websocket pushes gray→in-flight→yellow** (§3.7–3.8) |
| Theme convergence | Not contemplated | **Named, not built** — schema is merge-safe so it lands later without rework (§4) |
| G1–G4 coder cards | Backlog | Superseded/archived; re-decomposed as N1–N4 (§8) |

---

## 1. The approach, in sentences someone can disagree with

1. **Neo4j is the graph's serving store; Postgres stays the system of
   record.** Every graph read (Document Viewer subgraph, neighborhood
   expansion) hits Neo4j. But reference rows, theme rows, minted
   identities, and the future merge map live in Postgres, and the entire
   Neo4j graph is rebuildable from them with one idempotent sync
   (`POST /api/graph/rebuild`). This is deliberate: Neo4j Community's
   backup is a stop-the-container dump (and aw-app-neo4j defers backups
   with a named trigger), and aw-knowledgeable's 2026-10-21 outage (app crash-looped
   at boot because Neo4j was unhealthy) is the failure mode we refuse to
   inherit — if `aw-app-neo4j` is down, research-search boots, serves
   search, and degrades the graph loudly.
2. **pgvector is not migrated.** Chunks, figures, embeddings, and corpus
   search stay in `store.py` untouched. Neo4j owns papers, themes, and
   edges — the hybrid the original card named as an explicit option.
3. **Themes get minted, durable identity** (`theme_id`), and papers link
   to papers *through* theme nodes, not via string equality at read time.
   That makes theme linking first-class and makes the future convergence
   job a data operation (merge map + edge repoint), not a schema change.
4. **The graph UI lives inside Document Viewer, below the PDF,** rooted
   on the open document and expanding outward (references + theme links).
   There is no top-level Graph tab in this design.
5. **Lazy import is an agentic flow**: clicking a gray node dispatches a
   contributed importer agent that calls the MCP tool chain
   (`search_papers` → `download_with_fallback` → `index_paper`) through
   the gateway; the app correlates the run to the node via an
   `import_jobs` row and pushes state transitions to the UI over a new
   websocket. No client poll loop.
6. **Only rework-free work runs in parallel** (Frederico's own
   constraint): the Postgres identity layer (N1) and the `aw-app-neo4j`
   app share no moving parts; the Neo4j adapter (N2) waits for
   aw-app-neo4j to be published (its contract is already frozen, §2); the
   UI (N3) builds against the API payload spec, which is fixed here
   independently of Neo4j specifics.

---

## 2. The `aw-app-neo4j` contract this builds against (frozen 2026-10-24)

The dependency app was designed in parallel on card
`4035bf3b-9510-811b-951e-c074bd6fd2b1` and its connection contract is now
**frozen** — [`docs/design/aw-app-neo4j.md`](aw-app-neo4j.md) §2 (commit
`4d47e43`), written first precisely so this card could build against it.
This design re-derives nothing; the five rules, as they bind
research-search (consumer #1):

- **C1 — URI**: `bolt://aw-app-neo4j:7687`, resolvable from the workspace
  process (research-search is Tier-1 in-process, so from here) over the
  shared podman network. Not published to the host.
- **C2 — credential**: database `neo4j`, user `neo4j`; research-search
  adds a `neo4j_password` field to its own `config_schema`
  (`x-secret: true`, no default) — the consumer carries the credential in
  its own config, per contract rule 2.
- **C3 — dependency declaration**: `dependencies.apps` +=
  `{"id": "neo4j", "required": true, "reason": ...}` — the reconciler
  auto-installs the dep first and protects it from uninstall
  (`src/apps/reconciler.py` `_install_dependencies` /
  `_loaded_dependency_closure`). `required: true` is the contract's rule 3
  verbatim; it means **N2 cannot merge before `aw-app-neo4j` is published
  to the marketplace** (an unresolvable required dep fails the consumer's
  own install, `reconciler.py:892`) — which the §8 sequencing enforces
  anyway.
- **C4 — degrade, never crash** (contract rule 4, the aw-knowledgeable
  10h-outage lesson inverted): install-time dependency is hard, runtime
  dependency is soft. Empty password or failed bolt connection → the app
  boots normally, graph features disable, degradation surfaces in
  `GET /status` / doctor. Bolt readiness lags container-running by
  10–30 s (JVM boot) — the adapter retries with backoff.
- **C5 — namespacing** (Community = one shared database): labels
  prefixed with the consumer slug — **`rs__Paper`, `rs__Theme`** —
  relationship types **`RS_CITES`, `RS_HAS_THEME`**, every
  constraint/index named `rs_...`. Convention, not enforcement: this app
  ships a static guard test asserting all its Cypher stays inside the
  prefix (aw-knowledgeable's K5 pattern, named in the contract itself).

Only `graph_db.py` (§3.4) touches C1/C2; only the manifest touches C3;
nothing else in this design knows Neo4j's address or credential.

---

## 3. Where it lands — files and seams

### 3.1 Postgres migration `0008_graph_identity.sql` (identity, not graph)

Same header rules as `migrations/0007_create_review_coding.sql` (no vector
types → nothing `public.`-qualified; hyphenated names double-quoted).

- **`app__research-search__ref_targets`** — survives verbatim from the
  superseded design (one row per cited work; `key` = normalized DOI else
  `sha256(norm_title|year|norm_first_author)[:40]`; `norm_version`;
  `document_id REFERENCES documents(id) ON DELETE SET NULL` — NULL = gray;
  `resolved_at`). `review_references` gains `target_key text` (indexed).
- **`app__research-search__themes`** — the merge-safe theme identity (§4):
  - `theme_id uuid PRIMARY KEY` — minted once, never reused, never derived
    from the label.
  - `norm_label text NOT NULL` (lowercase, whitespace-collapsed,
    punctuation-stripped), `label text` (first-seen display form),
    `created_at timestamptz`.
  - `merged_into uuid NULL REFERENCES themes(theme_id)` — **written only
    by the future convergence job**; NULL for every row v1 writes.
  - Partial unique index on `norm_label WHERE merged_into IS NULL` — one
    *active* theme per normalized label; merged rows keep their label
    without blocking the canonical one.
- **`review_coding` gains `theme_id uuid NULL`** (indexed) — stamped on
  rows with `kind='theme'` by the resolution pass: look up active
  `norm_label`, insert a new `themes` row if absent. Membership is by id
  from day one, so convergence never rewrites membership rows.
- **`app__research-search__import_jobs`** — the lazy-import correlation
  row (§3.7): `id uuid PK`, `ref_key text REFERENCES ref_targets(key)`,
  `root_document_id text`, `run_id text NULL` (the dispatched agent run),
  `state text` (`dispatched|running|indexed|failed`), `error text`,
  `created_at`, `updated_at`. All state in Postgres — WORKERS>1 rule, same
  precedent as `review_generate_jobs` (`migrations/0006`).

### 3.2 Resolution module — `research_search_app/graph_identity.py`

Pure functions + a store over `ctx.db` (the `store.py`/`review.py` house
shape). Carried over from the superseded design with one addition:

- `resolve_references(db, document_id)` — upsert `ref_targets`, stamp
  `review_references.target_key`; match to imported documents by
  normalized-DOI then normalized-title equality.
- `resolve_new_document(db, document_id)` — inverse pass after an import;
  flips gray → yellow (sets `ref_targets.document_id`).
- **`resolve_themes(db, document_id)`** (new) — for each `review_coding`
  row with `kind='theme'` and `theme_id IS NULL`: normalize label, find
  active theme (following `merged_into` if a merged row matches), else
  insert; stamp `theme_id`. Idempotent per row.
- **Hook sites** (all already in the request/worker path):
  - `review_generate.py:918` — right after `replace_ai_references(...)`
    persists the references sink: `resolve_references` + `resolve_themes`
    (the coding sink persists via `replace_ai_coding`, `review.py:330`, in
    the same completion path).
  - `api/corpus.py` `_run_index` success path (after `ingest.index_paper`
    returns ok, `api/corpus.py:146-151`): `resolve_new_document`. **Still
    the future auto-AI-Generate enqueue seam** (flagged, not built).
  - Reference/coding CRUD in `api/review.py` — human edits re-resolve
    that document.
- `POST /api/graph/resolve` — idempotent backfill over all 40 existing
  documents (also the drift-recovery tool).

### 3.3 Neo4j schema (namespaced per contract rule C5)

```
(:rs__Paper {key, document_id?, title, year, first_author, doi,
             imported: bool, has_review: bool})
(:rs__Theme {theme_id, label, norm_label})

(:rs__Paper)-[:RS_CITES {source_document_id, ref_row_id}]->(:rs__Paper)
(:rs__Paper)-[:RS_HAS_THEME {document_id, source}]->(:rs__Theme)
```

Constraints/indexes all named `rs_...`: uniqueness on `rs__Paper.key` and
`rs__Theme.theme_id`, index on `rs__Paper.document_id`.

- **One work = one `rs__Paper` node**, keyed by `ref_targets.key`; an
  imported document is the same node with `document_id` set and
  `imported: true` (gray/yellow is a property, not two node types).
- **Theme-based paper↔paper linking is the 2-hop pattern**
  `(a:rs__Paper)-[:RS_HAS_THEME]->(t:rs__Theme)<-[:RS_HAS_THEME]-(b:rs__Paper)` —
  first-class in the data model, computed by traversal, and it converges
  automatically when the future job merges `rs__Theme` nodes. No
  materialized theme-link edge in v1 (it would be a second copy of the
  same fact that convergence would then have to rewrite); if viewer-query
  timings ever demand one, it is a cache derived from `RS_HAS_THEME`.
- `imported` / `has_review` are denormalized at sync time from
  `documents` / `review_generate_jobs.state='done'`.

### 3.4 Neo4j adapter + sync — `research_search_app/graph_db.py`

- Thin adapter owning the driver (`pip_requires` += `neo4j`), lazy
  connect with backoff (C4's 10–30 s JVM-boot lag), short timeouts, the
  one place reading C1's URI and C2's `neo4j_password` config field.
  **The only module in this app allowed to import the Neo4j driver**, and
  the ship point for C5's static guard test (all Cypher inside the `rs`
  prefix) — aw-knowledgeable's K5 one-module discipline, adopted.
- `sync_document(db, graph, document_id)` — idempotent `MERGE`s for the
  document's paper node, its references' paper nodes, `CITES` edges,
  theme nodes (by `theme_id`), `HAS_THEME` edges. Called from the §3.2
  hook sites after resolution; failures are logged + counted, never
  raised into the ingest path (import must succeed even when the graph is
  down; `rebuild` repairs).
- `rebuild(db, graph)` — `POST /api/graph/rebuild`: wipe the `RS_`
  namespace, replay all documents. The disaster-recovery and
  contract-migration story in one endpoint.
- `GET /api/graph/health` — bolt reachability + node/edge counts vs
  Postgres expectations; surfaced in `GET /status` so `doctor` sees it.

### 3.5 Graph read API — `research_search_app/api/graph.py`

`GET /api/graph/neighborhood?root=<document_id>&depth=1..3` (default 1):

```
{
  "root": "<document_id>",
  "nodes": [{"id": "<key>", "document_id": "...|null", "title", "year",
             "first_author", "doi", "imported": bool, "has_review": bool,
             "import_state": "none|dispatched|running|failed"}],
  "edges": [{"source", "target", "kind": "reference"},
            {"source", "target", "kind": "theme",
             "themes": [{"theme_id", "label"}]}],
  "stats": {"nodes": n, "truncated": bool}
}
```

- One Cypher round trip: variable-length expansion over `CITES` (both
  directions) plus the theme 2-hop, from the root paper node, bounded by
  `depth` and a **500-node cap** prioritized by citation count —
  `truncated: true` when hit, never silent.
- `import_state` joined in from `import_jobs` (Postgres) at response
  assembly — the API is the single source the UI needs on first paint;
  the websocket (§3.8) only carries deltas.
- Theme edges in the payload are paper↔paper (what the viewer draws),
  with the shared themes listed — the UI does not draw `RS_Theme` nodes
  in v1.

### 3.6 Document Viewer graph — `ui/src/views/DocGraph.jsx`

- Mounted in `DocumentViewer.jsx` **below the existing `viewer-split`
  div** (`ui/src/views/DocumentViewer.jsx:229-236`) — full width, below
  the PDF+Review panes, per Frederico's placement instruction. No change
  to `App.jsx` tabs.
- **Rendering: cytoscape** (plain npm dep, house idiom per
  aw-knowledgeable's `GraphCanvas.tsx`), `breadthfirst`/concentric layout
  **rooted on the open document**; re-rooting = hash navigation to
  `#/doc/<id>` (the component keys off `documentId` and refetches).
- Node states (the card's point (c) — more than two): **yellow**
  imported; **gray** known-not-imported; **gray + pulse/spinner badge**
  while `import_state` ∈ `dispatched|running`; **red-edged gray** on
  `failed` (tooltip carries `error`, click reopens the import panel with
  the failure shown). Styling via `theme.css` variables only (no invented
  Tailwind — this app has none).
- Click yellow → `window.location.hash = '#/doc/<id>'` (existing route,
  graph re-roots). Click gray → in-view side panel: title/author/year/DOI
  /raw citation + the **Import** action (§3.7). A depth control (1–3)
  drives the neighborhood refetch.

### 3.7 Agentic lazy import — the DAG + MCP tool chain

Frederico's mechanism comment (2026-10-23T21:21): the import must be an
agentic flow that calls the MCP tool with the reference, with the result
returned via websocket.

**Hard constraint discovered, not assumed**: AP-MT's structured-completion
DAG nodes (the literature-review mechanism, `aw-app.json`
`contributes.agents.workflows`) have **no network path to this workspace's
MCP gateway** (live finding, 2026-10: AP-MT-hosted MCP `transport=http`
cannot reach the gateway). A pure AP-MT DAG therefore cannot call
`search_papers`/`index_paper`. Runner-backed agents (agents-platform
runner containers) reach the gateway every day — so the agentic flow runs
there:

1. Gray-node panel **Import** → `POST /api/graph/import {ref_key,
   root_document_id}` → app inserts an `import_jobs` row
   (`state='dispatched'`) and dispatches a run of a new contributed agent
   **`research-reference-importer`** with the reference's structured
   fields as input. Dispatch goes over the AP-MT REST surface using the
   credential machinery this app already owns
   (`research_search_app/apmt_credential.py` — self-minted ApiKey from the
   inherited identity JWT; same path AI Generate uses for `/v1`). The
   returned `run_id` is stamped on the job row.
2. The importer agent — tools scoped to exactly `search_papers`,
   `download_with_fallback`, `index_paper`, `get_indexed_paper` —
   resolves the reference against the catalogues (DOI first, title
   fallback, with judgement on ambiguous hits: this disambiguation is
   *why* the flow is agentic), downloads, and calls `index_paper`.
3. `index_paper` **lands back inside this app in-process**
   (`routes.py:157` `POST /mcp` → `mcp_ingest`), so the app observes the
   import natively: the ingest success path runs `resolve_new_document`
   (§3.2), which flips `ref_targets.document_id`, marks the `import_jobs`
   row `indexed`, and triggers the Neo4j `sync_document`. The agent's
   report is secondary; the import's truth is the app's own tables.
4. Failure paths: ingest failure → `ingest_failures` + job `failed`
   (existing machinery, `api/corpus.py:146-151` records every exit); dead
   or stuck run → a lightweight watchdog marks jobs `failed` after a
   timeout (runner runs are killed by the platform's own 600s idle
   watchdog), with the error surfaced on the node.

**Deviation flag, stated for veto rather than smuggled**: Frederico said
"DAG". v1 dispatches **one tool-using agent run** (search → disambiguate →
download → index executed agentically in one session) rather than a
multi-node AP-MT DAG, because AP-MT DAG nodes cannot reach the gateway
(above). The flow is still "agentic flow → MCP tool chain → websocket
result", which is the mechanism he specified; if AP-MT grows
runner-backed DAG nodes, the importer decomposes into one without touching
the app-side contract (`import_jobs` + websocket stay identical).

### 3.8 Websocket — new, app-owned, answering the card's question (a)

- **There is no existing live-update socket in this app** — audio and
  AI-Generate status are deliberately polled (`DocumentViewer.jsx:7-13`
  documents the sidecar/heartbeat rationale). This design adds the app's
  first websocket; it does **not** retrofit audio/review status onto it
  (out of scope, separable later).
- `@app.websocket("/ws/graph/{document_id}")` on the app's sub-app
  (`routes.py`). Auth is already solved by core: IdentityGuard handles the
  websocket scope for mounted apps — `?token=` query param or the apex
  cookie, close `4401` on failure (`src/apps/runtime.py:192-222, 288`).
  The browser connects with its session cookie; nothing new to build.
- **Cross-worker delivery (question (b), correlation)**: the workspace
  runs WORKERS>1, `ctx` exposes no pub/sub (verified: `db`, `services`,
  `secrets`, `config`, `package_dir`), and the worker completing the
  import (whichever handled the `POST /mcp` ingest) is generally not the
  worker holding the socket. **Postgres is the bus**: the socket handler
  watches `import_jobs` rows for its `document_id`'s neighborhood
  server-side (~1s loop over one indexed query) and pushes deltas
  `{ref_key, state, document_id?, error?}` to the client. The *client*
  never polls — which is the requirement as stated; the server-side watch
  is the WORKERS-safe implementation, swappable for LISTEN/NOTIFY or a
  ctx-level bus if core ever grows one (named in §6).
- UI: `DocGraph.jsx` opens the socket on mount, applies deltas to
  cytoscape in place (gray → pulse → yellow without refetch; a `failed`
  delta restyles the node). Socket drop → one refetch of
  `/neighborhood` + reconnect with backoff.

### 3.9 MCP tool `graph_neighborhood` — agent-flow access via the gateway (N5)

Frederico's 2026-10-24 instruction (card `4035bf3b-9510-8188-ac64-ef59cb66efb5`,
PT, verbatim): "precisamos usar o ap-mt pq ele terá o fluxo de agentes, pra
resolver o problema de rede, a app vai externar uma mcp tool que vai fazer a
busca no grafo, e o agente vai poder alcançar ela via mcp-gateway". This
generalizes §3.7's resolution of the network constraint from import to
**search**: AP-MT agent-flow agents run runner-backed and reach the gateway;
a literal AP-MT node does not — so the graph's agent surface is an MCP tool
on this app, behind the gateway, never a direct connection.

- **Server: `research-search-index`** (`mcp_ingest.py`), the app's in-process
  Streamable-HTTP server. Forced, not preferred: it is the only one of the
  three servers with `ctx.db` + `ctx.config` in hand — `handle_request`
  already receives both (`routes.py:182-188`), which is everything
  `graph_db.connect` + `graph_db.neighborhood` need. The stdio
  `research-search` server is a gateway-spawned child with neither; the
  `paper-search` server is upstream's separate venv (`mcp_config.py:9-46`
  records this same reasoning for `index_paper`).
- **One tool, a thin wrapper over N2's one query function** — no second
  implementation: one `_TOOLS_SCHEMA` entry + one dispatch branch calling
  `graph_db.neighborhood(db, graph, root, depth, cap=...)` on a thread
  (sync driver, async dispatcher — the `_generate_review` pattern),
  impatient connect like the REST route (`api/graph.py:160-163`).
- **Name**: `graph_neighborhood` → gateway-prefixed
  `aw__research_search_index__graph_neighborhood`. Deliberately NOT
  `search_graph`: aw-knowledgeable already serves
  `aw__aw_knowledgeable__search_graph` and agents see both upstreams —
  near-twin names across servers is a recorded confusion source.
- **Input**: `root` (required, a research-search `document_id` — same root
  the REST route takes), `depth` (1–3, clamped via `graph_db.clamp_depth`,
  default 1), `max_nodes` (optional, default **100**, clamped 1–500 →
  `neighborhood`'s existing `cap` kwarg). The lower agent-side default is
  the one deviation from the REST route: 500 nodes is a UI canvas budget,
  not an agent context budget; `stats.truncated` already says when to
  re-ask bigger.
- **Output**: §3.5's payload verbatim — nodes carry
  `imported`/`import_state`/`import_error`, so the gray/yellow (and
  N4-in-flight) distinction N3's UI draws ships to agents for free.
- **Degradation**: on `GraphUnavailable`, the tool returns the structured
  reason (`error: graph_unavailable`, `configured`, `detail`, pointer to
  `GET /api/graph/health`) in the module's existing error idiom — never an
  unhandled 500. Same two-causes contract as `api/graph.py:57-68`.
- **Auth: nothing new.** The gateway already reaches `POST /mcp` with
  `X-Api-Key` (`mcp_config.ingest_server`, `mcp_config.py:94-108`).
  Per-agent scoping to exactly this tool reuses the scoped-gateway-profile
  pattern proven on `knowledgeable-explorer`
  (`aw-autoskill-ap-mt-scope-agent-to-one-tool`).
- **Consumers it unblocks**: the future theme-convergence job's agent, a
  Telegram-facing research assistant, and N4's importer (which may read the
  neighborhood before disambiguating). Theme-rooted or ref-key-rooted
  queries are **deferred** — they need new Cypher in `graph_db.py`, which
  makes them N2-shaped work, not wiring; re-card if a consumer actually
  asks.

---

## 4. Theme convergence — named, NOT built, and what v1 guarantees it

The future job Frederico described ("observa os temas e tenta encontrar
coisas comuns pra convergir temas e ligações") is **out of scope for every
card below**. What v1 bakes in so it lands later with zero schema rework:

- **Stable identity**: `theme_id` is minted once and never derived from
  the label — a theme can be renamed, re-labelled, or merged without its
  id changing (`themes` table, §3.1).
- **Durable merge map**: `themes.merged_into` is the job's write target,
  in Postgres — so merges survive a full Neo4j rebuild (the rebuild
  replays canonical identities, which is exactly why Neo4j is the serving
  store and not the system of record, §1.1).
- **Membership by id**: `review_coding.theme_id` + `HAS_THEME` edges
  reference ids, never labels — converging two themes repoints edges to
  the canonical node and marks the loser merged; paper↔paper theme links
  converge automatically because they are traversals through the theme
  node, not materialized copies (§3.3).
- **Resolution follows the chain**: `resolve_themes` already lands new
  rows on the canonical theme when a merged `norm_label` matches (§3.2),
  so post-convergence ingests don't resurrect merged themes.

The job itself (near-duplicate detection — label embedding similarity
and/or LLM confirm — scheduling, and the repoint pass) is a future card
the PO scopes. Nothing in N1–N4 blocks on it or anticipates its internals
beyond the four guarantees above.

---

## 5. What I rejected, and why

1. **Full migration — vectors into Neo4j too.** Still rejected, now as
   the one surviving piece of the old decision: Neo4j Community's vector
   index takes no filter argument (the 2026-10-12 reversal's measured
   driver), `store.py`'s figures dual-embedding + `_PENDING_SHA`
   crash-safety would be rebuilt for zero retrieval gain, and Frederico's
   override asks for the graph + theme links + a reusable Neo4j app — not
   for moving embeddings. **Flagged for veto** like last time; if he
   wants vectors in Neo4j too, that is a PO scope conversation (it
   roughly doubles the work and touches every retrieval path).
2. **Neo4j as system of record (no Postgres identity tables).** Loses:
   rebuildability (Community backup = stop-the-container dump, and
   aw-app-neo4j explicitly defers backups), the app's existing migration
   machinery for identity evolution (`norm_version`), and durable
   convergence merges across rebuilds. Costs one `MERGE`-sync module —
   cheap next to what it buys.
3. **Pure AP-MT DAG calling the MCP tools** (the literal multi-node
   reading of the mechanism comment). Factually blocked: AP-MT-hosted
   MCP over HTTP has no network route to this workspace's gateway (live
   finding). Runner-backed single-agent run chosen instead; decomposes
   into a DAG later without app-side changes (§3.7).
4. **In-process import with an LLM-disambiguation-only DAG node** (app
   does search+index itself, agent only picks the match). Cheapest and
   fastest, but it is *not* "an agentic flow that calls the MCP tool" —
   the exact literalism the Neo4j override just taught us not to shave.
   Kept as the fallback if runner dispatch proves too slow/expensive in
   practice (~minutes and agent-tokens per import vs. seconds).
5. **Materialized `THEME_LINK` paper↔paper edges.** A second copy of a
   fact the convergence job would then have to rewrite; the 2-hop
   traversal is the point of having a graph store. Revisit only on
   measured viewer-query timings.
6. **Sharing `aw-neo4j` (aw-knowledgeable's instance).** Still rejected
   unconditionally — Community is single-database; that instance is
   aw-knowledgeable's private store (its K5 discipline), and Frederico's
   override itself names a **new** reusable app instead.
7. **Core-relay or new-infra websocket fan-out** (Redis pub/sub from the
   app). `ctx` exposes no broker, and reaching around the app framework
   to core's internal relay couples this app to core internals. The
   Postgres-watch socket is boring and WORKERS-correct.

---

## 6. What this makes harder later

- **A second store to keep honest.** Neo4j can drift from Postgres truth
  (a sync failure during ingest is logged, not raised). `rebuild` +
  `graph/health` counts make drift visible and repairable, but "graph
  slightly stale" is a new quiet state this app never had. `doctor`
  surfacing (§3.4) is the mitigation; silent degradation is this
  workspace's known failure mode.
- **The frozen contract is now baked into two repos.** `rs__`/`RS_`
  naming, the `neo4j_password` config field, and the bolt hostname bind
  research-search to aw-app-neo4j's §2; if that contract ever changes
  (e.g. Enterprise per-consumer databases), `graph_db.py` absorbs it but
  it is a coordinated two-repo change. A namespace rename stays
  `rebuild`-cheap today (40 docs) and gets more annoying with corpus
  growth. N2 still cannot *merge* before `aw-app-neo4j` is published
  (C3's `required: true`); sequencing (§8) keeps everything else off
  that critical path.
- **The websocket's server-side watch is a 1s/socket Postgres query.**
  Fine at one-user scale; a ctx-level event bus or LISTEN/NOTIFY is the
  named upgrade path if socket counts ever matter.
- **Per-import agent runs cost real time and tokens.** A runner container
  per gray-node click (~1–3 min, agent tokens) vs. the old design's
  in-process seconds. Accepted as the explicit mechanism choice; the §5.4
  fallback is documented if it grates in practice.
- **`import_state` becomes UI-visible API surface**, and the websocket
  delta shape becomes a contract the moment `DocGraph.jsx` ships.

---

## 7. Risks for the Coders

1. **Never block boot or ingest on Neo4j.** Lazy connect, short
   timeouts, log-and-count on sync failure. The import path must succeed
   with the graph down (aw-knowledgeable's boot crash-loop on unhealthy
   Neo4j is the recorded anti-pattern).
2. **`DbTables.execute` sharp edges** (`store.py:93-99, 388-396`): only
   `SELECT` is fetched; check `rowcount` on UPDATE; no cross-call
   transactions — every resolution/sync step idempotent per row.
3. **Migration rules**: migration 0008 has no vector types → nothing
   `public.`-qualified; every hyphenated table name double-quoted
   (`migrations/0001` header).
4. **WORKERS>1**: all job/graph state in Postgres; no module-level graph
   caches; the websocket watch must tolerate another worker finishing the
   job it is watching.
5. **Importer agent permissions**: unattended runner dispatches stall
   forever on MCP tool-approval gates (recorded platform behavior). The
   contributed agent's tool grants must be pre-approved/allowlisted at
   seed time; verify one headless end-to-end import before calling N4
   done.
6. **IdentityGuard WS auth**: browsers can't set WS headers — the token
   rides `?token=`/cookie (`runtime.py:196-207`). Don't invent a bespoke
   auth handshake inside the socket.
7. **UI dist is committed**: `cytoscape` dep ⇒ `npm install` (HOME-cache
   EACCES workaround) + `vite build` + commit `ui/dist`; check-dist gate
   must pass. `theme.css` variables only.
8. **Don't touch the DAG legs** beyond the two named hook lines
   (`review_generate.py:918` area, `_run_index` success) — the
   literature-review pipeline is live with its own in-flight cards.
9. **Shared checkout**: multiple agents on this tree; `git status`
   immediately before committing.

---

## 8. Decomposition — N-cards (G1–G4 superseded/archived)

| Card | Scope | Depends on | Parallel-safe? |
|---|---|---|---|
| **N1 — Postgres identity layer** | migration 0008 (`ref_targets`, `themes`, `theme_id` stamp, `import_jobs`), `graph_identity.py` (both resolutions + `resolve_themes`), hooks, `POST /api/graph/resolve` backfill, tests | — | **Yes — zero Neo4j surface; starts now** |
| **N2 — Neo4j adapter + sync + read API** | `dependencies.apps` += `{"id": "neo4j", "required": true}` (C3), `neo4j_password` config field (C2), `graph_db.py` (driver, K5 discipline, lazy connect + backoff, `rs` prefix guard test), `sync_document`/`rebuild`/`health`, `GET /api/graph/neighborhood` | N1 + **aw-app-neo4j published** (contract frozen in `aw-app-neo4j.md` §2; the app itself is `4035bf3b…d2b1`'s coder card) | No — merges only after aw-app-neo4j is on the marketplace (the one deliberate serialization) |
| **N3 — Document Viewer graph UI** | `DocGraph.jsx` below the PDF, cytoscape rooted layout, 4 node states, depth control, yellow→re-root nav, gray→import panel (panel wired to N4's endpoint, Import disabled until it exists) | N1; builds against §3.5's payload spec with a fixture until N2 serves it | **Yes, against the specced payload** |
| **N4 — agentic import + websocket** | importer agent (contributed, tool-scoped, pre-approved), `POST /api/graph/import` + dispatch via `apmt_credential.py`, job watchdog, `/ws/graph/{id}` + Postgres watch, `DocGraph` live deltas | N1, N2 (node flip syncs to Neo4j), N3 (panel) | Last |
| **N5 — `graph_neighborhood` MCP tool** (§3.9, card `4035bf3b-9510-8188-ac64-ef59cb66efb5`) | `_TOOLS_SCHEMA` entry + dispatch branch in `mcp_ingest.py` wrapping `graph_db.neighborhood`, `contributes.mcp.provides` += the tool, gateway re-dial + fresh-session verification, tests in `tests/test_mcp_ingest.py` | N2 **merged** (wraps its query function — not a second implementation) | Yes vs N3/N4 — shares no files with either |

Sequence: **N1 now ∥ aw-app-neo4j (already running)** → N2 (on contract)
∥ N3 (on fixture) → N4, with **N5 any time after N2 merges** (independent
of N3/N4). This is the "só faz em paralelo o que não gerar retrabalho"
split: N1 and N3 cannot be invalidated by any contract outcome; N2 is the
only piece that waits.

**Deferred, named** (not in any N card): the **theme convergence job**
(§4 — schema-ready, PO scopes the card); corpus-wide graph view (the old
Graph-tab idea — the neighborhood endpoint generalizes via a virtual
root); graph-expanded retrieval (`related` section, ex-G4 — still wanted,
now reads Neo4j neighborhoods; re-card after N2); fuzzy/embedding
reference resolution; bulk "import all references"; background
auto-import; **auto-AI-Generate-on-import** (hook seam unchanged at
`_run_index` success); retrofitting audio/review status onto the new
websocket; moving vector search into Neo4j (rejected §5.1 — veto point).

New infra: the `aw-app-neo4j` dependency (its own card). New deps:
`neo4j` (python driver), `cytoscape` (UI). LLM cost: ~per-import agent
run (N4) — everything else is string normalization and sync.
