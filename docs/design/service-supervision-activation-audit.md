# Service supervision: activation audit, restart loop, health surface, and the core-restart contract

**Status:** design — approved for implementation planning, not yet built.
**Card:** `reliability:core-activate-skips-on-boot-and-no-service-restart-loop` (3f65bf3b-9510-8171).
**Evidence:** debugger run `3aa454de242c42bfa862ef33b3444236` on card
`reliability:proxy-server-dies-silently-killing-all-browser-mcp` (2026-10-11) — not re-derived here.
**Author:** architect, 2026-10-11.

## 0. The incident, restated against the code that produced it

A clean core restart (`ExitCode=0`, 00:28:50 UTC) killed `proxy:proxy-server`
along with every other Tier-1 managed service — they are `subprocess.Popen`
children of the core process (`src/apps/services.py:105`). On the next boot,
`proxy`'s service never came back, with **no log line and no error anywhere**.
All four browser-automation MCP families were down for ≥27 minutes; the only
component that could name the cause was `aw-workspace-cli doctor`, which
nothing runs proactively. The 900s convergence reconcile passed over the dead
service at 00:45:21 without noticing, because it diffs *packages*, never
*processes*.

Mechanics as they exist today, with paths:

- An app's `activate()` registers its service via `ServicesFacade.register`
  (`src/apps/base.py:481-503`), which gates the actual spawn on
  `autostart and ctx.provision` — `provision` is **a per-boot one-shot** held
  by the single boot-reconcile leader (`src/api/app.py:234-245`,
  `src/apps/boot_reconcile_coord.py`).
- `ServiceSupervisor` (`src/apps/services.py:65`) spawns the `Popen`, pumps
  its stdout in a reader thread, and when the process exits, `_pump`'s
  `finally` (`services.py:113-144`) logs a WARNING, releases the cross-worker
  lease, and **forgets the process forever**. There is no restart loop and no
  health check anywhere in the module.
- The reconcile pass (`Reconciler._reconcile_provisioned`,
  `src/apps/reconciler.py:1184`) buckets apps into
  `installed`/`upgraded`/`removed`/`errors`. Nothing else is accounted for.
- Follower workers and post-boot convergence load apps with
  `provision=False` (`Reconciler.attach`, `reconciler.py:472-517`), which
  registers services with autostart **suppressed by design**
  (`base.py:498`). Correct for duplicate-prevention; it also means the boot
  leader's one-shot is the *only* code path in the entire system that ever
  starts an autostart service. Miss it once, and no later pass — converge,
  attach, 900s reconcile — will ever start it.

### 0.1 Why `activate()` could skip with no log line (question A's "mechanism")

The reconciler treats `slug in runtime._apps` (`AppRuntime.is_loaded`,
`src/apps/runtime.py:844`) as proof of a completed activation, and **every
skip path keyed on it is silent by design**:

1. `reconciler.py:1275` — an app partitioned as `present` with no
   version/trust change is touched by *nothing* in the pass and appears in
   *no* summary bucket. Zero log lines.
2. `reconciler.py:947-954` — the per-app-lock re-check returns
   `{"skipped": "already-loaded"}`; the caller (`install_one`,
   `reconciler.py:1465`) silently reroutes it to the `present` side. Zero
   log lines.
3. `reconciler.py:858` — a dependency that is already loaded is a bare
   `continue`. Zero log lines.

These paths are *normal* for `proxy` specifically: `browser` and
`mini-browser` both declare `proxy` in `dependencies.apps`
(`apps/browser/aw-app.json`, `apps/mini-browser/aw-app.json`), so at boot
proxy's real activation usually happens *inside a peer's install* via
`_install_dependencies` (`reconciler.py:845-868`), and proxy's own install
slot then takes silent path 2. Three concurrent roots race for it
(`_RECONCILE_CONCURRENCY` = 3, `reconciler.py:85`).

The debugger established that in the incident boot, no activation evidence
for proxy exists in SigNoz at all — no register, no start, no "loaded"
(`runtime.py:1239`), no error — while the pass's summary shows proxy desired,
skipped, 0.0s, no error: i.e. `is_loaded('proxy')` answered True to someone
without a logged activation. The precise first cause is **not reconstructable
from surviving evidence**, and that is the finding: between three legitimate
silent skip paths, a per-boot one-shot start gate, and a log pipeline with a
known worker-level drop mode (see memory/skill
`aw-autoskill-uvicorn-worker-silent-log-drop`), the system cannot answer
"did activate() run for X this boot, and did X's services start?" from its
own records. Any fix that merely patches the one path that fired would leave
the class intact.

