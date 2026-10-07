# SigNoz: central moves into aw-stack, and the app splits into server + connector

Status: **design approved-pending-review** — architecture-first, no code yet.
Kanban: `3f15bf3b-9510-81ce-98ce-e3b484fafdb9`. Requested by Frederico
(Telegram, 2026-10-06), evolving his 2026-07-27 decision that observability =
two apps (light connector + heavy optional local server — see Kanban backlog
card `feature:aw-app-otel-collector` and `feature:aw-app-signoz`).
Revised 2026-10-06 (same day) after Frederico's review: scope note in §2
(zero new app-server installs this phase) and new §9 (Phase B — Tier-2
container-log coverage inside the workspace). §1/§3/§4/§5 unchanged.
Revised again 2026-10-06 (rev 3, final before Coder dispatch): **the central
is operator-internal only** — §3.1's automatic env injection is REMOVED from
Phase A (no trustworthy "operator-owned workspace" signal exists today; see
§10 for the audit), `central` mode is deferred to post-T2, and seed-apps is
declined per §9.5. Phase A scope as dispatched is §6's revised list.
Rev 4 (2026-10-06, post-delivery): §2.1 added — two §2 requirements
(doctor check, `file_storage` queue) turned out to need core enablers the
app repo could not ship unilaterally; both decided, both stay requirements.
Rev 5 (2026-10-06, post-`ab7adc0`): the doctor fix shipped but the
Architect-flagged port verification came back negative — the doctor route
proxies to `runtime.port` (4318, the OTLP receiver), never the health
extension on 13133. §2.1 gains the third enabler: `contributes.doctor`
entries learn an optional `port` for a direct container dial.

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

**Scope confirmation (Frederico, 2026-10-06 review):** the full app-server
is installed in **zero** workspaces this phase — not pre-installed, not
actively offered in any Settings flow. It stays in the marketplace and
existing installs keep working, but the only thing this phase puts inside
workspaces is the connector. The `local` observability mode and the `auto`
local-first rung remain in the code for the workspaces that already have
the app; nothing new points anyone at it.

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

### 2.1 Phase A findings → core enablers (decided 2026-10-06, rev 4)

The Coder delivered the connector (v0.2.1) with two §2 items honestly
dropped, both measured live rather than inferred, both blocked by core, and
pinned the absence with regression tests. Decisions:

**Doctor check — core fix, requirement stands.** The collector's
`health_check` extension serves a JSON body (`{"ok": ...}`, pipeline-aware
via `check_collector_pipeline`) but labels it `text/plain`, and its
`response_headers` override is accepted-then-ignored upstream. Core's
`src/apps/routes.py::_app_doctor_checks` (line 238) only parses bodies whose
content-type starts with `application/json`, so the check would read as a
permanent false red. **Decision: core parses the body regardless of
content-type** — attempt `resp.json()` always; a body that genuinely isn't
JSON reports as a FAILING check with the content-type and a body snippet in
`detail` (an upgrade over today's silent `{}`), preserving the
"unreadable = failing" invariant the docstring and
`tests/unit/apps/test_capabilities.py` pin. Rejected: a second container
(violates §2 one-container), a custom image (violates stock-pinned-image),
waiting on an upstream otel-collector fix (file it, don't depend on it).

**`file_storage` queue — core learns `--user`, requirement stands.** The
stock image is `USER 10001:10001`; core `makedirs`'s `$AW_APP_DATA` as the
workspace uid mode 0755; `CAP_CHOWN` only helps images whose entrypoint runs
as root and chowns (the collector's doesn't); and the redeploy sweep rewrites
the whole mount's ownership to the workspace uid anyway
(`src/apps/containers.py:91-102`, tracked
`core:workspace-redeploy-chowns-app-data-dirs`), so any chown-to-a-declared-
uid scheme rots exactly the way aw-app-blender's PUID did. **Decision:
invert it — run the process as the uid that already owns the data.**
`_parse_run_flags` (`src/apps/containers.py:302`) learns `--user`, mapped to
the docker SDK `user` kwarg, with the value passed through `expand_value`
at `start()` time so the manifest can say
`run_flags_needed: ["--user=${data.uid}:${data.gid}"]` — `app_data_owner()`
(`containers.py:82`) already derives exactly this number for the same
reason. Register-time validation (`containers.py:659`) shape-checks only
(value required), it does not resolve placeholders. The collector is a
static Go binary with no uid assumptions, so running it as the workspace
uid costs nothing. Trust call: `--user` is NOT in `--privileged`'s class —
container-root is already what every USER-less image gets today, so the
flag can only select among identities an image author could bake in anyway;
it bypasses no capability or host-opt-in gate. Rejected: chown
`$AW_APP_DATA` to a manifest-declared uid (fights the redeploy sweep
forever, adds a manifest field + start-time machinery for a worse
invariant), accept in-memory permanently (the queue exists precisely to
survive the recreate a destination change causes — the one moment it would
be empty), a non-bind writable path (nothing durable), wrapper/custom image
(violates stock-image).

Rollout for both: **core before app** (the validator-first rule — a
marketplace connector v0.2.2 declaring `--user` against a core that rejects
unknown flags fails at register). After core ships, connector v0.2.2
re-adds the volume + `fs:workspace-data`, the `file_storage` config, the
`contributes.doctor` entry, and deletes the two pin-the-absence tests.

**Rev 5 (2026-10-06): doctor needs a port, not just a parser.** The
content-type fix shipped in `ab7adc0` and is correct, but the verification
rev 4 flagged ("confirm the routed port reaches the health extension")
came back negative, measured live: `:13133/healthz` answers
`{"ok":true,...}`; `:4318/healthz` is `404 page not found` from the OTLP
receiver. The cause is structural: `_app_doctor_checks`
(`src/apps/routes.py:240-251`) dials `loaded.drainable.app`, which for a
Tier-2 app is `ContainerReverseProxy` (`src/apps/proxy.py:89`) holding the
**one** `base_url` from `containers.py:1067-1071` —
`http://<container>:<runtime.port>`. `runtime.port` must stay 4318 because
`src/api/otel.py::ensure_export_state` dials `base_url()` to export OTLP
(§3.3, already live in `46cb560`). One proxied port, two consumers needing
different ones; the collector cannot merge them — `health_check` and the
OTLP HTTP receiver are separate listeners by upstream design.

**Decision: `contributes.doctor` entries gain an optional `port`, and a
check that declares one is dialled directly at
`http://<container-host>:<port><route>` instead of through the mounted
proxy.** Where it lands:

* `src/apps/manifest.py::doctor_checks` (line 158): parse `port` —
  coerce to `int`, accept 1–65535, anything invalid is treated as absent
  (the entry survives and dials the default path, so a manifest typo
  surfaces as a red doctor row instead of vanishing — same
  never-break-loading posture as the existing route sanitation, but
  visible). Docstring documents the field.
* `src/apps/routes.py::_app_doctor_checks`: when a check carries `port`,
  take the host from `runtime.containers.base_url(slug)` (`urlsplit`, swap
  the port — this inherits the `c.name if c.network else self._proxy_host`
  branch at `containers.py:1070` for free) and issue a real
  `httpx.AsyncClient` GET with the same `_DOCTOR_CHECK_TIMEOUT_S`. An app
  that declares `port` but runs no container (Tier-1, or `base_url`'s
  `_require` raises) reports as a FAILING check — "the app could not tell
  us" stays a red row, never a skip. **Constraint for the Coder: one
  verdict path.** Only the transport/URL may fork; the status-first /
  parse-regardless-of-content-type / dict-shape / `bool(body.get("ok"))`
  block stays single-sourced so the three literals
  `tests/unit/apps/test_capabilities.py:219-236` pins and the new
  `test_app_doctor_checks.py` suite keep covering both paths.
* `port` targets the app's **own** container, not `ui_sidecar` (the
  `proxy_key` indirection at `src/apps/runtime.py:1547-1551` is a UI
  concern). An app whose doctor endpoint lives on a sidecar is out of
  scope; a future `container` field extends this without breaking it.
* Connector manifest (v0.2.2+): doctor entry becomes
  `{label, route: "/healthz", port: 13133}`, with 13133 declared in the
  collector config's `health_check` extension as today.

Trust posture: this is *more* in the doctor docstring's spirit, not less —
the in-process ASGI dial already bypasses the host router's identity gate
(it talks to the inner mounted app), so a direct container dial on the
workspace-internal podman network crosses no boundary the current code
doesn't. Container ports are reachable within the app network regardless of
publish, so no new exposure is created.

Rejected (rev 5):

* **A second proxied port per app** (the "strictly bigger" option): touches
  `register`, `_Container`, the mount, and `base_url()`'s one-URL contract,
  and puts a health listener on the public-facing mount where it then needs
  identity-gating — framework surface nobody browses. All cost, no reach
  the direct dial doesn't already have.
* **Dropping the doctor entry and striking the §2 requirement**: the app
  whose only job is "telemetry reaches the destination", with an in-memory
  queue that drops silently on sustained outage, is the *last* app doctor
  should be mute about — §2's own rationale.
* **Repointing `runtime.port` at 13133 and hardcoding `:4318` in
  `otel.py`**: inverts the special case — the OTLP receiver *is* this
  app's main surface, and every other `base_url` consumer (status, window
  mount, Apps tile) would then target the health port.
