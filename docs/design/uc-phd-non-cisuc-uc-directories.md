# uc-phd — resolving supervisors against non-CISUC UC directories

Architect decision for Kanban card `3fd5bf3b-9510-8113-a699-ec378da4a0c9`
("uc-phd: expand collaborator resolution to non-CISUC UC directories").
Written 2026-10-18. Repo: `/opt/aw-workspace/repos/aw-app-uc-phd`.

The mechanism (per-unit scraper suite, not a curated list, not a hybrid) was
decided by the PO and is not re-litigated here. This document decides the
five things delegated to the Architect: storage/identity shape, matcher
plumbing, harvest structure, UI/metric presentation, and the classification
instrument — plus the sequencing against the live sibling run.

## 0. Facts the design rests on (verified 2026-10-18)

* Committed seed `data/cisuc.sqlite3` @ `8c82189`: 2690 theses; supervisor
  rows exact=113 / confident=517 / ambiguous=4 / unmatched=3940; 4570
  distinct supervisor (handle, name) edges, 630 resolved rows (~13.8%
  corpus-wide). Within the 569 CISUC-touching theses: 636/1065 edges (60%) —
  prior design doc `uc-phd-collaborator-match-coverage-2690.md` §0.
* 2195 distinct unmatched supervisor names corpus-wide, 1539 appearing once
  (PO measurement on the card). The tail is flat; only pool size reaches it.
* **The committed corpus inputs are inconsistent with the committed seed.**
  `git ls-files estudo_geral` = 181 `.md` + a 181-entry manifest; the
  2509 newer `.md` files and the 2690-entry manifest are **untracked** in
  the shared tree. A clean worktree from `HEAD` therefore rebuilds a
  181-thesis seed that does NOT match the committed 2690-thesis seed, and
  the mandated verification recipe ("rebuild == committed seed") is
  currently unsatisfiable from a clean checkout. See §6 step 0.
* Sibling run `38e7563feb7a4cbf941b372d37917cb5` (card
  `3fd5bf3b-9510-8128-958f-e1d6b9e63660`) is live; its code is largely
  landed in `8c82189` (`data/multi_thesis_authors.json`,
  `analysis/multi_thesis_authors.py`, the `resolve_author_conflicts`
  exemption, the `trg_one_thesis_per_resolved_author` trigger replacing the
  partial index — all read in the current tree and designed against here).
* `scraper/people.py` is the harvest recipe precedent (loud coverage
  failure, additive upsert, suggest-only duplicate detection);
  `scraper/run.py` is the one-shot-offline precedent (manifest first,
  commit what succeeded, non-zero exit naming what failed);
  `analysis/audit_unmatched_supervisors.py` is the "triage, never a
  matcher" precedent.
* Spike findings (card `3fd5bf3b-9510-8198-8520-f9161be15a19`): UC runs no
  SIGARRA; apps.uc.pt university-wide listings redirect to SAML SSO;
  what exists is ~8–10 per-Faculty "Docentes" pages + per-centre "Pessoas"
  pages; FEUC's Docentes page 403s plain `requests` and needs a real
  browser; all 10 top unmatched names were found on such pages.

## 1. Decision 1 — storage/identity shape: same `people` table, disjoint slug namespace, new `directory_listings` provenance table

### The approach

A non-CISUC UC person becomes an ordinary row in **`people`** — that is what
makes them "resolved" in every sense the app already has (a `person_slug`,
a profile route, a valid FK target) — under a slug in a **disjoint,
prefixed namespace** (`uc--<source>--<page-slug>`, e.g.
`uc--feuc--boaventura-de-sousa-santos`). What distinguishes them from a
CISUC member is a row in a new, purely additive table:

```sql
-- scraper/schema.sql, next to person_aliases/person_full_names
CREATE TABLE IF NOT EXISTS directory_listings (
    person_slug       TEXT NOT NULL REFERENCES people(slug),
    source            TEXT NOT NULL,   -- unit code: 'feuc', 'fduc', 'ces', ...
    unit_name         TEXT NOT NULL,   -- 'Faculdade de Economia (FEUC)'
    listing_url       TEXT NOT NULL,   -- the directory page the row was read off — live, checkable
    person_url        TEXT,            -- per-person page when the unit publishes one
    name_as_published TEXT NOT NULL,
    fetched_at        TEXT NOT NULL,
    PRIMARY KEY (person_slug, source)
);
```

