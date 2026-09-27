# aw-knowledgeable — CI/CD design (deploy-on-push)

Status: **design, not code.** Written by the Architect agent, 2026-09-27, for
card `feature:aw-knowledgeable-cicd-auto-deploy-prod`.

Request, verbatim: *"Faz o ci/cd dele, a cada push, faz o release cut e deploy
em prod, testes rodando na pipeline."*

Scope: the delivery pipeline of `fredericowu/aw-knowledgeable` only. Out of
scope: M2 (Neo4j backup), M4/M5/M7, the ingestion connector, the Caddy
hostname (§4 of `aw-knowledgeable-infra.md` — this service stays loopback-only
until that card lands).

Every claim below is grounded in a file read or a live API call made in this
session. Where I could not verify something, it is named as unverified.

---

## 0. The one thing not to re-litigate

`aw-knowledgeable-infra.md:102-105` **rejected** "`:latest` with an auto-deploy
on push", and `:708-713` says *"do not 'fix' it by adding a push trigger to
aw-stack"*. Read in context, both statements are about **aw-stack** — the
shared Postgres/Redis/Caddy/Neo4j stack whose own `deploy.yml` is
`workflow_dispatch`-only by design (`repos/aw-stack/.github/workflows/deploy.yml:75-76`).

They do **not** bind aw-knowledgeable's own compose project. The split the
design doc created in §1 — durable store in aw-stack, application container in
this repo's own compose — exists precisely so the application can iterate on a
different cadence from the stack. Auto-deploy-on-push for the *app* is the
payoff of that split, not a violation of it.

**Invariant this design must preserve:** nothing in aw-knowledgeable's pipeline
ever triggers, restarts, recreates, or `docker compose`-touches anything in
`repos/aw-stack`. Moving `aw-neo4j` stays a human `workflow_dispatch` in that
repo, forever.

---

## 1. Where "prod" is, and how CI reaches it

### Decision

**Prod is the bare-metal host, at `/opt/aw-knowledgeable`, deployed by a
dedicated repo-level self-hosted runner `aw-baremetal-aw-knowledgeable` running
directly on that host — no SSH key, no deploy secrets for host access.**

This is not a new mechanism; it is the only mechanism every sibling repo uses.
Verified live via `gh api repos/<repo>/actions/runners`:

| repo | runner | status |
|---|---|---|
| `fredericowu/agents-platform-multitenant` | `aw-baremetal-agents-platform-multitenant` | online |
| `fredericowu/agentic-workspace` | `aw-baremetal-agentic-workspace` | online |
| `fredericowu/aw-backend` | `aw-baremetal-aw-backend` | online |
| `fredericowu/aw-console` | `aw-baremetal-aw-console` | online |
| **`fredericowu/aw-knowledgeable`** | **none — empty list** | **must be created** |

All carry the same label set `self-hosted,Linux,X64,aw-baremetal`.

**Why a per-repo runner rather than the shared pool:** the four
`aw-baremetal-tekflox*` runners are **org**-level runners on the `tekflox` org
(runner group `aw-private`, per KB `tekflox-aw-baremetal-four-org-runners-20260730.md`).
`fredericowu` is a **User** account, not an org (`gh api users/fredericowu --jq
.type` → `User`, and `gh api orgs/fredericowu/actions/runners` → 404). GitHub
has no user-account runner group, so a personal-account repo **cannot** borrow
that pool. A repo-level runner is not a preference here, it is the only option
short of moving the repo to the org.

**Why the deploy job needs to be on the host at all:** the compose file joins
the external network `aw-stack-net`
(`repos/aw-knowledgeable/docker-compose.yml:46-49`) to reach
`aw-stack-aw-neo4j-1:7687`. That network lives on the bare metal's Docker
daemon. The deploy has to run against *that* daemon; nothing else will do.

### Where it lands

- **New** `repos/aw-knowledgeable/.github/workflows/deploy.yml`.
- Modified `repos/aw-knowledgeable/.github/workflows/test.yml` (see §3).
- A one-time on-host bootstrap (see §1a). No change to `repos/aw-stack`.

