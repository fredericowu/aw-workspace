# codex-runner: OpenAI 429 exhaustion under concurrent spawns — scoped retry + shared cooldown, no static concurrency cap

**Card:** `4055bf3b-9510-8179-a3d3-de7933e15ed1` (follow-up design ask after the
v0.189.0–v0.196.0 retry-gate fixes, which are DONE and not reopened here).

**The problem being designed for:** codex's own internal HTTP client hits a
real OpenAI `429 Too Many Requests`, exhausts its own internal retry budget,
and surfaces `{"type":"error","message":"exceeded retry limit, last status:
429 Too Many Requests"}` as its only output. Confirmed live on 3 concurrent
`knowledgeable-search-planner` runs, 08:36:47–08:37:07 UTC 2026-10-26. The
existing contention retry correctly does NOT fire (the error line is real,
informative output, `live=True`), and the coder correctly declined to widen
it: unconditionally respawning containers into an already-exhausted
account-level quota during a concurrency storm amplifies the storm.

## 1. The decision

Handle 429 exhaustion **reactively, inside aw-app-agents-platform-runners,
on both codex paths (cold + warm), coordinated through one shared Redis
key**. Three mechanisms, shipped together:

1. **A second retry class, "rate-limited", distinct from contention.**
   Both stream loops learn to recognize codex's terminal 429 event while
   still in the buffered (`not live`) phase, withhold it exactly like the
   rollout signature, and retry the whole attempt on a **long, jittered
   schedule**: new constants, suggested `_CODEX_RATE_LIMIT_MAX_ATTEMPTS = 3`
   (1 initial + 2 retries) and `_CODEX_RATE_LIMIT_RETRY_BACKOFF_S = (20, 60)`
   with ±25% jitter. Never the contention schedule (0.3s/0.6s): by the time
   this event surfaces, codex has already burned its own ~4 internal retries
   against the same quota, so sub-second reattempts are guaranteed wasted
   spawns. Rate-limit windows are typically per-minute; the first useful
   moment to retry is tens of seconds out.

2. **A shared Redis cooldown key — the anti-storm piece.** Whenever any
   process (any of the 10 uvicorn workers' cold paths, or any warm relay in
   any agent container) sees the 429-exhaustion signature, it arms
   `apr:codex:429-cooldown:<codex-home-id>` via `SET ex=<ttl>`; TTL starts at
   30s and doubles on each re-arm while a previous cooldown is still (or
   recently) active, capped at 120s. **Every** codex attempt — cold attempt 1,
   cold respawns, and every warm-relay attempt — first waits for this key to
   clear before spawning (poll 1s, bounded at 150s total, then proceed
   anyway: fail-open, so a stuck key can never freeze the fleet; the TTL
   guarantees expiry regardless). After the key clears, each waiter sleeps an
   extra random 0–5s before spawning, so a cleared cooldown doesn't release a
   thundering herd that instantly re-exhausts the quota.

3. **Visibility in the run timeline, not a config knob.** When a run is
   delayed by a cooldown wait or a rate-limit retry, publish a notice line
   into the run's own Redis stream ("codex rate-limited by OpenAI — waiting
   Ns before retry (attempt i/N)") plus a `log.warning` naming the cooldown
   state. The stream line is load-bearing twice over: it feeds the 600s
   no-activity watchdog during long waits, and it answers "why is
   search-knowledge slow right now" in the exact UI Frederico is already
   looking at when this bites. If every attempt exhausts, the buffered real
   429 error line is flushed (same `is_last_attempt` contract as contention),
   so the run still fails with the true cause, never a vaguer one.

**Deliberately NOT in this design:** no static workspace-wide concurrency
semaphore (runner-up, §3), no AP-MT / DAG-engine change, no `aw-app.json`
config field.

Why reactive-cooldown over proactive-cap, in one paragraph someone can
disagree with: the quota being protected is a ChatGPT-plan codex account
limit — opaque, dynamic, and unmeasurable from here. A static cap is a guess
against a moving target: too low and it taxes every interactive run forever,
too high and it prevents nothing (the observed burst that 429'd was only 3
concurrent planners — a cap of 4 would have admitted it). The cooldown is
self-tuning: it serializes exactly when the account is actually exhausted,
costs nothing when it isn't, and its re-arm frequency in the logs is the
measurement that tells us whether a cap is needed after all.

## 2. Where it lands

All in `repos/aw-app-agents-platform-runners` (auto-ships on push to master):

