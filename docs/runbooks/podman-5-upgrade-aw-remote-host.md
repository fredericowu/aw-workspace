# Runbook — podman 4.3.1 → 5.4.2 on `aw-remote-host` (trixie rebase)

**Status:** written 2026-09-08, **NOT YET EXECUTED**.
**Authorized by:** Frederico, Telegram 2026-09-08 (*"sim, vamos que vamos, para não, segue até o fim"*).
**Card:** `3d25bf3b-9510-8156-a87a-f337960a2b36` — Architect rounds 1–3 live in its comments; this
document is round 4 and supersedes their Phase-B section only.
**Companion:** [`aw-remote-host-image-rebuild.md`](./aw-remote-host-image-rebuild.md) — **that
runbook is now fully executed** (see §1 below); do not re-run it.

Every number below was measured live on **2026-09-08 ~15:30Z**. Re-measure before acting.

---

## 0. The single most important operational fact — unchanged

> **The agent executing §5 CANNOT be running inside `aw-remote-host`.**

Every agent session on this workspace is a nested podman container inside `aw-remote-host`.
Recreating it kills the executing session mid-runbook. Execute against the bare metal via
`remote_host_exec_run` with **`host_id=2d5d56ef224359c0`** (`bare-metal-privileged`,
`elevated: true`), or a human SSH session.

Two properties of that exec path that shape every step:

- **It runs your command twice.** Every step must be idempotent or explicitly guarded.
- **Sockets drop at ~60 s.** Anything longer must be detached with output to a file on the bare
  metal.

---

## 1. What is already done — do not redo it

| Item | State | Evidence |
|---|---|---|
| Dockerfile moved into `repos/aw-remote-host` (old §3.1) | **DONE** 2026-09-04 | `Dockerfile` header comment |
| `iproute2 wireguard-tools openvpn` baked (old §3.2) | **DONE** | live: `ip`→`/usr/sbin/ip`, `wg`→`/usr/bin/wg`, `wg-quick`→`/usr/bin/wg-quick`, `openvpn`→`/usr/sbin/openvpn` |
| tailscale lands in the image (old §3.3) | **DONE** | live: `tailscale`→`/usr/bin/tailscale` |
| graphroot repointed off the ephemeral layer (old §5) | **DONE** | `podman info` → `/home/aw-remote-host/.local/share/containers/storage`; `/etc/containers/storage.conf` carries graphroot + `runroot = /run/containers/storage` |
| Container recreate rehearsed against production | **DONE** 2026-09-08 13:07 | aw-stack cutover; 25 nested containers came back |
| podman version floor + `verify.sh` graphroot assertion | **DONE** | `d99cab1`, on `origin/main`, in the running v0.1.86 image |
| CI publishes + digest-pins the image | **DONE** | `a535ef2`; `AW_REMOTE_HOST_IMAGE=ghcr.io/tekflox/aw-remote-host@sha256:4360436807b6…` in `/opt/aw-stack/.env` |
| `Dockerfile`/`entrypoint.sh`/`healthcheck.sh` trigger `release.yml` | **DONE** | `a535ef2` added them to the `paths:` list |

**`aw-remote-host-image-rebuild.md` still says "NOT YET EXECUTED" in its status line. It is wrong
and it is dangerous** — it describes a destructive procedure that has already run, by a different
delivery path (CI image + digest pin, not a bare-metal `docker compose build`). Correct that line
as part of this work.

**Nothing in that runbook remains to be executed as a separate step.** In particular, do **not**
schedule a standalone "add the VPN packages to the image" phase; those packages are on the
running host's PATH today.

---

## 2. What is actually left, and why it is now small

podman is **not in the image**. `bootstrap/podman/install.sh:124` runs
`ensure_cmd podman install_podman_linux` → `apt-get install -y podman`, against whatever the base
image's distro offers. So the podman version is a property of `Dockerfile`'s `FROM` line, not of
the host.

Measured package availability (queried directly, re-confirmed today):

