# SigNoz: central moves into aw-stack, and the app splits into server + connector

Status: **design approved-pending-review** — architecture-first, no code yet.
Kanban: `3f15bf3b-9510-81ce-98ce-e3b484fafdb9`. Requested by Frederico
(Telegram, 2026-10-06), evolving his 2026-07-27 decision that observability =
two apps (light connector + heavy optional local server — see Kanban backlog
card `feature:aw-app-otel-collector` and `feature:aw-app-signoz`).

## The decision, in five sentences

1. The central SigNoz (today 4 ad-hoc containers on the bare metal driven by
   `agentic-workspace/docker-compose.signoz.yml`) moves into
   `repos/aw-stack/docker-compose.yml` using the exact aw-derper/aw-headscale
   adoption playbook: same container names, same bind mounts, zero bytes
   copied, `docker rename` (never `rm`) for rollback.
2. The otel-collector loses `network_mode: container:aw-sandbox` (the banned
   legacy pattern) and becomes a normal service with its own networks; awserv
   repoints its already-wired-but-dead OTLP exporter at it via a pinned IP on
   `aw-shared-net`.
3. `aw-app-signoz` stays exactly as it is and becomes the "application
   server" option; a **new** single-container Tier-2 app
   `aw-app-signoz-connector` (plain `otel/opentelemetry-collector-contrib`,
   no ClickHouse, no UI) is the light option.
4. The destination choice is NOT a new settings schema: it extends the
   existing Observability setting (`src/api/observability.py`, modes
   auto/off/local/custom) with a fifth mode, **`central`**, and `auto` gains
   a fall-through: local app installed → local; else central configured →
   central; else nothing. That single setting remains the one source of
   truth; the connector's container env is pushed from it.
5. Connector→central auth is **one shared ingest token** validated by the
   central collector itself (`bearertokenauth` extension), handed to
   workspaces by the control plane at provision time; `workspace.slug` (the
   resource attribute every sender already stamps) separates workspaces at
   query time. This is explicitly an operator-trust model, not a tenant
   security boundary — the upgrade path is named in §6.

Everything below is the where, the rejected alternatives, and the costs.

---

## 1. Central SigNoz → aw-stack (data preserved)

### What exists today (read, not assumed)

`repos/agentic-workspace/docker-compose.signoz.yml` defines 6 services:
`zookeeper-1`, `init-clickhouse` (one-shot), `clickhouse`,
`telemetry-migrator` (one-shot), `signoz`, `otel-collector`. Data is bind
mounts under `${AGENTIC_WORKSPACE_DIR}/data/signoz/` (clickhouse, zookeeper,
sqlite, configs). The UI is `signoz.app.aw.tekflox.com` → aw-caddy →
`aw-signoz:8080` over `agentic-workspace_default`. The collector runs
`network_mode: "container:aw-sandbox"` (compose line 150) so awserv can POST
`localhost:4318` — the exact pattern aw-stack exists to kill — and reaches
ClickHouse via a **host-published 0.0.0.0:9000** (line 82), the same
exposure class as the 2026-08-31 public-Postgres finding.

Known live-state caveat: the 2026-09-26 investigation (card
`observability:awserv-otlp-log-exporter-points-at-dead-endpoint`) found
**nothing listening on 4318 inside the aw-sandbox netns** — the collector
and the one-shots are not restart-managed and historically die on host
restarts (see `agentic-workspace/docs/knowledge_base/memory/signoz-otel-collector-migration-recovery-2026-07-12.md`).
**First implementation step is a live inventory on bare-metal-privileged**
(`docker ps -a | grep signoz` + row counts), not trusting the compose file.

### Target shape in `repos/aw-stack/docker-compose.yml`

Four long-lived services added, following the file's own precedents:

