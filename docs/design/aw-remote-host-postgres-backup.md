# Automated backup for `aw-remote-host-postgres` — design

**Card:** `3fd5bf3b-9510-815a-91e7-dc8f29fd2def` — nothing backs up the shared
Postgres, and it now holds the execution index.
**Status:** DESIGN ONLY. Nothing here has been provisioned, scheduled or run.
Unblocks step 6 of the exec-index cutover
(`execution-index-migration-off-aw-app-kb.md`): kb's dual-writes stay alive
until the first *verified* nightly artifact of this design exists in the
destination.
**Date:** 2026-10-18. Every number below was measured live that day (read-only,
via `host_id=bare-metal-privileged`); re-measure before trusting them later.

---

## 1. The decision

Extend the **existing aw-stack nightly backup pipeline** — the GitHub Actions
workflow that already dumps the two docker-hosted clusters to S3 every night at
03:00 UTC (`repos/aw-stack/.github/workflows/backup-postgres.yml`,
`repos/aw-stack/scripts/backup-postgres.sh`) — with a **second, independent
job** that:

1. dumps `aw-remote-host-postgres` (all four databases, `pg_dumpall --clean
   --if-exists`) through a nested exec from the bare-metal runner,
2. **restores the dump into a throwaway pgvector container and checks row
   counts against the live server before anything is uploaded** — a run whose
   restore fails never ships an artifact and goes red,
3. uploads to the **same S3 bucket** under its own prefix
   (`aw/postgres-remote-host/daily/<weekday>.tgz`), 7 rotating weekday slots
   plus 6 monthly slots,
4. writes a status file into the workspace tree that an `aw-workspace-cli
   doctor` check turns into a dead-man's switch: "last verified backup is
   older than 26 h" is a named degradation, not something a human has to go
   look for.

v1 needs **zero new credentials, zero new infrastructure**: the AWS key
already sits in aw-stack's repo secrets, the bucket already exists, the
restore procedure for this exact artifact contract was already validated on
2026-06-29 — including a pgvector cluster restored into `pgvector/pgvector`
(KB: `postgres-backup-restore-validation.md`). This design's whole job is to
turn "rehearsed, not backed up" into "rehearsed on every single run".

The destination is Frederico's call (§3); everything else below is decided.

## 2. Measured facts the design stands on

- `aw-remote-host-postgres` is a **podman container inside the `aw-remote-host`
  workspace container**, which itself runs under **docker on the bare metal**
  (`docker ps` shows it; bare-metal root podman holds only `aw-hosted-crispal`).
  Port published to the workspace container's `127.0.0.1:5432` only.
- The nested exec path **works today**:
  `docker exec aw-remote-host podman exec aw-remote-host-postgres pg_dumpall --version`
  → `pg_dumpall (PostgreSQL) 16.14`, exit 0.
- The GHA runners run as `ubuntu` on the bare metal
  (`/home/ubuntu/gh-runners/*`); `/var/run/docker.sock` is mode 666 and
  `/etc/sudoers.d/ubuntu` is `NOPASSWD:ALL` — the runner can run the nested
  exec with no new access grants. (That same fact is a *threat* in §7.)
- Database sizes: `aw_workspace` 670 MB (of which
  `workspace_aw."app__aw-app-uc-phd__chunks"` is 551 MB — embeddings),
  `execution_index` 226 MB (incl. the 83 MB HNSW index, which a logical dump
  does **not** carry — it's rebuilt by `CREATE INDEX` on restore),
  `ux_proto` and `awserv` ~8 MB each. `awserv` currently has **zero user
  tables**.
- `aw_workspace` carries `telegram_messages` / `telegram_chats` /
  `notifications` — personal content. The workspace **secret store is not in
  this Postgres** (file-based under `AW_WORKSPACE_HOME/secrets/`,
  `src/apps/paths.py`), so dumps hold personal data but not keys.
- `/usr/bin/restic` 0.18.1 is installed on the host and wired to nothing
  (card measurement, taken as given).

## 3. Q2 first — where the backups go (Frederico's call)

| Option | Cost | What breaks it |
|---|---|---|
| **A. Existing S3 `tekflox-aw-backup` (recommended)** | ~+6 GB ≈ **$0.15/mo**; zero provisioning, zero new credentials | Single AWS account; the pipeline credential can overwrite weekday slots (`PutObject` *is* overwrite) and delete monthlies — no bucket versioning/object-lock today; the key died silently once before (2026-08-31, `InvalidClientTokenId`) — the script's `sts get-caller-identity` preflight now catches that at run time, not before |
| B. Backblaze B2 (S3-compatible) | ~$0.006/GB → ~$0.04/mo | New account + new key pair (→ aw-stack repo secret, same sanctioned pattern); real value is as a **second** copy for provider diversity, not as the first |
| C. restic repo (on S3/B2), `/usr/bin/restic` already present | Storage much lower (dedup across near-identical nightly dumps); encryption + `restic check` built in | New repo password = a credential with an escrow problem (lose it → lose every backup); restore runbook diverges from the rehearsed `psql` path; needs its own prune/check maintenance jobs |
| D. Another machine (Mac.Home / a VPS) via rsync+ssh | No cloud bill | Availability (Mac.Home is flaky), same-geography risk, ssh key management, and nobody watches it |
| E. GitHub artifacts / LFS | — | Rejected outright: 90-day artifact retention, size limits, personal data in a repo |

**Recommendation: A**, with two bucket-side hardenings that are Frederico's /
Terraform's to apply (not this pipeline's): **enable bucket versioning** (so an
overwritten or deleted slot is recoverable) and optionally object-lock in
governance mode on the `monthly/` prefix. B as a later second copy if wanted.
Until versioning is on, ransomware protection is incomplete (§7).

