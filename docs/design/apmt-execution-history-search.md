# AP-MT execution-history search: the second arm

**Status:** design (card `3fb5bf3b-9510-819b-8e4a-dd5a4caa5a37`, Architect run `faf3bc6b82eb49b4ac021c3487d1421a`, 2026-10-16)
**Question answered:** Frederico, Telegram 2026-10-16 — *"search on agents-platform-multitenant runs body/tools/tools results all the stuff … I'm not sure if I want vector search or some SQL search translation so we don't have to vectorize/store every run — how useful could vector search be here?"*

## Summary (read this if you read nothing else)

1. The vector arm already exists and works: `execution_index.py` → `aw-app-kb /api/kb/executions` → `exec_pg.py` → `search_execution_history`. Keep it exactly as it is. Do not widen it.
2. Measured today: it is lossy at **three** independent layers — the `_interesting()` gate drops 59% of terminal runs by design, the 12×1500-char cap keeps at most ~26% of tool events even for indexed runs, and the fail-open hook only captures ~25% of the runs it *wants* to index. It is also a rolling ~30-day window (prune), while AP-MT's `runs`/`run_events` are never purged.
3. So the part of the ask it cannot serve — *"tools / tool results, all the stuff"*, exhaustively — is structural, not tunable. Raising the caps means embedding ~18 MB/day of run text, which is exactly the cost Frederico wants to avoid, for no gain on exact queries.
4. **Second arm:** a fixed, parameterised search endpoint in AP-MT itself (`backend/app/api/runs.py`): filter + substring match over `run_events` (tool name, event kind, text, agent/target/status/time), returning **metadata and pointers only — never payload text**.
5. **Agent surface:** a new MCP tool `search_run_events` in `aw-app-agents-platform-runners`, which calls that endpoint and optionally enriches the top hits with short snippets it extracts client-side and passes through the *same* `execution_index.redact()` that the vector arm uses — shared code, not a re-implementation.
6. That split resolves the hard constraint: the raw rows hold live credentials; an endpoint that returns no payload text cannot leak them, and the only text an agent ever sees went through the one shared redaction function.
7. **NL→SQL is rejected.** The real query classes are enumerable and few; a parameterised surface covers them without an LLM in the loop, without a read-only-role/timeout/allowlist apparatus, and without unbounded scans.
8. `aw-app-kb` does not reach into AP-MT's Postgres. AP-MT owns the data and the tenancy enforcement; the runners app already proxies every other AP-MT surface. KB stays untouched.
9. Vector stays the *recall* layer ("have we hit this before, which run was it") — re-verified today: the card's benchmark query returns the same 3 correct runs at 0.74–0.78. SQL becomes the *exhaustive/exact* layer and, since the source is never purged, also the only arm that sees anything older than 30 days or outside the gate.
10. `execution_index_mode='all'` is quantified in §7 and left for Frederico — but §7's numbers say it buys breadth the SQL arm already gives for free, so the recommendation is: don't.

## 1. Measurements (all run 2026-10-16, ~09:40–10:00 UTC)

Everything below was measured directly against the two live databases, not taken from the card. Method: SQL via `podman exec aw-app-kb` (KB pgvector, `executions` table) and `docker exec aw-stack-aw-postgres-1 psql -d agents_platform_multitenant` on the bare-metal host (AP-MT source of truth).

### The vector index (`executions`, aw-app-kb pgvector)

| Metric | Value |
|---|---|
| Chunks / distinct runs | 16,529 / 1,891 (card said 16,517/1,890 an hour earlier — confirmed, still indexing) |
| Table size (incl. HNSW) | 174 MB (~11 KB/chunk all-in) |
| Chunks-per-run histogram | 4:2 · 5:8 · 6:138 · 7:243 · **8:860** · 9:148 · 10:58 · 11:48 · **12:386** |
| Runs at exactly `MAX_CHUNKS=12` | **386 / 1,891 = 20.4%** — these are the cap-truncated runs |
| Chunk kinds | trace 8,033 · io 6,605 · summary 1,891 |
| `events_truncated` | false on all 1,891 runs — nobody hits `EVENTS_LIMIT=5000`; the binding limit is the chunk cap, not the event fetch |
| Oldest / newest `indexed_at` | 2026-09-15 / 2026-10-16 08:27Z — exactly the 30-day `prune()` horizon (`exec_pg.py` defaults: 30 days, 80k rows) |

