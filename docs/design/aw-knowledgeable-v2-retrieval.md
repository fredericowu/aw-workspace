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
  `NEO4J_server_memory_pagecache_size` is `1g`
  (`repos/aw-stack/docker-compose.yml:151`, raised from the `256m` this doc
  previously cited — commit `e1b9c6a`, "raise page cache 256m -> 1g for
  aw-knowledgeable's vector wave"). Neo4j stores float array components as
  **doubles (8 bytes each)**, not 4-byte floats: a V-wave coder measured this
  directly against the live store — 20,000 chunks written through the app's
  own seam (384-dim embedding + ~380 chars of text + heading_path) grew
  `/data/databases/neo4j` by 77.5 MB, i.e. **3.9 KB per chunk, measured, not
  derived**. At that rate embeddings alone exceed the current 1 GiB cache
  past roughly **~269 k chunks** — not the ~50 k this bullet previously
  claimed, which was false under either figure (50 k × 1.5 KB = 75 MB, or
  50 k × 3.9 KB = 195 MB — neither exceeds even the old 268 MB/256m cache,
  let alone the current 1 GiB one). Over the cap: return what was scanned,
  log that the result is partial — never silently truncate, and never OOM
  the shared store.
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

### Amendment 5 (2026-09-27) — `BAAI/bge-m3` supersedes mpnet; and Amendment 2a blamed the wrong cause

Frederico asked for `BAAI/bge-m3` to be evaluated against the mpnet decision,
with the same bar the 128-token defect was found at. Everything below was
**measured in an Architect container this session**, not read off a catalogue or
a model card. Harness kept at `.tmp/kn-bgem3/pt_longform.py`.

**Decision: `BAAI/bge-m3`.** The decision above (mpnet) is superseded before it
was ever applied — `embeddings.py:76` still reads
`paraphrase-multilingual-MiniLM-L12-v2`, so **no code depends on mpnet** and this
costs a one-line default change plus a new backend, not a migration.

#### The correction that matters most: external-data ONNX is NOT broken here

Amendment 2a records that `multilingual-e5-large` and `embeddinggemma-300m`
"fail to load under this onnxruntime" with *"External data path escapes model
directory"*, and Amendment 3 extrapolated from that to predict jina-v3 would
fail the same way. **That diagnosis is wrong, and it is wrong in a way that was
about to cost a model choice.**

Measured on `onnxruntime 1.30.0`, loading bge-m3's `onnx/model.onnx` +
`model.onnx_data` (2266.8 MB external data):

- through a **HuggingFace cache tree** → `FAIL: External data path escapes model
  directory`, resolving to `…/cache/blobs/8b3c6cec…`;
- the **same files copied out as real files in one directory** → `LOADED OK in
  1.3s`.

The failure is ORT 1.30's external-data path validation refusing HF's
`snapshot/…/onnx/model.onnx_data → ../../../blobs/<sha>` **symlink**, which by
construction escapes the model directory. It is a *packaging* failure, not a
model or runtime incompatibility. Consequences: external-data models are all
reachable here if materialised as real files, so e5-large and jina-v3 were
**never disqualified on runtime grounds** (jina-v3's `cc-by-nc-4.0` rejection
stands on licence alone, which is the rejection that needed no benchmark);
and **the Dockerfile bake step must materialise real files** — a bake that
leaves an HF symlink tree produces an image that loads fine in the build's own
probe and dies on first embed in production.

#### Measured facts

| | MiniLM-L12 (live) | mpnet (superseded) | **bge-m3 (chosen)** |
|---|---|---|---|
| licence | apache-2.0 | apache-2.0 | **mit** (HF `cardData`, live) |
| in fastembed 0.8.1 | yes | yes | **no — any family** |
| real output dim | 384 | 768 | **1024** (forward pass) |
| real window | **128** (hard trunc.) | 512 | **no truncation config**; 4803 tok OK measured, `max_position_embeddings` 8194 |
| weights | 0.22 GB | 1.0 GB | **2.27 GB** |
| throughput @512 tok | n/a (truncates) | 843 tok/s | **562 tok/s** |

`bge-m3` is absent from **every** fastembed 0.8.1 family — `TextEmbedding`
(37 models; only `bge-*-en` and `bge-small-zh`), `SparseTextEmbedding`,
`LateInteractionTextEmbedding`. So it cannot arrive the way MiniLM and mpnet do.

#### The retrieval test §3 said could not be run

§3 admitted the mpnet gain was *"a reasoned inference"* and named the missing
test: a Portuguese set whose answers sit **beyond 128 tokens**. It was built and
run. All 12 passages share a near-identical 228-token Portuguese preamble, so the
discriminating fact lives **only** in the tail and a 128-token window must score
at chance. Two independently-reworded 12-query sets, recall@1:

| | set A | set B | combined |
|---|---|---|---|
| MiniLM (128 tok) | 1/12 | 1/12 | **2/24** — chance is 1/12 |
| mpnet (512 tok) | 9/12 | 7/12 | 16/24 |
| **bge-m3** | 12/12 | 9/12 | **21/24** |

Read this honestly, because the two findings in it are not equally strong:

1. **The 128-token defect is confirmed emphatically and reproducibly.** MiniLM
   lands exactly on chance in both sets. The 10/12 tie that chose it could not
   see this, because every passage in that set fit its window.
2. **bge-m3 > mpnet is a genuine Portuguese *quality* win, not a window
   artifact.** Every passage is ~270 tokens — well inside mpnet's 512 — so mpnet
   saw everything and still missed. Its misses are synonym failures
   (`descanso anual`→férias, `dormida`→alojamento, `estagiar`→estagiários),
   exactly the PT lexical variation this corpus is for.
3. **But the margin is modest and the corpus is synthetic.** +5/24 across 24
   queries, consistent in direction across both sets, on an adversarial corpus
   built to isolate the window. It is not a prediction of the delta on
   Frederico's real documents.

The large, unambiguous win is **getting off 128 tokens at all** — mpnet delivers
that too. bge-m3 is chosen for the increment on top, bought now while it is free.

#### Why now, and what it costs

Now, because §8.1's cost is the argument: no production corpus has embeddings, so
this is a default change. Later it is a full re-embed **plus** dropping and
recreating the vector index **plus** rebuilding every V3 centroid.

Costs accepted, all of them real:

- **A hand-rolled ONNX backend**, ~40 lines, where fastembed was free. It needs
  `tokenizers` + `onnxruntime` only — **no torch, no sentence-transformers**,
  both already present as fastembed's own transitive deps (verified: neither
  `torch` nor `transformers` is installed, and neither is needed). This is the
  premise in the question that resolved *favourably*: there is no PyTorch import
  in the answer. It is nonetheless idiomatically foreign — `aw-app-kb` and
  `research-search` both run the fastembed path — so `core/embeddings.py` keeps
  **both** backends, selected by model name, which is also the cheap revert.
- **+1.27 GB image** over mpnet. Host was at 90% / 42 GB free this session —
  **check before deploying** (memory `host-disk-near-full-breaks-apps-silently`).
- **1024 dims = 2.67× the page-cache pressure** the §2 scan cap was sized for.
  `core/graph.py:770`'s arithmetic still says "384 dims × 4 bytes ≈ 1.5 KB" and
  must be rewritten to ~4 KB/vector; raising
  `NEO4J_server_memory_pagecache_size` is already booked in §2.
- **~1.5× slower embedding** than mpnet per token. Ingest is async, so this is
  throughput, not a request-latency regression.

#### What bge-m3 does NOT give us

Its headline "multi-functionality" (dense + sparse + ColBERT multi-vector) **is
not available on this path** — measured: the ONNX graph exposes only
`token_embeddings` and `sentence_embedding`. The sparse and ColBERT heads ship as
torch checkpoints (`sparse_linear.pt`, `colbert_linear.pt`), so reaching them
means `FlagEmbedding` + PyTorch, which is the architectural cost this decision
avoids. **We are adopting bge-m3 as a dense multilingual encoder only.** Nothing
in §2–§5 assumes otherwise, and no hybrid-retrieval plan should be built on that
capability without re-opening this.

Its 8192-token window is architecturally real but **not practically usable on
this CPU-only host**: attention is quadratic and measured cost runs 0.91 s @512,
1.96 s @1024, 6.87 s @2048, 26.7 s @4803 tokens — a single 8k embed exceeded a
120 s budget. Chunking stays bounded by §4's window, sized well below the model
cap. The window's value here is **headroom that removes silent truncation as a
failure mode**, not an invitation to embed whole documents.

**Runner-up: mpnet, unchanged and reachable by configuration.** What would send
us back: the bespoke backend proving fragile, or disk. Qwen3-0.6B-Q stays the
runner-up for the *deferred bake-off*, which this does not close — the harness
now exists, and the corpus to replace is still Frederico's real documents.

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
  `POST /api/nodes`.** *(Revisited: §11. The ingest-worker LLM extractor is
  that moment — the gates and evidence anchoring arrive there. Auto-discovery
  stays out.)*
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

### Amendment 1 (V3, 2026-09-27) — "shares a leaf topic" is not a usable edge rule

Written from the implementation of this section (`backend/app/topics/`), against
a real 1 232-chunk corpus of 9 workspace documents. Two things in the BUILD half
above are wrong as specified, and both were found by running it rather than by
reading it.

