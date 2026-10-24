# aw-app-neo4j — a reusable Tier-2 Neo4j dependency app

Card: `4035bf3b-9510-811b-951e-c074bd6fd2b1`
(`architecture:aw-app-neo4j-reusable-tier2-dependency`, target `aw-app-neo4j`).

Context: Frederico explicitly vetoed the "no Neo4j, stay on Postgres" decision
in `docs/design/research-search-graph-layer.md` (commit 79294f1) — that doc
itself named "Frederico overrides on reading this" as the trigger that reopens
it, and he did: *"Eu quero o neo4j, uma instância que será tier2 da workspace.
Podemos criar uma app separada chamada neo4j, daí outras apps podem
reutilizá-la."* This doc designs that separate, generic piece. The
research-search graph layer itself is being redesigned on a parallel card
(`4025bf3b-9510-81c9-8daa-ec144a45a9b4`, target `research-search-mcp`) against
§2 of this doc, which is therefore written first and frozen hardest.

## 0. Live facts this design stands on (verified 2026-10-24)

| Fact | Evidence |
|---|---|
| Tier-2 app containers join a shared podman network with the workspace process and resolve each other by container name (`aw-app-<id>`) via aardvark-dns | `src/apps/containers.py:905-935` (start() comment: "same way it already reaches postgres/redis — see the rootless-podman-tier2 ADR"); crispal's wordpress sidecar dials `aw-app-crispal-db` by name (`repos/aw-app-crispal/aw-app.json`) |
| A Tier-2 app can be a **stock upstream image with zero custom code**, configured purely via `runtime.env` + mounted files | aw-app-signoz-connector runs `otel/opentelemetry-collector-contrib` exactly this way; its manifest notes "a Tier-2 manifest has no command/entrypoint override" |
| `runtime.env` placeholders: `${config.x}` / `${env.X}` / `${app.url}` / `${data.uid}`, whole-value only, `\|` fallback chain; **an unresolved placeholder drops the variable entirely** | `src/apps/containers.py:50-54,172-265` (`expand_env`, `expand_value`) |
| `dependencies.apps` already exists and is enforced: required deps are auto-installed before the dependent, and a loaded required dep is protected from uninstall | `src/apps/reconciler.py` `_install_dependencies` / `_loaded_dependency_closure`; precedent `aw-app-mcp-tools` → `mcp-gateway` (aw-create-app SKILL §9) |
| `$AW_APP_DATA` mounts `<AW_WORKSPACE_HOME>/data/<app_id>` rw and survives uninstall/update; package-relative volumes do NOT | `src/apps/runtime.py:1816-1830` |
| App containers get `restart_policy: unless-stopped`, `cap_add: [CHOWN]`, no healthcheck kwarg; `register()` **requires** `runtime.port` for the app's own container | `src/apps/containers.py:705-740,835-943` |
| Config saved via `POST /api/apps/<id>/config` lands in `<AW_WORKSPACE_HOME>/app-config/<app_id>.json`, which uninstall deliberately keeps | aw-create-app SKILL §4 (mcp.template section) |
| Neo4j 5.26.4 Community: **exactly one user database** (`CREATE DATABASE` → "Unsupported administration command"), no online backup (Enterprise-only); this exact pinned image runs healthy in prod as aw-stack's `aw-neo4j` with heap 512m / pagecache 256m / tx-log retention "200M size" | M1 delivery evidence on card `aw-knowledgeable-m1`, 2026-09-26; `docs/design/aw-knowledgeable-infra.md` §1 |
| aw-knowledgeable crashed at boot when its Neo4j was unhealthy, and the rollback crashed too — total outage from a hard runtime dependency | card "aw-knowledgeable: por que o Neo4j ficou unhealthy 10h" |
| research-search is **Tier-1** (`"tier": "inprocess"`) — it runs inside the workspace process, which is itself on the shared network | `repos/aw-app-research-search/aw-app.json` |

