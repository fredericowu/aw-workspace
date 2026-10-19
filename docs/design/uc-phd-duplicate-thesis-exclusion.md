# uc-phd — reviewed-evidence exclusion of a duplicate thesis record

Architect decision for item #2 of Kanban card `3fe5bf3b-9510-8127-b32c-c47722c20c65`
("resolve items #2 and #3 — follow-up from multi-thesis-tile card").
Written 2026-10-19. Repo: `/opt/aw-workspace/repos/aw-app-uc-phd`.

Scope was decided by the Product Owner and is not re-litigated here: one
additive, reviewed-evidence exclusion instrument for a duplicate *thesis
record*, following the pattern of the three existing curated files, seeded
with exactly one entry — `10316/90536` is the duplicate-to-drop,
`10316/90537` is the record that stays. Item #3 ("Soma, Paulo") is being
resolved by the PO in parallel and is untouched by this design.

## 0. Facts the design rests on (measured 2026-10-19, HEAD `3f87407`)

* **The duplicate is real and is a one-off.** The Debugger (card
  `3fe5bf3b-9510-8137-b4cc-f53bbbbe4ec4`, comment of 07:41) pulled both
  `.md` front matters and the OAI records: byte-identical
  `abstract_pt`/`abstract_en`, identical author ("Moio, Isabel Sofia
  Fernandes"), identical date (2018-01-09), identical `oai_sets`; a
  corpus-wide `(author, date)`-spanning-handles check across all 2690
  theses found no other occurrence. Re-read both front matters directly
  today (`estudo_geral/10316-90536.md`, `estudo_geral/10316-90537.md`):
  confirmed. The fields that **differ** between the two records are
  `title` (90536's is literally the author's own name — the malformed
  extraction), `supervisors` (same two humans, full legal form on 90536 vs
  short form on 90537) and `keywords` (same concepts, bilingual-combined
  strings on 90536 vs separate PT/EN strings on 90537). The fields that are
  **byte-identical** — and that the duplicate verdict actually rests on —
  are `authors`, `date`, `abstract_pt`, `abstract_en`.
* **What the committed seed holds for `10316/90536`** (queried
  `data/cisuc.sqlite3` directly): `theses` 1 row, `thesis_people` 3 rows
  (author "Moio, Isabel Sofia Fernandes" `unmatched`/NULL; supervisors
  `uc--fpce--joaquim-luis-medeiros-alcoforado` and
  `uc--fpce--cristina-maria-coimbra-vieira`, both `exact` via the UC
  directory pass), `thesis_keywords` 5 rows, `thesis_sets` 4 rows. **No
  other table keys this handle** — `bib_references.cited_by`,
  `thesis_references`, `thesis_group_affinity`, `external_people` all have
  zero rows for it. Current totals: `theses` 2690, `thesis_people` 7260,
  author rows 2690.
* **The seam.** `analysis/build_thesis_facts.py::build()`
  (`analysis/build_thesis_facts.py:598`) is the single point where
  `estudo_geral/*.md` front matters become every thesis-keyed table.
  `reconcile()` (`analysis/build_thesis_facts.py:105`) checks manifest ==
  extracted set *before* any table is generated. Everything downstream —
  `uc_phd_app`'s API/dashboard, the `phd_knowledge_base` MCP counts, the
  pgvector mirror, group affinity, bibliography — reads the seed, never the
  `.md` files directly.
* **The curated-file pattern this must follow.** Three files, each with a
  loader+validator module and a test file, each materialized as a seed
  table, each failing the build loudly when stale: `data/person_aliases.json`
  (+`analysis/person_aliases.py`), `data/person_full_names.json`
  (+`analysis/person_full_names.py`), `data/multi_thesis_authors.json`
  (+`analysis/multi_thesis_authors.py`, `tests/test_multi_thesis_authors.py`
  — including `test_the_committed_file_validates_against_the_real_seed`,
  the committed-file-vs-committed-seed check). `multi_thesis_authors` is the
  closest sibling: required `source_url` (must be a URL) + required
  `evidence`, shape errors in `load_entries()`, identity/staleness errors in
  `validate()` raised never skipped, a schema-level backstop trigger
  (`scraper/schema.sql:323`), and a write-order fact documented in
  `build()`'s docstring.
* **Known repo-state trap (pre-existing, NOT this card's to fix):**
  `estudo_geral/` on disk holds 2690 `.md` files but the *tracked*
  `manifest.json` has 181 entries (and only 181 `.md` are tracked). A naive
  `python -m analysis.build_thesis_facts` against the main tree fails
  `reconcile()` today, before this change. Every prior delivery (coder-opus
  e3ca34d, QA 26af81a1) rebuilt on an isolated copy with a manifest
  regenerated from the `.md` files' own `handle` front-matter fields —
  nothing downstream reads `manifest.json` except `reconcile()`'s set
  check. The Coder must use the same workaround; this design does not
  change it.
* **PO assumptions, checked and confirmed:** exclusion-at-build is the
  right seam (see §2 and the rejected alternatives), and 2689/2689 are the
  right targets (`theses` 2690−1; author rows 2690−1; the invariant
  `author rows == theses` restored by item #1 holds through the change).

## 1. The decision

A fourth curated file, **`data/duplicate_theses.json`**, keyed by **thesis
handle**, loaded and validated by a new **`analysis/duplicate_theses.py`**
(same triad as its siblings), consulted by `build()` **immediately after
`reconcile()` and before any table row is generated**. The excluded
handle's front matter is dropped from the build's working list, so every
generated table (`theses`, `thesis_people`, `thesis_keywords`,
`thesis_sets`) is free of it by construction — no per-table filtering, no
orphan-row risk. The file is materialized as a **`duplicate_theses` seed
table** (so the reason the handle is gone is queryable, not only readable
in git), and a schema **trigger backstops** the exclusion the way
`trg_one_thesis_per_resolved_author` backstops the one-thesis rule.

The on-disk corpus stays complete: `estudo_geral/10316-90536.md` is not
deleted, its manifest entry stays, and `reconcile()` runs on the *full*
corpus before the exclusion applies — so the exclusion can never be
mistaken for a harvest gap (PO criterion 3.f), and reverting the call is
deleting one JSON entry and rebuilding.

### The file — `data/duplicate_theses.json`

Same envelope as its siblings: a top-level `note` explaining what the file
is and is not, plus `entries`. One entry per duplicate record:

```json
{
  "note": "Human-reviewed DUPLICATE THESIS RECORDS … (what/why, the option-C fence, pointer to analysis/duplicate_theses.py)",
  "entries": [
    {
      "duplicate_handle": "10316/90536",
      "kept_handle": "10316/90537",
      "identical_fields": ["authors", "date", "abstract_pt", "abstract_en"],
      "source_url": "https://estudogeral.uc.pt/handle/10316/90536",
      "evidence": "One physical submission deposited as two Estudo Geral records. Byte-identical abstract_pt/abstract_en/authors/date to 10316/90537; 90536's title field is the author's own name rather than a thesis title; same two supervisors in fuller name forms; identical oai_sets. Verified against the whole 2690-thesis corpus as the only (author,date) pair spanning >1 handle. <OAI GetRecord URLs + date read>",
      "note": "90537 is the record that stays — real title, same content. The malformed title on 90536 is moot once the record is dropped (PO: out of scope)."
    }
  ]
}
```

Field semantics, enforced by the module:

* **`duplicate_handle`** — the record the build drops. The key of the file
  is a THESIS HANDLE and the entry's claim is "this record is not a
  distinct thesis". It never says whose thesis anything is — the file must
  never grow person semantics (the PO-rejected option C fence, same line
  `multi_thesis_authors.py`'s docstring draws).
* **`kept_handle`** — the surviving record, named so the claim is
  falsifiable: the entry asserts a *pair*, not a lone deletion.
* **`identical_fields`** — the front-matter keys the duplicate verdict
  rests on, re-proven equal on every build (see `validate()` below). Must
  include at least `authors` and `date`; listed per entry rather than fixed
  in the module so the evidence each exclusion stands on is explicit in the
  reviewed file itself.
* **`source_url`** (http/https) + **`evidence`** (non-empty) — required,
  exactly as in `multi_thesis_authors.json`: the citable record and why a
  human is satisfied, for whoever has to re-make this call.
* **`note`** — optional.

### The module — `analysis/duplicate_theses.py`

Mirrors `analysis/multi_thesis_authors.py`'s four-function shape (no
`load_and_write`, because the steps run at different points of one build):

* `load_entries(path=None)` — shape checks only (keys present, no unknown
  keys, non-empty strings, URL prefix, `identical_fields` a non-empty list
  of strings containing `authors` and `date`, `duplicate_handle !=
  kept_handle`, no `duplicate_handle` listed twice, no handle appearing as
  both a `duplicate_handle` and a `kept_handle` anywhere in the file — no
  chains, no cycles). Raises `DuplicateThesisError` (a `RuntimeError`
  sibling of `MultiThesisAuthorError`), never warns.
* `validate(entries, front_matters_by_handle)` — the staleness checks, run
  against the corpus the build just read and reconciled, each raised with a
  message that says what went stale and what to do (PO criterion 3.e —
  non-negotiable):
  1. `duplicate_handle` must be in the corpus — if `10316/90536`
     disappears upstream the exclusion is standing over nothing; delete
     the entry.
  2. `kept_handle` must be in the corpus — if the survivor disappears, the
     entry would silently erase the thesis entirely instead of deduping
     it; the build must stop.
  3. every key in `identical_fields` must be present in **both** front
     matters and compare equal (parsed-value equality — the front matters
     are YAML, so post-parse equality is the real "these fields still
     carry the same content" check). If the records stop being identical
     in the fields the evidence rests on, the verdict is no longer
     reviewed — fail.
* `excluded_handles(entries) -> frozenset[str]` — what `build()` filters
  by. Documented like `exempt_claims()`: call `validate()` first.
* `write_table(conn, entries)` — full replace of the `duplicate_theses`
  table (one row per entry: `duplicate_handle` PK, `kept_handle`,
  `source_url`, `evidence`), same no-commit caller-owns-transaction
  contract as `multi_thesis_authors.write_table`.

### Where it lands in `build()` (`analysis/build_thesis_facts.py`)

```python
front_matters = load_front_matters(estudo_geral_dir)
reconcile(front_matters, estudo_geral_dir / "manifest.json")   # full corpus — unchanged

dup_entries = duplicate_theses.load_entries(duplicate_theses_path)
duplicate_theses.validate(dup_entries, {fm["handle"]: fm for fm in front_matters})
excluded = duplicate_theses.excluded_handles(dup_entries)
front_matters = [fm for fm in front_matters if fm["handle"] not in excluded]
```

* Validation runs on the **unfiltered** corpus (it needs both records'
  front matters); the filter applies right after, before `thesis_row`,
  `people_rows`, `keyword_rows`, `set_rows`, `supervision_edges` or any
  other derivation touches the list. Everything downstream of this point
  is automatically consistent — that is the whole argument for this seam.
* `build()` grows a `duplicate_theses_path: Path | None = None` parameter
  (seventh, same convention as the six existing) and `main()` grows
  `--duplicate-theses` (`analysis/build_thesis_facts.py`, argparse block).
  Defaults point at the committed file, so the default path is always
  exercised — no optional-config-gated dead path.
* `write_table` is called **before** the `theses` inserts (the trigger
  below reads it), and `"duplicate_theses"` is appended to
  `THESIS_TABLES` (`analysis/build_thesis_facts.py:80`) so the
  delete-before-rebuild sweep covers it.
* The build report dict gains
  `"duplicate_theses": {"entries": N, "excluded_handles": N}` so the
  exclusion is a stated measurement of every build, like
  `theses_per_harvest_set`.

### Schema (`scraper/schema.sql`) — table + backstop trigger

```sql
CREATE TABLE IF NOT EXISTS duplicate_theses (
    duplicate_handle TEXT PRIMARY KEY,   -- the record the build drops. Deliberately NOT REFERENCES theses — its absence there is the point.
    kept_handle      TEXT NOT NULL REFERENCES theses(handle),
    source_url       TEXT NOT NULL,
    evidence         TEXT NOT NULL
);

CREATE TRIGGER IF NOT EXISTS trg_no_excluded_thesis
BEFORE INSERT ON theses
WHEN EXISTS (SELECT 1 FROM duplicate_theses d WHERE d.duplicate_handle = NEW.handle)
BEGIN
    SELECT RAISE(ABORT, 'this handle is a reviewed duplicate record (data/duplicate_theses.json) and must not enter theses');
END;
```

The trigger is the same defense-in-depth move as
`trg_one_thesis_per_resolved_author`: `build()` filtering is the
mechanism, the trigger is the fence against any *other* writer (or a
future refactor that forgets the filter) re-inserting a reviewed-out
record. Single string literal in RAISE, per the comment at
`scraper/schema.sql:328`.

### Tests

* **`tests/test_duplicate_theses.py`** — mirrors
  `tests/test_multi_thesis_authors.py`: shape rejections (missing keys,
  unknown keys, empty evidence, non-URL source, `identical_fields` missing
  `authors`/`date`, dup==kept, handle listed twice, kept-also-duplicate);
  staleness rejections (duplicate gone from corpus, kept gone from corpus,
  a listed field diverging between the two front matters); and at the
  bottom `test_the_committed_file_validates_against_the_real_seed`-style
  checks against the committed file + committed corpus: the committed
  entry loads, `10316/90536` is absent from the committed seed's `theses`
  and `10316/90537` present, and the `duplicate_theses` table carries
  exactly the committed entries.
* **`tests/test_build_thesis_facts.py`** — a build-level fixture test: a
  tiny corpus with a reviewed duplicate pair builds with the duplicate
  absent from all four thesis tables and present in `duplicate_theses`;
  a control building the same corpus with an empty exclusion file writes
  both records (proves default behaviour unchanged when the file is
  empty); a trigger test (direct INSERT of an excluded handle aborts).

## 2. Rejected alternatives

* **Exclusion at harvest** (skip in `estudo_geral_extractor`, or delete
  the `.md`). Rejected — and the PO's seam assumption confirmed — for
  three reasons: a re-harvest would silently resurrect the duplicate (the
  extractor has no memory of editorial decisions); deleting the `.md`
  makes the call invisible and irreversible on disk (PO scope §4
  explicitly keeps the corpus complete); and `reconcile()` would then
  report the exclusion as a manifest/corpus gap unless the manifest were
  edited too, which is exactly the "looks like a harvest gap" failure 3.f
  forbids. Build-time exclusion over a complete corpus keeps the decision
  in one reviewed file, applied idempotently on every rebuild.
* **Automatic duplicate detection** (byte-identical abstracts,
  `(author,date)` collisions, fuzzy titles). Rejected by the PO and
  independently wrong here: the Debugger measured the problem as a
  one-off across 2690 theses; a detector solves a problem measured as not
  existing, and its false positives silently delete real theses — the
  exact failure mode the curated-file house pattern exists to prevent
  ("byte-identical is the maximally similar pair; similarity is not
  identity", `analysis/multi_thesis_authors.py` docstring). A
  detect-and-report pass for humans is a separate card if ever wanted.
* **Reusing an existing curated file** (`multi_thesis_authors.json`,
  `person_aliases.json`). Wrong key grain: those files' keys are
  *people*; this fact's key is a *thesis handle*. Folding it in would
  blur the one fence every sibling file documents — none of them may
  become the rejected option C (thesis-name → person lookup,
  `docs/thesis-attribution.md`). A separate file whose entries carry no
  person field at all cannot drift across that line.
* **Query-time filtering** (`WHERE handle NOT IN (SELECT …)` in
  `uc_phd_app`'s SQL). Rejected: the seed would still carry the duplicate,
  so every consumer — the app's ~dozen SQL files, the `phd_knowledge_base`
  MCP counts, the pgvector mirror, `build_group_affinity`,
  `build_bibliography` — would each need the filter, and the first one
  that forgets is silently wrong. One build-time filter fixes all
  consumers at once, and "absent from `theses`" is the invariant the
  counts rest on.
* **An `excluded` flag column on `theses`** instead of absence. Same
  defect as query-time filtering (every aggregate needs `WHERE excluded=0`
  forever), plus it breaks the restored `author rows == theses` invariant
  or forces role rows to stay for a non-thesis.
* **Merging the records** (re-pointing 90536's rows at 90537). There is
  nothing to merge — 90537 already carries the same author, date,
  abstracts and both supervisors (in shorter name forms); 90536
  contributes nothing but a malformed title and fuller supervisor name
  strings. A merge layer would add a handle-aliasing concept to the schema
  for zero information gain.
* **Module-fixed identity fields instead of per-entry `identical_fields`**
  (runner-up, genuinely close). A frozen `{"authors","date","abstract_pt",
  "abstract_en"}` in the module is simpler and prevents a weak entry
  listing only `date`. Chosen against because a future duplicate pair may
  rest on different evidence (e.g. one record with a missing abstract),
  and the reviewed file should state what was actually compared — the
  module's `authors`+`date` minimum keeps the weak-entry door shut. What
  would change this: if a second entry ever games the field list, fix the
  minimum in the module; the file shape doesn't change.

## 3. What this makes harder later

* **Upstream cleanup breaks our build — by design.** If Estudo Geral ever
  deletes or merges `10316/90536`, `validate()` fails every build until
  someone deletes the entry. That is the loud-staleness trade the three
  siblings already made; the cost is accepted, not accidental.
* **A fifth curated file raises the pattern's carrying cost.** Four files
  × (loader + validator + table + tests) is a lot of ceremony per fact.
  If a fifth kind of reviewed fact appears, consider extracting the shared
  envelope — not now, with n=1 entries in the new file.
* **The on-disk corpus count and the seed count now legitimately differ**
  (2690 `.md` vs 2689 `theses` rows). Anyone auditing by `ls
  estudo_geral/*.md | wc -l` sees a discrepancy that is explained in
  `data/duplicate_theses.json`, not in the directory. The build report's
  `excluded_handles` count is the breadcrumb.
* **The trigger binds future writers.** Any future tool inserting into
  `theses` directly must not carry an excluded handle — correct, but it
  turns a data decision into a schema constraint someone may trip on.
* **`build()` gains a seventh optional path parameter.** A caller that
  forgets it gets the committed default — safe (unlike `match_name`'s
  third argument, where forgetting silently restores old behaviour).

## 4. Risks for the Coder (non-obvious breakage)

* **The manifest trap is live.** The main tree today has 2690 `.md` but a
  181-entry tracked `manifest.json` — a rebuild fails `reconcile()` before
  your change is even reached. Follow QA 26af81a1's exact workaround:
  isolated copy of the corpus + manifest regenerated from the files' own
  `handle` front-matter fields, rebuild into a **copy** of the seed, never
  the live one, per the `aw-autoskill-uc-phd-identity-match-verification`
  recipe (worktrees, row-diff, cleanup).
* **Pinned-number tests will move, and the deltas are knowable in
  advance.** Expected, from the measured seed rows for `10316/90536`:
  `theses` 2690→2689; `thesis_people` 7260→**7257** (−1 unmatched author
  row, −2 `exact` UC-directory supervisor rows); author rows 2690→**2689**
  (= theses — invariant holds); `thesis_keywords` −5; `thesis_sets` −4;
  `supervision_edges` 4570→**4568** (it is counted from front matters,
  which are filtered). The two dropped supervisor rows were `exact` via
  the UC-directory pass (`uc--fpce--…alcoforado`, `uc--fpce--…vieira`), so
  UC-directory matched-edge counts in the audit numbers drop by 2; the
  full-form name strings "Alcoforado, Joaquim Luís Medeiros" / "Vieira,
  Cristina Maria Coimbra" may exist only on 90536, in which case
  `distinct_names` drops too. Amend pinned tests with the reasoning in
  their docstrings — the house pattern from e3ca34d — and let the row-diff
  be the proof that nothing outside `10316/90536` moved (PO criterion 3.d).
* **Dashboard expectations:** theses tile 2690→2689; Students stays
  **2686** (Moio remains an author exactly once — distinct raw author
  names don't change); the raw-name "appears as author on >1 thesis"
  grouping goes 3→**2** (`joao-campos`, "Soma, Paulo") — "Moio, Isabel
  Sofia Fernandes" leaves it. Do not touch the tile's design/wording and
  do not touch anything about "Soma, Paulo" (PO scope §4; item #3 is being
  resolved in parallel on this same card).
* **Verification is the full PO list 3.a–3.g**, via the
  `aw-autoskill-uc-phd-identity-match-verification` recipe: isolated
  rebuild row-identical to the committed seed (row-identity, not
  byte-identity — the sqlite header counter differs on any rebuild);
  row-diff before/after shows only `10316/90536` rows removed; golden
  fixture `docs/thesis-attribution.json` unedited; "Fonseca, José Carlos"
  and "Cruz, Luís Alberto da Silva" still UNMATCHED; full suite green
  (779 passed at HEAD; expect amended pinned tests + new tests).
* **Release chain:** commit the rebuilt seed; never hand-bump the version
  (release bot owns it); the marketplace sync PR will NOT auto-merge
  (repo is `fredericowu/`-owned, the auto-merge step is gated on
  `tekflox/`) — hand-merge it or nothing publishes, then
  `marketplace install --update` and verify `/healthz`
  (`row_counts.thesis_people` 7260→7257, `seeded_from_version` bumped)
  and live `/api/theses` (2689, `10316/90536` absent, `10316/90537`
  present).
* **After QA passes, do NOT `mark_flow_done`** — the card stays open for
  item #3 (PO-owned). Report on the card and hand to QA; the PO closes.
