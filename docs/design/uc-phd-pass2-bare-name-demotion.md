# uc-phd — demote pass-2 matches that rest on a bare 2-token published name

Architect decision for Kanban card `3fe5bf3b-9510-81a5-a40e-d54296cef50d`
("uc-phd: URGENT — pass-2 UC-directory matcher false-merges multiple real
people under bare 2-word published names"). Written 2026-10-19. Repo:
`/opt/aw-workspace/repos/aw-app-uc-phd`. Root cause is the Debugger's, on the
card, confirmed experimentally — this document only decides the fix.

## 0. Facts the design rests on (measured 2026-10-19, live `data/cisuc.sqlite3` @ v0.23.0)

* Pass 2 has resolved **1471 supervisor rows across 527 distinct names**:
  1264 `exact` (token-set-equal), 207 `confident` (subset rule).
* The failure shape, per the Debugger: a `confident` fit whose candidate
  display tokens ⊆ **floor** = `set(surname_tokens) ∪ {first given token}` —
  the exact shape `minimal_evidence_matches()`
  (`analysis/build_thesis_facts.py:525-552`) already names "the weakest
  evidence this matcher ever acts on", but that net sees only author rows ×
  the CISUC pool, and pass 2 is only ever supervisor rows × the UC pool.
* **The pool's `Person.name` is not the richest evidence the harvest holds.**
  `_UC_PEOPLE_SQL` (`build_thesis_facts.py:149-156`) flattens to `people.name`,
  but `directory_listings.name_as_published` keeps each unit's verbatim form:
  Elísio Estanque's slug (`uc--feuc--elisio-guerreiro-estanque`) was derived
  from a listing form carrying "Guerreiro" that `people.name` ("Elísio
  Estanque") dropped; Pedro Góis's FEUC listing publishes his full name while
  his CES listing publishes "Góis, Pedro".
* Measured against the live seed, the rule in §1 **demotes 93 rows (49
  distinct names, 39 slugs)** and **rescues 40 rows (9 slugs)** via a richer
  listing form. All three confirmed merges (João Rodrigues ×3 variants, the
  Cruz sentinel, "Góis, Pedro Miguel Pimenta") are demoted; all 16 of Elísio
  Estanque's resolved rows survive (9 set-equal `exact` + 7 rescued by the
  "Guerreiro" listing form); the correct Góis row (`10316/114758`) is rescued
  by the FEUC full-form listing. The demoted list also catches at least one
  further real merge the card predicted exists in the 73-slug list:
  "Carvalho, António José Olaio (Correia de)" (the artist António Olaio)
  currently resolved to `uc--feuc--antonio-manuel-simoes-lopes-paiva-de-carvalho`,
  a different FEUC professor, published bare as "António Carvalho".
* Supervisor coverage cost: 93 of 2101 resolved supervisor rows (−4.4% of
  resolved rows; 2101→2008 of 4574 edges). The removed edges are wrong or
  unverifiable, so this is the number becoming honest, not a regression.
* `person_full_names.py::validate` checks slugs against the live `people`
  table, not a CISUC-only set, and `_UC_PEOPLE_SQL` already joins
  `person_full_names` — the curation rescue path works for `uc--*` slugs
  with zero new plumbing.
* `directory_listings.person_url` exists but is not selected into `Person`
  (Debugger verified). It is also non-probative for this failure: the URL is
  the page that published the bare name, so it cannot confirm middle names.

## 1. The decision

**Pass 2 refuses to act on floor-evidence subset matches unless a richer
harvested form corroborates the thesis name.** Concretely, in
`people_rows`'s pass-2 acceptance (`build_thesis_facts.py:296-301`), a
`uc_result` with status `CONFIDENT` is **demoted to report-only** (the row
keeps its pass-1 `unmatched` verdict, exactly as a pass-2 `ambiguous` already
does) when all of:

1. the matched person's pool display tokens ⊆ floor(`name_raw`), where
   floor = `set(surname_tokens) ∪ {first given token}` — **one shared helper**
   with `minimal_evidence_matches()`, extracted so the two definitions cannot
   drift;
2. the thesis token set is a strict superset of the candidate's (there exist
   given names the candidate cannot confirm);
