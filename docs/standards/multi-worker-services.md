# Standard — writing code for a `WORKERS=N` service

**Status:** shipped. Distilled 2026-11-07 from two `aw-autoskill-*` skills
(see [§7](#7-provenance)) for Kanban
`4115bf3b-9510-81fb-8509-de540f23bca0`, target
`aw-autoskill-registry-sync-triage`.
**Scope:** any service in this workspace launched with
`uvicorn.run(..., workers=N)` or an equivalent multi-process launcher —
`aw-workspace` core (`AW_WORKSPACE_WORKERS`), `aw-backend`,
`agents-platform-multitenant`. Sibling to
[`app-shared-state.md`](./app-shared-state.md), which covers `ctx.state` for
**Tier-1 apps** specifically; this document is the general case, including
the launcher itself.

Every bug in this class has one root cause: **code written as if it runs in
one process, deployed where it does not.** The two consequences that bite
hardest are a side effect firing N times, and a breadcrumb that never
appears — so the code "obviously never ran".

---

## 1. The audit — six grep-able patterns

Fan these out as one read-only pass per pattern (cheap; parallel
cheapest-tier agents are fine) rather than one slow serial read.

| | Pattern | Why it breaks |
|---|---|---|
| **A** | Module-level mutable state with no claim/CAS — `_pending = {}`, `_seen_ids = set()` mutated across requests | each worker has its own copy, so a second worker's requests never see the first's writes |
| **B** | `asyncio.Lock` / `threading.Lock` used as a cross-process mutex | protects only tasks inside one worker's event loop; invisible to every other process, and gives false confidence |
| **C** | A global holding "the" event loop or "the" connection — `_MAIN_LOOP = None` set once at startup | only whichever process runs that init path populates it; every other worker reads `None` forever, surfacing as an intermittent failure rather than a clean crash |
| **D** | Boot/lifespan singletons with no leader election — a startup hook that seeds something "once" | N workers all race to run it |
| **E** | Pub/sub fan-out with no dedup **before** an external side effect — a `psubscribe` handler that sends a Telegram message or hits a webhook | every worker subscribes independently, so every worker fires the side effect once each |
| **F** | Boot-time seed data relying only on a DB unique constraint, with no `IntegrityError` catch | the loser of the race gets an unhandled exception and the whole process crashes, instead of degrading to "someone else already seeded this" |

For **E**, look for a sibling function that already does it right and use it
as the template. In this workspace `deliver_recovered_run`'s `mark_delivered`
claim is the good pattern; `deliver_foreign_session_event` was the bug,
sending the same Telegram message once per worker until fixed.

This is worth reaching for proactively, not only after a failure: while
building `aw-app-ddns`, a coder caught unprompted that its DNS-refresh loop
relied on a mechanism that never runs because `aw-backend` deploys as
"replica" rather than "core", and fixed it with the same `RedisLease` pattern
before shipping.

## 2. The fix — an atomic claim, not a bigger lock

**Do not reach for "dedup at the point of delivery."** That treats the
symptom. Claim ownership atomically so only one worker ever attempts the side
effect.

- Move the "am I the owner" check out of an in-process dict into Redis or
  Postgres, with an atomic compare-and-set: `SET key value NX EX <ttl>`, or
  `INSERT ... ON CONFLICT DO NOTHING` returning whether the row was new.
- **Include a real heartbeat and expiry** so a crashed or hung owner's claim
  is picked up by a live worker. A claim with no expiry is just a new single
  point of failure.
- **Mutation-test the CAS primitive itself** — temporarily remove the
  `nx=True` and confirm the new test fails. The atomicity guarantee is
  exactly what a passing-but-vacuous test misses; testing only the
  surrounding business logic does not cover it.

**Worked template, end to end.** `agents-platform-multitenant`'s Telegram
approval buttons intermittently said "expired" on a tap that should have
worked: `_pending_approvals` / `_awb_deliveries` lived in a plain in-process
dict while the backend runs 5 workers, so a tap landing on a worker that
never saw the request found nothing for that `request_id`. Fixed by moving
that state to Redis (commit `10ccac3`), then reproduced live — a real
SSH-approval round trip through the actual bot, tapping the actual button,
watched to complete post-deploy.

## 3. The launcher — `log_config=` is the only thing that reaches a worker

Hit and independently re-diagnosed twice, two days apart, in two different
repos here: first `aw-workspace` core (`src/start/workspace.py`, commit
`08eb5cf`), then from scratch in `aw-backend` (`src/start/backend.py`) while
debugging with `DUPDBG` breadcrumbs that never appeared anywhere.

**The diagnostic tell.** You add `logger.info("DUPDBG: …")` to a handler, hit
the endpoint, and it appears in neither stdout nor `docker logs` — while
uvicorn's own access line for that same request
(`INFO: 127.0.0.1 - "GET /x HTTP/1.1" 200`) prints fine. **Uvicorn's own logs
working while yours are silent is this bug, not proof your handler never
ran.**

Why: `uvicorn.run(..., log_level=...)` configures only uvicorn's own loggers
(`uvicorn`, `uvicorn.error`, `uvicorn.access`) via its internal
`Config.configure_logging()`. It never touches the root logger or an
application-named one. A bare `logging.basicConfig()` before `uvicorn.run()`
does not help either once `workers > 1`: each worker is a genuinely separate
process (`multiprocessing.get_context("spawn")`), and it only re-runs
`Config.configure_logging()` with whatever `log_config=` was handed to the
original call. It does not re-run your `main()`.

**The rule — any `uvicorn.run(..., workers=N)` in this workspace passes an
explicit `log_config`:**

```python
import copy
import uvicorn
from uvicorn.config import LOGGING_CONFIG

log_config = copy.deepcopy(LOGGING_CONFIG)
log_config["root"] = {"level": "INFO", "handlers": ["default"]}
# add your own formatters/handlers here if needed

uvicorn.run(app, host=host, port=port, workers=n_workers, log_config=log_config)
```

Audit grep:

```bash
grep -rn "uvicorn.run\|log_config\|logging.basicConfig" src/start/*.py
```

A `uvicorn.run(...)` with `workers=` and no `log_config=` has this bug
**latent** even if nobody has noticed — any logging added inside a handler
will vanish under real multi-worker traffic.

## 4. Proving a fix holds — green CI is not enough

A green workflow proves the path was exercised once, usually against a single
test worker.

1. Hit the stateful endpoint from **fresh connections, repeatedly** — not one
   long-lived connection, which sticky routing can pin to a single worker.
   One verification did 15 fresh polls and confirmed all 15 saw shared state.
2. **Cross-check via SigNoz (or equivalent tracing) that those polls actually
   landed on different worker PIDs.** Without this, sticky load balancing can
   make a broken fix look correct by accident — every poll happening to hit
   the one worker holding the stale in-process state.

### The 50/50 probe — a partial reload is not a wrong fix

Under `WORKERS=N`, a code push or app update does **not** make every existing
worker reload the module; a worker keeps running what it imported until it
restarts. One session chased a post-deploy probe that looked flaky (some
responses "bare", some "headered") and proved the fix was fine by firing 6
identical POSTs back to back: 3 bare, 3 headered — a clean ~50/50 split, the
signature of half the workers still on the pre-fix module.

The remedy is `aw-workspace-cli restart core` (forces every worker to
reload), not another code change, and not restarting the individual app's
container — that does not necessarily reload core-hosted code. Reach for
probe-N-times-and-look-for-a-split whenever a just-deployed fix to a
`WORKERS=N` service seems inconsistently applied; it is faster and more
conclusive than re-reading the diff.

See also
[`../runbooks/shipping-and-proving-a-deploy.md`](../runbooks/shipping-and-proving-a-deploy.md)
— the general "a green deploy is a presence check" discipline, which applies
doubly here, since this class of bug only reproduces under real multi-worker
traffic.

## 5. What not to do

- Don't add a lock and call it done — verify it is a **cross-process** lock
  (Redis/Postgres), not pattern **B**.
- Don't dedup at the delivery point when the bug is "N workers all think they
  own this". Fix the ownership claim.
- Don't trust a green deploy workflow as proof for this class of bug.

## 6. The reference instance — architecture provision jobs (**closed** 2026-09-30)

The most expensive instance of **pattern A** in this workspace, and the one
to read as a second worked example of §2, because the measurement and the
cost are both on record.

`apps/architecture/architecture_app/jobs.py` kept its provision-run job
registry as a bare in-process `_jobs` dict under `AW_WORKSPACE_WORKERS=10` —
ten processes, ten heaps, no request stickiness. `POST /provision/run`
created the job on whichever worker answered; the very next
`GET /testcases/jobs/<id>` landed on a different one, found nothing and
returned `404 no such run job`, surfacing to a user as "lost track of the
job" even though every component had provisioned correctly.

**Measured before the fix: 7 of 12 consecutive polls of one healthy job came
back 404.** That is the shape of evidence this class of bug needs — not a
re-read of the diff.

The cost was not the 404. `aw-workspace-cli architecture autoprovision` read
it as a failure and exited non-zero, and the hourly *Architecture Test
Provisioning* task escalated every exit-code **change** to a fresh,
memory-less agent. Because the race is probabilistic the exit code flapped,
so every flip was a "change": ~1.56M input tokens in one 14h window, and at
least ten rounds since 2026-09-07 each independently re-deriving the same
diagnosis.

**The fix is §2 applied literally** (landed 2026-09-30, Kanban
`3eb5bf3b-9510-810a-a513-ec5628c2656f`): each job is **also** written to a
TTL'd Redis key (`aw:ws:<ws>:architecture:job:<id>`) that every worker can
read. Four details worth copying:

- **The mirror is strictly additive.** The owning worker's `_jobs` entry stays
  authoritative for its own jobs — it holds the live object the run thread
  mutates. Only *visibility* moved to Redis, not ownership of the object.
- **A TTL plus a heartbeat answers the original objection.** The module used
  to argue against a shared registry on durability grounds: a workspace
  restart kills whatever was running, so a "running" row would outlive the
  thing it describes. A short-lived entry renewed by a heartbeat from the
  owning worker means a dead worker takes its claim with it (§2's expiry
  rule); a finished job is retained for exactly the same window the
  in-process copy was, then expires. The durable record is still
  `last_run_status` on the testcase.
- **Every store call degrades, never fails a request.** An unreachable Redis
  falls back to exactly the pre-fix worker-local behaviour.
- **A failed call opens a breaker for 30s.** Otherwise "Redis is unreachable"
  becomes "every poll waits for a socket timeout first", which is worse than
  the bug.

`AW_ARCHITECTURE_JOBS_SHARED=0` turns the mirror off outright — tests that
want purely worker-local behaviour, and the field escape hatch.

**If you land on a `no such run job` 404 today**, it is not the known race:
that is closed and on by default. Run `aw-workspace-cli architecture
provision --check` (read-only, ~10s) to establish whether any component
actually failed, then investigate as a new bug — including whether the mirror
is disabled or the store is unreachable.

## 7. Provenance

Consolidated on 2026-11-07 from the `aw-autoskill` tenant store, card
`4115bf3b-9510-81fb-8509-de540f23bca0`, WP2b-i:

- `aw-autoskill-workers-n-fanout-audit-and-fix` → §1, §2, §4, §5, §6
- `aw-autoskill-uvicorn-worker-silent-log-drop` → §3

§6 **corrects** its source skill, which recorded the architecture job-registry
race as still open and option (a) as not done. It shipped 2026-09-30 and is on
by default; `jobs.py`'s own module docstring is the authority. The section is
kept, rewritten as the closed reference instance, because its measurement and
its cost are the best argument this document has for §2.
