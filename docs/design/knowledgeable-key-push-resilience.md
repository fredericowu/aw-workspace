# aw-knowledgeable key push: manual lever, observability, supervision tier, and the deploy window

**Status:** design — approved for implementation planning, not yet built.
**Card:** `resilience:knowledgeable-key-push-loop-unrecoverable` (3f75bf3b-9510-81c0).
**Evidence:** Frederico's measured timeline of 2026-10-12 (23:36 key present →
~23:30 core restart → 00:14 deploy wipes production's in-memory key → 00:40
still `no_apmt_key` after five push intervals → 05:32 second core restart
re-arms it) — not re-derived here.
**Author:** architect, 2026-10-12.
**Sibling design this composes with, and does not duplicate:**
`docs/design/service-supervision-activation-audit.md` (the core card,
`reliability:core-activate-skips-on-boot-and-no-service-restart-loop`). Its
§A.2 activation audit is the fix for the *cause* of this incident; everything
below is the fix for this app's *unrecoverability and invisibility* once any
cause kills the loops.

## 0. The failure, restated against the code that produced it

The push-not-pull architecture is sound and is not revisited here: production
aw-knowledgeable deliberately holds no `AW_WORKSPACE_HOST_TOKEN`, so the
Tier-1 in-workspace connector reads three narrow vault secrets and re-asserts
them into production over `X-Internal-Secret`
(`repos/aw-app-knowledgeable/knowledgeable_app/playground_key_push.py` and its
twins `ingest_key_push.py`, `label_key_push.py`; received by
`POST /api/{playground,ingest,topics}/key` in `repos/aw-knowledgeable`).
Production holds each value in a per-process in-memory slot
(`backend/app/api/playground.py:69`, `ingest/extraction.py`,
`topics/label.py`) — wiped by every container recreation, by design.

Four properties compound into the observed unrecoverable state:

1. The three loops are started **only** from `plugin.activate()` as bare
   tasks: `asyncio.create_task(…run_forever())`
   (`knowledgeable_app/plugin.py:45,51,57`).
2. `activate()` can **silently skip on boot** — the known, still-open core
   bug, mechanism established in the sibling design §0.1. That is the cause
   of this incident; its fix (the §A.2 activation audit) is that card's, not
   this one's.
3. The app is Tier-1 in-process: `aw-workspace-cli restart knowledgeable` →
   "no component matching" — there is no restart unit smaller than core.
4. `push_once()` is reachable **only from the loop** — no CLI, no route, no
   manual trigger of any kind.

And nothing reports it: `/api/health` consumes core `degradations()`
(`src/apps/degradations.py`), which knows services, containers and autostart —
not an app's internal asyncio tasks. `doctor`'s app self-checks exist
(`contributes.doctor`, `src/apps/manifest.py:159`) but this app declares none.
The only signal is a per-request `clamped` note on `knowledge_search`
responses.

Two latent adjacent gaps found while reading, fixed by the same design:

- `run_forever()` has **no try/except around `push_once()`**
  (`playground_key_push.py:148-155`). `push_once` promises never to raise,
  but a promise is not a supervisor: any unexpected exception (httpx API
  drift, a bug in `_read_from_vault`) kills the task silently, forever, with
  the app otherwise healthy.
- At `AW_WORKSPACE_WORKERS>1`, every core worker that loads the app runs its
  own copy of all three loops — N×3 duplicate vault reads and pushes every
  300s. Harmless but wasteful, and it masks single-task death.

## 1. Decision — the manual re-push lever: a self-contained CLI command, not a route

**`aw-workspace-cli knowledgeable-keys push [--only playground|ingest|label]`**
and **`… status`**, shipped as `commands/knowledgeable_keys.py` in the app
repo (the `commands:install` discovery this app already uses for
`knowledgeable-ingest`).