### The source of truth (AP-MT Postgres, never purged)

| Metric | Value |
|---|---|
| Total runs (since 2026-08-02) | 27,173 — success 24,347 · error 2,775 · cancelled 48 |
| `runs` / `run_events` size | 310 MB / **947 MB** (1,256,716 events) |
| Event kinds | llm_token 525k · **tool_call 295k · tool_result 295k** · node_start/end 53k · … |
| Terminal runs ended ≥ 2026-09-15 (the index window) | 18,475 |
| — dropped by gate: compact | 206 |
| — dropped by gate: cancelled-no-output | 5 |
| — dropped by gate: no error AND no tool_call (`mode='interesting'`) | **10,919 (59.1%)** |
| **Eligible** for indexing per `_interesting()` | **7,548** (6,648 tenant-bootstrap + 900 second tenant) |
| Actually indexed | **1,891 → ~25% of eligible** |
| Tool events per terminal run (window) | p50 **0** · p75 4 · p90 36 · p99 244 · max 5,695 · mean 14.9 |
| Tool events that fit under the cap (generous 20-events/run assumption) | 72,708 / 274,801 = **26.5%** |
| tool_result payload size | p50 495 B · p90 6.8 KB · max 44 KB · total 642 MB |
| Full run text (events + input + output), window | p50 8.5 KB · p90 67 KB · p99 351 KB · max 6.3 MB · **total 558 MB / 31 d ≈ 18 MB/day** |
| Terminal runs/day, trailing 14 d | 77–2,925, mean ~824 |
| Indexes on `run_events` | `run_events_pkey`, `ix_run_events_run_id` — **no index on `ts`, none on payload**; `runs` has no `ended_at`/`started_at` index either |
| Server | PostgreSQL 16.14; `pg_trgm`/`btree_gin` **available but not installed** |

### Three loss layers, separated (this is the refined version of the card's finding)

The card's ceiling is confirmed, and it decomposes into three independent losses:

1. **Gate loss (by design):** 59% of terminal runs have no error and no tool_call — mostly chat/Telegram turns — and are never indexed in `mode='interesting'`.
2. **Cap loss (structural):** `MAX_CHUNKS=12 × MAX_CHARS=1500` ≈ 18 KB/run. 20.4% of indexed runs sit at the cap. Because tool pairs are enumerated before `io` chunks and the list is cut at 12 (`execution_index.py:116-122`, pairs at `:82`), a 200-tool-call run keeps ≤ ~10 pairs; across the window ≥ 73.5% of all tool events can never be in the index *even under a generous reading*.
3. **Operational loss (new finding, not on the card):** only ~1,891 of 7,548 eligible runs are in the index — and it is ongoing, not pre-deploy history: last-7-day capture is 5–87 runs/day against ~200+ eligible/day. The hook is a fail-open daemon thread in the workspace runner door (`execute.py:2948,2965,3026,3040,3151`); app restarts kill in-flight threads, the warm-path watcher and the 60 s finalize deadline both bail silently, and `index_run` swallows every exception by contract. Nothing retries, nothing backfills, nothing counts the misses.

Layer 3 is worth its own small follow-up card (a miss counter + a backfill sweep), but **no amount of fixing layer 3 fixes layers 1–2** — and the second arm makes all three moot for exact queries, because it reads the table the data already lives in.

Also re-verified: the card's quality claim — `search_execution_history("fs-sync delete policy approve pending deletions")` returns the same 3 correct runs (`acd7bcca…` 0.778, `99c18af1…` 0.762, `b252008f…` 0.739). The vector arm is good at what it is for.

## 2. The approach

Add a **fixed parameterised search endpoint over `run_events`/`runs` inside AP-MT**, returning metadata + pointers only, and surface it to agents as a new MCP tool in the runners app that does bounded, **redacted** snippet enrichment using the vector arm's own `redact()`. Keep the vector arm untouched as the semantic-recall layer. No NL→SQL, no second embedding pipeline, no new datastore — the "second arm" is ~one endpoint, one tool, and three small indexes on tables that already hold the complete record.

## 3. Where it lands

### 3a. AP-MT backend — the endpoint (owns data, tenancy, bounding)

