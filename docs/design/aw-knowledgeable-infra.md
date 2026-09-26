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

The five existing `custom_domains` all declare `"tls": "route53"` (DNS-01).
**Memory `caddy-route53-credential-is-dead` says that credential may no
longer work.** Verify a DNS-01 challenge can still be answered *before*
assuming the new host will get a cert; otherwise use the on-demand-TLS path
the per-workspace blocks now use (`on_demand_tls { ask … }`,
`aw-backend/src/libs/caddy_template.py:758-759`). A new site whose cert never
issues looks like a routing bug and is not one.

Upstream: `proxy_host: aw-knowledgeable`, its own port. aw-caddy is on
`aw-stack-net`, `agentic-workspace_default` and `aw-workspaces-net` — the new
container on `aw-stack-net` is reachable by name, no fourth network needed.

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
- Library: recommend `react-force-graph-2d` (canvas-based, comfortable into
  the low thousands of nodes) over a hand-rolled d3-force or Cytoscape. That
  is a craft call, not an architecture one — if Frederico wants the
  interaction designed before it is built, that is the **UX Coder's** lane
  (UX-Proto prototype first), not mine.

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
5. **A second SPA to keep coherent.** Own frontend means a third visual
   language beside aw-console and aw-workspace-ui, with no shared design
   system to anchor it. That divergence is permanent unless someone extracts
   one; naming it now is cheaper than discovering it at the third screen.
6. **The over-fetch vector search caps how large a single tenant can get**
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
