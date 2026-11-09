# Execution-index warm-watcher check: a crash-safe in-flight registry

Card 4135bf3b-9510-8132-880e-ce48d0644338. Architect decision, 2026-11-09.
Supersedes the warm-watcher half of
`docs/design/execution-index-doctor-windowed-failure-ratio.md` (that
decision's premise — "gap 8 < 20, green, leave untouched" — is falsified by
measurement below).

## The problem

Two distinct, both-true findings from the Debugger (same card, code read
confirmed at `routes.py:357`, `execution_index.py:813-865`):

**1. The doctor check is an unbounded ratchet by design, not by bug.**
`routes.py:357` gated on

```python
gap = counters["warm_watchers_started"] - counters["warm_watchers_resolved"]
if gap > _WARM_WATCHER_GAP_THRESHOLD:   # routes.py:52, = 20
```

reading the **lifetime** Redis counters via `counters_snapshot()`, never
the 24h `counters_window_snapshot()` the sibling failure-ratio check next
to it uses. This was a deliberate scope decision in the 2026-10-17
architect decision for the sibling check (see the "Superseded" note added
there), not an oversight — but nothing in `_wait()` or anywhere else ever
decrements `warm_watchers_started`/`resolved` or their difference. Every
restart of this (Tier-1, in-process) app kills in-flight daemon watcher
threads outright — a daemon thread gets no finally-block, no graceful
shutdown, nothing runs for it — so `started` ticks for that run but
`resolved` never does, for that run, ever. The lifetime gap is therefore
monotonically non-decreasing by construction. Measured history: gap 4
(2026-10-16) → 8 (2026-10-17, decision time) → 23 → 24 → 56 (2026-11-01) →
81 (2026-11-08) → 84 (2026-11-09). It has never gone down. This is the
"degradation signal needs SET+CLEAR+GATE" shape: a SET with no CLEAR and a
GATE that is a fixed threshold on an ever-growing value is permanently red
as the only possible long-run state.

**2. The leak it measures is real and ongoing, not just stale history.**
The 24h window (`counters_window_snapshot`) showed `warm_watchers_started
=312` vs `warm_watchers_resolved=301` on 2026-11-09 — an 11-run gap accrued
in that 24h window alone. Each of these runs is genuinely never indexed:
the retry mechanism (`_RETRY_QUEUE_KEY`/`sweep_retry_queue`) only queues a
run when `_index_and_maybe_queue_retry` actually executes and chooses to
retry — it is never reached for a watcher thread killed outright by process
exit. The only out-of-band recovery path, `scripts/backfill_execution_index.py`,
is manual and un-scheduled (no cron, no scheduled task, no app.json
wiring) — nobody currently generates the run_id list it would need.

### The wrinkle that rules out the obvious fix

`_wait()` has its own internal 24h deadline (`deadline = time.monotonic() +
24 * 3600`) before it gives up and resolves on its own. A naive
window-the-gap-too fix (mirror the sibling failure-ratio check's hourly
buckets) would need to exclude watchers started within the last 24h from
the "should have resolved by now" count — otherwise a legitimately
still-watching thread counts as leaked. The sibling check's windowing code
is not a drop-in template here for that reason.

## The approach

One mechanism answers both questions: a **crash-safe in-flight registry in
Redis** that (a) gives the doctor a windowed, self-clearing signal and (b)
IS the list of orphaned run_ids nobody generates today, drained into the
already-existing, already-proven hourly retry sweep. No shutdown hook, no
new scheduled job.

**1. In-flight registry** (`agents_platform_runners_app/execution_index.py`)

- New Redis hash `execution_index:warm_watchers_inflight`, run_id → start
  epoch.
- `start_after_stream_done`: `HSET` right where `warm_watchers_started`
  increments, best-effort fail-open exactly like `_incr` — a Redis failure
  must never affect watching/indexing. `HDEL` if `thread.start()` raises.
- `_wait()`: `HDEL` in a `finally` so all three resolution branches (done /
  24h deadline / exception) clear the entry.
- `_WATCHER_DEADLINE_S` (module constant, = the `24 * 3600` literal
  `_wait()` already used) and `ORPHAN_ADOPT_AFTER_S = _WATCHER_DEADLINE_S +
  3600` derive from the same number — adoption correctness depends on it.
- Key property: `_wait()` always self-resolves within `_WATCHER_DEADLINE_S`,
  so an entry older than that is a **provably dead thread**. Age-based
  adoption is therefore race-free across `WORKERS=N` (watcher threads live
  in any worker; the sweep runs leader-only — no liveness probing needed).

**2. Recovery: adoption pass in the existing sweep**
(`sweep_retry_queue`; registered hourly + on boot)

