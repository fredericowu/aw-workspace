# Per-tool exec-indexing policy, declared in each contributing app's manifest

Card `3fe5bf3b-9510-8121-ba56-de678aa4ca89`. Frederico's decision, designed —
not re-litigated: each app declares, per tool it contributes, whether the
CALL (arguments) and/or the OUTPUT (result) may be indexed into the execution
graph. Blacklist model: indexed unless the tool opts out. Materialised at
install, maintainable from the knowledgeable connector's settings UI.

This is the structural replacement for the hard-coded pair that §13.2 of
`docs/design/execution-history-in-production-neo4j.md` found had never fired
in production (138 secret-tool calls with args retained; 29 of 34
`write_secret` calls with no redaction marker). Slice 0 of that card — the
terminal-segment rule + purge — **shipped 2026-10-19** and stays underneath
everything here; this design is a policy layer on top of recognising the
tool, never a replacement for it.

It also dissolves, rather than overrides, §13.10's rejection of a per-tool
registry: that rejection was about 662 names maintained *centrally* rotting
with no owner. A declaration that ships inside the app that contributes the
tool arrives and leaves with the app; the UI maintains exceptions, not the
world.

## 1. The approach, in five sentences

Each app may add `contributes.mcp.indexing` to its `aw-app.json`: a list of
per-tool entries, each naming a bare tool and stating `call` and `output`
booleans; absence of an entry means "indexable" (blacklist), presence with
both axes true means "considered and indexable" — a visibly distinct third
state. The installed manifest copy at `apps/<slug>/aw-app.json` **is** the
install-time-generated artifact; the connector (`aw-app-knowledgeable`,
Tier-1, `fs:workspace-read`) derives the effective policy table from it —
plus the sibling `apps/<slug>/mcp.json` server keys, which are what the
gateway's observed prefixes are actually made of — once per sync tick, the
same build-per-tick shape `exec_scrub.Scrubber` already uses. An observed
tool name resolves to a registry entry by a specified grammar; a name that
parses as MCP-routed but resolves to nothing **fails closed** (arguments and
result dropped, name and timestamp kept, counted and surfaced on the sync
status and doctor — the `event_kinds_unreviewed` mechanism). Four tools —
`read_secret`, `collect_secret`, `write_secret`, `delete_secret` — remain a
hard-coded floor in both existing copies (`exec_scrub.py` and the service's
`exec_history.py`) that no manifest entry and no UI edit can lift. UI
overrides live in the connector's own state facade (never in the config blob
the settings form rewrites), and a maintenance page served from a connector
route, embedded as an `app_iframe` widget in its declarative window, shows
every row with its state AND the source its current value came from.

## 2. The concern, answered in writing: the floor stays, hard-coded

The card asked whether the four secret-store tools stay a floor no manifest
and no UI edit can lift. **Accepted, unreservedly**, on three grounds:

1. **The registry is policy; the floor is an invariant — and an invariant
   must not be expressible as data.** The 138-row defect was a deny rule
   that failed open on a name-spelling detail. Any data path to lifting the
   floor reintroduces a data bug as a leak vector — and this exact app has
   the precedent: its own `/settings` route once clobbered its own
   `service_secret` (recorded incident). A clobbered override store must be
   able to cost us *configuration*, never `write_secret` arguments.
2. **Scrubbing cannot be the backstop for these four.** §13.2 measured why:
   `write_secret` arguments are vault-only values (no exact-match hit) and
   shapeless (no pattern fires). Drop-wholesale is the only safe treatment,
   so it must not sit behind any switch, declared or clicked.
3. **The floor costs nothing.** No one has a legitimate use for indexing
   `write_secret` arguments; a rule that can never annoy a legitimate user
   is free to hard-code and expensive to make configurable.

Enforcement order is part of the contract: **floor check first, before any
registry or override consultation**, in both copies —
`exec_scrub.scrub_tool_args`
(`repos/aw-app-knowledgeable/knowledgeable_app/exec_scrub.py:555`) and the
service-side refusal (`repos/aw-knowledgeable/backend/app/core/exec_history.py:102`).
The service side keeps refusing **only the floor**: it has no independent
source of truth for declarative policy (manifests live in the workspace),
and mirroring the connector's own claim back at it adds zero refusal power.