| Service | Keeps `container_name` | Networks | Notes |
|---|---|---|---|
| `aw-signoz-zookeeper` | yes | `aw-signoz-net` | unchanged otherwise |
| `aw-signoz-clickhouse` | yes | `aw-signoz-net` | **drop the `9000:9000` host publish** (see below) |
| `aw-signoz` | yes | `aw-signoz-net` + `agentic-workspace_default` | Caddy route untouched — same name, same network |
| `aw-signoz-otel-collector` | yes | `aw-signoz-net` + `aw-shared-net` (pinned IP) + `aw-stack-net` | the real change — see below |

* **Bind mounts stay at `${AGENTIC_WORKSPACE_DIR:-/opt/agentic-workspace}/data/signoz/...`.**
  This is the aw-derper/aw-headscale decision verbatim (aw-stack README,
  "Moved without moving a single byte"): no copy step inside the cutover
  window, rollback = `docker rename` back and both sides read identical
  bytes. The cost — agentic-workspace stays a *data* dependency of aw-stack —
  is already accepted and documented for headscale/derper; relocating the
  bytes to `/opt/aw-stack/data/` is a separate follow-up card for when the
  monolith checkout is retired, not part of this cutover.
* **Container names kept** for the same reason aw-caddy keeps its name: the
  generated Caddyfile proxies `aw-signoz:8080` by docker-DNS name, and the
  recovery memory above shows ops muscle-memory (`docker logs aw-signoz-*`)
  exists. Legacy containers are renamed `-legacy`, never removed, until the
  new ones are verified.
* **`aw-signoz-net` is declared `external: true`** in aw-stack. It already
  exists on the host without compose labels, and the 2026-07-12 recovery
  memory documents that compose refuses to manage it otherwise (the
  `docker-compose.signoz.override.yml` workaround exists for exactly this).
  aw-stack adopting it as external sidesteps the labels problem entirely.
* **One-shots fold into entrypoints** instead of being ported as compose
  one-shot services: `aw-app-signoz`'s own
  `container/otelcol/entrypoint.sh` already proved migrate-then-serve
  (migrations are idempotent; a re-applied migration is a no-op). aw-stack's
  deploy does per-service `up -d --no-deps`, which has no good story for
  `depends_on: service_completed_successfully` chains — an entrypoint that
  migrates then serves removes the ordering problem instead of managing it.
  `init-clickhouse` (histogram-quantile download) becomes part of the same
  pattern or a documented manual bootstrap step; it is non-fatal by its own
  comment.
* **Deploy workflow**: the four services are added as named choices and
  **excluded from `all`**, same as aw-derper — ClickHouse data is a
  singleton. The post-deploy check is an *identity* check per the README
  doctrine: row count on `signoz_logs.logs_v2` (or equivalent) must match
  the pre-cutover snapshot, UI answers on the public hostname, and one fresh
  OTLP POST shows up — not merely "containers are Up".

### The otel-collector, specifically

* Drops `network_mode: container:aw-sandbox`. Gets:
  * `aw-signoz-net` — reaches ClickHouse as `tcp://clickhouse:9000` by real
    docker DNS (the `host.docker.internal:9000` hack and therefore the
    **host-published 9000 die together**; before deleting the publish,
    grep the host for other `:9000` dialers — the implementation must
    verify nothing else grew a dependency on it).
  * `aw-shared-net` with a **pinned `ipv4_address`** — this is how awserv
    (inside the aw-sandbox netns, which has `dns: 1.1.1.1` and therefore
    *no docker DNS*) reaches it. Same reasoning, same network as the
    `aw-postgres` 172.23.0.3 pin.
  * `aw-stack-net` — the redis receiver repoints from the legacy address to
    `aw-redis:6379` (aw-redis has lived here since 2026-09-28).
* Keeps `user: "0:0"`, `/var/run/docker.sock:ro`, `/:/hostfs:ro` and the
  docker_stats/hostmetrics/filelog receivers — they are host-level, not
  netns-level, and work identically from any compose project on this host.
* awserv's env changes `OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318`
  → `http://<pinned-ip>:4318`. This closes the standing backlog card
  `observability:awserv-otlp-log-exporter-points-at-dead-endpoint` as a side
  effect of the move; note it on that card rather than doing separate work.