**Scope is derivable, never asserted twice:** a `people` row with no
`directory_listings` row is a CISUC-directory person exactly as today (only
the new harvester writes listings); a row with one is a UC-directory
person. The harvester **refuses, loudly, to write a slug that already
exists in `people` outside its own namespace** — the prefixed namespace
makes collision with CISUC's arbitrary URL-tail slugs structurally
impossible, and the check makes the guarantee enforced rather than
intended.

One person listed in two units (Boaventura on FEUC's Docentes AND CES's
Pessoas) is deduplicated at harvest **only on identity evidence, never on
name**: when both listings publish the same stable per-person URL
(`apps.uc.pt/mypage/...` or a faculty profile URL), they collapse into one
slug with two listing rows (the PK grain exists for exactly this);
otherwise each unit's listing gets its own slug and the pair is surfaced to
`possible_duplicates`-style review, settled — like every identity decision
in this repo — by a `data/person_aliases.json` verdict with evidence.
`person_aliases` already FKs `people(slug)` generically, so the existing
instrument covers cross-source merges with zero change.

### Blast radius on existing consumers (worked, not guessed)

* `thesis_people.person_slug REFERENCES people(slug)` — new rows are valid
  targets; no schema change to `thesis_people` at all. The source of a
  resolution is derivable by joining `directory_listings`; no `source`
  column is added to `thesis_people` (it would denormalise a fact the join
  already states and force the trigger/index conversation open again).
* `trg_one_thesis_per_resolved_author` and the dropped
  `idx_one_thesis_per_resolved_author` (`scraper/schema.sql:248-302`) —
  **untouched by construction**: pass 2 (§2) resolves supervisor mentions
  only, so no new resolved *author* row is ever inserted and the trigger
  never sees this feature. The sibling card's exemption machinery is not
  interacted with.
* `person_aliases` / `person_full_names` (`scraper/schema.sql:84-118`) —
  slug-generic, work unchanged; aliases become the cross-source merge
  instrument, full names remain available for a UC-directory person whose
  published form is wrong-ended (same gate: reviewed evidence only).
* `people_canonical` view (`scraper/schema.sql:127-129`) — includes UC
  rows; its consumers (`sql/top_coordinators.sql:51`,
  `uc_phd_app/collab.py:488`) join through `project_people`, which UC rows
  never appear in, so coordinator/collab outputs are unchanged. The one
  place that counts `people_canonical` raw (`collab.py:488`,
  `total_people`) WILL grow — that is a UI label question, handled in §4.
* `project_people` — only the CISUC project scraper writes it; UC rows have
  none, which is the correct empty state (§4).
* `uc_phd_app/theses.py:_person_group_shares` (line ~136) — returns `{}`
  for a slug with no project history, so the attribution pipeline already
  degrades to no group signal for UC people with no code change. Nothing
  may fabricate a group; nothing does.
* `scraper/people.py::sync_people` — unchanged; its
  `absent_from_directory` count must keep meaning "absent from CISUC's
  directory", which it does because it compares against its own fetch, not
  against `people` scope.

### Rejected storage alternatives

* **A parallel `uc_people` table.** Breaks or forks the FK from
  `thesis_people.person_slug`, forces a UNION shim into every consumer
  (profile route, collab, aliases, full names), and makes "resolved" mean
  two different things in two tables. The entire point of a `person_slug`
  is one identity space.
* **A `source`/`unit` column on `people`.** `people` is applied via
  `CREATE TABLE IF NOT EXISTS` over the committed seed
  (`db.apply_schema`, `build_thesis_facts.py:478`); SQLite has no
  `ADD COLUMN IF NOT EXISTS`, so this needs a hand-rolled migration path
  the repo deliberately doesn't have for the seed. And it is the wrong
  grain anyway: one person can hold listings in several units, which is a
  table, not a column.
