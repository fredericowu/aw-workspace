# `contributes.knowledge` + the cli_reference ownership split

Architect decision for Kanban card
`feature:app-contributes-knowledge-docs-folder`
(page `3f55bf3b-9510-81f5-ab1c-d7d0127d3391`). Written 2026-10-11.

Frederico's ask: core CLI docs belong committed under `/opt/aw-workspace/docs`;
app-hooked CLI docs belong in the owning app's own `docs/`; and for the second
half to be reachable, an app must be able to **contribute knowledge** — declare
a docs folder that core mirrors under
`.aw-workspace/knowledge_base/apps/<app-id>/`.

Design only — nothing here is implemented yet. Every path below was read
before being cited.

---

## 0. Facts established while designing — two of them change the card's premises

1. **`cli_reference/` is not a set of authored docs. It is 100% generated.**
   `src/libs/cli_reference.py:201-233` (`sync_cli_reference`) captures
   `aw-workspace-cli <cmd> --help` for the root, every discovered command and
   every subcommand, and writes one `.md` per capture, with a bilingual
   intent line (`:109-157`) and frontmatter (`:77-90`). It is re-run by the
   `kb-reference` scheduled task. "Moving the 35 files" is therefore the wrong
   frame — the job is **changing the generator's destination per owner**, and
   the files follow.

2. **The generator has no delete pass.** `sync_cli_reference` only writes
   (`:218-231`); nothing ever removes a doc for a command that stopped
   existing. An uninstalled app's `--help` capture lives in
   `cli_reference/` forever today — the exact staleness failure the card
   fears for `apps/` is already live in the current tree. The split must add
   exact-mirror semantics, not just re-route output.

3. **The ownership split, derived mechanically** (the card's fact 1, executed —
   `src/cli/discovery.py:44-46` built-ins, `:48-62` app loop):

   | Owner | Commands |
   |---|---|
   | **core** (15 built-ins, → 25 of the 35 files incl. subcommand pages + `root.md`) | `agent`, `apps`, `doctor`, `folders`, `help`, `kb-reference`, `logs`, `marketplace`, `restart`, `sideload`, `start`, `status`, `stop`, `test`, `update` |
   | `agents-platform-runners` | `agents-platform` |
   | `architecture` | `architecture` |
   | `aw-app-uc-phd` | `uc-phd-index` |
   | `kb` | `knowledge-base` |
   | `knowledgeable` | `knowledgeable-ingest` |
   | `notion` | `notion-sync` |
   | `remote-host-cli` | `remote-hosts` |
   | `secrets` | `secrets` |
   | `ssh` | `rsync`, `ssh` |

   The card's eyeball list was wrong in one place it itself predicted it might
   be: `kb-reference.md` is **core** (`src/cli/commands/kb_reference.py`), not
   kb-app-hooked. Nine apps, nine-plus-one commands, never hand-sorted again.

4. **No capability gates `contributes.skills`** (`src/apps/runtime.py:989-1008`
   registers it with no permission check), while `tasks`/`agents`/`repos` each
   take a `*:contribute` capability. Which precedent `knowledge` follows
   decides most of question E — see §5.

5. **`contributes` is open in every validator.** Core's hand-written validator
   tolerates unknown keys (`src/apps/manifest.py` module docstring; the strict
   checks at `:1155-1175` only inspect known keys). aw-marketplace's
   `schemas/aw-app.schema.json` sets no `additionalProperties: false` on
   `contributes`, so an unknown key passes jsonschema. **aw-backend mirrors
   only the capability catalog** (`repos/aw-backend/src/api/app_capabilities.py`
   — "control-plane mirror of … capabilities.py"), and validates no
   `contributes` key at all.

6. **The KB's pgvector build prunes; its folder sync does not.**
   `repos/aw-app-kb/kb_app/kb_ops.py:208-210` deletes every indexed doc whose
   file disappeared from `KB_DIR` — anything living *directly* under
   `knowledge_base/` self-heals. The live 57-ghost-file bug
   (`mapped_folders/docs/architecture/`) is in the mapped-folder **sync copy**,
   a different code path that only content routed through `mapped_folders/`
   inherits.

---

## 1. The approach

Add a `contributes.knowledge` point: an app declares one docs directory, and
core **copies** it (exact mirror, content-hash-gated) into
`knowledge_base/apps/<app-id>/docs/` on every activate, removing it on
uninstall. **Ownership is the namespace** — the `apps/<app-id>/` directory name
— not a marker file, which makes cross-app collisions impossible by
construction instead of detected at write time. No new capability, matching
the `contributes.skills` precedent, which is what reduces question E from a
three-repo lockstep to "core first, then apps".