## 1. The approach, in sentences someone can disagree with

**aw-app-neo4j is a Tier-2 app whose main container IS the stock pinned
`neo4j:5.26.4-community` image. There is no wrapper, no custom image, no
Python, no UI, and no new framework.** The app is a manifest, a skill, and a
README. Its entire value is: a dedicated Neo4j reachable at a **fixed,
documented hostname on the shared container network** (`aw-app-neo4j:7687`),
owned by the app framework (install, restart policy, resource caps, durable
data dir, doctor visibility), consumable by any app that declares
`dependencies.apps: [{"id": "neo4j"}]` and carries the credential in its own
config. Every mechanism this needs already exists; the design's job is to
*choose and freeze* the contract, not to build machinery.

The dependency contract is deliberately minimal — one consumer exists today.
The card asked for exactly this: "propose the minimal mechanism rather than
over-engineering a general dependency framework for a need of exactly one
consumer."

## 2. The connection contract (FROZEN — the parallel card builds against this)

A consuming app connects as follows. research-search is consumer #1; any
future consumer follows the same five rules.

1. **URI**: `bolt://aw-app-neo4j:7687` (scheme `neo4j://` equally fine; no
   routing, single instance). Resolvable from the workspace process (so from
   every Tier-1 app, research-search included) and from every Tier-2 app
   container on the shared podman network. **Not published to the host** —
   there is no `127.0.0.1:7687`, by design; the network boundary is the
   shared podman network itself.
2. **Database**: `neo4j` — the only one Community Edition has. **User**:
   `neo4j`. **Credential**: a single shared password, generated once at
   install time and stored twice-per-consumer: in aw-app-neo4j's own config
   (`auth` = the literal `neo4j/<password>` string the image's `NEO4J_AUTH`
   wants — the placeholder grammar is whole-value, so the config field holds
   the full pair) and in each consumer's own `config_schema` (research-search
   adds `neo4j_password`, `x-secret: true`, no default). Both live in the
   app-config store, which survives update/uninstall/redeploy.
3. **Install-order**: the consumer declares
   ```jsonc
   "dependencies": { "apps": [ { "id": "neo4j", "required": true,
     "reason": "Dedicated workspace graph store; this app's theme/reference graph lives in it." } ] }
   ```
   The reconciler then auto-installs aw-app-neo4j first and protects it from
   uninstall while a dependent is loaded.
4. **Runtime posture — degrade, never crash** (the aw-knowledgeable 10h-outage
   lesson, inverted): a consumer whose `neo4j_password` is empty or whose bolt
   connection fails MUST boot normally, disable its graph features, and
   surface the degradation in its own health/status endpoint (for
   research-search: `index_status` / its `/health`). Install-time dependency
   is hard; runtime dependency is soft. Bolt readiness lags container-running
   by 10–30 s (JVM boot) — consumers retry with backoff rather than failing
   the first call.
