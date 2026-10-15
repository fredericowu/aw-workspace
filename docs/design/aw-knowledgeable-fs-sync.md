# aw-knowledgeable fs-sync — bidirectional sync between `knowledge_base/` and the `main` bucket

Status: **design, not code.** Written by the Architect agent, 2026-10-10,
against card `3f55bf3b-9510-8147-9171-d1357c9d26ec` (target
`knowledgeable-fs-sync`). Builds on `aw-knowledgeable-v2-retrieval.md` §13
(the bulk-ingest driver, which this design extends rather than replaces) and
`aw-knowledgeable-buckets.md`. Frederico's eight decisions from the card are
taken as given; two of them have sharp edges that are named in §12 but
designed around, not reopened.

**Addendum 2026-10-10, same card:** §15 adds **collections** (a source-path
prefix as retrieval scope) and **anchors** (a node a traversal starts from)
— the third axis next to bucket (who can see) and topic (what it's about).
It changes §14's sequencing and the scope of its step 2; §14 is rewritten
accordingly. Frederico's decision, verbatim: *"I believe anchor is the thing
but I like the collection idea too, folders can be collections and we can
also anchor it right? If so, let's do both."*

**Addendum 2026-10-11, card `3f65bf3b-9510-81ce-9fb7-ff2e67a10035`:** §16
adds the **git backend** (commit/push the synced tree to a user-named repo
on each tick) and its hard prerequisite, the **leading-dot exclusion rule**
(no dot-folder or dot-file ever enters the scan — `.git` above all).
Frederico's ask, verbatim: *"user will specify the git repo that he wants to
use to commit the knowledge base artifacts, so on sync we wil also
commit/push git changes to that backend, any folder starting with '.' on the
knowledge_base folder should be ignored. I'm saying this specially because
of .git, I don't want to add it to the knowledge graph."*

Assumes Postgres as the identity store's destination (the SQLite exit is
card `3f55bf3b-9510-8124-ae31-d67ea0cc96dd`, same target) — nothing here
adds new SQLite state on the aw-knowledgeable side. **Amended 2026-10-10 by
the SQLite-exit design (`aw-knowledgeable-sqlite-exit.md`):** the earlier
claim here that the connector's journal "stays SQLite" is overruled — the
SQLite-exit card names `bulk_ingest.py`'s journal as its second store, and
it moves to the workspace Postgres via `ctx.db` (`db:own-tables`). Nothing
in this design depends on the journal's storage engine.

**Amendment 2026-10-12 — as-built status (card
`3f75bf3b-9510-81eb-9c4c-f63d3a4a8f52`, delta inventory; corrects claims
below that were true on 2026-10-10/11 and are no longer):**

- **The §5 restart-core gate has CLEARED.** `ctx.state` is live in the
  running core (`AW_WORKSPACE_WORKERS=10`): the connector's `run_tick`
  already claims `ctx.state.lease` on every scheduled tick
  (`bulk_ingest.py:431-477`, `routes.py:200`) and `state:own` ships in
  `aw-app.json` v0.27.0. §14 step 3 is done; the "not live
  (`bb23bd8`)" note below is historical.
- **§15 (collections + anchors) is shipped AND deployed** — verified live
  2026-10-12 via `search_graph` (`scope.collection` honored;
  `strategy: "anchored"` with `anchor="path:memory/"` resolves). The
  Library collection facet and `GET /api/collections` also shipped
  (commit `2e86caa`).
- **§16.1 (leading-dot rule) and §16.2–16.7 (git backend) are shipped**
  (`bulk_ingest.py:162-172`, `_iter_markdown_files`' walk-prune,
  `git_backend.py`, `_run_git_step` inside the lease-held tick; tests in
  `tests/test_git_backend.py`). The git backend is **dormant**: live
  config has `git_remote_url` empty and no `git_token` — enabling it is
  operator config, not code.
- **§9 (owner allowlist) is shipped and configured live**
  (`config.py:59,352-383`, `identity.py:256`; `KNOWLEDGEABLE_OWNER_SUBS`
  set on the production container).
- **The journal moved to the workspace Postgres**
  (`app__knowledgeable__bulk_ingest_files` via `ctx.db`) per the
  SQLite-exit amendment — already reflected above.
- **Still unbuilt:** §1 (path-identity templates, `PUT /by-path`,
  `GET /changes`, mark-delete/restore), §2 (global bucket-slug check),
  §3.2 journal columns, §3.3 sync tick / `fs_sync.py`, §4's 403 seam,
  §6 refusal, §7 delete pipeline + UI, §8 step-1 backfill. `scan()` is
  still create-only (frozen `uploaded` rows, `bulk_ingest.py:327-369`).
  *(Later that day: cards A/B/D shipped §1/§2/§3.2/§3.3-upsync/§4/§6/§7
  and are live; see each card. Card C downsync and §16 enablement remain.)*

**Amendment 2026-10-12 (evening), card
`3f75bf3b-9510-8155-a345-c9b812686f9d`:** §17 — delete detection as built
is journal-row-driven and therefore blind to any graph document with no
journal row pointing at it (35 live instances at the dead `cli_reference/`
paths). §17 redefines delete marking to key on the **path** (a new
service-only `mark-delete-by-path` fan-out seam) and adopts
server-claimed paths the journal lacks, under the same §7 valve, widened.

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

### 8.1 As built (2026-10-10, card `3f55bf3b-9510-810c-99b4-cc473cd87815`)

The bucket half of §8 shipped as `repos/aw-knowledgeable/scripts/
collapse_to_main.py` plus the connector retarget in
`aw-app-knowledgeable/knowledgeable_app/bulk_ingest.py`. The plan above held;
four things it did not name had to be decided, and are recorded here rather
than left in a run log:

1. **The `workspace_slug`/`rev` backfill (step 1) was NOT done.** It belongs
   to §1's path-identity regime, which this card did not ship — nothing reads
   either property yet, and writing them now would fabricate a `rev: 1` for
   documents whose next write is still `create_document`, not
   `upsert_document_by_path`. Left to the card that lands §1.1.
2. **The Collection spine is torn down and rebuilt, not moved.** A
   collection's `external_id` hashes its bucket
   (`_collection_external_id`, §15.1), so a Collection carried along by a
   bucket move keeps an id nothing recomputes to — a saved anchor would miss
   it. The script deletes every Collection node in the tenant and lets
   `ensure_schema()`'s own §15.7 spine backfill rebuild the tree from the
   surviving documents' `source_path`. §15.1's "nobody human deletes them"
   holds: the teardown is derived-data GC, not a delete the approval list in
   §7 should ever see.
3. **The shared entity layer needed two steps §8 does not mention.** An
   `(:Entity)` carries no `bucket` (§11.1), so it is neither moved by a
   bucket move nor removed by deleting the documents that evidenced it.
   `MENTIONS` from a `(:Chunk)` is its only tie to a document, so the script
   runs ONE tenant-wide sweep (`delete_orphan_entities`) after every delete
   — never per-document — and then re-stamps the `ASSERTS` edges left
   pointing at a torn-down bucket onto `main`, because `ASSERTS` is the one
   edge type carrying a bucket of its own (§11.1) and
   `entity_visible_predicate` arm 2 never fires for one stamped at a bucket
   that no longer exists.
4. **Empty registry rows are dropped; `default` keeps its row.** Eleven empty
   `kb-*`/pilot rows in `GET /api/buckets` is the opposite of "one bucket
   named `main`". `default` is exempt for §2's reason: every tenant
   implicitly holds it and `mark_bucket_dirty` MERGEs its row back on demand.

Journal (step 4): the `bucket` column is **rewritten** to `main` and the rows
under the two skip prefixes are **deleted**, both as idempotent DML on every
journal connect (`_migrate_journal_to_main`). Accepting re-dedup on
`content_hash` instead was rejected — `run_tick`'s own SELECT filters on that
column, so a stale value means those rows are simply never picked up again.
`last_synced_hash` is not set: the column belongs to §3.2's journal
extension, which this card did not ship.

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

## 14. Sequencing (rewritten by the §15 addendum)

A correction to this section's first version, forced by thinking through
what §15 actually depends on: **`restart core` gates only the connector**
(steps 4–5 — `ctx.state`/`state:own` are consumed by `fs_sync.py`, nothing
else). The aw-knowledgeable server work never needed it, and §15's retrieval
work needs *neither* the restart *nor* the sync engine — the 9,108
already-ingested documents already carry `source_path` (bulk-ingest always
sent it), so collection-scoped retrieval is shippable against the live
corpus **today**, buckets still `kb-*`, before any migration.

1. **Step 2b first: collections + anchors (§15).** aw-knowledgeable only.
   Chunk `source_path` denormalization + backfill, `(:Collection)` spine +
   backfill, the retrieval pre-filter, the anchor expansion, API/MCP params.
   No dependency on anything else in this doc; immediate user value
   (`collection=notion/kanban/` against `bucket=kb-notion` works the day it
   deploys — `notion/kanban/done/` was this example's original illustration
   but §19 flattens that status directory out of existence, so a
   status-scoped collection on the Kanban mirror is no longer expressible
   as a path prefix after that lands). Deploy is manual (service's own
   build first, then stack re-pull).
2. **Step 2a: sync server seams (§1, §2, §9)** — templates, endpoints,
   constraint, allowlist, backfill. Deployable alone; nothing calls the new
   seams yet. 2a and 2b touch the same files (`graph.py`, `documents.py`,
   `ensure_schema()`) — if parallelized across coders, 2b owns the retrieval
   templates and 2a the write seams, and whoever lands second rebases; the
   `ensure_schema()` migration slot is append-only so backfills compose.
3. `restart core` (human/deploy) → `state:own` live. Gates steps 4–5 only.
4. Migration (§8, steps 2–4) — operator-run scripts, one sitting. The §15.7
   collection-spine backfill re-runs (idempotent MERGE) after the bucket
   moves so `main`'s spine is complete.
5. Connector work (§3–§7) + manifest bump; ship via marketplace.
6. Enable the sync task; watch one full day of ticks; then (optionally,
   phase 2) inotify.

**Cost of the addendum on the old step 2:** roughly +50% on the
aw-knowledgeable half — ~6 new/changed Cypher templates, 1 constraint +
2 indexes, 2 one-time backfills, the §15.6 API rows and their validation-
matrix entries, K5/K5-ANN guard extensions, and the frontend Collection
node type in graph view + legend. The connector half grows only the §15.6
tool-schema passthrough (small). Nothing in §15 adds connector runtime work,
which is why 2b jumps the queue.

QA gates: K3/K5 green with the new templates; the direction-refusal 403;
the valve test (simulated empty tree → zero marks); two-worker lease test
(two processes, one tick runs); echo-loop test (pull → next scan no-ops).
Addendum gates: the §15.10-1 starved-collection escalation test; the
collection-scoped `mode=tree` bypass envelope test; the anchor∩collection
composition test; K5-ANN's third dimension.

---

## 15. Addendum (2026-10-10): collections and anchors — the third axis

The graph already separates **who can see** (bucket — permission + tenant,
rule 7 binds them 1:1) from **what it's about** (topic — derived from
content by the labeller). What Frederico is asking for is the third axis
the data already carries but retrieval cannot use: **where it came from** —
the source tree. Two named concepts, both delivered:

- A **collection** is a source-path prefix inside one bucket
  (`notion/`, `notion/kanban/`, `memory/`, `skills/`), used as a **hard
  scope filter** on retrieval. Deterministic, human-legible, full depth —
  what six flat buckets never gave. (`notion/kanban/<status>/` was a valid
  prefix at the time this was written; §19 removes the status directory,
  so a collection can scope to the whole Kanban mirror but no longer to
  one status within it.)
- An **anchor** is a node a traversal **starts from** — "answer this query,
  but start at this Notion card / this entity / this folder and follow the
  edges."

