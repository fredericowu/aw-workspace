# research-search — AI Generate as manual graph refresh, auto-generate on import, MCP corpus-navigation audit

Status: **design, not code.** Architect, 2026-10-25.
Card `4045bf3b-9510-8100-a9e3-e77e4ca53737` (target `research-search-mcp`,
repo `tekflox/aw-app-research-search`, checkout
`repos/aw-app-research-search` @ `f33539d`, v0.48.0).

Downstream of, and deliberately NOT re-scoping:
[`research-search-lazy-import-n4.md`](research-search-lazy-import-n4.md)
(card `4045bf3b-9510-8133`, Planned, coder-opus) and the N5 Backlog card
`4035bf3b-9510-81eb-966c-edce486e5cfb` (graph_neighborhood MCP tool, fully
specced there).

Every claim below is grounded in a file read this session (2026-10-25).

---

## 0. Verified facts

| Fact | Where |
|---|---|
| AI Generate = `start_generate_job` → `run_generate_job`; routes legacy one-shot vs literature-review DAG on `_is_workflow_model` | `research_search_app/review_generate.py:695`, `:760-780` |
| The DAG persist **already** calls `graph_identity.resolve_document_quietly` + `graph_db.sync_document_quietly` for the document — but only inside `if written:`, and only on the DAG branch | `review_generate.py:951-969` (HEAD `c424253` lines 999-1019; the dirty working tree there is unrelated llm_reply refactor work) |
| The legacy one-shot path writes review FIELDS only (no references, no coding) and has no graph hooks | `review_generate.py:135`, `:912` comment |
| `_run_index` (HTTP import) runs resolve+sync on success; the auto-AI-Generate seam is explicitly flagged "its own card" — this card | `research_search_app/api/corpus.py:223-248`, comment `:231-233` |
| The MCP `index_paper` branch has NO resolve/sync hooks and no pdf_path normalization — **owned by N4 refresh 1, not re-scoped here** | `research_search_app/mcp_ingest.py:302-319`; `research-search-lazy-import-n4.md` §1.1 |
| Nothing anywhere auto-queues a generate job: the only `start_generate_job` callers are the REST route and the `generate_review` MCP tool | `api/review.py:321`, `mcp_ingest.py:487` (grep, whole app) |
| `start_generate_job` dedups via `claim_job` ("reusing the running job") | `review_generate.py:734-739` |
| MCP tools live today — `research-search-index`: `index_paper`, `get_indexed_paper`, `list_indexed_papers`, `index_status`, `search_paper_figures`, `paper_to_audio(+_status)`, `generate_review(+_status)`; `aw-research-search`: `get_paper`, `get_citations` (external catalogue citations, not corpus); plus the 21-catalogue `paper-search` upstream (`search_papers`, `download_with_fallback`, `read_*`) | `mcp_ingest.py:44-245`, `mcp_server.py:60-129`, `aw-app.json` `contributes.mcp.provides` |
| `get_indexed_paper` returns METADATA ONLY (id/source/doi/title/pdf_path/md_path/...), never text | `research_search_app/store.py:450-468` |
| Corpus full-text search exists as REST (`GET /corpus/search` → `store.search_paper_text`) with **no MCP equivalent** | `api/corpus.py:76-86`, `store.py:765` |
| `GET /graph/neighborhood` exists as REST with no MCP equivalent — that gap IS the N5 card, Backlog, spec complete including the gateway-stale-tools verification procedure | `api/graph.py:154`, card `4035bf3b-9510-81eb-966c-edce486e5cfb` |
| `mcp_ingest.handle_request` already receives `db`, `config` and the lazy secret callables (needed by `generate_review`) — everything D2 needs is plumbed | `mcp_ingest.py:264-278` |
| App config is a plain dict read with `.get()` + defaults (`llm_transport_of` et al.) — no manifest schema to touch for a new flag | `aw-app.json` `config: {}`, `review_generate.py` config readers |

---

