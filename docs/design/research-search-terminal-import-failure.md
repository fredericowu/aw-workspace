# research-search: terminal import failure, a settled state for "not in any catalogue"

Card `4045bf3b-9510-810d-aa96-d3f3b0e84005`. Repo `tekflox/aw-app-research-search`,
scoped against `origin/master` at **v0.55.0** (`eef3f89`). The local checkout at
`/opt/aw-workspace/repos/aw-app-research-search` is at v0.50.0 and has no
`graph_import.py` at all; every citation below is `git show origin/master:<path>`.

**Outcome (fixed by the PO, not re-opened here):** a gray reference node whose
import failed because the work is in NO catalogue must settle into a visibly
terminal state that names that as a settled outcome, and must stop offering
Retry as the primary action. The design question delegated to this doc:
`import_jobs.error` is free prose written by an LLM, and nothing today
distinguishes terminal from retryable. Decide where that classification lives.

## 1. The decision

**The classification lives on the bail report as a structured reason code: an
optional `code` enum on the `graph_import_report` MCP contract, persisted in a
new nullable `import_jobs.error_code` column, and carried to the UI alongside
the existing `error` prose. Terminal-ness is decided app-side from the code
(`TERMINAL_CODES`, initially just `no_match`), and it is purely
presentational: the server never refuses a retry on a terminal row.**

Three load-bearing properties, each falsifiable:

1. **The agent reports the *category*, the app decides the *consequence*.**
   The agent is the only party that knows why it bailed (it ran the searches),
   so the code must come from it. But whether a code means "stop offering
   Retry" is an app-side constant, so a confused or future-drifted agent can
   never widen its own authority by inventing a code: an unknown code is
   stored as NULL and renders exactly like today's unclassified failure (red,
   Retry primary).

2. **`mark_report`'s anti-faking property is preserved by making terminal
   advisory, not enforced.** Success stays observed, never reported
   (`graph_import.py:9-17`), and `code` only ever rides `state='failed'`. The
   worst a wrong `no_match` can do is demote a button to a secondary "Retry
   anyway" link; it cannot close off an importable reference, because
   `start_import` (`graph_import.py:485-525`) keeps accepting new jobs for the
   ref regardless of the previous row's code. One extra click recovers from
   any misclassification. That bounded blast radius is what keeps this change
   small instead of a trust-machinery project.

3. **The computed TTL stays retryable.** `effective_state`
   (`graph_import.py:115-123`) is the single place the TTL turns an active row
   into `failed`/`TIMEOUT_ERROR`; it returns `code=None` on that branch, so a
   platform-killed run can never be swept into the terminal bucket.

## 2. The taxonomy

Three codes in the tool schema, one of them terminal:

| code | emitted when | terminal? |
|---|---|---|
| `no_match` | no catalogue result's normalized title matches (the Pan 2026 case) | **yes** |
| `ambiguous` | two or more plausible candidates the agent cannot choose between | no (see runner-up) |
| `tool_error` | a download/ingest tool error passed through verbatim | no |

Absent, unknown, or NULL code (old rows, dispatch-side `mark_failed`, TTL
timeouts, a stale installed agent that predates the schema): unclassified,
renders as today. The agent prompt gets one bias rule: when unsure between
`no_match` and `tool_error`, prefer `tool_error`. Wrong-toward-retryable costs
a futile run; wrong-toward-terminal costs a mislabeled settled state, and the
first is the cheaper mistake.

**`ambiguous` is deliberately NOT terminal in this card.** Retrying it is just
as deterministic a waste as `no_match`, but its correct settled treatment is a
human-pick affordance over the candidate list the agent already emits, which
is a different design and not this card's stated outcome (the PO fixed the
outcome to the no-catalogue-match class). Defining the code now is free and
future-proofs that card; flipping it terminal later is a one-line
`TERMINAL_CODES` edit plus a label. That is the runner-up decision: if the
futile-run cost of ambiguous retries shows up in practice first, flip it.

## 3. Where it lands

Everything below is on `origin/master` (v0.55.0) paths.

**3.1 Migration `migrations/0013_add_import_job_error_code.sql`** (0012 is the
latest). `ALTER TABLE "app__research-search__import_jobs" ADD COLUMN IF NOT
EXISTS error_code text;` Nullable, **no CHECK constraint**: the app validates
(`FAILURE_CODES`), and a CHECK would make every future code (the sibling
card's, `ambiguous`'s promotion) another migration. The existing CHECK on
`state` (`migrations/0009_graph_identity.sql:176-177`) is untouched, which is
most of why this is a column and not a new state (see rejection B).

**3.2 `research_search_app/graph_import.py`**
- New constants: `FAILURE_CODES = ("no_match", "ambiguous", "tool_error")`,
  `TERMINAL_CODES = frozenset({"no_match"})`.
