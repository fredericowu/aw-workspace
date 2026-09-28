# aw-knowledgeable — knowledge buckets (isolation, interlinking, rebalancing)

Status: **design thinking for a future phase. Changes nothing in v1.**

Written by the Architect agent, 2026-09-26, answering Frederico's question of
the same day (card `3e75bf3b-9510-819b-a1c1-d0063d104675`). It is a companion
to `docs/design/aw-knowledgeable-infra.md`, not a revision of it: **no
milestone M1–M7 or D1 changes, and no decision in that document is
re-litigated.** Exactly one recommendation here would cost something if
ignored (§1's reservation, which lands in M3) — it is flagged as a Product
Owner call, not taken.

`repos/aw-knowledgeable` does not exist yet, so the seams cited below are
either (a) the infra design doc, which is the contract M1–M7 are being built
against, or (b) the in-tree precedents those milestones are copying.

---

## 0. The reframe the rest of this document rests on

> **AMENDED 2026-09-28** — the second bullet below, "**Bucket** is a
> *relevance* boundary… **There is no adversary**", was the operative model
> through 2026-09-26 and is no longer true. On 2026-09-27 Frederico stated
> that a tenant has multiple users with different roles and that bucket
> isolation is scoped by the API token; `aw-knowledgeable-v2-retrieval.md`
> §7bis recorded the flip in one sentence: **"The bucket is now a permission
> boundary."** There is an adversary now — a co-tenant caller whose token does
> not grant a given bucket. Everything below that reasons from "there is no
> adversary" needs the note this amendment points to at its own site, not a
> rewrite here: see the dated amendments in §3, §4 and §5, and §2's own
> SUPERSEDED note above the fold there. The first bullet (tenant is a security
> boundary) is untouched by any of this. The text below is left standing on
> purpose — it is the record of why §1–§4 were decided the way they were,
> under the premise then in effect, and deleting it would turn load-bearing
> decisions into arbitrary ones for whoever reads this next.

**A tenant boundary and a bucket boundary are not the same kind of thing, and
must not be built out of the same machinery.**

- **Tenant** is a *security* boundary. There is an adversary. Its correctness
  condition is "tenant B's bytes never reach tenant A, and A cannot learn that
  a given id exists in B". That is why `test_tenant_isolation.py:229-232`
  insists on **404, not 403** — "a 403 confirms the id exists" — and why the
  infra doc makes the enforcement structural (`aw-knowledgeable-infra.md:151-168`).
- **Bucket** is a *relevance* boundary, inside one tenant, where the tenant
  already owns everything on both sides. There is no adversary. Its
  correctness condition is "a query about cardiology does not get answered out
  of the tax-law corpus, **and the caller knows that is what happened**".

Conflating them is the single most expensive mistake available here, because
it is invisible: bucket scoping built with tenant machinery would silently
answer "that knowledge does not exist" when the truth is "that knowledge
exists and is not in your scope". For a human that is a confusing UI. For an
agent — the actual consumer, per Frederico's phrasing *"ele vai entender que
não vai ter acesso"* — it is a false premise it will then reason from.

This workspace already has the right shape built, in the right place, for the
right reason. `aw-app-kb/kb_app/mcp_http.py:200-224` scopes the KB's MCP
surface per gateway profile, and its comment states the property that matters:

> It is absent from every tool's public inputSchema on purpose: the caller
> neither sees it nor can unset it, **which is what makes it a boundary rather
> than a convention.**

…while `_scope_note()` (`mcp_http.py:235-236`) makes every scoped answer say
which scope produced it: `"No results found in 'crispal/'."` The boundary is
enforced and *declared*. That pair — enforced, declared — is the whole answer
to question 2, and it already ships.

---

## 1. Bucket model: a second property, reserved in M3, surfaced later

### Decision

**`bucket` is a second string property alongside `tenant` on every node,
carried in the template catalogue's pattern vocabulary from the moment
`core/graph.py` is written — with a single hardcoded default value, no API,
no UI, and no query ever filtering on it in v1.**

Not a label, not a sub-graph, not a separate database, not a relationship to a
`(:Bucket)` node.

### Where it lands

`aw-knowledgeable-infra.md:151-168` defines the seam: feature code never
writes Cypher, it calls named templates from a fixed catalogue, each of which
carries `{tenant: $tenant_id}` in every node pattern. The change is that the
catalogue's node patterns read `{tenant: $tenant_id, bucket: $bucket_id}`, and
that **K5 — the static guard test** (`:258-259`) asserts *both* properties on
every pattern rather than one.

That is the entire v1 cost: one extra key in each template literal, one extra
assertion in one test, and `$bucket_id` resolved to a constant. No endpoint
changes, no frontend changes, M1–M7 unaffected.

### Why reserve it now rather than add it in the phase that needs it

The card asks for this cost comparison explicitly. It is asymmetric, and the
asymmetry is not about the property — it is about the constraint and the guard.

- **Now**: the templates are being written anyway. K5 is being written anyway.
  A guard test that has always checked two properties can never be satisfied
  by a one-property template, so a template added in month six is born
  bucketed. This is the same argument `api/__init__.py:32-50` makes for AP-MT's
  single shared gate list — quoted in the infra doc at `:316-322` — where
  per-route gating let 48 GET routes ship unisolated.
- **Later**: a property backfill over a live graph (cheap), *plus* rewriting
  every template (mechanical but unbounded), *plus* every template written in
  between was born unbucketed and nothing warned — the exact failure the guard
  test exists to prevent, arrived at from the other direction.

### The constraint must NOT gain `bucket`

`aw-knowledgeable-infra.md:182-185` specifies
`REQUIRE (d.tenant, d.external_id) IS UNIQUE`. **Leave it at two columns.**

If it became `(tenant, bucket, external_id)`, then the same document in two
buckets is two nodes, and §4's rebalancing — which moves a node between
buckets — stops being a property update and becomes a node merge with edge
rewriting. Worse, memory `composite-unique-breaks-tenant-consolidation`
records that widening a composite unique in this tree is the thing that later
has to be dropped and rebuilt, and `aw-knowledgeable-infra.md:703-707` already
books that debt once for `(tenant, external_id)`. Booking it twice, for a
dimension explicitly designed to be mutable, is a straightforward mistake.

Consequence, stated so it is falsifiable: **a document lives in exactly one
bucket at a time.** Multi-bucket membership is an edge (§3), not a second node.

### Rejected

- **A Neo4j label per bucket** (`:Document:Cardiology`). Labels are indexed
  and fast, which is the attraction. Rejected: labels are schema, not data —
  a bucket rename is a `SET`/`REMOVE` over every node, buckets become
  user-creatable schema (an injection surface in the template catalogue, which
  is precisely the thing `:151-168` exists to make impossible), and label
  cardinality is not designed for user-generated values. A property rename is
  one `SET`.
- **A `(:Bucket)` node with `(:Document)-[:IN_BUCKET]->(:Bucket)`.** The
  relational-minded shape, and genuinely better *if* buckets ever need
  attributes, hierarchy, or many-to-many membership. Rejected for now because
  every read then pays a hop, and the hop is on the hot path of every query —
  while the property version costs one index. **Revisit if** buckets need to
  nest (a "Medicine" bucket containing "Cardiology"), which is the one
  requirement a flat property genuinely cannot serve.
- **A separate Neo4j database per bucket.** Already foreclosed:
  `aw-knowledgeable-infra.md:116-122` — Community Edition has one user
  database, and Enterprise is a licence decision for Frederico. Even with a
  licence it caps in the low hundreds, and buckets are more numerous than
  tenants by definition.
- **Reusing the `workspaces` concept from aw-backend as the bucket.**
  Tempting, because `/api/me` already returns a live workspace list
  (`agents-platform-multitenant/backend/app/api/me.py:42-52`) and the frontend
  already has it. Rejected: AP-MT returns that list for a **Phase B selector**
  and scopes *no data at all* by it — grepping `workspace_id` across
  `agents-platform-multitenant/backend/app/` finds only Notion-subscription
  fields, nothing tenant-partitioning. Adopting workspaces as the bucket key
  would make aw-knowledgeable the first service to give that identifier data
  semantics, and would bind knowledge organisation to an account-management
  concept that changes for billing reasons. A knowledge area and a workspace
  are not the same object.

---

## 2. Denial semantics: declared scope, not silent absence — and 403, not 404

> **SUPERSEDED 2026-09-27 — see `aw-knowledgeable-v2-retrieval.md` §7bis.**
> This section rests on the premise stated in §0 and §5.4 that a tenant is
> effectively one person, so "the caller owns the id". Frederico answered the
> product question this section itself flagged under *"What would change this
> decision"*: **a tenant is multi-person, with per-bucket scopes carried on the
> token.** That makes the bucket a permission boundary, so the denial shaping
> below inverts to 404 and `GET /api/buckets` stops listing out-of-scope names.
> Mechanisms 1 and 2 (server-injected unforgeable scope; machine-readable scope
> envelope) survive unchanged, and §5.4's refusal to put a `user` property on a
> node is *vindicated*, not reversed. Read §7bis before implementing anything in
> this section.

### Decision

**Out-of-scope buckets are *visible as names*, their *contents* are not, and
every scoped response says which scope produced it. A direct read of a node id
that exists in this tenant but in another bucket answers 403 with the bucket
name — deliberately the opposite of K1's tenant rule.**

Frederico's instinct is the correct one and this section is mostly the
argument for why, plus where the precedent already lives.

### Three mechanisms, each with its precedent

1. **Scope is server-injected and unforgeable by the caller.** For MCP/agent
   callers the bucket scope arrives the way `kb_index` already does: the
   gateway forces it onto the call, and the argument is not in the tool's
   public schema — `aw-mcp-gateway/back/gateway/config_gateway.py:440-446`
   injects `_gateway_kb_index`, and `aw-app-kb/kb_app/mcp_http.py:211-215`
   explains why the omission from the schema is the load-bearing part. Use the
   identical mechanism (`kb_index`-style scalar profile key, per
   `config.py:207`'s `CONFIG_SCALAR_KEYS`) rather than inventing a second one.
2. **Every scoped answer declares its scope.** `_scope_note()`
   (`mcp_http.py:235-236`) appends `in 'crispal/'` to results *and* to the
   empty-result message. Copy the behaviour, **not** the representation: for
   aw-knowledgeable the scope belongs in a machine-readable field of the
   response envelope (`{"results": [...], "scope": {"buckets": [...]}}`), not
   interpolated into a human string — the consumer is an agent that should be
   able to branch on it, and a future `deep=true` retrieval mode needs to read
   it, not parse it.
3. **Bucket *names* are listable; bucket *contents* are not.** A tenant-wide
   `GET /api/buckets` returning `[{id, name, node_count, in_scope: bool}]` is
   the thing that makes *"ele vai entender que não vai ter acesso"* literally
   true. Counts are safe: the caller owns the tenant.

### Why 403 inside a tenant, when the platform's rule is 404

`test_tenant_isolation.py:229-232` is unambiguous — across tenants, 404, "a
403 confirms the id exists". The card is right not to assume it transfers, and
it does not, because the rule's *reason* does not survive the change of
context:

- Across tenants, the fact that an id exists is itself confidential, and the
  caller has no legitimate route to it. 404 is the only answer that leaks
  nothing.
- Within a tenant, the caller **owns** the id. It is not confidential from
  them; it is merely not in the slice they are currently asking through. A 404
  here does not protect anything — it asserts something false, and the caller
  has a legitimate next action (widen the scope) that a 404 hides.

So: **403 with the bucket name in the body.** A 404 within a tenant is
reserved for "no such id in this tenant".

This is a deliberate inversion of the codebase's strongest security
convention, so it must be *recorded as a checked claim* rather than left to
drift. K1's route table already forces every GET route into a literal
classification with `expect` values, and treats an unclassified route as a
failure and never a skip (`aw-knowledgeable-infra.md:243-246`). The
post-v1 card that introduces buckets must add a **third** `expect` class —
`"403-cross-bucket"` — alongside the existing `"404"`, so the day someone
decides bucket *is* a security boundary (§5), the routes that have to flip are
enumerated in a table instead of discovered by audit.

### What would change this decision

If a single tenant ever needs compliance-grade separation between areas — one
organisation, two client engagements that must not see each other — the bucket
becomes a security boundary and this whole section inverts to K1's rule. That
is a product question, and it is the one I would most want to hear an answer
to before building rather than after. It does not block the design: the
enforcement point is the same seam either way, and only the error-shaping
changes.

### Rejected

- **Silent empty results (no scope declaration).** The cheapest option and the
  status quo of most scoped search. Rejected because the consumer is an agent:
  an empty result with no scope marker is indistinguishable from "this
  knowledge does not exist", and the agent's next move — inventing the fact,
  or telling the user it is not documented — is worse than the missing answer.
- **Full invisibility (bucket names hidden too).** Consistent with the tenant
  model, and rejected for the same reason as 404: nothing is being protected,
  and it removes the caller's only path to a correct next action.
- **Client-side filtering with the full graph sent down.** Non-starter, and
  named only because §5 of the infra doc (`:526-529`) already designed out the
  whole-graph endpoint on exactly this reasoning — "the widest leak surface and
  the fastest way to hang the browser".

---

## 3. Interlinking: the same primitive, one new question, no new concept

### Decision

**A cross-bucket link is an ordinary M6 typed edge whose endpoints happen to
differ in `bucket`. No new primitive. Relationships carry `tenant` and do
*not* carry `bucket`; an edge's bucket visibility is derived from its
endpoints.**

### Where it lands

M6's `POST /api/links {from_id, to_id, type}` is already the right shape and
is already implemented in the approved prototype
(`.aw-workspace/data/ux-proto/projects/aw-knowledgeable-graph/latest/frontend/app.js:584-588`),
with an open type vocabulary — the modal offers a select plus a free-text
custom type (`index.html:220-225`). "Bucket A relates to bucket B" needs no
new endpoint; it is a link between two nodes that sit in different buckets.

### Why edges do not carry `bucket`, when they do carry `tenant`

`aw-knowledgeable-infra.md:180-181` puts `tenant` on the relationship "so a
traversal cannot walk out of the tenant even if an endpoint is ever
mis-stamped". That reasoning is about defence in depth against a security
failure, and it does not transfer:

- A cross-bucket edge has no single correct `bucket` value, so any choice is
  arbitrary and the traversal filter built on it is wrong half the time.
- Bucket membership is **mutable by design** (§4 moves nodes). A denormalised
  `bucket` on the edge goes stale on every rebalance, and every rebalance then
  has to rewrite edges it otherwise would not touch.

**Derived rule instead:** an edge is visible in scope *S* iff **both** of its
endpoints are visible in *S*. One extra endpoint predicate in the traversal
templates, nothing to keep in sync, and rebalancing a node changes zero edges.

Note what this produces, because it is the interesting part and it is not
obviously desirable: a single-bucket scope sees a cross-bucket edge as
**absent**, not as a stub pointing somewhere unreachable. Combined with §2's
scope declaration, the honest surface is a neighbourhood response that reports
*"3 links hidden by scope"* alongside the visible ones — the same
enforced-and-declared pair, applied to edges. That count is the cheapest
version of Frederico's *"vai aparecer… ele vai entender que não vai ter
acesso"*, and unlike a stub it leaks no ids.

### The one genuinely new question: what does the link picker search?

The prototype's add-link modal searches for its target
(`app.js:612` — `GET api/search?q=…&exclude=<sourceId>`). Once buckets exist,
that search either crosses the boundary or it does not, and both answers are
defensible. **Decision: in-scope by default, with an explicit widening
control** (`GET /api/search?q=…&scope=current|all`), because a manual link is
the one deliberate act where a human asserts that two things belong together —
that is precisely when the boundary should yield to intent, and precisely when
the yielding should be visible rather than automatic.

This makes `/api/search` carry the scope decision on **both** sides. Infra-doc
risk 10 (`:793-806`) already flags that `/api/search` is a new tenant-scoped
fanout read needing classification in K1's `GET_ROUTE_PLAN`; it now also needs
the §2 `"403-cross-bucket"` treatment for `scope=current` and an explicit entry
justifying `scope=all`.

### Amendment (2026-09-28): the two-extremes rule survives the premise flip, and matters more under it

> Written against card `3e95bf3b-9510-8187-9f23-ca98c7412aca`, following §0's
> amendment and `aw-knowledgeable-v2-retrieval.md` §7bis.

The both-endpoints-visible rule derived above does not depend on §0's
now-superseded "no adversary" premise — it depends only on bucket membership
being mutable and edges not carrying `bucket`, both still true. If anything
the rule is **more load-bearing** under a permission boundary than it was
under a relevance one: a relevance boundary that leaked an edge produced a
wrong answer for a caller who owned both sides anyway; a permission boundary
that leaks one discloses that a restricted document exists and how it
connects to something the caller can already see. **The filter that enforces
this has to live in the Cypher template, never in a post-fetch step or the
frontend** — a node that never enters the result set cannot leak; a node
fetched and then hidden client-side already has.

The "N links hidden by scope" count this section proposes above (*"3 links
hidden by scope"*) was designed under the relevance premise, where the count
is informative and harmless — it tells an honest caller why their own view
looks smaller than the graph. Under a permission boundary the count is itself
a signal: "this document has 47 hidden links" tells a caller without bucket
access that restricted content exists and roughly how connected it is, even
though they can name none of it. **The count must be configurable per
bucket, and off by default for a bucket marked sensitive.** No mechanism for
marking a bucket sensitive exists yet in this design — that is a requirement
to carry into whichever card builds the scope-aware traversal, not something
resolved here.

### Rejected

- **A `(:Bucket)-[:RELATES_TO]->(:Bucket)` edge as a first-class "bucket
  link".** The obvious reading of "interligar buckets", and the one I expect to
  be proposed again — which is why it is written down here. Rejected: it
  requires the `(:Bucket)` node rejected in §1, and it answers a different
  question than the one retrieval asks. Retrieval never needs to know that two
  *areas* are related; it needs to know that this *document* relates to that
  *document*. A bucket-level edge is a UI affordance for an authoring gesture,
  and it can be synthesised from the node-level edges that already exist
  ("these two buckets share 14 links") without being stored.
- **Forbidding cross-bucket edges at the template level**, the way §2's
  both-endpoints-tenant-matched `MERGE` forbids cross-tenant ones
  (`aw-knowledgeable-infra.md:177-179`). The strictest reading of "por agora
  considerar que não [se interligam]". Rejected because it converts a default
  into a wall, and a wall here has to be demolished by a migration: K3 would
  assert zero relationships created, so the test suite itself would have to be
  rewritten to enable the feature later. Default to in-scope; do not refuse
  the write.
- **Bucket-scoped link types** (a vocabulary per bucket). Speculative, and it
  fights the open type vocabulary the prototype already ships.

---

## 4. Rebalancing: intra-bucket is the unit of work; placement is not

### Decision

**The living-graph retro pass runs per bucket, fenced by tenant. Deciding
which bucket a node belongs in is a separate, rarer, tenant-wide pass. Split
those two now, in the vocabulary, even though neither is being built.**

### Why per bucket rather than per tenant

`.tmp/aw-knowledgeable/design-brief-phase1.md:22-33` records why Microsoft's
original GraphRAG was rejected as the model: no incremental-update story, and
adding one document means regenerating every community report — *"1,399
communities × 2 × 5,000 tokens"*. A tenant-wide rebalance reproduces exactly
that cost curve, one level up. The brief picks LightRAG's union-on-write
model for the same reason (`:25-31`).

Bucket is the natural unit between "one document" (too small to re-derive
structure from) and "the whole tenant" (the cost curve we already rejected):
it is bounded, it is independently schedulable, one area's churn does not
invalidate another's, and a failed pass damages one area rather than
everything. It is also the unit at which the work is *meaningful* — merging
duplicate entities and re-weighting edges is a judgement about a coherent
domain, which is what an "área de conhecimento" is.

### The part per-bucket cannot do, and why naming it now matters

A pass that can only see its own bucket cannot decide that a node belongs in a
different one — it has no view of the alternatives. So there are two passes,
and they have different shapes:

| | Intra-bucket pass | Placement pass |
|---|---|---|
| Scope | one bucket | one tenant, all buckets |
| Does | merge duplicate entities, re-weight edges, re-summarise, retire stale nodes | move a node from bucket X to Y; propose splitting or merging buckets |
| Frequency | often; after ingestion bursts | rarely; explicitly or on a slow schedule |
| Cost | bounded by bucket size | bounded by tenant size |
| Touches edges | yes | **no** — by §3's derived-visibility rule, a move is a property update |

That last row is the payoff from §3 and the reason to settle the edge rule
before the retro pass is designed rather than after. If edges carried a
`bucket`, every placement move would rewrite the moved node's entire edge set,
and the cheap pass would be the expensive one.

### Where this collides with something already decided

The brief's schema auto-discovery (`design-brief-phase1.md:45-48`) — *"the
schema expands itself above a confidence threshold as new domains show up"* —
has an unasked question that buckets answer: **is the discovered schema
per-tenant or per-bucket?** Per-bucket, and I think this is the strongest
argument for buckets existing at all: a bucket *is* a domain, so a per-bucket
ontology is what stops a tax-law entity type from being proposed for a
cardiology corpus merely because they share a tenant. It also means a
placement move carries a schema-reconciliation question with it. Not this
card's problem; it is the next one's, and it should not be discovered then.

### Amendment (2026-09-28): per-bucket scope is now a security requirement, not only a cost optimization

> Written against card `3e95bf3b-9510-8187-9f23-ca98c7412aca`, following §0's
> amendment and `aw-knowledgeable-v2-retrieval.md` §7bis.

The decision above — retro passes run per bucket, fenced by tenant — was
reached on cost grounds: the LightRAG-shaped incremental cost curve, and one
area's churn not invalidating another's. That reasoning still holds
unchanged. A stronger reason now sits beside it: any *derived aggregate*
computed over more than one bucket — a topic centroid, a community summary, a
`RELATED_TO` weight — bakes content from every bucket it drew on into a
single artifact, and nothing in the graph remembers afterward which buckets
contributed. Under a permission boundary, showing that artifact to a reader
who cannot see all of its inputs **is** a leak, not a UX rough edge. So a
cross-bucket rebalance, or a cross-bucket topic tree, is no longer merely
expensive — it is a mechanism this design cannot allow to exist. Corollary:
**there is no such thing as a cross-bucket topic tree.** A genuinely global
view would have to be built per permission set, which is combinatorial;
cross-bucket connections stay structural links (§3), never a shared derived
tree.

### Rejected

- **Tenant-wide rebalance only.** Simpler, and it is the only pass that can
  make every decision. Rejected on the cost curve above. **Revisit if** real
  tenants turn out to hold one bucket each, in which case this distinction is
  free to collapse and nothing is lost by having named it.
- **Per-document rebalance only** (never re-derive structure above the
  document). Cheapest, and closest to LightRAG's pure union model. Rejected
  because it cannot merge duplicate entities arriving from different
  documents, which is the main thing a living graph is *for*.
- **Rebalancing across buckets as one operation** ("re-partition the tenant").
  The most powerful version and the one to be most careful about: it rewrites
  the organisation a user chose. If it ever exists it must propose, not act.

---

## 5. What this makes harder later

1. **The vector-search over-fetch compounds.**
   `aw-knowledgeable-infra.md:187-204` already carries a tenant over-fetch
   because Neo4j vector indexes are per-label, not per-tenant, and warns the
   failure presents as "bad results" rather than as a security finding. The
   in-tree precedent lives with the same thing — `_SCOPED_OVERFETCH = 80` in
   `aw-app-kb/kb_app/mcp_http.py:224` exists because "the filter runs AFTER the
   vector search". A bucket filter is a **second** post-filter on the same
   search, so a small bucket inside a small tenant inside a large index is
   diluted twice, multiplicatively. **K4 needs a bucket variant** (seed two
   buckets in one tenant with near-identical embeddings; assert the in-scope
   bucket still returns its expected row count), and the seam's "log when the
   floor is not met" must report *which* filter starved the result.
2. **Bucket offboarding is a batched delete, forever.** Same door §7.1 of the
   infra doc closes for tenants, one level down: a property-based bucket can
   never be exported as a file or dropped as a database. Write the
   `MATCH (n {tenant:$t, bucket:$b}) DETACH DELETE n` job with the schema.
3. **403-inside-tenant is a convention someone will "fix".** It is the
   opposite of what the rest of this estate does, and it looks like a bug to
   anyone who learned the 404 rule first. The `"403-cross-bucket"` class in
   K1's route table is the mitigation and it is not optional — without it the
   convention lives only in this document.
4. **No user dimension exists, and the graph should not grow one.** If
   "bucket X is visible to user 1 but not user 2 within one tenant" ever
   becomes a requirement, the scope must keep being resolved per request from
   the caller's context (the `kb_index` shape) and mapped to a bucket set — a
   `user` property on a node is the shape to refuse, because it makes every
   node's ACL a data-migration problem.

   **Amendment (2026-09-28), against card `3e95bf3b-9510-8187-9f23-ca98c7412aca`:**
   this is exactly what happened. `aw-knowledgeable-v2-retrieval.md` §7bis
   confirms the trigger fired — per-user bucket scopes now exist, carried on
   the API token — and resolves it exactly as predicted here: scope is
   resolved per request via `resolve_bucket_scopes()`, mapped to a bucket set,
   with no `user` property added to any node. §7bis's own words: this refusal
   is **"vindicated, not reversed."**
5. **One bucket per document is baked in by §1's constraint choice.** Correct
   for the mutable-placement model, and it forecloses "this paper is genuinely
   in both areas" as a *membership* answer. The escape hatch is §3's edge, and
   it is a weaker answer.

---

## 6. Risks for whoever builds this

1. **The guard test is the whole enforcement mechanism, and it is a test.**
   The infra doc is explicit that Neo4j has no `do_orm_execute` analogue and
   that "the runtime hook AP-MT gets for free is bought here with a test"
   (`:151-168`). Bucket inherits that bargain entirely. A template added
   without `bucket` fails K5 or it fails nothing.
2. **Do not widen the composite unique constraint.** §1. It is one line, it
   looks obviously right, and it converts every future placement move from a
   property update into a node merge.
3. **Do not put `bucket` on relationships.** §3. Same shape of mistake, same
   symptom — it will look like symmetry with `tenant`, and the reasoning that
   justifies `tenant` there is about an adversary that does not exist here.
4. **The scope declaration is part of the contract, not a nicety.** An agent
   reading an unlabelled empty result will conclude the knowledge does not
   exist. If the envelope field is dropped as "extra payload", the failure
   surfaces as a confidently wrong agent answer weeks later, attributed to the
   model.
5. **`/api/search` is now load-bearing in three ways** — tenant fanout (infra
   risk 10), bucket scope (§2), and link-target widening (§3) — while still
   having to stay bounded by `q` so it never becomes the whole-graph endpoint
   §5 of the infra doc designed out. It is the single most over-loaded
   endpoint in this design.

---

## 7. What this means for v1, concretely

**Nothing changes in M1–M7 or D1** except one optional reservation, and that
one is a Product Owner call because it touches a milestone already in flight:

- **Recommended, in M3 only** (the `core/graph.py` seam + K5): carry
  `bucket: $bucket_id` in the template catalogue's node patterns with a single
  constant value, and have K5 assert both properties. No endpoint, no UI, no
  query filter. Cost now: one key per template literal and one assertion.
  Cost later: §1.
- **Explicitly not in v1**: `GET /api/buckets`, the scope envelope field, the
  `"403-cross-bucket"` route class, `?scope=current|all`, bucket-aware K4, and
  every part of §4.

Two things to route rather than absorb:

- **To the Product Owner**: whether to take M3's reservation (a small, in-flight
  scope change), and the §2 product question — can a single tenant ever need
  compliance-grade separation between areas? A yes inverts §2's error shaping,
  and it is much cheaper answered before the route table exists than after.
- **Open, deliberately unanswered here**: who creates a bucket and when — a
  user act at upload time, or inferred by the ingestion loop from content? That
  is a question about the RAGA-style construction loop, which is out of scope
  for v1 by the PO's own card, and answering it now would be designing against
  a loop nobody has specified.

---

## 8. Addendum (2026-09-27): folders are not buckets

Status: **amendment to §1's rejection list, and a scoping answer.** Written by
the Architect agent against card `3e85bf3b-9510-81d9-85c4-da4412eca7b6`.
**Nothing already shipped is reopened** — V2 (the flat registry, `bucket_ctx`,
the scope envelope) is Done and deployed and stays exactly as built.

Frederico's ask, verbatim, with a screenshot of the **`kb` app's** file-tree
sidebar (`mapped_folders/docs/{architecture,design,runbooks,standards}`) as his
visual reference:

> *"eu queria ter um estrutura assim de buckets, com sub buckets (sub pastas) e
> quando eu clicasse, eu visse os documentos e como eles se interligam […]
> Dentro da estrutura de arquivos, vamos ter o documento original, na visao do
> grafo, veriamos a interligacao dos documentos; queria experimentar isso"*

### Decision

**Add a `folder_path` string property to `(:Document)` and render it as a
navigable tree. Do not nest buckets, and do not repurpose V3's topic tree for
this.**

The reframe that makes this a decision rather than a compromise: a filesystem
uses **one** mechanism — a directory — for **two** unrelated jobs, and the ask
inherits that conflation. In this system those jobs are already separate, and
one of them is expensive:

| | what it answers | who authors it | cost to change |
|---|---|---|---|
| **bucket** | who may see this at all | a tenant admin, via a token scope | a permission boundary (§7bis) |
| **folder** | how *I* arrange what I can already see | the uploader | a property |
| **topic** (V3) | what the machine found in here | the clustering pass | rebuilt on demand |

Three trees, three jobs, and they are orthogonal on purpose:

- the **folder tree** is *mine* — human-authored, stable, exact, and it is the
  one in the screenshot;
- the **topic tree** (V3, `aw-knowledgeable-v2-retrieval.md:594-628`) is *the
  machine's* — derived from chunk-embedding clusters, with labels from an LLM
  or, degraded, from c-TF-IDF keywords; it drifts on every rebuild and a
  document surfaces wherever its *chunks* cluster, which can be several places
  at once;
- the **graph** is *the links* — `LINKS_TO` (human) and `RELATED_TO` (derived).

So the answer to "does V3 already cover this?" is **no, and yes to a different
half of the sentence.** V3+V4+V5b already deliver "click into a tree, see the
documents, see how they interconnect" as a **discovery** affordance — V5b's own
card says so, and that remains true. What they cannot deliver is *Frederico's
folders, with Frederico's names, in Frederico's order, stable across rebuilds*.
He pointed at `docs/design`, not at `Cluster 3: retrieval, embeddings, neo4j`.
A semantic clustering pass is structurally the wrong mechanism for reproducing a
filing decision a human already made, and the tell is that nobody would accept a
file manager that re-arranged their folders when the corpus grew.

### Where it lands

```
(:Document {tenant, bucket, external_id, label, …, folder_path})
```

One property. No new node, no new relationship, no hop, no change to the
constraint, nothing on the hot path, no authorization semantics.

- **`backend/app/core/graph.py`** — the template catalogue (`:267-463`):
  `list_documents` (`:327-333`) gains an optional folder predicate; a new
  `list_folders` returns the distinct `folder_path` values in the bucket with a
  document count each, on the shape `list_buckets` (`:311-325`) already uses for
  counting; `set_document_folder` and `move_folder_prefix` for the write side.
- **`backend/app/api/documents.py:43`** — `upload_document` gains an optional
  `folder_path` form field. **This is the part the ask does not come with:**
  today upload is a single multipart file and the only thing it knows is
  `file.filename` (`:44`, `:64`) — there is **no source folder path to
  preserve**, because unlike the `kb` app (`apps/kb/kb_app/kb_ops.py:1093`,
  `:1208`, which mirrors a real directory into
  `mapped_folders/<name>/<rel_path>`) this app has no filesystem to mirror. It
  has an upload form. The path has to be *supplied*, and there are exactly three
  supply lines: a browser **directory** upload (`<input webkitdirectory>` hands
  the frontend `File.webkitRelativePath` = `docs/design/foo.md`), an explicit
  field on the single-file upload defaulting to the currently-selected tree
  node, and the D2 service caller (`core/identity.py`'s `X-Internal-Secret`
  branch) passing one from an ingestion connector.
- **`backend/app/api/documents.py`** — a move route, so a mis-filed document is
  not stuck until it is re-uploaded.
- **a new `GET /api/folders`** — flat list of paths + counts, nested by the
  frontend. Bucket-scoped through the existing `require_bucket_read`
  (`core/identity.py:392-405`); it invents no gate of its own.
- **`frontend/src/app.js`** — the tree is a **sidebar in the `library` view**,
  not a fourth `state.view` (`:19`, `switchView` `:63-67`). That is what the
  screenshot shows, and it composes with V5a's bucket switcher instead of
  competing with it: pick bucket → tree filters `renderLibraryGrid` (`:101`) →
  click a document → `openGraph` (`:164`) → M7's existing focus/depth expansion
  (`focusNode` `:239`).
- **"the graph of this folder"** seeds the Cytoscape view from the folder's
  documents and merges through the existing `mergeGraphData` (`:179`), capped,
  with the cap stated in the UI ("showing 30 of 84 — narrow the folder").

### Rejected

- **Nested buckets — a `parent` on `(:Bucket)`, or `IN_BUCKET` with
  `PARENT_OF`.** This is the option §1 itself left a door open for: *"Revisit if
  buckets need to nest (a 'Medicine' bucket containing 'Cardiology'), which is
  the one requirement a flat property genuinely cannot serve"* (`:127-129`). The
  trigger fired, and the answer is still no — because **a fact arrived between
  that sentence and this one that §1 did not have: the bucket became a
  permission boundary** (`aw-knowledgeable-v2-retrieval.md` §7bis, shipped;
  `core/identity.py:368-390`). Nesting a permission boundary is not a schema
  question, it is a scope-inheritance question, and both answers are bad:
  - **inherited** — a grant of `view-one: medicine` silently widens the moment
    anyone creates a child bucket under it. Privilege escalation by creation,
    performed by a user who was only filing documents.
  - **not inherited** — the tree has holes. `GET /api/buckets` filters rows to
    `scopes.visible_buckets()` (`backend/app/api/buckets.py:54-61`), so a token
    holding a child but not its parent either renders an orphan, or renders the
    parent's **name** — which is precisely the leak §7bis Decision 2 deleted the
    `in_scope` field to close (`v2-retrieval.md:794-801`).

  Two further costs, either of which would be enough on its own: a document
  lives in exactly one bucket (`:111-112`, and `graph.py:171-180` on why the
  constraint stays two columns), so a parent bucket renders **empty** while its
  children are full unless every read expands a descendant set — the hot-path
  hop §1 rejected, arrived at from the other direction. And V3's tree is
  per-bucket with per-bucket centroids, so a parent bucket either has no topic
  tree or gets a second clustering pass over its descendants' chunks.

  **What would change this:** a stated requirement to *grant* on a subtree
  ("read everything under Medicine"), or per-sub-area retrieval config. Both are
  bucket-shaped and neither is what was asked for. Filing is not granting.
- **V3's topic tree as the answer, card closed as a clarifying note.** Rejected
  above. Keeping it would ship a tree whose nodes are named by a language model
  and whose membership moves when the corpus grows, in answer to a request for
  `docs/design`. V3/V4/V5a/V5b stay in the backlog **exactly as scoped** — this
  adds a card, it does not edit theirs.
- **A `(:Folder)` registry node with `PARENT_OF`,** by symmetry with Amendment
  1's `(:Bucket)`. Amendment 1's argument was that a bucket *must* be able to
  exist while empty, because it is a boundary you grant before there is data to
  put behind it. A folder is the inverse: it is created by putting something in
  it, and an empty folder has no scope to hold and nothing to authorize. Adopt
  this shape the day folders need to exist empty, carry attributes, or hold one
  document in two places — it is the same argument one level down, and it is
  cheap *then*.
- **Deriving the folder from content** (topic → path). That is a second derived
  tree, i.e. a worse V3, and it destroys the one property the folder tree exists
  to have: that a human put it there.
- **`GET /api/graph?folder=<path>` as a seeded multi-node endpoint.** The
  genuine runner-up, and one request instead of N. Rejected for now because
  `api/graph.py:1-8` and `aw-knowledgeable-infra.md:526-529` guard "no endpoint
  that can return the whole graph" deliberately, and a `folder=` seed is that
  door with a parameter on it. Take it if the N-request version measures badly —
  but then it keeps the node cap **and** reports the cap in the envelope, on
  §2's declared-scope rule.

### What this makes harder later

1. **Two organising axes over one document, and they will disagree.** Moving a
   document between folders does not move it between topics; the topic tree
   keeps showing it wherever its chunks cluster. That is correct and it will
   read as a bug, so each tree has to say on screen what produced it.
2. **A folder rename is O(documents under the prefix), forever.** Batched and
   bounded, but it is a miniature data migration every time — the exact cost a
   `(:Folder)` registry would reduce to a single `SET`. Booked knowingly.
3. **One folder per document is baked in** by the same logic as one bucket per
   document — a path string holds one value. Unlike the bucket case there is no
   §3 edge as an escape hatch; the escape is the `(:Folder)` node above.
4. **Granting on a subtree stays unexpressible.** A token names buckets one at a
   time; "read everything under Medicine" is not a thing this can say, and after
   this card it is still not a thing this can say.

### Risks for the Coders

1. **Folders must acquire no authorization logic whatsoever.** Anything
   folder-shaped appearing near `_bucket_denied` (`core/identity.py:368-390`) is
   the mistake this whole section exists to prevent. And it will not be caught by
   testing through the API: `resolve_bucket_scopes`'s T2-less body
   (`identity.py:333-366`) grants `write` on **every** bucket in the tenant, so
   nothing in the running system denies anything yet. A broken boundary looks
   identical to a working one from outside.
2. **Do not add `list_folders` to `_BUCKET_UNSCOPED_NODE_TEMPLATES`**
   (`graph.py:1250`). That exemption exists for exactly one reason — enumerating
   the registry itself has no single bucket to filter by (`:305-311`). A folder
   listing is bucket-scoped by definition, so it carries `{tenant, bucket}` like
   every other node pattern and K5 must keep asserting it. Pattern-matching the
   exemption is the trap.
3. **Every document that already exists has no `folder_path` at all.** Neo4j
   deletes a null property rather than storing it — `documents.py:67-71` already
   documents this behaviour for `processing_error`. So root is `""` on write, and
   every read must treat **absent** as root too, via `coalesce(d.folder_path,
   '')` **inside the template**, not in Python. That is what makes this a
   backfill-free migration.
4. **The prefix-boundary bug on rename.** `STARTS WITH 'docs/de'` matches
   `docs/design`. Match `folder_path = $from OR folder_path STARTS WITH $from +
   '/'`, and have a test whose fixture contains both `docs/de` and `docs/design`.
5. **`folder_path` goes on `(:Document)` only, never on `(:Chunk)`.** Chunks
   reach it through `PART_OF`. Denormalising it makes a rename rewrite every
   chunk, and §10's re-embed pass already owns that table.
6. **Do not widen the unique constraint.** `(tenant, external_id)`, two columns
   (`graph.py:171-180`). Third time this is written down in these documents.

**Not verified here:** no Neo4j credential is reachable from an Architect
container (the same limitation `aw-knowledgeable-v2-retrieval.md` §2 and §10
both record), so the `coalesce`-on-absent-property and `STARTS WITH` semantics
above are from the Cypher contract, not from a live query. Both are cheap to
check in the first five minutes of the card.