## 1. The approach, in sentences someone can disagree with

**Ask 1 is already ~90% true; make it an invariant rather than a side
effect.** "AI Generate rebuilds the graph" means *per-document*
`resolve_document_quietly` + `sync_document_quietly` — never
`POST /graph/rebuild` (full namespace wipe+replay stays an admin action).
Today the hooks fire only when the DAG wrote something. Change: run them on
**every generate-job termination** — done, partial, and all-legs-failed, on
both the DAG and legacy branches — so clicking AI Generate is a reliable
manual "refresh this document's graph" even when the LLM produced nothing
new. The hooks are idempotent projections of current Postgres rows; the
extra cost is two quiet calls.

**Ask 2: one new module, two one-line call sites, one config flag.**
`maybe_queue_generate(...)` fires AI Generate after every successful import
— from `_run_index` and from the MCP `index_paper` branch — gated by config
flag `auto_generate_on_import` (default **true**), swallowing every
exception. Import itself stays exactly as manual as it is: no new import
triggers anywhere (N4's agentic click-to-import funnels through the MCP
`index_paper` tool and therefore inherits auto-generate for free).

**Ask 3: two real gaps get filled here, one is N5's and gets promoted, the
rest is confirmed covered.** New MCP tools `search_corpus` (corpus
full-text search) and `get_paper_text` (read an indexed document's
markdown) on the `research-search-index` server. The references/graph gap
is the unbuilt N5 `graph_neighborhood` tool — recommend moving that card
from Backlog to Ready; do NOT rebuild it in this card.

### D1 — AI Generate ⇒ graph refresh, precisely

Semantics: at the end of `run_generate_job`, for this `document_id`, run
`graph_identity.resolve_document_quietly` then
`graph_db.sync_document_quietly` (that order; sync reads what resolve
wrote), **before** the job row is stamped done/partial/failed — so
`generate_review_status` saying "done" implies the graph is fresh.

