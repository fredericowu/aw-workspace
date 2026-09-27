# aw-knowledgeable v2 — embeddings, transversal semantic search, buckets, topic tree

Status: **design, not code.** Written by the Architect agent, 2026-09-27,
against Frederico's request of the same day (card
`3e85bf3b-9510-8194-98fc-e253e65ad4a3`).

Companion to — not a revision of — `aw-knowledgeable-infra.md` (v1 infra, all
of M1–M7/D1/D2 shipped and live) and `aw-knowledgeable-buckets.md` (the bucket
model). **Nothing already Done is reopened.** Two things in those documents are
deliberately amended, and both are named as amendments in §1 and §3 rather
than quietly changed.

Verified live this session: `knowledgeable.aw.tekflox.com/api/health` → 200,
`/api/documents` → 401 (gate holds); Neo4j reachable at
`aw-stack-aw-neo4j-1` (172.22.0.11:7687), image pinned
`neo4j:5.26.4-community` (`repos/aw-stack/docker-compose.yml:91`); the app runs
**one** uvicorn worker (`Dockerfile` CMD, no `--workers`).

The one thing I could **not** verify and that the first card must verify in
its first five minutes is in §2 ("The escalation"): no Neo4j credential is
reachable from an Architect container (`repos/aw-stack/.env` does not exist in
this tree; `.aw-workspace/secrets/knowledgeable.json` holds only
`service_secret`).

---

## 0. What actually blocks the outcome Frederico named

His acceptance condition, verbatim: *"enquanto eu nao ver uma arvore de
documentos ligando um ao outro e uma forma de busca semantica transversal, nao
paramos"*.

Today, `POST /api/documents` writes the bytes to a volume and creates **one**
`(:Document)` node carrying `label`/`summary`/`size_kb`/`storage_path`
(`backend/app/api/documents.py:61-71`). There is no text extraction, no chunk,
no vector. `search_nodes` is a case-insensitive substring of `label`
(`backend/app/core/graph.py:207-214`). Links exist only where a human clicked
"Add link" (`backend/app/api/links.py:27`).

So the graph is a **dust cloud of unconnected dots with filenames on them**.
The missing piece is not a view — M7's Cytoscape view works. It is that there
is nothing in the graph for a view to show, and nothing for a search to match.

That reframes the whole request: **the "tree" is a structural layer in the
graph that does not exist yet, and the semantic search is what builds it.**
Items 1, 2, 4 and 5 of the request are one dependency chain, not four
features. Item 3 (buckets) is the scope the chain runs inside.

---

## 1. Node model: three tiers, and a registry

### Decision

```
(:Document {tenant, bucket, external_id, label, summary, storage_path,
            processing_status, processing_error, processing_attempts, ...})
(:Chunk    {tenant, bucket, external_id, document_id, chunk_index,
            heading_path, text, embedding, embedding_model, embedding_dim})
(:Topic    {tenant, bucket, external_id, level, label, summary,
            chunk_count, centroid})
(:Bucket   {tenant, bucket, name, description, created_at})   -- registry only

(:Chunk)   -[:PART_OF   {tenant}]-> (:Document)
(:Topic)   -[:PARENT_OF {tenant}]-> (:Topic)
(:Topic)   -[:COVERS    {tenant}]-> (:Chunk)
(:Document)-[:RELATED_TO{tenant, score, via}]-> (:Document)   -- derived
(:Document)-[:LINKS_TO  {tenant, type}]->(:Document)          -- human, exists
```

Every **node** pattern carries `{tenant, bucket}`; every **relationship**
carries `{tenant}` only. That is not a style choice — it is `core/graph.py`'s
K5 static guard (`graph.py:522-529`) and `aw-knowledgeable-buckets.md` §3's
derived-visibility rule. A new template that omits either fails the build,
which is the point.

### Amendment 1 — `(:Bucket)` exists as a registry node

`aw-knowledgeable-buckets.md:60-66` says bucket is "not a relationship to a
`(:Bucket)` node", and §1's rejection list gives the reason: with
`(:Document)-[:IN_BUCKET]->(:Bucket)`, "every read then pays a hop, and the
hop is on the hot path of every query".

That reasoning is about **membership**, and it still holds — membership stays
the `bucket` property, and there are **zero** edges between a `(:Bucket)` node
and its members. What §1 did not have to face is that Frederico asked to
*create* buckets ("poder criar novos buckets"), and a bucket whose membership
is a property on other nodes has **nowhere to store its own name until
something is in it**. An empty bucket cannot exist; `POST /api/buckets` would
be a no-op that returns 201.

So: a `(:Bucket)` node holding identity and metadata only, read by
`GET /api/buckets` and written by `POST /api/buckets`, **traversed by nothing
on the hot path**. Both properties hold at once — registry node for identity,
property for membership, zero hops on reads.

The alternative, `RETURN DISTINCT n.bucket`, was rejected for two reasons
beyond the empty-bucket hole: it is a full node scan on every render of the
bucket list, and it makes a typo'd bucket id indistinguishable from a real
one — which is precisely the false-premise failure
`aw-knowledgeable-buckets.md` §2 exists to prevent.

Consequences that must be built, not discovered:

- `REQUIRE (b.tenant, b.bucket) IS UNIQUE` on `(:Bucket)`. Still two columns
  (`aw-knowledgeable-buckets.md:97-109` — do not widen `(:Document)` either).
- `bucket` is a **slug**, `name` is the display string. A rename must be a
  `SET` on one registry node, never a data migration.
- **Resolving a `bucket` with no registry row is an error, not an empty
  result.** A silent empty graph for `?bucket=cardiolgy` is the exact failure
  mode buckets.md §2 is about.

### Rejected

- **A Neo4j label per bucket/topic** (`:Document:Cardiology`). Already
  rejected in buckets.md §1 (labels are schema, not data). Restated because
  the topic tree makes it tempting a second time.
