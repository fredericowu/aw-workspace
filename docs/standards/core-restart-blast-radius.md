# Standard — the blast radius of a restart

**Status:** standard, written 2026-10-11. This is a **promise about current
behaviour**, not a proposal: everything stated as "today" was read in
`/opt/aw-workspace` at that date and is enforced by tests named inline.
**Source design:** [`docs/design/service-supervision-activation-audit.md`](../design/service-supervision-activation-audit.md)
§D. **Card:** `reliability:service-supervisor-restart-loop-and-health-degraded`
(`3f65bf3b-9510-812a`), itself from the 2026-10-11 proxy outage
(`reliability:proxy-server-dies-silently-killing-all-browser-mcp`).
**Scope:** what a restart of the workspace container, of the core process, or
of a single uvicorn worker does to supervised state — Tier-1 managed services
(`subprocess.Popen` children of core, `src/apps/services.py`) and Tier-2 app
containers (`src/apps/containers.py`).

Nobody had written this down, and the reasonable assumption — "restart is
safe" — was false. On 2026-10-11 a **clean** core restart (`ExitCode=0`)
killed `proxy:proxy-server` along with every other Tier-1 service; it never
came back, with no log line and no error anywhere, and all four
browser-automation MCP families were down for ≥27 minutes. The only
component that could name the cause was `aw-workspace-cli doctor`, which
nothing runs proactively.

---

## 1. Workspace-container restart

`aw-workspace-cli restart` of the container itself, a host reboot, a redeploy
that recreates it.

**Kills:** the core process; every Tier-1 managed service (they are `Popen`
children — `src/apps/services.py`); and — because podman runs *inside* this
container — **every Tier-2 app container too**.

**Recovery owner:** the boot-reconcile leader's `activate()` pass. An app's
`activate()` registers its service through `ServicesFacade.register`
(`src/apps/base.py`), which gates the actual spawn on
`autostart and ctx.provision`, and `provision` is a **per-boot one-shot**
held by a single elected worker (`src/apps/boot_reconcile_coord.py`).

**Guarantee today — read this before assuming recovery:**

> A core restart permanently stops any Tier-1 service whose activation is
> missed on the next boot, and **nothing will notice**.

That is not hyperbole, it is the mechanism: follower workers and every
post-boot convergence pass load apps with `provision=False`, which registers
services with autostart suppressed by design (correct — it prevents duplicate
spawns). The boot leader's one-shot is therefore the *only* code path in the
system that ever starts an autostart service. Miss it once and no later pass
— attach, converge, or the 900s reconcile — will ever start it, because the
reconcile diffs **packages**, never **processes**.

The missing piece is the **activation audit** (§A of the source design): a
post-pass assertion that every desired app is loaded and every autostart
service is accounted for, re-run on a ~60s cadence. It is **deliberately not
built yet** — it introduces a third home for desired-state and has to land
after the open `$framework.autostart` ADR
(`app-launcher-live-preview-autostart.md`, card
`feature:app-autostart-framework-setting`) or it will resurrect apps a user
switched off on purpose. Until it ships, the sentence in the quote above is
the honest contract, dated.

What *does* hold today, and is new as of this standard:

- a service that dies **while its owning worker lives** comes back within
  ~1s, with backoff and a give-up (§4);
- a service that is not running while its config says `auto_start: true` is
  named in **`/api/health`** (`status: "degraded"`, `degraded[]`) and in
  **`doctor`** — the same check, one implementation
  (`src/apps/degradations.py`). The outage above would have been visible in
  the first second to anything polling health, instead of invisible for 27
  minutes.

## 2. Core-process-only restart, and uvicorn worker respawn

A reload of the Python process without recreating the container; one worker
of `AW_WORKSPACE_WORKERS` dying and being replaced.

**Does NOT kill Tier-1 services.** They are spawned with
`start_new_session=True` (`src/apps/services.py`) so that `stop()` can kill a
service's whole process group — which also means they are **not** killed with
their worker. They *orphan*:

| | |
|---|---|
| process | alive |
| its port | still bound |
| lease heartbeat (`src/apps/service_lease.py`) | dead → lease expires in ≤30s |
| fleet-wide `status()` | reads **"off"** while the process runs |

A naive `start()` into that state loses to `OSError: Address already in use`
— the exact error class named in `service_lease.py`'s docstring, and the one
the proxy app used to mask in its own server class.

**Contract:** `ServiceSupervisor.start()` must, before it spawns:

1. **Reap a verified orphan.** Only once this worker holds the lease (so no
   live worker owns the service), and only for a process that matches on all
   three of: exact argv, the service's own package dir as cwd, and being its
   own session leader — which is what `start_new_session=True` produces.
   Anything less is somebody else's process and killing it would be far
   worse than the collision being avoided.
   Tests: `test_start_reaps_a_verified_orphan_before_spawning`,
   `test_an_unrelated_process_is_never_reaped`.
2. **Treat a bind-failure exit as an immediate give-up**, with
   `reason=orphan-suspected`, rather than entering the backoff loop. No
   amount of backoff wins against a port someone else holds, so five attempts
   would just be five failures and 31 seconds.
   Test: `test_a_bind_collision_gives_up_immediately_as_an_orphan`.

> Implementation note, because the source design said "pid recorded in the
> lease payload": the pid is **not** read from the lease. The lease expires
> 30s after its worker dies, which is precisely when the orphan starts
> mattering, so a pid stored there is gone exactly when it would be needed.
> The reap reads `/proc` instead and verifies three independent facts, which
> is strictly stronger than a recorded pid.

## 3. Tier-2 app containers

They **survive** a core-process restart (podman outlives the worker) and are
reconciled by `ContainerSupervisor` on the next pass. They die with the
workspace container (case 1), because podman itself is inside it.

## 4. What the supervisor now guarantees, and what it costs you

`src/apps/services.py`, since 2026-10-11:

- **A service that dies is respawned** by the reader thread that observed its
  death — detection is free and immediate (it is the moment stdout closes),
  not a poll. Backoff is `min(1s · 2^n, 60s)`.
- **Five consecutive crashes inside the 60s startup window → `gave_up`**: no
  further spawns, one ERROR log with the exit code and the last output, and a
  durable Redis marker (`svc:gaveup:{app}:{service}`) so every worker,
  `doctor` and `/api/health` can see it. An unconditional respawn of a
  service that crashes on startup is a tighter loop than the bug it fixes —
  port flapping, CPU burn, log spam.
- **`gave_up` is cleared only by** a manual `start()`/`restart()` (including
  the forwarded `service_relay` path), an app update/reinstall, or an app
  config save. Waiting does not clear it, and nothing clears it on its own.
- **A deliberate stop is never undone.** `stop()` sets `desired = stopped`
  **before** it signals, because the reader thread's `finally` runs *after*
  `stop()` returns; set that flag afterwards and the pump reads a stale
  `running` and respawns the service the operator just killed.
  Test: `test_a_deliberate_stop_is_not_respawned`.
- **A start the ownership gate declines is deferred, not lost** (added
  2026-10-11, after the first version of this shipped without it). `start()`
  refuses to spawn while another worker's lease is live — that refusal is
  right, it is the port collision the lease exists for — but the lease is a
  30s TTL kept alive by a heartbeat, so a worker that *died* leaves a **ghost
  lease**: a key that outlives its owner and then expires with nothing
  running anywhere. The refusal therefore re-asks for ownership every 5s for
  up to a minute (twice the TTL) instead of ending the start, with its own
  budget: a decline is not a crash and must not spend the give-up budget.
  Tests: `test_a_ghost_lease_defers_the_start_instead_of_losing_it`,
  `test_a_live_peer_lease_still_refuses_the_spawn`,
  `test_a_stop_during_a_lease_wait_is_not_undone_when_the_lease_frees`.