`repos/agents-platform-multitenant/backend/app/api/runs.py` — alongside `get_events` (`runs.py:117-146`), which already demonstrates the tenancy pattern this endpoint must copy: `RunEvent` has no ORM tenant filter of its own (`core/tenant_filter.py` trap 3, referenced at `runs.py:127`), so **resolve the tenant through `runs` and join**, never query `run_events` bare.

```
GET /api/runs/search-events
  q            substring match against payload text (ILIKE, case-insensitive) — optional
  tool_name    exact match on payload->>'name' for tool_call/tool_result — optional
  kind         event kind filter (tool_call|tool_result|error|…) — optional
  agent_slug / source_slug, target_slug, status, notion_task_id, session_id
  since / until (against runs.ended_at)
  group_by_run bool, default true
  limit        default 20, max 100 (runs when grouped, events otherwise)
  count_only   bool — return totals only

→ grouped: [{run_id, match_count, first_ts, last_ts, kinds: {...},
             tool_names: [...], status, source_slug, target_slug,
             ended_at, cost_usd}]
   (payload text is NEVER in the response — see §5)
```

At least one of `q`/`tool_name`/`kind` + a selective filter is required (reject a bare full-table enumeration). Server-side bounding: `SET LOCAL statement_timeout = '10s'` inside the request transaction (Postgres only — skip on the sqlite test dialect), LIMIT always applied, matching via SQLAlchemy `.ilike()` so the sqlite CI suite (`AGENTS_DATABASE_URL='sqlite://'`) still compiles.

New DDL, additive, via the existing `_apply_*_migration` pattern in `backend/app/db.py` (house rule: SQLAlchemy, no alembic):

- `ix_runs_ended_at` on `runs(ended_at)` — today *every* date-bounded query seq-scans 310 MB;
- `ix_run_events_ts` on `run_events(ts)`;
- partial expression index for the headline query class ("every run that called X"): `CREATE INDEX ix_run_events_tool_name ON run_events ((payload->>'name')) WHERE kind IN ('tool_call','tool_result')` — a few MB of btree.

**No trigram/FTS index at first.** `q` ILIKE runs as a parallel seq scan over ≤947 MB *after* the other filters cut the row set; with a time or tool filter that is low seconds, and the statement timeout catches the pathological case. If unfiltered substring search turns out to matter, the upgrade path is `CREATE EXTENSION pg_trgm` + a GIN expression index `CONCURRENTLY` (both available on this PG 16.14, measured) at a one-off cost of roughly 1–2 GB disk — a decision to take with latency numbers in hand, not pre-paid now. Postgres `tsvector` FTS was considered and is the wrong tool here: stemmed word search behaves badly on code, identifiers and error strings, which is precisely what tool results contain; the use case on the card is exact-substring ("every tool_result containing this error string"), which is ILIKE/trgm territory.

### 3b. Runners app — the MCP tool (owns the agent surface and redaction)

`repos/aw-app-agents-platform-runners/agents_platform_runners_app/mcp_server.py` — a new tool `search_run_events`, registered next to `get_run_detail` (`mcp_server.py:1285`) and handled like every other AP-MT proxy (`mcp_server.py:2585`).

Behaviour: call `/api/runs/search-events`; if `include_snippets` (default true), for the **top ≤5 runs** fetch `/api/runs/{id}/events` (the existing endpoint `get_run_detail` already uses), locate the matches locally, and attach **≤3 snippets per run, ≤300 chars each, centred on the match, each passed through `execution_index.redact()`** — imported from the same module the embedding hook uses (`execution_index.py:31-43`). Same process, same pattern list, one source of truth. The tool's description tells agents what `search_execution_history`'s already does: this finds runs; `get_run_detail` opens them.

The tool name is **not** a mode on `search_execution_history`: that tool lives in a different repo against a different store with different guarantees (lossy 30-day digest vs exhaustive permanent record). One tool that is sometimes-exhaustive and sometimes-not, depending on a parameter, is how an agent silently builds an audit on a digest.

### 3c. What stays untouched

`aw-app-kb` entirely (exec_pg, routes, the vector tool); `execution_index.py`'s gate, caps and mode (see §7 for the mode='all' numbers); AP-MT's existing `/api/runs*` surface; `get_run_detail`.

## 4. Why AP-MT owns the endpoint (the coupling argument, both ways)

*For kb owning it:* "the knowledge base should be aware of past executions" reads naturally as kb's job, and the vector arm already lives there.

