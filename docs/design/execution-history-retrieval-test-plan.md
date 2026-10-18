# Execution-history retrieval: pgvector vs Neo4j vs SQL — the deciding experiment

**Status:** test-plan design (card `3fd5bf3b-9510-8137-9e4d-c4b3d4a49d93`,
Architect, 2026-10-18). Nothing here is executed — the Coordinator dispatches
execution and builds the presentation from the results.

**The question, Frederico verbatim:** *"quero saber se a busca no pgvector vs
neo4j vs tools de sql, se vamos ter ganhos. Estou inclinado a jogar tudo pro
neo4j, mas queria rodar um teste agora pra saber se as tools cobrem de forma
satisfatória e manter o histórico de execução em grafo seria mais problema do
que solução."*

Companions this builds on, not re-derives:
`docs/design/apmt-execution-history-search.md` (the measured three-loss-layer
analysis of the vector arm and the per-query-class table in its §8) and
`docs/design/execution-index-migration-off-aw-app-kb.md` (why option
A/knowledgeable was rejected for the index's *home*; this card asks the prior
question — which retrieval surface is worth owning at all).

---

## 1. The decision (disagree with this, not with the details)

1. **Win criterion: "an agent with no prior context gets there unaided."**
   Not "the right answer was obtainable" — that criterion favours SQL by
   construction (any answer is obtainable from a Turing-complete surface) and
   therefore measures nothing. §2 justifies this.
2. **Four arms, run as ablations:** structured MCP tools (T), raw SQL (S),
   pgvector semantic (V), Cypher over a graph (G). Each arm is a fresh,
   memory-less agent scoped to exactly that arm's access, answering the same
   10 questions, graded against SQL-established ground truth.
3. **The graph arm is testable today only as a metadata graph in a throwaway
   Neo4j** (§5). Against the *production* Neo4j it answers 0/10 questions —
   no execution data lives there, extraction is OFF by flag, and the
   embedding dims don't match. The test prices the graph honestly by
   splitting the Neo4j option into its three separable claims (§5.1) and
   measuring the one that is measurable without unauthorized spend.
4. **Coverage is controlled explicitly** (§4): questions are tiered by
   whether their ground-truth runs are inside the 9.4%-coverage vector index,
   and a semantic miss on an unindexed run is scored as a **coverage-miss**,
   a separate failure class from a semantic failure.
5. **Pre-registered decision rubric** (§8): what result pattern leads to
   which action, written down before the first run, so the outcome can't be
   narrated into the preferred answer afterwards.

One position stated up front because it bounds the whole comparison: even a
maximal graph win cannot mean "move the execution history into Neo4j". AP-MT
is the multitenant system of record (28,817 runs, 1.32 M events, never
purged, growing ~20 MB/day); its `run_events` payloads carry live
credentials (apmt-execution-history-search.md §5) and its tenancy is
enforced in exactly one codebase. What a graph win would justify is a
**projection** — a metadata graph fed from Postgres, the same relationship
the pgvector index already has to the source — never a change of the system
of record. The test decides between "tools/SQL suffice", "keep pgvector as
the recall layer" (already paid for), and "add a graph projection". "Jogar
tudo" is off the table on tenancy and secrets grounds regardless of scores,
and this doc says so rather than letting the test imply otherwise.

## 2. The win criterion, fixed

**Measured: unaided cold-agent success.** A fresh agent session — no
conversation history, no auto-memory, no KB search, no lessons — receives
one question and one arm's access, and must produce the answer with citations
(run ids). Grading is against ground truth the executor froze beforehand.

Why this criterion and not "obtainable":

- The consumer of execution history **is** agents. `search_execution_history`
  exists because agents ask "have we seen this before" mid-task; nobody
  proposes a human dashboard here. The retrieval layer pays for itself
  exactly when it converts an agent's cold question into a right answer
  without a human writing the query — so that conversion rate is the thing
  to measure.
- "Obtainable" collapses the comparison: I can always write the SQL, and the
  card's own ground-truth column proves it — every question below *has* an
  exact SQL answer. Under "obtainable", SQL scores 10/10 by construction and
  the test is decided before it runs.
- Unaided-cold is also the honest frame for the graph question: Frederico's
  instinct ("jogar tudo pro neo4j") is really a bet that a graph makes the
  history *navigable without prior knowledge*. That bet is testable — it
  predicts the Cypher arm beats the SQL arm on traversal questions for a
  cold agent. If a cold agent does equally well with a recursive CTE or
  `run_tree`, the bet loses on its own terms.

Secondary metrics, recorded per cell (question × arm), used as tie-breakers
and for the cost half of "mais problema do que solução": tool calls to
answer, total tokens, wall-clock, and — scored as the *worst* outcome, below
honest refusal — **confident-wrong answers**. The platform measures these
for free: every arm run is itself an AP-MT run with `tokens_in/out`,
`cost_usd`, duration.

## 3. The arms (exact access, nothing more)

