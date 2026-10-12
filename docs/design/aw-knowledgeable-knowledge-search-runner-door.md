# `knowledge_search` on the runner door — budget design + the default-budget regression fix

Status: **design, planned** — written by the Architect against card
`3f75bf3b-9510-81d7-a7ce-f7b3a4da2d89`
(`architecture:knowledge-search-runner-door-budget`, target `aw-knowledgeable`),
2026-10-12. Every file:line below was read on this pass.

Frederico's constraint, verbatim (Telegram, 2026-10-12), treated as a boundary:

> "I want to use everything with runners for now. I wanna pay only the
> subscription cost, I don't care about the latency it comes with, we try to
> mitigate the latancy with other stuff, but I'd rather use the cli"

So no `provider=openai`/`anthropic` anywhere in this design (the anthropic
door is dead on this ap-mt regardless — no `ANTHROPIC_API_KEY`). Latency is
acceptable; a missing answer is not.

---

## 0. The arithmetic that forces everything else

- A `claude-runner-*` "model" is a CLI dispatched into a container:
  **13.6–19.0s end to end**, measured on live ap-mt Run rows, roughly
  independent of prompt size (memory
  `apmt-runner-models-cannot-meet-a-sub-20s-caller-deadline`).
- The pipeline needs **two sequential** such calls — planner before fan-out,
  synthesis after fusion (`backend/app/core/knowledge_search.py:6-11`).
  2 × (13.6–19.0) = **27–38s**.
- The synchronous ceiling is ~30s: the tunnel edge cuts the browser→production
  hop (memory `tunnel-edge-cuts-requests-at-30s`), which is why
  `BUDGET_MS_CEILING = 25_000` exists (`core/knowledge_search.py:56`). The
  in-workspace MCP connector is local-network (`DEFAULT_BASE_URL =
  "http://aw-knowledgeable:8090"`, installed connector
  `apps/knowledgeable/knowledgeable_app/mcp/client.py:27`) with its own
  computed 30–45s timeout (`client.py:437`), so it has a little more headroom
  but the same order of ceiling.

**Conclusion: two runner calls can NEVER both run inside one synchronous
request.** Not at 20s, not at 25s, not at the raw 30s edge. Any design that
keeps both on the sync path is arithmetic denial. The sync path gets exactly
one runner call — synthesis, because an answer is the deliverable and the
deterministic planner fallback demonstrably produces correct answers (measured
live 2026-10-12, repeatedly) while there is no deterministic fallback for
synthesis.

The live regression, measured today with the planner back on
`claude-runner-haiku`:

| budget_ms | planner (timeout 10.0s) | synthesis | answer |
|---|---|---|---|
| 25000 (ceiling) | ReadTimeout → fallback, 10s burned | OK | 809 chars |
| 20000 (default) | ReadTimeout → fallback, 10s burned | ReadTimeout | **null** |

The planner can never succeed at `knowledge_search_planner_timeout = 10.0`
(`backend/app/config.py:305`) on a 13.6–19s door — today it spends 10s of the
budget accomplishing nothing, then starves synthesis.

## 1. Deliverable 1 — the immediate fix (one coder card, backend-only)