The existing `knowledgeable-ingest` command is a thin client over the app's
own routes via `src.cli.local_client` — and that pattern is **exactly wrong
here**, because app routes are registered inside `activate()`
(`plugin.py:33`): in the one failure mode this lever exists for, the routes
are as dead as the loop. The lever must share no fate with the app's load
state. The command process therefore assembles everything itself:

- **Vault access:** `workspace_env("AW_WORKSPACE_API_URL"/"AW_WORKSPACE_API_KEY")`
  (`knowledgeable_app/workspace_env.py:17`) reads `os.environ` or
  `<AW_WORKSPACE_HOME>/.env` — both readable from any process in the
  container, CLI included.
- **Production base_url:** `GET /api/apps/knowledgeable/config` via
  `local_client` — a **core** route, alive whenever the server is, regardless
  of whether the app loaded; falls back to `client.DEFAULT_BASE_URL`.
- **`X-Internal-Secret`:** decrypted directly from the app's own secret-store
  file (`<home>/secrets/knowledgeable.json`) via
  `src.apps.secret_store.SecretStore` — same uid, same container, files 0600;
  no new exposure surface. (App command modules already import core modules;
  `knowledgeable_ingest.py` imports `src.cli.local_client`.)
- Then `mcp.client.configure(...)` with those two resolvers and
  `asyncio.run()` of the three existing `push_once()` functions — **the same
  code path the loop uses**, so the lever cannot drift from the loop. Prints
  one line per credential, exits non-zero if any push failed, never echoes a
  value.

`status` calls the three new production `GET /key/status` endpoints (§2)
directly — a working status reading even when the app is not loaded.

**Rejected:**

- *App route + thin CLI client (the `knowledgeable-ingest` pattern).* Dies
  with `activate()` — unavailable precisely when needed. A route additionally
  duplicates a trigger surface agents don't need (they have Bash in this
  container) and humans reach through the CLI anyway.
- *A core-level "re-run activate for app X" lever.* The genuinely general
  fix for "Tier-1 has no restart path", but it is core surgery on the
  load/unload path and belongs with the core card — building it here would
  absorb that card's scope silently. Runner-up; if core later ships
  `aw-workspace-cli apps reload <slug>`, this CLI lever remains useful (it is
  also the deploy-window tool) and loses nothing.
- *Documented curl incantations.* Hand-handling the secret per incident, and
  drift from `push_once` the first time the payload changes.

## 2. Decision — observability: production answers, the connector aggregates, doctor and health consume existing primitives

**Vocabulary rule first:** the words are `paused_reason()`'s existing ones —
`no_apmt_key`, `secret_unavailable` (`api/playground.py:159`), plus
extraction's richer set (`ingest/extraction.py:391`). They are surfaced
verbatim at every layer below; no new words are invented.

