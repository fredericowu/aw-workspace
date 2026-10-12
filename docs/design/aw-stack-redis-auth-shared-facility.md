# aw-stack Redis: ACL auth + shared-facility contract — zero-outage rollout via the verified 2-arg AUTH pre-stage

**Status:** design — decided, not implemented.
**Card:** `security:aw-stack-redis-no-auth-shared-facility`
(3f75bf3b-9510-8164-8575-c3b83b19b432).
**Constraint set by Frederico, verbatim (Telegram, 2026-10-12):** *"Estimate
the cost and time and if this the right way to restructure so we don't have
to do it again. Any component in aw-stack should be able to use redis, no
doubt keep going, set a pass and everyone on the stack can use it. Make sure
you don't break existing redis consumer, restarting is not a problem."* —
doing it is decided; this note is the how, the estimate, and the
"won't redo it" answer.
**Author:** architect, 2026-10-12.
**Amended 2026-10-12** (card `infra:redis-credential-automatic-sync`,
3f75bf3b-9510-81da-9278-c4f0d52dcb94): Phase 2 redesigned from nine manual
env edits to an automatic consumer sync, per Frederico's instruction while
Phase 1 was deploying — *"one thing, since we are rebuilding stuff, let's do
it with the automatic synchronization."* §5 Phase-2 block, §6 Phase 2, §6a,
§7, §8, §10, §11 changed; the decision in §1 and the ACL model in §4 stand.
**Relates to, does not touch:**
`docs/design/knowledgeable-apmt-key-pull-through.md` (0e12767) — the ap-mt
key stays on pull-through; authenticated Redis does not reopen that decision
(§9). The Postgres per-consumer-role card is precedent only.

---

## 1. The decision

**Redis 6 ACL users — three service users scoped to the three real
key-prefix families plus `default` kept as the admin credential — not one
shared `requirepass`. Credentials travel only inside full Redis URLs with an
explicit username (`redis://<user>:<pass>@host:6379/<db>`); building URLs
from host/port parts is banned and the two constructors that do it get
fixed, along with the auth-blind TCP probe. Rollout is zero-coordination:
create the users while `default` stays nopass, migrate consumers one at a
time in any order, prove completeness with `CLIENT LIST` (it names the ACL
user per connection), close `default` last. Rollback at every phase is one
runtime command, no restart.**

Three choices someone could disagree with, stated as choices:

- **(a) ACL users, not a single password.** One shared password is the
  Postgres-superuser card again (7 consumers as superuser, now in our own
  backlog to undo). It also hands `FLUSHALL`/`CONFIG SET`/`SHUTDOWN` to every
  holder — and this credential gets injected into every nested runner
  workspace's env, i.e. into the hands of semi-trusted agent processes. The
  service users carry `-@admin -@dangerous`, so a runaway agent with
  `AW_REDIS_URL` can corrupt its own prefix, not the stack.
- **(b) Three users, not nine.** Frederico argued per-consumer; the prefix
  survey says the real principals are per-*family* (§4). Five components
  legitimately share `run:*`/`warm_token:*` by construction, and
  aw-workspace's `aw:ws:*` is a subset of aw-backend's `aw:*`. Nine users
  would be a fiction that multiplies rotation sites without adding isolation.
- **(c) No big-bang flip window.** The card's ordering trap is real only for
  the password-only URL form. Verified live on redis:7-alpine (§2): 2-arg
  `AUTH <user> <pass>` against a nopass user returns `+OK` (ACL semantics:
  nopass matches any password), and named ACL users work the moment they are
  created server-side. So consumers can carry credentials *before* the door
  closes, and the door closes only after `CLIENT LIST` proves nobody is
  still walking through it unauthenticated.

## 2. Verified measurements (card numbers checked, one correction)

Everything re-verified against the tree and a live redis:7-alpine this
session, not taken from the card on faith:

| Claim | Verdict |
|---|---|
| `repos/aw-stack/docker-compose.yml:212-236` — `--requirepass "${REDIS_PASSWORD:-}"`, empty default, host-published `6379:6379` with the 2026-09-28 regression comment | **Confirmed**, lines 212–247 in the current tree. |
| `shared_redis.py:144` and `warm_redis.py:161` build `f"redis://{host}:{port}/{db}"` with no password field | **Confirmed** — `repos/aw-app-agents-platform-runners/agents_platform_runners_app/shared_redis.py:144`, `repos/aw-mcp-gateway/back/gateway/warm_redis.py:161`. |
| `warm_redis.py` discovery is auth-blind | **Confirmed** — `_accepts_tcp()` (`warm_redis.py:111-116`) is a bare `socket.create_connection`; `shared_redis.py` has the same twin. |
| 7 Redis-URL env vars, ~28 files, 9 repos | **Confirmed in shape** — my counts: `AW_REDIS_URL` 84 refs/47 files (card said 64; mine includes docs+CI), `AW_WORKSPACE_REDIS_URL` 43/28, `AW_TEST_REDIS_URL` 40/22, `AP_REDIS_URL` 19/10, `AGENTS_REDIS_URL` 18/15, `AW_SHARED_REDIS_URL` 16/5, `AW_MCP_GATEWAY_WARM_REDIS_URL` 6/3. |
| "You cannot pre-stage a password against a nopass server" | **Partially true, and the exception is the whole design.** Verified against redis:7-alpine + redis-py 8.0.1 + raw sockets: 1-arg `AUTH <pass>` → `ERR AUTH <password> called without any password configured…` and redis-py RESP2 raises `AuthenticationError`. But 2-arg `AUTH default <anypass>` → `+OK` (nopass user matches any password), `redis://default:pass@host` connects fine on RESP2 *and* RESP3 (redis-py 8 sends `HELLO 3 AUTH default <pass>`), and a named ACL user (`redis://svc-x:realpass@`) works the moment `ACL SETUSER` runs, while `default` stays open. Also verified: prefix escape by an ACL user → `NoPermissionError`; closing `default` afterwards does not disturb connected/named users. |

The nested-runner path needs **no code change**: AP-MT's
`_container_redis_url()` (`repos/agents-platform-multitenant/backend/app/core/models/cli.py:85-165`)
passes the *full* backend URL through to spawned containers — its own
docstring says credentials "are correct by construction" — and injects it as
`AW_REDIS_URL` at `cli.py:592` and `cli.py:745`. `aw-connector-redis` and
`aw-warm-relay.py` (`repos/agents-platform-multitenant/agent-images/shared/`)
both do `redis.from_url(os.environ["AW_REDIS_URL"])`. Credentialed URL in →
credentialed connection out. The risk is purely *operational* (the AP-MT
host env must be updated), which is why Phase 2 has a named live check for
exactly this consumer.

## 3. Which Redis — the decision table a new consumer reads

Two instances, three logical targets. The ambiguity has caused two incidents
(memories: `workspace-redis-url-differs-between-agent-container-and-server`,
`mcp-gateway-warm-redis-url-override-pointed-at-wrong-instance`); this table
is the fix and goes verbatim into `repos/aw-stack/README.md`:

| You are | Use | Instance / db | Env var | ACL user |
|---|---|---|---|---|
| A stack-level service coordinating across hosts/workers (leases, pub/sub, cooldowns, meta-display state) | shared `aw-redis`, **db 0**, keys under `aw:<yourprefix>:*` | aw-stack | `AW_REDIS_URL` (full URL) | `svc-backend` (or a new family user — see §4) |
| aw-workspace core (any workspace runtime) | shared `aw-redis`, db per deployment, keys under `aw:ws:<ws>:*` | aw-stack | `AW_WORKSPACE_REDIS_URL` → `AW_REDIS_URL` fallback (`src/libs/redis_coord.py`) | `svc-workspace` |
| Anything in the run-event/warm-token flow (AP-MT, runners app, mcp-gateway warm lookup, agent-image connectors, nested runners) | shared `aw-redis`, **db 1**, keys `run:*` / `warm_token:*` | aw-stack | `AGENTS_REDIS_URL`/`AP_REDIS_URL` (AP-MT), `AW_SHARED_REDIS_URL` (apps) — all full URLs | `svc-runs` |
| Workspace-internal state that must not leave the workspace | the per-workspace companion (`aw-remote-host-redis`, loopback inside the workspace netns, provisioned by `repos/aw-backend/src/api/placement/docker_driver.py:181`) | companion | `AW_WORKSPACE_REDIS_URL` as injected | none — stays unauthenticated, loopback-only, **deliberately out of scope** |
| Tests | throwaway container, or `AW_TEST_REDIS_URL` **with credentials** (§8 risk 1) | — | `AW_TEST_REDIS_URL` | `svc-*` of the suite's family |

