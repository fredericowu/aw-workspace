# Design: apps contribute instruction blocks to AGENTS.md

Card: `40f5bf3b-9510-8128-84fa-c6ae3d8523e7` (target `apps-contribute-agents-md`).
Step 1 of 2 — step 2 (decoupling AGENTS.md content into per-topic files) is a
follow-up and is deliberately NOT designed here beyond "don't block it".

## The approach

Apps declare one Markdown block via a new `contributes.agents_md` manifest
entry (path-based, like `contributes.skills`). The apps framework records it
in a small registry file at `AW_WORKSPACE_HOME/agents-md-blocks.json`
(the `skill_sources.py` precedent), and **`sync_agents_md()` regenerates a
marker-delimited managed region at the bottom of AGENTS.md from that registry
on every sync** — boot, CLI, UI — before fanning the file out to the mirrors.
Install/activate writes the registry entry and triggers one sync; uninstall
removes the entry and triggers one sync. The hand-authored core of AGENTS.md
is everything outside the region and is never touched by machinery.

The load-bearing choice: the managed region is **always rebuilt whole from
the registry**, never patched incrementally. A block whose app is gone from
the registry vanishes on the next sync no matter which worker ran the
uninstall or whether the lifecycle hook fired at all. This is a direct
response to two recorded failure modes of the push-only `contributes.skills`
design: `arch:app-activate-skips-agent-sync` (activate copies skills but
never syncs, so the result is invisible to CLIs) and the write-once-skills
bug (update + explicit sync both left the merged tree stale, silently).

## AGENTS.md template / marker format

Appended once, by hand, at the end of today's AGENTS.md (committed):

```markdown
<!-- aw-apps-md:begin -->
<!-- GENERATED SECTION — app-contributed instruction blocks.
     Managed by src/apps/agents_md.py + `aw-workspace-cli agent sync`.
     Do not hand-edit below this line: any sync regenerates this whole
     region from the installed apps' manifests. Put your own instructions
     ABOVE the aw-apps-md:begin marker. -->

<!-- aw-app-md:begin aw-app-foo -->
## Foo (app: aw-app-foo)

...the app's markdown, verbatim from its package...
<!-- aw-app-md:end aw-app-foo -->

<!-- aw-apps-md:end -->
```

Grammar (one regex each, anchored to line starts):

- Region: `<!-- aw-apps-md:begin -->` … `<!-- aw-apps-md:end -->`, at most
  one per file, everything between them owned by the rebuild.
- Block: `<!-- aw-app-md:begin <app-id> -->` … `<!-- aw-app-md:end <app-id> -->`.
  Blocks are emitted sorted by app id, each prefixed with a generated
  `## <title> (app: <app-id>)` heading (title from the manifest), so apps
  don't fight over heading levels and a reader always sees the owner.

Parsing rules (fail-safe is the contract, because a mangled AGENTS.md
poisons every agent in the workspace within one sync):

- Region markers absent → append the empty region at EOF (first-run migration
  is therefore automatic for mirrors/forks of this repo).
- Exactly one `begin` without `end` (or vice versa), or `end` before `begin`
  → **refuse to rewrite**, log an error, report `success=False` in the
  `FileSyncResult`, leave the file byte-identical. Never truncate, never
  guess.
- Writes are atomic (`tmp` + `os.replace`) under an `fs_lock` flock
  (`locks/agents-md.lock`) — `AW_WORKSPACE_WORKERS=10` means N processes can
  sync concurrently (src/apps/fs_lock.py explains why flock, and why lock
  files are never unlinked).
- No-op detection: if the regenerated file equals the current bytes, don't
  write (keeps mtime stable and `agent sync --check` honest).

Step-2 compatibility: step 2 splits the *core* (above the region) into
per-topic files; the region contract — "AGENTS.md = free-form core + one
machine-owned region" — is unchanged by that, and the block grammar already
supports non-app owners if step 2 wants to reuse it (`aw-app-md:begin` is
just an id-carrying delimiter). Nothing here assumes the core is monolithic.

## Manifest schema (`aw-app.json`)

```json
"contributes": {
  "agents_md": { "path": "docs/AGENTS-block.md", "title": "Foo" }
}
```

- Single object, not a list — one block per app (mirrors
  `contributes.knowledge`). The marker grammar tolerates multiple blocks per
  id, so growing this to a list later is a schema-only change.