5. **Namespacing** (Community = one database, shared by all consumers):
   every consumer prefixes its labels with its slug (`rs__Paper`,
   `rs__Theme`), its relationship types likewise (`RS_CITES`, `RS_HAS_THEME`),
   and names every constraint/index `<slug>_...`. Convention, not
   enforcement — same property/prefix tenancy pattern aw-knowledgeable already
   uses in its own instance, and each consumer ships its own static guard test
   (aw-knowledgeable's K5 pattern) asserting all its Cypher stays inside its
   prefix.

That is the whole contract: a hostname, a database name, a password handed
through config, an install-order declaration, a crash-isolation rule, and a
naming convention.

## 3. Where it lands

### 3.1 New repo `tekflox/aw-app-neo4j` (scaffold from aw-app-template)

The repo contains **no runtime code** — delete the template's `template_app/`,
`ui/`, tests-of-code; keep `tests/validate_manifest.py` wiring, `release.yml`,
README, and add one skill. `aw-app.json`:

```jsonc
{
  "manifest_version": 1,
  "id": "neo4j",                          // container: aw-app-neo4j; routes /api/apps/neo4j
  "name": "Neo4j Graph Database",
  "version": "0.1.0",
  "description": "A dedicated Neo4j graph database your other apps can share. Install it once and any app that declares it as a dependency gets a durable graph store on the workspace's own network — no setup beyond a password.",
  "icon": "share-2",
  "tier": "container",
  "publisher": "TekFlox",
  "resource_estimate": { "cpu": "medium", "memory": "~1 GB", "disk": "medium" },
  "runtime": {
    "image": "neo4j:5.26.4-community",    // pinned — the exact image prod already runs (M1)
    "port": 7474,                          // Neo4j's HTTP API; the framework's proxy/reach target
    "resources": { "cpus": 1.0, "mem_mb": 1536 },
    "env": {
      "NEO4J_AUTH": "${config.auth}",
      // Caps copied verbatim from the prod-proven M1 settings:
      "NEO4J_server_memory_heap_initial__size": "512m",
      "NEO4J_server_memory_heap_max__size": "512m",
      "NEO4J_server_memory_pagecache_size": "256m",
      "NEO4J_db_tx__log_rotation_retention__policy": "200M size"
    },
    "volumes": [
      { "source": "$AW_APP_DATA/data", "target": "/data", "mode": "rw",
        "reason": "The graph itself. $AW_APP_DATA survives app update/uninstall and workspace redeploy; a package-relative mount would be wiped by the next update." },
      { "source": "$AW_APP_DATA/logs", "target": "/logs", "mode": "rw",
        "reason": "debug.log etc. — keeps the writable layer clean and the logs inspectable from the host tree." }
    ]
  },
  "permissions": [ "containers:manage", "fs:workspace-data" ],
  "config_schema": {
    "type": "object",
    "properties": {
      "auth": { "type": "string", "default": "", "x-secret": true,
        "title": "NEO4J_AUTH (user/password)",
        "description": "The full 'neo4j/<password>' pair, set once at install. Consumers carry the password half in their own config. Left empty, the container boots with Neo4j's locked default credential and no consumer can connect — set it before first start." },
      "auto_start": { "type": "boolean", "default": true, "title": "Auto-start" }
    },
    "required": []
  },
  "contributes": {
    "skills": [
      { "id": "aw-neo4j-dependency", "path": "skills/aw-neo4j-dependency/SKILL.md",
        "description": "How an aw-workspace app declares and consumes the shared aw-app-neo4j graph store: dependency entry, bolt URI, credential handling, namespacing rules, degradation contract." }
    ]
  },
  "dependencies": {}
}
```

No `contributes.windows`: Neo4j Browser connects from the *user's* browser
over a bolt websocket to 7687, which is deliberately unreachable from outside
the shared network — an `app_iframe` window would render a Browser that can
never connect. Human inspection path for now: `docker exec -it aw-app-neo4j
cypher-shell -u neo4j -p <pw>`. A real UI story is deferred (§6).

### 3.2 The skill `skills/aw-neo4j-dependency/SKILL.md`

The durable home of §2, written for the next consumer's Architect/Coder:
the five contract rules, the rotation runbook (change `auth` on this app +
`neo4j_password` on every consumer — **2 places today, 1+N in general; list
the consumers in the skill and keep the list current**; the production
`aw-neo4j` rotation touching five scattered places is the anti-pattern this
centralizes away), and the namespacing/guard-test expectation.

### 3.3 Consumer side (research-search) — NOT this card's scope

Belongs to the parallel redesign card (`4025bf3b…`, target
`research-search-mcp`): add the `dependencies.apps` entry, `neo4j_password` +
`neo4j_uri` (default `bolt://aw-app-neo4j:7687`) config keys, the `neo4j`
Python driver to `pip_requires`, the degradation behaviour, and its own
prefix-guard test. Listed here only so the seam is named; nothing in this
card's delivery touches `repos/aw-app-research-search`.

### 3.4 Untouched

`repos/aw-stack` (the prod `aw-neo4j` stays exactly as it is — different
instance, different owner, different lifecycle), `src/apps/*` in aw-workspace
core (no framework change needed — that's the point), `repos/aw-knowledgeable`.

## 4. What I rejected, and why

1. **Sharing aw-stack's `aw-neo4j`.** Forbidden by the card, and
   independently wrong: Community is single-database, so research-search
   nodes would live *inside* aw-knowledgeable's store (breaking its K5
   one-module discipline), its bolt port is loopback-only on the bare-metal
   host (not even reachable from the workspace's app network), and it would
   inherit the stop-the-world backup window and add a sixth place to an
   already five-place credential rotation.
2. **A second service in `repos/aw-stack`** (the aw-knowledgeable M1 pattern
   again). Operationally proven, but it is not an app: no marketplace
   install, no `dependencies.apps` declaration, deploys by manual
   `workflow_dispatch` on a repo most workspaces don't have, invisible to the
   app framework and `doctor`. The card asks for a *workspace app* other apps
   can depend on — aw-stack is the wrong layer, and Frederico's instruction
   ("uma instância que será tier2 da workspace") names the right one.
3. **Neo4j as a sidecar of research-search** (the crispal-db pattern). The
   simplest shape and the strongest runner-up — zero new repo, one manifest
   edit. Dies on the stated requirement: sidecars are private implementation
   details (registry key `<app>:<name>`, torn down with the app, no
   independent install/version/identity), so "other apps can reuse it" is
   structurally false. If reuse had not been a requirement, this is what I
   would have proposed.
4. **A custom wrapper image** (manager API + Neo4j in one image, or a
   FastAPI sidecar doing provisioning/health). Adds a build+release pipeline
   and an HTTP layer with no caller. The stock-image-as-Tier-2-app pattern is
   established (signoz-connector's otel collector; crispal's stock
   `mysql:8.0` sidecar) and the framework already supplies restart policy,
   env, volumes and resource caps declaratively.
5. **A general dependency/connection-injection framework** (core-mediated
   service discovery, per-consumer credential minting, env injection into
   dependents). One consumer exists. `dependencies.apps` + a fixed
   aardvark-dns name + config-carried credentials cover the need with zero
   core changes; the day a third consumer or a real isolation requirement
   arrives is the day this gets designed (see §6 triggers).

## 5. Risks for the Coders

- **`expand_env` drops an unresolved `${config.auth}` silently** — the
  container then boots with Neo4j's default locked credential
  (`neo4j/neo4j`, forced password change) and every consumer 401s, while
  the app shows green. This is this workspace's canonical failure mode
  (silent degradation). The build card MUST end with a live verification:
  config set, container restarted, then an authenticated
  `RETURN 1` over bolt **from the workspace process** (one-liner with the
  `neo4j` driver, or `curl -u` against `http://aw-app-neo4j:7474` tx API) —
  not just "container is running".
- **Container-running ≠ bolt-ready.** No healthcheck kwarg exists in the
  framework (`containers.py` `start()` builds none), and the stock image
  declares none. JVM boot is 10–30 s; a wedged boot (bad /data ownership,
  OOM) looks identical to a slow one from outside. Consumers own readiness
  (retry + degrade, §2.4).
- **/data ownership across redeploys.** The workspace redeploy sweep chowns
  every app's `$AW_APP_DATA` to the workspace uid (`app_data_owner`,
  `containers.py:82-169`); the neo4j image's entrypoint re-chowns `/data` to
  its own uid 7474 at start, which works because every Tier-2 container gets
  `cap_add: [CHOWN]` (`containers.py:867`). If it ever doesn't (the sudo-
  under-nested-podman class of failure), the documented fallback is
  `"run_flags_needed": ["--user=${data.uid}:${data.gid}"]` — don't improvise
  a chown in a wrapper script.