**(a) Production — three sibling `GET /key/status` endpoints**, one beside
each push endpoint (`api/playground.py`, `api/ingest.py`, `api/topics.py`),
`Depends(require_service)` like their POST twins, each returning
`{"configured": bool, "paused_reason": str|null}`. Three endpoints, not one
combined, mirroring the deliberate three-way split of the push endpoints
(each credential revocable/rotatable independently — the reasoning
`api/ingest.py::push_key`'s docstring carries). These double as decision 4's
reactive trigger.

**(b) Connector — one aggregating app route + `contributes.doctor`.**
`GET /api/apps/knowledgeable/keys/status` answers `{"ok": bool, ...detail}`
and is declared in the manifest:
`"doctor": [{"label": "ap-mt key push", "route": "/keys/status"}]`.
`ok=false` when **any** of:

- a production slot reports `configured=false` or a non-null `paused_reason`
  (word passed through verbatim);
- any push task's `last_success_at` is older than **2× its assert interval**
  — the staleness clause is the one that would have caught this incident
  *before* the damage: from 23:30 (loop dead) to 00:14 (deploy wipes key) the
  key was still present and a presence-only check reads green; the loop's
  silence was the only early signal;
- the last status poll itself failed (production unreachable ≠ keys fine).

`doctor` already renders app self-checks and exits non-zero on a failing one
(`src/cli/commands/doctor.py:102-128`) — no core change needed for the doctor
half.

**(c) `/api/health` — via one small, generic core seam, not via app-specific
wiring.** Health consumes `degradations()` only, and the approved core design
§C explicitly rejected doctor-style per-app fan-out on the monitor-polled
path; wiring *this app's* key state into core health would invert the
framework. The honest route that still meets the bar: `degradations()` gains
a generic row kind — **a registered watchdog task in failure/backoff for ≥2
consecutive failures** → `{"kind": "watchdog", "app", "task",
"consecutive_failures"}`. Core reading its own `WatchdogSupervisor` state
(`src/apps/watchdog.py` already tracks backoff per task) is in-process, zero
I/O, consistent with §C's cost rules, and serves every app's watchdog tasks,
not just this one's. The connector's **sensor task** (§3) raises when keys
are absent/stale, which is what lights that row. This is the one core-side
piece of this design; it is ~20 lines and may be folded into the core card's
implementation instead of shipped separately — either owner is fine, the seam
is the decision.

**Stated dependency, not a gap being hidden:** when the app is not loaded at
all, `_app_doctor_checks` iterates `runtime.loaded_slugs()`
(`src/apps/routes.py:222`) and asks nothing, and no watchdog tasks exist to
be degraded. That case — the incident's actual cause — is covered by the core
card's §A.2 audit (**I1: every desired app is loaded**, violation → errors +
doctor + health). This card's surfaces cover every failure *inside* a loaded
app; composed with the approved core design, the bar "the workspace cannot
report healthy while this app's LLM half is dead" is met. Alone, it cannot
be, and pretending otherwise would be re-absorbing the core card.

## 3. Decision — supervision: `ctx.watchdog` (the tier's existing primitive), not `ServiceSupervisor` — and supervision is not the fix for the incident

**Say the honest part first:** neither supervisor fixes what happened on
2026-10-12. The loops did not crash — they were **never started**, because
`activate()` silently skipped. Registration with any supervisor also happens
inside `activate()`, so a supervised task is exactly as unborn as a bare one.
The fix for the cause is the core card's activation audit, full stop. What
supervision fixes here is the *adjacent* real gaps named in §0: silent task
death on an unexpected exception, N×3 duplication across workers, and zero
introspection.

**`ServiceSupervisor` is the wrong shape** — it supervises `Popen`
subprocesses (`src/apps/services.py`): leases, stdout pumps, exit codes.
Promoting three HTTP-POST loops into a subprocess would mean re-plumbing
config, vault env and the X-Internal-Secret into a child process — building a
second copy of §1's CLI as a daemon — to gain a restart loop the in-process
tier already has elsewhere.

**The tier already has its own primitive:** `WatchdogSupervisor` via
`ctx.watchdog.register` (`src/apps/base.py:428`, capability `watchdog:tasks`,
risk *low* in `src/apps/capabilities.py:33`). It gives exception backoff
(`min(interval·2ⁿ, 1800s)`, reset on success), cross-worker leader gating via
the flock lease (exactly one worker runs the tasks; handoff re-fires
`run_immediately=True` tasks), cancel-on-unload, and the
`GET /api/apps/-/watchdog` introspection endpoint.
aw-app-agents-platform-runners already runs this exact idiom in production —
`runner-registration-reassert`
(`apps/agents-platform-runners/agents_platform_runners_app/plugin.py:577`) is
a periodic re-assertion task, same shape as these three.