**1. `RELATED_TO {via: "topic"}` needs a weighted score and a threshold.** The
rule as written — related if they *share a leaf topic* — produced a **complete
graph**: all 36 possible pairs of 9 documents, every one of them "related",
because a long document has chunks under most leaves. That is the dust cloud
this whole section exists to replace, with edges drawn on it.

The implemented rule keeps the shape and weights it: the score is the cosine
between the two documents' per-leaf **chunk-count distributions**, thresholded
(`TOPIC_RELATED_MIN_TOPIC_SCORE`, default 0.5). 36 edges → 4, and the four are
the pairs a reader names unprompted. Two set-based scores were measured first and
are recorded in `topics/related.py` as worse, with the reason each fails:
`shared / min(topics)` lets a one-topic README score 1.0 against everything, and
Jaccard over topic sets still ranks `buckets ↔ pipeline-testing` above
`cicd ↔ pipeline-testing` because membership is binary.

`via: "embedding"` needed no amendment and is the precise arm of the two.

**2. c-TF-IDF alone does not produce a usable label.** §5 says labels fall back
to "c-TF-IDF keywords over the cluster's chunks", and that alone labelled the
top-level topics `one, api, app` and `bucket, one, every` — the idf penalty is
outweighed by a high in-cluster frequency for a term that is everywhere. The
implementation adds a ceiling on how many sibling topics a term may appear in
(`MAX_CLUSTER_SHARE`, 0.6) before it is disqualified from a label; the same
topics then read `aw-remote-host, socket, github`. A label's only job is to tell
a topic apart from its siblings, so a term in most of them is disqualified
however it scores.

### Amendment 2 (V3, 2026-09-27) — a threshold cannot bound edge density; fan-out can

Found after Amendment 1 had already landed, by running the pass on the **live**
`architecture-pilot` bucket: 32 generated architecture docs that share a
template, so every pair genuinely is similar. The thresholds calibrated on the
varied corpus above produced **707 edges against 496 possible pairs per `via`** —
436 `embedding` (88% of every pair in the bucket) and 271 `topic` (55%). The same
defaults on `docs-pilot`, 10 hand-written documents, produced **7**.

The thresholds are not wrong, and re-tuning them is not the fix: one global
similarity floor cannot serve both a hand-written corpus and a generated one, and
the generated near-duplicate corpus is the kind this estate keeps producing.

So the derived graph gains the bound that holds whatever the corpus looks like:
**each document keeps at most `TOPIC_RELATED_MAX_PER_DOC` (8) derived neighbours
per `via`**, applied as a mutual top-N in descending score order — a pair
survives only while *both* endpoints still have room, so what is kept is always
the strongest ties rather than whichever pair was enumerated first. The invariant
that buys is the one the graph view needs and a threshold can never promise: no
document ever renders more than N derived neighbours of one kind.

Drops are counted in the build response and the split between the two bounds is
logged, per §5's own standing rule against a silent downgrade.

**Not amended, and confirmed by the same run:** the centroid as cluster mean,
`k` from `round(sqrt(n/2))` with a ceiling, the 3-level cap, per-bucket scope,
and the LINKS_TO guarantee in §9.5 — which the implementation makes structural
(one delete template per relationship type, no `DETACH DELETE`, plain `DELETE`
on `(:Topic)` so an unowned edge fails the rebuild instead of being detached).

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

**The topic tree in item 2 is not the folder tree**, and a later request asked
for the folder one. `aw-knowledgeable-buckets.md` §8 (addendum, 2026-09-27)
decides that they are separate mechanisms answering separate questions — the
topic tree is *derived* (discovery: "what is in here?"), a `folder_path`
property on `(:Document)` is *authored* (filing: "where did I put it?") — and it
rejects both nesting buckets and reusing this tree for filing. **Nothing in §5,
§6 or their cards changes;** §8 adds a sidebar to the `library` view and a
property, in a card of its own. Read it before "unifying" the two trees.

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

---

## 10. Per-bucket embedding models, and cross-bucket correlation across them

Written 2026-09-27, answering Frederico verbatim: *"da pra desenhar que cada
bucket tem um modelo e ainda sim ser possível correlacionar buckets?"*, plus
his wish to A/B models before committing a corpus, and his framing that
*"eles na verdade poderão ser selecionados"*.

### Decision

**One embedding model per deployment, not per bucket. Model choice is settled
by an offline bake-off on real documents, not by a production feature.** Two
cards follow from that and neither is `(:Bucket)`-shaped:

1. **Promote the bake-off harness into the repo** (`.tmp/kn-bgem3/pt_longform.py`
   is scratch and will be swept). Corpus + query set + N model names →
   recall@1/@5 table. No Neo4j, no index, numpy cosine in-process. This is what
   "rodar testes com modelos" actually needs, and it is strictly better than an
   in-product comparison because the query set is controlled.
2. **Build the re-embed pathway**, which does not exist. `grep` over
   `backend/app` finds no reindex/re-embed path at all; `ensure_schema`
   (`graph.py:209-217`) raises `RuntimeError` at boot when the model's dimension
   disagrees with the index and tells a human to "drop the index and re-embed
   every (:Chunk)" by hand. That is the operation that makes a model decision
   reversible, and it is the real scaffolding — not a registry field.

**`(:Bucket)` gains no `default_embedding_model` field in V2.** The door it
would hold open is already open, and the field would be a policy record with
nothing enforcing it: set it to `bge-m3` on a bucket whose chunks are all
MiniLM and nothing reconciles the two. The **fact** is already recorded where it
cannot lie — `embedding_model` / `embedding_dim` stamped per chunk at
`ingest/worker.py:100-101`.

### Why the chunk stamp is a sufficient *record* but not a sufficient *mechanism*

Going multi-model needs five things the stamp does not provide. They are listed
because "we already stamp the model, so we're ready" is the exact wrong
conclusion to draw from §1:

1. **One vector index cannot hold two models.** Not primarily a dimension
   problem — two 1024-dim models are equally incomparable, and mixing them in
   one ANN index returns confidently-ranked noise. Separating them means a
   second index, and a second index on the same `(:Chunk)`/`embedding` schema
   descriptor is (to my knowledge, **unverified here** — no Neo4j credential is
   reachable from an Architect container, same limitation §2 records) rejected
   by Neo4j as an equivalent index. So it needs a **property per model**
   (`c.embedding_bge_m3`), never a label per model — labels are schema, already
   rejected twice in these documents (buckets.md §1, §1 above).
2. **Query-side: one embed per distinct model in the scoped set.** The binding
   cost is resident memory, not latency: bge-m3 alone is 2.27 GB of weights in a
   single-worker container (`Dockerfile` CMD, no `--workers`).
3. **Merging is rank fusion, not score merging.** Cosine scores are not
   calibrated across models — model A's best hit at 0.81 and model B's at 0.54
   may be equally good. Merging by score silently favours whichever model runs
   hotter. The sound merge is **RRF over ranks**. This is the trap: merging by
   score looks like it works.
4. **The escalation path is the easy half.** `vector_search_exact`
   (`graph.py:396-408`) pre-filters in the `MATCH`, so `c.embedding_model = $m`
   costs nothing there. `vector_search_ann` (`graph.py:363-376`) post-filters,
   so a model predicate becomes a **third** dilution filter on top of tenant and
   bucket — the compounding §2 already fights.
5. **V4's cross-bucket tree descent breaks the same way.** V3 centroids are
   per-bucket means of chunk embeddings and stay intra-bucket, which is fine;
   but beam-searching across buckets on two models needs the same fusion.

### The "canonical search model" shortcut does not exist

The tempting cheap answer — one canonical model for retrieval, per-bucket models
as a storage/quality choice — **is a category error in the retrieval
direction.** The stored vectors *are* what search compares against; a canonical
query vector cannot be compared to a bucket's foreign-model vectors at all. The
only version that works is dual-embedding every chunk (canonical + bucket-local),
which doubles embed cost and storage and buys retrieval **nothing**, because
retrieval would only ever touch the canonical vector. The bucket-local vector
would feed intra-bucket clustering (V3) only — an unmeasured quality delta at 2×
the ingest cost. Rejected.

### Why deferring costs nothing, stated falsifiably

**Every step of going multi-model later is bounded by a re-embed pass we would
have to run anyway.** Renaming `embedding` → `embedding_<model>` is one batched
Cypher `SET`/`REMOVE` over the same chunks the re-embed already rewrites; the
index is dropped and recreated in both cases; V3 centroids are rebuilt in both
cases. And crucially the source bytes are never lost — `storage_path` stays on
the volume and `extract_text(storage_path)` is re-runnable
(`ingest/worker.py:53-60`), so no re-embed ever needs a re-upload. That is the
fact that makes all of this reversible.

**What would prove this wrong:** any multi-model step whose cost is *not*
bounded by a re-embed pass. If one is found, this section is wrong and
per-bucket models should be scaffolded early.

The cost that *does* grow is §3's, unchanged and now urgent for a different
reason: re-embedding is free today because no production corpus has embeddings.
The bake-off on real documents wants to happen **before** the first big ingest,
and it needs Frederico's actual documents plus a query set with known answers.

### Rejected

- **`default_embedding_model` on `(:Bucket)` now, "to keep the door open".**
  Rejected above: a registry property is a `SET` on a handful of nodes — the
  cheapest migration in this system — and an unenforced one can lie.
- **Per-bucket model selection exposed in the UI.** It would promise something
  retrieval cannot honour: the moment two buckets differ, `scope=all`
  (buckets.md §3) silently becomes N searches needing fusion that does not
  exist. Model selection belongs at deploy/tenant config —
  `EMBEDDING_MODEL` already exists (`embeddings.py:model_name()`) — paired with
  the reindex operation above. Whether it is later surfaced per tenant is a
  Product Owner call; the design absorbs either, because reindex is the same
  operation.