The 7 env vars **do not collapse** in this change. `AW_WORKSPACE_REDIS_URL`
and `AW_TEST_REDIS_URL` point at different instances by design, and a
7-name→1-name rename sweep across 9 repos adds regression risk with zero
security value. What becomes canonical is the *contract*: every var carries a
full URL including `username:password`, URLs are injected not constructed,
and this table says which var a new consumer reads. `AW_SHARED_REDIS_HOST`-style
part-vars stay only as probe-fallback inputs inside the two fixed modules.

## 4. The ACL model

Users, created via an `users.acl` file mounted into `aw-redis` (passwords
stored as sha256 in the file — the file never holds plaintext; the
plaintexts live in the vault and in the host `.env` files that compose and
consumers read):

```
user default on ><ADMIN_PASS> ~* &* +@all                  # Phase 3; nopass until then
user svc-backend  on ><PASS> ~aw:* &aw:bcast:* +@all -@admin -@dangerous
user svc-workspace on ><PASS> ~aw:ws:* &aw:ws:* +@all -@admin -@dangerous
user svc-runs     on ><PASS> ~run:* ~warm_token:* +@all -@admin -@dangerous
```

Stated honestly: `svc-backend`'s `~aw:*` **includes** `aw:ws:*`. aw-backend
provisions and supervises workspaces, so trust flows upward anyway; carving
backend down to its exact families (`aw:bcast:*`, `aw:leader:*`,
`aw:cooldown:*`, `aw:mdpoll:*`, `aw:meta:*`, per
`repos/aw-backend/src/libs/redis_coord.py` docstring) would mean an ACL edit
on every new backend key family — an operational coupling that *will*
produce the next NOPERM-swallowed-by-a-narrow-except silent incident. Three
coarse families is the grain that matches how the keys are actually shared.
Key patterns are not db-scoped in Redis; the db0/db1 split is convention,
the prefixes are the enforcement.

Secrets handling (standing rules respected): four passwords generated once,
stored in the workspace secret store under names
(`aw-stack-redis-admin`, `aw-stack-redis-svc-backend`,
`aw-stack-redis-svc-workspace`, `aw-stack-redis-svc-runs`); raw values land
only in the host-mounted `.env` files that compose/services read.
`REDIS_PASSWORD` in `repos/aw-stack/.env` becomes the admin password
(healthcheck at `docker-compose.yml:243` already passes `-a
"${REDIS_PASSWORD:-}"`, so it keeps working through every phase). New vars
are APPENDed; the `*_REDIS_URL` value changes in consumer `.env` files are
made by the consumer sync (§5 Phase 2, amended), which mechanizes exactly
the old manual rule — diff before and after, stop on any value that isn't
what this design predicts — instead of trusting nine humans to follow it.

## 5. Where it lands — files and seams

**Code (Phase 0, all pre-stageable, no server dependency):**

1. `repos/aw-mcp-gateway/back/gateway/warm_redis.py:111-170` — replace
   `_accepts_tcp` with a real RESP `PING` probe: AUTH with the credential
   from `AW_SHARED_REDIS_URL`/`AW_SHARED_REDIS_PASSWORD` if present; on
   `-NOAUTH`/auth-required reply, **return `Resolution(None, "auth")` and log
   at ERROR** — "host found, credential missing" must be loud, never a cached
   passwordless URL. The fallback constructor at `:161` gains the credential
   when configured. `/healthz`'s `source` field (already there) gains the
   `auth` state.
2. `repos/aw-app-agents-platform-runners/agents_platform_runners_app/shared_redis.py:103-150`
   — the identical twin fix (this module is the original the gateway ported
   from; fix both, keep them line-comparable).