Copy the **shape** of
`repos/agents-platform-multitenant/.github/workflows/deploy.yml` — read it
end to end before writing, it is 414 lines of encoded incidents. Specifically
inherit, in this order:

1. `Preflight — disk` (`:73-87`). Memory `host-disk-near-full-breaks-apps-silently`:
   the host was at 95% / 22G free on 2026-09-15. `MIN_FREE_GB` with a prune
   fallback and a hard refuse.
2. `Reclaim root-owned leftovers in the deploy checkout` (`:104-113`). The
   runner is `ubuntu`; root-owned `.git/objects` kills the fetch.
3. **`Guard: refuse a stale (ancestor) re-run` (`:143-174`) — mandatory, see §4.**
4. `Record the current revision` + `Pin the running image as the rollback
   artifact` (`:216-248`).
5. `Move the checkout to this commit` — `git reset --hard <sha>`, naming the
   SHA, never `FETCH_HEAD` (`:250-259`, read the comment).
6. `Apply` → `docker compose up -d --build`.
7. `Health gate` → poll `http://127.0.0.1:8090/api/health`
   (`backend/app/main.py:30`, port from `docker-compose.yml:33`).
8. `Roll back` (`:305-334`) — restore the **pinned image**, do not rebuild.
9. `Page a human` on Telegram (`:359-393`), including the REFUSED wording for
   a guard refusal.

**Do NOT inherit** these three, and say why in a comment so the next reader
does not "restore" them:

- **AP-MT's two-lane rebuild/restart split** (`:261-287`). It exists because
  AP-MT bind-mounts `backend/` and runs `uvicorn --reload`, so a Python-only
  change needs no image build. aw-knowledgeable does not: its Dockerfile
  `pip install`s the package into the image (`Dockerfile:10-13`) and its
  compose mounts no source. Every change is an image change here — one lane,
  always `up -d --build`. A restart-only lane would silently deploy nothing.
- **The `Wait for a quiet window` gate** (`:190-214`). It reads AP-MT's `runs`
  table to avoid cutting live agent runs. aw-knowledgeable has no long-running
  jobs and no users (`is_live=false`). Adding a Postgres-dependent gate that
  swallows its own failure is pure downside.
- **The `paths:` filter on the push trigger** (`:24-30`). AP-MT filters because
  most of its tree is not deployable. Here the ask is literally "a cada push":
  trigger on every push to `master`, unfiltered. This also removes the guard's
  "not every master commit produces a deploy run" complication.

### Rejected

- **GitHub-hosted runner + SSH to the host** (the shape `aw-stack`'s and
  `aw-backend`'s deploys use: `secrets.SSH_PRIVATE_KEY` + `secrets.DEPLOY_HOST`,
  `aw-stack/.github/workflows/deploy.yml:129-142`). It would work without
  registering a runner, and that is its only advantage. Rejected because it
  puts a *new copy of a root/deploy SSH private key* into a fifth repo's
  secrets to buy nothing — both of those workflows already run their SSH
  *from* a self-hosted `aw-baremetal` runner anyway, so the SSH hop there is
  legacy, not a design choice. AP-MT's header says so outright: aw-backend's
  deploy "was written for a runner elsewhere and still SSHes".
- **Deploying aw-knowledgeable from `aw-stack`'s `deploy.yml`** by adding it as
  a service. Rejected by `aw-knowledgeable-infra.md:27-34` (its own compose,
  joining `aw-stack-net`) and by §0 above.
- **The `aw-frontend` → `aw-console` signed-webhook pattern** (KB
  `aw-frontend-static-caddy-console-webhook-20260730.md`: build → upload
  artifact → authenticated `POST /api/deploy/...` on a live service that
  replaces a static directory). Genuinely attractive: no runner, no git on the
  host. Rejected for two reasons. (a) It only fits a *static bundle*; here the
  deploy has to build an image and recreate a container on a specific Docker
  network — the receiving service would need Docker socket access, which is a
  much larger grant than a runner. (b) That path has a live, **open**
  failure: KB card `resilience:aw-console-deploy-webhook-ok-but-static-root-stale`
  — the webhook returned `{ok:true}` for at least 4 consecutive deploys while
  production served a bundle 2 days stale, and nobody noticed. A deploy
  mechanism whose success signal is known to lie is the wrong thing to copy
  for a service nobody is watching yet.