* Gains the ingest-auth extension for external connectors (§4) and a new
  public hostname (`otlp.aw.tekflox.com`, via a `custom_domains` entry in
  `src/config/aw.json` → regenerated Caddyfile → `otel-collector:4318`).
  The OTLP/HTTP paths are `/v1/{traces,metrics,logs}` as standard; gRPC is
  not exposed publicly, mirroring the per-workspace app's decision.

### Rejected for §1

* **Copying ClickHouse data into aw-stack-owned volumes during the cutover.**
  Rejected for the same reasons the headscale DB copy was: a copy step
  inside the window that also carries the operator's observability, and a
  rollback that reconciles two divergent copies. Relocation is its own card,
  after the dust settles.
* **Keeping the collector in the sandbox netns "for now".** It is the single
  named anti-pattern in the request, it is why the collector silently died
  across host restarts, and the pinned-IP-on-shared-net pattern is already
  proven by the Postgres cutover.
* **Making the `aw` workspace's own `aw-app-signoz` the central** (it is
  live today at `signoz.app.aw.workspace.aw.tekflox.com`, already receiving
  aw-backend's logs and aw-caddy's access log via `aw-otelcol-edge`).
  Tempting because it exists and its auth works — rejected because it ties
  fleet-wide telemetry to one workspace's app lifecycle (every app update or
  workspace recreate is a fleet telemetry outage), and Frederico explicitly
  asked for aw-stack. **Open follow-up for the PO**: once the central is in
  aw-stack, do aw-backend/`aw-otelcol-edge` repoint to it, or keep shipping
  to the `aw` workspace's instance? Both can coexist; consolidating is a
  product call about where Frederico wants to look at platform logs, not an
  architecture constraint.

## 2. The app split

**`aw-app-signoz` does not change.** It is already exactly what the request
calls the "application server": ClickHouse + query-service/UI + embedded
otel-collector + MCP sidecar, isolated per workspace
(`repos/aw-app-signoz/aw-app.json`, tier `container`, 6 sidecars). Keeping
its embedded collector means **existing installs need no migration, no
downtime, no data move** — the "migração de quem já tem" requirement is
satisfied by not creating the problem. Only its marketplace description
should be retouched to say "full server; if you only want to forward
telemetry to a central SigNoz, install the connector instead".

**New app: `aw-app-signoz-connector`** (manifest id `signoz-connector`, own
public repo per the apps-public policy; remember the two-GitHub-allowlists
trap for new app repos).

* **Tier-2 container app, one container**, image
  `otel/opentelemetry-collector-contrib` (pinned tag — the same image
  aw-stack's `aw-otelcol-edge` already runs, proven with `otlphttp` +
  `${env:...}` expansion). *Not* `signoz/signoz-otel-collector`: the
  connector never touches ClickHouse, so the SigNoz fork's exporters and its
  migrate-on-boot machinery are dead weight.
