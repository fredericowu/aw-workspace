# aw-knowledgeable fs-sync — bidirectional sync between `knowledge_base/` and the `main` bucket

Status: **design, not code.** Written by the Architect agent, 2026-10-10,
against card `3f55bf3b-9510-8147-9171-d1357c9d26ec` (target
`knowledgeable-fs-sync`). Builds on `aw-knowledgeable-v2-retrieval.md` §13
(the bulk-ingest driver, which this design extends rather than replaces) and
`aw-knowledgeable-buckets.md`. Frederico's eight decisions from the card are
taken as given; two of them have sharp edges that are named in §12 but
designed around, not reopened.

Assumes Postgres as the identity store's destination (the SQLite exit is
card `3f55bf3b-9510-8124-ae31-d67ea0cc96dd`, same target) — nothing here
adds new SQLite state on the aw-knowledgeable side. The connector's journal
stays SQLite: it is per-workspace scratch under `AW_WORKSPACE_HOME/data/`,
not service state, and the SQLite-exit card does not cover it.

Verified against the working tree this session:

- `repos/aw-knowledgeable/backend/app/core/graph.py:505` — `create_document`
  MERGEs on `{tenant, bucket, content_hash}`, `ON CREATE SET` only.
- `repos/aw-knowledgeable/backend/app/api/documents.py` — GET list / GET
  links / GET content / DELETE / POST. No PUT, no PATCH, anywhere.
- `repos/aw-knowledgeable/backend/app/core/identity.py:283` —
  `resolve_tenant_id` sends service callers to
  `KNOWLEDGEABLE_SERVICE_TENANT_ID` and every human through
  `_get_or_mint_tenant(sub)`, a fresh tenant on first sight.
- `repos/aw-app-knowledgeable/knowledgeable_app/bulk_ingest.py` — the §13
  driver: journal schema (`relpath` PK, `sha256`, `bucket`, `status`,
  `external_id`, `canonical_relpath`), `scan()`, `run_tick()` with the
  ignition guard + 200/tick + 500-backlog ceilings, `_tick_lock`
  (asyncio.Lock — single-process only).
- `repos/aw-app-knowledgeable/knowledgeable_app/workspace_env.py` — reads a
  named var from `os.environ` or `<AW_WORKSPACE_HOME>/.env`.
- `/opt/aw-workspace/src/apps/state_facade.py:334-393` — `ctx.state.lease`,
  flock-backed, `claim`/`release`/`release_all`, gated by `state:own`. On
  master at `f63e43c`; **not live** (running core reports `bb23bd8`).
- `.aw-workspace/.env:2` — `AW_WORKSPACE=aw`. The card's "no source for
  `workspace_slug`" gap is closed by this line + `workspace_env()`; see §6.

---

## 0. The approach, in four sentences

Synced documents stop being identified by their bytes: a new path-keyed
upsert seam (`(tenant, bucket, workspace_slug, source_path)` identity,
`content_hash` + monotonic `rev` as version) is added **next to** the
existing content-hash MERGE, which stays untouched for pathless ad-hoc
uploads. The §13 driver grows into a sync engine in the same module family:
same journal (extended), same ceilings, same ignition guard, with a
periodic scan as the source of truth and single-flight across
`AW_WORKSPACE_WORKERS=10` via `ctx.state.lease`. Write direction is
asymmetric by subtree: only `knowledge_base/authored/` is bidirectional;
everything else is pull-only and a write-back is refused with a reason at
the server seam, never dropped. Deletes are two-phase (marked → human
approves in the UI → deleted), with an explicit `force` path for agents and
a mass-delete valve so a transient `agent sync` wipe cannot cascade into
the graph.

Blast radius: two repos. `aw-knowledgeable` (graph templates, two new
endpoints + one extended, identity allowlist, schema migration, UI list) and
`aw-app-knowledgeable` (sync engine, journal migration, config, manifest
permission). `aw-workspace` core changes **nothing** — but shipping depends
on the already-merged `f63e43c` going live via `restart core` (§5).

---

## 1. Identity and version (problem A)

### 1.1 Two identity regimes, deliberately

