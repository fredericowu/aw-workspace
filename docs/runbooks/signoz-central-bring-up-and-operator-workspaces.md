# Runbook — bring the central SigNoz up in aw-stack, then point your own workspaces at it

**Status:** written 2026-10-06, **NOT YET EXECUTED.** Everything below is the
manual half of Phase A; the code half is pushed and CI-green.
**Authorized by:** Frederico, card `3f15bf3b-9510-81ce-98ce-e3b484fafdb9`
(approved for implementation).
**Design:** [`../design/signoz-central-aw-stack-and-connector-split.md`](../design/signoz-central-aw-stack-and-connector-split.md)
— §1 and §4 for the central, §3.1/§10 for why step 5 is manual on purpose.

Read §0 first. The inventory that section records is why this is a
*bring-up* and not the data-preserving cutover the design describes.

---

## 0. The single most important operational fact

**There is nothing to migrate.** Measured on the bare metal 2026-10-06,
before any change:

- **Zero** signoz containers, running or stopped.
- `aw-signoz-net` does not exist as a docker network.
- `data/signoz/clickhouse`, `clickhouse-logs` and `zookeeper` are all
  **empty directories**, mtime 2026-08-14. No docker volumes either.
- `aw-sandbox` / `awserv` are gone too.

So: no `-legacy` rename to do, no row count to match afterwards, no cutover
window, and **no live operator telemetry at risk**. Re-measure before acting
— do not trust these as current:

```bash
docker ps -a | grep -i signoz
docker network ls | grep signoz
du -sh /opt/agentic-workspace/data/signoz/*
```

**The one thing that is NOT empty, and the one thing you can destroy:**

```
/opt/agentic-workspace/data/signoz/sqlite/signoz.db   # 884 KB, 2026-08-12
```

That is SigNoz's own metadata — **accounts, dashboards, alert rules**. The
`aw-signoz` service bind-mounts it from exactly that path. Point that mount
anywhere else, or let the directory be recreated empty, and every login and
dashboard is silently gone while the UI comes up looking fine. Back it up
before step 3:

```bash
cp -a /opt/agentic-workspace/data/signoz/sqlite/signoz.db \
      /opt/agentic-workspace/data/signoz/sqlite/signoz.db.pre-awstack
```

## 1. Generate the ingest token and put it in `.env`

One shared token for the whole fleet. It is **not** a tenant boundary — see
design §4/§5, and §5 of this runbook for what that means in practice.

```bash
openssl rand -hex 32
```

Set it as the repo secret **`AW_OTLP_INGEST_TOKEN`** on `tekflox/aw-stack`
(Settings → Secrets and variables → Actions). The deploy merges it into
`/opt/aw-stack/.env`; it does not overwrite other keys.

