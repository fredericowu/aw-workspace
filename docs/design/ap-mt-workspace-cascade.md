# AP-MT: make `Run.workspace` resolution as unambiguous as `tenant_id` — explicit cascade at the edges, NOT a ContextVar

Kanban: 4025bf3b-9510-815c-bfca-e4f6d455a53c (design) · follows
bug:ap-mt-runner-workspace-stopgap (coder-sonnet, same Target) and commit
`dc83c70` (the 5-tier inheritance chain + loud ambiguity refusal).
Frederico's framing (2026-10-23, PT-BR): *"a workspace deveria ser
cascateada, o chamador sabe, pode passar pro chamado"* — the caller knows
its workspace; it should pass it to the callee, not leave the callee to
infer it after the fact.

## 1. The decision

**Do not port the tenant ContextVar mechanism to workspace.** Instead:

1. **Cascade explicitly at every edge that demonstrably knows its
   workspace** — above all the workspace-resident MCP dispatch tools in
   `aw-app-agents-platform-runners`, which statically know their own slug
   and today forward nothing. Injection is server-side (like
   `caller_run_id`), never an LLM-settable tool parameter.
2. **Close the three dispatch paths that still drop workspace on the
   floor**: workflow runs, parallel fan-outs, and workflow step dispatch.
3. **Extract the hand-written 5-tier chain into one shared, named,
   logged resolver** reused by the agent path and the monitor path, so
   there is exactly one copy and every resolution says which tier
   answered.
