# research-search — surface + retry failed imports

Card `4035bf3b-9510-817c-8de0-c6da1ef28abe`. Repo `tekflox/aw-app-research-search`.
PO scope: the last comment on the card (2026-10-24T16:47Z) — it overrides the card
body in two places and is restated only where it binds a decision below.

## 1. The approach

A **"Failed imports" section on the existing My corpus view** (`Corpus.jsx`),
fed by one new read endpoint and one new dismiss endpoint, with Retry reusing
the existing `POST /corpus/index` → `_run_index` path unchanged. No schema
migration, no new table, no new tab, no new MCP tool. `import_jobs` is left
alone. Dismiss is a hard DELETE of every `ingest_failures` row for the
`(source, paper_id)` pair. Classification is a server-side pure function over
the verbatim `reason` string, computed at read time, stored nowhere.

Everything durable already exists: the failures are in Postgres
(`app__research-search__ingest_failures`), success-shadowing is already a join
away (`documents` has `UNIQUE (source, paper_id)`,
`migrations/0001_create_documents_and_chunks.sql:38`), and the retry path is
the import path. This card is one list query, one delete, one additive field
on an existing response, and one UI section.

## 2. Where it lands

### 2.1 Store — `research_search_app/store.py`

Next to the existing failure helpers (`record_failure` :308,
`latest_failure` :316):

* **`VectorStore.list_unresolved_failures(limit=200) -> list[dict]`** — one
  entry per `(source, paper_id)` that has at least one `ingest_failures` row
  and **no** `documents` row, newest failure per pair, newest first overall:

  ```sql
  SELECT id, source, paper_id, reason, attempted_at FROM (
      SELECT DISTINCT ON (f.source, f.paper_id)
             f.id, f.source, f.paper_id, f.reason, f.attempted_at
      FROM {table} f
      LEFT JOIN {table:app__research-search__documents} d
             ON d.source = f.source AND d.paper_id = f.paper_id
      WHERE d.id IS NULL
      ORDER BY f.source, f.paper_id, f.attempted_at DESC
  ) sub ORDER BY attempted_at DESC LIMIT :limit
  ```

  The two-table placeholder syntax follows the exact precedent at
  `graph_db.py:358` (`{table:app__research-search__ref_targets}`). Table names
  carry a hyphen — never spell them literally in SQL, always via the
  placeholder. The join is exact because `documents` is unique on
  `(source, paper_id)`; a pair whose paper was later indexed (including one
  still stamped `pending-reembed`, i.e. mid-reindex) never appears, which is
  the PO's "later success shadows the failure" criterion with no resolution
  flag needed.

* **`VectorStore.dismiss_failures(source, paper_id) -> int`** — count rows for
  the pair, `DELETE FROM {table} WHERE source = :source AND paper_id =
  :paper_id`, return the count. Count-then-delete, NOT `DELETE ... RETURNING`:
  `DbTables.execute` only hands rows back when the statement's first word is
  `select` — the gotcha is documented in this repo four times
  (`review.py:37`, `graph_identity.py:48`, `review_generate.py:45`,
  `theme_converge.py:593`). Same shape as `delete_quote` (`review.py:257`).
  Deleting the whole pair, not one `id`, is deliberate: the table has no
  unique key per pair, so deleting only the newest row would resurrect the
  previous stale failure in both this list and `latest_failure`'s poll.