- Before draining the queue: `HGETALL` inflight; every entry older than
  `ORPHAN_ADOPT_AFTER_S` → `_queue_retry(run_id, "watcher_orphaned")`
  **then** `HDEL` from inflight (that order, not the reverse — a crash
  between the two calls must leave the run recoverable from at least one
  of the two structures, caught in PR review and fixed before merge), then
  `_incr("warm_watchers_orphaned")` (new name in `COUNTER_NAMES`; `_incr`
  dual-writes the window bucket for free).
- Orphans then flow through the existing retry machinery: idempotent
  dual-write upsert, `_RETRY_MAX_ATTEMPTS=24`, `retry_exhausted`
  visibility. Never touches `warm_watchers_resolved` — lifetime counters
  are never reset or reinterpreted (this doc's own §"what this makes
  harder later", and the 2026-10-17 decision's design §6).
- Recovery latency is up to ~26h. Acceptable: this is a searchable history
  index, not a live signal.

**3. Doctor semantics** (`routes.py`)

- Deleted the lifetime-gap gate and `_WARM_WATCHER_GAP_THRESHOLD`. Lifetime
  counters stay in `detail.counters` — reported, never gating.
- Three new problems, each windowed and self-clearing:
  - (a) windowed `warm_watchers_orphaned` > **50** → restart churn. Orphans
    are recovered now, so this signals churn, not loss; threshold is
    ~4-5x the measured 11/day.
  - (b) any in-flight entry older than `ORPHAN_ADOPT_AFTER_S + 2*3600`
    (~27h) → the adoption machinery itself is dead (sweep not ticking, or
    `watchdog:tasks` capability not granted — `plugin.py` already degrades
    silently in that case; this is its backstop).
  - (c) windowed `retry_exhausted` > 0 → actual permanent loss (the only
    remaining path to losing a run forever; previously invisible to the
    doctor because the failure-ratio prefix rule only sums `skipped:*`).
- An in-flight snapshot (count + oldest age) is included in `detail`.

**4. Historical backlog (~84 lifetime orphans).** One manual post-deploy
run of `scripts/backfill_execution_index.py` — the script does **not**
need anyone to supply run_ids; `_fetch_run_ids` self-enumerates all
terminal runs from AP-MT, and `index_run` upserts idempotently and
re-applies the gate. Needs `--limit` large enough to span back to
mid-October (e.g. 5000) and a `--dry-run` first. One-off, stays
unscheduled — the in-flight registry makes a *scheduled* backfill
permanently unnecessary going forward.

## Rejected alternatives

- **Naive windowed gap (copy the sibling: `started_w − resolved_w` over
  24h).** Mis-pairs starts and resolves across window edges (can go
  negative, or count a resolve whose start aged out) and counts every
  healthy in-flight watcher as leaked, since `_wait()` legitimately runs up
  to 24h. The exact wrinkle above, confirmed.
- **Lagged-window comparison (starts >24h old vs later resolves).** The
  hourly buckets have a 26h TTL — the data expires right as it becomes
  comparable — and resolves can't be attributed to their start cohort
  without per-run state. Once you hold per-run state, the in-flight hash is
  strictly better.
- **Reset baseline per deploy epoch.** Erases the evidence at exactly the
  moment the leak happens (the restart), and still gates on an unpaired
  lifetime difference.
- **Graceful shutdown/drain hook on the Tier-1 app.** Only covers graceful
  exits — crashes, OOM, SIGKILL and core restarts bypass it — and it can't
  block shutdown for `index_run`'s up-to-60s finalize poll per in-flight
  run. Crash-unsafe by construction; the persistent registry covers every
  death mode. Not needed even as a secondary layer.
- **Scheduled auto-backfill (periodically diff AP-MT runs vs the index).**
  Heavy (full enumeration per tick), and the `_interesting()` gate means
  gated runs would be re-examined forever. The registry gives targeted
  recovery for free.

## What this makes harder later

- The 24h watcher deadline becomes load-bearing for adoption correctness:
  anyone raising `_WATCHER_DEADLINE_S` must know `ORPHAN_ADOPT_AFTER_S`
  derives from it — one shared constant, not two literals.
- New durable state in shared Redis: a Redis flush silently empties the
  in-flight hash, losing pre-flush orphans — same existing exposure as the
  retry queue, not new in kind.
- 2 extra Redis ops per warm run (`HSET`/`HDEL`) — negligible at ~312
  runs/day.
- Lifetime `warm_watchers_started`/`resolved` gap keeps growing forever by
  design; anyone reading raw counters later must not re-conclude it's a
  bug — the comments in `execution_index.py`/`routes.py` are the defense.

## Risks for the coder

- Every new hash op must keep the fail-open contract (`try`/`except` + debug
  log), same as `_incr` — counting/registry failures must never affect
  indexing or run delivery.
- The sweep only runs when `watchdog:tasks` is granted; don't "fix" that
  silently — doctor problem (b) is the designed backstop for exactly this
  gap.
- Never rename/reset the existing lifetime Redis counters.
- Mutation-test the new doctor checks: assert each problem fires AND
  clears, not just the happy path.