All arm agents: same model (`sonnet` — scoped-MCP profiles are bypassed by
codex agents, documented trap), same effort, dedicated throwaway agents
created via `create_agent` with minimal system prompts and deleted after.
Prompt template is fixed (§7.3). No `search_knowledge_base`, no memory, no
`search_lessons` on any arm — contamination control.

| Arm | Access (allowlist, exhaustive) | What it represents |
|---|---|---|
| **T — structured tools** | `list_runs`, `run_tree`, `get_run`, `get_run_detail`, `run_events`, `list_target_runs`, `target_summary`, `list_retro_scores`, `list_sessions`, `list_targets` — **not** `search_execution_history` | "as tools cobrem de forma satisfatória?" — the fixed-shape surface as shipped |
| **S — raw SQL** | `remote_host_exec_run` (host_id=`bare-metal-privileged`) only. The prompt gives the connection recipe — `docker exec aw-stack-aw-postgres-1 psql -U postgres -d agents_platform_multitenant` — and nothing else: no schema, no column names. Discovering the schema is part of the measured work; discovering *which container* holds the data is ops knowledge, not retrieval, so the recipe is given. | the "SQL tools" option at its strongest |
| **V — pgvector semantic** | `search_execution_history` + `get_run_detail` (the tool returns pointers by design; without the follow-up it can't answer anything) | the index as deployed: db `execution_index` on `aw-remote-host-postgres`, served by the exec-index service (`exec_index_store.py:183-191` — `vector(768)` + HNSW cosine; `exec_index_embed.py:29-30` — nomic-embed-text-v1.5; 25,368 chunks / 2,714 runs at freeze) |
| **G — Cypher over metadata graph** | Bash access to `cypher-shell` against the throwaway Neo4j of §5.2 (runner containers reach podman siblings — documented). Connection recipe given; schema not (Cypher has `CALL db.schema.visualization`; schema discovery is part of the work, symmetric with arm S). | what "execution history as a graph" can be *today, without unauthorized spend* |
| **X — everything (optional)** | T ∪ V | production reality; run only if budget allows, after the ablations — it measures deployment, not marginal value |

## 4. The coverage control (load-bearing, so explicit)

The vector index covers **2,714 of 28,817 runs = 9.4%**, because
`execution_index_mode: "interesting"` (`execution_index.py:476,598`) drops
runs with no error and no tool_call, retention is 30 days / 80k rows
(`exec_index_store.py:56-57`), and the capture hook is fail-open with
measured ~25% capture of eligible runs (apmt doc §1, loss layer 3). A test
that ignores this measures the gate, not the technology.

Control, two parts:

1. **Tier A questions (technology questions):** before dispatch, the executor
   verifies every ground-truth run id is actually in the index —
   `SELECT count(*) FROM executions WHERE run_id = '<id>'` against
   `postgresql://postgres:<pw>@127.0.0.1:5432/execution_index` from the
   workspace container (by container name `aw-remote-host-postgres:5432`
   from app containers; `127.0.0.1` is refused there — documented). A
   question whose GT runs aren't indexed is swapped for one whose runs are,
   or demoted to Tier B. On Tier A, a vector-arm miss is a **semantic
   failure** and counts fully.
2. **Tier B questions (system-as-deployed questions):** deliberately include
   runs outside the index (older than 30 days, gate-excluded, capture-missed).
   On Tier B, a vector-arm miss is recorded as **coverage-miss**, a separate
   class — it is a true fact about the deployed system and belongs in
   Frederico's answer, but it must not masquerade as "vector search doesn't
   work". Both numbers are reported side by side.

The same tiering applies to arm G for a different reason: the metadata graph
holds no payload *text* (§5.2), so text-dependent questions are scored for G
as **structural-miss** — again a separate class, because it prices what the
graph lacks, not how Cypher performs.

## 5. The graph arm, head-on

### 5.1 The Neo4j option decomposes into three claims — only one is testable now

1. **Semantic retrieval in the graph.** Needs every chunk re-embedded:
   knowledgeable is BAAI/bge-m3 at **1024 dims**
   (`repos/aw-knowledgeable/backend/app/core/embeddings.py:88`, dim read
   from the model at load, `:320-323`); the exec index is **768**
   (`exec_index_embed.py:29`). The re-embed path **does not exist** (open
   card since 2026-09-27; the dim-mismatch boot guard hard-raises). This is
   the same wall that killed option A in the migration design
   (`execution-index-migration-off-aw-app-kb.md` §4). Even once built, same
   vectors ≈ same neighbours: vector search quality is store-independent
   given the same embeddings, so **the V arm's scores are the upper bound
   for this claim** and nothing about Neo4j improves them. Not re-measured;
   priced (§9).
2. **The entity/relation layer** — the thing that makes a graph more than a
   vector store. **Extraction is globally OFF by flag**
   (`repos/aw-knowledgeable/backend/app/config.py:160`, gated at
   `ingest/extraction.py:61-66`); the config comment itself names the stake
   (an ~$18k full-corpus backfill needs a human-owned durable OFF). Full
   corpus extraction is **not authorized**, and there is a standing veto on
   new LLM provider keys (ap-mt only). Extracting 28.8k runs is exactly the
   unapproved thing. **So this claim is out of the test by design** — the
   test does not and must not design around an unapproved spend. Stated
   plainly for the presentation: *without extraction, a graph of execution
   history is a vector store plus a metadata DAG, carrying Neo4j's
   operational cost.* If Frederico wants claim 2 tested, that is a separate
   authorization decision (a bounded pilot — e.g. 100 runs through the
   existing extractor via ap-mt keys — is the smallest honest experiment,
   roughly 100 runs × ~8 chunks × 1 LLM call; it still proves little,
   because entity value shows at corpus scale). This doc deliberately does
   not include it.
3. **Structural traversal** — Cypher over the run DAG (`parent_run_id`,
   `session_id`, `qa_run_id`, `notion_task_id`, `flow_slug`, tool-call
   edges). **This is testable today, cheaply and with zero LLM calls**, and
   it is where the graph could genuinely beat SQL/tools for a cold agent.
   Arm G measures exactly this.

### 5.2 The arm-G harness: throwaway Neo4j, metadata only

A throwaway `neo4j:5.26.4-community` container on the workspace podman host
(combined `NEO4J_AUTH` var, re-read the bridge IP after any restart —
documented harness traps). Loader is a one-off script (~200 lines; CSV via
`\copy` from `aw-stack-aw-postgres-1`, hand files to the sandbox via the
workspace docker volume, `LOAD CSV` or the python driver). **Not**
knowledgeable's production Neo4j: writing a new node family there means new
`graph.py` catalog templates under the K5 guard, tenant stamping, and
single-worker write load — a real build, pointless for a test, and it would
pollute the production corpus.

Schema (what a metadata projection would actually be):

```
(:Run {id, agent_slug, status, model_slug, source_slug, flow_slug,
       flow_outcome, hop_count, cost_usd, tokens_in, tokens_out,
       started_at, ended_at})
(:Run)-[:CHILD_OF]->(:Run)          // parent_run_id
(:Run)-[:IN_SESSION]->(:Session {id})
(:Run)-[:QA_OF]->(:Run)             // qa_run_id
(:Run)-[:ON_CARD]->(:Card {page_id})// notion_task_id
(:Run)-[:CALLED {n}]->(:Tool {name})// run_events kind='tool_call',
                                    // payload->>'name' ONLY, aggregated
```

**No payload text, no input/output/error bodies, ever.** The raw payloads
hold live credentials (apmt doc §5); copying them into an unhardened
throwaway container would be a self-inflicted leak. Tool *names* are not
secrets. Consequence, scored honestly: arm G cannot answer text questions
(Q2, Q8, Q9) — recorded as structural-miss, because a real graph projection
would face the same choice (ship text into Neo4j and inherit the redaction
problem, or stay metadata-only and stay text-blind).

Load size: 28.8k `Run` nodes; ~295k tool_call events aggregate to far fewer
`CALLED` edges. Minutes to load, no embeddings, no LLM, ~zero cost.

## 6. The 10 questions

Conventions for every question:

- **T0** = freeze instant, chosen by the executor at execution start; every
  ground-truth query and every question text is bounded `ended_at < T0` so
  live growth can't drift the answer. All arms run within 48 h of T0 (the
  index prunes daily; Tier A membership is checked at freeze).
