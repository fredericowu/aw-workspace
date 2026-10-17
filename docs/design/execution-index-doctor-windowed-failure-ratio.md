# Execution-index doctor: measure failures, not exclusions — and in a window

Card 3fc5bf3b-9510-8142-9542-ec167a502fb3. Architect decision, 2026-10-17.

## The problem

`GET /execution-index/doctor` (repos/aw-app-agents-platform-runners,
`agents_platform_runners_app/routes.py:182-183`) computes

```python
skipped = max(attempted - indexed, 0)
skip_ratio = (skipped / attempted) if attempted else 0.0
```

which counts `skipped:gate` — intentional exclusions by `_interesting()` —
as failures. Live counters (2026-10-17): attempted 935, indexed 154,
skipped:gate 562, skipped:other 208 (all historical, pre-v0.173.0),
skipped:other:ReadTimeout 8, every other failure key 0. With ~60% of runs
legitimately gated, the ratio can never drop below the 0.5 threshold:
the check is red by construction while the indexer is demonstrably healthy
(8/9 runs indexed since the 15:19 restart, zero new failures). An
always-red check trains people to ignore it — and this is the exact check
that should have surfaced the original skipped:other incident.

## Decision 1 — the numerator and denominator

- **Denominator (eligible):** `attempted − skipped:gate`, clamped ≥ 0.
  These are the runs that *should* have been indexed.
- **Numerator (failures):** the sum of every counter whose name starts
  with `skipped:` **except** `skipped:gate`. Expressed as a prefix rule,
  not an enumerated list, because `skipped:other:<ExceptionType>` keys are
  discovered dynamically via `scan_iter`
  (`execution_index.py:126-140`) — a new exception type must count as a
  failure the first time it appears, with no code change.
- **Not `eligible − indexed`.** `attempted` increments at the top of
  `index_run` (`execution_index.py:293`); the outcome counter lands up to
  ~180s later (60s finalize poll + 120s KB POST budget). A subtraction
  counts every in-flight run as a failure for its whole duration; the
  explicit failure-key sum counts only concluded failures.
- No double counting: since v0.173.0 the generic handler increments only
  the dynamic `skipped:other:<Exc>` key (`execution_index.py:363`); plain
  `skipped:other` is a frozen legacy key. Summing both is safe — their
  increments were always disjoint.

## Decision 2 — the window

**Hourly Redis buckets with TTL; the doctor ratio reads a 24h window.
Lifetime counters stay untouched for `/execution-index/status`.**

Mechanics (seams, not line-by-line implementation):

- `_incr(name)` (`execution_index.py:116`) dual-writes in one pipeline:
  `INCR` the existing lifetime key **and** `INCR` + `EXPIRE` an hourly
  bucket key, e.g. `execution_index:counters:w:<name>:<YYYYMMDDHH>` (UTC,
  derived from `time.time()` — deterministic across workers). TTL ≈ 26h:
  a bucket is only written during its own hour, so it lives ≤ ~27h and
  the keyspace is self-pruning — no reset job, no manual surgery.
- A new window snapshot function mirrors `counters_snapshot()`
  (`execution_index.py:143`): one `MGET` over the last 25 bucket keys per
  counter name (fixed names + dynamically discovered `skipped:other:*`),
  summed per name; `None` for everything when Redis is unreachable, same
  contract as today.
- The doctor route computes `eligible`, `failures` and
  `failure_ratio = failures / eligible` from the **window** sums. Gate:
  `eligible ≥ 5` in the window, else `ok: true` with an explicit
  "insufficient volume to judge" note in the detail. Threshold:
  `failure_ratio > 0.2` → problem. (The old 0.5/20 pair calibrated a
  polluted ratio; with a clean numerator, a sustained 20% failure rate on
  eligible runs is already an incident.)
- Doctor detail carries both views: `counters_window`, `window_hours`,
  and the existing lifetime `counters`, so a human lands on the full
  history without a second call.