**The decision: teach the pipeline the runner door's real floors and admit the
planner only when the remaining budget fits BOTH the planner and the synthesis
that must follow — plus raise the default budget to the ceiling.** Four
changes, all in `backend/app/` (no connector change, no core restart, deploys
through aw-knowledgeable's own CI):

1. **Planner admission rule** in `core/knowledge_search.py`. Today
   `_deadline_bound_timeout()` (`:75-89`) skips a call only when the remaining
   budget is under `MIN_USEFUL_CALL_SECONDS = 1.0` (`:63`) — a floor that is
   true for an API door and off by ~20x for a runner. Replace the planner-side
   check (`:279-288`) with: run the planner only if

   ```
   min(cfg.timeout, remaining) >= planner_floor
   and (not synthesize or remaining - planner_floor >= synth_reserve)
   ```

   with two new config values:
   `knowledge_search_planner_min_useful_seconds = 20.0` (the 19.0s measured
   worst case + margin — same constant discipline as
   `tests/test_knowledge_search_config.py:20`) and
   `knowledge_search_synth_reserve_seconds = 20.0`. Otherwise fall back
   exactly as today, with the reason in `clamped` in plain English, naming
   both numbers (pattern already at `:280-288` / `:513-519`), e.g.
   `planner skipped: 24.0s budget cannot fit a ~20s runner-door planner call
   AND leave the ~20s synthesis reserve — deterministic fan-out used`.
2. **`BUDGET_MS_DEFAULT` 20_000 → 25_000** (`core/knowledge_search.py:55`,
   echoed by the request model default `api/knowledge_search.py:36`). With
   the planner skipped, synthesis gets `min(22.0, ~23s)` of real timeout —
   clears the 19.0s worst case with margin. 25_000 stays under the ~30s edge
   with transport headroom.
3. **`knowledge_search_planner_timeout` 10.0 → 25.0** (`config.py:305`) so
   that when the planner IS admitted — retrieval-only sync calls
   (`synthesize=false` drops the reserve term: 25s budget ≥ 20s floor, so the
   LLM planner runs and fits), and the async path in §2 — it can actually
   finish. The comment at `config.py:301-304` ("~2-4s typical for a nano
   model") describes a door this agent no longer lives behind; rewrite it.
4. **`MIN_USEFUL_CALL_SECONDS = 1.0` stays as-is for synthesis.** Synthesis
   is the last call — a marginal attempt starves nothing downstream, and a
   lucky 13.6s run inside a 14s window is strictly better than a guaranteed
   `answer: null`.

**Why not just raise the default to 25s (the card's named alternative):**
25 − 10 (planner burn) − ~2 (fan-out/fusion) ≈ **13s synthesis window against
a measured 13.6–19.0s runner range — below the measured minimum.** Today's
809-char success at 25s was the runner landing at the fast end of its range;
raise-only turns the default path from "broken" into "coin flip", and still
burns 10s per request on a call that cannot succeed. Rejected as the sole fix;
subsumed as change 2.

**Declared consequence, stated honestly:** with synthesis requested, the LLM
planner will be skipped on *every* synchronous call while both agents sit on
the runner door — that is what the arithmetic in §0 says, and the fix makes
the pipeline say it out loud in `clamped` instead of burning 10s to discover
it. The planner still runs: on retrieval-only sync calls, and on the async
path below. `plan.planner: "fallback"` + the `clamped` note keep it visible
per call (the declared-degradation contract the card requires).

Mutation tests the coder owes: (a) fake-clock pipeline test asserting that at
`budget_ms=25000, synthesize=true` the planner is skipped with the reason in
`clamped` and synthesis still gets ≥ the reserve — goes red if the admission
rule is reverted; (b) config floor tests alongside
`tests/test_knowledge_search_config.py` (whose existing ceiling assertion,
`synth_timeout*1000 < BUDGET_MS_CEILING` at `:33`, survives: 22 < 25); (c) a
`synthesize=false` test asserting the planner IS admitted at the new default.

## 2. Deliverable 2 — how a runner-door planner ever runs: an async job door

**The decision: add an in-app async job endpoint pair; the synchronous
endpoint keeps exactly one runner call forever.**

### 2.1 Shape

- `POST /api/knowledge/search/jobs` — same body as `/api/knowledge/search`
  (`api/knowledge_search.py:28-37`), same `resolve_search_scope` dependency
  (`:77`) evaluated **at submit**, with the resolved `buckets`/`collection`
  frozen into the job (a permission boundary must not be re-resolved at poll
  time — same rule as design §5.1 of
  `aw-knowledgeable-knowledge-search.md`). Returns `202 {job_id}`.
- `GET /api/knowledge/search/jobs/{job_id}` — `{status: pending|running|done|
  failed, phase: plan|retrieve|synthesize, ...partial}` where `chunks`/
  `citations`/`effective`/`clamped` appear as soon as fusion completes and
  `answer`/`usage` on completion. Unknown AND foreign job ids both answer 404
  (don't leak existence); ownership = the submitting token's sub.
- **Async budgets:** `ASYNC_BUDGET_MS_DEFAULT = 60_000`,
  `ASYNC_BUDGET_MS_CEILING = 180_000`. The §1 admission rule needs no async
  special-case: at 60s remaining, `60 ≥ 20 + 20` → the planner is admitted and
  runs on the runner door. `rounds` up to the existing `ROUNDS_CEILING = 2`
  genuinely works for the first time: each refine round re-applies the rule
  against the shrinking remainder — structural stop, same as today
  (`core/knowledge_search.py:500-530`).
- **Job store: in-process, bounded** — max 50 retained, 15-min TTL, at most
  **2 jobs executing** with a queue of 8 (409 beyond). The concurrency cap is
  not optional: the runner warm pool is unbounded and bulk dispatch has
  already killed the host once (memory
  `runner-door-warm-pool-is-unbounded-bulk-work-exhausts-the-host`).
  Production is single-worker and must stay so (memory
  `aw-knowledgeable-production-must-stay-single-worker`) — which is exactly
  what makes an in-process store correct; this feature becomes one more reason
  the worker count is load-bearing. Jobs die with the process; a restart turns
  live job ids into 404s, which callers must treat as "re-ask" — declared, not
  hidden.
- **Core pipeline change: none.** `knowledge_search()` already takes
  `budget_ms` and configs as parameters (`core/knowledge_search.py:455-470`);
  the job executor calls the same function with the async budget and a
  progress callback (or splits around the existing seam between fusion and
  synthesis at `:546-557`) — the job layer is `api/`-side orchestration, not a
  second pipeline.

### 2.2 Consumers

- **MCP connector** (`repos/aw-app-knowledgeable`, Tier-1 in-process): two new
  tools, `knowledge_search_deep` (submit, returns the job id immediately) and
  `knowledge_search_result` (poll once, returns the envelope as it stands).
  The existing `knowledge_search` tool stays synchronous and unchanged — its
  adaptive timeout (`client.py:437`) already covers the new 25s default
  (max(30, 25+10) = 35s). Agents poll between their own turns; no long write
  call sits against the gateway's write-call timeout. Ops note: Tier-1 code
  only reloads on a core restart (memories `tier1-inprocess-apps-have-no-
  restart-path`, `aw-app-uc-phd-tier1-code-reload-needs-a-core-restart`) —
  the connector increment must be scheduled for a window with no in-flight
  runs. §1 deliberately needs none of this.
- **Playground UI — reconciliation with
  `aw-knowledgeable-playground-on-knowledge-search.md` (Phase 1 carded, not
  dispatched):** Phase 1 ships **unchanged** against the sync endpoint. After
  §1 it gets real answers at the default budget again, and its D3 already
  renders `clamped[]` as a notice strip — the "planner skipped" note surfaces
  with zero extra work. A later UI increment (after Phase 1 is observed live)
  adds a "Deep" toggle on fused mode → submit + poll against the job
  endpoints; each poll is a short request, so the tunnel edge stops being the
  ceiling for exactly the surface that hits it. Phase 1's card does not block
  on, and is not blocked by, anything here.

### 2.3 Complement, not the fix: a plan cache

An in-process LRU (≈256 entries) keyed by (normalized query, effective
`subqueries`, exclude-set signature), filled by LLM plans produced on the
async path, consulted by the sync path before falling back to deterministic.
A repeated or near-verbatim question then gets LLM decomposition
synchronously at zero marginal latency. Plans derive from the question alone
(not corpus state — `_planner_prompt`, `core/knowledge_search.py:247-255`),
so staleness is bounded to prompt-version changes; key the cache on a prompt
version constant. Deliberately **phase 3 and optional**: cold-start dominates
real traffic, so this can never be the primary mechanism — it rode along only
because it is ~30 lines on an existing seam. Declared in `plan` as
`planner: "llm_cached"`.

## 3. Rejected alternatives

1. **Raise `BUDGET_MS_DEFAULT` to 25s and change nothing else.** The card's
   own candidate. Rejected as sole fix: leaves a guaranteed-doomed 10s planner
   call on the hot path and a ~13s synthesis window under a 13.6–19.0s
   measured need — restores the answer only when the runner is fast. §1
   subsumes the budget raise.
2. **SSE/streaming to beat the edge cut.** Rejected on three independent
   grounds: ap-mt's `/v1` streaming is fake — it resolves the entire run,
   then chunks the final text as SSE (`agents-platform-multitenant/backend/
   app/api/openai_compat.py:449-456` — "resolve, then chunk the final text"),
   so time-to-first-byte doesn't improve and the read timeout bites
   identically; the tunnel strips `content-encoding` and cuts long-lived
   requests (memories `tunnel-strips-content-encoding`,
   `tunnel-edge-cuts-requests-at-30s`), making a >30s stream exactly the hang
   §5.3 of the original design refused; and MCP tool calls can't consume a
   stream at all — the connector would have to buffer it back into a blocking
   call. Polling a job id wins on every axis and is the only shape that
   serves UI and MCP with one mechanism.
3. **One combined plan+synthesize runner call (pay 13–19s once).** Rejected:
   the data dependency is planner → retrieval → synthesis — a single call
   can't sit on both sides of retrieval unless the agent calls back into
   retrieval mid-run, which is the agentic loop this pipeline explicitly is
   not (`core/knowledge_search.py:4` "Bounded, NOT agentic";
   v2-retrieval §12's identity-grounds rejection, reaffirmed in
   `aw-knowledgeable-playground-on-knowledge-search.md` §3.4). The weaker
   variant — synthesis also emits refine sub-queries for a second round —
   still needs a second runner call for the re-synthesis, which cannot fit
   the sync budget; on the async path it's redundant with `rounds`, which
   already does exactly this with the planner's `exclude` list
   (`core/knowledge_search.py:507-510`). It would also re-couple the two
   workloads the config deliberately split (`config.py:288-294`).
4. **Delete the LLM planner; deterministic fallback forever.** Tempting —
   the fallback demonstrably answers, and §1 makes it the sync-path reality.
   Rejected as the *end state*: multi-angle decomposition (conceptual
   rephrasings, literal-identifier sub-queries, the PT/EN bilingual pair —
   `core/knowledge_search.py:224-233`) is the headline of the feature card
   and materially matters on a bilingual corpus; the async door gives it a
   place to run on the subscription-cost door Frederico chose. Keeping the
   planner also keeps `rounds` meaningful — the refine loop without a planner
   would re-run the same sub-query.
5. **ap-mt workflow orchestrating repeated sync `knowledge_search` calls**
   (the original §5.3 runner-up). Rejected: keeps the edge cut as the
   per-call ceiling (solves nothing for the planner), and moves the reasoning
   loop out of the app — reversing the original design's central in-app
   decision (`aw-knowledgeable-knowledge-search.md` §1-2) for no latency gain.
6. **Move planner/synthesis to `provider=openai`.** Out of scope by
   Frederico's constraint, recorded so the next reader knows it was not
   overlooked: it is the one-line change that would make everything fit
   synchronously, and it is explicitly not wanted — subscription cost via the
   CLI runner door is the decision (memory
   `extraction-cost-is-the-door-not-the-model` records the same choice on the
   cost axis: ~100x per-token savings is not the point; the flat subscription
   is).

## 4. What this makes harder later

- **The floors are config coupled to each agent's door.** If the planner or
  synthesizer ever moves to a faster door, someone must lower
  `knowledge_search_planner_min_useful_seconds`/`_synth_reserve_seconds` or
  the sync path keeps skipping a planner that would now fit. Visible (the
  skip is declared in `clamped` on every call), but it needs remembering —
  the config comments must point here and at
  `apmt-runner-models-cannot-meet-a-sub-20s-caller-deadline`.
- **Default == ceiling (25s/25s)** removes the headroom between "what you get"
  and "what you may ask for" — a future caller wanting a faster degraded
  search must lower `budget_ms` explicitly. Acceptable: the knob is advisory
  and echoed back.
- **The in-process job store pins single-worker harder.** `--workers N` would
  404 (N−1)/N of polls — the exact per-process-global failure class already
  on file. Scaling ever requires externalizing job state (Redis/Neo4j); that
  migration is the price deferred today. The job-store module must carry the
  same loud comment `aw-knowledgeable-production-must-stay-single-worker`
  demands.
- **Two result contracts.** The partial-envelope (phase, incremental fields)
  becomes public API the moment the connector ships `knowledge_search_result`;
  changing it later breaks agents, not just the UI.
- **Sync answers never get LLM decomposition** (cache hits aside). If goldset
  numbers ever show fallback-planned fused retrieval materially worse, the
  pressure lands on the async path's adoption, not on re-admitting the
  planner synchronously — that door is closed by arithmetic, and this doc is
  the place that says so.

## 5. Risks for the Coders

- **§1 is backend-only — resist touching the connector.** The installed
  connector already adapts its timeout to `budget_ms` (`client.py:437`);
  nothing in §1 needs a Tier-1 reload or a core restart (which kills the
  calling run — memory `restart-core-from-inside-kills-your-own-run`).
- **The repo checkout of the connector is stale.** `repos/aw-app-knowledgeable`
  is at 0.24.0 without the `knowledge_search` tool; installed is 0.25.0
  (`/opt/aw-workspace/apps/knowledgeable/`). Pull/sync before the §2 connector
  increment or you'll edit a tree that's behind production.
- **The admission rule runs per refine round.** `_plan` is called inside the
  rounds loop (`core/knowledge_search.py:507`) with a shrinking remainder —
  implement the rule inside `_plan`'s gate (or a helper it calls), not once
  before the loop.
- **Keep the two floors from colliding in `_deadline_bound_timeout`.** It is
  shared by both callers (`:75-89`); the per-call floor must arrive as a
  parameter defaulting to `MIN_USEFUL_CALL_SECONDS`, not a new global —
  synthesis keeps 1.0 (last call, nothing downstream to starve).
- **Job scope binding:** resolve buckets/collection at submit, store them in
  the job, never re-resolve at poll; poll auth by submitting sub; unknown ==
  foreign == 404.
- **Gateway-injected arguments** (`_gateway_caller_run_id`) must be dropped
  field-by-field in the new connector tools — the existing house pattern
  (`client.py:17-19` docstring).
- **Bound the executor, not just the store.** 2 concurrent jobs max; the
  queue rejects loudly. An unbounded `asyncio.create_task` per submit is the
  warm-pool incident again, slower.
- **Clamp-message honesty:** every skip names both numbers (remaining vs
  floor/reserve) — a bare "planner skipped" would regress the exact
  visibility `bug:knowledge-search-apmt-door-fails-live` was about.
- **Don't trust a green run at one budget.** The regression table in §0 came
  from runs that "worked" at 25s and failed at 20s on the same code. Verify
  §1 live at `budget_ms` **default** (omit the field), 20000, and 25000, and
  check `clamped` text in each.

## 6. Ship order

1. §1 (backend, one coder card) → verify live per the matrix above.
2. §2.1 job endpoints (backend only; curl-testable in production).
3. §2.2 connector tools (needs a planned core-restart window) + UI "Deep"
   toggle (after Playground Phase 1 is live).
4. §2.3 plan cache — optional, only if repeated-question traffic shows up in
   practice.
