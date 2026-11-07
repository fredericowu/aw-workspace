# How to stand up a new aw-stack service, and decommission its predecessor

**Verified on:** 2026-11-07 (distilled from two `aw-autoskill-*` skills, see
[Provenance](#provenance)).
**Scope:** giving something its own repo, its own service in the aw-stack
`docker-compose.yml`, its own CI/CD and its own public hostname — and then
making the thing it replaced actually stop serving traffic.

**Historical note, read first.** Both source skills were written for the
recurring "port `custom_apps/<x>` out of the monolith" card shape. The
`agentic-workspace` monolith is now deactivated, so that specific intake is
gone. The ordering below survives it: steps 2-8 are how you add *any* new
service to the stack, and §2 is how you decommission *any* predecessor that
a supervisor will otherwise resurrect.

---

## 1. The order

1. **Pull source and data from wherever it lives today** via
   `remote_host_read_file` / `remote_host_download_file`, rather than
   re-deriving access from scratch.
2. **Scaffold the new repo locally** (e.g. under `.tmp/<name>/`) before
   pushing anything.
3. **Push to GitHub as the human's own identity**, not a bot token. Shell
   state does not persist between tool calls, so **every** `Bash` call needs
   both:
   ```bash
   export HOME=/opt/aw-workspace
   export GH_CONFIG_DIR=/opt/aw-workspace/.aw-workspace/data/git/config-gh
   ```
   Forgetting either on any one call silently falls back to the wrong git
   identity or a dead token. Do not assume it carried over.
4. **Register a self-hosted Actions runner on bare metal** for the new repo
   (`config.sh`, then `svc.sh install` under the runner's own directory) if
   the job needs to reach internal-only hosts. GitHub-hosted runners cannot.
5. **Splice the service into the aw-stack `docker-compose.yml`**, rather than
   running it as a standalone container outside the stack's network.
6. **Migrate data with checksum verification** — sha256 both sides. A `cp`
   that exits 0 is not evidence the bytes arrived.
7. **Confirm container-to-container reachability before touching Caddy** —
   curl the new service from inside the aw-stack network. Routing public
   traffic at a service that is not reachable yet just moves the failure to a
   harder-to-diagnose layer.
8. **Repoint the public hostname through the backend's own repoint routine**,
   not a hand-edited Caddyfile — a hand edit gets silently reverted by, or
   conflicts with, the generator.

Then ship and verify per
[`shipping-and-proving-a-deploy.md`](./shipping-and-proving-a-deploy.md) —
note especially that "Deploy aw-stack" does not build images.

This order was independently re-walked by a second session doing QA on the
same work, which is why it is written as an order and not a checklist.

## 2. Decommissioning the predecessor — the step that gets skipped

A new service going live does **not** stop the old one. The public hostname
can keep resolving to the predecessor and serve frozen data for weeks while
the new container, reached any other way, reports correct data the whole
time.

Recorded instance: `ux-proto` moved to a Tier-2 app on 2026-08-15; the old
`aw-custom-ux-proto` container was never stopped and Caddy kept routing the
public hostname to it. The public site showed data frozen since 2026-07-28,
discovered only because it blocked an unrelated verification. A sweep of
three more suspect apps found the identical bug in `aw-browser` — 2 real hits
out of 4 checked, not a one-off.

### When to check

- Any time a migrated app's public behaviour looks stale or cached, or
  contradicts what its own API/MCP surface reports.
- **Proactively, right after any cutover.** Do not assume the process
  decommissioned the predecessor.
- Auditing a batch: any slug with **both** a new-style and an old-style
  container running is suspect.

### How to confirm

1. List running containers on the shared host and look for the predecessor
   alongside the expected new container.
2. Check what Caddy's **live** config routes that hostname to, and compare
   against the new container's actual name/port.
3. Compare data timestamps between the public domain and the new service's
   own API for the same resource. **A freeze date matching the cutover date
   (or earlier) is the tell.**

### The fix has to be durable

`docker stop` / `rm` alone is not enough when a supervisor owns the
container: the monolith's `awserv` watchdog resurrects anything still flagged
`auto_start: true` in `aw.json` — on every boot, not only after a crash. The
removal only becomes durable once that entry is also purged and a
reload/restart applied. Verify with a boot cycle, not an immediate
`docker ps`.

**Expect a blast radius.** Restarting a supervisor to apply this recreates
*every* service it auto-starts on that host, not just your target — one
session saw unrelated Crispal containers bounce. Check what else depends on
those staying up first.

### Close it out in a real browser

The follow-up QA pass reproduced the end-to-end flow in an actual browser and
confirmed real content rendered — not an empty shell returning 200. "The
container is gone and the Caddy config looks right" is not a verdict.

---

## Provenance

Consolidated on 2026-11-07 from the `aw-autoskill` tenant store, card
`4115bf3b-9510-81fb-8509-de540f23bca0` (target
`aw-autoskill-registry-sync-triage`), WP2b-i:

- `aw-autoskill-port-monolith-app-to-aw-stack-service` → §1
- `aw-autoskill-tier2-port-zombie-legacy-container` → §2
