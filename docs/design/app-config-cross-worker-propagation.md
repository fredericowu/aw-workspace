# App-config saves must reach all AW_WORKSPACE_WORKERS without a restart

Card: `3fc5bf3b-9510-81aa-8f5a-c32aeb1db705` · Architect decision 2026-10-17 ·
Implementation: coder-opus

## The surprise that reframes the card

The card asks "choose a propagation mechanism". **The mechanism already
exists, is the right one, and was verified live 60/60 on 2026-09-06** (KB:
card 3d35bf3b, `aw-app-agents-platform-runners/tests/test_on_config_reloaded.py`
docstring). The full chain is in this tree today:

- `src/apps/routes.py:1180` — `save_app_config` ends with
  `reconciler._trigger_broadcast("config", slug)`, after the mirror write.
- `src/apps/lifecycle.py:198` — `AppLifecycle.publish` → `apps:changed` over
  `src/libs/redis_coord.py`'s `RedisBroadcaster`
  (`aw:ws:<slug>:bcast:apps:changed`).
- `src/apps/reconciler.py:528` — every worker's handler
  `converge_in_process` → `_refresh_config` (reconciler.py:612) mutates
  `ctx.config` **in place** and calls `on_config_reloaded` (reconciler.py:648).
- `src/apps/base.py:57` — the hook contract the apps already implement.

So today's 1/5-stale measurement is a **regression of a working mechanism**,
not a missing design. Two concrete silent-death defects were found by reading,
one of them sufficient on its own to produce exactly the measured signature
(only the worker that served the POST is fresh ⇒ ~4/20):

### Defect A — the pending-broadcast wedge (prime suspect for the live failure)

`Reconciler._reconcile_provisioned` arms the coalescing flags at
`src/apps/reconciler.py:1262-1265` (`_pending_gateway_reload = False`,
`_pending_broadcast = False`) and only clears them back to `None` on the happy
path at :1398-1411. **There is no try/finally.** Any exception or cancellation
escaping the ~140 lines in between leaves `_pending_broadcast = False`
(≠ `None`) forever on that worker — from then on *every*
`_trigger_broadcast` on that worker (reconciler.py:466-468), including the one
at the end of every config save it serves, takes the "inside a pass" branch
and never publishes. Known live triggers for an escaping exception:

- `reconcile_on_boot`'s `asyncio.wait_for(reconciler.reconcile(), 1200)`
  (`src/apps/routes.py:1951`) — a timeout **cancels the coroutine mid-pass**
  (the 2026-10-08 incident was exactly a boot-budget overrun).
- `POST /api/apps/reconcile` cut by the tunnel edge at 30s (known edge
  behaviour) — Starlette cancels the handler mid-pass.
- Any unexpected exception between the per-app try/excepts (e.g.
  `self.local.list` in the upgrade branch, :1346-1348).

