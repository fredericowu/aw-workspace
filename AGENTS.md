# Agent instructions - Multi Agent

> **Source of truth** — always edit **`AGENTS.md`**, never the mirrors.
> `CLAUDE.md`, `GEMINI.md`, and `.github/copilot-instructions.md` are
> auto-generated copies. After any change here run
> `aw-workspace-cli agent sync` to propagate.

> Cross-CLI bootstrap for **Claude Code / Cursor / Codex / Gemini / Copilot**
> running inside the `aw-workspace` container at `/opt/aw-workspace`.
> Read the **`aw-workspace` skill** (`skills/aw-workspace/SKILL.md`) for the
> deep reference — the sections below are the irreducible bits every agent
> needs on every run.

## Knowledge Base — search before acting

**Always call `search_knowledge_base` (via the workspace MCP gateway) at the
start of every non-trivial task and before invoking any tool that touches the
codebase, infrastructure, or project decisions.** This surfaces relevant docs,
lessons learned, architecture notes, and prior decisions that would otherwise
be invisible.

When to search (mandatory):
- Any new user request or task — use the user's message as the query
- Before reading/editing a file you haven't seen this session — query the file
  path or topic first
- Before proposing an architecture or design — query the subject area
- When unsure how something works in this project — query it before guessing

How to search well:
- Prefer short, focused queries (3–8 words) over long ones
- Run 2–3 searches with different angles if the first yields thin results
- If results contain a relevant doc, read it before proceeding

This is not optional. Skipping the KB search is the single biggest cause of
repeated mistakes and re-doing work that was already documented.

The KB is an installed app (`kb`), not core, so the tool name you see depends
on the gateway: `search_knowledge_base` directly, or
`aw__kb__search_knowledge_base` when routed through `aw-mcp-gateway`. If the
`kb` app genuinely isn't installed in this workspace there is nothing to
query — but **verify that before concluding it**, don't assume from a tool
you can't immediately see. A flaky or reconnecting MCP server is not the same
as an absent one; retry once before giving up, and say so in your answer if
you proceeded without the KB.

## Inbound message routing

When the **first user message starts with `/aw-agent-telegram`**, load and
follow the **`aw-agent-telegram` skill** (`skills/aw-agent-telegram/SKILL.md`)
before doing anything else. That skill teaches you how to reply (voice,
photo, document, text), which bracket markers to use, and how to avoid
duplicate bubbles.

## Project skills

Skills are real directories under **`skills/<name>/SKILL.md`** (YAML
frontmatter with `name` + `description`). When a user request matches a
skill's description, or they mention AW orchestration, presentation,
knowledge base, the diff tool, VS Code, scheduled tasks, the app
marketplace, or agent orchestration, **open and follow the matching
`SKILL.md` instead of improvising.**

`skills/` is the source of truth. It holds both this repo's own skills and
the ones installed apps contribute (`contributes.skills` in an `aw-app.json`
is copied here on every app activate — see `src/apps/paths.py::skills_dir`).
Per-agent mirrors live in:

| Mirror | Used by |
|---|---|
| `.claude/skills/` | Claude Code (Copilot also reads from here) |
| `.cursor/skills/` | Cursor |
| `.gemini/skills/` | Gemini CLI |

All three are gitignored and regenerated from `skills/` by
**`aw-workspace-cli agent sync`**.

Sync semantics: **exact mirror** — files removed from `skills/` are deleted
from every per-agent dir. Anything weaker leaves an uninstalled app's skill
behind, teaching agents to call tools that no longer exist.

## MCP servers

**`.mcp.json`** at the repo root is the canonical config, and Claude Code
reads that path natively. `aw-workspace-cli agent sync` fans it out to the
CLIs that want their own location.

Unlike the monolith, `.mcp.json` here is **not generated from a static
config** — installed apps write their own entries into it at boot (today:
`aw-mcp-gateway`, gated behind the high-risk `mcp:register-gateway`
capability; see `_container_volumes` in `src/apps/runtime.py`). Regenerating
it from a checked-in file would delete what the app framework just
registered. Edit `.mcp.json` directly, then sync.

Most tools reach this workspace through **`aw-gateway`** — the
`aw-mcp-gateway` app, which aggregates every installed app's MCP surface
behind one endpoint and prefixes tool names (`aw__kb__…`,
`aw__playwright__…`, `aw__agents_platform_runners__…`). Which tools exist
therefore depends on which apps are installed; `aw-workspace-cli apps` is the
authoritative list.