- **Cross-repo `workflow_dispatch` into `agentic-workspace`'s break-glass
  shell.** Needs a PAT in a second repo, gives no health gate, no rollback,
  and no run history tied to the commit. Fine as the bootstrap lever (§1a),
  wrong as the steady-state deploy.

### 1a. Bootstrap — the one-time, on-host prerequisites

Three things must exist on the bare metal before the first green deploy. All
three are one-time, and **all three are reachable without a human at a
keyboard** — I verified both halves of that claim live:

- A runner registration token can be minted with the credentials available to
  this session: `gh api -X POST repos/fredericowu/aw-knowledgeable/actions/runners/registration-token`
  returned a token with a 1-hour expiry.
- `fredericowu/agentic-workspace` has an active **`Bare-metal shell (break
  glass)`** workflow (id `342887685`, `.github/workflows/bare-metal-shell.yml`)
  that runs an arbitrary command as **root** on the bare metal via
  `workflow_dispatch`. That is the documented lever for exactly this.

The three:

1. **Register the runner.** Install a GitHub Actions runner configured for
   `https://github.com/fredericowu/aw-knowledgeable`, name
   `aw-baremetal-aw-knowledgeable`, label `aw-baremetal`, as a systemd service
   running as **`ubuntu`** — mirroring
   `tekflox-aw-baremetal-four-org-runners-20260730.md` (`/home/ubuntu/gh-runners/<name>`,
   `actions.runner.<scope>.<name>.service`). Confirm `gh api
   repos/fredericowu/aw-knowledgeable/actions/runners` shows it `online`
   before pushing anything.
2. **Create the checkout**, `git clone` into `/opt/aw-knowledgeable`, then
   `chown -R ubuntu:ubuntu` it. Root-owned is the failure AP-MT's
   reclaim step (`:104-113`) exists to survive; start clean instead.
   `/opt/aw-knowledgeable` mirrors `/opt/aw-stack`, not
   `/opt/agentic-workspace/repos/…` — this is a standalone service, and
   parking it under the monolith's `repos/` would imply a relationship that
   §1 of the infra doc deliberately does not have.
3. **Seed `/opt/aw-knowledgeable/.env`** with a real `NEO4J_AUTH`, copied from
   `/opt/aw-stack/.env` (`.env.example:10-12` says exactly this: copy it from
   there, do not derive a second credential). Also `NEO4J_URI=bolt://aw-stack-aw-neo4j-1:7687`,
   `NEO4J_DATABASE=neo4j`, `KNOWLEDGEABLE_TENANT_ENFORCEMENT=strict`,
   `DOCUMENT_STORAGE_DIR=/data/documents`. Leave
   `KNOWLEDGEABLE_SERVICE_SECRET` / `..._TENANT_ID` **both empty** —
   `.env.example:29-36` records that setting the secret without the tenant
   refuses to boot on purpose.