- `:tenant` = this workspace's tenant id. **Step 0 for the executor:** run
  `\d runs` once, confirm the tenant column and the timestamp column names
  (the SQL below uses `tenant_id`, `started_at`, `ended_at` per the 48-col
  list on the card and `apmt-execution-history-search.md`; adjust if `\d`
  disagrees — that is a 2-minute check, not a redesign), and resolve the
  tenant id. **Trap, repeated because it will bite:** the live db is
  `agents_platform_multitenant`; `agents_platform` on the same server is the
  old single-tenant one with 1 run.
- `payload` is `json`, not `jsonb` (`models.py:1159` per the apmt doc):
  `payload->>'name'` works; jsonb operators don't without a cast. There is
  no index on `ts`/`ended_at` — GT queries seq-scan ~1.3 GB; bound each with
  the time filters shown and expect seconds-to-low-minutes, run them in a
  `screen`-safe way (the 600 s silent-command watchdog is real).
- Ground truth is established **once**, by the executor, with these queries,
  and frozen into a fixtures table (answer + GT run ids per question) before
  any arm runs. Where a question needs a concrete anchor (a card id, a leaf
  run id, a rare tool name), the executor picks it at freeze using the
  selection query given, and substitutes it into the question text for all
  arms identically.

Grading classes: **pass** / **fail** / **honest-refusal** (said it can't
answer with its access — scores above fail) / **confident-wrong** (scores
below fail) / **coverage-miss** (V on Tier B) / **structural-miss** (G on
text questions).

---

### Q1 — semantic recall, Tier A (the known-good benchmark)

**Question:** "Have we dealt before with fs-sync pending deletions needing
approval or decline? Which runs, and how was it resolved?"

(a) **Ground truth** — the three known runs (re-verified twice in the apmt
doc at scores 0.74–0.78: `acd7bcca…`, `99c18af1…`, `b252008f…`), confirmed
at freeze by:

