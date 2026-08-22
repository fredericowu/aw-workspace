---
name: aw-autoskill-compile-sessions-path
description: aw-autoskill's own bootstrap command (`python3 native-skills/aw-autoskill/compile_sessions.py`) 404s because the aw-autoskill skill itself migrated to being owned by the aw-app-maintenance-agents app. Use `skills/aw-autoskill/compile_sessions.py` (the materialized mirror) instead — don't re-run `find / -name compile_sessions.py` to rediscover this.
auto_generated: true
generated_at: 2026-08-22T08:30:00Z
evidence_sessions: [326dbc5f-e5f7-4fbd-bf33-9ab61edf7873, c4e62ecb-a419-445d-a696-ab2dc9c91f52]
---

# aw-autoskill's Step 1 command points at a path that no longer exists

`AGENTS.md` / the injected `[skill:aw-autoskill]` system prompt tells every
aw-autoskill run to start with:

```bash
cd /opt/aw-workspace
python3 native-skills/aw-autoskill/compile_sessions.py
```

That directory hasn't existed since the skill was ported from the
`agentic-workspace` monolith to the `aw-app-maintenance-agents` app (see
`repos/aw-app-maintenance-agents/skills/aw-autoskill/`, marked with
`.aw-app-id` in the materialized `skills/aw-autoskill/` mirror). This has
independently confused **3 separate aw-autoskill sessions** now (an aborted
run, the prior full run that shipped `aw-autoskill-ap-multitenant-rest-api`,
and the run that wrote this skill), each burning 4-6 tool calls (`ls`,
`find / -name compile_sessions.py`, `cat .aw-app-id`, `Read SKILL.md`) to
rediscover the same thing.

**Just run it from the materialized mirror instead — skip the rediscovery:**

```bash
cd /opt/aw-workspace
python3 skills/aw-autoskill/compile_sessions.py
```

This is the same file (`skills/` is synced verbatim from the app's
`contributes.skills` by `aw-workspace-cli agent sync`), so behavior is
identical — `--all`, `--max-sessions N`, and the `last_run` state file are
all unaffected.

## The actual bug, and why this skill doesn't just fix it

The `native-skills/aw-autoskill/compile_sessions.py` path is hard-coded at
`repos/aw-app-maintenance-agents/skills/aw-autoskill/SKILL.md:45` — a real
bug in that file, one line, trivial to fix. But per this workspace's own
skill-ownership rule, **aw-autoskill is not native to this repo anymore** —
it's app-contributed, so `native-skills/` is not the place to patch it, and
this agent's mandate (per its own skill) is explicitly "if the skill you
want to update is app-contributed, it isn't yours to edit; note the gap in
your report instead." That's what this skill is: the noted gap, plus the
workaround, so the next run doesn't re-pay the discovery cost while the real
fix waits on someone touching the `aw-app-maintenance-agents` repo directly.

If you *are* working in `repos/aw-app-maintenance-agents` for an unrelated
reason, the one-line fix is: in `skills/aw-autoskill/SKILL.md`, change
`native-skills/aw-autoskill/compile_sessions.py` to
`skills/aw-autoskill/compile_sessions.py` (both occurrences — Step 1's
command block and the "No venv activation needed" note right after it),
then release the app the normal way so the fix reaches the materialized
mirror and this skill can be retired.