Seam: hoist the existing block at `review_generate.py:951-969` out of
`if written:` so it runs on the DAG's failure exit too, and add the same
two calls to the legacy one-shot completion path (both branches already
converge in `run_generate_job`; a single post-persist call site there is
acceptable if cleaner — coder's call, the contract is "every termination
path after the LLM ran"). The early validation exits in
`start_generate_job` (bad field key, missing md, no API key) do NOT
refresh — no job ran.

Keep the local `from . import graph_db` import style and the swallow-by-
construction discipline documented at `:948-968` (a graph outage must
never fail a generate job). `tests/test_graph_db.py:1435` asserts config
threading via `inspect.getsource(start_generate_job)` — keep it honest if
the hook moves.

### D2 — auto-trigger AI Generate on import completion

New `research_search_app/auto_generate.py`:

```
maybe_queue_generate(db, config, document_id, *,
                     get_secret, set_secret, delete_secret, where) -> dict | None
```

- Config gate: `auto_generate_on_import`, default **true**, read the same
  way `review_generate`'s other config readers do.
- Looks up the document itself via `VectorStore.get_document` (md_path +
  `preferred_language` — thread it like `api/review.py:321` does). Missing
  doc or md_path → log + return None.
- Calls `review_generate.start_generate_job(field_keys=None,
  overwrite=False, preferred_language=...)` with the passed secret
  callables.
- **Swallows and logs every exception** (`LLMNotConfiguredError`,
  `FileNotFoundError`, anything): the trigger must never fail or slow an
  import. `claim_job` already dedups a concurrent manual click ("reusing
  the running job" is the correct outcome, not an error).

Exactly two call sites, one line each:

1. `api/corpus.py` `_run_index`, after `sync_document_quietly` (`:245-248`)
   — replace the ":231-233 its own card" comment with the call. Secrets via
   `review_generate_mod.secret_reader(ctx)` etc., same as the route.
2. `mcp_ingest.py` `index_paper` branch — after the ingest call, gated on
   `payload.get("ok")` explicitly (the branch currently returns non-ok
   payloads too; do not queue on those). `handle_request` already has
   `config` and the secret callables.

Do NOT put it inside `ingest.index_paper` (no ctx/secrets there; recorded
rule from the archived Phase A card). Do NOT add any other call site —
"import stays manual" is half of Frederico's instruction.

Ordering note: the import-time resolve+sync still runs first (node flips
immediately); the generate job's own end-of-job resolve+sync (D1) lands
the themes/references edges minutes later. Both are idempotent.

### D3 — MCP corpus-navigation audit (the deliverable Frederico asked for)

| Capability | Today | Verdict / fill |
|---|---|---|
| (a) Read an indexed document | `get_indexed_paper` (metadata only), `list_indexed_papers`, `search_paper_figures`, `generate_review_status` | **GAP: no tool returns document text** — `md_path` is useless to a gateway-only agent (no filesystem). Fill: new tool **`get_paper_text`** — input `document_id` (or `source`+`paper_id`, reuse `_document_id_from`), `offset`/`max_chars` (default ~40k chars, hard cap; return `total_chars` so an agent can page). Reads `md_path`; missing file → tool error in the module's `_err` idiom. |
| (b) References / graph connections | REST `GET /graph/neighborhood` only; `get_citations` is external-catalogue citations, not the corpus graph | **GAP = N5**, card `4035bf3b-9510-81eb-966c-edce486e5cfb`, Backlog with a complete spec (clamping, GraphUnavailable shape, gateway-re-dial verification). `view=references` rooted at a document returns resolved + gray cited works with import_state — exactly "pegar as referências". **Action: promote N5 to Ready; do not rebuild it here.** |
| (c) Search for documents | Catalogue: `search_papers` + 21 per-source tools ✓. Corpus: REST `GET /corpus/search` only | **GAP: corpus text search has no MCP tool.** Fill: new tool **`search_corpus`** — input `query` (required), `top_k` (default 5, clamp 1-20); wraps `store.search_paper_text` via `asyncio.to_thread` (embed is sync CPU work — same idiom as `mcp_ingest.py:313,351`). Name it `search_corpus`, not `search_papers`/`search_paper_text`, to stay unambiguous next to the upstream's `search_papers`. |
| (d) Import a document | `index_paper` ✓ (+ `download_with_fallback` upstream for the PDF) | Works end-to-end for an agent today, with two known defects **owned by N4** (hook parity + pdf_path normalization, N4 doc §1.1 — don't duplicate). This card adds D2's auto-generate line to that same seam. |

Registration mechanics for the two new tools: `_TOOLS_SCHEMA` entries +
dispatch branches in `handle_request`; `aw-app.json`
`contributes.mcp.provides` += `["search_corpus", "get_paper_text"]`
(marketplace listing only). `mcp_config.py`'s mcp.json does NOT change —
which means the gateway will NOT re-dial the upstream on its own
(recorded: upstreams re-dial on spec change only). Post-release
verification is mandatory: restart `mcp-gateway`, confirm the tools in a
**fresh** agent session, make one real call each (the N5 card's §
VERIFICATION procedure, reused verbatim).

## 2. Where it lands (summary)

| Piece | File/seam |
|---|---|
| D1 hook hoist | `research_search_app/review_generate.py:951-969` (+ legacy branch completion in `run_generate_job`) |
| D2 module | new `research_search_app/auto_generate.py` |
| D2 call sites | `api/corpus.py:245-248` region; `mcp_ingest.py:313-319` success path |
| D3 tools | `mcp_ingest.py` `_TOOLS_SCHEMA` + `handle_request`; `aw-app.json` provides list |
| Tests | extend `tests/test_review_generate.py` (hooks fire on failed/empty jobs), `tests/test_mcp_ingest.py` (new tools: list, happy path, clamps, missing-md error; auto-generate queued on ok / not queued on not-ok / not queued when flag off), new `tests/test_auto_generate.py` (flag gate, swallow discipline) |
| Untouched | `ingest.py` (stays pure), `graph_db.py`/`graph_identity.py` (consumers only), N4's scope (`POST /graph/import`, ws, importer agent, mcp hook parity), N5's scope (`graph_neighborhood`), `POST /graph/rebuild`, UI |

## 3. What I rejected, and why

1. **Full `POST /graph/rebuild` from AI Generate.** Wipes and replays the
   whole `rs` Neo4j namespace per click, races concurrent generate jobs,
   and scales with corpus size not document size. Per-document
   resolve+sync is the granularity every existing hook already uses.
2. **Auto-generate inside `ingest.index_paper`.** No ctx/secrets there;
   keeping it pure is the recorded rule from the archived Phase A card —
   and it would fire for internal re-index paths that aren't imports.
3. **Rebuilding `graph_neighborhood` in this card.** N5's card already
   holds the complete spec including the gateway-verification trap; a
   second implementation diverges. The fix is promoting the card, not
   duplicating it.
4. **A generate backfill endpoint for old documents** (archived Phase A
   item 3). Not asked on this card; manual AI Generate covers it; its
   own card if Frederico wants it.
5. **MCP tools for reading/writing review fields.** Not one of the four
   asked capabilities (read / references / search / import); defer.
6. **Shipping auto-generate without the config flag.** Every import
   becomes a DAG run (real tokens, minutes). Default-on honors the ask;
   the flag is the kill switch when N4's agentic imports multiply volume.
7. **A `document_id` filter on `search_corpus`.** `store.search_paper_text`
   has no per-document filter today; adding one is store-layer work this
   card doesn't need — `get_paper_text` + offsets covers the in-document
   case.

## 4. What this makes harder later

- **Auto-generate couples import cost to LLM cost.** A future bulk import
  (N4 "import all references" if ever built) multiplies DAG runs; the flag
  is the only throttle. Any batching/rate-limiting is new work.
- **`get_paper_text`'s cap/offset contract is agent-facing** — changing
  defaults later silently breaks agent pagination loops.
- **Unconditional end-of-job sync** adds Neo4j writes on failed jobs
  (harmless, logged by the quiet wrappers, but it widens when sync runs —
  anyone counting sync calls in tests must update).
- Two more tools on `research-search-index` widen the surface the N4
  importer agent's minimal-scope argument is made against; the importer
  stays scoped to its five tools regardless.

## 5. Risks for the Coder

1. **Gateway stale tools/list** — the new tools won't exist for agents
   until the gateway re-dials AND a fresh session starts (recorded;
   `verify-new-gateway-tools-in-same-session`). The acceptance test is a
   real gateway call, not tests/list in-process.
2. **The shared tree is dirty and N4's coder-opus will touch
   `mcp_ingest.py` concurrently.** Work in a git worktree; commit only
   this card's files; expect a trivial rebase around the `index_paper`
   success path whichever lands second.
3. **Do not hand-bump the version** (release bot owns it).
4. **`md_path` can be NULL or the file gone** (pending-sha documents,
   container moves): `get_paper_text` must return a tool error, and
   `maybe_queue_generate` must skip quietly — never raise.
5. **Non-ok ingest payloads flow through the MCP branch** — gate
   auto-generate on `payload.get("ok")` explicitly or imports that failed
   will queue generate jobs against missing markdown.
6. **`test_graph_db.py:1435` reads `start_generate_job`'s source** to
   assert config threading — if D1 moves the hook, keep that assertion
   meaningful rather than deleting it.
7. **Acceptance (all five, live):** (i) web import → generate job
   auto-starts → themes/references land; (ii) MCP `index_paper` import →
   same; (iii) AI Generate re-run on an unchanged document ends with a
   fresh resolve+sync (log lines `where="review_generate ..."`); (iv)
   `search_corpus` + `get_paper_text` answer real calls from a fresh
   gateway session; (v) flag off → import queues nothing.
