# research-search: AP-MT "Literature Review" DAG — multi-model coding/themes + review fields + references, AI Generate rewired

**Card:** `4025bf3b-9510-816a-8b9e-f080322851c9`.
**Supersedes the LLM-call mechanism of** the one-shot `research-review-generator`
path (card `4025bf3b-9510-81d2-a14f-ed8ddbc8e020`, shipped v0.27.0) — the
credential machinery (`apmt_credential.py`), job table, polling UX and
fill-only-empty discipline from that delivery all stay and are reused.

## 1. The decision

research-search contributes a **`kind: "dag"` workflow `literature-review`**
("Literature Review") to AP-MT via the **existing** `contributes.agents`
manifest surface — the runners app's provisioner already reconciles a
`workflows` list (`aw-app-agents-platform-runners/agents_platform_runners_app/
agent_provisioner.py:107,128,145`); **no AP-MT code changes at all** (the dag
engine shipped: `run_dag` at `agents-platform-multitenant/backend/app/core/
orchestrators/__init__.py:320`, save-time validation at
`backend/app/api/workflows.py:52`).

The DAG generalizes Activity 3: **three agent nodes code the paper
independently on three genuinely different models**, an **arbiter node
reconciles** them into one agreed codes+themes JSON, and two **independent
legs** run concurrently — the existing 9 descriptive review fields
(reusing agent `research-review-generator`) and an **LLM reference
extraction** node. Every agent node emits its answer **already as JSON**
(prompt-enforced structured output, temperature 0.2) — no reformat pass;
the only transforms are three one-step `get text` extractors
(the envelope rule: `{prev}`/`{node:x}` on an agent dependency renders the
whole `{text, tool_results, ...}` envelope — `orchestrators/__init__.py:
261-286` — same reason the live search-knowledge dag has `plan-text`/
`sources-text` extractors).

**AI Generate keeps its exact transport** — `call_llm_apmt`'s
`POST /v1/chat/completions` with the self-minted ApiKey — and only changes
the model string to **`workflow/literature-review`**: `/v1` natively runs
workflows (`openai_compat.py::_parse_model:180` → `_run:394` →
`executor.run_workflow:2971`). The run returns one JSON object
`{"reconcile": <coding JSON string>, "review-fields": <fields JSON string>,
"references": <refs JSON string>}` (multi-sink dag output, json.dumps'd by
`_run:404`), which the app parses per leg and persists.

**Model/credential decision: everything runs on live-credential models —
`bedrock-sonnet-4-5` + `bedrock-opus-4-1` + `openai-gpt-5-4` for the three
coders, `bedrock-sonnet-4-5` for arbiter and review-fields,
`openai-gpt-5-4-mini` for references.** The `anthropic` provider stays dead
(`ANTHROPIC_API_KEY` unset in AP-MT's container, reproduced 3× on the
sibling card); we do NOT block this card on that infra fix. This includes
**repointing the existing `research-review-generator` agent's `model_slug`
to `bedrock-sonnet-4-5`** (one `update_agent` data flip — same underlying
Claude Sonnet 4.5, confirmed enabled) — which is also exactly option (b)
the sibling Need Human card `…81d2…` asked for; comment there when done.
What would change this: AP-MT gaining a live `ANTHROPIC_API_KEY` — then
repointing back is one `update_agent` per agent, nothing structural.

**Inference-fields policy (`my_take` / `anything_else`) — the flagged
decision:** the DAG **does address them, but never writes into them.** The
arbiter additionally emits `my_take_suggestion` / `anything_else_suggestion`;
the app persists these under **separate** note keys (`ai_my_take`,
`ai_anything_else` — NOT added to `REVIEW_FIELDS`, so the existing
PUT-route gate and pane field list are untouched), and the UI shows each as
a collapsed "AI suggestion" block under the corresponding inference field
with a "copy into field" button. The documented audit principle
(review.py:44-51 — "the user's own analysis stays auditable") survives
because the user's field only ever contains what the user typed or
explicitly copied; Frederico's "answer these questions" ask is met because
the DAG produces an answer for every field on the pane. **Flip condition:**
if Frederico says "just fill the field", delete the suggestion keys and
write directly — one small diff, flagged on the card for his veto.

## 2. Where it lands (real files)

All code changes in `repos/aw-app-research-search`. AP-MT: zero code; one
`update_agent` data flip. Runners app: zero.