```sql
SELECT DISTINCT r.id, r.agent_slug, r.ended_at
FROM runs r JOIN run_events e ON e.run_id = r.id
WHERE r.tenant_id = :tenant
  AND e.kind = 'tool_call'
  AND e.payload->>'name' LIKE '%pending_deletions%'
ORDER BY r.ended_at;
```

(b) **Semantic:** `search_execution_history(query="fs-sync delete policy approve pending deletions", n_results=5)` → `get_run_detail` on hits.
(c) **Graph:** today, in prod: **impossible** (no execution data in Neo4j).
In the harness: `MATCH (r:Run)-[:CALLED]->(t:Tool) WHERE t.name CONTAINS 'pending_deletions' RETURN r` — answers the *which runs* half; the *how resolved* half needs text → structural-miss on that half.
(d) **Pass:** names ≥2 of the 3 GT run ids and the resolution matches the
cited runs' outputs (executor checks against `get_run_detail`).

### Q2 — paraphrase recall, Tier A (vector's home turf, worded to defeat grep)

**Question:** "A run once fixed a problem where Redis authentication worked
after being repaired live, but the fix was silently lost on restart. Find
that run."

Deliberately shares almost no tokens with the run's own vocabulary
(`ACL SAVE`, `aclfile`) — a cold SQL agent must guess keywords.

(a) **Ground truth** (executor freezes the canonical run id):

```sql
SELECT r.id, r.agent_slug, r.ended_at
FROM runs r
WHERE r.tenant_id = :tenant
  AND (r.output ILIKE '%acl save%' OR r.output ILIKE '%acl list%'
       OR r.output ILIKE '%aclfile%')
ORDER BY r.ended_at;
-- widen into run_events (e.payload::text ILIKE '%ACL SAVE%') if runs.output is thin
```

Tier A gate: the chosen run must be in `execution_index` (§4 check); if not,
swap anchor to another paraphrase-shaped incident that is (executor's pick,
same selection recipe: find via memory-known incidents, verify via ILIKE,
verify indexed).

(b) **Semantic:** `search_execution_history(query="redis auth fix lost after restart not persisted", n_results=5)`.
(c) **Graph:** **impossible today and in the harness** (needs text or
vectors; vectors need the nonexistent 1024-dim re-embed). Structural-miss.
In a hypothetical post-re-embed Neo4j: `db.index.vector.queryNodes` with a
query vector from the embedder — i.e. exactly what pgvector does now, one
engine over.
(d) **Pass:** GT run id among the cited ids (top-5).

### Q3 — exact audit, Tier B (completeness is the point)

**Question:** "Exactly how many distinct runs called the tool
`approve_pending_deletions` before T0? List the run ids."

(a) **Ground truth:**

```sql
SELECT count(DISTINCT e.run_id) AS n_runs,
       array_agg(DISTINCT e.run_id) AS ids
FROM run_events e JOIN runs r ON r.id = e.run_id
WHERE r.tenant_id = :tenant AND r.ended_at < :t0
  AND e.kind = 'tool_call'
  AND e.payload->>'name' LIKE '%approve_pending_deletions%';
```

(b) **Semantic:** `search_execution_history(query="approve pending deletions tool call")` — structurally cannot guarantee a count (index holds ≤26.5% of tool events, 9.4% of runs). Expected correct behaviour for V is **honest refusal** on the count; a confident count is confident-wrong.
(c) **Graph (harness):** `MATCH (r:Run)-[:CALLED]->(:Tool {name:'approve_pending_deletions'}) WHERE r.ended_at < $t0 RETURN count(DISTINCT r), collect(r.id)` — **possible**, metadata only. Prod: impossible.
(d) **Pass:** exact count + exact id set.

### Q4 — aggregation, Tier B

**Question:** "Top 5 agents by total `cost_usd` over the 14 days before T0,
with totals."

(a) **Ground truth:**

```sql
SELECT agent_slug, round(sum(cost_usd)::numeric, 2) AS total_usd, count(*) AS n
FROM runs
WHERE tenant_id = :tenant
  AND ended_at >= :t0 - interval '14 days' AND ended_at < :t0
GROUP BY 1 ORDER BY 2 DESC LIMIT 5;
```

Known caveat, inherited identically by every arm so it doesn't skew the
comparison (but must be footnoted in the presentation): **openai-backed runs
record `cost_usd = 0`** — the totals are recorded-cost, not true spend.