**Where it lands:** `plugin.py:45-57` — replace the three
`asyncio.create_task(…run_forever())` with **four** watchdog registrations
(`run_forever()` and the manual `deactivate()` cancels at `plugin.py:72-78`
are deleted; the runtime's `cancel_all_for` owns teardown):

- three **actuator** tasks (`key-push-playground|ingest|label`, interval 30s,
  `run_immediately=True`): tick logic in §4. Actuators **never raise** on
  business outcomes — a raise would engage backoff and slow the exact retries
  that should be fast.
- one **sensor** task (`keys-health`, interval 60s): pure check of the three
  production statuses + actuator `last_success_at` staleness; **raises** on
  any `ok=false` condition from §2b. Backoff on a sensor is harmless (the
  degraded state persists between probes), and its failure streak is what
  reaches `/api/health` through §2c. One module-level state dict per
  credential (`last_success_at`, `last_status`) is shared by the actuators,
  the sensor, and the `/keys/status` route.

Manifest change: `permissions` += `"watchdog:tasks"`.

**Rejected:** `ServiceSupervisor` (shape mismatch above); a new bespoke
supervision primitive for Tier-1 asyncio tasks (it would be `WatchdogSupervisor`
re-written — the primitive exists, use it); "fix the activate() bug instead
of supervising" as a *reason to change nothing here* — the bug fix is
necessary and separately owned, but it does not give this app a recovery
lever, observability, or exception-safety, and those stand on their own.

## 4. Decision — keep push, keep the 300s unconditional re-assert, add a 30s reactive poll; reject the backend-initiated boot callback

Actuator tick (every 30s): `GET` the credential's production `/key/status`
(§2a — cheap, no vault involved). Push (`push_once()`: vault read + POST)
when **either** `configured=false` **or** ≥300s since the last successful
assert. Effects:

- **Deploy window: ≤300s → ≤~30s.** Today every production deploy guarantees
  up to 5 minutes of dead synthesis; tonight proved the window is unbounded
  when the loop is dead. 30s is below the time a human takes to check the
  deploy worked.
- **Vault read cadence is unchanged** (~300s per credential). The status poll
  never touches the vault, so no audit-log noise and no new exposure to the
  approval gate if the `auto_approve_for` entry is ever dropped — in that
  regression, prompts stay at today's rate instead of 6×.
- **The unconditional 300s re-assert survives because rotation is invisible
  to a presence check:** production reporting `configured=true` says nothing
  about *which* value it holds; after a vault rotation only the unconditional
  push converges production to the new key. A pure push-on-missing design
  silently breaks rotation.

**Rejected:**

- *Backend-initiated push-request at boot (the card's inversion).* Three
  independent reasons. (1) It inverts the deliberately one-way coupling: the
  design's virtue is that production knows nothing about the workspace; a
  callback needs the workspace's URL and a workspace-side route in
  production's config. (2) The callback target would be a connector route
  registered in `activate()` — dead in exactly the activate-skip mode, so it
  cannot be the recovery mechanism, only an optimization. (3) As an
  optimization it buys ~30s over the poll, for a new cross-service contract.
  Not worth it at this estate's deploy cadence.
- *Shortening `PUSH_INTERVAL_S` to ~30s.* 3 vault reads per 30s ≈ 8,640
  audited reads/day of values that change approximately never — audit noise
  that buries real reads, and a 6× dependency amplification on aw-app-secrets.
- *Status quo (pure 300s).* Tonight's measurement: a guaranteed dead window
  per deploy even when everything is healthy, unbounded when it isn't.

## Where it lands — full file list

| Repo | File | Change |
|---|---|---|
| aw-app-knowledgeable | `knowledgeable_app/plugin.py:45-57,72-78` | three `create_task` → four `ctx.watchdog.register`; drop manual cancels |
| aw-app-knowledgeable | `{playground,ingest,label}_key_push.py` | keep `push_once`; add shared per-credential state (`last_success_at`, `last_status`) + the 30s tick fn; delete `run_forever` |
| aw-app-knowledgeable | `knowledgeable_app/mcp/client.py` | add `get_playground/ingest/label_key_status()` GETs (same `_headers()` channel) |
| aw-app-knowledgeable | `knowledgeable_app/routes.py` | add `GET /keys/status` aggregator |
| aw-app-knowledgeable | `aw-app.json` | `permissions` += `watchdog:tasks`; `contributes.doctor` += the keys check |
| aw-app-knowledgeable | `commands/knowledgeable_keys.py` (new) | the §1 self-contained lever |
| aw-app-knowledgeable | `tests/` | per existing `test_*_key_push.py` style: tick logic (push-on-missing, push-on-stale, no-push otherwise), sensor raise conditions, CLI assembly with a fake secret store |
| aw-knowledgeable | `backend/app/api/{playground,ingest,topics}.py` | sibling `GET /key/status` (`require_service`) each |
| aw-knowledgeable | `backend/tests/` | status endpoints reflect `set_pushed_key`/`paused_reason` transitions |
| aw-workspace (core, foldable into the core card) | `src/apps/degradations.py` | generic "watchdog task failing ≥2" rows (§2c) |

## What this makes harder later

- **Production must stay single-worker, and now it's a load-bearing
  constraint.** The key slots are per-process module globals; the Dockerfile
  runs plain `uvicorn … --port 8090` (`Dockerfile:114`, no `--workers`).
  Adding workers would make each push seed one random worker — a silent
  partial outage with `configured=true` answers from the seeded one. The
  constraint must be written at the slot definitions when `/key/status`
  lands; this estate's WORKERS=10 history says it will otherwise be
  rediscovered the hard way.
- **Pushes now run on exactly one core worker** (watchdog leader) instead of
  redundantly on all. The flock handoff covers leader death, but a wedged
  (not dead) leader process pauses all re-assertion until handoff — the
  sensor's staleness alarm is the compensating control.
- **Permission bump** (`watchdog:tasks`) rides the signed-trust update flow —
  the three-clock re-approval dance applies on release.
- **CLI-decrypts-app-secret precedent.** §1 sanctions a CLI process reading
  an app's *own* secret-store file directly. Acceptable (same trust domain);
  it must not quietly generalize into cross-app reads — the namespacing-by-
  slug rule in `secret_store.py`'s docstring is the line.
- **A standing 30s poll against production** — trivial load, but it changes
  the "quiet logs mean nothing is happening" baseline on aw-knowledgeable.

## Risks for the coders (non-obvious breakage)

- **Do not merge sensor and actuator into one task.** The watchdog backs off
  a raising task; an actuator that raises on "key missing" retries *slower*
  during the exact window it exists to close. Actuators swallow and record;
  only the sensor raises.
- **`set_pushed_key`'s breaker contract is frequency-sensitive prose.**
  `ingest/extraction.py:108-142` deliberately does not close the circuit
  breaker on a push and reasons explicitly about the ~300s cadence vs the
  240s cooldown. A reactive push on `configured=false` does not change those
  semantics (same endpoint, same function), but a coder "simplifying" the
  breaker veto while in there would reopen the 2026-10-01 null-blindness —
  re-read that docstring before touching anything nearby.
- **Leader handoff re-fires `run_immediately=True` tasks** (see
  `tests/integration/apps/test_watchdog.py`) — an extra push per handoff.
  Idempotent by design; do not "fix" it by setting `run_immediately=False`,
  which would reintroduce a full-interval boot gap.
- **The CLI lever must keep working with the app unloaded** — that is its
  acceptance test, literally: uninstall nothing, simulate the skip by never
  loading the app in a test harness, run the command, assert the pushes land.
  If a coder reaches for `local_client` + app routes out of habit
  (`knowledgeable-ingest` is right there as a template), the lever silently
  becomes decoration.
- **`workspace_env` fallback reads `<home>/.env`** — written at boot, 0600.
  A CLI run as a different user (root via `podman exec` without `-u`) gets
  empty strings and a confusing "vault not published" message; the command
  should detect euid≠expected and say so (the `ss`-as-root class of bug).
- **Vault-read failure in the CLI must print the `secret_unavailable`-style
  reason, not stacktrace** — the lever is an incident tool; its output is
  read at 00:40 by someone annoyed.
