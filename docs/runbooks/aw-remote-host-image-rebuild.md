# Runbook — prune, repoint podman's graphroot, rebuild the `aw-remote-host` image

**Status:** written 2026-09-03, **NOT YET EXECUTED**. Review before dispatch.
**Authorized by:** Frederico, Telegram 2026-09-03.
**Design context:** [`../architecture/vpn-profiles-in-general.md`](../architecture/vpn-profiles-in-general.md) §2.7.
**Cards:** V0 `3d05bf3b-9510-81c5-aae7-c3a14d99f89d`, VPNs #3 `3d05bf3b-9510-81f4-8c3e-f92dd7c241e5`.

This operation destroys and re-creates the container that hosts **130 running
containers and every agent session** on the production bare metal. The
2026-09-02 incident (`byod-postgres-lost-bind-mount-2026-09-02`) was this exact
operation going wrong by accident. Every number below was measured on
2026-09-03; **re-measure before acting — do not trust these as current.**

---

## 0. The single most important operational fact

> **The agent executing Phase 3 CANNOT be running inside `aw-remote-host`.**

Every agent session on this workspace runs in a podman container inside
`aw-remote-host`. Destroying that container kills the executing session
mid-runbook, leaving the operation half-done with nobody watching — the same
"the fix-it tool is taken down with the thing it fixes" shape as the dead-man's
switch, one layer up.

**Phases 1–3 execute against the bare metal via `remote_host_exec_run` with
`host_id=2d5d56ef224359c0`** (`bare-metal-privileged`, `elevated: true`), or a
human SSH session. Phase 0 and Phase 4 may run from anywhere.

Anything long-running must be detached with output to a file on the bare metal,
not held on an exec socket (sockets drop at ~60s).

---

## 1. What survives, what dies — measured, and this is the whole safety case

`docker inspect aw-remote-host` shows it has **exactly one mount**:

```
volume | src=/var/lib/docker/volumes/agentic-workspace_aw-remote-host-state/_data
       | dst=/home/aw-remote-host
       | name=agentic-workspace_aw-remote-host-state | rw=true
```

| | Where it lives | Fate on recreation |
|---|---|---|
| `/home/aw-remote-host` — Postgres, Redis, the workspace tree, app data, KB, secrets | docker named volume `agentic-workspace_aw-remote-host-state` | **SURVIVES** — the volume is re-attached by name |
| podman images (31, **42.07 GB**) | graphroot on the container's **writable layer** | **DESTROYED**, re-pulled |
| podman container definitions (131) | same | **DESTROYED**, re-created by bootstrap + app reconciler |
| podman **named volumes** (24, 529.8 MB) | `<graphroot>/volumes` — same writable layer | **DESTROYED** — see §1.1 |

**`/proc/self/mounts` inside the container proves the graphroot is on the
writable layer:** `/var/lib/containers/storage/overlay` sits on the containerd
overlay with `upperdir=/var/lib/containerd/…/snapshots/36063/fs`, while
`/home/aw-remote-host` is `/dev/md2 ext4`. `/etc/containers/storage.conf` is
empty — `bootstrap/lib/podman_storage.sh`'s fix has never been applied here.

### 1.1 The real data exposure is 47 MB, in one volume

Of 24 podman named volumes, **only 2 are attached to any container**:

| Volume | Size | Used by |
|---|---|---|
| `823c3cf2ea90…` | **47 MB** | `aw-workspace-repro2-pg-2402704` |
| `98740e731e9e…` | **4.0 KB** | `aw-app-kb` |

The other 22 are orphans. And **1137 bind mounts across all containers point
into `/home/aw-remote-host`** — i.e. essentially all real data is on the
surviving volume already. The only bind sources outside it are
`/run/podman/podman.sock` and `/var/run/docker.sock` (sockets, not data).

**So the data-loss surface of this whole operation is ~47 MB in one volume
belonging to a container named `…repro2-pg…`.** Phase 0 backs it up anyway —
47 MB is free to protect, and "it looked like a repro container" is not a
thing to be wrong about.

### 1.2 Where the space actually comes from

