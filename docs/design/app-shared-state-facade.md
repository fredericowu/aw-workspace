# `ctx.state` — a gated primitive for app cross-worker state

Status: **design, not code.** Written by the Architect agent, 2026-09-30, for
Kanban card `3eb5bf3b-9510-8109-ac4d-cf8e0ea3dce2`. Every claim is grounded in
a file read this session and cited. Companion to the 10th-instance fix
(aw-app-architecture v0.24.0, card `3eb5bf3b-9510-810a-a513-ec5628c2656f`).

## 0. The decision, in sentences someone can disagree with

Build it. Add one **low-risk** capability, **`state:own`**, and one facade,
**`ctx.state`**, exposing exactly three operations — a TTL'd key-value
registry, a lease, and a topic broadcast — all thin wrappers over
`src/libs/redis_coord.py`, all prefix-validated to the app's own namespace
the same way `DbFacade` validates table names. The four existing app-side
hand-rolls migrate (one follow-up card each), except
`aw-app-agents-platform-runners`, which is explicitly **not** a candidate
(§5). Redis-down semantics become a platform ruling (§6): degrade open,
edge-triggered WARNING on the transition, surfaced in `doctor`.

The alternative seriously considered — "make the per-worker failure loud
instead of preventing it" — is rejected as a *replacement* (§7.1) but half of
it is folded in: the enforcement mechanism for the facade boundary **is** a
loudness mechanism (a `doctor` check that flags installed apps importing
`redis_coord` directly, §4.3), because in-process Python cannot be sandboxed
and pretending otherwise would be dishonest about what a Tier-1 boundary is
(`src/apps/base.py:12-14` says this in core's own words).

## 1. Established facts the design rests on

- **Worker count.** The shipped default is `AW_WORKSPACE_WORKERS=5`
  (`Dockerfile:110`), lowered from 10 by commit `16ae17a` (2026-09-14).
  Runtime override comes from `.aw-workspace/.env` via `read_workspace_env`
  (`src/start/workspace.py:156-169`, `src/apps/paths.py:112`); that file
  currently has **no** override, so 5 is live. Older cards/memories saying 10
  are stale. The design must not care: any N>1 reproduces the class, and N is
  editable without a rebuild — nothing below depends on the number.
- **Core already owns the primitives.** `src/libs/redis_coord.py` ships
  `RedisBroadcaster` (177), `RedisLease` with CAS renew/release Lua (254),
  `cooldown_acquire` (390), `RedisPollQueue` (416), `get_redis_pool` (156),
  and the `aw:ws:<slug>:` prefix (`_key_prefix`, 107).
- **Apps have no gated path to them.** `src/apps/base.py` hands out facades
  for routes/commands/secrets/db/services/watchdog/notify/containers
  (`_FACADES`, base.py:523-532) — nothing for shared state. The capability
  catalog (`src/apps/capabilities.py:27-59`) has no state/Redis entry at all.
- **The hand-rolls, verified:**
  - `repos/aw-app-devctl/devctl_app/relay.py:76,109,116` — lazy-imports
    `RedisBroadcaster`, `get_workspace_redis_url`, and the *private*
    `_key_prefix`.
  - `repos/aw-app-tasks/tasks_app/updates.py:89` — lazy-imports
    `RedisBroadcaster`.
  - `repos/aw-app-architecture/architecture_app/jobs.py:133,146` — lazy-
    imports `get_workspace_redis_url` and `_key_prefix`; has a breaker
    (`_breaker_until`, jobs.py:113) whose degrade path logs at **DEBUG**
    (jobs.py:178,189,218) — invisible under this workspace's INFO default.
  - `repos/aw-app-agents-platform-runners/agents_platform_runners_app/
    warm_pool.py:149-166` — raw `redis.from_url` against a URL from the
    `shared_redis_url` secret: **agents-platform's own Redis, not core's**
    (warm_pool.py:80). Different animal — see §5.
  - Core's own siblings: `src/apps/install_jobs.py` (KV mirror + broadcaster,
    no heartbeat) and `src/apps/service_lease.py` (sync re-implementation of
    the same Lua CAS, service_lease.py:55-70).