| Suite | podman | netavark | aardvark-dns |
|---|---|---|---|
| **bookworm** (today's base) | `4.3.1+ds1-8+deb12u1+b3` | `1.4.0-3` | `1.4.0-3` |
| bookworm-backports | **no `podman` package at all** | — | — |
| Kubic `…stable/Debian_12` | `100:3.4.2-5` (**older**) | — | — |
| **trixie** | **`5.4.2+ds1-2`** ✓ | `1.14.0-2` | `1.14.0-3` |
| sid/forky | `5.8.6+ds1-2` | `1.17.2-2` | `1.17.1-3` |

Pinning trixie's `.deb` into bookworm needs `libc6 ≥ 2.38` (bookworm has 2.36), `libgpgme11t64`
and `libsubid5` — a glibc bump plus the whole 64-bit-`time_t` transition, i.e. a distro upgrade
in place. Rejected in round 1; re-confirmed.

**→ The change is `FROM debian:bookworm-slim` → `FROM debian:trixie-slim`, plus the tailscale
apt suite next to it.** Everything else — the version floor, the conditional assertion, the
`upgrade_podman_to_floor` helper, the graphroot assertion — already shipped in `d99cab1` and will
simply start having teeth once the candidate reaches 5.

---

## 3. Baseline, measured 2026-09-08 ~15:30Z

Inside `aw-remote-host` (`host_id=11e8bd4157845a24`, hostname `ffbd890b6e62`, `cli_version v0.1.86`):

```
Debian GNU/Linux 12 (bookworm)          podman 4.3.1+ds1-8+deb12u1+b3
NetworkBackend = netavark               netavark 1.4.0-3 / aardvark-dns 1.4.0-3
GraphRoot  = /home/aw-remote-host/.local/share/containers/storage
RunRoot    = /run/containers/storage
containers = 25 running / 25 total      images = 27
networks   = aw-remote-host (10.89.0.0/24), podman
iptables inside: filter 36 / nat 48 / mangle 7, iptables-save 103 lines
ip rule: tailscale set only (5210/5230/5250/5270) — the orphaned 5399 is gone
```

Bare metal (`host_id=2d5d56ef224359c0`):

```
Docker 29.6.0   podman 5.7.0 (the metal's own — proves the kernel is fine, says nothing about the store)
df -h /  →  436G total, 62G avail (86% used)
container: image pinned by digest, restart=always, privileged=true, net=aw-stack-net,
           compose project aw-stack, /opt/aw-stack/docker-compose.yml, ONE mount:
           agentic-workspace_aw-remote-host-state -> /home/aw-remote-host
state.json: provisioned=true, workers=10, vpn={}, last_bootstrap_version="v0.1.86"
```

### 3.1 Storage — the number that decides the rollback design

`du` of the graphroot, from the bare metal (authoritative — the inside view double-counts active
overlay mounts and reports 80 G):

| Path | Size | Re-pullable? |
|---|---|---|
| `overlay/` | **40 G** | **yes** — layer content |
| `overlay-layers/` | 68 M | no — layer metadata |
| `overlay-containers/` | 9.5 M | no — the 25 container definitions |
| `libpod/` | 2.6 M | no — the BoltDB state |
| `overlay-images/` | 848 K | no |
| `volumes/` | 76 K | no |

**Everything whose loss would matter is ~81 MB.** That is the whole backup, and it is what makes
this a reversible operation instead of a one-way door.

### 3.2 The hazard the V0 fix created

Before the graphroot repoint, a container recreate wiped podman's store and podman came up on a
clean slate. **It no longer does.** The store now survives on the persistent volume, so podman
5.4.2 will open a 40 GB store written by 4.3.1 — an in-place major upgrade, which is the one
thing the earlier plan could not have anticipated because the fix had not landed yet.

The prerequisite and the risk are the same change. Plan for inheritance, not for a clean slate.

### 3.3 Still on the ephemeral layer, and correctly so

`stat -c '%m'` → `/` for `/etc/containers/networks`, `/etc/containers/storage.conf`,
`/etc/cni/net.d`, `/etc/wireguard`, `/run/containers/storage`. All are rewritten by the bootstrap
modules on every recreate (`aw-remote-host.json` is dated 13:07 today, matching the cutover).
`/etc/cni/net.d/87-podman-bridge.conflist` is an unused 2022 leftover — the backend is netavark,
so **podman 5's headline breaking change (CNI removal) does not apply to this host.**

---

## 4. Abort criteria — stop and escalate, do not improvise

Abort if **any** of these is true at the moment you check it:

- `docker volume inspect agentic-workspace_aw-remote-host-state` fails or the volume is missing.
- The §5.1 metadata backup cannot be verified (`tar -tzf` empty, or sha256 mismatch).
- `df -h /` shows **< 45 GB** free.
- The §5.0 image gate fails: the new image does not report trixie, does not carry all five of
  `ip wg wg-quick openvpn tailscale`, or `apt-cache policy podman` inside it does not offer a
  candidate with major ≥ 5.
- Frederico is not reachable to press the bootstrap action (§5.2 step 8 — **this cannot be
  automated**, measured, see §7).
- A VPN is up (`state.json`'s `vpn` key is non-empty). It is `{}` today; if that changes, take the
  tunnel down through the dialer's own path first.
- You are executing from inside `aw-remote-host` (§0).

There is no step here where "try it and see" is correct.

---

## 5. Execution

### 5.0 — Phase B0: pre-flight and the image gate (no downtime)

1. **Fetch before you read.** `/opt/aw-workspace/repos/aw-remote-host` is a *shared* working tree
   and was measured **2 commits behind `origin/main`** (missing `d99cab1`, `a535ef2`). Other agent
   sessions share it and its index. Read via `git show origin/main:<path>`; never `git add -A`.

2. **Write the inventory to the surviving volume**, from the bare metal, into
   `/var/lib/docker/volumes/agentic-workspace_aw-remote-host-state/_data/.podman5-2026-09-08/`
   (= `/home/aw-remote-host/.podman5-2026-09-08/` from inside):
   `podman ps -a --format json`, `podman images --digests --format json`,
   `podman volume ls --format json`, `podman network ls --format json`, `podman info`,
   `iptables-save` (**both netns** — inside and on the metal), `ip rule show`,
   and a copy of `state.json`.

3. **The code change** — `tekflox/aw-remote-host`, one commit on `main`:
   - `Dockerfile:34` `FROM debian:bookworm-slim` → `FROM debian:trixie-slim`
   - `Dockerfile:81` `…/debian/bookworm.noarmor.gpg` → `trixie.noarmor.gpg`
     (verified HTTP 200)
   - `Dockerfile:83` `…/stable/debian bookworm main` → `trixie main`
     (verified HTTP 200)
   - Update the surrounding comments that assert bookworm facts, and `bootstrap/manifest.json`'s
     podman version string if it names 4.x.
   - **Do not touch `bootstrap/lib/podman_version.sh`.** `d99cab1` already does the whole job;
     its `upgradable` branch becomes reachable on trixie, which is the designed behaviour.

4. Push. `release.yml` fires (Dockerfile is in `paths:` since `a535ef2`), the `image` job builds on
   `[self-hosted, aw-baremetal]` and publishes `v0.1.87` plus a digest.

5. **GATE — the last completely safe moment. Nothing has been destroyed yet.**

   ```
   docker run --rm --entrypoint sh <new-digest> -c '
     head -2 /etc/os-release
     command -v ip wg wg-quick openvpn tailscale
     apt-get update -qq && apt-cache policy podman | head -3'
   ```

   Must print trixie, **all five** binaries, and `Candidate: 5.4.2…`. If any of that is wrong,
   **stop here.**

### 5.1 — Phase B1: the window

Execute from the bare metal only (§0). All 5 warm agent sessions die and do not return.

1. Announce. Accept that every agent session, including the executing one if it is nested, ends.
2. `docker stop aw-remote-host` — its 25 nested children stop with it.
3. **Metadata backup, with podman stopped** (an online copy would be inconsistent). From
   `/var/lib/docker/volumes/agentic-workspace_aw-remote-host-state/_data/.local/share/containers/storage/`,
   tar `libpod/ overlay-containers/ overlay-layers/ overlay-images/ volumes/ defaultNetworkBackend`
   into `.podman5-2026-09-08/`. **Exclude `overlay/`** — 40 GB, re-pullable, and untouched by the
   rollback. Expect ~81 MB. Verify `tar -tzf` lists non-zero content and record `sha256sum`.
4. **`docker rename aw-remote-host aw-remote-host-legacy`.** Rename, never `docker rm` — the
   stopped renamed container is the entire 60-second rollback, and this is the pattern the
   aw-stack cutover already proved on this host.
5. Set the new digest in **both** places, repo variable first:
   - `tekflox/aw-stack` repo variable `AW_REMOTE_HOST_IMAGE`
   - `/opt/aw-stack/.env` (`86e225b` makes `deploy.yml` **merge** rather than overwrite, so a hand
     edit survives — but a repo variable left stale reverts it on the next deploy)
6. `docker compose -f /opt/aw-stack/docker-compose.yml up -d --no-deps aw-remote-host`
7. If the new container does not reach `running` within 120 s → auto-rollback (§5.3).
8. **Press the console "bootstrap" action** (or `POST /api/workspaces/aw/bootstrap`). This is what
   runs the modules: `entrypoint.sh` passes neither `--with-workspace` nor `--full`, so a plain
   recreate is a **lean link that installs nothing** (`commands.go:410` gates every module on
   `provisionWorkspace`). **This step cannot be automated — see §7.**

### 5.2 — Phase B2: verify, in this order

The first check exists to distinguish catastrophe from non-catastrophe. Do it first.

1. `podman ps -a | wc -l` and `podman info --format '{{.Host.DatabaseBackend}}'`.
   **If the count is 0, this is NOT data loss** — every app's real data is in bind mounts under
   `/home/aw-remote-host`, not in podman's store. Do not delete or restore anything; go to §5.4.
2. `podman --version` → `5.4.2`
3. `podman info --format '{{.Store.GraphRoot}}'` → `/home/aw-remote-host/.local/share/containers/storage`
4. `cat /etc/containers/storage.conf` → graphroot **and** runroot present
5. `podman info --format '{{.Host.NetworkBackend}}'` → `netavark`; `podman network ls` → both
   `aw-remote-host` and `podman`
6. Container count back to the durable set; **diff by name against §5.0's inventory and report
   anything missing by name.** "Looks about right" is not a verification.
7. Postgres, Redis, and the workspace API healthy; `aw-workspace-cli apps` lists 52
8. `command -v ip wg wg-quick openvpn tailscale` — all five
9. `aw-workspace-cli doctor` — clean, or every degradation explained. Today's baseline is **1
   problem**: `windows-pilot` declared but not live in the gateway, plus `browser` auto_start off.
   Anything beyond those two is new.
10. `iptables` counts inside vs baseline (36 / 48 / 7, 103 lines); `ip rule show` vs baseline
11. `state.json` → `last_bootstrap_version: v0.1.87`
12. **`podman network update --help`** — the verb this entire operation exists to obtain. If it is
    not there, the upgrade bought nothing and should be rolled back.

### 5.3 — Rollback, and the trap in it

1. `docker rm -f aw-remote-host && docker rename aw-remote-host-legacy aw-remote-host && docker start aw-remote-host`
2. **Restore `state.json`'s `last_bootstrap_version` to `v0.1.86`**, at
   `/var/lib/docker/volumes/agentic-workspace_aw-remote-host-state/_data/.aw-remote-host/state.json`,
   editable from the bare metal without entering the container.

   > **This step is mandatory and it is not obvious.** A successful Phase B records `v0.1.87`.
   > `state.CheckDowngrade` (`internal/state/state.go:303-329`) then **refuses** the v0.1.86
   > binary's full bootstrap, and `runner.go`'s fail-fast means podman, postgres, redis and the
   > workspace never start. The `--force` escape exists (`commands.go:210`,
   > `ops.Bootstrap`'s `args["force"]`) but the console button does not send it —
   > `remote_host_driver.py` dispatches `"bootstrap"` with no args. **A rollback that skips this
   > step leaves the host down and the reason is three layers away from the symptom.**
3. Restore the 81 MB metadata tar over the graphroot **only if** steps 1–2 leave podman 4.3.1
   unable to see its containers.
4. Revert `AW_REMOTE_HOST_IMAGE` — repo variable **and** `.env` — to
   `sha256:4360436807b6aebe9a37a1405662d8b9b9875d814b088fd3fda616a3c7d16418`.

### 5.4 — Fallback: podman 5 sees zero containers

Recoverable, and not data loss (§5.2 step 1). Let the bootstrap and the app reconciler re-create
the containers; images re-pull. **Watch disk**: the old `overlay/` (40 GB) is still present and
62 GB was free, so a full re-pull lands near the edge. Once the metadata tar is verified, deleting
the old `overlay/` is safe and reclaims 40 GB.

### 5.5 — Phase C: soak, then the acceptance test

Re-check firewall counts **after it has run a while**, not at the moment of change — the 3,714-rule
leak grew silently and a check at cutover would have passed. Then dial the VPN and read
`dns_tunneled`.

**It will still be `false`.** See §8.

---

## 6. What I rejected, and why

- **bookworm-backports / Kubic / apt-pinning trixie's `.deb`.** Measured dead ends (§2), first in
  round 1 and re-confirmed today. Recorded so nobody re-derives them.
- **Baking podman into the image.** Tempting — it removes `apt-get` from the outage window. Rejected
  because `bootstrap/podman/verify.sh` returns `AlreadyOK` the moment podman is present and healthy,
  which is exactly the path that would skip `install.sh` and therefore skip
  `configure_podman_graphroot`. `d99cab1` just closed that hole; baking podman re-opens the shape it
  closed. **This is the runner-up**: if the trixie apt install proves flaky in the window, it is the
  escape, and the Dockerfile already documents the pattern.
- **Pre-emptively wiping the graphroot to give podman 5 a clean store.** It forces a 40 GB re-pull
  into 62 GB of headroom with the old 40 GB still on disk. The 81 MB metadata backup buys the same
  reversibility for 0.2% of the cost. In-place first; wipe only as §5.4.
- **A separate "Phase A rehearsal" recreate**, as round 1's plan specified. Superseded: the aw-stack
  cutover on 2026-09-08 13:07 *was* that rehearsal, executed against production, with the digest pin
  already in place. Running another one now costs an outage and tests nothing new.
- **Bundling the `AW_WORKSPACE_IMAGE` un-pin, the aw-backend pin, or the dialer work** into this
  window. Three simultaneous changes on a silently-failing host are unattributable.

---

## 7. What I could not verify — read this before starting

1. **podman 5.4.2 opening a store created by 4.3.1. Untested.** I did not run it. The bare metal's
   own podman 5.7.0 proves the kernel is fine and proves nothing about the store. Podman 5 is
   *supposed* to keep BoltDB when it finds an existing one rather than migrating to SQLite; if that
   detection does not fire, §5.2 step 1 reports zero containers and it reads exactly like the
   2026-09-02 incident. **The 81 MB backup exists precisely because I could not verify this.**
2. **The bootstrap action cannot be automated.** Measured, not assumed, by a prior Coder session
   that tested the only automation credential on the host (AP-MT's `RUNNER_CALLER_TOKEN`) and found
   it lacks the identity `POST /api/workspaces/{slug}/bootstrap` requires. **Frederico must be at a
   browser during the window.** This is the single thing that can stall the operation mid-outage,
   and it needs confirming before anyone runs `docker stop`.
3. **Whether the bare-metal runner is healthy and idle.** There are recorded failures both ways, and
   `gh run watch` hangs on already-completed runs — check status before watching.
4. **Whether trixie changes anything else the bootstrap depends on.** `sudo`, `bash`, `curl`,
   `procps`, `psmisc`, `file` all exist in trixie; I did not enumerate second-order behaviour
   changes (e.g. `apt` defaults). The §5.0 gate catches the ones that matter.

---

## 8. Scope: this does not, by itself, deliver the card

The card's acceptance test is `dns_tunneled: true`. podman 5 is a **precondition**, not the
feature. Three blockers were re-measured on 2026-09-07 and all persist:

1. **The dialer routes exactly one container.** `src/vpn/dialer.py:481` is
   `route_container = container or container_info["name"]`, and `internal/state/state.go:122`
   holds `ExternalRoute *ExternalRouteState` — one optional pointer, not a list.
2. **Create-time `--dns` *replaces* the resolver list** rather than prepending to it — re-probed
   live: baseline `10.89.0.1 + 8.8.8.8 + 8.8.4.4`, with `--dns 1.1.1.1` only `1.1.1.1` survives.
   The workspace addresses Postgres and Redis by name, so it would not boot.
3. **Containers started after the dial gain nothing**, which follows from 1 and 2.

`podman network update` is what dissolves blocker 2 and makes 3 tractable. **Blocker 1 is a
dialer design change and is not addressed by any version of podman.** Designing it against a host
that cannot run the verb is guesswork; it should be a separate round once §5.2 step 12 passes.

**Whether "até o fim" includes that dialer rewrite is Frederico's call, not the Architect's.**
