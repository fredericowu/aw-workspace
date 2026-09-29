# AP-MT `/v1` user-identity arm — delegated, narrowed tokens

Architect design, 2026-09-29, card
`architecture:apmt-knowledgeable-user-identity-delegation`
(`3ea5bf3b-9510-81e3-9197-f2f5d94b12ba`). Design only — no code in this
pass; for Frederico to review.

The idea, verbatim (Frederico, Telegram, 29/09):

> "Deveria ter uma relação de confiança entre o ap-mt e o knowledgeable por
> conta do jwt do usuário, talvez o próprio jwt poderia carregar algum token
> extra caso necessário."

Sibling designs this composes with, not duplicates:

- **T2** (`/opt/aw-workspace/.tmp/design/t2-tenant-jwt-claim.md`, target
  `aw-backend-t2-tenant-claim`): the tenant/role claims and the single mint
  seam (`aw-backend/src/api/identity_claims.py`) **already exist on master**
  (commits `b0da528` + `5aae5e1`, CI green, **not yet deployed** as of
  2026-09-29). This design reuses that seam; it does not invent a second
  place where claims are born.
- **§12** of `aw-workspace/docs/design/aw-knowledgeable-v2-retrieval.md`
  (`:1845-1861`): the Playground's hybrid fork was rejected on identity
  grounds, and the rejection names its own unlock condition — *"per-call
  user-scope propagation through gateway → app → backend; that is a
  project, not a card"*. This document is the design of that project's
  first, load-bearing slice.

---

## 0. Verified state of the code (the facts this design stands on)

All re-verified 2026-09-29, not inherited from the card:

1. **AP-MT's `/v1` has exactly one auth arm, and it is opaque.**
   `agents-platform-multitenant/backend/app/api/openai_compat.py:79-115`
   (`require_api_key`) strips `Bearer `, looks the string up in `api_keys`
   (`_resolve_api_key`, `:54-76`, deliberately `CROSS_TENANT` because the
   token is what identifies the tenant). A JWT presented there is a failed
   row lookup → 401. There is no JWT arm on that surface.

2. **`ApiKey` carries two things, and both must survive.**
   `backend/app/models.py:2013-2048`:
   - the **tenant bind** — `require_api_key` runs the whole request under
     `tenant_scope(key.tenant_id)` (`openai_compat.py:114`); its docstring
     records that this half was once missing and was a real isolation leak
     (a slugless key could address every tenant's agents; `GET /v1/models`
     enumerated all of them);
   - the **slug scope** — `_check_scope` (`:118-123`): `agent_slugs`
     non-empty ⇒ allowlist; empty ⇒ unrestricted.
   The S4 comment (`models.py:2026-2040`) pins the invariant: the token is
   globally unique **because the tenant is derived from the credential**.
   Any second arm must preserve that direction of derivation.

