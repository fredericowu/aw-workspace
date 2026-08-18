---
name: aw-autoskill-architecture-legacy-component
description: Short-circuit any investigation of why architecture test discovery exits non-zero on the `agents-platform-legacy` component — it's a known stale registry row with no repo checkout, already diagnosed 12 times; fix the record instead of re-investigating it again.
auto_generated: true
generated_at: 2026-08-18T03:15:00Z
evidence_sessions: [b74c084d-06b6-4bc9-b9af-49ed7854db64, 49f7d5fc-6874-443c-98aa-71f486b0a2c2, a7f4d743-39d5-4854-aaa4-ddf11f070768, fa998230-fc31-4906-9a19-50f95d7dbd97, 1001e6bd-b6d4-4481-9c34-695a039d3ef8, 79c446d6-7b7b-45eb-9c1e-eedd425465b5, c1d76b2a-2397-4e6e-92fc-2595701a20b3, f14fe255-ba07-4078-b40c-eac88d6b971b, 4324bc78-ed3e-4209-93e4-f3e496579b98, 786c39a8-fe7d-4b09-a391-2a2536822418, bd387fab-0884-4f02-9219-bd37589ba374, 09c6b6f8-6d50-4928-aff2-f5dd634c1669]
---

# aw-autoskill-architecture-legacy-component — the `agents-platform-legacy` exit-1 is already solved

The "Architecture Test Discovery" scheduled task (`POST
/api/apps/architecture/discovery/run`) fires repeatedly and its non-zero exit
gets handed to a fresh agent for triage. **12 separate sessions in one day**
each independently re-ran the exact same investigation — `ls repos/`, `git
log --all` grepping for a rename, `aw__architecture__list_components` /
`get_component`, a KB search — and every single one landed on the identical
conclusion. None of them fixed it, because each was scoped read-only. That
scoping is the bug: re-diagnosing a known fact 12 times is pure waste.

## The known fact (verified independently 12 times — do not re-derive it)

**Component `agents-platform-legacy`:**
- Registered `test_base_path`: `repos/agents-platform-legacy/tests`
- `/opt/aw-workspace/repos/agents-platform-legacy` does not exist and never
  has — not a moved/renamed directory, confirmed via `git log --all` across
  `repos/` turning up nothing and no `.git/config` anywhere referencing that
  name.
- `agents-platform-multitenant` is a **separate, unrelated** checkout (its
  own origin: `github.com/fredericowu/agents-platform-multitenant`) — it is
  NOT `agents-platform-legacy` renamed. Don't propose pointing
  `test_base_path` at it.
- Verdict: an orphaned architecture-catalog row with no corresponding repo.
  Nothing to discover, migrate, or restore — it just needs to stop being
  scanned.

If you land in a session investigating this exact exit code for this exact
component, **skip straight to citing the paragraph above** and go to the fix
below, instead of re-running `ls`/`git log`/KB search/`get_component` again.

## The actual fix (none of the 12 prior sessions did this — do it)

Component curation writes are MCP-only by design (the REST API under
`/api/apps/architecture/` is read-only reads + two execute-actions, see
`apps/architecture/architecture_app/routes.py`'s module docstring — writes
are deliberately "LLM-managed" via MCP, not the REST surface). Two valid
fixes, pick based on whether the row should still exist for other reasons
(requirements/connections/history hanging off it):

**If nothing else references it** — delete it outright:
```
aw__architecture__delete_component slug="agents-platform-legacy" cascade=true
```

**If it should stay as a historical/inactive record** — clear the path so
discovery stops erroring on it:
```
aw__architecture__update_component slug="agents-platform-legacy" test_base_path=""
```

(Tool names as seen through the gateway: `mcp__aw-gateway__aw__architecture__delete_component` /
`...update_component`. Both map to `store.delete_component` /
`store.update_component` in `apps/architecture/architecture_app/store.py`.)

After either fix, the next "Architecture Test Discovery" run will no longer
exit non-zero for this component, and no further triage session should be
needed for it.

## Why this matters beyond this one component

The pattern, not just the instance: when a *scheduled* task's failure output
is what spawns a fresh investigation session each time, and that session is
scoped read-only, the finding never persists anywhere the next run can see —
so the same tool-call sequence repeats indefinitely at real token cost. If a
future triage session is scoped read-only "for safety" but the diagnosis is
this settled, push back and ask whether it should instead be allowed to
apply the one-line MCP fix that ends the recurrence.