* Pipeline: `otlp` receiver (4317 gRPC + 4318 HTTP, reachable only inside
  the workspace's app network) → `memory_limiter`, `batch`, `resource`
  processor upserting `workspace.slug` (defense against in-workspace senders
  forgetting it; everything through this connector *is* this workspace) →
  `otlphttp` exporter with endpoint/headers from env, plus a
  `file_storage`-backed `sending_queue` on a `$AW_APP_DATA` volume so an
  unreachable destination buffers across container recreates instead of
  dropping.
* Env comes from config the framework way:
  `OTLP_UPSTREAM_ENDPOINT=${config.endpoint}`,
  `OTLP_UPSTREAM_HEADER_VALUE=${config.api_key}` — and
  `src/apps/routes.py::_apply_runtime_config` already recreates the
  container when a config save changes resolved env, so "how does the
  connector read config at runtime" has a one-word answer: restart, by the
  existing seam. No polling, no custom reload endpoint.
* **No public window, no nginx gate, no public ingest in v1.** Its consumers
  are in-workspace (core, apps). The full app needed nginx+envsubst because
  it exposes OTLP publicly with the app-level identity gate off; the
  connector exposes nothing. A `contributes.doctor` route (exporter queue
  health via the collector's own health/zpages) is the one contribution
  besides the skill.

### Rejected for §2

* **Tier-1 in-process connector.** A collector is a long-lived listener with
  its own ports, buffering and backpressure; Tier-1 is in-process Python
  with no container, and Tier-1 code reload requires a core restart (known
  operational memory). Wrong tier on every axis.
* **Stripping the embedded collector out of `aw-app-signoz` and making the
  connector its mandatory ingest front.** Breaks every existing install for
  zero benefit and couples the two apps' lifecycles; contradicts the
  2026-07-27 two-apps decision where the heavy app is self-contained.
* **One app with a "mode" config (full vs connector).** A mode flip that
  creates/destroys a ClickHouse is a data-loss UI affordance; two apps make
  the heavy/light choice an install-time fact with honest
  `resource_estimate`s (~6 GB vs ~256 MB).

## 3. Settings: extend Observability, don't invent Integrations schema

The request says "Settings > Integrations", but the codebase already has the
exact seam with four of the five needed states:
`src/api/observability.py` (modes `auto/off/local/custom`, stored in the
`settings` KV table, served at `/api/settings/observability`, UI in
`aw-workspace-ui/src/components/ObservabilityTab.jsx`) and
`src/api/otel.py` (process-global OTel graph, **endpoint swappable at
runtime with no restart** via `ensure_export_state`). Designing a second
schema beside it would be the two-sources-of-truth bug by construction.

Changes:

1. **New mode `central`** in `MODES` + resolve/update: endpoint + api key
   come from control-plane-injected workspace env
   (`AW_OTLP_CENTRAL_ENDPOINT`, `AW_OTLP_CENTRAL_KEY` — injected by
   aw-backend's placement drivers at provision/recreate, the same mechanism
   that already delivers `AW_WORKSPACE_API_KEY`/`AW_CONTROL_PLANE`; exact
   injection point for the coder: the env assembly in
   `aw-backend/src/api/placement/docker_driver.py` / `hosted_driver.py` /
   `remote_host_driver.py`). A workspace without those env vars simply
   doesn't offer `central` (`central_available: false` in the GET payload,
   same pattern as `local_available`).
2. **`auto` gains a fall-through**: local signoz app installed → local
   (unchanged, existing installs keep exactly today's behaviour); else
   central available → central; else nothing. This single line is what makes
   "default por workspace = manda pro central" true for every workspace that
   never touched the setting — **without** requiring the connector to be
   installed, because core's dynamic exporters can ship direct (they already
   do, to whatever `resolve()` returns).
3. **Connector integration**: when the connector app is installed,
   `ensure_export_state` swaps the core's own export target to the
   connector's internal container URL — the exact swap `otel.py:144` already
   does for the local app — and the observability PUT handler additionally
   pushes the resolved destination into the connector's app config (which
   recreates its container via `_apply_runtime_config`). The observability
   setting stays the single source of truth; the connector's config fields
   are managed values, documented as such in its `config_schema`
   (`provision_status` in the signoz app is the precedent for
   framework-written config fields).
4. **UI**: ObservabilityTab grows the "Central" radio (shown only when
   available) and a line saying where telemetry is flowing right now.
   Whether that tab is renamed/moved under "Integrations" is a UI-polish
   call for the UX Coder, not load-bearing; the scoped-API-keys work already
   put an IntegrationsTab beside it, so co-locating is cheap either way.

### Rejected for §3

* **Connector reads the observability setting itself over the loopback API**
  (the signoz provisioner-sidecar pattern). Works, but needs polling or a
  manual restart to notice changes; push-on-save through the existing
  config-save→recreate seam is already built and already tested.