- `agents_platform_runners_app/execute.py`
  - New constants next to the existing retry constants, replacing the
    "NOT fixed here" comment block at `execute.py:753-768` with a pointer to
    this doc: `_CODEX_RATE_LIMIT_*` beside `_CODEX_ROLLOUT_MAX_ATTEMPTS`
    (`execute.py:769`).
  - `_stream_cold_attempt` (`execute.py:786`): detect + buffer the 429 event
    alongside the existing signature checks (`execute.py:850-858`); the
    return value grows a retry *reason* ("contention" | "rate_limit" | None)
    instead of the bare `contention_hit` bool.
  - `_run_cold_agent_with_retry` (`execute.py:883`): two retry classes with
    their own schedules; arm + honor the cooldown via the `r` it already
    holds; keep the `is_aborted` check before each respawn
    (`execute.py:908`) and slice every new sleep ~1s so aborts land.
  - Cooldown-gate helper (new, ~30 lines: wait-for-clear + arm): called
    before the attempt-1 cold spawn in `_run_job_blocking`
    (`execute.py:3137`) and before respawns (`execute.py:920`). The warm
    dispatch path in execute.py needs NO gate — the warm turn's codex
    subprocess is spawned by the relay, which runs its own.
- `agent-images/shared/aw-warm-relay-codex.py`
  - Mirrored constants next to `_MAX_RESUME_ATTEMPTS`
    (`aw-warm-relay-codex.py:126`), same "duplicated rather than imported"
    rule as every signature there.
  - `_run_codex_turn` (`aw-warm-relay-codex.py:159`): same detection in the
    buffered phase (`:210-217`), same two retry classes, cooldown read/arm
    via the `r` it already has (`main()` builds it from `AW_REDIS_URL`,
    `:347` — the same Redis the run streams use, which is the correct one).
- Tests: extend `tests/test_execute_codex_rollout_retry.py` and
  `tests/test_warm_relay_codex_retry.py` (both already have `_FakeRedis`;
  they need `SET ex`/`GET`/`TTL` support added), plus the
  `_run_cold_agent_with_retry` signature touch in `tests/test_abort_job.py`.
  Suite baseline: 934 passed, 9 pre-existing skips.
- Untouched: AP-MT entirely (`orchestrators/__init__.py`, `cli.py`), the DAG
  definitions in `aw-app.json`, `warm_pool.py`, the app config schema.

## 3. Rejected, and why

- **Reusing the contention retry for 429 (unconditional short-backoff
  respawn).** The coder's own reasoning stands: it pours fresh spawns into an
  exhausted quota mid-storm. Also its schedule is ~100x too short to ever
  help (codex already spent its own retries before we see the line).
- **Workspace-wide codex concurrency semaphore at the spawn layer** — the
  runner-up. A Redis lease-semaphore (ZSET of slot leases with heartbeat
  refresh from the stream loops) at the same two seams the cooldown gate
  uses would genuinely bound storm amplitude, and the runners app is the
  right choke point for it (every consumer, cold and warm, passes through).
  Rejected *now* because: the cap value is a guess against an opaque quota;
  the observed failing burst (3 concurrent) was below any cap we'd plausibly
  set; it taxes every interactive run at all times for a condition that is
  bursty; and it roughly doubles the implementation (lease protocol + 
  heartbeats duplicated into the relay). **What would revive it:** cooldown
  re-arm warnings recurring under normal daily usage (not synthetic load)
  after this ships — that log line is the designed-in evidence collector.
- **Capping fan-out in the DAG engine (AP-MT `run_dag`,
  `orchestrators/__init__.py:371`).** Wrong layer: the DAG's own fan-out is
  only 4–6, and the problem is the *sum* across independent consumers
  (search-knowledge + literature-review + Frederico's standalone UI retries),
  which no per-workflow cap can see. Cross-repo change for partial coverage.
- **Raising codex's own retry budget (`request_max_retries` etc. in the
  shared `$CODEX_HOME/config.toml`).** A hand-managed global edit of the
  allowlist-gated kind this workspace already knows to distrust (invisible,
  applies to every consumer and every error class, no cross-process
  coordination), and it holds the container + job thread sleeping inside
  codex where we can't see or abort it.
- **A Frederico-facing config knob.** There is no value he could rationally
  tune it to — the quota is invisible. Constants-in-code (the existing
  `_CODEX_ROLLOUT_*` pattern) plus timeline visibility instead. If the
  semaphore ever ships, *that* cap is the knob worth exposing.