Independently, split the `cli_reference` generator by owner, read off
`discover_commands()`: built-in captures go to **`docs/cli/`** in this repo
(committed; writes are checksum-gated so the tree only dirties when a
command's interface actually changed), app-command captures go to
**`knowledge_base/apps/<app-id>/cli_reference/`** (generated, workspace-local,
never committed anywhere). Both passes get the delete-stale semantics the
generator is missing today, and `knowledge_base/cli_reference/` is retired
once the new locations are verified searchable.

So `apps/<app-id>/` ends up with exactly two sub-origins, each exact-mirrored
by its own writer:

```
.aw-workspace/knowledge_base/apps/<app-id>/
  docs/            ← mirrored from the app's contributes.knowledge (on activate)
  cli_reference/   ← generated --help captures (kb-reference task)
```

---

## 2. The decisions, A–F

### A. Manifest shape — a single object with a `path`, its own small module

```jsonc
"contributes": {
  "knowledge": { "path": "docs" }          // dir, relative to the app package dir
}
```

- **One directory, not a list.** Every app's ask today is "point at my
  `docs/`". If an app later needs two trees, the compatible extension is
  accepting a list of these objects — nothing in the mirror layout has to
  change. No label field: the app id *is* the label (the namespace).
- **New module `src/apps/knowledge.py` (a `KnowledgeRegistry`), not a reuse of
  `SkillsRegistry`.** The skills machinery (`src/apps/skills.py`) exists to
  solve two problems knowledge does not have: unprefixed-id collisions between
  apps (`:38-43`, the `.aw-app-id` marker + `SkillError`) and preserving a
  user's hand-edits to a copied skill across no-op reboots (`:84-125`, the
  content-hash no-clobber dance). A mirrored docs tree is app-owned reference
  material — the app's copy always wins, the destination is namespaced, so
  the right semantics are a plain exact mirror (hash-gated to skip no-op
  copies). Borrow `resolve_skill_dir`'s escape check (`src/apps/skills.py:64-78`)
  for path validation, the journal pattern for uninstall, nothing else.
- **Validation** in `src/apps/manifest.py`'s `_validate`, next to the skills
  check at `:1163-1165`: `knowledge` must be a dict whose `path` is a
  non-escaping relative directory. Plus a `Manifest.knowledge` property next
  to `.skills` (`:217-218`).
- **No new capability** (see §0.4): writing docs into the shared KB tree is
  strictly lower-blast-radius than writing executable instructions into the
  shared `skills/` tree, which already takes none. This is also the decision
  that keeps aw-backend out of the rollout entirely (§5).

### B. Ownership on sync — the namespace IS the marker

Skills need `.aw-app-id` because every skill lands unprefixed in one flat
directory and `materialize()` must know which entries are not its to delete
(`src/libs/skills_sync.py:31-37, :204-217`). Knowledge never has that problem:
the registry only ever writes inside `apps/<its-own-app-id>/docs/`, so one
app's sync **cannot** touch another's by construction. Rules:

- The activate-time mirror for app X exact-mirrors into `apps/X/docs/` only.
- `apps/X/cli_reference/` is the kb-reference generator's to exact-mirror, and
  it never touches `apps/X/docs/`.
- A **reconcile sweep** (in the apps reconciler, after the install roster is
  known) deletes any `apps/<id>/` whose id is not in the installed set — the
  self-heal for every path where uninstall cleanup was missed.
