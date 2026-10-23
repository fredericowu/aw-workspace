# research-search: Literature Review DAG writes in the paper's own language

**Card:** `4025bf3b-9510-81c3-aa4f-f51b524c2b17`.
**Builds on** the Literature Review DAG design
(`docs/design/research-search-literature-review-dag.md`, card
`4025bf3b-9510-816a-8b9e-f080322851c9`, shipped research-search-mcp v0.30.0).
Trigger: PT paper `crossref_10.21814_rpe.9032` got an all-English review;
no language instruction exists anywhere in the DAG's prompts today.

## 1. The decision

**Detect the source paper's language ONCE, app-side, in Python — then carry
it into every DAG leg through the only dynamic channel the engine has: the
`{input}` string — as an app-owned `=== OUTPUT LANGUAGE ===` header above an
app-owned `=== PAPER ===` marker.** All five `input_template`s in
`aw-app.json` are edited to (a) stop emitting their own `=== PAPER ===`
marker (it moves into `{input}`), and (b) carry a one-sentence language
instruction. Code/theme **labels stay translated prose** (Frederico's manual
fix is the precedent, and the UI renders `label` as the primary editable
text), and the fragile theme→code **label-string cross-reference gains an
id-based resolution at persist time** (`code_ids` beside the existing
`codes` label snapshot — no migration, anchors is jsonb). Fallback: the
detector's majority language; English only when detection fails or is
below confidence, logged.

Zero AP-MT changes, zero DB migrations, sink ids untouched.

## 2. Where it lands

Repo: `repos/aw-app-research-search` (target `research-search-mcp`).

### 2a. Detection — `research_search_app/review_generate.py`

The DAG path already builds its input at
`review_generate.py:741-747` (`run_generate_job`: `llm_input =
prepare_input_text_full(...)`, bare paper text, "the DAG's node
input_templates already carry every instruction"). New pieces:

* `detect_paper_language(trimmed_text) -> tuple[code, name]` — e.g.
  `("pt", "Portuguese")`. Implementation: **`langdetect`** (pure-python;
  add to `runtime.pip_requires` in `aw-app.json`, currently
  `["fastembed", "edge-tts==7.3.1"]`), with `DetectorFactory.seed = 0`
  (module-level) for determinism.
* **Sample to dodge the English front matter**: many non-English papers
  (incl. the trigger paper) lead with an English abstract. Concatenate three
  ~3,000-char slices at ~10%/50%/90% of the *trimmed* text and run
  `detect_langs` on that, not on the head.
* Accept the top candidate when `prob >= 0.70`; on lower confidence, empty/
  tiny text, or `LangDetectException` → `("en", "English")` and
  `log.warning` with document_id + what was detected. A small
  `LANG_NAMES` dict maps langdetect's ISO codes to English names
  ("Portuguese", "Spanish", …); unknown code falls back to the code itself
  in the header ("language code 'xx'").
* `run_generate_job` (DAG branch) builds:

  ```
  === OUTPUT LANGUAGE ===
  Portuguese (pt)

  === PAPER ===
  <prepare_input_text_full(...) text>
  ```

  Header strings live as module constants next to `SINK_*`
  (`review_generate.py:93-99`) — this is the same kind of app↔manifest
  contract as the sink ids and must be documented as such.
* **Legacy/anthropic parity**: `build_prompt` (`review_generate.py:272`)
  gains a `language=(code, name)` arg — same instruction sentence + the
  answers-in-this-language rule — so the one-shot
  `agent/research-review-generator` escape hatch and the `anthropic`
  transport don't regress. Detection runs once in `run_generate_job` for
  both branches (it already has `full_markdown` in hand; detect on the
  trimmed text).

### 2b. Threading — the five `input_template`s in `aw-app.json`

`{input}`/`{prev}`/`{node:x}` are the engine's only template variables, so
the language can only arrive inside `{input}`. Template edits
(`contributes.agents.workflows[0].graph.nodes`):

* **code-a / code-b / code-c** (identical ×3) and **reconcile**: replace the
  trailing `=== PAPER ===\n{input}` with `{input}` and add:
  *"Write every human-readable value — code and theme labels, descriptions,
  and the suggestion fields — in the OUTPUT LANGUAGE declared in the
  `=== OUTPUT LANGUAGE ===` block above the paper text (this is the paper's
  own language). JSON keys and the `agreement` enum values stay exactly as
  specified, in English. Never translate verbatim quotes — reproduce them
  exactly as printed."*
* **reconcile** additionally: *"Every entry in a theme's `codes` array must
  byte-for-byte match a `code` label you emit in this same answer."* (Labels
  are now localized prose; the exact-match discipline the persist step
  resolves ids from matters more.)