The card's third bullet — flag an app that self-declares "index my output"
for a credential-handling tool: the canonical manifest validator
(`repos/aw-marketplace/scripts/validate_manifest.py`) **rejects** an
`indexing` entry that sets either axis true for a floor name, so the lie
dies at release, the earliest possible point. Beyond the four names we do
NOT add a credential-name heuristic as enforcement — suffix heuristics are
exactly the `spread_secret` bug class §13.2 warns against. The human check
is the UI's source column (§7): a row whose value came from a manifest
self-assertion is labelled as such, and a human can override it downward.

## 3. Identity resolution — the repeat-history risk, specified

The registry is keyed on tool identity; the sync observes gateway-prefixed
names in three measured spellings (§13.2):
`mcp__aw-gateway__aw__<upstream>__<tool>`,
`aw-gateway.aw__<upstream>__<tool>`,
`mcp__workspace-gateway__aw__<upstream>__<tool>` — plus federated nesting
(`…aw__kali__aw__playwright__browser_click`) and bare harness names
(`Bash`, `run_agent_async`).

**The join key is NOT the app slug.** Measured: the `<upstream>` segment is
the app's `mcp.json` server key, sanitised (`apps/secrets/mcp.json` declares
server `aw-secrets` → observed `aw__aw_secrets__write_secret`;
`apps/kb/mcp.json` declares `kb` → `aw__kb__search_knowledge_base`). An app
can declare several server keys (aw-app-notion ships both `notion` and
`aw-kanban`). So the connector builds, per installed app:
`sanitised server keys (apps/<slug>/mcp.json) → slug`, and
`(slug, bare tool) → declaration (apps/<slug>/aw-app.json)`.
The coder must copy the gateway's actual sanitiser (dash→underscore is the
observed rule; verify against the gateway's prefixing code, do not assume).

Resolution of an observed name, in order:

1. `seg = tool_name_segment(name)` (exec_scrub.py:87 — the shipped slice-0
   rule, segment EQUALITY, never `endswith`).
2. **Floor:** `seg ∈ SECRET_TOOL_NAMES` → both axes denied. Always first.
3. **Envelope parse:** strip a leading `mcp__<anything>__` or `<anything>.`
   envelope; if the remainder starts `aw__`, the next segment is
   `<upstream>` and the name is *routed*. The envelope segment is NOT
   allowlisted — allowlisting gateway spellings is how
   `workspace-gateway` was missed last time.
4. **Routed and upstream matches an installed app:** look up
   `(slug, seg)`. Declaration present → apply it (subject to override
   precedence, §6). Absent → blacklist default: index both axes.