* **Upstream-config workarounds** (serving health on the OTLP port, a
  custom image): the listeners are separate by collector design; custom
  image violates §2 stock-pinned-image, same as rev 4.

What rev 5 makes harder later: doctor traffic for ported checks is
out-of-band from the one observable proxy path, and the framework now has
a second way an app names a container port (`runtime.port` + per-check
`port`) — acceptable, both are manifest-declared facts, but a future "all
app ingress through one seam" refactor must remember doctor. Sequencing is
*soft* for this field, unlike `--user`: old core's `doctor_checks` ignores
unknown keys inside an entry, so a `port`-declaring manifest on old core
degrades to today's red row, not a register failure. It still rides the
same core-restart-then-v0.2.2 train as `--user`, which is hard-gated.

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

1. ~~New mode `central` + control-plane env injection~~ — **REMOVED from
   Phase A (rev 3, Frederico 2026-10-06: "o central signoz é só interno e
   só vou instalar ele nas minhas workspaces e que cada tenant instale o
   seu").** Automatic injection would have pointed every hosted tenant's
   telemetry at the operator's central by default — the exact opposite of
   the constraint — and §10's audit found **no trustworthy signal** to gate
   it on today. Phase A ships **zero** placement-driver changes and no new
   mode. Operator workspaces reach the central through the **existing
   `custom` mode** (`src/api/observability.py:197-206`): endpoint = the
   central ingest hostname (§1), api_key = the shared ingest token (§4),
   set by the operator in each of his own workspaces' Settings — explicit,
   per-workspace, default-deny by construction. `central` as a first-class
   auto-resolvable mode returns post-T2, gated on
   `workspace.tenant_id == <operator tenant>` (§10).
2. ~~`auto` fall-through to central~~ — **REMOVED with it** (rev 3). `auto`
   keeps today's exact meaning: local app installed → local, else nothing.
   A workspace that configures nothing sends nothing — Frederico's accepted
   outcome ("nem todo mundo vai rodar o signoz e tudo bem").
3. **Connector integration** (unchanged, still Phase A): when the connector app is installed,
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

**Decision: one shared ingest token, v1 — handed out by the operator, not
by code.** Generated once into aw-stack's `.env` (`AW_OTLP_INGEST_TOKEN`),
validated by the central collector itself via the `bearertokenauth`
extension on the OTLP receiver (collector-native, no new proxy component).
Distribution (rev 3): the operator pastes it into the `custom`-mode api_key
field of **his own workspaces only** (§3.1) — no control-plane delivery, no
driver injection, so no workspace ever holds it unless a human put it
there. aw-caddy fronts `otlp.aw.tekflox.com` with TLS only; auth stays in
the collector.

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

Phase A (this card's scope, as revised in rev 3 — this is the dispatched
list):
1. aw-stack adoption of the 4 services + awserv repoint + ingest hostname +
   shared ingest token on the central side (§1, §4). **No aw-backend
   placement-driver work** (§3.1 rev 3, §10).
2. Connector↔core integration only (§3.3): push-on-save of the resolved
   observability destination into the connector's config, and
   `ensure_export_state`'s swap to the connector's internal URL when
   installed. No new modes, no `resolve()` semantic changes.
3. `aw-app-signoz-connector` app (§2), installable from the marketplace.
4. Operator runbook step (docs, not code): set `custom` mode +
   endpoint/token in each operator-owned workspace that should ship to the
   central.

**Explicitly routed to the Product Owner, not absorbed here:** "every new
workspace comes with the connector pre-installed". There is **no
default-apps-on-provision mechanism** in aw-backend or core today (verified
by search; apps reconcile from each workspace's own desired state, which
starts empty). Building one is real scope with fleet-wide blast radius, and
Phase A.2 already delivers the *outcome* for core telemetry (it flows to
central by default) without it. **This answer was materially revised by
§9**: once the connector is also the thing that ships Tier-2 container
logs — a job `auto` mode can never cover, because core only exports its own
Python logs — a workspace without the connector has a real, permanent
observability gap, and pre-installing stops being redundant. The sharpened
recommendation to the PO is in §9.5.

## 7. What this makes harder later

* **agentic-workspace's checkout becomes load-bearing for one more aw-stack
  service family** (the `data/signoz` bind mounts). Decommissioning the
  monolith now requires a signoz data relocation card too — same list
  aw-derper/aw-headscale are already on.
* **The shared ingest token bakes in operator-trust** (§4); third-party
  tenant onboarding inherits a rotation-coupling debt until per-workspace
  tokens land.
* ~~`auto`'s meaning grows~~ — no longer true in rev 3 (`auto` keeps
  today's exact meaning). The constraint it stated survives for the
  post-T2 card though: when `central` mode returns (§10), every future
  destination must be an explicit mode, not another silent rung in `auto`,
  or the setting's "four explicit states" design rationale erodes.
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

## 9. Phase B — Tier-2 container-log coverage inside the workspace

Added 2026-10-06 after Frederico's review. The gap: every Tier-2 app
container a workspace hosts (Penpot-class apps, Crispal, Browser, Windows —
anything that isn't our own instrumented Python/JS) logs only to `podman
logs` and never reaches any SigNoz. The bare metal already solved this for
the central's own host via the filelog-on-`/hostfs` pattern
(`agentic-workspace/docs/knowledge_base/memory/docker-container-logs-filelog-signoz-2026-07-21.md`);
Frederico wants the same coverage *inside* each workspace, via the
connector. This section is analysis + decision — **no Phase B code ships
with this card** (see §9.4).

### 9.1 Measured facts the design rests on (verified live, 2026-10-06)

* Tier-2 app containers run on the workspace's podman (docker-compat API,
  `AW_CONTAINER_SOCKET` — `src/apps/containers.py` module docstring). Log
  driver reports `json-file` (podman's alias for **k8s-file**), and the
  files live at
  `<graphroot>/overlay-containers/<id>/userdata/ctr.log` — on the **podman
  host's** filesystem (measured on the `aw` workspace:
  `/home/aw-remote-host/.local/share/containers/storage/...`), not inside
  the workspace container.
* That `userdata/` directory also holds each container's **full OCI spec,
  env included** — i.e. every sibling app's secrets in plaintext next to
  the log file. Any design that mounts the storage tree leaks them.
* `src/apps/containers.py` sets **no `LogConfig` today** (grep: zero hits)
  — podman defaults apply, which also means today's `ctr.log` files grow
  **unrotated and unbounded**. Phase B fixes this as a side effect.
* **Proven this session against the live engine**: podman's docker-compat
  API honors a per-container log-path override —
  `docker run --log-opt path=/tmp/aw-test-logpath.log ...` succeeded and
  `.LogPath` reported the custom path. So the process that creates every
  Tier-2 container (core) can *choose where the log files land*.
* **Disqualifier found**: `otel/opentelemetry-collector-contrib` has **no
  receiver that reads container logs over the docker/podman API**
  (`docker_stats` is metrics-only). A vanilla collector can only tail
  files. Any socket-based log design therefore needs a custom pump
  component regardless of its permission story — the permission debate is
  moot before it starts.

### 9.2 Decision: core redirects the log files; the connector tails them

Three pieces, each on an existing seam:

1. **Core, `src/apps/containers.py`**: when creating any Tier-2 container
   (app container and every sidecar), set
   `LogConfig(type="json-file", config={"path": <logs-root>/<app_id>/<container-name>.log, "max-size": ...})`,
   where `<logs-root>` is a new core-owned directory under the workspace
   home (`paths.workspace_home()/data/container-logs/` — the same
   durable tree `$AW_APP_DATA` binds already resolve against, which is what
   guarantees the path is podman-host-resolvable). `docker logs` /
   `aw-workspace-cli logs` keep working — podman serves them from the
   recorded `LogPath`. Existing containers migrate **lazily**: the path
   changes on their next recreate (update, config save, reinstall); no
   fleet restart.
2. **Framework, `src/apps/runtime.py::_container_volumes`**: one new
   reserved volume source, `$AW_CONTAINER_LOGS`, expanding to `<logs-root>`
   **forced read-only**, gated by a **new capability**
   `observability:container-logs`. Its risk description must be honest:
   "read the stdout/stderr of every app in this workspace" — apps print
   secrets into logs, so this is *not* `fs:workspace-data`-equivalent and
   gets its own grant. The `$AW_MCP_JSON`-behind-`mcp:register-gateway`
   entry in the same function is the exact precedent for a
   single-purpose, capability-gated source.
3. **Connector, same app (`aw-app-signoz-connector`)**: a second, optional
   pipeline — `filelog` receiver with the collector's `container` parser
   (k8s-file format, *not* docker-json), include glob
   `<mount>/*/*.log*`, checkpointed via the `file_storage` extension it
   already has (§2). The volume + permission are **optional**: not
   granted → the mount is absent → the connector's entrypoint renders the
   OTLP-only config it ships in Phase A. One app, graceful degradation —
   the same shape `host_power_optional` set for device grants
   (`src/apps/hostpower.py::resolve_optional`).

### 9.3 Rejected for §9

* **Hand the connector the podman socket** (what made this look like a
  Tier-1-vs-Tier-2 reopener). Dead on two independent grounds: (a) the
  disqualifier in §9.1 — no otelcol log-over-API receiver exists, so the
  socket alone collects nothing without writing a custom pump; (b) a
  socket is *manage* power (create/exec/kill every sibling), categorically
  more than read-logs — it would drag the connector into the
  `containers:manage`/socket-proxy tier for a read-only job. The tier
  question does NOT reopen: the connector stays Tier-2.
* **Transplant the bare-metal `/hostfs` pattern** (mount the graphroot /
  `overlay-containers` read-only and glob `ctr.log`). The central
  collector gets away with it because it is operator infrastructure;
  inside a workspace this mounts every sibling's OCI spec **env vars
  included** (§9.1) into a marketplace app. Rejected outright, whatever
  capability gates it.
* **Per-file binds of each `ctr.log`**: inode pinning — the aw-stack
  README documents this bug class twice; every container recreate would
  leave the connector tailing an orphaned inode.
* **Core pumps logs itself** (stream via its existing socket client into
  its own OTel pipeline — no new capability, works with zero apps
  installed). **Runner-up, kept on file**: it is the fallback if the PO
  rejects both seed-apps and the coverage gap, because it is the only
  shape that covers container logs with no connector installed. Rejected
  as primary: unbounded log I/O, backpressure and retry land inside the
  single-event-loop core process, reinventing what the collector's
  `memory_limiter`/`file_storage`/queue already do, and tails die with
  every core restart.
* **A second app** ("signoz-logs-agent") instead of extending the
  connector: two marketplace entries forwarding to the same destination
  with one config between them; the optional-grant degradation in §9.2.3
  gets the same security isolation without the split.

### 9.4 Same card or new card — decision: new card

Phase B is a **separate card**, sequenced after Phase A, designed here so
the context lives in one doc. Three reasons: Phase A is complete and
deployable without it (nothing in §1–§6 waits on §9); Phase B's center of
gravity is a *core framework* change (new capability + volume source + log
redirection), not an observability change, and new capabilities have a
hard rollout order (core validator ships before any manifest that declares
the capability — the `core-validator-change-must-precede-the-app` lesson);
and its risk profile (touching how every Tier-2 container is created) must
not ride along inside a telemetry cutover window.

### 9.5 The seed-apps answer, revised

§6 originally said pre-installing the connector was redundant because
`auto` mode already ships core telemetry. **§9 changed the premise**:
container-log coverage is a connector-exclusive job — core's `auto` mode
exports only core's own Python logs and never will tail other containers
(that is the rejected runner-up). So without the connector, every
workspace has a permanent, silent gap exactly where the hardest debugging
happens (the apps that aren't our code).

**DECLINED by Frederico (2026-10-06, rev 3) — conditionally, not
permanently.** His reasoning, which the doc's own §5 supports: seeding the
connector fleet-wide only makes sense if its default destination is safe
for *every* workspace, and the central has no real per-tenant isolation
(`workspace.slug` is client-set; §5 says in as many words it is not a
security boundary). "Se isso não for garantido, não quero." Recorded
condition for reopening: seed-apps returns to the table **if and when the
central gets genuine per-tenant isolation** — per-tenant ClickHouse
databases or the paid SigNoz tier, the same "honest comparison" §5 already
names as the bar for any tenant read-access. Until then: the Tier-2
container-log gap (Phase B) stands, nobody installs the connector by
default, each tenant that wants observability installs their own stack
(connector → their own destination, or the full app-server), and the
central stays operator-internal (§10).

### 9.6 Phase B risks for the Coders

1. **`log-opt path` through docker-py**: proven at the engine (this
   session, via CLI) but not yet through docker-py's `LogConfig` — its
   `config` dict passes through unvalidated, but verify end-to-end,
   including that `max-size` actually rotates k8s-file on this podman
   version, before relying on either.
2. **Rotation glob**: with `max-size` set, podman renames rolled files;
   the filelog include pattern must match rotations (`*.log*`) and the
   `container` parser must be validated against a **real** `ctr.log` line
   (k8s-file ≠ docker-json — the bare-metal config's `json_parser` chain
   will NOT parse it).
3. **Self-tailing loop**: the connector's own container log lands in the
   same `<logs-root>`. Exclude it in the filelog config (`exclude:
   */signoz-connector/*` or equivalent) — a failing exporter logging
   errors that get tailed and re-exported is the same amplification loop
   `src/api/otel.py::_AMPLIFICATION_PREFIXES` exists to kill on the Python
   side.
4. **Capability rollout order**: core (validator + volume source +
   redirection) deploys first, connector manifest second — the reverse
   order fails every install with an unknown-capability validation error.
5. **Uninstall hygiene**: `<logs-root>/<app_id>/` must be cleaned by the
   journaled uninstall replay (the same mechanism that removes the
   container), or the directory accumulates logs of apps that no longer
   exist — including anything secret they printed.
6. **Path resolution is podman-host-relative**: the redirect path core
   writes must be the podman-host-visible form, not the workspace
   container's own view — use the identical resolution `$AW_APP_DATA`
   binds already go through, never a hardcoded translation.

## 10. Operator-only central: the signal audit behind rev 3's default-deny

Added 2026-10-06 (rev 3). Frederico's constraint: the central receives
telemetry **only from workspaces he owns** — never from a paying tenant's
workspace, hosted or BYOD. The question was which signal in aw-backend
says "this workspace belongs to the platform operator". Answer, after
reading the real code: **none that can be trusted today.** All three
candidates fail:

| Candidate | Where | Why it fails as the gate |
|---|---|---|
| `is_hosted` / `is_hosted_workspace()` | `aw-backend/src/api/placement/hosted_driver.py:436` | Means "a workspace on **our metal**" (durable `container_name` match) — by its own docstring it exists so a *paying hosted customer's* console badge is correct. Paying host-with-us customers are exactly `is_hosted=true`; it measures who runs the infra, not who owns the account. |
| `placement_driver == "hosted"` | `workspace` table, `db_models.py:895` block | **Mutable, by design**: `host_link.py` flips it `hosted → remote-host` the moment the outer host dials `/link` (that is why `is_hosted_workspace()` exists at all). Also fails the other direction: BYOD customers and the operator's own workspaces are *both* `remote-host`. |
| `workspace.tenant_id == <operator tenant>` | `db_models.py` (T1, live) | The right **shape**, broken **today**: 112 of 115 `tenant_members` rows still point at the shared `tenant-bootstrap` tenant (open card `identity:t2-backfill-preexisting-tenant-bootstrap-memberships`), so "operator tenant" is not yet distinguishable from almost everyone else. |

Two additional facts that killed automatic injection independently of the
signal question:

* The hosted driver's env surface is a fixed 3-key dict for the **outer**
  container (`hosted_driver.py:523`); reaching the **nested** workspace's
  core process goes through aw-remote-host's `EnvPassthrough` — a fixed
  allowlist in Go + `bootstrap/workspace/install.sh`, in a different repo.
  "Just inject two env vars" was never a one-repo change.
* The failure mode of a wrong gate is **silent tenant telemetry flowing to
  the operator's ClickHouse** — a privacy breach that no test in aw-backend
  would catch, discovered only if a tenant audits their egress.

**Decision: default-deny, no gate at all in Phase A.** Nothing in
aw-backend decides who may ship to the central, because nothing injects
the credential anywhere — the operator pastes endpoint+token into the
existing `custom` mode (§3.1 rev 3) in his own workspaces, one by one.
A paying tenant can only reach the central by the operator deliberately
handing them the token, which is the correct bar.

**The automated path, when it becomes safe:** after the T2 backfill makes
`tenant_id` trustworthy, reintroduce `central` mode with injection gated on
`workspace.tenant_id == <operator tenant id>` (an aw-stack/.env-configured
id, not a hardcoded slug list), plus the aw-remote-host `EnvPassthrough`
leg for hosted/BYOD-shaped placements. That future card should also revisit
§4's shared token → per-workspace tokens, since the same control-plane
delivery it builds is what per-workspace credentials need. Runner-up
rejected for the interim: an `AW_OTLP_CENTRAL_WORKSPACES` slug allowlist in
aw-backend's env — it duplicates ownership truth into a hand-maintained
list that silently drifts from the tenant table T2 is about to make real.

## 11. Query MCP + web UI, central-side (implemented, card 3f25bf3b...)

Closes the gap §10 left standing: a connector-only workspace could forward
telemetry to the central but had no way to **query** it or even **see** it
— only a full `aw-app-signoz` install got query tools, and only for its
own local instance. Shipped:

**aw-stack** — `aw-signoz-mcp-server` (`signoz/signoz-mcp-server:v0.14.0`,
the same image `aw-app-signoz`'s own `mcp` sidecar runs, pointed at this
central's `aw-signoz` instead), public at `signoz-mcp.aw.tekflox.com`
(`custom_domains`, same TLS mechanism as §4's `otlp.aw.tekflox.com`). Gated
at the edge by a Caddy `forward_auth` subrequest to `aw-signoz:8080`'s
`GET /api/v1/service_accounts/me` — closes the "tools/list with no auth at
all" residual named when this section was still a comment thread: any
request without a valid `SIGNOZ-API-KEY` never reaches the MCP server.
`aw-signoz-mcp-provisioner` (`config/signoz/mcp_provisioner.py`, a sibling
of `aw-app-signoz`'s own provisioner against the identical v0.128.0 API)
mints a **viewer**-role key — never `signoz-admin` — and writes it to a
file (`./data/signoz-mcp/query_api_key`) instead of POSTing anywhere, since
there is no per-workspace app here to own the result. Same default-deny
distribution as §4's ingest token: nothing injects this key anywhere, an
operator reads the file and pastes it by hand.

**aw-backend** — `custom_domains` entries gain an optional
`forward_auth: {host, port, uri}` key (`src/libs/caddy_template.py`),
rendered before the entry's own `reverse_proxy`. Generic — any future
`custom_domains` entry with an upstream that has no request-level auth of
its own can opt in the same way.

**aw-app-signoz-connector** (0.3.0) — three new MANAGED config fields
(`query_mcp_url`, `query_api_key`, `web_ui_url`), pushed by the same
`observability.py::_push_connector_config` seam that already manages
`endpoint`/`api_key`. A new `mcp.template.json` (`type: http`, precedent:
`aw-app-browser` — no sidecar) renders a `signoz-query` MCP server pointed
at `${config.query_mcp_url}`; empty disables it (the same
`mcp_template.py` seam `aw-app-signoz` already proves) rather than
connecting and 401ing. A new declarative window uses the existing `iframe`
widget, `src: "${config.web_ui_url}"`, for the destination's own web UI —
**no start/stop/restart control of the destination**, by construction: the
widget is a plain iframe, and the window's own lifecycle controls (if any)
govern the *connector's* container, never the remote one.

**aw-workspace core** — `observability.py`'s `custom` mode gained the same
three fields, all optional (unlike `endpoint`, which stays required). Only
`custom` resolves them: `auto`/`local` point at this workspace's own
`aw-app-signoz`, which already serves its own query tools locally and has
nothing here to push. `web_ui_url` falls back to `query_mcp_url` at PUSH
time (not in the connector's schema default, not persisted) so a pasted
`query_mcp_url` with no `web_ui_url` still gets a working window rather
than an empty iframe. `src/apps/runtime.py::_resolve_window` gained
`${config.x}` expansion for declarative window specs — the one piece that
was missing for ANY app to put a managed config value into a window with
no backend route of its own. **The risk this section exists to flag**:
`spec_data` is served straight to the SPA with no further auth check, so
the expansion explicitly refuses any placeholder naming an `x-secret: true`
config key (`query_api_key` chief among them) — pinned by
`src/tests/unit/apps/test_window_spec_resolution.py`. Do not relax that
without re-reading why it's there.

**aw-workspace-ui** — the generic `iframe` widget gained an empty-`src`
guard (`AppWindow.jsx`, mirroring `app_iframe`'s own), so an unconfigured
`web_ui_url` hides the window instead of loading the SPA's own document
into itself.

### Operator runbook — order matters

Pasting a value before its target exists is harmless to resolve (empty
stays empty) but pointless to retry — the field just sits there wrong
until redone. Sequence, one step at a time:

1. **Deploy aw-stack first.** `aw-signoz-mcp-server` and
   `aw-signoz-mcp-provisioner` (`Deploy aw-stack` workflow,
   `services: "aw-signoz-mcp-server aw-signoz-mcp-provisioner"`), after
   `aw-signoz` itself is already healthy — see that repo's README "Deploy
   order" section for the full 1–6 sequence. Confirm
   `https://signoz-mcp.aw.tekflox.com/mcp` answers through the edge (any
   HTTP status is fine as a liveness check; a 401 with no key is correct).
2. **Deploy aw-backend** with the `forward_auth` support (manual deploy —
   this repo has no auto-deploy-on-push). Without this live, step 1's
   hostname is reachable but ungated — an anonymous caller could still get
   `tools/list`.
3. **Mint or read the viewer key.** `AW_SIGNOZ_ROOT_EMAIL`/
   `AW_SIGNOZ_ROOT_PASSWORD`/`AW_SIGNOZ_ROOT_ORG_ID` set in aw-stack's
   `.env` → the provisioner writes `./data/signoz-mcp/query_api_key`
   automatically. Unset → create a viewer-role service account key by hand
   through the central SigNoz's own UI instead (Settings → API Keys /
   Service Accounts).
4. **Only now** paste `query_mcp_url`
   (`https://signoz-mcp.aw.tekflox.com/mcp`) and `query_api_key` (the file
   from step 3) into each workspace's Settings → Observability → Custom.
   `web_ui_url` is optional (falls back to `query_mcp_url`'s value) but
   worth setting explicitly to the central's own UI host
   (`https://signoz.app.aw.tekflox.com`) for a real UI rather than a
   fallback that happens to load.

An empty `query_mcp_url`/`query_api_key` never breaks anything — the
connector's upstream just stays disabled and the window stays hidden, the
same safe-by-default shape §4's ingest token already has. The only way to
get this order wrong in a way that *looks* broken is step 4 before step 1:
a pasted URL that 404s/refuses because the hostname doesn't exist yet.
