---
source: aw-workspace-cli agent
type: cli-reference
checksum: sha256:f2ca8ac674d8a7fb28e7f1e0c020c0323917ea43ec01bad0bbd78b7c6581da36
captured: 2026-10-11
edited: false
---

# aw-workspace-cli agent --help

**Como eu sincronizo os agentes (skills, AGENTS.md e MCP) na workspace?** / **How do I sync agents (skills, AGENTS.md, MCP) in the workspace?**

Sync agent-facing state: skills/, AGENTS.md and .mcp.json → per-CLI paths

`aw-workspace-cli agent`

```
usage: aw-workspace-cli agent [-h] {sync,status} ...

Sync agent-facing state: skills/, AGENTS.md and .mcp.json → per-CLI paths

positional arguments:
  {sync,status}
    sync         Run the unified sync (skills + AGENTS.md + MCP)
    status       Show the source files and their fan-out targets

options:
  -h, --help     show this help message and exit
```
