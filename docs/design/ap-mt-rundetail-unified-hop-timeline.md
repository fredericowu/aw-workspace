# AP-MT RunDetail: one view — hop hierarchy crossed with the event chronology

Card: `4025bf3b-9510-818a-9b8a-f119307e373b` · Architect run `5450d9633cdd427db27137038ecec589` · 2026-10-23
Repo: `repos/agents-platform-multitenant` (frontend only). Item 2 of a 3-item PO scope; items 1+3
(WorkflowEdit.tsx, Page.tsx) are a different coder's in run `303c60a9fbb5497dbd78b208f8362628` — **do not
touch `WorkflowEdit.tsx` or `Page.tsx`**, and since both runs edit the same shared checkout, the Coder
should work in a git worktree (see memory `concurrent-coder-worktree-isolation`).

## 0. The decisive fact the PO scope didn't have

The PO chose reading (A) — "the chronological spine is THIS run's own events, hop events revealed
lazily" — partly to avoid N extra `getRunEvents` calls. That trade-off is moot, because **the parent
workflow run's event stream already contains every direct child's full event stream**:

- `backend/app/core/executor.py:1978-1984` — `ev_ids = {run_id, event_run_id}`; the local `emit()`
  publishes **every** event to both ids. All of the child's `node_start`, `tool_call`, `tool_result`,
  `thinking`, `llm_token`, `error`, `finalizing`, `node_end` go through this `emit`
  (`executor.py:2303,2330,2435,2462,2467,2680`).
- `backend/app/core/executor.py:3064-3072` — workflow node dispatch (`child_agent`) calls
  `run_agent(..., event_run_id=run_id, parent_run_id=run_id, node_id=node_id)` where `run_id` is the
  **parent workflow run**. The docstring at `executor.py:1740` states it: "publish events on this id
  too (so workflows roll up nicely)".
- Mirroring is **one level deep only**: a sub-workflow node goes through `run_workflow(...)`
  (`executor.py:3046-3051`) with no `event_run_id`, so a sub-workflow's internals do NOT appear in the
  root's stream — only the root orchestrator's `node_start`/`node_end` for that node do.

Consequences:
1. The flat "Event timeline" on a workflow run **already shows the full one-level interleave** — that
   is exactly why it reads badly today (every hop's tool calls mixed flat, attribution only via the
   small `node_id` column).
2. "Lazy per-hop loading" is unnecessary for direct hops: expanding a hop is a **client-side reveal**
   of rows already fetched, not a fetch. Page load cost is byte-identical to today (same single
   `GET /api/runs/{id}/events`, `frontend/src/lib/api.ts:662`).
3. The only genuinely lazy candidate — fetching a sub-workflow hop's internals on expand — is **cut
   for v1** (explicitly, per the PO's permission). The hop's child-run link navigates to that run's
   own page, which renders its own unified view. Depth-first navigation instead of inline recursion.
4. To the PO, re option (B): no re-scope needed. The chosen design delivers (A)'s reading with (B)'s
   information already paid for, in one fetch. Zero backend change, confirmed.

Every event row therefore carries the hop's `node_id` (the executor stamps `node=node_id` on all
child events), and the join key to the tree is better than `node_id` alone: the mirrored child
`node_start` payload carries the child's own `run_id` (`executor.py:1986-1992`), and the
orchestrator's `node_end` carries `child_run_id` (`backend/app/core/orchestrators/__init__.py:116`).

## 1. The approach (disagree with this, not with the code)

On a **workflow root run** (`run.kind === "workflow" && tree?.root?.id === run.id`), replace the flat
`TimelineList` in the main page's "Event timeline" card (`RunDetail.tsx:548-553`) with a new sibling
component **`HopTimeline`** that regroups the already-fetched `display` array into:

- **root-level rows** — events not belonging to any hop segment, rendered flat, chronological,
  exactly as today (delegated to the untouched `TimelineList`);