- **First release CI will fail** on `MARKETPLACE_SYNC_TOKEN` (new-repo
  allowlist) — fix via the `GH_RUNNERS_ADMIN_TOKEN` PUT documented in
  aw-create-app SKILL §10, then re-run. Test through the marketplace, not a
  sideload (reconciler reverts sideloads; same section).
- **Image pull on create can exceed the default timeout** — a second Tier-2
  app on this hosted workspace has timed out on create before
  (`AW_CONTAINER_CREATE_TIMEOUT`, default 120 s; the image is ~500 MB).
  Pre-pulling or one retry is acceptable; report it if hit.
- **Host disk pressure is real** (bare-metal has run at 86–95%). The caps
  above bound tx-log growth (the known unbounded-growth vector); the `/data`
  mount makes usage visible at
  `/opt/aw-workspace/.aw-workspace/data/neo4j/data`. Check free disk before
  first start, same as M1 did.

## 6. What this makes harder later (and the named triggers)

- **One shared credential, no per-consumer isolation.** Any consumer can
  read/write any other's subgraph; a leak rotates everybody. Fine at N=1,
  tolerable at N=2 with the skill's rotation list. **Trigger to revisit:** a
  consumer whose data is sensitive to other apps, or N≥3 consumers — then
  design per-consumer Neo4j-native users (Community does support multiple
  users, just not multiple databases) with grant scoping, which slots into
  this contract without moving data.