The convergence watchdog (routes.py:1854, leader-only, 900s) re-runs
`reconcile()` and therefore re-arms/flushes the flags — so the **leader**
self-heals; the boot provisioner (a different worker, by design — see
`attach_on_boot`'s note) and any worker that served a cancelled manual
reconcile stay wedged until restart. Silent: zero log, `doctor` green.

### Defect B — the relay dies permanently on any Redis hiccup

`RedisBroadcaster._relay_loop` (`src/libs/redis_coord.py:205-223`) catches
only `asyncio.CancelledError`. A dropped connection (Redis restart, auth
change — the 2026-10-12 Redis-auth rollout is a live example of such an
event class) raises `ConnectionError` out of `pubsub.listen()`, the relay
task dies, and that worker never converges again. No reconnect, no resubscribe,
no log beyond an unretrieved task exception. `AppLifecycle.start`'s failure
path (lifecycle.py:171-184) is equally silent: a worker that boots with Redis
briefly unreachable runs forever without convergence, on a `log.warning`.

### Measured live today (2026-10-17, from an agent container)

A probe `PUBLISH aw:ws:aw:bcast:probe-architect` on the server's Redis
(`aw-remote-host-redis:6379/0`) was delivered to **80 subscribers** — the
relays are alive *right now* (post this morning's restart). So the transport
is not the standing problem; the wedge (A) is the best explanation for the
pre-restart measurement, and (B) is a second, independent way to reach the
same state. Both are fixed by this spec; neither alone restores trust, which
is why there is also a floor.

## The decision

**Fix the existing broadcast (A and B), make its death visible in
`doctor`/`/api/health`, and add a Redis-free deterministic floor: an on-disk
config-generation token checked at the request boundary.** No new transport,
no change to the app-facing contract. The broadcast stays the push path
(instant, covers idle workers — e.g. the flock-leader watchdog reading
`kanban_sweep_enabled`, the 2026-09-06 case); the generation check is the
correctness floor that turns "any future transport failure ⇒ wrong answers
indefinitely" into "wrong answers never, on request-serving paths".

## Where it lands

### 1. Wedge fix — `src/apps/reconciler.py`

Restructure `_reconcile_provisioned` so this invariant holds: **after any
exit (return, raise, cancellation), `_pending_broadcast` and
`_pending_gateway_reload` are `None`.** Arm → `try:` body → `finally:` read
both flags, reset both to `None`, then best-effort flush (the existing
clear-first semantics at :1398-1411 move into the finally). Under
cancellation, skip the awaits (an `await` in a cancelled task re-raises
immediately) — resetting the flags is the part that matters; a partial pass's
convergence is already the convergence watchdog's job (routes.py:1860-1872).

### 2. Relay self-heal — `src/libs/redis_coord.py`

`_relay_loop` becomes a reconnect loop: catch `Exception` (not just
`CancelledError`), log, backoff (1s doubling, cap ~30s), re-create the
pubsub and re-`psubscribe`, resume. `CancelledError` still exits. Expose
`RedisBroadcaster.relay_alive` (task exists and not done) and a
reconnect/failure counter for §4. Keep the single-delivery-path property
(publish never short-circuits locally).

### 3. Generation floor — new, small

- `src/apps/config_store.py`: add `bump_generation()` — atomically write a
  fresh random token to `<workspace_home>/app-config/.generation` using the
  same `mkstemp` + `os.replace` pattern `save()` already uses (:86-90) — and
  a cheap reader returning `(st_mtime_ns, token)`, reading the file only when
  the mtime moved. Missing file = "no save has ever happened", skip.
- `src/apps/routes.py::save_app_config`: call `config_store.bump_generation()`
  immediately before `_trigger_broadcast("config", slug)` at :1180 — i.e.
  **after** the mirror write at :1092-1104, for the same already-documented
  reason the broadcast is published last: a worker acting on the new token
  must read a mirror row that is already final.
- `src/apps/reconciler.py`: new `refresh_configs_in_process()` — under
  `lifecycle.in_process_exclusive()`, read the mirror
  (`asyncio.to_thread(self.local.list)`) and run the **existing**
  `_refresh_config` (:612) over loaded apps. Config only: no
  `attach`/`detach`, so no mounting work ever runs inline in a request.
  Log `reconfigured=[...] pid=<pid>` when non-empty (per-worker evidence for
  acceptance #2).
- `src/api/app.py` (`create_app`, near the existing `add_middleware` at
  :493): a **pure-ASGI** middleware (not `BaseHTTPMiddleware` — known
  contextvars hazards) scoped to `scope["type"]=="http"` and
  `path.startswith("/api/apps/")`. Per request: one `os.stat` of
  `.generation`; if `st_mtime_ns` differs from this worker's last-seen,
  take an `asyncio.Lock` (on `app.state`), re-check, read the token, and if
  it moved `await reconciler.refresh_configs_in_process()` before passing the
  request on. State lives on `app.state`, not module globals.

Why `/api/apps/` is the right scope: every measured read path is under it —
the app's own mounted routes (`Mount("/api/apps/<id>")`,
`src/apps/runtime.py:1031`), `GET /api/apps/{slug}/config`, `leaf_tool_call`,
and the Tier-1 MCP endpoints the gateway dials (the `knowledge_search` case).
One stat (~µs) per such request; the mirror read is paid only when a save
actually happened since this worker last looked.

### 4. Kill the silence — `src/apps/degradations.py` + doctor

Add a per-worker check to `_collect` (degradations.py:90), active only when
`AW_WORKSPACE_WORKERS > 1`: broadcaster never started (`AppLifecycle.start`
returned False / `_broadcaster is None`) or relay task dead/flapping (§2's
accessors, threaded through `app.state.app_lifecycle`) ⇒ a degradation row
("config/app changes will not propagate from/to this worker"). `/api/health`
is served by one worker per request, so each worker reports its own state —
degrades-open, same philosophy the module already documents (:32-35). The
existing CLI section `src/cli/commands/doctor.py::_redis` (:287) picks it up
via the server report it already consumes.

## The two-hook contract — preserved, and pinned

- `on_config_saved` **exactly once in total**: unchanged — it is called only
  from `save_app_config` (routes.py:1150-1152). Neither
  `refresh_configs_in_process` nor the middleware can reach it;
  `_refresh_config`'s docstring (reconciler.py:612-620) already forbids it.
- `on_config_reloaded` **in every worker**: inline on the request worker
  (routes.py:1147-1149), via broadcast on idle workers (reconciler.py:648),
  via the generation floor on any worker the broadcast missed.

Pin both with a test in `src/tests/unit/apps/test_lifecycle_split.py`: one
"save" through the routes path against reconciler A, one converge **and** one
`refresh_configs_in_process` on reconciler B sharing the same mirror —
assert `saved_calls == 1` total, `reloaded_calls == 1` on A and ≥1 on B, and
that a second refresh with no change calls nothing (the
`config_with_defaults` normalization at :633-634 is load-bearing; don't
compare raw dicts).

## Acceptance mapping (5 workers, no restart after the deploy's own)

1. **20/20 fresh reads** — deterministic via the floor: every `/api/apps/*`
   request re-checks the generation before dispatch, independently of Redis.
2. **`on_config_reloaded` on every traffic-serving worker** — observable as
   the new `reconfigured=[knowledgeable] pid=N` log lines (distinct pids) plus
   the existing `apps: 'apps:changed' received` lines.
3. **`on_config_saved` once** — pinned by the test above; live, verify no
   duplicated save-side effects (one `published 'apps:changed'` line, one
   mcp.json render).
4. **Real case** — re-save `forward_upstreams` on `knowledgeable`
   (`POST /api/apps/knowledgeable/config`, the merging route), then 20×
   `knowledge_search(include_code=true)` through the gateway → `code.status
   == "ok"` 20/20. Expect `published ... to N subscriber(s)` with N ≥ 5.

## Rejected

- **`ctx.config` as an mtime-revalidating property (card option 2).** The
  hook is `async`; a sync dict read cannot await `on_config_reloaded`, so
  half the contract (and half the acceptance) is unreachable from that seam.
  Replacing the dict with a proxy also breaks the documented stable-identity
  contract (reconciler.py:637-642) whose violation is what originally made
  an app invent its own `_live_config` cache.
- **Generation polling INSTEAD of pub/sub (card option 3 alone).** Idle
  workers never stat: the flock-leader watchdog reading a flipped flag — the
  exact 2026-09-06 bug — regresses. The floor complements the push; it
  cannot replace it.
- **Supervisor/signal fan-out (card option 4).** uvicorn owns the worker
  processes and their signal handling; a signal handler can't await the async
  hook; it adds a second IPC channel to maintain when fixing the first one +
  a floor already meets the acceptance.
- **New Redis machinery / new topic (card option 1 as "build it").** It's
  built. The per-instance risk the card warns about is real but already
  settled: the broadcaster uses `get_workspace_redis_url()`
  (redis_coord.py:97, `AW_WORKSPACE_REDIS_URL` → `AW_REDIS_URL` → default) —
  the same resolution every other core redis_coord consumer uses, confirmed
  live today (probe delivered to 80 subscribers on
  `aw-remote-host-redis:6379/0`, prefix `aw:ws:aw:`). This design adds **no
  new Redis dependency**, and the floor makes a wrong-instance Redis degrade
  to "slower idle-worker propagation + a visible degradation row", never to
  wrong answers.
- **Runner-up, kept on file:** if Redis flakiness recurs, add a per-worker
  2s poll task on the same `.generation` file (third leg, still Redis-free).
  Not now — the middleware gives request-path determinism without a timer.

## What this makes harder later

- **Bakes in "all workers share one filesystem."** The floor (flock, locks,
  `.generation`) is per-container. A future multi-host core keeps only the
  broadcast and silently loses the floor — any such split must revisit this
  doc.
- **`on_config_reloaded` latency becomes request latency.** The first
  `/api/apps/*` request after a save pays mirror-read + every changed app's
  hook inline. base.py:57's "in-process derived state only" rule is now also
  a latency contract; a slow hook shows up as one slow unrelated request.
- **Scope rule to remember:** a future core route *outside* `/api/apps/`
  that reads `ctx.config` is push-covered but not floor-covered.
- **Reconnect can mask a persistently-down Redis** — that's what §4's
  degradation row is for; don't ship §2 without §4.

## Risks for the coder

- **Do not bump the generation inside `config_store.save()`** — that's also
  called from the uninstall-snapshot path; bump explicitly in
  `save_app_config`, at the broadcast site, keeping the ordering argument in
  the comment at routes.py:1169-1179 true for both channels.
- **The finally-flush under cancellation must not await** (it would re-raise
  at the first await and still skip the reset if ordered wrong). Reset flags
  first, then best-effort flush guarded by its own try/except.
- Compare `st_mtime_ns` by `!=`, not `>` (rename/restore can move it
  backwards); confirm change by token content.
- Middleware: tolerate ENOENT (fresh workspace); never run attach/detach;
  hold the per-worker lock across re-check + refresh so a burst of requests
  after a save converges once, not N times.
- **Tests must not touch the live Redis keyspace** (known hazard: the suite
  shares it). §1/§3 tests are pure-FS/stub; §2's reconnect test injects a
  fake pubsub in `src/tests/unit/libs/test_redis_coord_unit.py`. If anyone
  insists on a live-Redis integration test, isolate with a random
  `AW_WORKSPACE` slug.
- Wedge regression test (test_lifecycle_split.py): run a pass whose body
  raises mid-way, assert both flags are `None` after, and that a subsequent
  `_trigger_broadcast` publishes immediately (stub lifecycle records it).
  Repeat with `asyncio.CancelledError`.
- Existing `src/tests/unit/apps/test_routes_config.py` will exercise the new
  bump — conftest already isolates `workspace_home`; keep it that way.
- **Out of scope, do not touch:** Tier-1 Python code reload on marketplace
  update (neighbouring problem, separate note), and anything about
  aw-knowledgeable's own single-worker constraint (different service).
