# Standard — `ctx.state`, an app's own cross-worker state

**Status:** shipped. Distilled from `docs/design/app-shared-state-facade.md`
(approved 2026-09-30, §11 amendment 2026-10-09) for Kanban
`3f45bf3b-9510-81f0-9429-e88fbf216297`. Read the design doc for the full
rationale and rejected alternatives; this is the rule a coder follows, not
the argument for it.

**Scope:** any Tier-1 app that needs a fact to be the same answer on every
one of `AW_WORKSPACE_WORKERS` worker processes — a job's status, a tab
registry, a background task's fan-out, a named claim one worker should hold
at a time. If a per-process cache or module-level dict would give a
different answer depending on which worker handled the request, that is the
shape this standard exists for.

---

## 1. The rule

**An app that needs cross-worker state asks for the `state:own` capability
and uses `ctx.state`. It does not import `src.libs.redis_coord`,
`src.apps.fs_lock`, or a raw `redis`/`redis.asyncio` client against core's
Redis.** `aw-workspace-cli doctor` flags the latter as a facade bypass (§5
below) — not a style preference, an enforced boundary the same way
`db:own-tables` enforces the `app__<slug>__` table prefix.

This is **not** a replacement for `ctx.db` (durable, relational, no
TTL/pub-sub) or `ctx.secrets` (credentials). Use `ctx.state` only for the
shape above: transient, cross-worker, TTL'd or claim-based.

## 2. The three operations — nothing else

`ctx.state` exposes exactly three sub-namespaces. There is no fourth, and
none of the three grows a raw-client escape hatch.

### 2.1 `ctx.state.kv` — TTL'd key/value, Redis-backed

```python
await ctx.state.kv.put("last-sync", {"at": time.time()}, ttl=300)
value = await ctx.state.kv.get("last-sync")       # None on miss or Redis-down
names = await ctx.state.kv.scan(prefix="job-")     # bare names, own namespace only
await ctx.state.kv.delete("last-sync")
```

- `name` is a bare local identifier (`[a-z0-9][a-z0-9_.-]{0,127}`) — never a
  full key. The facade **constructs** the full key
  (`aw:ws:<ws>:app:<app_id>:kv:<name>`); there is no argument shape that
  reaches outside that namespace, because the prefix is never caller-supplied.
- **`ttl` is required and must be `> 0`.** There is no way to write an
  immortal key through this facade — every real call site treats its state
  as transient (a finished job is a UI concern, not a durable record).
- Degrades open: a Redis outage makes `get`/`scan` return a miss/empty and
  `put`/`delete` silent no-ops. Check `ctx.state.degraded` if your caller
  cannot tolerate a miss masquerading as "never written".

### 2.2 `ctx.state.broadcast` — pub/sub fan-out, Redis-backed

```python
await ctx.state.broadcast.publish("job-done", {"id": job_id})
await ctx.state.broadcast.subscribe("job-done", handle_job_done)
```

- `topic` is validated the same way as a kv name, and the facade forces it
  under `app:<app_id>:<topic>` inside `RedisBroadcaster`'s existing `bcast:`
  prefix — same construction rule as kv.
- Best-effort. A publish during a Redis outage is **dropped**, with **no
  local-loopback fallback** — a loopback would make `AW_WORKSPACE_WORKERS=1`
  dev look correct while production silently isn't, which is the entire bug
  class this facade exists to close. Do not add one.

### 2.3 `ctx.state.lease` — named mutual exclusion, **flock-backed**

```python
if ctx.state.lease.claim("sync-job"):
    try:
        ...  # only one worker runs this at a time
    finally:
        ctx.state.lease.release("sync-job")
```

- **Not Redis.** Backed by `src/apps/fs_lock.py` under
  `<AW_WORKSPACE_HOME>/locks/apps/<app_id>/<name>.lock` — its own
  subdirectory per app, so an app can never collide with a core lock name
  or another app's. See §4 for why.
- `claim(name) -> bool` is **one-shot and non-blocking**: `True` means this
  worker now holds (or already held) it; `False` means another worker holds
  it right now. There is no `renew` — the held open-file-description IS the
  liveness proof, dropped by the kernel on exit, crash or `kill -9`.
- `release(name)` and `is_held(name) -> bool` round out the API. `is_held`
  is an acquire-probe when this worker isn't already the holder (claims,
  releases, reports `True`); the microsecond probe-hold is a documented
  limitation shared with `src/apps/service_lease.py`'s identical shape.
- **No `ttl` parameter anywhere in this API.** An API that accepted one and
  silently ignored it would be lying about what backs it.
- Synchronous, not `async` — two O(1) syscalls, correct inline on the event
  loop, not worth `asyncio.to_thread`.
- **No degrade mode.** `ctx.state.degraded` describes kv/broadcast only.
  `fcntl.flock` has no reachability failure: `EAGAIN` *is* the fact that
  someone else holds it. A broken lock directory (read-only, full) raises —
  a real error, not something to swallow.

## 3. `ctx.state.degraded -> bool`

