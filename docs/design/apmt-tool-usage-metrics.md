# AP-MT tool-usage metrics: one endpoint + one MCP tool

**Status:** design (card `40f5bf3b-9510-81a3-9c50-d9cd45222b5f`, Architect, 2026-11-05)
**Ask (Frederico, Telegram 2026-11-05):** extract tool-usage metrics from AP-MT, tenant-isolated — tool call count, detailed per-call list (session, agent, duration, timestamp), date filter, `count_only` as a parameter (not a second tool/endpoint), `limit` default 20 + offset for pagination.

## 1. The decision

Add **one new endpoint, `GET /api/runs/tool-metrics`**, in AP-MT's `backend/app/api/runs.py`, enumerating `tool_call` run-events joined to their (tenant-filtered) parent `runs` row, with `count_only`, date-range, `limit`/`offset` params — and **one new MCP tool, `tool_usage_metrics`**, in `aw-app-agents-platform-runners/agents_platform_runners_app/mcp_server.py`, a thin proxy over it. No payload text is ever returned (tool *name*, yes; tool *input/output*, never).

### Relation to `docs/design/apmt-execution-history-search.md` (card `3fb5bf3b…`, 2026-10-16) — separate endpoint, shared spine

Read in full. That design's `GET /api/runs/search-events` is a **discovery** surface: substring/filters over events, grouped-by-run pointers, and it deliberately **rejects a bare enumeration** ("at least one of `q`/`tool_name`/`kind` + a selective filter is required"). This card is the opposite contract: a bare, paginated **enumeration** ("all tool calls last Tuesday, newest first, page 3") with per-call duration — exactly the query search-events refuses, with a per-call response shape (rows + duration) search-events doesn't have (grouped run pointers, no duration, no offset).

So: **not an extension of search-events** — folding them means either breaking its no-bare-enumeration rule or carrying a `view=calls|runs` mode whose params half-apply per mode. Also practical: search-events is still unbuilt; absorbing it here would silently expand this card's scope by a whole other card (that's the PO's call, not mine).

What this card **inherits from it verbatim**, so the two ship as siblings, not rivals:

- the tenancy pattern (§3 below — trap 3, resolve tenant through `runs`);
- the no-payload-text rule (its §5): this endpoint returns metadata only, so AP-MT needs no redaction patterns and there is no bulk-harvest primitive;
- two of its three DDL indexes (`ix_run_events_ts`, the partial tool-name expression index — §4), so when search-events lands it finds them already there;
- the sqlite-CI and `statement_timeout` discipline (its §11).

Runner-up, recorded: one endpoint with `group_by_run`/`view` modes serving both cards. Rejected for the param-combinatorics above and because the cards have independent lives; revisit only if a third "query run_events" card appears.

## 2. Endpoint spec

`GET /api/runs/tool-metrics` — **must be registered ABOVE `get_run` in `runs.py`**: the `GET /{run_id}` catch-all at `backend/app/api/runs.py:106` matches the literal string `tool-metrics` first otherwise (FastAPI resolves in registration order) and the new route would 404 as "run not found".

Query params (all optional unless noted):

