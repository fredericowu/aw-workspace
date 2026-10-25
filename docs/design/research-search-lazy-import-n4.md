# research-search N4 — wire the gray-node Import button: agentic resolve → import → live gray-to-yellow

Status: **design, not code.** Architect, 2026-10-25.
Card `4045bf3b-9510-8133-8df7-fbac77715fc4` (this card is the go-ahead for,
and supersedes as dispatch vehicle, the Backlog card
`4035bf3b-9510-81ad-8cdd-d50676a8a2c6` "N4 — agentic lazy import").

This is a REFRESH of the N4 section already designed in
[`research-search-neo4j-graph.md`](research-search-neo4j-graph.md) §3.7–3.8
(commit 94a52c3+8628e66), re-grounded against the code as it exists after N3
(`aw-app-research-search` v0.48.0, commit `efef288`) and against the live
data. The mechanism is not re-litigated: Frederico's explicit instruction
(parent card comment 2026-10-23T21:21) is that import runs as an **agentic
flow calling the MCP tool chain, with the result pushed to the UI over a
websocket (not polling), the node flipping gray → yellow live**. What this
doc adds: the resolution contract made concrete with live numbers, two
silent gaps found by reading the current code that the old spec assumed were
already closed, one deliberate deviation (a bail-report MCP tool), and the
failure/ambiguous-match UX the new card explicitly asks for.

Every claim below is grounded in a file read or a live query this session
(2026-10-25).

---

## 0. Live facts this design stands on (verified 2026-10-25)

| Fact | Where |
|---|---|
| `ref_targets`: **723 rows, 19 with a DOI (2.6%), 5 resolved**; `import_jobs`: **0 rows** | live query, `aw_workspace.workspace_aw."app__research-search__ref_targets"` / `__import_jobs` |
| Gray-node payload already carries `key, title, year, first_author, doi, imported, import_state, import_error` | `research_search_app/graph_db.py:1118-1127` (`_DOC_GRAY`), `graph_db.py:1203-1229` (`_node_payload`) |
| `import_jobs` schema: `ref_key` FK CASCADE → `ref_targets`, `root_document_id`, `run_id`, `state CHECK (dispatched/running/indexed/failed)`, `updated_at` | `migrations/0009_graph_identity.sql:171-187` |
| `import_states()` serves latest row per `ref_key` (retry shows the retry, not the old failure) | `graph_db.py:1177-1198` |
| Panel Import button hardcoded `disabled`, "Coming in N4"; failed-state tooltip and pulse plumbing already view-agnostic | `ui/src/views/DocGraph.jsx:406-421`, `:164`, `:176` |
| `POST /graph/import` and `/ws/graph/{id}` declared "N4, not here yet" | `research_search_app/api/graph.py:26` |
| Ingest success hooks (`resolve_document_quietly` + `sync_document_quietly`) exist **only in `_run_index`**, the HTTP route's background task | `research_search_app/api/corpus.py:223-249` |
| The MCP `index_paper` branch calls `ingest.index_paper` **without those hooks** | `research_search_app/mcp_ingest.py:302-318` |
| Relative-pdf-path normalization (`_resolve_downloaded_path`) exists only in corpus.py | `api/corpus.py:144-161` (card 4035bf3b-9510-81fe precedent) |
| `paper-search` is this app's own managed HTTP service; `download_with_fallback` writes under `paths.downloads_dir()` when `save_path` is passed, which `upstream_client` does but a raw gateway tool call does not | `mcp_config.py:11-17` docstring, `upstream_client.py:118-137` |
| In-app sources = OpenAlex + Semantic Scholar only (both `can_resolve` DOI); the 21-catalogue surface is the `paper-search` upstream behind the gateway | `sources/registry.py:24`, `openalex.py:295,314`, `semantic_scholar.py:253,274` |
| App's contributed agents today are all no-tool structured-completion model agents | `aw-app.json` `contributes.agents.agents` (7 slugs, all `tool_specs: []`) |
| Credential machinery: runners-app identity JWT authenticates AP-MT identity-gated REST; `/v1` needs the self-minted ApiKey | `research_search_app/apmt_credential.py:1-56` |
| AP-MT structured-completion nodes have **no network path to this workspace's MCP gateway**; runner-backed agents reach it daily | recorded constraint, `research-search-neo4j-graph.md` §3.7 |

