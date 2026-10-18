# uc-phd — João Campos multi-thesis author + collaborator-match audit at 2690 theses

Architect decision for Kanban card `3fd5bf3b-9510-8128-958f-e1d6b9e63660`
("uc-phd: fix stale tests + João Campos author-conflict + audit collaborator
match coverage at 2690-thesis scale"). Written 2026-10-18. Repo:
`/opt/aw-workspace/repos/aw-app-uc-phd`.

Scope is the PO's message on that card (D1–D4); this document designs D2's
mechanism and D3's audit method. D1 (stale tests) and D4 (report) need no
design beyond the constraints restated in §6.

## 0. Verified facts the design rests on (measured 2026-10-18, live `data/cisuc.sqlite3`)

All PO numbers re-verified: 2690 theses, 905 `people` rows, thesis_people
exact=176 / confident=705 / ambiguous=81 / unmatched=6312; 569 CISUC-touching
theses; within them supervisor edges exact=113 + confident=523 = 636 matched,
426 unmatched (358 distinct names), 3 ambiguous.

Three facts that **correct the card**:

* **João Campos's root cause is misdescribed.** "Campos, João Rodrigues de"
  tokenizes to `{campos, joao, rodrigues}` — "de" is a connector,
  dropped (`analysis/name_match.py:89`) — which IS token-set-equal to the
  CISUC row "João Rodrigues Campos". Both claims are set-equal; the gate
  demotes because `resolve_author_conflicts` picks a keeper only when
  **exactly one** claimant is set-equal (`analysis/build_thesis_facts.py:238`:
  `keeper = equal[0] if len(equal) == 1 else None`). Two legitimate claims by
  the same person → `len(equal)==2` → both demoted. The demotion note then
  lies: it prints the no-keeper text "none of them equals the CISUC row
  exactly" (`build_thesis_facts.py:249-253`) when in fact both do.
* **João's class has exactly one member.** Of the 76 distinct ambiguous
  author names: 68 are gate demotions, 8 are matcher-ambiguous
  (multiple candidates fit, `name_match.py:452`). Of the 68, only
  `joao-campos` is "one identical raw name claiming >1 thesis"; the other 67
  belong to 28 slugs each claimed by *different* raw names — the
  Pedro-Martins shape the gate exists to catch, correctly demoted. D2 fixes
  João's class and must not widen into the 28.
* **The PO's candidate frequency cuts are empty inside the audit universe.**
  Within the 358: frequency distribution is {1: 313, 2: 30, 3: 11, 4: 2,
  6: 2}. "≥5 theses" keeps 2 names; "≥10" keeps 0. The 151/24 numbers were
  corpus-wide (Boaventura de Sousa Santos et al., outside the universe).
  The cut has to be ≥2 (45 names, 113 of 426 edges) or the audit reviews
  almost nothing.

**PO assumption confirmed:** `people` is CISUC-scoped by design, not by
accident. `analysis/external_people.py` exists precisely so that people the
directory is *right* to omit get display metadata without ever getting a row;
`scraper/` syncs only the CISUC directory; every matcher docstring treats
`people` as "CISUC's own published roster". Nothing in the codebase wants a
UC-wide table.

## 1. D2 — the mechanism: a reviewed multi-thesis-author fact, not a rule relaxation

### The approach

Add a fourth curated file, **`data/multi_thesis_authors.json`**, following
exactly the pattern of its three siblings (`person_aliases.json`,
`person_full_names.json`, `external_people.json`): a human-reviewed,
evidence-carrying fact at the person grain, validated loudly at build time,
never derived from names. An entry says "this CISUC person genuinely authored
these N theses in this corpus", keyed on **slug + the exhaustive list of
handles** — never on a thesis name.

`resolve_author_conflicts` consults it: claims on `(slug, handle)` pairs the
file enumerates are exempt from the one-person-one-thesis demotion and keep
their slug. Any claim on that slug for a handle **not** enumerated is demoted
as today, with a note citing the curated entry ("the reviewed entry for
joao-campos lists 10316/83144 and 10316/100421; this claim is not among
them"). The matching rules in `name_match.py` are untouched — the file never
*creates* a match, it only prevents the withdrawal of two matches the matcher
already made independently. That is the same side of the option-C line the
other three files live on (docs/thesis-attribution.md §"The accepted fourth
option").

First entry: `joao-campos`, handles `10316/83144` (MSc 2017) +
`10316/100421` (PhD 2021), with `source_url`/`evidence` the Coder gathers
from the Estudo Geral records and the CISUC page (same evidence discipline as
the siblings — mandatory, not decoration).

### Where it lands

* **`data/multi_thesis_authors.json`** — new. Same `{note, entries}` shape as
  the siblings.
* **`analysis/multi_thesis_authors.py`** — new, modeled on
  `analysis/external_people.py` (`load_entries` / `validate` / `write_table`).
  Validation refuses, loudly, inside the build transaction: a slug not in
  `people` (post-canonical — compare against `identity_of`, since
  `people_rows` writes canonical slugs, `build_thesis_facts.py:150-168`), a
  handle not in the corpus, and — the staleness guard, mirroring
  `external_people`'s `unmatched` check — an entry whose enumerated claims
  don't all actually resolve to that slug at exact/confident. An obsolete
  entry breaks the build until someone edits the file; that is the house trade.
* **`analysis/build_thesis_facts.py`**:
  * `resolve_author_conflicts()` (line 201) gains the exemption parameter.
    Exempt claims never enter `claimants` grouping (or are filtered before
    keeper selection — Coder's choice of seam); everything else is unchanged.
  * While in there, fix the lying note: the no-keeper branch must distinguish
    "none of the claims equals the CISUC row" from "several claims equal it
    and nothing separates them" (`build_thesis_facts.py:249-253`). Today's
    text misled the card's own root-cause analysis; the note is the audit
    trail and has to be true.
  * `build()` wires the new file in with its own `--multi-thesis-authors`
    CLI arg, like the other three (`build_thesis_facts.py:372-399`).
* **`scraper/schema.sql:231`** — **this is the part a gate-only fix would
  miss.** `idx_one_thesis_per_resolved_author` is a partial UNIQUE index on
  `thesis_people(person_slug) WHERE role='author' AND person_slug IS NOT
  NULL`; João keeping his slug on both theses violates it at INSERT no matter
  what the gate says. SQLite partial-index predicates cannot subquery, so the
  backstop moves from an index to a **BEFORE INSERT trigger** on
  `thesis_people` that RAISEs unless the colliding `(person_slug, handle)`
  pairs are all present in a new `multi_thesis_authors` seed table (written
  by `write_table`, like its siblings). The trigger preserves the index's
  documented guarantee — "a re-merge is impossible to commit even if the
  build pass is bypassed" — instead of silently downgrading it to test-only.
  Because `build()` only DELETEs rows and `schema.sql` is `IF NOT EXISTS`
  (`build_thesis_facts.py:403-410`), the schema change must include
  `DROP INDEX IF EXISTS idx_one_thesis_per_resolved_author` or rebuilding the
  committed seed file in place keeps the stale index and still fails.
* **Tests** (`tests/test_build_thesis_facts.py`): (1) João-shaped fixture —
  identical raw name, two handles, exemption entry → both rows
  resolved with the slug; (2) a third, unlisted claim on an exempted slug is
  still demoted; (3) an exemption entry naming a claim that doesn't resolve
  fails validation; (4) existing conflict tests green, verbatim —
  `test_one_supervisor_across_many_theses_is_not_a_conflict`,
  `test_with_no_set_equal_claimant_every_claimant_is_demoted`,
  `test_the_set_equality_claimant_keeps_the_slug_and_the_subset_claim_is_demoted`;
  (5) the trigger itself: direct INSERT of a second resolved author row for a
  non-exempted slug raises.

The 28 different-name conflict slugs and the 8 matcher-ambiguous names are
**not** this card's problem and the fix must not move any of them: the
corpus-wide row-diff (§5) proves it — the only thesis_people delta allowed
from D2 is João's two rows.

### Rejected alternatives

* **(b) on the card — accept all-byte-identical claimants under one slug as
  keepers, as a rule.** Rejected because byte-identical is the *maximally
  similar* pair of claims, and the corpus's core trap is that similarity is
  not identity: two "Martins, Pedro" theses years apart could be two real
  people, and the matcher's `cache[name_raw]` (`build_thesis_facts.py:173`)
  already can't tell them apart — the gate's demotion is the only thing that
  declines to assert. At 2690 theses with short Portuguese names the
  collision prior is real. Every automated shortcut to a higher number in
  this repo's history produced a confidently wrong person (relaxations A, B);
  this would be the third.
* **Exempting by name pattern (e.g. "≥3 tokens and identical ⇒ safe").**
  Same rejection, gradual version: it derives an identity decision from a
  name. The whole post-relaxation architecture is built on refusing that.
* **Dropping the schema backstop and relying on the build pass + tests.**
  Rejected: the index comment says why it exists; a trigger preserves the
  guarantee at equal strength.

## 2. D3 — the audit method

**Universe:** the 358 distinct unmatched supervisor names on the 569
CISUC-touching theses (426 edges). Everything outside it is counted once
under Frederico's accepted justification, not investigated.

### 2.1 The cut

**Individually review every name with ≥2 in-universe edges: 45 names,
113 edges (27% of unmatched edge mass).** Justification: the in-universe
distribution is flat (max 6); ≥2 is the only cut that is both non-empty and
bounded (45 names is one session of individual review); a repeated name is
both worth more (≥2 recoverable edges) and likelier to be a
differently-published CISUC person than a one-off external committee member.
The 313 singletons get a **uniform random sample of 30** (seeded, seed
recorded in the report) individually checked the same way; by the rule of
three, 0/30 CISUC-present bounds the singleton false-negative rate below
~10% at 95% confidence, and each positive found is both fixed and counted
against that bound honestly in D4. The 3 in-universe ambiguous supervisor
edges are reviewed too (trivial count).

### 2.2 The discriminator (replacing surname-overlap, which the PO showed drowns in 1361 false hits)

A real "in the directory under a different published form" miss has, on this
repo's entire recorded history, exactly two shapes — both documented and both
with a curated instrument already built:

* **Superset shape** (the `jose-fonseca-1` / Cardoso/Fonseca class, backlog
  card `matching:uc-phd-fits-extra-cisuc-initial`): the CISUC display name is
  a *superset* of the thesis form — surname matches and **every thesis token
  appears among the candidate's tokens**, but the candidate carries extra
  non-initial tokens so `_fits()` rule 2 rejects it. Detect by running the
  fit **in reverse** with the existing `tokenize`/`split_thesis_name`
  machinery.
* **Wrong-ended shape** (the Dourado/Penousal class, fixed person-by-person
  via `person_full_names.json`): candidate shares the **primary given name
  plus ≥1 further given token** but fails the surname test — CISUC published
  a form ending in a different name.

Score all 358 names against all 905 rows with these two tests (pure
functions, milliseconds). A name flagged by either shape is "plausibly
present — review the flagged candidates"; a name flagged by neither and (for
the 45+30 reviewed names) absent from a live `cisuc.uc.pt/en/people` search
is "confirmed absent". Surname-mere-overlap is explicitly **not** a flag.
This scorer lives in a one-off audit script (suggested
`analysis/audit_unmatched_supervisors.py` or `.tmp/` — Coder's call; it must
**never** be imported by `name_match.py`, it is triage, not matching).

The live-directory check on reviewed names also catches the **scrape-gap
class** (the Sven Stork precedent: a real CISUC page, no `people` row). If
found, re-running the existing `scraper/` people sync is in scope — it keeps
the CISUC scope the PO fixed — with the row-diff discipline of §5 applied to
the `people` table too.

### 2.3 Instruments for what the review finds

Per reviewed name, exactly one of:

| finding | instrument | notes |
|---|---|---|
| CISUC person, superset/wrong-ended published form | `data/person_full_names.json` entry (slug-keyed, evidence mandatory) | the gated `_fits_full` path resolves it; capped at `confident` by design |
| CISUC lists the person under 2+ slugs | `data/person_aliases.json` verdict | `same` with canonical, or `distinct` to stop re-flagging |
| real CISUC page, no `people` row | scraper people sync re-run | then re-audit the name |
| genuinely absent from CISUC | counted; optionally `data/external_people.json` entry if display metadata is worth it (mention-keyed) | **this is Frederico's accepted justification — named per name in D4** |

**No `_fits()` change ships on this card.** If the audit measures a large
class (>15 of the 45) sharing one mechanical shape curation can't cover, that
is a written recommendation with counts to PO/Architect (the
`matching:uc-phd-fits-extra-cisuc-initial` backlog card is the existing home
for the superset class), not an implementation. Both rejected relaxations
stay rejected.

**Known collision to re-assert, not re-litigate:** "Fonseca, José Carlos" may
be resolved to `jose-fonseca-1` **via a reviewed full name** (the superset
instrument — `external_people.json`'s own entry for him anticipates exactly
this), but must never land on `carlos-fonseca` (relaxation A's wrong person).
If resolved, his two `external_people` entries MUST be removed in the same
commit — `external_people.validate()` hard-fails the build on an entry whose
row is no longer `unmatched` (`analysis/external_people.py:48-52`). This
applies to **every** name the audit resolves: grep `external_people.json`
for it first. Same staleness check applies to any D2/D3 change.

### 2.4 The 9 same-folded-name/different-slug cases

(`luis-rodrigues`/`-2`/`-3`, `jcfgonc`/`joao-goncalves`, `nuno-silva-1`/`-2`,
`joao-bicker`/`-1`.) Verify each against the live CISUC pages with the
evidence types that settled the 12 existing verdicts (email local part,
ORCID, project overlap, publication sets — docs/thesis-attribution.md,
amendment of 2026-10-15), and record a verdict per pair in
`person_aliases.json` — `same` with canonical, or `distinct`. The
counterexample to keep in view: `nuno-silva-1`/`nuno-silva-4` satisfy every
automated similarity test and are two real people. No pair is merged without
written evidence; `_one_folded_name` keeping all rows is the correct
*interim* behaviour, the verdicts make it permanent or correct it.

## 3. What this makes harder later

* **Every genuine multi-thesis author needs a curated entry forever.** At UC
  scale (MSc+PhD both deposited) this recurs. Acceptable: the demotion note
  now names the exemption file, so each new case is a 10-minute reviewed
  entry, and the alternative (a rule) is the false-merge machine. If entries
  ever exceed ~20, that volume is itself the evidence to bring the rule
  discussion back — with data.
* **The trigger makes `thesis_people` inserts order/content-sensitive** in a
  way an index wasn't (the exemption table must be written before
  thesis_people rows). Build order is already disciplined
  (`build_thesis_facts.py:412-446` — aliases before people, externals after
  rows); this adds one more ordering fact the build docstring must state.
* **The ≥2 cut leaves 313 singleton names unreviewed by design.** The sample
  bounds the error; it doesn't make it zero. D4 must say so in plain words.
* **`external_people.json` acquires more coupling:** every future matcher or
  curation improvement that resolves a name with an entry breaks the build
  until the entry is removed. Already the designed behaviour; the audit will
  exercise it for the first time at volume.

## 4. Risks for the Coder

1. **The schema backstop (§1) is the trap.** A gate-only fix passes every
   unit test that doesn't rebuild the real seed, then dies on IntegrityError
   mid-rebuild — or worse, survives because the stale index was never
   dropped from the committed DB file and the new trigger was never created
   in it. Rebuild into a **fresh** file and byte-compare against a rebuild
   into the existing file; both must succeed and match.
2. **Green tests are explicitly not sufficient** (the test module docstring
   says so). Every curated-file change is measured corpus-wide: full
   `thesis_people` row-diff before/after, every changed row inspected, new
   matches listed by name in the Kanban report, and the two known false
   positives re-asserted by running `match_name` directly: "Fonseca, José
   Carlos" must not resolve to `carlos-fonseca`; "Cruz, Luís Alberto da
   Silva" must not resolve to `luis-silva` (he has an `external_people`
   entry — he is IT/DEEC, not CISUC).
3. **Follow the `aw-autoskill-uc-phd-identity-match-verification` recipe
   verbatim**: git worktree (shared tree — sibling agents are active),
   isolated DB copies, rebuild == committed-seed assertion, clean up
   worktrees.
4. **`match_name` caches by `name_raw`** (`build_thesis_facts.py:173`) — two
   identical raw strings can never resolve differently. Don't design any
   per-thesis behaviour into matching; only the conflict pass sees handles.
5. **D1's numbers are re-derived, not copied**: run
   `python -m analysis.build_thesis_facts` yourself; the card's counts
   (7274/21884/4570) may be stale against the current checkout. Baseline
   suite state: 113 passed / 2 failed.
6. **Deployment tail:** after landing, back up
   `.aw-workspace/data/aw-app-uc-phd/cisuc.sqlite3`, copy the rebuilt seed
   over it, confirm `/api/collab/summary` and `/api/collab/graph` reflect the
   new edges. The app opens the DB read-only; no restart should be needed,
   but verify.

## 5. Acceptance (what D4 must contain, per the PO's reframing)

A Kanban comment in English, non-engineer-readable, answering "is it 100%
and if not why not":

* (a) supervisor-edge resolution within the 569 CISUC-touching theses:
  before 636/1065 (60%) → after (measured), and (b) corpus-wide context
  number with the scope justification stated once.
* Names individually reviewed (45 + 30-sample + 3 ambiguous) vs sampled-only
  (283 singletons), newly resolved (by instrument) vs confirmed absent —
  each absent name above the cut with its named, evidenced reason.
* João Campos resolving on both handles; the 9 slug-pair verdicts.
* The final remaining unmatched count with the justification applied, and
  the explicit statement that the ~2369 unmatched author names are PhD/MSc
  graduates who are structurally not CISUC staff (counted, reason named
  once).

## 6. Out of scope (restating the PO's fence — binding)

UC-wide people directory (recommendation-only if evidence appears); OAI
harvest re-runs or set-list changes; relaxations A/B of `_fits()`;
hand-researching the singleton tails; collaboration-graph UI.