* **review-fields**: same marker move; instruction scoped to field answers:
  *"Write every field answer in the OUTPUT LANGUAGE declared above the
  paper."* JSON keys unchanged (the existing "keys are exactly [...]" line
  already pins them).
* **references**: same marker move; the **opposite** instruction, explicit:
  *"Ignore the OUTPUT LANGUAGE header for this task: reproduce every
  citation exactly as printed in the paper — never translate any part of a
  reference."* (Card scope: citations stay in their original language —
  standard practice, not a bug.)

### 2c. Labels & cross-references — `review.py` + `ReviewPane.jsx`

* **Labels = translated prose.** `ReviewPane.jsx` renders `coding.label` as
  the primary editable input (`ui/src/views/ReviewPane.jsx:571-584`); a
  stable English slug there would defeat the request on-screen.
* **`replace_ai_coding` (`review.py:330`) resolves theme→code ids at
  persist time**: insert the code rows first, read their fresh ids back
  (`list_coding` — no RETURNING, per this repo's DbTables rule), then insert
  theme rows with anchors
  `{"codes": [<labels, display/compat snapshot>], "code_ids": [<ids>],
  "sources": [...]}`. A theme label that doesn't match any just-inserted
  code (or matches two) keeps its string in `codes` and is omitted from
  `code_ids` — graceful, never fails the persist.
* **UI**: the theme row's `codes:` line
  (`ReviewPane.jsx:583-584`) prefers resolving `anchors.code_ids` against
  the loaded coding state (so a human renaming a code updates every theme's
  chip on next render — the exact fragility Frederico hit by hand), falling
  back to `anchors.codes` strings for legacy rows, **including the
  hand-fixed PT document**, which must keep rendering correctly unmodified.
* No write-path UI change: there is no UI today for editing a theme's
  member codes, only label/description.

### 2d. Tests

* `tests/test_literature_review_workflow_manifest.py` — already pins
  template content (field keys, sink ids). Add: every agent node's template
  contains the OUTPUT LANGUAGE instruction; the references node contains
  the never-translate instruction; **no template still contains
  `=== PAPER ===`** (the marker is app-owned now — a template that kept it
  would double it).
* `tests/test_review_generate.py` — `detect_paper_language` units (clear PT
  sample → pt; clear EN → en; garbage/short → en fallback); DAG
  `llm_input` carries both markers and the detected language; `build_prompt`
  carries the language line.
* `tests/test_store.py` / `test_review_api.py` — `replace_ai_coding`
  resolves `code_ids`, keeps `codes`, and handles an unmatched label.

### Untouched

`parse_dag_response` and its envelope unwrap (fixed on the sibling card),
sink ids, job table, credential machinery, fill-only-empty discipline,
`review_references` handling (no translation), migrations, AP-MT.

## 3. Rejected alternatives

1. **Per-leg self-detection** ("write in the paper's language" in each
   template, no header). Zero app-code change — the runner-up. Rejected:
   five independent detections on three different model families can
   disagree (coder-a answers PT, the mini-model coder-c answers EN — the
   arbiter then merges codebooks in different languages), and the
   mixed-language fallback becomes undefined per leg instead of one policy.
2. **A detect node in the DAG.** Threads cleanly via `{node:detect}`, but
   serializes every leg behind one more model call (today all five prompt
   nodes start immediately from `{input}`), adds a single point of failure
   (a failed `needs` skips every downstream leg → whole-job failure), and
   spends tokens/latency on something Python does deterministically for
   free.
3. **Language from source metadata** (Crossref `language` field etc.).
   Nothing persists a language today — `store.py` and `sources/*.py` have no
   language column/field at all — and coverage is patchy across OpenAlex/
   arXiv/Semantic Scholar. Detection from the indexed markdown covers every
   document uniformly, including everything already indexed. (If a language
   column ever lands, detection becomes its backfill, not its competitor.)
4. **Stable non-localized code labels, only `description` translated.**
   Keeps theme→code refs trivially stable, but the label IS the visible,
   editable primary text in the UI — an English slug on a Portuguese review
   fails the actual request, and the manual fix already set the
   labels-are-prose precedent.
5. **Id-only cross-references (drop the `codes` label array).** Breaks every
   existing row — including the hand-translated trigger document — for no
   gain over dual-write with fallback.
6. **A `language` override param/config in v1.** Cheap, but it's scope the
   PO didn't grant and the trigger case doesn't need; noted as the obvious
   follow-up if misdetection ever shows up in practice.

## 4. What this makes harder later

* **The `{input}` header becomes a contract.** Any other caller of
  `workflow/literature-review` that passes bare paper text now feeds
  templates that reference a header that isn't there (degrades to
  paper-language-by-imitation, not broken — but a second caller must build
  the same envelope; the constants in `review_generate.py` are the one
  place that knows how).