* **Mention-keyed storage (an `external_people` extension).**
  `external_people` is display metadata for names that must NEVER resolve;
  giving it a slug path is explicitly forbidden by its own module
  docstring and would be option C with extra steps.

## 2. Decision 2 — matcher plumbing: a second pass over a second pool, supervisors only, never touching pass 1

### The approach

`match_name` stays byte-identical in its rules. The build runs it twice,
against two pools:

* **Pass 1 — the CISUC pool** (people rows with no `directory_listings`
  row): exactly today's behaviour, same cache, same verdicts. Byte-for-byte
  identical output to the current build for every name — this is the
  comparability and zero-regression guarantee, held structurally rather
  than by review.
* **Pass 2 — the UC pool** (people rows joined to `directory_listings`):
  consulted **only** for mentions with `role='supervisor'` whose pass-1
  verdict is `unmatched`. Same `match_name` function, own cache. Tier
  semantics unchanged (`exact` on token-set equality with the published
  form, `confident` on subset/initial/tie-break, `ambiguous` surfaced with
  candidates). The written `match_note` is prefixed with the source
  (`"via UC directory feuc: ..."`) so every row is auditable without a
  join; the join to `directory_listings` remains the machine-readable
  provenance.

Pass 2 deliberately does **not** run for: authors (PO fence — students are
structurally absent from staff directories, and matching them against one
invites exactly the staff/student name collisions this corpus fears);
pass-1 `ambiguous` names (they already have real CISUC candidates; adding
more candidates cannot resolve them, only widen them).

### The cross-source collision, stated (this will happen)

**A name that fits a CISUC row never reaches pass 2 — the pass-1 verdict
stands, exactly as it does today.** This is a source-precedence rule and it
is not silent: the build report gains a `cross_directory_fits` list — for
every pass-1 *resolved* supervisor name, the UC pool is additionally
checked with the same `_fits` machinery, and any UC row that also fits is
**reported, never acted on** (the `audit_unmatched_supervisors` triage
pattern: a flag for a human, not a verdict). A reviewer who finds the CISUC
match wrong fixes it with the curated instruments; the matcher never
flip-flops on source priority. Rationale: today's behaviour for these names
IS the CISUC match; a merged pool would instead demote them to `ambiguous`
en masse (every common-surname CISUC match acquires UC-wide competitors),
which moves the within-CISUC tile *down*, churns the golden fixture, and
spends Frederico's trust on honesty theatre — the collision is surfaced
either way, and only this way preserves before/after comparability.

**Within pass 2**, a name fitting rows from two different units is
`ambiguous`, surfaced with both candidates — that is what the tier is for —
EXCEPT that `_one_folded_name` (`name_match.py:292`) must not be allowed to
auto-collapse them: it was written for CISUC listing one human twice, and
"João Silva" at FDUC vs "João Silva" at FFUC is two-people-until-proven-
otherwise. **`Person` gains a `source: str = "cisuc"` field and
`_one_folded_name` additionally requires all candidates to share one
source.** This is a *tightening* — it can only withhold a collapse, never
create a match; it buys zero edges (every edge bought by a rule change is a
regression per the PO); and on the legacy corpus it is a no-op by
construction (all pass-1 rows carry one source), so the golden fixture
(`tests/test_name_match.py`) stays green unmodified. Cross-source
same-human pairs are then merged the house way: a `person_aliases` verdict
with evidence, which `_one_canonical` already honours.

### Where it lands

* `analysis/name_match.py` — `Person` gains the `source` field (default
  `"cisuc"`, so every existing constructor call is untouched);
  `_one_folded_name` gains the single-source condition. **Nothing else.**
  Both rejected relaxations, `_fits`, `_fits_full`, the tie-break: frozen.
