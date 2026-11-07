# How to work on the bare-metal host without measuring the wrong thing

**Verified on:** 2026-11-07 (distilled from three `aw-autoskill-*` skills, see
[Provenance](#provenance)).
**Scope:** the `aw.tekflox.com` bare metal and the nested container runtimes
stacked on it. Three failure modes that share one cause: **the thing you
measured is not the thing you meant to measure** — a path that resolves to a
different mount, a `du` that double-counts, a cleanup that fixed one of four
layers.

Companion runbooks:
[`aw-remote-host-image-rebuild.md`](./aw-remote-host-image-rebuild.md),
[`podman-5-upgrade-aw-remote-host.md`](./podman-5-upgrade-aw-remote-host.md).

---

## 1. Which `/opt/aw-workspace` am I looking at?

`/opt/aw-workspace/...` is a **different filesystem** depending on which
container resolves it.

| Resolved from | `/opt/aw-workspace` is |
|---|---|
| the nested podman containers (`aw-remote-host-workspace`, an agent sandbox) | the real workspace tree |
| **inside the outer `aw-remote-host` docker container** | not the workspace tree. The host-mounted tree there is `/home/aw-remote-host/aw-workspace/...` |

This permanently broke a backup doctor dead-man's-switch: the script runs
inside the **outer** container and wrote its status file to
`/opt/aw-workspace/.aw-workspace/data/backup-status/...`, which landed in an
orphan location. A script running there has to use the
`/home/aw-remote-host/aw-workspace/...` form.

**The re-verification trap this causes.** A second, independent session
checking the fix found the file missing from its own sandbox and from
`aw-remote-host-workspace`, which read as the bug being back. It was not — it
was mount propagation, confirmed only by checking the outer container
directly.

**The rule.** Before trusting "the file isn't there" for anything written by
a script in the outer container, verify via `remote_host_exec_run` against
`aw-remote-host` itself (`docker exec aw-remote-host ...` from that tool).
Don't trust `cat`/`ls` from your own sandbox or from the nested workspace
container, and allow for propagation lag before calling a missing file a bug.

This is **not** the same thing as moving a file into a hosted tenant's nested
workspace (`podman cp` / `remote-hosts push`) — that is a different host
relationship. This is path-identity confusion across the outer and nested
containers of the *same* linked BYOD host.

## 2. Disk pressure has four independent layers, not one

The host hit 90%+ disk from container image/build-cache bloat **four separate
times**, because each incident found and cron'd exactly one accumulation
point and left the others to refill the disk days later with the identical
symptom — same volume name, same crisis.

Before closing a disk-pressure incident, walk all four:

| # | Layer | Check / clean |
|---|---|---|
| 1 | host's own docker **builder cache** | `docker builder du` / `docker builder prune -f --filter until=72h` |
| 2 | host's own **dangling images** (one per app deploy) | `docker image prune -af --filter until=72h` |
| 3 | `aw-remote-host`'s **internal nested podman** — a separate store, one image per app update it ever relayed | `aw-workspace-cli ssh root@aw.tekflox.com -- "docker exec aw-remote-host podman system df"`, then `podman system prune -af` |
| 4 | each `aw-hosted-<tenant>` container's **own internal podman** — same nested pattern, one per hosted tenant | `docker exec aw-hosted-<tenant> podman system df` |

The volume behind the second incident
(`agentic-workspace_aw-remote-host-state`) is layer 3; it filling mid-E2E-run
killed a CI runner, with no relation to the first incident's root cause in
layers 1/2.

**The `du` gotcha.** A plain `du -sh` across these layers double-counts
FUSE-overlayfs bind mounts and produces a false "64GB unaccounted for" gap:

```bash
du -sh --one-file-system /var/lib/docker/* 2>/dev/null | sort -rh | head -20
```

**The rule.** Don't cron a fix for whichever layer triggered *this* alert and
close the incident. Confirm each of the four has either headroom or a cron,
and only then consider the class of problem closed.

## 3. Deduping iptables rules a crash-looping container leaked

A container that crash-loops with `restart: always` and sets up its own
iptables rules on start (VPN, firewall sidecars, port-forward helpers
commonly do) leaks **one full copy of those rules per restart** — Docker
never runs the container's own cleanup path on a crash, only on a graceful
stop. `wg-vpn` did this across 619 restarts, accumulating 3714 duplicate
rules (`filter FORWARD` 1227 → 9 unique; `nat POSTROUTING` 621 → 12).

### Why `iptables-restore` is the wrong tool

The obvious fix — dump, `sort -u`, restore — is dangerous on a host where
**`dockerd` is itself a concurrent writer** to the same tables (every
container start/stop touches `nat`/`filter` through Docker's iptables
integration). A restore replaces the whole table atomically from your
snapshot; anything dockerd added or removed in the window between snapshot
and restore is silently reverted, and a live NAT rule for a *different,
healthy* container can disappear mid-restore.

> Usei `iptables -D` em laço e **não** `iptables-restore`. O restore troca a
> tabela inteira, e o docker escreve nessas tabelas o tempo todo.

### The safe procedure

1. **Stop the crash-looping container first, with restart disabled**, so it
   stops re-adding rules while you clean up:
   ```bash
   docker update --restart=no <container>
   docker stop <container>
   ```
2. **Back up the full table state** (`/tmp` is fine — single-host disposable
   scratch, nothing that needs to survive a restart):
   ```bash
   iptables-save > /tmp/iptables-backup-$(date +%s).rules
   ```
3. **Dedupe in place with targeted deletes**, never a table replace. For each
   table/chain, count distinct rule specs and delete all but one copy:
   ```bash
   iptables -t <table> -S <chain> | sort | uniq -c | sort -rn | \
     awk '$1 > 1 {for (i=0;i<$1-1;i++) print}'
   # then per duplicate spec: iptables -t <table> -D <chain> <rule-without-count>
   # repeated (count - 1) times
   ```
   `iptables -D` matches by rule **content**, not line number, so repeated
   calls against the same spec are safe in a plain loop and each removes
   exactly one match.
4. Re-add the container's own rules by starting it once cleanly (or restore
   `--restart=always` and let its entrypoint re-run), then verify counts
   dropped to the expected unique set per chain.

### When to suspect it

If a container's restart count is high
(`docker inspect -f '{{.RestartCount}}' <container>`) and you are debugging
seemingly unrelated network weirdness on the same host, check for this
first — leaked duplicates silently change matching order for unrelated
traffic on a busy `FORWARD` chain.

---

## Provenance

Consolidated on 2026-11-07 from the `aw-autoskill` tenant store, card
`4115bf3b-9510-81fb-8509-de540f23bca0` (target
`aw-autoskill-registry-sync-triage`), WP2b-i:

- `aw-autoskill-remote-host-outer-vs-nested-path-identity` → §1
- `aw-autoskill-bare-metal-docker-layers` → §2
- `aw-autoskill-iptables-dedupe-crash-loop` → §3