Naively this looks impossible: 39 GB free, and a full re-pull needs ~42 GB.
It works because **the old graphroot is freed when the old container is
removed** — 42.07 GB of images + 3.45 GB of containers, all on the writable
layer. Hence the hard ordering rule in Phase 3: **remove the old container
BEFORE creating the new one.** The prune in Phase 1 exists to give margin so
the operation does not *depend* on that ordering being perfect.

---

## 2. Abort criteria — stop and escalate, do not improvise

Abort if **any** of these is true at the moment you check it:

- `docker volume inspect agentic-workspace_aw-remote-host-state` fails or the
  volume is missing.
- The Phase 0 backup cannot be verified by checksum.
- Free space after Phase 1 is **< 45 GB**.
- The new image fails to build, or builds without `ip`, `wg` or `openvpn` on
  its PATH (Phase 3 gate).
- `aw-vpn-hub` is not `Up (healthy)` — it shares the host netns and its state
  is host state; do not do surgery next to a sick tunnel.
- You are executing from inside `aw-remote-host` (see §0).

There is no step in this runbook where "try it and see" is correct.

---

## 3. Phase 0 — inventory and backup (non-destructive, do this first)

Run against the bare metal. Nothing here changes state.

**0.1 — Re-measure everything in §1.** Confirm the mount is still the single
named volume; confirm the graphroot is still on the writable layer; confirm
`/etc/containers/storage.conf` is still empty. If any differs from §1, **stop
and re-plan** — the premise moved.

**0.2 — Write an inventory to the surviving volume**, so it exists after the
container is gone. Target dir: `/var/lib/docker/volumes/agentic-workspace_aw-remote-host-state/_data/.rebuild-2026-09-03/`
(= `/home/aw-remote-host/.rebuild-2026-09-03/` from inside).

Capture, as files:
- `podman ps -a --format json` — every container, its image, its mounts.
- `podman images --format json`, and a plain list of image refs — **this is the
  re-pull manifest; without it you cannot prove afterwards that everything came
  back.**
- `podman volume ls --format json` + `podman volume inspect` for all 24.
- `podman network ls --format json` (the `aw-remote-host` 10.89.0.0/24 network
  and `podman` 10.88.0.0/16).
- `docker inspect aw-remote-host` (full JSON — the container's own spec, needed
  to re-create it identically).
- The full `docker run`/compose invocation that created it. **Find this before
  proceeding** — the image is `agentic-workspace-aw-remote-host`,
  `Restart=always`, `Privileged=true`; if it is a compose service, name the
  compose file and project.

**0.3 — Back up the two active podman volumes.** Tar them into the same
`.rebuild-2026-09-03/` dir, record `sha256sum`, and **verify the tar lists
non-zero content** (`tar -tzf`). A backup nobody opened is not a backup.

**0.4 — Back up the workspace-critical trees anyway.** They are on the
surviving volume, so this is belt-and-braces, but it is cheap relative to the
blast radius: `.aw-workspace/secrets`, `.aw-workspace/data`,
`.aw-workspace/app-config`, and the Postgres data dir. If Postgres is dumped
rather than file-copied, do it with the DB **running**, before anything else.

**0.5 — Gate.** Do not proceed until 0.2–0.4 exist on the *docker volume path*
(`/var/lib/docker/volumes/agentic-workspace_aw-remote-host-state/_data/…`)
verified **from the bare metal**, not from inside the container. That is the
proof they are on the surviving side of the boundary.

---

## 4. Phase 1 — prune, then measure

Frederico's instruction: prune docker **and** podman first, then verify
headroom rather than assume it.

**Measured 2026-09-03 — `docker system df` on the bare metal:**

| Type | Total | Active | Size | Reclaimable |
|---|---|---|---|---|
| Images | 103 | 39 | 83.56 GB | 16.23 GB (19%) |
| Containers | 54 | 38 | 48.66 GB | 13.14 MB |
| Local Volumes | 46 | 13 | **115.7 GB** | 1.398 GB |
| Build Cache | 403 | 0 | 30.58 GB | **12.49 GB** |