Why this is WORKERS=N-safe: the only state is Redis `INCR` on keys whose
names are pure functions of (counter, wall-clock hour). No in-process
accumulation, no leader, no broadcast, no read-modify-write — nothing for
a worker to own. This is deliberately the same shape as the existing
lifetime counters, which already survived the WORKERS=N audit
(`execution_index.py:54-59`).

Redis cost: ≤ ~27 live buckets × (9 fixed + a handful of dynamic) short
string keys — a few KB, self-expiring.

### Explicitly out of scope (declared, not forgotten)

- **The warm-watcher gap check** (`routes.py:185-189`) stays on lifetime
  counters, unchanged. It shares the cumulative property (today's gap of 8
  is permanent history), but it is currently green (threshold 20), it was
  the check that worked during the original incident, and windowing a
  started/resolved *pair* has a skew problem the ratio doesn't (a watcher
  started at the window's edge resolves outside it). If a mass-kill
  incident ever wedges it red, apply the same bucketing then.
- **The other three `contributes.doctor` checks** were audited and do not
  have this pattern: aw-app-call-agent `/credentials/check` is a live
  probe (`call_agent_app/routes.py:114-133`), aw-app-architecture
  `/provision/check` reads current provisioning state, aw-app-signoz
  serves a provision-state file. No change needed anywhere else.

## Rejected alternatives

- **Persisted baseline + delta comparison.** Needs a second state object
  (the baseline), a policy for when to re-stamp it, and a cross-worker
  answer to "who stamps it" — after an incident the baseline must be
  manually advanced, which is the manual-reset problem wearing a costume.
  The window forgets by itself; the baseline forgets only when told.
- **Keep cumulative, document a manual reset.** Guarantees the exact
  failure mode this card exists to kill: permanent red after any incident
  until someone does Redis surgery, i.e. a check whose green requires an
  operator. Rejected on principle.
- **Precise sliding window (ZADD per event + ZREMRANGEBYSCORE).** Exact,
  but one sorted-set member per run event with explicit trimming — more
  state and more code for precision the doctor check cannot use. Hourly
  granularity is far finer than anyone reads this check.
- **One-off zeroing of the lifetime counters.** Destroys the historical
  record `/status` exists to show, and recurs at the next incident.
- **`eligible − indexed` as the numerator.** Counts in-flight runs as
  failures (see Decision 1).

## What this makes harder later

- The ratio only sees the last 24h: a slow failure (say one lost run per
  day at low volume) stays under both the min-sample gate and the
  threshold indefinitely. The lifetime counters at `/status` remain the
  place where slow leaks are visible; the doctor check now answers "is it
  failing *now*", not "has it ever failed".
- Bucket keys bake in the hour format; changing window granularity later
  means a transition period where old buckets don't match the new scan
  pattern (benign — reads just under-count for ≤ 27h).
- Anything else that starts reading the windowed keys inherits the 26h
  TTL as an API.

## Risks for the coder

- `FakeCounterRedis` in `tests/test_execution_index_counters.py` has only
  `incr`/`mget`/`scan_iter` — the pipeline + `expire` dual-write needs the
  fake extended, and the existing tests assert lifetime counts that must
  keep passing untouched.
- `_incr` is best-effort by contract (`execution_index.py:117`): the dual
  write must stay inside the same try — a bucket-write failure must never
  affect indexing or the lifetime count.
- The dynamic-name scan for the window must match the bucket pattern
  (`...:w:skipped:other:*`), not the lifetime pattern — parse the name by
  stripping the trailing `:<hour>` segment, don't split on `:` naively
  (counter names themselves contain colons).
- `decode_responses=True` on the counter client — `scan_iter` yields
  `str`, `mget` yields `str|None`; mirror `counters_snapshot()`'s
  None-handling exactly.
- Immediately after deploy the window is empty → insufficient volume →
  `ok: true`. That is desired (fresh deploys start green), but a test
  asserting "red stays red across restart" would be wrong — don't write
  one.