- Drop a `.generated`-style README at `apps/` root (same idea as
  `skills_sync.py`'s `GENERATED_MARKER`, `:60, :193-201`) telling humans and
  agents where the sources live.

No `.aw-app-id` files. Copying the marker pattern was the runner-up; rejected
because it reintroduces a collision-detection code path that the namespace
makes unnecessary, and markers are exactly the kind of invisible state that
drifts (a marker-less dir after a partial copy is ambiguous; a namespaced dir
never is).

### C. Uninstall — journal entry for parity, but cleanup must not depend on it

Register journals `knowledge:register {dest_path}` and the uninstall replay
(`src/apps/runtime.py:1399-1403` is the `skill:register` analogue) rmtrees it.
**But** the journal is in-memory per worker, and with `AW_WORKSPACE_WORKERS>1`
an uninstall can land on a worker that never provisioned the app — the exact
gap `src/apps/skills.py:17-30` documents for skills, where `unload()` silently
no-ops the mirror deletion. Skills can't fix that generically because their
destinations aren't derivable; knowledge's is. So:

1. the uninstall path **unconditionally** purges
   `knowledge_base/apps/<app-id>/` (idempotent, journal-independent), and
2. the reconcile sweep (§B) catches anything that still slipped through.

An orphaned docs tree teaching agents about a removed CLI command — the
AGENTS.md failure mode — is thereby handled three times over: journal replay,
unconditional purge, roster sweep. The pgvector rows for the deleted files are
pruned by the next `--build` (verified, `kb_ops.py:208-210`).

### D. KB indexing — `apps/` dodges the staleness class; aw-knowledgeable needs one line; the folder-sync bug stays a separate card

- `apps/**` lives directly under `KB_DIR`, so `kb_app.kb_ops._build` walks it
  with **zero kb-app changes** and — verified — **prunes** docs whose files
  vanish (`kb_ops.py:208-210`). It is *not* routed through the mapped-folder
  sync, so it cannot inherit the `mapped_folders/docs/architecture/` ghost-file
  class. No new mechanism to build, only one to avoid.
- **aw-knowledgeable** (graph side) scans a fixed allowlist:
  `SUBTREE_ORDER` in `repos/aw-app-knowledgeable/knowledgeable_app/bulk_ingest.py:111-118`
  (`crispal, memory, notion, cli_reference, skills, mapped_folders`). Without
  `"apps"` added, the graph never ingests the new tree. One-line change in
  that app's repo → **its own card**.
- The committed core captures (`docs/cli/`) are indexed through the `docs`
  mapped folder, which **does** sit on the buggy sync path — a deleted
  built-in command's doc would ghost in `mapped_folders/docs/cli/` exactly as
  `architecture/` ghosts today. That is a pre-existing kb-app bug affecting
  all of `docs/`, not a regression introduced here; fixing the folder-sync
  prune is the **second kb-side card** the Kanban card anticipated. Retiring
  `knowledge_base/cli_reference/` should wait for it (§5 step 6), otherwise we
  trade a fresh direct-indexed copy for a ghost-prone synced one.

### E. Rollout order — and why this design shrinks the blast radius

The two hazards the card names, checked against source rather than assumed:

- *"Core validator must land first or installs fail"* — true for **unknown
  permissions** (strictly validated, the aw-app-kb release failure), **not**
  for unknown `contributes` keys, which every validator in the chain tolerates
  (§0.5). By **not** adding a `knowledge:contribute` capability, this design
  moves the change entirely into the tolerant category: an app shipping
  `contributes.knowledge` against an old core installs fine and the key is
  ignored (docs just don't mirror yet). Core-first remains mandatory for
  *function* and because new core validates the key's shape strictly — but
  mis-ordering degrades soft instead of failing installs.
- *"The manifest schema is mirrored in aw-backend and aw-marketplace"* —
  partially true. aw-marketplace mirrors the schema + canonical validator;
  **aw-backend mirrors only the capability catalog**
  (`app_capabilities.py`) and validates no contributes keys — with no new
  capability, **aw-backend takes no change at all**. (If a future revision
  does add a capability, the full three-clock order in the
  `new-capability-rollout-order` memory applies: core → aw-backend →
  marketplace schema → apps.)

The ordered sequence:

| # | Repo | Change | Lands how |
|---|---|---|---|
| 1 | `aw-workspace` (core) | `manifest.py` knowledge property+validation · `src/apps/knowledge.py` registry · runtime register/journal/uninstall-purge · reconciler sweep · discovery owner-stamp · `cli_reference.py` split + delete pass + `docs/cli/` writer | direct push to master; deploy via the core restart path; verify live per the `core-deploy-verify-and-mirror-sync` skill (don't trust "doctor green" alone) |
| 2 | `aw-marketplace` | schema: `contributes.knowledge` property · `validate_manifest.py`: declared path must exist in the repo (the dangling-reference class, `scripts/validate_manifest.py:118-138`) | PR (precedent: PR #1118) |
| 3 | `aw-backend` | **nothing** (verified §0.5) | — |
| 4 | the 9 app repos (§0.3 table) | author/move the command's docs into `docs/`, add `contributes.knowledge`, version bump, release, `marketplace install --update` per app | direct push per app repo + CI release; step 2 should be merged first so release CI validates the new key's shape |
| 5 | `aw-app-knowledgeable` | `"apps"` in `SUBTREE_ORDER` | its own card/release |
| 6 | migration/verification | run `kb-reference` (new split now live) · commit `docs/cli/` · verify `search_knowledge_base` returns an app doc from `apps/<id>/…` and a core doc from `docs/cli/…` · then delete `knowledge_base/cli_reference/` (next `--build` prunes the index rows) | gated on the kb folder-sync prune fix if core retrieval must not regress (§D) |

Steps 1 and the cli_reference half are separable: the generator split depends
only on the discovery owner-stamp, not on `contributes.knowledge`, so the two
core changes can be two cards built in parallel (see §7).

### F. Copy, not symlink, not bind

**Copy.** Both alternatives fail on mechanics this codebase has already
documented:

- **Symlink** — app containers see `knowledge_base/` wholesale through
  `$AW_KB_DIR` (`src/apps/runtime.py:1939-1958`); a symlink pointing at the
  app's package dir resolves to nothing on the far side of the bind — the
  precise failure `skills_sync.py:25-29` records ("an agent that silently
  finds no skills"). And the link target itself is wiped on every app update
  (`skills.py:9-12`), so the link dies mid-update even host-side.
- **Bind-mounting each app's docs dir into the KB tree** — binds are fixed at
  container creation; every app install/uninstall would recreate the kb app's
  container (the mapped-folders cost, AGENTS.md), and a bind can't outlive its
  source during reinstall windows.
- **Index-in-place via mapped folders** — routes every app's docs through the
  folder-sync path that currently ghosts deleted files (§0.6), and would need
  one mapping per app.

Copy semantics: exact mirror into `apps/<id>/docs/` on every activate, skipped
when a stored content hash of the source dir matches (same
`_hash_dir` approach as `skills.py:143-154`) so a no-op boot does no I/O.

---

## 3. Where it lands

| File | Change |
|---|---|
| `src/apps/manifest.py` | `Manifest.knowledge` property (next to `:217`); shape validation next to `:1163` |
| `src/apps/knowledge.py` | **new** — `KnowledgeRegistry.register/unregister` (mirror + hash-gate + escape check) |
| `src/apps/runtime.py` | `_register_knowledge` beside `_register_skills` (`:989`), called from both activate sites (`:1205`, `:1634`); journal kind `knowledge:register` in `_revert` (beside `:1399`); unconditional namespace purge in `unload`/uninstall |
| `src/apps/reconciler.py` | roster sweep: delete `apps/<id>/` for ids not installed (near `uninstall`, `:1092`) |
| `src/cli/discovery.py` | stamp `module.__aw_app_id__ = slug` in `_load_app_command` (`:26-38`); built-ins get none. Zero caller churn — `discover_commands()`'s return type is unchanged |
| `src/libs/cli_reference.py` | `sync_cli_reference` splits by owner stamp: built-ins → `docs/cli/<slug>.md` (checksum-gated write, GENERATED header), app commands → `knowledge_base/apps/<id>/cli_reference/<slug>.md`; **both passes delete stale files in their own target dir** |
| `docs/cli/` | **new** — committed generated core-CLI reference |
| `repos/aw-marketplace` | `schemas/aw-app.schema.json` + `scripts/validate_manifest.py` (step 2) |
| `repos/aw-app-knowledgeable` | `bulk_ingest.py` `SUBTREE_ORDER` + `"apps"` (step 5) |
| 9 app repos | `docs/` + `contributes.knowledge` (step 4) |
| untouched | `aw-backend` entirely; `src/apps/skills.py`; `skills_sync.py`; the kb app (for `apps/` — the folder-sync prune fix is its own card) |

## 4. Rejected alternatives

1. **Reuse `contributes.skills` machinery with a `kind: knowledge` flag** —
   drags in edit-preservation and collision semantics knowledge must not have
   (an app's docs must always win; §A), and couples two lifecycles that
   evolve independently.
2. **`.aw-app-id` marker files for ownership** — solved problem via
   namespacing; markers reintroduce drift and ambiguity (§B).
3. **A `knowledge:contribute` capability** — would force the aw-backend
   catalog mirror + marketplace permission-enum updates into the critical
   path (three-repo lockstep, the `state:own`/PR-#1118 class) for no security
   gain over the ungated `skills` precedent (§A, §E).
4. **Commit app `--help` captures into app repos** — a capture is a product of
   the *installed workspace* (version, environment), not of the repo; CI can't
   regenerate it honestly, and it drifts the moment the app updates. Authored
   docs are the repo's; captures are the workspace's.
5. **Keep core captures in `knowledge_base/cli_reference/` instead of
   `docs/cli/`** (the runner-up, and retrieval-wise the safer one — direct
   KB_DIR indexing with working prune, vs the ghost-prone folder-sync path).
   Rejected because the PO's ask is explicit: core CLI docs committed under
   `docs/`. Committed-generated has precedent here (`aw-app-template` ships a
   committed `ui/dist`), and a PR that shows a `--help` diff when a command's
   interface changes is genuinely useful review surface. The retrieval risk is
   mitigated by sequencing (§E step 6 waits for the folder-sync prune fix).
6. **Fan knowledge out through `agent sync` instead of activate-time copy** —
   docs change only on app update, which re-activates; wiring it into
   `agent_sync.sync_all()` adds a second writer with its own ownership rules
   for zero freshness gain.

## 5. What this makes harder later

- **Knowledge is per-install state.** An app's docs are searchable only where
  it's installed. A cross-workspace docs portal (marketplace-rendered app
  docs) gets the pointer from `contributes.knowledge` but no transport — that
  would be new aw-backend/marketplace work this design deliberately avoids.
- **The single-`path` shape** means an app wanting disjoint trees must
  restructure under one dir until the list extension lands.
- **`docs/cli/` as committed-generated** bakes in a tree-dirtying scheduled
  task: when a command's `--help` changes, the shared working tree shows
  modified files until someone commits. Checksum-gating makes this rare and
  coincide with real CLI changes, but concurrent agents will occasionally see
  it, and a hand-edit to `docs/cli/*.md` is silently overwritten on the next
  tick (the GENERATED header is the only guard).
- **Two writers under `apps/<id>/`** (activate-time mirror, kb-reference
  task): any third origin must land inside the namespace and bring its own
  exact-mirror pass, or it recreates the ownership problem.
- **Retiring `knowledge_base/cli_reference/`** removes the best-scoring
  retrieval docs from the direct-indexed tree; if the folder-sync prune fix
  stalls, core CLI retrieval rides a known-buggy path. The sequencing gate in
  §E step 6 is the mitigation — it is also a real coupling.

## 6. Risks for the Coders

- **The missing delete pass is the trap.** If the generator split re-routes
  output without mirroring-with-deletes per target dir, the migration leaves
  all 35 old files in `cli_reference/` *plus* the new copies — double hits in
  every search. The old tree's removal is explicit migration work (§E step 6).
- **Command shadowing:** `discover_commands()` is last-writer-wins
  (`discovery.py:62`, apps iterate after built-ins, sorted by slug). Owner
  attribution must follow the module that actually won the name, and a
  shadowed built-in should be logged — today it's silent.
- **WORKERS>1:** never rely on the journal alone for cleanup (§C). The
  unconditional purge + roster sweep are not optional hardening; they are the
  actual fix.
- **Tier-1 reload:** the core change needs a real core restart to be live
  (`tier1-inprocess-apps-have-no-restart-path` memory), and "doctor green"
  does not prove the new code is running — verify per the
  `core-deploy-verify-and-mirror-sync` skill.
- **`kb-reference` cadence:** a freshly installed app's `--help` capture
  appears only on the next scheduled tick, not at activate. Its mirrored
  `docs/`, by contrast, appears immediately. Don't "fix" the former by wiring
  capture into activate — capturing shells out per command and is priced for
  a periodic task (`cli_reference.py:33-39`).
- **Escape check:** `knowledge.path` must get the same treatment as
  `resolve_skill_dir` (`skills.py:64-78`) — a `../` path writing outside the
  package dir into the KB tree is the vulnerability class to test for.

## 7. Recommended card breakdown

1. **core: `contributes.knowledge`** — manifest + registry + runtime +
   reconciler sweep + uninstall purge (§3 rows 1–4). The enabler; everything
   app-side waits on it.
2. **core: cli_reference split** — discovery stamp + generator split +
   `docs/cli/` + delete passes. Parallel with card 1 (no dependency between
   them).
3. **aw-marketplace: schema + validator** (PR). Before any app release.
4. **apps ×9: adopt `contributes.knowledge`** (§0.3 table) — can be one
   sweep card or per-app; each is mechanical once 1+3 are live.
5. **aw-app-knowledgeable: `apps` subtree** — one line + release.
6. **kb app: folder-sync prune fix** — pre-existing bug, now load-bearing for
   step 7; the card the Kanban card predicted.
7. **migration + verification** — regenerate, commit `docs/cli/`, verify
   retrieval from both new locations, delete `knowledge_base/cli_reference/`.

Order: 1‖2 → 3 → 4‖5 → 6 → 7.