- **Chunk text stored outside Neo4j** (bytes on the volume, only vectors in
  the graph). Consistent with §5's "do not store bytes in Neo4j", and
  rejected: a chunk is 1 KB of text that every search result has to render as
  a snippet, and a second fetch per result to a file offset buys nothing. The
  10 MB *source file* stays on the volume; its extracted chunks do not.
- **One `(:Chunk)` per document** (i.e. today's shape with an embedding bolted
  on). Cheapest, and it is why search cannot work: one vector for a 40-page
  PDF averages every subject in it into a point that matches nothing well.

---

## 2. Vector search: pre-filtered exact, as the escalation from ANN

### Decision

**`core/graph.py` owns `vector_search()` as one first-class operation with two
strategies and one automatic, logged escalation rule:**

1. **Primary — ANN.** `db.index.vector.queryNodes(index, k * OVERFETCH, q)`,
   then post-filter `tenant` + `bucket`.
2. **Escalation — exact, pre-filtered.** If step 1 yields **fewer than `k`**
   in-scope rows, re-run as
   `MATCH (c:Chunk {tenant:$t, bucket:$b}) WITH c, vector.similarity.cosine(c.embedding, $q) AS s ORDER BY s DESC LIMIT $k`,
   bounded by a scanned-node cap, and **log which filter starved the first
   pass** (tenant, bucket, or both).

`aw-knowledgeable-infra.md:187-204` already established the hole: Neo4j vector
indexes are per-label, not per-tenant, `db.index.vector.queryNodes` on
5.26.4-community takes no filter argument (verified live at M1, reconfirmed by
QA against production), so a post-filter "fixes the leak but **silently
degrades recall**… a correctness bug that presents as 'bad results', not as a
security finding". `aw-knowledgeable-buckets.md:398-409` then notes the bucket
filter is a **second** post-filter on the same search, so the dilution
compounds multiplicatively.

The escalation is what converts that from *silently degrades* into
*measurably escalates and says so*. The exact scan pre-filters in the `MATCH`,
so it cannot leak and cannot be diluted; it is exact, so there is no ANN
recall loss; and its cost is bounded by **this tenant's own** chunk count
rather than the global index size. Today that count is in the hundreds.

### What the first card must verify before anything else

**`vector.similarity.cosine()` availability on `neo4j:5.26.4-community`.** It
is a function (not the `db.index.vector.*` procedure family) and general
knowledge puts it in 5.18+, but I could not run it: no Neo4j credential is
reachable from an Architect container. One Cypher call answers it:

```cypher
RETURN vector.similarity.cosine([1.0,0.0], [0.0,1.0]) AS s
```

If it is **absent**, the escalation is unavailable: fall back to ANN
over-fetch with a floor and a loud log (exactly what
`aw-knowledgeable-infra.md:200-201` specified), report it as an amendment to
this section, and **do not invent a third mechanism.**

### Bounds that are not optional

- **`OVERFETCH`** and the **scan cap** are settings, not literals. The
  in-tree precedent is `_SCOPED_OVERFETCH = 80` in
  `aw-app-kb/kb_app/mcp_http.py`, which exists for the same reason.
- The scan cap is a **page-cache** constraint, not a CPU one.
  `NEO4J_server_memory_pagecache_size` is `256m`
  (`repos/aw-stack/docker-compose.yml:123`), set before the graph held
  anything. At 384 dims × 4 bytes ≈ 1.5 KB per embedding, ~50 k chunks of
  embeddings already exceed that cache. Over the cap: return what was scanned,
  log that the result is partial — never silently truncate, and never OOM the
  shared store.
- **Raising the page cache is part of this wave**, not a later optimisation:
  this is the change that makes Neo4j's memory config matter for the first
  time. It is an `aw-stack` edit and therefore an `aw-stack` deploy (infra doc
  §7.3 — do **not** "fix" that by adding a push trigger to aw-stack).

### K4 finally becomes writable

`aw-knowledgeable-infra.md:254-257` specifies K4 and it has never been
written, because there were no embeddings. It now gets three assertions, and
the third is new:

1. Query as A with `k` below B's row count → none of B's chunks appear.
2. The over-fetch returned the expected number of A-rows.
3. **Seed B large enough that A's post-filter starves, and assert the
   escalation fired** (it is observable: the returned row count is right, and
   the log line names the starving filter). Plus buckets.md §5.1's variant:
   two buckets in one tenant with near-identical embeddings, and the in-scope
   bucket still returns its full expected count.

Needs a real Neo4j — a mocked driver passes while production leaks
(`aw-knowledgeable-infra.md:844-847`).

### Rejected

- **pgvector-on-Postgres for the vectors.** The phase-1 brief proposed it
  (`.tmp/aw-knowledgeable/design-brief-phase1.md`, "Proposed v1 shape"), the
  workspace already has pgvector installed, and SQL would pre-filter tenant
  **and** bucket natively with no over-fetch at all. It is rejected because
  locked decision 2 of `aw-knowledgeable-infra.md:14` is native Neo4j vector
  indexes, and because of a reason the brief did not weigh: the entire value
  of a GraphRAG store is that retrieval goes *vector hit → graph
  neighbourhood → topic path* inside one query. Splitting the vectors into a
  second store turns every tree descent into a round-trip per level. The
  escalation in this section recovers the pre-filtering that pgvector would
  have given for free, inside the store that already holds the graph.
  **Revisit if** `vector.similarity.cosine` turns out to be absent *and* a
  single tenant's chunk count passes the point where ANN dilution is
  measurable — that combination, and only that, makes the split worth its
  cost.
- **Label-per-tenant to get a per-tenant index** (`:Chunk:T_abc123`). It
  would genuinely give a pre-filtered ANN search. Rejected for buckets.md
  §1's reason applied one level up: labels are schema, tenant ids are data,
  and label cardinality is not designed for generated values.