- **`doctor` reads "deliberate or not" off `desired`, never off the absence
  of a crash.** `desired = stopped` is a decision; `desired = running` while
  nothing runs is a fault, and is reported as one, loudly. The ghost-lease
  bug above was first mistaken for an intentional stop *because* the label
  came from `state == "stopped"`, which says nothing about intent — in the
  one tool this workspace has for catching silent degradation.
  Test: `src/tests/unit/cli/test_doctor_autostart_label.py`.

### The operator consequence: `kill -9` stops working as a tool

Before this, a manually killed service stayed dead. Now the supervisor brings
it back in about a second. **Deliberate downtime has to go through
`stop()`** — `aw-workspace-cli stop <app>`, or the Stop control in the app's
title bar. That is the point of the change, and it does invalidate muscle
memory, which is why it is in this standard rather than only in a commit
message.

### Known limits, stated rather than discovered later

- **Backoff counters are per-worker memory**; the give-up marker is the
  durable half. A worker respawn mid-backoff resets the count, so a
  crash-looping service whose owning worker *also* crash-loops could in
  theory never reach give-up.
- **The supervisor's knowledge dies with its worker.** It covers "the
  service died"; it does not cover "the service was never started" or "the
  worker that owned it is gone". That is §1's missing audit.
- **`desired` is per-worker memory too**, which bounds both of the
  declined-start rules above. A peer still renewing its lease after the full
  retry window is taken at its word and this worker stands down; if that peer
  dies *afterwards*, nothing here starts the service (again §1). And a worker
  that never ran a service has an untouched `desired = stopped` that is not a
  record of anything — so `doctor` only calls a stop deliberate when the
  worker answering actually ran the process (`supervised_here` in
  `ServiceSupervisor.status()`), and says "off and unexplained" otherwise.
  At `AW_WORKSPACE_WORKERS=5` that is four answers in five.
- **`/api/health` gained a soft Redis dependency.** With Redis away it falls
  back to this worker's own process table and says so
  (`health_scope: "local-worker"`); it never fails. See §5.

## 5. `/api/health` is 200 even when degraded

`status` is `"ok"` only when `degraded` is empty; otherwise `"degraded"`,
with one row per component that is configured to auto-start and is not
running. **The HTTP status stays 200.** A 5xx here makes any
container-orchestrator healthcheck restart core — the exact restart that
caused the outage this standard documents — and conflates "serving but
degraded" with "down". Callers that genuinely want gate semantics ask for
them: `?strict=1` → 503.

The check is the same primitive `doctor` uses, never a doctor sweep: that one
shells out to system CLIs, probes podman and HTTP-calls every app's
`contributes.doctor` route — hundreds of milliseconds to seconds, on an
endpoint monitors poll every few seconds.

**Breaking change:** any script doing `status == "ok"` will start alarming on
a degraded workspace. That alarm is the feature.

## 6. Operator guarantee

What can be promised today, in one sentence:

> A restart of core or of the workspace container brings back every Tier-1
> service whose activation runs, within seconds of any later death; and a
> service that is *not* running while its config says it should be is named
> in three places a human or an agent actually looks — `doctor`,
> `/api/health`, and the service's own `state` — with the one somebody
> switched off told apart from the one that is broken. What is **not** yet
> guaranteed is the activation itself: a service whose `activate()` is
> skipped on boot still stays down, and the activation audit that would
> close that is deferred (§1).

The second half of that sentence is the remaining work, tracked on
`reliability:core-activate-skips-on-boot-and-no-service-restart-loop`.

## 7. Where this is referenced from

- `src/apps/lifecycle.py` — module docstring, the seam that owns provision
  vs attach.
- `src/apps/services.py` — module docstring.
- `native-skills/aw-workspace/SKILL.md` — so an agent reaching for
  `restart` reads it first.