- **The namespacing trap, reproduced first-hand this session.** In an agent
  container: `AW_REDIS_URL=redis://172.18.0.1:6379/1`, `AW_WORKSPACE` unset →
  `redis_coord` resolves prefix `aw:ws:default:` on the bridge-gateway Redis.
  In the server: `doctor` reports `redis://aw-remote-host-redis:6379/0`, and
  `.aw-workspace/.env` sets `AW_WORKSPACE=aw` → prefix `aw:ws:aw:`.
  **Different instance, different DB, different prefix.** A test that
  connects from an agent container exercises a keyspace production never
  touches.

## 2. Approach

`ctx.state`, gated by `state:own`, backed entirely by what
`src/libs/redis_coord.py` already has. Three operations, because that is what
the real call sites use — not two (the card undercounted: `tasks_app/
updates.py` exists *solely* for broadcast, and devctl needs it too):

- **`ctx.state.kv`** — TTL'd put/get/scan/delete under
  `aw:ws:<ws>:app:<app_id>:kv:`. TTL is **required** on put (no immortal
  keys; every real site's state is transient by design — install_jobs.py's
  "a finished job is a transient UI concern" rule becomes the platform rule).
  Covers architecture's job mirror, devctl's tab registry, install-status-
  style polls.
- **`ctx.state.lease`** — `claim(name, ttl) -> bool`, `renew`, `release`,
  `is_held`, i.e. `RedisLease` / `service_lease.py`'s `SET NX PX` + CAS Lua
  exposed rather than re-derived, keyed
  `aw:ws:<ws>:app:<app_id>:lease:<name>`.
- **`ctx.state.broadcast`** — `publish(topic, payload)` / `subscribe(topic,
  handler)` over `RedisBroadcaster`, topics forced under
  `app:<app_id>:<topic>` within the existing `bcast:` prefix
  (redis_coord.py:111-112). Covers tasks' fan-out and devctl's relay.

Deliberately **not** included: raw client access, cross-app or core-key
reads, SCAN outside the app's own prefix, arbitrary Lua, `RedisPollQueue`.
This is the narrowest surface covering the observed call sites, not a Redis
passthrough — an app that outgrows it should force this document to grow
rather than route around it.

Prefix validation happens **in the facade**, exactly like
`DbFacade.table()` → `_validate` (base.py:360-363) enforces
`app__<slug>__`. URL and workspace-slug resolution happen **inside the
server process** via `get_workspace_redis_url()` / `_key_prefix()` — the
facade runs in-process (Tier-1), so it inherits the server's env by
construction and the agent-container divergence in §1 cannot leak in through
it. Apps stop importing the private `_key_prefix` entirely.

Async-first, matching `redis_coord`. A sync consumer, if a migration
genuinely needs one, follows `service_lease.py`'s cached-sync-client shape —
do not build a sync mirror speculatively.

## 3. Where it lands

| Change | File | Nature |
|---|---|---|
| Capability `state:own` (risk: low) | `src/apps/capabilities.py:27-52` (`CATALOG`) | add one entry |
| `StateFacade` + `_FACADES` entry + `ctx.state` property | `src/apps/base.py` (pattern: `DbFacade`, 352-390; registry 523-532; property 603-605) | add ~100 lines |
| Key/topic validation helper | new `src/apps/state_facade.py` or inside base.py — coder's call; validation mirrors `src/apps/db_tables.py`'s `_validate` | add |
| Byte-identical catalog mirror | `repos/aw-backend/src/api/app_capabilities.py:18` | add same entry |
| `doctor`: facade-bypass check + degraded surface | `src/cli/commands/doctor.py` (Redis section already at 230-236, fed by a server report) | extend |
| Standard entry (the written rule apps follow) | `docs/standards/` (sibling to `app-backend-websocket-messaging.md`) | new, distilled from this doc |
| Untouched | `src/libs/redis_coord.py` | the facade wraps it; no primitive changes needed |

**Rollout order (binding, from the `new-capability-rollout-order` lesson +
capabilities.py:7-8):** (1) core catalog + validator + facade ship and
restart first; (2) aw-backend mirror updated and deployed (manual deploy —
it does not ride any hook); (3) only then may any app manifest declare
`state:own`. An app shipped against a validator that does not know the
capability is refused the grant, and the refusal is quiet — this ordering is
what prevents that.

