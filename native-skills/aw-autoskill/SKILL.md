---
name: aw-autoskill
description: Analyze recent Claude Code sessions in this workspace to discover skill-creation opportunities and automatically create or update aw-autoskill-<name> skills. Run when tasked with finding skill opportunities, or when the "aw-autoskill daily" scheduled task fires.
---

# aw-autoskill — Automatic Skill Discovery & Maintenance

This skill scans recent Claude Code sessions run against this workspace,
identifies recurring patterns that would benefit from a dedicated skill, and
creates or updates `aw-autoskill-<name>` skills under `native-skills/`.

State is tracked in `.aw-workspace/data/aw-autoskill/aw-autoskill.json` so
each run only analyzes the delta since the last run.

Ported from the agentic-workspace monolith's own `aw-autoskill` skill. Two
differences from that version, both load-bearing:

1. **No separate Telegram mining.** The monolith queried an awserv Postgres
   table because Telegram messages there lived outside any Claude Code
   session. In this workspace every Telegram turn (via `aw-agent-telegram`)
   *is* its own Claude Code CLI session under
   `~/.claude/projects/-opt-aw-workspace/`, so it's already covered by the
   session scan below — there's nothing extra to query. If you ever need one
   specific caller's raw message history for some other reason, use the
   `list_callers` (source="telegram") + `list_caller_messages`
   agents-platform-runners MCP tools, not direct DB access — this agent's
   container has no DB credentials and likely can't reach the DB host anyway.