- **GDS / Personalized PageRank for subgraph narrowing.** LEGO-GraphRAG's
  recommendation in the phase-1 brief, and a good one. Rejected for now
  because GDS is a plugin absent from the pinned image, installing it into a
  shared aw-stack service is its own infra change, and it wants heap this
  container does not have (512 MB, `docker-compose.yml:125`). §4's topic tree
  does the narrowing at zero infra cost. **Revisit if** tree narrowing
  measurably underperforms on multi-hop queries.

---

## 3. Embeddings: `multilingual-e5-small` via fastembed

### Decision

**`intfloat/multilingual-e5-small` (384-dim, cosine) through `fastembed`,
with the model baked into the image at build time.**

The *mechanism* is copied from `aw-app-kb/kb_app/kb_pg.py:84-117` verbatim —
`fastembed`'s ONNX runtime (no PyTorch), a process-cached model handle, and
the asymmetric query/passage prefixes that model family requires. That is the
in-tree standard and two apps already run it.

### Amendment 2 — the model is not `nomic-embed-text-v1.5`

`kb_pg.py:8` uses `nomic-ai/nomic-embed-text-v1.5` (768-dim). Diverging,
deliberately, for one reason: **aw-app-kb indexes this estate's English code
and docs; aw-knowledgeable will hold Frederico's own documents, which are
substantially Portuguese.** An English-only embedding model over Portuguese
text fails as *"a busca está ruim"* and never as an error — the same class of
invisible failure as the recall dilution in §2.

Two secondary wins that are not the reason but do matter: 384 dims is **half**
the storage and half the page-cache pressure of 768 (§2's cap), and the model
is ~470 MB against nomic's ~520 MB, on a host that was at 95% disk
(memory `host-disk-near-full-breaks-apps-silently`).

### Non-negotiables

- **Bake the model into the image.** CI/CD recreates this container on every
  push (`aw-knowledgeable-cicd.md`). A lazy first-request download means the
  first search after every deploy hangs past the 30 s edge cut (memory
  `tunnel-edge-cuts-requests-at-30s`) and looks like a broken search.
- **Read the dimension from the model, do not trust this document.** A Neo4j
  vector index fixes its dimension at creation; a wrong guess means dropping
  and rebuilding the index. Confirm against
  `TextEmbedding.list_supported_models()` in the installed fastembed, and if
  that exact model is not in the catalogue, pick the nearest multilingual one
  that is and **say which** — do not silently fall back to an English model.
- **Stamp `embedding_model` and `embedding_dim` on every `(:Chunk)`.** §6.1.
- **Do not copy `kb_pg.py`'s silent truncation.** `_EMBED_MAX_CHARS = 1500`
  truncates oversized input without a word; `exec_pg.py:188` had to add an
  explicit oversized guard for exactly that. Chunk to fit; raise on overflow.

### Rejected

- **OpenAI `text-embedding-3-*`.** Better quality, and it adds an API key, a
  per-token cost, an egress dependency on the retrieval hot path, and a rate
  limit that makes a full re-embed a scheduling problem. For a corpus this
  size the quality delta does not buy those four.
- **`multilingual-e5-large` / `bge-m3`.** Better multilingual quality, ~2 GB
  of image. Not on this host.

### Amendment 3 (2026-09-27) — `jina-embeddings-v3` is rejected; the 128-token window is the real defect

Frederico asked why not `jinaai/jina-embeddings-v3`. Two answers, and the second
matters more than the question.

**jina-v3 is rejected, on licence, before quality is even weighed.** Verified
against Hugging Face's own metadata this session: `jinaai/jina-embeddings-v3` is
**`cc-by-nc-4.0` — non-commercial**. aw-knowledgeable is a multi-tenant product
with tenants, plans and billing behind aw-console; a non-commercial model in the
retrieval hot path is a licence violation baked into every stored vector, and
§8.1 already says the vectors are a full rebuild to change. It is the one
rejection here that needs no benchmark. On the merits it was otherwise the
strongest candidate on paper — 8192-token sequence length, ~100 languages,
1024 dims.

Secondary, and independently disqualifying: fastembed ships it as
`onnx/model.onnx` **plus `onnx/model.onnx_data`** — external-data layout. That is
the same layout as `intfloat/multilingual-e5-large`, which the V1 coder measured
as failing to load in this runtime with *"External data path escapes model
directory"* (`backend/app/core/embeddings.py`, Amendment 2a). jina-v3 would
very likely fail identically. So it is not merely licence-blocked; it probably
does not run here.

**The real finding is in the model V1 actually shipped with.** §3's
`intfloat/multilingual-e5-small` **is not in fastembed's catalogue at all**, so
the V1 coder correctly escalated per this section's own instruction ("pick the
nearest multilingual one that is and say which") and chose
`sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2`. That model's real
input window is **128 tokens** — verified independently this session by loading
it and reading the tokenizer, and worth stating loudly because **fastembed's own
catalogue metadata advertises 512 for it and is wrong.** A design that trusted
`list_supported_models()` would never have seen this.

128 tokens is ~90–110 words of Portuguese. It forced the chunker off §4's
~1000-char target — `backend/app/core/chunking.py:8-12` records the concession —
and it is a defect the coder's own 12-query head-to-head **could not detect**,
because every passage in that set was short enough to fit. The visible symptom is
already in V1's live E2E: retrieved `chunk#1` of the test PDF opens mid-sentence
on *"frequencia e a forca da contraccao"*.

Three compounding costs, none of which the tie score shows:

1. Chunks ~3× smaller than designed means ~3× as many, each carrying less
   context — the standard recipe for retrieving a fragment that scores well and
   answers nothing.
2. **V3's topic tree clusters chunk embeddings and averages them into
   centroids** (`§5`). Thinner chunks make noisier centroids, and the centroid is
   the field V4 beam-searches. The defect propagates into the tree.
3. Per §8.1 this is a full rebuild to change later, and it is near-free now.

**Decision: `sentence-transformers/paraphrase-multilingual-mpnet-base-v2`.**
512-token window (verified by loading it, not read from the catalogue), 768 dims,
apache-2.0, already downloaded and already measured by the V1 coder at the same
10/12 as MiniLM on short passages. It is the same family, it is the one the coder
proved loads in this runtime, and it is a one-line change to `DEFAULT_MODEL`
because `embeddings.py` reads the dimension and the window *from the model* and
`chunking.py` derives its budget from `max_tokens()`. Amendment 2b (no
`query:`/`passage:` prefixes) stands — mpnet is symmetric too.

Be honest about what is and is not measured: the **window is a verified fact**,
the **retrieval gain from it is a reasoned inference.** The test that would
confirm or refute it is a PT query set whose answers span more than 128 tokens of
context — which is precisely the set the existing benchmark lacks.

Costs accepted: the image grows ~780 MB beyond §9.4's estimate (0.22 → 1.0 GB;
48 GB free at 89% — check before deploying), and 768 dims doubles the
page-cache pressure §2's cap is bounded by. §2 already books raising
`NEO4J_server_memory_pagecache_size` as part of this wave, so that is the same
change, not a new one.

