# aw-knowledgeable ap-mt keys: pull-through on miss, connector as the key source

**Status:** design — decided, not implemented.
**Card:** `reliability:apmt-key-in-process-memory-outage-on-every-deploy`
(3f75bf3b-9510-8195-8b43-fa5e9e189be8).
**Constraint set by Frederico, verbatim (Telegram, 2026-10-12):** *"Have a
card to fix the deploy issue and key, they key shouldn't leave in the process
then. Where is redis?"* — "not only in process memory" is decided; this note
is where the key lives instead.
**Evidence:** tonight's `524d3d5` deploy (08:46–08:49 UTC): ~5 min of
`answer: null` / `no_apmt_key` on every synthesis call while retrieval stayed
rank-1-healthy, 200s, health green. Self-healed at the next
`PUSH_INTERVAL_S = 300` tick. Not re-derived here.
**Author:** architect, 2026-10-12.
**Composes with, does not replace:**
`docs/design/knowledgeable-key-push-resilience.md` (dfb6f96) — its
observability (`/api/keys/status`), CLI lever and supervision-tier items stay
valuable; its 30s reactive poll is superseded by this note (see §4.3).

---

## 1. The decision

**Invert the key flow: aw-knowledgeable pulls its ap-mt key when it needs
one — at worker boot, on an empty slot at request time, and on an ap-mt
401 — from a new pull endpoint served by the in-workspace connector,
authenticated with the `X-Internal-Secret` the two sides already share. The
push loops stay untouched for one release as belt-and-braces, then retire.**

Someone could disagree with any of these three choices, so they are stated
as choices: (a) pull, not a shared store — the window is removed rather than
shrunk and no new at-rest location is created; (b) the **connector** is the
key source, not the vault and not ap-mt — it preserves Frederico's 3ea5bf3b
decision that production never holds a vault-opening credential, and it
reuses a credential both sides already hold; (c) **pull-through on miss**
(the first request after a restart fetches inline and then answers), not
pull-at-boot-only — that is what makes the card's acceptance test ("restart,
first request answers") literally true and what makes every uvicorn worker
self-sufficient.

### The three answers the card requires

- **Where the credential lives at rest: the workspace vault, and nowhere
  else — unchanged from today.** This design adds **no** new at-rest
  location. In-memory copies exist transiently in the connector (as today,
  during the read-and-forward) and in each production worker's existing
  module-global slot (as today). The one at-rest credential this design
  *leans on harder* is the already-existing shared service secret:
  production's `.env` (compose `env_file: .env`,
  `repos/aw-knowledgeable/docker-compose.yml:34`) and the workspace app
  secret store (Fernet-encrypted, `aw-workspace/apps/secret_store.py`). Both
  pre-date this design; neither gains a new copy.
- **Who can read the ap-mt keys:** (1) vault callers allowlisted per secret
  (`auto_approve_for: agent:aw-app-knowledgeable`), as today; (2) **new:**
  any holder of the shared `X-Internal-Secret`, via the pull endpoint.
  Today that secret's holders — the connector and production itself — can
  already push keys and call the whole M6 API; both already handle these
  keys, so no new *party* gains access. What changes is what a *future
  compromise* of that secret yields: today "inject a key / call APIs",
  tomorrow also "exfiltrate the three ap-mt keys". §5.1 weighs this; the
  keys are narrow per-agent ApiKeys with a one-command revoke path in
  ap-mt, which is what makes the widening acceptable.
- **Does `--workers N` become safe?** The *key* stops being the blocker:
  pull-through runs independently in every worker process, so each worker
  keys itself on its first miss — the (N−1)/N part-outage mode documented
  in `aw-knowledgeable-production-must-stay-single-worker` disappears.
  **The pin is not thereby lifted.** Other per-process state has never been
  audited for multi-worker (at minimum the extraction circuit breaker,
  `backend/app/ingest/extraction.py:226`, and the ingest drain loop's
  claim assumptions). Unpinning is its own card with its own sweep; this
  design removes the *named* blocker and nothing more. Anyone reading this
  as "workers are now safe" is reading more than it says.

## 2. Why pull wins on the actual failure modes

| Failure mode | Push @300s (today) | dfb6f96 30s poll | Pull-through (this note) |
|---|---|---|---|
| Deploy/crash/recreate | ≤5 min silent outage | ≤30s outage | **First request answers** (inline fetch on miss) |
| Key rotated in ap-mt | ≤5 min of 401s | ≤30s of 401s | **One failed call** (401 triggers re-pull) |
| Connector down | outage until connector returns **+ up to one interval** | same, + ≤30s | outage until connector returns, **heals on next request** — no interval tail |
| `--workers N` | push lands in 1 of N workers; (N−1)/N keyless forever | same — polling is still push-delivered | every worker pulls for itself |