* **Making `central` the stored default mode.** `auto` with a fall-through
  degrades gracefully in every direction (app installed later, central env
  appearing later, BYOD workspace with no control plane) and never persists
  a choice the user didn't make — the same reasoning that made `auto` the
  default over `local` in the original design.

## 4. Connector → central auth

`X-Api-Key` (docs/app-workspace-api-auth.md) is validated **by each
workspace's own core** in front of its own app routes — the central is not a
workspace, so that credential cannot be "reused" there in any meaningful
sense; a workspace key means nothing to aw-stack.

**Decision: one shared ingest token for the whole fleet, v1.** Generated
once into aw-stack's `.env` (`AW_OTLP_INGEST_TOKEN`), validated by the
central collector itself via the `bearertokenauth` extension on the OTLP
receiver (collector-native, no new proxy component), and distributed to
workspaces by aw-backend as `AW_OTLP_CENTRAL_KEY` (§3.1). aw-caddy fronts
`otlp.aw.tekflox.com` with TLS only; auth stays in the collector.

Why shared-token is enough *today*: every workspace in this fleet belongs to
the operator; the central UI is operator-only; the threat a per-workspace
credential defends against (one workspace impersonating/polluting another's
slug, or needing revocation without rotating everyone) is an intra-operator
non-threat right now. Why it's **named as a debt**: the day third-party
hosted tenants default to the central (§6), shared-token means one leaked
tenant env rotates the entire fleet's ingest. The upgrade path is
per-workspace tokens minted by aw-backend at provision and materialized into
the collector's auth config — the shape of §3.1's env injection doesn't
change, only the value becomes per-workspace, which is why shipping shared
first doesn't paint us into a corner.

Operational trap to carry over: the anti-recursion lesson from
`aw-otelcol-edge` (aw-stack compose, `data/otelcol/caddy.yaml`) — the
central ingest path now passes through the same aw-caddy whose access log is
shipped to a SigNoz; the filter that stops ingest-of-the-ingest must cover
the new hostname.

## 5. Multi-tenant isolation in the central ClickHouse

Direct answer to the question asked: **`workspace.slug` as a resource
attribute is sufficient for query-time separation, and is NOT a security
boundary — and that is acceptable for this design's scope.** It is client-set
(core stamps it in `src/api/otel.py::_build_resource` from `AW_WORKSPACE`;
the connector upserts it in §2), so any sender can claim any slug. The only
reader of the central UI is the operator; SigNoz community edition has no
per-user data scoping anyway, so *no* attribute scheme could turn the shared
UI into a tenant boundary. The real boundary is: **tenants never get central
UI access** — their queryable observability is the per-workspace app.

What would make it insufficient, so the trigger is written down: offering
tenants any read path into the central (dashboards, alerts, an MCP upstream
pointed at central). That requires per-workspace ingest credentials with
**server-side** slug stamping (per-token pipelines or an auth-aware fronting
hop — vanilla collector cannot rewrite resources from auth identity), and at
that point per-tenant ClickHouse databases or the paid SigNoz tier are the
honest comparison. Out of scope now, deliberately.

## 6. What ships when, and what is the PO's call