*Against, and decisive:* kb's awareness is already delivered by the vector arm. A SQL arm in kb means the kb container holding credentials to AP-MT's production Postgres across a host boundary (kb runs on the workspace podman host; AP-MT's `aw-stack-aw-postgres-1` on the bare-metal docker network — measured, there is no existing route or credential), **re-implementing AP-MT's tenancy enforcement outside the one codebase that owns it** (strict there: `TenantScoped` ORM filter + trap-3 discipline), and a schema coupling that breaks silently the next time AP-MT migrates `run_events`. AP-MT exposing one more read endpoint over its own tables is the shape every other consumer already uses, and the runners app proxying it to agents is the shape every other AP-MT tool already uses.

## 5. The redaction constraint, resolved

Facts first (verified in source today):

- The vector path redacts **before embedding**: `_split()` → `redact()` (`execution_index.py:52-54`), patterns at `:31-37` (sk-, ghp_/gho_, ntn_/secret_, Bearer JWTs, X-Api-Key).
- The raw rows are **not** redacted, and the existing authenticated surface already serves them: `get_run_detail` is a straight passthrough of `/api/runs/{id}` + `/events` (`mcp_server.py:2585-2594`), unredacted, 500 events at a time. That is today's accepted exposure: one run at a time, behind platform auth.
- This workspace has shipped exactly this leak class before (PAT in plaintext, workspace API key in a public repo — both in the memory index).

The risk a search arm adds is not "an agent can see a secret" (it already can, one run at a time); it is **bulk harvest**: `q="sk-"` or `q="password"` over 947 MB of payloads is a secret-mining primitive the platform does not currently have.

**Decision — scoped, not re-redacted, at the API; redacted at the only place text is emitted:**

1. The AP-MT endpoint returns **no payload text, ever** — metadata and pointers only. A surface that emits no text cannot serve a credential regardless of pattern coverage. This is the card's option B at the API layer, and it means AP-MT needs no copy of the redaction patterns (nothing to drift).
2. The one place search output becomes text an agent reads — snippet enrichment in the runners app — calls the **same `redact()` function in the same module** the embedding hook calls. Shared by import within one repo and process, not re-implemented across repos. Snippets are additionally capped (≤3×300 chars per run, top 5 runs), so even a pattern the denylist misses leaks at most fragments, not a bulk dump.
3. Residual, stated honestly: `q` matching happens against *raw* text server-side, so a caller can still **probe for existence** ("does any run contain `sk-abc123…`" → match counts). That is an oracle, not an exfiltration channel, and it is strictly weaker than what `get_run_detail` already grants any authenticated caller. Accepted.
4. Recommended hardening while in the file (cheap, benefits both arms since the module is shared): extend `_REDACTIONS` with connection-string credentials (`[a-z]+sql://user:pass@` → mask the password) and `(?i)(password|passwd|token)\s*[=:]\s*\S+`. The current five patterns would not catch a Postgres URL — and this very design document's measurement session handled one.

A design that had the endpoint return raw snippets and redacted only in the MCP layer was rejected: it would create a new *bulk* raw-text HTTP surface for any direct API caller — and AP-MT's legacy auth accepts any workspace JWT, so that surface would be wide.

## 6. Retention asymmetry — keep it, it's the point

The vector index is a rolling window (30 days / 80k chunks, pruned daily from kb's own lifespan); the source tables are never purged (back to 2026-08-02, growing ~20 MB/day → ~7 GB/year at current volume). The second arm **inherits the permanent record and adds no purge**: it is the only arm that can answer anything older than 30 days, which is a feature, not a bug to fix. Flag for the future, not this card: at ~7 GB/year, `run_events` meets the bare-metal disk preflight (which already blocks deploys when the host runs hot) sometime in 2027 — when a purge/partition card happens, the SQL arm is also the reason it must archive rather than delete.

## 7. `execution_index_mode='all'` — the numbers, Frederico's call

Not recommended, not decided here. What it would cost at current volume (824 terminal runs/day mean, trailing 14 d):

- **'all' with today's caps kept:** ~824 runs/day indexed instead of ~61/day actually captured now. ~6–7 chunks avg for the gate-excluded runs (they are mostly chat turns, small) → ~5k chunks/day. Embedding is local fastembed CPU in the kb container — no API spend, minutes of CPU/day. Storage ~55 MB/day at the measured ~11 KB/chunk; the **80k-row cap then becomes the binding limit in ~16 days**, i.e. the 30-day window silently shrinks to ~2 weeks unless `KB_EXEC_MAX_ROWS` is raised (at ~880 MB per 80k chunks, against a container with a ~2 GB disk budget per `exec_pg.py`'s docstring). Net effect: more runs findable semantically, **zero additional tool-result coverage** — the cap still drops 73%+ of tool events.
- **Actually "vectorize everything" (remove the caps):** ~18 MB/day of run text → ~12.5k chunks/day → ~140 MB/day of table+index → ~4 GB steady-state at a 30-day window. Blows the kb container's disk budget by 2×, multiplies embed CPU by ~10×, and buys recall on text whose queries are overwhelmingly *exact* (error strings, tool names) — which §1 shows vector similarity is the wrong instrument for anyway.

The second arm delivers the coverage both options chase, for the cost of three btree indexes on data already stored.

## 8. Where each arm is the right tool

| Query | Arm | Why |
|---|---|---|
| "Have we seen this failure before? How did it get resolved?" | **vector** | fuzzy semantics; re-verified 0.74–0.78 on the benchmark query |
| "Find the run where we did something like X" (wording unknown) | **vector** | paraphrase tolerance is the one thing SQL can't do |
| "Every run that called `approve_pending_deletions`" | **SQL** | exhaustive by construction; tool-name index; vector holds ≤26.5% of tool events |
| "Every tool_result containing this exact error string" | **SQL** | substring match; vector embeds almost none of this text |
| "Runs by agent/target/status/date, counts, cost rollups" | **SQL** | it's a WHERE clause; `search()`'s metadata filters already mimic a subset, lossily |
| Anything older than 30 days | **SQL only** | vector index is pruned; source never is |
| The 59% of runs with no error and no tool call | **SQL only** | gate-excluded from the vector index by design |
| Anything needing a completeness guarantee (audit, "did we ever…") | **SQL only** | vector arm is lossy at three layers (§1) |
| "What exactly did run X do" | `get_run_detail` | already exists; both search arms end in its pointer |

**No fused default.** A combined tool that merges both result lists was considered and rejected: the two arms carry different guarantees (exhaustive vs lossy digest), and fusion hides which one answered — an agent asked "did we ever call tool X" must not get a vector-shaped "probably not". Agents compose two tools fine; the descriptions state the guarantee each carries.

## 9. Rejected alternatives

- **NL→SQL translation** (the "SQL search translation" in the ask, LLM-generated queries over a read-only role). Rejected: the observed query classes are few and enumerable (§8); the safety apparatus it demands (read-only role, statement timeout, column allow-list, LIMIT injection, injection review) buys flexibility nobody has yet asked for; it adds an LLM round-trip to every search; and against *this* schema the LLM must rediscover trap 3 (tenant via parent run) every time — one missed join is a cross-tenant leak. **Runner-up**, and the migration path if the parameterised surface accumulates feature requests: the endpoint's params are exactly the vocabulary a bounded NL→SQL layer would compile into, so nothing built here is thrown away.
- **Widening the vector arm** (raise `MAX_CHUNKS`, `mode='all'`): §7 — cost without coverage, or coverage at ~140 MB/day.
- **SQL arm inside aw-app-kb:** §4 — cross-host DB credentials, re-implemented tenancy, schema coupling.
- **Postgres `tsvector` FTS:** wrong matching semantics for code/error text (§3a); trgm-GIN kept as a measured later upgrade, not a default.
- **Raw snippets from the endpoint, redaction only in the MCP layer:** creates a new bulk raw-text surface for direct API callers (§5).
- **A mode flag on `search_execution_history`:** one tool with per-call guarantees invites audits built on digests (§3b).
- **A second embedding store with bigger caps / hybrid dense+sparse store:** everything a sparse index would serve here, ILIKE + btree already serves against the source table; a new store adds a second retention/redaction/capture pipeline to keep honest.

## 10. What this makes harder later

- **Fixed params mean new query classes need a deploy.** "Group by tool name across all runs", "regex match", etc. are endpoint changes, not prompt changes. Accepted deliberately (that's the NL→SQL trade), and softened by the runner-up path in §9.
- **Pointer-only API means non-agent consumers (the AP-MT frontend, say a future "search runs" UI) get no snippets** unless they re-implement enrichment+redaction client-side or a redacted-snippet variant is added to the endpoint later — at which point the §5 decision (where redaction lives) must be revisited, since the shared module is in the runners repo, not AP-MT.
- **It entrenches "secrets at rest in run_events".** A searchable pointer index over never-purged raw payloads makes the eventual at-rest scrub/rotation story *more* urgent, not less; this design documents the exposure but does not reduce it. The real fix stays what it always was: rotate what leaked, and stop secrets reaching tool args.
- **Three more prod indexes** to carry through every future `run_events`/`runs` migration, and ~1–2 GB of one-off disk if the trgm upgrade is ever taken.
- **The capture-rate finding (§1 layer 3) now has a documented number**; if nobody opens the follow-up card, the vector arm quietly stays a ~25%-sample and this doc becomes the only place that says so.

## 11. Risks for the Coders

- **Tenancy trap 3 is the big one.** Do not query `run_events` without resolving tenant through `runs` — copy the pattern at `runs.py:117-146` / the comment at `:127`. A bare `run_events` filter is a cross-tenant leak that no existing test will catch (the suite's tenant-filter tests cover ORM-filtered models; `RunEvent` is `TenantStamped`, not `TenantScoped` — `models.py:1104`).
- **`payload` is SQLAlchemy `JSON`, not `JSONB`** (`models.py:1159`) — on Postgres that's the `json` type: `payload->>'name'` and the expression index work, but no jsonb operators/GIN-on-jsonb without a cast. Verify the prod column type before writing the index DDL.
- **sqlite CI dialect:** `AGENTS_DATABASE_URL='sqlite://'` runs the suite. Use ORM `.ilike()` (compiles on both), guard `SET LOCAL statement_timeout` and the expression-index migration behind the Postgres dialect check the other `_apply_*_migration`s already use, and don't `import docker` in tests (known CI trap).
- **No `ts`/`ended_at` indexes exist today** — a "last 7 days" query that looks instant in dev sqlite seq-scans 310 MB + 947 MB in prod. The DDL in §3a is part of the card, not an optimization afterthought. `CREATE INDEX` on a 947 MB live table: prefer `CONCURRENTLY` (outside the migration transaction) or accept a few seconds of write lock at deploy; say which in the PR.
- **Snippet enrichment must stay bounded** — the gateway kills long tool calls (known `mark_flow_done` timeout class). Top-5 runs × 1 events fetch each, hard-capped, no retries inside the tool call.
- **`redact()` import direction:** the MCP server imports from `execution_index` — fine today (same package); don't copy the patterns into `mcp_server.py`, that's the drift §5 exists to prevent. If `_REDACTIONS` is extended (§5.4), the existing `test_execution_index.py` redaction tests need the new patterns covered too, and note the extension changes what future *embeddings* contain, not what's already stored.
- **AP-MT deploy order:** endpoint (AP-MT repo, its own deploy workflow) must land and be verified live (`docker exec` grep in the running container — green CI is not proof, documented trap) *before* the runners-app tool that calls it ships; the runners app auto-releases on push.

## 12. The direct answer to Frederico's question

**How useful is vector search here?** Genuinely useful, and already paid for — as a *recall* instrument. "Which past run dealt with X", asked in words that don't match the run's own words, is a query SQL fundamentally cannot answer, the live index answers it correctly today (re-verified, §1), and its cost at current settings is trivial: ~174 MB, local-CPU embeddings, no API spend. Keep it.

**What it is not, and cannot become by tuning:** a searchable record of run bodies and tool results. Measured: it holds ≤18 KB per run however big the run, ≥73% of tool events never reach it, 59% of runs are gated out by design, only ~25% of the remainder actually get captured by the fail-open hook, and everything vanishes from it after 30 days. Each of those is a different mechanism; tuning one doesn't move the others.

**What is not worth paying for:** embedding your way out of that — `mode='all'` or raised caps costs up to ~140 MB/day of vectors and 10× embed CPU to approximate, lossily, what one endpoint over the Postgres you already run answers exactly, over the full never-purged history, for the price of three btree indexes. The "SQL search" half of your instinct was right, with one correction: it doesn't need an LLM translating to SQL — the queries you actually ask ("every run that called tool X", "every tool_result with this error", by agent/target/date) fit a fixed parameterised surface that is safer, faster, and keeps the credential-bearing raw rows behind a pointers-only API with the same redaction the vector path already applies.