| param | type | default | semantics |
|---|---|---|---|
| `count_only` | bool | `false` | `true` → aggregate only, no rows |
| `tool_name` | str | — | exact match on `payload->>'name'` of `tool_call` events |
| `agent_slug` | str | — | match on `Run.source_slug` |
| `session_id` | str | — | match on `Run.session_id` (`models.py:564`) |
| `since` | datetime | — | `RunEvent.ts >= since` (ISO8601; events are the unit, so filter on event ts, not `runs.ended_at` — a 3-hour run's calls land on the hour they happened) |
| `until` | datetime | — | `RunEvent.ts < until` |
| `limit` | int | **20** | page size, `ge=1 le=100` (detailed mode only) |
| `offset` | int | 0 | `ge=0` (detailed mode only) |

Response, `count_only=true`:

```json
{"count_only": true, "count": 294813,
 "by_tool": [{"tool_name": "aw__kb__search_knowledge_base", "count": 51230}, ...]}
```

`by_tool` is one GROUP BY over the same filtered set, capped at the top 50 names. It is the one thing here beyond the literal ask ("tool call count") — included because a count endpoint that can't say *which* tools is half a metric; it adds no params and one bounded query. If the PO wants it cut, cutting it changes nothing else.

Response, `count_only=false`:

```json
{"count_only": false, "count": 294813, "limit": 20, "offset": 0,
 "calls": [
   {"ts": "2026-11-05T09:14:02", "tool_name": "aw__kanban__get_kanban_card",
    "run_id": "…", "session_id": "…", "agent_slug": "coder-sonnet",
    "duration_ms": 1842, "is_error": false}
 ]}
```

`count` is the total matching rows (so callers can paginate); `calls` ordered `ts DESC`. **Never in the response:** `payload['input']`, `payload['content']`, run input/output — the §5 rule of the sibling design, kept.

### Duration — pairing, measured against the real emitters

All three doors emit a pairable id (verified in source):

- API-direct: `tool_call {id, name, input}` / `tool_result {tool_use_id, name, content}` — `core/agent_loop.py:149,158`;
- Claude CLI: same shape (executor pairs them itself via `_cli_tool_names[tc_id]`, `core/executor.py:2455-2469`);
- Codex CLI: `item.started`/`item.completed` → same `{id,…}`/`{tool_use_id,…}` meta chunks, `core/models/cli.py:1413-1447`.

`duration_ms = tool_result.ts − tool_call.ts`, paired on `tool_call.payload->>'id' == tool_result.payload->>'tool_use_id'` **within the same `run_id`**. `null` when no result exists (crashed/in-flight) — report it as `null`, don't drop the row.

**Do the pairing in Python, not as a SQL self-join**: page the `tool_call` rows first (≤100), then one second query for `tool_result` events `WHERE run_id IN (page's run_ids) AND kind='tool_result'` and match ids in-process. A JSON-extract self-join across a 1.25M-row table is the slow and dialect-fragile version of the same thing; the two-query shape is bounded by the page size and uses the existing `ix_run_events_run_id`.

### Query shape and tenant isolation (the non-negotiable)

`RunEvent` is `TenantStamped`, **not** `TenantScoped` — the global ORM filter never touches it (`core/tenant_filter.py` docstring, trap 3; `models.py:1104`). The documented enforcement point is the parent `Run`, which IS `TenantScoped` and IS auto-filtered (`with_loader_criteria(..., include_aliases=True)`, `core/tenant_filter.py:277-281`). Therefore:

- Every query in this endpoint is an **ORM select with an explicit INNER `.join(Run, RunEvent.run_id == Run.id)`** — the joined `Run` entity is what the tenant filter rewrites, so another tenant's events have no joinable parent and vanish. This is also where `agent_slug`/`session_id` filters live.
- **No raw SQL anywhere** in this endpoint (trap 2: `text()` is invisible to the filter forever).
- **No bare `RunEvent` query**, not even for the count or the result-pairing fetch — the pairing fetch keys on run_ids that came OUT of the tenant-filtered page query, and still joins `Run` for belt-and-braces.
- Route goes through the standard router registration in `api/__init__.py`, so `require_tenant` binds the tenant like every sibling (`core/tenant_filter.py:30-39`).
- Note trap-3's own caveat: the docstring warns against a correlated EXISTS on the **SSE hot path**; a paginated metrics JOIN is not that path, and a join (not a correlated subquery) is the shape it prescribes.

JSON-not-JSONB (`models.py:1159`, prod column is `json`): extract via SQLAlchemy `RunEvent.payload['name'].as_string()` — compiles to `->>'name'` on PG `json` and `JSON_EXTRACT` on the sqlite CI dialect. No jsonb operators, no GIN-on-jsonb, anywhere.

Bounding: `SET LOCAL statement_timeout = '10s'` inside the request transaction, Postgres-dialect-guarded (skip on sqlite), same as the sibling design.

### Acceptance — `test_tenant_isolation.py` T1

- Add `"/api/runs/tool-metrics": {"expect": "scan"}` to `GET_ROUTE_PLAN` (`backend/tests/test_tenant_isolation.py:234`). An unclassified route is a hard failure, so this is not optional.
- Make the scan a **real probe, not a vacuous one**: the fixture (`:460-472`) already seeds a Run + one `log` event per tenant. Extend it so each tenant's run also carries a `tool_call`/`tool_result` pair, with tenant B's **tool name containing `SENTINEL_B`** and B's `Run.session_id` carrying it too — the fields this endpoint actually serializes. A default-param call as tenant A then returns A's rows and the sweep's raw-substring assert has something to catch if the join is wrong.
- Plus a dedicated test file (sqlite): A sees only A's calls; `count_only` math matches the detailed `count`; `limit`/`offset` page correctly; duration pairs and is `null` for an unpaired call; `tool_name`/`since`/`until` filter. Per the mutation-test rule: temporarily drop the `.join(Run, …)` and confirm the isolation test goes red — a test that passes without the join proves nothing.

**Not done until `AGENTS_DATABASE_URL='sqlite://' pytest backend/tests/test_tenant_isolation.py` is green with the route classified.**

## 3. DDL (additive, `_apply_*_migration` pattern in `backend/app/db.py` — house rule, no alembic)

- `ix_run_events_ts` on `run_events(ts)` — no ts index exists today (measured in the sibling doc §1); without it every date-bounded query seq-scans 947 MB in prod while looking instant on dev sqlite.
- Partial expression index `ON run_events ((payload->>'name')) WHERE kind IN ('tool_call','tool_result')` — PG-only, dialect-guarded like the existing migrations.
- Both are lifted unchanged from the sibling design §3a so search-events reuses them. (`ix_runs_ended_at` stays with that card — this endpoint doesn't filter on it.)
- Prod `run_events` is ~1 GB live: prefer `CREATE INDEX CONCURRENTLY` outside the migration transaction, or accept seconds of write-lock at deploy — say which in the PR.

## 4. MCP tool spec (aw-app-agents-platform-runners)

**One tool**, `tool_usage_metrics`, registered next to `list_runs`/`get_run_detail` (`agents_platform_runners_app/mcp_server.py:1363-1380`), handled like the other AP-MT REST proxies (`:2804-2814`):

```
tool_usage_metrics(count_only: bool = false, tool_name?: str,
                   agent_slug?: str, session_id?: str,
                   since?: str (ISO8601), until?: str (ISO8601),
                   limit: int = 20, offset: int = 0)
→ GET /api/runs/tool-metrics, response passed through as JSON text
```

No client-side enrichment, no second fetch, no retries — one bounded GET (the gateway kills long tool calls). The tool description must say: counts and per-call metadata only; `get_run_detail(run_id=…)` opens a specific run. `count_only` is a parameter here too — **no second tool**, per the explicit requirement and the sibling design's §3b argument against mode-split tools.

## 5. What this makes harder later

- Per-call shape is now an API contract: if `run_events` is ever partitioned/archived (the sibling doc flags ~7 GB/year, disk preflight meets it ~2027), this endpoint is one more reader that must survive the migration — and one more argument that archiving must beat deleting.
- Two more prod indexes to carry through every future `run_events` migration.
- Python-side pairing means per-call duration can't be pushed into SQL aggregates later (e.g. "p95 duration by tool") without revisiting; if aggregate latencies become an ask, that's the moment to reconsider a SQL pairing CTE — on PG only, behind the same endpoint.
- `by_tool` in count mode is a small de-facto analytics surface; growing it (percentiles, group-by-agent) belongs to a new card, not creep here.

## 6. Risks for the Coders

1. **Route shadowing** — register above `get_run` (`runs.py:106`) or every call 404s "run not found". The test suite *will* catch this only if a test actually calls the route; the T1 sweep does.
2. **Tenancy trap 3** — no bare `RunEvent` query, no raw SQL, join `Run` in every select including count. The global filter only rewrites entities present in the statement.
3. **`Session.get()` trap 1** — don't `s.get(Run, …)` anywhere in this path; identity-map hits bypass the filter (`core/tenant_filter.py:48-60`).
4. **json vs jsonb** — `payload` is `json` in prod; use `.as_string()` extraction and the `->>` expression index exactly as written; verify the prod column type before the DDL.
5. **sqlite CI** — the suite runs on `AGENTS_DATABASE_URL='sqlite://'`; dialect-guard the statement timeout and the partial index; no `import docker` in tests.
6. **CLI `tool_result` has no `name`** — only `tool_use_id`; tool name must come from the `tool_call` side of the pair. Don't read `payload['name']` off results.
7. **Deploy order** — AP-MT endpoint first, verified live in the running container (`docker exec` grep — green CI is not proof, documented trap), *then* push the runners-app tool (it auto-releases on push).
8. **Naive-UTC timestamps** — `RunEvent.ts` defaults to naive `_now()`; parse `since`/`until` accordingly (reject/strip tzinfo consistently) so comparisons don't throw on PG.