3. **no richer evidence corroborates the full thesis name** — evidence being
   every `directory_listings.name_as_published` for that slug plus its
   reviewed `person_full_names` entry, and "corroborates" meaning every
   thesis token appears in the evidence form or is a single letter prefixing
   one of its tokens (the `_fits_full` convention, thesis-side initials only).

A match kept via condition 3 stays `confident` and its note names the
corroborating listing (`via UC directory feuc, corroborated by FEUC's
published form "…": …`), so every such row is auditable without a join.

Pass-2 `EXACT` rows (set-equal: the thesis name is itself bare) are **not
demoted** — a bare thesis mention has no middle names to contradict, this is
the same evidence grade pass 1 has always accepted, and multi-candidate
collisions already come out `ambiguous`. They become visible instead (§2).

Pass 1 (CISUC pool) is **untouched, deliberately**. The row-diff of the
rebuild must show zero pass-1 changes. Changing pass-1 behaviour would churn
every published number, re-litigate the golden fixture, and is a Product
Owner conversation, not a regression fix. Pass 1's bare-floor risk is real
(pedro-martins) but bounded by the author-conflict gate, the
`minimal_evidence_matches` net, and a far lower bare-name rate than the UC
pool's 312/1449.

### Why the corroboration check is not measured-rejected relaxation B

Relaxation B failed because a mirror comparison against *published display
names* **created** matches (`"Cruz, Luís Alberto da Silva"` → Luís Silva).
Here the mirror comparison runs only on candidates `_fits()` already
accepted, and only to decide *confident vs demoted* — it is monotone in the
safe direction: it can prevent a demotion, it can never create an edge or
turn a rejection into a fit. A test must pin this fence (§4).

## 2. Visibility net extended

`minimal_evidence_matches()` is generalised (or given a sibling — Coder's
call on mechanics) so the build report also lists **every pass-2 resolved
supervisor row whose evidence sits at the floor** — after §1 these are the
kept set-equal `exact` rows (127 rows today) and any corroboration-rescued
rows. New report keys, names indicative: `uc_pool_bare_floor_demotions`
(name_raw, candidate slug/name, the uncorroborated tokens) and
`uc_pool_minimal_evidence` (kept rows at the floor). The demotions list is
the instrument the next curation pass works from.

## 3. What was rejected, and why

* **Card option (a) as stated — require ≥2 published given tokens, with a
  `person_url` escape.** The floor formulation subsumes the token-count rule
  (it handles multi-token surnames and matches the codebase's existing
  definition of weakest evidence), and the `person_url` escape is both
  unplumbed (Debugger verified) and non-probative — the URL points at the
  page that published the bare name.
* **Card option (b) — demote every pass-2 match onto a bare-published
  person, set-equal included.** Over-broad: it strips set-equal matches that
  carry the same evidence grade pass 1 has always accepted, costs ~127
  additional rows for no measured failure (all three confirmed merges are
  superset-shaped), and Elísio Estanque's 9 `exact` rows would go with them.
* **Visibility-only (extend `minimal_evidence_matches` to supervisors × UC
  pool, change no verdicts).** It leaves three confirmed-wrong attributions
  live in production data; a list nobody is forced to act on is how
  pedro-martins happened the first time.
* **Rebuilding the pool one `Person` per listing** (so the rich FEUC form
  competes with the bare CES form). It does not fix the failure — `_fits()`
  still accepts the bare listing row — and it perturbs `_one_folded_name`
  semantics for every UC person, a much wider blast radius than the
  acceptance-seam guard.
* **A rare-given-name frequency exemption** (keep "Urbano Nunes"-shaped
  matches because the given name is rare in the pool). It is a similarity
  heuristic — the exact class of identity-from-name-statistics inference
  this module's doctrine forbids, and unfalsifiable at the margin. Rescue of
  true-but-demoted matches goes through curation instead (§5).
* **Demoting to a written `ambiguous` row with the candidate attached.**
  Breaks the v0.23.0 row-level invariant (pass 2 only ever adds slugs to
  unmatched supervisor rows) for no reader benefit — the candidates are in
  the build report either way, and report-only is exactly how pass-2
  `ambiguous` is already handled.

## 4. Where it lands

* `analysis/build_thesis_facts.py` — floor helper extracted and shared with
  `minimal_evidence_matches()`; a listings-name map (`{slug: [name_as_published…]}`)
  loaded alongside `load_uc_directory_people`; the guard at the pass-2
  acceptance seam in `people_rows` (`:296-301`); report keys in `build()`
  (`:728-751` area). `match_name`/`_fits` in `analysis/name_match.py` are
  **not modified**.
