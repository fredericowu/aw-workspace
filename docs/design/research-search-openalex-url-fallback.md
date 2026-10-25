# research-search — generalize the last-resort URL fallback beyond Scholar (OpenAlex first)

Card: `4045bf3b-9510-81ae-928c-c7ebe9846a74` (Medium, `research-search-mcp`).
Parent precedent: `docs/design/research-search-scholar-url-fallback.md` (shipped,
live). Repo: `repos/aw-app-research-search` (SOURCE repo, never
`apps/research-search`). NOTE: the local checkout of that repo is stale at
v0.50.0; the design below cites **origin/master** (v0.55.0), fetch before coding.

## 0. The two facts this design stands on (verified live, 2026-10-25)

**W181715176 has no recoverable source.** Live OpenAlex API response:
`doi: null`, `open_access.is_oa: false`, `oa_url: null`,
`best_oa_location: null`, one single location whose `pdf_url` is null and whose
`landing_page_url` is a Dialnet book page (HTML,
`https://dialnet.unirioja.es/servlet/libro?codigo=180134`). It is a 1990
Spanish print monograph (Muñiz, "Psicometría: teoría y práctica en la
construcción de tests"). No automatic import can ever succeed for it; "solving
this one" is giving the researcher that sentence instead of a generic failure.

**OpenAlex works in general DO carry a reusable URL.** The vendored package
already maps it: `paper_search_mcp/academic_platforms/openalex.py:134-146`
builds `Paper.url` from `primary_location.landing_page_url` and
`Paper.pdf_url` from `primary_location.pdf_url`, falling back to
`open_access.oa_url` when `is_oa`. `Paper.to_dict()` (`paper.py:53-54`)
serializes both, so every consumer of `search_papers`, the UI catalogue AND
the N4 importer agent, already receives `pdf_url` today. Meanwhile
`primary_downloaders` (`paper_search_mcp/server.py:1099-1116`) has no
`openalex` key and `OpenAlexSearcher.download_pdf` raises NotImplementedError,
so the primary arm always yields `Unsupported source 'openalex'`; with no DOI
the Unpaywall arm no-ops, exactly the Scholar wall.

## 1. The approach

Reuse the already-shipped `download_paper_from_url` tool unchanged
(`launch.py:230 register_url_fallback_tool`, mandatory `expected_title`
verification), and point BOTH consumers of the download chain at it:

1. **N4 agentic path (where this failure actually happened).** The
   `research-reference-importer` agent gains `download_paper_from_url` as its
   sixth tool, and its prompt gains one step: on a CONFIRMED match whose
   `download_with_fallback` fails, if the matched record carries a `pdf_url`
   (or, failing that, a `url`), try `download_paper_from_url` with the
   matched record's own title as `expected_title`; if no URL exists or the
   attempt fails, bail via `graph_import_report` with a detail that says
   plainly whether an open-access copy exists at all. The N4 design rejected
   widening this agent's tool scope "until a real bail transcript shows a
   need" (`research-search-lazy-import-n4.md` §3 rejection 6); this card is
   that transcript.

2. **UI Index/Retry path.** `_run_index`'s scholar-only fallback gate
   (`api/corpus.py:187`) generalizes to a named app-side set,
   `URL_FALLBACK_SOURCES = frozenset({"google_scholar", "openalex"})`. This
   set IS the bridge Frederico asked for: the next source hitting the same
   "unsupported primary + no DOI" wall is one entry away. The attempted URL
   becomes `pdf_url or url` (one attempt, not two), with `pdf_url` newly
   threaded from the UI.

No new download mechanism, no new migration (`ingest_failures.url` and
`.title` already exist), no change to `launch.py`, `upstream_client.py`, or
the Sci-Hub gate.

## 2. Where it lands (all paths on origin/master)

### 2.1 `aw-app.json` — the contributed agent (the fix for Frederico's case)

- `contributes.agents.agents[research-reference-importer].system_prompt`:
  - "exactly five tools" becomes six, adding `download_paper_from_url`.
  - New step between today's steps 1 and 2 of ON A CONFIRMED MATCH: if
    `download_with_fallback` did not return an existing file, and the matched
    candidate has `pdf_url` or `url`, call
    `download_paper_from_url(url=<pdf_url, else url>, expected_title=<the
    MATCHED candidate's title>, filename_hint=<paper_id>)` and continue to
    `index_paper` with its returned path if it succeeds. Default `save_path`
    is correct: the service cwd IS the durable download root
    (`scripts/run_server.sh:16-22,57`) and `mcp_ingest.index_paper` resolves
    relative paths against it (`mcp_ingest.py:401-408`).
  - New bail guidance replacing the bare "pass the error through verbatim"
    for download failures: distinguish three cases in `detail`, since the
    detail is what the researcher reads on the red node:
    (a) the catalogue record is closed-access with no DOI and no open-access
    URL anywhere in its metadata: say "no open-access copy exists" and name
    the landing page so the human can chase a print/library copy;
    (b) an OA URL existed but did not yield a verified PDF: say which URL was
    tried and that it failed verification or download;
    (c) a genuine tool/transport error: pass it through verbatim.
  - `expected_title` MUST be the matched candidate's title, not `ref.title`
    (the citation string differs in accents/subtitles; the candidate title is
    the identity the agent just verified).
- Same file, the agent's `description` sentence "The ONLY tool-using agent"
  stays true; no other agent changes.
- Contributed agents are RECONCILED on marketplace update, not seed-once, so
  this ships with the normal release; verify the live AP-MT agent row shows
  the new prompt after the update.

### 2.2 Gateway profile — a LIVE config edit, not a repo change

`/opt/aw-workspace/.aw-workspace/data/mcp-gateway/gateway.json`,
`configs.research-reference-importer.tools_allow` gains
`"download_paper_from_url"`. Nothing in the repo writes this file (verified:
no `*.py` on origin/master touches gateway configs); it was hand-provisioned
when N4 shipped. Without this edit the new prompt instructs the agent to call
a tool its profile denies, and the feature ships green and invisible. Record
the edit in the card comment, and verify from a FRESH agent session (the
gateway zombie-caches tool lists per session).

### 2.3 `research_search_app/api/corpus.py`

- `IndexRequest` (:119) gains `pdf_url: str | None = Field(default=None,
  max_length=2048)`.
- `_run_index` (:164) gains `pdf_url: str | None = None`; `start_index`
  (:251) threads it.
- The fallback branch (:187): gate becomes
  `source in store_mod.URL_FALLBACK_SOURCES and (pdf_url or url) and title`;
  the attempted URL is `fallback_url = pdf_url or url`; every
  `record_failure` in the branch persists `url=fallback_url` so Retry
  (which resends `failure.url` as `url`, `pdf_url` absent) re-attempts the
  same URL with zero further changes to the retry path.
- The recorded reason uses the marker per 2.4:
  `f"{source} {store_mod.FALLBACK_FAILED_MARKER} (url={fallback_url}): {downloaded}"`.

### 2.4 `research_search_app/store.py`

- `FALLBACK_FAILED_MARKER` (:156) changes from
  `"google_scholar url fallback failed"` to `"url fallback failed"`. This is
  deliberately a SUBSTRING of the old recorded reasons, so every existing
  scholar failure row keeps classifying `fallback_failed` with no data
  migration. `classify_failure_reason` (:159-179) is otherwise unchanged;
  the marker check stays FIRST, ahead of the `"Unsupported source"`
  substring, same load-bearing precedence as before.
- New `URL_FALLBACK_SOURCES = frozenset({"google_scholar", "openalex"})`
  beside the marker, with a comment naming it as the widening seam.

### 2.5 UI

- `ui/src/api.js:96 startIndex` adds `pdf_url: paper.pdf_url || null`.
- `ui/src/views/Corpus.jsx` `handleRetry` and the failure-class chips are
  UNTOUCHED: `fallback_failed` already renders, and Retry already resends
  `failure.url`.

### 2.6 Tests

- Extend `tests/test_api_corpus.py`: openalex + pdf_url + failed primary
  calls the fallback with pdf_url; openalex with only landing `url` uses it;
  openalex with neither records the plain upstream reason (no fallback
  call); scholar behavior byte-identical to today; an unlisted source
  (e.g. `arxiv`) with a url never triggers the fallback.
- Extend the `classify_failure_reason` tests: new marker classifies; an OLD
  recorded scholar reason string still classifies `fallback_failed`;
  marker-beats-"Unsupported source" ordering stays pinned; the 6 live
  pinned reasons unchanged.
- `tests/test_url_fallback_tool_wire.py` must pass UNCHANGED (the tool
  contract is not touched).
- Store round-trip for url persistence already exists; keep green.

## 3. What was rejected, and why

- **Adding an `openalex` entry to upstream's `primary_downloaders`** (vendored
  venv patch, fork, or upstream PR): identical rejection to the Scholar card,
  the venv is rebuilt from a pinned SHA on any reinstall and the patch
  silently dies; upstream even raises NotImplementedError on purpose there.