**`podman system df` inside `aw-remote-host`:**

| Type | Total | Active | Size | Reclaimable |
|---|---|---|---|---|
| Images | 31 | 23 | 42.07 GB | 4.302 GB |
| Containers | 131 | 127 | 3.454 GB | 122.7 kB |
| Local Volumes | 24 | **2** | 529.8 MB | 481.7 MB |

**1.1 — `docker system prune -f`. NEVER `--volumes`.**

> ⛔ `--volumes` is the flag that deletes
> `agentic-workspace_aw-remote-host-state`. It is "safe" only while a running
> container holds it — and Phase 3 deliberately stops that container. **The
> flag must never appear in this operation at all**, so that no ordering
> mistake can arm it.

Expect ≈ **12.5 GB+** back (build cache + dangling images + the 4 stopped
`aw-warm-*` containers).

**1.2 — Do NOT reach for `-a` by default.** `docker system prune -a` would
reclaim ~16 GB more but deletes every image not used by a *running* container —
including the images of the **16 stopped docker containers** (54 total, 38
running). A stopped service would then need a re-pull to start. Only escalate
to `-a` if 1.4's gate fails, and say so explicitly when you do.

**1.3 — `podman system prune -f` inside `aw-remote-host`. No `-a`, no
`--volumes`.** Measured: the only stopped containers are **4 `aw-warm-*`**
(ephemeral per-run agent containers, exited 15 s – 6 min ago), so this is safe
today — **re-check the stopped list before running it**; a stopped *app*
container in that list changes the answer and must be reported, not pruned.
Note this reclaims space on the writable layer that Phase 3 destroys anyway;
it is here because Frederico asked for it and because it shrinks the window
where disk is tight.

**1.4 — Gate: `df -h /` must show ≥ 45 GB free.** Baseline was 39 GB free of
436 GB (91%). Below 45 GB, stop and report the number — do not proceed hoping
§1.2's freeing effect covers it.

---

## 5. Phase 2 — repoint the graphroot (the actual fix)

**2.1 — Establish what "arming" requires.** `bootstrap/lib/podman_storage.sh`
points nested rootful podman's graphroot at a path under `$HOME` instead of
`/var/lib/containers/storage`. `bootstrap/podman/install.sh` calls it behind an
`id -u == 0` gate (the gate is outside the function on purpose — read the
comment there). Team memory records that v0.1.71 ships the fix but it stays
**inert until a full bootstrap runs**. The host reports `cli_version v0.1.71`.
**Determine and state, before executing, exactly what makes it take effect** —
if the code is already present and only the bootstrap needs to run, say so.

**2.2 — The migration decision, and it is already made.** Do **not** copy the
old graphroot to the new path. Copying ~46 GB within the same filesystem needs
a transient ~92 GB that does not exist. Frederico authorized the re-pull
explicitly (*"vamos considerar que cabe um pull completo"*). So: repoint, let
podman re-pull, restore the 47 MB volume from the Phase 0 backup.

**2.3 — Confirm the new graphroot lands under `/home/aw-remote-host`**, i.e.
on the surviving docker volume. That is what makes this a **one-time** loss
instead of a recurring one. If the fix would point it anywhere else, stop.

**2.4 — Note the accounting change:** afterwards `/home/aw-remote-host` carries
both the workspace tree and ~46 GB of podman storage. Same physical disk, but
anything that reasons about that volume's size needs to know.

---

## 6. Phase 3 — rebuild the image and recreate the container

**Execute from the bare metal only (§0).**

**3.1 — Move the Dockerfile into `repos/aw-remote-host`.** It currently lives
at `repos/agentic-workspace/tools/aw-remote-host/Dockerfile` and line 5 is
already `COPY repos/aw-remote-host/ .` — the Go source is the new repo's, only
the packaging is in the vetoed repo. Move `Dockerfile`, `entrypoint.sh`,
`healthcheck.sh`; adjust the build context.

**3.2 — Add the packages** to the existing apt line (keep
`ca-certificates bash sudo curl procps psmisc file` — its comment records why
those diagnostics are not optional):

```
iproute2 wireguard-tools openvpn
```