**Every deploy thereafter must MERGE `.env`, never overwrite it.** Copy
`repos/aw-stack/tools/host-env/merge_env.py` into this repo (it is small, and
the two repos are deliberately independent — do not reach across the
filesystem into aw-stack's checkout at deploy time). The keys the pipeline
owns come from repo secrets: `secrets.NEO4J_AUTH` at minimum. Overwriting
would silently erase whatever a human set by hand later — which is precisely
how `KNOWLEDGEABLE_SERVICE_SECRET` will arrive.

**Unverified, and the Coder must check it rather than assume:** whether
`/opt` on that host is writable by `ubuntu` at all, and whether the
`ubuntu`-owned checkout can `docker compose build` against
`/var/run/docker.sock`. AP-MT's runner demonstrably does both in its own
directory, so the answer is very likely yes for both, but I could not reach
the bare metal from this container to confirm it for a *new* path under
`/opt` — the workspace is nested inside `aw-host`, a container on
`aw-stack-net` (memory `aw-host-is-a-container-on-aw-stacks-own-network`),
with no view of the metal's daemon. If `/opt` refuses, put the checkout at
`/home/ubuntu/aw-knowledgeable` and say so in the workflow header.

---

## 2. What "release cut" means here

### Decision

**A release is an annotated git tag + a GitHub Release, cut from the tested
commit, and the deploy deploys that tag. Version is
`v<MAJOR>.<MINOR>.<github.run_number>`, where `MAJOR.MINOR` is read from
`pyproject.toml`'s `version` (today `0.1.0` → `v0.1.<run_number>`).**

The tag is the pipeline's rollback vocabulary: "roll back to `v0.1.41`" is a
sentence an operator can act on, `git reset --hard 7275504` is not. That is
the whole justification — this is an internal service, not a package anyone
installs, so a release earns its keep only as a named rollback target plus an
auto-generated changelog. Nothing beyond that.

Rules:

- **Cut after the tests pass, before the deploy runs.** A tag that names a
  red commit is worse than no tag.
- **Never commit back to `master`.** The third version component comes from
  `github.run_number`, which is monotonic and needs no file edit. Bumping
  `pyproject.toml` from CI would push a commit, which would trigger this same
  push-triggered workflow, which would bump again — an infinite deploy loop.
  This is the trap in the request and the reason the numbering is shaped this
  way. `pyproject.toml`'s own patch digit is ignored for release naming; say
  so in a comment next to it.
- **Release notes**: GitHub's auto-generated notes (`gh release create
  --generate-notes`), not a hand-maintained CHANGELOG. Nobody will maintain a
  CHANGELOG for this.
- **A refused (stale-guard) or cancelled run cuts nothing.**
- The tag is what the deploy job resets the host checkout to, so the running
  commit is always a released commit.

### Rejected

- **Release = just the deployed commit, no tag.** Cheapest, and it is what
  AP-MT does. Rejected because Frederico asked for a release cut in the same
  sentence as the deploy, and because `v0.1.N` is the only artifact that makes
  the rollback path statable.
- **Semantic versioning from Conventional Commits** (auto-derive major/minor
  from commit prefixes). Ceremony with no consumer: nothing depends on this
  service's version, so a "breaking change" has no one to break. The card says
  it directly — *"não inventar cerimônia que ninguém vai usar."*
- **Bumping `pyproject.toml` in CI and committing it.** The loop above. Also
  every deploy would carry a CI-authored commit, making `git log` on the
  service useless.
- **A GHCR image tagged per release.** `docker compose up -d --build` builds
  locally on the host; pushing to GHCR would add registry auth, pull time and
  a second artifact to keep consistent, for a rollback path the pinned local
  image (`ROLLBACK_TAG`, AP-MT `:236-248`) already covers. Revisit only if a
  second host ever needs the same image.

---

## 3. Tests in the pipeline

### Decision

**One suite, in one file, called by the deploy — `test.yml` becomes
`workflow_call`-able and loses its `push` trigger; `deploy.yml` gates on
`uses: ./.github/workflows/test.yml`. The test job stays on `ubuntu-latest`.**

The `uses:` form is AP-MT's (`deploy.yml:47-48`) and the reason is in its own
comment: *"Reused from test.yml rather than copied, so 'what CI runs' and
'what must pass before a deploy' can never drift apart."* That answers the
card's question 4 — the existing `test.yml` **is** the pipeline's test stage;
it is not duplicated and no second suite is invented.

**Divergence from AP-MT, deliberate:** AP-MT's `test.yml` keeps its own `push`
trigger *and* is called from `deploy.yml`, so a push runs the suite twice. Its
runner is free. aw-knowledgeable's test job runs on billable GitHub-hosted
minutes in a private repo, so `push` comes off `test.yml` and the deploy owns
the push path. `pull_request` and `workflow_dispatch` stay.

**Why the tests stay on `ubuntu-latest` — and why this is a considered
exception to the `aw-*` self-hosted policy** (KB
`aw-repos-github-actions-self-hosted-policy.md`):

- `test.yml:23-35` runs Neo4j as a `services:` container publishing `7687:7687`.
  On a self-hosted runner, job steps execute on the host, so a service
  container is only reachable through a **host** port — and the bare metal
  already has aw-stack's production `aw-neo4j` on `127.0.0.1:7687`. The
  collision is not hypothetical; it is the exact caveat
  `tekflox-aw-baremetal-four-org-runners-20260730.md` closes with, and with up
  to 5 runners on that host, remapping to some other fixed port just moves the
  collision between concurrent runs.
- Stronger reason: a test suite that runs on the bare metal is one
  misconfigured env var away from writing to the **production graph**. Off-host
  is a structural guarantee that it cannot.

So: `test` on `ubuntu-latest` with its throwaway Neo4j; `deploy` on
`[self-hosted, aw-baremetal]`. Keep `test.yml`'s existing header comment
explaining the GitHub-hosted choice and extend it with the port-collision
reason, since the original ("no self-hosted runner registered yet") stops
being true the moment §1a lands and would otherwise read as an invitation to
move it.

Leave the suite's content alone. 35 tests across
`test_tenant_isolation.py` (6), `test_service_identity.py` (5),
`test_api_contract.py` (24); `pip install -e ".[dev]"` on Python 3.11 matching
the image. Nothing about that needs changing for this card. Adding a `ruff`
step is in scope only if it is green on the current tree — `ruff` is already a
dev dependency (`pyproject.toml:25`); a red lint gate on day one blocks the
deploy this card is supposed to create.

### Rejected

- **Duplicating the pytest steps inside `deploy.yml`.** Two copies drift; the
  AP-MT comment exists because someone considered this.
- **Re-running the suite on the host after the container is up** (a smoke/E2E
  stage against production Neo4j). Tempting, and wrong: these tests
  `DETACH DELETE` their fixtures. The health gate (§1, step 7) is the
  post-deploy check, and it is enough — `backend/app/main.py:17-20` runs
  `graph.ensure_schema()` in `lifespan`, so the app cannot report healthy
  without a working Neo4j connection. The health gate is already a
  connectivity test.

---

## 4. The stale-re-run guard — non-negotiable, and it goes in first

The card's question 3 is already answered by working code:
`agents-platform-multitenant/.github/workflows/deploy.yml:115-174` is the
fix for `bug:ap-mt-deploy-yml-stale-rerun-rollback` (incident 2026-09-07: a
re-run of an old Deploy job reset the live checkout *backwards* from `c48846b`
to `7275504`, silently un-deploying two fixes). **Port that step, comment and
all, in the first version of this file** — not as a follow-up.

The three subtleties that make a hand-rolled version wrong, all recorded in
that comment block:

- `github.sha` on a **re-run** is the run's original trigger commit, not the
  branch tip. That is the entire bug.
- The invariant is *"the live checkout must never move backwards along its own
  history"*, compared against the **checkout's own `HEAD`** — not against
  `origin/master`'s tip. (aw-knowledgeable deploys on every push with no
  `paths:` filter, so tip-comparison would be *nearly* right here — but it
  still breaks any legitimate re-run and any manual `workflow_dispatch` of an
  older commit. Keep the HEAD comparison; it self-heals after a rollback,
  which tip-comparison does not.)
- `git merge-base --is-ancestor` has **three** exit codes: `0` ancestor, `1`
  not, `>1` git itself errored. Capture `$?` explicitly. A bare
  `if ! git merge-base …` folds "git blew up" into "proceed" — the one branch
  that must refuse.

Plus, from the same repo's board: `concurrency: group: deploy-aw-knowledgeable,
cancel-in-progress: false` (AP-MT `:38-42`) — *queue* deploys, never cancel one
midway, because a cancelled job can leave the checkout moved and the container
not restarted. Card `infra:ap-mt-deploy-yml-stale-rerun-race` is the open
sibling finding for the ordering half of this.

A refusal is **not** a failure: it must not exit non-zero, and the Telegram
page must say `REFUSED` with its own wording (AP-MT `:352-358` explains why —
dressing a refusal as a failure trains people to ignore the real alert).

---

## 5. Downtime, and what it costs

The card's question 5, answered rather than designed around: **accept the gap,
do not build for zero-downtime.** `docker compose up -d --build` stops the old
container and starts the new one — a gap of seconds to a couple of minutes
(the build is the slow part, and it happens before the swap). Nothing consumes
this service: no hostname is wired (§4 of the infra doc is a separate card,
and `docker-compose.yml:28-33` publishes loopback-only), no client exists,
`is_live=false` on M3/M6. The cost of a gap today is zero, measurable.

What that *does* buy, and should be said plainly rather than discovered: the
first person to depend on this service inherits an unannounced restart on
every push to master. When a hostname lands, revisit — and note that a
blue/green swap is harder here than usual, because the container name matters
(`aw-knowledgeable-infra.md` D2: compose-default names do not resolve from
other compose projects, so a fixed name is required), and two containers
cannot share one name.

Explicitly **not** in this design: load balancer, rolling update, healthcheck
in compose, `stop_grace_period` changes. The existing `stop_grace_period: 30s`
(`docker-compose.yml:25`) already covers uvicorn's drain.

---

## 6. What this makes harder later

1. **A fifth runner on a host at 95% disk.** Each runner keeps a `_work`
   directory and Docker build cache. The disk preflight in §1 protects the
   *deploy*; it does not stop the runner's own cache from growing. Memory
   `ap-mt-deploy-has-a-disk-preflight-that-blocks-every-commit` is what this
   looks like when it goes wrong: the preflight starts refusing every commit,
   and the CI is then the symptom rather than the cause.
2. **Deploy-on-push means `master` is production.** No staging, no approval.
   The stale-guard protects against going *backwards*, not against a bad
   commit going forwards — that is what the health gate and rollback are for,
   and a commit that is healthy-but-wrong deploys cleanly. If a second
   consumer ever appears, a protected branch or a `deploy` environment gate is
   the next step, and adding it later means changing the trigger this card
   just created.
3. **`aw-stack` is now an undeclared runtime dependency of a green CI.** If
   `aw-neo4j` is down or `NEO4J_AUTH` drifts between `/opt/aw-stack/.env` and
   `/opt/aw-knowledgeable/.env`, `lifespan`'s `ensure_schema()` fails, the
   health gate fails, and the deploy rolls back — correctly, but the run log
   will look like "our code broke" when the cause is one directory over. The
   workflow header should name this explicitly so the first person to hit it
   looks in the right place. Nothing keeps those two `.env` files in sync; a
   rotation of `NEO4J_AUTH` is a two-repo change (`secrets.NEO4J_AUTH` exists
   on `tekflox/aw-stack` *and* will now exist on `fredericowu/aw-knowledgeable`),
   the same two-repo drift KB `aw-workspace-release-webhook-token-stale-20260801.md`
   records for `AW_RELEASE_WEBHOOK_TOKEN`.
4. **Local build on the host forecloses a second host cheaply.** No image in a
   registry means a second deployment target has to rebuild. Deliberate (see
   §2 Rejected); revisit if `aw-knowledgeable` ever runs anywhere else.
5. **The version's third digit is a CI run counter, not a code fact.** You
   cannot infer "how much changed" from `v0.1.41` → `v0.1.57`, and re-running
   CI advances it. Accepted; the tag's job is identity, not semantics.

---

## 7. Risks for the Coders — the non-obvious failures

1. **The first deploy has no rollback target.** `Pin the running image` finds
   no container, so `steps.pin.outputs.pinned=false` and the rollback falls
   through to a rebuild of the same failing thing (AP-MT `:318-320`, and the
   2026-08-13 incident its comment describes: a rollback that re-runs the
   failing operation only works when it isn't needed). Expect the first run to
   either be green or need a human. Do **not** "fix" this by weakening the
   health gate.
2. **`ensure_schema()` in `lifespan` means the container exits on a Neo4j
   failure, it does not serve 500s.** The health gate's `docker inspect …
   State.Status` branch (AP-MT `:297-301`) is what catches it — a naive
   curl-only loop burns the full timeout instead of failing in 10s with logs.
   Keep that branch.
3. **`docker compose` project name.** Run from `/opt/aw-knowledgeable`, the
   project is `aw-knowledgeable` and the container is
   `aw-knowledgeable-app-1`. D2 (`aw-knowledgeable-infra.md:857+`) found that
   compose-default names **do not resolve across compose projects**, which is
   why `.env.example:6-9` uses `aw-stack-aw-neo4j-1`. Whatever name the
   workflow's `$CONTAINER` variable uses for `docker inspect` / `docker logs`
   must match what compose actually creates on that host — verify with
   `docker compose ps` on the first run rather than deriving it. One
   mis-derived name and the health gate's failure branch never fires (AP-MT's
   `PG_CONTAINER` comment, `:59-66`, is that exact bug: a wrong container name
   made two gates degrade silently).
4. **`.env` must exist before `docker compose` runs.** `docker-compose.yml:26-27`
   declares `env_file: - .env` with no default; a missing file fails the whole
   command, and it is gitignored so the checkout never carries one. The merge
   step has to run before `Apply`, and has to work when the file does not yet
   exist.
5. **`aw-stack-net` must already exist.** It is `external: true`
   (`docker-compose.yml:46-49`). If aw-stack has never been deployed on that
   host, `up` fails with a network error. That is the correct failure — do not
   add a `docker network create` fallback; a hand-created network is exactly
   the outage AP-MT's compose trailer records (the comment at
   `docker-compose.yml:10-15` of this repo already quotes it).
6. **The suite runs twice on the first PR after the trigger change** if
   `push` is removed from `test.yml` but `deploy.yml`'s trigger is misspelled
   — and conversely, forgetting `workflow_call` makes `uses:` fail with a
   confusing "workflow not found". Check both triggers in the same edit.
7. **Concurrency interaction.** `test.yml`'s existing `concurrency:
   test-${{ github.ref }}, cancel-in-progress: true` (`test.yml:15-17`) keeps
   applying when the workflow is *called*, and `github.ref` is the same for two
   consecutive pushes to master. The desired behaviour: a superseded push
   neither deploys nor pages anyone. Make sure the cancellation path leaves a
   **cancelled** run, not a **failed** one, because `Page a human` fires on
   `failure()`. Worth an explicit test: push twice within a minute and check
   that exactly one deploy lands and no Telegram alert fires.
8. **Do not verify by trusting a green run.** Skill
   `aw-autoskill-verify-deploy-in-container` is the rule for this class of
   service: a green CI and a 200 on `/api/health` do not prove the new code is
   live. Confirm the running container's own files carry the commit
   (`docker exec … git`-free check: compare the image's build time / a file
   the commit touched) before reporting done.

---

## 8. Handoff — order of operations

1. Bootstrap the host (§1a: runner → checkout → `.env`). Confirm the runner is
   `online` via `gh api` before step 2. Nothing else can be tested until this
   is true.
2. `test.yml`: add `workflow_call`, remove `push`, extend the header comment
   with the port-collision reason (§3).
3. `deploy.yml`: the full shape from §1, with §4's guard present in the first
   commit and §2's release cut between `test` and `deploy`.
4. Add `merge_env.py` (copied from aw-stack) + `secrets.NEO4J_AUTH` on
   `fredericowu/aw-knowledgeable`.
5. Push. Watch the run with `gh run watch <id> --exit-status` (skill
   `aw-autoskill-gh-run-watch` — do not hand-roll a poll loop).
6. Verify live per §7.8, and verify the release: `gh release list` shows
   `v0.1.<run_number>` and `git -C /opt/aw-knowledgeable rev-parse HEAD`
   equals that tag.