- `path` is relative to the package dir, resolved and validated exactly like
  `resolve_skill_dir` does for skills (`src/apps/skills.py:64` — no `..`
  escape, file must exist). `title` optional, defaults to the app's `name`.
- Validation at register time: reject content containing `<!-- aw-app-md` or
  `<!-- aw-apps-md` (marker injection breaks the parse); demote nothing, but
  log a warning when the block exceeds ~4 KB — every byte lands in every
  agent's context on every run, and the real fix for bloat is step 2.

## Where it lands (files and seams)

1. **`src/apps/agents_md.py` (new)** — validation + the registry
   (read/write `AW_WORKSPACE_HOME/agents-md-blocks.json`,
   `{app_id: {"path": <abs path into apps/<id>/…>, "title": …}}`), modeled on
   `src/apps/skill_sources.py:60-80` (`registry_path`/`read_registry`).
   Registry mutation under the same flock as the file rebuild.
2. **`src/apps/manifest.py`** — `agents_md` property next to `skills`
   (src/apps/manifest.py:221-223), returning the dict or `None`.
3. **`src/apps/runtime.py`** — `_register_agents_md(loaded)` called in the
   provision block right after `_register_skills`
   (src/apps/runtime.py:1252-1258); journaled as `agents_md:register`
   (payload: app id only — removal is registry-based, not payload-based);
   reversal added to the journal replay switch next to `skill:register`
   (src/apps/runtime.py:1469). After the provision block's registrations, the
   runtime calls `agent_sync.sync_agents_md()` **directly** — AGENTS.md +
   mirrors update at install time, not at next boot. Only `sync_agents_md()`,
   NOT `sync_all()`: `sync_all` shells out to the Codex CLI per managed MCP
   entry (src/libs/agent_sync.py sync_codex_mcp) and rematerializes skills —
   per-app-load that is slow and has side effects beyond this surface.
   Same call on the real-uninstall path (not the upgrade half — an upgrade's
   unload+load nets out to a refresh anyway and register is idempotent).
4. **`src/libs/agent_sync.py`** — `sync_agents_md()`
   (src/libs/agent_sync.py:132-143) gains a rebuild step before the existing
   3-target `_copy_file` fan-out: read registry → read each block's source
   file fresh → regenerate the region → atomic write if changed. A registry
   entry whose source file is gone (app dir manually deleted) drops the block
   with a warning — unlike `skill_sources`' keep-on-`ok:False` rule, this is
   a local file read, not a plugin call: missing means uninstalled, there is
   no "transient outage" state to protect. `aw-workspace-cli agent sync
   --check` (src/cli/commands/agent.py) must treat "region differs from
   registry" as reportable drift and write nothing.
5. **`AGENTS.md`** — one-time hand edit adding the empty region + banner at
   EOF; committed. Also a short paragraph in the "what apps contribute" area
   of the core text documenting the mechanism (AGENTS.md documents itself —
   the mirrors pick it up via the normal fan-out).
6. **`repos/aw-marketplace/schemas/aw-app.schema.json`** +
   **`repos/aw-marketplace/scripts/validate_manifest.py`** (the
   referenced-files-must-exist pass, scripts/validate_manifest.py:118) —
   schema addition + existence check for `contributes.agents_md.path`.
7. **Tests** — `src/tests/unit/apps/test_contributed_agents_md.py`
   (register/unregister/collision/marker-injection/malformed-region refusal)
   following `test_contributed_tasks.py`'s shape, plus rebuild cases in
   `src/tests/unit/libs/test_agent_sync.py` (region absent → appended;
   registry entry removed → block gone; malformed region → byte-identical
   file + `success=False`; no-op → no write).
8. **Follow-ups outside this repo** (coder should note, not block on):
   `aw-create-app` skill in the app-template repo should document the new
   key once this ships.

## Decisions on the open questions

**Hand-edit guard (card §4): no `seeded_state`-style guard.** The region
banner declares the content generated; rebuilding whole-region from the
registry makes in-region hand edits ephemeral *by design*. Rationale:
app blocks are app-owned instructions, not user-tuned objects —
`seeded_state.py` exists for rows the user legitimately owns after seeding
(schedules, prompts), which has no analogue here. A user who wants to
override an app's instructions writes theirs in the core section (above the
region), which always wins by position and is never touched. The
hash-no-clobber approach on skills is also exactly what produced the
write-once staleness family of bugs; not importing that trade-off is a
feature.