If Frederico picks B/C/D, the credential goes into the **workspace secret
store** (for anything a workspace-side process needs) or **aw-stack repo
secrets** (for the runner job) — never a file on the host, never the code.
This design creates none of them.

## 4. Q1 — logical dump, not volume snapshot

`pg_dumpall --clean --if-exists` per cluster, identical to the validated
artifact contract (`backup-postgres.sh:129-146` — same header/content checks,
same bootstrap-role stripping that `make_dump_restore_friendly()` introduced,
validated against a real restore 2026-06-29).

Why logical wins here:

- **The restore is already rehearsed for exactly this shape** — including a
  pgvector cluster restored into `pgvector/pgvector` with
  `psql -v ON_ERROR_STOP=1`. A snapshot restore has never been rehearsed on
  this stack, and a backup nobody restored is the failure mode this card
  exists to kill.
- The PGDATA volume lives **inside the workspace container's podman
  graphroot** — the exact class of state the 2026-09-02 incident lost
  (`byod-postgres-lost-bind-mount-2026-09-02`). Snapshotting it means copying
  a hot data directory, which without `pg_backup_start()` choreography is a
  corrupt backup that *looks* complete; with it, it's a bespoke mechanism
  nobody here has rehearsed. There is no LVM/ZFS snapshot layer in use by this
  stack's tooling to make it atomic for free.
- Logical dumps are version-portable (restore into pg16 or pg17 both
  validated), restore selectively (one database at a time via a scratch
  cluster), and **don't carry the 83 MB HNSW index** — it rebuilds on restore.
- The pgvector objection (vectors-as-text compress badly) is real but small at
  this scale: the whole cluster is ~900 MB on disk including indexes;
  estimated artifact **300–500 MB** gzipped. Implementation step 0 is a
  `SKIP_UPLOAD=true` dry run that measures the real size and wall-clock before
  the schedule is wired.

**`execution_index` is included in the daily dump.** It's the least critical
database (30-day retention, documented rebuild from AP-MT), but including it
costs cents and is precisely what lets step 6 stop kb's dual-writes with a
straight face — after the flip, this artifact *is* the second copy. The knob
if it ever bloats: `pg_dump --exclude-table-data` for the chunks table or a
weekly-only cadence for that database; not needed at 226 MB.