* **`classify_failure_reason(reason: str) -> str`** — pure function returning
  `"unsupported_source" | "transient" | "unknown"`. Contract, in precedence
  order: contains `"Unsupported source"` (the verbatim prefix the live
  google_scholar rows carry from `download_with_fallback`'s error string) →
  `unsupported_source`; else case-insensitive match on a short transient list
  (`certificate`, `timeout`, `timed out`, `temporarily`, `connection
  refused/reset/error`, `ssl`) → `transient`; else `unknown`. Never raises.
  Must classify the 6 live rows exactly as the PO's table does: rows 2–5
  unsupported_source, row 6 transient (its reason contains "TLS CA certificate
  bundle"), row 1 (`./downloads/2605.26355v1.pdf`, a path, not an error) →
  unknown. Unit tests pin those six verbatim strings as fixtures.

### 2.2 API — `research_search_app/api/corpus.py`

* **`GET /corpus/failures`** → `{"failures": [{id, source, paper_id, reason,
  attempted_at, class}], "count": N}`. Thin wrapper over
  `list_unresolved_failures` + `classify_failure_reason`. `reason` is the row
  verbatim — no trimming, no rewriting (PO hard constraint: these strings are
  the only diagnostic anyone gets). Class is computed server-side so a future
  REST/MCP consumer gets the same label the UI shows.

* **`DELETE /corpus/failures`** with query params `source`, `paper_id` →
  `{"deleted": n}` (404 when n == 0, so a double-dismiss is visible rather
  than silently idempotent).

* **One additive change to `index_job_status` (:208)**: the `failed` branch
  (:224) also returns `attempted_at` from the `latest_failure` dict it already
  holds. This is what makes a retry *visibly resolve* (see risk R1) —
  without it a retrying row cannot tell the stale failure from the new one.
  Additive; `Catalogues.jsx` ignores unknown fields.

* **Retry is NOT a new endpoint.** The UI calls the existing
  `POST /corpus/index` (:196) with `{source, paper_id}` — the same
  `_run_index` background task, the same recording contract ("every exit path
  lands a `documents` row or an `ingest_failures` row", :126–130). No doi, no
  title (the failure row doesn't store them — see rejected alternative A5 for
  why that's acceptable in v1).

### 2.3 UI — `ui/src/views/Corpus.jsx` + `ui/src/api.js`

* `api.js` gains `listFailures: () => getJSON('/corpus/failures')` and
  `dismissFailure: (source, paperId) => sendJSON('DELETE', '/corpus/failures?...')`
  (or query-param form matching `sendJSON`'s shape — coder's call).
* `Corpus.jsx` gains a **FailedImports** component rendered after
  `CorpusStatus`, loaded with the existing `useAsync`/`AsyncBoundary`
  machinery (`ui/src/components.jsx`). When `count === 0`, render nothing.
  When > 0: a third `Tile` ("failed imports") in the existing `tile-row`, and
  a card list. Each row shows: source `chip`, paper_id (display-truncated to
  ~80 chars with the full value on an expandable element — row 6's paper_id is
  6000 chars and must not wreck the layout; truncating the *paper_id display*
  is fine, truncating the *reason* is not), the verbatim reason in a wrapping
  monospace block, the timestamp, the class chip, and two actions:
  * **Retry** — enabled on every row regardless of class (PO: "a structural
    failure that fails again, cheaply and visibly, is a better outcome than a
    UI that argues with the operator"). Prominence varies: normal button for
    `transient`/`unknown`, low-emphasis button plus a one-line note for
    `unsupported_source` ("this source has no direct download path; retrying
    will likely fail the same way"). On click: remember the row's current
    `attempted_at`, call `api.startIndex({source, paper_id})`, show
    "Retrying…", poll `api.indexJobStatus` every 2s (same cadence as
    `useIndexJobs`, `Catalogues.jsx:17`): `indexed` → refetch the list (row
    disappears); `failed` with `attempted_at !== the remembered one` → new
    failure, refetch (newest reason + timestamp show); `failed` with the SAME
    `attempted_at` → still the stale row, keep showing "Retrying…" and keep
    polling. Comparing timestamps row-vs-row (not against the client clock)
    sidesteps clock skew entirely.
  * **Dismiss** — `api.dismissFailure(...)` then refetch.
* **Not touched: `App.jsx`.** No new tab (there are only two today — Search
  and My corpus; the card's "five views" counts files, three of which mount
  inside DocumentViewer). A failure-count badge on the "My corpus" tab label
  was considered and deferred: it would put this card into `App.jsx`, which
  the in-flight DocGraph work is the most likely file to also touch.
* Rebuild `ui/dist` and commit it (this repo ships a committed dist) — but
  see risk R4 before running the build.

### 2.4 Untouched, explicitly

* `latest_failure` / `index_job_status` state machine — reused, not rebuilt
  (one additive response field only).
* `app__research-search__import_jobs` — **left alone** (decision the PO asked
  for, §4 A1).
* `record_failure`, `_run_index`, `download_with_fallback` — unchanged. The
  two bugs the PO surfaced (relative-path false negative; no `paper_id`
  length validation) are **not** fixed here; they need their own cards.
* MCP surface — no new tool. `index_status` already exposes the
  `ingest_failures` count; agents who need the list can get it when there's a
  demonstrated consumer (that's a new card, not a freebie here).
* No migration. The join works (§2.1), so the PO's "prefer no schema
  migration" stands with no caveat.

## 3. Acceptance mapping (PO §3, checkable today)

* 6 live rows → list shows 6 entries (each pair is distinct), rows 2–5 read
  unsupported_source, row 6 transient, row 1 unknown; reasons verbatim.
* Later-indexed papers never appear — the LEFT JOIN, by construction.
* Retry re-runs the same import path (`POST /corpus/index`) and visibly
  resolves to indexed or to a new failure with a new timestamp (R1 mechanism).
* Dismissing rows 1 and 6 deletes their pairs; they do not come back unless a
  *new* failure event inserts a new row (correct: that's a new event).
* Cold session: open the app → My corpus tab → the section is there. No
  search, no prior state.

## 4. What I rejected, and why

* **A1 — reuse/extend `import_jobs`.** Rejected. It is keyed on `ref_key`
  with `ON DELETE CASCADE` into `ref_targets` (`migrations/0009:171-181`) —
  a catalogue import has no `ref_targets` row, so every Search-tab import
  would need a synthesized identity row just to satisfy the FK. Its state
  machine (`dispatched|running|indexed|failed`) models an agent-dispatched
  job (N4, not yet built), not an append-only failure log; and it has 0 rows
  while `ingest_failures` already holds the data this feature surfaces.
  Bending it here would couple this card to N4's unbuilt semantics for zero
  saved work.
* **A2 — a sixth (third) top-level tab.** Rejected. PO constraint, and the
  app has two tabs; a failures list is corpus-adjacent content with a natural
  home next to the corpus stats tiles.
* **A3 — soft-delete (a `dismissed_at` column or a `dismissals` side
  table).** Rejected. Either is a migration the PO prefers to avoid, and both
  exist only to preserve rows whose entire population today is junk (test
  debris, a false negative) or permanent re-failures. The log's only
  consumers are this list and `latest_failure`; hard delete keeps both
  honest. Cost acknowledged in §5.
* **A4 — scheduled sweep / backoff / attempt counter.** Declined by the PO on
  measured evidence (retry-worthy population today: zero) and I do not think
  the design needs one: the manual path plus the §5 note on measurement is
  sufficient, and the join approach is workable, so neither escape hatch the
  PO left open gets used.
* **A5 — store `doi`/`title` on the failure row so retries are as strong as
  the original attempt.** Rejected for v1: it's a migration, and the one
  retry-worthy class (transient infra) fails before doi/title matter —
  row 6's cert failure dies in the primary download, which runs on
  `(source, paper_id)` alone. The degradation is real for the *fallback*
  legs (unpaywall wants a DOI, repositories wants a title) — this is the
  runner-up, and the first thing to add if transient retries prove weaker
  than original attempts. Until then, a retry that re-fails writes a fresh
  verbatim reason, which is the feature working, not failing.

## 5. What this makes harder later

* **Hard-delete dismiss destroys history.** The PO's own v2 trigger ("if
  transient-class failures exceed ~¼ of new rows over a month, the sweep
  earns itself") is measured over this table; dismissed pairs leave the
  dataset. Today dismissals target junk, so the measurement survives — but if
  dismiss becomes habitual, the sweep's evidence base erodes. The soft-delete
  migration (A3) is the undo if that happens.
* **Substring classification will not scale with new sources.** The class
  label is cosmetic today (prominence only), so `unknown` is a safe default.
  The moment a sweep exists, class becomes load-bearing (a sweep retries
  `transient` only) and the classifier needs per-source hardening. The pinned
  six-fixture test is the seam where that hardening starts.
* **Two failure surfaces once N4 lands.** Agentic lazy-import failures will
  live in `import_jobs.state='failed'`, invisible to this list. Fine while N4
  doesn't exist; a unification (or a second section) is a future card.
* **`LIMIT 200` is a silent cap.** At 6 rows it's theoretical; the endpoint
  returns `count` from the same query, so if pagination is ever needed the
  response shape doesn't change.

## 6. Risks for the Coders

* **R1 — the stale-failure race on retry.** The poll state machine
  (`corpus.py:207-226`) has no "retry in flight" state: the instant a retry
  starts, `latest_failure` still returns the OLD row, so the poll says
  `failed` with the stale reason and a naive UI shows the retry as having
  instantly failed. This is why `attempted_at` goes into the `failed`
  response and the UI compares it to the value it remembered at click time.
  Test this path explicitly; it is the one place this design can lie to the
  operator.
* **R2 — `DbTables.execute` returns rows only for `select`.** No
  `RETURNING`, anywhere. Count-then-delete for dismiss. Repo-documented trap
  (`review.py:37-40`).
* **R3 — two-table SQL goes through `{table:...}` placeholders.** Names have
  a hyphen; a literal table name in a query breaks quoting. Precedent:
  `graph_db.py:358`.
* **R4 — the shared tree is dirty with someone else's in-flight work.**
  `ui/src` currently carries uncommitted DocumentViewer/theme.css changes and
  untracked DocGraph files (Neo4j migration V1–V3 and theme convergence are
  in flight). Running `npm run build` in this tree bakes THEIR uncommitted
  source into the `ui/dist` you commit. Build and commit from an isolated
  checkout/worktree containing only master + this card's changes, and commit
  only this card's files. The committed-dist check will not catch
  contamination — the build "succeeds".
* **R5 — row 6's 6000-char `paper_id` travels in a GET query string** on
  every `indexJobStatus` poll and in the retry POST. It works today
  (the per-job poll already handles this pair) but is near default
  header/URL limits; do not special-case it — it exists to be dismissed, and
  the missing length validation is one of the two flagged separate-card bugs.
* **R6 — don't "fix" row 1 en passant.** Its reason is a relative file path;
  the temptation is to make the classifier or the list "detect" path-shaped
  reasons and hide them. Render it verbatim as `unknown`. The underlying bug
  (`download_with_fallback` return contract vs `_run_index`'s `is_file`
  check) is explicitly out of scope.

## 7. Test plan (what QA should expect to find)

`tests/test_store.py`: `list_unresolved_failures` excludes a pair with a
`documents` row; returns the newest reason per pair; `dismiss_failures`
removes all rows for the pair and returns the count; classifier pins the six
live verbatim reasons. `tests/test_api_corpus.py`: `GET /corpus/failures`
shape + verbatim reason; `DELETE` happy path and 404; `index_job_status`
failed branch now carries `attempted_at` (regression-guard the existing
fields). UI: rebuilt dist committed from a clean tree (R4).

Effort: one coder session, matching the PO's sizing.
