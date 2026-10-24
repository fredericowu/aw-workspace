# research-search — Google Scholar result URL as a last-resort download fallback

Card: `4035bf3b-9510-81d8-82ba-f1a6221cbec9` (Low, `research-search-mcp`).
Frederico's instruction (Telegram): "Sim, abre e dispara" — if "Open at source"
works manually for a Scholar result, the pipeline should try that same URL
automatically before giving up.

Repo: `repos/aw-app-research-search` (the SOURCE repo, never
`apps/research-search`, which is the installed copy).

## 1. The approach

When an Index of a `google_scholar` row fails its normal download chain, the
app makes one last attempt against the scraped result `url` (the "Open at
source" link), with the SAME content verification upstream already applies to
its Unpaywall fallback. The attempt runs inside the `paper-search` service
process as a new app-owned MCP tool registered by `launch.py`, because that is
the only process where the vendored `paper_search_mcp` package is importable.
The scraped URL is persisted on the failure row so the Failed-imports Retry
can re-attempt it (without that, Retry on a Scholar row is a guaranteed
zero-attempt repeat, see §4). Failures of the fallback get their own failure
class, `fallback_failed`, so they stop reading as "structurally permanent".

Scholar-only by design: other sources already get primary + repository +
Unpaywall attempts; widening the scraped-URL attempt to them is a separate,
deliberate decision later.

## 2. Where it lands

Verified code facts this stands on:

- `paper_search_mcp/academic_platforms/google_scholar.py:132` builds
  `paper_id=f"gs_{hash(url)}"` — Python's salted string hash. The id is
  unstable across service restarts and CANNOT be reversed to the URL.
  `pdf_url=""` is hardcoded at `:137`.
- `paper_search_mcp/server.py:1099` `primary_downloaders` has no
  `google_scholar` key, so `:1131` yields
  `Unsupported source 'google_scholar' for primary download.` with zero
  attempt. Scholar rows usually have no DOI, so the Unpaywall arm no-ops too.
- `paper_search_mcp/server.py:247` `_download_from_url(pdf_url, save_path,
  filename_hint, expected_title, expected_doi)` already does everything the
  fallback needs: 30s httpx GET with redirects, `%PDF-` header check
  (`:319 _looks_like_pdf`), and title/DOI content verification
  (`:279-293`, via `_pdf_matches_expected`) before atomically writing the file.
- The app CANNOT import `paper_search_mcp`: the package lives only in the
  service's private venv (`.data/venv/...`), and the app process is Tier-1
  in-process workspace Python. `research_search_app/launch.py` is the one
  app-owned file that runs inside that venv (run by `scripts/run_server.sh`),
  and it already re-registers tools on the live FastMCP instance
  (`launch.py:96 apply_scihub_gate`).
- The UI already holds `paper.url` ("Open at source",
  `ui/src/views/Catalogues.jsx:97`) and posts the whole paper to
  `startIndex` (`Catalogues.jsx:47`, `ui/src/api.js:96`).
- Retry posts only `{source, paper_id}` (`ui/src/views/Corpus.jsx:268`).
- `store.py:349 record_failure` inserts `(source, paper_id, reason,
  attempted_at)`; migrations end at `0010_theme_converge.sql`.

### 2.1 `research_search_app/launch.py` — new tool `download_paper_from_url`

New function `register_url_fallback_tool(mcp, upstream)`, called from
`main()` AFTER `apply_scihub_gate`. **Do not touch `apply_scihub_gate` or its
assertions** — the gate is a security surface and must stay byte-stable.

- Shape-check `upstream._download_from_url` exists and its signature has
  `pdf_url, save_path, filename_hint, expected_title, expected_doi`. On
  mismatch: log loudly and SKIP registration. Unlike the Sci-Hub gate this is
  a feature, not a security property — degrade, never exit non-zero.
- Tool `download_paper_from_url(url, expected_title, save_path="./downloads",
  filename_hint="paper", expected_doi="") -> str`. REQUIRES non-empty
  `expected_title` (return an error string if empty): verification is always
  on, so the tool cannot be used as a generic unverified fetcher through the
  gateway.
- Contract mirrors `download_with_fallback`: returns a path on success or a
  human-readable error string on failure (`_download_from_url` returns `None`
  without detail; compose e.g. `"URL fallback failed: not a PDF, unreachable,
  or content did not match expected title/DOI (url=...)"` — detail is in the
  service log). Callers check `is_file`, never pattern-match.
- `launch.py` must stay import-free of `research_search_app` (its own
  docstring rule, lines 4-11).

### 2.2 `research_search_app/upstream_client.py`

`download_paper_from_url(config, *, url, filename_hint, expected_title,
expected_doi="") -> str` — same `_call_tool` path, same absolute
`save_path=str(paths.downloads_dir())` convention as
`download_with_fallback` (`upstream_client.py:118-138`).

### 2.3 `research_search_app/api/corpus.py`

- `IndexRequest` (`corpus.py:119`) gains `url: str | None = None`
  (max_length 2048).
- `_run_index` (`corpus.py:129`) gains `url`. On the existing
  `if not is_file:` failure path (`corpus.py:167`), when
  `source == "google_scholar"` and `url` and `title` are present: call the
  fallback, run the SAME relative-path-resolution + `is_file` block on its
  result (`corpus.py:160-170` — that block exists for a reason, reuse it,
  don't duplicate a weaker check). Success → continue into
  `ingest.index_paper` and the graph calls exactly as the primary-success
  path. Failure → `record_failure` with reason prefixed by the stable marker
  `google_scholar url fallback failed` (then url and the original
  `download_with_fallback` error appended).
- Every `record_failure` call site in `_run_index` passes `url` when present.
- `start_index` threads `body.url` through.

### 2.4 `research_search_app/store.py` + migration

- `record_failure(source, paper_id, reason, url=None)`; `latest_failure` and
  `list_unresolved_failures` return `url`.
- New migration (next free number; `0011` is nominally free but the Neo4j
  vector-move design reserves "migration 0011 drops chunks/figures" — take
  the next free number at merge time and flag the renumber on whichever card
  loses): additive nullable TEXT `url` column on `ingest_failures`.
  **The `.sql` file must contain NO `:word` sequences anywhere, comments
  included** (SQLAlchemy `text()` eats them; the failure is logged, not
  fatal, and then every INSERT naming the column dies inside a background
  task — see risks).
- `classify_failure_reason` (`store.py:149`): new FIRST-precedence branch,
  marker `google_scholar url fallback failed` → `"fallback_failed"`. It must
  run before the `"Unsupported source"` substring check because the composed
  reason embeds upstream's text. Pin the ordering with a test. The 6 live
  pinned reasons must keep classifying exactly as today.

Classifier decision (card question 4): a 4th class value, not a relabel.
`unsupported_source` keeps meaning "no path exists" for genuinely unsupported
sources (unchanged behavior, unchanged UI note). `transient` would be
dishonest: most fallback failures are paywalled landing pages, not blips.
`fallback_failed` says exactly what happened: we attempted the URL and it did
not yield a verified PDF; Retry is a real re-attempt (sites rate-limit and
intermittently serve), so it gets the normal-prominence button and no "will
likely fail the same way" note. Old Scholar rows with no persisted url keep
their verbatim upstream reason → stay `unsupported_source`, which stays
honest: retrying those IS a zero-attempt repeat.

### 2.5 UI (`ui/src/api.js`, `ui/src/views/Corpus.jsx`)

- `api.js:96 startIndex` adds `url: paper.url || null` (Retry's
  `startIndex({source, paper_id})` then naturally sends null unless given).
- `Corpus.jsx:264 handleRetry` passes `url: failure.url || null` (and
  `doi`/`title` are NOT persisted — out of scope, pre-existing gap).
- `FAILURE_CLASS_LABEL`/`failureClassChip` (`Corpus.jsx:203-213`) gain
  `fallback_failed` → label "fallback failed", `chip chip-warn`. The
  `isUnsupported` note logic is untouched.

### 2.6 Tests

- Wire test (clone the `tests/test_scihub_gate_wire.py` pattern):
  `download_paper_from_url` present in `tools/list`, refuses empty
  `expected_title`, and the Sci-Hub gate assertions still hold unchanged.
  This test is what makes an upstream SHA bump that reshapes
  `_download_from_url` go red instead of silently dropping the feature
  (degrade-at-boot is deliberate; degrade-undetected is not).
- `classify_failure_reason`: new marker cases + marker-beats-"Unsupported
  source" ordering + the 6 live reasons unchanged.
- `_run_index` (extend `tests/test_api_corpus.py`): scholar+url+failed
  primary → fallback called, its success ingests; non-scholar, or no url, or
  no title → fallback NOT called; fallback failure records marker-prefixed
  reason with url persisted.
- Store round-trip: url survives `record_failure` →
  `list_unresolved_failures`.

## 3. Rejected alternatives

- **(a) Patch the vendored package in `.data/venv`** — `installer.py`
  rebuilds the venv with `pip install git+…@<pinned-sha>` on any
  reinstall/update; the patch is silently wiped and lives outside git.
- **Fork openags/paper-search-mcp and repoint the installer** — whole-package
  maintenance burden for ~40 lines of need.
- **(c) Upstream PR and wait** — weeks of latency for a Low card. The
  launcher-level registration does not conflict with offering the same change
  upstream later; non-blocking follow-up.
- **Fallback inside `launch.py`'s `download_with_fallback` wrapper (extra
  `url` param)** — one fewer round-trip, but it mixes feature logic into the
  Sci-Hub security gate, whose auditability depends on staying minimal, and
  silently reshapes a widely-exposed gateway tool. Runner-up; would revisit
  if the two-call failure path ever measurably matters (it won't: it only
  runs on already-failed scholar imports).
- **Reimplement verified download app-side (httpx + pypdf)** —
  duplicates `_pdf_matches_expected`; two verifiers drift, and the failure
  mode of a weaker verifier is ingesting the WRONG paper silently, which is
  corpus corruption, strictly worse than a visible failed import.
- **Not persisting the url (first-attempt-only fallback)** — Scholar
  `paper_id` is `gs_{hash(url)}`, salted and irreversible, so Retry could
  never reconstruct the URL; a retry-worthy class whose Retry is structurally
  a no-op is incoherent.

Card question 2 (expected success rate): accept-as-visible-failure,
consistent with the Failed-imports philosophy (card 4035bf3b-9510-817c).
Direct-PDF links (the `[PDF]` badge class) should succeed; publisher landing
pages fail the `%PDF-` check and land as a `fallback_failed` row. The class
chip IS the "might not work" signal; no further UI. Explicitly out of scope
(card guardrail): headless rendering, cookies/session handling, anything
paywall-beating.

## 4. What this makes harder later

- A new gateway-visible tool that GETs caller-chosen URLs from the service
  container (bounded by mandatory title/DOI verification and `net:outbound`
  the service already holds). Widening beyond google_scholar is now a
  one-line temptation in `_run_index`; keep it a deliberate card.
- `fallback_failed` becomes a 4th pinned class value API consumers can
  depend on.
- `ingest_failures` gains its first migration-coupled behavior: Retry
  correctness now depends on the `url` column existing.
- A dependency on upstream's PRIVATE `_download_from_url`. The pinned SHA
  makes it stable today; every future `installer.UPSTREAM_SHA` bump must keep
  the wire test green or the feature silently degrades to today's behavior.

## 5. Risks for the coder

1. **Unguarded `record_failure` + failed migration = jobs stuck "queued"
   forever.** The `record_failure` calls in `_run_index` (`corpus.py:141,
   168, 183`) are not wrapped; if the migration silently failed (it is
   LOGGED, not fatal) the INSERT naming `url` raises inside the background
   task and the poller never sees a failure row. Verify the column exists
   live after install, not just in CI.
2. **Classifier precedence is load-bearing.** The composed reason contains
   "Unsupported source" verbatim; only test-pinned ordering keeps it out of
   the `unsupported_source` bucket.
3. **Gateway zombie-caches tool lists.** After the service restarts with the
   new tool, verify it from a FRESH session; an old session's cached list
   will say it doesn't exist.
4. **`hash(url)` instability is upstream's, leave it.** The same paper
   re-searched after a service restart gets a different `paper_id` and a new
   row that does not shadow the old one. Do not "fix" this here.
5. **`launch.py` discipline.** No imports from `research_search_app`; the
   script runs file-path-style under the private venv. The existing gate
   assertions and `tests/test_scihub_gate_wire.py` must pass unchanged.
6. **Retry poll compare.** `Corpus.jsx` R1 logic compares `attempted_at`;
   the retry path is unchanged, but if you forget to resend `url` the scholar
   retry silently degrades to the zero-attempt path and the test in §2.6
   won't notice unless it covers the UI payload (cover it in `api.js`'s
   shape or an API-level test).
7. Shared-tree conduct: work on master in `repos/aw-app-research-search`,
   never hand-bump the version field (release bot owns it).
