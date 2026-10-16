# aw-app-uc-phd — `contributes.agents`: Quality Researcher + Fable/Sonnet/Opus researchers

Architect decision for Kanban card `feature:uc-phd-contributes-agents-thematic-analysis`
(page `3fb5bf3b-9510-81e7-8325-ff9d37ee2800`). Written 2026-10-16.

## 1. The approach

Add a `contributes.agents` block to `repos/aw-app-uc-phd/aw-app.json` declaring
**one agent config, one group, and four agents — no models, no flows**. The
three model-variant researchers share **one prompt file** (the proven
Telegram-family pattern, `repos/aw-app-agents-platform-runners/aw-app.json`
agents block + `tests/test_contributed_agents.py`); the Quality Researcher
gets its own. The shared Braun & Clarke methodology and the multi-model
experiment lessons live in the **group's `instructions_file`**, so all four
agents inherit one copy. Models are **referenced, not declared**: the
`claude-runner-fable/sonnet/opus` model rows already exist (seeded by
`aw-app-agents-platform-runners`, which is also the provider — if it's absent
nothing seeds anyway, so the reference can never dangle in a workspace where
seeding happens at all).

**The seed-once gotcha the card asks us to decide on is no longer the whole
story, and the decision is: rely on reconcile, keep slugs stable.** The card
(and `docs/contributing-agents.md`, and the core test docstring) describe
create-once-never-update. Core has since grown a reconcile pass:
`src/apps/agents.py:270` (`_reconcile`) re-reads every declared object on
every activation and **pushes corrected fields back** via the provider's
`read_contributed_agent`/`update_contributed_agent`
(`repos/aw-app-agents-platform-runners/agents_platform_runners_app/plugin.py:708`,
docstring: "An existing object's CONTENT is reconciled, not left forever as
it was"), guarded by the hash-based hand-edit check in
`src/apps/seeded_state.py:206` (`updatable_fields`) — a field the user tuned
in the UI is never touched, a field still holding the app-seeded value is
updated to the new version's value. So: **a prompt fix in a later release
reaches existing installs**, unless Frederico hand-edited that specific field,
in which case his edit wins — which is the behaviour we want. No slug
versioning (`phd-fable-v2`), no contributed-skill indirection needed for this.

## 2. Where it lands

All changes in `repos/aw-app-uc-phd` (currently v0.18.0, zero contributed
agents, manifest keys `routes`/`windows`/`knowledge` only).

### 2.1 Manifest (`aw-app.json`)

* `permissions`: add `"agents:contribute"` — **risk: low**
  (`src/apps/capabilities.py:40`), so the unsigned personal-account app can
  hold it; no signing gate.
* `contributes.agents`:

```jsonc
"agents": {
  "agent_configs": [
    { "slug": "uc-phd-research-config", "name": "UC PhD Research",
      "description": "Workspace MCP gateway plus workspace file access — researchers read datasets and write analysis files under .tmp/.",
      "mcp_servers": ["aw-gateway"],
      "permissions": { "workspace_access": true, "tmp_access": true } }
  ],
  "groups": [
    { "slug": "uc-phd-researchers", "name": "UC PhD Researchers",
      "description": "Qualitative-research assistants for Frederico's UC DEI PhD — thematic analysis per Braun & Clarke, grounded in the course's own framing.",
      "instructions_file": "prompts/agents/researchers-group.md",
      "capabilities": "Thematic coding and analysis of qualitative data (forum threads, transcripts, documents) following Braun & Clarke's six stages; multi-analyst reconciliation." }
  ],
  "agents": [
    { "slug": "phd-fable",  "name": "PhD Researcher — Fable",
      "system_prompt_file": "prompts/agents/researcher.md",
      "model_slug": "claude-runner-fable",  "agent_config_slug": "uc-phd-research-config",
      "group_slug": "uc-phd-researchers", "disable_harness_tools": true, ... },
    { "slug": "phd-sonnet", "name": "PhD Researcher — Sonnet", "model_slug": "claude-runner-sonnet", /* same shape */ },
    { "slug": "phd-opus",   "name": "PhD Researcher — Opus",   "model_slug": "claude-runner-opus",   /* same shape */ },
    { "slug": "phd-quality-researcher", "name": "Quality Researcher",
      "system_prompt_file": "prompts/agents/quality-researcher.md",
      "model_slug": "claude-runner-opus", "agent_config_slug": "uc-phd-research-config",
      "group_slug": "uc-phd-researchers", "disable_harness_tools": true, ... }
  ]
}
```

  Fill in per agent: `description`, `capabilities`, `use_cases`
  (what `list_agents` pickers read), `icon`/`color` (distinct per model,
  mirror the Telegram family's colors so the model tier reads at a glance).
  `disable_harness_tools: true` follows every runner-backed agent in the
  runners manifest — copy it, don't innovate.
* `dependencies.apps`: add `agents-platform-runners` with `required: false`,
  reason documenting that it provides both the seeding surface and the
  `claude-runner-*` model slugs these agents reference (pattern:
  `repos/aw-app-template/examples/contributes-agents/aw-app.json`).

Slug collision check done against the live platform (2026-10-16): no
`phd-*` or `uc-phd-*` agent slugs exist; `uc-phd-researchers` and
`uc-phd-research-config` are likewise free. `telegram-fable/-sonnet/-opus`
exist, which is exactly why the bare names are not used.

### 2.2 Prompt files (new, English — Frederico was explicit)

```
prompts/agents/researchers-group.md     # group instructions: shared methodology
prompts/agents/researcher.md            # one prompt, three model variants
prompts/agents/quality-researcher.md    # arbiter persona
```

Content requirements (the card's §"Content the agents' instructions must
cover" is the authoritative source; `.tmp/activity3/arbiter.md` for the
experiment detail — both read in full for this design):

**`researchers-group.md`** — the shared contract:
1. Braun & Clarke's six stages **in the course's own framing** (card lists
   them verbatim: familiarise → generate initial codes → generate initial
   themes → review themes → define and name themes → write-up), stated as
   cyclical/iterative, not linear.
2. The course's codes-vs-themes distinction: a code labels a specific piece
   of data and says what it is about and *why it is interesting* — not a
   one-word label, not a catch-all, not an analyst-imposed abstraction; a
   theme is a central organising concept, not a topic summary.
3. The experiment lessons, as operative rules (not a history lesson):
   * **Evidential bar** — a code grounded in one throwaway line is a hook,
     not a pattern; before keeping a candidate code, re-check that it recurs
     across multiple posts/replies. (Every round-1→2 gain in the live
     experiment was a model conceding it had overrated thin evidence.)
   * **Topic vs interpretation self-check** — if the code restates what the
     text is about ("metformin isn't great for post-meal spikes") rather than
     what is interesting/meaningful about the talk, it is a topic summary;
     reframe or reject.
   * **Method ≠ finding** — don't merge "they experiment on themselves
     (n-of-1)" with "the standard rule doesn't hold for their body"; the
     clearest round-2 correction was splitting exactly this pair.
   * **In-vivo coding** — mirror participants' own wording where it retains
     more nuance than a paraphrase.
   * **Locators, never usernames** — refer to posts/replies by number
     (Post 3 R5), never reproduce usernames or identifying details.
4. One line pointing agents at the app's own data tools
   (`phd_knowledge_base` MCP prefix) and at `search_knowledge_base`.

**`researcher.md`** — the analyst role: works independently; produces
candidate codes → selected set (8–12, each with name, one-line definition,
grounding locators, one-line justification) → rejected set with stated
weakness; when shown other analysts' work, reconsiders in both directions
(adopt what two independent readers converged on *only after re-reading the
data*; drop own outliers) but **never adopts a code it doesn't believe the
data supports just to converge** — if a re-read still supports the original
call, keep it and say why. The raw data, not the other analysts, is the
authority.

**`quality-researcher.md`** — the arbiter/reviewer role: compares analysts'
selected sets for **substantive-concept agreement, not wording** (same
underlying concept differently named is already agreement — never ask anyone
to rename to match); runs bounded refinement rounds (state the decision rule
and threshold up front; in round-2 prompts, withhold the arbiter's own
mapping/near-miss list so convergence comes from re-reading, not
target-naming); records dissent rather than overruling it silently; treats a
concept moving *away* from agreement as evidence of honest re-reading, not
failure; synthesizes final definitions to carry contested readings honestly.
Explicitly: fabricated consensus destroys the analysis — an analyst holding
a line against the majority with a factual rebuttal (the experiment's
"placenta" code, re-grounded at P3 R3/R5) is the system working.

### 2.3 Tests (new: `tests/test_contributed_agents.py` in aw-app-uc-phd)

Model it on `repos/aw-app-agents-platform-runners/tests/test_contributed_agents.py`:
* the `agents:contribute` permission is declared;
* every `system_prompt_file`/`instructions_file` the manifest names **exists
  in the repo** — this is the test that matters most, because core drops a
  missing file's field with only a warning (`src/apps/agents.py`,
  `resolve_file_fields`) and seeds an agent with an **empty prompt**;
* the three researchers share one prompt file; all four name
  `uc-phd-research-config` and `uc-phd-researchers`;
* model slugs are exactly the three `claude-runner-*` strings;
* prompt/group files contain sentinel phrases ("Braun", "six", "usernames")
  so gutting the content fails a test.

### 2.4 Untouched

`uc_phd_app/` Python, `ui/`, migrations, routes, windows, knowledge — no
code change at all; this is manifest + prompt files + a test.

## 3. Rejected alternatives

* **Declaring own model rows** (e.g. `uc-phd-fable`) — duplicates the runner
  models; worse, declaring the *same* `claude-runner-*` slugs would make two
  apps reconcile the same rows with competing fingerprint baselines.
  Reference by slug only.
* **Reusing `agent-config-aw-full`** (the runners app's config) — works
  today, but couples this app to another app's slug and that config carries
  no explicit file-access permissions. Own config keeps the reconcile
  baseline ours and the privilege statement explicit.
* **Shipping the methodology as a contributed skill**
  (`contributes.skills` + `skill_slugs`) — the doc's recommended mitigation
  for seed-once, and the Telegram family's shape. Runner-up. Rejected for
  v1 because the reconcile pass already solves prompt-update-ability, and a
  skill adds a second distribution surface (skill sync, reinstall-to-refresh
  semantics) for content only these four agents consume. Revisit if other
  agents/humans start needing the methodology text.
* **Slug-versioning (`phd-fable-v2`) as the seed-once answer** — obsolete
  given reconcile; would orphan sessions/runs/retro-scores hanging off the
  old slug on every content fix.
* **Bare slugs `fable`/`sonnet`/`opus`** — free today, but one `list_agents`
  away from being mistaken for the Telegram family; the card itself warns
  against this.
* **An `agent_flows` entry wiring researchers → quality-researcher** — the
  ask is four agents, not a flow; Frederico dispatches these ad-hoc (the
  live experiment was driven by hand). Add a flow when a repeatable
  pipeline emerges.
* **Two groups (analysts vs arbiter)** — the methodology is identical for
  both roles; a split doubles the surface for zero divergence today.

## 4. What this makes harder later

* **First activation sets the reconcile baseline.** From then on, any field
  Frederico tunes in the UI permanently stops receiving manifest updates
  (by design). If v1 prompts are weak and he patches them live, later
  releases silently won't touch those fields — the fix channel becomes the
  UI, not the repo. Get v1 prompt quality right *before* release.
* **Coupling to `claude-runner-*` slugs.** If the runners app ever renames
  its model slugs, these agents point at nothing and fail only at dispatch
  time — nothing validates cross-app slug references at seed time.
* **Renames are new objects.** Changing a slug later creates a fresh agent
  and strands the old one (uninstall removes nothing, deliberately).
* **One shared group** means the arbiter can't diverge methodologically from
  the analysts without a group split (= new slugs) later.

## 5. Risks for the Coder

* **A wrong prompt path fails silently** — the agent seeds with an empty
  prompt and a single log warning. After install, `get_agent` each of the 4
  slugs and assert `system_prompt` is non-empty and contains "Braun".
* **Release**: conventional-commit push (`feat: ...`); the release bot owns
  the version bump — do **not** hand-bump `version` in `aw-app.json`, and
  never put `[skip release]` in the commit. Then
  `aw-workspace-cli marketplace install aw-app-uc-phd --update`; this app
  has a known stale-catalog trap — use `?refresh=true` / retry after the
  ~5-min raw-CDN lag before concluding the release failed.
* **Tier-1 seeding trigger**: seeding runs in `_register_agents` on app
  activation (`src/apps/runtime.py:1075-1092`). Verify with `list_agents`
  right after install; if the 4 agents aren't there, seeding will run on the
  next core restart — one is **already owed** for v0.18.0 and was
  deliberately deferred by Frederico on 2026-10-16, so ask him rather than
  restarting, and never restart core from inside your own run (it kills the
  run).
* **Check for a sideloaded copy** of aw-app-uc-phd before installing — a
  sideload package dir shadows the marketplace copy.
* The card carries the Braun & Clarke stages verbatim and the full lesson
  list; `.tmp/activity3/arbiter.md` has the experiment detail (final code
  list, round-2 prompt — a good template for the arbiter prompt's
  refinement-round instructions). `.tmp/` survives container recreation but
  is not a durable store: the prompt files must stand alone, no references
  to `.tmp` paths in shipped content.