**The collector refuses to start without it** — empty, or shorter than 24
chars, and `config/signoz/otelcol-entrypoint.sh` hard-exits. That is
deliberate: `AW_OTLP_INGEST_TOKEN` is `:-` in compose rather than `:?`
(because `:?` is evaluated project-wide and would fail every *other*
service's deploy), so the fail-closed check has to live in the entrypoint.
An unset token can never quietly become an open relay into your ClickHouse.

Optional: `AW_SIGNOZ_JWT_SECRET`. Leave it unset — compose defaults to the
literal the legacy container ran, which is what keeps the accounts in
`signoz.db` and their sessions valid. Setting it logs everyone out.

## 2. Verify the directories a fresh ClickHouse needs

The data directories exist and are empty on this host. On a genuinely fresh
host they must exist and be writable by the ClickHouse image's uid before
step 3, or the container crash-loops on its first write.

```bash
ls -ld /opt/agentic-workspace/data/signoz/{clickhouse,clickhouse-logs,zookeeper,sqlite,user-scripts}
ls -l  /opt/agentic-workspace/data/signoz/clickhouse-config/   # cluster.xml, memory.xml, custom-function.xml
```

`clickhouse-config/*.xml` are mounted but **not owned** by aw-stack — they
still live in the agentic-workspace data tree. `cluster.xml` hardcodes
`<host>clickhouse</host>` and `<host>zookeeper-1</host>`, which is why the
compose file restores those legacy names as **network aliases**. Do not
"clean up" either side independently.

## 3. Deploy the four services — IN THIS ORDER, one at a time

"Deploy aw-stack" (`workflow_dispatch`) on `tekflox/aw-stack`. All four are
**excluded from `all`**; name them explicitly. `up -d --no-deps` ignores
`depends_on`, so the order is yours to enforce:

| # | Service | Wait for |
|---|---|---|
| 1 | `aw-signoz-zookeeper` | healthy |
| 2 | `aw-signoz-clickhouse` | healthy — `start_period` is 60s, be patient |
| 3 | `aw-signoz-otel-collector` | healthy — **this is what creates the ClickHouse schema** |
| 4 | `aw-signoz` | healthy |

**Step 3 before step 4 is not a preference.** The collector's entrypoint runs
`migrate bootstrap / sync up / async up` (the retired `telemetry-migrator`
one-shot's job). Deploy `aw-signoz` first and you get a UI that comes up
perfectly and errors on every query — green containers, broken product.

The deploy's own health gate proves the schema really landed:

```bash
docker compose exec -T aw-signoz-clickhouse clickhouse-client -q 'EXISTS TABLE signoz_logs.logs_v2'
# must print 1
```

A collector that started before ClickHouse was ready, exhausted its retries
and exited is indistinguishable from a healthy one by any liveness probe.
That query is the difference.

**Rollback:** `docker compose stop` the four, nothing else. Nothing was
renamed and nothing was deleted, so there is no legacy state to restore — the
only destructive thing in reach is `signoz.db`, which §0 told you to copy.

## 4. The two steps the deploy does NOT do

### 4a. The public ingest hostname (`otlp.aw.tekflox.com`)

The rendered Caddyfile is generated from **the `workspace` row in Postgres**,
not from a file on disk. `aw-backend/src/config/aw.json.seed` carries the new
`custom_domains` entry, but a seed only applies to a fresh database — so the
live edge needs the row updated once, by hand.

**Do NOT round-trip through `get_config()` → `save_config()`.** `get_config`
expands `{{env.X}}` placeholders and `save_config` stores verbatim, so that
pair bakes resolved secrets into the database. Use the raw accessors:

```bash
docker exec -i aw-backend python - <<'PY'
from src.config import _load_raw_from_db, _persist_raw, invalidate_cache
from src.libs.caddy_template import regenerate_and_reload

cfg = _load_raw_from_db()
domains = cfg.setdefault("custom_domains", [])
if not any(d.get("hostname") == "otlp.aw.tekflox.com" for d in domains):
    domains.append({
        "comment": "Central SigNoz OTLP ingest (aw-signoz-otel-collector :4418, bearer auth in the collector)",
        "enabled": True,
        "hostname": "otlp.aw.tekflox.com",
        "port": 4418,
        "proxy_host": "aw-signoz-otel-collector",
        "tls": "route53",
    })
    _persist_raw(cfg)
    invalidate_cache()
    print("added")
else:
    print("already present")
print(regenerate_and_reload())
PY
```

Needs a DNS record for `otlp.aw.tekflox.com` → this host. `tls: route53`
issues via DNS-01, so the cert can be minted before the A record points
here, but ingest obviously cannot work until it does.

Note the port: **4418**, the *authenticated* receiver — not 4318, which is
the internal unauthenticated one. Pointing Caddy at 4318 publishes an open
relay into your ClickHouse.

Verify (401/403 is the **correct** answer with no token):

```bash
curl -s -o /dev/null -w '%{http_code}\n' -X POST https://otlp.aw.tekflox.com/v1/logs \
  -H 'Content-Type: application/json' --data '{}'
curl -s -o /dev/null -w '%{http_code}\n' -X POST https://otlp.aw.tekflox.com/v1/logs \
  -H "Authorization: Bearer $AW_OTLP_INGEST_TOKEN" \
  -H 'Content-Type: application/json' --data '{"resourceLogs":[]}'
```

### 4b. Restart `aw-otelcol-edge`

`otlp.aw.tekflox.com` rides the same `aw-caddy` whose access log
`aw-otelcol-edge` ships to a SigNoz, so the new hostname was added to that
agent's `filter/drop_self_ingest` drop list (design §4's named "operational
trap"). **The file is already updated on the host; it needs a restart to take
effect:**

```bash
docker restart aw-otelcol-edge
```

Do it in the same window as step 3. Without it, every connector→central POST
from every workspace generates an access-log line that gets shipped and
stored — noise for zero signal, converging but permanent.

> That config lives at `/opt/aw-stack/data/otelcol/caddy.yaml`, which falls
> under aw-stack's blanket `data/` gitignore — it exists on **one machine's
> disk** and a re-clone gets no filter at all. Open follow-up to re-home it to
> `config/otelcol/caddy.yaml`; the SigNoz collector's own config was committed
> to `config/signoz/` specifically to avoid repeating that.

## 5. Point your own workspaces at it — manually, one by one

**This is deliberately not automated, and must not be.** Design §3.1 and §10:
there is **no trustworthy signal** in aw-backend today that says "this
workspace belongs to the operator". `is_hosted` means "on our metal" (a
paying hosted customer is `is_hosted=true`), `placement_driver` is mutable by
design, and 112 of 115 `tenant_members` rows still point at the shared
`tenant-bootstrap` tenant. A wrong gate silently ships a **paying tenant's
telemetry into your ClickHouse** — a privacy breach no test would catch.

So: default-deny by construction. Nothing injects the token anywhere. A
workspace can only reach the central because a human typed the token into it.

In **each workspace you own**, and no others:

1. **Settings → Observability**
2. Mode: **Custom**
3. Endpoint: `https://otlp.aw.tekflox.com`
4. API key: the `AW_OTLP_INGEST_TOKEN` from step 1
5. Save.

There is no `central` mode and no "apply to all workspaces" button. If one
ever appears, it is a bug — read §10 of the design before building it.

Optionally install **`aw-app-signoz-connector`** from the marketplace in that
workspace. Then:

- Core exports **through** the connector instead of directly, and the
  connector forwards to whatever the setting above resolved (the save pushes
  it in and recreates the container).
- The connector stamps `workspace.slug` on everything, which is how you tell
  workspaces apart at the central.

Without the connector, core exports **straight** to the central — which also
works and is one less hop. The connector buys you `workspace.slug` enforced
at the edge for non-core senders, and one place that owns queue/retry. It
also adds a failure link: a broken connector *silences* telemetry a direct
export would have delivered.

### What `workspace.slug` is not

It is **client-set**, so anything holding this token can claim any slug. It
separates telemetry for querying; it is not a security boundary, and SigNoz
community has no per-user data scoping anyway. The real boundary is that
**tenants never get central UI access** (design §5).

Consequences to accept, written down rather than discovered:

- One leaked token rotates the **whole fleet's** ingest.
- Handing a tenant this token is the only way a tenant reaches the central.
  Don't.

## 6. Done when all of these pass

- [ ] `docker compose ps` shows all four `aw-signoz*` services healthy
- [ ] `EXISTS TABLE signoz_logs.logs_v2` → `1`
- [ ] `https://signoz.app.aw.tekflox.com` answers **for a logged-in session**
      — an unauthenticated request correctly gets **410** from aw-caddy's
      `@gated_unauth` matcher, so a bare `curl` cannot tell healthy from dead
- [ ] the pre-existing accounts/dashboards from `signoz.db` are still there
- [ ] `otlp.aw.tekflox.com` rejects an unauthenticated POST and accepts a
      bearer-authenticated one
- [ ] one fresh OTLP POST through the public hostname is queryable in the UI
- [ ] `docker logs aw-otelcol-edge` shows it restarted, and the central's own
      ingest requests are **not** appearing as logs in the `aw` workspace's
      SigNoz

## 7. Still open after this runbook

- **Does aw-backend / `aw-otelcol-edge` repoint to the central?** They ship to
  the `aw` workspace's own `aw-app-signoz` today
  (`https://signoz.app.aw.workspace.aw.tekflox.com`). Design §1 routes this to
  the **Product Owner** — it is a question about where you want to look at
  platform logs, not an architecture constraint. Until it is answered there
  are two live "central-ish" instances and dashboards split across them.
- **Phase B** — Tier-2 container-log coverage inside each workspace (design
  §9). Separate card, deliberately not in this window: its centre of gravity
  is a core framework change (new capability + volume source + log
  redirection) with a hard rollout order.
- **Per-workspace ingest tokens**, replacing the shared one (design §4), once
  the T2 tenant backfill makes `tenant_id` trustworthy enough to gate on.
