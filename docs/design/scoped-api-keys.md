# Scoped API keys — design (2026-10-04)

Status: **Accepted — handed to Coder**. Requested by Frederico (via Telegram
dispatch): a credential that can call exactly one set of routes (first
consumer: an Alexa Lambda calling aw-app-xiaomi's `/tv/*`), instead of
either no auth at all or the workspace-wide master key.

## The decision

**Scoped API keys are a second credential class next to the workspace
master key, validated and enforced entirely inside aw-workspace core.
aw-backend's edge proxy is deliberately not changed.** A scope is a named,
reusable set of route rules managed in Settings → Integrations; a key binds
to exactly one scope, has a selectable expiry (short / long / never), and is
sent in the same `X-Api-Key` header, distinguished by an `awsk_` prefix.
Per-app `auth_type: scoped | workspace` (default `workspace`) controls one
thing only: whether that app still accepts the **master** key. Scoped keys
work wherever their scope says — the scope is the single authority; the
app's `auth_type` never gates them.

Why the edge needs no change (the `auth_required` vs `public` trap does not
recur here): `repos/aw-backend/src/api/routes/workspace_tunnel_proxy.py`
already forwards any request carrying a **non-empty** `X-Api-Key`
(`_has_workspace_api_key`, l.468; gate at l.272–289; rationale l.59–67:
"it widens nothing on its own — an invalid key still 401s at the
workspace"). The edge never validates the key — it delegates to the
workspace, which is the only layer that knows apps, routes and scopes. The
gap Frederico hit before was the **no-credential** case (`auth_required:
false` without `public: true`); a scoped caller always presents the header,
so the existing carve-out applies. Single enforcement point, no scope
definitions synced up to aw-backend, no reconciler involvement, nothing to
drift.

## Credential classes after this change

| Credential | App routes (`auth_type: workspace`) | App routes (`auth_type: scoped`) | Framework routes |
|---|---|---|---|
| Identity JWT (`aw_id_jwt` / bearer) | ✅ | ✅ (Settings UI keeps working) | ✅ |
| Master key `AW_WORKSPACE_API_KEY` | ✅ (today's behavior, unchanged) | ❌ **403** | ✅ |
| Scoped key `awsk_…`, route in scope | ✅ | ✅ | ❌ **403** |
| Scoped key, route NOT in scope | ❌ **403** | ❌ **403** | ❌ **403** |
| Scoped key invalid / expired / revoked | 401 | 401 | 401 |

403 = authenticated but not permitted (per the acceptance target); 401 =
not a valid credential at all. The `routes:local` loopback bypass and the
presentation-share-link exemption in `IdentityGuard` run before all of this
and are untouched.

## Where it lands

### aw-workspace core (this repo) — the whole enforcement surface

**New `src/api/scoped_api_keys.py`** (sibling of
`src/api/workspace_api_key.py`, same idioms):

- `KEY_PREFIX = "awsk_"`; mint = `KEY_PREFIX + secrets.token_hex(32)`.
- At rest: `key_hash = sha256(full_token)` (hex, unique-indexed). SHA-256 is
  correct for 256-bit random secrets — there is nothing to brute-force and
  bcrypt would add real latency on every request.
- `resolve_scoped_key(presented) -> dict | None`: prefix check → hash →
  lookup → revoked/expiry check → claims
  `{"sub": "scoped-api-key", "scoped": True, "key_id", "key_name", "rules": [...]}`.
  Returns `None` for anything invalid/expired/revoked (→ caller 401s).
- `scope_allows(rules, app_id, route_path, method) -> bool`. Path patterns
  are **mount-relative** (`/tv/power`, `/tv/*`), exact match or trailing
  `/*` prefix wildcard only — no regex. Optional `methods` list per rule,
  absent = all methods.
- CRUD helpers for scopes and keys; `last_used_at` touch throttled (skip
  the write if fresher than 60s).

**New tables in `src/api/models.py`** (created by
`src/api/db.py::create_all_tables`, l.94 — new tables need no hand-rolled
ALTERs):

```
api_scopes:       id (uuid pk), name (unique), rules (JSON list of
                  {"app": str, "paths": [str], "methods": [str]?}), created_at
scoped_api_keys:  id (uuid pk), name, scope_id (fk api_scopes),
                  key_hash (unique idx), key_hint (first 10 chars, for lists),
                  expires_at (nullable → never), created_at,
                  revoked_at (nullable), last_used_at (nullable)
```

Scope deletion while keys still reference it → 409 (revoke/delete the keys
first). Rules only name **app** routes; framework routes are structurally
outside any scope in v1.

**`src/apps/runtime.py`** — the app-route gate:

- `_default_verify_http` (l.135–152) and `_default_verify_ws` (l.155–184):
  when the `X-Api-Key` value starts with `awsk_`, resolve via
  `resolve_scoped_key` and return its claims; otherwise fall through to the
  existing master-key compare, byte-identical to today. An `awsk_` value
  that fails to resolve returns `None` (→ 401/4401), it must NOT be retried
  against the master key.
- `IdentityGuard` (l.240–370) grows `app_id: str` and
  `auth_type: Callable[[], str]` ctor params. After claims are obtained —
  in **both** the `auth_required` branch and the relaxed
  (`auth_required: false`) branch:
  - claims `scoped` → `scope_allows(rules, app_id, get_route_path(scope),
    method)` or **403** (HTTP) / close **4403** (WS), detail
    `"forbidden: key scope does not cover this route"`. Applies in the
    relaxed branch too: an explicitly presented scoped credential never
    grants, nor masquerades as, more than its scope — silently downgrading
    it to anonymous would hide misconfiguration.
  - claims `api_key` (master) and `auth_type() == "scoped"` → **403**,
    detail `"forbidden: workspace key not accepted (auth_type=scoped)"`.
- `_attach_mount` (l.1243–1294): pass `app_id=app_id` and
  `auth_type=lambda: str(loaded.config.get("auth_type", "workspace"))` —
  same live-lambda pattern as `auth_required` at l.1257. Any value other
  than `"scoped"` is treated as `"workspace"` (the config endpoint's
  `_coerce_config` in `src/apps/routes.py:291` passes unknown keys through
  unvalidated, so the guard must tolerate garbage). Both the path `Mount`
  and the per-app-subdomain `Host` mount (l.1260–1273) wrap the SAME
  guarded app, so both entry points enforce identically by construction —
  but test both, because `get_route_path` derives the mount-relative path
  differently for each.

**`src/api/identity.py`** — the framework-route gate:

- `require_identity` (l.175) and `authorize_ws` (l.202): when the header
  value starts with `awsk_`, resolve it (off-loop via `asyncio.to_thread`,
  same reasoning as the existing `_workspace_api_key_authorized` threading);
  a VALID scoped key → **403** `"scoped keys cannot access framework
  routes"`; an invalid one → 401 as usual. This is what makes a scoped key
  unable to mint more keys, read settings, or touch `/api/apps` CRUD.

**`src/api/app.py`** — CRUD routes, next to the existing key routes
(l.524–541), all `Depends(require_identity)`:

```
GET  /api/settings/api-scopes            list
POST /api/settings/api-scopes            create {name, rules}
DELETE /api/settings/api-scopes/{id}     409 while keys reference it
GET  /api/settings/api-keys              list (hint, scope, expiry, last_used, revoked)
POST /api/settings/api-keys              create {name, scope_id, expires_in_seconds|null}
                                         → returns the full token ONCE
POST /api/settings/api-keys/{id}/revoke
```

`expires_in_seconds: null` = never. The UI offers presets — short (1 h),
long (90 d), never — the API stays an arbitrary duration.

### aw-workspace-ui (`repos/aw-workspace-ui`, branch **main**)

`src/components/IntegrationsTab.jsx` (165 lines): add a
`ScopedApiKeysIntegration` section below `WorkspaceApiKeyIntegration`
(l.15), matching its pattern (apiFetch, css-var styling, `data-testid`,
masked values): scopes list with a rules editor (app slug + path patterns +
optional methods), keys list with revoke, create-key dialog with the expiry
presets and a one-time token reveal + copy. Extend
`src/__tests__/IntegrationsTab.test.jsx`.

Per-app `auth_type` is set the same way `auth_required`/`public` are today
— `POST /api/apps/{slug}/config` (`src/apps/routes.py:1007`). No new UI for
it in v1; a toggle in the app-settings panel is a follow-up, not scope
creep to absorb silently.

### aw-backend — **no changes** (see "The decision" for the full argument).

## Rollout for the xiaomi/Alexa case (ops, fredericowu workspace)

1. Deploy core + UI. Defaults change nothing (`auth_type` unset ⇒
   `workspace`; no scoped keys exist).
2. Settings → Integrations: create scope `xiaomi-tv` with rules
   `[{"app": "xiaomi", "paths": ["/tv/*"]}]`; mint a never-expiring key;
   put it in the Lambda.
3. `POST /api/apps/xiaomi/config` with `{"auth_type": "scoped",
   "auth_required": true, "public": null}` — `_merge_config`
   (`src/apps/routes.py:307`) removes `public` on explicit null, closing
   the edge's no-credential carve-out that is currently holding the door
   open. The Lambda's requests pass the edge via the X-Api-Key carve-out
   and are scope-checked by `IdentityGuard`. Xiaomi's `local_paths`
   loopback bypass (HA, agents) is unaffected throughout.

## Rejected alternatives

1. **A distinct header (`X-Scoped-Key`)** — would force an aw-backend edge
   change (its carve-out matches `x-api-key` only) plus doc fan-out to
   every app repo. The `awsk_` prefix discriminates on the same header for
   free. Runner-up; would be revisited only if key classes ever need
   different edge handling.
2. **Enforcing scopes at the edge (aw-backend)** — the edge knows neither
   routes nor the key store; scope definitions would have to sync up via
   the reconciler like `public` does, recreating exactly the two-layer
   drift class (`auth_required` vs `public`) this task started from.
3. **`auth_type: scoped` as the opt-in that makes scoped keys work on an
   app** — makes authorization a two-place AND (scope says yes AND app
   says yes): same split-brain failure mode again, and a scope spanning two
   apps would require flipping both. Instead the scope is the single
   authority and `auth_type` only controls the master key. No privilege
   escalation: only workspace-authenticated users (who already hold
   full access) can define scopes/mint keys.
4. **Stateless signed tokens (JWT with embedded scope)** — no revocation
   without a denylist; "never expires" + unrevocable is disqualifying.
   Opaque DB-backed keys revoke instantly and match the master-key pattern.
5. **Scope rules inline on the key (no `api_scopes` table)** — loses the
   named, reusable "scope definitions" entity Frederico explicitly asked
   for in Settings → Integrations, and makes key rotation copy-paste.
6. **bcrypt/argon2 at rest** — adds per-request latency to defend 256-bit
   random tokens that have no brute-force surface.

## What this makes harder later

- `awsk_` becomes permanent public API (docs, caller configs, log
  grepping).
- Scope rules bind to mount-relative paths: an app renaming its routes
  silently 403s every key that covered them; nothing detects this.
- `auth_type: scoped` rejects the master key for that app — any internal
  caller using `AW_WORKSPACE_API_KEY` against such an app (notably the MCP
  gateway's app-surface calls) breaks per-app. Fine for xiaomi (no MCP
  contributions, internal callers use loopback `local_paths`), but every
  future flip of `auth_type` needs this check.
- Keys live in the per-workspace schema; a future cross-workspace key
  would pull aw-backend in after all.
- One DB read per scoped request (same cost shape as the master key
  today). A future cache must survive WORKERS=N — per-worker state is a
  known failure class here; don't add a cache casually.
- The relaxed-branch 403 means apps can't use scoped keys as "optional
  identity hints" on `auth_required: false` routes.

## Risks for the Coder

- **No in-process caching** of keys/scopes — read the DB per request
  exactly like `verify_workspace_api_key` does. WORKERS=10 per-worker
  state is the house's recurring silent breakage.
- **Both entry points**: path mount AND `Host` subdomain mount must get
  scope-check tests — `get_route_path` is the only correct way to derive
  the mount-relative path (see the comment at `runtime.py:302`).
- **Ordering**: `_local_bypass` and the share-link exemption stay FIRST;
  never scope-check a loopback `local_paths` call.
- **Don't regress the master path**: `test_valid_workspace_api_key_authenticates_app_routes`
  and friends in `src/tests/integration/apps/test_identity_guard.py` must
  stay green with byte-identical behavior for non-`awsk_` keys.
- **Threading**: scoped resolution hits Postgres; in `identity.py` wrap it
  in `asyncio.to_thread` like the existing key check, or one slow request
  freezes the worker's loop (documented failure, `identity.py:178–187`).
- **WS close codes**: 4401 = unauthenticated (existing), 4403 = scope
  denied (new) — don't reuse 4401 or callers can't tell.
- Core integration tests need a throwaway Postgres; `conftest`
  monkeypatches `httpx.get` globally; tests share the live Redis keyspace
  — all known local-test traps in this repo.
- New tests live in `src/tests/integration/apps/` (guard behavior) and a
  new `src/tests/integration/api/` module for CRUD + resolve/expiry logic.
