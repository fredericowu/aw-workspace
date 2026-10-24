# AP-MT: `/v1` door workspace binding — the ApiKey is the channel, so the ApiKey carries the workspace

Kanban: 4035bf3b-9510-819f-9fea-e14ac5cb57d4 (AP-MT /v1 500s for every
provider=runner agent) · Architect run 21a5ce7576384280b9263c821eb67cc7 ·
follows `docs/design/ap-mt-workspace-cascade.md` (commit 8c0e14e) and the
Debugger's root cause on the card (run d84893b62c2e4e7e8aa9ac04923eb356).

Repo: `repos/agents-platform-multitenant`, verified at HEAD `beb60f2`
(matches deployed).

## 1. The problem, one paragraph

Commit 8c0e14e made workspace resolution an explicit cascade and turned on a
strict ambiguity refusal, and updated `api/workflows.py` / `api/runs.py` /
`api/monitor.py` to carry workspace through, but missed
`api/openai_compat.py`. `_run` (`openai_compat.py:395-408`) calls
`run_agent(slug, input, target_id=…, one_shot=True)` with no run_id, no
parent_run_id, no session_id, no workspace, so `resolve_workspace`
(`core/workspace_resolution.py:26`) returns `tier="unresolved"`, and
`_check_runner_workspace_ambiguity` (`executor.py:774-813`) finds two
registered Runner workspaces for tenant-bootstrap (`aw`, `fredericowu`) and
raises `WorkspaceAmbiguousError` under the default
`workspace_enforcement="strict"` (`config.py:167`). The exception fires in
`_run_agent_impl`'s setup phase, before the generation loop's own guard
(`executor.py:2491`), so it reaches FastAPI as a bare 500 with no body.

## 2. The decision

**Bind the workspace to the `ApiKey` row that authenticates `/v1`, and thread
it into every dispatch that door makes (agent, workflow, and session_chat) as
the resolver's tier-1 `explicit` value.** This is option (a) from the
Debugger's handoff.

The cascade design's own doctrine decides this: *"The executing workspace is
a property of the inbound CHANNEL, not of the workspace of an account"*
(`models.py:1354`, the TelegramBot.workspace comment). A `/v1` caller is a
bare OpenAI-shaped HTTP client with no notion of workspace, so the channel
identity it does present, the ApiKey, is the only place the binding can
live. `TelegramBot.workspace` and `RunSyncInput.workspace` are the exact
same shape; this makes `/v1` consistent with them instead of inventing a new
mechanism.

Secondary, same change: **setup-phase exceptions escaping into
`openai_compat.py` become declared OpenAI-shaped error responses** (409 for
ambiguity, 404 for not-found races, 502 for anything else), per the card's
step 4. Precedent: aw-knowledgeable's "502 still carries a declared body"
contract.

## 3. Where it lands (all paths real, read at HEAD beb60f2)

### 3.1 `backend/app/models.py` — `ApiKey.workspace`

Add to `ApiKey` (`models.py:2020-2056`):

```python
workspace: Mapped[str | None] = mapped_column(String, nullable=True)
```

nullable, NULL = "unbound, behave exactly as today". Comment should mirror
`TelegramBot.workspace`'s rationale (`models.py:1350-1364`): the key is the
`/v1` channel, the channel names the executing workspace.

### 3.2 `backend/app/db.py` — inline migration

One `_ensure_column(conn, "api_keys", …)` entry in
`_apply_inline_migrations`, following the existing check-then-act pattern
(`db.py:248-253`). Nullable String, no backfill in the migration (see §5 for
why backfill is an operator action, not code).

### 3.3 `backend/app/api/api_keys.py` — mint + rebind

- `ApiKeyIn` (`api_keys.py:23-25`) gains `workspace: str | None = None`;
  `create_api_key` (`:52-57`) passes it through; `_out` (`:31-42`) exposes
  it.
- **Validation on write**: if `workspace` is provided and the tenant has at
  least one `Runner` row with `base_url` set, the named workspace must be in
  that candidate set (the same census `_check_runner_workspace_ambiguity`
  runs, `executor.py:793-798`), else 422 listing the candidates. If the
  tenant has zero registered Runner workspaces, accept as-is (keys can be
  minted before runners register; a runner-less tenant never hits the
  ambiguity check anyway).
- **New `PATCH /api/api-keys/{key_id}`** accepting `{workspace}` only, same
  validation. This is the rebind-without-rotation vehicle: the five live
  consumer keys hold raw tokens in two different vaults, and re-minting
  would mean rotating secrets in every one of them. PATCH binds the existing
  tokens in place.

### 3.4 `backend/app/api/openai_compat.py` — thread it through

- `_resolve_api_key`'s detached copy (`openai_compat.py:74-76`) must copy
  `workspace=row.workspace`. **This is the easiest line to miss and it
  makes the whole fix a silent no-op** (the column would exist, the UI would
  show it, and the door would still dispatch workspace=None). Pin it with a
  test (§6).