### 2a. `aw-app.json` — the contribution

`contributes.agents` gains:

- **agents** (all `tool_specs: []`, JSON-only system prompts in the style of
  the existing `research-review-generator` entry):
  - `research-lit-coder-a` → `model_slug: bedrock-sonnet-4-5`
  - `research-lit-coder-b` → `model_slug: bedrock-opus-4-1`
  - `research-lit-coder-c` → `model_slug: openai-gpt-5-4`
  - `research-lit-arbiter` → `bedrock-sonnet-4-5`
  - `research-lit-references` → `openai-gpt-5-4-mini`
  Role-stable slugs (a/b/c, not model names) so a later model swap is an
  `update_agent`, not a workflow-graph edit.
- **workflows**: one entry, slug `literature-review`, name "Literature
  Review", `kind: "dag"`, the graph below. The provisioner POSTs it to
  `/api/workflows`, where `_normalize_kind` → `_validate_dag_graph` runs at
  save time — a malformed graph fails seeding with a logged 400, so keep a
  unit test that mirrors the validator's structural rules (unique ids,
  needs exist, acyclic, agent nodes have `agent`).
- also update the existing `research-review-generator` manifest entry's
  `model_slug` to `bedrock-sonnet-4-5` (affects fresh installs; the LIVE
  row needs the one-time `update_agent` — provisioner content is
  seed-once, `agent_provisioner.py:340-344`).

### 2b. The graph (manifest payload, drafted)

```json
{
  "max_hops": 12,
  "max_tokens": 600000,
  "nodes": [
    {"id": "code-a", "agent": "research-lit-coder-a", "label": "Code (Sonnet/Bedrock)",
     "input_template": "<coding instructions, JSON schema §3a>\n\n=== PAPER ===\n{input}"},
    {"id": "code-b", "agent": "research-lit-coder-b", "label": "Code (Opus/Bedrock)",
     "input_template": "<same>"},
    {"id": "code-c", "agent": "research-lit-coder-c", "label": "Code (GPT-5.4)",
     "input_template": "<same>"},
    {"id": "text-a", "kind": "transform", "needs": ["code-a"],
     "steps": [{"op": "get", "path": "text"}]},
    {"id": "text-b", "kind": "transform", "needs": ["code-b"],
     "steps": [{"op": "get", "path": "text"}]},
    {"id": "text-c", "kind": "transform", "needs": ["code-c"],
     "steps": [{"op": "get", "path": "text"}]},
    {"id": "reconcile", "agent": "research-lit-arbiter", "label": "Reconcile",
     "needs": ["text-a", "text-b", "text-c"],
     "input_template": "<arbiter instructions, §3b>\n\nCODEBOOK A:\n{node:text-a}\n\nCODEBOOK B:\n{node:text-b}\n\nCODEBOOK C:\n{node:text-c}\n\n=== PAPER ===\n{input}"},
    {"id": "review-fields", "agent": "research-review-generator", "label": "Review fields",
     "input_template": "<today's build_prompt() field spec verbatim, §3c>\n\n=== PAPER ===\n{input}"},
    {"id": "references", "agent": "research-lit-references", "label": "References",
     "input_template": "<reference-extraction instructions, §3d>\n\n=== PAPER ===\n{input}"}
  ]
}
```

- Wave 1: `code-a/b/c`, `review-fields`, `references` run concurrently
  (parallelism falls out of `needs`). Wall-clock ≈ slowest coder + arbiter.
- Sinks = `reconcile`, `review-fields`, `references` (nothing needs them) —
  `run_dag` returns `{sink_id: text}` (`orchestrators/__init__.py:376-389`),
  `_run` json.dumps it. **Sink ids are now an app-side API contract** (§5).
  Never name a sink `text` or `reply` — `_run:404` would return that value
  alone.