**Runner-up, and the one thing that would change this: `Qwen/Qwen3-Embedding-0.6B-Q`.**
apache-2.0, 1024 dims, int8, 1.12 GB, **32k-token window**, 2025, and it needs
`onnxruntime>=1.23` — this host has **1.30.0**, so it is reachable. It is the
only candidate that would plausibly beat mpnet on Portuguese quality rather than
just on window. It is not the decision because it is unverified here (untested
load, and it needs an `Instruct:` query prefix, which un-does Amendment 2b), and
because mpnet removes the actual defect at zero new risk.

### The three-model bake-off: deliberately deferred, no card (2026-09-27)

This section previously argued that a measured bake-off across MiniLM-L12 /
mpnet / Qwen3-0.6B-Q on real long-form Portuguese documents was worth one card
**before** the first real corpus is ingested, because §8.1 makes the choice a
full rebuild to reverse. That was put to Frederico as a scoping call. **His
answer: no card now — proceed on mpnet.**

The reasoning is his and it is recorded here rather than argued with: mpnet is
*verified* (512-token window read off the tokenizer, apache-2.0, loads on this
host), and the point of the wave is to get something running rather than to
optimise an input to a pipeline that does not exist yet.

**This is registered as a fast-follow, not as closed.** The trigger is explicit:
*if Portuguese retrieval quality proves insufficient in real use.* Two things
the next person needs, so that re-opening it is cheap rather than archaeology:

- **The test that would settle it** is the one the V1 coder's benchmark
  structurally could not run: a Portuguese query set whose answers sit **beyond
  128 tokens into a passage**. Every passage in the 12-query tie (10/12 both
  models) fit inside MiniLM's real window, which is exactly why the tie could
  not see the defect. Reuse that harness; replace the corpus.
- **The cost of deferring is not zero and it grows.** Re-embedding today is ~free
  because no production corpus has embeddings. Once a tenant's real documents are
  ingested, the same change is a full re-embed of every chunk **plus** dropping
  and recreating the vector index (its dimension is fixed at creation —
  `core/graph.py:206-213` hard-raises on mismatch rather than mixing
  incomparable vectors) **plus** a rebuild of every `(:Topic)` centroid, since
  V3's centroids are means of chunk embeddings and are not comparable across
  models. That third cost did not exist when the question was first asked.

Qwen3-0.6B-Q remains the runner-up on the terms above, unchanged.

---

## 4. Chunking and ingestion: structure-aware, async, claimed atomically

### Decision — chunking

**Split on document structure first, then bound hard:** headings (markdown
`#`, PDF/DOCX outline) → paragraphs → a hard window of ~1000 chars with ~150
chars of overlap, never exceeding the model's input window.

Each `(:Chunk)` stores `chunk_index`, `heading_path`, and the text.
`heading_path` is load-bearing and nearly free: it is a document-native
hierarchy, it seeds §5's tree, and it is what a search result shows as
provenance instead of a bare filename.

Text extraction is new (`pypdf` / `python-docx` for the `.pdf`/`.docx`/`.doc`
extensions `config.py:28` already accepts). Read the
`aw-autoskill-pdf-text-extraction` skill before writing it.

Rejected: **fixed-size windows only** (throws away the cheapest structural
signal available); **embedding-based semantic chunking** (an embedding pass to
choose boundaries plus one to embed — 2× cost for a marginal gain at this
corpus size).

### Decision — ingestion runs async, and Neo4j is the queue

`POST /api/documents` returns 201 immediately with
`processing_status: "pending"`. A background task **inside the same
container** polls the graph for pending documents and runs extract → chunk →
embed → `ready`.

State machine on `(:Document)`: `pending → extracting → embedding → ready |
failed`, plus `processing_error` and `processing_attempts`. Reprocessing is
idempotent: delete this document's chunks (and its `RELATED_TO` edges, never
its `LINKS_TO`) before rewriting them.

- **Inline in the request** is rejected: a 10 MB PDF blows the 30 s edge cut.
- **Redis/Celery** is rejected: a fourth datastore for one queue, when the
  graph can hold the state it already has to hold anyway.
- **A separate worker container** is rejected for now: same image, same code,
  double the deploy surface, no isolation gained. **Revisit if** CPU
  contention with the API becomes measurable.

**Claim documents with an atomic Neo4j write, never a Python flag.** The
container runs one uvicorn worker today (`Dockerfile` CMD) — and adding
`--workers` is a one-line change this estate has made repeatedly, with a whole
family of consequences (memory `aw-backend-core-lease-never-runs-it-deploys-as-replica`,
`app-config-save-never-reaches-the-watchdog-leader-worker`,
`devctl-relay-in-process-singleton-vs-workers10`). A `SET status='extracting'`
inside a write transaction is correct at any worker count and costs nothing
now.