- `mark_report` (line 219) gains `code: str | None = None`: normalized
  (strip/lower), persisted only with `state='failed'`, unknown values stored
  as NULL rather than refused (a schema-drifted agent must still be able to
  bail; the detail prose is the part that must never be lost).
- Every job SELECT (`get` 152, `active_for` 169, `since` 278, `active` 296)
  and `_job_row` (312) gain `error_code`.
- `effective_state` (115) returns a triple `(state, error, code)`; the TTL
  branch returns `("failed", TIMEOUT_ERROR, None)`. Both in-repo callers
  updated: `graph_db.import_states` and `delta_of`.
- `delta_of` (345) adds `"code"` to the websocket frame when non-null.
- `mark_failed` (255, dispatch-side) unchanged: code stays NULL, a dispatch
  failure is always retryable.

**3.3 `research_search_app/mcp_ingest.py`**
- `graph_import_report` schema entry (lines 249-282): optional `code` property,
  `enum: ["no_match", "ambiguous", "tool_error"]`, with per-code guidance in
  the description. `required` stays `["job_id", "state"]`.
- Dispatch branch (483-492): pass `args.get("code")` through to `mark_report`.

**3.4 `aw-app.json`, `research-reference-importer` `system_prompt`.** The BAIL
section's three bullets each name their code, plus the prefer-`tool_error`
bias rule. Contributed agents are reconciled on marketplace update, so the
prompt ships with the release; during any window where the installed agent
predates the schema, it simply emits no code and everything renders as today.

**3.5 `research_search_app/graph_db.py`**
- `_IMPORT_STATES` (1170) selects `error_code`; `import_states` (1177) returns
  it per key through the new 3-tuple `effective_state`.
- `_node_payload` (1227) emits `import_error_code`; the imported-node
  shadowing rule (1250-1256) clears it together with `import_error`.

**3.6 `ui/src/views/DocGraph.jsx`**
- Delta application (329-330) also sets `import_error_code` from
  `delta.code || null`.
- Panel derivation (486-490) merges code the same dual-source way state and
  error already are (`panelDelta?.code`, falling back to
  `panelNode.import_error_code`), then `panelTerminal = panelState ===
  'failed' && TERMINAL_CODES.has(panelCode)` (a small mirrored constant; the
  UI owns presentation, drift risk is named in §5).
- Terminal rendering: status label becomes a settled one, "Not in any
  catalogue. Likely not an indexed academic work.", shown instead of
  `IMPORT_STATUS_LABEL.failed`; the agent's detail prose stays (it is already
  good, per the PO); the action button is NOT rendered as the primary button.
  Instead a link-styled "Retry anyway" (small, below the detail) that still
  calls `startImport`. This satisfies acceptance (b) via "explicitly marked
  futile" rather than "impossible", on purpose: impossible is the
  silent-close-off the anti-faking requirement forbids.
- Failed-node hover tooltip (211-214) is keyed on `import_state="failed"` and
  needs no change; terminal rows still carry their prose in `import_error`.

**3.7 `ui/src/views/docGraphStyle.js`**
- New selector `node[import_state="failed"][import_error_code="no_match"]`
  with a settled style: muted gray dashed border instead of `failedBorder`
  red. Red stays reserved for retryable failures, so a settled node stops
  shouting; that distinct quiet visual IS the acknowledgement (see rejection
  D, no dismiss affordance).
- **Order matters**: this rule must sit AFTER the generic
  `import_state="failed"` rule (line 61), later-rule-wins, same mechanism that
  bit the imported-vs-failed pairing recorded in `_node_payload`'s comment.