- **Retrying 429s that land mid-turn (after real output).** Unsafe (tool
  side effects may have executed) and automatically excluded: the gate only
  ever retries while `not live`, i.e. when the turn produced nothing real.
  A mid-turn 429 exhaustion stays a terminal, truthfully-reported error.

## 4. What this makes harder later

- **A sustained OpenAI outage now fails slower.** Worst case ~3–4 min of
  cooldown + retries per run instead of ~10s, before surfacing the same 429
  line. Deliberate trade; the KB's own lesson (client-side retry never
  covers a sustained outage) is respected by keeping the final error honest
  and loud rather than retrying forever.
- **Consumers are now coupled through the cooldown.** One workflow's storm
  pauses an unrelated interactive codex turn for 30–120s. That is the point
  (the quota is genuinely shared), but it is why the stream notice line is
  mandatory, not cosmetic — without it this reads as a new mystery hang.
- **Single-shared-account assumption gets baked in.** Mitigated: the key is
  suffixed with the codex-home identity, so a future second account/home
  gets its own cooldown lane without a migration.
- **The duplicated-logic surface grows.** Until now the two files shared
  only signature tuples; this adds ~30 lines of gate/backoff *behavior* kept
  in sync by convention and tests. A third codex path would make extracting
  a shared module (shipped into the image) worth revisiting.

## 5. Risks for the Coders

- **Signature matching must be structural, not substring.** While `not
  live`, every line passes the signature checks before anything can flip
  `live` — a model turn whose *first* content line quotes the 429 error text
  would be misclassified by a substring match. Require the line to parse as
  JSON with top-level `"type": "error"` and a message containing both
  "exceeded retry limit" and "429". (The rollout signature's nonzero-exit
  belt doesn't transfer here: round 2 of this card proved these processes
  can die cleanly, so do NOT gate this class on returncode.)
- **The notice line's shape needs a downstream check.** AP-MT's codex parser
  (`agents-platform-multitenant/backend/app/core/models/cli.py`) must not
  read it as model output or as a terminal error event. Verify how it treats
  unknown types before choosing the JSON shape; the cold path's existing
  runner-injected `{"type": "result", "subtype": "spawn_error"}` line is the
  precedent to imitate.
- **Clocks: Redis TTL only.** The relay runs in a different container than
  the workers; never compare wall-clock timestamps across them. `SET ex=` /
  `TTL` carries all the state this needs.
- **Aborts during the new sleeps.** Cold path: slice sleeps and re-check
  `is_aborted` (precedent at `execute.py:908`). Warm path: mirror whatever
  abort mechanism the relay honors today for in-flight turns.
- **Warm-relay deploy verification.** The relay ships into agent images and
  warm containers live 6h — a green release does not put the new relay into
  an existing warm container. Recycle/reap (or `list_warm_containers` +
  targeted reap) before verifying, as both earlier rounds on this card did.
- **You cannot force a live 429 on demand.** Unit-test the gate with faked
  event lines (both suites' `_FakeRedis`/fake-proc patterns extend
  naturally); live verification is "no regression under real concurrent
  load, cooldown key observed arming/clearing when faked in". Say exactly
  that in the delivery report — do not claim a live 429 repro you didn't get.
- **Venv gotcha:** the repo's test venv is affected by the broken
  python3.12-symlink issue; use the known-good interpreter per the
  `aw-autoskill-agents-platform-runners-test-venv-broken` skill.

## 6. Flagged separately (NOT this card's deliverable): DAG node "ok" on an errored child run

Raised 4 times on the card, never verified — now **source-confirmed at the
mechanism level**: `run_dag` sets `status[nid] = "ok"` whenever the node
coroutine returns without raising
(`agents-platform-multitenant/backend/app/core/orchestrators/__init__.py:356`),
and a child run that terminates `status=error` with 0 tokens does not raise
through `_run_node`/`run_agent` — so the rolled-up summary reports "ok" and
dependents are not skipped (`:347` only sees "skipped"/"error"). Fix
direction: `_run_node`'s result envelope must carry the child run's terminal
status, and `run_dag` must map a child `error` to node `error` (which the
existing `:347` dependent-skipping then honors for free). This is an AP-MT
change with its own blast radius (every graph shape shares `_run_node`) and
deserves its own card via the Product Owner; it must not ride along on the
429 implementation.