* `analysis/build_thesis_facts.py` —
  * `load_people()` splits into `load_cisuc_people(conn)` (the current
    `_PEOPLE_SQL` filtered to slugs absent from `directory_listings`) and
    `load_uc_directory_people(conn)` (joined to it, `source` populated;
    aliases/full-names LEFT JOINs kept — the instruments are slug-generic).
  * `people_rows()` takes both pools and two caches; on a supervisor
    mention whose pass-1 result is `unmatched`, consults the pass-2 cache.
    The canonical-slug write-through and dedup logic is shared, not copied.
  * `build()` report gains: per-pass tier counts (pass-1 tiers keep their
    exact current meaning; pass-2 counts reported separately),
    `cross_directory_fits`, and `uc_directory_people` count.
* `scraper/schema.sql` — the `directory_listings` DDL (§1). Nothing else.
* Tests (`tests/test_build_thesis_facts.py`, new
  `tests/test_uc_directory_match.py`): (1) a supervisor unmatched in pass 1
  resolves in pass 2 with the source-prefixed note; (2) an author with the
  same name does NOT; (3) a pass-1 `ambiguous` name is not consulted
  against pass 2; (4) a pass-1 resolved name with a fitting UC row keeps
  its CISUC verdict and appears in `cross_directory_fits`; (5) same folded
  name in two units → `ambiguous`; same folded name within one source →
  collapses as today; (6) a `person_aliases` `same` verdict across a CISUC
  and a UC slug resolves the pair with the canonical written; (7) golden
  fixture untouched and green.

### Rejected matcher alternatives

* **One merged pool, single pass.** Demotes an unknown but large set of
  currently-resolved edges to `ambiguous` (every subset-matched common name
  acquires UC-wide competitors), moving the within-CISUC tile down and
  breaking the PO's comparability constraint; churns the golden fixture;
  and makes the row-diff unreviewable (thousands of changed rows instead of
  additions-only). The honest surfacing it buys is kept anyway via
  `cross_directory_fits`.
* **A separate fifth tier (`confident-uc` etc.).** The tier vocabulary is
  a statement about *name evidence*, not source trust, and every consumer
  (`RESOLVED_TIERS`, `sql/thesis_attribution_coverage.sql`, the UI) would
  need to learn it. Source is provenance, carried by the join and the
  note — adding it to the tier enum conflates two axes.
* **Relaxing any rule to lift pass-2 yield.** Rejected relaxations A and B
  stay rejected; option C stays rejected. Not revisited.

## 3. Decision 3 — harvest structure: a registry-driven per-unit suite, offline one-shot, loud per-unit failure

### The approach

New package **`scraper/uc_directories/`**:

* **`units.py`** — the committed registry, one entry per unit:
  `{source, unit_name, kind: faculty|centre, listing_url, parser,
  expected_min}`. Seeded with UC's eight faculties' "Docentes" pages
  (FLUC, FDUC, FMUC, FCTUC, FFUC, FEUC, FPCEUC, FCDEFUC) plus the research
  centres the corpus actually touches (CES first — four of the ten spike
  names sit on FEUC and several on CES; then CNC, CITEUC, CEIS20 and
  whatever the OAI communities in `thesis_sets` justify). The registry is
  curated and extensible; units do not churn, and "which units exist" is a
  reviewable diff, not runtime discovery. Minimum first wave = the units
  that cover all 10 spike names (acceptance (a) on the card).