## A. Making a silent activate-skip structurally impossible

**Decision: stop trusting control flow; assert the outcome.** Two parts, the
second is the structural one.

### A.1 Total accounting in the reconcile summary (hygiene)

Every app in `desired_active` must land in **exactly one** explicit
disposition in the pass summary: `installed`, `upgraded`,
`present_unchanged`, `skipped_already_loaded` (with who loaded it, e.g.
`dependency-of: browser`), or `errors`. The pass ends with an assertion that
the union equals the desired set; a violation is itself an `errors` entry.
Paths 1–3 above each gain one INFO line. This makes the *next* incident
readable; it does not prevent it — log lines can be dropped and new skip
paths will be added.

### A.2 The activation audit (structural)

A post-pass **end-state assertion**, independent of which code path ran:

- **I1 — every desired app is loaded.** For each app in `desired_active`:
  `runtime.is_loaded(app_id)` must be true. Violation → `errors` entry +
  one repair attempt (`install`, which cleans residue via the journaled
  unload path, `reconciler.py:1034-1044`).
- **I2 — every autostart service is accounted for.** For each registered
  `(app_id, service_id)` with `autostart=True`
  (`ServiceSupervisor.registered()` + the per-service flag): the service is
  running locally (`proc.poll() is None`), OR running in another worker
  (`is_lease_held_anywhere`, `src/apps/service_lease.py`), OR in an explicit
  exempt state (`gave_up` marker or desired=stopped, §B). Anything else →
  start it through `ServiceSupervisor.start()` (lease-arbitrated, so a race
  with another worker resolves to a no-op), log at WARNING, and record it in
  a new `audit_repairs` summary bucket.

The audit runs (a) at the end of every reconcile pass (boot and 900s), and
(b) as its own watchdog task at ~60s cadence (leader-gated like every other
`WatchdogSupervisor` task — `src/api/app.py:347-380`), skipped while a
provisioning pass is in flight (a service mid-upgrade is legitimately down).
It emits **one line every run, including on zero findings**
("activation audit: N apps, M services, 0 repairs") and stamps a
last-run timestamp (in-process + Redis) that `doctor` checks for staleness —
so the audit itself cannot rot silently the way the doctor-watch task did
(disabled 2026-09-09, noticed 2026-10-11).

Why this is structural rather than another patch: a silent skip is only
dangerous because nothing downstream checks the invariant the skip broke.
The audit checks the invariant *on the world* — process table and lease
state — so any skip path, present or future, converges to either a running
service or a named error within one audit interval. The question "did
activate run?" stops mattering; "is the desired end-state true?" is asked
explicitly, forever.

This also closes the two stale `need_human` backlog cards the debugger
linked (`aw-workspace-multiworker:proxy-app-auto-start-not-running`,
`degraded:proxy-app-auto-start-not-running`).

## B. Where process health lives

**Decision: the restart loop belongs in `ServiceSupervisor`, event-driven,
with backoff and give-up. The convergence reconcile does not get a health
loop; it gets the §A audit — an assertion that may call
`ServiceSupervisor.start()` but owns no restart policy.**

### B.1 Why the reconcile is the wrong home for process health

- **Cadence.** 900s is a ≥15-minute MTTR floor; this outage ran ~27 minutes
  *with* a pass landing mid-outage. The supervisor's reader thread observes
  the child's death at the moment stdout closes (`services.py:117-122`) —
  the detection already exists, free, at zero polling cost; the reconcile
  would re-derive it 15 minutes late.
- **Wrong worker.** The convergence tick runs in the watchdog-leader worker
  (`routes.py` `_tick` → `runtime.watchdog.register`), which is not
  necessarily the worker that owns the `Popen` — a known structural fact
  (`base.py:490-495`, `service_lease.py`). It can see leases, not
  processes; restart policy keyed on lease staleness from a non-owner races
  the owner's exact knowledge.
- **Tick-skipping under load.** `_tick` has a reentrancy guard that skips
  the interval when the previous pass is still running — exactly the
  degraded conditions under which services die are the conditions under
  which a reconcile-hosted health check would not run.
