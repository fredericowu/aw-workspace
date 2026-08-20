---
name: aw-autoskill-architecture-legacy-component
description: The `agents-platform-legacy` architecture-catalog row was deleted on 2026-08-20 after 21 independent triage sessions re-diagnosed the same stale-row exit code without fixing it. If this description or a similar exit-1 for this slug ever resurfaces, read this first — do not re-run the 12-step diagnosis a 22nd time.
auto_generated: true
generated_at: 2026-08-18T03:15:00Z
last_updated: 2026-08-20T03:15:00Z
evidence_sessions: [b74c084d-06b6-4bc9-b9af-49ed7854db64, 49f7d5fc-6874-443c-98aa-71f486b0a2c2, a7f4d743-39d5-4854-aaa4-ddf11f070768, fa998230-fc31-4906-9a19-50f95d7dbd97, 1001e6bd-b6d4-4481-9c34-695a039d3ef8, 79c446d6-7b7b-45eb-9c1e-eedd425465b5, c1d76b2a-2397-4e6e-92fc-2595701a20b3, f14fe255-ba07-4078-b40c-eac88d6b971b, 4324bc78-ed3e-4209-93e4-f3e496579b98, 786c39a8-fe7d-4b09-a391-2a2536822418, bd387fab-0884-4f02-9219-bd37589ba374, 09c6b6f8-6d50-4928-aff2-f5dd634c1669, 172b7dd5-8f15-4464-89a7-3d1f3d0acdc0, 5bd1bd9e-9579-4697-84b7-985c943a631f, 35243e5b-b5d6-4f67-ba30-9afa10c2a957, 97709688-46f2-4421-be7b-85a501eb1a83, 079fa229-84ba-44f6-81a0-faa73b3c5593, 375c3f11-0d16-49fa-b874-758e82c61e53, c87163e8-3b70-47e6-9a32-6066e9445edb, 9f7d67a9-028f-4e46-aab6-34e305942f2a, 9ee0c8b7-7ff5-4138-ae11-f9d5617182b8]
---

# aw-autoskill-architecture-legacy-component — RESOLVED 2026-08-20, deleted for real this time

**Status: fixed.** On 2026-08-20 the aw-autoskill run that produced this
update called `aw__architecture__delete_component slug="agents-platform-legacy"
cascade=true` directly (component had zero requirements/connections/tools —
safe to delete outright) and confirmed via `get_component` that the slug now
404s. If you're reading this because "Architecture Test Discovery" is still
failing on this exact slug, the row came back — see "If it resurfaces"
below before doing anything else.

**Why the fix took 21 sessions to actually land**, in case this pattern
repeats for a different component: every one of the 21 prior sessions
(12 original + 9 more before this fix) was a *read-only triage* spawned off
a scheduled task's non-zero exit code. The skill told them the one-line MCP
fix to run, but a triage/investigation agent scoped read-only structurally
cannot call `aw__architecture__delete_component` — so the diagnosis repeated
21 times and the fix landed only when an agent with MCP write access (this
aw-autoskill run) read the skill and had the tools to act on its own advice.
**Lesson for future skills of this shape:** if the prescribed fix is a
specific write call, and the sessions hitting the skill are consistently
read-only-scoped, that's not solved by writing a better skill — flag it back
to whoever schedules the triage task, or have aw-autoskill itself apply
mechanical, already-vetted MCP fixes like this one when it has write access
and the fix has been independently confirmed safe (no requirements /
connections / tools attached) across many prior sessions.

## If it resurfaces

The row was deleted with `aw__architecture__delete_component
slug="agents-platform-legacy" cascade=true`. If "Architecture Test Discovery"
errors on this exact slug again, something re-created the row (e.g. a
`scan_workspace`/`scan_component` pass that re-adds it from some source other
than a `repos/` directory listing — the directory still won't exist, so
don't assume a scan re-created it correctly). Steps:

1. `aw__architecture__get_component slug="agents-platform-legacy"` — confirm
   it's back and check `edited_by` for what recreated it.
2. Confirm `/opt/aw-workspace/repos/agents-platform-legacy` still doesn't
   exist (`ls -d`) and still has no `git log --all` rename trail — one quick
   check, not the full 5-command investigation prior sessions ran.
3. If confirmed still orphaned and still zero requirements/connections/tools,
   delete it again the same way. If something now references it, use
   `aw__architecture__update_component slug="agents-platform-legacy"
   test_base_path=""` instead to stop discovery from erroring without
   destroying the row.
4. If it keeps coming back, the recreation source is the actual bug — find
   what's calling `create_component`/`sync_component` for this slug (likely
   `scan_workspace`) and stop it there instead of deleting the symptom
   forever.

## Why this matters beyond this one component

The pattern, not just the instance: when a *scheduled* task's failure output
is what spawns a fresh investigation session each time, and that session is
scoped read-only, the finding never persists anywhere the next run can see —
so the same tool-call sequence repeats indefinitely at real token cost. If a
future triage session is scoped read-only "for safety" but the diagnosis is
this settled, push back and ask whether it should instead be allowed to
apply the one-line MCP fix that ends the recurrence.