The 2.6% DOI coverage is the decisive number: a deterministic
DOI-first resolver (the new card's floated lean option) would light up ~19
of 723 gray nodes. Title-based search with judgement on ambiguous hits is
the main path for everything else, and the sampled rows (arXiv preprints,
a Portuguese-language scale-validation paper, a tech report) show why that
judgement is real: some of these will match several catalogue records, and
some will match none. That is exactly the case Frederico's agentic
instruction was made for.

## 1. The approach, in sentences someone can disagree with

**Build §3.7–3.8 as specced, with four refreshes.** Click Import →
`POST /api/graph/import {ref_key, root_document_id}` → `import_jobs` row
(`dispatched`) → dispatch one runner-backed run of a new contributed agent
`research-reference-importer` (tools scoped to exactly `search_papers`,
`download_with_fallback`, `index_paper`, `get_indexed_paper`, plus the new
`graph_import_report` below). The agent resolves (DOI rung when present,
title rung otherwise), downloads, indexes; `index_paper` lands back
in-process, where the ingest success path flips `ref_targets.document_id`,
marks the job `indexed`, and syncs Neo4j. A new
`/ws/graph/{document_id}` websocket pushes `{ref_key, state, document_id?,
error?}` deltas; DocGraph applies them live. The client never polls.

The four refreshes over the old spec:

1. **Hook parity in `mcp_ingest`** (silent-failure linchpin): the MCP
   `index_paper` success branch must run the same post-ingest block
   `_run_index` has (`resolve_document_quietly` → mark jobs indexed →
   `sync_document_quietly`), plus `_resolve_downloaded_path`-style
   normalization of `pdf_path`. Without this, every piece of N4 "works"
   and no node ever flips. This also fixes a latent N2 bug: any direct MCP
   import today leaves the graph stale until `POST /graph/rebuild`.
2. **A bail-report tool `graph_import_report`** on the
   `research-search-index` server (deviation from §3.7, which had only a
   watchdog): `{job_id, state: running|failed, detail}`. The ambiguous-match
   bail is the agent's most valuable output; with watchdog-only it surfaces
   as a generic timeout ~20 minutes later. The tool may write `running` and
   `failed` only, never `indexed` (success stays observed, not reported).
3. **Lazy watchdog, no sweeper**: `import_states()` and the ws loop treat
   `dispatched`/`running` rows older than `IMPORT_JOB_TTL` (default 20 min,
   above the runner platform's 600s idle kill) as `failed` ("import run
   timed out") at read time. No scheduler, WORKERS-safe by construction.
4. **Indexed delta triggers one background refetch**: the node restyles
   yellow immediately from the delta (the live flip Frederico asked for),
   and the UI then refetches `/graph/neighborhood` once in the background,
   because a newly imported document gains edges (its own references,
   shared themes/authors) that a `{ref_key, state}` delta cannot carry.

### The resolution contract (what the agent is instructed to do)

Input (the dispatch `input`, JSON): `{job_id, ref: {key, doi, title, year,
first_author}, root_title}` (`root_title` is disambiguation context: the
citing paper).

- **DOI rung** (when `ref.doi` present): search/lookup by DOI; a candidate
  whose normalized DOI equals `ref.doi` (normalization identical in spirit
  to `graph_identity.normalize_doi`) is accepted without further judgement.
- **Title rung**: `search_papers` with the title (retry with trimmed
  subtitle if zero hits). Accept a candidate only if it is the single
  plausible match: normalized-title equality (same idea as
  `graph_identity.normalize_text`, so the accept rule and the flip rule
  cannot disagree), year within ±1 when both known, first-author family
  name consistent when both known.
- **Anything else is a bail**, via `graph_import_report(failed, detail)`:
  zero hits → "no catalogue match found"; several plausible hits → detail
  lists the top candidates (title, year, source) so the panel shows *why*;
  download or ingest failure → the tool error verbatim. **Never import an
  uncertain match**: a wrong paper indexed under a gray node's identity is
  corpus poisoning, recorded as the blocking risk since the G3 design.
- On success: `download_with_fallback` (pass an absolute `save_path`;
  belt-and-braces given refresh 1's normalization), `index_paper(source,
  paper_id, pdf_path, doi, title)`, then `get_indexed_paper` to verify.

## 2. Where it lands

| Piece | File/seam |
|---|---|
| `POST /graph/import` | `research_search_app/api/graph.py` (router exists; 503-degradation idiom at `:60-74` does NOT apply here, the route is Postgres+dispatch only, no Neo4j needed). Validates ref exists and `document_id IS NULL`; **dedup**: an active (`dispatched`/`running`, non-stale) job for the same `ref_key` is returned as-is, no second dispatch; inserts job; dispatches; stamps `run_id`; 202 `{job_id, ref_key, state}` |
| Dispatch | new `research_search_app/graph_import.py` (or a section of `graph_identity.py`, coder's call): AP-MT REST run-dispatch of agent slug `research-reference-importer`, authenticated per `apmt_credential.py`'s documented JWT inheritance (identity-gated REST, NOT `/v1`; the `/v1` ApiKey path is for completions). Follow skill `aw-autoskill-ap-multitenant-rest-api` |
| Contributed agent | `aw-app.json` `contributes.agents.agents` + whatever the runners app's `agent_provisioner` actually supports for a runner-backed, MCP-tool-scoped agent (see Risks: this may need the raw-REST `mcp_config`/`disable_harness_tools` path from `aw-autoskill-ap-mt-scope-agent-to-one-tool`, reconciled at install per `contributed-agents-are-reconciled-not-seed-once`). Sonnet-class model. Tool grants pre-approved at seed time |
| `graph_import_report` tool | `research_search_app/mcp_ingest.py`: one `_TOOLS_SCHEMA` entry + one dispatch branch writing `import_jobs` (`running`/`failed` only, validates the job exists and is still active) |
| Hook parity | `mcp_ingest.py` `index_paper` success branch: shared post-ingest helper extracted from `api/corpus.py:223-249` (resolve → `mark_import_jobs_indexed` → sync), plus `pdf_path` normalization shared from `_resolve_downloaded_path` |
| `mark_import_jobs_indexed(db, document_id)` | `research_search_app/graph_identity.py`: one idempotent UPDATE, `state='indexed'` where `ref_key` now links to this document and state is active. Called from both ingest success paths |
| Websocket | `routes.py`: `@app.websocket("/ws/graph/{document_id}")`. Auth is core's IdentityGuard (ws scope handled for mounted apps, `?token=`/apex cookie, close 4401); do not invent a handshake. Loop: ~1s Postgres cursor on `import_jobs.updated_at > last_seen`, push every delta, **client filters by the ref_keys it displays** (simpler than server-side neighborhood recomputation, and cross-document correct). Postgres is the bus; WORKERS>1 safe |
| TTL | constant in `graph_db.py` or `graph_identity.py`; applied in `import_states()` and the ws loop (computed, not written) |
| UI | `DocGraph.jsx`: enable the button (`:418-421`) → `api.graphImport(...)` (new, `ui/src/api.js` sibling of `graphNeighborhood`); optimistic `dispatched` pulse; open ws on mount, deltas: `dispatched/running` pulse, `failed` red + tooltip with `error`, `indexed` yellow now + one background refetch; socket drop → one refetch + reconnect with backoff; failed panel state gets a **Retry** (same POST; a retry is a NEW job row, `import_states` latest-row-wins already renders it). Rebuild + commit `ui/dist` |
| Tests | job state machine on the fake db; `POST /graph/import` dedup + validation; `graph_import_report` guard rails; ws tests MUST use the TestClient context-manager form (`aw-autoskill-fastapi-testclient-websocket-hang-without-context-manager`); a fixture for the delta shapes |

Untouched: `view=references` byte-pin, `_DOC_GRAY`/neighborhood queries,
migrations (0009 already has everything, no schema change), the N5 MCP tool
(unshipped), audio/AI-Generate polling (explicitly NOT retrofitted onto the
new socket).

## 3. What I rejected, and why

1. **Deterministic in-app resolution** (the new card's floated lean option:
   `sources/*.py` DOI lookup + auto-accept top title hit, no agent). Two
   independent kills: 2.6% DOI coverage leaves the deterministic rung
   almost empty, and auto-accepting title hits without judgement is the
   recorded corpus-poisoning risk. It would also quietly override
   Frederico's explicit agentic+websocket instruction. Runner-up if the
   agentic path proves too slow or fragile in practice; even then it slots
   behind the same `import_jobs` + ws contract.
2. **Synchronous panel candidate-picker** (old G3: prefilled
   `/catalogue/search`, human picks, poll). Superseded by the owner's
   mechanism override on 2026-10-23. A picker for the agent-bailed case is
   a sensible later additive; not in this card.
3. **Polling instead of the websocket.** Explicitly forbidden by the
   instruction ("via WEBSOCKET (not polling)").
4. **A background watchdog sweeper.** The lazy TTL at read/watch time needs
   no scheduler, cannot leak across WORKERS, and `import_states` is already
   the single assembly point both consumers share.
5. **Marking bails by polling AP-MT run status from the ws loop.** Couples
   the socket loop to AP-MT REST availability and parses a transcript for
   what the report tool states in one structured call.
6. **Widening the agent's tool scope** (`get_crossref_paper_by_doi`,
   `graph_neighborhood`, full `paper_search` surface). Minimal scope is the
   security posture that makes the pre-approved grants defensible; widen
   only when a real bail transcript shows a need.
7. **Dispatching via `/v1` chat completions** with the existing ApiKey.
   `/v1` runs structured completions and DAG workflows; those nodes cannot
   reach this workspace's gateway (recorded network fact), so the tools
   would silently not exist. The run must be runner-backed.

## 4. What this makes harder later

- **First websocket in the app**: every open viewer holds a ~1s DB poll
  loop server-side. Fine at this corpus's single-user scale; a multi-viewer
  future wants LISTEN/NOTIFY or a ctx-level bus (named in the parent
  design §6; unchanged here).
- **Per-click agent runs cost real tokens and 1–3 min latency.** "Import
  all references" (723 grays) stays explicitly out; building it later means
  building batch dispatch + rate limiting, not looping this endpoint.
- **`graph_import_report` is agent-facing contract surface** on the index
  server: renaming it later breaks the seeded agent's instructions, which
  live in a different reconcile cycle than the code.
- **TTL is computed, not stored**: any future consumer of `import_jobs`
  must apply the same staleness rule or disagree with the UI about what is
  "failed".
- The agent seeding path bakes in a dependency on what the runners app's
  provisioner supports; if it needs the raw-REST fallback, app updates must
  re-assert it (reconcile semantics).

## 5. Risks for the Coders

1. **MCP tool-approval gates stall unattended runs forever** (recorded,
   CRITICAL on the old card): the agent's tool grants must be
   pre-approved/allowlisted at seed time. The acceptance test is **one
   headless end-to-end import flipping a real gray node live over the
   websocket** in the running app. Do not call this done on unit tests.
2. **Workspace binding of the dispatched run**: a Run row with a NULL/wrong
   workspace lands on the wrong runner and the agent's gateway tools hit
   the wrong (or no) workspace (recorded: `runner-execute-401-root-cause-
   null-workspace-fallback`, AP-MT `/v1` workspace-binding design). Verify
   the dispatched run executes against THIS workspace's gateway.
3. **Hook parity is the silent linchpin** (§1 refresh 1): forget it and
   every demo "succeeds" while the node stays gray. Write the test that
   drives the MCP `index_paper` path and asserts `ref_targets.document_id`
   flipped and the job is `indexed`.
4. **`pdf_path` relativity**: the agent receives whatever
   `download_with_fallback` returns, which has shipped relative paths
   before (card 4035bf3b-9510-81fe). Normalize in `mcp_ingest`, don't trust
   the prompt alone.
5. **AP-MT write timeouts can land minutes late** (recorded): the dispatch
   POST timing out does NOT mean no run was created; re-check before
   re-dispatching, or the dedup rule eats the duplicate anyway (that is
   part of why dedup is specced server-side).
6. **Shared tree is dirty right now** (theme_converge + llm_reply work in
   flight in `repos/aw-app-research-search`): work in a git worktree, and
   commit only this card's files. Rebuild `ui/dist` from the worktree and
   commit it (house rule); no invented tailwind classes.
7. **FastAPI ws TestClient hangs without the context-manager form**
   (recorded skill). Also: this is the app's first websocket; confirm
   IdentityGuard's ws handling on BOTH mount roots (subdomain and
   `/api/apps/research-search/`), the same dual-mount concern
   `routes.py:200-205` documents for HTTP.
8. **`import_jobs.ref_key` FK CASCADE**: a re-resolved/deleted ref target
   silently deletes its job rows; don't be surprised by a vanished job in
   tests that rebuild identity.
9. **Agent model slug must exist on the platform** (`list_models`); the
   manifest's `anthropic-sonnet-4-5` entry is this app's `/v1` model, not
   automatically a runner CLI config.