* **One parser per page shape**, not per unit — the spike found the same
  few templates repeated. `expected_min` is the honesty mechanism, exactly
  `fetch_all_people`'s coverage check (`scraper/people.py:100-154`): a
  parse yielding fewer rows than the floor **raises for that unit**. A page
  that changes shape fails ITS unit loudly; the run continues, commits what
  succeeded, and exits non-zero naming every failed unit — the
  `scraper/run.py` people-step precedent (`run.py:209-266`), not a silent
  partial harvest (a silently short pool is worse than no pool —
  `DESIGN.md` §6 via `people.py`'s docstring).
* **`run.py`** — `python -m scraper.uc_directories.run [db]`: fetch or
  load each unit, parse, upsert `people` + `directory_listings` (additive,
  never destructive — `db.upsert_person` semantics, so a person who drops
  off a current page survives, same as CISUC's ~182), print JSON counts
  per unit (fetched / new / renamed / dedup-by-person-url / slug-refusals /
  suspects). The committed seed diff is the review artifact. **No app code
  ever fetches** — the app opens the DB `mode=ro` as always.
* **`--snapshot-dir` mode** for browser-walled pages: FEUC 403s plain
  `requests` (spike-verified), so the suite accepts pre-saved HTML files
  (one per unit source, saved by a human or a browser-MCP session) and
  runs the same parser over them. `listing_url` stays the live URL;
  `fetched_at` records when; the run report records which units came from
  snapshots. No SAML, no credentials, no Selenium dependency baked into
  the repo.

### Rejected harvest alternatives

* **Runtime unit discovery** (crawl uc.pt for Docentes pages). Turns
  "which units are in the pool" into a scrape artifact nobody reviews;
  the registry diff is the review.
* **A live-fetch path in the app** (resolve-on-request). Violates the
  repo's core constraint (offline one-shot, reviewable diff, app reads
  `mode=ro`), and the PO's traceability constraint needs a frozen row to
  point at.
* **Ciência Vitae / ORCID as a pool.** PO fence: national registry, admits
  non-UC people into the identity space. Stays what it is — evidence a
  human may cite inside a curated entry.

## 4. Decision 4 — UI and metrics: resolved, linked, visibly not-CISUC, groupless

* **Thesis views** (`ui/src/components.jsx:99 ResolvedPersonName`,
  `ui/src/views/Theses.jsx:68 PersonList`, `ThesisDetail.jsx`): a pass-2
  resolved supervisor renders as a `PersonLink` like any resolved person
  — that is the product ask — plus a compact unit badge (e.g. "FEUC")
  sourced from the payload, in the visual register `ExternalIdentity`
  already uses for externals. The payload (`uc_phd_app/theses.py::
  _resolve_person_rows`) gains a `directory` field
  (`{source, unit_name, listing_url, person_url}`) populated by joining
  `directory_listings`; absent for CISUC people, so older UIs and every
  existing test see the exact current shape. **No research-group chip is
  ever rendered for them**: `group_shares` comes out empty from
  `_person_group_shares` (no project history), and the empty state is the
  correct display, not a gap to fill.
* **Profile page** (keyed on slug, existing route): for a UC-directory
  person it must state the affiliation and link the live `listing_url` /
  `person_url` ("Listed in: Faculdade de Economia (FEUC) — Docentes"),
  and render its CISUC sections (projects, groups, collaborators-via-
  projects) as honest empties with that line as the explanation — a
  profile that looks broken reads as a bug (the silent-degradation house
  failure mode). Coder must test the zero-project profile path explicitly.
* **Metrics** (`sql/thesis_attribution_coverage.sql`,
  `sql/thesis_match_tiers.sql`, the coverage tiles):
  * The existing within-CISUC tile keeps its **exact current query and
    definition** (resolved = `person_slug IS NOT NULL`, same universe).
    Its value may rise — that is the feature working — but what it counts
    does not change.
  * A corpus-wide supervisor-edge tile is added as a **separate number**,
    never merged with it.
  * Both gain a source split ("of which: CISUC directory / UC unit
    directories") via the `directory_listings` join, so no reader can
    mistake a FEUC professor for a CISUC member at the aggregate level
    either.
  * `collab.py:488`'s `total_people` label must say what it now counts
    ("people across all UC directories") or filter to CISUC — Coder picks
    with the UX surface in view; silence is the one wrong option.

## 5. Decision 5 — the classification instrument for the leftover list

New offline script **`analysis/classify_unmatched_supervisors.py`**,
extending the `audit_unmatched_supervisors.py` precedent (same guards: pure
triage, no verdicts, no slugs in its output, **`name_match.py` must never
import it**, output lands in `.tmp/`/the Kanban report, never in `data/`).
Differences from the existing audit, which stays as-is for its own
CISUC-touching universe:

* **Universe: corpus-wide** — all post-rebuild unmatched supervisor names
  (Frederico's scope does not respect the 569 boundary).
* Per name it emits a dossier: edge count, handles, thesis years,
  the theses' OAI sets (which faculty's corpus it sits in, via
  `thesis_sets`), surname-bearing rows and superset/wrong-ended shape flags
  computed against **both** pools, and whether the name's thesis-side
  faculty has a harvested unit in the registry.
* From those signals it assigns a **`suggested_class`** with the reasoning
  attached, over the three classes the PO fixed (binding, three, never
  two):
  1. **never-uc** — foreign co-supervisor / external examiner (no shape
     flag, no surname-bearing row, external evidence where known);
  2. **former-uc** — was UC staff, absent from every CURRENT directory
     (retired/left/deceased): thesis-side unit IS harvested, thesis years
     old, no current listing fits. **Counted separately, never folded into
     class 1** — this is the real ceiling, and if it is large it goes back
     to the PO as a product decision (hand-curation vs acceptance), not to
     a Coder;
  3. **resolvable-missed** — a shape flag or live-page evidence says this
     pass should have caught it: reported as a bug, each one either fixed
     (parser/registry/curated instrument) or explained before the card
     closes.
* `suggested_class` is a triage prior, not a verdict: the PO's completeness
  bar — **every top-100 name individually resolved or classified, the tail
  reported in aggregate per class** — is met by a human/Coder review pass
  over the dossiers, with the final class recorded in the report (and the
  seed, where class 1 earns an `external_people.json` entry worth the
  display). The report records the script's version and inputs so the
  numbers are reproducible.

Rejected: folding this into `audit_unmatched_supervisors.py` (different
universe and different question — "is this worth review" vs "which of three
product classes"; overloading it would quietly change the existing audit's
published numbers), and any output format a matcher could ingest (no
name→slug pairs anywhere in it).

## 6. Sequencing, verification, deployment — binding on the Coder

0. **Do not start until sibling run `38e7563f…` has ended**, then
   `git log`/`git status` the repo and resolve the corpus gap first: the
   2509 untracked `estudo_geral/*.md` + the 2690-entry manifest MUST be
   committed (by the sibling if its card covers it — check before
   duplicating; otherwise as this card's step 0) so that
   `git ls-files estudo_geral` count == manifest count == seed thesis
   count. Until that holds, the mandated rebuild-equals-committed-seed
   check is unsatisfiable and every downstream claim is unverifiable.
1. **Work in an isolated git worktree**
   (`aw-autoskill-concurrent-coder-worktree-isolation`); never edit the
   shared tree. The seed is a committed binary — merge conflicts on it are
   unresolvable; rebase and rebuild rather than ever merging the file.
2. **Follow `aw-autoskill-uc-phd-identity-match-verification` verbatim.**
   The row-diff acceptance for this card is sharp: the `thesis_people`
   delta must consist **only** of previously-`unmatched` supervisor rows
   gaining a slug (plus their note). Zero author rows change; zero pass-1
   verdicts change; `author_conflicts` stays 0; golden fixture green
   without edits; `"Fonseca, José Carlos"` does not resolve to
   `carlos-fonseca`; `"Cruz, Luís Alberto da Silva"` does not resolve to
   `luis-silva`.
3. **`external_people.json` will fire.** Any supervisor mention pass 2 now
   resolves that has an `external_people` entry hard-fails the build by
   design (`analysis/external_people.py:154-160`) — remove those entries in
   the same commit, as the validator's message says.
4. **The 2 stale tests in `tests/test_build_thesis_facts.py` are CUT from
   this card** — the sibling run owns them. Do not duplicate.
5. **Model routing:** anything touching `match_name`/`name_match.py`
   (the `Person.source` field, `_one_folded_name`) or
   `resolve_author_conflicts`' vicinity is coder-opus work per the card.
6. **Deployment tail:** back up
   `.aw-workspace/data/aw-app-uc-phd/cisuc.sqlite3`, copy the rebuilt seed
   over it, verify `/api/theses` and the coverage tiles live
   (`aw-autoskill-local-workspace-api-testing`); commit on master, release
   bot owns the version bump.
7. **Report on the card** (PO's format): mechanism and why; before/after
   corpus-wide (630/4570 → measured) AND within-CISUC (636/1065 → measured)
   as two numbers; the 10 spike names each with its directory row URL; the
   top-100 ledger; the three leftover classes counted separately, class 2
   explicitly flagged back to the PO.

## 7. What this makes harder later

* **`people` stops meaning "CISUC person".** Every future consumer that
  wants a CISUC-scoped claim must join (or exclude) `directory_listings`;
  the scope is one join away, but forgetting the join now produces a wrong
  *label* rather than a wrong count. The tile source-split and the profile
  badge are the mitigations that keep the distinction visible.
* **Pass order becomes a semantic fact.** A name's resolution can depend on
  which pool answered first: if CISUC's roster later grows a person pass 2
  already resolved to a unit slug, the name flips pools on the next
  rebuild (old slug orphaned, edges move). Mitigation: `possible_duplicates`
  surfacing + an alias verdict merging the two slugs — but it is a standing
  curation tax.
* **Alias-verdict volume grows structurally.** Most CISUC members also
  appear on FCTUC/their faculty's page; each cross-source duplicate that
  ever matters (carries thesis edges or shows in a profile) needs a
  reviewed verdict. Suggest-only detection will report hundreds of pairs;
  review is prioritised by edges-carried, and the backlog is visible, not
  silent.
* **The ceiling is the directory's currency, permanently** (class 2). Every
  per-unit page publishes current staff; the corpus starts in the 1990s. No
  scraper iteration fixes this — only a curated historical-person
  instrument (a PO decision deliberately NOT designed here) or acceptance.
* **The seed grows** by a few thousand `people` rows and listings —
  negligible for SQLite, but the committed-binary diff gets larger on every
  harvest refresh.
* **`exact`'s docstring drifts**: token-set equality is now "with the
  publishing directory's form", not "with CISUC's published identity".
  `docs/thesis-attribution.md` gets an amendment section recording this
  design (the Coder writes it; this file is the source).

## 8. Risks for the Coder (the non-obvious breakage)

1. **The corpus-not-committed trap (§0/§6.0)** is the one that silently
   invalidates everything: a clean worktree rebuild "succeeds" at 181
   theses and every number you then measure is fiction. Assert
   `theses == 2690` in the build output before trusting anything.
2. **Two caches, not one.** `match_name` results are cached by `name_raw`
   (`build_thesis_facts.py` `cache` dict); pass 2 needs its own cache, and
   a name must never read pass-2 results for its author mentions. The
   per-role consultation is the seam bugs will hide in — test (2) in §2
   exists for it.
3. **`_one_folded_name` tightening must be invisible on the legacy
   corpus.** If the golden fixture or the row-diff shows ANY pass-1 change
   after adding `Person.source`, the default wiring is wrong — stop and
   fix rather than regenerate fixtures.
4. **FEUC needs a real browser** for the snapshot (plain WebFetch/requests
   403s). Use the gateway browser MCP, save the full rendered HTML, and
   note in the run report that FEUC came from a snapshot. Budget the same
   fallback for any other unit that 403s.
5. **Portuguese name folding at harvest:** unit pages publish accented,
   sometimes comma-formed names; slugs must be derived with the existing
   `fold`-style normalisation, not a new ad-hoc one, or dedup-by-URL and
   collision refusal misfire.
6. **The profile page has never rendered a zero-project person** that is
   nonetheless "resolved". Test it; an exception or a blank page there is
   this feature's most visible failure.
7. **Don't trust the harvest's own coverage**: `expected_min` floors are
   set from the first manual look at each page; record the actual counts
   in `units.py` comments so the next person can tell drift from breakage.

## 9. Runner-up, recorded

The genuine runner-up was the **merged single pool with source-aware
ambiguity** (§2 rejected #1): it is more honest about pre-existing CISUC
false positives, at the cost of mass demotions, tile regression and an
unreviewable diff. If Frederico ever asks "is the CISUC match for name X
actually right, now that we can see all of UC?", the answer path already
exists in this design — `cross_directory_fits` is that question, computed
on every build and reported rather than acted on. If its review ever shows
systematic pass-1 wrong-person matches, THAT evidence reopens the merged-
pool decision; nothing else should.