The frontend must show this state. A document that sits at `pending` with no
indication is indistinguishable from a broken upload.

---

## 5. The tree: RAPTOR-shaped topics, beam-searched

### Decision

**Build a hierarchy of `(:Topic)` nodes by recursive clustering of chunk
embeddings, one bucket at a time, and retrieve by beam-descending it.**

This is the RAPTOR shape, and it is the interpretation of *"algum algoritmo de
busca em arvore pra saber o assunto buscado"* that this design commits to.
Recording the interpretation, since the request was open:

> A search does not return a flat ranked list. It walks from a general
> subject to a specific passage, and it **reports the path it walked** —
> `Medicina > Cardiologia > Arritmias` — because that path *is* "o assunto
> buscado". A flat list cannot answer that question at all.

**Build** (the per-bucket pass `aw-knowledgeable-buckets.md:324-366` already
named as the "intra-bucket pass", now getting an implementation):

1. Read every chunk embedding in one bucket.
2. Cluster at level 0 → level 1 (k-means/agglomerative over cosine, `k` from
   a simple rule, not a hyperparameter search).
3. Per cluster, write a `(:Topic)` with `label`, `summary`, `chunk_count` and
   `centroid` (the cluster mean vector — **this is what makes the tree
   searchable**), `COVERS` its chunks.
4. Recurse over the level-1 centroids to level 2, cap at 3 levels.
5. Derive `(:Document)-[:RELATED_TO {score, via}]->(:Document)`: two documents
   are related if they share a leaf topic (`via: "topic"`) or their chunks are
   near neighbours above a threshold (`via: "embedding"`).

`label`/`summary` come from one LLM call per cluster — tens of calls per
bucket, batched, offline. If no LLM is configured, fall back to c-TF-IDF
keywords over the cluster's chunks and **mark the topic as
`label_source: "keywords"`** so a degraded tree is visible rather than merely
worse. The build must never hard-fail on a missing key.

**Retrieve** — `GET /api/search?q=…&mode=tree`:

1. Embed the query (`query:` prefix).
2. From the bucket's root topics, score children by cosine against their
   `centroid`; descend the best `b` branches (beam search, `b` ≈ 2–3) to the
   leaves.
3. At the leaves, re-score chunks exactly against the query; return top-`k`.
4. The envelope carries `topic_path` per result, the `strategy` actually used,
   and the scope (§7).

**If a bucket has no tree yet, fall back to flat vector search and say
`strategy: "flat"` in the envelope.** Never a silent empty and never a silent
downgrade — `aw-knowledgeable-buckets.md:445-449` is explicit that an agent
reading an unlabelled thin result concludes the knowledge does not exist.

### Why this, and why the derived edges are a separate type

`RELATED_TO` is deliberately **not** `LINKS_TO`. A rebuild deletes every
`RELATED_TO`, `PARENT_OF`, `COVERS` and `(:Topic)` in its bucket and writes
them again; it must be structurally incapable of touching an edge a human
created. Same reason the two render differently in §6.

### Rejected

- **RAGA-style write-time quality gates and schema auto-discovery.** The
  phase-1 brief says skipping them "is not really an option" — for a graph
  **N agents write entities into**. This wave's writers are a human upload
  form and a deterministic clustering pass; there is no garbage-injection
  problem to gate yet, and gates built against a writer that does not exist
  will be wrong. **Revisit the moment anything autonomous drives
  `POST /api/nodes`.**
- **Think-on-Graph 3.0's 5-agent reflective loop (`deep=true`).** The brief
  measures it at +8–11% on multi-hop for 2–3× latency and tokens, worth it
  only for "accuracy-critical" queries, with TURA finding ~95% of real queries
  single-intent. Ship the cheap default path; `mode=` is the seam that makes
  the expensive one additive rather than a rewrite.
- **LLM-extracted topic taxonomy instead of clustering.** The other reading of
  "tree", and the more powerful one. Rejected because it needs the ontology
  and the quality gates above, i.e. the whole undesigned construction loop,
  to produce its first node. Clustering produces a tree from the embeddings
  that §2–§4 create anyway.