**3.8 Tests** (extend, don't restructure): `tests/test_graph_import.py`
(mark_report code normalization and unknown-code-to-NULL, effective_state
triple incl. the TTL-yields-no-code case, delta_of frame), `test_graph_db.py`
(payload carries `import_error_code`, imported-shadow clears it),
`test_api_graph.py` (code survives the route). The fake in-memory db enforces
neither the uuid type nor CHECKs (that blindness already shipped one 500);
nothing in this design may rely on the db rejecting anything.

**Untouched:** `POST /graph/import` / `start_import` semantics (retry stays
allowed on every failed row), the resolution rungs in the agent prompt, the
bail prose, `mark_import_jobs_indexed`, the dedup rule, the TTL value, the
sibling card's download-fallback work.

## 4. The shared surface for the sibling card

The sibling (resolved-but-undownloadable, OpenAlex download fallback, card
`4045bf3b-9510-81ae`, Medium, lands first) should reuse, not duplicate:
whoever lands second inherits (a) the `error_code` column and the `code` wire
field end to end, (b) the panel's terminal branch keyed off
`import_error_code`, and (c) the settled node style selector pattern. Adding a
failure class there is: one enum value in the tool schema, one entry in
`FAILURE_CODES`, a terminal/retryable mapping decision, and a label. No new
plumbing. This is stated here so the second lander does not build a parallel
classification path; if that card ships its own panel treatment first, this
card adapts to it instead.

## 5. What this rejects, and why

- **A. App-side inference over the prose** (regex on "No catalogue match").
  The prose is written by an LLM under a prompt that lives in `aw-app.json`;
  editing the prompt would silently break classification with no test able to
  notice. The precedent that worked (`classify_failure_reason` on the
  failed-imports card) classified deterministic tool error strings, not model
  prose.
- **B. A new `unresolvable` value in `import_jobs.state`.** Requires altering
  the 0009 CHECK, and ripples through `ACTIVE_STATES`, `mark_report`'s state
  machine, `effective_state`, `_node_payload`, `IMPORT_STATUS_LABEL` and the
  style rules. Worse, it conflates lifecycle (did the job finish) with failure
  taxonomy (why), and hands the agent a bigger lever over the state machine
  that the observed-success design deliberately keeps small.
- **C. Server-side retry refusal on terminal rows.** Directly violates the
  anti-faking requirement: one mislabeled report would silently close off an
  importable reference. Terminal must stay presentational.
- **D. A dismiss/acknowledge affordance.** Needs a write path (endpoint plus
  an acknowledged marker read at first paint and in the ws loop) for a
  single-user app, when the actual complaint is "sits red forever". Making the
  settled state visually quiet (not red) removes the standing alarm without
  any new state. If a hide-this-node need emerges later it composes on top.
- **E. A bare `terminal: true` boolean from the agent.** Same plumbing cost as
  a code but loses the why, so the UI could not name the outcome ("not in any
  catalogue") and the sibling card could not reuse the field for a different
  class.

## 6. What this makes harder later

- **The code taxonomy is a cross-layer contract.** A new code touches the tool
  schema enum, `FAILURE_CODES`, the prompt, and (if visible) a UI label and
  style. Four files per code is accepted on the bet that codes stay rare.
- **`effective_state` changes arity.** Both consumers are in-repo
  (`import_states`, `delta_of`) and break loudly, but any future out-of-tree
  consumer of `import_jobs` now has three things to agree on with the UI
  (state, TTL, code semantics), not two. The module docstring's "every
  consumer must apply this same rule" warning gets one more clause.
- **Terminal-ness is app-side opinion, not data.** Re-deciding "ambiguous is
  terminal" is trivially cheap (by design), but it also means the DB cannot
  answer "which failures are settled" without knowing `TERMINAL_CODES`.
- **`TERMINAL_CODES` exists twice** (Python decides nothing with it today
  beyond tests; the UI maps presentation). If the sibling card grows the set,
  hoisting it into the neighborhood/ws payload as a computed `terminal` bool
  is the consolidation path; not done now because one mirrored one-entry
  constant does not yet earn a wire-format field.

## 7. Risks for the Coder

1. **The migration must contain no `:word` sequence anywhere, comments
   included.** The runner feeds it through SQLAlchemy `text()`, which eats
   colon-binds, and a failed migration is LOGGED, not fatal: the symptom would
   be runtime "column error_code does not exist" 500s on every job SELECT,
   far from the cause. Verify the column exists live after deploy.
2. **Style rule order** (§3.7). Putting the settled selector before the
   generic failed rule means red wins and the feature silently does not exist
   visually. There is live precedent for exactly this class of bug.
3. **Dual-source panel state** (§3.6). Miss the `panelDelta` side and first
   paint disagrees with the live delta (or vice versa); test both paths.
4. **The fake test db validates nothing** (§3.8). Do not write a test that
   "proves" bad codes are rejected by the schema; the rejection lives in
   `mark_report`.
5. **`running` reports must not write a code.** Only `failed` carries one;
   letting a running progress note set `no_match` would paint a live import
   settled if a race lands it last.
6. **Shared tree, stale checkout.** Work from a worktree cut from
   `origin/master` (v0.55.0); the shared checkout is at v0.50.0 and must not
   be reset. `ui/dist` is committed in this repo: rebuild it from the clean
   worktree, not from a tree carrying other cards' uncommitted ui/src work.
7. **QA is live-UI acceptance.** On a deployed build, click Import on a
   reference with no catalogue match (the Pan 2026 ref on the
   `arxiv_2602.12735` document reproduces it): the node must settle to the
   quiet terminal visual, the panel must name the settled outcome, and the
   primary Retry must be gone (only the "Retry anyway" link). Old failed rows
   (NULL code) must still render red with primary Retry: that is the
   regression check that unclassified stays retryable.

## 8. Cost check against the PO's sizing

One nullable column, one optional tool-schema field, a third value threaded
through four existing read sites, a prompt paragraph, and a UI branch plus one
style rule. No new endpoint, no new table, no state-machine change, no trust
machinery (the anti-faking property is preserved by construction, §1.2). This
is within the "cheap" the PO priced; no re-scope conversation needed.