5. **Routed and upstream matches nothing** (app uninstalled, unknown
   gateway spelling #4, a third-party `mcp__…` server with no `aw__`
   marker): **FAIL CLOSED.** Arguments and result dropped; name, timestamp
   and edges kept (the floor's own treatment); the distinct unresolved name
   counted into `policy_unresolved` on the sync status and surfaced by the
   doctor — the proven `event_kinds_unreviewed` shape
   (`exec_sync.py:_run_tick_locked`). Unresolvable is NEVER "no opt-out
   found, therefore index".
6. **Bare name (no envelope):** collect every declaration and override
   across installed apps whose tool equals `seg`. Any match → apply the
   **intersection of permissions** (any restriction restricts — ambiguity
   tightens, never widens; this also covers a direct-connected MCP tool
   arriving unprefixed). No match → harness tool, blacklist default.

Properties worth stating because they are the anti-§13.2 guarantees: every
path that cannot attribute a routed name ends in *deny*; every ambiguity
resolves toward *deny*; and uninstalling an app flips its tools from
"declared" to "unresolved → deny" automatically, with a visible counter,
instead of silently reverting to "index".

## 4. The manifest field

```json
"contributes": {
  "mcp": {
    "indexing": [
      {"tool": "write_secret", "call": false, "output": false,
       "reason": "arguments carry the secret value"},
      {"tool": "search_graph", "call": true, "output": true}
    ]
  }
}
```

- Sits under `contributes.mcp`, beside the existing `provides` /
  `reload_on_save` / `profiles` (core accessors at
  `src/apps/manifest.py:345-403`).
- `tool` is a **bare terminal segment** — the validator rejects any `__` or
  `.` in it, so a prefixed spelling can never be enshrined in a manifest.
- `call` and `output` are both **required** booleans per entry — an entry
  whose meaning depends on defaults is the ambiguity this design exists to
  remove. Entry absent = indexable (blacklist). Entry with both true =
  "considered, indexable" — the explicit third state (§7).
- `reason` is required whenever either axis is false: an opt-out nobody can
  review is the §13.10 rot in miniature.
- Duplicate `tool` values rejected; floor names with any axis true rejected
  (§2); floor names with both false allowed (redundant, harmless, good
  dogfood for aw-app-secrets).

Validator chain, in the standing order (core validator change precedes any
app shipping the field): `src/apps/manifest.py` (install+load — today it
tolerates unknown keys forward-compatibly, so it gains a typed accessor and
malformed-shape rejection), `repos/aw-marketplace/schemas/aw-app.schema.json`
+ `scripts/validate_manifest.py` (the canonical CI copy — this is where
floor-lie rejection and bare-segment enforcement live), and the per-repo
`tests/validate_manifest.py` template copies inherit it from the canonical
one.

## 5. Materialisation — derive per tick from the installed tree

Frederico: "a gente gera na instalação dela essa info." Satisfied as
follows: install already materialises the manifest into
`apps/<slug>/aw-app.json` (and the server keys into `apps/<slug>/mcp.json`,
which the gateway scans) — **that installed copy is the generated artifact.**
The connector builds the lookup table from those files once per sync tick,
exactly as `Scrubber.from_workspace()` is built per tick
(`exec_sync.py:736`), in a new `knowledgeable_app/exec_policy.py`. ~36 small
JSON files per tick is microseconds; per-call cost after that is one dict
lookup — no per-row I/O on a 300k-row backfill (card point e).

Consequences, each automatic rather than hooked:

- **App update:** marketplace update rewrites `apps/<slug>/…` → next tick
  (≤10 min) reflects it. No Tier-1 code-reload dependency — this is a data
  read per tick, not an import.
- **Uninstall:** files gone → the app's routed names become unresolved →
  **deny** + counted (§3.5). Fail-safe direction, visible.
- **Backfill / pre-field apps (all ~36 today):** nothing to generate.
  Undeclared = indexable is the model; the floor covers the four secret
  tools; the UI shows all of them honestly as "never declared".

## 6. Precedence

**floor > UI override > manifest declaration > blacklist default.**

Rationale: the floor is an invariant (§2); a human override must beat an
app's self-assertion (the override exists because the manifest can be wrong
— the weakest evidence in the system is an app describing itself); the
manifest beats the default because that is the whole card. Overrides are
keyed `(slug, tool)` and stored in the connector's **state facade**
(`state:own`, cross-worker safe) under a dedicated namespace — deliberately
NOT in the app-config blob, because this app's settings form has already
clobbered a sibling config key once (`service_secret` incident), and an
override store wiped by a form bug must not be able to change indexing
behaviour invisibly (it can't lift the floor regardless, §2). Overrides are
edited only through dedicated routes (§7), never through the config form.

## 7. The maintenance UI — three states, plus provenance

Lands in the connector's existing declarative window
(`repos/aw-app-knowledgeable/aw-app.json` → `windows/main.json`;
settings panel `knowledgeable.settings` points at the same window). The
declarative vocabulary has **no data-bound table** (the aw-app-proxy scar —
a `table` widget that has never rendered) and the settings iframe has **no
modals**; both constraints are designed around, not against: the page is a
self-contained HTML+JS document served by a connector route (NOT under the
reserved `/ui` prefix — e.g. `/policy-ui`), embedded via the existing
`app_iframe` widget (`aw-workspace-ui/src/components/AppWindow.jsx:349`),
with inline row-expansion for edit/confirm instead of modals.

Each row: app, tool, call/output effective values, **state** and **source**:

- `floor` — the four secret tools, rendered locked, no toggle.
- `override` — a human set it here; shows who/when/note.
- `declared — restricted` / `declared — indexable` — from the manifest,
  visibly distinct from each other and from:
- `never declared` — behaviourally identical to "indexable" under the
  blacklist, and deliberately NOT visually identical (card point f): this
  is the only way anyone can ever tell which tools have been considered.
- `unresolved` — observed names that failed closed (§3.5), with their raw
  spellings, so spelling #4 is diagnosed from the UI instead of from a
  future incident.

Row population: union of (a) manifest `indexing` entries, (b)
`contributes.mcp.provides` where present (display only — `provides` is a
stale-prone marketplace list and must never feed enforcement), and (c) the
distinct resolved identities the sync has actually observed (kept by the
policy layer in the state facade, capped). (c) is what makes the list honest
about the tools that actually run here without inventing a dependency on a
live gateway enumeration.

Routes (all on the connector's existing authed surface,
`knowledgeable_app/routes.py`): `GET /exec-policy` (effective table +
sources + unresolved), `PUT /exec-policy/override` (`{app, tool, call,
output, note}` — rejected for floor names), `DELETE /exec-policy/override`.

## 8. Where it lands — the full seam list

| Seam | File(s) | Change |
|---|---|---|
| Manifest schema + canonical validation | `repos/aw-marketplace/schemas/aw-app.schema.json`, `repos/aw-marketplace/scripts/validate_manifest.py` | `contributes.mcp.indexing`; bare-segment, required-axes, reason-on-restrict, floor-lie rejection |
| Core manifest loader/validator | `src/apps/manifest.py` (`contributes` handling, new accessor beside `mcp_profiles:368`) | typed accessor + malformed-shape rejection at install AND load; **lands before any app ships the field** |
| Policy engine | `repos/aw-app-knowledgeable/knowledgeable_app/exec_policy.py` (new) | manifest+mcp.json scan, resolution grammar (§3), per-tick table, unresolved tracking |
| Enforcement, call axis | `knowledgeable_app/exec_sync.py:385` (`build_toolcall_row`) | policy consulted after the floor; args dropped with a distinct marker when denied by policy vs floor |
| Enforcement, output axis | §13 slice 3's `result_text` path when it lands | consults `allow_output`; until then the axis is stored policy with a named consumer |
| Floor (unchanged) | `knowledgeable_app/exec_scrub.py:77,555`; `repos/aw-knowledgeable/backend/app/core/exec_history.py:64,102` | stays first, both copies; service side mirrors only this |
| Observability | sync status via `client.put_exec_sync_state` + the doctor check that already surfaces `event_kinds_unreviewed` | `policy_unresolved` distinct names; `policy_denied_calls` counter |
| Overrides | connector state facade (`state:own`) | dedicated namespace; never the config blob |
| UI | `windows/main.json` + new route-served page + `AppWindow.jsx` `app_iframe` (no UI-repo change) | §7 |

## 9. Slices

- **Slice 0 (shipped 2026-10-19, card `…81d0`):** terminal-segment rule in
  both copies + `exec_drop_secret_toolcall_args` purge. This design's
  prerequisite; verified in-tree.
- **Slice 1 — validators.** Marketplace schema + canonical validator + core
  `manifest.py`. No behaviour change anywhere; apps may now ship the field.
  Independently shippable and strictly first (standing rule).
- **Slice 2 — policy engine + call-axis enforcement.** `exec_policy.py`,
  wiring into `build_toolcall_row`, counters, doctor surfacing. Tests pin:
  all three measured spellings; nested federation (`aw__kali__aw__…` →
  app `kali`); `kb`-style short server key; `aw_secrets`-style
  key≠slug join; uninstalled-app → deny; `mcp__` third-party → deny;
  bare-name intersection rule; floor-beats-everything including an
  override row that lies.

  **Shipped 2026-10-19.** `knowledgeable_app/exec_policy.py` (resolution
  grammar, per-tick table, `Decision`, `apply_call_axis`), consulted from
  `build_toolcall_row`; `policy_unresolved` / `policy_unresolved_total` /
  `policy_denied_calls` / `policy_scan_error` ride the sentinel's `counts`
  and degrade the existing `contributes.doctor` check. 31 tests, every one
  §9 asks for. Three things measured against the REAL installed tree rather
  than fixtures (§12 risk 1):

  * **38 of the gateway's 40 live upstream segments resolve**, each to the
    right app — including every key≠slug case the design predicted
    (`aw_kanban`→`notion`, `paper_search`→`research-search`,
    `vscode`→`code-server`, `devctl_browser`→`devctl`,
    `playwright`→`mcp-tools`, `phd_knowledge_base`→`aw-app-uc-phd`) and the
    federated `kali`→`kali-linux`.
  * **The 2 that do not are the linked-workspace federations**
    (`fredericowu`, `fredericowu_browser` — BYOD `/link`, registered into
    the gateway's own config, not `apps/<slug>/mcp.json`). They fail closed
    and will sit on the doctor permanently once a tick observes a call to
    one. Correct per §3.5 and a known §11 cost, but it is a **standing**
    unresolved row rather than a transient one, which §7's `unresolved`
    state was written for and §3.5's counter was not: a doctor line that is
    always red detects nothing. Slice 3 should give the UI a way to
    acknowledge a row as "attributable to nothing, on purpose" — the
    `vault_coverage.py` registry shape — or core should expose the
    gateway's non-app upstreams so they can be attributed.
  * Decisions for all three measured spellings of real production names
    (`write_secret` → `floor`, args `None`; `add_kanban_comment` → app
    `notion`, `never_declared`, args kept) printed from the live tree.

  Deliberately NOT in this slice: the output axis. `Decision.allow_output`
  is resolved and carried (the §6 precedence rules govern both axes and
  §7's UI shows both) and is read by nothing — see the note on slice 4.
- **Slice 3 — overrides + maintenance UI.** State-facade store, the three
  routes, the `app_iframe` page with five-state source column, floor rows
  locked. No modals.
- **Slice 4 — output axis + policy-driven retro-purge.** `allow_output`
  consumed by the §13-slice-3 `result_text` path. **Now a real gap, not a
  future one (2026-10-19):** §13 slice 3 landed in the connector the same
  day slice 2 did (`attach_results` / `scrub_tool_result` in
  `aw-app-knowledgeable`), so `ExecToolCall.result_text` is being projected
  *today* under the floor alone — an app's `output: false` declaration is
  resolved, carried on the `Decision`, and consulted by nothing. The
  no-declarations-exist-yet fact (all ~36 installed apps) is what keeps that
  from being a live exposure, and it stops being true the moment any app
  ships the field. Slice 4 is therefore the next one to run, not an
  optional tail, and its wiring point is one call in
  `exec_sync.attach_results` beside the floor's. Retro-purge: when a new
  opt-out appears (manifest update or override), existing rows don't
  change; extend the §13.2 purge with a **per-bare-name, service-authed**
  variant the UI exposes per row ("purge existing rows"). This consciously
  relaxes §13.2's "no caller-supplied names" decision, and the argument is
  concrete: the purge shares ingest auth, and an ingest-secret holder can
  already overwrite `args_text` via the MERGE upsert — so a per-name
  null-out grants no destructive power that auth tier doesn't already
  have. It stays audit-stamped (`args_dropped_at`) and name-at-a-time.
- **(Optional, any time after slice 1):** aw-app-secrets declares its four
  tools `call:false, output:false` in its own manifest — redundant under
  the floor, but it dogfoods the field and makes the UI's "declared"
  state real on day one.

## 10. Rejected alternatives

- **Core-materialised consolidated registry, written at install** (a new
  lifecycle hook emitting one merged JSON/DB table). A second copy of
  truth that can go stale across update/uninstall — the §13.10 rot
  objection reborn one layer down — plus a core hook, a backfill migration,
  and a staleness protocol, to produce a file whose entire content is
  derivable in microseconds from the installed tree that already exists.
  Core's only change is the validator. *Runner-up; revisit if the
  connector ever moves out of process and loses `fs:workspace-read` — the
  honest fix then is a core API, not a generated file.*
- **Whitelist model.** Frederico decided blacklist; not re-litigated. The
  compensating controls are named: the floor (§2), fail-closed resolution
  (§3), and the three-state UI (§7) that keeps "never considered" visible.
- **Keying resolution on app slug / manifest id.** Measured wrong:
  observed segment is the sanitised `mcp.json` server key (`aw_secrets` vs
  slug `secrets`); a slug join silently resolves nothing — which under
  fail-closed would deny *everything*, the loud version of wrong, but
  still wrong.
- **Mirroring declarative policy server-side.** The service has no
  independent source for it; a mirror would re-state the connector's claim
  with refusal theatre. The server mirrors invariants only (floor, kinds).
- **Overrides in the app-config blob / settings form.** This app's own
  `/settings` route has clobbered a config key before; the store that can
  change indexing behaviour doesn't ride a wholesale-rewritten JSON blob.
- **A `table` widget or a new frontend bundle for the UI.** The
  declarative `table` widget has never rendered (aw-app-proxy, months,
  silent); a full `contributes.frontend` bundle is the heavyweight path
  with its own build/tailwind traps. `app_iframe` + route-served page is
  the existing idiom and needs no aw-workspace-ui change.
- **Credential-name heuristics as enforcement** ("flag tools whose name
  looks secret-ish"). Suffix heuristics are the `spread_secret` class.
  Humans review self-assertions via the source column instead.
- **Per-row policy evaluation recorded on each graph node** (stamping which
  policy version allowed a row). Costs a property on 300k+ nodes to answer
  a question Postgres (system of record) can answer retroactively; the
  purge + `args_dropped_at`/`args_policy_dropped` markers cover the cases
  that matter.

## 11. What this makes harder later

- **Policy is evaluated at sync time, not re-applied retroactively.** A
  tightened declaration only governs rows from that tick on; history needs
  the explicit per-name purge (slice 4). Deliberate — silent retro-mutation
  of the graph is the scarier default — but it means "we opted out last
  month" is not "nothing from before is indexed".
- **The connector is now coupled to the workspace FS layout**
  (`apps/<slug>/aw-app.json` + `mcp.json`). Moving it out of process later
  requires a core API for the same data (named in §10's runner-up).
- **The bare-name intersection rule (§3.6) lets one app's opt-out restrict
  same-named tools from other apps** when a call arrives unattributed.
  Chosen (ambiguity must tighten), but it will one day surprise someone
  whose `browser_click` args stopped indexing because a sibling app opted
  its own out.
- **Three sources per row (floor/override/manifest) make the UI's source
  column load-bearing.** If it ever lies, humans will edit the wrong layer
  and conclude the system ignores them.
- **A fourth prefix spelling degrades to deny + counter, not to index.**
  Correct direction, but it means a gateway rename silently stops indexing
  args workspace-wide until someone reads the doctor — the price of never
  repeating §13.2, and the doctor line is the mitigation.

## 12. Risks for the Coders

1. **Copy the gateway's sanitiser, don't re-derive it.** The server-key →
   observed-segment transform (dash→underscore observed) must match the
   gateway's prefixing code exactly; a near-match resolves nothing and
   fail-closed turns that into "no args indexed anywhere" — loud in the
   counter, invisible in green tests that used hand-written fixtures.
2. **Floor before policy, both copies, pinned by a test that tries to lift
   it** via a manifest entry AND via an override row. The test that matters
   is the one proving the floor wins over a *lying* data layer.
3. **Never feed `contributes.mcp.provides` into enforcement.** Display
   only. It predates this field, is maintained by hand, and is exactly the
   stale list this design exists to avoid trusting.
4. **The policy-denied drop must stamp a different marker than the floor
   drop** (e.g. `args_policy_dropped` vs the floor's plain `None` +
   service refusal), or the §13.2 purge/verification queries stop being
   able to distinguish a remediated exposure from routine policy.
5. **State-facade reads per tick, no module-level cache.** Core runs
   multi-worker; a worker-local cached override table is the WORKERS=N
   silent-divergence bug this workspace has already catalogued.
6. **`tool_name_segment` is shared with slice 0 — do not fork it.** The
   resolution grammar (§3) wraps it; a second segmentation function is how
   the two paths drift and one of them re-learns `endswith`.
7. **Tests must include the federated double-prefix** — attribution goes to
   the *installed* app (`kali`), not the inner server (`playwright`); the
   inner `aw__` marker is the trap.
8. **The iframe page is inside the no-modals constraint** — `confirm()` in
   the purge-row action will silently no-op in some embeddings; inline
   confirm rows only.
9. **Slice 1 must not reject existing manifests.** ~36 installed apps have
   no `indexing` key; absence is valid forever, not a deprecation.
   **Corollary, and it bit us** (risk 11): that makes "declared nothing" a
   legitimate everyday state, which is exactly why "could not read" must not
   be spelled the same way.
10. **A test that reads the real `apps/` tree can only be green here.**
    Paid for on slice 4's own release (2026-10-19). `PolicyTable.
    from_workspace()` reads `/opt/aw-workspace/apps`, which exists for every
    human and every agent running the suite inside this workspace and does
    NOT exist on a GitHub runner — so in CI the table degrades, every routed
    name fails closed exactly as §3.5 requires, and a test asserting "a
    never-declared tool's result lands" is false. The release went red on
    one such test.

    What makes this a risk entry rather than a bug note: the test was
    verified twice, by two parties, each running the full suite, each
    getting **715 passed** — and neither verification could see it, because
    both ran in the one environment that satisfies the dependency. A suite
    that reads the machine it runs on has no local signal that it is doing
    so, so "I ran the tests" is not evidence about CI for this module.

    Two things follow, both now in place:

    * `tests/conftest.py` carries an **autouse guard** pointing
      `exec_policy._apps_root()` at a path that does not exist, so local and
      CI agree by default and the dependency cannot be acquired by
      forgetting something. A test that wants a tree declares one
      (`_policy_tree` / `_estate`); a test that genuinely wants the real one
      asks for the `real_apps_root` fixture, which **skips** when the tree
      is absent — the same shape `tests/test_exec_scrub.py:863` already used
      for "aw-workspace core is not on the path".
    * Verify this module's suite in a **CI replica** — repo root, no
      workspace checkout on `PYTHONPATH`, only the deps
      `aw-marketplace/.github/workflows/app-release.yml` installs
      (`pytest jsonschema fastapi httpx uvicorn`) — not only in-workspace.
      The in-workspace run is the one that cannot fail.

    The mirror direction exists too and is NOT fixed: `test_routes.py`'s
    `reset-watermark` test exercises §13.7's preflight against the **real
    host disk**, so it is red here whenever free space is under the 25 GB
    floor (observed failing at 22.2 GB and 24.3 GB, passing at 36.3 GB on a
    host that sits near 95 % used) and green in CI, where a runner has room.
    `disk_preflight` is pure specifically so the low-disk case can be tested
    with an invented number; the route-level test injects nothing. Left as a
    finding rather than fixed, because it belongs to §13 slice 3 — but any
    slice touching that route should take the measurement as a parameter.
11. **Under a blacklist, every read path must fail CLOSED — and they must
    all agree.** Found by QA on slice 2 (run
    `ee833426cc074f1ea716d7001497160e`), filed non-blocking, reclassified as
    blocking and fixed in slice 4.

    `_declared_entries` caught `(OSError, ValueError)` and returned `{}`.
    Under a blacklist `{}` does not mean "unknown" — it means *this app
    opted out of nothing*, i.e. every tool of that app is indexable on BOTH
    axes. So an unreadable or malformed `apps/<slug>/aw-app.json` silently
    converted every declared opt-out in that app into permission to index.

    Three things made it worse than it looks, and each is a general lesson:

    * **The docstring said the opposite of the code.** It stated "every
      coercion here tightens rather than widens, because this is a security
      default", and every coercion *inside* the loop genuinely did. The
      guard *around* the loop widened. A docstring that describes the body
      but not the error path is worse than none — a reader who trusts it
      never looks at the `except`.
    * **Its siblings failed the other way.** `from_workspace` losing the
      apps ROOT fails closed, and `_server_keys` losing an `mcp.json` fails
      closed (the app's upstreams vanish from the map, so its routed names
      are unattributable). Only this path failed open. Either direction can
      be defended; **two read paths over the same tree disagreeing cannot**,
      and the narrower-scoped one being the unsafe one is how it stayed
      invisible.
    * **It is the exact failure mode this card exists to fix.** The 138
      exposed secret-tool calls happened because a policy failed open. The
      floor covers four tools; `indexing` is the only thing between the
      other ~658 and the index, and the middle tier — a non-secret tool
      whose output carries a credential — is what slice 4 was built to
      protect. A declared `output: false` that evaporates on an `IOError` is
      that protection failing silently.

    Fixed by returning `(entries, read_error)`: a read failure makes that
    app's routed tools `unresolved` (both axes denied, name and timestamp
    kept) and lands in `policy_unresolved_actionable` plus
    `policy_manifest_unreadable` (`{slug: error_name}` — the pointer to the
    file to fix). Checked *after* the override lookup, because §6 puts a
    human override above the manifest and a corrupt manifest does not
    invalidate what a human said about one tool. Scoped to apps that
    contribute upstreams: an app serving no tools cannot have its policy
    widened, and this workspace's `apps/` holds 7 `.<app>.fetch-*`
    download-staging directories with no manifest and no `mcp.json` that
    would otherwise be 7 permanent entries in the actionable bucket — the
    always-on-counter defect `UNRESOLVABLE_UPSTREAMS` exists to prevent.
    Measured after the fix: `manifest_errors: 0` on the real tree and all 39
    live upstreams resolving exactly as before, so the fix is a no-op until
    something is actually broken.

    The general rule for anyone extending this module: **when a function
    returns a security default, its error path needs the same scrutiny as
    its happy path, and the docstring must cover both.**
