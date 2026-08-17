---
name: aw-autoskill-enable-migrated-task
description: Diagnose and fix a monolith-migrated aw-app-tasks scheduled task (agent_prompt type) that stays disabled or never actually fires — check whether its agent_slug exists in Agents Platform first, and whether the target skill sends its Telegram report, before chasing any other cause.
auto_generated: true
generated_at: 2026-08-17T08:19:11Z
evidence_sessions: [685c9f34-aaf7-4f83-b3a1-dc373078d658]
---

# aw-autoskill-enable-migrated-task — enabling a migrated daily-audit task

12 of the agentic-workspace monolith's scheduled tasks were copied into this
workspace's `aw-app-tasks` on 2026-08-12, **all created disabled** (see the
`monolith-tasks-migrated-to-aw` memory). Enabling one is not just flipping
`enabled: true` — two specific, silent failure modes have each hit real
tasks already and will keep hitting the remaining ones:

## 1. The task's `agent_slug` may not exist in Agents Platform yet

A task of type `agent_prompt` with `agent_slug: kb-curator` will sit disabled
(or enabled-but-never-run) with **no error anywhere** if no Agents Platform
agent named `kb-curator` was ever created — the monolith had one, this
workspace's Agents Platform does not inherit it. `aw-system-analyst`'s task
happened to already have its agent registered; `aw-kb-curator`'s did not,
and the task had clearly never fired (zero runs) for exactly this reason.

**Before enabling any migrated task, check first:**

```
list_agents  # or get_agent with the exact slug
```

If the slug is missing, create it by mirroring the config of an existing,
working sibling (e.g. `system-analyst`) — same model tier, same tool
allowlist shape, system prompt adapted to the new skill's contract — via
`create_agent`. Don't invent a new pattern; copy the one that's already
proven to run cleanly.

## 2. The target skill may produce a report but never send it

Both `aw-system-analyst`'s and `aw-kb-curator`'s `SKILL.md` had a "publish
findings" step but no step that actually notified anyone — the audit ran,
opened Kanban cards, published a presentation, and Frederico saw nothing in
Telegram. This is now fixed in both skills (each app's own repo), but any
other migrated daily-task skill you enable should be checked for the same
gap before you consider it done. The known-good pattern (already used by
`aw-system-analyst`, `aw-kb-curator`, and this very `aw-autoskill` skill):

```bash
curl -s -m 15 -X POST http://172.18.0.1:10014/api/telegram/report \
  -H 'Content-Type: application/json' \
  -d "$(python3 -c '
import json, sys
print(json.dumps({"title": sys.argv[1], "text": sys.argv[2]}))
' "<Skill Name> — $(date +%Y-%m-%d)" "$YOUR_SUMMARY_TEXT")"
```

The payload fields are **`title` and `text`, not `summary`** — the endpoint
silently accepts unknown field names and just sends a blank body, so this
class of bug does not error, it just produces silence. This edit belongs in
the skill's own repo (check `.aw-app-id` in its materialized `skills/<name>/`
copy to find which repo owns it) — not in `native-skills/`.

## 3. Verify the fix live, not just by HTTP status

`{"ok": true, "sent": N}` from the report endpoint is necessary but not
sufficient proof a human will see it — confirm with Playwright against the
actual Telegram web client (the bot chat, e.g. WS-main) that the message
landed, the same way both fixes above were verified in the evidence session.

## 4. When polling a task run's status, don't trust a single empty response

A polling loop that reads `run['status']` from the tasks API can get one
transient empty/malformed HTTP response mid-run and misread it as "done" —
this produced a false "finished" report while the run was still in progress
in the evidence session. Retry on empty/unparseable response instead of
treating it as a terminal state; only trust a `status` field that actually
parsed.

## What this does NOT cover

- The 11 `terminal`-type migrated tasks are separately, structurally broken
  (unset `terminals_api_base` + no auth header) — see
  `tasks-terminal-type-structurally-broken` memory. That's a different bug
  class from the `agent_prompt` one this skill addresses.