`create_document`'s content-hash MERGE (`graph.py:505`) is **kept as-is**
for callers that send no `source_path`: the UI upload, the ad-hoc
`upload_document` MCP tool. Its dedup contract ("a re-POST of the same
bytes is a perfect no-op") is correct for those callers and §13.2's
idempotency depends on it. Retrofitting `ON MATCH` onto it would silently
turn every dedup hit into an update.

A **new** template, `upsert_document_by_path`, carries the synced regime:

```
MERGE (d:Document {tenant: $tenant_id, bucket: $bucket_id,
                   workspace_slug: $workspace_slug, source_path: $source_path})
ON CREATE SET d.created_at = datetime(), d.external_id = $external_id,
              d.rev = 1, d += $properties
ON MATCH SET  d.prev_content_hash = d.content_hash,
              d.content_hash = $content_hash,
              d.rev = coalesce(d.rev, 0) + 1,
              d.updated_at = datetime(),
              d += $update_properties
RETURN d, (d.external_id = $external_id) AS created
```

`content_hash` moves from key to attribute. On a content change the node —
its `external_id`, its manual `LINKS_TO` edges, its topic-tree position,
its entity `ASSERTS` edges' endpoint — survives; only the chunks are
rebuilt (`processing_status` reset to `PENDING`, old chunks deleted in the
same transaction, reusing `delete_document`'s chunk-deletion clause — the
coder must not hand-roll a second chunk sweep). `extraction_status` resets
too, which is inert while extraction stays globally off (the card's
standing veto) and correct when it ever comes back.

New schema (added to `ensure_schema()`, `graph.py:299`, next to the
existing constraint block at `:212-289`):

```
CREATE CONSTRAINT document_sync_identity IF NOT EXISTS
FOR (d:Document) REQUIRE (d.tenant, d.bucket, d.workspace_slug, d.source_path) IS UNIQUE
```

Neo4j exempts nodes missing any constrained property, so pathless ad-hoc
documents and pre-migration documents (which have `source_path` but no
`workspace_slug`) never trip it. Composite uniqueness is proven viable on
this exact deployment — `document_tenant_external_id` (`graph.py:212`) is
already composite on `neo4j:5.26.4-community`.

Plus a range index for the changes feed (§3.3):
`CREATE INDEX document_sync_updated IF NOT EXISTS FOR (d:Document) ON
(d.tenant, d.bucket, d.updated_at)`.

### 1.2 The new/changed API surface (`backend/app/api/documents.py`)

| Route | Gate | Semantics |
|---|---|---|
| `PUT /api/documents/by-path` | `require_bucket_write` + the §4 direction check | Multipart: `file`, `source_path`, `workspace_slug`, optional `base_hash`. Upserts via `upsert_document_by_path`. Returns `{id, rev, content_hash, outcome}` where outcome ∈ `created\|updated\|unchanged`. `unchanged` (incoming hash == current hash) writes nothing — the no-op detector that replaces hash-as-identity. |
| `GET /api/documents/changes` | `require_bucket_read` | `?since=<cursor>&limit=` — documents whose `updated_at` is past the cursor, ordered `(updated_at, external_id)`, cursor returned opaque. The downsync's poll feed (§3.3). |
| `POST /api/documents/{id}/mark-delete` | `require_bucket_write` | Sets `sync_status: pending_delete`, `pending_delete_since`, `pending_delete_reason`. Nothing else changes; the document stays searchable but the Library badges it. With `{"force": true}` — **service caller only** (`require_service` branch inside the handler) — it performs the real delete immediately (decision 5's agent path). |
| `POST /api/documents/{id}/restore` | `require_bucket_write` | Clears the pending-delete mark (file came back, or a human declines). |
| `DELETE /api/documents/{id}` | unchanged | The approval action. The UI's "Pending deletion" list calls this per row (or per selection — §7). |
| `GET /api/documents?pending_delete=1` | unchanged gate | Filter for the approval list. |

`base_hash` on the PUT is **recorded, not enforced**: decision 4 is
last-change-wins, so a mismatched base never rejects the write — it sets
`conflict_overwrote: <old hash>` on the node and logs at WARNING, giving
the audit trail the LWW rule otherwise erases. (Rejected: 409-on-mismatch
compare-and-swap — that is a *first*-writer-wins rule, the opposite of
decision 4.)

All new templates go through the `_CATALOG` + visibility-predicate
machinery; every node pattern carries `{tenant, bucket}` so K5
(`test_tenant_isolation.py`'s static guard) passes without new exemptions.

---

## 2. Tenancy — rule 7 (problem B)

The rule: *"1 tenant per N buckets but one bucket belongs to one tenant and
one tenant only."* The `(:Bucket)` registry (`graph.py:517-525`) already
carries `tenant`, so "a bucket has exactly one tenant" is representable
today; what is missing is **global slug uniqueness** — nothing stops two
tenants from each registering `main`.

**Decision: enforce global uniqueness at the API seam, not as a Neo4j
constraint — because `default` makes the constraint impossible.** Every
tenant implicitly holds a `default` bucket (`resolve_bucket_scopes` adds
`DEFAULT_BUCKET` unconditionally, `identity.py:379`; `mark_bucket_dirty`
MERGEs its registry row on demand, `graph.py:587`). The moment two tenants
each touch `default`, a global `REQUIRE b.bucket IS UNIQUE` constraint
violates — and Neo4j constraints take no WHERE clause, so "unique except
`default`" cannot be expressed in schema. So:

1. `POST /api/buckets` (`buckets.py:63`) gains a **global** existence check
   before `create_bucket` — a new `bucket_exists_any_tenant` template
   (`MATCH (b:Bucket {bucket: $bucket_id}) RETURN b.tenant LIMIT 1`),
   registered in `_BUCKET_UNSCOPED_NODE_TEMPLATES` *and* exempted from the
   tenant predicate with a K5 allowlist entry whose justification is this
   section. A cross-tenant collision is a 409 naming no owner ("bucket slug
   already in use"), consistent with K1's existence-hiding posture.
2. `default` is declared **reserved**: `POST /api/buckets` rejects creating
   it, and rule 7 is documented as applying to *registered* buckets only.
3. `ensure_schema()` gains a boot-time audit: group registry rows by slug,
   log ERROR on any slug (≠ `default`) owned by >1 tenant. Catches whatever
   a future second write path sneaks past the API (§13 names this as a door
   this design leaves open).
4. Tenant-from-bucket derivation: a small `tenant_for_bucket(slug)` helper
   over the registry, for operators and the §6 boot validator. Not a new
   endpoint — nothing routes by bare bucket slug yet.

Migration of the 6 existing `kb-*` buckets: all already live under the
service tenant, so global uniqueness holds vacuously; no row changes needed
for rule 7 itself. Their *content* migration is §8.

---

## 3. The sync engine (extends §13, `aw-app-knowledgeable`)

New module `knowledgeable_app/fs_sync.py` beside `bulk_ingest.py`, reusing
its journal, its `client.py`, and its ceilings. `bulk_ingest.py` itself is
not rewritten: `scan()`'s walk/hash/journal-upsert core and
`run_tick`'s guard sequence are refactored into shared helpers both
entrypoints call. The CLI grows `knowledgeable-sync run|status|report`
subcommands next to the existing `knowledgeable-ingest` family.

### 3.1 Scope and config

Connector `config_schema` gains `bucket` (default `"main"`) — decision 1:
the connector knows its bucket. The scan roots at `kb_root()` with two hard
skips (decision 2): `mapped_folders/repos/` and
`mapped_folders/aw-workspace/`. Still `*.md` only, still the 10MB cap.
`BUCKET_ORDER`'s six-bucket split retires for sync (one bucket, one
permission class now); the constant stays until the §8 migration completes,
then is deleted.

### 3.2 Journal extension (idempotent ALTERs, house style — no alembic)

`files` gains: `last_synced_hash TEXT` (the three-way base — what the
server last confirmed), `remote_rev INTEGER`, `missing_scans INTEGER
DEFAULT 0`, `pending_delete INTEGER DEFAULT 0`, `origin TEXT`
(`scan`/`downsync`). Existing rows keep working; `bucket` is rewritten to
`main` during migration (§8).

### 3.3 One tick, both directions

A sync tick, under the same ignition guard and ceilings as `run_tick`:

1. **Scan (the truth).** Walk, hash, diff against the journal.
   - new path → `PUT by-path` (`created`).
   - hash ≠ `last_synced_hash` → `PUT by-path` with
     `base_hash=last_synced_hash` (`updated`); journal updated from the
     response's `rev`/`hash`.
   - path gone → `missing_scans += 1`; at **≥2 consecutive scans** missing,
     `POST mark-delete` (never on the first scan — a half-written
     `agent sync` pass or an unmounted tree must not look like intent).
   - uploads per tick capped at 200; the remainder waits — same
     backpressure contract as §13.5.3.
2. **Pull.** `GET /api/documents/changes?since=<journal cursor>`. For each
   change with `source_path` under `authored/` and `rev >` journal's
   `remote_rev`: write the journal row **first** (`origin=downsync`,
   `last_synced_hash=<new hash>`), then the file. Journal-before-file is
   the echo-loop breaker: the next scan sees hash == `last_synced_hash` and
   no-ops. Changes outside `authored/` update the journal's `remote_rev`
   only (so drift is visible in `report`) — they are never written to disk.
3. **Both changed** (file hash ≠ base AND remote rev advanced): decision 4,
   last change wins — compare file mtime against the document's
   `updated_at`; newer side wins, ties (±5s skew epsilon) go to the
   filesystem, which is the corpus's primary home. The loser's hash lands
   in `conflict_overwrote` server-side (§1.2); nothing is silently merged.
4. **Debounce.** A detected change starts a 30s quiet window before the
   upload pass — notion-sync writes 1,564 files in one burst, and the
   window collapses that into one tick's worth of stable files instead of
   1,564 half-written reads.

Cadence: periodic scan every 5 minutes (configurable), run by the lease
holder (§5). **inotify is phase 2 and only ever a latency shortcut** that
triggers an early scan — ~1,500 directories of watches against a shared
`max_user_watches` budget, and queue overflow loses events silently, so the
scan remains the only mechanism that is *trusted* (the card's own
recommendation, adopted).

---

## 4. Write direction — `authored/` and the refusal seam

Decision 3: exactly one subtree is bidirectional —
`knowledge_base/authored/`, a new directory no generator writes (the KB's
generators own `notion/`, `skills/`, `memory/`, `mapped_folders/`,
`cli_reference/`, `crispal/`). House precedent followed deliberately:
`notion-sync --bidirectional` is only for notes and never for the Kanban
mirror, "which is derived, not a source."

Enforcement is **server-side, at the PUT**, so it cannot be bypassed by a
new client: `PUT /api/documents/by-path` on a document whose `source_path`
is outside `authored/` is accepted from the **service** caller (that *is*
the upsync) and refused with `403 {"detail": "pull-only subtree — edit the
file in the workspace tree, not here"}` for a **human** caller. The refusal
names its reason — decision 3's "refused, never silently dropped" — and the
connector's pull pass enforces the mirror-image rule (§3.3 step 2: nothing
outside `authored/` is ever written to disk).

Documents created in `main` with no `source_path` (UI drag-drop, ad-hoc
MCP) are **outside the sync's contract**: they live in knowledgeable only,
are never materialized to disk, and the design reserves a future "adopt
into `authored/`" action rather than inventing a path for them now.

---

## 5. Single-flight under `AW_WORKSPACE_WORKERS=10` (problem C)

The connector is Tier-1 — `plugin.activate` runs in **all ten** core
workers, so a naive `asyncio.create_task(sync_loop())` is ten watchers, ten
uploaders, ten journal writers (the existing `_tick_lock` is an
`asyncio.Lock` and protects nothing across processes; the journal already
throws `database is locked` under contention — bulk_ingest.py:93-101).

Design: every worker runs the loop; the loop body is
`if not ctx.state.lease.claim("fs-sync"): sleep(poll); continue` — the
flock-backed, app-namespaced lease from `src/apps/state_facade.py:355`.
Claim-in-the-loop, not claim-in-activate, so a dead holder's lease (flock
drops on process exit) is picked up by a sibling within one poll interval.
`deactivate` calls `release_all()` (state_facade.py:387). The CLI/route
door (`knowledgeable-sync run`) lands on an arbitrary worker and claims the
same lease; a loser returns the same declared
`{"blocked": true, "reason": "another worker holds the fs-sync lease"}`
shape `run_tick` already uses. `_tick_lock` stays — it still serializes
overlap *within* the holder process.

Manifest: `permissions` gains `"state:own"` (aw-app.json currently lists
seven permissions; this is the eighth).

**Hard dependency, declared:** `ctx.state` merged in `f63e43c` but the
running core is `bb23bd8` — the facade and the `state:own` capability are
**not live** until a `restart core`. Rollout order is core-first (the
capability must exist in the live validator before any app manifest
requests it — the `new-capability-rollout-order` rule), and Tier-1 code
reloads also need the core restart. The coder's first verification step is
`state:own` appearing in the live capabilities route, *before* touching the
connector. Note: `restart core` from inside a run kills that run — this
restart is a human/deploy step, not something the implementing agent does
mid-session.

---

## 6. `workspace_slug` — the gap has a source

The card flags "nothing in the container environment carries the workspace
slug." Closed without new plumbing: **`AW_WORKSPACE=aw` is line 2 of
`<AW_WORKSPACE_HOME>/.env`**, and the connector already ships
`workspace_env()` (`knowledgeable_app/workspace_env.py`) reading exactly
that file-or-environ — the same helper it uses for `AW_WORKSPACE_API_KEY`.
Core itself treats this var as its Redis key namespace
(`src/libs/redis_coord.py:117`, `get_workspace_slug`), so it is already
load-bearing identity, not a label.

One deviation from core's behavior, on purpose: core falls back to a
default slug when unset ("legibility aid, not a correctness boundary" —
redis_coord.py:119). Here the slug **is** a correctness boundary (it is a
quarter of the document identity key), so `fs_sync` **refuses to run** with
a declared reason when `workspace_env("AW_WORKSPACE")` is empty — a
defaulted slug would happily interleave two workspaces' documents under one
identity.

---

## 7. Delete with approval (decision 5) + the mass-delete valve

Lifecycle: file gone for ≥2 consecutive scans → `mark-delete` →
`sync_status: pending_delete` → UI list ("Pending deletion" filter in the
Library, count badge; approve = existing `DELETE /api/documents/{id}`,
decline = `restore`) → only an approval removes the node, its chunks, its
edges. Approve supports multi-select and a **prefix filter** ("everything
under `mapped_folders/repos/`") — without it, §8's migration would hand a
human a 7,000-row checkbox list.

`delete=force` (decision 5's agent path): the service caller may send
`{"force": true}` on `mark-delete` for an immediate hard delete — for an
agent deliberately reorganizing documents. The connector exposes it as an
explicit MCP-tool argument (`sync_delete(path, force=True)`), never as a
default.

**The valve.** `agent sync` is an exact mirror that deletes (AGENTS.md:
"files removed from a source are deleted downstream"), so a transient app
uninstall can vaporize `skills/` (99 files) legitimately-looking-ly. Two
stacked guards:

1. **Zero-trust scan precondition:** `kb_root()` missing, unreadable, or
   yielding zero files → the tick aborts entirely; no row's `missing_scans`
   advances. An unmounted volume marks nothing.
2. **Mass-delete threshold:** if one scan would advance `missing_scans` on
   more than `max(25, 10% of uploaded rows)` files, it marks **none** of
   them, sets a journal flag, and exits non-zero — which the contributed
   scheduled task already escalates to an agent with a prompt (aw-app.json's
   task contract); that prompt gains a fourth case: "mass-deletion tripped —
   verify the tree, then either clear the flag or approve in the UI." Even
   `force` respects this threshold: a reorganizing agent deleting hundreds
   of documents does it through the UI-visible pending list or in
   sub-threshold batches.

Plus the ≥2-scans rule (§3.3), which alone absorbs any wipe shorter than
one scan interval.

---

## 8. Migration of the 9,108 already-ingested documents

Decision 6 bounds the effort: move is preferable to losing, re-ingest is
acceptable, do not over-optimize. Sequence:

1. **Backfill** (one-time, in `ensure_schema()`'s existing one-time-migration
   slot, pattern at `graph.py:1779`): every Document with a `source_path`
   and no `workspace_slug` gets `workspace_slug: "aw"`, `rev: 1`,
   `updated_at = coalesce(updated_at, uploaded_at)`. Bulk-ingest always
   sent `source_path` (`bulk_ingest.py:417-419`), so this covers the full
   9,108 minus UI-test uploads.
2. **Move the curated five** — `kb-crispal`, `kb-memory`, `kb-notion`,
   `kb-cli-reference`, `kb-skills` (≈1,797 docs) — into `main` wholesale
   via the existing `scripts/move_bucket.py` (membership is a mutable
   property; a move is a SET, buckets.md §1).
3. **`kb-mapped-folders`:** a one-off scoped move for the keepers only
   (`SET n.bucket = 'main' WHERE n.source_path STARTS WITH
   'mapped_folders/docs/'`, ~76 docs), then bulk `force`-delete the rest
   (~7.3k repos/aw-workspace code-maps — regenerable, exactly what decision
   6 says is acceptable to drop; deleting server-side avoids pushing 7.3k
   rows through the human-approval list).
4. **Journal rewrite:** set `bucket='main'` on kept rows,
   `last_synced_hash = sha256` on `uploaded` rows; drop rows under the two
   skip prefixes. Then the first sync scan reconciles the remainder
   normally — the dedup-alias rows (`status=alias`) under kept subtrees
   re-enter as ordinary files and upload as their own path-keyed documents
   (hash-dedup no longer applies to synced docs; post-skip duplication is
   minimal, the card's own note).
5. Entity extraction stays **off** throughout — no phase-2 pass is
   authorized by this design.

~1,873 files, far under the measured 46h-CPU full-corpus cost; most are
step-2 moves, not uploads.

---

## 9. The owner-visibility blocker (`resolve_tenant_id`)

Today the owner's browser mints a private empty tenant
(`identity.py:283-285` → `_get_or_mint_tenant`) while the connector writes
to `KNOWLEDGEABLE_SERVICE_TENANT_ID` — two disjoint graphs, so the UI shows
73 test documents instead of 9,108, and the entire knowledgeable→workspace
half of this design would be invisible to the person approving deletes.

**Decision: a declared owner allowlist.** New env
`KNOWLEDGEABLE_OWNER_SUBS` (comma-separated aw-backend account ids);
`resolve_tenant_id` maps an allowlisted `sub` to
`settings.knowledgeable_service_tenant_id` **before** the mint branch.
Boot-time validator (same guard-rail style as D2's existing "secret without
tenant never boots" check in `config.py`): `KNOWLEDGEABLE_OWNER_SUBS` set
without `KNOWLEDGEABLE_SERVICE_TENANT_ID` refuses to start. Declared
config, not a hand-edited row — survives the SQLite→Postgres identity
migration untouched, and when a real tenant claim lands in the JWT (T2),
the allowlist retires inside the same single swap point it lives in.

Rejected: writing the owner's minted tenant id into the connector config
(inverts ownership — the service tenant predates and outlives any one
human's row); minting-time redirection in `_get_or_mint_tenant` (that
function is the thing the Postgres card is about to rewrite; keep the
policy in `resolve_tenant_id`, the declared swap point).

---

## 10. Where it lands (both repos)

**`repos/aw-knowledgeable`:**
- `backend/app/core/graph.py` — `upsert_document_by_path`,
  `list_document_changes`, `mark_document_delete` / `restore_document`,
  `bucket_exists_any_tenant` templates in `_CATALOG`; new constraint +
  index + backfill in `ensure_schema()`; `updated_at`/`rev` stamping on the
  mutating document templates.
- `backend/app/api/documents.py` — `PUT /by-path`, `GET /changes`,
  `POST /{id}/mark-delete`, `POST /{id}/restore`,
  `?pending_delete=1` filter (§1.2 table).
- `backend/app/api/buckets.py` — global slug check, `default` reserved.
- `backend/app/core/identity.py` — `KNOWLEDGEABLE_OWNER_SUBS` branch in
  `resolve_tenant_id`; `backend/app/config.py` — the two new settings +
  boot validator.
- `backend/tests/` — K5 exemption entry + tests mirroring
  `test_tenant_isolation.py`'s shape for the new templates; direction-
  refusal test (human PUT outside `authored/` → 403 with reason).
- frontend — Library "Pending deletion" filter, approve/decline, prefix
  multi-select.

**`repos/aw-app-knowledgeable`:**
- `knowledgeable_app/fs_sync.py` — the engine (§3), lease loop (§5),
  valve (§7).
- `knowledgeable_app/bulk_ingest.py` — extract shared scan/journal helpers;
  journal ALTERs; no behavior change to the ingest door.
- `knowledgeable_app/mcp/client.py` + tools — `sync_delete`, `by-path`
  upload, changes poll.
- `aw-app.json` — `state:own` permission, `bucket` config key, task prompt
  gains the valve case, version bump.
- `plugin.py` — arm the loop task; `release_all` in `deactivate`.

Untouched: `create_document`'s MERGE, the chunk/claim pipeline, the
retrieval surface, `aw-workspace` core (dependency only).

---

## 11. Rejected alternatives (keep with the design)

1. **S3 object versioning as the version authority** (Frederico's own
   "maybe"). Rejected per the card's analysis, which this design confirms:
   per-key opaque version ids, no compare-and-swap spanning Neo4j and the
   blob store, so every conflict decision would still need a graph-side
   authority — at which point S3 adds a second source of truth instead of
   removing one. The bytes are already covered (nightly `documents-data` →
   S3 under `files/`, `aw-stack/scripts/backup-knowledgeable-volumes.sh`).
   Authority lives on the Document node: `rev`, `content_hash`,
   `updated_at`, `conflict_overwrote`.
2. **One identity regime — migrate ad-hoc uploads to path keys too.**
   Rejected: pathless documents have no path to key on, inventing one
   (`uploads/<uuid>`) creates fake provenance, and the content-hash dedup
   contract is still right for them. Cost accepted: two regimes (§12.2).
3. **CAS / reject-on-stale-base (409 on `base_hash` mismatch).** Rejected:
   contradicts decision 4 (LWW). Kept as audit metadata instead.
4. **inotify as the primary change source.** Rejected: ~1,500 watch
   descriptors against a host-shared budget, silent event loss on queue
   overflow, and the notion-sync burst would stampede it. Scan is truth;
   inotify is an optional phase-2 latency shortcut.
5. **DB-level global uniqueness constraint on bucket slug.** Rejected
   because the implicit per-tenant `default` bucket makes it violate on
   contact (§2) — API-seam enforcement + boot audit instead.
6. **A fourth-repo watcher (aw-workspace core task or standalone
   container).** Rejected: core must stay out of app business
   (`sync_on_boot` precedent notwithstanding), a container watcher can't
   see `AW_WORKSPACE_HOME` without new mounts, and the Tier-1 connector
   already owns the client, the journal, and the config.
7. **Hash-keyed identity with a rename/alias table on top** (keep
   `content_hash` as key, map paths separately). Rejected: it re-creates
   the orphaning bug one layer up — an edit still mints a new node and the
   alias table must then migrate edges, which is the hard part identity-
   by-path makes unnecessary.
8. **Tombstone documents instead of a `pending_delete` property** (separate
   `(:Tombstone)` node). Rejected: the pending document must stay visible
   and searchable until approved (a tombstone hides it), and one property +
   one filter is the whole feature.

## 12. What this makes harder later

1. **Renames are delete+create.** Path identity has no rename detection; a
   reorganized tree shows up as N pending-deletes + N creates, losing
   manual links and topic positions on the moved docs. The `force` path
   plus decision 6's "move is preferable but re-ingest is acceptable"
   makes this survivable, but a future rename-preserving sync will need a
   content-hash candidate-matching pass this design deliberately skips.
2. **Two identity regimes are permanent residents.** Every future document
   query/template must stay correct for docs with and without
   `source_path`/`workspace_slug`/`rev`. The constraint exemption that
   makes migration painless is the same hole a buggy writer could use to
   create near-duplicates.
3. **Bucket uniqueness is API-enforced, not schema-enforced.** Any future
   second write path to the registry (admin script, bulk import) can
   violate rule 7 silently until the boot audit notices. The audit logs; it
   cannot un-merge.
4. **LWW loses updates by design.** Two edits to one authored doc inside a
   scan interval: one survives, the other leaves only `conflict_overwrote`.
   If authored/ grows real multi-writer traffic, this needs a merge story.
5. **The owner allowlist deepens the one-big-service-tenant shape.** Real
   multi-user tenancy later means migrating data *out* of the service
   tenant and retiring the allowlist — one more legacy auth path, same
   family as aw-backend's legacy gates.
6. **Single `main` bucket flattens the permission classes** the six-bucket
   split encoded (§13.1 called buckets permission boundaries). Re-splitting
   later is cheap on the server (bucket is a SET) but the journal and the
   connector config assume one bucket per workspace.
7. **The changes-feed cursor assumes one writer clock.** `(updated_at,
   external_id)` ordering is safe while aw-knowledgeable runs one uvicorn
   worker (verified in the v2 doc). If that service ever goes multi-worker,
   the cursor needs a real sequence.

## 13. Risks for the coders (the non-obvious breakage)

1. **The restart-core dependency is invisible in CI.** Tests import
   `state_facade` from the working tree and pass; the live core at
   `bb23bd8` has neither the module nor the capability. Verify `state:own`
   in the **live** capabilities route before the manifest change ships, or
   the app install fails validation — and remember the app's own Tier-1
   code also only reloads on core restart.
2. **Lease probes lie if you use `is_held`.** `state_facade.is_held`
   acquires-and-releases as a probe (documented microsecond-hold,
   `state_facade.py:372`); never use it to decide to *start* work — only
   `claim()`'s boolean is the truth.
3. **Echo loop if downsync writes the file before the journal.** §3.3's
   order (journal, then file) is load-bearing; reversed, a crash between
   the two re-uploads every pulled doc forever.
4. **`scan()`'s frozen-row rule inverts.** Today `uploaded` rows are never
   recomputed (`bulk_ingest.py:220-226`) — correct for one-shot ingest,
   wrong for sync, where a changed uploaded file is exactly the signal.
   The shared helper must re-hash everything and diff against
   `last_synced_hash` instead of skipping frozen rows.
5. **K5 will reject the global bucket-exists template** until it has an
   explicit allowlist entry — budget for touching
   `test_tenant_isolation.py`, not just adding Cypher.
6. **Chunk cleanup on update must reuse `delete_document`'s clause.** A
   hand-rolled sweep that misses the chunk vector index or the
   `HAS_CHUNK` shape leaves ghost chunks that still answer vector search
   with stale content — the test that "won't catch it" is any test that
   only checks the document node.
7. **mtime is a conflict input, not a change detector.** Change detection
   is hash-only (generators rewrite identical bytes; mtime flaps). mtime is
   consulted exactly once, in the both-changed LWW comparison, with the ±5s
   epsilon.
8. **The connector's `_ensure_buckets` creates buckets as the service
   tenant** — fine — but after §2 it can now 409 against another tenant's
   slug; treat that 409 as fatal-with-reason, not steady-state (today's
   `create_bucket` treats 409 as success, `bulk_ingest.py:290-300`).
9. **`conftest` in aw-workspace monkeypatches `httpx.get` globally** —
   connector tests that exercise JWKS or client paths inherit that; don't
   chase phantom fetch behavior in CI.
10. **Approval UI and `mark-delete` must not touch `processing_status`** —
    the claim queue matches indexed equality on it; a pending-delete doc
    that re-enters `pending` gets re-chunked while awaiting deletion.

## 14. Sequencing

1. `restart core` (human/deploy) → `state:own` live. Everything else is
   blocked behind this single step.
2. aw-knowledgeable server work (§1, §2, §9 — templates, endpoints,
   constraint, allowlist, backfill). Deployable alone; nothing calls the
   new seams yet. Deploy is manual (aw-backend-style: service's own build
   first, then stack re-pull).
3. Migration (§8, steps 2–4) — operator-run scripts, one sitting.
4. Connector work (§3–§7) + manifest bump; ship via marketplace.
5. Enable the sync task; watch one full day of ticks; then (optionally,
   phase 2) inotify.

QA gates: K3/K5 green with the new templates; the direction-refusal 403;
the valve test (simulated empty tree → zero marks); two-worker lease test
(two processes, one tick runs); echo-loop test (pull → next scan no-ops).
