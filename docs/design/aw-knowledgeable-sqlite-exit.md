# aw-knowledgeable SQLite exit — two stores, two Postgres, zero fallback

Status: **design, not code.** Written by the Architect agent, 2026-10-10,
against card `3f55bf3b-9510-8124-ae31-d67ea0cc96dd` (target
`knowledgeable-fs-sync`). Frederico's order, verbatim: *"there should not
exist a SQLite, if it does, we need to move it to postgres, so create a card
for that, no sqlite for ap-mt, migrate the sqlite and delete it, don't leave
behind so we don't have mistakes in the future."*

Every path below was read this session; cite lines when you disagree.

---

## 1. The approach

The two live SQLite stores go to **two different Postgres instances**,
because they are two different kinds of state owned by two different
runtimes. The identity projection (`sub` → tenant) is **service state** of
aw-knowledgeable and goes to the **aw-stack central Postgres**
(`aw-stack-aw-postgres-1`), new database `knowledgeable`, via SQLAlchemy —
the decision Frederico already made verbatim on the 2026-09-27 card
("ele pode usar o postgres utilizado pela aw-stack que é um postgres
central"). The connector's bulk-ingest journal is **per-workspace app
state** of a Tier-1 in-process workspace app and goes to the **workspace's
own Postgres** through the existing `db:own-tables` facade
(`src/apps/db_tables.py:54`), table `app__knowledgeable__bulk_ingest_files`.
Both migrations are one-shot count-verified scripts; after cutover the
SQLite code, files, the `identity-data` volume, and the SQLite half of the
nightly backup are deleted. No flag, no "if Postgres is down use SQLite" —
a Postgres outage is a loud 5xx, exactly like it already is for Neo4j.

## 2. The decision that must not be improvised: which Postgres for which store

### Store 1 — identity projection → aw-stack central Postgres, db `knowledgeable`

- `repos/aw-knowledgeable/backend/app/config.py:67`
  (`identity_tenant_db_path = "/data/identity/tenants.db"`) and
  `backend/app/core/identity.py:248` (`sqlite3.connect` inside
  `_get_or_mint_tenant`).
- **Why this instance:** (a) it is a prior closed decision — the 2026-09-27
  Kanban card records Frederico's verbatim instruction to put identity on
  the aw-stack central Postgres; (b) the app already sits on `aw-stack-net`
  (`repos/aw-knowledgeable/docker-compose.yml:54-56`) so
  `aw-stack-aw-postgres-1:5432` resolves with zero network work — the same
  hop AP-MT's `AGENTS_DATABASE_URL` already makes
  (`repos/agents-platform-multitenant/.env.example:24`); (c) **backup is
  free**: `aw-stack/scripts/backup-postgres.sh:129` runs `pg_dumpall`, so a
  new database is covered by the existing nightly the moment it exists —
  which is what lets the bespoke SQLite backup step retire with no coverage
  gap.
- **Own database `knowledgeable`**, not a table inside
  `agents_platform_multitenant` or `hoppscotch`: db-per-service is the
  pattern on that instance, and identity rows must survive any other
  service's reset.
- **House ORM:** SQLAlchemy 2.x + `psycopg[binary]` (new deps in
  `repos/aw-knowledgeable/pyproject.toml:14`), `create_all` + hand-rolled
  idempotent ALTERs if ever needed — there is no alembic anywhere in this
  estate, don't introduce one.

### Store 2 — bulk-ingest journal → workspace Postgres via `ctx.db` (DbTables)

- `repos/aw-app-knowledgeable/knowledgeable_app/bulk_ingest.py:128-155`
  (`journal_path()` → `bulk_ingest.sqlite`, `_connect()` with
  `sqlite3.connect`).
- **Why NOT the aw-stack Postgres:** the connector is a Tier-1 *in-process*
  workspace app (`repos/aw-app-knowledgeable/aw-app.json` →
  `"tier": "inprocess"`, pip deps `httpx` only). The workspace container is
  a sibling of the aw-stack network context; reaching
  `aw-stack-aw-postgres-1` from inside the workspace server is not a given
  and would couple per-workspace scratch to another stack's credential.
- **Why the DbTables facade:** it is the purpose-built seam for exactly
  this — `db:own-tables` is a **low-risk** capability
  (`src/apps/capabilities.py:30`), the facade enforces the
  `app__<slug>__` prefix and workspace-schema isolation
  (`src/apps/db_tables.py:45-58`), it is cross-worker safe (real Postgres,
  one engine, `WORKERS=10` included), and there is live precedent:
  `apps/research-search` declares it (`aw-app.json:30`) and threads
  `ctx.db` through its store (`research_search_app/store.py:257` even
  documents the duck-type seam for tests).
- This **kills the `database is locked` class for real**: the in-process
  `asyncio.Lock` at `bulk_ingest.py:101` protects nothing across 10
  workers; Postgres per-statement transactions remove the torn-lock crash,
  and tick mutual exclusion moves to **`ctx.state.lease`** (flock-backed,
  cross-worker, shipped 2026-10-09 in commit `f63e43c` exactly as "the
  cross-worker state primitive") — add `state:own` (also low-risk) to the
  manifest alongside `db:own-tables`.
- `ctx.state.kv` was considered and **rejected** for the journal itself:
  Redis-backed, TTL'd, degrade-open — scratch semantics; the journal is a
  9k-row durable ledger whose loss costs a full re-upload pass.

## 3. Where it lands

**`repos/aw-knowledgeable`** (⚠️ a coder is live in this repo on the
fs-sync card editing `core/graph.py`, `api/documents.py`,
`tests/test_tenant_isolation.py`, frontend — touch NONE of those):

- **NEW** `backend/app/core/tenant_store.py` — lazy singleton SQLAlchemy
  engine (`pool_pre_ping=True`, mirror AP-MT `backend/app/db.py:46`),
  `tenants` table (`account_ref TEXT PRIMARY KEY, tenant_id TEXT NOT
  NULL`), once-guarded `create_all`, and `get_or_mint_tenant(account_ref)`
  preserving the race-safe two-step shape: `INSERT … ON CONFLICT
  (account_ref) DO NOTHING` then re-`SELECT` (AP-MT `identity.py:270-304`'s
  shape — do not "optimize" to `RETURNING`, it returns nothing on
  conflict). A new file is deliberate: zero collision surface with the
  concurrent coder, and no edits to `main.py`'s lifespan.
- `backend/app/core/identity.py` — delete `_tenant_db_path` (:228) and the
  sqlite body of `_get_or_mint_tenant` (:234-264); the function becomes a
  thin call into `tenant_store`. `resolve_tenant_id` (:267-285) keeps its
  exact two-branch shape and docstring role — see §6 sequencing.
  `require_tenant_or_service` (:288) stays an **async** generator and keeps
  `run_in_threadpool` around the now-SQLAlchemy-sync resolution (memory
  `fastapi-sync-dependency-breaks-contextvars`: a sync dependency here
  leaks the tenant ContextVar across requests).
- `backend/app/config.py` — `identity_tenant_db_path` (:67) replaced by
  `identity_database_url: str = ""` + a boot validator in the existing
  guard-rail style (:277-295): URL unset → refuse to boot. Tests set it
  explicitly; prod `.env` gets
  `postgresql+psycopg://postgres:<pw>@aw-stack-aw-postgres-1:5432/knowledgeable`.
- `docker-compose.yml` — remove the `identity-data` mount (:47-53) and the
  volume declaration (:60). `documents-data` untouched.
- `backend/tests/conftest.py:147-155` — the `identity_db` fixture swaps
  from a tmp sqlite *path* to a tmp SQLAlchemy sqlite *URL*
  (file-based, per-test). Test-only SQLAlchemy-sqlite is the house pattern
  (AP-MT runs its whole suite on `AGENTS_DATABASE_URL='sqlite://'`) and is
  explicitly not what the order forbids; the card's own check hint greps
  for `sqlite3.connect`, which disappears entirely.
- `.env.example` — document `IDENTITY_DATABASE_URL`.
- **NEW** `scripts/migrate_identity_to_postgres.py` — one-shot, see §4.

**`repos/aw-app-knowledgeable`:**

- `aw-app.json` — add `db:own-tables` + `state:own` to `permissions`;
  version bump (reinstall required for new grants — memory
  `app-contributed-skill-refresh-needs-reinstall`).
- `knowledgeable_app/bulk_ingest.py` — drop `sqlite3` import, `_data_dir`
  journal file, `journal_path()`, `_connect()`, `_tick_lock`. Journal
  access goes through a `db` parameter duck-typing `ctx.db`
  (research-search's documented seam), threaded from
  `routes.py:build_routes(ctx)` into `scan`/`run_tick`/`status`/`report`.
  Table `app__knowledgeable__bulk_ingest_files`, same columns
  (`scanned_at`/`updated_at` stay `DOUBLE PRECISION` epoch), same three
  indexes. `run_tick` wraps in a non-blocking `ctx.state.lease`; a held
  lease returns the same declared `blocked` shape `_tick_lock` produces
  today. DbTables is sync — keep `run_in_threadpool` at the route layer.
- **NEW** `scripts/migrate_journal_to_postgres.py` — one-shot, see §4.
  Lives in `scripts/`, outside `knowledgeable_app/`, so the check-hint grep
  stays at zero; it deletes the sqlite file as its last verified step, so
  nothing is left behind either way.

**`repos/aw-stack`:**

- `scripts/backup-knowledgeable-volumes.sh` — delete the entire
  identity-data half (:183-259 snapshot/manifest/pack, :274-279 upload,
  plus the `IDENTITY_*` wiring at :68, :118, :128-130, :141-143) and the
  header's SQLite/third-orphaning-vector prose (:26-54). The script becomes
  documents-data-only. `KNOWLEDGEABLE_SERVICE_TENANT_ID` disambiguation
  moves nowhere: after migration the mapping lives in a `pg_dumpall`-covered
  database, and the service tenant id can be recorded there too if ever
  needed — the manifest hack existed only because SQLite was outside the
  Postgres backup.
- `.github/workflows/backup-knowledgeable-volumes.yml` — strip the
  identity references (:10-11, :23, :31, :34-37).
- No new backup work: `backup-postgres.sh` is `pg_dumpall` (:129), the
  `knowledgeable` database is covered on its first nightly after creation.
  The frozen `files/aw-knowledgeable-identity/daily/*` S3 slots stop
  rotating and remain as historical artifacts — harmless; optionally clean
  after a quiet month.

**`docs/design/aw-knowledgeable-fs-sync.md:19-23`** — amended in the same
commit as this document: the claim that "the connector's journal stays
SQLite" predates this card's ruling and is now false.

## 4. Data migration — counts or it didn't happen

Order matters for identity because a row lost = a returning account silently
re-mints an empty tenant (the exact disaster
`backup-knowledgeable-volumes.sh:10-18` documents as its reason to exist).

1. **Create the database** (one-time, operator):
   `docker exec aw-stack-aw-postgres-1 psql -U postgres -c 'CREATE DATABASE knowledgeable'`.
2. **Identity import (additive, pre-deploy):**
   `scripts/migrate_identity_to_postgres.py` runs in a throwaway container
   on `aw-stack-net` with `identity-data` mounted ro (same mechanics as the
   backup script at :191-205): read all `(account_ref, tenant_id)` rows via
   stdlib sqlite3, `INSERT … ON CONFLICT DO NOTHING` into Postgres, print
   `sqlite_count`, `pg_count`, and fail non-zero unless every sqlite row
   exists in Postgres with an identical `tenant_id` (a differing id must
   ABORT, never overwrite). Idempotent by construction.
3. **Deploy** the new aw-knowledgeable image (now Postgres-only).
4. **Re-run the import** once post-deploy — closes the window where an
   account first authenticated between steps 2 and 3. Re-verify counts.
5. **Journal import:** `scripts/migrate_journal_to_postgres.py` runs inside
   the workspace container (it imports `src.api.db.get_engine` /
   `src.apps.db_tables` from `/opt/aw-workspace`, which Tier-1 scripts can —
   precedent: `src/cli/local_client.py` usage pattern): rows from
   `bulk_ingest.sqlite` → `app__knowledgeable__bulk_ingest_files`, verify
   total AND per-status counts match, then **delete the sqlite file**. Run
   while no ingest tick is active (check `bulk-ingest/status` first).
6. **Delete the volume:** remove `identity-data` from compose (ships with
   step 3's deploy), then `docker volume rm aw-knowledgeable_identity-data`
   after step 4's counts pass. The last 7 S3 slots remain for a week as the
   natural safety net — that is retention, not a fallback path.

## 5. What was rejected, and why

- **Both stores into the aw-stack Postgres.** The connector would need a
  cross-stack credential and a routable path from inside the workspace
  container for what is per-workspace scratch; it also bypasses the
  facade the workspace built precisely so Tier-1 apps don't hold raw DSNs
  (`research_search_app/mcp_ingest.py:9-13` calls that bypass out as the
  risk).
- **Both stores into the workspace Postgres.** Inverts ownership for
  identity: aw-knowledgeable is a standalone service with its own deploy
  and backup story on aw-stack; parking its auth-critical state in a
  workspace's schema ties service availability to a workspace install. Also
  contradicts the 2026-09-27 closed decision.
- **Journal on `ctx.state.kv`.** TTL'd, Redis-backed, degrade-open — wrong
  durability class for a ledger (see §2).
- **Journal migration via the app `migrations/` mechanism.** It is SQL-only
  (`src/apps/migrations.py:12-16`) — it cannot read a sqlite file.
- **Auto-import inside `plugin.activate()`.** Would keep `sqlite3.connect`
  living in `knowledgeable_app/` forever (fails the card's own check hint)
  and hides a data migration inside app boot, where a half-failure is
  invisible. One-shot operator script with printed counts is auditable.
- **pg advisory lock for tick exclusion.** Advisory lock keys are
  instance-global (collision surface across apps) and would have to be
  smuggled through DbTables' table-scoped `execute`; `ctx.state.lease` is
  the approved primitive for exactly this.
- **Keeping the journal on SQLite** (the fs-sync doc's earlier position,
  `aw-knowledgeable-fs-sync.md:19-23`). Overruled by the order this card
  carries — "there should not exist a SQLite" names no exception for
  scratch — and the journal's `database is locked` history (:93-101) was
  already an argument it had outgrown the file.

## 6. Sequencing against §9 (`KNOWLEDGEABLE_OWNER_SUBS`) — not yet dispatched

§9 adds an allowlist branch to `resolve_tenant_id` *before* the mint call.
This design keeps `resolve_tenant_id`'s body shape byte-compatible with
that insertion: the service branch (:283-284) and the final
`_get_or_mint_tenant(str(identity.user_id))` call (:285) survive verbatim —
only `_get_or_mint_tenant`'s internals move to `tenant_store`. §9's own
rejected-alternatives section anticipated exactly this ("that function is
the thing the Postgres card is about to rewrite; keep the policy in
`resolve_tenant_id`") — the two cards compose with zero overlap. The §9
claim that the allowlist "survives the SQLite→Postgres identity migration
untouched" **remains true** under this design: it is declared config +
a branch in a function this design does not reshape.

## 7. What this makes harder later

- **aw-knowledgeable gains a second hard runtime dependency.** Today a
  human request needs Neo4j; after this it needs Neo4j AND the aw-stack
  Postgres. An aw-postgres outage becomes a knowledgeable auth outage.
  Accepted: AP-MT already lives with the identical dependency, and the
  alternative (local file) is what we're deleting.
- **The journal becomes workspace-schema state.** It survives app
  reinstalls (DbTables never auto-drops — `db_tables.py:12-21`) but no
  longer survives moving the workspace to a fresh host unless the workspace
  Postgres moves too. Recoverable: a rescan + the server's `content_hash`
  MERGE dedup rebuilds it, at re-upload cost.
- **One more consumer of the central Postgres password** in
  aw-knowledgeable's `.env`.
- **T2 (real tenant claim in the JWT)** still retires `tenants` cleanly —
  the swap-point contract is unchanged — but now retiring it includes a
  `DROP TABLE` in someone's rollout notes instead of deleting a file.

## 8. Risks for the coders

- **Concurrency, again:** `coder-sonnet` is live in `repos/aw-knowledgeable`
  on `graph.py` / `api/documents.py` / `test_tenant_isolation.py` /
  frontend. This card's aw-knowledgeable surface is `identity.py`,
  `config.py`, `conftest.py`, compose, one new module, one new script.
  Work in a git worktree; rebase before pushing.
- **The async-generator trap is real and already documented** at
  `identity.py:294-301` — SQLAlchemy sync calls must stay behind
  `run_in_threadpool`; do not convert the dependency to sync and do not
  make the engine async (house pattern is sync SQLAlchemy).
- **`DbTables.execute` only `fetchall()`s when the SQL starts with
  `select`** (`db_tables.py:78-80`) — a `RETURNING` or CTE write loses its
  rows silently; use `execute_multi` (checks `returns_rows`,
  `db_tables.py:105`) where that matters.
- **Boot validator vs. CI:** `identity_database_url` unset refuses boot —
  every test env and the Dockerfile healthcheck path must set it or the
  suite dies at import of `config.settings`. Mirror how conftest already
  monkeypatches settings (:147-155).
- **Migration script abort semantics:** on `tenant_id` mismatch between
  sqlite and Postgres, abort loudly. Overwriting either direction loses a
  tenant mapping — the one unrecoverable mistake in this whole card.
- **The backup script edit must keep documents-data byte-identical** —
  M2b's restore rehearsal was proven against that artifact shape; don't
  rename its S3 key or manifest fields while deleting the identity half.
- **Tick lease:** lease TTL must exceed the longest real tick
  (`max_uploads=200` has been observed to outlive a client timeout —
  `bulk_ingest.py:92-100`); a lease that expires mid-tick readmits the
  overlap this exists to prevent.

## 9. AP-MT — verified clean, nothing to migrate

Confirmed against the working tree, not the card's note:
`backend/app/db.py` is dialect-agnostic SQLAlchemy; every sqlite branch is
`_engine_is_sqlite()`-guarded test-engine support. Production default
(`config.py:28`) and `.env.example:24` both point at
`aw-stack-aw-postgres-1`; `find … -name '*.sqlite*' -o -name '*.db'` over
the repo returns nothing. SQLite exists there only as the test suite's
`AGENTS_DATABASE_URL='sqlite://'` default. No production SQLite, no action.

## 10. Verification (what QA should check)

- `grep -rn 'sqlite3.connect' repos/aw-knowledgeable/backend/app
  repos/aw-app-knowledgeable/knowledgeable_app` → zero hits (the card's
  check hint).
- Identity: `sqlite_count == pg_count` printed by the migration script,
  twice (pre- and post-deploy runs); a known `account_ref` resolves to the
  SAME `tenant_id` through the live API after cutover.
- Journal: total and per-status counts identical; `bulk_ingest.sqlite`
  gone; a `bulk-ingest/run` tick works against Postgres under two
  concurrent requests (second gets `blocked`, not a 500).
- `backup-knowledgeable-volumes.sh` runs green with `SKIP_UPLOAD=true`
  (documents-only) and never mentions identity.
- Both repos' suites: report the real pass counts.