Phase A (this card's scope):
1. aw-stack adoption of the 4 services + awserv repoint + ingest hostname +
   shared token (§1, §4).
2. `central` mode + `auto` fall-through + env injection in aw-backend (§3).
   At the end of Phase A every untouched workspace is already shipping core
   telemetry to the central — the requested default outcome — with no new
   app installed.
3. `aw-app-signoz-connector` app + push-on-save integration (§2, §3.3),
   installable from the marketplace.

**Explicitly routed to the Product Owner, not absorbed here:** "every new
workspace comes with the connector pre-installed". There is **no
default-apps-on-provision mechanism** in aw-backend or core today (verified
by search; apps reconcile from each workspace's own desired state, which
starts empty). Building one is real scope with fleet-wide blast radius, and
Phase A.2 already delivers the *outcome* (telemetry flows to central by
default) without it. If the PO wants the connector container itself to be
the default vehicle (e.g. for future container-log tailing inside
workspaces), that's a new card for a seed-apps mechanism.

## 7. What this makes harder later

* **agentic-workspace's checkout becomes load-bearing for one more aw-stack
  service family** (the `data/signoz` bind mounts). Decommissioning the
  monolith now requires a signoz data relocation card too — same list
  aw-derper/aw-headscale are already on.
* **The shared ingest token bakes in operator-trust** (§4); third-party
  tenant onboarding inherits a rotation-coupling debt until per-workspace
  tokens land.
* **`auto`'s meaning grows**: it was "local or nothing", it becomes a
  two-step fall-through. Every future destination (a second central? a
  partner's collector?) must become an explicit mode, not another silent
  rung in `auto`, or the setting's "four explicit states" design rationale
  erodes.
* **Two live "central-ish" instances during/after Phase A** (the aw-stack
  central and the `aw` workspace's app that aw-backend currently ships to).
  Until the PO decides §1's consolidation question, dashboards split across
  them — confusion documented in advance rather than discovered.
* **Core→connector→central double-hop** adds a failure link for workspaces
  with the connector installed; mitigated by the file-backed queue, but a
  misconfigured connector now *silences* core telemetry that direct export
  would have delivered. The doctor contribution in §2 exists for exactly
  this.

## 8. Risks for the Coders (the non-obvious breakage)

1. **Don't trust the compose file as live-state.** The collector and
   one-shots have a history of being dead while the UI is green
   (2026-07-12 memory). Inventory + ClickHouse row-count snapshot *before*
   touching anything; the snapshot is also your §1 identity check.
2. **`aw-signoz-net` has no compose labels** — any compose command that
   tries to own it fails; it must be `external: true` in aw-stack (the
   override file in agentic-workspace is the proof this bites).
3. **Dropping clickhouse's `9000:9000` host publish**: grep the bare metal
   for other dialers of `host.docker.internal:9000` / `127.0.0.1:9000`
   first. The compose comment says the collector is the reason it exists,
   but comments drift.
4. **awserv repoint must be a literal IP** (`aw-shared-net` pin) — the
   sandbox netns has no docker DNS (`dns: 1.1.1.1`); a hostname silently
   never resolves, and the exporter fails quietly by design.
5. **Version-coupled migrations**: the collector image tag and the
   ClickHouse schema travel together (`migrate sync check` gates serving).
   Fold migrate-then-serve into the entrypoint (app precedent:
   `repos/aw-app-signoz/container/otelcol/entrypoint.sh`) and keep the
   collector's fail-loud healthcheck (`/dev/tcp/localhost/13133`) — the
   silent-retry variant caused the 2026-07-24 multi-minute invisible outage.
6. **Caddy changes go through the generator**, not the Caddyfile: the new
   `otlp.` hostname is a `custom_domains` entry in aw.json; hand-editing the
   rendered file is overwritten on the next regen, and remember the
   inode-pinning rule (directory mounts only).
7. **`expand_env` drops unresolved `${config.x}`** — the connector's
   manifest env must have schema defaults for every referenced key
   (`config_with_defaults` applies them), or a never-saved config yields a
   collector with *no* exporter endpoint that exports nothing and logs
   little. The CRISPAL_SITE_URL incident comment in `runtime.py:1468` is the
   cautionary tale.
8. **Tests that touch export targets hang CI when they fall back to public
   URLs** — `otel.py`'s own docstring records the 25-minute CI stall. The
   `central` mode resolution must keep the "no runtime → export nothing"
   convention, never "no runtime → try the public endpoint".
9. **OTLP through edges**: workspace tunnels cut at 30s and strip
   content-encoding; the central ingest path deliberately rides aw-caddy
   directly (not a workspace tunnel), and `aw-otelcol-edge` proves that path
   end to end. Don't "simplify" the connector's egress onto a workspace
   tunnel URL.
