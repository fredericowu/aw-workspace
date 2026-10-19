# Execution history in the production Neo4j — tenant isolation design

Status: **implemented, flag ON; all four §9 steps live. The human-facing
surface is decided (§10 exec-view, 2026-10-19) and build-pending. The full
Event-timeline projection (all 17 kinds) is designed in §13 (2026-10-19,
card `3fe5bf3b-9510-81d0-a0de-ea04b0e9cd46`) with a per-kind verdict table,
and §13.2 records a live scrubbing hole found during that design — the
secret-tool args drop never fired for gateway-prefixed tool names.
**Slice 0 (§13.2's fix + purge) shipped 2026-10-19**; the measured exposure
was 34 `write_secret` rows, not the three §13.2 first named.** Measured
2026-10-19: `exec_sync_enabled` true, ~27,970 `ExecRun` / ~288,013
`ExecToolCall`, sentinel fresh, degraded false. The MCP tools
(`search_execution_graph`, `exec_run_detail`, card
`3fd5bf3b-9510-819f-8370-e80d964590d3`) are at v0.52.0 with
`search_in=name|args|both` and `matched_field` on every row
(`knowledgeable_app/mcp/client.py:602`). The §6 vault coverage registry maps
all 21 vault names to tested families (`vault_names_uncovered` empty), its
`contributes.doctor` check is live and was proven to fire and clear, and the
`VAULT_REVIEWED_NOT_CREDENTIAL` third state exists and is empty.

Where it landed, against §4: `repos/aw-knowledgeable/backend/app/core/graph.py`
(constraints, two fulltext indexes, the `exec_*` templates, `EXEC_BUCKET`),
`backend/app/core/exec_history.py` + `backend/app/api/exec_history.py` (the
service-caller-only router), `backend/tests/test_exec_isolation.py` (K7 — the
route-level reachability proof, §3 point 5), the exec guards in
`backend/tests/test_tenant_isolation.py` (§12 risk 1), and in the connector
`repos/aw-app-knowledgeable`: `knowledgeable_app/exec_scrub.py` (§6),
`knowledgeable_app/exec_sync.py` (§4/§8) and the
`knowledgeable-exec-sync` CLI + two flag-off scheduled tasks.
`scripts/load_exec_retrieval_neo4j.py` grew the §4 refusal guard.

Design card `3fd5bf3b-9510-814c-be85-c2f9723dca01` (Architect, 2026-10-18).
Predecessors: `docs/design/execution-history-retrieval-test-plan.md` (the
measurement), `.tmp/exec-retrieval-test/RESULTS.md` (the results),
`scripts/load_exec_retrieval_neo4j.py` (the throwaway v2 loader — reused for
its SQL and shape knowledge, **not** for its write path, see §4).

**The decision this designs under, not against:** Frederico decided the
agent-run execution history loads into the production Neo4j —
`aw-stack-aw-neo4j-1`, the same Community-edition instance that holds
aw-knowledgeable's knowledge corpus — isolated by tenant. The Coordinator's
counter-position (separate instance, or full-text in Postgres) was heard and
overruled; this document does not re-open it. The test plan's own P3 rubric
("a graph win justifies a metadata projection, never moving the system of
record") is satisfied in the letter — **Postgres remains the system of
record** (§5) — and overridden in the venue: the projection lands in the
shared production graph rather than a dedicated one.

No hard blocker was found. The design is buildable as specified. The two
places where a blocker *could* have lived — Community's single database, and
the credential-bearing text — are closed by §2/§3 and §6 respectively.

---

## 1. The approach, in five sentences

The execution history becomes a second, structurally disjoint subgraph inside
the one `neo4j` database: new `Exec*`-prefixed labels, zero relationships to
any knowledge node, stamped with the same `tenant` property every knowledge
node already carries, and with a **reserved, deliberately unregistered
bucket** that the existing bucket-scope machinery therefore never grants to
anyone. All writes and reads go through `core/graph.py`'s template catalog —
the single seam that the K5 static guard already enforces — so tenant
isolation on exec data is bought by the exact mechanism that already protects
the knowledge corpus, not by a new convention. Ingest is an incremental,
watermark-driven upsert run by the Knowledgeable Connector (workspace side),
which scrubs credentials out of run text *before* it leaves the workspace and
posts batches to a new service-authed ingest route; the connector never holds
the Neo4j password. Freshness is observable via a per-tenant sync-state
sentinel node with a staleness alarm and a weekly count-reconcile against
Postgres. Rollback is one batched label-scoped delete, because nothing in the
knowledge corpus references the exec subgraph.

---

## 2. The three scoping models, reconciled — what the real boundary is TODAY

Read before proposing, as measured against the current tree:

**Tenant is the security boundary, and it is real.** In aw-knowledgeable it
is enforced structurally, not by runtime filter:
`repos/aw-knowledgeable/backend/app/core/graph.py:1-16` states it plainly —
feature code is forbidden from touching the driver; every query is a named
template in `_CATALOG`; and `backend/tests/test_tenant_isolation.py` fails
the **build** if that stops being true. Three distinct guards:

- `test_k5_no_driver_calls_outside_graph_py`
  (test_tenant_isolation.py:436-444): any `session.run(`/`execute_query(`
  outside `core/graph.py` is a red build.
- `test_k5_every_node_pattern_carries_tenant_and_bucket`
  (test_tenant_isolation.py:447-503): a regex walk over every catalog
  template; any node pattern whose body lacks `tenant` is a red build.
  Relationship patterns have their own guard
  (`test_k5_relationship_patterns_carry_tenant_...`:536), and index-call
  templates — which have no MATCH pattern for the regex to see — are
  enumerated by name in `test_k5_ann_templates_filter_both_tenant_and_bucket`
  (:780).
- Runtime backstop: `graph.py:104-109` — strict enforcement (the default)
  raises `UnscopedTenantAccess` when a graph call has no tenant bound via
  `tenant_scope()`, and `require_tenant_or_service`
  (`core/identity.py:262-289`) is the one shared gate every router inherits.

**Bucket is a *declared* permission boundary whose enforcement mechanism is
complete but whose grants are trivial today.** Since §7bis
(docs/design/aw-knowledgeable-buckets.md, amended 2026-09-28) bucket is a
permission boundary between co-tenant tokens, enforced by `_bucket_denied()`
(`core/identity.py:356-377` — the 404/403/200 table). The T2-less resolver
(`resolve_bucket_scopes`, identity.py:321-353) then grants **every registered
bucket in the caller's OWN tenant, read+write, to every authenticated caller
of that tenant** — humans and the service caller alike. So today, bucket
separates nothing *between co-tenant callers* in practice. Any design that
says "the exec data is safe because it's in its own bucket" is wrong until
T2 per-token scopes land. This design does not say that (§3, §6).

**Correction, verified 2026-10-18 (Frederico), against an earlier draft of
this section that overstated it.** This is a **within-tenant** gap, not a
cross-tenant leak. `resolve_bucket_scopes` calls
`list_buckets_for_tenant(tenant_id)` (`core/graph.py:3982-4007`), which
passes `tenant_id` into the catalog query — it *is* tenant-filtered, and a
caller never sees another tenant's bucket slugs through it. Do not propagate
"every registered bucket to every tenant caller" as if it crossed tenants:
it does not. The design's conclusion is unchanged either way, because it
never rested on the stronger claim — an unregistered `_exec-history` bucket
can never enter any caller's `$bucket_ids`, whatever the grant rule is, so
the exec subgraph stays invisible to every bucket-scoped generic template.

**Neo4j Community has one user database, no RBAC — confirmed and designed
around.** `SHOW DATABASES` on the production image (`neo4j:5.26.4-community`)
returns `neo4j` and `system` only; `backup-neo4j.sh` hardcodes
`DATABASE="neo4j"  # Community Edition's one and only database`. So isolation
must be in-graph. The database itself enforces **nothing**: anyone holding
the `neo4j` password sees every tenant's bytes. That residual is accepted and
mitigated the way it already is for the knowledge corpus — no published port,
password in the stack's secret handling, and (new, §4) the workspace side of
this feature never holds that password at all.

**The reconciliation:** exec rows in AP-MT Postgres carry AP-MT `tenant_id`
(`tenant-bootstrap` today); aw-knowledgeable's graph `tenant` namespace is
its own (`resolve_tenant_id`, identity.py:228-259 — service callers resolve
to `KNOWLEDGEABLE_SERVICE_TENANT_ID`). These are different namespaces that
happen to be 1:1 today. The sync job carries an explicit mapping (v1: one
entry, AP-MT `tenant-bootstrap` → the knowledgeable service tenant), and each
sync pass binds exactly one destination tenant via `tenant_scope()`. The
mapping is config, looked up at runtime — never the AP-MT string assumed to
equal the graph tenant.

---

## 3. The sharpest question: what structurally stops a missing WHERE

The card asks: if isolation is a property every query must filter on, what
stops a forgotten filter from leaking tenant B's run text to tenant A? The
answer is that in this codebase **the developer who could forget the WHERE
cannot reach the database, and the place where the WHERE lives is checked by
the build, not by review.** Five layers, four of which already exist:

1. **No code path to the driver.** Feature code cannot run Cypher; only
   `core/graph.py` can (K5 file scan, red build otherwise). A missing WHERE
   can only be missing from a catalog template.
2. **The template is parsed, not trusted.** Every node pattern in every
   catalog template must carry `tenant` *in the pattern*
   (`test_k5_every_node_pattern_carries_tenant_and_bucket`). The new exec
   templates land in the same `_CATALOG` and are checked by the same regex
   on the same CI run. The new exec *fulltext* templates (index calls, no
   MATCH pattern) must be added to the K5 ANN-guard's named list
   (test_tenant_isolation.py:780) with `WHERE node.tenant = $tenant_id AND
   node.bucket = $bucket_id` after the YIELD — the guard asserts the
   predicate is present, same as it already does for the vector templates.
3. **Edges refuse by construction.** Every relationship-creating exec
   template MATCHes both endpoints on `tenant` before MERGE (the K3
   pattern): a cross-tenant `CHILD_OF` finds no endpoints and creates
   nothing. This is not a filter that can be bypassed; it is the shape of
   the write.
4. **Runtime strict mode.** An exec template invoked with no tenant bound
   raises `UnscopedTenantAccess` (graph.py:107) instead of running
   unscoped.
5. **New — a reachability proof, not just a pattern check (K7).** An
   integration test that seeds exec nodes for tenant A and a full-scope
   caller for tenant A, then asserts: tenant B's caller sees nothing
   (existing K1 shape), *and* tenant A's own caller sees no exec node
   through any generic surface — `search_nodes`, `get_node`, `get_graph`,
   `/api/search`, `/api/knowledge/search`, `list_documents`. This is the
   test that holds §7's contamination story true over time.

What this is honestly *not*: a database-enforced boundary. The guard is CI +
code structure. A human with the Neo4j password at a cypher-shell bypasses
all five layers. That is already true of the knowledge corpus and is the
accepted Community-edition posture; the design keeps the blast radius of that
credential small (§4: the workspace/connector side never holds it).

---

## 4. Where it lands — files and seams

**`repos/aw-knowledgeable/backend/app/core/graph.py`** — the only place new
Cypher is legal:

- New constraints, following the house composite pattern
  (graph.py:214-263): `(tenant, id)` unique on `ExecRun`, `ExecSession`,
  `ExecToolCall` (ToolCall id = `run_id + seq`), `(tenant, page_id)` on
  `ExecCard`, `(tenant, name)` on `ExecTool`, `(tenant)` unique singleton on
  `ExecSyncState`. The AP-MT lesson that composite-unique breaks tenant
  consolidation is noted in §10 — it is the house pattern here regardless,
  and consolidation would be a graph rebuild anyway (Postgres is the record).
- New write templates: `exec_upsert_runs` (UNWIND batch, MERGE on
  `{tenant, bucket, id}`, SET all metadata + `input_text`/`output_text` —
  MERGE not CREATE, because incremental sync re-visits runs whose status
  mutated), `exec_upsert_edges` (CHILD_OF/QA_OF/IN_SESSION/ON_CARD, both
  endpoints MATCHed on tenant), `exec_upsert_called`, `exec_upsert_toolcalls`,
  `exec_update_sync_state`, `exec_wipe_tenant` (batched DETACH DELETE over
  exec labels only — the rollback, §9).
- New read templates: `exec_search_runs` / `exec_search_toolcalls` (fulltext
  `queryNodes` over the two new indexes, tenant+bucket predicate after
  YIELD, added to the K5 ANN-guard list), `exec_run_detail`,
  `exec_run_children`, `exec_counts` (feeds the reconcile, §8).
- Two new fulltext indexes, per-label so they cannot interact with the
  chunk/document indexes: `exec_run_text` over
  `ExecRun(input_text, output_text)`, `exec_toolcall_text` over
  `ExecToolCall(name, args_text)` (shape proven in
  `scripts/load_exec_retrieval_neo4j.py:302-309`).
- **No vector index, no embeddings, no chunking for exec text in v1.** The
  measurement said the deciding variable was text + fulltext (text-bearing
  graph 10/10 vs metadata-only 5/10); embeddings were not what won, and
  embedding run text is exactly the path that would entangle exec data with
  topics/centroids/knowledge_search (§7).

**The reserved bucket.** All exec nodes carry
`bucket: "_exec-history"` — a module constant in graph.py, so K5's two-
property pattern check stays green with zero exemptions. The bucket is
**never registered** (no `(:Bucket)` row): `resolve_bucket_scopes` builds
grants from `list_buckets_for_tenant` (identity.py:351-353), so no caller's
`$bucket_ids` set can ever contain it, and every bucket-scoped generic
template (`search_nodes`' `<<node_in_scope:n>>`, `/api/search`'s scope set,
`list_documents`) excludes exec nodes *by the predicate that already exists*.
An explicit `?bucket=_exec-history` request 404s via `_bucket_denied`'s
none-row (existence hidden). Two small guards make the reservation
enforceable rather than hoped: the bucket-registration route rejects slugs
beginning with `_` (so nobody can register it later and silently expose the
subgraph), and K7 (§3) proves the invisibility end-to-end.

**`backend/app/core/exec_history.py` + `backend/app/api/exec_history.py`** —
new module and router. The router joins the one shared gate list
(`api/__init__.py`), inheriting `require_tenant_or_service`, and additionally
requires the **service caller** (`require_service`, identity.py:213-225) for
*both* ingest and search in v1. Rationale: the only consumers are agents via
the connector's MCP tools, which already reach richer versions of this exact
data in Postgres (`search_execution_history`, `get_run_detail`, raw SQL
arms) — exposing exec search to service callers widens nothing. Human
exposure was settled 2026-10-19 (§10 exec-view decision): a separate
owner-allowlisted router, not a dependency swap on these routes —
`/api/exec/*` stays `require_service` permanently, and the bucket stays
unregistered under every planned future.

**`repos/aw-app-knowledgeable` (the Knowledgeable Connector, workspace
side)** — the worker, matching Frederico's 2026-10-12 direction that the
connector does the heavy work:

- A scheduled sync job (every 10 min): read AP-MT Postgres (the SQL, the
  `source_slug`-not-`agent_slug` fact, and the `\u0000` client-side-parse
  rule are lifted verbatim from `scripts/load_exec_retrieval_neo4j.py:30-42,
  227-244`), **scrub** (§6), then POST batches to the ingest route with the
  service secret. The connector process never holds the Neo4j password —
  which also retires the v2 script's direct-HTTP-to-Neo4j pattern from
  anything production-facing.

  **Correction, 2026-10-19 (card `3fe5bf3b-9510-8105-9a48-f85615d6bd1a`):**
  an earlier version of this bullet said the *workspace* never holds the
  password. Since the 2026-10-18 rotation that is false as written: the
  vault entry `aw-stack-neo4j-auth` (aw-vault via aw-backend) is
  workspace-scoped and readable from the workspace through the
  approval-gated `read_secret` path. The property that actually holds, and
  the one this design needs: **no connector code path reads the Neo4j
  password, and the file SecretStore — the only store `exec_scrub` can
  enumerate — never contains it.** The scrubbing consequence of that
  distinction is §6 point 1, corrected the same day.
- Batch sizing: ≤500 runs or ≤2,000 tool calls per request, target <10 s per
  request — production is single-worker, and the tunnel edge cuts requests
  at 30 s; a sync pass must interleave with live search traffic, not starve
  it.
- A doctor/status surface reporting the sync-state sentinel (§8).

**Untouched:** `scripts/backup-neo4j.sh` (§8 quantifies its new window, no
code change), the knowledge ingest pipeline, `knowledge_search`'s arm list,
pgvector `search_execution_history` (stays as-is in Postgres), AP-MT schema
(v1; §8's `parent_run_id` fix is a separate AP-MT card).

**Explicitly dead: the v2 loader as a production path.** It wipes the entire
graph first (`wipe()`, load_exec_retrieval_neo4j.py:320-329) — against the
shared production instance that is the knowledge corpus erased; and it writes
raw Cypher over the HTTP tx endpoint, bypassing the only enforcement
mechanism this design relies on. The script stays for the throwaway harness;
the Coder should add a refusal guard (abort if the target contains any
`Document` node) so it can never be pointed at production by accident.

---

## 5. What is loaded, what is scrubbed, what stays in Postgres

**Loaded** (per tenant, through the catalog): `ExecRun` with metadata
(id, agent_slug ← `runs.source_slug`, status, model_slug, flow_slug,
flow_outcome, hop_count, cost_usd, tokens_in/out, started_at, ended_at) and
scrubbed `input_text`/`output_text`; `ExecSession`, `ExecCard`, `ExecTool`;
edges CHILD_OF, QA_OF, IN_SESSION, ON_CARD, CALLED{n}; `ExecToolCall` with
scrubbed `args_text` (the `MADE_CALL` layer — it is what carried Q-class
wins in the measurement).

**Scrubbed on load — mandatory, not optional.** See §6.

**Stays in Postgres (system of record, always):** the unscrubbed originals;
all non-`tool_call` run_events; lessons, retro scores, artefacts; the
pgvector embeddings and `search_execution_history`; every table AP-MT owns.
The graph is a *projection*: any doubt about graph content is resolved by
rebuilding from Postgres, never the reverse. Nothing is ever deleted from
Postgres on behalf of this feature.

---

## 6. Credentials in run text — why scrubbing is mandatory, and what else holds

The run text contains live tokens (measured; the test instance was built
portless and had its password rotated afterwards). Production Neo4j backs a
publicly reachable app. And §2 established that bucket grants are trivial
today — every tenant caller gets every registered bucket. So:

1. **Scrubbing on load is mandatory.** It happens in the connector, before
   the text crosses toward Neo4j, in two passes: (a) **exact-match
   redaction of every value in the workspace FILE secret store**
   (`src.apps.secret_store.SecretStore` over `.aw-workspace/secrets/*.json`)
   — the connector runs where that store lives, so its set of known-live
   secrets is enumerable, which is a far stronger guarantee than patterns
   *for the credentials that store holds*; (b) pattern
   families for secrets not in the store (`sk-…`, `ghp_`/`github_pat_`,
   `AKIA…`, `eyJ…` JWTs, `Authorization: Bearer …`, `password=`,
   `X-Internal-Secret`, URL userinfo). Replacement is `<REDACTED:kind>`,
   irreversible in the graph; Postgres keeps the originals.

   **Correction, 2026-10-19 — exact-match does NOT cover the vault, by
   decision.** There are two secret stores in this workspace: the per-app
   file store above, and aw-vault (written by `aw-workspace-cli secrets` /
   the `write_secret` tool, via aw-backend). `exec_scrub._secret_values()`
   reads only the file store. Measured, not inferred: after the production
   Neo4j password was written to the vault as `aw-stack-neo4j-auth`,
   `scrub.exact_values` stayed at 10 — unchanged. **A vault-only credential
   is therefore pattern-scrubbed only**, and an earlier version of point (a)
   claimed exact-match coverage it did not have.

   This is kept, not fixed, and the reason is the vault's own design: the
   read path is approval-gated (a human on Telegram) precisely so a value
   leaving the vault is always a conscious act. An unattended bulk read of
   every vault secret on every 10-minute sync tick is impossible through
   that gate and wrong around it — see §10 for the two rejected
   alternatives. The containment for a vault-credential miss is §6 point 2's
   population-equivalence argument, which holds under the pinned reader
   population (service callers, plus §10 exec-view's owner-allowlisted
   operators) regardless of which store a secret lives in.

   **What replaces the lost guarantee — a tested, monitored requirement,
   not a coincidence:**

   - *Tested:* every credential family known to transit the vault must have
     a pinned pattern test in `tests/test_exec_scrub.py`. For
     `aw-stack-neo4j-auth` this already holds: `NEO4J_AUTH=user/pass`
     (env-assignment family, test_exec_scrub.py:146-152) and
     `bolt://user:pass@…` (url-userinfo family, test_exec_scrub.py:237-248)
     are both pinned, and both were verified to redact against the live
     scrubber on 2026-10-19.
   - *Monitored:* the connector keeps a committed coverage registry mapping
     each vault secret NAME to the pattern families that cover its value's
     shape (names are listable without approval — "listing is not a read",
     aw-app-secrets). Each sync tick compares `list_secrets()` names against
     the registry and reports unacknowledged names on the sync status
     (`vault_names_uncovered`), surfaced by doctor — so a new vault secret
     with no argued pattern coverage degrades *visibly*, which is the
     property the silent gap violated. A unit test pins that each registry
     entry's declared family redacts a representative invented shape, so the
     registry cannot drift from the pattern table. (Implementation: a
     follow-up Coder card; the registry lives in `aw-app-knowledgeable`
     beside `exec_scrub.py`, the status field beside
     `exact_match_unavailable` in `exec_sync.py`.)
2. **Residual risk stated plainly:** pattern scrubbing is best-effort; a
   pasted credential with no recognisable shape survives it. The layers that
   contain that miss: the reserved unregistered bucket (generic surfaces can
   never return an exec node, §4), a pinned reader population (service
   callers, plus — once §10's exec-view ships — owner-allowlisted humans
   only), disjoint labels and zero cross edges (§7), and no published Neo4j
   port. The population-equivalence argument, in its general form: **every
   authorized reader of exec text can already read the unscrubbed original
   in Postgres** — service callers because they hold the service secret,
   exec-view humans because the allowlist admits only operators who hold
   host root, vault approval authority and the service secret in their own
   config. A scrubber miss therefore grants nothing to anyone who didn't
   already have it. "Service callers only" was the v1 *instance* of that
   invariant, never the invariant itself. That equivalence is the actual
   safety argument; scrubbing is what keeps it from degrading if the
   audience ever widens beyond that set (§11), and what keeps backups/dumps
   (which leave the box for S3) from carrying live tokens. What the human
   surface does change, stated rather than waved away: membership in the
   reader set becomes stealable as a browser session (`aw_id_jwt`, on a
   publicly reachable origin) rather than only as a server-side secret —
   contained by §10 exec-view point 4's rendering shape, which bounds what
   a stolen session exfiltrates casually.
3. **Tool-call hygiene:** `args_text` for secret-handling tools
   (`read_secret`, `collect_secret`, `write_secret`, `delete_secret`) is
   dropped wholesale, not scrubbed — name and timestamp load, arguments
   never do.

   **Correction, 2026-10-19 (§13.2): the drop as shipped matched BARE tool
   names only, and production names arrive gateway-prefixed in at least
   three spellings** (`mcp__aw-gateway__aw__aw_secrets__write_secret`,
   `aw-gateway.aw__aw_secrets__write_secret`,
   `mcp__workspace-gateway__aw__aw_secrets__write_secret`) — so the drop
   never fired for a single real MCP-routed call, and vault-only secret
   values written via `write_secret` landed in the graph in plaintext. The
   fix (suffix-segment matching, both copies, plus a purge of the already
   landed rows) is §13.2 / slice 0 of the timeline projection, **shipped
   2026-10-19**: `exec_scrub.is_secret_tool` / `tool_name_segment` in the
   connector, the same pair in aw-knowledgeable's `core/exec_history.py`,
   and `POST /api/exec/purge-secret-args` over the
   `exec_drop_secret_toolcall_args` template for the rows that had already
   landed.

---

## 7. Protecting the knowledge product from ~325k new nodes

Four structural separations, each riding an existing mechanism:

1. **Disjoint labels** (`Exec*` prefix, colliding with nothing in
   graph.py's 259 label references). The knowledge pipeline's label-scoped
   work (ingest scans, topic build, chunking) never matches them.
2. **Zero relationships between the subgraphs.** No exec template creates an
   edge to a non-`Exec*` node; traversal templates (`graph_neighbors_*`,
   tree search's `related_vias`) therefore cannot wander in — there is no
   edge to follow. (ON_CARD tempts a future link to Kanban-derived knowledge
   docs; deliberately not in v1 — the moment that edge exists, §3's K7 test
   is the thing that must be renegotiated, not quietly broken.)
3. **Per-label indexes.** `knowledge_search`'s arms call named chunk/doc
   indexes (`_ARMS_BY_MODE`, core/knowledge_search.py:556); the two exec
   fulltext indexes are called only by the two exec templates. Lucene/vector
   scoring for the knowledge product is numerically unaffected by exec
   volume. Topic membership and centroids never see exec data because exec
   text is never chunked or embedded (§4).
4. **Bucket-set exclusion for the generic templates.** The genuinely
   label-generic templates (`search_nodes` at graph.py:1136, `get_node`,
   `nodes_exist`) filter by the caller's bucket set — which can never
   contain the unregistered `_exec-history` (§4). This is the layer that
   closes the hole the Topic-leak bug
   (`bug:aw-knowledgeable-topic-nodes-linkable-wedge-rebuild`) proved is
   real for property-scoped generic matches.

What is *not* protected: global operations — the backup window (§8), heap
pressure, store size, `MATCH (n)` admin queries at a cypher-shell. Those are
shared-instance costs Frederico's decision accepts; §8 bounds them.

---

## 8. Freshness, backup, disk — the operational envelope

**Freshness: incremental upsert, not rebuild.** A nightly full rebuild is
disqualified twice over: the wipe is unthinkable on the shared graph, and a
24 h-stale "current-looking" copy is the exact failure the card names. The
sync job runs every 10 min: upsert runs with `started_at` or `ended_at` past
the watermark, **plus** a re-sweep of all non-terminal runs seen in the last
48 h (runs mutate after insert — status flips, output lands at the end; the
Coder must verify whether `runs` carries a usable `updated_at` and tighten
the re-sweep to it if so). MERGE semantics make every pass idempotent, so a
pass killed mid-flight (backup stop, host stall) just re-runs.

**Observable, not assumed:** each pass writes `(:ExecSyncState {tenant})` —
`last_sync_at`, `watermark`, per-label counts, `last_error`. Exposed through
the connector's doctor/status surface with two alarms: staleness
(`now − last_sync_at > 60 min` → degraded, the dead-man's-switch pattern the
backup doctor check already uses) and a weekly count-reconcile reusing the
verification-table idea from the test loader (CHECKS,
load_exec_retrieval_neo4j.py:248-293): SQL counts vs `exec_counts` template,
mismatch → degraded. Backfill is the same route, one-off, off-hours
(estimate: the direct load took 63 s; through the service-authed route in
bounded batches, 15–40 min — acceptable for a one-time pass).

**Backup/downtime, quantified.** `backup-neo4j.sh` stops the container
nightly (03:00 UTC pipeline), dumps, restarts, waits for health; the
health-check `start_period` (60 s) already dominates the window on today's
small store. Exec v1 adds ~204 MB text + Lucene overhead → store delta
roughly 0.5–1 GB; `neo4j-admin database dump` moves at disk speed, so the
dump step grows by well under a minute. Expected window: from ~1–2 min today
to **~2–4 min nightly** — accepted. Growth: ~824 runs/day ≈ ~6 MB/day of
text ≈ +2.2 GB/yr/tenant → dump time grows by low tens of seconds per year.
Tripwire rather than hope: the script already writes `downtime_seconds` into
the manifest; alarm when it exceeds **600 s** — that is the signal to revisit
(retention valve below first, instance split only if the valve isn't enough).
The sync job and the backup window interact safely: a pass that dies when
Bolt goes down is retried by the next tick (idempotent MERGE).

**Disk (87 % used, 55 GB free).** Store+index ≈ 2–3× raw text. One tenant,
one year: ~5–8 GB. All tenants today is effectively one real tenant
(`tenant-bootstrap`); at T comparable tenants, multiply. Ceiling: at 3
tenant-years (~20 GB) or host disk >92 %, the **retention valve** fires:
null out `ExecToolCall.args_text` older than 180 days (metadata and edges
stay; the text is rebuildable from Postgres at any time, and Postgres now has
its own verified backup as of 2026-10-18). The valve is a catalog template +
a monthly connector task, cheap to build with the rest.

**`parent_run_id` breaking at every wakeup: follow-up, not prerequisite.**
The measured 10/10 was achieved *with* the broken chain — text was the
deciding variable, lineage was not. Fixing it is an AP-MT write-path card
(stamp the causal parent on wakeup-resumed runs; `core/executor.py`'s
pre-create-then-run paths are the seam, per the aw-agents-flow gotchas), and
the graph inherits the fix automatically through the incremental sync as new
runs arrive. Until then, every exec-search tool description must say
`CHILD_OF understates lineage across wakeups` — the measured failure
(4 descendants reported, 11 real) must be a documented limitation, not a
silent wrong answer.

---

## 9. Migration and rollback

Migration, in order, each step shippable alone:
1. graph.py templates + constraints + indexes + K5 list additions + K7 test
   (schema only, no data — index creation on the empty label set is
   instant).
2. Ingest route + connector sync job behind a connector-side flag, default
   off.
3. Flag on for `tenant-bootstrap`; backfill; reconcile table green.
4. MCP tools (`search_execution_graph`, `exec_run_detail`) through the
   connector, service path only.

Rollback (any step, no knowledge data touched): flag off → `exec_wipe_tenant`
per tenant (batched DETACH DELETE, exec labels only — the 20k-batch pattern
from the test loader) → drop the two exec fulltext indexes and exec
constraints. Postgres unaffected by construction. A botched load is also
recoverable forward: wipe-and-backfill, since the graph is a projection.

---

## 10. Decisions and rejected alternatives

### Decision 2026-10-19 — the human surface ships as **exec-view**: owner-allowlisted, no bucket registration, digests stay shelved

Card `3fd5bf3b-9510-818d-8801-ce4061140cfa` (the UI card) is §11's reopen
trigger #1 fired, and this section settles it (Architect run
`eb44177852ba4e068a8cba6fca8beebc`). The finding that settles it: **§6
point 2's argument was never "service callers only" — it is "every
authorized reader can already read the unscrubbed original."** Service-only
was the v1 instance of that invariant. The UI ships by preserving the
invariant under a new instance, not by weakening it:

1. **A dedicated human-facing router** (working name `exec-view`) in
   `backend/app/api/`, joining the shared gate list like every router
   (`api/__init__.py`), plus its own dependency: an authenticated identity
   whose `sub` is on a new `KNOWLEDGEABLE_EXEC_VIEWER_SUBS` allowlist. A
   boot-time validator (same home and shape as `config.py:382`'s owner-subs
   validator) refuses to start unless exec-viewer subs ⊆
   `KNOWLEDGEABLE_OWNER_SUBS`. The subset relation IS the equivalence
   argument made machine-checkable: an exec viewer must be an operator who
   already resolves to the service tenant (`resolve_tenant_id`,
   `core/identity.py:256-257`) and already holds the estate. An
   authenticated non-allowlisted human gets **404** (existence hidden, the
   `_bucket_denied` convention), covered by a K7 extension.
2. **The backend is the proxy.** The router calls `core/exec_history.py` /
   the `exec_*` catalog templates directly under the request's own
   `tenant_scope()`. No service secret in the browser — and none needed
   server-side either, so there is nothing for the frontend to hold.
   `/api/exec/*` stays `require_service`, untouched.
3. **The bucket is never registered — including for this.** T2 token scopes
   are not a prerequisite, and bucket registration is not the exposure
   mechanism (rejected below). K7's underscore-rejection guard and its
   zero-exec-through-generic-surfaces proofs stay exactly as they are; the
   new router is *added to* K7, it does not renegotiate K7.
4. **What the UI renders:** search/list views render metadata plus the
   matched-field snippet (`matched_field` is already on every row,
   `mcp/client.py:602-606`; default `search_in='both'`, with the toggle
   exposed because the name/args distinction changes answers). Full
   scrubbed `input_text`/`output_text` renders only on an explicit per-run
   detail view (the `exec_run_detail` mirror). No bulk-text list mode, no
   export endpoint. This bounds what a stolen owner session exfiltrates
   casually — exposure shaping layered on the allowlist boundary, not a
   substitute for it. The UI also carries the MCP tools' two honesty
   constraints: `CHILD_OF` understates lineage across wakeups (4 shown
   where 11 are real), and "how many runs called tool X" must show its
   predicate or the breakdown, never one confident number.
5. **The door that stays closed:** any reader who cannot already read
   unscrubbed Postgres breaks the equivalence. Widening the audience past
   the operator allowlist therefore requires reviving the digest runner-up
   *first* — that is now the §11 trigger, and the subset validator turns an
   attempted quiet widening into a refused boot.

Rejected alternatives, 2026-10-19 (this decision):

- **Option A — reviving the digest runner-up before the UI ships.** The
  21/21 coverage registry does not cure the digest approach's recorded
  objections: digests still must be computed where plaintext lives (the
  brute-forceable vault endpoint), and write-time capture still silently
  misses out-of-band writes — the same silent-divergence family as the bug
  that created the vault-coverage card. What the registry *does* change is
  the digests' marginal value, downward: with every vault name mapped to a
  tested pattern family, uncovered names alarmed through a doctor check
  proven to fire and clear, and `VAULT_REVIEWED_NOT_CREDENTIAL` empty, the
  residual gap digests would close is a value whose real shape diverges
  from its registry-declared family, or a credential living in neither
  store — and the pinned reader population already contains both cases.
  Buying the most expensive defence for the thinnest residual slice stays
  rejected. Still the named runner-up; its revive-trigger is now sharper
  (§11: audience beyond the operator allowlist).
- **Option B as framed — "pattern coverage alone is sufficient for a
  human-facing surface."** Not signed, and deliberately so: pattern
  scrubbing is best-effort by §6 point 2's own words, and no improvement in
  coverage makes it sufficient for an audience that couldn't otherwise read
  the source rows. What makes the surface shippable now is not that the
  patterns got good enough — it is that the audience can be pinned to a
  population for which equivalence holds, with coverage gaps degrading
  visibly instead of silently. Coverage is supporting, never sufficient.
- **T2 per-token scopes + registering the bucket as the human-exposure path
  (what §11 originally assumed).** Registration is exposure to the wrong
  population: it puts exec nodes in reach of the bucket-scoped generic
  templates for co-tenant callers — exactly what K7 forbids — and forces
  renegotiating the underscore guard, while also gating Frederico's request
  on T2, which does not exist. The dedicated router reaches named humans
  without touching the bucket machinery at all; the unregistered
  reservation survives its first human surface intact.
- **"A view plus an auth gate" — any authenticated co-tenant human.**
  Breaks population equivalence: an authenticated human who is not an
  operator cannot read unscrubbed Postgres, so a scrubber miss would grant
  them something new. Rejected regardless of the gate mechanism's strength
  — the argument is about who may be in the population, not about who
  holds the password.
- **Not shipping the UI at all.** A legitimate reading of trigger #1,
  rejected because it treats the trigger as a veto when it is a design
  obligation: the equivalence argument survives under the operator
  allowlist, Frederico asked for the surface explicitly, and deferral buys
  no safety the subset validator doesn't already enforce.

### Rejected alternatives (2026-10-18, the original design)

- **Separate Neo4j instance per use (the Coordinator's position).** Overruled
  by Frederico; recorded as the named runner-up, and it remains the designed
  fallback if the shared-instance tripwires (§8 downtime >600 s after the
  retention valve) ever fire. Not re-argued here.
- **Instance-per-tenant.** Solves isolation by multiplication: N containers,
  N backup windows on a disk at 87 %, N upgrade/heal surfaces on a host
  whose failure mode is silent degradation. Unjustifiable at one real
  tenant; revisit only if a second tenant with hostile-co-tenant
  requirements materialises before T2 token scopes.
- **Enterprise licence (database-per-tenant, RBAC, hot backup).** Buys real
  database-enforced isolation and would shrink §3's caveat — at a licence
  cost nobody has approved, for a boundary the K5 mechanism already holds to
  the standard the knowledge corpus itself lives under. Standing veto on new
  spend applies.
- **Label-per-tenant (`:Tenant_xyz`).** Fights the house mechanism: K5's
  guard is built around the `tenant` property in pattern bodies; per-tenant
  labels would need per-tenant fulltext indexes (they are per-label) and a
  template catalog that can't be static. Strictly worse than the property.
- **The v2 loader pointed at production.** Wipes the shared graph; bypasses
  the catalog; requires the Neo4j password on the workspace side. Dead (§4).
- **Nightly full rebuild as the freshness story.** 24 h staleness that looks
  current + a wipe on shared data. Dead (§8).
- **Chunk + embed run text into the knowledge pipeline.** Maximum
  contamination (topics, centroids, beam behaviour all exposed to 10× node
  count), LLM extraction cost on a corpus that regenerates daily, and
  extraction is globally OFF by flag anyway. The fulltext-only shape won the
  measurement without it.
- **Unscrubbed text, relying on bucket gating.** Bucket grants are
  all-buckets-to-everyone until T2 (identity.py:351-353). Dead on the facts
  of §2.
- **Registering `_exec-history` as a normal bucket.** Under the T2-less
  resolver, registration *is* exposure — every tenant caller would get it,
  and `search_nodes`/`get_node` would start returning exec nodes in the UI
  link picker. The unregistered reservation is the design, not an oversight.
- **Making `_secret_values()` read the vault too (2026-10-19).** Rejected
  on two grounds. First, it cannot be done through the sanctioned path: the
  vault's `read_secret` is approval-gated by design (`aw-app-secrets` —
  writing is ungated because the caller already holds the value; the gate
  exists to stop values *leaving*), so a bulk unattended read every 10
  minutes would either spam a human with approval prompts or require a new
  ungated bulk-read endpoint — widening the single most deliberately
  guarded boundary in the estate to strengthen a second-layer defence.
  Second, it couples every scrub tick to vault availability, and the vault
  has already gone dark for a day once (2026-10-02, the netns
  decommissioning) — a scrubber whose strong pass silently depends on a
  remote service reproduces the failure mode this estate is named for.
- **Hash-only exact-match against vault values (2026-10-19).** Compare
  digests, never hold plaintext: technically feasible (per distinct secret
  length, a rolling-hash candidate scan over the text with a cryptographic
  confirm — it matches only whole values, exactly like plaintext
  exact-match, so substrings are no worse). Rejected because the digests
  have to be computed somewhere that holds the plaintext, and both homes
  are bad: a new aw-backend/vault digests endpoint is a new cross-repo
  contract exposing offline-brute-forceable material for low-entropy
  passwords; capturing digests at `write_secret` time in the workspace
  covers only writes that pass through this workspace's write path — the
  aw-backend UI or another workspace's write silently bypasses it, which
  recreates the exact silent-divergence failure the 2026-10-19 card was
  about. The marginal win over tested patterns is only the
  no-recognisable-shape case, which §6 point 2's population-equivalence
  argument already contains. **Named runner-up:** this is the approach to
  revive if the reader population ever widens beyond the operator
  allowlist — see §11, and the 2026-10-19 decision above, which re-examined
  and kept this rejection when trigger #1 fired.

## 11. What this makes harder later

- **Exposing exec search to humans** was settled 2026-10-19 (§10 exec-view
  decision): a dedicated owner-allowlisted router, not
  T2-plus-bucket-registration — the bucket stays unregistered and K7's
  guards stay unrenegotiated. What stays hard, deliberately: widening the
  audience beyond the operator allowlist (anyone who cannot already read
  unscrubbed Postgres) requires reviving the §10 digest runner-up first,
  and the subset boot validator turns a quiet config widening into a
  refused boot.
- **exec-view couples "may see exec runs" to "is an owner":** the subset
  validator means a future partial grant — a teammate who may browse runs
  but must not inherit the owner tenant mapping of
  `KNOWLEDGEABLE_OWNER_SUBS` — is impossible without either widening
  owner-subs (wrong: it remaps their whole knowledge tenant) or reviving
  digests and then decoupling the two lists (the designed path).
- **Linking exec data to knowledge** (e.g. ON_CARD → Kanban-derived docs) is
  structurally forbidden by the zero-cross-edge rule; adding the first such
  edge reopens §7's traversal-containment argument entirely.
- **Tenant consolidation/rename** now spans Postgres *and* graph composite
  keys (the AP-MT composite-unique scar); mitigated by the projection
  property — consolidation = wipe + re-backfill under the new tenant, not an
  in-place migration.
- **Scrubber misses are durable** until someone re-scrubs and reloads;
  backups inherit them for their retention life.
- **Vault credentials are pattern-scrubbed only (§6 point 1, corrected
  2026-10-19)** — the exact-match guarantee is permanently narrowed to the
  file store. Acceptable while §6 point 2's population-equivalence holds.
  Three things would reopen it, in order of likelihood: (1) the exec
  reader population widens beyond the operator allowlist — anyone who
  cannot already read unscrubbed Postgres. This is the surviving form of
  the original trigger ("T2 exposes exec search to humans"), which fired
  2026-10-19 via the UI card and was resolved by pinning the population
  (§10 exec-view decision) rather than by digests; (2) a vault credential
  whose real shape diverges from its registry-declared family — or that
  lives in neither store — is measured transiting run text: the "pattern
  coverage can be made a requirement" premise fails; (3) the coverage
  registry rots in practice (unacknowledged vault names sitting in doctor
  output — the doctor surface now exists and was proven to fire, so this
  trigger is live rather than aspirational). Any of the three: revive the
  §10 digest runner-up rather than re-arguing bulk vault reads.
- **Secret-store consolidation got harder:** this section now documents
  "two stores, two different scrub guarantees" as intended. Anyone merging
  the file store and the vault later must update `exec_scrub`, the coverage
  registry, and this document together.
- **The shared-instance ceiling is real**: Community's stop-the-world backup
  grows with this data forever; the retention valve delays, not removes,
  that trajectory.
- **Moving exec history out later** means deprecating the MCP tools and
  search surface this creates, not just dropping data.

## 12. Risks for the Coders — where this breaks non-obviously

1. **The exec fulltext templates will pass K5's pattern regex vacuously**
   (no MATCH pattern to inspect). They are only guarded once added to the
   ANN-guard's named template list — do that in the same commit as the
   template, or the guard is a lie.
2. **`runs.agent_slug` does not exist** — it's `source_slug`
   (loader docstring:30-36). And `run_events.payload` contains literal
   `\u0000` that must be cleaned client-side, never cast server-side.
3. **MERGE-upsert ≠ the test loader's CREATE.** The v2 loader CREATEs into a
   wiped graph; production templates must MERGE on `{tenant, bucket, id}`
   and re-SET mutable fields, or the 48 h re-sweep silently duplicates.
4. **Single worker + tunnel 30 s cut**: an over-large ingest batch doesn't
   fail loudly, it starves live search and then gets cut mid-request.
   Bound batches by bytes (the loader's `BATCH_BYTES` trick), not row count
   alone — run text is wildly variable.
5. **Fulltext index creation is only instant on an empty label.** Create the
   exec indexes in step 1 (before any data), not after backfill, or the
   single worker owns a long index build.
6. **The K7 test must seed through the real routes**, not through graph.py
   directly — the leak K7 exists to catch lives in route-level scope
   resolution (the Topic-leak precedent), which a graph-layer-only test
   never exercises.
7. **The sync job's Postgres view of "tenant" is AP-MT's; the write side's
   is knowledgeable's.** The mapping is explicit config (§2). Hardcoding
   `tenant-bootstrap` on the write side reproduces the exact class of bug
   `resolve_tenant_id`'s minting branch exists to prevent.
8. **The vault coverage check (§6 point 1 correction) must list, never
   read.** `list_secrets` returns names and metadata without approval;
   `read_secret` puts a prompt on a human's phone — calling it from a
   10-minute tick is an incident, not a bug. Reach the secrets app over the
   app→app loopback; if it is absent or the backend link is down, report
   `vault_coverage_unknown` on the sync status and carry on — the same
   degrade-visibly-never-fatally contract `workspace_secret_values()`
   already keeps. And the registry's unit test uses invented representative
   shapes only; a real value in that file is the leak the whole module
   exists to prevent. While touching `exec_scrub.py`, also fix its module
   and `workspace_secret_values()` docstrings, which still claim "the set
   of known-live secret VALUES is enumerable" unqualified — scope the claim
   to the file store, mirroring this section.
9. **exec-view (§10 decision) gates in the backend dependency, never the
   frontend.** The SPA's AuthGate is presentation; the allowlist check is
   the router dependency. The K7 extensions land in the same commit as the
   router: a pinned endpoint list for the new router (the
   test_exec_isolation.py:646 shape — and enumerating the router's own
   routes, not `app.routes`, per commit 13507ab's lesson), an
   authenticated-but-not-allowlisted human → 404 with the sentinel absent,
   cross-tenant → nothing, and the generic surfaces still returning zero
   exec nodes.
10. **The subset validator is load-bearing, not decoration.** Exec-viewer
    subs ⊆ owner subs is §6 point 2's equivalence argument in code; without
    it, adding a viewer is a silent population widening. Refuse boot, same
    pattern as config.py:382. It also bakes in a named coupling (§11):
    granting exec-view requires owner-subs membership, which remaps that
    person's entire knowledge tenant.
11. **Single worker is still single.** exec-view search shares the one
    production worker with sync and knowledge search: bound page sizes, no
    unbounded run-text list rendering, detail view fetches one run.
12. **Correct the stale "until T2" claims in the same change, in place.**
    `api/exec_history.py`'s module docstring and `core/exec_history.py`'s
    third bullet both still say humans get no exec surface "until T2
    per-token bucket scopes make bucket grants real". After §10's decision
    the true statement is: humans get no surface through *these* routes
    ever; the human surface is the exec-view router, owner-allowlisted.
    Rewrite the lines — this document has twice been bitten by a correction
    appended below a stale claim.

---

## 13. The full Event-timeline projection — all 17 kinds (2026-10-19)

Card `3fe5bf3b-9510-81d0-a0de-ea04b0e9cd46`. Frederico's ask, verbatim: "eu
quero na verdade ver dentro do grafo toda a Event timeline". The definition is
AP-MT's own run detail page: `repos/agents-platform-multitenant/frontend/src/
routes/RunDetail.tsx:181` — `events.filter(e => e.kind !== "llm_token")` —
every kind except `llm_token`, chronological, one row per event. That filter
is the spec; this section does not invent its own. Supersedes the
decomposition of two archived cards (`…8170` thinking-only, `…81fd`
tool_result design); their security analysis is carried forward here, not
discarded.

**Measured 2026-10-19, live during this design (re-verified, not inherited):**
`run_events` 1,339,471 rows / 1001 MB; `llm_token` 573,500 → excluded;
**timeline = 765,971 events across 17 kinds.** Graph today: ExecRun 28,096,
ExecToolCall 290,447, sync fresh, scrub healthy (10 exact values, 16 pattern
families, vault coverage clean). Neo4j `/data` 4.0 GB. **Host disk 92 % used,
36 GB free — AT §8's retention-valve tripwire line already**, before this
feature adds a byte. `run_events.id` is its own varchar PK, and the table has
a `node_id` column (workflow node attribution) — both matter for the shape
below.

### 13.1 Per-kind verdicts — the blocking decision, one row each

Payload shapes were sampled from production (truncated, client-side), not
guessed. Sizes are `pg_column_size(payload)` avg/max.

| kind | n | payload (measured) | verdict | slice |
|---|---|---|---|---|
| `tool_call` | 308,430 | `{id, name, input}` 506 B/9.3 KB | **already projected** (ExecToolCall) | — |
| `tool_result` | 307,934 | `{tool_use_id, content}` 1.3 KB/20.7 KB | **project, onto ExecToolCall as `result_text`** — scrubbed, capped, secret-tool results DROPPED by suffix match. The one genuinely dangerous kind: results are the channel through which secrets enter a run (a `collect_secret`/`read_secret` return, a `cat .env`, `docker inspect`); the exact-match pass structurally cannot cover a third-party credential that was never in the file store. Shipped last, under the §13.5 restated equivalence. | 3 |
| `thinking` | 8,367 | `{text}` 2.1 KB/9.2 KB | **project as ExecEvent text, scrubbed.** NOT credential-free (a model can restate a secret it just read — the earlier chat claim to the contrary was wrong and is corrected on the card). §6.3-analogue decided in §13.3: no per-run drop. | 2 |
| `node_end` | 28,218 | `{text, tokens_in/out, cost_usd, run_id}` 998 B/7 KB | **project as ExecEvent — text is OUTPUT-class, scrubbed**, tokens/cost as structured props. The name lies: this is not lifecycle metadata, the payload carries the node's final text. Classified by measurement, not by name. | 2 |
| `node_start` | 29,087 | `{label, agent, provider, model, model_slug, run_id, parent_run_id}` 233 B | project, structured fields only | 1 |
| `finalizing` | 28,210 | `{tokens_in, tokens_out, cost_usd}` 66 B | project, structured | 1 |
| `cli.first_byte` | 25,210 | `{}` 3 B | project — the ts IS the value (first-byte latency) | 1 |
| `system.init` | 23,561 | `{session_id, model, cwd, tools[names], permission_mode}` 587 B / **max 787 B** | **project with a FIELD ALLOWLIST** (`model`, `cwd`, `permission_mode`, `tools_count`, `session_id`). Measured: uniformly bounded, tool NAMES only, **no env snapshot today** — but the payload shape is owned by the upstream CLI, which is exactly where an env snapshot would appear later, so only named fields ever cross; an added field is invisible to the graph by construction. | 1 |
| `done` | 4,195 | `{}` or `{status}` | project, structured | 1 |
| `cli.error` | 1,615 | `{subtype, is_error}` 45 B/112 B | project, structured | 1 |
| `error` | 719 | `{error: msg}` 107 B/1.1 KB | project, **text scrubbed** — error strings embed connection strings and DSNs; the `url-userinfo`/`env-assignment` families cover the measured shapes | 1 |
| `auto_compact.blind` | 179 | `{session_id, threshold, completed_runs}` | project, structured | 1 |
| `auto_compact.ineffective` | 165 | token/cooldown numbers | project, structured | 1 |
| `connector.timing` | 42 | `{phase, epochs, duration_s}` | project, structured | 1 |
| `container.started` | 31 | `{}` | project (ts-only) | 1 |
| `log` | 4 | `{msg}` | project, text scrubbed | 1 |
| `cli.timeout` | 2 | `{timeout_s}` | project, structured | 1 |
| `llm_token` | 573,500 | `{delta}` streamed chunks | **never.** Matches the page's own definition; deltas reassemble into assistant text that is already projected scrubbed as `output_text`; 573k rows of pure cost with zero retrieval value per row. If reassembled per-node assistant text for workflow runs is ever wanted, that is a new ask, not "the timeline". | — |
| *(any future kind)* | — | unknown | **excluded by default, visibly** — §13.6 registry | — |

### 13.2 Found live during this design: the secret-tool drop never fired for prefixed names

§6 point 3's drop is `if name in SECRET_TOOL_NAMES`
(`exec_scrub.py:scrub_tool_args`) — exact match on the BARE name. Production
tool names arrive gateway-prefixed in at least three spellings (measured via
`search_execution_graph(search_in='name')`):
`mcp__aw-gateway__aw__…`, `aw-gateway.aw__…`, `mcp__workspace-gateway__aw__…`.
Consequence, verified in the live graph: prefixed `write_secret` calls landed
with `args_text` intact, and because `write_secret` writes to the VAULT, the
values were vault-only (no exact match) and shapeless (no pattern fired) —
**at least three real secret values are in the graph and in its S3 dumps in
plaintext** (secret names: `zadarma_account_password`, `mac_home_vnc_password`,
`knowledgeable-explorer-apmt-key`; the values are deliberately not repeated
here). The triple hole is exactly the class this card's security analysis
feared, found already live in the narrow tool_call projection.

**Measured exactly when slice 0 shipped (2026-10-19): 34 rows, not three.**
`search_execution_graph(q='write_secret', search_in='name')` returns 34
`ExecToolCall` rows with `args_text` present (`possibly_truncated: false`),
spread over all three prefixes — `mcp__aw-gateway__aw__…` 29,
`aw-gateway.aw__…` 4, `mcp__workspace-gateway__aw__…` 1. Eleven distinct
real credential names beyond the three above, several with multiple
superseded generations; six throwaways with invented values. The names are
enumerated on the rotation card (`3fe5bf3b-9510-811a-9454-f2eac2acc2b4`),
never the values. Worth stating plainly because "at least three" read as a
bounded incident and it was an order of magnitude larger — the sentence that
was accurate about what had been *seen* was not a count.

The other three names in `SECRET_TOOL_NAMES` are a far smaller exposure by
shape, and that is an argument for purging them anyway rather than against:
`read_secret`/`delete_secret` arguments carry a secret NAME, and
`collect_secret`'s carry a request id. The value lives in the RESULT, which
slice 3 is what drops.

Containment honesty: §6 point 2's equivalence holds — every authorized graph
reader could already read these in Postgres — so no NEW population gained
access. What changed is durability (graph + backups). Remediation:

1. **Fix (slice 0, ships before everything) — SHIPPED 2026-10-19:** match
   the terminal segment — the substring after the last `__` or `.` —
   against `SECRET_TOOL_NAMES`, in BOTH copies (connector
   `exec_scrub.scrub_tool_args` and aw-knowledgeable's server-side refusal
   in `core/exec_history.py`), with tests pinning all three measured
   spellings plus the bare name.

   Landed as a `tool_name_segment` + `is_secret_tool` pair in each repo,
   duplicated rather than shared for the same reason `SECRET_TOOL_NAMES`
   already is (the connector is a plain HTTP client with no import of the
   service's code). **Segment EQUALITY, not `endswith`** — the obvious
   over-correction, and the one the tests in both repos pin negatively: a
   hypothetical `spread_secret` / `misread_secret` ends with `read_secret`
   and must keep its arguments. Both wrong rules (the old exact match, and
   `endswith`) were confirmed to turn the new tests red.
2. **Purge — SHIPPED 2026-10-19:** a catalog template nulling `args_text` on
   every ExecToolCall whose name's terminal segment is a secret tool (the
   `exec_trim_toolcall_args` shape, name-predicated); re-verify by searching
   the graph for the affected rows. Postgres keeps the originals (§5);
   the next S3 dump after the purge no longer carries them — older dumps do,
   for their retention life.

   Landed as `exec_drop_secret_toolcall_args` (batched, tenant+bucket in the
   pattern, one `$bare` per call looped in Python so the segment-safe
   predicate stays visible to a reader instead of hiding inside an `any()`
   lambda), stamping `args_dropped_at` rather than the valve's
   `args_trimmed_at` — a routine retention eviction and a remediated
   exposure are different events and an operator reading a nulled row needs
   to know which one it was. Exposed as service-authed
   `POST /api/exec/purge-secret-args`, with a connector route
   (`POST /api/apps/knowledgeable/exec-sync/purge-secret-args`) and
   `aw-workspace-cli knowledgeable-exec-sync purge-secret-args` in front of
   it. It takes no body on purpose: a caller-supplied name list would turn a
   remediation into a general "null the args of any tool I name" primitive.
   Idempotent, so it stays wired rather than being a one-off script — the
   next addition to `SECRET_TOOL_NAMES` already has its backfill button.
3. **Rotation is a separate security card** (filed with this design): the
   three named values must be treated as exposed — though their exposure
   population was always "anyone who can read AP-MT Postgres", the backups
   widen the blast radius of an S3 compromise.

The design lesson folded into everything below: **never exact-match a tool
name; a tool's identity is its terminal segment.** The result-drop list in
slice 3 uses the same suffix rule from day one.

### 13.3 Node shape — one `ExecEvent` label; `tool_result` rides `ExecToolCall`

**One `ExecEvent` label with a `kind` property, not a label per kind.**
Label-per-kind fights the house machinery everywhere at once: the catalog is
static (`exec_counts`' UNION arms, the per-label wipe templates, constraints
and K5 guard lists are all enumerated per label — 17 kinds would multiply
each), fulltext indexes are per-label (17 indexes), and §13.6's open-set fact
means a new kind would be a schema change. With one label, a new kind is one
registry row.

- **Identity:** `(tenant, id)` unique, `id` = `run_events.id` — the table has
  its own PK; no synthetic `run_id+seq` needed (unlike ExecToolCall, which
  predates this observation and keeps its shape).
- **Properties:** `id, run_id, kind, ts, node_id`, `text` (nullable, scrubbed,
  capped — §13.4), plus flat per-kind allowlisted props (`agent`, `label`,
  `provider`, `model_slug`, `tokens_in/out`, `cost_usd`, `status`, `subtype`,
  `cwd`, `permission_mode`, `tools_count`, `threshold`, `duration_s`, …) set
  from the §13.6 registry's field lists. **No `payload_json` catch-all** —
  a passthrough would smuggle every future unreviewed field (the system.init
  env-snapshot scenario) past the review this section exists to force.
- **Edge:** `(r:ExecRun)-[:EMITTED {tenant}]->(e:ExecEvent)`, both endpoints
  MATCHed on tenant (the K3 shape). No edge to any non-Exec node (§7).
- **`tool_call` stays ExecToolCall** — already projected; duplicating 308k
  calls as events would double-store them.
- **`tool_result` lands ON the existing ExecToolCall node** as `result_text`
  (scrubbed, capped), `result_ts`, `result_len`, `result_truncated`,
  `result_dropped` — paired client-side inside the sync batch via
  `tool_result.payload.tool_use_id` ↔ `tool_call.payload.id` (both measured
  present). Rationale: the call/result pair is one retrieval unit ("what did
  that call return"); it saves ~308k nodes on a disk-tight instance; and
  MADE_CALL stays the single tool chain. Unpaired results (measured ~500 of
  308k — calls that never returned, or cross-batch strays) are counted on the
  sync status (`orphan_results`), not silently dropped-and-forgotten.
- **Timeline reconstruction** = `exec_run_detail`'s events
  (ORDER BY ts) interleaved with its tool calls (call at `ts`, result at
  `result_ts`) — the full page-equivalent timeline, from the graph alone.
- **Fulltext:** one new per-label index `exec_event_text` over
  `ExecEvent(text)` (created in the schema slice, while the label is empty —
  §12 risk 5), and one new index `exec_toolcall_result` over
  `ExecToolCall(result_text)` — a NEW index rather than a rebuild of
  `exec_toolcall_text`, so the live name/args search surface is untouched
  while the one-off build over the populated label runs (off-hours; the
  single worker owns it for its duration, which is why it must not ride the
  existing index).
- **MCP/tool contract — extended, never repurposed:** `search_in=name|args`
  and their `matched_field` semantics stay byte-identical. New, explicit
  arms: `target='events'` (rows carry `kind`, `matched_field='event_text'`)
  and `search_in='result'` for tool_calls (`matched_field='result_text'`).
  A caller that never opts in sees exactly today's behaviour.

**The §6.3-analogue decision for `thinking` (and `node_end`), decided in
writing as the archived card demanded:** no per-run drop rule. Reasoning
emitted by a run that called a secret-handling tool is projected, scrubbed,
like everything else. Grounds: (1) the channels where a secret appears *by
definition* — a secret tool's args AND its result — are dropped wholesale
under the suffix rule, so what remains in thinking/output is a *restatement*
risk; (2) `output_text` — the same restatement risk, demonstrably — has been
projected scrubbed since v1; treating reasoning more harshly than output
would be incoherent with the design's own §6; (3) the residual is contained
by §13.5. What would change this: a measured restatement leak (a planted
canary surviving into thinking text past the scrubber) — that evidence would
reopen this as a per-run drop keyed on suffix-matched secret-tool usage,
which the batch already computes (`rollup_called`).

### 13.4 Size bounding

`EVENT_TEXT_MAX = 16_384` chars and `RESULT_TEXT_MAX = 16_384` chars,
enforced connector-side at build time (after scrubbing, so a truncation can
never bisect-and-expose a value the scrubber would have matched — truncate
the scrubbed text, never scrub the truncated text). Measured p95s are well
under (thinking 5.1 KB, tool_result 5.7 KB, node_end 2.8 KB); the cap
touches <5 % of events and exists for the unbounded tail (`llm_token`'s
63 KB max shows what one event can be). `*_len` carries the true length and
`*_truncated` makes the cut visible. §10 point 4's rule extends verbatim:
search surfaces return match-centred snippets (`args_text_snippet`'s exact
shape, the fix for the measured 83,609-char overflow); full stored text only
through `exec_run_detail`.

### 13.5 Does §6 point 2 survive? Yes — restated, carrying more weight

The population-equivalence argument's content is unchanged and still true:
every authorized reader of exec graph text (service callers; §10 exec-view's
owner-allowlisted operators) can already read the unscrubbed originals in
AP-MT Postgres. A scrubber miss grants nothing to anyone who didn't have it.
What this projection changes is not the argument but the load on it:
`tool_result` raises the per-event miss probability (it is the secret
channel, and the third-party-credential hole is structural — §13.2's finding
is the proof it bites in practice), and the durable surface (graph + nightly
S3 dumps) grows by ~0.5 GB of scrubbed text. So the equivalence is restated
as load-bearing rather than belt-and-braces, its §11 reopen triggers stay
exactly as written (any audience widening revives digests first), and four
containments ship with the slices: the suffix drop (args AND results), the
caps, the retention valve extension, and the default-exclude kind registry.

### 13.6 Governance — the three-state kind registry

The kind set is OPEN — minted at `core/executor.py`'s `emit()`, at
`bus.publish` sites across `api/runs.py`/`api/telegram.py`/`api/
playground.py`, and forwarded from runner CLIs (`system.init`, `cli.*`,
`thinking`); `auto_compact.*` appeared in 2026 with no schema change. So,
exactly as the vault-coverage registry (§6 point 1) made "new and unreviewed"
visibly distinct from "known safe":

- `EVENT_KIND_REGISTRY`, committed in `aw-app-knowledgeable` beside
  `exec_scrub.py`: `kind → {verdict: structured|text|excluded, fields: [...],
  text_field, reason}` — §13.1's table, as code. A unit test pins every
  `text`-verdict kind to a planted-fake-credential scrub test and every
  field list to names that exist in sampled payloads.
- **A kind not in the registry is NOT projected**, and the tick reports it in
  `event_kinds_unreviewed` on the sync status → surfaced by the existing
  doctor check (the `vault_names_uncovered` mechanism, proven to fire and
  clear). New kind ⇒ visible degradation ⇒ a card ⇒ an Architect verdict ⇒
  one registry row. Maintainer: whoever owns the doctor alarm, same as vault
  coverage today.
- **Server-side mirror:** aw-knowledgeable's ingest refuses an event row
  whose `kind` is not on its own allowlist (the `SECRET_TOOL_NAMES`
  two-copies-on-purpose pattern — a connector that regressed must not be
  able to land an unreviewed kind just because it forgot).
- **Kind-level (18 rows), not tool-level (662 live tool names)** — the
  tool_result card's suggested per-tool result allowlist is rejected below
  precisely because at 662 entries the registry would rot, and a rotten
  allowlist reads as review that never happened.

### 13.7 Cost, quantified (card demand: no hand-waving)

Raw scrubbed-text/property volume by slice, from the measured
`sum(pg_column_size(payload))`:

| slice | nodes/props added | raw payload | est. store+Lucene |
|---|---|---|---|
| 1 (13 structured kinds) | ~113,000 ExecEvent + EMITTED | ~25 MB kept fields | ~100–150 MB |
| 2 (thinking + node_end) | ~36,600 ExecEvent (text) | ~46 MB | ~100–150 MB |
| 3 (tool_result on ExecToolCall) | 0 new nodes; ~308k props | ~397 MB | ~0.8–1.2 GB |
| total | ~150k nodes (~0.37× current) | ~470 MB | **~1.0–1.5 GB** |

Context: `/data` is 4.0 GB today; host disk 92 % used / 36 GB free — already
AT §8's stated tripwire. Growth: run_events accrues ~29 MB/day raw, of which
the timeline's projectable text is ~13 MB/day → ~30–40 MB/day store-side.
Decisions that follow from the numbers rather than around them:

- **Slices 1–2 (~0.3 GB) are within noise and proceed.**
- **Slice 3 ships WITH the retention valve extension in the same slice** —
  `exec_trim` grows arms for `result_text` and `ExecEvent.text` older than
  `ARGS_TEXT_RETENTION_DAYS` (180 d) — bounding steady-state text at
  ~2.3 GB raw / ~5–7 GB store. Not a follow-up: at these volumes the valve
  is part of the feature, not an option on it.
- **Slice 3's backfill gets a disk preflight:** refuse to start when host
  free space < 25 GB, report why. The 92 % disk is a platform problem with
  its own trajectory (host-disk card); this feature must not be the straw
  that silently breaks it, nor hostage to it for the cheap slices.
- Backup window: dump moves at disk speed; +1.5 GB ≈ +low tens of seconds
  nightly. The §8 tripwire (`downtime_seconds` > 600 s) is unchanged and
  stays the signal to revisit venue.

### 13.8 Backfill and the watermark — no second watermark

Events ride the existing run-scoped selection: the sync fetches events for
the runs each batch selected, so the run watermark + 48 h non-terminal
re-sweep already bound event freshness — a run only emits events while
non-terminal, so a terminal run's event set is complete when the re-sweep
last saw it. No per-event watermark, no second frontier to corrupt
(`_watermark_of`'s complete/incomplete distinction keeps protecting both).

Historical events (everything synced before each slice ships) need a one-off
**watermark-reset backfill** per slice: the existing bounded-tick mechanism
(§8: ~15 ticks × 2,000 runs), off-hours, MERGE-idempotent against the
already-loaded runs. The Postgres trap is honored by construction: the event
query is `SQL_TOOL_CALLS`' shape with `kind = ANY(%(kinds)s)` — payload
selected RAW, parsed client-side by psycopg, NUL-stripped (`_clean`) —
`->>`/`::text` never appear (the `rollup_called` crash is the precedent).

### 13.9 Slices — safest first, each independently shippable

- **Slice 0 — URGENT, independent of everything (coder-ready now):** §13.2's
  fix. Suffix-segment matching for `SECRET_TOOL_NAMES` in both copies +
  tests pinning the three measured spellings; purge template for landed
  secret-tool `args_text`; live verification that the named rows are gone;
  rotation card filed. Blocked on: nothing.
- **Slice 1 — mechanism + the 13 structured/low-risk kinds:** ExecEvent
  label, constraint, `exec_event_text` index (created empty), EMITTED edge,
  upsert/detail/search/counts/wipe templates + K5 list additions + K7
  extension (ExecEvent invisible through every generic surface), the
  §13.6 registry + `event_kinds_unreviewed` doctor wiring + server-side kind
  refusal, connector fetch/build/scrub for the 13 kinds, `error`/`log`
  planted-credential scrub tests, backfill, live count verification
  (~113k events vs SQL). Blocked on: nothing — this document is the decision.
- **Slice 2 — the text kinds `thinking` + `node_end`:** registry rows flip
  to `text`, planted-fake-credential tests per kind (including a
  vault-style shapeless canary proving the §13.3 decision's stated residual
  honestly), backfill (~37k events), live verification incl. a real
  reasoning hit via the new `target='events'` arm. Blocked on: slice 1.
- **Slice 3 — `tool_result`:** result pairing in the connector, suffix-rule
  result DROP (+ server-side refusal of result text on secret-tool names),
  scrub + cap, `exec_toolcall_result` index (off-hours build),
  `search_in='result'` arm, retention valve extension, disk preflight,
  off-hours backfill, live verification with planted-credential tests for
  result-shaped fixtures (a `cat .env` output, a `docker inspect` dump, a
  git remote URL with token, a `collect_secret` return → dropped). Blocked
  on: slices 0+1 (not on 2).

QA reviews each slice's delivery; `mark_flow_done` only after the last slice
with a QA run id, per the flow contract.

### 13.10 Rejected alternatives (this section's own)

- **Label-per-kind (`ExecThinking`, `ExecNodeStart`, …).** Multiplies every
  per-label enumeration in the static catalog (counts, wipes, constraints,
  K5 lists, indexes) by 17 and turns every new kind into a schema change.
  One label + `kind` + registry row wins on every axis that matters here.
- **`tool_result` as separate ExecEvent nodes.** +308k nodes on a 92 %-full
  disk to represent something that is 1:1 with an existing node; splits the
  call/result retrieval unit across two fulltext hits. Runner-up; revisit if
  unpaired results stop being rare or per-result provenance (its own id/ts
  beyond `result_ts`) becomes load-bearing.
- **A generic scrubbed-payload passthrough (`payload_json` property).**
  Projects every future unreviewed field by default — the exact
  env-snapshot-in-`system.init` scenario the card told this design to look
  for. Field allowlists are the point, not an implementation detail.
- **Per-TOOL result allowlist (the archived tool_result card's suggested
  shape).** 662 live tool names, open set, no owner who could keep verdicts
  honest at that cardinality — the registry would become exactly the
  "chronically-absent review that reads as approval" its precedent exists to
  prevent. Kind-level registry + channel-level deny (suffix-matched secret
  tools) + scrub + cap instead.
- **Dropping `thinking` for runs that touched a secret tool (§6.3
  analogue).** Decided against, with grounds and a reopen condition, in
  §13.3 — incoherent with `output_text`'s existing treatment, and the
  by-definition channels are already dropped.
- **Projecting reassembled `llm_token` text.** Redundant with `output_text`
  (and with `node_end.text` per workflow node after slice 2); 573k rows of
  deltas is cost with no per-row retrieval value. If per-node assistant
  prose is ever wanted, it is a new ask against `node_end`, not the stream.
- **A second server-side scrubber for event text.** §6's single-scrubbing-
  point argument is unchanged; the server's role stays refusal (kinds,
  secret-tool names), never redaction.
- **Blocking all slices on the disk situation.** Slices 1–2 are ~0.3 GB on
  36 GB free; holding the whole ask hostage to a host-level trajectory this
  feature didn't create would be scope-shrinking by stealth. The expensive
  slice carries the preflight instead.

### 13.11 What this makes harder later (beyond §11, which stands)

- **Per-kind placement changes become data migrations.** Everything shares
  one label, so "move thinking to its own label later" is a property-scan
  rewrite, not a rename. Accepted: the registry makes per-kind *policy*
  cheap even though per-kind *storage* is uniform.
- **`result_text` on ExecToolCall couples call and result lifecycles** —
  trimming, wiping and (a future) per-result provenance all operate through
  the call node. Splitting later = backfill-shaped migration (cheap only
  because Postgres stays the record).
- **Default-exclude means a new kind is invisible until a human acts.**
  Deliberate — the doctor alarm is the mitigation, and the alternative
  (default-include) is how an env snapshot walks into the graph unreviewed.
- **More durable scrubbed text in every backup**, for the backups' retention
  life — the §13.5 restatement is now carrying real weight, and any future
  audience widening (§11 trigger 1) got strictly more expensive to grant.

### 13.12 Risks for the Coders — where this breaks non-obviously

1. **Never exact-match a tool name** — §13.2's lesson. Identity is the
   terminal segment after the last `__` or `.`; pin all three measured
   prefix spellings in tests, in both copies.
2. **`tool_call.payload` keys its id as `id`; `tool_result` as
   `tool_use_id`.** The pairing join is across those two names, within one
   run's rows in one batch.
3. **`node_end` is text-bearing despite its lifecycle-sounding name** — it
   is slice 2, not slice 1; putting it in the structured slice ships
   unscrubbed output text.
4. **Create `exec_event_text` while ExecEvent is empty** (schema commit,
   before any backfill — §12 risk 5); `exec_toolcall_result` is the one
   index that MUST build over a populated label — off-hours, and never as a
   modification of `exec_toolcall_text`.
5. **The event SQL must stay raw-payload** — `kind = ANY(...)`, parse
   client-side, `_clean` every string (the `rollup_called` crash recurs
   otherwise, and the first N ticks passing proves nothing — the NUL rows
   arrive when they arrive).
6. **Scrub BEFORE truncating**, never after — a cap applied first can cut a
   secret in half and hide it from both passes while leaving it greppable.
7. **K7 extension in the same commit as the templates**: ExecEvent must be
   unreachable through `search_nodes`/`get_node`/`get_graph`/`/api/search`/
   `list_documents`, and the exec fulltext guard list
   (`test_k5_exec_fulltext_templates_filter_tenant_and_the_reserved_bucket`)
   gains the two new templates or it is lying.
8. **`event_kinds_unreviewed` must count DISTINCT kinds seen, not rows** —
   a burst of one new kind must read as one decision owed, and the tick must
   still project everything registered (degrade visibly, never fatally —
   the `vault_coverage_unknown` contract).
9. **The registry's planted-credential tests use invented shapes only** —
   a real value in a fixture is the leak the module exists to prevent
   (§12 risk 8 verbatim, now for event text).
10. **Backfill ticks share the single production worker** with live search
    and the 10-minute sync: keep `MAX_RUNS_PER_TICK`/`BATCH_BYTES` bounds,
    run resets off-hours, and remember the lease means a long backfill tick
    delays, not corrupts, the scheduled tick.