(b) **Semantic:** cannot aggregate; per-hit cost only. Expected honest refusal.
(c) **Graph (harness):** `MATCH (r:Run) WHERE r.ended_at >= $t0_minus_14 AND r.ended_at < $t0 RETURN r.agent_slug, round(sum(r.cost_usd),2) AS total ORDER BY total DESC LIMIT 5` — **possible** (Cypher aggregates fine; the graph's weakness is not aggregation).
(d) **Pass:** same 5 agents, same order, totals within 1%.

### Q5 — flow lineage, Tier B (graph turf, shallow)

**Question:** "For Kanban card `<CARD>`: which runs delivered it — who
designed, who coded, who reviewed — in what order, and what was the flow
outcome?"

Anchor selection at freeze (executor): a completed software-engineering flow
card with ≥1 architect + ≥1 coder + ≥1 QA run, e.g.:

```sql
SELECT notion_task_id, count(*) AS n,
       array_agg(DISTINCT agent_slug) AS agents
FROM runs
WHERE tenant_id = :tenant AND notion_task_id IS NOT NULL AND ended_at < :t0
GROUP BY 1
HAVING array_agg(DISTINCT agent_slug)::text[] && ARRAY['architect']
   AND count(*) BETWEEN 4 AND 12
ORDER BY max(ended_at) DESC LIMIT 10;  -- pick one with a clean chain
```

(a) **Ground truth:**

```sql
SELECT id, agent_slug, status, flow_slug, flow_outcome,
       parent_run_id, qa_run_id, started_at
FROM runs
WHERE tenant_id = :tenant AND notion_task_id = :card
ORDER BY started_at;
```

(b) **Semantic:** `search_execution_history(query="<card title words>")` then follow pointers — indirect at best; likely partial.
(c) **Graph (harness):** `MATCH (r:Run)-[:ON_CARD]->(:Card {page_id:$card}) OPTIONAL MATCH (r)-[:QA_OF]->(q) RETURN r.id, r.agent_slug, r.status, r.flow_outcome, r.started_at ORDER BY r.started_at` — **possible**. Prod: impossible.
(d) **Pass:** all GT run ids, correct role attribution, correct order,
correct outcome. (Arm T note: `list_runs` filtered by `notion_task_id` +
`run_tree` should do this in ~2 calls — this question tests whether the
tools' shapes are discoverable cold.)

### Q6 — deep ancestry, Tier B (the sharpest graph-vs-SQL discriminator)

**Question:** "Run `<LEAF>` is somewhere inside a dispatch chain. Find the
root run of its chain, and the total number of descendant runs that root
produced (direct and indirect)."

Anchor selection: a leaf with `hop_count >= 3`
(`SELECT id FROM runs WHERE tenant_id=:tenant AND hop_count >= 3 AND ended_at < :t0 ORDER BY ended_at DESC LIMIT 20` — pick one whose root has a non-trivial tree).

(a) **Ground truth:**

```sql
WITH RECURSIVE up AS (
    SELECT id, parent_run_id FROM runs WHERE id = :leaf
  UNION ALL
    SELECT r.id, r.parent_run_id FROM runs r JOIN up ON r.id = up.parent_run_id
),
root AS (SELECT id FROM up WHERE parent_run_id IS NULL),
down AS (
    SELECT id FROM root
  UNION ALL
    SELECT r.id FROM runs r JOIN down d ON r.parent_run_id = d.id
)
SELECT (SELECT id FROM root) AS root_id,
       (SELECT count(*) - 1 FROM down) AS descendants;
```

(b) **Semantic:** no path — pointers don't traverse. Expected honest refusal
or partial (find the leaf's detail, read `parent_run_id`, stuck without a
walk tool).
(c) **Graph (harness):**

```cypher
MATCH (leaf:Run {id:$leaf})-[:CHILD_OF*0..]->(root:Run)
WHERE NOT (root)-[:CHILD_OF]->()
WITH root
MATCH (d:Run)-[:CHILD_OF*1..]->(root)
RETURN root.id, count(DISTINCT d) AS descendants;
```

**Possible**, and this is the question Cypher is *for*. Prod: impossible.
(d) **Pass:** exact root id + exact descendant count.

### Q7 — join + rate, Tier B

**Question:** "Among agents with ≥20 runs that called `mark_flow_done`,
which has the highest error rate on those runs, and what is the rate?"

(a) **Ground truth:**

```sql
WITH called AS (
  SELECT DISTINCT e.run_id
  FROM run_events e JOIN runs r ON r.id = e.run_id
  WHERE r.tenant_id = :tenant AND r.ended_at < :t0
    AND e.kind = 'tool_call' AND e.payload->>'name' LIKE '%mark_flow_done%'
)
SELECT r.agent_slug,
       round((count(*) FILTER (WHERE r.status = 'error'))::numeric
             / count(*), 3) AS err_rate,
       count(*) AS n
FROM runs r JOIN called c ON c.run_id = r.id
GROUP BY 1 HAVING count(*) >= 20
ORDER BY err_rate DESC LIMIT 1;
```

(b) **Semantic:** cannot. Honest refusal expected.
(c) **Graph (harness):** possible —
`MATCH (r:Run)-[:CALLED]->(:Tool {name:'mark_flow_done'}) WITH r.agent_slug AS a, count(*) AS n, sum(CASE WHEN r.status='error' THEN 1 ELSE 0 END) AS errs WHERE n >= 20 RETURN a, round(1.0*errs/n,3) ORDER BY 2 DESC LIMIT 1` (note the harness aggregates tool calls per run-tool pair; `DISTINCT r` semantics must match GT's per-run counting).
(d) **Pass:** correct agent + rate within 1 percentage point.

### Q8 — open semantic synthesis, Tier A (rubric-graded)

**Question:** "What approaches have past runs taken to Caddy
certificate/TLS problems? Cite the runs."

(a) **Ground truth** — a candidate set, not a single answer:

```sql
SELECT DISTINCT r.id, r.agent_slug, r.ended_at, left(r.output, 300)
FROM runs r
WHERE r.tenant_id = :tenant AND r.ended_at < :t0
  AND r.output ILIKE '%caddy%'
  AND (r.output ILIKE '%cert%' OR r.output ILIKE '%tls%'
       OR r.output ILIKE '%acme%')
ORDER BY r.ended_at;
```

Tier A gate: ≥2 of the candidate runs must be indexed; otherwise re-anchor
the topic (same recipe) to one that is.

(b) **Semantic:** `search_execution_history(query="caddy certificate TLS renewal problem", n_results=8)` + details.
(c) **Graph:** structural-miss (text question). Prod: impossible.
(d) **Pass (rubric, graded by the Coordinator, not an LLM judge):** cites ≥1
GT run id AND the described approach is consistent with the cited run's
actual output. Partial credit recorded but pass/fail is binary on that bar.

### Q9 — older than the index window, Tier B (coverage class by design)

**Question:** "In September 2026 a run created the Neo4j backup script
(`backup-neo4j.sh`). Find it and state what downtime decision it made."

(a) **Ground truth:**

```sql
SELECT r.id, r.agent_slug, r.ended_at
FROM runs r
WHERE r.tenant_id = :tenant
  AND r.ended_at >= '2026-09-01' AND r.ended_at < '2026-10-01'
  AND (r.output ILIKE '%backup-neo4j%' OR r.input ILIKE '%backup-neo4j%');
```

(The downtime fact, for grading: Community Edition has no online backup; the
script stops the container for `neo4j-admin database dump` —
`repos/aw-stack/scripts/backup-neo4j.sh` header.)

(b) **Semantic:** any query — the run predates the 30-day prune horizon.
Scored **coverage-miss** when it fails (it will); if V *refuses with the
reason* ("index covers ~30 days"), record that too — tool-description
quality is part of the system.
(c) **Graph:** structural-miss (text). Prod: impossible.
(d) **Pass:** GT run id + the stop-the-container downtime fact.

### Q10 — trustworthy absence, Tier B (prove a negative)

**Question:** "Before T0, did any run ever call the tool `<RARE>`? And the
tool `<NEVER>`? For each: if yes, which runs; if no, say no."

Anchor selection at freeze: from

```sql
SELECT e.payload->>'name' AS tool, count(DISTINCT e.run_id) AS n_runs
FROM run_events e JOIN runs r ON r.id = e.run_id
WHERE r.tenant_id = :tenant AND e.kind = 'tool_call'
GROUP BY 1 ORDER BY 2 ASC LIMIT 40;
```

pick `<RARE>` = a real tool with 1–3 calling runs; `<NEVER>` = a real tool
name from the gateway list verified to have **zero** rows.

(a) **Ground truth:** the query above, filtered to the two names.
(b) **Semantic:** structurally cannot prove absence (9.4% coverage). Correct
V behaviour: honest refusal on `<NEVER>`; finding `<RARE>` is a bonus, not
required.
(c) **Graph (harness):** possible — `MATCH (r:Run)-[:CALLED]->(:Tool {name:$name}) RETURN collect(r.id)`; an empty result *in the harness* is a true negative (the harness loads all tool_call names).
(d) **Pass:** both halves right: exact ids for `<RARE>`, a clean "no" for
`<NEVER>`. A fabricated run id or a "probably not" stated as "no" from arm V
is confident-wrong — this question exists to catch exactly that.

---

**Class balance:** semantic 3 (Q1 Q2 Q8) · exact/audit 2 (Q3 Q10) ·
aggregation 2 (Q4 Q7) · traversal/structure 2 (Q5 Q6) · coverage/age 1 (Q9).
Graph-answerable today (harness, metadata only): Q3–Q7, Q10 = **6/10**;
graph-answerable against production Neo4j today: **0/10**.

## 7. Protocol

1. **Freeze (executor, ~1 h):** pick T0; run step-0 schema/tenant checks;
   run all GT queries; pick the anchors (Q2 run, Q5 card, Q6 leaf, Q10 tool
   pair); run the Tier A index-membership checks; write the fixtures table
   (question → final text with anchors substituted, GT answer, GT run ids,
   tier) into `.tmp/exec-retrieval-test/fixtures.json` and attach it to the
   card. GT SQL runs via `remote_host_exec_run` on `bare-metal-privileged`.
2. **Harness (executor, ~2 h):** throwaway Neo4j + loader per §5.2. Sanity:
   node/edge counts vs SQL counts (`count(Run)` = runs rows; spot-check 3
   `CHILD_OF` chains against recursive CTE output).
3. **Arm agents:** 4 throwaway agents via `create_agent`, scoped per §3,
   sonnet, minimal system prompt. Fixed prompt template per question:
   *"Answer using only the tools available to you. Cite run ids for every
   claim. If your access cannot answer the question, say exactly that and
   why, instead of guessing."* The refusal sentence is load-bearing — it is
   what separates honest-refusal from confident-wrong.
4. **Dispatch:** 10 questions × 4 arms = 40 runs (+ optional arm X = 50).
   One run per cell; re-run only on infrastructure failure (watchdog kill,
   dispatch 404), never on a wrong answer. Dispatch in small batches, not a
   40-wide fan-out (concurrent runs die together when the host stalls —
   documented). Record every run id in the fixtures file; the runs' own
   AP-MT rows are the cost/latency measurements.
5. **Grading:** mechanical per the pass criteria; Q8 by Coordinator rubric.
   Output: a 10×4 matrix of {pass, fail, honest-refusal, confident-wrong,
   coverage-miss, structural-miss} + per-cell cost columns.
6. **Teardown:** delete the 4 agents, kill the throwaway Neo4j, keep the
   fixtures + results files.

Budget: ~40–50 sonnet runs, most of them short; GT + harness is one
executor-day. No new credentials, no LLM provider keys, no production writes
anywhere.

## 8. Decision rubric — pre-registered, so the result can't be narrated

Predictions, falsifiable, written before any run:

- **P1:** Arm T passes ≥7 of the 8 non-semantic-only questions (Q3–Q7,
  Q9, Q10, Q1's "which runs" half). If P1 holds, *"as tools cobrem de forma
  satisfatória"* is answered **yes** for the structural/audit classes.
- **P2:** Arm V is the only arm that passes Q2, and passes ≥2 of {Q1, Q2,
  Q8}. If P2 holds, the pgvector layer has demonstrated unique value —
  **keep it** (it is already paid for; ~zero marginal cost).
- **P3:** Arm G does **not** beat arm T or arm S on unaided correctness on
  Q5/Q6 (it may tie). If P3 holds, the graph adds no retrieval capability
  over what exists, and the Neo4j move is **declined**: all cost (§9), no
  measured gain.
- **P4:** Arm S pays a visible cold-start tax — schema discovery turns,
  and/or at least one wrong-database or missing-tenant-filter incident.
  This prices "an agent can always write SQL" honestly.

Decision table:

| Result pattern | Decision |
|---|---|
| P1 ∧ P3 (whatever P2) | **No graph.** Tools + pgvector as deployed; file the known tool gaps (below) as ordinary cards. |
| P1 fails on specific questions | Fix the losing tool in place — the remedies are already designed: `search_run_events` for the audit/text class (`apmt-execution-history-search.md` §3), a `run_tree`-from-any-node fix if Q6 is what failed. A tool gap is a card, not a migration. |
| P3 fails (G decisively beats T and S on Q5/Q6 for cold agents) | The graph signal is real — but the shape it justifies is a **metadata graph projection** (the §5.2 schema, fed incrementally from AP-MT, Postgres stays source of truth), not moving the history. Only then price that build properly. |
| V fails Tier A semantic questions outright | The index earns nothing even on its home turf → candidate for retirement; that would be the surprising result worth re-checking before acting on. |

"Decisively beats" means: G passes where both T and S fail, on ≥2 traversal
questions — not "the Cypher was shorter". Elegance is not the criterion;
unaided correctness is.

## 9. What the graph buys and costs — straight, for the presentation

**Buys, today (extraction off, no re-embed):** Cypher's variable-length-path
ergonomics over the run DAG (Q6-shaped questions), and nothing else that
SQL/tools don't already have. The DAG is small and shallow (hop_count is
single digits); recursive CTEs and `run_tree` cover it.

**Buys, hypothetically:** (1) with a re-embed — semantic search in the same
store as the KB corpus; retrieval quality ≈ today's pgvector (same-vector
upper bound, §5.1), so the gain is *consolidation*, not capability. (2) with
extraction ON — entity/relation queries across runs ("every run that touched
aw-remote-host-postgres"), the only genuinely new capability — and it is
locked behind an unauthorized corpus-scale LLM spend (the KB corpus's
phase-2 was priced ~$18k; the run corpus is bigger and grows ~824 runs/day).

**Costs, concretely:**
- Full re-ingest with re-embedding at 1024 dims: the path **does not exist**
  (open card since 2026-09-27), and building it is a prerequisite, not a
  detail.
- The index was migrated **yesterday** (v0.179.0, dual-write stopped hours
  before this doc); a Neo4j move would be the third home in a week, re-doing
  the §3.3 cutover choreography of the migration doc against a store with
  harder write semantics.
- knowledgeable prod is **single-worker** by constraint; execution history
  adds a write-per-run stream (~824/day mean) to it.
- Neo4j Community backup requires stopping the container
  (`repos/aw-stack/scripts/backup-neo4j.sh`); the shared-Postgres home got a
  restore-verified nightly backup pipeline **on 2026-10-18**
  (`docs/design/aw-remote-host-postgres-backup.md`) that a Neo4j move walks
  away from.
- New write seam: every node family in knowledgeable goes through
  `graph.py`'s catalog templates under the K5 tenant guard — a real build
  with review surface, vs zero build for the status quo.
- The metadata-projection variant (the only shape a win justifies) still
  adds: a sync pipeline to keep honest, a second query surface to teach
  agents, and a store the app framework doesn't manage.

## 10. Rejected alternatives

- **"Right answer was obtainable" as the criterion** — favours SQL by
  construction; measures the test author, not the system (§2).
- **Testing the graph against production knowledgeable** (ingest runs as
  documents or new node families) — pollutes the production corpus, requires
  the K5-guarded template build just to run a test, adds write load to a
  single-worker service, and the Documents/Chunks schema is the wrong shape
  for runs anyway. The throwaway harness answers the same question for ~0
  cost.
- **A 100-run extraction pilot inside this test** — it spends LLM calls in
  exactly the direction that is vetoed, and a 100-run entity graph can't
  demonstrate corpus-scale value anyway (entity queries pay off on
  cross-corpus joins). Named as a separately authorizable follow-up; not
  designed around.
- **An LLM judge for grading** — 9 of 10 questions are mechanically
  checkable (ids, counts, sets); adding judge noise to dodge one rubric
  question is a bad trade. Q8 is graded by the Coordinator.
- **N=3 repetitions per cell** — triples cost to measure variance the
  decision rubric doesn't need (the rubric keys on class-level patterns, not
  single-cell margins). If one cell decides the whole rubric, re-run that
  cell, not the matrix.
- **A fifth arm: NL→SQL translation layer** — already rejected with reasons
  in `apmt-execution-history-search.md` §9; re-testing it here would
  re-litigate a decided design.
- **Testing on the full 28.8k-run corpus via `mode='all'` re-index first**
  ("fix coverage, then test") — re-indexing everything to make the vector
  arm look better tests a configuration nobody has approved (§7 of the apmt
  doc prices it: binding 80k-row cap in ~16 days) and delays the answer by
  days. The tiered design measures both the technology and the deployed gate
  without touching either.

## 11. What this makes harder later

- **The result is only as general as these 10 questions.** They were chosen
  to span the known query classes (apmt doc §8), but a future use case
  outside them — especially entity-shaped questions — is explicitly
  unmeasured, because measuring it is unauthorized. The decision this test
  produces must be recorded as "graph buys nothing *that is authorized
  today*", not "graph buys nothing".
- **Frozen ground truth goes stale.** T0-bounded answers are wrong a week
  later; anyone re-running the matrix must re-freeze, not reuse
  `fixtures.json`.
- **A "no graph" outcome will be cited against future graph proposals** —
  including ones predicated on extraction being funded, which this test
  deliberately did not evaluate. §9 exists so the presentation carries that
  boundary; if it gets dropped there, this doc is the record.
- **The throwaway harness is throwaway.** If P3 fails and a projection gets
  built, the harness schema (§5.2) is a sketch to start from, not a design —
  the real one needs sync, retention, and tenancy answers this doc skipped
  on purpose.

## 12. Risks for the executor (the non-obvious breakage)

- **The wrong-database trap is live for you too**, not just arm S:
  `agents_platform` has 1 run and looks plausible. Every GT query: `-d
  agents_platform_multitenant`.
- **Tenant filter everywhere.** There is a second tenant (~900 runs). GT
  without `tenant_id = :tenant` grades arms against answers they can't see
  (the tools arm is tenant-scoped by construction).
- **No time/payload indexes on the source** — the Q3/Q7/Q10 GT queries
  seq-scan 947 MB of `run_events`. Run them once at freeze, bounded as
  written, in `screen` or backgrounded (600 s watchdog); don't let an arm-S
  agent's slow query be killed and scored as a failure — watchdog kills are
  infra failures (re-run), and say so in the results.
- **Reachability split for the index-membership check:** `127.0.0.1:5432`
  works from the workspace container; app containers must use
  `aw-remote-host-postgres:5432`; agent-runner containers reach neither
  (documented) — run the §4 checks from the workspace container.
- **Scoped-arm integrity:** codex-based agents bypass scoped MCP profiles —
  all arms on sonnet. After creating the arm agents, verify the scoping by
  listing the tools the agent actually sees before dispatching question 1
  (new gateway tools/sessions have a visibility lag; same for removals).
- **Don't let arm agents see the fixtures or this doc.** The arm prompt is
  the question text only. An agent that greps the workspace for the card or
  this file has contaminated the cell — arms T/V/G have no filesystem
  access by their allowlists; arm S has host shell by design, so its prompt
  must scope it to the database ("answer from the database only") and the
  grader should check its transcript for out-of-band reads.
- **Vector-arm scores shifted homes yesterday.** If search quality looks
  off vs the apmt doc's 0.74–0.78 benchmark, first re-run Q1's canonical
  query and compare scores to float precision — a drift means an
  embedding-path divergence from the migration (prefix/truncation), which is
  a bug to report, not a result to record (migration doc §3.3 step 4).
- **`cost_usd = 0` on openai runs** — footnote Q4 in the results, don't
  present recorded cost as true spend.