Runner-up (named so it isn't re-litigated): per-db `pg_dump --format=custom`
+ `pg_dumpall --globals-only`. Better selective restore and parallelism, but
it breaks the one artifact contract this stack has a rehearsed runbook for.
**Trigger to revisit:** artifact >1 GB, or the first time someone needs a
single-database restore in anger and the scratch-cluster detour hurts.

## 5. Q3 — retention and encryption

**Retention:** 7 rotating UTC-weekday slots (house convention, overwritten
weekly) + on the 1st of each month, copy that day's verified artifact to
`monthly/YYYY-MM.tgz`, keep 6 (script-side prune, or an S3 lifecycle rule if
Frederico prefers it bucket-side). Weekday slots cover "we broke it this
week"; monthlies cover the slow failure — a bad migration whose corruption
nobody notices for two months (§7). ~6 GB steady state.

**Encryption at rest: SSE-AES256 (server-side) for v1 — client-side encryption
is recommended as a fast-follow, not a blocker.** Reasoning: the dumps carry
personal content (`telegram_messages`) but **no credentials** (secret store is
file-based, not in this pg); the bucket is private; and everyone who could
read the bucket already holds live access to the same data. The fast-follow
that makes it strictly better: **age asymmetric encryption** — the backup job
holds only the *public* key (not a credential; committable to aw-stack), so
the pipeline still needs zero secrets; the private key is generated by
Frederico, lives in the workspace secret store plus one copy outside the blast
radius (password manager). Named trade-offs, which is why it's phase 2: lose
the private key → lose every backup; and the monthly restore-from-S3 check
(§6) can then only verify sha256, not re-restore, without a human presenting
the key.

## 6. Q4 + Q5 — scheduling, and restore verification as part of the job

**Scheduling: a second job in `backup-postgres.yml`** (same 03:00 UTC cron,
same `aw-baremetal` runner, same `concurrency: backup-postgres` group so the
two jobs serialize, `workflow_dispatch` with the same `slot/skip_upload/
keep_artifact` inputs). New script
`repos/aw-stack/scripts/backup-remote-host-postgres.sh`, a pattern-copy of
`backup-postgres.sh` with two differences: the exec prefix is
`docker exec aw-remote-host podman exec aw-remote-host-postgres`, and the
verify stage below runs before upload.

Why not the alternatives the card names:

- **root crontab on the host:** it's the only live mechanism today, which is
  an argument *against* it — it's also the mechanism in which this host
  silently had no backup for months. Cron output goes nowhere anyone looks,
  and the AWS credential would have to live in a root-readable file on the
  host, which the hard constraint forbids (and which already failed once: the
  dead `SERVICE_AWS_*` pair baked into `/opt/agentic-workspace/.env`,
  `backup-postgres.yml:28-31`). GHA gives logging, red-run visibility, and
  repo-secret credential handling that this exact pipeline already uses.
- **restic + systemd timer:** adds a second backup toolchain, a new secret
  with an escrow problem, and an unrehearsed restore path — for data that the
  existing pipeline handles with zero new parts. restic's dedup/encryption
  shine for host-level *file* backups; if that scope ever opens, revisit (§3C).
- **workspace-internal scheduled task:** scheduled tasks here dispatch agent
  sessions — an LLM in the loop of a backup is nondeterminism where none is
  tolerable, and the workspace container (which gets recreated) would be a
  single point of both production and scheduling.

**Restore verification — every run, before upload** (this is the core of the
design, not a follow-up):

1. **Capture ground truth at dump time** into `manifest.json`: for each
   database, `count(*)` of a sentinel table (NOT `reltuples` — those are
   estimates): `execution_index.executions` (24,970 on 2026-10-18),
   `aw_workspace` → `workspace_aw."app__tasks__runs"` and
   `workspace_aw.telegram_messages`, `ux_proto.projects`; `awserv` has zero
   user tables today, so its sentinel is "database exists" only (leave a
   comment saying why, so the first real table added there gets a sentinel).
   Plus a freshness anchor: `max(created_at)`-style timestamp from one
   continuously-written `aw_workspace` table.
2. **Dump + content checks** exactly as `backup-postgres.sh:129-146` (header
   grep, `CREATE DATABASE` grep, bootstrap-role strip).
3. **Real restore**: `docker run --rm` a scratch **`pgvector/pgvector:pg16`**
   (the dump contains `CREATE EXTENSION vector`; a plain `postgres:16-alpine`
   fails at the extension — same gotcha as `backup-postgres.sh:59-63`), then
   `psql -v ON_ERROR_STOP=1 -f` the dump. Any SQL error = run fails.
4. **Acceptance queries against the restored copy**: all four databases
   exist; each sentinel `count(*)` ≥ the captured value (append-mostly tables
   — the floor is what catches truncation) and within a small tolerance of it;
   the freshness anchor is < 26 h old (catches "valid dump of a stale/empty
   server"); and on `execution_index`, one nearest-neighbour query
   (`ORDER BY embedding <-> ... LIMIT 1`) returns a row — proving the
   extension, the data, and the HNSW rebuild all actually work.
5. Only now: manifest gains `verified: true`, `verified_at`, the sentinel
   counts, and a **sha256 per dump file**; pack, upload with `--sse AES256`,
   `head-object` size read-back, rotation listing (all existing patterns).
6. **Monthly, as a separate job**: download the current weekday artifact from
   S3, check sha256 against its manifest, and repeat steps 3–4 from the
   downloaded bytes. This closes the gap verify-before-upload leaves open:
   it proves the *stored* object restores, not just the local one. (This is
   also the job the 2026-06-29 rehearsal did by hand, now on a cron.)
7. **Dead-man's switch**: after a verified upload, the job writes
   `{slot, verified_at, counts, sha256s}` to
   `/opt/aw-workspace/.aw-workspace/data/backup-status/remote-host-postgres.json`
   *inside the workspace container* (via `docker exec aw-remote-host` — the
   tree is host-mounted, so this survives container recreation). A new core
   doctor check (`src/cli/commands/doctor.py`; the app self-check pattern is
   `_app_checks` at `doctor.py:103`, but this belongs in core — it isn't
   app-owned) goes red when `verified_at` is older than 26 h. Doctor is this
   stack's channel for exactly this failure class — silent degradation — and
   the System Analyst daily audit already runs it. This is what catches the
   mode a red GHA run cannot: the job that **stopped being scheduled at all**
   (GitHub disables cron workflows after 60 days of repo inactivity; a dead
   runner looks identical).

## 7. Q6 — what this does NOT protect against, plainly

- **Ransomware / `rm -rf` on the host:** the data survives (S3 is off-host) —
  but **the backups themselves don't, yet**. Anyone on the bare metal can act
  as the runner (docker.sock is 666, `ubuntu` is `NOPASSWD:ALL`) and the AWS
  key is in the job's environment during a run; with it, all 7 weekday slots
  can be overwritten (`PutObject` is overwrite) and the monthlies deleted.
  **Until bucket versioning / object-lock is enabled (§3, Frederico-side),
  a competent attacker with host access can destroy the live data *and* the
  backups.** Named, not solved here.
- **A bad migration that corrupts rows:** the dump preserves the corruption
  faithfully, and the verify stage will happily pass it — count floors and
  freshness probes do not detect *semantic* damage. Protection is retention
  depth only: 7 days of dailies, ~6 months of monthlies. If corruption is
  noticed later than that, the data is gone.
- **A silently truncated dump:** this is the one the design actually kills —
  a truncated file fails `ON_ERROR_STOP` restore or the count floor, the run
  goes red, and nothing is uploaded. Residual: truncation *after* verify,
  in S3 itself, is caught monthly (sha256 + re-restore), so the worst case is
  ~4 weeks of believing in one corrupt slot — while 6+ other verified slots
  still exist.
- **Up to 24 h of data loss:** nightly logical dumps have no WAL trail. For
  `aw_workspace` (the no-second-copy database) that means a day of journal
  writes can vanish. If that RPO is unacceptable, that's a scope conversation
  with the PO — the answer is WAL archiving (wal-g/pgBackRest), a different
  design, deliberately not smuggled into this one.
- **The workspace container being down at 03:00:** that night's job dies at
  preflight (loudly — red run), and because this is a *separate job*, the two
  existing clusters' backup is unaffected. Next night self-heals; the doctor
  check flags it if it persists.
- **AWS account closure / region loss:** single destination. Option B (§3) as
  a second copy is the mitigation if Frederico wants it.
- **Alert delivery:** the dead-man's switch lives in workspace doctor — if the
  workspace itself is down *and* nobody looks at GHA, nothing pages anyone.
  This stack has no out-of-band pager; named as a residual.

## 8. Where it lands (implementation map for the Coder)

| Repo | Change |
|---|---|
| `repos/aw-stack` | **New** `scripts/backup-remote-host-postgres.sh` — pattern-copy of `scripts/backup-postgres.sh` (keep its preflight/content-check/manifest/read-back structure verbatim) with the nested exec prefix, the §6 verify stage, sentinel capture, monthly-promote, and the status-file write |
| `repos/aw-stack` | `.github/workflows/backup-postgres.yml` — second job `backup-remote-host` (same cron/runner/concurrency/inputs), third job `verify-from-s3` on a monthly cron + `workflow_dispatch` |
| `aw-workspace` (this repo) | Core doctor check in `src/cli/commands/doctor.py` reading `.aw-workspace/data/backup-status/remote-host-postgres.json`, red at >26 h |
| Untouched | `TARGETS` and the artifact contract of the existing two clusters (`backup-postgres.sh:70-73`); the running Postgres; root's crontab; restic; the bucket itself |

Implementation step 0, before wiring the cron: one `workflow_dispatch` run
with `SKIP_UPLOAD=true` to measure real artifact size and dump+verify
wall-clock (the 45-minute job timeout at `backup-postgres.yml:69` is probably
fine; confirm, don't assume).

## 9. Rejected alternatives (so they aren't re-proposed)

1. **Volume snapshot of PGDATA** — unrehearsed, hot-copy-inconsistent without
   `pg_backup_start` choreography, version-locked to PG 16.14 + pgvector
   binaries, no selective restore, and the volume sits in exactly the state
   class the 2026-09-02 incident destroyed. (§4)
2. **Per-db `pg_dump -Fc`** — runner-up; breaks the rehearsed artifact
   contract; revisit triggers named in §4.
3. **restic as the transport** — new credential + escrow, diverging restore
   runbook, second toolchain; right shape only if host-level file backup ever
   becomes scope. (§3C, §6)
4. **root crontab / systemd timer on the host** — unmonitored, and forces a
   credential onto host disk against the hard constraint. (§6)
5. **Workspace-internal scheduled task** — agent-session scheduler, LLM in a
   backup loop, workspace container as single point. (§6)
6. **Folding this cluster into the existing `TARGETS` loop / same artifact** —
   couples failure domains: a workspace-container recreate at 03:00 would
   `die` the whole run and take the two healthy clusters' backup with it; the
   exec mechanism and the verify stage differ anyway. Separate job, separate
   artifact prefix, untouched validated path.
7. **WAL archiving / PITR now** — better RPO, but new storage layout, no
   rehearsed restore, and a scope the card didn't grant. Named as the upgrade
   path if 24 h RPO is rejected. (§7)
8. **Excluding `execution_index` because it's rebuildable** — saves cents,
   costs the clean "backup = second copy" story that step 6 of the cutover
   needs. (§4)

## 10. What this makes harder later

- **Ties backups to the GHA self-hosted runner.** If the runners ever move off
  the bare metal, the backup silently moves with them and breaks (needs host
  docker reach). The doctor dead-man's switch is what turns that into a
  visible failure.
- **Hardcodes two container names** (`aw-remote-host`,
  `aw-remote-host-postgres`) in the exec prefix. If the Postgres is ever
  adopted into aw-stack's compose (the way `aw-pgvector` was), the special job
  should be deleted and the cluster folded into `TARGETS` — leave that note in
  the script header.
- **Plain-SQL artifact grows with uc-phd's chunks table** (551 MB and
  growing). The `-Fc` migration (§4 runner-up) becomes necessary eventually,
  and switching artifact format means a runbook change plus one manual
  re-rehearsal.
- **Phase-2 age encryption changes the artifact format** (`.tgz` →
  `.tgz.age`) — another runbook fork, and the monthly S3-side re-restore then
  needs a human with the private key.
- **The status-file path becomes an implicit cross-repo contract** between an
  aw-stack script and a workspace doctor check. Document it in both files;
  neither CI can see the other break it.

## 11. Risks for the Coder (the non-obvious ways this breaks)

- **Two exec layers in the dump pipe.** `docker exec … podman exec … pg_dumpall
  > file` streams the dump through both; run non-tty (no `-t`), keep
  `set -o pipefail`, and treat the verify-restore as the real integrity check
  — an inner-layer disconnect can in principle truncate the stream, which is
  exactly what step 3–4 of §6 exists to catch.
- **`reltuples` lies.** The sentinel numbers in this doc came from `reltuples`
  (estimates, `-1` for never-analyzed tables). The manifest must capture
  `count(*)`.
- **`awserv` is empty.** A verify stage that demands a sentinel row count from
  every database fails forever on it. Assert existence only, with a comment.
- **Quoted identifiers.** The big tables live in schema `workspace_aw` with
  names like `app__aw-app-uc-phd__chunks` — hyphens and double underscores;
  quote everything in sentinel SQL.
- **The scratch restore needs pgvector.** `pgvector/pgvector:pg16`, not
  `postgres:16-alpine` — the dump's `CREATE EXTENSION vector` is the
  difference between a passing and a structurally-impossible verify. HNSW
  rebuild happens inside the restore; it's seconds at 25 k rows but budget it
  in the timeout math.
- **Don't verify via `docker logs`.** `--since` undercounts on this podman
  (known trap); the GHA run output and the status file are the truth.
- **Schedule contention.** 03:00 UTC is deliberate (`backup-postgres.yml:39-41`
  — 05:00 Sunday maintenance, 06:17 e2e). The added verify stage lengthens the
  window; the shared concurrency group keeps the two backup jobs serialized —
  keep it.
- **Credentials.** Reuse `secrets.AWS_ACCESS_KEY_ID`/`SECRET` already on
  tekflox/aw-stack. Do not introduce a host-side env file (the dead
  `SERVICE_AWS_*` precedent), and if a new destination is chosen in §3, its
  credential goes to repo secrets / the workspace secret store — nowhere else.
