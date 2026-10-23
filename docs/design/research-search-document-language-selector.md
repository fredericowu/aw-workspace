# research-search: Document View language selector (EN/PT/Auto) → AI Generate + paper-to-audio

**Card:** `4025bf3b-9510-81da-a163-db5db0cdfee8`.
**Builds on** the language-match delivery (card
`4025bf3b-9510-81c3-aa4f-f51b524c2b17`, shipped research-search-mcp
v0.31.0, design `research-search-literature-review-language-match.md`):
`detect_paper_language` at `review_generate.py:339`, the
`=== OUTPUT LANGUAGE ===` header at `review_generate.py:114`, and the
single detection call at `review_generate.py:849`.

## 1. The decision

**One per-document scalar — a `preferred_language` text column on the
existing `documents` table (`'auto'` default) — set by a small segmented
selector in `DocumentViewer.jsx`, and resolved SERVER-SIDE by both
consumers.** Neither the AI Generate request body nor the audio-start
request body changes: `POST .../review/generate` and `POST .../audio` read
the stored preference off the document row they already fetch. Explicit
`en`/`pt` skips detection; `auto` runs the exact
`detect_paper_language` the sibling card shipped — no second detector.

For audio, the preference picks the **TTS voice** via a tiny
`VOICE_BY_LANGUAGE` map (`en` → the existing
`en-US-AndrewMultilingualNeural` default, `pt` → a pt-BR neural voice);
an explicitly passed `voice` still wins (back-compat). Because `audio.py`
cannot import `review_generate.py` (it already imports `audio` —
`review_generate.py:61` region uses `audio_mod.trim_markdown`/`spawn`),
the detection function and `LANG_NAMES` **move to a new
`research_search_app/language.py`**, re-exported from `review_generate`
so its public name and tests keep working.

Zero `aw-app.json` changes (the OUTPUT LANGUAGE header already carries
whatever the app decides), zero AP-MT changes, one additive migration.

**The selector controls the language of GENERATED text and the spoken
voice — it does not translate the paper.** Selecting English on a
Portuguese paper writes the review in English but the audio still reads
the paper's own (Portuguese) text, just with the multilingual EN-default
voice. That's the asked-for contract, stated so nobody files it as a bug.

## 2. Where it lands

Repo `repos/aw-app-research-search`, target `research-search-mcp`.

### 2a. New module — `research_search_app/language.py`

Moved verbatim from `review_generate.py`: the `DetectorFactory` seed
(line 70 — determinism MUST move with the function), `LANG_NAMES`
(`:296`), `LANGUAGE_DETECT_MIN_PROB`, `LANGUAGE_DETECT_SLICE_CHARS`,
`_language_detect_sample`, `detect_paper_language` (`:339`). New:

```python
VOICE_BY_LANGUAGE = {"en": DEFAULT_VOICE-equivalent, "pt": "pt-BR-AntonioNeural"}
def voice_for(code: str) -> str   # unmapped code → the multilingual EN default
```

(`DEFAULT_VOICE` stays defined in `audio.py:41`; `language.py` holds only
the map/lookup, taking the default as an argument or importing from
`audio` — `audio` must not import `review_generate`, and `review_generate`
already imports `audio`, so the import direction is
`review_generate → language` and `audio → language` only if `language`
imports neither. Keep `language.py` import-free of both; `voice_for`
takes `default` as a parameter.) Coder verifies the pt voice id against
`edge-tts --list-voices | grep pt-BR` (edge-tts==7.2.8 pinned behavior,
`audio.py:47` comment).

`review_generate.py` re-imports `detect_paper_language` / `LANG_NAMES`
into its own namespace (existing references at `:849` and
`tests/test_review_generate.py` keep resolving).

### 2b. Persistence — migration + store

* `migrations/0008_add_document_preferred_language.sql`:
  `ALTER TABLE "app__research-search__documents" ADD COLUMN IF NOT EXISTS
  preferred_language text NOT NULL DEFAULT 'auto';` — mirror of
  `0004_add_document_starred.sql` including its key discipline: the column
  is deliberately NOT in `replace_document`'s upsert column list, so
  re-indexing never resets a chosen language.
* `store.py:333-348` `get_document`: add the column to the SELECT and the
  returned dict. (`list_documents`/the corpus list response don't need it.)

### 2c. Routes — `api/corpus.py` (beside `/star`)

* `POST /corpus/documents/{id}/language` body `{language}` — accept
  `'auto'` or any code in `LANG_NAMES` (UI only offers en/pt/auto; backend
  stays general), 400 otherwise, 404 via the existing `get_document`
  guard. `GET /corpus/documents/{id}/language` returns
  `{preferred_language}` (DocumentViewer fetches no document JSON today —
  `DocumentViewer.jsx:118-165` only builds URLs from `documentId` — so a
  read route is needed).
* `start_document_audio` (`api/corpus.py:281`): after the existing
  `voice = body.voice or DEFAULT_VOICE` line, the resolution becomes:
  explicit `body.voice` → unchanged; else stored pref `en`/`pt` →
  `voice_for(pref)`; else (`auto`) → pass `voice=None` down and let the
  job resolve it post-convert (next bullet). Sidecar written at the route
  (`:316-320`) uses the literal string `"auto"` as placeholder voice,
  overwritten once resolved.

### 2d. Audio job — `audio.py` + `mcp_ingest.py`

* `run_web_audio_job` (`audio.py:551`): accept `voice: str | None`. When
  `None`, after `convert_and_chunk` returns, detect on
  `"\n\n".join(result["chunks"])` via `language.detect_paper_language`,
  map with `voice_for`, write the resolved voice back into the sidecar
  (alongside the existing `_set_totals` update at `:566`), then pass it to
  `run_synthesis_job`.
* `_paper_to_audio` (`mcp_ingest.py:360`): new optional `language` arg
  (`'auto'`/code, default `'auto'`); explicit `voice` arg still wins;
  conversion is already inline there (`:403`), so resolution happens in
  place. **Deliberate behavior change:** the MCP default moves from
  always-Andrew to auto-detected voice. `mcp_config.py`'s tool schema for
  `paper_to_audio` documents the new arg.

### 2e. Consumers of the stored pref — AI Generate

* `start_generate_job` (`review_generate.py:769`) gains
  `preferred_language: str = "auto"`, threaded into `run_generate_job`
  (`:826`), where the existing single detection point (`:849`) becomes:
  pref `en`/`pt` → `(pref, LANG_NAMES[pref])`, no detection; `auto` →
  exactly today's call. Both downstream branches (DAG header `:853`,
  legacy `build_prompt(language=...)`) are already tuple-driven — no
  change below that line.