- **Widening `_run_index`'s fallback to ALL sources**: appends a doomed ~30s
  verified GET to every real failure of the 16 sources that already have
  primary + repository + Unpaywall arms, and re-opens the DSpace
  splash-page class with near-zero yield. The named set keeps each widening
  a one-line, reviewed decision (and is exactly the "bridge" asked for).
- **Two-URL attempts (pdf_url then landing url) per failure**: doubles
  worst-case latency for a tail of landing pages that are almost never PDFs
  (this card's own Dialnet page is the example); `pdf_url or url` picks the
  best single lead. Runner-up; revisit only with evidence of a real class
  where the landing page succeeds after pdf_url fails.
- **Backend pre-fetching `oa_url` into the N4 dispatch input**: dead on
  arrival; at dispatch time the work is an unresolved citation string, the
  OpenAlex record only exists after the AGENT resolves it. The URL must come
  from the agent's own `search_papers` result, which already carries it.
- **A new import_jobs state or failure class for "no OA copy exists"**:
  `graph_import_report`'s free-text `detail` already reaches the node
  tooltip; a new enum is schema churn with no second reader. The honesty fix
  is prompt-side wording, not state-machine surface.

## 4. What this makes harder later

- The agent's capability now spans TWO reconcile cycles: the prompt lives in
  the repo (reconciled on app update) but its tool allowlist lives in a
  hand-edited `gateway.json`. They can drift; any future tool added to the
  prompt must repeat the live edit, and a gateway config rebuild from
  scratch would silently re-break this (there is no reconciler to re-assert
  it). Worth a one-line note in the N4 doc's gotchas if the coder touches it.
- `URL_FALLBACK_SOURCES` is an app-side shadow of upstream's
  `primary_downloaders` dict. An upstream release that ADDS an openalex
  downloader makes our fallback a redundant (harmless, still verified)
  second attempt; a release that RENAMES a source quietly disables its
  fallback. The pinned `installer.UPSTREAM_SHA` makes both visible-at-bump.
- The generalized marker means `fallback_failed` no longer implies Scholar;
  any consumer that inferred source from class (none known today) breaks.

## 5. Risks for the Coder

1. **The gateway.json edit is the whole feature for the N4 path.** Skip it
   and everything is green while the agent's tool call is denied (recorded
   failure mode: allowlist-gated features ship green and invisible). Verify
   the tool is visible through the `research-reference-importer` profile
   from a fresh session, not by calling it from your own full-surface
   session.
