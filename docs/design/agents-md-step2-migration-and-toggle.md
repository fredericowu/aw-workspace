# Design: AGENTS.md decoupling step 2 — migrate 3 blocks, add codegraph block, per-app contribution toggle

Card: `4105bf3b-9510-81f1-870a-cbb2e6a79b23` (target `apps-contribute-agents-md`).
Step 2 of the effort designed in `docs/design/apps-contribute-agents-md-blocks.md`
(step 1, shipped in commits 6156715/066d72f/11acc3b). Three deliverables, in
Frederico's own scoping: (A) migrate the kb / agents-platform-runners /
mcp-gateway sections out of AGENTS.md's hand-authored core into app-owned
blocks, (B) a brand-new codegraph block, (C) a per-app enable/disable toggle
for `agents_md` contributions, default enabled, surfaced in the app's own
Settings modal.

## The approach

A and B are manifest + content work in four app repos, with a strict ordering
rule (block live before core section removed). C is one decision: **the
toggle is app *config*, not registry state.** A new framework-owned boolean,
`agents_md_contribution_enabled`, is injected into
`Manifest.effective_config_schema` whenever the manifest declares
`contributes.agents_md`, stored through the existing config pipeline
(`config_store`), and consulted by `agent_sync._render_region()` when it
rebuilds the managed region. The `agents-md-blocks.json` registry stays a
pure mirror of the manifests, exactly as its docstring promises
(`src/apps/agents_md.py:1-18`), and the reconcile-clobbering problem the card
flags does not need solving, because nothing ever re-asserts config from a
manifest: config is user-owned by construction
(`src/apps/config_store.py:1-50`, the 2026-08-15 crispal incident and
Frederico's "o delete de uma app deve deixar o data/ dela e o settings").

## Deliverable A — migrate three existing sections (mechanical)

Current AGENTS.md section map (verified 2026-11-06): "Knowledge Base — search
before acting" at `AGENTS.md:14-44`, "Inbound message routing" at
`AGENTS.md:46-52`, "MCP servers" at `AGENTS.md:96-137` with the `aw-gateway`
paragraph ("Most tools reach this workspace through **`aw-gateway`** …
authoritative list") as the third paragraph of that section. The managed
region markers already exist and render empty (`AGENTS.md:248-254`; the
registry `AW_WORKSPACE_HOME/agents-md-blocks.json` does not exist yet, which
`read_registry()` reads as `{}` — `src/apps/agents_md.py:100-123`).

Per app (the step-1 doc already names the file convention,
`docs/design/apps-contribute-agents-md-blocks.md:84`):

1. **kb** — repo `repos/aw-app-kb` (app id `kb`, currently v0.46.0). New
   `docs/AGENTS-block.md` holding the "Knowledge Base — search before acting"
   content; `aw-app.json` gains
   `contributes.agents_md: {"path": "docs/AGENTS-block.md", "title": "Knowledge Base"}`.
   The installed package ships the whole repo tree (verified:
   `/opt/aw-workspace/apps/kb/docs/` exists today), so `docs/` needs no
   packaging change.
   *Content adaptation:* the core text hedges with "If the `kb` app genuinely
   isn't installed in this workspace there is nothing to query — but verify
   that before concluding it". Inside an app-contributed block that hedge is
   obsolete — the block existing *is* the proof the app is installed. Trim
   that paragraph to the retry-once/flaky-gateway advice and drop the
   "verify it's installed" half. Keep the rest verbatim; this section is the
   workspace's single most load-bearing instruction.
2. **agents-platform-runners** — repo `repos/aw-app-agents-platform-runners`
   (app id `agents-platform-runners`, v0.212.0). Same move for "Inbound
   message routing" (`/aw-agent-telegram` dispatch convention), title
   `"Inbound message routing"`.
3. **mcp-gateway** — repo `repos/aw-mcp-gateway` (app id `mcp-gateway`,
   v0.46.0; note `contributes` is currently `{}` — this will be its first
   contribution). **Split, not a full move:** only the "Most tools reach this
   workspace through `aw-gateway`…" paragraph moves into its block (title
   `"MCP gateway"`). The rest of the "MCP servers" section (canonical
   `.mcp.json`, how apps write entries into it at boot, the per-CLI fan-out
   table at `AGENTS.md:116-137`) documents `src/libs/agent_sync.py` core
   machinery and stays in the hand-authored core. After the removal, the
   remaining section must still read coherently on its own — the editor
   should re-read it, not just delete the paragraph.

**Ordering rule (non-negotiable):** for each app, release the new app version
and `aw-workspace-cli marketplace install <app> --update` it, verify its
block renders inside the managed region, and only then remove the section
from AGENTS.md's core (then `aw-workspace-cli agent sync` to fan out). Brief
duplication during the window is fine; a window where the content exists
nowhere is not. Registration happens in the provision block at install
(`src/apps/runtime.py:1285`) followed by an inline `sync_agents_md()`
(`runtime.py:1286-1292`), so the block appears without a reboot.

Rendered heading will be `## <title> (app: <id>)`
(`src/libs/agent_sync.py:157-161`) — the moved content should start at
`###`-depth or plain prose, not repeat its own `##` heading.

## Deliverable B — new codegraph block (new content)

Repo `repos/aw-app-codegraph` (app id `codegraph`, v0.8.1). New
`docs/AGENTS-block.md` + `contributes.agents_md` entry, title
`"Code graph"`. Condense from the app's own skill
(`repos/aw-app-codegraph/skills/aw-codegraph/SKILL.md`, "Reach for
`codegraph_explore` first" section) so the two surfaces can't drift — the
block is the 10-line version, the skill stays the reference. It must say:

* `codegraph_explore` is the default first call for "where is X / how does Y
  work / what breaks if I change Z" — before any grep/Read loop;
* it returns verbatim, line-numbered current source (treat as already Read;
  do not re-read files it showed you) plus the call path;
* through the gateway the tools are prefixed `aw__codegraph__…`;
* one pointer to the `aw-codegraph` skill for the full tool table.

Keep it well under the 4 KB advisory (`src/apps/agents_md.py:38`) — every
byte lands in every agent's context on every run. Do not reproduce the
per-tool table.

## Deliverable C — per-app contribution toggle (the design decision)

### Decision

`agents_md_contribution_enabled` is a **framework config property**, stored
in the app's ordinary config and gated at render time. The agents-md registry
is not touched.

### Where it lands

1. **`src/apps/manifest.py` — `effective_config_schema`
   (`manifest.py:527-574`).** After the existing `is_managed_app` block, a
   second independent injection:

   ```python
   if self.agents_md is not None:
       props = {
           "agents_md_contribution_enabled": {
               "type": "boolean",
               "default": True,
               "title": "AGENTS.md contribution",
               "description": "Include this app's instruction block in "
                              "AGENTS.md (and its CLAUDE.md/GEMINI.md/"
                              "copilot mirrors). Takes effect on save.",
               "x-framework": True,
           },
           **props,
       }
   ```

   Conditioned on `self.agents_md` (`manifest.py:226-231`), **not** on
   `is_managed_app` — a Tier-1 app with a block gets the toggle too. The
   `public` field's `x-disabled` trick (`manifest.py:548-567`) is not needed:
   when the manifest declares no block, the prop simply isn't injected.
2. **`src/apps/manifest.py` — `has_config` (`manifest.py:491-507`).** Add
   `or self.agents_md is not None` to the `bool(...)`, so an app whose *only*
   configurable surface is this toggle still gets a Settings button. (All
   four step-2 apps are containers and already pass via `is_managed_app`;
   this is for the general case.)
3. **Storage — nothing new.** The value rides the existing save path:
   `save_app_config` (`src/apps/routes.py:1181-1205`) coerces it with the
   effective schema, mutates `loaded.config`/`ctx.config` in place, and
   persists via `config_store.save()` to
   `AW_WORKSPACE_HOME/app-config/<app_id>.json`, which deliberately survives
   uninstall + reinstall and is never overwritten from a manifest
   (`config_store.py:103` `load()`, restore-on-empty semantics). That is the
   entire "survives re-seed" story: there is no re-seed path to defend
   against.
4. **The gate — `src/libs/agent_sync.py`, `_render_region()`
   (`agent_sync.py:164-194`).** In the per-entry loop
   (`agent_sync.py:175-185`), right after `entry = registry[app_id]`:

   ```python
   if not _agents_md_block_enabled(app_id):
       continue
   ```

   with a small helper reading
   `config_store.load(app_id).get("agents_md_contribution_enabled", True)`
   (truthy coercion; absence = enabled). Importing `src.apps.config_store`
   from `agent_sync` adds no cycle (`config_store` imports only
   `src.apps.paths`; `agent_sync` already imports `src.apps.agents_md`).
   Because the gate sits in `_render_region`, every consumer inherits it for
   free: `sync_agents_md()` (boot, install, CLI `agent sync`) and
   `agents_md_region_drift()` (`agent sync --check`,
   `agent_sync.py:244-255`) — so a disabled block never reads as drift.
5. **Trigger on toggle — `save_app_config`
   (`src/apps/routes.py:1181`).** `previous` is already captured at
   `routes.py:1189`. Near the mcp re-render (`routes.py:1275`), add:

   ```python
   if (loaded.manifest.agents_md is not None
           and bool(previous.get("agents_md_contribution_enabled", True))
           != bool(loaded.config.get("agents_md_contribution_enabled", True))):
       await asyncio.to_thread(agent_sync.sync_agents_md)
   ```

   wrapped in the same try/except-log posture as `runtime.py:1289-1292`, and
   the same "only `sync_agents_md()`, never `sync_all()`" rule (sync_all
   shells out to the Codex CLI per MCP entry and rematerializes skills).
   Disk-file rebuild under the `agents-md` flock, so the multi-worker
   `apps:changed` broadcast needs no hook: there is no per-worker in-memory
   state derived from the region.
6. **UI — zero changes.** `GET /api/apps` already serves
   `effective_config_schema` as `config_schema` (`routes.py:364`, `:1009`),
   and `AppConfigBody.jsx:90-94` spreads the server schema **over** its
   local `MANAGED_APP_PROPS` fallback (`AppConfigBody.jsx:59-81`), so a new
   server-injected boolean renders as a checkbox automatically and saves
   through the same POST.

### What the toggle does NOT do

* It does not unregister the block. `agents-md-blocks.json` remains an exact
  mirror of installed manifests; `register()`'s unconditional overwrite
  (`agents_md.py:154`) stays correct and untouched.
* It does not skip register-time safety checks — marker-injection rejection
  and the size warning (`agents_md.py:68-92`) still run for disabled blocks.
* It does not touch `seeded_state.py`. See rejected alternative 1.

## Rejected alternatives

1. **`enabled` field inside the agents-md registry, protected by a
   `seeded_state`-style carve-out** (the card's leading hypothesis). Rejected
   on three counts. (a) It breaks the registry's stated invariant — a pure
   mirror of manifest declarations (`agents_md.py:1-18`) — and
   `register()`'s unconditional overwrite would need a read-merge-write
   carve-out, i.e. the exact clobbering machinery `tasks.py:171-199` +
   `seeded_state.py` had to grow, for one boolean. (b) The Settings modal has
   no write path into that registry; we would have to special-case the config
   save to write through to a second file, leaving two copies of one bool to
   drift. (c) The config store already has precisely the semantics asked for:
   user-owned, survives uninstall/reinstall, never re-asserted by the app —
   properties `seeded_state` exists to *approximate* for objects that live
   outside config. Runner-up; what would change the call: if the toggle ever
   needed to be flipped by the app itself or by reconcile policy (i.e. stop
   being purely user-owned), registry + seeded_state becomes right.
2. **Literal reuse of `seeded_state.py`.** Wrong tool: it solves "app updates
   content it seeded without clobbering user edits". The manifest never seeds
   this value at all (a schema `default` is not a seeded row), so there is
   nothing to reconcile and nothing to fingerprint.
3. **Per-app `settings_panels` contribution for the toggle.** Every
   contributing app would carry UI boilerplate, and a forgetful app would
   ship an un-disableable block. Framework injection makes the toggle an
   invariant of the mechanism, which is what "if some app contributes … I
   would like a way to disable" means.
4. **"Disable by uninstalling" / editing the block out of the managed region
   by hand.** Both already exist and both are wrong: the first throws away
   the app, the second is reverted wholesale on the next sync by design.

## What this makes harder later

* **Config residue.** The toggle key stays in `app-config/<id>.json` even if
  the app later drops `contributes.agents_md` (dead key, invisible in the UI
  since the schema prop vanishes with the manifest entry). Harmless, but a
  future "show all disabled contributions" admin surface must sweep
  `config_store.list_saved()` rather than read one registry file.
* **`src/libs` → config semantics.** `agent_sync` now encodes "what config
  key gates a block". If a second gated contribution surface appears
  (skills? knowledge?), the key-naming and gate-placement precedent set here
  becomes the pattern — pick the name accordingly (`<surface>_contribution_
  enabled`).
* **Blocks become install-coupled docs.** After A, four core instructions
  live in app repos: fixing a typo in the KB mandate now means an app
  release + `marketplace install --update`, not an AGENTS.md edit. That is
  the point of step 2, but it changes the editing workflow for the most
  load-bearing prose in the workspace, and `agent sync` alone will NOT pick
  up a block edit that never shipped through an app update (the registry
  points at the installed package copy, `apps/<id>/docs/AGENTS-block.md`).

## Risks for the Coders

* **Content-loss window.** The one real failure mode in A: removing a core
  section before the app block is live. Follow the ordering rule; verify
  with `rg "search_knowledge_base" AGENTS.md` style checks after each step —
  content present exactly once.
* **Release mechanics, per recorded lessons.** mcp-gateway auto-releases on
  push; container app code changes need the publish workflow actually run (a
  green CI without publish ships old code); the marketplace raw catalog can
  lag ~5 min after release (poll via `gh api`, not the raw URL); never
  hand-bump versions where the release automation owns the bump — follow
  each repo's existing release convention.
* **`agent sync --check` in CI.** After the core edits, run
  `aw-workspace-cli agent sync` then `agent sync --check` and commit the
  regenerated mirrors (CLAUDE.md / GEMINI.md / copilot-instructions.md) in
  the same change, or CI drift checks go red.
* **Boolean coercion.** The config POST arrives as JSON; `_coerce_config`
  runs against the *effective* schema (`routes.py:1188-1192`), which now
  contains the new prop — confirm a string `"false"` from any older client
  path coerces, same as `auto_start` does.
* **Tests to pin (aw-workspace core):** (1) `_render_region` skips a
  disabled app and includes an absent-key app; (2) `agents_md_region_drift`
  reports no drift for a disabled-and-synced state; (3) a toggle flip
  through the save endpoint rebuilds the region (and only then); (4) the
  registry file is byte-identical before/after a toggle flip (pure-mirror
  invariant); (5) `effective_config_schema` injects the prop iff
  `contributes.agents_md` is declared. Apply the mutation discipline:
  un-apply the gate and confirm test 1 fails.
* **The four blocks land only via app updates.** Local repo edits under
  `repos/` do nothing until released + installed; the registry entry points
  into `apps/<id>/…`. Don't "verify" against the repo copy.