3. **AP-MT already verifies aw-backend identity JWTs** — for its browser
   surface. `backend/app/core/identity.py:85-121`
   (`decode_identity_token`, EdDSA against aw-backend's JWKS, cached) and
   `require_identity` (`:124-143`). The new arm needs no new cryptography;
   it needs a new *narrowing discipline* on top of machinery that exists.

4. **A human's tenant in AP-MT is a local projection, not the claim.**
   `core/identity.py:264-267` maps `sub` → `Tenant.account_ref`;
   `_mint_tenant_for` (`:270-304`) mints on first sight. T2's claims are
   not read by AP-MT anywhere yet — that is the explicitly deferred **T2b**
   (T2 design §6.1).

5. **The raw workspace JWT is a broad bearer.** aw-backend's legacy
   `AuthMiddleware` (`aw-backend/src/api/app.py`, `_legacy_membership_ok`)
   accepts any identity JWT claiming membership in the legacy workspace —
   whiteboards, terminals, docker, devctl, git ops, KB, meta_display — and
   whole route families sit behind bypass prefixes with no gate but their
   own `Depends`. The token lives **30 days** (`identity_auth.py:38`,
   raised from 24h deliberately). Whoever holds it can be its subject,
   nearly everywhere, for a month.

6. **The Playground is live but paused in production** on
   `paused_reason=secret_unavailable`: `backend/app/api/playground.py`
   authenticates to AP-MT with a vault-stored `ApiKey`
   (§12 "Where it lands"), and production aw-knowledgeable has no vault
   link to read it.

7. **The §12 identity mismatch, precisely:** the MCP connector
   authenticates as `ServiceIdentity()`
   (`aw-knowledgeable/backend/app/core/identity.py:144-154`, via
   `X-Internal-Secret`, `:197-210`) and resolves to
   `KNOWLEDGEABLE_SERVICE_TENANT_ID` (`:268-269`) — a *different tenant*
   than the human's. An agent refining retrieval mid-run reads the graph as
   a different principal than the seeded retrieval, so §7bis's per-token
   bucket boundary cannot hold across one answer. That is why §12 chose
   closed-book (b2).

---

## 1. Decision 1 — a second arm on `/v1`, dispatched structurally, behind one caller seam

`/v1` gains a **user-identity arm** next to the ApiKey arm. One dependency,
`require_v1_caller`, replaces `Depends(require_api_key)` on all five `/v1`
routes (`openai_compat.py:566, :588, :606, :683, :748`) and yields a small
`V1Caller` value that both arms map into:

- `tenant_id` — what gets bound via `tenant_scope`, exactly where
  `require_api_key` binds it today (`:114`);
- `allowed_slugs` — the allowlist `_check_scope` and `list_models` read
  (`:118-123`, `:567`);
- `kind` (`"service" | "user"`) + attribution (`user_id` for the user arm,
  key id for the service arm) — so runs stop being attributable only to a
  shared service key.

**Dispatch is structural:** a presented credential containing two dots is a
JWT; anything else is an opaque key. `ApiKey.token` is
`secrets.token_urlsafe(32)` (`api/api_keys.py:53`) whose alphabet
(`A-Za-z0-9_-`) cannot contain a dot, so the discriminator is sound — pin
it with a test on the keygen, not on faith.

The routes themselves do not change shape: `_check_scope` keeps its
semantics (empty = unrestricted **only for the service arm** — see D4),
`list_models` keeps filtering, streaming keeps working because the
dependency stays an async generator (the contextvar trap documented at
`openai_compat.py:101-103` and in aw-knowledgeable's
`require_tenant_or_service` applies to the new arm identically).

## 2. Decision 2 — the credential is an aw-backend-minted **delegation token**, not the session JWT

Frederico's "token extra" intuition is right, and the right form for it is
a **derived, narrowed token minted by aw-backend on request** — the
token-exchange *shape* (present a subject token, receive a narrower one)
without the OAuth grammar.

### The endpoint

`POST /api/identity/delegate` on aw-backend, in the `/api/identity/*`
route family — which is already routed around the legacy `AuthMiddleware`
and gated solely by `require_identity`
(`aw-backend/src/api/identity_guard.py` module docstring). Request:

```json
{ "audience": "apmt-v1", "agent_slugs": ["knowledgeable-playground"], "ttl_seconds": 300 }
```

- `audience` must be in a small config allowlist (`{"apmt-v1"}` to start) —
  the registry of "services you can be delegated to" lives in one place;
- `agent_slugs` must be a **non-empty** list — an unrestricted delegation
  defeats the entire point and is refused at mint;
- `ttl_seconds` clamped: default **300**, max **900**.

### The token

Same Ed25519 key, same JWKS, verified by the same `decode` machinery:

```json
{
  "sub": "11",
  "aud": "apmt-v1",
  "token_use": "delegation",
  "agent_slugs": ["knowledgeable-playground"],
  "tenant": "tenant-…", "tenant_role": "owner",
  "tenants": [{"tenant": "…", "role": "owner"}], "aw_claims_v": 1,
  "iat": 1769000000, "exp": 1769000300
}
```

- **No `memberships` claim, deliberately.** The `/v1` arm doesn't need it,
  and its absence is a second, independent wall: even a verifier that
  somehow ignored `aud` would fail this token at
  `_legacy_membership_ok`, which requires a legacy-workspace membership.
- The tenant claims come from **T2's existing mint seam** — a new
  `identity_claims.mint_delegation_token(...)` beside
  `mint_identity_token`, so the T2 import guard ("only `identity_claims`
  imports `create_identity_jwt`") stays intact;
  `create_identity_jwt` grows whatever keyword plumbing that needs, and
  nothing else. Where the claim is born was decided by T2; this design
  only adds three narrowing claims next to it.
- Precedents in-tree for "aw-backend mints a short, purpose-built identity
  token": the 60s AP-MT delivery token
  (`ap_identity.py:35, :64-96`) and the ws-ticket
  (`routes/identity.py:241`). Both narrow **TTL only**. The delivery
  token's lesson is exactly why TTL is not enough: for its 60 seconds it
  is a full identity JWT, accepted everywhere one is. `aud` is what those
  precedents were missing.

### Rejected alternatives for D2

- **Extra claim inside the session JWT itself** (the literal reading of
  the idea). The session JWT is a 30-day bearer that rides in a cookie to
  every subdomain and is accepted across the legacy surface (fact 5). A
  capability embedded in it is held by every holder of the session token,
  for the token's whole life, revocable only by waiting — that is zero
  narrowing. It also forces aw-backend to know at *login time* which
  services a user will delegate to. Rejected.
- **Forward the raw JWT to `/v1`.** The confused deputy the card names:
  aw-knowledgeable (and AP-MT's logs, DB, and anything between) would
  hold a credential that acts as the user against everything in fact 5,
  for up to 30 days. Rejected — this is the failure mode the whole design
  exists to avoid.
- **AP-MT mints its own narrowed token** (aw-knowledgeable asks AP-MT to
  exchange). AP-MT becomes a second identity issuer with its own signing
  key and its own JWKS; every consumer gains a second trust root. Identity
  minting belongs to aw-backend — one key, one JWKS, one revocation story.
  Rejected.
- **Full RFC 8693 / OAuth token exchange.** There is no OAuth AS, no
  client registry, no `client_id` to authenticate deputies with (see D3's
  honest limit). Adopt the shape, skip the framework. Rejected as a
  framework, borrowed as a pattern.

## 3. Decision 3 — containment by `aud`, and it is the *library* that enforces it

The load-bearing mechanism, verified empirically (PyJWT **2.12.1**, this
workspace, 2026-09-29):

- A token **carrying** `aud`, decoded by a verifier that passes no
  `audience=` → `InvalidAudienceError`.
- A token **without** `aud`, decoded by a verifier that pins
  `audience="apmt-v1"` → `MissingRequiredClaimError`.

Every existing verifier in the fleet is in the first category — none
passes `audience=`:

- aw-backend `identity_auth.py:184` (`decode_identity_jwt`, which both
  `require_identity` and the legacy `AuthMiddleware` path go through),
- AP-MT `core/identity.py:105`,
- aw-knowledgeable `core/identity.py:116`.

So the delegation token is **rejected by every surface that exists today,
automatically, with zero changes to those verifiers** — aw-backend's
dashboard and legacy families, AP-MT's browser API, aw-knowledgeable's own
API. And the pinned decoder on the new `/v1` arm rejects every session
token symmetrically. The narrowing is not a convention every verifier must
remember; it is PyJWT's own `aud` semantics.

The `/v1` arm still verifies **belt-and-suspenders**, not library-default
only: `aud == "apmt-v1"` *and* `token_use == "delegation"` *and*
`agent_slugs` non-empty, each failure a plain 401. `token_use` exists so
that when a second audience is ever added for another service, a
session-shaped token can never be confused with a delegation even by a
verifier written carelessly.

**The honest limit (named, not hidden):** the exchange narrows *what the
forwarded artifact can do*, not *who can obtain one*. Anyone holding the
session JWT — which includes aw-knowledgeable, because the browser already
sends it the `aw_id_jwt` cookie for its own auth
(`core/identity.py:162-166`) — can call `/delegate`. Authenticating the
deputy itself (an RFC-8693-style `act` claim, requiring the exchanging
service to present its own credential) needs services to *have*
credentials toward aw-backend; production aw-knowledgeable today has none
(no `AW_WORKSPACE_HOST_TOKEN` — the same gap that pauses the Playground).
That hardening is deliberately deferred to the hybrid phase (§9), where
the deputy chain gets longer and the claim starts paying rent.

## 4. Decision 4 — authorization: a `user_invocable` flag on the Agent/Workflow row, default **false**

The JWT says who the person is; nothing about it says which agents they
may run. Today `ApiKey.agent_slugs` is that answer. For the user arm the
answer becomes:

**A boolean `user_invocable` column on `Agent` (`models.py:219`) and
`Workflow` (`models.py:347`), default `false`, tenant-scoped because the
rows already are, enforced in the same place `_check_scope` runs.** For a
`kind="user"` caller, invoking a slug requires **both**:

1. the slug is in the token's `agent_slugs` (the deputy's narrowing —
   which agent this delegation was minted for), **and**
2. the target row has `user_invocable = true` (the tenant's authorization
   — which agents humans may run at all).

Two independent axes: (1) is chosen per-call by the client at mint; (2) is
policy, set by the tenant on the resource, revocable instantly with no
token anywhere re-minted. `GET /v1/models` under the user arm lists only
flagged rows. Default-false means shipping the arm grants **nothing**
until someone flips a flag — the Playground rollout is exactly two flips
(`knowledgeable-playground`, `knowledgeable-explorer`).

Migration is the house style: `create_all` for fresh databases plus a
hand-rolled idempotent `ALTER` for existing ones (there is no alembic in
this stack; `db.py`'s existing `_apply_*_migration` functions are the
pattern).

### Rejected alternatives for D4

- **Allowlist in the claim** (aw-backend mints invocable slugs into the
  token). aw-backend becomes a per-app resource ACL — the exact thing T2
  §5 and §7bis Decision 3 already rejected for bucket scopes:
  *"authorization over your own resources is yours."* Creating an agent
  would become a change to the identity service; the list would be stale
  for the token's life; revocation = wait. Rejected on precedent that is
  already written down twice.
- **Inherit from `TenantMember` role** (`tenant_role` claim). Role says
  who you are in the tenant, not which agents are expensive or
  privileged; it collapses to "every member runs everything or nothing",
  and a promotion in the tenant would silently widen someone's invocation
  surface. Role belongs to a different question — *who may flip the
  flag* — which the existing admin surface already answers. Rejected.
- **A separate tenant→slugs allowlist table.** Semantically identical to
  the flag but stored away from the resource: it drifts on agent
  rename/delete (slug strings, no FK — `ApiKey.agent_slugs` already
  demonstrates the maintenance shape), and it needs a new admin surface,
  where the flag lands in the agent editor that exists. Rejected.
- **A per-user registry (user → invocable agents).** The most precise
  option and the eventual evolution if "this agent for some humans only"
  becomes real. Today's need (§12: two agents, every tenant human) doesn't
  justify a new table plus management UI — and because the flag is
  default-deny, the registry can later be added as a *further* narrowing
  without unwinding anything. Deferred, not rejected forever.

## 5. Decision 5 — the user arm resolves its tenant through AP-MT's **existing projection**, not the claim

The delegation token carries T2's `tenant` claim, but the `/v1` user arm
**must not read it for scoping yet**. It resolves `sub` through the same
`resolve_tenant_id` / `_mint_tenant_for` path the browser surface uses
(`core/identity.py:264-304`), then binds `tenant_scope` exactly as
`require_api_key` does.

Why: AP-MT's canonical tenant for a human is the local projection (fact
4). The Playground's ApiKey rows, every run the user's browser can see,
and every tenant-scoped row were all created under projection tenants. An
arm that scoped by the claim would land runs in a tenant id **different
from the one the same user's dashboard resolves to** — invisible runs,
indistinguishable from a broken deploy. Switching AP-MT from projection to
claim is **T2b** (T2 design §6.1), a whole-app cutover with its own
30-day-tail discipline; a single endpoint must not front-run it.

The claim is still *carried* and should be **logged when it disagrees with
the projection** — that WARN stream is exactly the observability T2b will
need before it can flip, the same "the log is what makes the tail
observable" move as T2 §4.

Rejected: requiring the `tenant` claim at mint or at verify. T2's rule is
"absence is never a default, fall back and log" — `mint_identity_token`
already degrades to claimless rather than raising (`ap_identity.py:87-88`
records why), and the arm resolves by `sub` regardless. Requiring it would
couple this feature to T2's deploy date for no behavioural gain.

## 6. Decision 6 — how the two arms coexist without the weaker becoming a bypass of the stronger

The service credential does not disappear. It is the **only** path for
callers with no human in the loop — scheduled tasks, the MCP connector,
Roblox, curl — and stays byte-for-byte unchanged. The containment matrix,
each cell enforced by something named above:

| credential ↓ presented at → | AP-MT `/v1` user arm | AP-MT `/v1` service arm | AP-MT browser API | aw-backend (legacy + identity) | aw-knowledgeable API |
|---|---|---|---|---|---|
| session JWT | **401** (no `aud` → `MissingRequiredClaimError`) | 401 (not a key row) | accepted (today, unchanged) | accepted (today, unchanged) | accepted (today, unchanged) |
| delegation token | accepted iff slug ∈ token ∧ flag ∧ TTL | 401 (not a key row) | **401** (`aud` → `InvalidAudienceError`) | **401** (`aud`; also no membership) | **401** (`aud`) |
| ApiKey | n/a (no dots → key arm) | accepted, unchanged | 401 | 401 | 401 |

Directionality checks:

- **Service → user:** an ApiKey grants no user identity; runs stay
  attributed to the key. Unchanged posture.
- **User → service:** a human whose target agent is not `user_invocable`
  cannot reach it through the user arm regardless of what their token
  names. The remaining route around the flag is *minting an ApiKey* — and
  that is a real hole today: `POST /api/api-keys`
  (`api/api_keys.py:52`) has **no role gate**, so in a multi-person
  tenant (T2's whole premise) any member can mint an unrestricted key and
  bypass the flag entirely. **This design requires key creation to be
  gated on tenant admin/owner** before the user arm's authorization story
  is honest in shared tenants. Today it must gate on what AP-MT can see
  (membership role via `/api/me`-style live lookup); when T2b lands it
  reads `tenant_role`. Named as a hard prerequisite for multi-person
  tenants, acceptable as a fast-follow while every tenant has one human.
- **Weakest link, stated:** both arms end in the same executor with the
  same powers. The arms differ in *who can hold the credential* and *what
  it names*, not in what a permitted run can do. Sandboxing an agent's
  runtime is the `permissions` column's job (`models.py:275`), out of
  scope here.

## 7. Where it lands

**aw-backend** (after T2 deploys — the mint seam must be live first):

- `src/api/routes/` — new `identity_delegate.py` (or a route in
  `routes/identity.py`): `POST /api/identity/delegate`,
  `Depends(require_identity)`, audience allowlist + TTL clamp from config.
- `src/api/identity_claims.py` — `mint_delegation_token(user_id, *,
  audience, agent_slugs, ttl_seconds)`; the T2 import guard stays green.
- `src/api/identity_auth.py` — `create_identity_jwt` grows the keyword
  plumbing for `aud`/`token_use`/`agent_slugs` (emit-only-when-present,
  same shape T2 used); the module stays DB-free.

**agents-platform-multitenant:**

- `backend/app/core/identity.py` — `decode_delegation_token(token)`:
  pinned `audience`, `token_use` + `agent_slugs` checks; sibling of
  `decode_identity_token`, sharing the JWKS cache.
- `backend/app/api/openai_compat.py` — `require_v1_caller` (structural
  dispatch → the two resolvers → one `V1Caller`); `_check_scope` and
  `list_models` read the caller, plus the `user_invocable` test for
  `kind="user"`; attribution: the user arm feeds `sub` into the existing
  `_upsert_caller_identity` path (`:625`) instead of trusting
  `X-Caller-Meta-*` headers.
- `backend/app/models.py` + `db.py` — `user_invocable` on `Agent` and
  `Workflow` + idempotent migration; exposed in the agent/workflow CRUD.
- Tests: the PyJWT containment pair (aud-token vs plain verifier, plain
  token vs pinned verifier) pinned as *tests, in this repo* — the design's
  single most load-bearing behaviour must not rest on a library default
  nobody asserts. Plus: dispatch discriminator (keygen never emits dots),
  flag default-deny, tenant = projection not claim, expired-token 401.

**aw-knowledgeable:**

- `backend/app/api/playground.py` — the AP-MT call swaps the vault ApiKey
  for: delegate (`POST {aw_backend}/api/identity/delegate`, presenting the
  caller's own bearer — the same token `require_identity` just verified)
  → call `/v1/chat/completions` with the delegation token. The vault-key
  code path stays as the configured fallback until Frederico retires it —
  it is also the second arm's reference implementation for headless use.
- No change to `core/identity.py` in this card: aw-knowledgeable does not
  *accept* delegation tokens yet (that is the hybrid phase, §9).

**ap-mt data (no repo change):** flip `user_invocable` on
`knowledgeable-playground` and `knowledgeable-explorer` once the arm is
live.

## 8. Explicitly not covered — and that is the point

This arm covers exactly one class of caller: **a request with a live human
behind it, whose browser session is the root credential.** Everything else
— scheduled/cron paths, the MCP connector's ingestion identity, Roblox
NPCs, curl, service-to-service alerts — has no subject token to exchange
and **stays on the ApiKey arm**, which this design leaves untouched. Two
arms, two caller classes, one executor; D6 is the contract that keeps the
boundary honest.

Sequencing with the Playground's production pause: the vault-key path
(service arm) is still the fastest unblock and needs its own decision
(prod aw-knowledgeable has no vault link — separate card). This design is
the *right* path, not the *fast* one; shipping the fast one first does not
change anything here, because the service arm survives regardless.

## 9. The product gain — why this is worth an auth change in AP-MT's highest-blast-radius file

1. **It unlocks the §12 hybrid fork.** §12 rejected "agent refines
   retrieval mid-run" because the refinement would read the graph as
   `ServiceIdentity` in the wrong tenant, and no enforcement point could
   clamp it. The delegation token is the missing artifact: minted for the
   user, bound to the agent, short-lived — AP-MT can hand it to the run,
   and the agent's `search_graph` calls back into aw-knowledgeable can
   present *the user's own narrowed identity* instead of
   `X-Internal-Secret`. Then the agent searches as the person, iterates
   strategy itself, and §7bis bucket scoping holds across every hop.
   What the hybrid phase still needs (a follow-up design, not this one):
   `aud` as a chain (`["apmt-v1", "knowledgeable-v1"]`) with
   aw-knowledgeable growing its own pinned decoder on the search routes;
   TTL rethought for mid-run admissions (a run outlives 300s; re-exchange
   or run-scoped tokens); the `act` claim from D3. The first slice (this
   design) is deliberately single-audience so none of that blocks it.
2. **Tenant isolation by construction on the interactive path.** The
   user's tenant travels with who they are; it cannot diverge from what
   they may see — the half of `ApiKey` that already leaked once
   (fact 2) simply has no analog to get wrong.
3. **No long-lived secret on the interactive path.** Nothing in the
   vault, nothing to rotate, nothing whose absence pauses production —
   the exact failure the Playground is sitting in today.
4. **Attribution becomes true.** Runs on `/v1` stop being "the service
   key did it": `Run`/`CallerIdentity` carry the real `sub`.

## 10. What this makes harder later

1. **`/v1` is no longer one-credential.** Every future `/v1` route must
   think in `V1Caller` terms, not `ApiKey` terms. The single dependency
   seam is the mitigation; the risk is someone adding a route with
   `Depends(require_api_key)` out of habit — worth a test asserting no
   `/v1` route depends on the old name once the swap lands.
2. **`aud` discipline becomes a fleet-wide invariant.** Every future
   verifier must keep passing no `audience=` (or pin its own), and nobody
   may ever "fix" a mysterious 401 with `options={"verify_aud": False}` —
   that one line reopens the deputy hole silently. Greppable, and worth a
   comment at every decode site.
3. **The delegate endpoint couples the interactive path to aw-backend's
   availability** — one extra round-trip per ask, and aw-backend down
   means Playground down (a static key would have kept working). Honest
   cost of removing the static secret.
4. **`token_use`/`aud`/`agent_slugs` become claim contract.** Renaming
   them is a coordinated aw-backend + AP-MT change forever after.
5. **The flag is per-agent, not per-user.** "Agent X for Alice but not
   Bob" needs D4's deferred registry; the flag can't express it.

## 11. Risks for the Coders

1. **`_resolve_api_key` is `CROSS_TENANT` for a reason that does not
   transfer.** The JWT arm has a verified subject *before* any query — it
   must never copy the unscoped-lookup shape; resolve the tenant, bind
   it, then touch the database.
2. **The containment rests on PyJWT semantics — pin them.** Verified here
   on 2.12.1, both directions. A PyJWT major bump that relaxes `aud`
   handling would reopen everything silently; the §7 containment tests
   are the only alarm that would ring.
3. **The dependency must stay an async generator.** The tenant bind wraps
   a `yield`; a sync generator resets the contextvar in the wrong Context
   (`openai_compat.py:101-103`, and the same warning in three other
   files). Streaming responses hold the dependency open — that is relied
   upon, keep it.
4. **Admission-time validation only.** A 300s token authorizes the
   request it opens; a streaming synthesis running past `exp` must not be
   killed mid-stream. Do not add per-chunk re-validation.
5. **All five routes, one swap.** `list_models` reads
   `api_key.agent_slugs` directly (`:567`) rather than through
   `_check_scope` — easy to miss when unifying; `session_chat` (`:683`)
   and `/v1/responses` (`:748`) are the ones a grep for
   `chat/completions` won't find.
6. **Deploy order:** aw-backend T2 deploy → delegate endpoint → AP-MT arm
   (verifies via JWKS, needs nothing else live) → aw-knowledgeable
   playground swap → flag flips. The AP-MT arm shipping "early" is safe:
   with no delegate endpoint live, no token exists to present.
7. **The api-keys role gate (D6) is not optional in shared tenants.** If
   multi-person tenants arrive before it, `user_invocable` is decorative.
8. **Test the discriminator, not the happy path:** a JWT presented to the
   key arm used to be a clean 401; make sure a *malformed* two-dot string
   still 401s cleanly (decode failure → 401, never a 500, never a
   fall-through to the key lookup).