- `_run(kind, slug, user_input, prior)` (`:395`) gains a
  `workspace: str | None` parameter and forwards it to **both** branches:
  `run_agent(..., workspace=workspace)` and
  `run_workflow(..., workspace=workspace)` (new executor param, §3.5).
- Call sites thread `api_key.workspace`: `chat_completions` non-stream
  (`:646`) and `_stream` (`:457`), `responses` non-stream (`:779`) and
  `_responses_stream`, and `session_chat`'s direct `run_agent` call
  (`:738-741`).
- `session_chat` note: explicit (tier 1) now wins over session-prev
  (tier 4) for bound keys. That is intended, the key is the channel, but it
  means rebinding a key mid-conversation moves execution to another
  machine where the resumed CLI session file does not exist (cold restart
  of the agent's memory). Acceptable; name it in the PATCH route docstring.

### 3.5 `backend/app/core/executor.py` — `run_workflow` learns `workspace`

`run_workflow` (`executor.py:3004-3012`) has **no workspace parameter at
all** today. Add `workspace: str | None = None` and pass it as `explicit=`
to the `resolve_workspace` call in the `run_id is None` branch
(`executor.py:3040-3042`, currently hardcoded `explicit=None`). Children
then inherit via tier-3 `parent` as designed. This also confirms and closes
the Debugger's unconfirmed workflow-door hypothesis: it is real by code
reading (`/v1`'s `_run` calls `run_workflow` with no run_id/parent, so the
top-level workflow Run row gets `workspace=NULL` and every runner child hits
the ambiguity check itself).

Do NOT touch `_run_agent_impl`, `resolve_workspace`, or
`_check_runner_workspace_ambiguity`. The resolver chain is correct; the door
was the gap. Also do not double-thread `api/workflows.py`'s
`start_workflow_run_bg` path, it pre-creates its row and already carries
workspace since 8c0e14e.

### 3.6 `backend/app/api/openai_compat.py` — declared errors (card step 4)

Small helper returning the OpenAI error shape (not FastAPI's `{"detail"}`,
OpenAI SDKs read `error.message`):

```python
def _openai_error(status, code, message) -> JSONResponse:
    # {"error": {"message": ..., "type": ..., "code": ...}}
```

Wrap the dispatch at the three non-streaming sites (`chat_completions`,
`responses`, `session_chat`):

| exception | status | code |
|---|---|---|
| `WorkspaceAmbiguousError` | 409 | `workspace_ambiguous` (message = str(exc) + "bind a workspace to this API key: PATCH /api/api-keys/{id}") |
| `ValueError` (not-found / soft-deleted race, slug already pre-checked at `:620-627`) | 404 | `model_not_found` |
| any other `Exception` escaping run_agent/run_workflow | 502 | `dispatch_failed` |

The generation loop's own guard (`executor.py:2491`) already converts
in-loop failures into `status="error"` results that `_run` renders as text
(`openai_compat.py:417-419`), so this wrapper only ever sees setup-phase
failures. Leave the streaming paths exactly as they are: `_stream` already
degrades errors to `"[error] …"` content (`:466-468`) because an SSE
response that has started cannot change status code. Document that split in
the route docstring.

### 3.7 `frontend/src/routes/Settings.tsx` — Access Keys panel

The create form (`Settings.tsx:90-113`, `api.createApiKey(name, scope)`)
gains an optional workspace input; the key list shows the binding.
`lib/api.ts`'s `ApiKey` type and `createApiKey` gain the field; add
`patchApiKey`. Keep it a free-text input with the 422 candidates surfacing
on error; a dropdown fed by a runners endpoint is polish, not required.

## 4. What was rejected, and why

- **(b) tenant-level default workspace**: invents a sixth, ambient tier that
  every unresolved dispatch would silently land on. That is precisely the
  wrong-host hazard strict mode exists to refuse (`config.py:150-166`), just
  relocated. It also cannot differ per consumer, while keys can.
- **(c) caller-supplied header/field as the carrier**: the `/v1` caller is
  the least-informed party in the chain (Roblox, LangChain, curl), and it
  would hand workspace choice to whoever holds a leaked key, widening the
  blast radius of a key leak from "can run these slugs" to "can choose which
  machine runs them". Defensible later as an *additive* override that must
  match or narrow the key's binding; not the primary mechanism. This is the
  runner-up.
- **(d) exempt `/v1` from strict enforcement**: re-opens silent
  cross-workspace execution on exactly the door with the least context.
  Defeats 8c0e14e for its highest-volume caller.
- **(e) data-only mitigation, `Agent.workspace="aw"` on the 5 affected
  agents (tier 5)**: would un-500 production today with zero code, but these
  are app-contributed agents and contributed agents are **reconciled from
  the app manifest, not seed-once**, so the next app update can clobber the
  column back to NULL. Tier 5 also cannot distinguish two workspaces of one
  tenant by design (resolver docstring,
  `core/workspace_resolution.py:55-58`). And there is no urgency payoff: the
  PO established there is currently no working LLM transport anyway (OpenAI
  credits empty, Bedrock credential-less), so a brittle stopgap buys
  nothing. Rejected; recorded so nobody re-proposes it as "the quick fix".
- **Blanket migration backfill (`UPDATE api_keys SET workspace='aw' WHERE
  workspace IS NULL`)**: bakes a guess about every tenant's keys into boot
  code. The binding is per-integration knowledge; see §5.

## 5. Restoring the five live consumers (sequencing, part of this card)

Code alone does not un-500 production: the five consumer keys
(knowledgeable-extractor-runner, -playground, -search-planner, -labeller
via vault-held keys; research-theme-convergence via research-search's
self-minted key) all have `workspace=NULL` and their owners re-mint only on
401/403, never on 409.

1. Ship + deploy AP-MT (verify in-container per
   aw-autoskill-verify-deploy-in-container, a green CI run is not proof).
2. `PATCH` each of the five keys to `workspace="aw"` (identify by name via
   `GET /api/api-keys`). Every one of these consumers ran on the `aw`
   workspace's runners before `fredericowu` existed (2026-09-27), so `aw` is
   evidenced, not guessed.
3. Live verification bar: one real `/v1/chat/completions` against a bound
   key for a runner-backed agent returns **200 with agent content** (runner
   transport does not depend on the OpenAI credit outage), and an unbound
   key against the same agent returns the declared **409 body**, not a bare
   500.

**Known residual, for the PO as a small follow-up card**: research-search's
`apmt_credential.py` re-mint flow deletes same-named keys and re-creates
them with only `name` + `agent_slugs` (`apmt_credential.py:129-141`), so a
future 401-triggered re-mint silently drops the binding and regresses that
one consumer to 409 (now declared and greppable, no longer a bare 500). Fix
is one line in `aw-app-research-search` (pass `workspace` at mint), but that
is another repo and its own release cycle; out of scope here, must not be
absorbed silently.

## 6. Tests (sqlite, `AGENTS_DATABASE_URL=sqlite:// python3 -m pytest`)

New `backend/tests/test_openai_compat_workspace.py` (sibling conventions:
`test_workspace_cascade.py`, including its mutate-the-settings-singleton
fixture for enforcement modes):

1. bound key → `run_agent` called with `workspace=<key's>` (tier
   `explicit`); covers chat_completions, responses, session_chat.
2. unbound key + 2 Runner rows + strict → **409** with
   `error.code == "workspace_ambiguous"` (this is the regression test for
   the card).
3. workflow door: bound key + `model="workflow/<slug>"` → the workflow's
   own Run row carries the workspace, a runner child resolves via tier
   `parent`.
4. the detached-copy pin: drive `require_api_key` → assert the yielded
   ApiKey carries `workspace` (kills the §3.4 no-op trap).
5. mint/PATCH validation: named workspace not in the Runner census → 422
   listing candidates; zero registered runners → accepted.
6. setup-phase 502: `run_agent` raising a generic exception → declared 502
   `dispatch_failed`, not a bare 500.

## 7. What this makes harder later

- **Keys couple to runner topology.** Moving an integration to another
  workspace is now a key rebind (PATCH), one more step in any future
  workspace migration runbook, and the five bindings in §5 are the first
  rows of that coupling.
- **A future per-request override (option c) inherits a precedence
  question.** Decide then: reject mismatches with the key binding rather
  than allow override, or the leak-widening argument in §4 comes back.
- **Every new `ApiKey` column must be hand-added to the detached copy** at
  `openai_compat.py:74`. This design is the second time that trap nearly
  fired; test 4 pins it, but the pattern itself remains.
- **One key = one workspace.** A consumer wanting to fan across workspaces
  needs N keys. Fine today; a real multi-workspace consumer would need a
  new mechanism, and should get one deliberately.
- **session_chat rebind semantics** (§3.4): rebinding mid-conversation
  cold-restarts the agent's memory on the new host. Documented, not
  prevented.

## 8. Risks for the Coder (non-obvious breaks)

- The **detached-copy no-op trap** (§3.4). The fix compiles, deploys, and
  does nothing. Test 4 is not optional.
- **Do not convert streaming errors to raised HTTP errors.** `_stream`'s
  error-as-content behavior (`openai_compat.py:466`) is correct for SSE and
  consumers already parse it (the `[agent <slug> <status>]` marker contract
  in research-search's `call_llm_apmt`).
- **FastAPI `HTTPException(409, …)` produces `{"detail": …}`**, which OpenAI
  SDKs do not parse. Use the JSONResponse helper with the `{"error": {…}}`
  shape.
- `run_workflow`: thread `explicit=` **only** in the `run_id is None`
  branch; the `api/workflows.py` pre-created-row path already carries
  workspace since 8c0e14e and must not be double-resolved.
- Do **not** change any agent's `model_slug` (card …81e5's territory, in
  progress elsewhere), and do not touch the Runner rows.
- Deploy verification is in-container grep + the §5.3 live calls, not CI
  green. The live 409-vs-200 pair is the falsifiable acceptance test of the
  whole design.
