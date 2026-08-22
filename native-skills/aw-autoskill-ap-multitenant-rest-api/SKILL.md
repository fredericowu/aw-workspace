---
name: aw-autoskill-ap-multitenant-rest-api
description: Inspecting agents-platform-multitenant's agent-groups or agent-flows data directly (not just individual agents) — the aw-agents-platform-runners MCP has no list_agent_groups/list_agent_flows tools, so you have to hit the raw REST API with a Bearer token pulled from app-config. Use this instead of re-deriving the auth dance from scratch.
auto_generated: true
generated_at: 2026-08-22T03:04:41Z
evidence_sessions: [582f6d55-d9f0-4e4e-8fa3-c4ac17aa6afb, 619487e8-8a8b-44ce-b07f-bfb6911e0ab2, 1e01c6ff-a31d-4b48-8c01-b609fc9be0c4, 729cbe56-59af-43c4-9785-2cc1ebc82c01]
---

# Calling agents-platform-multitenant's raw REST API

The `aw-agents-platform-runners` MCP surface covers individual agents
(`list_agents`, `get_agent`, ...) and workflows, but has **no tool for
agent-groups or agent-flows** — the raw graph/group data (`kanban_target_status`,
`hidden_from_flow`, flow `graph.nodes`/`graph.edges`, group `slug`/`name`/
`description`) is only reachable by calling the backend's REST API directly.
One session reconstructed this exact curl+auth incantation **34 times** because
each `Bash` tool call is a fresh shell with no persisted variables — don't
redo that from scratch.

## Auth

The token lives in this app's own config, not a secret store:

```bash
cd /opt/aw-workspace
T=$(jq -r .agents_platform_token .aw-workspace/app-config/agents-platform-runners.json)
B=http://172.18.0.1:10014   # NOT 127.0.0.1 (that's the agent container itself), NOT :10005 (retired)
```

If a call unexpectedly 401s, check the token isn't stale before assuming the
endpoint is wrong — decode the JWT and check its expiry:

```bash
python3 -c "
import base64,json,time
p='$T'.split('.')[1]; p+='='*(-len(p)%4)
d=json.loads(base64.urlsafe_b64decode(p)); print(d)
print('exp in', d.get('exp',0)-time.time(), 'seconds')
"
```

(Seeded configs can carry a dead token — see the
`seeded-agent-configs-carry-a-dead-gateway-token` memory if this comes up
for the MCP gateway token specifically; this is the *separate*
`agents_platform_token` used for direct REST calls.)

## The endpoints the MCP surface doesn't cover

```bash
mkdir -p .tmp/ap-rest
for p in agents agent-groups agent-flows; do
  curl -s -m 20 -H "Authorization: Bearer $T" "$B/api/$p" -o .tmp/ap-rest/$p.json
done

echo "=== GROUPS ==="
jq -r '.[] | "\(.slug) | \(.name) | kanban=\(.kanban_target_status//"-")"' .tmp/ap-rest/agent-groups.json

echo "=== FLOWS ==="
jq -r '.[] | "\(.slug) | enabled=\(.enabled) | max_hops=\(.max_hops//"-")"' .tmp/ap-rest/agent-flows.json
jq -r '.[0].graph.nodes[]? | "\(.id) | type=\(.type//"-") | agent=\(.agent_slug//.agent//"-")"' .tmp/ap-rest/agent-flows.json
jq -r '.[0].graph.edges[]? | "\(.source) -> \(.target)"' .tmp/ap-rest/agent-flows.json
```

## When to use this vs. the MCP tools

Prefer the MCP tools (`list_agents`, `get_agent`, `list_workflows`, ...)
whenever the data you need is agent- or workflow-scoped — they're already
authenticated and don't need this dance. Reach for the raw REST calls above
only for **group** or **flow-graph** data, since nothing in the MCP surface
exposes it.