- **hop sections** — one per `node_start`→`node_end` segment, whose header row sits at the opening
  `node_start`'s real chronological position, enriched with the hop's child run (status badge, model,
  tokens, duration, `→ child run` link) and expandable in place to reveal that hop's inner event rows
  (again rendered by the untouched `TimelineList`).

Every other run — single agent runs, child runs, agent runs that dispatched children via
`run_agent_async` (those children have **no** `node_start` anchor in the parent stream, so there is
nothing to cross) — keeps today's flat timeline unchanged. The `?view=telegram` branch
(`RunDetail.tsx:202-237`) is not touched at all.

The "Run group" card (`RunDetail.tsx:294-309`) keeps its totals strip everywhere. On workflow roots
only, `TreeRows` moves inside a collapsed `<details>` ("full run tree") as the escape hatch for tree
children that are not workflow hops (e.g. a hop's agent dispatching `run_agent_async` grandchildren —
they're in `/tree` but have no event anchor). On every other run the card is unchanged.

## 2. Where it lands

One file: `frontend/src/routes/RunDetail.tsx`. No `api.ts` change (no new fetch, no new params —
`getRunEvents` at `api.ts:662` stays as is). No backend change.

| Seam | Today | Change |
|---|---|---|
| `RunDetail.tsx:548-553` main timeline card | flat `<TimelineList display={display} …/>` | `run.kind==="workflow" && tree?.root?.id===run.id ? <HopTimeline …/> : <TimelineList …/>` (same card, same `data-testid="run-events"`, same `Event timeline ({events.length})` heading) |
| `RunDetail.tsx:294-309` Run group card | totals strip + always-visible `TreeRows` | totals strip unchanged; `TreeRows` wrapped in `<details>` **only** on workflow roots; unchanged otherwise |
| `RunDetail.tsx:560-598` `TimelineList` | shared with Telegram | **zero diff** — the hard constraint. `HopTimeline` calls it per segment; it does not change its signature, body, or styling |
| `RunDetail.tsx:202-237` telegram branch | flat `TimelineList` at line 221 | **zero diff** — Telegram stays flat even for workflow runs |
| `RunDetail.tsx:171-177` `assembled` + `display` | — | **zero diff** (settled definition) |
| new: `HopTimeline` + a pure `groupHops(display, tree)` helper + `expandedHops` state | — | added in the same file, next to `TimelineList`/`TreeRows` (file's idiom: all components colocated) |

### 2a. The grouping algorithm (`groupHops` — pure, `useMemo` over `[display, tree]`)

Input is `display` (the `llm_token`-filtered list, `RunDetail.tsx:177`) — **not** `events`.

1. Scan chronologically (the array is already server-ordered by `ts`, `backend/app/api/runs.py:140`).
   A `node_start` whose `node_id` has **no open segment** opens one (this is the orchestrator's
   emission, `orchestrators/__init__.py:100-102` — payload `{label, agent, hop, budget}`). A
   `node_end` with the same `node_id` closes it. Everything in between carrying that `node_id` —
   including the mirrored executor `node_start` with `payload.run_id` — belongs to the segment.
   A `node_start` for a `node_id` that already has an open segment is an **inner row** (the mirrored
   one), never a second hop. Same-`node_id` repeats across rounds (`run_group_chat`,
   `orchestrators/__init__.py:234`) become separate sequential segments naturally.
2. Rows with a `node_id` matching no open segment, and rows with no `node_id`, are root-level rows.
3. Child-run resolution per segment, in priority order:
   a. inner mirrored `node_start.payload.run_id` (`executor.py:1991`) — exact, available while running;
   b. closing `node_end.payload.child_run_id` (`orchestrators/__init__.py:116`) — exact, after finish;
   c. tree child with `node_id === nid`, taking the k-th by `started_at` for the k-th segment of that
      `nid` (`/tree` children are ordered by `started_at`, `backend/app/api/runs.py:757`).
   The resolved tree node supplies `model_slug`, `status`, `tokens_in/out` for the header. Transform
   nodes (`orchestrators/__init__.py:309-316`) resolve to nothing — header renders without a child
   link, that's correct, don't special-case an error.
4. **Ordering uses `RunEvent.ts` only** — one clock, one stream. `started_at` from the tree is never
   an ordering input (this dissolves the PO's two-clocks caveat), only an ordinal tiebreaker within
   one `node_id` in 3c.
5. Invariant the Coder should assert mentally and QA should count: root rows + hop header rows +
   inner rows == `display.length`. No event dropped, none duplicated.

### 2b. Rendering + state

- Hop header: timestamp (opening `node_start.ts`), `node_id`/label, agent slug, status badge (tree
  child status; "running" if the segment is unclosed), model `kbd`, tokens, duration
  (`node_end.ts − node_start.ts` when closed), inline `→ run xxxxxxxx` `<Link to={/runs/${childId}}>`,
  and an expand chevron with inner-row count. The header **is** the hop's `node_start`, rendered once,
  at its chronological position — the acceptance criterion. The mirrored second `node_start` appears
  only inside the expanded segment (it is a distinct event, not a dup — QA note below).
- Expansion state: a **new** `expandedHops: Set<string>` keyed by the opening event's `id` (stable —
  `RunEvent` rows are immutable, so WS refetches don't collapse open hops). Do **not** reuse the
  existing `expanded` set: its keys are event ids too, and the header row's payload-expand would
  collide with hop-expand. Header row click toggles the hop; the header's own payload stays reachable
  because the opening `node_start` is also rendered as the first inner row via `TimelineList` when
  expanded — simplest non-colliding contract. Inner rows keep their normal `TimelineList`
  payload-expand behaviour against the shared `expanded` set (event ids are globally unique).
- Live updates: nothing new — the existing WS handler (`RunDetail.tsx:67-75`) refetches
  events+tree and the `useMemo` regroups. An unclosed segment at stream end = running hop.
- Default state: hops collapsed. Frederico's top-to-bottom read is then: root events + one header per
  hop in time order — the PO's "WHAT CHANGES" verbatim.

## 3. Rejected alternatives (and why)

1. **(B) interleave descendants via N `getRunEvents` calls** — rejected by the PO, and now also
   pointless: the one-level interleave is already in the parent stream (§0). Deeper-than-one-level
   inline interleave remains out — it would re-introduce N fetches for sub-workflows only.
2. **A backend "unified timeline" endpoint** — out of scope by PO fiat, and demonstrably unnecessary.
3. **Extending `TimelineList` with grouping props** — rejected: it's the Telegram-shared component
   and the single biggest regression risk on the card. A sibling component with zero diff on
   `TimelineList` makes the Telegram guarantee checkable with `git diff` instead of by inspection.
4. **Merging tree children into the spine by `started_at`** — rejected: mixes the Run-row clock with
   the event clock (the PO's caveat is real: the child's `started_at` is written by a different code
   path than the parent's `node_start.ts`). Events-only ordering avoids ever comparing them.
5. **Using the React Flow execution graph as the hierarchy surface** (click node → its events) —
   rejected: the graph block is explicitly out of scope, and a graph has no chronology axis.
6. **Lazy-fetching sub-workflow internals on hop expand** — cut for v1 (explicit, per the PO's
   instruction to say so rather than quietly include/exclude). The hop link navigates instead. If
   wanted later it's additive: fetch `getRunEvents(childId)` on expand for `kind==="workflow"` hops
   and render through the same `TimelineList` — the seam is already shaped for it.
7. **Removing the Run group card on workflow roots entirely** — rejected: async-dispatched
   grandchildren exist in `/tree` but have no event anchor; deleting `TreeRows` outright would make
   them unreachable from the page. Collapsed `<details>` keeps one primary view without losing them.

## 4. What this makes harder later

- **Bakes in a dependency on the executor's one-level event mirroring** (`event_run_id`). If that
  mirroring is ever turned off to curb `run_events` growth (plausible: the table is large and
  `payload` is `json`, not `jsonb` — memory `apmt-run-events-payload-is-json-not-jsonb`), hop inner
  rows vanish and only headers survive (orchestrator events are emitted on the parent id directly).
  The view degrades, doesn't break — but the lazy-fetch variant (§3.6) becomes mandatory then.
- **One-level visibility**: sub-workflow internals are a navigation hop away, not inline. A future
  "expand everything recursively" ask reopens the N-fetch question the PO deferred.
- The full tree at a glance is now one `<details>` click away on workflow roots.
- Hop semantics are inferred client-side from event shape (`node_start`/`node_end` pairing by
  `node_id`). A future orchestrator kind that emits differently (e.g. no `node_end` on a path, or
  nested same-`node_id` starts) lands its events in root-level rows — visible, not lost, but ugly
  until the grouper learns the new shape.

## 5. Risks for the Coder (the non-obvious breakage)

- **The Telegram constraint is `git diff`-checkable**: `TimelineList` (560-598), the telegram branch
  (202-237), and lines 171-177 must show zero diff. If your implementation needs to touch any of
  them, stop and say so — that's the design being wrong, not something to absorb.
- **Group on `display`, not `events`** — grouping `events` would pull `llm_token` rows into segments.
- **Two `node_start`s per hop** is the normal shape (orchestrator's + mirrored executor's). Open a
  segment only on a `node_start` with no open segment for that `node_id`, or every hop doubles.
- **Don't key hop expansion in the existing `expanded` set** — key collision with the header row's
  payload toggle (both are event ids).
- **Running runs**: WS refetch replaces the arrays; grouping must be a pure `useMemo`, expansion
  keyed by stable event ids, or open hops collapse on every poll.
- **`node_id` fallback**: executor stamps `node_id or agent_slug` — for workflow hops it's always the
  real node id (dispatch passes it, `executor.py:3070`), but events emitted before/outside hops may
  carry the agent slug or null; they must land as root rows, not crash the grouper.
- **Concurrent run warning**: coder-sonnet run `303c60a9…` is editing `WorkflowEdit.tsx`/`Page.tsx`
  in the same shared checkout. Use a worktree; do not commit their in-flight diff.
- **Verification floor** (no unit-test harness exists in `frontend/` — only `@playwright/test` as an
  unused dep, no config, no test files): `cd frontend && npm run build` (tsc -b + vite) must pass
  (skill `aw-autoskill-ap-mt-test-verify`), plus a visual check of the four run shapes in §6 against
  a dev server or the deployed instance.

## 6. What QA should hunt

1. **Telegram byte-equivalence** (the card's #1 risk): `?view=telegram` on a multi-hop workflow run
   AND a plain agent run — flat list, same rows, payload expand works, child links keep
   `?view=telegram`. Plus `git diff` over `TimelineList`/telegram branch = empty.
2. **Single agent run** (no group, PO fact 4): page renders identical to production today.
3. **Agent run with async-dispatched children** (`kind==="agent"`, `tree.totals.runs > 1`): unified
   view must NOT activate; Run group card unchanged with visible `TreeRows`.
4. **Child-run view** (navigate into a hop): Run group card shows the full tree as today.
5. **Completed multi-hop workflow** (e.g. a `search-knowledge` run — dag + transform nodes): every
   hop header once, chronological, child link navigates; transform hops render with no link, no
   crash; totals strip values match `/tree`.
6. **The mirrored `node_start` inside an expanded hop is NOT a bug** — distinct event, by design
   (§2b). Flag it only if it appears collapsed at top level (that would be a grouper bug).
7. **Running workflow, live**: rows stream in, an open hop stays open across WS refetches, running
   hop shows running status, no scroll jump.
8. **group_chat workflow** (repeated `node_id` across rounds): one segment per round, each matched to
   the right child run (ordinal, 2a.3c).
9. **Count invariant**: headers + inner + root rows == `display.length`; heading still says
   `Event timeline ({events.length})` with the unfiltered count, as today.
10. **Untouched blocks**: retro scores, artefacts, execution graph, system prompt, input/output,
    retry/cancel, parent/retried-from breadcrumbs — zero behavioural change.
11. `npm run build` clean (tsc is the only type gate — there is no test suite to lean on).