Reports whether the kv/broadcast circuit breaker is currently open in this
worker process (one breaker per process, shared by every app's facade
instance in it — the Redis connection underneath is one resource, not one
per app). Not live-probed per call; a transition into degraded logs a
**WARNING** once per breaker cooldown window, recovery logs **INFO**,
individual failures inside an already-open window stay at **DEBUG**. Does
not cover `lease` (§2.3).

## 4. Why lease is flock, not Redis, when kv/broadcast are Redis

The design originally specified all three operations as thin wrappers over
`src/libs/redis_coord.py`, including a Redis-backed lease
(`RedisLease`/`SET NX PX` + Lua CAS). That class was deleted 2026-10-03,
three days after the design was approved, when every ownership decision in
core became `fcntl.flock`-only — a deliberate ruling (Frederico: "for
defining the leader, it should be flock only, it's not a fallback, it's the
only logic"), made on live evidence that a Redis lease unreachable-by-default
degrades to **no leader at all**, not to "proceed unguarded": 45/45 samples
of the pre-fix watchdog reported no leader, and the regression test for it
silently skipped in every environment, CI included.

The line that decides which backend a `ctx.state` operation gets is the one
`redis_coord.py`'s own docstring draws: **messaging degrades to staleness
(acceptable — a missed broadcast converges on the next restart); ownership
degrades to "two processes both think they own this" (not acceptable — it is
indistinguishable from no coordination at all).** kv and broadcast are the
first kind and stay on Redis. Lease is the second kind and does not ride a
backend whose own authors just proved unreliable for exactly this shape.

This holds for app-owned leases the same as it did for core's: every Tier-1
worker is a process in ONE container sharing ONE filesystem (`ctx` is
in-process only, by design — no Tier-2 surface exists for this facade), so
the flock backend is available to every possible consumer, not just core.

## 5. Enforcement — `aw-workspace-cli doctor`

`doctor` flags any installed **Tier-1** (`inprocess`) app file that imports
`src.libs.redis_coord` directly instead of going through `ctx.state` —
path-based, cheap, the same attribution philosophy as the architecture app's
path-based test attribution. In-process Python cannot be sandboxed, so this
is the honest version of enforcement: not a runtime block, a loud surface
where someone already looks (`src/apps/base.py`'s own module docstring says
this plainly for every capability facade).

One ruled exemption: `aw-app-agents-platform-runners` talks to
**agents-platform's own Redis**, a different instance/keyspace entirely
(cross-*service* coordination with an external system, not workspace
cross-worker state) — it is on an explicit allowlist in
`src/apps/state_facade.py` (`BYPASS_ALLOWLIST`), with its reason kept next to
the entry. Adding a second exemption needs the same standard of evidence:
name the Redis it actually talks to and show it isn't core's.

`doctor`'s report also carries kv/broadcast's degraded snapshot (§3) — not
folded into the overall `ok`, the same posture as the plain `redis` check,
for the same reason: no app has migrated onto the facade yet, so nothing in
production depends on it today. That changes the day the first migration
(below) ships.

## 6. Migration posture

New app code uses `ctx.state`. The four existing hand-rolls each get their
own follow-up card, in this order (per the design, §5):

1. `aw-app-architecture/architecture_app/jobs.py` — reference migration,
   fullest existing shape (TTL + breaker).
2. `aw-app-devctl/devctl_app/relay.py` — kv (tab registry) + broadcast
   (eval relay).
3. `aw-app-tasks/tasks_app/updates.py` — broadcast only.
4. **`aw-app-agents-platform-runners` is explicitly not migrated** (§5
   above) — it isn't talking to this Redis at all.

Core's own `src/apps/install_jobs.py` and `src/apps/service_lease.py` stay
on `redis_coord`/their own primitives directly — core importing core's own
libs is not a boundary violation, and this standard gates **apps**, not core.

## 7. Testing a `ctx.state` consumer

- **Cross-worker proof needs a real Redis (kv/broadcast) or real
  processes/threads sharing one lock directory (lease)**, never a mock —
  follow the multi-instance shape of
  `repos/aw-app-devctl/tests/test_relay_multiworker.py` (two facade
  instances, one Redis) for kv/broadcast, and
  `tests/unit/apps/test_flock_lease.py` /
  `tests/integration/apps/test_watchdog_flock_lease_multiworker.py` for
  lease.
- **Never assert a literal `aw:ws:default:` or `aw:ws:aw:` prefix.** Compute
  the expected key through `kv_key()`/`broadcast_topic()` in
  `src/apps/state_facade.py`, or `_key_prefix()` in `redis_coord.py` — an
  agent container and the production server resolve the workspace Redis URL
  and slug differently, and a hardcoded prefix in a test passes in one and
  silently tests the wrong keyspace in the other.
- **This workspace's tests share the LIVE Redis keyspace.** Use unique
  per-run key suffixes; never `FLUSH` or broadly `SCAN`-delete in a test — a
  broad cleanup in a test run deletes production leases and keys.
- A lease test must **not skip** when run without network access — flock
  needs nothing but a filesystem, and a test that skips reproduces the exact
  silent-coverage-gap the deleted `RedisLease` test left behind.