## 4. Capability, tier, and what "enforced" honestly means

### 4.1 Name: `state:own`, not `state:shared`

The catalog names grants by what the app may touch: `db:own-tables`,
`secrets:own`, `fs:workspace-data` (capabilities.py:30-37). This grant is *the
app's own cross-worker state namespace* — cross-workerness is the mechanism,
not the permission. `state:shared` (the card's placeholder) is the runner-up;
it reads as if the state were shared *between apps*, which is exactly what
the facade forbids.

### 4.2 Risk tier: **low**

- Reach through the facade is prefix-bounded to the app's own namespace —
  the same reach class as `db:own-tables`, which is low.
- The danger the card documents is today's **ungated** state: any Tier-1 app
  can already read/overwrite `aw:ws:<ws>:leader:*` and every other app's
  keys by importing `redis_coord`. The facade *removes* reach; a capability
  that removes reach and adds a manifest declaration should not be priced as
  high-risk.
- The decisive argument: high-risk caps are refused to unsigned/side-loaded
  apps (capabilities.py:116-117). A side-loaded app refused `state:own`
  would do what all five call sites already did — import `redis_coord`
  directly, ungated. A high tier here would *manufacture* the 11th instance.

### 4.3 Enforcement, stated honestly

In-process Python cannot be stopped from `from src.libs.redis_coord import
...` — base.py:12-14 already concedes this for every facade ("an honest,
auditable boundary… that is what Tier 2 is for"). So the boundary is enforced
the way this workspace enforces things that cannot be runtime-blocked —
by making them **loud where someone looks**:

- **`doctor` check (part of this card's build):** for each installed Tier-1
  app package, flag imports of `src.libs.redis_coord` (and `redis` /
  `redis.asyncio` against core's URL) outside the facade — path-based, cheap,
  same attribution philosophy as the architecture app's path-based test
  attribution. Reported in the existing Redis section
  (doctor.py:230-236). Apps with a ruled exemption (§5:
  agents-platform-runners) go on an explicit, in-repo allowlist with the
  reason next to the entry.
- The manifest finally *declares* cross-worker state, so the marketplace
  grant, `aw-workspace-cli apps`, and review all see it.

Without the doctor check the facade is documentation; with it, drift shows up
on the surface this workspace already trusts for silent degradation. The
check ships in the same card as the facade, not later.

## 5. Migration — mandatory for four, ruled out for one

A primitive nobody migrates to is documentation. Posture: **new app code must
use the facade** (the standard doc + doctor check make that stick), and the
existing sites migrate as **one follow-up card each**, in this order:

1. `aw-app-architecture/architecture_app/jobs.py` — first, and becomes the
   reference migration: it is the newest and already has the fullest shape
   (TTL + breaker). Its hand-rolled breaker is *replaced by* the facade's
   (§6), not kept alongside.
2. `aw-app-devctl/devctl_app/relay.py` — kv (tab registry) + broadcast
   (eval relay). Deletes the private `_key_prefix` import.
3. `aw-app-tasks/tasks_app/updates.py` — broadcast only; smallest diff.
4. **`aw-app-agents-platform-runners` — explicitly NOT migrated.** Its
   `warm_pool.py`/`execute.py`/`execution_index.py` clients talk to
   *agents-platform's own Redis* via the `shared_redis_url` secret
   (warm_pool.py:80,149-166), sharing a keyspace with AP-MT, an external
   system. Forcing those keys under `aw:ws:<ws>:app:<app_id>:` would break
   the contract with AP. It is cross-*service* coordination, not workspace
   cross-worker state; it goes on the doctor allowlist with this reason.

No dual-read/dual-write compatibility machinery: every migrating key is
TTL'd transient state (job mirrors, tab registries, fan-out topics), so a
key-shape change simply ages out across one deploy. Worst case is a
minutes-long blip of "job not visible from another worker" at migration
deploy time — the pre-fix behaviour, briefly, once.

Core's own `install_jobs.py` and `service_lease.py` **stay on `redis_coord`
directly** — core importing core's libs is not a boundary violation, and
rewriting working core coordination is churn this card does not need.
(Optional later cleanup: `service_lease` reusing `redis_coord`'s Lua instead
of duplicating it. Not this card, not the migration cards.)

## 6. Redis-down: one platform ruling instead of four private ones

Every existing site degrades open; the facade makes that the platform
default, per operation, with the caller-visible escape hatch none of the
hand-rolls had:

- **kv:** degrade open. Reads return miss/empty, writes drop, behind a
  breaker with a cooldown (jobs.py:113's shape, now owned by the facade).
  Matches install_jobs.py's rule that an install must not become impossible
  when Redis is down — the mirror is additive by definition.
- **lease:** degrade open — `claim()` returns True when Redis is
  unreachable, matching `service_lease.py:39-44` and `RedisLease`'s
  state-unchanged-on-error contract (redis_coord.py:334-346). Rationale:
  every current consumer prefers double-execution over no-execution.
  **But** the asymmetry is real for future consumers, so:
- **`ctx.state.degraded -> bool`** is part of the API. A caller for whom
  double-run is worse than no-run checks it and refuses. Degrade-open is the
  default, not a straitjacket.
- **broadcast:** best-effort; publish during an outage is dropped, and there
  is **no local-loopback fallback** — a local-only delivery path would make
  WORKERS=1 dev look correct while N>1 production silently isn't, which is
  this entire bug class wearing a new hat. (`RedisBroadcaster`'s
  single-delivery-path design, redis_coord.py:180-186, already embodies
  this; the facade keeps it.)
- **Logging, ruled:** the *transition into* degraded logs at **WARNING**
  (edge-triggered, once per breaker window — the same edge-triggering
  discipline as `RedisLease._try_acquire`, redis_coord.py:361-371); recovery
  logs at INFO; individual failures inside a degraded window stay at DEBUG.
  This directly fixes today's diagnostically-silent outage
  (jobs.py:178,189,218 all-DEBUG). Additionally the server's doctor report
  gains a "state facade degraded since <t>" line — an outage becomes visible
  on the first `doctor` run, not after a log-level archaeology session.

## 7. Rejected alternatives

1. **Loud-failure instead of a primitive.** Rejected as a replacement: there
   is no platform-layer way to detect that a module-level dict is *intended*
   as cross-worker state — a per-worker cache is a legitimate pattern, and
   behavioural detection (probing every app N times and diffing answers) is
   neither cheap nor sound. Ten instances were each "detected" only by a
   human paying the diagnosis cost. What *is* detectable is the facade
   bypass, so that loudness ships as §4.3's doctor check.
2. **The fifth hand-roll with a good comment.** Rejected on arithmetic. The
   class has cost ~10 diagnosis cycles; one instance alone burned ~1.56M
   input tokens across four rediagnoses in 14h. The facade is ~100 lines of
   plumbing over primitives that already exist and already work; the
   hand-roll is ~80 careful lines *per app*, and four implementations have
   already diverged into four different subsets of TTL/heartbeat/breaker.
   The comment does not stop the 11th author; the missing capability means
   the platform has no opinion for the comment to point at.
3. **A general Redis passthrough (`ctx.redis`).** Rejected: it would grant
   exactly the unbounded reach the card flags as the serious part, and it
   pushes the 80 hard lines (TTL, CAS, breaker) back onto every app — the
   generator would survive with a permission slip stapled to it.
4. **High-risk tier for the capability.** Rejected — §4.2: refusing
   side-loaded apps recreates the ungated workaround this exists to end.
5. **Postgres-backed state.** Rejected for this card: every observed call
   site needs TTL/expiry semantics ("rows must not outlive what they
   describe" is why the architecture state stayed in-process for months),
   pub/sub, or sub-second claim latency. Postgres answers none of those
   natively; durable state already has `ctx.db`.
6. **Migrating core's `install_jobs.py`/`service_lease.py` onto the facade.**
   Rejected: the facade exists to gate *apps* at the ctx boundary; core has
   no ctx and no business pretending to be a tenant of itself.

## 8. What this makes harder later

- **Redis becomes load-bearing API for apps.** Today Redis is core's
  implementation detail; `ctx.state`'s semantics (TTL, NX claim, pub/sub)
  are Redis-shaped. Swapping the substrate later means emulating those
  semantics. (Mitigation already banked: one choke point to swap — if F5a
  per-workspace companions ever land, `get_workspace_redis_url()`'s
  resolution order absorbs it, redis_coord.py:79-96.)
- **`degraded` and degrade-open become contract.** Moving lease to
  fail-closed later is a breaking behavioural change for every consumer.
- **Low-risk is hard to raise.** Once side-loaded apps hold `state:own`,
  re-tiering to high revokes grants *quietly* (the refusal path has no
  loud surface) — exactly the class of silent breakage this workspace
  documents. Re-tiering would need its own migration card and a doctor
  warning, so: get the tier right now, per §4.2.
- **`app:<app_id>:` in the key layout becomes ABI.** An app rename orphans
  its keys — TTL makes the data loss nil, but the layout is forever
  greppable and any future tooling (metrics, cleanup) will encode it.
- **Tier-2 apps are outside the door this opens.** `ctx` exists only
  in-process; a Tier-2 container needing cross-worker state will need an
  HTTP surface on core with the same namespace discipline. The capability
  string and key layout are designed to survive that; the transport is not
  built. Do not let a Tier-2 app "solve" this by connecting to Redis
  directly — that is the 11th instance in a container.
- **The doctor allowlist (§4.3) is a drift risk of its own.** Every entry is
  a standing exemption someone must re-justify; keep the reason strings in
  the allowlist file itself.

## 9. Risks for the Coders (non-obvious breakage)

- **Two-repo mirror, manual deploy.** `capabilities.py` ↔ aw-backend's
  `app_capabilities.py` must stay byte-identical in content; aw-backend's
  deploy is manual and its `deploy.yml` has a history of reverting
  production from a stale run. Verify the mirror is live in the running
  backend container, not just merged.
- **Tests lie across the container boundary (§1's trap).** A facade test run
  in an agent container hits a different Redis instance, DB, and prefix than
  production. Rules: never assert a literal `aw:ws:default:` or
  `aw:ws:aw:` prefix — always compute through `_key_prefix()`; integration
  verification of the live facade goes through the server process (an API
  probe or the doctor report), never a direct client connection from the
  agent container.
- **Workspace tests share the LIVE Redis keyspace** (known lesson): facade
  tests must use unique per-run key suffixes and must not FLUSH or SCAN-
  delete broadly — a broad cleanup in a test deletes production leases.
- **Facade activation must not require Redis.** `lifecycle.py`'s degradation
  rule runs config-reload inline when Redis is down; connect lazily like
  every current site does, or a Redis outage turns into an app-load failure
  — a worse failure mode than the one being fixed.
- **No `provision` gating on state ops.** Unlike `ServicesFacade`
  (base.py:439-461), state operations are shared-by-nature and idempotent
  (SET NX, XADD, publish); gating them on the provisioning worker would
  reintroduce per-worker behaviour through the back door.
- **Edge-triggered logging is easy to get wrong.** The WARNING must fire on
  the transition, not per failed op (log flood) and not never (today's DEBUG
  silence). `RedisLease._try_acquire`'s ternary state (redis_coord.py:289-296)
  is the shape to copy — "no fact" (exception) and "negative fact" (clean
  nil) are different states.
- **The existing code lies in one place:** `redis_coord.py`'s module
  docstring (lines 11-15) still says the module has "zero consumers" — it
  has at least six. Do not trust prose there over imports; update it while
  you are in the file.

## 10. Verification for QA (falsifiable claims)

1. An app granted `state:own` can put/get a TTL'd key from two different
   workers (the multi-instance test shape of
   `aw-app-devctl/tests/test_relay_multiworker.py` — two facade instances,
   one Redis).
2. An app *without* the grant touching `ctx.state` gets `PermissionError` +
   a `capability:denied` journal entry (base.py:571-583 path).
3. A key or topic outside the app's namespace is rejected by validation —
   there is no argument shape that reaches core's `leader:*` keys through
   the facade.
4. With Redis stopped: kv reads miss, `claim()` returns True,
   `ctx.state.degraded` is True, exactly one WARNING per breaker window is
   logged, and `doctor` reports the degraded facade. With Redis back:
   recovery INFO, `degraded` False.
5. `doctor` flags a Tier-1 app importing `redis_coord` outside the facade,
   and does not flag `aw-app-agents-platform-runners`.
6. Regression posture per the mutation-test lesson: un-apply the facade in
   the architecture migration card and confirm its cross-worker test fails.

## 11. Amendment (2026-10-09) — `ctx.state.lease` is flock-backed, not Redis-backed

Ruled by the Architect (Agents Platform run `e01007d23e514169bb06d7a5333d1c77`),
after commit `11b9109` (2026-10-03, three days after this doc was approved)
deleted `RedisLease` and made every ownership decision in core an
`fcntl.flock` (`src/apps/fs_lock.py`), on an explicit owner ruling ("for
defining the leader, it should be flock only, it's not a fallback, it's the
only logic") and on live evidence: with this Redis unreachable by default,
degrade-open WAS the behaviour — 45/45 watchdog samples reported no leader,
and the `RedisLease` multiworker test silently skipped in every environment,
CI included.

- **`ctx.state.lease` wraps `fs_lock.try_acquire`/`release`, not SET NX PX.**
  Lock files: `<AW_WORKSPACE_HOME>/locks/apps/<app_id>/<name>.lock` — own
  subdirectory so an app can never collide with a core lock name or another
  app's; `<name>` facade-validated (`[a-z0-9_.-]`, no path separators), same
  discipline as the kv prefix.
- **API: `claim(name) -> bool` (one-shot, non-blocking), `release(name)`,
  `is_held(name) -> bool`.** No `ttl`, no `renew`: the held
  open-file-description IS the liveness proof, dropped by the kernel on
  exit/crash/kill — accepting a ttl the backend ignores would be an API that
  lies. `is_held` is an acquire-probe (win → release → False; EAGAIN → True);
  the microsecond probe hold is documented and matches the flock limitation
  `service_lease.py`'s docstring already names. Synchronous, not `async` —
  these are O(1) syscalls, not wrapped in `asyncio.to_thread`.
- **§6 as applied to lease is void: lease has NO degrade mode.** flock has no
  reachability failure — EAGAIN is itself the fact that someone holds the
  lock. `ctx.state.degraded` describes kv/broadcast only. A broken lock dir
  (read-only, full) raises — a real error, not a degrade path (same ruling
  as `fs_lock._try_acquire`). Never unlink a lock file (fs_lock.py's rule).
- **kv and broadcast stay Redis-backed, unchanged** — the line
  `redis_coord.py`'s docstring draws: messaging/fan-out is genuinely
  Redis-shaped and degrades to staleness; ownership degrades to "two
  processes both think they own this" and therefore does not ride Redis.
- **§8 cross-host argument survives:** `ctx` is in-process only (§8), so
  every possible lease consumer shares one filesystem by construction. The
  API carries no transport detail; a future cross-host surface swaps the
  backend behind the facade.
- **§10 item 4 amended:** lease is unaffected by Redis state; its
  multiworker test must RUN in CI, not skip — the deleted `RedisLease`
  test's silent skip is the cautionary tale.

Rejected: re-deriving SET NX PX + degrade-open (mutual exclusion that is
fiction by default; the pattern core just excised; a test CI never runs);
fail-closed Redis (inherits unreachable-by-default → an op that never
grants); deferring lease (a PO scope change, and unjustified — the flock
backend is smaller than the Redis one it replaces).

Implementation risks flagged alongside the ruling:

1. **Release on the app's deactivate.** A held fd surviving an app reload
   strands the lease in a process whose app is no longer active — every
   facade instance's held fds are released from the app's `on_deactivate`
   hook.
2. **`claim` is one-shot and non-blocking** (`fs_lock.try_acquire` —
   "leadership is decided by one try, not by waiting"). Not wrapped in
   `to_thread`: two O(1) syscalls, inline on the event loop is correct.
3. **The lease test follows `test_flock_lease.py` and a real multi-process
   proof** — no Redis, no skip anywhere, including CI.
