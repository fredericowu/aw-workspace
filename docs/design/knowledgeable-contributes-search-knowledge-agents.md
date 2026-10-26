# aw-app-knowledgeable contributes the 6 search-knowledge agents + the `search-knowledge` DAG workflow

Architect decision for Kanban card `4045bf3b-9510-810b-9bda-d1438c59a5e5`
("contribute the 6 search-knowledge agents + the search-knowledge DAG workflow
as app-managed source, not hand-edited DB rows"). Written 2026-10-25, against
live state fetched the same day (`get_agent` x6, `get_workflow`, `.mcp.json`,
`gateway.json`, installed provider v0.188.0).

## 1. The approach

Add `agents:contribute` to `repos/aw-app-knowledgeable/aw-app.json`
(`permissions`, currently `aw-app.json:24`) and a `contributes.agents` block
declaring **one model, six agents and one workflow**. No core change, no
AP-MT change, no agents-platform-runners change: every mechanism this needs
already exists and is already installed.

The Bearer-token finding is resolved by the provider's existing
**`mcp_servers` by-reference form**, scoped-profile variant: the three
gateway-scoped retrievers declare

```jsonc
"mcp_servers": [
  {"name": "knowledgeable", "server": "aw-gateway", "profile": "knowledgeable-retriever"}
]
```

instead of an `mcp_config` literal. At seed time the provider resolves the
`aw-gateway` entry from the workspace's own `.mcp.json`, appends
`/knowledgeable-retriever` to its URL, and stores it under the key
`knowledgeable`
(`repos/aw-app-agents-platform-runners/agents_platform_runners_app/agent_provisioner.py:530`
`resolve_mcp_servers`, dict form documented at `:463`, crispal-full precedent
at `:491`). **Verified byte-exact against live**: `.mcp.json`'s `aw-gateway`
is `type: http`, `url: http://aw-app-mcp-gateway:9200/mcp`, and its
`Authorization` header equals both the token in the three live agents'
`mcp_config` and `gateway.json`'s own `token`. Resolution therefore
reproduces the live `mcp_config` exactly, with zero credential in the repo.
The credential is also **re-asserted on every activation**
(`agent_provisioner.py:405` `_refresh_credentials`, by-reference entries
only), so a gateway token rotation heals itself, which hand-edited rows never
did. A missing gateway profile is loudly logged per activation
(`agent_provisioner.py:616` `probe_scoped_profiles`).

Everything else is exact reproduction of the live rows: prompts as
`system_prompt_file` under `prompts/`, the 13-node `kind: "dag"` graph inline
in the manifest, `workspace: "aw"` as a literal, the `codex-runner-gpt-5`
model copied from the live row.

> **Superseded in part by
> `docs/design/search-knowledge-citable-external-sources.md` (card
> `4055bf3b-9510-81de`):** once that card lands, the live graph is **14
> nodes** (new `sources-digest` transform; `synthesize` gains it as a need
> and a new template) and `knowledgeable-retriever-sources`' prompt step 4
> replies `OK` instead of echoing the tool JSON. Copy-from-live at build
> time absorbs this automatically; the counts and prompt prose in this doc
> do not.

## 2. Where it lands

All changes in **`repos/aw-app-knowledgeable`** (one marketplace release):

* `aw-app.json`: `"agents:contribute"` appended to `permissions` (line 24),
  new `"agents"` key under `contributes` (line 35). Format reference:
  `repos/aw-app-call-agent/aw-app.json` (credential-free `mcp_servers`
  precedent) and `repos/aw-app-agents-platform-test-fixtures` (workflows
  precedent).
* `prompts/knowledgeable-retriever-{vector,lexical,graph,sources}.md`,
  `prompts/knowledgeable-search-planner.md`,
  `prompts/knowledgeable-playground.md`: the six live `system_prompt`s,
  copied from `get_agent` at build time (not from the card).
* `tests/test_contributed_agents.py`: shape tests, pattern:
  `repos/aw-app-call-agent/tests/test_contributed_agents.py` (including its
  `test_contribution_contains_no_credentials_or_fixed_gateway_url`
  equivalent).
* `scripts/verify_live_parity.py` (new, small): fetches the 6 agents + the
  workflow live and field-compares them against the resolved manifest, so
  "reconcile produces no diff" is checkable by QA instead of eyeballed.

What the declaration contains, per kind:

* **`models`** (1): `codex-runner-gpt-5`, copied from the live row
  (`provider: runner`, `model_id: codex-runner-gpt-5`, `display_name:
  "Codex GPT-5 (aw Runner)"`, `params: {cli: codex, runner: aw-codex,
  timeout_s: 900, dangerous_skip_permissions: true}`). It must be declared,
  not just referenced: the only app that declares this slug today is
  `repos/aw-app-crispal/aw-app.json:279`, and crispal is **not installed on
  this workspace** (it lives on its own hosted tenant), so on a fresh DB the
  reference would dangle. Create-if-absent + 409-tolerance
  (`agent_provisioner.py:169` seed, `:352`) makes the double declaration
  safe; on this tenant it is a no-op.
* **`agents`** (6), exactly the live field values:
  * `knowledgeable-retriever-vector` / `-lexical` / `-graph`:
    `model_slug: codex-runner-gpt-5`, `tool_specs: ["knowledge.search"]`,
    `disable_harness_tools: true`, `workspace: "aw"`, the `mcp_servers`
    scoped reference above, `system_prompt_file`, live
    `name`/`description`/`use_cases`.
  * `knowledgeable-retriever-sources`: **superseded by
    `docs/design/knowledgeable-sources-mcp-tool.md` §2c (card
    `4055bf3b-9510-81c5-8641-e7bf3e270ba4`)** — the live row now carries the
    same `mcp_servers` scoped reference as the other three
    (`knowledge_sources` added to the `knowledgeable-retriever` gateway
    profile's `tools_allow`), not "same minus MCP". Declare it identically:
    `model_slug: codex-runner-gpt-5`, `tool_specs: ["knowledge.sources"]`
    (kept deliberately — inert on the runner path, restores the native tool
    if ever flipped back to API-direct), `disable_harness_tools: true`,
    `workspace: "aw"`, the `mcp_servers` scoped reference above,
    `params: {"max_tokens": 16000}`.
  * `knowledgeable-search-planner`, `knowledgeable-playground`: no tools, no
    MCP, `disable_harness_tools` stays false (omit), `workspace: "aw"`.
  * `icon`/`color` are live at platform defaults (`bot`, `#58a6ff`); omit
    them (an omitted field is never reconciled, and defaults already match).
* **`workflows`** (1): `search-knowledge`, `kind: "dag"`, the full 13-node
  graph **inline** (core has no `graph_file` sugar: `src/apps/agents.py:135`
  `FILE_FIELDS` covers only `system_prompt`/`instructions`/`dataset`), plus
  live `name`/`description`/`use_cases`. Copied from
  `get_workflow(slug="search-knowledge")` at build time. The provider's
  allowlist covers it (`agent_provisioner.py:107`: `slug, name, description,
  use_cases, kind, graph`).

Untouched: `src/apps/agents.py`, `agents_platform_runners_app/*`, AP-MT, the
gateway named config, the live Target, and the other 8 OpenAI-direct agents.

## 3. The card's four questions, answered

**Q1: does `agents:contribute` exist?** Yes; ten installed apps declare it
(`aw-app-call-agent`, `aw-app-uc-phd`, `aw-app-maintenance-agents`, ...).
It is an existing capability in core's catalog, so there is **no
capability rollout**: adding the permission string to the manifest is the
whole step. Format reference: `repos/aw-app-call-agent/aw-app.json`.

**Q2: does a workflows-contribution surface exist?** Yes, and nothing needs
building. Core `KINDS` includes `workflows` (`src/apps/agents.py:130`), the
provider has the `/api/workflows` endpoint + ordering
(`agent_provisioner.py:107`, `ORDER` at `:128`, workflows after agents
because the graph names agents by slug), and
`repos/aw-app-agents-platform-test-fixtures` already ships 4 workflows
through it (floor: runners `>=0.80.0`; installed here: **0.188.0**, verified
to contain both the scoped-profile form and the `workspace` field in
`/opt/aw-workspace/apps/agents-platform-runners/.../agent_provisioner.py:105`).
There is no separate `workflows:contribute` capability; `agents:contribute`
covers the whole block.

**Q3: reconcile semantics / idempotent upsert.** The surface already is an
idempotent upsert with hand-edit protection:

* Seed pass (`agent_provisioner.py:169`): slug exists → no create; for
  by-reference entries only, `mcp_config` is re-PUT (`:405`). On first
  activation over today's live rows this writes a byte-identical
  `mcp_config` to the 3 retrievers and creates nothing. Zero behavior diff.
* Reconcile pass (`src/apps/agents.py:271` `_reconcile` +
  `src/apps/seeded_state.py:206` `updatable_fields`): first activation finds
  no baseline, writes no fields, and **adopts the manifest values as the
  baseline** (tenant-shared, in AP-MT's `seeded_objects` table via
  `read_state`/`write_state`). From then on, a manifest change is pushed
  only onto fields whose live value still fingerprints to what was seeded;
  a UI-tuned field is never touched.
* Deletion durability: AP-MT deletes are **soft**. A deleted agent or
  workflow is restored on the next activation (`RESTORE_ENDPOINTS`,
  `agent_provisioner.py:143`, restore path at `:352`), and a genuinely
  absent row is re-created. Either way the app now owns recreation, which
  is the card's original problem.

The catch that makes "no perceptible diff" conditional: **adoption
baselines the manifest, so the manifest must byte-match live.** See §5.

**Q4: effect on Target `openai-direct-agents-swap-to-runner`.** None. This
card covers exactly the 6 search-knowledge agents + the workflow. The
Target's other 8 OpenAI-direct agents
(`knowledgeable-extractor-4o-mini/nano/runner`, `knowledgeable-labeller`,
`research-theme-convergence`, `openai-o4-mini-probe`, ...) and the separate
`knowledgeable-explorer` agent (which has its own gateway profile) stay
hand-managed and out of scope.

## 4. Exactness requirements (the part that can silently fail later)

`seeded_state.updatable_fields` classifies any live field that does not
fingerprint to the recorded baseline as hand-edited and leaves it alone
forever. Adoption records the **manifest** values, so every divergence
between manifest and live at delivery time becomes a field the app can never
again update. Two known divergences and their fixes:

1. **Trailing newline.** `resolve_file_fields` deliberately `rstrip()`s
   prompt files (`src/apps/agents.py:331`, comment at the rstrip). The four
   retriever prompts live **end with `\n`**; planner and playground do not.
   So a prompt-file manifest yields values one byte short of live for those
   four. Fix: a **one-time normalization PUT** (raw REST,
   `PUT /api/agents/{slug}` with the rstripped `system_prompt`) on the four
   retrievers, done as part of delivery. Zero behavior change (one trailing
   newline), and afterwards live == manifest == baseline, so future
   app-shipped prompt fixes actually propagate.
2. **Everything else is copy-from-live.** `description`, `use_cases`,
   `name`, the workflow `description` and graph must be copied from
   `get_agent`/`get_workflow` at build time. The graph is inline JSON, and
   fingerprints hash the parsed value (`json.dumps(..., sort_keys=True)`),
   so formatting/key order are not a problem; content is.

`scripts/verify_live_parity.py` exists to make both checkable: it must
report zero field diffs after install.

## 5. What I rejected, and why

* **Token literal in the manifest**: forbidden by the card, and also worse
  mechanically: a hand-spelled `mcp_config` is treated as app/user-owned
  (`apply_mcp_references` skips entries that carry `mcp_config`), so token
  rotation would silently brick all three retrievers. By-reference gets
  rotation-healing for free.
* **App mints its own scoped API key / named config at activate** (the
  `aw-autoskill-ap-mt-scope-agent-to-one-tool` flow, the card's first
  suggestion). It would require this Tier-1 app to hold the gateway admin
  token (another app's data dir) and reimplement what
  `apply_mcp_references` + `_refresh_credentials` already do as the
  platform idiom. More code, more credentials in more places, same result.
* **Vault/secret reference in the manifest**: no resolution mechanism for
  that exists in the provisioner; building one duplicates
  `apply_mcp_references`.
* **`workspace_qualified: true`**: rewrites slugs to
  `<slug>-aw` (`src/apps/agents.py:391`), which breaks both live parity and
  the workflow graph's bare-slug agent references. Not usable here.
* **A core `workspace_bound` flag** (set `workspace` from `AW_WORKSPACE`
  without slug rewrite): the *runner-up*, and the right fix if this app
  ever needs to install correctly in another tenant. Rejected now because
  it needs a core change + deploy for something a literal `"workspace":
  "aw"` does exactly and conservatively today. Revisit when portability is
  a real requirement.
* **Contributing the gateway named config**: no contribution surface for
  `gateway.json` configs exists in `repos/aw-mcp-gateway`; building one is
  real scope this card does not need. The profile
  (`knowledgeable-retriever`: `upstreams: [aw-knowledgeable]`,
  `tools_allow: [knowledge_search]`) stays admin-managed state; its absence
  is logged per activation by `probe_scoped_profiles`, and recreation is
  documented in the app README.
* **`graph_file` sugar in core `FILE_FIELDS`**: cosmetic, deferred; the
  inline graph is ugly but correct.
* **A separate app**: Frederico said "pela app do knowledgeable", verbatim.

## 6. What this makes harder later

* **Portability.** `workspace: "aw"` and a deployment-specific model row are
  now baked into a marketplace app. Same-tenant reinstall is safe
  (create-if-absent on the same rows); a *different* tenant installing
  knowledgeable gets agents pinned to a workspace named `aw` that may not
  exist there. The `workspace_bound` runner-up is the escape hatch.
* **Edits in the UI fork permanently.** Post-adoption, any UI tweak to a
  prompt/field makes that field hand-owned; the app can never update it
  again without a manual re-baseline. That is the surface's contract, but
  for agents that were until now "just rows someone edits", it is a culture
  change worth saying out loud.
* **The gateway profile is still a hand-made dependency.** A fresh
  workspace needs `knowledgeable-retriever` created in the gateway before
  the three retrievers have tools. Visible (probe warning), not durable.
* **Two declarers of `codex-runner-gpt-5`.** crispal (its tenant) and
  knowledgeable (this one). Never collide live, but a future param change to
  that model has two manifests to update.
* **Model params are effectively create-only.** AP-MT has no
  `GET /api/models/{slug}` (verified: 404), so the provider's reconcile
  `read()` returns None for models and only the create path runs. A later
  params change to the declared model will not propagate via reconcile.

## 7. Risks for the Coder (and the tests that won't catch them)

1. **The `mcp_servers` local key must be `"knowledgeable"`** (the dict's
   `name`), because that is the server key in the live `mcp_config`; a
   different key produces a different (still working) config and a
   permanent parity diff. No test catches this except the parity script.
2. **Do not also set `mcp_config`** on those three entries;
   `apply_mcp_references` skips any entry that already carries one.
3. **Verify `POST /api/agents` persists `workspace`.** The provider sends it
   (allowlisted), and PUT is known to accept it (that is how the live rows
   got it), but the fresh-create path must be proven: a silently dropped
   `workspace` on recreate reproduces the NULL-workspace wrong-runner 401
   bug. QA's delete/reseed test must assert `workspace == "aw"` on the
   recreated row.
4. **The normalization PUTs (§4.1) are part of the delivery**, not
   optional polish. Skipping them changes nothing today and silently
   freezes the four retriever prompts forever.
5. **Copy live, not the card.** The card's inventory was accurate on
   2026-10-25; `get_agent`/`get_workflow` are the source of truth at build
   time.
6. **Test harness `.mcp.json` trap**: agent test harnesses drop a
   credentialed `.mcp.json` into app repos (standing memory). Unit tests for
   resolution must use `resolve_mcp_servers(config_path=...)` /
   `apply_mcp_references(config_path=...)` seams, never the workspace file,
   and nothing credentialed may be committed.
7. **Release mechanics**: version bump belongs to the release bot (never
   hand-bump); after the release, `marketplace install knowledgeable
   --update` can lag ~5 min on GitHub's raw CDN; a green release workflow
   can still ship old code until publish.yml runs.
8. **QA deletion test exercises the restore path, not create.** `delete`
   soft-deletes; reseed restores (`restored on reseed` log line). To prove
   the true fresh-create path, use the provisioner's unit-test transport
   seam (pattern:
   `repos/aw-app-agents-platform-runners/tests/test_agent_provisioner.py`)
   or a disposable environment; do not hard-delete production rows.

## 8. QA protocol (for the QA dispatch, summarized)

1. Shape tests green in the app repo (no credential literal anywhere in the
   package, 3 and only 3 entries carry the scoped reference, 4 carry
   `disable_harness_tools`, graph's agent ids ⊆ declared slugs, prompt
   files exist and rstrip-match the fixtures).
2. Install the release; `scripts/verify_live_parity.py` reports zero diffs;
   provider logs show "already exists/already existed" + "refreshed mcp
   credentials" and no creates.
3. Soft-delete `knowledgeable-retriever-vector`, re-activate the app,
   assert restore + `workspace == "aw"` + working `mcp_config` + intact
   prompt.
4. One live `run_workflow_async(search-knowledge)` smoke against the card's
   baseline shape (4 arms, dedup counts populated, synthesis cites chunk
   ids).
5. Do not close the card: Frederico + the Source review before closure
   (card instruction).