**Which sync runs after injection (card's `arch:app-activate-skips-agent-sync`
callout): two layers.** The runtime hook calls `sync_agents_md()` inline at
install/uninstall (immediate visibility), and the rebuild living *inside*
`sync_agents_md()` means even a future caller who forgets the inline call is
healed by the next boot/manual/UI sync — the bug class is contained
structurally, not by discipline.

## Rejected alternatives

1. **Generate AGENTS.md wholly from a source template** (`AGENTS.core.md` +
   blocks → generated AGENTS.md). Rejected: every doc, memory and habit in
   this workspace says "AGENTS.md is the source of truth — edit it, never
   the mirrors". Flipping that to "edit AGENTS.core.md" invalidates years of
   recorded guidance and muscle memory for zero functional gain; the marker
   region achieves the same separation inside the existing file.
2. **Inline markdown in `aw-app.json`.** JSON-escaped multi-line markdown is
   unreviewable in PRs and unvalidatable by eye; path-based follows
   `contributes.skills`/`contributes.knowledge`.
3. **Pointer-only blocks** ("read `skills/<app>/…` for instructions").
   Defeats the purpose — the mechanism exists precisely because referenced
   files are not reliably loaded into agent context; that direction is step
   2's per-topic decoupling, done properly.
4. **Push-only lifecycle hook, no sync-time rebuild** (the straight
   `skills.py` port). Rejected: it recreates the two recorded bug shapes
   named above, and with `WORKERS>1` an uninstall landing on a
   never-provisioned worker has no journal entry to replay (documented in
   src/apps/skills.py:17-30), stranding the block forever. Registry +
   full-region regeneration is exact-mirror by construction.
5. **Marker-scan in-place editing without a registry.** Same stranding
   problem on uninstall, plus the CLI sync process (no apps loaded) would
   have no authority to decide which blocks are orphans. The registry file is
   exactly the `skill_sources.py` answer to "sync runs where plugins don't
   live".

## What this makes harder later

- **AGENTS.md is git-tracked and now machine-mutated**: every install/
  uninstall dirties the working tree. Accepted — this tree is a live
  deployment that already runs dirty (see `docs/cli/*.md` in `git status`),
  and the region's current state is legitimate content to commit. If the
  noise becomes a problem, the only clean out is step 2 (markdown has no
  include mechanism, and the mirrors are byte copies).
- **Context budget**: every installed app now adds tokens to *every* agent
  turn in the workspace. The design deliberately makes contributing easy, so
  bloat is a when-not-if; the 4 KB warning is advisory only. Step 2 is the
  structural fix — this is the strongest argument for actually doing it.
- **One block per app** is baked into the manifest shape (not the markers);
  growing to multiple named blocks per app is a schema+registry change.
- The registry lives under `AW_WORKSPACE_HOME` (survives reinstall), so a
  registry entry outliving a manually-deleted app dir produces a
  warn-and-drop on every sync until uninstall runs properly — doctor could
  grow a check, not in scope here.

## Risks for the Coders

- **A bad rewrite of AGENTS.md is instantly workspace-wide** — the mirrors
  copy it to CLAUDE.md et al., which every agent session loads. The
  fail-safe parse (refuse on malformed markers, atomic writes, no-op
  detection) is not polish, it is the core safety property. Test the
  malformed-region refusal path first.
- **Do not call `sync_all()` from the runtime hook** — Codex subprocess
  per managed MCP entry on every app load will slow boot reconcile across
  ~dozens of apps and touch unrelated surfaces. `sync_agents_md()` only.
- **`agent sync --check` must stay write-free** with the rebuild step added —
  easy to regress by reusing the write path for the diff.
- The existing tests pin `sync_agents_md`'s current signature
  (`src/tests/unit/libs/test_agent_sync.py`) — extend, don't fork.
- Run core's pytest directly, not via `aw-workspace-cli test` (it clobbers
  PYTHONPATH; see the `aw-autoskill-core-pytest-direct` skill), and set
  `AW_APPS_LOCK_DIR` to a tmp dir in tests so no test takes the live
  provisioning lock (src/apps/fs_lock.py's own warning).
- Mirror files may be read-only (0444) from skills_sync's generated-marker
  posture — `_copy_file` already handles today's mirrors, but if a write
  fails with EACCES, chmod-first rather than skipping.