* `api/review.py:270` `post_review_generate` passes
  `doc.get("preferred_language", "auto")` (it already fetched `doc`);
  `mcp_ingest.py`'s `_generate_review` does the same from its own doc
  fetch. `GenerateBody` (`api/review.py:86`) is unchanged — the stored
  pref is the single source of truth.

### 2f. UI — `DocumentViewer.jsx` + `api.js`

* New `LanguageSelector({ documentId })` at the top of
  `DocumentViewer.jsx` (beside `AudioPanel`, `:136`): three-way segmented
  control (English / Portuguese / Auto) in the existing `viewer-toggle`
  button idiom (`:139-155`). On mount `GET .../language`; on click
  optimistic set + `POST`, brief disabled state while saving so a click on
  AI Generate right after can't race the save.
* `api.js`: `getDocumentLanguage` / `setDocumentLanguage` beside
  `setStarred` (`:117`).
* `ReviewPane.jsx` and `AudioPanel`: **untouched** — server-side
  resolution means their request bodies don't change.

### 2g. Tests

* `test_store.py`: default `'auto'`; survives a re-index
  (`replace_document`) like `starred` does.
* `test_api_corpus.py`: language GET/POST round-trip + 400 on junk;
  audio-start with pref `pt` writes the pt voice into the sidecar;
  explicit `body.voice` still wins.
* `test_audio.py` / `test_audio_wire.py`: `run_web_audio_job(voice=None)`
  detects and updates the sidecar voice (edge_tts mocked as existing
  tests already do).
* `test_review_generate.py`: pref `pt` skips detection and lands
  `Portuguese (pt)` in the header; pref `auto` keeps the existing
  detection tests green; re-export of `detect_paper_language` from
  `review_generate` still importable.
* `test_mcp_ingest.py`: `paper_to_audio` `language` arg.

## 3. Rejected alternatives

1. **Client-side threading** (UI sends language in each generate/audio
   body, React state only) — fails the card's persistence requirement
   (survive reload, available server-side), and the MCP tool path would
   never see it.
2. **A per-workspace config default** instead of per-document — papers
   differ per document; the card is explicit.
3. **A new `app__research-search__document_prefs` table** — one scalar;
   the column-on-documents precedent (`title` 0003, `starred` 0004)
   already solved the re-index/upsert discipline this needs.
4. **A second detector in `audio.py`** — forbidden by the card, and the
   reason `language.py` exists: the `audio ← review_generate` import
   direction makes reuse impossible without the extraction.
5. **Threading language through `aw-app.json` templates or a new DAG
   param** — unnecessary (the `=== OUTPUT LANGUAGE ===` header is already
   the app-owned dynamic channel) and would collide with the concurrent
   model-swap card (`4025bf3b-9510-81b2-b329-fa7158e30d8c`) editing
   `aw-app.json`.

## 4. What this makes harder later

* `'auto'` is a sentinel inside a text column — any future consumer must
  resolve it, never read the column as a literal language.
* Adding a UI language means touching `VOICE_BY_LANGUAGE` + the selector;
  acceptable for a two-language user base, annoying at ten.
* The MCP `paper_to_audio` default now auto-detects the voice — any
  caller that relied on Andrew-for-everything changes behavior.
* `preferred_language` must stay OUT of `replace_document`'s upsert —
  same standing trap `starred` already set; a future column refactor that
  "completes" the upsert column list silently breaks both.

## 5. Risks for the Coder

1. **Import cycle**: `review_generate` imports `audio`; `audio` must only
   import `language`, and `language` must import neither. Get this wrong
   and the app fails at import time, which Tier-2 surfaces as a dead
   container, not a stack trace in your face.
2. **The `DetectorFactory.seed` must move with the function**
   (`review_generate.py:70`) or detection goes nondeterministic and the
   language tests flake.
3. **Dirty shared tree**: `research_search_app/api/corpus.py`,
   `ingest.py`, `paths.py` + two test files carry uncommitted hunks from
   another in-flight run (an `is_within_downloads` refactor). Work in a
   git worktree; never commit those hunks.
4. **Concurrent card in `aw-app.json`** (model swap to
   openai-gpt-5.4/5.5/5.6-sol): this design touches `aw-app.json` not at
   all — keep it that way.
5. **Sidecar voice placeholder**: status polls read the sidecar; until
   post-convert resolution runs, `voice` reads `"auto"` — make sure
   `job_status`/wire tests tolerate it.
6. **No hand version bumps** — the release bot owns the version.