* `data/person_full_names.json` — curated entries only where the evidence is
  real (none required by this fix itself; §5).
* `tests/test_uc_directory_match.py` — new fences: (i) bare 2-token UC
  person + strict-superset thesis name → row stays `unmatched`, demotion in
  the report; (ii) same but a second listing publishes the full form → stays
  `confident`, note names the listing; (iii) bare person + set-equal thesis
  name → still `exact`; (iv) ≥3-token published name + superset thesis →
  still `confident` (unchanged); (v) the corroboration check never turns a
  non-fitting candidate into a fit (the relaxation-B fence).
* `data/cisuc.sqlite3` — rebuilt seed, committed (it ships in the repo).
* `docs/thesis-attribution.md` — a short section recording this rule next to
  "Two rejected relaxations", so the next relaxation proposal sees it.

## 5. Coder scope (one delivery)

1. Implement §1 + §2 + tests (§4).
2. Rebuild the seed and run the full verification recipe
   (`aw-autoskill-uc-phd-identity-match-verification`), with the known traps:
   the 2509 newer corpus `.md` files are **untracked** — rebuild in the
   shared tree or copy them in (prior design doc §6 step 0); the repo venv's
   python symlink is dead — system `python3` with `PYTHONPATH=.`.
3. Row-diff gates: delta consists **only** of pass-2 supervisor rows losing
   a slug (expected 93 rows / 49 names / 39 slugs — if the number differs,
   stop and explain before committing); zero pass-1 changes; zero author-row
   changes; the three confirmed cases demoted; Elísio's 16 rows and Góis
   `10316/114758` unchanged.
4. **The 73-slug triage the card mandates**: re-run the >1-distinct-given-
   token-set scan per `uc--*` slug after the rebuild (the Debugger's script
   is session-lost; the scan is ~15 lines — distinct given-token-sets among
   resolved rows per slug, flag >1). Expected: the bare-floor merges
   collapse out. Every slug still flagged gets a manual verdict written into
   the delivery report: benign (connector/diacritic variants of one human)
   or a real merge — and a real merge not covered by §1 comes back to the
   Architect, not patched ad hoc.
5. From the demotions report, propose (not silently apply) curation
   candidates for the clearly-identifiable demoted true matches (e.g.
   "Nunes, Urbano José Carreira" → DEEC's Urbano Nunes) as
   `person_full_names.json` entries **with sourced evidence** (personal/unit
   page, Ciência Vitae). Entries without a checkable source don't ship.
6. Version bump is the release bot's, not hand-edited.

## 6. What this makes harder later

* **The supervisor coverage number drops** (2101→2008 resolved rows live) and
  the v0.23.0 headline regresses accordingly. Every future report must use
  the post-fix baseline.
* **312 bare-published UC people are now structurally unresolvable for
  superset thesis forms until curated.** Closing that gap at scale means
  harvesting richer forms (e.g. following `person_url` to per-person pages
  and recording full names through a reviewed pipeline) — new scope, PO's
  call, and the demotions report is the shopping list for it.
* The pass-1/pass-2 asymmetry (CISUC still accepts floor-evidence subsets)
  is now a documented inconsistency someone will eventually question; the
  answer is in §1 and it is deliberate.

## 7. Risks for the Coder

* The two caches (`cache`/`uc_cache`) memoise `NameMatch` by `name_raw`. The
  demotion is a *per-name* decision too (floor and corroboration depend only
  on `name_raw` + slug), so it can be applied on the cached result — but do
  not mutate the cached `NameMatch`, or `cross_directory_fits` and the
  report counters will silently see doctored inputs.
* A `confident` result can carry multiple people (alias-collapsed duplicate
  rows). The guard must evaluate the kept people's evidence, demoting only
  if every kept person fails corroboration.
* `minimal_evidence_matches`'s floor and the guard's floor must be one
  function. Two hand-copied definitions is how the next pool expansion
  reproduces this card.
* The committed seed is the artefact; green tests on the 181-thesis fixture
  corpus prove nothing about the 2690-thesis rebuild (two prior relaxations
  stayed green while wrong). The row-diff gates in §5.3 are the acceptance.