- **UMAP + GMM soft clustering (RAPTOR's own choice).** Better clusters, and
  `umap-learn` + its compiled deps on a `python:3.11-slim` image for a corpus
  of hundreds of chunks is not a trade worth making yet.
- **Rebuilding the tree on every upload.** This is the cost curve the brief
  rejected Microsoft GraphRAG over (`design-brief-phase1.md:22-33`). The pass
  is explicitly schedulable/triggerable per bucket, and a new document is
  searchable by flat vector search the moment it is `ready`, tree or no tree.

---

## 6. Where the frontend lands

All of it in `frontend/src/app.js` / `index.html` / `style.css` — vanilla JS,
no framework (D1, and `aw-knowledgeable-infra.md:810-820` on why nothing is
shared with the other two SPAs).

1. **Bucket switcher + bucket view.** `state.view` (`app.js:19`) currently
   toggles `library` / `graph` (`switchView`, `app.js:63-67`). A third view
   listing buckets with document counts, and a create-bucket modal on the
   shape of the existing upload/link modals (`index.html:157`, `:207`). The
   active bucket is app state and goes on every API call.
2. **The graph overview is the topic tree.**
   `aw-knowledgeable-infra.md:526-529` rejected a whole-graph endpoint —
   "the widest leak surface and the fastest way to hang the browser" — and
   that still stands. The bucket's topic tree is the bounded projection that
   answers the same need: tens of topic nodes, not thousands of chunks, drill
   from topic → documents → M7's existing focus/depth neighbourhood. This is
   the overview entry point v1 never had, and it does not reopen §5.
3. **`RELATED_TO` renders differently from `LINKS_TO`** (derived vs. human),
   with a legend entry. The legend already exists (`index.html:102`).
4. **Semantic search in the existing top search box** (`wireTopSearch`,
   `app.js:357`), showing each result's `topic_path` and its snippet.
   Lexical substring stays the behaviour of the **link picker**
   (`app.js:612`'s `exclude=` call) — a human choosing a link target wants
   name matching, not semantic drift.
5. **Processing state on document cards** (`renderLibraryGrid`, `app.js:101`).
6. **Fix `bug:aw-knowledgeable-expand-offscreen-nodes-dense-graph` in this
   wave.** `mergeGraphData` (`app.js:179`) re-runs the layout with
   `fit: false`, so expanded neighbours can land outside the viewport. That
   card's own text says it "will bite as soon as the graph gets dense, which
   is exactly Frederico's stated goal" — and this wave is what makes it dense.
   Fixing it afterwards means his first look at a populated graph is the
   broken one.

---

## 7. Bucket scope, and the denial decision that is still open

`aw-knowledgeable-buckets.md` §2 is adopted as written, with one thing held
loosely on purpose.

- **`bucket_ctx`, a contextvar mirroring `tenant_ctx`**, bound by an
  **`async`** dependency. Not a sync generator: `aw-knowledgeable-infra.md:853-859`
  is the scar — a sync generator mints the token in one Context and unwinds it
  in another, leaving the value bound in a pooled worker for the next
  unrelated request. That reasoning is about tenant leakage; for bucket the
  same bug produces silently wrong scope, which is worse to debug.
  `core/graph.py:27-40` says not to build this in M3 — M3 is done; this is the
  milestone that builds it.
- **The scope goes in a machine-readable envelope field**, not interpolated
  into a human string: `{"results": […], "scope": {"bucket": …}, "strategy": …}`
  (buckets.md §2 mechanism 2 — the consumer is an agent that must branch on
  it).
- **`GET /api/buckets`** returns `[{bucket, name, document_count, node_count,
  in_scope}]`. Counts are safe: the caller owns the tenant.
- **K1's route plan gains the `"403-cross-bucket"` expect class**
  (buckets.md:204-210). Not optional — without it the convention lives only in
  a document, and §5.3 of that doc predicts someone will "fix" it back to 404.

**The open product question, and how the design absorbs either answer.**
Whether a tenant can ever be multi-person decides whether a cross-bucket read
answers **403 with the bucket name** (relevance boundary) or **404**
(security boundary). Frederico is being asked in parallel. Per the Product
Owner's standing recommendation this proceeds with **403-with-name**, and the
constraint that makes the answer cheap is structural: **every route raises
through one `_bucket_denied()` helper in one module.** Flipping to 404 is one
function body plus one column of K1's table. No route learns the convention
individually.

---

## 7bis. The answer arrived: a tenant is multi-person. Scopes live on the token

Frederico answered on 2026-09-27, verbatim:

> "1 tenant tem vários usuários com diferentes roles, igual no ap-mt,
> entretanto, os buckets são por tenant, nao por usuario, existe isolamento por
> bucket (poder ver tudo, ver somente um, ler/escrever) esses serão escopos do
> token de API -> um api key pode ter esse tipo de organizacao, um usuário tb
> pode ter esse tipo de organizacao, o aw-console/aw-backend é quem faz a gestao
> da identidade."

That is the "yes" branch §7 above and `aw-knowledgeable-buckets.md:213-220`
both named. **The bucket is now a permission boundary.** The paragraph above is
superseded; the sentence that saves it is the structural one — the flip really
is one helper body plus one column, and this section is that flip.

### Decision 1 — three outcomes, not a blanket flip to 404

| Token's scope for bucket B | Request | Answer |
|---|---|---|
| none | read or write | **404** — existence hidden |
| `read` (`view-all` or `view-one` covering B) | read | **200** |
| `read` only | write | **403** — existence already legitimately known |
| `write` | read or write | **200** |

**Flipping everything to 404 would be the over-correction**, and it is the
mistake I expect to be made here because "bucket is a security boundary now"
reads as "apply K1's rule". It is wrong for the read-only case: a caller who
legitimately holds `read` on B already knows B exists, so a 404 on their write
protects nothing and destroys the one next action they have — ask a tenant admin
for `write`. 404 is for *"this token has no business knowing B exists"*; 403 is
for *"you know it exists, you may not do that to it"*.

So K1's route plan gains **two** classes, replacing the single
`"403-cross-bucket"` that `aw-knowledgeable-buckets.md:204-210` specified:

- `"404-no-bucket-scope"`
- `"403-bucket-read-only"`

An unclassified route stays a failure, never a skip
(`aw-knowledgeable-infra.md:243-246`).

### Decision 2 — `GET /api/buckets` returns only what the token can see

`aw-knowledgeable-buckets.md:179-182`'s `in_scope: bool` field **is deleted.**
It was safe only under the premise that the caller owns the whole tenant; with
per-token scopes those rows leak the names of buckets the token has no business
knowing. `view-all` returns every bucket in the tenant; `view-one` returns
exactly one. The `document_count`/`node_count` counts stay — but only on rows
the token can already see.

What survives from buckets.md §2 untouched: **mechanism 1** (scope is
server-injected and absent from the tool's public schema — that is what makes it
a boundary rather than a convention) and **mechanism 2** (the scope goes in a
machine-readable envelope field, not a human string). Those were never about who
owns the tenant.

And **buckets.md §5.4 is vindicated, not reversed.** It refused a `user`
property on a node, on the grounds that if per-user visibility ever arrived the
scope must be "resolved per request from the caller's context and mapped to a
bucket set". That is exactly what arrived. Do not grow a node-level ACL.

### Decision 3 — where the scope comes from: identity is central, authorization is local

**aw-backend/aw-console own identity. aw-knowledgeable owns bucket
authorization. These are different questions and they must not be merged into
one claim.**

- **Central (aw-backend):** who the subject is, which **tenant** they belong to,
  and their **tenant-level role**. This is an *extension of what M4 already
  shipped*, and it is already the plan of record over there — not a new idea.
  `aw-backend/src/api/db_models.py:1097-1111` defines `TenantMember` as "**many
  per tenant**, which is the whole point of the table existing", with a
  tenant-level `role`; and `db_models.py:1084-1085` states plainly: *"Nothing
  reads this table yet: T1 is schema + backfill only. Minting the `tenant` JWT
  claim from it is T2."* Today `create_identity_jwt`
  (`aw-backend/src/api/identity_auth.py:138-143`) mints exactly
  `sub`/`memberships`/`iat`/`exp` — no tenant, no scopes. **T2 is the blocking
  prerequisite, and it is M4-shaped: same JWKS, same EdDSA, no new shared
  secret.**
- **Local (aw-knowledgeable):** which buckets, and read vs write.

**Why bucket scopes must NOT go in the central JWT**, even though that looks
like the tidy answer: a bucket is aw-knowledgeable's own resource and aw-backend
does not know its slugs exist. Putting per-bucket grants in the central token
would make aw-backend carry a per-app resource ACL, and creating a bucket would
become a change to the identity service. That is the wrong coupling, and it is
what "não invente um sistema de permissão paralelo aqui" actually protects
against read correctly: **do not reinvent identity; authorization over your own
resources is yours.** The same split is already how this estate works — the MCP
gateway injects `kb_index` scope per profile without aw-backend knowing any KB
path exists (buckets.md §2 mechanism 1).

### Where it lands

`backend/app/core/identity.py:250` — `resolve_tenant_id()`, already documented
in that file as "the single swap point" — gains a sibling in the same module:

- `resolve_bucket_scopes(identity) -> BucketScopeSet`, and
- `_bucket_denied(bucket, scopes, *, write: bool)` raising 404 or 403 per
  Decision 1's table.

Both in one module, one helper, one table. That is the entire enforcement
surface, which is the property §7 above bought and this section spends.

### The live bug this exposes — `one account, one tenant` is already baked in

**This is the part that is not a future problem.**
`backend/app/core/identity.py:217-247` (`_get_or_mint_tenant`) keys its SQLite
projection on `account_ref TEXT PRIMARY KEY`, with `account_ref =
str(identity.user_id)` (`identity.py:268`). So **two users of the same company
get two different minted tenant ids, and therefore two disjoint graphs.** A
multi-person tenant is not representable today — not as a missing feature, as a
primary key.

That is not an accident: `identity.py:16-21` says it deliberately copied AP-MT's
shape, and the T1 card that created `tenant_members` says of exactly that shape
*"um tenant com vários users não é representável lá… é o índice único, não
convenção, e o T2 remove aquilo"*. **aw-knowledgeable copied the defect T2
exists to remove.** Memory `ap-mt-one-account-one-tenant` records the same
property one repo over.

Consequence for sequencing, and it is the good news: the fix is the swap the
file was designed for. When T2 mints the claim, `resolve_tenant_id` reads it
instead of minting locally — one function body, in the one file, as
`identity.py:18-21` promised.

### Does this block V2? No — but it changes three things in its card

**V2 (`feature:aw-knowledgeable-v2-buckets-api`) is not blocked, provided it is
built against a scope-set seam instead of against the "caller owns the tenant"
premise.** Until T2 lands, `resolve_bucket_scopes()` returns *"every bucket in
this tenant, read+write"* for a human caller and the service tenant's buckets
for the D2 service caller. Same routes, same helper, same tests; when T2 lands,
the resolver's body changes and no route does.

What would genuinely block V2 is shipping it with buckets.md §2 as written —
`in_scope: false` rows, 403-with-name as the default, and no scope-set
indirection. Then every route learns the wrong premise and the multi-user work
becomes a rewrite of all of them, which is buckets.md §1's own K5 argument
arriving from the other direction.

So, three edits to the V2 card, all cheap and all now:

1. `GET /api/buckets` filters by the token's scope set; **no `in_scope` field.**
2. Denial goes through `_bucket_denied()` with Decision 1's table — default
   **404**, `403` reserved for read-only-on-write.
3. K1's plan gains `"404-no-bucket-scope"` and `"403-bucket-read-only"` instead
   of `"403-cross-bucket"`.

### Sequencing V2 against a T2 that is not ready (2026-09-27)

T2 is now a card (`3e85bf3b-9510-81f5-bcef-cc3805cab785`, target
`aw-backend-t2-tenant-claim`) and in flight, but V2 must not wait on it and must
not be scoped down to dodge it. The split is **not** "build the API, skip the
permissions" — that is the version that teaches every route the wrong premise.
It is a split along *where the scope set comes from*:

**V2 builds, without T2 — everything except the source of the scope set:**

- the `(:Bucket)` registry, `bucket_ctx`, `GET`/`POST /api/buckets`, the scope
  envelope, and real bucket filtering on every template;
- `BucketScopeSet` as a real type, and `_bucket_denied()` implementing
  **Decision 1's table in full** — all three outcomes, 404 and 403 both;
- `resolve_bucket_scopes(identity) -> BucketScopeSet` with its T2-less body:
  every bucket in the tenant, read+write, for a human caller; the service
  tenant's buckets for the D2 service caller.

**V2 does not build, and this is the only piece that waits for T2:** the body of
`resolve_bucket_scopes` that *reads scopes off the token*. That is one function
body — which is the whole point of §7's helper-shaped design.

**The trap this creates, and the thing that defuses it.** With a permissive
T2-less resolver, the 404/403 table is dead code in production: nothing ever
denies, so nothing ever proves the table is right, and the two K1 classes have
no route that can exercise them. Shipping it that way means the enforcement is
first exercised on the day T2 lands, which is the worst possible day to discover
it is wrong.

**So `_bucket_denied()` must be proven against an injected `BucketScopeSet`, not
against a token.** V2's tests construct the scope set directly — a set with
`read` on `a` only, one with `write`, one empty — and assert all three outcomes
of Decision 1 plus both K1 classes. When T2 lands, the *only* new thing under
test is the token→scope-set mapping; the table is already covered. Concretely:
`resolve_bucket_scopes` takes the identity and returns the set, and the routes
depend on the set, so a test overrides the dependency rather than minting a JWT.
If the enforcement can only be tested through a real token, the seam is in the
wrong place and V2 should be pushed back, not the tests weakened.

**What V2 may not claim.** Its live verification (two buckets, a document in
each) proves *filtering*, not *authorization* — a caller that holds everything
cannot demonstrate a denial. V2's report must say that in those words. The
end-to-end "token with `view-one` gets 404 on the other bucket" proof belongs to
the T2 follow-up card and nowhere else; a V2 that reports it as done is reporting
something it structurally could not have observed.

### What this makes harder later

1. **Who grants a scope is now an unanswered product question**, and it is on
   the critical path for the multi-user card in a way it was not before: a
   tenant admin needs a surface to say "user 2 gets `view-one` on
   `cardiologia`". That surface does not exist in aw-console (it manages
   workspaces, not tenants) and does not exist here. Naming it is the PO's.
2. **An API key with bucket scopes is a second credential shape**, and
   `require_identity_or_service` (`identity.py:195-208`) has only two branches
   today — human JWT, or the D2 `X-Internal-Secret` service caller with *no*
   memberships by design. A scoped API key is a third, and the service branch
   must not become the place it is smuggled in: `ServiceIdentity`
   (`identity.py:142-152`) deliberately carries no memberships precisely so an
   empty list can never be read as "all". A scoped key that resolves to an empty
   scope set must mean **nothing**, never everything.
3. **404-by-default makes a misconfigured scope indistinguishable from an empty
   graph** — §8.3's warning, now worse. The `(:Bucket)` registry-existence check
   from §1 is what separates "your token cannot see it" from "you typed it
   wrong", and it stops being a nicety.
4. **The 403/404 split is a two-line rule that will be flattened.** buckets.md
   §5.3 predicted someone would "fix" 403 into 404; the same reflex now argues
   for making *everything* 404. The two K1 classes are the mitigation and they
   are not optional.

---

## 8. What this makes harder later

1. **Changing the embedding model becomes a full rebuild.** Every
   `(:Chunk).embedding`, every `(:Topic).centroid`, and the vector index
   itself (its dimension is fixed at creation). `embedding_model` /
   `embedding_dim` per chunk are what make a half-migrated graph *detectable*
   instead of silently mixing incomparable vectors.
2. **The topic tree is derived state a user cannot correct.** If Frederico
   disagrees with where a document landed, there is nothing to edit — the next
   rebuild overwrites it. Honouring a correction needs a `PINNED_TO` edge the
   rebuild respects. Not now; but the first complaint about the tree is a
   feature request, not a bug, and should be read that way.
3. **`bucket` becomes load-bearing on the hot path.** Everything above is
   bucket-filtered, so a wrong bucket is an empty graph rather than an error.
   The registry-existence check (§1) is the only thing standing between that
   and a confident wrong answer.
4. **The graph becomes genuinely irreplaceable, and
   `feature:aw-knowledgeable-m2-neo4j-backup` is still in Backlog.** Up to now
   a lost volume cost a handful of re-uploads. After this wave it costs the
   extraction, the embeddings and the tree. **This is a Product Owner call I
   am flagging rather than absorbing: M2 should land inside this wave, not
   after it.**
5. **`/api/search` is now overloaded four ways** — tenant fanout (infra risk
   10), bucket scope, link-target widening (buckets.md §3), and `mode=`.
   Splitting it into `/api/search` (lexical, link picker) and `/api/retrieve`
   (semantic, tree) is the cleaner shape. Keeping one endpoint is a deliberate
   choice — the search box and the link picker are one widget today, and
   splitting them is churn with no user-visible gain. Recorded so the next
   reader knows it was chosen, not overlooked.
6. **A bucket delete is a batched job, forever** (buckets.md §5.2), and now it
   must also drop that bucket's chunks, topics and derived edges. Write it
   with the schema.

---

## 9. Risks for the Coders

1. **`vector.similarity.cosine` is unverified** (§2). Five minutes, first
   thing, before designing around the escalation.
2. **`pyproject.toml`'s `[tool.setuptools] packages` is an explicit list**
   (`pyproject.toml`, `packages = ["backend", "backend.app", …]`). A new
   subpackage — `backend.app.ingest`, `backend.app.retrieval` — that is not
   added there installs as nothing, and the container `ImportError`s at boot
   while local tests pass from the source tree. This will not show up in CI if
   CI runs pytest from the checkout.
3. **K5 fails closed on every new template.** `{tenant, bucket}` on every node
   pattern, `{tenant}` and **not** `bucket` on every relationship. Including
   `(:Bucket)`, `(:Chunk)`, `(:Topic)`.
4. **The image grows by roughly 700 MB** (onnxruntime + the baked model) on a
   host that has been at 95% disk. Check free space before the deploy; memory
   `ap-mt-deploy-has-a-disk-preflight-that-blocks-every-commit` is what
   happens when that is discovered by a blocked pipeline instead.
5. **A rebuild must be incapable of deleting a `LINKS_TO`.** Scope every
   delete in the pass by relationship type, not by endpoint.
6. **Neo4j is shared infrastructure.** A runaway exact scan or a
   full-bucket embedding read degrades a store that nothing else in this
   estate depends on *yet* — but the page cache is 256 MB and the heap is
   512 MB, both set before the graph held data. Bound every new query.
7. **The upload contract changes shape** (`processing_status` appears, and
   `link_count: 0` stops being the whole story). `backend/tests/test_api_contract.py`
   asserts the M6 shapes; update it deliberately rather than letting it
   drift, and keep `test_route_sweep.py`'s K1 table exhaustive — an
   unclassified route is a failure, never a skip.
