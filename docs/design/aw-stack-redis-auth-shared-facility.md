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
deliberate in-place edits — diff before and after each one, stop on any
value that isn't what this design predicts.

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

**Env/ops only (Phase 2, no code):**

- AP-MT host `.env`: `AGENTS_REDIS_URL`/`AP_REDIS_URL` → `svc-runs` form.
  Nested runners inherit via `cli.py:592/745` (§2).
- aw-backend host `.env`: `AW_REDIS_URL` → `svc-backend` form.
  `docker_driver.py:181` (companion) untouched.
- aw-workspace core deployments: `AW_REDIS_URL`/`AW_WORKSPACE_REDIS_URL` →
  `svc-workspace` form (resolution chain in `src/libs/redis_coord.py`
  unchanged).
- mcp-gateway app setting (`AW_MCP_GATEWAY_WARM_REDIS_URL` or
  `AW_SHARED_REDIS_URL`) and runners-app setting (`shared_redis_url`) →
  `svc-runs` form. **Restart the gateway, don't trust reload** (memory:
  `mcp-gateway-http-upstream-zombie-caching`; `warm_redis.reset_cache()`
  has a 30s cooldown but the clean path is a restart).
- agentic-workspace (monolith) env where it still reads a shared-Redis URL.
- Agent images: **no rebuild** — URL comes from env at run time.

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
- **Phase 2 — consumers flip, any order, no coordination (~1 day spread).**
  Each consumer's URL gains `svc-x:<pass>@`; restart it; verify with
  `redis-cli CLIENT LIST` that its connections now show `user=svc-x`.
  **Named check for the top-risk consumer:** dispatch a real run through the
  "aw" workspace's nested runners and confirm `run:<id>:events` receives
  entries — the 2026-09-28 failure signature is *zero stream events /
  transport failure*, not an auth error, so absence of errors proves nothing;
  presence of stream entries does.
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
| 2 — consumer flips + per-consumer verification (incl. the nested-runner live-run check) | 2–3 ops dispatches | 3–6 h |
| 3 — soak monitoring + flip + final verify | 1–2 dispatches | 1–3 h |
| **Total** | **10–14 dispatches** | **~12–20 h agent-time** |

**Wall-clock:** Phase 0 runs in parallel → ~1 working day including QA and
the three deploys (mcp-gateway manual `publish.yml` + marketplace update,
runners-app release + CDN lag, aw-stack deploy workflow). Phases 1–2 fit in
a second day. The **3–7 day Phase-3 soak is deliberate** (it must span a
warm-pool recycle and a core restart to prove no straggler), so calendar
time is **~1–1.5 weeks**, of which ~2 days are active work.

**Cost (USD):** observed runs on this platform span ~3–10M tokens
(cache-heavy); at Sonnet-class blended pricing that is roughly **$5–25 per
dispatch**. 10–14 dispatches → **$120–350; call it ~$250 expected, up to
~$500 if a Phase-2 flip uncovers rework** (e.g. the monolith pin or a
green-silent test suite). Assumptions visible: Sonnet-class models, no
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
- **Rotation touches N host `.env` sites.** The vault holds the source of
  truth, but hosts hold copies; a rotation runbook (per-family, which is the
  point of the split) belongs in the aw-stack README when Phase 3 lands.
- **`users.acl` is new state** under `repos/aw-stack` deployment data — it
  joins the backup/restore story (hashes only, so low-sensitivity, but a
  lost file means a redis that boots with defaults).
- **db 1 is now formally "the run-event domain".** If AP-MT ever moves its
  streams, the §3 table and `svc-runs` grants move with it — documented
  assumption, not incidental anymore.