- **Building RRF now.** We will probably want it — for hybrid dense+sparse
  (bge-m3's sparse head is off this path per Amendment 5, but BM25 is not) —
  and that is the reason to build it, not multi-model.

### What this makes harder later

1. **A genuinely per-bucket model becomes a schema change plus a re-embed**, not
   a config flip. Accepted on the argument above; revisit it the moment a
   *measured* per-bucket quality gap exists on a bucket that is mostly searched
   alone.
2. **One index means one dimension for the whole graph, forever-ish.** Every
   bucket inherits the model chosen before the first corpus lands, so the
   bake-off in card 1 is load-bearing in a way a reversible choice would not be.
3. **`scope=all` stays cheap only while the graph is single-model.** Anyone
   adding a second model must fix `scope=all` in the same change, or it returns
   half the corpus and says nothing — the unlabelled-thin-result failure §5 and
   buckets.md:445-449 both exist to prevent.

---

## 11. The entity layer — LLM extraction at ingest, LightRAG-style union, per-bucket assertions, versioned edges (Onda 1.1, 2026-09-28)

Written by the Architect agent against card
`3e95bf3b-9510-818b-80b0-e961a1868571`, from Frederico's 2026-09-28
conversation (context: `.tmp/aw-knowledgeable/entendendo-extracao-e-relink.md`,
`incremental-e-tempo.md`, `buckets-ajudam-ou-atrapalham.md`,
`plano-de-entrega.md`).

**This is an amendment to §5, not a rewrite of it.** §5's rejection of
"LLM-extracted topic taxonomy instead of clustering" stands on its original
reasoning: the topic tree is clustering-derived, tested, live, and V4
beam-searches its centroids. Nothing here replaces it. What this section adds
is the layer §5 never claimed to provide: **documents never link to
documents — they link *through entities*** (GraphRAG, arXiv 2404.16130), and
aw-knowledgeable skipped that layer entirely, which is why every document sits
at `link_count: 0` and `get_graph` returns empty edges.

Decisions of record, made by Frederico on 2026-09-28 and **not re-litigable
here**: incremental, LightRAG-style union — never a batch rebuild
(*"Definitivamente eu quero fazer incremental"*); RAGA's ReAct construction
loop stays out while the writer is an upload form rather than an agent;
RAGA's quality gates and evidence anchoring come **in** (this is §5's own
"revisit" trigger firing — an LLM extractor in the worker *is* "anything
autonomous driving `POST /api/nodes`"); no Leiden, no community reports; the
topic tree stays where it is; buckets are a permission boundary (§7bis).

### 11.0 Measured state (live Neo4j, 2026-09-28, cypher-shell on aw-stack-aw-neo4j-1)

The card required the corpus measured before scoping. Measured, not estimated:

| | value |
|---|---|
| Documents / chunks, whole store | **179 / 2,086** |
| Frederico's corpus (`docs-ingestion-test-66`) | **66 docs → 753 chunks** (~11.4/doc) |
| Embedding model on every chunk | `BAAI/bge-m3` @ 1024 — no mixed models |
| `(:Entity)` nodes | **4** — all manual QA artifacts (`Test Node Alpha/Beta`, `QA Node Gamma/Delta`) in `aw-workspace-default/default` |
| `LINKS_TO` / `RELATED_TO` edges | 3 / 703 |

So the cost baseline is: **first backfill = 2,086 LLM calls** (753 for
Frederico's corpus alone); steady state = one call per chunk at ingest,
forever. LightRAG's union removes the *rebuild*, not the *extraction* — that
bill does not shrink, it just stops repeating. Tuning knob (TURA's lesson,
already in the phase-1 brief): a cheap model for extraction, the strong model
only for labels/summaries. At ~800 tokens per call this backfill is
single-digit dollars on a cheap model; it is the per-upload forever-cost that
must stay visible, so the extractor logs tokens per document.

### 11.1 The write path: entity identity is tenant-wide; ALL content is bucket-scoped

This is the answer to the card's item 4 — the union leak — and it **adopts**
the proposed shape (shared identity, per-bucket content, description as a
reader projection), with two hardenings below. The leak, restated: under
union, an entity's stored description is built by merging mentions from every
document; if those documents sit in buckets with different permissions, a
shared description contains restricted content without anyone traversing an
edge. So the description cannot be a stored field on a shared node. Ever.

**The model:**

```
(:Entity {tenant, external_id, name, name_norm, kind})      -- identity ONLY.
      No description, no summary, no content field, ever. No `bucket`.

(:Chunk)-[:MENTIONS {tenant, description, entity_type,
                     confidence, schema_version}]->(:Entity)
      -- one edge per (chunk, entity); the mention's bucket is DERIVED from
         its chunk endpoint (buckets.md §3 two-endpoint rule) — no stamp.

(:Entity)-[:ASSERTS {tenant, bucket, predicate, description, weight,
                     confidence, valid_from, valid_to, schema_version,
                     evidence}]->(:Entity)
      -- subject-predicate-object between two shared identities. `evidence`
         is the list of supporting chunk external_ids; `weight` is DERIVED
         from `size(evidence)` (see idempotency, §11.2). `bucket` is the
         bucket of the asserting evidence — a named deviation, justified
         below.
```

**Union semantics (LightRAG):** `MERGE (e:Entity {tenant, name_norm})`; a new
mention adds a `MENTIONS` edge; a repeated relation `MERGE`s on
`(subject, ASSERTS {tenant, bucket, predicate}, object)` and set-unions the
new chunk id into `evidence`. Nothing is ever deleted by ingestion.

**Why `ASSERTS` carries `bucket` when buckets.md §3 says edges must not.**
§3's two reasons do not transfer: (1) *"a cross-bucket edge has no single
correct bucket value"* — an assertion **does**: the bucket of the evidence
that asserted it; both endpoints are bucket-less identities, so the edge is
the *only* place its scope can live. (2) *"bucket membership is mutable and
the edge goes stale on rebalance"* — true, and accepted as a named cost in
§11.8.3: a placement move must re-stamp the moved document's assertions,
which is bounded and enumerable through `evidence`. The alternative — a
reified `(:Assertion)` node whose bucket derives from its evidence edges —
avoids the stamp but puts two hops on every entity-neighbourhood traversal
and every graph render; rejected for the hot path (same argument as §1's
registry-node reasoning, in reverse).

**Visibility rules — the two hardenings, and they are structural, not
convention:**

1. **An entity may reach a reader only with at least one visible mention or
   assertion.** Every read template that matches `(:Entity)` must carry an
   `EXISTS { (c:Chunk {tenant, bucket IN $scope})-[:MENTIONS]->(e) }`-shaped
   predicate (or the `ASSERTS.bucket` equivalent) **in the Cypher** — a
   node's existence is what leaks when the name is the secret
   ("Projeto Fênix"). Post-filtering in Python or the frontend is the leak
   happening. *(Amended: a third evidence arm — in-bucket manual
   declaration — exists; see Amendment 1 at the end of §11.)*
2. **A new static-guard class enforces rule 1**, exactly as K5 enforces
   tenant scoping: `(:Entity)` joins a named bucket-exemption set (precedent:
   `_BUCKET_UNSCOPED_NODE_TEMPLATES`, `graph.py:1867`), and the guard asserts
   that every catalogue template touching `(:Entity)` without a bucket
   predicate either contains the visible-evidence predicate or sits in an
   explicit write-path allow-list. A template added without either fails the
   build. Without this guard, the rule survives exactly until the first
   convenient query.

**Description as projection:** `GET /api/entities/{id}` computes the
description at read time — aggregate the visible `MENTIONS.description`
strings (confidence-weighted, recency-ordered, capped), never store the
result on the node. The accepted cost: an entity render is an aggregate
query, not a property read (§11.8.1).

**Reuse or diverge from `create_entity` (`graph.py:296-301`)?** Diverge,
deliberately. That template's `MERGE (n:Entity {tenant, bucket, external_id})`
is precisely the per-bucket-copy shape this amendment exists to remove — each
bucket holding its own `RedisLease` is what makes cross-bucket linking
impossible. The `(:Entity)` **label** is kept (two entity-ish labels would
need a UI that explains which is which); its identity contract changes to
`(tenant, name_norm)` unique, and `create_entity`/`POST /api/nodes` is
rewritten against it. Manual node creation keeps working (name + kind); the
manual `description` field is **dropped in v1** — it has 4 test rows of
usage in the entire estate (measured, §11.0), and a stored description on a
shared node is exactly the leak channel. If a real annotation need appears,
it comes back as a per-(entity, bucket) note feature, designed then — a PO
call, flagged, not absorbed. The 4 existing artifacts are migrated
mechanically (label → name/name_norm) or deleted with Frederico's
confirmation, per the delete-card precedent.

### 11.2 Extraction: one LLM call per chunk, fixed schema v1, inside the worker

**Where it lands:** `ingest/worker.py`'s `process_one` keeps its current
pipeline and its current terminal state — the document goes `ready` and
becomes flat-searchable the moment chunks+embeddings are written
(`worker.py:113-117`), **then** entity extraction runs as a second,
lower-priority queue phase, not as a new stage blocking `ready`. A
753-call backfill must not delay anyone's upload.

- `(:Document)` gains `extraction_status: pending → extracting → done |
  failed | <null>` alongside `processing_status`.
- A `claim_next_extraction_document` template joins `_UNSCOPED_TEMPLATES`
  (`graph.py:730`) under the same two-way guard and the same
  routing-fields-only contract as `claim_next_pending_document` — Neo4j is
  the queue; that machinery is reused, not duplicated.
- `drain_once` claims **upload work first, extraction work only when the
  upload queue is empty**. Uploads are user-facing; extraction is the
  background citizen.

**First pass and re-extraction are one mechanism.** Every chunk gets
`schema_version` stamped at extraction. A document is due for extraction iff
any of its chunks has `schema_version < CURRENT_SCHEMA_VERSION`. Existing
chunks are backfilled to `schema_version: 0` at boot — **never claim on
`IS NULL`**: Neo4j range indexes do not index nulls, and
`backfill_missing_processing_status` (`graph.py:737-755`) exists because of
exactly this trap; copy its batched-backfill shape. With that, the Onda 1
backfill, a future schema bump, and a routine upload all drain through the
same claim — re-extraction stops being an event and becomes a meterable
drip, which is the card's item 6 delivered as a property of the design
rather than a feature.

**Schema v1 is small, fixed, versioned — no auto-discovery.** A constant
`SCHEMA_VERSION = 1` co-located with two short lists: entity kinds (on the
order of: `identifier`, `person`, `organization`, `system`, `concept`) and
predicates, each flagged `functional: true/false` (§11.4). The exact
vocabulary is the implementing Coder's to finalize against the real corpus;
the *shape* — small, fixed, versioned, functional-flagged — is decided.
Schema auto-discovery is the re-extraction debt generator (each silent
expansion is an implicit request to re-read the whole corpus) and stays out;
expanding the schema is a deliberate act that bumps the constant and accepts
the queue cost. Wave 2's event/temporal fields arrive as `SCHEMA_VERSION = 2`
through this exact path — that is why the stamp must exist now.

**No gleanings in v1 — decided, with the measurement path.** Microsoft's
gleaning rounds ("did you miss entities?") multiply the per-chunk cost for
unmeasured recall. Before paying: hand-label entities/relations on a ~20-chunk
golden set from the real corpus, measure single-pass recall with the same
promoted-harness pattern §10 card 1 uses for embeddings (`pt_longform.py`),
and buy gleaning rounds only if recall on identifier-kind entities falls
below ~0.8. Identifier recall is the one that matters here — a technical
corpus's linking entities are `claim_next_pending_document`-shaped, and a
fixed extraction pass is unusually good at those.

**LLM plumbing:** generalize `topics/label.py`'s pattern (Anthropic Messages
over httpx, `llm_enabled()`, semaphore concurrency — `label.py:186-257`) into
a shared `core/llm.py`; `label.py` keeps its own fallback semantics.
Extraction configures its own model (`EXTRACTION_LLM_MODEL`, default a cheap
model — the TURA knob) and does **not** silently fall back: with no key
configured the worker does not claim extraction work at all, documents stay
`extraction_status: "pending"`, and the pending count is exposed on the
status surface so the degradation is visible (this estate's failure mode is
silent degradation; a keyword-fallback "extractor" would be worse than none —
garbage identities are negative work).

**The gates (RAGA's, now due) live in the write seam, not the prompt.** The
extractor calls `core/graph.py` write functions that structurally require:
source chunk id + confidence on every entity and relation (**evidence
anchoring — retroactively impossible to add, so it ships in the first
commit**); name length/charset caps (a name is never a sentence — this is
also what bounds the §11.1 name-existence leak); per-chunk caps
(~30 entities / ~20 relations — over-cap marks the document
`extraction_status: "failed"` with the counts recorded, because a chunk that
"contains" 400 entities is a garbled extraction, not a dense chunk);
predicate ∈ schema; no self-loops; and the `MENTIONS` MERGE MATCHes its
chunk within `{tenant, bucket}`, so a hallucinated chunk reference is a
0-row failure, loud, not a silent skip. Prompted behaviour the model can
ignore; the tool it cannot.

**Idempotency is load-bearing:** a claim-crash-reclaim or a re-extraction
must not double-count. Hence `MENTIONS` MERGEd per (chunk, entity) and
`ASSERTS.weight` **derived from `size(evidence)`** after set-union — never a
blind `+= 1`. The test that proves it: extract the same chunk twice, assert
the graph is byte-identical.

### 11.3 Normalization: hard for identifiers, soft for prose

Microsoft's soft, summarize-away dedup is right for prose and wrong for a
technical corpus — `claim_next_pending_document`, `aw-stack-aw-neo4j-1`,
file paths are exact strings, and soft-matching them is how 400
same-thing entities are minted. The extractor therefore classifies each
entity's `kind`, and `kind` drives `name_norm`:

- **identifier** (code symbols, hostnames, paths, container names): verbatim
  minus surrounding backticks/quotes; case preserved. Exact match only.
- **prose** (people, orgs, concepts): NFKC, casefold, trim, collapse
  whitespace. No accent-stripping (this is a Portuguese corpus; merging
  distinct accented words is a real risk for a marginal gain), no stemming.

A prose mention and an identifier that denote the same thing ("o helper de
lease" vs `RedisLease`) stay **two entities until an explicit alias merge** —
which is the card's retro case (a), and the strongest argument for the layer:
the merge op moves `MENTIONS`/`ASSERTS` edges to the survivor, appends the
losing `name_norm` to an `aliases` list, deletes the loser — and every old
document is linked at the instant of fusion, zero re-reads. v1 ships the
merge **operation** (admin endpoint); candidate *discovery* stays manual.
Aliases never re-extract; schema changes never merge: **apelido funde, fato
versiona, esquema reprocessa.**

### 11.4 Versioned assertions: union has no notion of supersession

Pure union asserts everything it ever read: "Caddy runs on aw-backend" (true
in July) and "Caddy runs on aw-stack" (true since September) both stand, and
the graph returns both with equal confidence — undecided, which is worse than
wrong. So `valid_from`/`valid_to` go on `ASSERTS` **from the first commit**
(adding them later means not knowing the validity of anything already
written), with the closing rule:

- Only predicates flagged `functional` close (one current object per
  subject: `runs_on`, `located_in`, …). Non-functional predicates
  (`depends_on`, `part_of`, `authored_by`) union forever. Versioning
  everything is expensive; versioning nothing is the undecided graph.
- On a new assertion (S, P, O₂) with functional P **in the same bucket** as
  an open (S, P, O₁), O₁ ≠ O₂: set the old edge's `valid_to`, record
  `superseded_by`. **Closing never crosses a bucket** — an assertion a
  reader cannot see must not alter what they can see (permission boundary,
  §7bis). Cross-bucket contradiction is *surfaced* to a reader who holds
  both buckets (both assertions, dated), never resolved silently.
- Default reads return open assertions (`valid_to IS NULL`); closed history
  is opt-in.
- v1's `valid_from` is **transaction time** (when we learned it), stated
  explicitly so Wave 2's event-time model (`occurred_from`/`occurred_to`)
  lands beside it without collision. Wave 2 has its own design; nothing more
  is decided here.

### 11.5 `RELATED_TO {via: "entity"}` — and the aggregate rule made security-grade

Document-to-document `RELATED_TO` gains the third `via`. Score = sum of
per-bucket IDF over shared entities — rarity-weighted so a ubiquitous entity
("aw-workspace", mentioned everywhere) draws no edges. This is §5 Amendment 1's
complete-graph lesson applied *before* shipping instead of after; Amendment 2's
mutual top-N bound (`TOPIC_RELATED_MAX_PER_DOC`) applies to this `via`
unchanged, drops counted.

Two scope rules, and the second is the general one the card asked to be
written down:

1. **`via: "entity"` derivation runs per bucket**, incrementally (on a
   document's extraction completing, recompute that document's entity-derived
   neighbours within its bucket — bounded by the document's own entity set,
   never a bucket-wide rebuild).
2. **A structural link may cross a bucket; a derived aggregate never may.**
   A cross-bucket `RELATED_TO` score, like a topic centroid or a community
   summary, bakes content from every input into an artifact nothing will
   remember is sensitive. buckets.md §4 already made the topic pass
   per-bucket *for cost*; under §7bis the same decision is a **security
   requirement**, which is a stronger claim and is now recorded as one.
   Cross-bucket connection is served structurally — through the shared
   `(:Entity)` node, visible to each reader exactly as far as their own
   mentions reach (§11.1's rules do this with no extra machinery).
   *(Amendment 2 at the end of §11 extends this rule from the artifact to
   its inputs — what Onda 1.4 may and may not read off a shared identity.)*

### 11.6 Sequencing — the T2 golden window: couple them, and gate the backfill, not the code

The aw-backend T2 card (Ready to Deploy: `b0da528` + `5aae5e1`, CI green, QA
approved) records that after the swap the knowledgeable `tenant_id` for the
same user resolves differently and the existing graph goes **orphaned and
indistinguishable from empty**. Its Architect wrote: *"If the graph is empty
or near-empty this is the cheapest moment T2 will ever have."*

**Recommendation: yes — sequence the tenant swap with Onda 1, in this order,
and the coupling costs zero calendar time because only the last step waits:**

1. Fire the T2 deploy (it is a manual workflow away).
2. Land the claim-consumption card already in backlog (`Identity.claims`,
   `resolve_tenant_id` reading the claim behind `require_tenant_claim`,
   default false — the one-function-body swap `identity.py:18-21` promised).
3. Run the one-shot tenant remap of the existing graph. The two backlog
   cards (`reconcile minted tenant_ids`, `tenant_id derivado de
   account_ref`) collapse into this step — they *are* this remap, executed
   in the window. Today it is a bounded Cypher `SET` over 66 documents +
   753 chunks + edges for the affected tenant (measured, §11.0).
4. **Only then** run the first extraction backfill (a flag flip on the
   already-shipped worker).

Why this order rather than "re-extraction makes remap free": extraction does
**not** rewrite `Document`/`Chunk` tenant properties — union writes new
`Entity`/`MENTIONS`/`ASSERTS` alongside them — so the swap is never literally
free. What the window controls is the *size of the remap surface*: run the
backfill first and every extracted entity, mention and assertion carries the
doomed tenant id too, roughly doubling the surface and adding a
half-remapped-graph failure mode (two tenant spellings, each half invisible)
on top of the one the T2 card already warns about. Waiting costs a flag
flip; not waiting converts a 753-chunk remap into a remap of everything the
extractor writes. Ondas 1.2–1.5 **code** proceeds in parallel with steps
1–3 — nothing in the implementation depends on which tenant string is in the
rows.

### 11.7 Rejected

- **Per-bucket entity copies + a tenant-level `SAME_AS` hub node.** Keeps
  every content node bucket-stamped and K5 pristine; rejected because every
  cross-bucket traversal and every graph render pays two hops through the
  hub, the per-bucket copies still need the visible-evidence rule (the hub
  leaks existence the same way), and alias fusion must now operate on N
  copies + hub instead of one node. Same residual leak, more machinery.
- **No shared node at all — cross-bucket linking as a query-time join on
  `name_norm`.** The maximally-safe shape; rejected because the join
  re-derives identity on every read, alias fusion has nowhere to live (the
  instant-relink payoff of case (a) disappears), and the graph view cannot
  render an identity that only exists inside a query.
- **A per-reader ACL on the entity node.** buckets.md §5.4 already refused
  node-level ACLs and §7bis vindicated it: scope is resolved per request
  from the caller's context. Not reopened.
- **Reified `(:Assertion)` nodes** instead of `ASSERTS` edges — see §11.1;
  two hops on the hot path.
- **A separate label for extracted entities** (`(:Concept)` etc.) — two
  entity-ish labels need a UI that explains which is which, and manual and
  extracted mentions of the same name must land on the same identity anyway.
- **Gleanings in v1** — unmeasured recall for a multiplied bill; §11.2 has
  the measurement that would buy them.
- **Keyword/regex fallback extraction when no LLM key is configured** —
  garbage identities are negative work; visible pending is honest.
- **Schema auto-discovery** — the re-extraction debt generator; §11.2.
- **Blocking `ready` on extraction** — a user's upload must not wait on a
  backfill's LLM queue.

### 11.8 What this makes harder later

1. **Entity descriptions are no longer cheap reads.** Every entity render
   aggregates visible mentions at query time. When this gets slow, the fix
   is a per-(entity, scope) cache with invalidation on write — real work,
   deferred knowingly.
2. **Tenant-wide identity bakes in "a bucket never becomes a tenant".**
   Spinning a bucket out into its own tenant now requires splitting shared
   entities by mention provenance. Evidence anchoring makes it *possible*;
   it is still a migration.
3. **The placement pass loses its "moves touch no edges" property**
   (buckets.md §4's table): moving a document re-stamps the `bucket` on
   assertions its chunks evidence. Bounded and enumerable via `evidence`,
   but no longer a pure property update.
4. **Every schema bump costs a corpus re-extraction** — parcelled by the
   queue, but 2,086 chunks of LLM calls per bump at today's size. Budget
   per bump; the version stamp is what makes the bill visible instead of
   invisible.
5. **The graph becomes still more irreplaceable** — extraction output is
   LLM-priced state on top of §8.4's embeddings and tree, and
   `feature:aw-knowledgeable-m2-neo4j-backup` is *still* in Backlog. Flagged
   to the Product Owner a second time: M2 should land inside this wave.

### 11.9 Risks for the Coders

1. **Onda 0.1 is editing `core/graph.py` and `ingest/worker.py` right now**
   (link-count/summary card, in flight this session). Every Onda 1 card
   sequences after it lands; none may start against today's tree.
2. **The `IS NULL` claim trap** — backfill `schema_version: 0`, never claim
   on null (`graph.py:737-755` is the precedent and the reason).
3. **Idempotency by construction** — weight from `size(evidence)`, never
   `+= 1`; same-chunk-twice test is mandatory (§11.2).
4. **The static guards fail closed** — new `(:Entity)` templates must carry
   the visible-evidence predicate or sit in the named write-path allow-list;
   K5's tenant rule and the `_UNSCOPED_TEMPLATES` two-way assertion apply to
   the new claim template unchanged.
5. **LLM output is untrusted input.** Malformed JSON, hallucinated chunk
   refs (0-row MATCH must fail the item loudly, not skip it silently), and
   prompt-injection *from document content* are all expected; the gates are
   the defence and they live in code.
6. **`pyproject.toml`'s explicit `packages` list** (§9.2) — a new
   `backend.app.extraction` package that is not added there imports fine in
   tests and `ImportError`s in the container.
7. **Do not starve uploads** — extraction claims only when the upload queue
   is empty; a backfill of 2,086 chunks will otherwise sit between a user
   and their upload.
8. **Cost is a first-class output** — tokens per document logged, and a
   per-drain budget cap setting so a runaway corpus cannot silently spend.

### Amendment 1 (Onda 1.2, 2026-09-28) — the visibility predicate has a third arm: `declared_in`

**Ratified.** Written against production (`ed63e60`, CI green, QA 258/258
live) — the code shipped ahead of this text under the shared-tree cadence,
flagged on the Onda 1.2 card as an amendment request rather than taken
silently. This is the contract catching up, not a rubber stamp: the shape was
re-derived here from §11.1's own constraints before ratifying.

**The defect in §11.1 as written.** Rule 1 names exactly two evidence types —
a visible mention, a visible assertion — while the same section requires that
manual creation (`POST /api/nodes`, name + kind) keep working. A hand-created
entity has neither: no chunk mentions it, no extractor asserted anything
about it. Under rule 1 as written, `POST /api/nodes` writes a node that is
invisible to every read, unlinkable and unsearchable the instant it is born.
The two halves of §11.1 contradict each other, and it took implementing to
notice — the same lesson as §5's amendments.

**The ratified shape.** `(:Entity)` gains `declared_in` — a list of bucket
slugs, appended dedup-style on create (`backend/app/core/graph.py:926-934` at
`ed63e60`), unioned on alias merge so a manual declaration survives fusion
(`merge_entity_identity`, `graph.py:1153-1166`) — and
`entity_visible_predicate` (`graph.py:350-386`) gains the third arm:
`$bucket_id IN coalesce(e.declared_in, [])`. Rule 1 now reads: **an entity
may reach a reader only with at least one visible mention, visible assertion,
or in-bucket declaration.** K6 asserts against the expanded predicate text,
so the third arm is guard-covered like the first two.

**Why this does not reopen "an entity has no `bucket`".** Three properties,
each load-bearing:

1. **It is not membership.** The identity stays tenant-wide and shared — the
   same name declared by hand in two buckets is ONE node visible in both,
   which is the whole §11.1 point. `declared_in` records where a declaration
   happened, the exact analogue of what a mention's chunk endpoint records
   for extracted evidence.
2. **It carries no content.** The channel §11.1 closed was a stored
   description accreting restricted text. A slug list accretes nothing a
   reader wrote.
3. **It is never serialized to a caller — and this is now a rule of §11.1,
   not an implementation habit.** Bucket slugs can themselves be the secret;
   an entity visible to me via a mention in my bucket must not carry the
   names of the other buckets it was declared in. Test-enforced at
   `backend/tests/test_entities.py:248`
   (`test_declared_in_is_never_echoed_to_a_caller`). Any future endpoint
   that echoes `declared_in` is a §7bis scope leak, full stop.

**Rejected** (the first two recorded in the predicate's docstring, the third
added at ratification):

- **An edge to the `(:Bucket)` registry node.** §1 Amendment 1 keeps that
  node edge-free and off every hot path; this would put it on the hottest
  one — the visibility predicate runs inside every entity read.
- **Leaving manual entities invisible until first mention.** Breaks a live
  screen and §11.1's own requirement; "create, then see nothing" is
  indistinguishable from data loss to the user.
- **A synthetic zero-content "declaration chunk"**, so arm 1 could carry
  manual creation too. Keeps the predicate two-armed at the price of minting
  fake `(:Chunk)` rows into every chunk-facing surface — counts, topic
  clustering input, flat search. Uniformity of the predicate is not worth
  polluting the content model.

**What it makes harder later (extends §11.8):** bucket rename or deletion
must now sweep `declared_in` lists across the tenant's entities — a property
scan, not an edge walk. No bucket rename/delete op exists today, so the debt
is recorded before its creditor. And arm 3 legitimizes degree-0 entities
(visible with no edges at all), so every graph-facing surface must tolerate
an entity with no neighbourhood.

### Amendment 2 (Onda 1.2, 2026-09-28) — shared identity is not shared evidence: the rule Onda 1.4 builds against

**Ratified as a consequence, not a bug — with the inference rule below made
normative**, because a documented tension without a rule is how 1.4's coder
reaches for the convenient global count.

**The tension, precisely.** Identity is `(tenant, name_norm)` unique
(constraint at `graph.py:251-252`, `ed63e60`), so the union merge crosses
buckets *by construction*: mentioning `RedisLease` in bucket A and bucket B
produces one node. Authorization is per bucket (§7bis). The per-bucket-copy
alternative was rejected in §11.7 for real costs and is not reopened. What is
new here is naming what the shared node is allowed to *mean* to a reader who
does not see all of it.

**The rule: a shared identity is a join point, never shared evidence.** Any
artifact derived within bucket B — the `RELATED_TO {via: "entity"}` scores
1.4 will compute, and every future derived `via` — may take as input only
evidence visible in B: `MENTIONS` edges from B's chunks, `ASSERTS` edges
stamped B. Forbidden as inputs, explicitly: the entity's tenant-wide mention
or assertion counts, its degree, its `declared_in` list, its `aliases`, its
`created_at`, and its existence-in-other-buckets in any form. The falsifiable
form, which is also the test 1.4 must ship: **a score computed in bucket B is
byte-identical whether or not the same entity carries evidence in any other
bucket** — build the same corpus twice, once with and once without
cross-bucket evidence on the shared entities, and assert equal scores. A
score that shifts with out-of-scope evidence is a one-bit oracle ("this name
is also active somewhere you cannot see"); §11.5 rule 2 already bans derived
aggregates from *crossing* buckets, and this extends the same rule from the
artifact to its inputs.

The concrete trap this closes for 1.4: §11.5's "per-bucket IDF" means
document frequency counted over in-bucket mentions only. The entity node's
tenant-wide mention count is one property read away and is exactly the number
1.4 must not use.

**What stays legitimately cross-bucket, so it is not "fixed" later:**

- **The structural join itself.** §11.5 already serves cross-bucket
  connection through the shared node, each reader reaching exactly as far as
  their own evidence — §11.1's predicate does the fencing.
- **Alias merge (§11.3).** A merge is tenant-wide by design: fusing "o helper
  de lease" into `RedisLease` relinks every bucket's documents at once —
  that instant relink is the payoff the layer was bought for. Named
  consequence, accepted: a merge justified by evidence the admin saw in
  bucket A changes what a reader confined to bucket B sees (their links move
  to the survivor). Acceptable because merge is a deliberate admin operation
  with a human in the loop — ingestion never merges identities beyond exact
  `name_norm` collision (§11.3) — and what B's reader sees afterwards is
  still only B-visible evidence, re-hung on the surviving name.

**Rejected:**

- **Forbidding identity union across buckets** (per-bucket identity plus a
  cross-bucket `SAME_AS` hub) — §11.7's first rejection re-proposed through
  the authorization lens; same residual leak, more machinery.
- **Documenting the tension without a rule.** "Not a bug" is true and
  insufficient: the next reader of an unruled tension re-litigates it, which
  is this document's own §11.7 argument for writing rejections down.

**What it makes harder later:** every future derived `via` inherits the
byte-identical-score obligation as a standing test, and §11.8.2's
bucket-to-tenant split migration gains one more thing to carve — `declared_in`
lists and cross-bucket alias unions must be split by provenance too.

---

## 12. The Playground — per-request retrieval knobs, and an agent that answers only from the graph (2026-09-29)

Architect design, card `feature:aw-knowledgeable-retrieval-playground`
(`3ea5bf3b-9510-81bf-a213-d52673f7a96e`). The request, verbatim (Frederico,
Telegram, 29/09):

> "eu quero ver do lado do Library e Graph um Playground, uma forma de um
> agente responder somente baseado no conhecimento do grafo de forma que a
> gente possa brincar com a estrategia e tudo mais, veja como a gente pode
> parametrizar a busca que ele vai fazer, ou seja, que a tool aceite esses
> parametros (adicione caso necessario) e na interface a gente consiga fazer
> o ajuste deles. Podemos criar um agente no ap-mt que tenha acesso somente a
> tool pra poder usá-la com o conjunto de instrucoes pra isso"

### Decision

A third tab, **Playground**, beside Library/Graph in `frontend-react`.
Every §5 retrieval knob becomes a **per-request parameter on the existing
`GET /api/search`** (no `/api/retrieve` split — §8.5 stands), validated per
the matrix below, and the envelope **echoes the parameters that actually
ran**. The answer path is **closed-book (fork b2)**: a new
`POST /api/playground/ask` runs the retrieval in-process under the caller's
own token with the exact knobs, injects the retrieval envelope into the
prompt of a tool-less ap-mt agent (`knowledgeable-playground`,
`model_slug: claude-runner-haiku`, the extractor pattern), and returns
`{answer, retrieval, params, usage}`. The MCP surface gains a **new tool
`search_graph`** exposing the same knobs; `search_nodes` stays lexical-only
because it is the link picker's tool and its default is intentional (§6.4).
Frederico's "agente que tenha acesso somente a tool" ships too, as a second
agent (`knowledgeable-explorer`, tool_specs = the new tool only) for
free-form graph-only chat in ap-mt — explicitly **not** wired into the
Playground UI, for the identity reason below.

### The fork: who executes the retrieval the UI parametrized

Three candidates were on the table; the decision is **b2, not the hybrid
the dispatcher recommended**, and the reason is a fact found in the code,
not a preference:

- **(b1) the agent executes** — UI sends knobs, agent builds the tool call.
  Rejected: the LLM assembles the call, so the knob you set is not
  guaranteed to be the knob that ran. A playground whose purpose is
  comparing strategies cannot tolerate that; §5's own rule ("never a silent
  downgrade") would be violated by the *caller* instead of the server.
- **(hybrid) b2 seed + agent refines via the tool within a UI-set ceiling.**
  Rejected for now on two grounds. (1) **Identity mismatch:** the MCP tool
  authenticates with `X-Internal-Secret`
  (`aw-app-knowledgeable/knowledgeable_app/mcp/client.py:56`) — the app's
  service identity — while §7bis makes the bucket a **per-token** permission
  boundary enforced by `require_bucket_read` (`core/identity.py:392`). A
  mid-loop refinement would read the graph as a *different principal* than
  the seeded retrieval, so "same knobs, same scope, reproducible" cannot
  hold across the two halves of one answer, and a user scoped `view-one`
  could receive synthesis grounded in buckets their token cannot see.
  (2) **No enforcement point for the ceiling:** the refinement call
  originates in ap-mt and crosses the gateway as the app; the backend has no
  way to correlate it with the Playground request to clamp its parameters.
  A prompt-level clamp is not enforcement — it reintroduces b1's defect.
  The prerequisite that would unlock the hybrid is per-call user-scope
  propagation through gateway → app → backend; that is a project, not a
  card, and it is named here so the next person finds the real blocker.
- **(b2) backend retrieves, agent synthesizes** — chosen. "Responds only
  from the graph" is guaranteed by construction (the agent has **no tools**
  and sees only the injected envelope), the knobs that ran are exactly the
  knobs sent, and the retrieval runs under the caller's own token, so §7bis
  scoping holds end to end. The hybrid's virtue — iterative refinement — is
  preserved where it belongs in a playground: the human twists a knob and
  asks again.

### The knobs

Grounded in §5 and §7; a knob that changes nothing observable does not
exist here. `bucket` is not in this table — it is already a per-request
scope selector (`?bucket=`, `core/identity.py:392`) and the Playground
simply surfaces the existing active-bucket control.

| knob | applies to | default | valid range | observable change |
|---|---|---|---|---|
| `mode` | all | `lexical` (endpoint, unchanged — link picker) / `tree` (Playground UI) | `lexical\|semantic\|tree` | which algorithm runs; envelope `mode`/`strategy`; result shape (nodes vs chunks with `topic_path`) |
| `limit` | all | 20 | 1–100 (validated; today unbounded) | result count; the top-`k` of §5 RETRIEVE 3 |
| `beam_width` | `tree` only | server config (`TOPIC_SEARCH_BEAM_WIDTH`, 3) | 1–10 | how many branches survive each descent level (`topics/retrieve.py:53-55`) → different `topic_path`s and different leaves re-scored; echoed in envelope |
| `min_score` | `semantic`, `tree` | none (off) | 0.0–1.0 | results below the cosine cut are dropped **after** top-k, with `dropped_below_min_score: n` declared in the envelope |
| `related_vias` | `semantic`, `tree` | `[]` (off) | subset of `{topic, embedding, entity}` | envelope gains `related`: per result document, its `RELATED_TO` neighbours restricted to those vias, with `via` + `score` — the §5/§11.5 derived edges become visible in retrieval for the first time |

`beam_width` moves from global config to a per-request override:
`tree_search(query_vector, k, beam_width=None)` with `None` meaning the
config value (`topics/retrieve.py:30/44`, `core/graph.py:3035`). The config
knob stays as the default source, unchanged for every other caller.

### Never a silent downgrade — the validation matrix

§5's standing rule, applied to knob/mode mismatches. Two distinct cases:

- **Request-shape mismatch → 400.** `beam_width` with `mode≠tree`;
  `min_score` or `related_vias` with `mode=lexical`. The caller asked for
  something the chosen algorithm cannot honour; refusing is the honest
  answer.
- **Runtime inapplicability → declared in the envelope.** `mode=tree` with
  `beam_width` set, on a bucket with no tree: the flat fallback still runs
  (as today) and the envelope says `strategy: "flat"` **and**
  `not_applied: ["beam_width"]`. A 400 would be wrong — the request was
  well-formed; the world declined it.
- **Always:** semantic/tree envelopes gain `params`, echoing the effective
  values `{mode, limit, beam_width, min_score, related_vias}`. In a
  playground, "what actually ran" is the most important datum on the screen.

### Where it lands

- `repos/aw-knowledgeable/backend/app/api/search.py` — knob params +
  validation matrix + `params` echo; mode dispatch refactored into a helper
  `playground.py` can reuse in-process (no HTTP self-call).
- `repos/aw-knowledgeable/backend/app/topics/retrieve.py:30` —
  `beam_width` parameter.
- `repos/aw-knowledgeable/backend/app/core/graph.py` — one new read:
  `RELATED_TO` neighbours for a set of document ids, filtered by `via`,
  tenant/bucket-scoped like every other statement.
- `repos/aw-knowledgeable/backend/app/api/playground.py` (new) —
  `POST /api/playground/ask` (`{question, retrieve_only, …knobs}`);
  ap-mt call with `model: "agent/knowledgeable-playground"` and an ApiKey
  scoped to that slug, read from the workspace vault via `core/secrets.py`
  exactly like the extraction key (`ingest/extraction.py:104-117`).
- `repos/aw-knowledgeable/backend/app/core/llm.py` — a second transport
  function speaking OpenAI `POST /v1/chat/completions`; `complete()` speaks
  Anthropic `/v1/messages` and is not bent to do both.
- `repos/aw-knowledgeable/frontend-react/src/` — third NAV entry
  (`App.tsx:6-7`), `routes/Playground.tsx`: knob panel, question box,
  answer panel, and the **retrieved panel** (chunks with `score`,
  `topic_path`, strategy/escalated/`not_applied` badges, `params` echo,
  `related` neighbours). Without the retrieved panel it is a chatbot, not a
  playground.
- `repos/aw-app-knowledgeable/knowledgeable_app/mcp/` — the `search_graph`
  tool (schema + client passthrough, field-by-field per the module's own
  rule). Backend 400s surface verbatim to the agent — that is the declared
  contract doing its job; the tool does not pre-validate semantics.
- ap-mt (no repo change) — agents `knowledgeable-playground` (synthesizer,
  `tool_specs: []`) and `knowledgeable-explorer` (`tool_specs`: the new
  tool only), both `model_slug: claude-runner-haiku`; one ApiKey via
  `POST /api/api-keys {agent_slugs: ["knowledgeable-playground"]}`, stored
  in the workspace vault for the backend to read.

### Rejected

- **b1 and the hybrid** — above, with the unlock condition named.
- **A separate `/api/playground/search` or `/api/retrieve`** — §8.5's
  rejection stands; the knobs land on the one search endpoint every consumer
  already uses, so the tool and the UI cannot drift apart.
- **`overfetch` as a knob** — it changes escalation *latency*, and the
  escalation is already self-correcting and declared (`strategy`,
  `escalated`); a knob whose effect is mostly invisible in results fails
  this section's own admission rule.
- **Agent-side knobs (model, temperature)** — the ask is about retrieval
  strategy; one fixed cheap synthesizer keeps answer variance from
  polluting retrieval comparisons. Revisit only if answer quality becomes
  the thing being played with.
- **Conversation state** — v1 is single-turn by design; a stateless call is
  the reproducible one.

### What this makes harder later

- The `params`/`not_applied` echo becomes API contract; renaming or
  re-ranging a knob is a breaking change for the tool and the UI at once.
- The closed-book path couples `POST /api/playground/ask` to ap-mt's
  availability. Degradation is declared, not silent: retrieval still
  returns (`retrieve_only`, and the UI renders the retrieved panel even
  when synthesis 502s) — but "the Playground answers" now depends on a
  second system.
- `knowledgeable-explorer` reads as the app's service identity (default
  bucket today). Fine for free-form play; it must never be presented as a
  user-scoped surface until scope propagation exists.

### Risks for the Coders

1. `min_score` filters **after** top-k and declares the drop count — do not
   re-fetch to backfill, that silently changes the experiment.
2. `topics/retrieve.py` assumes a uniform-level frontier; `beam_width` 1–10
   does not disturb that, but a `beam_width` larger than the frontier must
   just take everything, not crash on `beam[0]`.
3. ap-mt's `anthropic-*` models are all dead (no `ANTHROPIC_API_KEY`);
   `claude-runner-haiku` only.
4. New gateway tools appear only in a **new** agent session
   (`verify-new-gateway-tools-in-same-session`), and the app self-registers
   — the manifest registers nothing (`app-mcp-needs-self-register-not-manifest`).
   Never edit the installed copy under `/opt/aw-workspace/apps/knowledgeable`.
5. The frontend must render both result shapes: lexical returns nodes,
   semantic/tree return chunks. A Playground that breaks on `mode=lexical`
   fails its own comparison purpose.
6. The ApiKey is a credential: vault only, never `.env`, never logged —
   commit `6f46811` is the precedent and the reasoning is in
   `core/secrets.py`.

---

## 13. The KB bulk-ingest driver — phase 1 over the whole corpus, extraction declared OFF (2026-10-03)

Written by the Architect agent against card `3ee5bf3b-9510-81ad-ab05-e219bdd3a337`,
from Frederico's 2026-10-03 request, verbatim: *"acho que agora a gente deveria
fazer o ingest de tudo pra validar as coisas, ou seja, fazer o ingest de
/opt/aw-workspace/.aw-workspace/knowledge_base completamente, ver o que falta"*.

The card's own measurements are adopted, not re-derived: 12,324 files /
181.5M chars (86% under `mapped_folders/`); ~181k chunks; embedding 0.91s/chunk
CPU-only (phase 1 ≈ 46h, $0); extraction $0.1022/chunk and 108.9s/call
(phase 2 ≈ $18,500 and ~57 days — **not authorized, and this design must not
be able to start it by accident**). No feeding path exists today: connector
v0.6.0 has `contributes.tasks: null`, none of the workspace's 17 tasks touches
knowledgeable, and the connector tenant holds 10 test documents.

### 13.0 The approach, in sentences someone can disagree with

A **batch-and-journal driver inside the connector app** scans the KB tree,
canonicalizes duplicates by content hash, and feeds `POST /api/documents`
in bounded, backpressured batches into **four buckets, one per top-level
subtree**. The server gains a **content-hash idempotency gate** so re-running
never duplicates. The driver is advanced unattended by a **connector-contributed
scheduled task** and driven manually by a **connector-contributed CLI command**
— same engine, two doors. Entity extraction stays off via the flag that already
exists, declared through the `paused_reason` vocabulary that already exists.
The deliverable is the driver's **report**: scanned / uploaded / deduplicated /
failed per bucket, plus before/after retrieval probes.

### 13.1 Decision 1 — document unit, and which bucket receives what

**One file = one `(:Document)`.** The KB tree is all `.md`; the file is the
unit the KB sync itself maintains, the unit `heading_path` chunking (§4)
expects, and the unit the report counts. The driver passes the file's
KB-relative path as provenance (new optional `source_path` form field on
`POST /api/documents`, stored as a document property — `label` stays the
filename as today, `api/documents.py:120-125`).

**Four buckets, one per top-level subtree** — `kb-notion`, `kb-memory`,
`kb-crispal`, `kb-mapped-folders` — created via `POST /api/buckets`
(`api/buckets.py:64`) before the first upload. Bucket is a permission
boundary (§7bis), so the partition must follow *who may read*, and the four
subtrees are four genuinely different permission classes: Kanban/Notion
content, agent memory, a paying client's business knowledge (crispal), and
generated code-maps of the repos. A future token scoped "crispal only" or
"everything except memory" is expressible on day one.

Rejected:
- **One bucket for everything** — erases the only permission lever the
  system has, and re-bucketing later is exactly the §11.8.3 re-stamp cost
  (cheap now while there are no assertions; expensive forever after phase 2).
- **Bucket per repo under `mapped_folders/`** — dozens of buckets with no
  distinct permission story between them; `GET /api/buckets` and every
  token-scope grant becomes noise. If a per-repo boundary is ever needed, it
  is a re-bucketing of a subset, done before extraction ever runs.

### 13.2 Decision 2 — dedup lives in BOTH layers, with different jobs

The duplication is measured, not theoretical: the same document appears 3–4×
under different prefixes (the monolith checkout carries a copy of the KB
tree; `apps/<slug>` is a copy of `repos/aw-app-<slug>`).

**The idempotency gate lives in `POST /api/documents`.** Upload computes
`sha256(body)` and stores it as `content_hash` on the `(:Document)`; the
create becomes a `MERGE` on `(tenant, bucket, content_hash)` — an upload whose
hash already exists in that bucket returns the existing document with
`deduplicated: true` instead of creating (and does not re-write bytes).
This is what makes the driver re-runnable and resume safe *without trusting
its own journal*, and it protects every other caller (UI, MCP tool) for free.
Scope is deliberately **per (tenant, bucket)**: a cross-bucket server-side
dedup would be an existence oracle — a `deduplicated: true` answer would leak
that the same content lives in a bucket the token cannot see (§7bis).

**Cross-prefix canonicalization lives in the driver**, because only the
driver has the full-corpus view: it hashes every file first, groups by hash,
uploads **one canonical copy**, and records the rest as aliases in its journal
(never uploaded). Canonical priority: curated subtree (`crispal/`, `notion/`,
`memory/`) beats `mapped_folders/`; within `mapped_folders/`,
`repos/<name>/...` beats the monolith-checkout copy and the `apps/<slug>`
copy. The server cannot make this call: at write time it has no path
priority and must not have cross-bucket sight.

Rejected: **driver-only dedup** (a lost journal, or any second caller, and the
corpus duplicates silently — the card says dedup is mandatory, so it must be
a server property, not a client discipline); **server-only dedup** (cannot
catch the cross-bucket duplicates, which are precisely the measured ones).

### 13.3 Decision 3 — `mapped_folders/` is IN this pass, ordered last

The request was *"completamente"*, phase 1 costs $0, and `mapped_folders/`
(the code-maps) is the only part of the corpus that validates traversal over
code — excluding it would validate the graph on 14% of the body and call it
done. So it is **in**.

What the order buys instead of an exclusion: subtrees ingest
**crispal → memory → notion → mapped_folders**. The three curated subtrees
(1,572 files, ~25k chunks, ~6.3h of embedding) land first, so search, topic
tree and Playground are validatable the same day; a stop after any batch
leaves a coherent, reportable corpus; and `mapped_folders/`' KB-tree and
`apps/<slug>` copies are already pruned by 13.2's canonicalization before its
remaining files upload. Its real document count will be measured by the scan
and stated in the report — that number (not 10,752) is the honest size of
what the pass adds.

### 13.4 Decision 4 — where the driver lives: one engine, two doors, both in the connector

**Engine:** `knowledgeable_app/bulk_ingest.py` in `repos/aw-app-knowledgeable`
— the connector already owns the auth plumbing (`mcp/client.py`'s
`X-Internal-Secret` channel) and runs Tier-1 in-process with the KB tree on
its own filesystem. Journal: sqlite at
`AW_WORKSPACE_HOME/data/knowledgeable/bulk_ingest.sqlite`
(rows: relpath, sha256, bucket, status ∈ pending/uploaded/alias/skipped/failed,
external_id, error) — durable across reinstalls per the workspace's own
storage convention (`src/apps/paths.py`).

**Door 1 — CLI** (`apps/<slug>/commands/` auto-discovery,
`src/cli/discovery.py:41-51`): `aw-workspace-cli knowledgeable-ingest
scan | run [--max-batches N] | status | report`. The operator door: start,
resume after anything, and produce the report.

**Door 2 — contributed task** (`contributes.tasks`, type `agentic_output`,
every 15 min): runs one bounded tick of the same engine. Exit 0 (progressed,
or nothing to do) costs nothing; a notable exit (precondition violated,
batch of failures, stall) dispatches an agent to triage. This is the task
Frederico predicted ("vai ter alguma task lá que vai chamar o ingest"), and
it is what makes a 46h pass advance unattended across restarts.

Resumability and idempotency come from three independent layers: the journal
(skip what's done), the server hash gate (re-POST of anything returns the
existing doc), and the deterministic scan (a wiped journal just re-walks and
re-POSTs, deduplicated server-side).

Rejected: **task-only** (no operator door for resume/report, and a 15-min
cadence is miserable to debug through); **CLI-only** (46h of unattended
progress would depend on a human re-running it); **driver inside
aw-knowledgeable itself** (the corpus lives on the workspace filesystem,
which that service deliberately cannot see — the connector exists precisely
because the two filesystems are different, `mcp/client.py:95-101`).

### 13.5 Non-negotiables, wired to things that already exist

1. **Extraction OFF, declared.** `extraction_enabled` stays `false`
   (`config.py:97`, the default). The declaration is the existing vocabulary:
   `/api/ingest/status` answers `paused_reason: "flag_disabled"` with its
   `PAUSE_EXPLANATIONS` text (`ingest/extraction.py:342-347`,
   `api/ingest.py::ingest_status`). No new flag.
2. **Ignition guard.** Every uploaded document is necessarily born
   `extraction_status: pending` (`api/documents.py:149`) — an inert backlog
   while the flag is off, but live tinder if anyone flips it. So the driver,
   **before every tick**, GETs `/api/ingest/status` and proceeds only if
   `extraction.claiming == false`. If claiming is true it uploads nothing,
   exits notable, and says exactly why. The driver never touches
   `EXTRACTION_ENABLED`, never calls `/api/ingest/key`, and contributes no
   config that could flip either.
3. **No work without a ceiling** (the 228-container / 37.2 GB / 145-document
   incident). Per tick: ≤ 200 uploads, and only while the backend's
   `processing_status: pending` backlog is < 500 (requires the small status
   addition in 13.6). Phase 1 spawns **zero** containers and makes **zero**
   LLM calls; embedding is the worker's own sequential in-process loop
   (`ingest/worker.py::drain_once`), which is its own ceiling. The breaker
   (`extraction_breaker_threshold=3`) is untouched and irrelevant while
   extraction is off — and stays visible in `/api/ingest/status` if that
   ever changes.

### 13.6 Where it lands — the full change list

aw-knowledgeable (`repos/aw-knowledgeable/backend`):
1. `content_hash` + MERGE-dedup on upload (`app/api/documents.py:100-160`,
   `app/core/graph.py::create_document`), response gains
   `deduplicated: bool`. Range index on `(tenant, bucket, content_hash)`.
2. Optional `source_path` form field, stored on the document.
3. `/api/ingest/status` (or a sibling counts route) gains
   `processing: {pending, extracting, embedding, ready, failed}` counts —
   the driver's backpressure signal. `list_documents` at 12k docs is not a
   counts API; do not use it as one.

aw-app-knowledgeable (`repos/aw-app-knowledgeable`):
4. `knowledgeable_app/bulk_ingest.py` (scan/hash/canonicalize/batch/journal/
   report) + `?bucket=` support on the upload path in `mcp/client.py`
   (today only search passes `bucket`, `client.py:272`).
5. `commands/` CLI module; `contributes.tasks` entry; version bump 0.6.0 →
   0.7.0; tests mirroring `test_ingest_key_push.py`'s shape.

Untouched: the worker pipeline, chunking, embeddings, topic-tree code, the
extractor, the breaker, all §11 entity code, the frontend (the Library already
renders `processing_status`).

### 13.7 Preconditions the Coder must verify before the first real batch

1. **The tenant.** Service callers write into `KNOWLEDGEABLE_SERVICE_TENANT_ID`;
   Frederico's browser session resolves to a *minted* tenant
   (`core/identity.py:267-287`) — two different tenants, the exact trap the
   66-doc card documented. A browser JWT cannot drive a 46h unattended run,
   so: **set `KNOWLEDGEABLE_SERVICE_TENANT_ID` to Frederico's real tenant id**
   (read his row from `identity_tenant_db_path`'s sqlite) and recreate the
   container — then the connector door and his browser read the same corpus.
   The 10 test docs stay behind in the old service tenant, harmlessly.
   If this is refused or blocked, the run still proceeds, but validation is
   via connector tools only and the report must say so.
2. **Prod's extraction state**: confirm `/api/ingest/status` live answers
   `paused_reason: "flag_disabled"` (not merely `no_api_key` — the key-push
   loop re-asserts a key every 300s, so "no key" is not a durable off-state;
   the FLAG is).
3. **Neo4j host disk**: ~181k chunks × 1024-dim vectors ≈ 0.8–3 GB of store
   growth on the bare-metal aw-stack host — check THAT host's disk, not the
   workspace container's (the estate's disk-full failure mode is silent).

### 13.8 What this makes harder later

- **Content-addressed and append-only.** A source file that *changes* re-ingests
  as a NEW document; the stale predecessor stays until a future sync/GC
  reconciles by `source_path` — which is why `source_path` is stored now.
  This driver is a bulk load, not a sync; designing the sync later inherits
  12k content-addressed docs to reconcile.
- **Four buckets bake a permission taxonomy.** Re-partitioning after phase 2
  means re-stamping assertions (§11.8.3). Re-bucket before extraction ever
  runs, or live with the partition.
- **Canonical-copy choice is frozen in the driver.** If a canonical subtree is
  later unmapped, its aliases point at a deleted origin; the journal's
  alias→canonical map is the recovery record — keep it in the report.
- **The service tenant becomes Frederico's tenant.** The connector door turns
  personal; a second human tenant later needs the real per-token work, not
  another env flip.

### 13.9 Risks for the Coders

1. `contributes.tasks` is **seeded once, never updated**
   (`src/apps/capabilities.py:39`): verify the task actually seeds on
   `marketplace install --update` of an already-installed app; if it does
   not, create it once by hand and say so in the report.
2. Empty or near-empty `.md` files (code-maps include tiny ones): verify a
   0-chunk document terminates in `ready`, not a retry loop, before the bulk
   run — one synthetic test.
3. Topic trees don't build themselves per this card: after each subtree
   completes, `POST /api/topics/build` per bucket (`api/topics.py:56`).
   Clustering ~156k chunks in `kb-mapped-folders` is unmeasured — build
   `kb-crispal` first and extrapolate before launching the big one.
4. Files > 10 MB are skipped with a journal reason (`documents.py:111`),
   never a driver crash. Expected count: ~0, but the report must say.
5. The MERGE dedup must be atomic in Cypher, not check-then-create in Python
   — single uvicorn worker today, but §4 already records how reliably this
   estate grows `--workers`.
6. **The deliverable is the report, not the driver.** Run the pass for real
   (or an honest, declared slice), and prove with `list_documents` + a
   Playground search that the corpus answers something it could not answer
   before. "The driver is ready" is not this card done.