The last row is the structural point: **any push-shaped delivery is
incompatible with more than one worker**, because a push is one HTTP request
landing in one process. No interval tuning fixes that. Pull is not merely
faster; it is the only shape under which the single-worker pin can ever be
revisited.

## 3. Where it lands

### 3.1 Connector — one new route (repos/aw-app-knowledgeable)

- `knowledgeable_app/routes.py` — `build_routes(ctx)` (registered at
  `plugin.py:33` via `ctx.routes.register`) gains
  `GET /keys/{playground|ingest|topics}`. Auth: the request's
  `X-Internal-Secret` header compared with `ctx.secrets.read(SECRET_KEY)`
  (`routes.py:20,35`) using `hmac.compare_digest` — the mirror image of
  production's `verify_service_secret`
  (`repos/aw-knowledgeable/backend/app/core/identity.py:182-194`). **The
  route must enforce this itself**; do not assume whatever auth the
  workspace server wraps app routes in is sufficient — a workspace API key
  must not be enough to extract an LLM credential (same reasoning as
  `require_service`'s "never a human" rule on the push side).
- `knowledgeable_app/playground_key_push.py::_read_from_vault` (and its two
  twins) — factor the vault read out so the pull route and the push loop
  share one implementation. The three-secrets-three-slugs split
  (`knowledgeable-{playground,extractor,labeller}-apmt-key`) is kept —
  per-key revocation/rotation independence is load-bearing, per the push
  modules' own docstrings.
- Response: `{"api_key": "..."}`. Never logged, never cached in the route.
  A vault miss returns 503 with a reason string, mirroring
  `paused_reason()` vocabulary so production's error surface stays uniform.

### 3.2 Production — pull-through in the three key modules (repos/aw-knowledgeable, master 524d3d5)

- `backend/app/config.py` — new setting `knowledgeable_key_source_url`
  (empty = pull disabled; keeps every test and local run offline, same
  contract as `playground_apmt_key` short-circuit at
  `backend/app/api/playground.py:118`).
- One small shared helper (suggest `backend/app/core/key_pull.py`): GET
  `{key_source_url}/keys/{slot}` with
  `X-Internal-Secret: settings.knowledgeable_service_secret`
  (`backend/app/config.py:36` — production already holds this in `.env`),
  short timeout (≤5s), **never raises** — on failure the slot stays empty
  and today's `paused_reason` vocabulary answers, unchanged.
- `backend/app/api/playground.py` — where `api_key()` currently returns the
  empty slot and the caller 503s with `no_apmt_key`: on empty, call the
  pull helper inline, fill the slot via the existing `set_pushed_key()`
  path, proceed. Same change in `backend/app/ingest/extraction.py` and
  `backend/app/topics/label.py` at their slot-read seams. On an ap-mt 401
  (`core/apmt.py` call sites), clear the slot and re-pull once before
  surfacing the error.
- A boot-time warm pull in each worker's lifespan is a nice-to-have on top
  (first request pays zero extra latency), but pull-on-miss is the
  correctness mechanism; do not make boot depend on the connector being up
  (`resolve_api_key()`'s never-raise contract stands).

### 3.3 What stays untouched

- The three push loops and `POST /api/{playground,ingest,topics}/key` —
  untouched this card. They become redundant once pull is verified in prod;
  retiring them is a follow-up one-liner each, and keeping them meanwhile
  means a connector that hasn't shipped the new route yet still heals
  production within 300s (deploy-order freedom).
- The vault, its policies, `core/secrets.py`, and
  `test_no_llm_provider_transport.py`'s veto guard — no provider key
  appears anywhere in this design; everything is the ap-mt ApiKey.
- dfb6f96's observability and CLI-lever items — still worth building;
  `/api/keys/status` gains a `source: pushed|pulled` field cheaply.

## 4. Rejected alternatives

### 4.1 Redis (`aw-stack-aw-redis-1`) — rejected, with or without auth

Frederico asked "where is redis?", so this was evaluated first, head-on.
It exists and is reachable with zero new plumbing
(`repos/aw-stack/docker-compose.yml:212`, on `aw-stack-net` like
aw-knowledgeable). But:

- **It has no auth** — compose lines 216-221, empty `--requirepass`,
  self-described as "worth revisiting separately — flagged to Frederico,
  not fixed here". A live LLM credential at rest in an unauthenticated
  store is a strict downgrade from a design whose whole point is that the
  key never touches a file, an env var or disk.
- **It is worse than the card states:** the same compose block publishes
  `6379:6379` on the **host's real IP** (the 2026-09-28 runner-reachability
  fix) — so the readership of an unauthenticated key there is not just
  "every container on aw-stack-net" but anything that can reach the bare
  metal host's :6379, including every nested runner workspace.
- **With auth enabled** it is still dominated by pull-through: (a)
  `REDIS_PASSWORD` must roll out to every existing consumer first — its own
  card, acknowledged by the card text; (b) the Redis password then has to
  be distributed to aw-knowledgeable, which is the same
  credential-distribution problem this card exists to solve, recursed;
  (c) a store the consumer polls or reads per-miss is just pull with an
  extra at-rest copy and an extra availability dependency; (d) it adds
  aw-knowledgeable's first Redis client dependency for nothing the
  connector endpoint doesn't already give.

Redis would be the right answer if the connector could be *down for long
periods while production restarts repeatedly* — a durable buffer decouples
the two lifetimes. That failure pattern has not been observed (the connector
outage mode we have seen, `core-restart-permanently-kills-app-supervised-
subprocesses`, kills push and would kill Redis-refresh identically), and
§3.3's kept push loops cover the transition anyway.

### 4.2 Production pulls the vault directly — rejected, and not re-litigated

The machinery exists (`backend/app/core/secrets.py`, used by
`resolve_api_key()` when configured). Frederico rejected it outright on card
3ea5bf3b (2026-09-30): production holding `AW_WORKSPACE_HOST_TOKEN` means a
production compromise opens the whole `aw` vault namespace, and that token
would sit in the same `.env` this design is keeping credentials out of.
Pull-through deliberately keeps the connector as the only vault reader; the
vault's audit trail and one-shot delivery semantics are preserved unchanged.

### 4.3 The dfb6f96 30s reactive poll — stopgap only

Strictly weaker than what Frederico asked for: it shrinks the window to
≤30s, it does not remove it, and being push-delivered it does nothing for
`--workers N` (§2's table, last row). If the implementation card stalls,
ship this as a stopgap and label it one — but it is a different, smaller
thing than this design, not a cheaper version of it.

### 4.4 ap-mt as the pull source — rejected

Chicken-and-egg: the credential needed to authenticate to ap-mt *is the
thing being fetched*. ap-mt would need a new bootstrap-credential class and
a new issuance surface — the largest possible blast radius for the smallest
possible gain.

## 5. What this makes harder later, and risks for the coder

### 5.1 The shared secret now unlocks key *read*, not just key *write*

Named in §1. If a third party is ever given the `X-Internal-Secret` for API
access (a second connector, a test harness), they inherit ap-mt key read. If
that day comes, split a dedicated pull secret — the route makes that a
one-line auth change. Do not split pre-emptively; two secrets to rotate for
one channel is standing cost against a hypothetical.

### 5.2 Reverse reachability is **verified-false-by-default, check it first**

The connector→production direction works by container name
(`http://aw-knowledgeable:8090`, `knowledgeable_app/mcp/client.py:27`)
because aw-host is itself a container on aw-stack's network
(172.22.0.0/16 — memory `aw-host-is-a-container-on-aw-stacks-own-network`).
The **reverse** is not symmetric: the workspace server (:9030) is a *podman
container nested inside* aw-host (10.89.0.0/24). From aw-knowledgeable,
`aw-host`'s aw-stack IP only reaches the workspace server if aw-host
forwards/publishes that port. **First implementation step: probe it** —
`docker exec aw-knowledgeable curl -sf http://<aw-host-name-or-172.22.0.9>:9030/api/health`.
If unreachable, the fallback is the workspace's public edge URL, which
provably reaches the workspace API (it is how every external client gets
in); the tunnel's 30s request cut is irrelevant for a sub-second key fetch.
`knowledgeable_key_source_url` being config, not convention, is what makes
this a deployment detail instead of a design risk. Note the memory's own
warning: the topology is a measurement with a shelf life, nothing enforces
it.

### 5.3 The place the existing code lies

`resolve_api_key()`'s docstring (playground.py:87-114) argues at length
*against* per-request vault reads — four-minute approval expiries, an HTTP
round trip per question. Do not mistake that for an argument against this
design: pull-through hits the **connector** (no approval protocol, no
Telegram gate, LAN round trip), and only on a **miss**, not per request. The
steady state is still a module-global read costing nothing.

### 5.4 Tests that won't catch the real failure

The outage this card fixes was invisible to every green signal (health,
200s, retrieval scores). The implementation card's acceptance test must be
the card's own: **restart the production container, issue one synthesis
request immediately, assert `answer` is non-null** — run it against the real
container (`aw-autoskill-aw-knowledgeable-live-measurement-harness` is the
pattern), not just under pytest, where `knowledgeable_key_source_url` empty
means the path under test is disabled (the exact trap
`optional-config-gated-code-paths-ship-untested-and-crash-on-first-real-use`
documents — add one test that runs with the setting *on* against a stub
source).

### 5.5 Concurrency on the miss path

Under `--workers N` (future) or concurrent first requests, several callers
may pull simultaneously. The reads are idempotent and the vault read is
auto-approved (no one-shot token burned per §3.1's shared `_read_from_vault`
— verify: the aw-app-secrets read is per-request, not per-value); last
writer wins on an identical value. An asyncio lock per slot is cheap
insurance against a thundering herd on the connector; more than that is
over-engineering.
