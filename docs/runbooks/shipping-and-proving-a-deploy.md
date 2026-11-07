# How to ship a change here and prove it is actually live

**Verified on:** 2026-11-07 (distilled from four `aw-autoskill-*` skills, see
[Provenance](#provenance)).
**Scope:** every service reachable from this workspace — a bare-metal-hosted
Docker service (`agents-platform-multitenant`, `aw-backend`, `aw-console`),
`aw-workspace` core, an installed Tier-1/Tier-2 app, a frontend SPA. One
question answered in one place: *which workflow actually builds, and how do I
know the new code is the code that is running?*

**The premise.** A green deploy run is a **presence check**, not proof. It
proves a build succeeded and a container was recreated. It does not prove the
code inside matches your diff. This workspace has bitten itself on that often
enough to have a separate incident memory per occurrence.

---

## 1. Pick the right workflow — the one that builds

`aw-stack` (`tekflox/aw-stack`) is the compose-owner for `aw-backend`,
`aw-console` and other bare-metal-hosted services. Its "Deploy aw-stack"
workflow (`workflow_dispatch -f services=…`) **recreates containers from
whatever image tag is already on GHCR**. It does not build.

So the order is:

1. Push the code to the service's own repo (e.g. `fredericowu/aw-backend`).
2. Run **that repo's own** "Deploy `<service>`" workflow — it builds, pushes
   the new `:latest` to GHCR, and per that repo's pipeline may redeploy on
   its own:
   ```bash
   gh workflow run "Deploy aw-backend" -R fredericowu/aw-backend
   gh run watch <run_id> -R fredericowu/aw-backend --exit-status
   ```
3. Only then, **if `aw-stack`'s own compose/env config also changed**, run
   "Deploy aw-stack" to pick up the new tag and recreate the container.
4. Verify per §3 or §4.

If you ran only step 3 and the deploy "succeeded" but the fix is not visible,
this is the first thing to suspect: you redeployed the old image. One session
did exactly that, watched it go green, and found the fix absent — *"rodei o
deploy do aw-stack cedo demais, antes da imagem ser reconstruída"*. Check
which repo owns the Dockerfile for the service you changed before picking a
workflow.

**Cross-repo dependencies deploy in dependency order.** If one side must be
idempotent before the other retries against it, deploy and verify the
dependency **first** — otherwise there is a live window where retries
double-execute.

## 2. Waiting for the run

See [`waiting-on-long-running-work.md`](./waiting-on-long-running-work.md) —
`gh run watch --exit-status`, the 600s auto-background cap, and the
`run_monitor_sync` / `run_monitor_async` choice.

## 3. Proving it live — a server-side change

Grep the **running container** for a string that can only exist if your fix
landed. Not a version number: a stale image tag can lag or lie.

```bash
ssh -i /opt/aw-workspace/.ssh/id_ed25519 root@65.109.66.88 \
  "docker ps --format '{{.Names}}' | grep -iE 'agents-platform|aw-backend'"

ssh -i /opt/aw-workspace/.ssh/id_ed25519 root@65.109.66.88 \
  "docker exec agents-platform-multitenant grep -c 'after {attempts} attempts' /app/app/core/models/runner.py"
```

If you don't know the file's path inside the image, resolve it in the same
exec rather than guessing:

```bash
docker exec agents-platform-multitenant sh -c \
  "grep -rc 'YOUR_MARKER' \$(find / -name runner.py -path '*core/models*' 2>/dev/null | head -1)"
```

**A DB schema/migration change needs the live database, not CI's.** "The
migration ran in CI" is a different claim:

```bash
docker exec -w /app aw-backend python3 -c "
from sqlalchemy import text
from src.api.pg_db import get_engine
with get_engine().connect() as c:
    cols = [r[0] for r in c.execute(text(\"select column_name from information_schema.columns where table_name='pinned_faces'\"))]
print('workspace_slug present:', 'workspace_slug' in cols)
"
```

### When SSH / `remote_host_exec_run` isn't usable

Fall back to the `bare-metal-shell.yml` GitHub Actions workflow
(`workflow_dispatch`, self-hosted `aw-baremetal` runner, real host shell with
`gh`/`docker`/`aws`):

```bash
gh workflow run "bare-metal-shell.yml" -R fredericowu/agentic-workspace \
  -f command='docker exec agents-platform-multitenant grep -c "YOUR_MARKER" /app/app/core/models/runner.py'
gh run list -R fredericowu/agentic-workspace --workflow=bare-metal-shell.yml -L1 --json databaseId -q '.[0].databaseId'
gh run view <run_id> -R fredericowu/agentic-workspace --log
```

Three distinct triggers for reaching for it, all confirmed live:

1. **The BYOD/`aw-remote-host` link is degraded or down.**
2. **`remote_host_exec_run` connects but lands in a scoped container** with no
   `gh`/`docker`/`aws` — not a real host shell. If a command that should see
   `docker ps` output comes back empty or tool-not-found, suspect this before
   concluding the deploy is broken.
3. **The link is flapping**, neither cleanly up nor down. One session
   dispatched the workflow 24+ times over ~30 minutes while the direct link
   recovered and died repeatedly. Do not commit to break-glass for the rest
   of the session — the workflow_dispatch round-trip is much slower per call,
   so re-check whether the direct link is back before each dispatch.

One call of `gh workflow view bare-metal-shell.yml --yaml` before the first
dispatch of a session confirms the input name is still `command=` — cheap
insurance on a workflow you touch rarely.

## 4. Proving it live — a frontend or static artifact

Faster and ungated: no SSH, so no Telegram approval wait.

Vite content-hashes its output filenames, so an identical hashed filename
live and in a fresh local build **is** proof the same bytes shipped.

1. `curl` the deployed `index.html`, extract the hashed asset path
   (`grep -oE 'assets/index-[a-zA-Z0-9]+\.js'`; Next.js:
   `_next/static/chunks/*.js`).
2. `curl` that asset, hash it (`md5sum`).
3. `git checkout` the commit that should be live, `cd frontend && npm run
   build`, hash `dist/assets/index-*.js`.
4. Identical filename hash (or identical md5) = the fix is live. Different =
   an older commit is deployed, or the build pipeline diverged.
5. Secondary corroboration: grep the live bundle for a minified constant your
   fix introduced, and cross-check `gh run view <run_id> --json headSha`
   against the fix commit.

This only applies when the changed artifact is **public and static**. A
server-side logic change has no public artifact to grep — use §3.

## 5. Proving it live — `aw-workspace` core

Core is **not** a container you SSH into on the bare-metal host. It also
usually touches out-of-repo mirrors.

**(a) Know which files are mirrors before you start.** A capability / grant /
manifest addition in `src/apps/capabilities.py` or `src/apps/manifest.py` is
duplicated in two other repos, each with its own "is this manifest valid"
tripwire test:

| Mirror | What it is |
|---|---|
| `repos/aw-backend/src/api/app_capabilities.py` | a literal Python mirror of the capability catalog |
| `repos/aw-marketplace/schemas/aw-app.schema.json` | the JSON Schema enum validating every marketplace manifest |

Grep the capability string you are adding across all three repos before
calling the change done. **A green `aw-workspace` suite does not catch a
stale mirror** — those tripwire tests only fail in their own repos' CI, which
you will not see unless you go look.

**(b) Push policy differs per repo, and `aw-workspace` changed.** As of
2026-11-07 `fredericowu/aw-workspace`'s `master` carries an **active
PR-required ruleset with zero bypass actors** — a direct push is rejected
outright; work in a worktree and open a PR (see `AGENTS.md`). `aw-backend`
still takes a direct `git push origin master`. `tekflox/aw-marketplace`'s
`master` is branch-protected behind a `validate-apps-json` check:

```bash
git branch -f feat/<short-name> HEAD
git push -u origin feat/<short-name>
git reset --hard origin/master      # keep local master clean for the next session
gh pr create --repo tekflox/aw-marketplace --base master --head feat/<short-name> --title "…" --body "…"
gh run watch <run_id> --repo tekflox/aw-marketplace --exit-status
gh pr merge <pr_number> --repo tekflox/aw-marketplace --merge --delete-branch
git checkout master && git reset --hard origin/master
```

A rejected push on a protected branch is expected behaviour, not a
permissions bug to escalate.

**(c) Restart, and confirm the right commit came up.**

```bash
aw-workspace-cli restart core --wait          # blocks until the new process reports git_head
aw-workspace-cli status 2>&1 | grep git_head  # diff against the commit you pushed
```

Never restart core from inside an agent run that must survive it — it kills
that run.

**(d) Expect a degraded MCP surface for ~1-3 minutes** after a core restart
while every app's MCP client reconnects. That is normal. Don't `sleep 90`
inline and don't loop-poll in-turn; arm a wait (§2) and come back.

**(e) "No silent degradation" is not proof your change is live.** A clean
`aw-workspace-cli doctor` says the workspace restarted healthily and nothing
about whether your field or behaviour is being served. Check for a key that
can only exist post-fix:

```bash
aw-workspace-cli doctor --json | python3 -c "
import json, sys
print(json.load(sys.stdin).get('host_power', {}).get('unmet_optional'))
"
```

Present (even as an empty list) = the new code path ran. Missing = the
restart did not pick up your commit; go back to (c).

**An installed app is different again.** `aw-workspace-cli marketplace
install <app> --update` already calls the live process's own
`POST /api/apps/{id}/update`, so an app usually does **not** need a restart
the way core does. Confirm with a real functional call to the app's own
endpoint — `aw-workspace-cli apps <slug>` reporting the new version only
reads what is on disk. For a committed `ui/dist`, see
[`../standards/pipeline-testing.md`](../standards/pipeline-testing.md) §6d:
the standard release workflow never rebuilds it.

## 6. A verdict that says "it failed" is also a presence check

The discipline cuts both ways. A QA run against `aw-vpn-hub` reported 0/8 and
asked a human whether its own methodology was even valid. The operator
reproduced the target behaviour with a known-good procedure against the same
deployed hub, passed immediately, and traced the failure to a bug in the QA's
own test — its probe packets looped back into the tunnel because it had not
fixed the tunnel-endpoint route. **Before escalating a failing verification
as "the deployed thing is broken", reproduce the failure yourself against the
same target.** A harness can be the thing that is wrong.

And if you are QA verifying someone else's "it's deployed" claim, redo the
check from scratch rather than trusting their hash or their grep.

---

## Provenance

Consolidated on 2026-11-07 from the `aw-autoskill` tenant store, card
`4115bf3b-9510-81fb-8509-de540f23bca0` (target
`aw-autoskill-registry-sync-triage`), WP2b-i:

- `aw-autoskill-aw-stack-selective-deploy` → §1
- `aw-autoskill-verify-deploy-in-container` → §3, §6
- `aw-autoskill-verify-frontend-deploy-via-bundle-md5` → §4
- `aw-autoskill-core-deploy-verify-and-mirror-sync` → §5

§5(b) **corrects** the source skill, which recorded `aw-workspace` core as
taking a direct push to `master`. That has been false since the PR-required
ruleset went active on this repo.