* **`langdetect` is a new runtime dep** in an inprocess (Tier-1) app — pure
  python, but it loads language profiles at import; pin the version.
* **Dual anchors representation** (`codes` labels + `code_ids`) persists
  until legacy rows age out; the UI fallback can't be deleted casually.
* **Localized labels close the door on cross-paper label-string grouping**
  (the future v2 matrix): aggregating codes across papers in different
  languages can't key on label text — it will need ids/descriptions or
  embedding grouping anyway, which was always the honest answer.
* **Language is computed per run, not persisted** — a future "document
  language" UI chip or per-corpus language filter needs a column + backfill.

## 5. Risks for the Coder

1. **Reconcile won't overwrite hand-edited rows.** Contributed workflows/
   agents are reconciled at marketplace install, but a field hand-tuned in
   the AP-MT UI is hash-guarded and skipped. After `marketplace install
   --update`, **verify the live workflow row actually carries the new
   templates** (`get_workflow` via the runners MCP) — a green install is not
   proof.
2. **Do NOT casually regenerate `crossref_10.21814_rpe.9032`.** It's
   hand-fixed; fill-only-empty means a regenerate writes nothing to filled
   fields anyway, but `replace_ai_coding` WOULD replace its coding rows.
   QA must use a *different* non-English paper.
3. **Shared tree is dirty.** The repo currently has unrelated uncommitted
   changes (corpus.py, ingest.py, paths.py, …) from another run in flight —
   commit only your own files.
4. **Never hand-bump `version`** — the release bot owns it. UI change means
   rebuilding the committed `ui/dist` per this repo's convention.
5. **Tier-1 reload is flaky** (known open issue): after update, verify the
   new `review_generate.py` is actually the one executing (e.g. log line
   from detection) — a stale in-process module fakes a prompt bug.
6. **langdetect edge cases**: empty/short trimmed text throws
   `LangDetectException` — the fallback path must be exercised by a test,
   not assumed.
7. The manifest test that asserts the review-fields template names exactly
   the generatable fields (`test_review_fields_input_template_names_...`)
   will still pass, but re-run the whole manifest test module — several
   tests pin template substrings that the marker move touches.

## 6. QA plan (for the QA run)

Pick a fresh non-English paper (PT or ES), index it, run AI Generate
(workflow path), then assert: all 9 field answers + code/theme labels +
descriptions + both `ai_*` suggestion notes are in the paper's language;
references are as-printed (not translated); every AI theme row's
`anchors.code_ids` resolves to sibling code rows; the hand-fixed
`crossref_10.21814_rpe.9032` document is byte-identical before/after the
deploy; an English paper still generates English (no regression); the
detection fallback log line appears for a garbage-text fixture (unit level).