- **Coupling.** The reconcile is declarative package convergence behind a
  provisioning flock with 450s+ cold passes. "Is this pid alive" must not
  be gated behind that machinery.

The inverse — supervisor-only, no reconcile involvement — is also wrong, and
this is why the §A audit exists: the supervisor's monitor dies with its
worker. A uvicorn worker respawn leaves the service orphaned (§D) and the
new worker's supervisor re-registered with autostart suppressed
(`attach`/`converge` → `provision=False`); without the audit, *nobody* ever
starts it again. Supervisor = fast recovery while the owner lives; audit =
cross-worker backstop for "owner died" and "never started". One restart
policy, two triggers.

### B.2 Restart loop contract

New per-service state on `_Service` (`services.py:34`):

- `desired`: `running | stopped`. `stop()` and `stop_all_for()` set
  `stopped` **before** signalling (`services.py:195`); `start()` sets
  `running`. This is the correctness keystone: a manual stop, an uninstall
  and an upgrade's stop-half must never trigger a respawn.
- `state`: `running | backing_off | gave_up | stopped` — surfaced in
  `status()` (`services.py:217`), doctor, and `/api/health`.
- Failure bookkeeping: `consecutive_fast_exits`, `last_spawn_monotonic`.

On `_pump` exit (the `finally` at `services.py:121-144`), after the existing
lease release: if `desired == running`, schedule a respawn after
`min(1s × 2^n, 60s)` where `n = consecutive_fast_exits`. A run that
survived ≥60s resets `n` to 0; a run that died in <60s increments it. At
`n == 5` (five consecutive startup-window crashes, ~2 minutes of wall
clock): **give up** — `state = gave_up`, one ERROR log with the exit code
and the last log-tail (`svc.log_lines` already captures it), and a durable
Redis marker `svc:gaveup:{app_id}:{service_id}` (reason, timestamp, exit
code) so every worker, doctor, and `/api/health` can see it. No further
spawns. An unconditional respawn of a crash-on-startup service is a new
failure mode — port flapping, CPU burn, log spam — not a fix.

Give-up is cleared by: manual `start()`/`restart()` (incl. the forwarded
`service_relay` path), app update/reinstall, and app config save. The §A
audit **must respect** `gave_up` and `desired == stopped` — it reports them
as degraded but never starts them; otherwise the 900s pass becomes the
unconditional respawner through the back door.

Respawn goes through the existing `start()` unchanged, so lease acquisition
still arbitrates cross-worker duplicates — a respawn that loses the lease is
the existing logged no-op (`services.py:95-100`), correct by construction.

## C. `/api/health`

**Decision: `/api/health` (`src/api/app.py:499-508`) stays HTTP 200 and
stays cheap, but stops lying.** It gains:

```json
{"status": "degraded", "degraded": [
  {"kind": "service", "app": "proxy", "service": "proxy-server",
   "state": "off", "autostart": true}]}
```

`status` is `"ok"` only when `degraded` is empty.

- **Shared primitive, not shared command.** Extract the check doctor
  already trusts — `_autostart_not_running` (`src/apps/routes.py:99`,
  rendered by `src/cli/commands/doctor.py:151`) — into a runtime-level
  `degradations()` that both doctor's report and `/api/health` consume. One
  source of truth; doctor remains the superset (CLI presence, container
  state, app self-check routes).
- **What consuming doctor wholesale would cost on the hot path, and why
  it's rejected:** the full doctor sweep shells out to system CLIs, probes
  podman, and HTTP-calls every app's `contributes.doctor` route — hundreds
  of milliseconds to seconds, with podman and per-app fan-out. `/api/health`
  is hit by uptime monitors and container healthchecks every few seconds;
  doctor-inline turns monitoring into load and makes health itself a
  denial-of-service surface. The chosen check costs: one `poll()` per
  registered service (a `waitpid` syscall, microseconds), one Redis `MGET`
  over lease keys, one over give-up markers (~1–2ms total), cached
  in-process with a ~5s TTL — amortized ≈ zero. Redis unreachable →
  fail-open to the local-only view plus `"health_scope": "local-worker"`;
  health must never 500 because its own dependency blinked.
- **HTTP 200 on degraded, deliberately.** A 5xx would make any container
  orchestrator healthcheck restart core — the exact restart that caused
  this outage — and would conflate "serving but degraded" with "down".
  Callers that want gate semantics get `?strict=1` → 503.