- **No backup in v1.** The only consumer's graph is a *projection* —
  rebuildable from research-search's Postgres (papers, AI-Generate refs and
  themes) by re-running its export. That is the explicit license for
  deferring the backup card, and it bakes in a rule: **a consumer that wants
  to store primary (non-rebuildable) data in this instance must first ship
  the backup** — a stop-the-container dump to the existing S3 bucket,
  modeled on `aw-stack/scripts/backup-neo4j.sh`. The skill states this rule;
  the parallel research-search card must keep its Postgres rows as the
  system of record.
- **Community single-database is now load-bearing.** Prefix-tenancy is baked
  into every consumer's schema; moving to real per-consumer isolation later
  means an Enterprise license or instance-per-consumer plus a data
  migration. Accepted knowingly — it is the same trade aw-knowledgeable made.
- **No shared network ⇒ no bolt.** On a degenerate host where the supervisor
  has no network (`containers.py:936` falls back to publishing only
  `runtime.port`), 7687 is unreachable and the app is useless. All current
  deployments have the network; named, not solved.
- **A pinned image means upgrades are app releases.** Deliberate (it is how
  the prod instance avoids `:latest` drift), but someone must bump it;
  stale-pin risk goes in the skill.

## 7. Decomposition

One coder-sized card — the app has no code, so scaffold-to-verified-install
is a single coherent delivery:

- **N1 — build & ship aw-app-neo4j** (target `aw-app-neo4j`): scaffold repo
  from aw-app-template per §3.1 (manifest + skill + README, no code);
  `validate_manifest.py` green; push to `tekflox/aw-app-neo4j`; fix the
  `MARKETPLACE_SYNC_TOKEN` allowlist; release → catalog; `aw-workspace-cli
  marketplace install neo4j`; generate a password, `POST
  /api/apps/neo4j/config` with `auth`; restart; **verify authenticated
  `RETURN 1` over bolt from the workspace process and paste the output**;
  `aw-workspace-cli doctor` clean.

Deferred (backlog, no cards yet — triggers in §6): backup script, per-consumer
users, a human inspection UI/tunnel story, APOC/GDS plugins (plain Cypher
covers theme-link queries; `NEO4J_PLUGINS` is a one-line manifest addition
when a consumer shows a concrete need).

Consumer wiring (research-search) is already running as card
`4025bf3b-9510-81c9-8daa-ec144a45a9b4` against §2.