Measured cost: 12 new packages, **6.5 MB** total (iproute2 3.9 MB,
wireguard-tools 321 KB, openvpn 2.4 MB). None pulls systemd, dbus or python.

**3.3 — Expect tailscale to appear.** The Dockerfile installs tailscale; the
*running* container has no tailscale binary and no tailscale dpkg entry. The
deployed image is stale relative to its own Dockerfile, so this rebuild also
lands tailscale and switches on the exit-gate path on this host. Expected, not
a surprise — but verify it does not fight `aw-vpn-hub`'s host-netns rules.

**3.4 — Build, and GATE on the binaries before touching the running
container:**

```
docker run --rm <new-image> sh -c 'command -v ip wg wg-quick openvpn'
```

All four must resolve. **If not, stop here — nothing has been destroyed yet.**
This is the last completely safe moment in the runbook.

**3.5 — Recreate, in this order, and only this order:**

1. Quiesce: stop accepting new agent runs if there is a way to; announce.
2. `docker stop aw-remote-host` (its 130 podman children stop with it).
3. **`docker rm aw-remote-host`** — this frees the ~46 GB writable layer.
   Re-check `df -h /` here; the freed space is what the re-pull consumes.
4. Create the new container from the recorded spec (0.2): same name, same
   `--restart=always`, same `--privileged`, **same volume mount
   `agentic-workspace_aw-remote-host-state:/home/aw-remote-host`**, new image.
5. Let the entrypoint run `bootstrap-workspace`. It re-pulls images and
   re-creates containers from the manifest.

**Do not `--force-recreate` via a compose path that creates before removing** —
that holds both writable layers at once and is exactly the case §1.2's ordering
rule exists to avoid.

**3.6 — Rollback.** Up to and including 3.4: nothing to roll back. After 3.5.3
there is no rollback to the *old container*; the recovery path is forward —
re-create with the **old** image tag (keep it, do not prune it, and record its
ID in Phase 0) and let bootstrap re-pull. This is why 0.2's image manifest is
mandatory: it is the only list of what should come back.

---

## 7. Phase 4 — verify (the operation is not done until these pass)

1. `docker volume inspect agentic-workspace_aw-remote-host-state` — exists,
   and `/home/aw-remote-host/.rebuild-2026-09-03/` is still there with matching
   checksums. **Data survival proven, not assumed.**
2. Container count back to ~130; **diff the running set against Phase 0's
   inventory and report anything missing by name.** "Looks about right" is not
   a verification.
3. Postgres, Redis, the workspace API (`curl 127.0.0.1:9030/api/health` from
   inside), and the KB all up.
4. Restore the 47 MB volume from backup if its container did not come back with
   its data.
5. `podman info --format '{{.Store.GraphRoot}}'` → under `/home/aw-remote-host`.
6. Inside the new container: `command -v ip wg wg-quick openvpn` all resolve.
7. `aw-workspace-cli doctor` — clean, or every degradation explained.
8. `aw-vpn-hub` still `Up (healthy)`; `wg show wg0` still shows the GL.iNet peer
   handshaking. Its rules are host state and this operation ran beside them.

---

## 8. `aw-vpn-hub` parity — what Frederico's requirement means, grounded

He said: *"é importante ter o mesmo que o aw-vpn-hub fornece pra gente poder
testar, ele é o importante."* Read against
`repos/aw-stack/scripts/vpn-hub-entrypoint.sh`, that resolves into two
requirements — and one reading I am rejecting.

**What `aw-vpn-hub` actually is:** a WireGuard **server/hub** in the host netns
— `Address = 10.8.0.1/24`, `ListenPort = 51820`, `MTU = 1420`, `Table = off`,
nine peers (client_1–8 at 10.8.0.3–.10, plus the GL.iNet at
`AllowedIPs = 0.0.0.0/0` with a roaming endpoint). It owns routing table 200
(`default via 10.8.0.2 dev wg0` plus runtime-discovered bridge routes) and
`ip rule from <client> table 200 priority 100–107`.

### 8.1 Requirement A — the dialer must be able to dial `aw-vpn-hub` as a client

