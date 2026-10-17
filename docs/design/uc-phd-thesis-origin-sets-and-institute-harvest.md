# uc-phd — record each thesis's OAI origin sets, backfill the 181, harvest the three institute communities

**Card:** 3fc5bf3b-9510-8128-862f-f6351a6dd464 · **Repo:** `repos/aw-app-uc-phd` · **Architect, 2026-10-17**

Frederico's ask (verbatim scope, do not narrow or expand without him): add an
origin property so theses can be grouped by community, backfill it for the
existing corpus, then import `com_10316_27710` (INESCC), `com_10316_27713`
(ISR) and `com_10316_47572` (IT), always recording origin and never
duplicating a handle already imported under another community.

## The decision

**Origin is multi-valued and it is free.** Live-probed today (2026-10-17):
every OAI record **header** on estudogeral.uc.pt carries the item's complete
set membership as `<setSpec>` elements — communities *and* collections —
regardless of which set you enumerated to reach it. Verified on both
reference handles:

- `10316/100421` (Campos, correctly imported): `com_10316_15517,
  com_10316_255, com_10316_2, col_10316_15519, col_10316_103`
- `10316/114724` (D'Abruzzo, the missing one): `com_10316_15517,
  col_10316_15519` — **only the UC-wide umbrella, see "Scope gap" below.**

So the design is:

1. **Capture the raw header `setSpec` list on every record** —
   `estudo_geral_extractor/oai.py:28` `parse_record` gains a `set_specs`
   list read from `oai:header/oai:setSpec`. No second request, no GetRecord
   backfill script: one ListRecords enumeration of the DEI set already
   yields the full membership of all 181 existing theses.
2. **Front matter is the durable home**: new `oai_sets` key (raw specs,
   complete list, umbrella and collections included) in
   `estudo_geral_extractor/markdown.py:4` `FRONT_MATTER_KEYS` and
   `build_front_matter` — same provenance family as `source_url` / `rights`
   / `full_text`.
3. **SQLite seed gets a many-to-many table** `thesis_sets(handle REFERENCES
   theses(handle), set_spec TEXT, PRIMARY KEY (handle, set_spec))` in
   `scraper/schema.sql` (next to `theses` at `scraper/schema.sql:169`), built
   from front matter by `analysis/build_thesis_facts.py` (add to
   `THESIS_TABLES` at `analysis/build_thesis_facts.py:61` so the full-replace
   delete covers it; insert alongside `theses` at
   `analysis/build_thesis_facts.py:402`). `thesis_row`'s strictness precedent
   (`analysis/build_thesis_facts.py:124-128`, source_url required) extends to
   `oai_sets`: missing ⇒ raise with the handle named. That is safe because
   the harvest/backfill run rewrites all 181 `.md` files before the seed is
   rebuilt.
4. **The known community→label map is a code constant** in `oai.py`:
   `HARVEST_SETS = {com_10316_255: DEI, com_10316_27710: INESCC,
   com_10316_27713: ISR, com_10316_47572: IT}`. `thesis_sets` stores raw
   specs; grouping by "category" is `WHERE set_spec IN (...)` plus the label
   map. Storing raw keeps memberships we did not plan for (e.g.
   `col_10316_15519`) queryable later without re-harvesting.
5. **Harvest extension + backfill are ONE mechanism.** `run.py` enumerates
   each of the four sets via the existing `fetch_all_records(set_spec=...)`
   (`estudo_geral_extractor/oai.py:52` — already parameterised, only the
   caller hardcodes), filters with the existing doctoral-thesis selector,
   and merges by handle into one population (first record wins; headers are
   identical). Then per handle:
   - `estudo_geral/<slug>.md` exists → **no network**: re-render the file via
     the existing `render_markdown` path with `oai_sets` added, body
     preserved from the current file. Deterministic dump ⇒ the diff is the
     `oai_sets` lines only. This IS the backfill of the 181.
   - no `.md` → full `process_one` (`estudo_geral_extractor/run.py:54`),
     bot-gate and all, with `oai_sets` in the front matter and the manifest
     entry. This is the dedup Frederico asked for: a handle cross-listed in
     two sets appears once in the merged population, so `process_one` runs
     at most once per handle.
6. **Manifest is merged, not clobbered.** `run.py:150-151` currently rewrites
   `manifest.json` from this run's entries. Load the old manifest keyed by
   handle; carried-forward (front-matter-only) handles keep their old
   outcome entry (+ `oai_sets`), processed handles get fresh entries. The
   `reconcile` invariant in `analysis/build_thesis_facts.py:85` (manifest
   handles == md files, both directions) stays the gate; if an old handle
   vanishes from every harvested set (withdrawn upstream), reconcile fails
   loudly — correct, a human decides.

### Where the property does NOT go: the pgvector `documents` table

No migration 0003, no change to `uc_phd_app/store.py`. The split is already
written down in `analysis/build_thesis_facts.py`'s module docstring and was
decided on the multi-store architecture card: **structured thesis facts live
in the SQLite seed; Postgres keeps only what needs vectors.** Grouping and
filtering by community joins `thesis_sets` on `handle` — the same key
`documents` and `/api/search` results already carry — and the app's
established pattern for filtered semantic search is over-fetch `k` + filter
against SQLite, explicitly revisitable at ~10k docs. Duplicating the fact
into `documents` creates a second source of truth that goes stale whenever
upstream cross-listing changes, for zero query it enables today.

## Measured facts the coder should not re-derive

- Set sizes (ListIdentifiers, 2026-10-17): INESCC **262** records, ISR
  **139**, IT **57** — all types; the doctoralThesis subset will be smaller
  still, and many are cross-listed with DEI, so expect **few genuinely new
  theses**. The run must print per-set and merged counts so the delivered
  number is a measurement.
- `com_10316_27707` (CISUC's own community) measured **0 theses** on
  2026-09-23 (S1 card) and is not in Frederico's list — stays out.
- The OAI endpoint is NOT bot-gated (plain curl works); only bitstream
  downloads need the browser-check cookie dance in `download.py`. Keep the
  existing `REQUEST_SPACING_SECONDS` throttle for the extra enumerations.

## Scope gap — routed to Frederico, not absorbed

**The motivating thesis is not captured by this card's scope.** D'Abruzzo's
`10316/114724` is set-listed ONLY under `com_10316_15517` ("UC - Dissertações
e Teses", 35,308 items) and `col_10316_15519` ("UC - Teses de Doutoramento",
2,565 items) — university-wide containers, not any CISUC-affiliated
community. Importing INESCC+ISR+IT as asked will not bring it in. Options
for a follow-up decision (his call, per his own "don't expand without
checking"): (a) enumerate `col_10316_15519` (metadata-only, ~26 pages, cheap)
filtered to records whose `dc:description` names the programme ("Tese de
Doutoramento em Engenharia Informática ..."), which would catch DEI theses
never cross-listed anywhere CISUC; (b) targeted single-handle imports on
request. The seam this design builds (multi-set enumeration keyed on a
constant, origin always recorded) makes either a small change later.

## Rejected alternatives

- **Single "primary origin" column on `theses`.** Harvest-order-dependent
  (whichever set enumerated first), and provably lossy: Campos's thesis
  legitimately belongs to 3 communities. It answers "where did the harvester
  find it", not Frederico's "group by category".
- **"First-seen set" + separate membership table.** The first-seen set is an
  artifact of loop order, not a fact about the thesis; recording it invites
  grouping by it.
- **JSON list column on `theses`.** Not queryable in the plain-SQL idiom the
  seed's consumers use (`uc_phd_app/theses.py`, `sql/*.sql`); SQLite m2m is
  the shape `thesis_keywords` already uses.
- **Mirroring origin into pgvector `documents` (text[] column).** See above
  — second source of truth, needs migration + loader + store changes, and
  the established split already answers filtered search.
- **Per-handle OAI GetRecord backfill script.** The dispatch suggested it; it
  is 181 requests doing what one already-needed ListRecords enumeration of
  the DEI set does for free (headers carry full membership). A separate
  script is a second mechanism to maintain.
- **Manifest-only origin (keep `.md` untouched) to dodge the re-index.** The
  `.md` is "the durable artifact" (S1/PO decision) and `build_thesis_facts`
  reads front matter; hiding provenance in the manifest splits the artifact.

## Costs / what this makes harder later

- **Full pgvector re-embed on next ingest.** Rewriting all 181 front matters
  changes every `md_sha256`, so `estudo_geral_extractor/pgvector_index.py`'s
  resume check (line ~152) sees every document as changed and re-embeds the
  whole corpus (minutes-to-hours of ONNX CPU, same cost as the
  corpus-widening run). Unavoidable under content-hash resume and the run is
  needed anyway for the new theses. Run it where the pgvector loader already
  documents it must run.
- `oai_sets` becomes a **required** front-matter key: any future hand-added
  `.md` (e.g. a targeted import of D'Abruzzo's thesis) must carry it or the
  seed rebuild refuses. That strictness is the point, but it is a new way
  for a quick manual addition to fail.
- Set membership is snapshot-at-harvest: if UC re-maps an item's
  communities, our rows are stale until the next harvest run. Acceptable —
  same staleness model as every other harvested field.

## Risks for the coder

- **New theses bring non-CISUC authors/supervisors** (ISR/IT lean
  electrical/telecom). Expect a jump in `unmatched` tier rows in
  `thesis_people` — that is the schema working as designed, NOT a matching
  bug to "fix" by loosening `analysis/name_match.py`. Do not touch the
  matcher. QA should use the identity-match verification procedure
  (aw-autoskill-uc-phd-identity-match-verification) and confirm known false
  positives stay rejected.
- **Front-matter rewrite must preserve bodies byte-for-byte** and must go
  through the existing `render_markdown` dump so the 181-file diff is
  reviewable (one added key per file). Spot-check `git diff --stat` before
  committing: any file whose diff is more than the `oai_sets` lines means
  the YAML round-trip mangled something (multiline abstracts are the risk).
- **Embargoed/no-PDF items exist in the new sets too** — `process_one`
  already records those outcomes in the manifest instead of failing; don't
  "fix" a missing full text.
- The deposit-date floor must stay gone (`fetch_all_records(from_date=None)`)
  — corpus scope is a row attribute, per the corpus-widening decision.
- Tests: extend the OAI fixture in `tests/test_estudo_geral_extractor.py`
  with multi-`setSpec` headers; cover `thesis_sets` in
  `tests/test_build_thesis_facts.py`. All touched code is outside the
  `uc_phd_app` 100%-coverage gate, so no gate gymnastics — but the pure
  functions (`parse_record`, selector, front-matter build, merge/dedup)
  should be the tested surface, live enumeration stays untested as before.
- After the harvest + seed rebuild + pgvector ingest, the committed seed
  (`data/cisuc.sqlite3`), `estudo_geral/*.md` and `manifest.json` all change
  together in one commit — partial commits of this set break `reconcile`.