- Round-2 refinement (Activity 3's r2) is **deferred**: the live 2-coder
  exercise (`.tmp/lit-review/agreed-codebook.md`) reconciled cleanly without
  it. Named trigger: arbiter output showing consistently low agreement or
  dropped anchors in real use → add three `refine-*` nodes between coders
  and arbiter — a data-only `update_workflow`, no code.

### 2c. Output schemas (§3 — the contract every prompt enforces)

- **3a coder** (and the agreed shape mirrors `.tmp/lit-review/*.md`):
  `{"codes": [{"code", "description", "anchors": [{"quote", "location"}]}],
    "themes": [{"theme", "description", "codes": [<code labels>]}]}`
- **3b arbiter**: same `codes`/`themes` shape plus per-item
  `"agreement": "unanimous"|"majority"|"single-adopted"` and
  `"sources": ["a","b","c"]`, plus `"my_take_suggestion": str,
  "anything_else_suggestion": str`.
- **3c review-fields**: exactly today's one-shot object — keys from
  `REVIEW_FIELDS`' nine descriptive entries, omit-if-unaddressed
  (`review_generate.build_prompt` semantics moved into the node's
  `input_template`). **Drift guard:** a unit test asserts the manifest
  template names exactly `generatable_fields()`' keys — the field spec now
  exists in two places in the same repo, and that test is what keeps them
  honest.
- **3d references**:
  `{"references": [{"raw": "<citation as printed>", "first_author", "year",
    "title", "venue", "doi_or_url"}]}` — `raw` required, the structured
  subfields best-effort. Compact: no abstracts, no commentary.

### 2d. research-search backend

- **`review_generate.py`** —
  - `DEFAULT_LLM_MODEL_AP_MT` becomes `f"workflow/{WORKFLOW_SLUG}"`
    (constant beside `AGENT_SLUG` in `apmt_credential.py`). `llm_model`
    config override still works; `agent/research-review-generator` remains a
    valid value and takes the legacy one-shot parse path — the result parser
    branches on shape (sink-ids object vs flat fields object), which gives
    the escape hatch for free.
  - New `parse_dag_response(raw_text)` → per-leg tolerant parsing: outer
    JSON → three strings (a leg absent/`null` = that leg failed upstream) →
    each inner doc through the existing fence-stripping `parse_response`
    style. **Partial success is a real state**: write whatever legs
    succeeded, record failed legs in the job row (`fields_written` plus a
    new `legs_failed` note folded into `error` text; job state `done` if ≥1
    leg landed, `failed` if all three died).
  - `prepare_input_text` **cannot be reused as-is**: it calls
    `audio.trim_markdown`, which strips the references section — the
    references leg would read a paper with no bibliography. Add
    `prepare_input_text_full` that keeps the bibliography and keeps the
    head+tail truncation (tail is where references live). Raise the
    workflow call's budget: `llm_max_input_chars` default stays, but cap
    awareness documented.
  - Timeouts/heartbeat: a 6-node DAG runs minutes, not 15-45s.
    `LLM_TIMEOUT_S` (120s) must become per-transport (`600s` for the
    workflow call), and the job heartbeat (`HEARTBEAT_STALE_S` 120s, touched
    only at claim) must be touched periodically during the await (small
    asyncio task) — otherwise `generate_review_status` reports `stale`
    mid-run and a second click re-claims a healthy job.
- **`apmt_credential.py`** — `_mint`'s scope list (`:125`) becomes
  `{"agent_slugs": [AGENT_SLUG, WORKFLOW_SLUG]}` (the `/v1` scope check is
  one flat slug allowlist covering agents AND workflows,
  `openai_compat.py::_check_scope:118`). The already-cached key in
  `ctx.secrets` is scoped to the agent only → the first workflow call 403s →
  the existing remint path (`_call_llm_apmt_with_remint`) self-heals it
  **because the new mint carries the wider scope**. Add a test asserting the
  mint body names both slugs.
- **`review.py` / migration `0007_create_review_coding.sql`** — structured
  storage, not notes-text (Frederico: JSON "so we can store in the database
  somehow the values"; `review_tags`' docstring already reserves the v2
  cross-paper matrix):
  - `review_coding`: `id, document_id FK→documents(id), kind
    ('code'|'theme'), label, description, anchors jsonb, agreement,
    position, source ('ai'|'human'), created_at, updated_at`.
  - `review_references`: `id, document_id FK, position, raw text,
    structured jsonb, source, created_at, updated_at`.
  - `ReviewStore` gains list/replace/add/update/delete for both — CRUD
    shape copied from quotes (`review.py:157-224`). AI persist = replace
    rows `WHERE source='ai'`, never touching `source='human'` rows; that is
    the fill-only-empty analogue for lists. AI suggestions for inference
    fields land as `review_notes` rows under `ai_my_take`/
    `ai_anything_else` (allowed via a new `AI_SUGGESTION_KEYS` set on the
    write path; NOT in `REVIEW_FIELDS`, NOT writable via the field PUT
    route).
- **`api/review.py`** — GET `/review` response gains `coding`, `references`
  and the suggestion notes; new CRUD routes for coding rows and reference
  rows mirroring the quotes routes (same `_get_document_or_404` guard).
  MCP surface (`mcp_ingest.py`): extend `get_review`-equivalents the same
  way; `generate_review`/`generate_review_status` signatures unchanged.

### 2e. UI (`ui/src/views/ReviewPane.jsx`)

- New "Coding & themes" section: two editable lists (codes, themes) —
  add/edit/delete rows, save-on-blur per row via the new CRUD routes (the
  quotes UI in this same pane is the precedent; note the existing tags
  input is labelled "Themes section" in `review.py:260` — relabel that
  input "Tags" to avoid collision).
- New "References" collapsible section at the bottom: list of `raw`
  citations, editable/deletable rows.
- Inference fields: collapsed "AI suggestion" block under `my_take` and
  `anything_else` with "copy into field".
- AI Generate button: unchanged start/poll UX; on `done`, also refresh
  coding/references/suggestions and highlight what was written. Surface
  per-leg failure from the job row ("coding leg failed: …") instead of a
  single opaque error.

### 2f. Data flips at delivery (live tenant)

1. `update_agent research-review-generator` → `model_slug:
   bedrock-sonnet-4-5` (resolves sibling card `…81d2…`'s open decision —
   comment there).
2. New agents + workflow arrive via marketplace install/update reconcile
   (provisioner runs at install — create-if-absent, so the new rows seed;
   mind raw-CDN lag up to ~5min on `--update`).
3. Any later graph iteration during dev = `update_workflow` + **mirror the
   final JSON back into `aw-app.json`** (seed-once means the manifest and
   the live row drift unless the Coder keeps this discipline — the manifest
   is what a fresh install gets).

## 3. Untouched

Quotes CRUD, tags, star/title features, the PDF viewer, the `anthropic`
transport escape hatch (`llm_transport=anthropic`, byte-identical), the
descriptive/inference PUT-route gating, the job table schema (0006), the
five existing AP-MT orchestrators, the search-knowledge workflow.

## 4. Rejected alternatives

- **Dispatch the workflow via runners MCP (`run_workflow_async`) or AP-MT
  REST instead of `/v1`** — would need a new transport, new auth path, and
  polling machinery research-search already has for free in
  `call_llm_apmt`; `/v1` runs workflows natively and the 200-trap regex is
  already workflow-aware (`_APMT_RUN_FAILED_RE` matches
  `[workflow <slug> failed]`).
- **A final `collect` transform merging the three legs into one JSON sink**
  — `parse_json` on agent text is brittle (a single stray code fence in one
  leg fails the whole merge node; the transform has no fence-stripping),
  and the app must tolerant-parse anyway. Three sinks + app-side per-leg
  parsing is strictly more robust and makes partial success natural.
  Runner-up; revisit only if another consumer (not this app) needs the
  merged JSON from the workflow itself.
- **Storing coding/themes as markdown text in `review_notes`** — reuses the
  autosave machinery but destroys the structure Frederico explicitly wants
  in the DB and that the v2 cross-paper matrix needs; editing JSON in a
  textarea is worse UX than rows.
- **Writing `my_take` directly** — silently reverses a documented design
  (review.py:44-51) that exists for auditability of the user's own
  analysis; suggestions-beside-the-field answers the ask without the
  reversal. Flagged for Frederico's veto either way.
- **Blocking on the `ANTHROPIC_API_KEY` infra fix** — an already-diagnosed
  external dependency with a confirmed-live substitute
  (`bedrock-sonnet-4-5`, same underlying model); shipping an unrunnable DAG
  to preserve a provider label is the exact failure the card forbids.
- **Three coders on one model with different prompts/temperatures** — not
  independent coding; Activity 3's design point is model diversity
  (sonnet/opus/fable there; two live provider families here).
- **Round-2 refinement in v1** — deferred with a named trigger (§2b).

## 5. What this makes harder later

- **Sink node ids become an app API contract** — renaming `reconcile`/
  `review-fields`/`references` in the workflow breaks the app's parser.
  Document in both places.
- **The field spec lives in two places** (`REVIEW_FIELDS` and the
  `review-fields` node's `input_template` in the manifest) — held together
  by one unit test; adding a review field now means touching both plus the
  arbiter-adjacent UI.
- **Seed-once drift**: the live workflow row and the manifest copy diverge
  the moment someone edits one via `update_workflow` — a standing
  discipline, not enforced by anything.
- **Bedrock becomes a load-bearing dependency** of the review feature; an
  AWS credential rotation now breaks AI Generate. (The anthropic repoint
  back is one `update_agent` per agent if that key ever goes live.)
- **`ai_*` suggestion keys** create a second class of `review_notes` rows
  outside `REVIEW_FIELDS` — any future "iterate all fields" code must know
  the set.

## 6. Risks for the Coders (the non-obvious breakage)

1. **Output truncation mid-JSON is the landmine.** `anthropic-sonnet-4-5`'s
   row caps `max_tokens` at 4096 and the bedrock/openai rows' params are
   `{}` (provider defaults, unverified). An arbiter output with anchors, or
   a 60-entry reference list, can exceed a 4096-token ceiling and truncate
   → leg parse failure that looks like a model problem. Verify each chosen
   model row's effective output ceiling and set `params.max_tokens`
   explicitly (≥8192 for arbiter/references) — the C4 precedent (80→16000)
   is this exact trap.
2. **Smoke-test every model slug via `/v1` before wiring** — only
   `bedrock-sonnet-4-5` is CONFIRMED live; `bedrock-opus-4-1` and
   `openai-gpt-5-4` are inferred live (same provider credentials as
   confirmed-working slugs). Bedrock model access is granted per-model in
   AWS; a 1-prompt `agent/`-less check per slug costs cents and prevents a
   dead coder node. If one is dead, swap the agent's `model_slug` — the
   workflow JSON doesn't change.
3. **One dead coder kills the coding leg** (arbiter `needs` all three →
   skipped), but review-fields/references still land — that's the intended
   partial-success path. Make the job-row error name the failed node from
   the sink dict's `null`, don't report "generate failed".
4. **The cached ApiKey will 403 the first workflow call** (scoped to the
   agent only). That's the designed remint path — but only after `_mint`'s
   scope list includes the workflow slug. If you see a second 403 after
   remint, the scope change didn't land; don't loop.
5. **Heartbeat/timeout mismatch** (§2d): without the periodic heartbeat
   touch, every DAG run >120s reports `stale` and invites a duplicate
   claim; without the raised HTTP timeout, httpx aborts a healthy run. The
   existing tests won't catch either — they mock the transport. Add a test
   for the heartbeat toucher, and live-verify one real run end to end.
6. **`{input}` goes to five nodes** — the paper text is charged ~5×. Keep
   `graph.max_tokens` explicit (600k) or `hops.DEFAULT_MAX_TOKENS` may
   mark later waves skipped on long papers; `BudgetExceeded` surfaces as
   skipped nodes, not an error.
7. **Workflow runs produce `Run` rows under the ad-hoc `/v1` target** —
   fine, but it means RunDetail is where live debugging happens; the dag
   nodes light up from `node_start`/`node_end` events with zero extra work.
8. **Release discipline**: version bump belongs to the release bot; tests
   run as the release gate from `tests/test_*.py`; green CI ≠ live —
   verify in the running workspace (marketplace update + a real
   `generate_review` against `arxiv_2508.02999`, the paper both prior runs
   used).

## 7. Acceptance (what QA should check)

1. Live `generate_review` on `arxiv_2508.02999` reaches `done` with
   `fields_written` covering descriptive fields, coding rows (`kind` code
   AND theme, `source='ai'`), ≥10 `review_references` rows, and both
   `ai_*` suggestion notes; AP-MT RunDetail shows the dag with 3 coder
   nodes + arbiter + 2 leg nodes all green.
2. Editing a code row by hand, re-running with `overwrite=false`: the
   human row survives.
3. `llm_model=agent/research-review-generator` config override still
   produces the legacy one-shot behaviour (fields only).
4. The pane renders coding/themes/references sections and the suggestion
   blocks; "copy into field" fills `my_take` only on click.
5. Unit suite green locally AND in the release gate; the manifest/
   `REVIEW_FIELDS` consistency test exists and fails if a field key is
   added on one side only.