### Fan-out targets (driven by `aw-workspace-cli agent sync`)

| Agent | Path | Mechanism |
|---|---|---|
| Claude Code | `.mcp.json` (in place) | Read natively — no copy needed. |
| Cursor | `.cursor/mcp.json` | Byte-for-byte copy. Reload MCP / window after change. |
| Codex | `~/.codex/config.toml` | Projected via `codex mcp add/remove/list --json`. |
| Gemini | `.gemini/settings.json` (project scope) | `mcpServers` block written directly — `gemini mcp add` clobbers the whole block. |
| Copilot | (reads `.claude/skills/` for skills, no project MCP today) | — |

For Codex + Gemini, only entries matching `aw-*` / `playwright*` are managed;
user-added MCP servers are preserved across syncs. A CLI that isn't installed
is reported as a skip, not a failure — a slim BYOD image with only Claude
Code present is the normal case.

After editing `.mcp.json`, `AGENTS.md`, or anything under `skills/`:

```bash
aw-workspace-cli agent sync           # propagate
aw-workspace-cli agent sync --check   # CI: report drift, write nothing
aw-workspace-cli agent status         # what goes where
```

## The workspace CLI

**`aw-workspace-cli`** is this workspace's own CLI, on `PATH` from any cwd
(the repo root is on `PATH` — see the Dockerfile). It is **not** the
monolith's `./aw`; the `./aw` file in this repo is a deprecated stub that
only prints a pointer.

```bash
aw-workspace-cli status                     # health + components + mapped folders
aw-workspace-cli apps [<slug>] [--json]
aw-workspace-cli start|stop|restart <app>   # by bare app slug, not docker:aw-<app>
aw-workspace-cli logs <app> [-f]
aw-workspace-cli folders list|add|rm|browse # map ANY folder — see below
aw-workspace-cli agent sync
aw-workspace-cli marketplace install <app> [--update]
aw-workspace-cli test [pytest args...]
```

Commands are auto-discovered from `src/cli/commands/*.py` **and** from each
installed app's `commands/` dir, so an app ships its own CLI surface with no
change to this repo.

## Mapped folders — point at any directory

`aw-workspace-cli folders add /absolute/path` registers a folder with the
workspace by name. It does **not** need to be a git checkout and does not
need to live under `repos/`. Apps that declare a `$AW_WORKSPACE_FOLDERS`
volume receive every mapped folder bind-mounted at `<target>/<name>`, and
mapping or unmapping one re-mounts it into those already-running containers.

Also available in the UI at **Workspace › Folders**, and over
`/api/folders`. See `src/api/folders.py`.

Note the cost: a folder change **recreates** the container of every app that
declared the volume, because binds are fixed at container creation. Adding
five folders one at a time is five recreations — map them in one sitting.

## Temporary / scratch files → `.tmp/`

**Always write temporary files to `.tmp/<scope>/...` unless a more specific
location is required.** `.tmp/` at the workspace root is the canonical
scratch dir.

Unlike the monolith, there is **no bind-mount indirection here** — `.tmp/`
is a plain directory inside the workspace tree, which is itself host-mounted
(see `AW_WORKSPACE_HOME` in the Dockerfile), so it survives container
recreation and is visible from the host at the same relative path. A sibling
agent-runner container that mounts `/opt/aw-workspace` sees exactly the same
files, which makes `.tmp/` the reliable way to hand a file between the
workspace and an agent session.

Do **not** write to `/tmp` for anything that should outlive the process
(`/tmp` is process-scratch only — it vanishes on container restart and is
invisible to every other container). Use `/tmp` only for true single-process
scratch, e.g. an output file you read back and discard immediately.

Durable state — the things that must survive an app reinstall — belongs
under **`AW_WORKSPACE_HOME`** (`/opt/aw-workspace/.aw-workspace`) instead:
`bin/` for command shims, `secrets/` for the secret store, `data/<app_id>/`
for per-app storage, `knowledge_base/` for the KB's indexed tree. See
`src/apps/paths.py`.

Conventions:

- One file per logical artifact: `.tmp/<scope>.json` or `.tmp/<scope>/<item>`.
- Use absolute paths in code, resolved from
  `AW_WORKSPACE_CONTAINER_DIR` (default `/opt/aw-workspace`) rather than a
  relative path — the CLI, the server and app containers all run from
  different cwds.
- If you create a new convention, document it in
  `skills/aw-workspace/SKILL.md` so the next agent finds it.