2. **Prompt reconcile verification**: after the marketplace update, read the
   live agent row (AP-MT) and confirm the six-tool prompt actually landed;
   contributed-agent reconcile has bitten before when assumed.
3. **Marker substring compatibility is load-bearing**: the new marker must
   remain a substring of the OLD recorded reasons; pin with a test against a
   verbatim old-format row, not just the new format.
4. **Stale local checkout + dirty shared tree**: local master is v0.50.0
   with unrelated uncommitted edits; fetch origin/master and work in a git
   worktree. Never hand-bump the version (release bot owns it).
5. **Acceptance is end-to-end, twice**: (a) re-click Import on the
   Psicometría node (ref resolved to W181715176) and confirm the node goes
   red with the honest "no open-access copy exists (closed access, no DOI;
   Dialnet landing page only)" detail, not a generic error; (b) one
   openalex-resolved reference that DOES carry an `oa_url`/`pdf_url` imports
   end to end through the new fallback (UI path is acceptable if no gray
   node with an OA-carrying target is handy). Unit tests alone do not close
   this card's loop.
6. **`expected_title` source**: in the agent prompt, bind it explicitly to
   the matched candidate's title. Binding it to `ref.title` makes the
   verification reject correct downloads whose PDF header matches the
   canonical title but not the citation string.