Verified for this addendum, beyond the header list: `graph.py:949-1004`
(the three ANN/exact/diagnose templates and their WHERE-on-window filter),
`graph.py:3431-3585` (`vector_search`'s escalation decision at `:3520`),
`graph.py:931` (`count_chunks_in_scope`), `graph.py:2591` (`get_graph`,
depth 1..2), `graph.py:4160` (`topic_document_counts`' PARENT_OF/COVERS
walk), `graph.py:4128` (`related_to_neighbors`), `api/search.py:159-378`
(`run_search`'s knob matrix; the `buckets is not None → no descent` bypass
at `:293`), `topics/retrieve.py:90` (`tree_search`),
`aw-app-knowledgeable/knowledgeable_app/mcp/client.py:321-363` + `:455`
(the `search_graph` passthrough and schema). Measured live 2026-10-10:
`search_graph(q="watchdog leader election flock", bucket="kb-notion",
mode="tree", include_traversal=true)` — the width-3 beam descent **pruned**
topic `t-kb-notion-l0-016`, labelled literally *"Watchdog leader election"*,
88 documents beneath, score 0.4414, while keeping "Thesis matching
methodology" (0.483) and "componente, documentação, bdd" (0.479). The
derived hierarchy is not a navigation structure you can trust; the folder
tree is.

### 15.1 The central decision: materialize the skeleton, derive the membership

**`(:Collection)` nodes are materialized; `CONTAINS` edges to documents are
not.** The folder tree becomes first-class nodes —

```
(:Collection {tenant, bucket, workspace_slug, path, label, external_id})
(:Collection)-[:PARENT {tenant}]->(:Collection)      // child -> parent
```

— but a collection's *document membership* stays **virtual**, derived at
read time from the one property that cannot drift because it IS the
identity key (§1): `d.source_path STARTS WITH c.path`. Splitting the
decision this way is the heart of the addendum:

- **Filtering never touches collection nodes.** The retrieval pre-filter
  (§15.2) is a string-prefix predicate on an indexed property — it works on
  the live corpus before any spine exists, keeps working if the spine is
  ever wrong, and adds zero joins to the hot path.
- **Anchoring and navigation get real nodes.** A collection appears in
  `get_graph`, can be the `anchor` of a search, renders in the UI as the
  deterministic navigational spine next to the derived topic tree.
- **No edge maintenance liability.** A materialized `CONTAINS` edge per
  document (~9,108 today) would have to be written by every path-keyed
  writer, migrated by every bucket move, and GC'd by every delete — and any
  drift makes the graph lie about exactly the thing the filter answers
  truthfully from the property. Membership that is *derived from identity*
  cannot disagree with identity.

Identity and shape:

- `path` is normalized: relative, no leading slash, **always
  trailing-slash-terminated** (`notion/kanban/`) so prefix matching is
  segment-safe (`notion/kan` can never match `notion/kanban/`).
- `external_id` is **deterministic**: `col-` + short hash of
  `(workspace_slug, bucket, path)` — a GC'd-and-recreated folder yields the
  same id, so saved anchors survive folder churn.
- `label` = last path segment, for display.
- Constraint, next to §1.1's:
  `CREATE CONSTRAINT collection_identity IF NOT EXISTS FOR (c:Collection)
  REQUIRE (c.tenant, c.bucket, c.workspace_slug, c.path) IS UNIQUE`.
- **Who writes them:** the server, not the connector — `upsert_document_by_path`
  (§1.1) MERGEs the full ancestor chain + `PARENT` edges in the same
  transaction as the document upsert, derived from `source_path` at the
  seam. Any future path-keyed writer maintains the spine for free. A
  backfill in `ensure_schema()`'s migration slot builds the spine for the
  existing corpus (§15.7).
- **Who deletes them:** nobody human. Collections are derived data (like
  topics, unlike documents): after a document hard-delete, a sweep removes
  collection nodes whose prefix matches zero remaining documents —
  including `pending_delete` ones, which are still members until approved
  (§15.7). No approval UI for collections; the §7 approval list stays
  documents-only.

### 15.2 Collection as filter: a PRE-filter in the vector search's own WHERE

The measured pruning above is why this cannot be a post-filter: a beam
descent that already discarded the right branch, or an ANN top-k that spent
its window on other folders, legitimately returns zero rows that match the
prefix afterwards. The filter must ride **inside** the search, next to
tenant and bucket. Mechanically, in `core/graph.py`:

1. **Denormalize `source_path` onto `(:Chunk)` at write time** — one more
   property in `write_chunks`' `chunk.properties` (`graph.py:920-924`),
   plus a one-time backfill via `PART_OF` (§15.7). This is what lets the
   collection predicate sit in the *same* WHERE as tenant/bucket in all
   three templates, uniformly on the node the index yields — not after the
   document join. Safe to denormalize because path identity (§1) makes
   `source_path` write-once per node: renames are delete+create (§12.1),
   so the copy cannot drift. (The §8 bucket moves already accept this
   pattern for `bucket`, which chunks carry since v2.)
2. `vector_search_ann` (`graph.py:949`): the window WHERE gains
   `AND ($collection IS NULL OR node.source_path STARTS WITH $collection)`.
   Same for the document side of the join, same as `<<in_scope:d>>` today.
3. `vector_search_exact` (`graph.py:987`): same predicate in the WHERE
   immediately on the MATCH, **before** `LIMIT $scan_cap` — with the new
   composite range index `(c.tenant, c.bucket, c.source_path)` this is a
   seek, so a narrow collection makes the exact scan *cheaper*, not a
   cap-burning full walk.
4. `count_chunks_in_scope` (`graph.py:931`) gains the same predicate.
   **Load-bearing:** `vector_search`'s escalation decision (`:3520`) is
   `len(deduped) >= k OR in_scope_total <= len(raw_rows)` — if the count
   ignores the collection, a small collection can never satisfy the second
   arm and every query over it escalates forever; worse, a count that
   overstates the corpus makes a complete answer look starved.
5. `vector_search_diagnose` (`graph.py:973`) gains a third CASE arm,
   `dropped_by_collection`, and `_starving_filter` (`:3588`) can now answer
   `"collection"` — the §2 rule that the escalation log names the starving
   filter extends to the new dimension, and K4's observable check with it.
6. The K5-ANN guard (`test_k5_ann_templates_filter_tenant_and_bucket`)
   grows the third dimension; the predicate is written `($collection IS
   NULL OR ...)` verbatim so the guard can see it.

Validation at the API seam (`api/search.py`, the §12 matrix):
`collection` is valid with `mode=semantic|tree` only (400 on lexical — the
link picker never scopes by folder); it **requires an explicit `bucket`**
(400 without: a collection is a subtree of one bucket's source tree and
inherits its tenant — rule 7 unchanged, §2 untouched, `main` stays the one
synced bucket). The value is normalized server-side (strip slashes, append
the trailing `/`, reject `..` and absolute paths). The envelope's `scope`
gains the axis: `scope: {bucket, collection: "notion/kanban/"}`. Pathless
ad-hoc documents have no `source_path` on their chunks and therefore match
no collection — correct: they are in no folder (§4's "adopt into
`authored/`" future action is also where they would join the spine).

### 15.3 `mode=tree` + explicit collection: bypass the descent, declared

**Decision: an explicit `collection` skips the topic tree entirely.** The
descent's job is to *find* the scope of the question; here the human handed
the scope over. Descending "within the subset" would mean re-scoring every
level's centroids against collection membership (the centroids aggregate
across folders, so they are wrong for the subset) and would still inherit
the hierarchy the live measurement just showed pruning its own best branch.
The precedent is already in the code: `run_search` at `search.py:293`
declines to descend when `buckets is not None`, for exactly this shape of
reason ("whichever bucket happened to be bound would be the wrong one").

Contract: `mode=tree` + `collection` runs the §15.2-filtered flat search;
the envelope says `strategy: "flat"` (the existing enum value — the
discriminator is `scope.collection`, already machine-readable; a new enum
value was considered and rejected, §15.8-5), `traversal.kind: "flat"` with
`reason: "collection_scope"`, and `not_applied: ["beam_width"]` if the
caller set one — §12's runtime-inapplicability rule, verbatim. Results in a
collection-scoped tree-mode call carry **no `topic_path`** (nothing was
walked to produce one); attaching it via a reverse COVERS walk per result
was considered and rejected (§15.8-6). `mode=semantic` + `collection` is
the same filtered flat search with today's `ann|exact` strategies — no
contract change beyond the filter and the scope field.

### 15.4 Anchor: what starts a traversal, what it walks, how it orders

**What can anchor:** a `(:Document)`, an `(:Entity)`, or a `(:Collection)`
— by `external_id`, plus the sugar `anchor="path:notion/kanban/"` resolved
to the collection node server-side. **`(:Topic)` cannot anchor**: topics
are rebuilt wholesale (a saved topic anchor dies on the next rebuild — the
same fragility behind the link-picker-leak card), and the measured descent
above is the standing evidence the derived hierarchy should not be a
navigation contract.

**Expansion** (new template family, `anchor_expand`): from the anchor,
collect the candidate **document set** —

- Document anchor: the document itself (hop 0) plus neighbours over
  `LINKS_TO {tenant}` and `RELATED_TO {tenant}` (both endpoints
  bucket-checked, the `graph.py:634-648` rule), up to `anchor_depth`.
  `related_vias` — the existing knob, same `{topic, embedding, entity}`
  vocabulary (`search.py:103`) — selects which `RELATED_TO` vias the
  expansion follows; default all three plus `LINKS_TO`.
- Entity anchor: documents reachable under the entity visibility rule
  (`graph.py:382-405` — a `MENTIONS` from a chunk in the reader's bucket,
  or an `ASSERTS` stamped with it) are the hop-1 set; further hops expand
  as document anchors do.
- Collection anchor: the member set by prefix (hop 0 — this is
  collection-as-anchor vs collection-as-filter made concrete: the anchor is
  a *starting set the edges may leave*, the filter is a *boundary results
  may not cross*), then edges as above.

`anchor_depth` ∈ 0..2, default 1 — `get_graph`'s clamp (`graph.py:2593`)
is the precedent, and depth 0 is meaningful ("answer from this card
alone"). Fan-out is capped per hop (`anchor_expand_cap`, a setting per §2's
"bounds are not optional", suggested 200 docs/hop); what the cap cuts is
declared as `anchor.truncated: true`, never silent.

**Scoring:** the candidate set is small and known, so no ANN — a new
`vector_score_documents` template scores the candidates' chunks exactly
against the query (the `vector.similarity.cosine` shape of
`vector_search_exact`, seeded by document ids instead of a bucket scan),
then the existing dedup-collapse and cut to `k` in `vector_search`'s
Python. `q` stays required (semantic modes already require it,
`search.py:225`; query-less neighbourhood *browsing* is `get_graph`'s job,
not retrieval's). **Ranking is by query score alone; `anchor_hops` is a
declared per-result field, never a rank input** — a hop-discounted blended
score was rejected (§15.8-7). Envelope: `strategy: "anchored"` (a genuinely
different algorithm earns a new enum value where a narrower filter did
not), plus `anchor: {id, kind, depth, vias, expanded_documents, truncated}`.

### 15.5 Composition: anchor ∩ collection

"Anchor on this Notion card, follow the entity edges, but stay inside
`memory/`" is:

```
search_graph(q=..., bucket="main",
             anchor="<card doc external_id>", anchor_depth=2,
             related_vias=["entity"], collection="memory/")
```

Rule: **edges are walked regardless of collection; the collection filters
which candidates are *kept*.** An expansion that hops *through* a document
outside `memory/` at depth 1 to reach one inside it at depth 2 is exactly
the point of anchoring — the filter is on result membership, not on the
path. (The inverse — constraining the walk itself — was rejected, §15.8-8.)
Both axes land in the envelope: `scope.collection` + the `anchor` block.
`anchor` + `mode=tree` bypasses the descent exactly as §15.3 does
(`traversal.reason: "anchor_scope"`), so the full matrix stays two rules,
not four.

### 15.6 API / MCP surface

`GET /api/search` (and `run_search`, so the Playground inherits it):

| Param | Validity | Semantics |
|---|---|---|
| `collection` | `semantic`/`tree`, requires `bucket` | §15.2 pre-filter; normalized prefix; `scope.collection` in the envelope. |
| `anchor` | `semantic`/`tree`, requires `bucket`, requires non-empty `q` | `external_id` of a Document/Entity/Collection, or `path:<prefix>` sugar. 404 if it doesn't resolve in the caller's tenant (existing K1 posture). |
| `anchor_depth` | only with `anchor` | 0–2, default 1. 400 outside the range, 400 without `anchor`. |
| (`related_vias`) | unchanged | With `anchor`: additionally selects the expansion's `RELATED_TO` vias (§15.4). Without: today's meaning exactly. |

`GET /api/graph` (`get_graph`): `focus` now also accepts a Collection
`external_id`; its neighbours are its `PARENT` parent, its child
collections, and its **direct-member** documents (prefix match, one extra
path segment), returned with synthesized `kind: "derived"` edges of type
`contains` — computed at read time, consistent with §15.1's
virtual-membership rule, same bounded shape as today (no whole-graph
endpoint — the `infra.md` rejection stands). Frontend: Collection node type
in the legend + graph view; the Library gains a collection breadcrumb
filter driven by the same `collection` param on the list endpoint (cheap:
one more STARTS WITH on `GET /api/documents`).

Connector (`aw-app-knowledgeable`): `search_graph` tool schema + passthrough
gain `collection`, `anchor`, `anchor_depth` (coerce-and-forward,
`client.py:321`'s existing pattern — the backend's validation matrix stays
the single source of truth); `get_graph` passthrough unchanged. Tool
descriptions must state the two-concept model in one line each: *collection
= stay inside this folder; anchor = start from this node.*

### 15.7 Impact on the decisions above

- **§1 (identity):** `upsert_document_by_path` additionally MERGEs the
  collection chain (§15.1) — same transaction, same template family.
  `write_chunks` carries `source_path` (§15.2-1). The §1.1 re-chunk on
  update keeps the chunk property (it re-writes chunks from the same
  document). New indexes: `(d.tenant, d.bucket, d.source_path)` and
  `(c.tenant, c.bucket, c.source_path)` range indexes — prefix seeks for
  the filter, the virtual membership, and the GC sweep.
- **Backfills** (both in `ensure_schema()`'s one-time slot, §8-1's
  pattern): (a) `c.source_path = d.source_path` via `PART_OF` for every
  chunk whose document has one; (b) build the Collection spine from
  `DISTINCT` ancestor prefixes of existing `source_path`s. Both idempotent;
  (b) re-runs after the §8 bucket moves so `main`'s spine is complete.
- **§7 (two-phase delete):** a folder vanishing from disk is only ever
  observed as its *files* vanishing — the ≥2-scans rule, the valve, and the
  approval list all operate on documents, unchanged. The collection node
  outlives its documents until the deletes are **approved**, then the GC
  sweep (same transaction as the hard delete, or the next tick) removes
  spine nodes with zero remaining members. `pending_delete` documents count
  as members — the Library's pending list can still show their breadcrumb.
- **§2 / rule 7:** unchanged. `(:Collection)` carries `{tenant, bucket}`
  like every node (the `graph.py:235` two-columns rule); collections live
  inside a bucket and inherit its tenant; `main` stays the single synced
  bucket — this addendum exists precisely so folder scope with full depth
  never argues for re-splitting buckets.
- **§13-roster:** the coders' risk list gains §15.10.

### 15.8 Rejected alternatives (addendum)

1. **Property-filter only, no materialization** (collection as pure prefix,
   anchors restricted to documents/entities). Cheaper, but a folder you
   cannot anchor or see in the graph is half of what was asked for —
   Frederico explicitly chose both, and the spine is what makes the folder
   tree a *navigational* counterpart to the topic tree rather than a query
   trick.
2. **Full materialization with `CONTAINS` edges to documents.** The edge
   duplicates what `source_path` already states, must be maintained by
   every writer/mover/deleter forever, and any drift is a lie in the graph.
   Virtual membership from the identity property cannot drift (§15.1).
3. **Post-filtering ANN/beam results by prefix.** The live measurement is
   the refutation: the descent pruned the literally-correct branch, so a
   post-filter over it returns zero legitimately. Dead on arrival.
4. **Descending the topic tree within the collection subset.** Re-scoring
   centroids per level against membership is expensive, and the subset
   centroids are wrong anyway (built over the whole bucket); the human
   already supplied the scope the descent exists to find (§15.3).
5. **A new `strategy` enum value for collection-scoped flat search.**
   `scope.collection` already discriminates machine-readably; a new value
   would make every existing strategy-switch consumer re-learn the enum for
   zero information gain. (Contrast `"anchored"`, §15.4, which IS a new
   algorithm.)
6. **Reverse-COVERS `topic_path` on collection-scoped tree results.**
   Possible (the `topic_document_counts` walk inverted, `graph.py:4160`),
   but it reports a tree that was *not walked* as if it had been —
   provenance theater. If wanted later it must be a separately-named field,
   not `topic_path`.
7. **Hop-discounted blended ranking for anchored search.** An unmeasurable
   blend of two honest signals; the consumer is an agent that can re-rank
   with both fields visible (`score`, `anchor_hops`).
8. **Collection as a traversal constraint (edges must stay inside).**
   Breaks the headline composition: "start at the card, follow entity
   edges, land in `memory/`" requires crossing folders mid-walk.
9. **Six buckets back, folders as buckets.** The standing restriction:
   buckets are permission boundaries (one tenant each, rule 7); folders are
   provenance with unlimited depth. One `main` bucket, collections inside
   it.

### 15.9 What the addendum makes harder later

1. **`source_path` now lives in two places** (Document + Chunk). The §12.1
   rename story, when it comes, must rewrite chunks too — the
   delete+create shape handles it today, but a future in-place rename
   optimization inherits a second property to move.
2. **`anchor_depth ≤ 2` bakes in shallow traversal.** Deep multi-hop
   research walks ("follow the chain five entities out") need different
   machinery — iterative expansion with its own budget, not a bigger clamp
   on this one.
3. **The envelope grows again.** `scope.collection`, `anchor{}`,
   `anchor_hops` — every consumer that pattern-matches envelopes (the
   Playground UI, agent prompts that teach the tool) needs the new fields
   taught, and the tool description is already long.
4. **Virtual membership means collection reads cost a prefix query** per
   `get_graph` focus — indexed and bounded, but a future "collection sizes
   on every node in the overview" feature would N+1 it; that feature needs
   a counted projection, not a loop.
5. **The deterministic `external_id` hash pins the triple.** If
   `workspace_slug` semantics ever change (multi-workspace merge), every
   saved collection anchor changes identity with it.

### 15.10 Risks for the coders (addendum)

1. **The escalation count is the trap.** If `count_chunks_in_scope` does
   not carry the collection predicate (§15.2-4), every narrow-collection
   query either escalates forever or returns starved results that look
   complete. The test: a 30-chunk collection, `k=10`, must answer
   `strategy: "ann"`-or-`"exact"` with the right rows *and* the right
   `escalated` flag — assert on the envelope, not just the rows.
2. **Chunk backfill before filter deploy, atomically.** The ANN predicate
   on `node.source_path` silently excludes every chunk the backfill hasn't
   reached (NULL fails STARTS WITH) — ship the backfill in the same deploy
   as the templates, and verify `count(c.source_path IS NULL AND d.source_path
   IS NOT NULL) == 0` before calling it live.
3. **Normalization is segment-safety.** Forgetting the trailing-slash rule
   makes `collection=notion/kan` match `notion/kanban/` — a wrong-scope
   result that looks right. Test the adversarial prefix explicitly.
4. **The spine MERGE must not resurrect GC'd parents mid-delete.** Order
   inside the delete transaction: remove documents, then sweep collections
   — a concurrent upsert re-MERGEing the chain is fine (it has a live
   member), but the sweep must re-check membership, not trust a cached
   count.
5. **`search_nodes` will offer Collections as link targets** the moment
   they carry `label` — the exact (:Topic) leak already on the backlog
   (`bug:aw-knowledgeable-topic-nodes-linkable-wedge-rebuild`). Land the
   label-restriction fix (Document|Entity only) in the same change that
   creates the first Collection node, or a human link onto a Collection
   wedges the GC sweep the way topic links wedge rebuilds.
6. **K5 applies to every new template** — `anchor_expand`,
   `vector_score_documents`, the spine MERGEs, the GC sweep: `{tenant,
   bucket}` on node patterns, `{tenant}` on relationships, `ASSERTS` with
   `bucket` (`graph.py:54`). The anchor expansion's `RELATED_TO` must
   bucket-check **both** endpoints (`graph.py:634-648`'s recorded reason).
7. **`anchor` + omitted `bucket` must 400 before resolution.** The anchor
   resolves inside one bucket's scope; resolving it against whatever
   `bucket_ctx` defaulted to answers from the wrong corpus with a valid-
   looking envelope.
8. **Entity anchors cross collections by design** — an entity's hop-1
   documents come from MENTIONS/ASSERTS visibility, not from any folder.
   Don't "optimize" the expansion by pushing the collection predicate into
   it; §15.5's keep-vs-walk rule is the contract.

---

## 16. Addendum (2026-10-11): the git backend, and the leading-dot rule that must land first

Card `3f65bf3b-9510-81ce-9fb7-ff2e67a10035`. Two features, one hard
ordering: the user names a git repo; each sync tick, after the graph work,
also commits and pushes the synced tree there — and **no path with a
leading-dot component may ever enter the scan**, because the git backend's
very first act is to put a `.git/` inside the tree the scanner walks.

Verified for this addendum, beyond the header list:

- `repos/aw-app-knowledgeable/knowledgeable_app/bulk_ingest.py:248-262` —
  `scan()` walks only the six `SUBTREE_ORDER` subtrees with
  `rglob("*.md")`; `is_skipped` (`:130`) is a bare prefix check.
- `repos/aw-app-kb/kb_app/kb_ops.py:162` —
  `dirs[:] = [d for d in dirs if not d.startswith(".")]`: the sibling
  consumer of the **same tree** already prunes dot-dirs at the walk;
  `kb_app/routes.py:78` skips dot-files; tested at
  `tests/test_kb_ops_pipeline.py:165-172` (`.hidden/inner.md` → zero
  upserts). The rule below is this idiom adopted, not invented.
- `repos/aw-app-knowledgeable/knowledgeable_app/routes.py:1-45` — the
  credential pattern this app already uses: secrets go to `ctx.secrets` via
  `POST /settings`, **never** through plain app config; the manifest's
  `x-secret` flag only makes the UI render a password field.
- `src/apps/secret_store.py:1-19` — `ctx.secrets`' backing store: Fernet-
  encrypted, one file per app at `<home>/secrets/<slug>.json`, no app can
  address another app's namespace.
- `repos/aw-app-knowledgeable/knowledgeable_app/playground_key_push.py` —
  the shared-vault (aw-app-secrets REST + `auto_approve_for`) read pattern;
  considered and not chosen (§16.8-5).
- `repos/aw-app-knowledgeable/aw-app.json` — `config_schema` with
  `service_secret` (`x-secret`), the contributed `agentic_output` task whose
  prompt enumerates failure cases, `notify_exit_codes: [1]`.
- `/opt/aw-workspace/.aw-workspace/knowledge_base/` — contains today
  exactly the six subtrees, **no dot-entries and no `.git`**: the rule can
  land with zero cleanup migration.

### 16.1 The leading-dot rule — prerequisite, shipped first

**Rule: any path whose KB-relative form contains a component starting with
`.` — directory *or file*, at any depth — is excluded from the scan.**
Directories are pruned **at the walk** (the `kb_ops.py:162` idiom:
`os.walk` with `dirs[:] = [...]`, never descended), files are skipped by
the same leading-dot test. It is a general rule, never a `.git` denylist —
`.github`, `.obsidian`, `.vscode`, `.venv`, `.DS_Store` are the same class,
and a one-name denylist is wrong the first time any other tool writes into
the tree.

Why this cannot wait for, or ride behind, the git backend — stated
honestly, because the current code half-shields us by accident:

- Today `scan()` never reaches a root-level `.git` at all (it walks only
  `SUBTREE_ORDER`, `bulk_ingest.py:248`) and matches only `*.md` (`.git`'s
  object store contains none). **Neither shield is load-bearing**: §3.1
  roots the sync engine's scan at `kb_root()`, which removes the subtree
  shield, and the `*.md` filter is an ingest-format choice that this very
  addendum's git scope already steps around (§16.3) — correctness must not
  hinge on it. The structural hole is real: `rglob` descends hidden
  directories, so a `.github/` or `.obsidian/` dropped inside `notion/` or
  `authored/` would ingest its `.md` content. **Verified against the live
  tree for this addendum: this is prevention, not remediation** — zero
  `.md` files under any dot-component path and zero dot directories exist
  in `knowledge_base/` today, so nothing is currently leaking and there is
  no cleanup to perform. The urgency is that the git backend's own `.git/`
  (§16.2) is what turns this structural hole into a live one, the moment
  that feature lands.
- **Dot-files are excluded too — decided, not left open.** Frederico's ask
  says "folder", but the file half is the severity driver: a `.env` in a
  synced tree becoming a searchable, vector-indexed graph document is a
  secrets exposure, not a tidiness bug. The `*.md` extension filter does
  not durably protect it (`.credentials.md`, a notes-app's `.trash.md`,
  and any future format widening all pass), the sibling kb app already
  skips dot-files (`routes.py:78`), and no legitimate corpus content is
  hidden-by-convention. Cost: zero — the tree has no dot-entries today.
- Reading of the verbatim `". "` (dot-space): leading dot. Nothing in the
  tree or the generators produces a literal dot-space name; the kb app's
  existing rule is leading-dot; `.git` itself — the stated motivation —
  is leading-dot.

Where it lands:

- `knowledgeable_app/bulk_ingest.py` — the rule goes into `scan()`'s walk
  **now** (a small, standalone change to today's code: replace the
  `rglob` with the pruned `os.walk`, extend `is_skipped` with the
  component test), and is inherited by the §3 shared scan helper when
  `fs_sync.py` extracts it. Enforced in the WALK, same reasoning as the
  existing `SKIP_PREFIXES` comment (`bulk_ingest.py:255-258`): a pruned
  path is never hashed, never journalled, can never win a canonical slot.
- `repos/aw-knowledgeable/backend/app/api/documents.py` — defense in
  depth at the §1.2 seam: `PUT /by-path` 400s any `source_path` with a
  leading-dot component, next to the `..`/absolute-path normalization
  §15.2 already specifies. A future buggy client cannot push `.git`
  content past a correct server.
- Journal/graph cleanup: none needed (verified above — no dot-entries
  exist, so no rows, no documents). If one ever existed, the §7
  pending-delete path handles it as an ordinary vanished file; no special
  case.

Verification (the card's own bar): a test tree containing `.git/` with a
planted `objects/x.md`, a `.github/README.md`, a nested `notion/.obsidian/
cache.md`, and a root `.env` produces **zero journal rows and zero
documents** — proven by the test mirroring
`aw-app-kb/tests/test_kb_ops_pipeline.py:165`, not by inspection.

### 16.2 The git backend: where the repo lives, what a commit contains (question B)

**`knowledge_base/` itself is the git worktree** — `git init` at
`kb_root()`, `.git/` living inside the tree (which is exactly why §16.1 is
a prerequisite). The commit is the **synced source tree, not a graph
export**: human-readable markdown in the same folder structure the
collections (§15) expose, so `git log -p memory/foo.md` answers "what
changed in this document and when" — the point of using git. The graph is
a projection of this tree (§0) and is rebuildable from it; committing a
graph dump would add churn with no provenance (rejected, §16.8-7).

Commit scope = the tree minus two subtractions, each enforced by the layer
that owns it:

- **Dot-entries** are excluded from the *scan* by §16.1 but handled by
  *git* natively (git never commits `.git/`; other dot-files follow
  `.gitignore`). Two deliberate dot-named files ARE committed —
  `.gitignore` and `.aw-kb-sync.json` (§16.5) — and §16.1 keeps both out
  of the graph automatically. The asymmetry is intentional: the scanner's
  dot rule protects the corpus, git's scope protects the repo.
- **`SKIP_PREFIXES`** (`mapped_folders/repos/`, `mapped_folders/
  aw-workspace/` — code maps, ~79% of the tree) are excluded from the repo
  via a **generated `.gitignore`** written from the same constant
  (`bulk_ingest.py:124`), asserted equal in a test so the two can never
  drift. Without it the repo swallows thousands of regenerable code-map
  files per sync.

Deliberate divergence from the ingest scope, stated: git commits non-`.md`
files and >10MB files that the scanner skips. Those filters are *ingest*
choices (format support, chunking cost); a PNG or a dataset in `authored/`
is still a KB artifact worth versioning. "What's in the repo" ⊇ "what's in
the graph", and the delta is exactly the two ingest filters — auditable.

### 16.3 Credentials (question A)

**Storage: `ctx.secrets`, key `git_token`, written only via the connector's
`POST /settings` — the exact `service_secret` pattern** (`routes.py:19-45`).
The manifest's `config_schema` gains `git_remote_url` (plain config — HTTPS
URL, **rejected if it carries userinfo** (`https://user:token@...` is a
credential in a config file), `git_branch` (default `main`), and
`git_token` marked `x-secret` — which, per this app's own docstring, is
only the UI password-field hint; the value itself never lands in plain
cloud-syncable config. It is read fresh on every tick, like `service_secret`
is per-call, so rotation needs no restart.

**Credential type: a fine-grained GitHub PAT scoped to the one named repo,
Contents read/write only.** This estate has already paid for the
alternative once — the aw-knowledgeable cross-repo PAT that turned out to
be a full-admin token. A repo-scoped PAT bounds the blast radius of a leak
to the mirror repo itself, whose entire content the credential holder could
already read from the tree.

**Injection: per-invocation, environment-only.** The subprocess git call
gets `GIT_CONFIG_COUNT=1`, `GIT_CONFIG_KEY_0=http.<host>.extraheader`,
`GIT_CONFIG_VALUE_0=Authorization: Basic <b64(x-access-token:PAT)>`, plus
`GIT_TERMINAL_PROMPT=0`. The token is never in argv (`/proc/<pid>/cmdline`
is world-readable; environ is not), never in the remote URL, never in
`.git/config`, never in a credential-helper file, never on disk anywhere —
the standing rule, kept mechanically.

**Absent or revoked secret fails visibly, in three places at once:**

1. The tick's git step records a declared status in the journal —
   `git_state ∈ {ok, no_secret, auth_failed, diverged, push_failed}` with
   `since` and detail — and `knowledgeable-sync run`/`status` surface it.
2. `no_secret`/`auth_failed`/`diverged` make the CLI exit **non-zero**,
   which the contributed `agentic_output` task (aw-app.json, `notify_exit_
   codes: [1]`) already escalates to an agent; its prompt gains the git
   cases next to the §7 valve case.
3. The connector's `/status` (what the window's `auth_status` widget binds
   to) gains the git backend state, so the settings panel shows "push
   blocked: credential revoked" instead of nothing.

A 401/403 from the remote is `auth_failed` from the first failing tick —
never retried silently into oblivion. The graph half of the tick is
untouched by any of this (§16.6).

### 16.4 Direction: push-only, and why the asymmetry is correct (question C)

Frederico's "on sync we will also commit/push" reads one-way, and **one-way
(workspace → git) is the design — deliberately, not by omission.** The
existing fs-sync is bidirectional between *tree and graph* with exactly one
conflict authority: LWW arbitrated through the journal's three-way base
(§3.3). The tree is the corpus's primary home (§3.3-3). Pulling from git
would add a **third writer** to that same tree with its own, different
conflict semantics (merge/rebase), and two overlapping conflict mechanisms
over one tree is how an edit gets silently lost — the exact failure §3.3
was designed to exclude. Git's role here is what S3 was rejected *as
authority* for in §11.1 but is genuinely useful *as mirror* for: versioned,
human-readable, offsite history. It asserts no authority: filesystem stays
truth (§16.6).

**Divergence policy: fast-forward-only, refuse loudly.** Before pushing,
the lease holder fetches; if the remote branch has commits the local
history lacks (a human edited on GitHub, another writer pushed, a
force-push happened), the git step does **not** merge, rebase, or
force-push — it sets `git_state: diverged` and escalates per §16.3. A
mirror that silently force-pushes destroys the one thing git adds
(history); one that auto-merges silently resolves conflicts in generated
content — both worse than stopping. Mechanically, a plain `git push`
already fails on non-fast-forward; the step classifies that failure rather
than fighting it.

**The door to phase-2 pull stays open, and cheaply, because scan-is-truth:**
a future `git pull --ff-only` executed before the scan makes remote edits
indistinguishable from local edits — the existing upsync propagates them to
the graph with zero new mechanism. What that phase must design (and this
one deliberately does not) is *scope*: an unrestricted pull would let a
GitHub edit to `notion/` overwrite generator-owned content until the next
notion-sync run; a pull restricted to `authored/` (sparse checkout or a
path-filtered merge) matches §4's write-direction contract. Named here so
the next card doesn't rediscover it.

### 16.5 Per-tenant / per-repo scoping (question F)

**Scope: one remote repo per workspace-install of the connector.** The
remote URL lives in this install's app config next to `bucket`; the tree
being committed is this workspace's `knowledge_base/` and nothing else; the
one synced bucket `main` belongs to one tenant (rule 7, §2). The chain is
airtight for a structural reason worth stating plainly: **the git backend
never consults the graph** — it commits a local directory tree — so no
query bug, no visibility-predicate gap, no cross-tenant Neo4j row can ever
leak another tenant's document into a commit. The two residual risks are
config-level, and each gets a mechanism:

1. **Two workspaces pointed at one repo** (copy-pasted config, cloned
   workspace): first push writes **`.aw-kb-sync.json`** at the repo root —
   `{workspace_slug, bucket, initialized_at}` — and every subsequent tick
   verifies it against `workspace_env("AW_WORKSPACE")` (§6, which already
   refuses to run when the slug is empty) before pushing. Mismatch →
   `git_state: diverged`-class refusal naming the claiming workspace.
   Dot-named deliberately: §16.1 keeps the marker out of the graph for
   free. The marker is advisory (someone can delete it remotely), but the
   failure it prevents is accident, not attack — the attack surface is the
   credential, bounded by §16.3's repo-scoped PAT.
2. **Pointing at a wrong, non-empty repo** (burying an existing project):
   a remote that is non-empty *and* has no marker refuses with a declared
   reason; adopting an existing repo requires placing the marker manually
   — one deliberate human act, same spirit as §7's delete approval.

Not per-bucket (there is exactly one synced bucket by design, §3.1), not
per-collection (collections are retrieval scope, not storage boundaries —
§15.8-9's folders-are-not-buckets rule applies unchanged).

### 16.6 Granularity, identity, failure isolation (questions D, E)

**One commit per tick that changed anything** — subject line
`kb-sync: +<created> ~<updated> -<deleted> [<workspace_slug> <tick-iso>]`,
body listing changed paths (bounded, first ~50 + count). Per-file commits
were rejected: the notion-sync burst (§3.3-4) would mint 1,564 commits of
pure noise and the 30s debounce already defines the natural batch. Author
and committer are fixed: `aw-kb-sync (<workspace_slug>) <kb-sync@
<workspace_slug>.invalid>`. The sync cannot know who edited a file (the
generators don't record authorship), so it must not fabricate attribution —
an agent-driven `force` reorg and a human edit both arrive as the bot, with
the *message* carrying what the tick knew. A future per-author story needs
authorship captured at write time, out of scope here.

**Failure isolation — the tick's order is scan → graph upsync/pull → git
commit+push, and the boundary is one-way both ways:**

- A git failure (`no_secret`, `auth_failed`, `diverged`, network) never
  rolls back or blocks the graph write that already happened, and never
  aborts the next tick's graph work. It sets `git_state`, exits non-zero,
  escalates (§16.3).
- A graph failure never blocks the commit: git mirrors the **tree**, not
  the graph, and the tree is truth for both (§3.3-3). When they disagree,
  the filesystem wins — graph and repo are both projections of it.
- **No retry bookkeeping exists, by construction:** a commit is a
  full-tree snapshot, not a per-file queue. Whatever a failed tick didn't
  push is still in the worktree; the next successful tick's commit
  contains it. `git_state` is the only persistent git-side state.
- One visible consequence, stated so nobody files it as a bug: the repo
  records a file deletion on the tick after the file vanishes, while the
  graph holds it in `pending_delete` awaiting §7 approval. The repo is a
  tree mirror, the approval gate is a graph gate — and git history is
  itself the recovery path for a wrongly-deleted file.

### 16.7 Multi-writer safety (question G)

**The git step runs strictly inside the §5 lease holder's tick — same
`ctx.state.lease.claim("fs-sync")`, no second coordination mechanism.**
Commit and push are stages of the one serialized sync tick, never a
separate task, schedule, or loop; the CLI door already funnels through the
same lease (§5). git's own `index.lock` remains as a backstop against
something outside the connector touching the repo, but it is not the
design's concurrency story — the lease is. Two workspaces are not two
writers to one remote because §16.5 forbids sharing a remote at all.

### 16.8 Rejected alternatives (addendum)

1. **A `.git` denylist** (or any name-list). The card itself rules it out;
   `.github`/`.obsidian`/`.venv` are the same class, and the list is wrong
   on the first unlisted name. The general rule costs the same line.
2. **Dot-folders only, dot-files still scanned.** The `.env` secrets class
   decides it (§16.1); the extension filter is not a durable shield, and
   the sibling kb app already skips both.
3. **A shadow clone** (commit from a copy under `data/knowledgeable/`
   instead of `.git` inside `knowledge_base/`). Avoids placing `.git` in
   the shared tree — but doubles disk, adds a copy pass that is a second
   scan, drifts from the tree it mirrors, and the dot rule is needed
   anyway (`.github` et al. arrive without git's help). Runner-up: revisit
   only if another consumer of `knowledge_base/` proves unable to tolerate
   `.git` (the known consumers are covered: kb app prunes dots, bulk
   ingest gains §16.1, bind-mounts don't care).
4. **`gh auth` / credential-helper auth** (the agent-side repos-push
   pattern). That path stores a broad user token in a plain file
   (`~/.config/gh/hosts.yml`) under a human identity — three rule
   violations for an unattended app loop. Apps hold their own narrow
   credentials in `ctx.secrets`.
5. **The shared vault (aw-app-secrets) with `auto_approve_for`** — the
   `playground_key_push.py` pattern. Works, but that pattern exists for
   secrets that *cross an app boundary*; this credential is consumed only
   by this app, which is exactly what `ctx.secrets` is for, and the
   approval-gated store would add a human interrupt (or a standing
   auto-approve entry) for no isolation gain. Runner-up if the repo
   credential ever needs to be shared across apps — it should not be.
6. **SSH deploy key.** Narrower than a PAT in theory, but the private key
   must exist as a file (or an agent daemon we don't run) for
   `GIT_SSH_COMMAND` to use — violating never-on-disk for real, today, to
   avoid a hypothetical. HTTPS + env-injected fine-grained PAT keeps the
   secret memory-only.
7. **Committing a graph export** (JSON/Cypher dump) alongside or instead
   of the tree. Opaque diffs, churn every tick, second source of truth —
   and the graph is rebuildable from the tree, so it versions nothing the
   tree doesn't.
8. **Merge or rebase on divergence.** Auto-merge silently resolves
   conflicts in generated files (the exact class §3.3 refuses to resolve
   silently); rebase rewrites published history; force-push destroys it.
   Fast-forward-or-stop is the only policy where the repo never lies.

### 16.9 What this makes harder later

1. **Push-only bakes in "the repo is read-only for humans."** People *will*
   eventually edit the mirror on GitHub; every such edit becomes a
   `diverged` stop until phase-2 pull (scoped to `authored/`, §16.4) is
   designed. The ff-only policy is also what makes that phase safe to add.
2. **`.git` inside the shared tree obligates every future consumer** of
   `knowledge_base/` to honor the leading-dot convention forever. Today's
   consumers all do or will; the convention must ride in the tree's
   documentation (`native-skills/aw-workspace/SKILL.md` per house rule).
3. **Repo growth is unbounded.** Full-history snapshots of a churning
   corpus (notion regenerates constantly) grow the pack store
   monotonically; an eventual shallow/squash/gc story is deferred — and
   history rewriting will then collide with the ff-only rule and need a
   deliberate re-initialization protocol.
4. **The marker scheme assumes repo-per-workspace.** A future "one repo,
   branch per workspace" consolidation redoes §16.5's guard (marker per
   branch, not per repo root).
5. **Env-injected HTTPS auth assumes an HTTPS remote.** An SSH-only
   self-hosted remote reopens the deploy-key question §16.8-6 closed.

### 16.10 Risks for the coders (addendum)

1. **Prune at the walk, not post-filter.** Replacing `rglob` matters:
   filtering `.git` paths *after* globbing still descends tens of
   thousands of object-store entries every 5-minute tick. Use the
   `kb_ops.py:162` `os.walk`/`dirs[:]` idiom so pruned trees are never
   entered.
2. **A hung git subprocess holds the fs-sync lease forever.** The lease
   releases on process exit (§5), not on tick timeout — a push to a
   blackholed remote with no subprocess timeout wedges every future tick
   on every worker. Hard timeout on every git call, `GIT_TERMINAL_
   PROMPT=0` so auth failure can never block on a prompt.
3. **Never enable `GIT_TRACE`/`GIT_CURL_VERBOSE` in this path** — both
   print the Authorization header, and the task-escalation flow would then
   mail the token to an agent transcript.
4. **The generated `.gitignore` must be asserted against `SKIP_PREFIXES`
   in a test.** If they drift, the repo silently swallows the ~79%
   code-map tree on the next tick — the failure is a huge slow push, not
   an error.
5. **Re-verify the marker on every config change, not just first push.**
   A user editing `git_remote_url` in settings must hit the §16.5
   non-empty-remote check again; caching "already initialized" on the
   install defeats the guard exactly when it's needed.
6. **Exit codes are the escalation channel.** The task contract fires on
   non-zero (`notify_exit_codes: [1]`); a git step that catches its own
   failure, logs it, and returns success has silently disabled §16.3's
   entire visibility story. Classify, record `git_state`, *then* exit
   non-zero.
7. **The dot rule changes `is_skipped()`'s contract** from prefix-only to
   component-aware; the §3 helper extraction must carry the test tree from
   §16.1's verification with it, or the fs_sync rewrite quietly reverts to
   prefix-only.
8. **Commit after the upsync, inside the same tick.** The §3.3 debounce
   already guarantees ≥30s of quiet before the tick acts; committing in a
   separate loop reintroduces the half-written-burst snapshot the debounce
   exists to prevent.

### 16.11 Sequencing — recommended card breakdown

The dot rule is deliberately **not** bundled into the git-backend card: it
is a two-function change to *today's* `bulk_ingest.py` plus a test, with no
dependency on `fs_sync.py`, the lease, or `restart core` — and it must be
live before any `.git` ever appears in the tree.

1. **Card 1 — leading-dot exclusion (ship immediately, no dependencies).**
   `bulk_ingest.scan()` walk prune + `is_skipped` component test + the
   §16.1 test tree. Optionally in the same card: the server-side
   dot-component 400 lands with §1.2's PUT seam whenever that card runs
   (it has no standalone seam to land in before then — note it on that
   card, don't block this one).
2. **Card 2 — git backend** (depends on card 1 and on the §14 step-5
   connector work: the tick, the lease, `fs_sync.py`). New module
   `knowledgeable_app/git_backend.py` (subprocess git, env credential
   injection, marker check, failure classification, `.gitignore`
   generation); `routes.py` settings + `/status` additions; `aw-app.json`
   config keys + task-prompt cases + version bump; the §16.3/16.4/16.5
   failure-path tests.

Within §14's numbering, card 1 can run **today**, in parallel with step 2b
— it touches only the connector's existing scan and needs no restart. Card
2 joins step 5.

QA gates (addendum): the §16.1 zero-rows test tree; marker-mismatch
refusal; revoked-PAT tick → `auth_failed` + non-zero exit + graph write
intact; diverged remote → no force-push, declared stop; dead remote →
tick completes graph work and the *next* tick still runs (lease not
wedged); `.gitignore` ≡ `SKIP_PREFIXES` assertion.

---

## 17. Amendment (2026-10-12): journal-blind documents — delete detection must key on the path, not the journal row

Written against card `3f75bf3b-9510-8155-a345-c9b812686f9d`, after cards
A/B/D shipped and went live. **Design, not code** — the implementation is
two cards, §17.7.

### 17.1 The defect, measured

§3.3/§7 as built (card B, `fs_sync._mark_missing_as_deleted`) iterates
**journal rows** and marks by each row's `external_id`
(`aw-app-knowledgeable/knowledgeable_app/fs_sync.py:382-418`). The journal
is one-row-per-path (`relpath` is the PK — `bulk_ingest.py`'s schema); the
graph can hold **several documents per path** (duplicate uploads through
the old content-hash MERGE, before §1's path-keyed identity existed —
today's 35 `workspace_slug IS NULL` losers from card A's backfill, but the
class is open: any client that ever PUTs by-path or POSTs with a
`source_path` outside the connector creates more).

So a document with no journal row pointing at it is **permanently
unreachable by delete detection** — not stale, not racy; no code path ever
visits it. Live on production 2026-10-12: top-level `cli_reference/` is
gone from disk, 70 documents still claim those 35 paths, exactly 35 got
marked (the journaled winners) and 35 structurally never can be (the
losers). They still answer `knowledge_search` — the precise failure this
whole design exists to end.

Two aggravating facts any fix must respect:

- **The journal's document and the surviving graph document are not
  reliably the same one.** Card A's backfill picked duplicate-group
  winners by Cypher visit order during a crashed deploy (QA verified a
  live group where the *older* upload won). Dereferencing `external_id`
  to decide anything about "the" document at a path is unsound.
- **Already-marked rows are skipped forever.** The engine's
  `not row["pending_delete"]` guard means a path marked once is never
  revisited — correct for the per-id mechanism, but it means the live 35
  cannot be healed by the structural fix alone; they need an explicit
  catch-up (§17.4).

### 17.2 The decision: mark by path, adopt paths (not documents) into the journal

The journal's unit is the **file**; the graph's unit is the **document**.
The bug is the 1:1 cast between them at the marking step. Fix the cast,
keep everything else:

1. **Marking fans out server-side.** A new service-only seam,
   `POST /api/documents/mark-delete-by-path`, marks **every** document
   claiming `{tenant, bucket, source_path}` with
   `workspace_slug = $slug OR workspace_slug IS NULL` — winners, losers,
   and anything a future client minted. The connector stops dereferencing
   `external_id` for deletes entirely; `_mark_missing_as_deleted` calls
   by-path instead of by-id. Pending-delete only — **no `force` variant
   exists on this seam**, by design.
2. **Paths the journal has never heard of get adopted — as paths.** A new
   enumeration seam, `GET /api/documents/sync-paths` (paged; distinct
   `source_path` + claimant count, same slug-or-NULL filter), lets the
   tick insert a journal row (`origin='adopted'`, `external_id` NULL) for
   any server-claimed path the journal lacks. From then on the **existing**
   machinery covers it: the same ≥2-consecutive-scans clock, the same
   valve, the same by-path mark. Adoption never marks anything — it only
   starts observation.
3. **One mechanism, one tick.** Both changes live inside
   `fs_sync.run_tick`'s existing pass, strictly **after** the §7 valve
   check. No second engine, no second schedule, no second source of truth
   for missing-ness: the journal remains the only holder of per-path scan
   state.

A pleasant consequence of adopting paths rather than documents: an adopted
path whose file **does** exist on disk converges into a normal synced row
on its own — the scan sees the row, debounces, PUTs by-path, and journals
the winner's `external_id` from the response. Adoption heals live paths
and watches dead ones with the same rows.

### 17.3 Adoption exclusions (each one is a correctness rule, not tidiness)

- **`is_skipped()` paths** (the `SKIP_PREFIXES`): a document claiming a
  skipped path mirrors a file the scan deliberately never looks at — its
  file may exist. "Missing from source tree" would be a lie; those
  documents stay outside sync's contract, like the 73 pathless ad-hoc
  uploads (explicitly out of scope, unchanged).
- **`authored/` paths, until card C lands**: pre-downsync, a
  human-authored document legitimately exists server-side with no file on
  disk yet. Adopting it would mark card C's future downsync payload for
  deletion. Card C removes this exclusion deliberately (its journal-first
  echo-breaker rows make it moot).
- **Paths claimed only by other `workspace_slug`s**: not this connector's
  documents. NULL-slug claims are adopted by this (single) workspace's
  connector — see §17.6-3 for what that bakes in.
- **Dot-paths**: already impossible on both sides (§16.1 walk-prune +
  server 400); the enumeration must still never return one.

### 17.4 The one-time catch-up for the live 35

For every journal row already `pending_delete=1`, re-issue
`mark-delete-by-path(relpath)` once — idempotent on the server (`SET` on
an already-marked document is a no-op), and it sweeps the unmarked
siblings at those 126 paths, which includes all 35 `cli_reference/`
losers. This runs inside a valve-cleared tick like everything else (gated
by a journal flag so it runs once per upgrade, not per tick). After it,
the structural fix keeps the invariant: **a marked path means every
claimant at that path is marked.** Approving the `cli_reference/` backlog
afterwards remains a human act in card D's UI — nothing here deletes.

### 17.5 Valve coverage — designed in, not bolted on

Everything new runs strictly after `_check_mass_delete_valve`, and the
valve's arithmetic widens to see the new rows:

- **Guard 1 (zero-file / unreadable root) is unchanged and still covers
  everything**: adoption, catch-up and marking all sit behind it. A
  transiently-unmounted tree aborts the whole tick before any row's state
  moves — same as today.
- **Guard 2's tracked set widens** from `status='uploaded'` rows to *rows
  participating in missing-tracking*: `external_id IS NOT NULL OR
  origin='adopted'` — numerator (missing-now count) **and** denominator
  (threshold base) both. Without this, a mass disappearance of adopted
  paths would be invisible to the valve, which is the exact failure mode
  §7 exists for.
- **A second, in-pass ceiling on documents**: by-path marking fans out
  (one path can mark N documents), and guard 2 counts paths. The marking
  pass therefore also sums the per-call `marked` counts the server
  returns and **stops marking** when the tick's document total would
  exceed the same `max(25, 10%)` threshold — sets the valve flag, exits
  non-zero, defers the remainder. Marking is idempotent and two-phase, so
  deferral is free.
- **Adoption cannot amplify a wipe past the valve**: adoption itself never
  marks, and the paths it adds are inside guard 2's tracked set from the
  next tick on. First-tick-after-wipe: guard 1 or guard 2 aborts before
  adoption runs at all.
- **Two-phase with human approval is untouched.** Both new seams mark;
  only card D's live UI deletes. No `force` on the by-path seam, no new
  deletion path of any kind.

Behavior on a transiently-unmounted tree: guard 1 aborts, nothing moves.
On a half-written tree (e.g. `agent sync` caught mid-write): the ≥2-scan
clock absorbs anything shorter than one scan interval; a wider gap trips
guard 2 (the missing count exceeds the threshold) and the tick marks
nothing. A path that reappears resets `missing_scans`, which now shields
*every* claimant at that path, journaled or not.

### 17.6 Rejected alternatives, and what this makes harder later

Rejected:

1. **Pure server-side reconciliation** (enumerate documents whose file is
   missing, server-driven): the server cannot see the filesystem — it
   runs on bare-metal aw-stack; the tree lives in the workspace
   container. Any server-resident "missing file" notion would be a second,
   blind source of truth needing its own 2-scan state. The connector owns
   the tree; the journal already is the state store.
2. **Adopting documents (one journal row per document)**: breaks the
   `relpath` PK, fabricates rows whose `sha256`/`last_synced_hash` mean
   nothing, and makes `bulk_ingest`'s own queries see phantom files.
   Paths are what the journal models; adopt paths.
3. **A separate operator-triggered sweep**: a second code path that
   drifts from the first, covers nothing continuously (the duplicate
   class is open, §17.1), and would need its own valve from scratch. The
   §17.4 catch-up delivers the sweep's one legitimate job inside the
   existing engine.
4. **Connector enumerates per-path document ids and marks each by id**
   (no new server write seam): more round trips, pushes "which document
   is which" back into the connector — the exact unsound dereference
   §17.1 forbids — and leaves the winner/loser distinction load-bearing.
   By-path marks the set atomically without ever naming a winner.

Harder later:

1. **Journal rows stop meaning "a file this connector uploaded."**
   `origin='adopted'` rows have no upload history and a NULL
   `external_id`; any future code that assumes `external_id IS NOT NULL`
   on tracked rows (or that `sha256` reflects a local read) must filter
   by origin. This is the price of one state store instead of two.
2. **`mark-delete-by-path` is a fan-out write seam.** Service-only and
   two-phase, but the server cannot know tree state, so the valve
   protecting it lives client-side in the connector. A future non-connector
   service caller inherits no valve. Mitigation accepted as-is: the seam
   marks (reversible), never deletes.
3. **NULL-slug documents are adopted by whichever workspace's connector
   syncs the bucket.** Correct while `aw` is the only workspace writing
   `main`; a second workspace on the same bucket would need the NULL-slug
   claim rule revisited before it syncs.
4. **The `authored/` exclusion is a tripwire for card C.** It must be
   removed deliberately when downsync lands; forgetting it means
   human-authored documents whose files were deleted on disk are again
   invisible to delete detection — a smaller re-instance of this very bug,
   confined to one subtree. Card C's card must name it.
5. **Decline/restore does not reset the journal.** A human declining a
   mark leaves `pending_delete=1` journal-side, so the engine will not
   re-mark that path if it goes missing again later (pre-existing
   behavior, now slightly wider since marks cover more documents). The
   clean fix is card C's changes feed (`sync_status` is already in it);
   until then a declined path that dies again needs the catch-up flag
   cleared by an operator. Named, not solved here.

### 17.7 Implementation cards (dependency order) and acceptance gates

**Card G1 — server: the two path-level seams (`repos/aw-knowledgeable`).**
`POST /api/documents/mark-delete-by-path` (service-only, marks all
claimants at `{tenant, bucket, source_path}` with slug-or-NULL, returns
the marked ids/count, no `force`) and `GET /api/documents/sync-paths`
(paged distinct path-claims, same filter, includes paths of
already-marked documents). Both reuse `validate_source_path`. Gates:
- Marks the NULL-slug sibling in a real duplicate group, and the call is
  idempotent (re-run marks 0 new).
- Human caller → 403 with a reason body; dot-path → 400.
- `sync-paths` returns a path whose only claimant is already
  pending-delete (catch-up idempotence depends on this).
- K3/K5 green — both templates tenant+bucket scoped; the slug-or-NULL
  `WHERE` needs a K5-shaped test, not an exemption.
- Live (throwaway data, then cleaned): create a duplicate group on
  production via `POST` with `source_path` (legacy shape) + `PUT by-path`
  (migrated shape) at one path; by-path mark → both marked; `force`
  cleanup; graph back to pre-test state.

**Card G2 — connector: path-keyed marking, adoption, catch-up, valve
widening (`repos/aw-app-knowledgeable`).** Depends on G1 deployed and
verified live. Gates:
- `_mark_missing_as_deleted` marks via by-path; the `external_id`
  dereference for deletes is gone.
- Adopted row (no `external_id`) is NOT marked on first miss, IS marked
  via by-path on the second — i.e. the old `not row["external_id"]` guard
  demonstrably no longer excludes adopted rows.
- Adoption excludes `is_skipped()` paths, `authored/`, and foreign-slug
  claims (three tests).
- Adopted path whose file exists converges to a normal synced row
  (upload happens, `external_id` lands from the PUT response).
- Valve: guard 2 counts adopted rows in numerator and denominator;
  the in-pass document ceiling stops marking, sets the flag, exits
  non-zero (simulated fan-out group exceeding the threshold).
- Catch-up: runs once (flag-gated), re-issues by-path for
  `pending_delete=1` rows, idempotent across restarts.
- Zero-file tree: still aborts before adoption or catch-up touch anything.
- Live (coordinator-driven, post-release): after two ticks, production's
  marked count rises from 126 paths' worth to include the 35
  `cli_reference/` losers (graph query: 0 documents with
  `source_path STARTS WITH 'cli_reference/'` and no `sync_status`);
  valve untripped; card D's UI lists them with reason. **Nothing
  deleted** — approving the backlog stays a human act in card D's UI.

Out of scope, unchanged by this amendment: the 73 pathless documents,
card C (downsync), card F (soak), entity extraction (off), Redis.

## 18. Amendment (2026-10-13): mark/restore are invisible to the changes feed — status transitions must move `updated_at`, approvals must be inferred from `sync-paths`

Supersedes §17.6 point 5's claim that "the clean fix is card C's changes
feed (`sync_status` is already in it)". QA on card C
(`3f85bf3b-9510-8105-85b3-f85101fcbdb0`, 2026-10-13) proved that claim
half-true in a way that makes it false: the feed's **payload** does carry
`sync_status` (`document_changes`,
`repos/aw-knowledgeable/backend/app/core/graph.py:707-718`) but its
**key** — the `WHERE`/`ORDER BY` on `(updated_at, external_id)` — never
moves on a mark or a restore. `mark_document_pending_delete`
(graph.py:722) and `restore_document` (graph.py:729) SET/REMOVE only the
three `sync_status`/`pending_delete_*` properties; the only template that
bumps `updated_at` is `upsert_document_by_path` (graph.py:683), and only
on a real content change. So card C's restore branch
(`repos/aw-app-knowledgeable/knowledgeable_app/fs_sync.py:400-408`) is
correct code that can never run: the transition it listens for is
structurally absent from the one stream the pull pass consumes. Measured
live: the 18 documents Frederico restored through card D's UI on
2026-10-12 all still show `rev: 1` and `updated_at` from 2026-10-03/08.

Three decisions, one per QA question.

### 18.1 Decision 1: marks and restores bump `updated_at`; `rev` stays content-only

**The feed is the connector's single source of remote mutations; a feed
that omits a mutation class is a trap for every consumer. Fix the feed,
not the consumer.** `SET d.updated_at = datetime()` is added to all three
status-writing templates in graph.py:

- `mark_document_pending_delete` (graph.py:722) — card D's per-document
  mark, and decision 5's agent path (the `force` branch deletes the node
  and needs nothing).
- `restore_document` (graph.py:729) — the decline path. The `REMOVE`
  stays; one `SET` is added alongside it.
- `mark_documents_pending_delete_by_path` (graph.py:747) — the §17.2
  fan-out. Its existing `WHERE ... <> 'pending_delete'` idempotency gate
  means a re-run matches 0 rows and re-stamps nothing, so no feed churn.

**`rev` is NOT bumped, and this is a load-bearing invariant, not a
stylistic preference.** The pull pass's `rev <= remote_rev` gate
(fs_sync.py:412-415) sits *after* the restore branch and *before* the
`authored/` conflict resolution. A status-only transition therefore
surfaces on the feed, fires the journal reset, and then short-circuits at
the rev gate — no content pull, no conflict comparison, no file write.
Bump `rev` on a restore and that shield breaks: an unchanged-content
restore of an `authored/` document would flow into §3.3 step 3's
last-change-wins comparison with a freshly-minted remote timestamp and
overwrite local edits with identical-but-"newer" remote content. The
semantics after this amendment, to be stated at §1.2: **`rev` = content
version (bumped only with `content_hash`); `updated_at` = feed cursor key
(any sync-visible mutation: content, mark, restore).** Any future status
transition added to the graph must follow the same split.

Idempotency note for the single-id mark (graph.py:722): unlike the
by-path template it has no already-marked guard, so a re-mark re-bumps
`updated_at` and the document re-surfaces on the feed once. The
connector's handling is a no-op either way (journal already
`pending_delete=1`, feed `sync_status='pending_delete'` fails the
fs_sync.py:400 condition). Adding the same guard for symmetry is
harmless and optional.

### 18.2 Decision 2: restore semantics — re-arm on file reappearance, not immediate re-detection

Card C's reset (`pending_delete = 0, missing_scans = 0`,
fs_sync.py:400-408) is amended, because once restores actually arrive
(18.1) it creates a human-vs-engine loop: the 18 declined documents'
files are *still missing from disk* — that is why they were marked — so a
bare reset re-enters them into delete detection and
`_mark_missing_as_deleted` (fs_sync.py:895) re-marks them two ticks
later. The operator declines again, the engine marks again, forever. A
decline means "keep this document even though its file is gone", and the
journal must encode that.

**On an observed restore (feed or catch-up), the row is cleared AND
suppressed: `pending_delete = 0`, `missing_scans = 0`, plus a new journal
column `restore_suppressed INTEGER DEFAULT 0` set to 1** (idempotent
hand-rolled ALTER in `bulk_ingest._ensure_table`, this repo's
convention). While suppressed:

- `_mark_missing_as_deleted` skips the row (alongside its existing
  `pending_delete` skip at fs_sync.py:915).
- The scan/upload loop clears the flag the first time it sees the file
  present at that path. From then on detection is fully re-armed — a file
  that comes back and later disappears again is re-marked normally, which
  closes §17.6-5's original gap *without* the nag loop.

### 18.3 Decision 3: approvals never reach the feed — GC journal rows whose claimants are gone, and narrow the valve's numerator to mark-eligible rows

A hard delete removes the node; `document_changes` MATCHes live nodes, so
an approval is structurally invisible to the feed *even after 18.1* — no
tombstone exists (rejected, §11-8) and none is added. The ~107
approved-and-deleted journal rows (and every future approval) need a
different signal, and the connector already has one for free: the
adoption pass pages `GET /api/documents/sync-paths` **every tick**
(fs_sync.py:567; server seam documents.py:495, `list_sync_paths`
graph.py:3336), and that enumeration deliberately includes
already-marked claimants (§17.7 G1 gate). Absence from it is therefore
server-truth that the last claimant at a path is gone.

**Steady-state GC, every tick, after the adoption pass:** delete every
journal row with `pending_delete = 1` whose relpath is absent from the
just-fetched sync-paths set. Guards, zero-trust like §7 guard 1: GC runs
only when the enumeration completed fully (every page, no error) and
returned a nonzero path set while the journal is nonempty; GC touches
only `pending_delete = 1` rows — a *live* row whose claimant vanished is
the §17 journal-blind family, not deletion evidence, and is never GC'd.
Dropping the row is safe: if a file later reappears at that path, the
scan treats it as new and re-uploads, which is the filesystem-is-primary
answer.

**Valve numerator (§7 guard 2, fs_sync.py:203-244):** the numerator
`missing_now = tracked_paths - found` currently counts rows that cannot
produce a new mark. The valve protects one thing — the number of *new*
marks a tick could initiate — so the numerator query is narrowed to
mark-eligible rows: `_TRACKED_ROWS_WHERE` plus `pending_delete = 0 AND
restore_suppressed = 0`. The denominator (`_tracked_row_count`,
fs_sync.py:196) stays the full tracked set — the threshold remains "10%
of the corpus the journal tracks". With GC removing the ~107 dead rows
from both sides and suppression removing the 18 from the numerator, the
132-of-206 permanently-consumed headroom QA measured returns to ~0.

### 18.4 One-time catch-up for the already-stuck rows

The feed cannot retroactively surface the 18 restores — the cursor is
past their stale `updated_at`, and 18.1 does not rewrite history. A
flag-gated one-time pass (same shape and table as §17.4's
`CATCHUP_FLAG`, fs_sync.py:104/297-304; new flag
`restore_reconcile_catchup_v1`): fetch the server's currently-marked set
via `GET /api/documents?pending_delete=1` (documents.py:69-85), then for
every journal row `pending_delete = 1`:

- path has a live claimant in sync-paths but is NOT in the marked set →
  it was restored → apply 18.2 (clear + suppress);
- path IS in the marked set → genuinely awaiting approval → untouched;
- path has no claimant in sync-paths → 18.3's steady-state GC already
  collects it; the catch-up does nothing extra.

Flag set only when the pass completed without errors; the operations are
idempotent, so a crash mid-pass re-runs clean. This absorbs the open
backlog card "Pre-card-C declines left 18 journal rows stuck at
pending_delete" (FindingKey
`knowledgeable:fs-sync-stale-journal-pending-rows-after-decline`) — its
one-time reconciliation is this section.

### 18.5 Rejected alternatives

1. **Connector-only fix: reconcile restores by polling
   `?pending_delete=1` every tick, never touch the server.** Works, no
   deploy coupling — and leaves the changes feed permanently lying about
   the mutation class its own payload advertises (`sync_status` is IN the
   response shape). The next feed consumer inherits the trap QA just
   paid to find. One `SET` per template is cheaper than a second
   standing consistency mechanism.
2. **Bump `rev` as well, "a transition is a revision".** Breaks the rev
   gate's shield exactly as 18.1 describes — status-only restores of
   `authored/` paths would overwrite local edits. `rev` drives content
   decisions (pull, conflict, `base_hash`); polluting it buys nothing
   the `updated_at` bump doesn't already deliver.
3. **Tombstones for approvals** (`sync_status: 'deleted'` kept on the
   node) so deletes ride the feed too. Re-rejection of §11-8: dead nodes
   keep matching searches, collections, and §15 member counts unless
   every reader learns to exclude them. The sync-paths absence rule
   gets the same information from a seam that already exists.
4. **Server-side one-time backfill** (bump `updated_at` on restored
   docs so the feed replays them). Impossible as specified: `restore_
   document` REMOVEs all three status properties, so restored documents
   are indistinguishable from never-marked ones server-side. The
   connector's journal is the only place that remembers the 18.
5. **Exclude stuck rows from the valve's denominator too.** Shrinking
   the denominator shrinks the threshold — it makes the valve *more*
   trigger-happy as rows get suppressed, punishing exactly the corpus
   state the operator just blessed. Numerator-only narrowing keeps the
   threshold stable at 10% of tracked reality.

### 18.6 What this makes harder later

1. **`updated_at` stops meaning "content last changed".** Any future
   consumer wanting content recency must read `rev`/`content_hash`. The
   §3.3 conflict rule keeps using `change["updated_at"]` as remote
   mtime and stays correct only because the rev gate filters
   status-only changes before the comparison — bump `rev` in any future
   status transition and that correctness silently ends. Stated as an
   invariant in 18.1; it is the one rule a future editor most needs.
2. **GC bakes in §17.6-3's single-workspace assumption.** Absence from
   sync-paths means "deleted" only while the slug-or-NULL filter
   enumerates everything that could claim a path. A second workspace
   syncing the same bucket revisits the NULL-slug rule (§17.6-3) and
   must revisit 18.3 in the same breath.
3. **A third journal row regime.** Tracked, adopted (§17.2), and now
   suppressed. Every future pass over the journal must decide how it
   treats `restore_suppressed = 1`.
4. **Feed replays after mass operations.** A 161-document mark/restore
   event now lands on the feed and forces the connector through full
   pages of status-only rows. Bounded by `CHANGES_PAGE_LIMIT` paging
   and cheap no-op handling, but a future mass-status tool should know
   it is writing to the feed.

### 18.7 Implementation cards (dependency order) and acceptance gates

**Card H1 — server (`repos/aw-knowledgeable`): `updated_at` on the three
status templates.** Tiny diff, graph.py only, plus tests. Gates:
- Mark → the document surfaces on `GET /api/documents/changes` past the
  old cursor with `sync_status: 'pending_delete'` and unchanged `rev`;
  restore → surfaces again with `sync_status: null`, `rev` still
  unchanged. Both as template-level tests starting from the *deployed*
  node shape (the schema-test blindness lesson: fixtures must not wipe
  only what the test creates).
- By-path mark re-run matches 0 rows and moves no `updated_at` (feed
  idempotency).
- K3/K5 template-scoping tests still green for all three templates.
- Live (throwaway doc, then force-cleaned): mark → poll changes →
  present; restore → poll → present; `rev` unchanged throughout.
  Verified in the running container, not by green CI (deploy lesson:
  a green release can ship old code).

**Card H2 — connector (`repos/aw-app-knowledgeable`): suppression, GC,
valve numerator, catch-up.** Depends on H1 deployed and verified live
(its feed-path gates cannot pass against a pre-H1 server). Gates:
- Restore on the feed → row cleared AND `restore_suppressed = 1`; two
  subsequent ticks with the file still missing → NOT re-marked.
- File reappears → flag cleared; file disappears again → marked after
  the normal 2 scans (§17.6-5 closed, loop-free).
- GC: `pending_delete=1` row whose path is absent from a fully-fetched
  sync-paths set → row deleted; same row with an errored or empty
  enumeration → untouched; live (`pending_delete=0`) row with absent
  claimant → untouched.
- Valve: suppressed and pending rows out of the numerator, denominator
  unchanged; a simulated 18-suppressed/107-dead corpus shows headroom
  restored.
- Catch-up: flag-gated, runs once, crash-idempotent; the three-way
  split of 18.4 each covered by a test.
- Live, post-deploy (coordinator-driven): journal `pending_delete`
  count drops from 132 to match the graph's (6 at last measure); the
  18 restored paths show `restore_suppressed = 1`; guard-2 numerator
  measured near 0 on a quiet tree.

Out of scope, unchanged: pathless documents, tombstones, the §17.6-3
multi-workspace claim rule, card F (soak).

## 19. Amendment (2026-10-13): the Kanban mirror's status directory turns every card move into delete+create — fix the writer, not the sync

Written against card `3f85bf3b-9510-8196-9799-c11a50fbd85e`, after H1/H2
landed and their live gates passed. **Design, not code** — the
implementation is one card, §19.7, and it lands in a repo this document
has not touched before: `repos/aw-app-notion`. fs-sync itself — server,
connector, valve, feed — changes **nothing** under this amendment.

### 19.1 The finding, confirmed

The Kanban mirror encodes a card's status as a directory:
`notion/kanban/<status>/<slug>.md`. Moving a card between columns is
therefore a rename on disk. fs-sync has no rename concept — correctly, see
§19.5-1 — so the scan sees the old path vanish (→ two-phase mark, human
approval queue) and the new path appear (→ fresh document, fresh
identity). Every component behaves as specified; the composition is the
bug: **routine card movement fills the one queue in the system that
deliberately demands a human, and silently re-keys the card's document
identity (links, embeddings, annotations do not follow) on every column
change.** Five moves in one evening produced 6 pending approvals; the
other 6 pending are the same cause from the night before.

Confirmed live 2026-10-13 (aw-stack Neo4j, `aw-stack-aw-neo4j-1`):

    MATCH (d:Document) WHERE d.sync_status = 'pending_delete'
    RETURN count(d) AS pending,
           count(CASE WHEN d.source_path STARTS WITH 'notion/kanban/'
                 THEN 1 END) AS kanban_pending
    → pending 12, kanban_pending 12          (100% of the queue)

    MATCH (d:Document)
    RETURN count(d) AS all_docs, count(d.source_path) AS with_path,
           count(CASE WHEN d.source_path STARTS WITH 'notion/kanban/'
                 THEN 1 END) AS kanban_docs
    → all_docs 2046, with_path 1973, kanban_docs 1649

(`count(d.source_path)` counts documents **with a path** — the 73-document
gap to `all_docs` is the known pathless ad-hoc uploads, out of scope since
§17, not drift.) On disk the mirror holds 1,619 files across 13 status
directories. The kanban mirror is **84% of all pathed documents**: this
subtree's churn is not marginal noise, it is most of what delete
detection watches.

Two aggravating facts:

- **The churn is not only human-driven.** The board's 7-day auto-archival
  routine (Done/Self-closed → archived dirs) moves cards with no human in
  the loop — the queue refills even on a week nobody touches the board.
- **The layout already has a demonstrated failure class.** Backlog card
  `bug:notion-sync-stale-status-folder-not-removed-for-12-cards`
  documents 12 cards whose *old* status file was left behind — and the
  live tree shows exactly 12 duplicate basenames across status dirs
  today. A layout where one logical document can occupy two paths is
  fragile in precisely the way §1's path-keyed identity cannot absorb.

### 19.2 Who owns the path shape

The mirror is written by `sync_kanban` in
`repos/aw-app-notion/notion_app/sync.py:435` — **not** by the KB app and
not by fs-sync; both are downstream consumers of whatever tree the writer
lays out. The status directory is entirely the writer's choice:
`_render_card` builds `rel_path = f"{KANBAN_SUBDIR}/{status_key}/{slug}.md"`
(sync.py:382). Decisive detail: the writer already *knows* a move is a
move — it tracks every card by `page_id` in its state file and removes the
old file itself when the status changes (sync.py:501, counted as
`moved`). The rename information exists at the source and is destroyed by
the act of encoding status into the path; fs-sync downstream is then asked
to reconstruct it from two unlabeled filesystem events. The fix belongs
where the information is.

### 19.3 The decision: status out of the path (option 2), in `aw-app-notion`

`sync_kanban` mirrors every card to **`notion/kanban/<slug>.md`** — one
flat directory, no status component. Status is not lost: it is already in
the file twice, as frontmatter (`status:` and `status_key:`,
sync.py:403-404) and in the rendered "Propriedades" section, so search
and grep keep working; only the directory layout changes. Slug-collision
disambiguation (`_card_slugs`, sync.py:331) keys on the slug alone
instead of `status/slug` — the 12 current cross-status basename
duplicates (all one stale-file bug) plus any genuine title twins get the
existing `-<id8>` suffix treatment, board-wide.

After this, a column move is a **content update at a stable path**:
fs-sync's happy path. The connector sees the same `source_path` with a
new hash, PUTs by-path, the server bumps `rev` and `updated_at`, the
document re-embeds once — no mark, no approval, no identity re-key. The
approval queue returns to carrying only genuine disappearances.

Stated as an invariant, to live beside §1's identity rule and §18.1's
rev/updated_at split: **path is document identity, so a mirror writer
must never encode mutable state in a path.** Status today, assignee or
priority tomorrow — anything that changes over a document's life belongs
in content or metadata. The notes mirror (`notion/notes/<slug>.md`)
already complies; any future mirror must.

Known, accepted residual: the **title** is still in the path, so a card
retitled in Notion still re-keys once. Titles change rarely and
deliberately where status changes constantly and automatically; buying
full stability with `notion_id`-named files would cost every human and
agent a readable tree. Named, not solved.

### 19.4 Migration — staged under the valve, using only existing machinery

The flatten re-keys all 1,649 kanban documents once. Done in one shot,
1,619 old paths go missing in one scan — guard 2's threshold is
`max(25, 10% of tracked rows)` ≈ ~200, so a bulk flatten **trips the
valve by design and the migration wedges** (the valve marks none and
exits non-zero, every tick, forever). There is no sanctioned mass-rename
override, and this amendment deliberately does not add one. Instead the
**writer throttles**: each `sync_kanban` run flattens at most ~100 cards
(move the file, update tracked state), paced so no single fs-sync scan
sees more missing-advances than ~half the guard-2 threshold — writer-side
pacing, zero connector changes. Each batch then flows through the normal
machinery: ≥2-scan debounce → by-path mark (§17) → card D's UI, where the
operator clears it with one `notion/kanban/` **prefix multi-select
approve** per batch; §18.3's GC then collects the journal rows. Total
one-time operator cost: a handful of prefix-approve clicks over a day or
two. The migration also deletes the 12 stale dual-folder files, absorbing
backlog card `bug:notion-sync-stale-status-folder-not-removed-for-12-cards`.

Window costs, named: until a batch's old paths are approved, both copies
of those cards answer `knowledge_search` (duplicate results, bounded by
batch size and operator promptness), and the one-time re-key loses
whatever was attached to the old nodes — acceptable once, since today it
happens on *every move forever*, and entity extraction is still off so
attachments on kanban mirrors are minimal.

The 12 documents currently pending are untouched by this amendment; they
are the operator's call in card D's UI, and they are genuinely stale
old-status copies that the migration's own batches would otherwise have
produced anyway.

### 19.5 Rejected alternatives

1. **Rename detection in fs-sync (content-hash pairing of a vanished and
   a created path).** Structurally cannot catch the case that motivates
   it: the mirror file's content *changes* on a move — frontmatter
   `status:`, `status_key:`, `checksum:` all differ (sync.py:397-412) —
   so the old and new files never share a hash. Three further strikes,
   each sufficient alone: the pair never coexists in one pass (a new file
   ingests on first sight; the old path needs ≥2 scans to count as
   missing), so pairing needs a cross-tick holding pen for unmatched
   creations; §17 made a path mean *N claimant documents*, so "which
   document follows the rename" is exactly the winner-picking §17.1
   forbids as unsound; and identical-content files (empty stubs,
   templates) pair wrongly. Byte-identical rename detection might someday
   earn its keep for human reorganizations of mapped folders — it would
   still not have fixed this card.
2. **Exempt `notion/kanban/` from approval (auto-approve deletes
   there).** Cheapest and worst, and the measured numbers make it worse
   than the card assumed: this subtree is 100% of current delete traffic
   and 84% of pathed documents, so the exemption doesn't trim an edge —
   it unplugs the human gate from essentially the whole corpus the gate
   watches. The writer has a demonstrated stale-and-missing-file bug
   class (§19.1); auto-approve converts the next writer bug directly
   into silent hard deletes with no tombstone and no feed signal (§18.3:
   approvals are structurally invisible). It also sets the precedent of
   per-subtree trust carve-outs, which is how §7 dies by a thousand
   exemptions.
3. **Document-and-accept.** The rate scales with exactly the things we
   want more of (Dev Team flow activity) plus one thing no human even
   does (auto-archival), the re-keying silently severs card-document
   continuity on every move, and a queue that trains the operator to
   bulk-approve mirror churn erodes the gate for the subtrees that
   still need it. Writing it down doesn't change any of that.
4. **Teach fs-sync the mirror's semantics** (parse `notion_id`
   frontmatter to track identity, or a writer→connector rename hint
   channel). Couples the generic sync engine to one producer's file
   format / adds a second mutation channel beside the filesystem,
   which §17.6-1 already rejected in another form: the tree is the
   interface. The writer owns its layout; fixing the layout needs no
   new interface at all.

### 19.6 What this makes harder later

1. **Browsable-by-column dies.** The on-disk tree and the §16 git-backend
   repo stop showing status at a glance; "what's in backlog" becomes a
   frontmatter grep or a board question, not an `ls`. Anything that
   globs `notion/kanban/<status>/*.md` breaks — known live consumers:
   old Kanban cards' CheckHints (at least
   `bug:notion-sync-stale-status-folder-not-removed-for-12-cards`, whose
   card this migration absorbs anyway), and the writer's own
   empty-dir semantics (sync.py:259-268), which are deleted with the
   layout. §19.7 gates include a consumer sweep.
2. **One flat directory of ~1,650 files.** GitHub's web UI truncates
   directory listings at 1,000 entries, so the §16 repo's kanban dir is
   only fully listable via clone/raw; directory scans get marginally
   slower. Sharding (e.g. by initial) was considered and skipped —
   it reintroduces a path component that must never change, for a
   cosmetic win.
3. **A one-time identity re-key of every kanban document**, with a
   migration window of duplicate search results per batch (§19.4). After
   it, kanban document identity is stable for the first time — but any
   external reference captured against an old path (none known today)
   dangles once.
4. **Title edits still churn** (§19.3's residual): the path still encodes
   one mutable thing. If title-edit churn ever becomes measurable, the
   next step is id-suffixed-always filenames, not another layout
   migration — note it here so that option isn't re-derived.
5. **The invariant binds future mirrors.** "No mutable state in paths"
   constrains every future tree-producing writer (and is the reason this
   amendment exists); a writer that can't comply (e.g. one mirroring a
   system whose native keys are mutable) needs its own design pass
   before it may feed a synced tree.

### 19.7 Implementation card and acceptance gates

**Card I1 — writer: flatten the Kanban mirror + staged migration
(`repos/aw-app-notion`).** No fs-sync, server, or connector change; no
other card. Gates:

- A new card lands at `notion/kanban/<slug>.md`; a column move rewrites
  the **same** path (state-file diff shows `updated`, not
  `moved`+`removed`) and the archived statuses land flat too.
- Slug collisions disambiguate board-wide with the existing `-<id8>`
  suffix; the suffix applies to *all* colliders, preserving
  sync.py:331's order-independence property.
- Migration: flag/state-gated, ≤100 cards per run, idempotent across
  crashes (a half-moved batch re-runs clean); the old file is removed
  and the new one written in the same run; stale dual-folder leftovers
  are removed; after convergence no `notion/kanban/*/` subdirectory
  remains on disk.
- Consumer sweep: grep this workspace's skills, scheduled tasks,
  CheckHints and docs for `notion/kanban/<status>` path patterns;
  update or retire each hit in the same card.
- Live, coordinator-driven, against production fs-sync: across the whole
  migration the valve never trips (`blocked: false` on every tick);
  pending approvals arrive only in operator-sized batches and the
  operator clears each with a prefix approve; after convergence and
  approvals, the graph shows zero live status-dir documents —
  `MATCH (d:Document) WHERE d.source_path =~ 'notion/kanban/[^/]+/.+'
  RETURN count(d)` → 0 — and a deliberate test-card column move
  produces `deleted_marked: 0` on the following two ticks.

Out of scope, unchanged: the 12 currently-pending approvals (operator's
decision), the 73 pathless documents, rename detection for mapped
folders, card F (soak), everything in §17/§18 as built.

---

## 20. Amendment (2026-10-15): per-prefix quiet period before upsync — hold a changed file until it has been still for N (`notion/` = 3h)

Written against card `3f85bf3b-9510-814e-917d-e93357322ddd`. Frederico's
request, verbatim intent: *stop syncing a card while it is still being
modified* — hold every detected change until the file has been unmodified
for 3 hours, as a **generic** setting in the connector config, applied to
`notion/`. Lands entirely in `repos/aw-app-knowledgeable` (the
connector); server, valve semantics, feed, and `aw-app-notion` are
untouched.

The earlier cost justification is retired: the mapping agents moved to
`openai-gpt-4o-mini` (162 labeller runs ≈ $0.04 vs ≈ $5.56 before, ~145×
cheaper), so "repeated LLM rebuilds are expensive" no longer carries the
card. What stands is the correctness/behaviour argument: the topic tree
rebuilt 4× in the last 24h, three of them inside 90 minutes (05:21,
06:22, 06:52), each a full ~7-minute rebuild producing an essentially
identical tree — syncing a card mid-edit embeds transient states and
re-derives the same graph over and over. Every agent comment on a card
bumps Notion's `last_edited_time` and re-renders that card's mirror file
(comments are rendered into the body — the churn source confirmed on the
card), so a card under active work churns continuously.

### 20.1 The mechanism already exists — this generalizes it

The upload pass already holds a changed file until it has been still:
`DEBOUNCE_SECONDS = 30` (`knowledgeable_app/fs_sync.py:127`), enforced
hash-anchored in the found-loop (fs_sync.py:981-992) — a new hash restarts
the window, upload happens only once the same hash has been observed
stable past the window. **This amendment is that mechanism with a
per-prefix window and a starvation cap, not a new subsystem.** Creates are
held exactly like edits, as today (fs_sync.py:931-934 — a brand-new card
that is immediately commented on is the same churn).

### 20.2 The decision

**Per-prefix quiet rules, hash-anchored stillness, journal-persisted
clocks in fs-sync-owned columns, with a max-hold cap.**

- **Stillness signal: content hash as observed by the scan, not mtime**
  (card decision 2). Three reasons, each sufficient: the scan already
  hashes every file every tick (`_walk_sync_tree`, fs_sync.py:177) so the
  signal is free; a re-clone or restore of the tree rewrites every mtime
  wholesale — under mtime a recovery re-clone of the §16 mirror would
  freeze ALL syncing for the full window (git's own `checkout -B` on the
  in-place repo, git_backend.py:165, touches nothing, but the recovery
  path is exactly when you least want a silent 3h global hold); and the
  existing debounce is already hash-anchored — same idiom, no second
  timing concept.

- **The clock must live in columns only fs_sync writes.** This is the
  non-obvious load-bearing constraint: `bulk_ingest.scan()` runs inside
  every bulk-ingest tick (bulk_ingest.py:544) — **every 15 minutes in
  production** (the "KB bulk-ingest — tick" scheduled task, enabled, last
  run ok 2026-10-15) — and its upsert stomps `sha256` and
  `scanned_at = now` on every row not status `uploaded`/`skipped`
  (bulk_ingest.py:400-408). A never-yet-uploaded row's quiet clock
  anchored on `scanned_at` would be reset every 15 minutes and **never
  elapse**. Three new journal columns, idempotent ALTERs in
  `bulk_ingest._ensure_table` beside the §3.2 set (house style, no
  alembic): `pending_hash TEXT` (the candidate content awaiting
  stillness), `pending_since DOUBLE PRECISION` (when that hash was first
  observed — restarts on every hash change), `dirty_since DOUBLE
  PRECISION` (when the row first diverged from `last_synced_hash` — does
  NOT restart on further changes; the starvation cap's anchor).

- **Found-loop contract** (replaces the `sha256`/`scanned_at` debounce
  branch at fs_sync.py:981-992; `sha256`/`scanned_at` keep their values
  for reporting but are never again trusted for timing):
  1. `digest == last_synced_hash` → clear `pending_hash`/`pending_since`/
     `dirty_since` if set (an edit that reverted cancels its own hold);
     unchanged.
  2. `digest != pending_hash` → `pending_hash = digest`,
     `pending_since = now`, `dirty_since = COALESCE(dirty_since, now)`;
     no upload — the window restarts.
  3. stable pending hash → upload iff
     `now - pending_since >= quiet(relpath)` **or**
     `cap(relpath) > 0 and now - dirty_since >= cap(relpath)`.
     On confirmed upload, clear all three columns.
  A successful downsync write clears the hold naturally (it sets
  `last_synced_hash` to the new content → rule 1 next scan).

- **Starvation cap: required** (card decision 1, the coordinator's view
  confirmed). A pure quiet period never fires on a file that keeps
  changing, and that is the common case here: several fs-sync cards went
  12h+ without a 3h-still window because agents kept commenting. Without
  a cap, a card is invisible to `search_knowledge_base` for exactly as
  long as it is interesting. Default `cap = 4 × quiet` (12h for
  `notion/`): a hot card syncs a consistent snapshot at most twice a day,
  and the final state lands 3h after the activity ends. `cap = 0`
  disables it per rule, for a future subtree where "never while dirty" is
  genuinely wanted. A cap-fire uploads whatever bytes the scan read —
  atomic per file, possibly mid-burst per card; the next still-window
  supersedes it, which is the normal churn path, not a new hazard.

- **Post-deploy transient, named:** existing dirty rows have NULL
  `pending_hash`, so the first tick after deploy starts their window
  fresh — already-pending `notion/` edits wait one full quiet period
  from the restart, once.

### 20.3 The delete side is NOT gated (card decision 3)

The quiet period is an **upload gate only**. Delete marking keeps its
existing independent damping unchanged: ≥2-consecutive-scans (§3.3), the
§7 valve, human approval (card D). Reasons: an absent file is not "being
modified" — the request's rule has no referent on the delete path, which
already has three dampers and a human gate; and gating deletes on a quiet
period is what would convert the §19.4 migration's 17 paced batches into
one super-burst (§20.4). The race the card names — a delete "racing
ahead" of a held content update — is moot by construction: when a path
vanishes mid-hold there is nothing left to upload; the mark proposes
deleting the last-*synced* content, subject to approval as always, and if
the file reappears `missing_scans` resets (fs_sync.py:961-968) and the
hold resumes. Rows mid-hold that were never synced (`external_id` NULL,
not adopted) stay outside delete tracking and the valve's numerator
exactly as today (`_TRACKED_ROWS_WHERE`, fs_sync.py:192) — no valve
change of any kind.

### 20.4 Interaction with the §19 I1 flatten migration (card decision 5)

Because deletes are not quiet-gated, **the migration keeps exactly
§19.4's cadence**: writer-paced ≤100 old-path removals per batch, each
batch marking ≈ half of guard 2's threshold (`max(25, 10% of ~2,008
journal rows) ≈ 200` today). The feared single ~1,700-removal burst
cannot occur under this design; the valve is never approached. No
migration bypass exists or is added.

The residual interaction is on the **create** side: each batch's ~100 new
flat paths are new files and wait out the 3h window before first upload.
If the operator prefix-approves a batch's old paths before its new paths
ingest, that batch's cards are absent from search for up to ~3h — a gap
window replacing §19.4's duplicate window.

**Required landing order: I1 completes first — through its §19.7
convergence gate (zero `notion/kanban/*/` directories on disk, zero
status-dir documents in the graph) — then this card deploys.** The order
is free: I1 is `ready_to_deploy` with no dependency on this card, while
this card still needs implementation, release, and the Tier-1 core
restart. Stated fallback so nobody panics mid-flight: if the order
inverts anyway, nothing trips and nothing wedges — the migration merely
acquires the 3h ingest lag and the gap window above. Degraded UX, not a
correctness failure. Post-I1 the two changes compose: I1 makes a column
move a content update at a stable path; this card coalesces those
updates.

### 20.5 Config shape (card decision 4)

One new `config_schema` key in `aw-app.json` — existing keys, notably
`service_secret` and `git_token`, are byte-for-byte untouched:

    "sync_quiet_rules": {
      "type": "string",
      "default": "notion/=10800",
      "title": "Sync quiet periods",
      "description": "Comma- or newline-separated rules
        `prefix=quiet_seconds[:max_hold_seconds]`. A changed file under
        `prefix` uploads only once its content has been unchanged for
        quiet_seconds; max_hold (default 4× quiet, 0 = unlimited) syncs
        it anyway if it has been continuously changing that long.
        Longest matching prefix wins. Paths matching no rule keep the
        built-in 30s debounce."

Threading: the `/fs-sync/run` route (routes.py:234-247) reads
`ctx.config.get("sync_quiet_rules")`, parses, and passes a `quiet_rules=`
kwarg into `fs_sync.run_tick` — the CLI door POSTs that route
(commands/knowledgeable_sync.py:29), so the route stays the single config
seam. With the kwarg absent, behaviour is byte-identical to today (30s
everywhere), so every existing test keeps its meaning. The parser is a
pure function with its own unit tests including malformed input; an
invalid rule is skipped with a logged warning AND surfaced as
`quiet_rules_invalid` in `status()` — a typo must not silently disable
the feature (the optional-config-gated-paths lesson: the default value
must exercise the real code path in tests, not a bypass).

A second contributed scheduled task (`knowledgeable-sync run`, cron
`*/15 * * * *`, `enabled: false` like the bulk-ingest task, operator
enables) ships in the same manifest change: a held file only uploads on a
LATER tick, so without a periodic fs-sync tick a quiet period never
releases. Today no schedule drives fs-sync (verified against the live
tasks list 2026-10-15) — ticks are coordinator-driven — which the I1
migration gates already implicitly require fixing; this makes it
explicit.

Rejected shapes: dedicated per-prefix keys (`notion_quiet_seconds` — not
generic, next prefix needs a schema change, which is what Frederico
explicitly asked to avoid); a JSON-object field (every existing key is a
flat scalar the declarative settings UI renders as a plain input —
hand-typed JSON in a text box is the error-prone version of the same
string); separate quiet-map and cap-map fields (two lists to keep
aligned by hand).

### 20.6 Supersession (card decision 6): yes — closes the topic-rebuild debounce card

This supersedes card `3f85bf3b-9510-81f2-ace9-f029266fd424` (raise
`TOPIC_BUILD_DEBOUNCE_SECONDS` in `repos/aw-knowledgeable`); the
coordinator should close it rather than ship both. Judged on correctness
alone (its cost case evaporated with the 4o-mini move too): the quiet
period removes the rebuild trigger at the source — mirror churn never
reaches the graph, so there is no burst for the debounce to damp — while
the debounce knob damps the symptom downstream, in a repo with live work
in it, and would still let every intermediate state ingest and re-embed.
Falsifiable reopen condition: if a week after this deploys the tree still
rebuilds >2×/day producing essentially identical trees, reopen that card
with the measurement — the quiet period will have been shown insufficient
at the source.

### 20.7 Observability

`status()` gains `holding` (rows with `pending_hash` set, not yet
eligible), `quiet_rules` (the parsed, effective rules) and
`quiet_rules_invalid`; `report()` gains per-subtree `holding`. Without
these, "held for 3h" and "silently broken" are indistinguishable from the
operator's seat — this workspace's named failure mode.

### 20.8 What this makes harder later

1. **KB freshness for `notion/` becomes ≥3h stale by design** (≤12h for
   continuously-active cards). Agents reading board state through
   `search_knowledge_base` see old data; the `aw-kanban` MCP tools (live
   Notion) are the fresh path. This is the requested trade — named, not
   hidden.
2. **The two-engines-one-table asymmetry deepens**: bulk-ingest may stomp
   `sha256`/`scanned_at` at will; fs-sync must never again derive timing
   from them. Anyone merging the engines later unwinds three more
   columns.
3. **A mini-DSL in config** — a new silent-degradation surface, mitigated
   by §20.7, not eliminated.
4. **E2E flows that drop a file and expect prompt ingestion** must use a
   non-`notion/` path or override the rules; a future "sync now" action
   needs an explicit hold-bypass flag on the route (reserved, not built).
5. The built-in 30s default for unmatched paths stays hardcoded; if it
   ever needs configuring, extend the same rules string with a `*` or
   empty prefix — noted so it isn't re-derived as a new field.

### 20.9 Implementation card and gates (one card, the connector)

Everything above is one coder card in `repos/aw-app-knowledgeable`:
columns in `bulk_ingest._ensure_table`, found-loop + rule resolution in
`fs_sync.py`, config threading in `routes.py`, `sync_quiet_rules` + the
scheduled task + version bump in `aw-app.json`, `status()`/`report()`
fields, tests. Gates:

- **The stomp test, failing pre-change** (stash/run/restore proof): a
  `notion/` row mid-hold, `bulk_ingest.scan()` runs (overwriting
  `sha256`/`scanned_at`), the quiet window must still elapse on
  schedule. Pre-change this fails because timing lives in `scanned_at`.
- Window restart on content change; upload at quiet elapse; cap-fire at
  `dirty_since + cap` despite continuous changes; revert-to-synced
  clears the hold; downsync clears the hold.
- Longest-prefix: `notion/` vs `notion/kanban/` rules, both orders.
- Unmatched prefix keeps 30s; absent kwarg keeps today's behaviour
  (existing suite green unmodified, specifically the valve tests —
  `test_valve_*`, tests/test_fs_sync.py:191-290,739-809 — untouched).
- Parser: malformed rule → skipped + `quiet_rules_invalid` surfaced,
  valid rules still applied.
- Live after deploy (Tier-1: needs the core restart; verify the
  INSTALLED copy, not CI): `status()` shows the default rule active and
  `holding > 0` while the board churns; a deliberately-commented test
  card does NOT re-ingest within the window and DOES ingest after 3h of
  stillness; `deleted_marked` behaviour on a deleted file is unchanged
  (2 scans, no 3h wait).
- Deploy blocked on I1's convergence gate (§20.4's landing order).

---

## 21. Amendment (2026-10-15): per-prefix `delete_policy` — unattended approval for routine deletions

Written against card `3fa5bf3b-9510-81f5-9992-d977b17726ad`. Frederico,
verbatim: *"I deleted the random* folder, so I want everything working e2e
without manual intervention."* Verified live that day: `random/` and
`random2/` deleted ~21:08Z, journal reached `missing_scans=5,
pending_delete=1` on its own, and both documents kept answering searches
until a hand approval at 22:06Z. §7's two-phase delete has no unattended
path: creates and edits converge alone, deletes stop dead. This amendment
adds the unattended path for the routine case while keeping every §7
safeguard for the anomaly.

### 21.1 The decision

**A `delete_policy` config string in §20.5's DSL family, resolved per
path; an in-tick auto-approve pass that reuses the card-D approve
machinery verbatim; and the §7 valve latch promoted from reporting-only
to a hard gate on that pass.**

Rules: comma- or newline-separated `prefix=auto[:min_missing_scans]` or
`prefix=manual`, longest prefix wins, paths matching no rule are
`manual`. Default `""` — ships inert, today's behaviour byte-for-byte,
exactly how §20 itself shipped safely mid-migration. Two deliberate
parser restrictions beyond §20's:

- **An empty prefix is invalid** (skipped + surfaced in
  `delete_policy_invalid`). §20.8-5 reserves `*`/empty for a global
  default; for deletions a global `auto` is precisely the blanket
  approve the 2026-10-15 queue proved catastrophic (4
  `apps/knowledgeable/cli_reference/` marks were live documentation
  whose files a daily task restores — a blanket approve deletes them).
  Scope stays explicit, always.
- **`authored/` cannot carry an `auto` rule** (same invalid-surface
  path). §4's downsync is still unexercised; until it is, "the file
  vanished" is weak evidence about a document a human authored in the
  graph. Lift this restriction consciously, in an amendment, when §4 is
  exercised — not by discovering the parser allows it.

`min_missing_scans` defaults to **2** — the same tick the mark lands
(see 21.3). The knob exists per rule (`notion/=auto:4`) for operators
who want to buy extra confidence with latency.

### 21.2 Where it lands

All in `repos/aw-app-knowledgeable` (the connector) plus this doc; the
server, the feed, the valve thresholds, and `tests/test_fs_sync.py`'s
frozen valve ranges (:191-290, :739-809) are untouched.

- **`knowledgeable_app/fs_sync.py`** — `parse_delete_policy` /
  `_resolve_delete_policy`, mirroring `parse_quiet_rules`
  (fs_sync.py:150) and `_resolve_quiet_rule` (:197): same splitting,
  same longest-prefix sort, same skip-and-surface on malformed entries.
  A new pass `_auto_approve_pending`, called immediately after
  `_mark_missing_as_deleted` (:1171) so a mark and its approval land in
  one tick. The pass:
  1. **No-ops entirely** if guard 2's pre-pass check tripped this tick
     (`_check_mass_delete_valve`, :301 — the tick already aborted), if
     the mid-pass `_MarkBudget` tripped (:1196-1209), **or if the
     sticky valve latch is set** (threaded in, below). Skips are
     counted, never silent: `auto_approve_suspended: <reason>` in the
     tick result.
  2. Selects journal rows `pending_delete = 1 AND missing_scans >=
     rule.min AND policy(relpath) == auto`. `missing_scans` keeps
     advancing on pending rows (the harmless `else` branch,
     :1273-1278), so thresholds above 2 work; a file that reappears
     resets the counter (:1067) and the approval never fires.
  3. Fetches the server's pending list (fresh, after this tick's
     marks) and, per matched rule, calls
     `pending_deletions.approve_pending_deletions` with
     `node_ids = [every pending doc whose source_path equals an
     eligible relpath]` and `expected_count = len(node_ids)`, actor
     `"fs-sync:delete_policy rule=<prefix>"`. One approve path, one
     audit shape, one journal-reconcile (`_reconcile_journal_for_
     succeeded`), for human, agent and policy alike. Matching on
     `source_path` means co-claimant duplicates at an approved path go
     together — §17's by-path semantics, unchanged.
- **`knowledgeable_app/routes.py`** — `/fs-sync/run` (:252-270) threads
  two more kwargs: `delete_policy=ctx.config.get("delete_policy")` and
  `valve_latched=bool(await ctx.state.kv.get(FS_SYNC_VALVE_KV_NAME))`.
  `run_tick`'s `valve_latched` parameter defaults to `None` = unknown =
  **auto-approve disabled** — a caller that doesn't prove the latch is
  clear gets marking-only behaviour (fail closed; existing tests keep
  their meaning for free). New route `POST /fs-sync/valve/clear`
  (identity-gated like its siblings): deletes the KV entry and writes a
  `valve_clear` row to the card-D audit table via `_caller_actor`
  (:72). The latch's TTL changes from 30 days to **none**: a latch that
  silently expires would silently re-arm unattended deletion, which is
  this workspace's named failure mode. The auto pass itself NEVER
  clears the latch — constraint 2 holds: a trip always waits for an
  explicit, audited, out-of-tick decision.
- **`aw-app.json`** — `delete_policy` config_schema key beside
  `sync_quiet_rules` (:153), default `""`; the fs-sync escalation
  prompt (:97) gains one line: a latched valve also suspends
  auto-approval until `/fs-sync/valve/clear`. Version bump belongs to
  the release bot, not the diff.
- **`knowledgeable_app/pending_deletions.py`** — no behaviour change;
  at most a thin internal entry point if the dict-args surface is
  awkward to call from the tick. The audit premise on the card is
  stale: this module takes `db` explicitly and `run_tick` already
  holds `ctx.db` (routes.py:263), so the auto path reuses the same
  audit table with zero new plumbing.
- **`status()`/`report()`** (fs_sync.py:1282/:1318) — `delete_policy`
  (parsed effective rules), `delete_policy_invalid`,
  `auto_approve_suspended`; tick result gains `auto_approved`,
  `auto_approve_failed`. §20.7's rule: without these, "working" and
  "silently off" are indistinguishable.
- **Tests in a NEW file** (`tests/test_delete_policy.py`), not appended
  to `test_fs_sync.py` — the valve contract is pinned to line ranges;
  inserting tests above them renumbers the contract.

### 21.3 The card's open questions, answered

**Prefix, not provenance.** Measured 2026-10-15: all 2,079 tracked
journal rows carry `origin` NULL/empty (1,107) or `scan` (975 incl.
pending) — zero `feed`, `adopted`, `downsync`. The column records how
the JOURNAL ROW came to exist, not who writes the FILES; a Kanban
mirror file and a human's hand-written note both arrive as `scan`. The
risk classes Frederico distinguishes — machine mirror vs generated docs
vs human-authored — are encoded only in the tree layout (`notion/`,
`apps/`, `authored/`, `mapped_folders/`), so in this tree **prefix IS
the provenance axis**, and it is the axis every adjacent mechanism
already uses (§20 quiet rules, card D's `path_prefix` scoping, the
UI's prefix breakdown).

**Latency buys almost nothing here; default `min_missing_scans=2`.**
The protective window a higher threshold adds runs from the mark to the
approval — and the one measured false-positive class (cli_reference,
restored by a *daily* task) has a restore horizon of hours-to-a-day:
~96 fifteen-minute scans to cover it, at which point the path isn't
"auto" in any meaningful sense. That class is why `apps/` stays
`manual`, not a reason to slow every auto delete. Meanwhile the
acceptance contract is "delete under an auto prefix, two ticks,
gone" — which is exactly mark-at-2 + same-tick approval. The real
protections are structural: the 2-scan rule, guard 1's zero-files
abort, guard 2 + the mark budget, the latch gate, explicit scope, and
reappearance resetting `missing_scans`. The per-rule knob preserves
the option without imposing the cost.

**The valve denominator keeps auto prefixes in.** Re-rejection of
§18.5-5: excluding them shrinks the threshold and makes the valve
MORE trigger-happy for everyone else. Auto-approval already
self-adjusts it honestly — each approval's journal-reconcile deletes
rows, so the denominator tracks the corpus as it genuinely shrinks.
Measured with the code's own query (`_TRACKED_ROWS_WHERE`): 2,079 →
threshold 207. (The narrower pending/suppressed-excluded query reads
1,975 — both were measured, per the twice-burned measure-never-infer
rule; the code uses the wider one.)

**`authored/`** — parser-rejected for `auto` until §4 is exercised
(21.1). A local file deletion proposing removal of a graph-authored
document is a conflict question, not a convergence question.

### 21.4 Interactions in flight

- **I1 flatten migration (paused, ~750 cards left).** This ships inert,
  so there is no landing-order constraint. When I1 resumes AND the
  operator enables `notion/=auto` (or `notion/kanban/=auto`), each
  batch's ≤100 legacy-path marks auto-approve in the same tick —
  replacing precisely the per-batch manual prefix-approvals the
  coordinator performed by hand on 2026-10-15, at 100 marks against a
  measured threshold of 207, with the `_MarkBudget` ceiling still
  pacing mid-tick. The policy accelerates the §19.7 convergence gate;
  it cannot fight the migration. Recommended ramp: enable after
  watching one resumed batch tick.
- **The 544 orphaned duplicates** (card
  `3fa5bf3b-9510-812c-a439-d7fcc773f2bd`, debugger in flight). A
  policy keyed on journal rows cannot reap a document no journal row
  references; this design does not claim to. One helpful edge it does
  cover: because approval matches on `source_path`, an orphan twin
  co-claiming a path whose tracked row is auto-approved is deleted
  with it (by-path semantics). Orphans at live or never-journaled
  paths remain that card's problem — coordinate, don't duplicate.

### 21.5 Rejected alternatives

1. **Provenance (`origin`) as the policy axis.** Measured zero
   discriminating power (21.3), and file-writer provenance is recorded
   nowhere else — adopting this axis would first require building the
   very metadata it presumes.
2. **A scheduled agent as the approver** (cron → agent run → MCP
   approve tools). Replaces human-in-the-loop with LLM-in-the-loop:
   non-deterministic, token-cost per routine delete, and the
   run-reliability record (watchdog kills, duplicate QA fan-outs,
   wakeup races) makes it the least dependable component in the
   chain the policy is supposed to make dependable. The deterministic
   20-line pass is the honest version.
3. **`force` hard-delete for auto prefixes** (decision 5's existing
   path, skipping the pending state). Loses the pending window and the
   reappearance cancel, bypasses the queue UI mid-flight, and forks a
   second delete path whose valve interactions must be maintained
   separately. The two-phase shape stays; phase 2 gains a machine
   approver — strictly smaller change.
4. **Auto-DECLINE for known-churn prefixes** (e.g. cli_reference).
   Inherits §18's permanence trap in automated form: `restore_
   suppressed=1` rows silently exempt paths from deletion forever —
   18 such rows made §19.7's gate unreachable while every batch
   "succeeded". The policy enum is `auto|manual` only; nothing
   auto-declines, ever.
5. **Age-based blanket approve** ("approve anything pending > 7
   days"). Scope by time instead of place violates constraint 3 in
   the exact way the cli_reference marks demonstrate: they would have
   quietly aged into deletion overnight.
6. **An out-of-tick approver** (second scheduled task calling the MCP
   approve tools with computed counts). Adds a scheduler racing the
   tick (TOCTOU between its list and its approve), duplicates the
   eligibility logic outside the journal's custody, and gains nothing
   the in-tick pass — already serialized by the §5 lease — doesn't
   have.

### 21.6 What this makes harder later

1. **Unattended destructive writes now exist at all.** A *valid* but
   wrong rule (`mapped_folders/=auto`) is operator rope the parser
   cannot take away. Mitigations are audit + status surfacing + the
   latch, not prevention.
2. **A second config mini-DSL** beside `sync_quiet_rules`, with
   different value semantics (enum vs numbers). Share the
   split/longest-prefix helper so they cannot drift apart
   structurally; the silent-degradation surface §20.8-3 named now has
   two tenants.
3. **The valve latch becomes load-bearing.** Today it is cosmetic;
   after this, its presence suppresses auto-approval and its absence
   permits it. The TTL removal is part of the design, not an
   optimization — and anyone touching the KV layer must know a lost
   latch re-arms unattended deletion.
4. **Every future `run_tick` caller must thread latch state** or
   silently get marking-only behaviour. Fail-closed is the right
   default, but it means "auto-approve mysteriously off" is the
   failure mode to document (hence `auto_approve_suspended` in
   status).
5. **The `authored/` parser rejection must be consciously lifted**
   when §4 is exercised — a future coder who finds it will be tempted
   to just delete the check.
6. **§17.6-3's single-workspace assumption deepens**: auto-approval
   trusts that absence from this tree means absence everywhere. A
   second workspace syncing the bucket revisits this amendment
   alongside §17.6-3 and §18.3.

### 21.7 Risks for the coders

- **Ordering inside the tick is load-bearing**: the auto pass must run
  after `_mark_missing_as_deleted` (:1171) and must fetch the server
  pending list AFTER those marks, or same-tick approval (the default)
  never sees them.
- **Check both trip signals**: guard 2's pre-pass abort exits the tick
  before the pass; the mid-pass `_MarkBudget` trip (:1196) does not —
  the pass must test `mark_budget.tripped` itself.
- **A TOCTOU count mismatch is a feature**: if the queue moved between
  selection and approve, `expected_count` rejects, the rejection is
  audited, and the next tick retries. Do not retry in-pass.
- **Adopted rows** (`external_id` NULL) won't match
  `_reconcile_journal_for_succeeded`'s `(relpath, external_id)` key;
  their journal rows are collected by the existing §18.3 GC a tick
  later. Expected, not a leak.
- **Deploy is Tier-1**: config + code land only after a core restart;
  verify the INSTALLED copy. Enable via `POST
  /api/apps/knowledgeable/config` — never via `/settings`, which
  clobbers `service_secret`.
- **Verification ticks**: drive `POST .../fs-sync/run` directly with a
  long timeout; the CLI's 30s HTTP timeout tracebacks while the tick
  keeps running.

### 21.8 Acceptance (the card's falsifiable contract)

With `delete_policy = "random/=auto"` set and the latch clear:

1. Sync a file under `random/`, delete the directory, run two ticks →
   tick 2 marks AND approves; the document is gone from the graph and
   from search; the audit table shows `action=approve,
   actor=fs-sync:delete_policy rule=random/` with matching counts; the
   journal row is gone. No agent, no human.
2. Same deletion under a `manual` (or unmatched) prefix → marked,
   stays pending, queue visible in UI and MCP list. Unchanged from
   today.
3. A deletion large enough to trip guard 2 → tick aborts, latch set,
   nothing marked or approved; a subsequent small delete under the
   auto prefix is marked but NOT approved while latched
   (`auto_approve_suspended` says why); after `POST
   /fs-sync/valve/clear` (audited), the next tick approves it.
4. With `random/=auto:5`: file deleted, restored at `missing_scans=3`
   → counter resets, nothing approved, document intact.
5. `delete_policy=""` → tick result and status byte-identical to
   today; the full existing suite green unmodified, the frozen valve
   ranges untouched.