**This is the reading I am confident in**, and it is what makes the feature
testable without NordVPN credentials or any external provider. Measured today:

| Peer | Allowed IP | Last handshake | State |
|---|---|---|---|
| client_1 | 10.8.0.3 | ~1 d 15 h ago | stale, was used |
| **client_2 … client_8** | 10.8.0.4 – 10.8.0.10 | **0 — never** | **7 free slots** |
| GL.iNet | 0.0.0.0/0 | seconds ago, 6.7 GB rx / 7.8 GB tx | live |

`aw-vpn-hub` is `Up 41 hours (healthy)`, and the legacy `wg-vpn` container is
**gone from the host entirely** — the "unresolved conflict" in the aw-stack
README is resolved in practice; worth telling Frederico.

**The acceptance test writes itself, and it is falsifiable:** bare-metal egress
is **65.109.66.88**; through the hub it must be **24.90.8.255** (the GL.iNet).
So: dial `aw-vpn-hub` from the new dialer on a free client slot, route one
container through it, and assert the container's public IP is `24.90.8.255`
while the host's is still `65.109.66.88` — which is exactly
`externalroute.go`'s stated invariant.

⚠️ The client_1–8 **private keys already exist** — they are the
`client_[1-8].conf` files committed in `fredericowu/aw-backend` (the security
card, `3d05bf3b-9510-819d-90fe-ede84280842d`). Using one for a test is
acceptable; **treat them as burned** and do not let a test slot become the
production path.

### 8.2 Requirement B — inherit the hub's WireGuard discipline

Its entrypoint is a list of hard-won corrections. The new dialer should carry
the same ones, and they are cheap to copy:

- **`Table = off` plus routing we manage ourselves** — otherwise `wg-quick`
  populates the main table from a peer's `0.0.0.0/0` and hijacks the default
  route. Same rule as `external-vpns-in-networking.md` §3.4.
- **Never hardcode a bridge name; resolve from the live routing table** —
  `discover_bridge()`. Hardcoding is what caused 619 restarts and 3,714 leaked
  rules.
- **Idempotent mutations**: `ipt_ensure()` drains all matching rules in a loop
  then adds exactly once (its comment explains why `-C` is unreliable on this
  busy host); `ip route replace`; `ip rule` delete-then-add.
- **Identity validated before the tunnel starts** — derive the public key from
  the private key and refuse to start on mismatch, rather than failing two
  layers away.
- **A teardown trap that removes exactly what was installed** — the legacy
  container's `PostDown` never completed on a crash exit.
- **`MASQUERADE` scoped to the tunnel subnet**, never to "everything leaving
  the external interface".

### 8.3 The reading I am rejecting — and flagging that I might be wrong

"The same thing aw-vpn-hub provides" could mean **the dialer should itself be a
hub** (accept inbound peers). I do not think so: the whole feature is an
*outbound client* ("conectar NordVPN"), an inbound server is a different
product, and the sentence's own clause — *"pra gente poder testar"* — points at
testing, not at capability. A + B are mutually consistent and both supported by
the code; C is supported by neither.

**This is the one place in this document where I am interpreting rather than
measuring.** If Frederico meant C, the scope changes materially and it is a
Product Owner conversation, not something to absorb here. **Worth one
confirming question before V4′ starts.**

---

## 9. What I could not verify

- **Whether the deployed v0.1.71 binary contains the graphroot fix**, or what
  precisely arms it. §2.1 makes determining this a step rather than an
  assumption.
- **Whether `aw-workspace-repro2-pg-2402704` matters.** Named like a
  reproduction; backed up regardless.
- **The exact `docker run`/compose invocation that created `aw-remote-host`.**
  Step 0.2 requires finding it; the runbook cannot proceed without it.
- **Whether re-pulled images will be byte-identical.** They are `:latest` tags;
  a rebuild elsewhere in the interim means a container comes back on a newer
  image. Phase 0's manifest records refs, not digests — **capture digests too.**
- **Whether `PickProbeNetwork`'s fix is in the deployed binary** (it is in the
  source).