This resolves the tracked finding
`resilience:api-health-hardcoded-ok-presence-check`.

## D. The core-restart blast-radius contract

Nobody had written down what a core restart does to supervised subprocesses;
the reasonable assumption "restart is safe" is currently false. The contract
below ships as **`docs/standards/core-restart-blast-radius.md`**
(a standard — it is a promise, not a proposal), cross-referenced from
`src/apps/lifecycle.py`'s module docstring and the `aw-workspace` skill.
Draft text:

1. **Workspace-container restart** (what happened 2026-10-11): kills the
   core process, every Tier-1 managed service (`Popen` children), and —
   because podman runs *inside* this container — every Tier-2 app
   container. Recovery owner: the boot-reconcile leader's `activate()` pass
   plus the §A activation audit. Guarantee (once §A/§B are built): every
   `autostart` service is running again by the end of the boot pass + first
   audit, **or** it is a named entry in the reconcile summary's `errors`/
   `audit_repairs`, in `doctor`, and in `/api/health`. Until then the honest
   contract is: *a core restart permanently stops any Tier-1 service whose
   activation is missed, and nothing will notice* — and the standard says
   so, dated.
2. **Core-process-only restart / uvicorn worker respawn:** Tier-1 services
   are spawned with `start_new_session=True` (`services.py:109`), so they
   are **not** killed with their worker — they orphan: process alive, port
   bound, lease heartbeat dead → lease expires → fleet-wide `status()`
   reads "off" while the process lives. A naive `start()` then collides on
   the port (the masked `OSError: Address already in use` class named in
   `src/apps/service_lease.py`'s docstring). Contract: `start()` (and
   therefore the audit's repair path) must first reap a verified orphan —
   pid recorded in the lease payload, cmdline-verified before SIGTERM — or
   treat an immediate bind-failure exit as give-up with
   `reason=orphan-suspected` instead of entering the backoff loop.
3. **Tier-2 containers** survive a core-process restart (podman outlives
   the worker) and are reconciled by `ContainerSupervisor` on the next
   pass; they die with the workspace container (case 1).
4. **Operator guarantee** (post-implementation): "`restart` of core or of
   the workspace container is safe: supervised state converges within one
   boot pass + one audit interval, or the gap is named in three places a
   human or agent actually looks."

## Rejected alternatives

1. **Process health as a new live-state dimension *inside* the convergence
   reconcile (as the primary mechanism)** — the card's option B-alt.
   Rejected for the four reasons in §B.1 (15-min MTTR floor, wrong worker
   for `Popen` visibility, tick-skipping under exactly the conditions that
   kill services, coupling an imperative respawn to a 450s flock-gated
   declarative pass). Kept only as the §A assertion/backstop with no policy
   of its own.
2. **Patching log lines onto each silent skip path and calling it fixed.**
   Necessary hygiene (§A.1), insufficient alone: the skip-path set is open
   (three exist today; the next refactor adds a fourth), and the incident
   itself demonstrates the log pipeline can lose the evidence. Assert
   end-state, don't narrate control flow.
3. **`/api/health` consuming doctor wholesale.** Cost analysis in §C;
   seconds of podman/CLI/HTTP fan-out on a monitor-polled endpoint.
4. **Re-platform Tier-1 services onto podman/systemd units with native
   restart policies.** The runner-up. It buys a battle-tested supervisor
   for free, but converts every cheap in-process subprocess into a
   container/unit: image or unit-file authoring, mount plumbing, a
   capability-model change, and the death of Tier-1's whole reason to exist
   (zero-infra services). Not justified against a ~200-line supervisor
   change; revisit if managed services multiply or need resource limits.
5. **A standalone 60s WatchdogSupervisor liveness poller as the primary**
   (no supervisor changes). Same wrong-worker blindness as the reconcile,
   just cheaper; still leaves a 60s MTTR where the reader thread gives ~0s,
   and still has no backoff state to consult. The audit *does* use this
   cadence — but as backstop over lease state, with policy living in the
   supervisor.

## What this makes harder later

- **Desired-state now lives in three places:** the app's `auto_start`
  config, the supervisor's `desired` flag, and the Redis give-up marker.
  The open `$framework.autostart` design (ADR
  `app-launcher-live-preview-autostart.md`, card
  `feature:app-autostart-framework-setting`) **must** integrate with the
  audit when it lands, or the audit will resurrect apps a user deliberately
  stopped. Rule to carry over: the audit treats config-level
  `auto_start=false` (doctor's existing source) and any future
  `$framework.autostart=false` as `desired=stopped`.
- **`kill -9` as a debugging tool stops working.** Today a manually killed
  service stays dead; after this, the supervisor or the audit resurrects
  it within seconds to a minute. Deliberate downtime must go through
  `stop()` (CLI `aw-workspace-cli stop <app>`). This is the point, but it
  changes operator muscle memory and must be in the standard.
- **A new (soft) Redis dependency on the health path.** Degrades to a
  local-worker view when Redis is away — health can under-report services
  owned by other workers. Accepted; fail-open is mandatory.
- **`status != "ok"` is a behavior change for `/api/health` consumers.**
  Any script string-matching `"ok"` will start alarming on a degraded
  workspace. Announce it; that alarm is the feature.
- **Backoff counters are per-worker memory.** A worker respawn mid-backoff
  resets the count (the give-up marker is the durable half). A
  crash-looping service whose owning worker also crash-loops could in
  theory never reach give-up; acceptable because worker death re-routes
  recovery through the audit, which starts fresh and re-arms backoff.
- **The §D contract commits `start()` to orphan-reaping semantics** — any
  future re-platforming of services (alternative 4) must preserve that
  promise or re-issue the standard.

## Risks for the coders (non-obvious breakage)

- **`ServiceSupervisor` has no internal locking today** and is deliberately
  synchronous (`service_lease.py` docstring explains why). Adding respawn
  timers introduces concurrency the module never had: use
  `threading.Timer`/plain threads plus a per-service lock — do not hold any
  lock across `Popen()` (fork+exec), and do not touch asyncio from `_pump`'s
  reader thread.
- **The lease-identity dance in `_pump` is load-bearing** — it releases the
  lease *captured at spawn time*, not `svc.lease` at exit time
  (`services.py:129-144`), to survive back-to-back restarts. The respawn
  path must follow the same identity discipline; re-read that comment and
  `src/tests/integration/apps/test_services.py` (and the lease tests for
  non-owning-worker restarts) before touching it.
- **`stop()`-then-respawn race:** `_pump`'s `finally` runs *after* `stop()`
  returns. If `desired=stopped` is set after SIGTERM rather than before,
  the pump observes a stale `running` and respawns the service `stop()`
  just killed. Set the flag first; test exactly this interleaving.
- **Upgrade = uninstall + install** (`reconciler.py:1359-1361`): the stop
  half runs with `desired` correctly set via `stop_all_for`, but the audit
  must not fire between the halves — hence "skip while a provisioning pass
  is in flight" is a correctness requirement, not an optimization.
- **The audit's I1 repair can hit journaled residue** ("container already
  registered", the 2026-08-16 crispal class). Repairs must go through the
  full `install()` path, which already reverts residue on failure — never a
  bare `runtime.load`.
- **Tests that assert the reconcile summary shape will break** on the new
  buckets (`present_unchanged`, `skipped_already_loaded`, `audit_repairs`)
  — `test_doctor_autostart.py` and the reconciler tests encode today's
  shape.
- **Where the existing code lies:** `ServicesFacade.register`'s return
  value claims `"started": bool(autostart and ctx.provision)` — it reports
  the *intent*, not whether `start()` actually spawned (a lease refusal
  still returns `started: true` upstream). Don't build the audit on that
  field; build it on `status()`/`poll()`/leases.

## Verification sketch (for QA, end-state not implementation)

1. `kill -9` a running autostart service's pid → respawned within ~1s by
   the owner worker; after five scripted instant-exit crashes → `gave_up`,
   no sixth spawn, ERROR logged, marker set, doctor + `/api/health` show it.
2. `aw-workspace-cli stop proxy` → no respawn, `desired=stopped`,
   `/api/health` still truthful per §C.
3. Restart the workspace container → every autostart service running after
   boot, and the reconcile summary accounts for **every** desired app in
   exactly one bucket.
4. Delete a service's registration path mid-boot (simulate the skip) → the
   audit's I1/I2 names it in `errors`/`audit_repairs` within one interval —
   the incident's signature (absence) can no longer occur.