2. **New skills land in `native-skills/`, not `skills/`.** `skills/` in this
   workspace is a generated, gitignored mirror (materialized by
   `aw-workspace-cli agent sync` from `native-skills/` + each installed
   app's `contributes.skills`) — writing there directly is invisible to the
   next session and gets silently overwritten on the next sync. Every new or
   updated skill this agent produces must go under `native-skills/<name>/`,
   be committed and pushed to the `aw-workspace` repo, and only then synced.

---

## Step 1 — Compile session data

Run the bundled script to get the session differential:

```bash
cd /opt/aw-workspace
python3 native-skills/aw-autoskill/compile_sessions.py
```

Options:
- `--all` — ignore last_run, analyze ALL sessions (use on first run or to re-scan)
- `--max-sessions N` — cap the number of sessions analyzed (default: 15)

No venv activation needed — the script only uses the standard library, and
this workspace's system `python3` (3.12) is enough.

The script outputs JSON with `sessions`: Claude Code JSONL sessions with
messages, recurring bash commands, and user corrections.

If `session_count` is 0 (nothing new since `last_run`), that's a legitimate
clean result — skip straight to Step 4, write a run record noting zero new
sessions, and send the Telegram report saying so. Do not treat it as a
failure or retry with `--all`.

---

## Step 2 — Analyze for skill opportunities

Read the compiled JSON and look for:

1. **Recurring tool sequences** — the same 3-5 tool calls appear across multiple sessions to accomplish a similar goal (e.g. always searching KB + reading a specific file + editing it). A skill could shortcut this.

2. **Re-explained context** — the user restates the same background or constraint in multiple sessions. A skill (or memory) should capture it so it's pre-loaded.

3. **Friction patterns** — the agent had to try multiple approaches for the same class of problem. A skill with the correct approach would eliminate the retries.

4. **Missing tool knowledge** — agent used a workaround (e.g. Bash fallback) because it didn't know an MCP tool existed. A skill should document the right tool.

5. **New workflows** — a sequence of steps the user walked through that would be reusable (e.g. "deploy X", "add Y to the platform").

For each opportunity, decide:
- Is it truly reusable (not one-off)?
- Does a skill already exist for it? (`ls native-skills/` + check existing app-contributed skill descriptions via `search_skills`)
- Would it be `aw-autoskill-<name>` (auto-generated) or an update to an existing skill?

---

## Step 3 — Create or update skills

### Creating a new skill

```
native-skills/aw-autoskill-<name>/SKILL.md
```

Frontmatter:
```yaml
---
name: aw-autoskill-<name>
description: <one-line, specific trigger description>
auto_generated: true
generated_at: <ISO timestamp>
evidence_sessions: [<session_id>, ...]
---
```

Content: clear instructions for the agent — what to do, which tools to use, what to avoid. No vague prose. One concrete example minimum.

### Updating an existing skill

Only edit skills under `native-skills/` — if the skill you want to update is
app-contributed (check for a `.aw-app-id` marker in its materialized
`skills/<name>/` copy), it isn't yours to edit; note the gap in your report
instead. Edit the SKILL.md to add the new knowledge. Add a `last_updated`
field to the frontmatter and note what changed.

### Commit and push

This repo (`aw-workspace`) is Frederico's own — push straight to `master`,
no feature branch or PR needed:

```bash
cd /opt/aw-workspace
git add native-skills/aw-autoskill-<name>/
git commit -m "feat(skills): aw-autoskill-<name> — <one line>"
git pull --rebase origin master   # in case another run/agent pushed meanwhile
git push origin master
```

---

## Step 4 — Update aw-autoskill.json

After completing the analysis, write the run record to
`.aw-workspace/data/aw-autoskill/aw-autoskill.json` (create the file/dirs if
they don't exist yet):

```json
{
  "last_run": "<ISO timestamp of NOW>",
  "history": [
    {
      "run_at": "<ISO timestamp>",
      "sessions_analyzed": <N>,
      "skills_created": ["aw-autoskill-foo"],
      "skills_updated": ["aw-autoskill-bar"],
      "opportunities_found": <N>,
      "log": "<one-paragraph summary of what was found and done>"
    },
    ...
  ]
}
```

Always prepend the new entry to `history` (newest first). Keep at most 50 entries.

This state file is workspace-local durable state, not repo content — don't
commit it.

---

## Step 5 — Sync skills

After creating/updating skills, run:

```bash
aw-workspace-cli agent sync
```

This propagates any new/updated `native-skills/` entries to
`skills/`, `.claude/skills/`, `.cursor/skills/`, `.gemini/skills/` so they're
available in the next session. (Not `./aw agent sync` — that binary is a
deprecated stub in this repo, see `AGENTS.md`.)

---

## Step 6 — Send the Telegram report (mandatory, every run)

Even on a run that found zero opportunities, deliver a short summary via the
same deterministic callback every other daily task in this workspace uses —
`kanban_manager.send_report()`, reached over the workspace's docker bridge
gateway, unauthenticated by design (internal-only endpoint):

```bash
curl -s -X POST http://172.18.0.1:10014/api/telegram/report \
  -H "Content-Type: application/json" \
  -d '{
    "title": "aw-autoskill daily",
    "summary": "<2-4 sentences: sessions analyzed, opportunities found, skills created/updated, or \"nothing new since last run\">"
  }'
```

If the curl fails (non-2xx or connection error), say so explicitly in your
final agent output — do not silently retry in a loop, and do not treat a
failed *notification* as a failed *run*: the skill analysis and any skill
writes already happened and are still valid.

---

## Naming conventions

| Pattern | Skill name |
|---|---|
| Auto-generated by this agent | `aw-autoskill-<topic>` |
| Human-authored skill | `aw-<topic>` |

New skills from this agent MUST be prefixed `aw-autoskill-` so they're identifiable as auto-generated and can be audited/pruned separately.

---

## What NOT to create a skill for

- One-off tasks (single occurrence, no pattern)
- Things already covered by an existing skill
- Knowledge that belongs in KB (`update_knowledge_base`) rather than a skill
- User preferences that belong in auto-memory (`~/.claude/projects/-opt-aw-workspace/memory/`)
- Anything specific to a single installed app's own internals — that belongs in the app's own repo, contributed via `contributes.skills`, not `native-skills/`
