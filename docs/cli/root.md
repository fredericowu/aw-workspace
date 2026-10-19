---
source: aw-workspace-cli
type: cli-reference
checksum: sha256:621ea4b70130423c5a809d079b7a3695b2e91a315dccd81966085f1df77eb4b1
captured: 2026-10-19
edited: false
---

# aw-workspace-cli --help

**Como eu uso o `aw-workspace-cli`?** / **How do I use `aw-workspace-cli`?**

List and run every aw-workspace-cli command.

`aw-workspace-cli`

```
aw-workspace-cli
This workspace's own CLI — not to be confused with the monolith's ./aw.

Usage:
  aw-workspace-cli <command> [args...]
  aw-workspace-cli help
  aw-workspace-cli <command> --help   (per-command usage and flags)

Commands:
  agent                     Sync agent-facing state: skills/, AGENTS.md and .mcp.json → per-CLI paths
  agents-platform           Control Agents Platform: telegram bots, agents, runs (and more)
  apps                      List installed apps (add --json for the raw payload)
  architecture              Architecture namespace — components, tests, discovery, docs
  doctor                    Report anything silently degraded (CLIs, permissions, MCP tools)
  folders                   Map any folder into the workspace by name (no git repo needed)
  help                      Show this help message
  kb-reference              Regenerate CLI --help and skills/ content under the knowledge-base tree
  knowledge-base            Manage the knowledge base (build, search, map) — runs inside the kb app
  knowledgeable-exec-sync   execution-history projection sync — run/status/reconcile/retention
  knowledgeable-ingest      KB bulk-ingest driver (§13) — scan/run/status/report
  knowledgeable-sync        fs-sync upsync engine (§3/§7) — run/status/report
  logs                      Show (or follow) a component's logs
  marketplace               Install or update apps from the marketplace catalog
  notion-sync               Mirror Notion notes + Kanban cards into the knowledge base
  remote-hosts              Status/exec/ps on the remote hosts linked to this account (BYOD)
  restart                   Restart a workspace component (app container or managed service), or the core process itself
  rsync                     rsync over ssh with the key/password injected from the workspace vault
  secrets                   read, write and list the workspace's shared secrets
  sideload                  Install an app from a local directory (dev loop, no marketplace)
  ssh                       ssh with the key/password injected from the workspace vault
  start                     Start a workspace component (app container or managed service)
  status                    Show workspace health, components and mapped folders
  stop                      Stop a workspace component (app container or managed service)
  test                      Run this workspace's pytest suite (extra args go to pytest)
  uc-phd-index              Index the Estudo Geral thesis corpus into pgvector (aw-app-uc-phd)
  update                    Update the workspace code or its remote host (aw-console Manage > Update)

Run 'aw-workspace-cli <command> --help' for a command's full usage.
```