4. **Put the already-shipped "ambiguous workspace" refusal behind an
   `AGENTS_WORKSPACE_ENFORCEMENT` off/warn/strict lever** (defaulting to
   `strict`, i.e. today's shipped behavior) so the next production
   misfire is an env-var flip, not a revert — the exact shape
   `AGENTS_TENANT_ENFORCEMENT`'s rollout proved out.

The DB `Run.workspace` column — not an in-memory ContextVar — remains the
ambient carrier for callers that can only inherit. It already survives
everything a ContextVar cannot (see §3.1).

## 2. Why tenant and workspace are genuinely different (the §3 question in the card)

The tenant pattern is three pieces:
- bind once at the gate: `core/identity.py:307` (`require_tenant`) /
  `core/identity.py:394` (`require_tenant_or_service`) set
  `core/tenant_ctx.py:40`'s `_current_tenant` ContextVar +
  `Session.info["tenant_id"]`;
- stamp writes automatically: `core/tenant_filter.py:209`
  (`_stamp_tenant_on_insert`);
- filter reads automatically, with an off/warn/strict lever.

It works because **tenant is a per-request identity fact, re-derivable at
every gate from the caller's JWT/API key inside one process**. Workspace
fails every one of those preconditions:

1. **Not derivable from identity.** A tenant has N workspaces and the
   choice is the caller's, per dispatch — `require_tenant` resolves
   `tenant_id` from the identity row; there is nothing on an `Identity`
   that names a workspace. There is no gate at which AP-MT could bind a
   workspace ContextVar from what the request itself proves.
2. **The chain crosses processes.** A flow hop is: agent CLI container →
   `aw-app-agents-platform-runners/mcp_server.py` (another container, on
   the workspace's machine) → AP-MT REST → executor → remote Runner
   `/execute`. A ContextVar dies at the first hop. The `tenant_id`
   equivalent survives because each process re-derives it from
   credentials; workspace has no credential to re-derive from — it must
   be *carried*, which is exactly Frederico's cascade.
3. **WORKERS=N.** AP-MT runs multi-worker; this workspace has a whole
   memory index of per-worker ambient state breaking silently. The Run
   row is shared state; a ContextVar is per-process, per-task.
4. **The 462-call-site problem doesn't exist here.** tenant_filter.py's
   docstring motivates the hook with 462 ORM call sites that could each
   forget. Workspace resolution already lives in exactly one function
   per dispatch kind (`executor.py:1786-1838`, plus a 2-tier copy in
   `monitor_run.py:111-115`). The remaining bug class is *edges that
   don't speak*, not *call sites that forget to filter* — a write-stamp
   hook would need an ambient value to stamp from, and per (1)-(3) there
   is no trustworthy ambient value to begin with.

So: **explicit-and-loud at the edges that know; row-inheritance (the
shipped chain) as the ambient fallback for the edges that only inherit;
strict refusal, behind a lever, when both come up empty.** Both halves
the card asked about (§4) are needed; neither alone closes the gap QA
found.

## 3. Where it lands

### 3.1 The carrier stays the Run row (no new mechanism)

`Run.workspace` already: survives process restarts and WORKERS=N, crosses
the runner↔AP-MT process boundary (it's read back by `caller_run_id` /
`parent_run_id` lookups), feeds retry (`executor.py:1893-1897` backfills
pre-created rows for exactly this reason), and is what
`monitor_run.py:111-115` and tiers 2–4 of the chain read today. Nothing
new to build; this section exists so nobody "improves" it into memory.

### 3.2 `aw-app-agents-platform-runners` — the caller that knows, finally says so

`mcp_server.py` dispatch handlers gain one shared helper (mirror the
`_caller_run_id` pattern — resolved server-side, **not** added to any
LLM-facing `inputSchema`):

```python
def _own_workspace() -> str:
    # same source kanban_dispatch.py:230-235 and runner_registration use
    return platform_base.workspace_env("AW_WORKSPACE") or "aw"
```

and every dispatch POST sets `body.setdefault("workspace", _own_workspace())`:

| handler | today | change |
|---|---|---|
| `run_agent_async` (`mcp_server.py:2403-2439`) | forwards caller_run_id, call_me_back; **no workspace** | add `body["workspace"]` |
| `run_workflow_async` (`:2440-2458`) | same | same |
| `run_monitor_async` (`:2461-2489`) / `run_monitor_sync` (`:2490-2513`) | same | same (needs §3.3 server change) |
| `run_agents_parallel` (`:2834-2855`) | `body = dict(args)` wholesale | same (needs §3.4) |

`kanban_dispatch.py:251-255` already does this on the agent-run path and
its comment ("workspace is only accepted on the agent-run path") becomes
stale — update it; its ready-sweep dispatch should pass workspace on
whatever path it posts to once the server accepts it.

This is the heart of the card: every dispatch that transits a workspace's
own runner app is, by construction, *from* that workspace, so the hop is
now explicit at creation time — tier 1 of the chain, never inference.

### 3.3 AP-MT monitor path — accept the explicit value

- `api/monitor.py:14` `MonitorRunInput` gains
  `workspace: str | None = None` (same comment style as
  `schemas.py:335`'s `RunInput.workspace`).
- `api/monitor.py:31` `run_monitor` passes it to `dispatch_monitor_run`.
- `core/monitor_run.py:111-115`: explicit param wins; the parent-row
  derivation becomes the fallback — i.e. replace the inline 2-tier logic
  with the shared resolver (§3.5).

### 3.4 AP-MT workflow + parallel paths — stop dropping it

- `api/workflows.py:331-333`: `run_workflow_ep` receives `RunInput` (which
  already has `.workspace`, `schemas.py:335`) **and silently drops it** —
  pass it through; `start_workflow_run_bg` (`executor.py:3445-3451`) gains
  a `workspace` param, recorded on the workflow-root `Run` row
  (`executor.py:3463-3476`), with parent-row inheritance via the resolver
  when the caller omitted it (a flow hop into a workflow keeps its
  workspace).
- `core/orchestrators/__init__.py:103`: step dispatch is
  `run_agent(agent_slug, payload, node_id=nid)` — **no parent_run_id, no
  session_id, no workspace**, so ALL of tiers 1–4 miss and a workflow step
  lands on `Agent.workspace`-or-NULL today. Thread the workflow-root run's
  workspace (and, while there, its run id as `parent_run_id` — that also
  repairs target/card inheritance for steps, but if the coder finds
  parent linkage has wider side effects, workspace alone is the required
  minimum of this card).
- `api/runs.py:293-305`: `_ParallelDispatch` gains
  `workspace: str | None = None`; the ephemeral parent row and/or each
  `start_agent_run_bg(...)` child call at `api/runs.py:616` threads it
  (children already pass `parent_run_id`? — verify; if they do, stamping
  the ephemeral parent row suffices via tier 3).

### 3.5 One resolver, not two copies — `core/workspace_resolution.py`

Extract `executor.py:1811-1836` (the 5 tiers, verbatim semantics, order
preserved) into:

```python
def resolve_workspace(s, *, explicit, run_id, parent_run_id,
                      session_id, agent) -> tuple[str | None, str]:
    # returns (workspace, tier) — tier in {"explicit","own-row","parent",
    # "session-prev","agent-default","unresolved"}
```

Callers: `_run_agent_impl` (keeping the call **after**
`_agent_to_runtime`, per the params-clobber comment at
`executor.py:1832-1835`) and `dispatch_monitor_run` (replacing
`monitor_run.py:111-115`, with `explicit=` wired from §3.3). Log one INFO
line per resolution naming the tier — the next 401 forensics should read
`workspace=aw via tier=session-prev` instead of re-deriving the chain by
hand. Must run inside a tenant-bound context (both callers already are —
wakeups bind per-row `tenant_scope` since commit `7543648`); the tier-4
query touches `Run`, which is tenant-filtered under strict.

### 3.6 The lever — `AGENTS_WORKSPACE_ENFORCEMENT`

New `config.Settings.workspace_enforcement` ∈ `off|warn|strict`,
**default `strict`** (code default, not env-presence — per the
allowlist-gated-features lesson), read at the two dispatch chokepoints:

- **monitor**: `monitor_run.py:216` `_resolve_runner_dispatch` /
  `:408-411` — `strict` = today's shipped refusal; `warn` = log
  `WORKSPACE-AMBIGUOUS <tenant> <candidates> <run_id>` and pick the
  sorted-first candidate (deterministic, unlike the pre-dc83c70 arbitrary
  row); `off` = pre-dc83c70 behavior, the pure-rollback setting.
- **agent runs**: after the resolver in `_run_agent_impl`, when
  `tier == "unresolved"` and `runtime["provider"] == "runner"` and the
  tenant has >1 registered Runner workspace — same three modes. Today
  this case doesn't refuse at all; it silently rides whatever stale
  `params["runner"]` the agent config carries (see the
  `_apply_workspace_override` docstring, `executor.py:718-757`). `strict`
  makes the agent path as loud as the monitor path.

Why default `strict` and not a warn-first rollout like tenant's: strict
*already shipped* for the monitor path in dc83c70 and the stopgap card
(static `Agent.workspace` on the flow agents) plus §3.2's cascade close
the known ways to reach it. Defaulting to `warn` now would be a silent
regression of shipped protection. The lever exists so the *next*
unforeseen unresolved path is a 30-second env flip to `warn` (with the
greppable log line pointing at the exact edge to fix) instead of a
revert — dc83c70's own first production block (run `4e3920e9…`, ~10 min
post-deploy) is precisely the incident shape this absorbs.

### 3.7 Untouched

- Telegram (`api/telegram.py:2542/5856` `bot.workspace`, `:793` ask_human
  resume) and Watch/Glasses (`RunSyncInput.workspace`,
  `api/agents.py:777-789`) — already explicit tier-1 callers; they are the
  model, not the patient.
- The 5-tier order and semantics, `_apply_workspace_override`
  (`executor.py:718-757`), the `Run.workspace` column, wakeups' reliance
  on tier 4 (`executor.py:1795-1804`) — all unchanged.
- `Agent.workspace` static defaults (stopgap card) — stays as tier 5.

## 4. Rejected alternatives (and why they'll be re-proposed)

1. **`workspace` ContextVar bound at `require_tenant`** — the obvious
   mirror, rejected for the four reasons in §2. The one-sentence version
   for the next proposer: *tenant is re-derivable from credentials at
   every gate; workspace must be carried across process boundaries, and
   the Run row already carries it.*
2. **SQLAlchemy insert-hook stamping (mirror `_stamp_tenant_on_insert`)**
   — stamping requires a trustworthy ambient value; per (1) there is
   none. Also only `Run` needs it, and `Run` creation happens in ~4
   places all inside the executor — a global hook is machinery without a
   problem.
3. **Make `workspace` REQUIRED (fail at the call site if omitted) on
   RunInput / the MCP tools** — the maximal reading of "explicit and
   loud". Rejected: it breaks every legitimate inherit-only caller (SPA
   Quick-test, wakeup resumes, aw-backend), and an LLM-facing required
   param invites hallucinated workspace slugs — `_apply_workspace_override`
   deliberately hard-fails on an unknown workspace
   (`executor.py:739-742`), so a hallucination is a new outage class.
   Server-side injection gives the same guarantee at the edges that
   actually know, without trusting the model. This is the answer to the
   card's "explicit instead of ambient?" — explicit *where the caller is
   a program*, never *where the caller is a model*.
4. **Bind workspace at session/channel creation (a `CliSession.workspace`
   column)** — the runner-up, and the card's own suggested alternative
   binding point. Rejected because workspace is per-*dispatch*, not
   per-session: `RunSyncInput.workspace`'s comment
   (`api/agents.py:777-781`) documents the same watch agent answering
   "out of workspace A today and B tomorrow with no config change",
   resuming sessions across that flip. Pinning at session creation bakes
   in staleness exactly there. Tier 4 (most recent run's workspace) keeps
   per-dispatch freshness with the same inheritance effect. Would change
   my mind: if sessions ever become workspace-exclusive by contract.

## 5. What this makes harder later

- **Cross-workspace orchestration.** Server-side injection means an agent
  on workspace A cannot deliberately dispatch a run onto workspace B's
  runners through its own runner app. Nothing supports that today either,
  but this design entrenches same-workspace-by-default; a future
  multi-workspace feature must add an explicit override field (injection
  must therefore use `setdefault`, never clobber).
- **A third enforcement env var** joins `AGENTS_TENANT_ENFORCEMENT` in
  ops surface area; drift between them (tenant strict / workspace off)
  will read as "enforced" to a skimmer.
- **Workflow-root rows gain `workspace`** — any UI/retro code assuming
  workspace is only populated on `kind=agent` rows.
- **Tier-5 static defaults can mask a broken cascade in tests** (every
  flow agent now has `Agent.workspace='aw'`), which is why the resolver's
  tier log line is load-bearing, not nice-to-have: a healthy system
  should show tier `explicit` dominating; drift toward `agent-default`
  is the regression signal.

## 6. Risks for the Coders

1. **Deploy order: AP-MT before the runners app.** The runners app will
   start POSTing `workspace` to `/api/monitor/run` and `/api/runs/parallel`.
   Pydantic v2's default ignores unknown fields, so old-AP-MT + new-app
   *should* degrade to today's behavior — but verify neither model sets
   `model_config = ConfigDict(extra="forbid")` before relying on that; if
   any does, wrong order 422s every monitor dispatch. Ship AP-MT first
   regardless. Remember the runners app releases via the marketplace bot
   (never hand-bump its version) while AP-MT ships on push to master.
2. **`run_agents_parallel` forwards `body = dict(args)` wholesale**
   (`mcp_server.py:2847`) — the injected key must survive
   `_ParallelDispatch` validation and reach the children; confirm whether
   children get `parent_run_id` (tier 3) or need workspace passed into
   `start_agent_run_bg` per child at `api/runs.py:616`.
3. **Resolver extraction must not move across `_agent_to_runtime`** —
   the override is applied after it on purpose (`executor.py:1832-1835`);
   an "obvious" tidy-up that hoists it gets silently clobbered by
   `params.update(agent.params)`.
4. **Tenant filter interplay.** The resolver queries `Run` (tiers 2–4);
   under `AGENTS_TENANT_ENFORCEMENT=strict` an unbound caller raises.
   Both current callers are bound; any NEW caller of the resolver must
   bind `tenant_scope` first — say so in the resolver's docstring.
5. **The agent-path strict check must count Runner rows, not assume** —
   a single-workspace tenant (every BYOD deployment) must resolve exactly
   as today with zero new refusals; mirror
   `_resolve_runner_dispatch`'s `len(candidates) > 1` guard
   (`monitor_run.py:236-246`) rather than inventing a second census.
6. **Tests that won't catch it:** unit tests run single-process and
   single-workspace, so they can't see the cross-process drop this fixes.
   Add: (a) a resolver test matrix per tier incl. `unresolved` ×
   enforcement mode; (b) a runners-app test asserting every dispatch
   handler's POST body carries `workspace` (the `test_run_dispatch_*`
   files are the pattern); (c) a workflow-hop test: root run created via
   `/run` with `workspace` set → step dispatch resolves the same value.
   Live verification needs the two-workspace tenant on production — the
   tier log line is what makes that observable.

## 7. Suggested landing order (3 independently shippable slices)

1. **AP-MT accept + resolver + lever** (§3.3, §3.4, §3.5, §3.6) — pure
   superset of today's behavior; nothing sends the new fields yet.
2. **Runners-app cascade** (§3.2) — flips the common case to explicit.
3. **Flip nothing**: `strict` is already the default and already live
   for the monitor path; slice 2 is what makes it safe for the agent path
   too. If slice 2 must lag, run slice 1 with the agent-path check at
   `warn` and flip to `strict` in config after slice 2 deploys.