3. `repos/aw-stack/docker-compose.yml:212-247` + `.env.example` +
   `README.md` — render `users.acl` from `.env` at container start (small
   entrypoint wrapper or init script; coder's call), mount it, add the
   facility contract (§3 table) to the README. `--requirepass` drops in
   favor of the aclfile's `default` line. Compose config validated in the
   repo's existing CI.

**Phase 2 — the consumer sync (amended; was "env/ops only, nine hand
edits"):**

The credential is distributed by extending the pipeline Phase 1 already
built, not by hand edits and not by a new distribution system. The pipeline
today: **GitHub Actions secrets on `tekflox/aw-stack` (the one write point)
→ `deploy.yml`'s "Sync repo + .env on server" step →
`tools/host-env/merge_env.py` (merge-not-overwrite, atomic, preserves every
unmanaged line) → host `/opt/aw-stack/.env` → `scripts/redis-render-acl.sh`
renders the aclfile at every boot** (`deploy.yml:224-290`,
`docker-compose.yml:232-234`). That is already a one-source sync for the
server half and for the three plaintexts; Phase 2 extends the same pipeline
to the consumers. URLs are **derived at the point of use from the password
vars — never stored as a second copy**:

1. **aw-stack's own services — compose interpolation, no sync step at all.**
   `repos/aw-stack/docker-compose.yml:530` currently hardcodes
   `AW_REDIS_URL: "redis://aw-redis:6379/0"` on the `aw-backend` service;
   it becomes
   `redis://svc-backend:${REDIS_SVC_BACKEND_PASSWORD}@aw-redis:6379/0`.
   Compose interpolates from the same `.env` the aclfile is rendered from,
   at container-create — a recreated service converges by construction
   (the `sync_on_boot` property). The repo file holds a `${...}`
   placeholder, never a value.
2. **Other bare-metal checkouts — a sibling of the render script.** New
   `repos/aw-stack/scripts/redis-sync-consumers.sh`, invoked by `deploy.yml`
   right after the existing `.env` merge, renders the consumer URL lines
   from the three password vars and merges them into each target file using
   the **same `merge_env.py`**. Targets (checked-in map, see below):
   - AP-MT checkout `.env` → `AGENTS_REDIS_URL` in `svc-runs` form (pydantic
     `env_prefix="AGENTS_"` + `redis_url`,
     `repos/agents-platform-multitenant/backend/app/config.py:21,31`; its
     compose reads `env_file: .env` and its own `deploy.yml` never touches
     that file — this script becomes the file's only automated writer).
     Then `docker compose restart app` in that checkout. Nested runners
     inherit via `cli.py:592/745` (§2) — plus a warm-pool recycle, since
     warm containers hold the env they were created with.
   - agentic-workspace (monolith) `.env` at `$AGENTIC_WORKSPACE_DIR` (the
     deploy already knows it, `deploy.yml` heredoc) — whichever
     shared-Redis URL vars it still reads; the coder enumerates the exact
     keys at build time and pins them in the target map.
3. **Workspace app settings — the app framework's own config injection,
   driven by the same script.** The gateway's Tier-2 container gets
   `AW_MCP_GATEWAY_WARM_REDIS_URL` from `${config.warm_redis_url}`
   (`repos/aw-mcp-gateway/aw-app.json` runtime.env) and the runners app
   reads its `shared_redis_url` setting — both per-workspace app config,
   reachable as `POST /api/apps/<slug>/config` with the
   `AW_WORKSPACE_API_KEY` that already lives in aw-stack's `.env` (the
   SigNoz provisioner uses exactly this surface,
   `repos/aw-app-signoz/container/provisioner/provision.py:144-150`). The
   sync script POSTs the `svc-runs` URL to both apps per workspace in the
   target map, then restarts the gateway app — **restart, don't trust
   reload** (memory: `mcp-gateway-http-upstream-zombie-caching`).
4. **Not sync targets, stated so nobody adds them:** the per-workspace
   companion Redis stays loopback/netns-unauthenticated
   (`docker_driver.py:181,546` injects `redis://127.0.0.1:6379`;
   `aw-remote-host/bootstrap/workspace/install.sh:24-27,334` injects
   `aw-remote-host-redis` — both companions, §3). Agent images: no rebuild,
   URL arrives via env at run time. Any deployment that points a workspace
   core at the *shared* instance gets its `svc-workspace` URL from its
   provisioner's env, which the sync converges — the workspace itself is
   downstream, not a target.

**The target map is checked into aw-stack** (`scripts/redis-consumers.map`
or a block in the script — coder's call) and is the answer to "what
constrains the fan-out": adding a consumer is a reviewed aw-stack commit,
not an ad-hoc edit; the script refuses targets not in the map.

**Safety under the shared host-`.env` rule** (the sharpest edge): the sync
*does* write production `.env` files, so the rule is mechanized, not
trusted to discipline. `merge_env.py` already writes atomically, preserves
every unmanaged line and the file's mode. On top of that, for files the
aw-stack deploy does not already own (AP-MT, monolith), the script
diffs before/after into the workflow log and **stops without writing** when
a managed key's current value is neither the expected-previous nor the
expected-new rendered form — an operator set it to something else, and
overwriting an unexplained value is exactly the incident class this rule
exists to prevent. aw-stack's own `.env` keeps its existing merge semantics
unchanged (that step predates this design).

**Drift detection:** `redis-sync-consumers.sh --check` renders everything,
compares, writes nothing, exits non-zero on divergence — same contract as
`aw-workspace-cli agent sync --check`. It runs (a) as a verify step at the
end of every deploy, and (b) on a schedule on the `aw-baremetal` runner
(the repo already runs backup crons there), alongside the live-truth check:
`redis-cli CLIENT LIST` grouped by `user=`, which is what actually proves
who authenticates as what. CI on `ubuntu-24.04` cannot see host files, so
the check lives where the files live.

**Untouched:** AP-MT backend code, aw-backend code, agent-image code, the
companion Redis, the knowledgeable connector/key path.

## 6. Rollout order

- **Phase 0 — harden (parallel, ~3 coder + QA dispatches).** The three code
  changes above. Deploy: aw-stack via its deploy workflow;
  **mcp-gateway needs a manual `publish.yml` run BEFORE
  `marketplace install --update`** (memory:
  `container-app-code-change-needs-a-manual-publish-yml-run` — a green
  release otherwise ships old code); runners app via its release flow
  (raw-CDN lag up to ~5 min is normal). Gate: `doctor` clean, gateway
  `/healthz` shows a sane warm-redis source, a live run still streams events.
- **Phase 1 — users exist, door stays open (~1h).** Deploy the compose
  change; `aw-redis` restarts with the aclfile: `default` still nopass, three
  svc users live. Gate: every existing consumer works unchanged (they all
  connect as `default`), `redis-cli ACL LIST` shows 4 users.
- **Phase 2a — build the sync (code, pre-stageable, nothing flips).**
  The §5 Phase-2 pieces: the `docker-compose.yml:530` interpolation edit,
  `scripts/redis-sync-consumers.sh` + target map + `--check`, the
  `deploy.yml` sync/verify steps, the dual-hash extension to
  `redis-render-acl.sh` (§6a), and the app-config POST step. Also
  **regenerate all three svc passwords as `openssl rand -hex 24`** via the
  rotation path itself (§6a) before any consumer flips — it guarantees
  URL-safe passwords (a `:@/#?%` in a password breaks every URL it rides
  in) and exercises rotation once while nobody depends on the users.
  Gate: `--check` green on the host, `ACL LIST` still shows 4 users,
  every consumer still connects as `default`. Verify CI with
  `gh run view`/`gh run watch` from a shell — **`run_monitor_async` is dead
  workspace-wide right now** (five consecutive 401s), so no gate may depend
  on monitors.
- **Phase 2b — consumers flip: run the deploy (~half a day, one dispatch).**
  One `deploy.yml` run converges everything: compose recreates aw-backend
  with the interpolated URL, the sync script merges AP-MT/monolith `.env`
  and restarts them, POSTs the app configs and restarts the gateway, and
  the warm pool is recycled. Per-consumer verification is unchanged:
  `redis-cli CLIENT LIST` shows `user=svc-x` per connection.
  **Named check for the top-risk consumer:** dispatch a real run through the
  "aw" workspace's nested runners and confirm `run:<id>:events` receives
  entries — the 2026-09-28 failure signature is *zero stream events /
  transport failure*, not an auth error, so absence of errors proves nothing;
  presence of stream entries does. Rollback stays per consumer: revert one
  target's rendered line (or the compose edit) and restart it.
- **Phase 3 — close the door (soak 3–7 days, then ~1h).** Watch
  `CLIENT LIST` until zero connections authenticate as `default` (healthcheck
  excepted) across several days *including at least one warm-pool recycle and
  one core restart*. Then set `default on ><ADMIN_PASS>` and restart.
  Rollback: `ACL SETUSER default nopass` — one runtime command, instant, no
  restart, no consumer impact.

"Restarting is not a problem" is used where it helps (Phases 1 and 3 restart
`aw-redis`; every consumer restarts once in Phase 2) — but no phase *relies*
on a synchronized window, which is what makes a silently-missed consumer a
Phase-3-gate finding instead of an outage.

## 6a. Rotation — the acceptance test, walked through

The test this amendment exists to pass: **rotating one `svc-*` password is
ONE operation and every consumer converges with no human editing a file.**

```
gh secret set REDIS_SVC_RUNS_PASSWORD -R tekflox/aw-stack \
    --body "$(openssl rand -hex 24)" \
  && gh workflow run deploy.yml -R tekflox/aw-stack
```

What the deploy then does, in order:

1. `merge_env.py` lands the new value in `/opt/aw-stack/.env`. The sync
   script sees the incoming value differs from the current one and first
   moves the current value to `REDIS_SVC_RUNS_PASSWORD_PREV` **in the host
   `.env` only** — the previous password never exists as a GitHub secret
   (you cannot read a secret back; the host copy is the only place the old
   value is still known, so that is where the stash lives).
2. `redis-render-acl.sh` (extended in Phase 2a) emits **two** `#sha256`
   entries for a user whose `_PREV` var is set — Redis ACL users hold
   multiple passwords, each `#hash` adds one — and `aw-redis` restarts.
   Old and new URLs now both authenticate: **no window where a
   not-yet-restarted consumer is locked out**, which matters because this
   stack's failure signature is silent (*zero stream events*), not an auth
   error.
3. The consumer sync re-renders every URL from the new value and converges
   each target per §5: compose recreate (aw-backend), `.env` merge +
   restart (AP-MT, monolith), app-config POST + gateway restart, warm-pool
   recycle. Nested runners pick up the new URL on their next run via
   `cli.py:592/745`.
4. The verify step runs `--check` plus the `CLIENT LIST` summary.

Retiring the old password is a **second, deliberately separate operation**
after the verification gate (not part of "rotate" — stated plainly rather
than pretending the whole lifecycle is one step): a deploy input
(`retire_prev=true`) drops `_PREV` from the host `.env`, the aclfile
re-renders with one hash, done. If `_PREV` retirement is forgotten, the old
password stays valid — bounded exposure, visible in `--check`'s output,
not silent.

Revoking a leaked family is the same walkthrough minus the grace: set the
new secret, run the deploy with `retire_prev=true` in the same dispatch —
consumers converge inside the run; anything still holding the leaked value
is locked out at the end of it, which is the point.

## 7. Rejected alternatives

- **One shared `requirepass` for everyone.** Rejected — it is the Postgres
  superuser card with a different port: unrevocable without a stack-wide
  rotation, unattributable (`CLIENT LIST` would say `default` forever), and
  it hands @dangerous commands to every nested runner env. This is the
  direct answer to "do we redo this": with a shared password, yes, in about
  six months; with per-family ACLs, no — adding a consumer, revoking a leak,
  or rotating one family are all documented one-liners.
- **Big-bang flip window (update everything, restart everything at once).**
  Rejected — unnecessary once the 2-arg AUTH pre-stage was verified (§2),
  and the auth-blind probe plus the "zero stream events" failure mode means
  a flip-day miss degrades silently, exactly the incident class this stack
  keeps producing.
- **Nine per-consumer users.** Rejected (this is the correction to the card's
  position) — `run:*`/`warm_token:*` is legitimately one principal spanning
  five components, and `aw:ws:* ⊂ aw:*`. Finer grain buys rotation overhead
  and NOPERM landmines, not isolation.
- **Collapsing the 7 env vars into one.** Rejected for this change — two of
  them point at different instances by design; a rename sweep is pure
  regression risk. The contract (§3) is what gets canonicalized. Revisit
  only if a real incident is traced to the naming itself.
- **Unpublishing host :6379 and routing runners through a docker network.**
  Rejected — re-litigates the 2026-09-28 regression; the publish is load-
  bearing for the nested-runner topology. Auth is what makes it safe.
- **TLS.** Not now — single-host LAN traffic, auth closes the actual hole;
  revisit if Redis ever crosses hosts.

Rejected for the Phase-2 amendment (how the credential is distributed):

- **Riding `aw-workspace-cli agent sync`.** Its three properties (exact
  mirror, `--check`, sync-on-boot) are the model, and the amendment copies
  all three — but the command itself is the wrong vehicle: it runs *inside
  a workspace container* and fans out agent-facing files
  (`src/cli/commands/agent.py`, `src/libs/agent_sync.py`); it cannot reach
  `/opt/aw-stack/.env` or any other bare-metal checkout, and teaching the
  agent-visible surface to carry credentials would put a secret path in
  every agent's reach. Copy the semantics, not the command.
- **A new sync daemon / cron on the host.** The deploy workflow already IS
  the process that owns host `.env` state (`merge_env.py`, since
  2026-09-08), runs as root on the right machine, and leaves an auditable
  log per run. A daemon would be the "second bespoke distribution path"
  this card forbids — one more thing `doctor` can't see when it silently
  dies.
- **Vault pull-through for every consumer** (the ap-mt-key pattern,
  0e12767). Right for one key with one consumer and a code path we own;
  wrong here — it would require auth-fetch code in nine consumers across
  three mechanisms and two languages, including compose files that cannot
  fetch anything. Pull-through scales with code ownership; this problem
  scales with deploy ownership.
- **Per-consumer GitHub secrets in each consumer repo's own deploy.** Nine
  write points is the disease this amendment treats; it also hands the
  rotation runbook to five repos' CI configs. One repo (aw-stack) owns the
  facility; its deploy distributes.

## 8. Risks for the coders (the non-obvious breaks)

1. **Test suites going green-silent.** Several aw-backend suites skip when
   Redis isn't reachable, and that skip has already hidden a dead default
   URL once (`test_console_ws_multiworker.py::_redis_url` docstring).
   `AW_TEST_REDIS_URL` must gain credentials in CI/dev envs in Phase 2, and
   `RedisLease` ignores `AW_TEST_REDIS_URL` entirely (memory) — audit those
   suites or they stop testing without failing.
2. **The probe fix must distinguish three states** — nothing listening /
   listening but auth-required / authenticated PING OK — and only the last
   may produce a cached URL. Collapsing the middle state into either
   neighbor recreates the silent degradation this card exists to kill.
   Test against a passworded throwaway redis via its bridge IP (loopback
   port-publish doesn't work from this container — memory).
3. **redis-py floor.** Username-form URLs need redis-py ≥ 4.x. Verified:
   core pins 8.0.1, AP-MT `>=5.0`, gateway `>=5.0`, aw-backend custom apps
   5.2.1, agent images install unpinned (current). The monolith's exact pin
   was not found this session — verify it before its Phase 2 flip.
4. **aclfile syntax errors prevent redis from booting.** The render step
   must be validated in aw-stack CI (`redis-server --test-memory`-style dry
   run isn't a thing for acl; a `redis-server` boot smoke in CI is).
5. **`.env` discipline.** Host-mounted, shared: append new vars, diff
   before/after every in-place URL edit, stop on surprises. Never commit a
   password; the aclfile ships hashes only.
6. **One long silent ops command trips the 600s watchdog** (memory) — chunk
   the Phase 2 verification loops.
7. **URL-reserved characters in a password break every consumer at once.**
   `redis://svc-x:<pass>@` and compose interpolation both die on `:@/#?%`.
   Phase 2a regenerates all three secrets as `openssl rand -hex 24` before
   any consumer flips (§6), and the rotation walkthrough hard-codes the hex
   alphabet. Do not accept an operator-chosen password here.
8. **The monolith's own deploys also run `merge_env.py` on its `.env`**
   (that's where aw-stack's copy was ported from). If the monolith's deploy
   template ever carries the same key the sync manages, the two writers
   fight — last deploy wins, silently. The coder enumerating the monolith's
   keys (§5 item 2) must check its deploy heredocs and either remove the key
   there or exclude it from the map, not both.
9. **Warm containers and already-running workspaces hold the env they were
   created with.** A converged `.env` is necessary, not sufficient — the
   deploy's convergence list must include the warm-pool recycle and note
   that managed workspace containers converge on their next
   provision/restart, which is why the Phase-3 soak must span a warm-pool
   recycle and a core restart (§6 already requires exactly that).
10. **The sync script's STOP-on-unexpected-value rule (§5) will fire one
    day**, and the failure mode to avoid is an agent "fixing" it by deleting
    the operator's value. A stop means a human set something on the host the
    map doesn't know about; the resolution is updating the map or the
    secret, never force-writing.

## 9. Out of scope — handback, verbatim

- **The aw-knowledgeable ap-mt key.** Pull-through (0e12767, card
  `…8195-8b43-fa5e9e189be8`, coder mid-build) stands. Authenticated Redis
  does **not** change that calculus and no question is raised to the coder:
  the veto was about creating a second at-rest location for the key, and a
  password changes who can read the location, not whether it exists.
- **The Postgres per-consumer-role retrofit.** Cited as precedent in §1/§7;
  its own card does the work.
- **The per-workspace companion Redis** stays unauthenticated and
  loopback-only (§3) — it is netns-isolated by construction.
- **TLS, env-var renames** — §7.

## 10. Estimate

**Agent dispatches** (Sonnet-class coders + QA, this platform's usual flow):

| Phase | Dispatches | Agent-time |
|---|---|---|
| 0 — gateway probe fix + tests | 1 coder + 1 QA | 2–4 h |
| 0 — runners-app twin fix + tests | 1 coder + 1 QA | 2–4 h |
| 0 — aw-stack aclfile + README + CI | 1 coder + 1 QA | 2–4 h |
| 1 — deploy + user creation + verify | 1 ops dispatch | 1–2 h |
| 2a — consumer sync: compose interpolation, sync script + map + `--check`, deploy steps, dual-hash render, app-config POST (+ tests) | 1–2 coder + 1 QA | 4–8 h |
| 2b — flip: one deploy run + per-consumer verification (incl. the nested-runner live-run check) | 1 ops dispatch | 2–4 h |
| 3 — soak monitoring + flip + final verify | 1–2 dispatches | 1–3 h |
| **Total** | **11–15 dispatches** | **~14–23 h agent-time** |

**Wall-clock:** Phase 0 runs in parallel → ~1 working day including QA and
the three deploys (mcp-gateway manual `publish.yml` + marketplace update,
runners-app release + CDN lag, aw-stack deploy workflow). Phases 1–2 fit in
a second day. The **3–7 day Phase-3 soak is deliberate** (it must span a
warm-pool recycle and a core restart to prove no straggler), so calendar
time is **~1–1.5 weeks**, of which ~2 days are active work.

**Cost (USD):** observed runs on this platform span ~3–10M tokens
(cache-heavy); at Sonnet-class blended pricing that is roughly **$5–25 per
dispatch**. 11–15 dispatches → **$130–380; call it ~$275 expected, up to
~$500 if a Phase-2 flip uncovers rework** (e.g. the monolith pin or a
green-silent test suite). The sync build (2a) adds one coder+QA pair over
the hand-edit version and removes the per-rotation human cost forever —
that trade is the amendment. Assumptions visible: Sonnet-class models, no
Opus-tier dispatches needed, no incident-response loops beyond one retry.

**Rollback story:** Phase 0 is pure hardening (safe alone); Phase 1 is
additive (users nobody uses yet); Phase 2 is reversible per consumer (revert
one URL); Phase 3 reverts with `ACL SETUSER default nopass`, runtime, no
restart. At no point does a partial state break a consumer that hasn't been
touched yet.

## 11. What this makes harder later

- **New key family ⇒ ACL grant.** A consumer inventing a prefix outside its
  user's patterns gets NOPERM at runtime. Mitigated by the §3 table living
  in the aw-stack README and by the loud probe, but it is a real new
  coupling — the price of answering "who can write what".
- **Rotation is one operation (§6a), but the write point moved.** The
  operational source of truth for the four passwords is now the GitHub
  Actions secrets on `tekflox/aw-stack` — the vault copies become the
  recovery/backup record, not the thing the sync reads. Rotating requires
  aw-stack repo access, and a compromise of that repo's Actions is a
  compromise of credential distribution. Stated rather than hidden: that
  was already true of the deploy pipeline (it holds SSH root to the host);
  this concentrates it, it does not create it.
- **The aw-stack deploy now writes other repos' host state.** AP-MT's and
  the monolith's `.env` gain a second writer (first automated one, in
  AP-MT's case). The target map and the STOP-on-unexpected-value rule are
  the containment; §8 items 8 and 10 are the failure modes. Moving a
  consumer to a new path means updating the map — a coupling that did not
  exist when every edit was manual, and the price of never missing one.
- **`users.acl` is new state** under `repos/aw-stack` deployment data — it
  joins the backup/restore story (hashes only, so low-sensitivity, but a
  lost file means a redis that boots with defaults).
- **db 1 is now formally "the run-event domain".** If AP-MT ever moves its
  streams, the §3 table and `svc-runs` grants move with it — documented
  assumption, not incidental anymore.
