# aw-knowledgeable — infrastructure design (phase 1)

Status: **design, not code.** Scope is the five infrastructure points below.
The RAGA-style agentic construction/retrieval loop is deliberately **out of
scope** and still under direct discussion with Frederico.

Written by the Architect agent, 2026-09-26. Every claim below is grounded in
a file path read in this session; the three things I could **not** verify
from the code are named as such in §6.

Locked decisions inherited (not re-litigated here):

1. standalone service, not a Tier-2 aw-workspace app;
2. native Neo4j with its own vector index — no reuse of `aw-app-kb`'s pgvector;
3. a RAGA-style build loop is the intended direction (next phase).

Prior art check: the only Neo4j decision already in this tree is
`repos/aw-app-uc-phd/docs/graph-db-plan.md:153-179`, which **rejected** Neo4j
— for a single-user, offline, no-deploy repo with a ~2k-edge graph. Different
problem, different constraint. It does not contradict decision 2, and it is
worth reading only for the cost list it enumerates ("a container, a port, a
volume, a backup story and a service to keep alive"), all five of which this
document has to answer.

---

## 1. Neo4j deployment topology

### Decision

**Neo4j runs as a service in `repos/aw-stack/docker-compose.yml`**, beside
`aw-postgres` / `aw-redis` / `aw-caddy`. The aw-knowledgeable application
container lives in **its own repo's own compose file** and joins
`aw-stack-net` to reach it — exactly the split AP-MT already uses.

### Where it lands

- `repos/aw-stack/docker-compose.yml` — new `aw-neo4j` service. Copy the
  shape of `aw-postgres` (lines 18-78), which already encodes every decision
  worth inheriting:
  - **no `container_name`** unless something has to `docker exec` it. The
    `aw-caddy` comment (lines 107-140) is explicit that a fixed name is only
    justified by real `docker exec` call sites.
  - **loopback-only publish**: `127.0.0.1:7687:7687`. The comment at lines
    38-56 records that a `0.0.0.0` publish on the Postgres replica "made it
    reachable from the whole internet with nothing but that password (found
    2026-08-31)". Neo4j's default credential story is no better. Do not
    publish 7474 at all — Neo4j Browser is not needed in prod, and the graph
    UI is §5.
  - volume under `./data/` (e.g. `./data/aw-neo4j`), matching
    `./data/aw-postgres-data`.
  - `restart: unless-stopped` + a real `healthcheck` (`cypher-shell
    'RETURN 1'`), so a wedged boot is visible rather than silent.
  - **pinned image tag**, never `:latest`. aw-stack's own strongest
    convention is the digest pinning for `aw-remote-host` via repo
    *variables* (deploy.yml lines 33-40: "a digest is not a credential, and
    it needs to be readable in the run log — 'which digest did this deploy
    pin?' is the first question of any rollback").
- `repos/aw-stack/.github/workflows/deploy.yml` — `workflow_dispatch` only,
  with a `services` input; a deploy is `up -d --no-deps <service>`, so adding
  `aw-neo4j` never restarts Postgres. `NEO4J_AUTH` goes into the `.env`
  template that `tools/host-env/merge_env.py` **merges** (deploy.yml lines
  42-62) — a key added there is not truncated away by the next unrelated
  deploy, which is why this is the right home for the credential.
- `repos/aw-knowledgeable/docker-compose.yml` (new repo) — the app only.
  Model it on `repos/agents-platform-multitenant/docker-compose.yml`: one
  `app` service, `env_file: .env`, `stop_grace_period: 30s`, published port,
  and a `networks:` block that **declares** `aw-stack-net` as external
  rather than relying on a hand-run `docker network connect`. AP-MT's own
  file (lines ~150-175 of the trailing comment) records the outage that
  caused: "a manual `docker network connect` is invisible to compose — so
  every recreate dropped it … the service stayed down until the network was
  reattached by hand."

### Why aw-stack and not aw-knowledgeable's own compose

aw-stack's purpose is stated in its first lines: shared base infra whose
lifecycle is independent of its consumers, because "stopping either of those
must never take Postgres, Redis or Caddy down with it." A graph database is
the durable asset of this project; the application container will be
recreated on every deploy. Three concrete wins:

- **Backups already have a home there** (`.github/workflows/backup-postgres.yml`,
  `scripts/backup-postgres.sh`). A second compose project means a second
  backup story that nobody writes — and memory `postgres-backup-was-never-scheduled`
  says exactly how that ends here.
- **Precedent**: AP-MT's own data lives in aw-stack's Postgres, not in AP-MT's
  compose. Its compose holds the app and one small named volume.
- **Selective deploy** already exists: recreating the app never touches the DB.

### Rejected

- **Neo4j inside aw-knowledgeable's own compose, as a named volume.** The
  precedent for that in this tree is AP-MT's `gallery-data` volume, shipped
  as an explicit "for now" (its compose comment quotes Frederico: *"não quero
  perder [imagens] por enquanto"*) with a real object store named as the
  proper fix. Doing the same for the *primary* datastore repeats a known
  compromise on the one thing that cannot afford it.
- **Neo4j inside the aw-workspace container as a Tier-2 app.** Ruled out by
  decision 1, and independently by
  memory `nesting-a-container-runtime-inside-a-non-privileged-podman-container`.
- **`:latest` with an auto-deploy on push.** aw-stack's deploy is manual on
  purpose ("this stack holds the shared Postgres/Redis/Caddy that everything
  else depends on, so a deploy is never implicit on push"). Don't weaken that
  for a new service.

### Constraints the Coders must handle before first boot

- **Disk.** Memory `host-disk-near-full-breaks-apps-silently`: the bare-metal
  host was at **95% (22G free)** on 2026-09-15 and creeping. Neo4j's default
  transaction-log retention is generous. Set
  `NEO4J_db_tx__log_rotation_retention__policy` and an explicit page-cache /
  heap cap in compose **before** the first boot, and clean the disk first.
  `aw-workspace-cli doctor` does not check disk — it will report green while
  the store fails with ENOSPC.
- **Community Edition supports exactly one user database** (`neo4j`) plus
  `system`. That forecloses database-per-tenant and drives §2. If anyone
  wants `CREATE DATABASE` per tenant, that is an Enterprise **licence**
  decision for Frederico, not something to design around. **Verify the
  edition/version limits against the pinned image before writing the
  isolation layer** — this is the one external fact in this document I have
  not confirmed against a running server.

---

## 2. Multi-tenancy with data isolation

### Decision

**Property-based tenant scoping on every node and every relationship,
enforced at a single Cypher-execution seam — and made *structural*, because
Neo4j has no equivalent of SQLAlchemy's statement-rewriting hook.**

Three parts:

1. **Carrier** — port `agents-platform-multitenant/backend/app/core/tenant_ctx.py`
   verbatim in shape: a `ContextVar` + `tenant_scope()` + an explicit
   `CROSS_TENANT = "*"` opt-out. Read its docstring before copying: it
   explains why *two* carriers exist (contextvar for async work, plus a
   per-session stamp) and why `tenant_scope` clears defensively when the
   caller straddles a thread hop — "using only the contextvar works in tests
   … and fails in production under concurrency."
2. **Gate** — one module, say `backend/app/core/graph.py`, is the **only**
   place in the repo that touches the Neo4j driver. It reads the bound
   tenant, and under `KNOWLEDGEABLE_TENANT_ENFORCEMENT=strict` (the default)
   **raises** a named `UnscopedTenantAccess` when nothing is bound. Same
   three-mode ladder as AP-MT's `AGENTS_TENANT_ENFORCEMENT`
   (`core/tenant_filter.py` docstring): `strict` default, `warn` for hunting
   an unbound path, `off` as the env-var rollback lever that needs a restart
   and not a deploy.
3. **Structural enforcement, not rewriting.** This is where the design has to
   *diverge* from AP-MT rather than copy it, and the reason is written down in
   AP-MT's own docstring: trap 2 says raw SQL "is invisible to this filter
   forever … every *future* raw-SQL call site is born unprotected, and nothing
   will warn about it." In Neo4j, **every** call site is that case —
   `do_orm_execute` has no analogue and a Cypher string cannot be safely
   rewritten to add a predicate. So:
   - feature code never writes a Cypher string. It calls named templates from
     a fixed catalogue, each of which carries `{tenant: $tenant_id}` in every
     node pattern and on every relationship it creates;
   - a **static guard test** fails if `session.run(` / `execute_query(`
     appears anywhere outside `core/graph.py`, and fails if any template in
     the catalogue has a `MATCH`/`MERGE`/`CREATE` pattern without `tenant`.

   The runtime hook AP-MT gets for free is bought here with a test. Say that
   out loud in the module docstring so the next person does not assume a
   filter exists.

### Write-side refusal (the composite-FK analogue)

AP-MT bought write-side integrity with composite foreign keys, and its
`backend/tests/test_composite_fk_cross_tenant.py` docstring explains why a
read filter is not enough: "17 foreign keys pointed at tenant-scoped tables
and not one required the two ends to agree … A filter cannot un-write that."
Neo4j has no foreign keys at all, so:

- every relationship-creating template **MATCHes both endpoints with the
  tenant property on both** before `MERGE`-ing the edge. A cross-tenant link
  then finds no endpoints and creates nothing — refusal by construction.
- the relationship itself carries `tenant` too, so a traversal cannot walk
  out of the tenant even if an endpoint is ever mis-stamped.
- composite uniqueness as a real constraint:
  `CREATE CONSTRAINT FOR (d:Document) REQUIRE (d.tenant, d.external_id) IS UNIQUE`.
  This is also what makes AP-MT's T2 behaviour true here — two tenants may
  hold the same document id without a 409.

### The vector index is the leak

**Neo4j vector indexes are per-label, not per-tenant.**
`db.index.vector.queryNodes` returns global nearest neighbours; a `WHERE
n.tenant = $t` applied *after* the call filters the result but does not
filter the search. Consequences, both real:

- a naive `k`-NN as tenant A can return B's chunks → leak;
- over-fetching `k*N` and post-filtering fixes the leak but **silently
  degrades recall** for a small tenant whose neighbours are crowded out by a
  large one — a correctness bug that presents as "bad results", not as a
  security finding.

So the seam must own vector search as a first-class operation (over-fetch
factor, a floor on returned rows, and a log when the floor is not met), and
K4 in §2's proof below is the only test that catches this. If the pinned
Neo4j version supports a tenant-filtered / pre-filtered vector query, use it
and delete the over-fetch — verify against the actual image.

### Where the tenant comes from — correcting the card's premise

The card says aw-knowledgeable should "consume the tenant_id from aw-backend,
the same way AP-MT does". **AP-MT does not do that.** What the code actually
says:

- `agents-platform-multitenant/backend/app/core/identity.py:229-267`
  (`resolve_tenant_id`) maps the JWT's `sub` claim to AP-MT's **own local**
  `Tenant` row via `Tenant.account_ref`, minting one on first sight. "One
  account, one tenant."
- aw-backend's `tenants` / `tenant_members` tables exist (`aw-backend/src/api/db_models.py:1088-1125`)
  but are **schema + backfill only**. `src/api/pg_db.py:655-712` says it
  outright: *"Nothing here reads a tenant: minting the `tenant` JWT claim
  from these rows is T2"*. There is **no tenant claim in the JWT today.**

So aw-knowledgeable must do what AP-MT does: own a small local `tenants`
projection keyed on `account_ref = sub`. That is not inventing a competing
tenant concept — identity still originates in aw-backend, and the projection
is the same one AP-MT has. It is the only thing that works until T2 lands.

**Design `resolve_tenant_id` as the single swap point**, so that when T2
mints the claim, switching from "map `sub` → local row" to "read the claim"
is one function in one file, for both services.

### Isolation is a proof, not a promise — the acceptance test

Mirror `agents-platform-multitenant/backend/tests/test_tenant_isolation.py`.
Read its docstring first; the reasoning is more valuable than the code.
`backend/tests/test_tenant_isolation.py` in aw-knowledgeable, five tests:

- **K1 — route sweep.** Enumerate `app.routes`, call every GET as tenant A
  with a signed `aw_id_jwt` (port `backend/tests/conftest.py:41-53`'s
  `sign_identity_jwt` / `valid_identity_payload`), and assert tenant B's
  **sentinel string** appears nowhere in the **raw** response body — a
  substring search, not a field comparison, so a field nobody enumerated
  still trips it. Routes naming one of B's ids must answer **404, not 403**
  (a 403 confirms the id exists, which is already a leak). Every GET route
  must be classified in a **literal** `CROSS_TENANT_ALLOWLIST` or
  `GET_ROUTE_PLAN` table; an unclassified route is a **failure, never a
  skip** — that rule is what makes this an acceptance criterion instead of a
  snapshot.
- **K2 — write side.** As A, create a Document whose `external_id` already
  exists in B: must be 2xx, **not 409**. Then as B, fetch it and get B's node.
- **K3 — cross-tenant edge refusal.** Build a link from A's document to B's
  document, push it *through the seam*, and assert **zero relationships
  created**. Written the ugly way round, like `test_composite_fk_cross_tenant.py`:
  attempt the crossed write and assert the graph refused it. A test that only
  asserted "the query doesn't return it" would pass against a broken schema.
- **K4 — vector search.** Seed A and B with near-identical embeddings, query
  as A with `k` below B's row count, assert B's chunks appear nowhere, **and**
  assert the over-fetch actually returned the expected number of A-rows. This
  is the only test that covers the hole above.
- **K5 — static guard.** No driver call outside `core/graph.py`; every
  template mentions `tenant` in every pattern.

Two harness rules that are load-bearing:

- **Run against a real Neo4j**, not a mock — K4's behaviour cannot be faked.
  CI needs a Neo4j service container; the in-tree precedent for "this suite
  needs a throwaway datastore" is memory `core-integration-tests-need-a-throwaway-postgres`.
- **Do not let a suite-wide tenant bind leak into this file.** AP-MT marks it
  `pytestmark = pytest.mark.no_tenant_bind` precisely because "the whole
  question is whether the APP binds one, so the suite-wide bind has to stay
  out of this file."

Also port the *second* half of AP-MT's lesson:
`backend/tests/test_public_entrypoints_under_strict.py` — every identity-free
entrypoint must keep working under `strict`. In AP-MT, three of them 500'd
the first time strict went on.

### Rejected

- **Database-per-tenant (`CREATE DATABASE tenant_x`).** The strongest
  isolation Neo4j offers, and I am rejecting it: Enterprise-only (Community
  is one user database), it caps in the low hundreds of databases, and each
  one carries its own page cache and file handles. For a platform whose real
  tenant count is 1–2 that buys a licence and an ops model for a problem that
  does not exist yet. **Revisit if** a tenant contractually requires physical
  separation, or if the tenant count passes ~20 with large per-tenant graphs.
- **One Neo4j instance per tenant.** Same reasoning, strictly worse.
- **Shipping `warn` first, like AP-MT did.** AP-MT had to, because it was
  retrofitting 462 existing call sites. A greenfield service has zero, so
  **start at `strict`** and skip the phase where the acceptance test finds 48
  leaking routes. Keep `off` as the rollback lever.
- **Relying on a runtime query-rewriting filter.** There isn't one. See above.

---

## 3. Identity integration (mirror AP-MT "Phase A")

### Decision

Port AP-MT's Phase A almost verbatim, with two deliberate deviations.

### Where it lands

- `backend/app/core/identity.py` — port from
  `agents-platform-multitenant/backend/app/core/identity.py`. Verify the
  `aw_id_jwt` cookie (`COOKIE_NAME = "aw_id_jwt"`, `JWT_ALGORITHM = "EdDSA"`,
  Ed25519) against aw-backend's JWKS at
  `{aw_backend_base_url}/api/identity/.well-known/jwks.json`
  (`backend/app/config.py:137-138`), cached in-process, refreshed **only** on
  a verify-failure or kid-miss. Issuer side is
  `aw-backend/src/api/identity_auth.py` (`create_identity_jwt` :125,
  `get_public_jwk` :94) — no shared secret, no per-request round trip.
  - **Deviation 1:** default `aw_backend_base_url` to **`http://aw-backend:9025`**,
    not AP-MT's `http://127.0.0.1:9025`. That loopback default is a leftover
    from when AP-MT shared aw-sandbox's network namespace; a new service on
    `aw-stack-net` should name the service. `aw-backend` has
    `container_name: aw-backend` and is on that network, so it resolves.
- `backend/app/api/__init__.py` — **one shared gate list**,
  `_identity_gate = [Depends(require_tenant)]`, applied to every router.
  Do **not** gate per-route. The comment at AP-MT's `api/__init__.py:32-50`
  is the reason: card #5 shipped the ORM filter with only one router binding
  a tenant, and the acceptance test "then found 48 GET routes handing one
  tenant's rows to another." One list means "the next router added here
  inherits isolation instead of having to remember it."
  - `require_tenant` must be **async**, not a sync generator — see §7 risk 3.
- `backend/app/api/me.py` — port from AP-MT's, same shape:
  `{"user": {"id": …}, "tenant_id": …, "workspaces": […]}`. Workspaces are
  fetched **live** from aw-backend `GET /api/workspaces`, forwarding the
  caller's cookie, *not* read from the token's possibly-stale `memberships`
  claim. Keep `tenant_id` in the payload, for the reason AP-MT's docstring
  gives: "Without it, tenant isolation is unfalsifiable in production: every
  scoped endpoint just returns fewer rows, which is indistinguishable from
  having no rows."
  - `/api/me` resolves identity itself via `Depends` (so it 401s in the same
    shape) and sits **outside** the gate list, alongside `/api/health`.
- **Redirect to console is frontend-side, and this is deviation 2.** There is
  no backend `RedirectResponse` in AP-MT — I checked; `settings.console_login_url`
  at `backend/app/config.py:59` is **dead code, read by nothing**. Do not port
  that knob. What actually exists:
  - `frontend/src/components/AuthGate.tsx` — checks `GET /api/me`, and on 401
    renders an explicit logged-out screen with a **Log in** link to
    `${CONSOLE_BASE_URL}/login?return=<encoded href>`. Its comment records the
    product call: "Frederico wants an explicit click, not a bare bounce to
    the console."
  - `frontend/src/lib/api.ts:505-513` — on a 401 from any path **except**
    `/api/me`, navigate to `${CONSOLE_LOGIN_URL}?return=…`. The `/api/me`
    exemption is what prevents a redirect loop; keep it.

  Port both, including the exemption and the explicit-click behaviour.

### The end-state Frederico asked for

"A tenant logged in via aw-console can reach `knowledgeable.aw.tekflox.com`
the same way they reach the AP-MT domain today." That works because the
`aw_id_jwt` cookie is set with `Domain={AW_DOMAIN}` — see
`aw-backend/src/libs/caddy_template.py`'s module docstring on cookie scoping
— so it is already sent to any `*.aw.tekflox.com` host. No new login, no
cookie work: the new host inherits the session as soon as §4 puts it on the
edge.

### Rejected

- **aw-knowledgeable minting its own sessions / its own login page.** Would
  fork the identity story and break the single-sign-on property above.
- **Verifying the JWT by calling aw-backend per request.** JWKS + cache is
  already the chosen shape and puts aw-backend off the hot path.
- **Trusting the token's `memberships` claim for the workspace list.** AP-MT
  deliberately rejected this; a console-side create/switch/remove must be
  visible immediately.

---

## 4. Routing / domain

### Decision

**`knowledgeable.aw.tekflox.com`, as a `custom_domains` entry, written by
aw-backend's `caddy_template.py` — behind a mandatory render-diff pre-flight,
and with no new write path to the Caddyfile.**

### Hostname pattern — resolved, with evidence

The card asks whether AP-MT's live hostname is `agents-platform.aw.tekflox.com`
or `agents-platform.app.aw.tekflox.com`. It is the **first**:

- `agentic-workspace/src/config/aw.json` → `custom_domains` lists, enabled:
  `api.tekflox.com`, `www.tekflox.com`, `api.aw.tekflox.com`,
  `console.aw.tekflox.com`, **`agents-platform.aw.tekflox.com`** (port 10014,
  `proxy_host: agents-platform-multitenant`), `headscale.aw.tekflox.com`,
  `derp.aw.tekflox.com`.
- Probed live this session: `curl https://agents-platform.aw.tekflox.com/api/health`
  → **200**. `knowledgeable.aw.tekflox.com` → no TLS, does not exist yet.

`*.app.aw.tekflox.com` is a *different thing* — the app-framework wildcard
block, whose whole purpose (`aw-backend/src/libs/caddy_template.py` module
docstring) is that one wildcard cert covers every present and future app
subdomain, because a per-hostname block makes Caddy attempt a per-hostname
DNS-01 challenge that fails against a single wildcard TXT record. A
standalone service is not an app, so it belongs in `custom_domains`, beside
api.aw / console.aw / agents-platform.aw.

### Which generator is the live source of truth — the memories are stale

Both memories the card names (`live-caddyfile-generator-is-aw-backends-copy`,
`caddy-two-generators-read-two-different-config-stores`, both 2026-09-05) are
**superseded**. `agentic-workspace@676145a`, 2026-09-13, on `origin/main`:

> `fix(caddy): retire agentic-workspace as a Caddyfile writer` … "Per
> Frederico: only aw-backend/aw-stack may generate the Caddy template now.
> `regenerate_and_reload()` is a deliberate no-op going forward."

So the live generator is **`aw-backend/src/libs/caddy_template.py`**, and its
config store is the **Postgres `workspace` row**, not a file:
`aw-backend/src/config/__init__.py` — "Source of truth is the Postgres
`workspace` table … aw-backend ships without the file." The `custom_domains`
loop is `src/libs/caddy_template.py:1084-1087`.

### The hazard that is still open, and the gate on it

The 2026-09-05 outage was a **data** divergence, not a code one: the DB row
had 2 `custom_domains` where aw.json had 7, so aw-backend's render dropped
six live site blocks and took api.aw, console.aw, agents-platform.aw,
headscale.aw, derp.aw and www.aw off the edge for ~4 minutes. What I can
still see today:

- `aw-backend/src/config/aw.json.seed` carries **only 2** custom domains
  (`api.tekflox.com`, `www.tekflox.com`) and **zero** static sites. It is
  seed-only (applies to an empty DB), so it is not the live value — but it is
  the same stale snapshot that started the drift.
- **Nothing is currently guaranteed to be rewriting the main Caddyfile at
  all.** aw-backend's `regenerate_and_reload()` on the core-lease path
  (`src/api/app.py:1006`) never runs, because the container is pinned
  `AW_ROLE: "replica"` in `aw-stack/docker-compose.yml`. awserv is now a
  no-op. The only live write path is `POST /api/aw-config/reload`
  (`src/api/routes/app_builder.py:66` → `config_reload.reload_aw_config`
  → `regenerate_and_reload()` at `src/api/config_reload.py:118-124`), and it
  is **ungated**.
- **`PUT /api/settings/aw` does not reload Caddy.**
  `src/api/routes/settings.py:300-345` saves the config, bumps the warm
  generation, invalidates the cache, restarts docker components — and never
  calls `regenerate_and_reload`. Editing `custom_domains` there changes
  nothing at the edge, silently.

**Mandatory pre-flight, in this order, before the hostname goes live:**

1. Read the prod `workspace` row's `custom_domains` and compare it against
   the live on-disk Caddyfile at
   `/opt/agentic-workspace/data/tmp/caddy-conf/Caddyfile` (a **directory**
   mount — never turn it back into a file mount;
   `aw-stack/docker-compose.yml`'s aw-caddy volume comment explains the
   week-long stale-inode bug).
2. Run aw-backend's `render()` against the live config and **diff it
   byte-for-byte** against that file.
3. Proceed **only if** the diff is exactly the new `knowledgeable` block. If
   it drops any existing site block, **stop** — that is the 2026-09-05
   outage, reproduced.

Do **not** create a third writer. Do **not** flip `AW_ROLE` to hand over
ownership — that lease starts a dozen unrelated singletons (scheduler,
watchdogs, terminals, KB) that would collide.

Note that `aw-backend/src/tests/unit/libs/test_caddy_template_render_parity.py`
compares `render()` across both copies, so touching one turns it red — and it
injects synthetic configs, so it can only ever catch **code** divergence,
never the data divergence that actually caused the outage. Its green tick is
not the pre-flight.

### TLS

The existing `custom_domains` entries all declare `"tls": "route53"` (DNS-01).
Memory `caddy-route53-credential-is-dead` documented that credential failing
with `InvalidClientTokenId` on 2026-09-05 — but that memory's own RESOLVED
addendum (verified 2026-09-26) already found the credential working again.
**Confirmed a second time live on 2026-09-27, end to end, for a brand-new
hostname**, not just re-checked cert expiries on existing ones: adding
`knowledgeable.aw.tekflox.com` with `"tls": "route53"` and reloading produced
a real Let's Encrypt certificate (`issuer=Let's Encrypt CN=YE2`,
`notBefore=Sep 27 2026`, `notAfter=Dec 26 2026`), matching the pattern of the
7 pre-existing entries. **Route53 DNS-01 is the healthy, working path — do
not reach for on-demand TLS as a fallback.** The inversion to remember: it is
on-demand TLS that is unstable today (`docker logs aw-caddy` shows a retry
storm of `tls.on_demand … context canceled` on per-workspace
`*.workspace.aw.tekflox.com` names), not DNS-01. Use `"tls": "route53"` for
any new `custom_domains` entry unless a specific reason rules it out.

One transient artifact worth knowing about, not a fresh problem: CertMagic's
first DNS-01 attempt against the *production* ACME endpoint can fail with
"No TXT record found" if the TXT hasn't finished propagating (which is why
existing entries set `wait_for_route53_sync true`), and CertMagic reacts by
issuing a throwaway cert from the **staging** CA to keep the site TLS-live
while it retries — a log line mentioning `acme-staging-v02` during a renewal
is this self-healing behavior, not a sign DNS-01 is broken, as long as the
cert actually being served (check with `openssl s_client … | openssl x509
-noout -issuer`) is production Let's Encrypt shortly after.

Upstream: `proxy_host: aw-knowledgeable`, its own port. aw-caddy is on
`aw-stack-net`, `agentic-workspace_default` and `aw-workspaces-net` — the new
container on `aw-stack-net` is reachable by name, no fourth network needed
(confirmed live: `docker exec aw-caddy wget -qO- http://aw-knowledgeable:8090/api/health`
→ `{"status":"ok"}`).

### Verified live 2026-09-27 — M5 delivery

Ground truth at the moment `knowledgeable.aw.tekflox.com` went live, measured
on the bare-metal host, superseding the "hazard" framing above where it
disagrees:

- **The 2026-09-05 data divergence is fully converged, re-confirmed with a
  fresh byte-for-byte diff** (not inherited from the 2026-09-26 note): the
  `workspace` row's `custom_domains` (7 entries) rendered byte-identical
  (md5 `f57ef75ec344a902f9ae4d5feac5dffe`, 34083 bytes both sides) to the live
  on-disk Caddyfile, immediately before this change. The mandatory pre-flight
  is not a formality here — it is what made it safe to proceed.
- **The dry-run-then-real-write pattern worked as designed.** Rendering the
  proposed config in-memory (monkeypatching `get_config()` inside a throwaway
  `docker exec` Python process, no DB write) showed the diff was exactly the
  new `knowledgeable.aw.tekflox.com` block — 32 added lines, zero removed —
  before anything was persisted. Only after that dry-run confirmed a
  purely-additive diff was `save_config()` called for real.
- **The reload path is `save_config()` (writes the Postgres `workspace` row,
  drops the in-process cache) then `regenerate_and_reload()` from
  `aw-backend/src/libs/caddy_template.py`**, called directly inside the
  `aw-backend` container rather than through the HTTP routes — `PUT
  /api/settings/aw` and `POST /api/aw-config/reload` are the same two calls
  wrapped in FastAPI handlers behind `AuthMiddleware`, so calling the
  underlying functions in-process has an identical effect without needing to
  extract a production API key into an agent session. **This milestone's
  reload is manual** — nothing schedules or triggers it automatically; the
  `docker exec` above ran once, deliberately, by a human-supervised agent.
  Automating that trigger is out of scope here (target
  `caddy-ownership-consolidation`).
- **Result:** `custom_domains` now holds 8 entries. `curl -sf -o /dev/null -w
  '%{http_code}' https://knowledgeable.aw.tekflox.com/api/health` → `200`,
  backed by a real (non-staging) Let's Encrypt cert. All 7 pre-existing
  hostnames re-checked immediately after (`api.aw` via `/api/health` since
  the bare API root correctly 404s, `console.aw`, `agents-platform.aw`,
  `headscale.aw`, `derp.aw`, `api.tekflox.com`, `www.tekflox.com`) still
  served 200 with no interruption.
- **DNS is a non-issue for this hostname, confirmed as a wildcard, not an
  explicit record**: `dig +short` against `knowledgeable.aw.tekflox.com`,
  the bare apex, and an arbitrary nonexistent subdomain
  (`zzz-nonexistent-check-*.aw.tekflox.com`) all resolved to the same
  `65.109.66.88` — a `*.aw.tekflox.com` (likely with-apex) wildcard already
  covers any hostname under the zone, which is why this milestone never
  needed a DNS step.

### Rejected

- **`knowledgeable.app.aw.tekflox.com` under the wildcard.** Free cert, but
  it declares aw-knowledgeable an aw-workspace *app*, contradicting decision
  1, and it inherits the wildcard block's edge auth model
  (`auth_required`/cookie-presence regex) instead of owning its own gate.
- **A path prefix on an existing host** (`agents-platform.aw.tekflox.com/knowledgeable`
  or `api.aw…/knowledgeable`). Cheapest routing, but it couples the new
  service's availability to AP-MT's or aw-backend's, and a standalone service
  under someone else's hostname is not standalone.
- **Adding a generator, or a `custom_domains` writer, inside aw-knowledgeable.**
  Directly the thing that caused the 2026-09-05 outage.
- **Converging the config stores as part of this card.** It is the right fix
  and it is not this card's scope. Flagging it to the Product Owner as its own
  piece of work: *"aw-backend's `workspace` row vs the live Caddyfile —
  converge and prove, then make aw-backend the gated owner."*

---

## 5. Graph navigation UI

### Decision

**aw-knowledgeable ships its own frontend, built from source inside its own
image and served by its own FastAPI app** — the same shape AP-MT uses. Not an
extension inside aw-console.

### Where it lands

- `Dockerfile` — two stages, copied from
  `agents-platform-multitenant/Dockerfile`: stage 1 `node:22-slim` runs
  `npm ci` + `npm run build`; stage 2 copies `--from=frontend-build
  /fe/dist/`. Read that file's stage-1 comment before changing it: until
  2026-08-13 the image copied whatever `frontend/dist` happened to be in the
  build context, so "every frontend commit merged after that date was in
  master and NOT in production, and the deploy still reported success." Keep
  `frontend/dist` in `.dockerignore` for the same reason.
- `backend/app/main.py` — mirror AP-MT's `main.py:466-484`: mount `/assets`
  from `frontend/dist/assets`, SPA-fallback any unmatched path to
  `index.html`.
- `frontend/src/components/AuthGate.tsx` — ported per §3.
- API seams for the five requested features:
  - **Upload** — `POST /api/documents` (multipart). Bytes go to a volume (or
    an object store later); Neo4j gets a `(:Document {tenant, id, …})` node
    and a path reference. **Do not store bytes in Neo4j.**
  - **Graph view** — `GET /api/graph?focus=<id>&depth=1..2` returning
    `{nodes, edges}`. `focus` is **required**, by design: an endpoint that
    can return the whole graph is the thing that gets added by accident, and
    it is simultaneously the widest leak surface and the fastest way to hang
    the browser. Design it out now.
  - **Navigate between linked documents** — the same endpoint, re-focused.
    Force layout client-side.
  - **Create a manual link** — `POST /api/links {from_id, to_id, type}`,
    hitting the both-endpoints-tenant-matched `MERGE` template from §2. Must
    answer **404, not 403** when either id belongs to another tenant (K1's
    rule: a 403 confirms the id exists).
  - **See existing links** — part of the neighbourhood payload, plus
    `GET /api/documents/{id}/links`.
- Library: **`cytoscape` (npm), not `react-force-graph-2d`.** This paragraph
  originally recommended `react-force-graph-2d`; that recommendation is
  **withdrawn** — see §5a. The UX Coder built and Frederico approved a
  Cytoscape.js prototype, and Cytoscape is now a locked decision.

### Why its own frontend

- Decision 1 already says the service delivers value alone. A UI that only
  exists inside aw-console makes it undeliverable without a console release.
- aw-console is a **Next.js App Router** app (`repos/aw-console/src/app/` —
  `login`, `signup`, `plans`, `invitations`, `(app)`) whose scope is
  account/plans/workspace/login. A force-directed canvas, a file uploader and
  an entity-link editor are a different class of surface, and hosting them
  there couples every graph iteration to aw-console's own manual
  `workflow_dispatch` deploy.
- AP-MT's Dockerfile already encodes the hard-won frontend-build lesson.
  Reusing its shape gets that for free; re-deriving it inside aw-console
  does not.

### Rejected

- **An extension inside aw-console.** The genuine argument for it: one login
  surface, one nav, one deploy, the user never leaves the console. Rejected
  because the console would become a **hard dependency** of a service
  declared standalone, and because the console is the identity issuer's own
  UI — putting tenant graph data inside it widens what a console bug can
  leak. **Revisit if** Frederico later wants a single pane of glass; the
  right move then is a console nav entry that **links out**, not a port of
  the graph view.
- **A Tier-2 aw-workspace app window.** Blocked twice: decision 1, and the
  declarative widget vocabulary has no canvas primitive at all (memory
  `declarative-window-widgets-no-table`), plus app bundles cannot invent
  Tailwind classes (`app-bundles-cant-invent-tailwind-classes`).
- **Neo4j Browser / Bloom as the user-facing graph UI.** It is an operator
  tool with no tenant scoping whatsoever — exposing it would hand every
  tenant the whole database. Keep 7474 unpublished.

---

## 5a. Frontend stack — decision D1 (2026-09-26)

### Decision

**Vite in vanilla (no-framework) mode, bundling the approved prototype's own
`index.html` / `style.css` / `app.js` essentially verbatim, with `cytoscape`
as an npm dependency instead of a CDN `<script>`, and the AuthGate behaviour
re-expressed in plain JS.** The AP-MT *packaging* shape (Dockerfile stage 1,
`.dockerignore`, `/assets` mount, SPA fallback) is kept byte-for-byte; the
AP-MT *UI* stack (React/TSX/Tailwind) is not adopted.

So: AP-MT's build and serving shape, the prototype's code. React is the part
that gets dropped, not the part that gets kept.

### Where it lands (new repo `repos/aw-knowledgeable`)

| Path | Content |
|---|---|
| `frontend/index.html` | prototype `index.html`, `<body>` verbatim minus `#demo-controls`; the `cdn.jsdelivr.net/cytoscape` `<script>` and the bare `<link href="style.css">` replaced by one `<script type="module" src="/src/app.js">`. **`#app` starts hidden** — see risk 3. |
| `frontend/src/style.css` | prototype `style.css` **verbatim**, plus a `.logged-out-*` block for the gate screen |
| `frontend/src/app.js` | prototype `app.js` verbatim except: `import cytoscape from "cytoscape"`, `import "./style.css"`, `API_BASE = "/api/"` (was `"api/"` — the prototype is served under `/_frame/`), `api()` moved to `lib/api.js`, and the prototype-only `wireDemoControls()` / `state.demoFirstUploadFailed` / `sleep()`-faked upload progress deleted |
| `frontend/src/lib/api.js` | the prototype's `api()` helper + the 401 redirect. Behaviour ported from `agents-platform-multitenant/frontend/src/lib/api.ts:505-523` — **the `path !== "/api/me"` exemption is what prevents the redirect loop; keep it**, and keep the never-resolving promise so callers don't run error paths during navigation |
| `frontend/src/lib/auth.js` | the gate. Behaviour ported from `agents-platform-multitenant/frontend/src/components/AuthGate.tsx:10-27`: `fetch("/api/me")` directly (not through `api()`), 401 → render a logged-out screen with an explicit **Log in** anchor to `${CONSOLE_BASE_URL}/login?return=<encoded href>`; `Sign up` alongside it. Explicit click, no bounce |
| `frontend/package.json` | deps: `cytoscape` only. devDeps: `vite`. `"build": "vite build"` — **no `tsc -b`**, there is no TypeScript |
| `frontend/vite.config.js` | `build: { outDir: "dist", emptyOutDir: true }`, dev `server.proxy["/api"]` → the local backend, mirroring `agents-platform-multitenant/frontend/vite.config.ts` |
| `frontend/.gitignore` | `dist`, `node_modules` |
| `.dockerignore` | `frontend/dist`, **with the comment from `agents-platform-multitenant/.dockerignore:11-14` copied across**, because the comment is the load-bearing part |
| `Dockerfile` | stage 1 copied from `agents-platform-multitenant/Dockerfile:28-33` (`node:22-slim`, `npm ci`, `npm run build`) + `COPY --from=frontend-build /fe/dist/ frontend/dist/` (:71). Read that file's stage-1 comment (:5-26) before touching it |
| `backend/app/main.py` | mirror `agents-platform-multitenant/backend/app/main.py:466-484` — `/assets` mount + SPA fallback. Works unchanged: Vite's vanilla build emits `dist/index.html` + `dist/assets/<name>-<hash>.{js,css}`, and the `f.is_file()` branch (:481) covers anything dropped at `dist/` root |

The gate screen's markup is written in the prototype's **own** CSS vocabulary
(`.state-panel`, `.btn.btn-primary`, the `--bg`/`--accent` custom properties
in `style.css:6-33`) — **not** Tailwind. AuthGate.tsx's classes
(`AuthGate.tsx:33-52`) are not ported. This is the one place the design
authorises new UI rather than reuse, and the reason is that pulling Tailwind
in for a single screen is the same disproportionate dependency the UX Coder
rejected React for, just spelled differently.

### Why not React

`react-force-graph-2d` and the React port were both rejected, for different
reasons, and neither is re-open:

- **Port the two screens to React, keeping `cytoscape` direct** (the
  alternative that aligns with AP-MT). Rejected: it is a rewrite of a UI
  Frederico approved and asked to *reuse*. `app.js` is ~600 lines of
  imperative DOM wiring — `renderLibraryGrid`, `renderInspector`,
  `renderSearchResults`, the four-step upload machine
  (`showOnlyUploadStep`), the add-link modal's `linkState` — and every one of
  those becomes new code under React. The reuse would be the CSS and the
  markup only, i.e. the cheap half. Cytoscape also has to live behind a ref +
  `useEffect` regardless, so React buys nothing for the one screen that
  matters. **Revisit if** the graph view has to be embedded inside aw-console
  (§5's "revisit" clause) — that is the only scenario where React earns its
  cost, and it is a rewrite either way.
- **`react-force-graph-2d`** — already rejected by the UX Coder, with reason
  recorded: it brings React in disproportionately to the UX-Proto stack.
  (Also rejected there: vanilla force-graph — manual hit-testing; sigma.js —
  WebGL overkill; vis-network — weaker edge styling.)
- **No build step at all** (serve the three files statically, keep the CDN
  `<script>`). The most verbatim option, and genuinely tempting: zero changes
  to the prototype files, no node in the image, and the 2026-08-13 stale-dist
  trap cannot happen because nothing is built. Rejected on three counts, and
  the first is the disqualifying one: a live third-party script
  (`cdn.jsdelivr.net`) inside a page that renders tenant graph data means a
  CDN compromise reads the graph, and any tenant with restricted egress sees
  a blank canvas. Second, no lockfile and no SRI — `cytoscape@3.30.2` is
  pinned by tag only. Third, unhashed `app.js`/`style.css` filenames mean
  browser and edge caches serve stale JS after a deploy, which is the same
  "deployed and nothing changed" failure class as the bug in (c), arrived at
  from the other direction. Vendoring `cytoscape.min.js` into the repo fixes
  the first two and not the third, at the cost of a 1MB blob nobody updates.

### What this costs

See §7.5, §7.6 and §7.7 — this decision makes the "second SPA" debt concrete,
adds a duplicated auth gate, and drops build-time type checking.

---

## 6. What I could not verify from the code

Stated explicitly rather than assumed, per the card's instruction.

1. **Is aw-backend's T1 migration deployed in production? Code: yes. Board:
   stale. Rows: unverified.**
   - Commit `bda250a` *"feat(tenant): tenants + tenant_members +
     workspace.tenant_id, backfilled"* (2026-08-26) **is an ancestor of
     `origin/master`**.
   - The migration runs at boot in `src/api/pg_db.py:655-712`, not as a
     separate step.
   - `gh run list --workflow=deploy.yml` shows successful **Deploy
     aw-backend** runs on 2026-09-19 (×3), 09-21, 09-25 (×2) and
     **2026-09-26 09:02 UTC** — all after the commit. So the tables were
     created and the backfill ran.
   - The Kanban card *"T1: tenants + tenant_members + workspace.tenant_id no
     aw-backend"* (`3c85bf3b-9510-810f-85de-e43c0240f084`) is still in
     **Ready to Deploy**. The card is stale, not the code — worth moving.
   - **Not verified:** that the rows are actually populated. I have no read
     access to the prod database from this container. This blocks nothing
     here: nothing in this design reads those tables (see §2).
2. **The prod `workspace` row's `custom_domains` contents**, and therefore
   whether aw-backend's `render()` currently reproduces the live Caddyfile.
   Unreadable from here. This is the §4 pre-flight and it is the single
   highest-risk item in the document.
3. **Whether the live on-disk Caddyfile was last written by awserv
   (pre-2026-09-13) or by aw-backend.** Same pre-flight answers it: grep the
   file for a feature that exists only in aw-backend's copy (e.g. the
   `log access-json` global logger / the `on_demand_tls` block).
4. **Neo4j Community's exact multi-database and vector-index-prefilter
   behaviour on the version that ends up pinned.** My statements — one user
   database in Community, per-label rather than per-tenant vector indexes —
   are from general knowledge, not from a running server in this workspace.
   Both drive §2's core decision, so **confirm them against the pinned image
   before writing the isolation layer.** If pre-filtered vector search is
   available, the over-fetch in §2 becomes unnecessary.

---

## 7. What this makes harder later

1. **Tenant offboarding stops being a drop.** Property-based scoping in one
   shared database means a tenant's data can never be handed over as a file,
   exported wholesale, or deleted by dropping a database. Offboarding becomes
   a batched `MATCH (n {tenant:$t}) DETACH DELETE n` over a live graph. Write
   that job with the schema, not after the first request for it.
2. **Composite unique keys make a later tenant *merge* hard.** `(tenant,
   external_id)` is correct for isolation and it is exactly the shape that
   already bit this tree — memory `composite-unique-breaks-tenant-consolidation`.
   If two tenants ever have to be consolidated, this is the constraint that
   will have to be dropped and rebuilt.
3. **aw-stack's deploy becomes aw-knowledgeable's dependency.** The
   `services` input has to be picked correctly or the DB does not move, and
   the stack's deliberate "never implicit on push" rule now covers a service
   a feature team will want to iterate on. Expect friction; do not "fix" it
   by adding a push trigger to aw-stack.
4. **"One aw-backend user = one tenant" gets baked in.** A local projection
   keyed on `account_ref` reproduces the limitation aw-backend's own T1
   docstring criticises about AP: *"`Tenant.account_ref` is the
   counter-example — `unique=True` over a single `user_id`, so a multi-user
   tenant is not representable there at all"*
   (`aw-backend/src/api/db_models.py:1100-1111`). When T2 lands,
   aw-knowledgeable needs the same column removal AP-MT does. Keeping
   `resolve_tenant_id` as the single swap point is what keeps that a
   one-function change.
5. **A second SPA to keep coherent — and after §5a, a third *stack*, not just
   a third visual language.** aw-console is Next.js App Router, AP-MT and
   aw-workspace-ui are React/Vite/Tailwind, aw-knowledgeable is
   vanilla-JS/Vite with hand-written CSS custom properties. No component,
   design token or hook can be shared with either of the others in any
   direction. I am accepting this deliberately: the alternative was rewriting
   an approved UI, and a shared design system does not exist today to be
   diverged from. But the door it closes is real — if one is ever extracted,
   this is the codebase that cannot adopt it without a rewrite, and the same
   rewrite is the price of §5's "embed the graph view in aw-console" revisit
   clause.
6. **The auth gate now exists twice, in two languages.** `frontend/src/lib/
   auth.js` + `lib/api.js` are a hand translation of AP-MT's `AuthGate.tsx` +
   `api.ts:505-523`. A fix to one will not propagate to the other, and the
   subtle half — the `/api/me` exemption that prevents the redirect loop — is
   exactly the kind of thing that gets fixed in one place. Mitigation is
   cheap and must not be skipped: keep both files tiny and have each name its
   counterpart's path in a header comment, so the next person editing one
   knows a sibling exists.
7. **No TypeScript means the API contract is unchecked until runtime.**
   AP-MT's `npm run build` runs `tsc -b`, so a renamed backend field fails the
   build; here it fails in the browser, on the screen where it matters. A
   cheap floor — `// @ts-check` plus a `jsconfig.json` with `checkJs` — is
   worth adding, and is not a substitute for a Playwright smoke test over the
   library → graph → inspector path.
8. **The over-fetch vector search caps how large a single tenant can get**
   before recall for small tenants degrades measurably. If pre-filtering is
   unavailable on the pinned version, that ceiling is real and undocumented
   unless the seam logs when the floor is not met.

---

## 8. Risks for the Coders

1. **The vector index is the leak, and a property filter does not cover it.**
   `WHERE n.tenant = $t` on a `MATCH` says nothing about
   `db.index.vector.queryNodes`. K4 is the only test that catches it, and it
   needs a real Neo4j — a mocked driver will pass while production leaks.
2. **Start at `strict`.** AP-MT shipped `warn` and its acceptance test found
   48 GET routes handing one tenant's rows to another. A greenfield service
   has no retrofit debt; `warn` here would only create the same punch list
   voluntarily. Keep `off` as the documented rollback lever (env var +
   restart, not a deploy).
3. **The contextvar and the thread hop.** The Neo4j Python sync driver
   blocks. If `require_tenant` is a **sync** FastAPI generator dependency, the
   `tenant_scope` token is minted in one Context and unwound in another, and
   the value can stay bound in a **pooled worker** — where the next unrelated
   request reads another tenant's id. AP-MT hit exactly this and made
   `require_tenant` async for it; `tenant_ctx.py`'s `ValueError` branch is the
   scar. **Use the async driver, and make the dependency async.**
4. **`PUT /api/settings/aw` does not reload Caddy.** A Coder who adds the
   hostname through that endpoint sees a saved config and a dead domain, with
   nothing logged. The reload is `POST /api/aw-config/reload`, and it is
   ungated — which is also why it must not be called without the §4 diff.
5. **The route53 credential may be dead.** A new `custom_domains` host whose
   DNS-01 challenge cannot be answered never gets a cert, and the symptom
   reads as a routing bug. Verify before assuming the domain comes up.
6. **Disk before Neo4j.** Host was at 95% / 22G free on 2026-09-15 and
   creeping. `aw-workspace-cli doctor` does not check disk and will report
   green while the store hits ENOSPC. Cap transaction-log retention and page
   cache in compose before the first boot.
7. **`test_caddy_template_render_parity.py` will go red** if you touch only
   one `caddy_template.py` copy. Update it deliberately rather than silencing
   it — and do not read its green tick as evidence about the data.
8. **Neo4j Community is one user database.** Anything that reaches for
   `CREATE DATABASE` needs a licence decision from Frederico, not a
   workaround.
9. **AP-MT's dead `console_login_url`** (`backend/app/config.py:59`) is read
   by nothing. Don't port it as-is; either use it or leave it out. Shipping a
   third unused knob is how the next person concludes the redirect is
   backend-side when it is not.
10. **The prototype's API paths are not the paths §5 specified.** `app.js`
    calls `GET api/documents`, `GET api/graph?focus&depth`, `POST api/links`,
    plus `GET api/nodes/{id}`, `GET api/nodes/{id}/links` and
    `GET api/search?q=[&exclude=]`. §5 listed `documents/{id}/links`, not
    `nodes/…`, and listed no search endpoint at all. "Verbatim reuse" is only
    true if the real backend serves the **prototype's** paths — reconcile this
    against M6's transcribed `backend.py` before writing either side, and if
    they differ, the backend moves, not the frontend. Note also that
    `POST api/documents` in the prototype is **JSON**, while §5 specifies
    multipart; that one is a real change to `doUpload()` and the only place
    the frontend diff is more than cosmetic. And `GET /api/search` is a new
    tenant-scoped fanout read — it needs classifying in K1's
    `GET_ROUTE_PLAN`, and it must stay bounded by `q` so it never becomes the
    whole-graph endpoint §5 deliberately designed out.
11. **The gate must hide the shell, not just skip rendering it.** In React,
    `AuthGate` returning `null` means the children were never mounted. Here
    the entire app chrome is already in `index.html` before any JS runs, so a
    logged-out user sees the topbar, the search box and the empty-state flash
    before `/api/me` answers. Ship `#app` hidden and let the gate reveal it;
    do not leave this to the boot listener. Same trap in reverse: the
    prototype boots from `DOMContentLoaded` — that call has to move *behind*
    the gate, or the app fetches `documents` as an anonymous user and the
    first thing the gate screen sits behind is a 401 toast.
12. **Nobody has ever rendered this prototype.** The UX Coder's visual
    verification was blocked by an aw-ux-proto platform bug (card
    `3e75bf3b-9510-8164-983e-f380fd415d12`); Frederico approved the design as
    he saw it, not this code as executed. Budget for first-render defects and
    treat the first real load as discovery, not regression. One specific
    suspect: `mergeGraphData()` re-runs `cose` with `fit: false`
    (`app.js`'s layout call), so nodes added by an expand can land outside the
    viewport with no visible feedback — plausible, unverified.
13. **Two things not to "fix".** Do not delete the frontend build stage
    because "it's just static files" — that puts the CDN `<script>` back, see
    §5a. Do not add Tailwind for the gate screen — it is the dependency this
    decision exists to avoid.

---

## 9. Handoff

Ready for a Coder to break into cards. Suggested order, because each step
de-risks the next:

1. **aw-stack: `aw-neo4j` service + pinned image + disk caps + `NEO4J_AUTH`
   in the merged `.env`.** Nothing depends on it yet, so it is the cheapest
   thing to get wrong.
2. **New repo skeleton + `core/graph.py` seam + `core/tenant_ctx.py` +
   `strict` by default + K3/K5.** The isolation layer before any feature, so
   no feature is ever written against an unguarded driver.
3. **Phase A identity port + `/api/me` + `AuthGate` + K1/K2.**
4. **The §4 Caddy pre-flight, as its own card with the diff as its
   deliverable** — findings first, hostname second. If the diff is dirty,
   that card's output is a blocker for the Product Owner, not a workaround.
5. **Upload + neighbourhood graph endpoint + the graph view + K4.**

Two things to route elsewhere rather than absorb:

- **To the Product Owner:** the config-store convergence in §4 is a real
  piece of work that this card's scope does not grant, and it gates step 4.
- **To the UX Coder:** if the graph interaction should be designed before it
  is built, that is a UX-Proto prototype, not an architecture decision.

---

## 10. Decision D2 (2026-09-26) — the ingestion MCP connector's shape

Scope is the PO's and is not re-litigated here: the 6 tools (`upload_document`,
`create_node`, `create_link`, `get_graph`, `list_documents`, `search_nodes`)
are a 1:1 mapping over M6's endpoints, and the connector is an **HTTP client**
— no Cypher, no Neo4j driver, no knowledge of `core/graph.py`. Sequence is
M3 → M6 → this. Nothing below re-opens M3 or M6.

### Decision, in three parts

1. **Form: a Tier-1 (in-process) aw-workspace app**, new repo
   `repos/aw-app-knowledgeable`, installed to `apps/knowledgeable/`. It
   borrows `aw-app-whiteboard`'s Tier-1 discovery wiring and
   `aw-app-whiteboard`'s *standalone* MCP server's tool-body style — two
   different files in the same repo, and the split is the whole point (below).
2. **Reachability: inbound closes. Verified, not assumed.** The stdio-bridge
   inversion the PO flagged **does not trigger**; direct HTTP from the
   workspace/gateway to aw-knowledgeable works today by container name.
3. **Identity: the tenant comes from the connector's own credential, never
   from the calling agent.** `resolve_tenant_id` gains exactly one branch — a
   service-caller branch that already exists in AP-MT and is ported, not
   invented. **This is a BLOCKING prerequisite of M6**, and it is cheap.

---

### Q2 first, because it gates Q1 — reachability, measured

The PO was right to demand this before the form, and right that the socat
precedent looked threatening. It does not transfer, and the reason is a
topology fact that was not written down anywhere:

**`aw-host` — the Docker container the workspace runs inside — is itself on
aw-stack's Docker network.** Measured 2026-09-26:

- `remote_host_exec_run` on host `11e8bd4157845a24` ("aw-host", the default
  host for workspace `aw`) reports `hostname` = `74eea54e9090`, the presence
  of `/.dockerenv`, and `nameserver 127.0.0.11` — Docker's embedded DNS. Its
  own `/etc/hosts` self-entry is `172.22.0.9`.
- The aw-workspace container (`aw-remote-host-workspace`, 10.89.0.33:9030)
  and all ~70 app containers are **podman containers nested inside that
  Docker container**, on network `aw-remote-host` (10.89.0.0/24, gateway
  10.89.0.1 = aardvark-dns).
- aardvark-dns forwards unknown names upstream to aw-host's resolver, which
  is Docker's 127.0.0.11, which resolves aw-stack's containers. `nslookup
  aw-backend 10.89.0.1` returns a **non-authoritative** answer of
  172.22.0.8 — that forwarding hop, visible.

So the path is not "workspace host → another host's network". It is "child
netns → parent's own network". Two direct measurements, from the container
that actually matters:

```
# from INSIDE aw-app-mcp-gateway (10.89.0.12) — the container that would
# hold the upstream entry:
aw-backend               -> 172.22.0.8      HTTP GET :9025/api/health -> 200
aw-stack-aw-postgres-1   -> 172.22.0.3      TCP :5432 OPEN
```

**Why socat was needed for the monolith and is not needed here.** Memory
`aw-stack-postgres-cutover-done` records the constraint precisely: "The socat
target must be an **IP**. The aw-sandbox netns has resolv.conf forced to
1.1.1.1, killing Docker's 127.0.0.11 resolver — names don't resolve in
there." That is a property of **`aw-sandbox`** (the monolith's container),
not of the aw-workspace tree. The aw-workspace tree's resolv.conf points at
aardvark-dns, which forwards to 127.0.0.11 — the exact resolver aw-sandbox
had broken. The precedent is real and it is about a different container.

**The non-obvious part, and the actual deliverable for the Coders.** Name
resolution is **per-name, not per-network**. Probed from aw-host:

| resolves | does not resolve |
|---|---|
| `aw-backend` → 172.22.0.8 | `redis` |
| `aw-console` → 172.22.0.2 | `postgres` |
| `aw-caddy` → 172.22.0.5 | `aw-stack-redis-1` |
| `aw-redis` → 172.22.0.7 | `aw-stack-aw-console-1` |
| `aw-stack-aw-postgres-1` → 172.22.0.3 | `aw-stack-aw-caddy-1` |
| `aw-postgres` → 172.22.0.3 (alias) | `neo4j` |

Compose's default `{project}-{service}-{n}` names mostly do **not** resolve;
the `aw-*` names do, because aw-stack gives those services an explicit
`container_name`/alias. `aw-knowledgeable` resolves to nothing today, which
is correct — it does not exist yet.

**Therefore:** the aw-stack service MUST declare an explicit
`container_name: aw-knowledgeable` (or an equivalent network alias) on the
network aw-host is attached to. That is a one-line requirement, and it is the
difference between this design working and a Coder discovering
`getaddrinfo failed` after M6 is green. Do not configure the connector with
an IP — `aw-postgres` proves aliases work, and an IP would re-create the
brittleness the socat workaround had to accept.

Base URL for the connector: **`http://aw-knowledgeable:<port>`**. Config knob,
defaulted to that, for the same reason §3 deviation 1 defaults to
`http://aw-backend:9025` and not a loopback address.

**What would change this decision:** if aw-knowledgeable is ever placed on a
network aw-host is *not* attached to, or moved to a different physical host,
Q2 flips and the stdio-bridge becomes the only option. The verification above
is reproducible in one command — re-run it, don't assume it.

---

### Q1 — the form, and what actually survives of the kb/whiteboard pattern

The PO's warning is correct and sharper than it first looks. Both existing
`self_register.py` files are about **discovery**, not about calling a manager.
The "no HTTP hop" rationale lives in a different file —
`repos/aw-app-whiteboard/whiteboard_app/mcp/http_handler.py:14-18`:

> "this handler calls `WhiteboardManager`/`WhiteboardBrowser` **DIRECTLY** —
> no HTTP hop needed, it's the same Python process."

So the two concerns are already separated in the tree, and only the second one
fails to apply here. Splitting the pattern along that line:

**What survives, essentially verbatim:**

- `self_register.py` — `repos/aw-app-whiteboard/whiteboard_app/mcp/self_register.py:44-52`.
  Tier-1 shape: `socket.gethostname()` (not `127.0.0.1`, not kb's
  `AW_APP_SELF_HOST` — that env var is the Tier-2 variant, see
  `apps/kb/kb_app/self_register.py:36-37`), plus `X-Api-Key` in the entry's
  `headers` because Tier-1 routes sit behind IdentityGuard. Atomic
  `os.replace` write of `mcp.json`. This is 100% reusable and must not be
  re-derived — memory `app-mcp-needs-self-register-not-manifest` records that
  `contributes.mcp.provides` registers **nothing**; an app declaring only that
  installs clean, passes `doctor`, and serves zero tools silently.
- The Streamable-HTTP `/mcp` JSON-RPC 2.0 handler + `TOOLS_SCHEMA` + the
  `_tool_result` envelope — `http_handler.py`, `apps/kb/kb_app/mcp_http.py`.
  The wire protocol is what `HttpUpstream` speaks; none of it is manager-specific.

**What does not survive — and what replaces it:** the tool bodies. And the
replacement is also already in this tree, which is the find that settles the
form. `repos/aw-app-whiteboard/mcp_server/server.py:1-35` is *exactly* this
problem already solved once:

> "Talks to a running aw-workspace's OWN routes … over plain HTTP,
> authenticating with the workspace-wide `X-Api-Key` header … This is a
> STANDALONE process … it can run anywhere that can reach the workspace's API
> host."

Note its credential discipline, which the connector should copy: the key is
"**Read fresh on EVERY call (not cached at import time) so a regenerated key
takes effect without restarting this process**". The connector's own service
secret must be read the same way.

So the connector is: whiteboard's `self_register.py` + whiteboard's
`http_handler.py` skeleton + whiteboard's `mcp_server/server.py` **tool-body
style** (httpx against a remote base URL with a shared-secret header). Three
files that exist, recombined. There is no novel mechanism here.

**Tier-1 over Tier-2.** Reasons, in order of weight:

1. **Tier-2's isolation buys nothing here.** kb is Tier-2 because it owns
   pgvector/Postgres deps and heavy indexing. This connector's entire
   dependency surface is `httpx`, already in core. No DB table, no background
   loop, no managed service, no image to pin.
2. **Tier-1 ships on core's deploy path, not the marketplace.** That skips an
   entire documented failure class: `app-release-and-update-gotchas`,
   `marketplace-app-onboarding-gaps`,
   `marketplace-catalog-refresh-is-sequential-blocking`, and
   `aw-app-uc-phd-marketplace-update-reverts-to-stale-catalog` (a `--update`
   that silently reverts to a stale catalog). Frederico wants to use this the
   day it lands; a CDN-lag debugging session is a bad first experience.
3. **The service secret lives in the workspace secret store**, which a Tier-1
   app reads in-process.
4. `inprocess` is the house default — 24 of ~30 installed apps under `apps/`.
   §"prefer the shape the codebase already uses" applies.

**The cost I am accepting, explicitly.** Tier-1 code is not live until the
owning process restarts — memories
`tier1-inprocess-apps-have-no-restart-path`,
`aw-app-uc-phd-tier1-code-reload-needs-a-core-restart`,
`tier1-managed-service-code-is-not-live-until-the-owning-worker-restarts-it`
("service restart lies; check process etime"), and
`restarting-the-workspace-container-recreates-it-from-latest`. So
per-iteration turnaround is worse than Tier-2's `docker restart`. I accept it
because the tool surface is **locked at 6 tools with a fixed 1:1 contract** —
churn belongs on the aw-knowledgeable side, not here. **Runner-up: Tier-2.**
Switch if the connector turns out to need per-call iteration (e.g. M6's
contract proves unstable in practice), or if it ever grows a background
worker — at which point the WORKERS=10 hazards above become the deciding
factor rather than a cost.

**Rejected: the generic stdio-bridge** (`repos/aw-mcp-gateway/connector/`).
Not because it is unfinished — and here the code lies about itself, which the
Coders must know. `connector/link_client.py:5-8` still says *"STATUS:
skeleton matching the gateway side's current stub … a placeholder
bearer-token check, not the final `awlk_<id16>_<secret32>` scheme."* That
docstring is **stale**: the gateway side is done —
`back/gateway/remote_upstream.py:12-23` says it "Closes the
reverse-registration TODOs from the original skeleton", with a real `awlk_`
token verified against a `TokenStore`, scope globs, and collision handling,
and `remote_upstream.py:117` reads the same `?token=` query param
`link_client.py:29` sends. The bridge would very likely work. It is rejected
on cost and fit:

- Its reason to exist — "reached over HTTP without opening any inbound port"
  (`connector/__init__.py`) — is answered by Q2. Nothing needs to be opened.
- It would add a **new long-lived deployable** next to aw-knowledgeable on
  bare metal, plus link-token lifecycle, for zero capability gain.
- It spawns a **local stdio MCP child** (`main.py:16-17`, `local_mcp.LocalMcp`),
  so a stdio MCP server still has to be written *and* wrapped — strictly more
  code than the Tier-1 handler, not less.
- It namespaces tools `{workspace_name}__{app_name}__{tool}`
  (`remote_upstream.py:5-7`), diverging from the `aw__<app>__<tool>` form
  every agent and skill in this workspace already knows.

**Revisit if** Q2 ever flips, or if aw-knowledgeable is deployed somewhere
this workspace cannot dial — that is precisely the bridge's case, and it is
ready for it.

---

### Q3 — agent-originated identity: the load

**The honest starting point: there is no agent identity to authenticate.**
`repos/aw-mcp-gateway/back/gateway/caller_context.py:1-11` states it plainly:

> "An agent talks to this gateway, and the gateway talks to an app. The app
> therefore sees the *gateway* as its caller and has no way to tell which
> agent is on the other end."

What does arrive is a three-header allowlist —
`caller_context.py:78`: `("x-aw-caller-session-id", "x-aw-caller-run-id",
"x-aw-caller-agent")` — and the same file, at :76-77, says exactly what they
are worth: *"Keep this short and boring — **every addition is something a
caller can now assert about itself**."* They are **self-asserted
attribution**, not authenticated claims. AP-MT already wrote the trust rule
for the run-id, at `api/telegram.py:3090-3092`:

> "`run_id` is caller-supplied and **unauthenticated**: this grants **no read
> access**, it only selects a delivery **destination**."

Four of the six locked tools are reads (`get_graph`, `list_documents`,
`search_nodes`, and the read half of the others). **Binding a tenant from
`X-Aw-Caller-Run-Id` would therefore be precisely the bypass the PO
forbade** — worse than aw-backend's legacy authmiddleware, which at least
required *a* signed JWT. Ruled out.

#### The decision

**The tenant is a property of the connector's credential, not of the caller.**

This is not a new idea and that is its main virtue — AP-MT shipped it, and the
pieces are ported rather than designed:

- `agents-platform-multitenant/backend/app/core/identity.py:344-353` —
  `ServiceIdentity`, `SERVICE_USER_ID = -1`, deliberately **carrying no
  memberships** ("a service is not a member of anything, and a route that
  needs to scope by workspace must not silently read an empty list as *all*").
- `identity.py:356-369` — `verify_service_secret`: `X-Internal-Secret`,
  compared with `hmac.compare_digest` "so a wrong guess cannot be narrowed
  down by timing", returning **False rather than raising** when absent, so a
  caller falls through to identity auth.
- `identity.py:372` — `require_identity_or_service`, the composite dependency
  for "a person clicking in the SPA **or** a background producer with no
  session behind it".
- `identity.py:251-257` — the `resolve_tenant_id` service branch: service
  callers resolve to a **declared** ops tenant, "rather than the bootstrap
  tenant hard-bound".

So, in aw-knowledgeable:

- `backend/app/core/identity.py` gains `ServiceIdentity`,
  `verify_service_secret` (reading `KNOWLEDGEABLE_SERVICE_SECRET`) and
  `require_tenant_or_service`.
- `resolve_tenant_id` gains **one branch**, at the single swap point §2:226-228
  already designated: `if identity.user_id == SERVICE_USER_ID: return
  settings.service_tenant_id`. When T2 mints the real claim, this file is
  still the only one that changes.
- `backend/app/api/__init__.py`'s **one shared gate list** (§3) becomes
  `[Depends(require_tenant_or_service)]`. One list, both branches — not a
  per-route decision, for the reason §3 already cites: per-route gating is how
  card #5 shipped 48 GET routes handing one tenant's rows to another.

**Why this satisfies all three of the PO's constraints:**

- **(a) No second tenant concept.** It resolves to a row in the *same* local
  `tenants` projection §2:206-228 defines, keyed the same way. Identity still
  originates in aw-backend.
- **(b) `strict` is not weakened.** `strict` raises `UnscopedTenantAccess`
  when **nothing** is bound. Here a real tenant *is* bound, from an
  authenticated credential. The ladder is untouched; no new `off`-like escape.
- **(c) Not a "any workspace JWT" bypass.** The legacy aw-backend gate
  accepted any JWT as any identity. This accepts **one pre-shared secret**,
  constant-time-compared, and maps it to **one declared tenant**. There is no
  impersonation surface: a caller cannot name a tenant, so there is nothing to
  forge. The agent-supplied headers are recorded as **provenance only** (audit
  log / node `created_by_run` property) — never read to choose a tenant.

**Two guard-rails that are load-bearing, not polish:**

1. **`service_tenant_id` must have no silent default.** If
   `KNOWLEDGEABLE_SERVICE_SECRET` is set and `KNOWLEDGEABLE_SERVICE_TENANT_ID`
   is not, **refuse at boot**. AP-MT's own §1 structural finding is that a
   silent ops default is what let the bootstrap-tenant leak "survive three
   prior fixes" (`telegram.py:3093-3097`), and `resolve_tenant_id`'s docstring
   (`identity.py:237-244`) is a 7-line account of how a convenient fallback
   made isolation unfalsifiable: "every unknown caller landed in the owner's
   data."
2. **One credential, one tenant — enforced by shape.** Today this workspace is
   one account / one tenant (memory `ap-mt-one-account-one-tenant`), so a
   single secret is correct. But the mapping must be written as
   *credential → tenant*, so a second workspace gets a second secret rather
   than inheriting the first tenant. Getting this wrong reproduces exactly the
   bug AP-MT fixed in `1de307f`.

**Test, in the K-series' style — K6:** as a service caller with a valid
secret, writes land in `service_tenant_id`; with a **wrong or absent** secret,
every gated route 401s and **no** route falls back to an unscoped or default
tenant; and a call carrying a forged `X-Aw-Caller-Run-Id` naming another
tenant's run still resolves to `service_tenant_id`. That last assertion is the
one that would catch a future "helpful" refactor re-introducing header-derived
tenancy. Also extend §2's
`test_public_entrypoints_under_strict.py` port: the service path must work
under `strict`.

#### Blocking or separate card — the answer the PO asked for

**The seam is a BLOCKING prerequisite of M6. The connector that uses it is a
separate card.** Annotate M6.

The split:

- **Into M6 (blocking):** `ServiceIdentity` + `verify_service_secret` +
  `require_tenant_or_service` + the one `resolve_tenant_id` branch + the
  config knobs + K6. Roughly one file touched, one dependency, two config
  values, one test file.
- **Separate card (after M6):** the Tier-1 app itself — repo, manifest,
  `self_register.py`, `/mcp` handler, 6 tool bodies, secret provisioning.

**Why blocking rather than "add it when we build the connector":** M6 ships
the **one shared gate list**. Retrofitting a second auth branch into an
already-shipped gate list is done per-router by whoever needs it, and §3
already documents where that ends — 48 routes. Adding the branch while the
list has one entry is a one-line change; adding it later is an audit.

**On the PO's escape hatch — it does not trigger.** This is nowhere near the
cost of M3+M6. It is a port of four small, already-written, already-reviewed
functions from a sibling repo into a file M6 is creating anyway, plus one
test. My estimate is a small fraction of M6, not a multiple of it. **No scope
finding to return**; the granted scope is right.

---

### What D2 makes harder later (extending §7)

9. **The graph cannot attribute a node to an agent in a trustworthy way.**
   Provenance comes from self-asserted headers, so `created_by_run` is a hint,
   not evidence. If an audit trail ever needs to be authoritative — "which
   agent inserted this claim?" — that requires per-agent credentials, which
   the gateway has no concept of (`caller_context.py:1-11`). Do not let the
   provenance property quietly become load-bearing for a reasoning feature.
10. **One service credential means agent writes are indistinguishable from
    each other at the tenant boundary.** Revoking one misbehaving agent's
    access means rotating the single secret, i.e. cutting off every agent.
    Acceptable at one tenant and one workspace; it is the first thing to hurt
    if aw-knowledgeable is ever offered to a second tenant whose agents call
    through their own gateway.
11. **Tier-1 couples the connector's release to core's.** A connector fix
    ships on the workspace-core deploy path and needs the owning process
    restarted — so a trivial tool-description typo is a core deploy. This is
    the concrete form of the Tier-2 runner-up's advantage; it is the
    trigger to revisit, not a reason to pre-emptively split.
12. **The reachability finding is a measurement with a shelf life.** It rests
    on aw-host being a container on aw-stack's own network — an arrangement
    nothing enforces and no test covers. If aw-stack's networking is ever
    reorganised, this connector breaks with a DNS error and the design note
    above is the only record of why it used to work.

### Risks for the Coders

1. **`contributes.mcp.provides` registers nothing.** The gateway only finds an
   upstream by scanning for the file `self_register.py` writes. Declaring the
   6 tools in `aw-app.json` and stopping there yields an app that installs
   clean, passes `aw-workspace-cli doctor`, and serves **zero tools with no
   error anywhere**. Memory `app-mcp-needs-self-register-not-manifest`.
2. **A new gateway tool is invisible to the session that created it.** Memory
   `verify-new-gateway-tools-in-same-session` — verify from a *fresh* session,
   and expect the reload/zombie-cache behaviour in
   `mcp-gateway-http-upstream-zombie-caching`.
3. **`upload_document` is multipart through two hops.** The gateway's
   `HttpUpstream` speaks JSON-RPC; bytes have to be carried as a
   base64 argument and re-encoded as multipart by the tool body. Do not
   assume a file path is shareable — the agent container, the workspace
   container and aw-knowledgeable have three different filesystems. Memory
   `remote-host-download-file-mcp-misses-the-shared-tree` is the same trap.
   Also: the tunnel edge cuts requests at 30s (`tunnel-edge-cuts-requests-at-30s`)
   — a large ingest must not be a synchronous call that returns the finished
   parse.
4. **`StdioUpstream` injects `_gateway_caller_run_id` into tool arguments.**
   Memory `gateway-injects-caller-run-id-into-notion-calls`: a strict schema
   rejects the unexpected field with a 400. M6's request models must tolerate
   (and ignore) an unknown underscore-prefixed key, or the connector must
   strip it before forwarding.
5. **Read the service secret fresh on every call**, per
   `mcp_server/server.py`'s rationale — a rotated secret must not require a
   core restart to take effect, because on Tier-1 that restart is expensive.
6. **Do not test reachability from an agent container and call it done.** An
   agent runner and `aw-app-mcp-gateway` are different containers; the
   upstream entry is used by the gateway. Verify from inside
   `aw-app-mcp-gateway`, as §10's Q2 did.
