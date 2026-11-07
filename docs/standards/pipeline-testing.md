# Standard — pipeline testing, coverage, lint and pre-commit for `aw-*` repos

**Status:** design, written 2026-09-09. Amended 2026-11-04: §6a (JS lint)
and §6b (hook-set amendments) added alongside the `aw-app-template` Phase 1
implementation; by then the `aw-app-kb` pilot and the template's own
coverage gate (at 100%, not 80 — an explicit per-repo exception) were live.
**Card:** `Architecture doc: pipeline testing standards` —
`3d65bf3b-9510-8169-8392-c0d2d24e599a`, target `aw-pipelines-test-coverage`.
**Scope:** this document decides the shape. Implementation lands in separate
per-repo cards; see [§8 Rollout](#8-rollout).

Every number in this document was **measured on 2026-09-09** on the checkouts
under `/opt/aw-workspace/repos/`. They are a snapshot, not a contract —
re-measure before acting on any of them. How to re-measure is in
[§9](#9-how-to-reproduce-every-number-here).

---

## 0. The three findings that shaped this design

Read these first. Each one contradicts an assumption the card was written on,
and each one changes what the right answer is.

**0.1 — The pipeline the apps share does not live in `aw-app-template`.**
The template's entire CI is a 9-line caller
(`repos/aw-app-template/.github/workflows/release.yml:21`) into
`tekflox/aw-marketplace/.github/workflows/app-release.yml@master` — a **moving
ref**, deliberately (`app-release.yml:9-10`). Every `aw-app-*` repo calls the
same file. So the template is where an app's *local* files come from, but
`app-release.yml` is where the *gate* actually is. Changing the template first
changes nothing for the 47 apps that already exist. See [§2](#2-where-the-gate-lives).

**0.2 — A hard 80% gate today would fail the majority of the estate, starting
with the reference template itself.** Measured across 41 app packages:
**5 are at or above 80%**; the median is **62%**; `aw-app-template` — the thing
every new app is born from — is at **68%**, with `template_app/plugin.py`
(its whole aw-workspace integration surface) at **0%**. Full table in
[§4](#4-coverage). This is why the coverage decision is a ratchet, not a cliff.

**0.3 — Pylint's error class is worth adopting on its own merits; its style
classes would fight this codebase.** Full pylint on `template_app` scores
**8.80/10** with 11 messages — 10 are `missing-*-docstring`, and the 11th
(`C0415 import-outside-toplevel`) flags a pattern `app-release.yml:113-121`
explicitly *documents as required*. But `pylint --disable=all --enable=E` on
`agents-platform-multitenant/backend/app` found **two real latent bugs** that
have been sitting in production code (see [§5.1](#51-what-the-error-class-already-found)).
That asymmetry is the whole basis for [§5](#5-lint).

---

## 1. What exists today — measured

### 1.1 The two-tier reality

There is no single "aw-* pipeline". There are two, and they have almost nothing
in common.

| | Tier A — the 48 `aw-app-*` repos | Tier B — core & services |
|---|---|---|
| Repos | `aw-app-*` | `aw-workspace` (this repo), `aw-backend`, `agents-platform-multitenant`, `aw-mcp-gateway`, `aw-console`, `aw-workspace-ui`, `aw-mobile`, `aw-remote-host`, `aw-stack`, `aw-marketplace`, `aw-vault`, `aw-automation` |
| CI | one shared reusable workflow | one bespoke workflow per repo |
| Owned by | `tekflox/aw-marketplace` | the repo itself |
| Test gate | conditional (see 1.2) | varies per repo |
| Coverage gate | none | none |
| Lint gate | none | none |
| pre-commit | none | none |

**Zero repos in either tier have a `.pre-commit-config.yaml`. Zero repos have a
pylint configuration. Zero repos enforce a coverage threshold.** Those three are
greenfield, which is the good news in this document.

### 1.2 Tier A — what `app-release.yml` actually gates

`repos/aw-marketplace/.github/workflows/app-release.yml:110-137`, in order:

1. `pip install -q pytest jsonschema fastapi httpx uvicorn`, plus
   `requirements-dev.txt` if the repo has one (`:123-125`).
2. **Manifest validation, unconditional**, against the canonical schema in
   `aw-marketplace` (`:133-134`). This one is already exemplary — the comment at
   `:126-132` records why it stopped being per-repo (28 drifted copies, and apps
   that shipped no validator published with no validation at all).
3. **`if compgen -G "tests/test_*.py" > /dev/null; then python3 -m pytest tests/ -q; fi`**
   (`:135-137`).

Step 3 is the whole test gate, and it has two properties worth naming:

- **It is conditional on tests existing.** An app with no `tests/test_*.py`
  releases green having run zero tests. Today exactly one app is in that
  position: `aw-app-kali-linux` (0 test files — it wraps a stock
  `lscr.io/linuxserver/kali-linux` image). It is not a crisis, but it is the
  reason "every public function has a unit test" cannot be enforced by this
  workflow as written — the workflow cannot tell "no tests needed" from
  "no tests written".
- **The guard and the run disagree about scope.** The guard globs
  `tests/test_*.py` (top level only); the run is `pytest tests/` (recursive). An
  app that put everything under `tests/unit/` would be skipped entirely while
  looking well-organised. No app is currently in that state, so this is latent,
  not live — but it is a trap for exactly the repo layout this standard will
  encourage.

There is **no coverage step and no lint step** anywhere in that file.

### 1.3 Tier B — three repos that already tried, and stopped short

These matter because in each case the machinery is already built and switched
off. Turning it on is cheaper than the greenfield work, and the reason it is off
needs to be understood before flipping it.

**`aw-backend` — coverage fully configured, disabled twice.**
`src/tests/pytest.ini:9-14` sets `--cov=src`, `--cov-report=term-missing`,
plus HTML and XML reports and `--cov-config=src/tests/.coveragerc`. The
`.coveragerc` has a considered `omit` list and an `exclude_lines` block — and
`fail_under = 0`. On top of that, **every** CI invocation passes `--no-cov`
explicitly (`.github/workflows/test.yml:80`, `:126`, `:153`). So coverage is
measured nowhere and gated nowhere, through two independent switches. This repo
needs a threshold, not a coverage setup.

**`agents-platform-multitenant` — a linter configured that never runs.**
`pyproject.toml:80-82` defines `[tool.ruff]` (`line-length = 100`,
`target-version = "py311"`) and `:67` declares `ruff>=0.7` as a dev dependency.
A grep across all four workflows in `.github/workflows/` finds **zero**
invocations of `ruff`. It is dead config — which is worth stating plainly,
because "AP-MT already uses ruff" is the kind of thing that reads as true and
would quietly derail the lint decision in [§5](#5-lint).

**`aw-workspace` (core) — ~~a serious test job, no quality gates~~ ON STANDARD
as of 2026-11-04 for lint; coverage still open.**
`.github/workflows/test.yml` stands up an ephemeral Postgres *and* Redis, joins
a `python:3.12-slim` container into the Postgres netns, and runs `pytest src/tests`.
The comments in that file are the best documentation in the estate of *why a
skipped test is a failure* ("a skipped test is how a card's whole VERIFY step
quietly stops running").

The lint half of this entry is now closed. Core has a `pyproject.toml` (lint
config only — deliberately **no `[project]` section**, so the repo cannot
quietly become pip-installable alongside the `src/` it already imports off
`PATH`), a `.pre-commit-config.yaml` on the Tier-B hook set, and a `lint` job
in `test.yml`:

- **pylint `--errors-only` is BLOCKING.** The tree is measured clean under it,
  in the CI container with `requirements.txt` installed. Triage found **1 real
  bug** — `src/tests/unit/apps/test_contributed_tasks.py` defined
  `test_valid_agentic_output_task_is_accepted` twice, so Python's
  redefinition rules meant pytest only ever collected the lower one. The dead
  upper copy asserted that an `agentic_output` task with **no** `agent_slug` is
  accepted, the exact opposite of the rule `485ca9c` had added, and would have
  failed the moment it ran. The remaining 4 findings are false positives
  disabled inline with a reason at each site.
- **`ruff check` is REPORT-ONLY (`|| true`).** Measured: **78 pre-existing
  violations across 247 files** — 50 `I001` (import sorting), 15 `F401`, then
  6 `E741` / 2 `F841` / 2 `F541` / 2 `E702` / 1 `E731`. Same posture, and the
  same precedent, as AP-MT: a blanket `ruff check --fix` is **not** safe here.
  AP-MT tried it on its own baseline and reverted it when reordered imports
  silently broke 2 passing tests via SQLAlchemy listener registration order;
  core has the same hazard in a different place (`src/apps` registration is
  import-order-sensitive) and 50 of the 78 findings *are* import reordering.
  Cleanup is hand-done, test-verified, file by file, on its own card.

Still open for core: **coverage**. No baseline has been measured (see
[§11](#11-what-i-could-not-verify) — the suite needs a real Postgres and
Redis), so there is no `fail_under` to set yet.

### 1.4 Runner and interpreter facts that constrain everything below

- Tier A runs on `[self-hosted, aw-baremetal]` (`app-release.yml:38`) with
  **Python 3.11** (`:106-108`). Core runs on `[self-hosted, aw-workspace]` in a
  **Python 3.12** container. AP-MT targets **3.11**. Any tool version pinned by
  this standard must work on both 3.11 and 3.12.
- These are self-hosted runners sitting next to production. Every new CI step
  costs wall-clock on a shared machine, and the existing workflows go to real
  trouble to avoid colliding with production Postgres. A coverage or lint step
  that doubles job time is a real cost, not a rounding error.

---

## 2. Where the gate lives

**Decision: the enforcement lives in `tekflox/aw-marketplace`'s
`app-release.yml` for Tier A, and in each repo's own workflow for Tier B.
`aw-app-template` carries the *local* files, not the gate.**

Concretely, a change to the testing standard lands in up to three places:

| What | Where it goes | Reaches |
|---|---|---|
| The gate (run tests, measure coverage, run lint, fail the build) | `aw-marketplace/.github/workflows/app-release.yml` | all 48 apps on their next push |
| Lint/coverage **configuration** an app must own (`pyproject.toml` sections, `.pre-commit-config.yaml`, per-app coverage baseline) | `aw-app-template`, then per-repo | new apps immediately; existing apps per card |
| Tier B gates | each repo's own `.github/workflows/*.yml` | that repo |

This inverts the card's stated order ("template first, then repo by repo") for
the gate specifically, and the reason is mechanical: the moving `@master` ref at
`aw-app-template/.github/workflows/release.yml:21` means one merge in
`aw-marketplace` changes the gate for every app at once, whereas a template
change reaches only repos created after it.

**The card's instinct is still right for configuration.** A coverage baseline
or a pylint rc file cannot live in a reusable workflow — it is per-repo data.
Those go in the template first, exactly as the card says, so a new app inherits
them without thinking.

**Rejected: put the gate in the template and roll it out repo by repo.** It
turns one reviewed change into 48 near-identical PRs, and guarantees permanent
drift — which is the precise failure `app-release.yml:126-132` already documents
having lived through with the manifest validator ("28 copies in 10 drifted
versions"). Re-creating that shape for coverage and lint would repeat a mistake
this codebase has already paid for once.

**Consequence, and it is a real one:** a bad commit to `app-release.yml` breaks
the release path of all 48 apps simultaneously. That risk is already accepted
(the moving ref is deliberate), but this standard increases the blast radius by
adding steps to that file. Mitigation in [§8.1](#81-phase-0--make-the-shared-workflow-safe-to-change).

---

## 2a. A reusable-workflow caller must grant `checks: read` (addendum, 2026-11-04)

The Tier-B review/merge loop now lives in
`fredericowu/aw-shared-workflows/.github/workflows/code-review-reusable.yml`,
called by three repos. **Every caller's `uses:` job must list all five
permissions**, and the fifth is the one that bites:

```yaml
    permissions:
      contents: read
      pull-requests: write
      issues: write
      statuses: write
      checks: read        # <- needed HERE *and* in the shared file; see below
```

**It must be in BOTH places, and this is where I got it wrong first.** The
effective token is the *intersection* of the caller's `uses:`-job block and the
called workflow's own top-level `permissions:`. Once a `permissions:` key names
any scope, every scope it does **not** name is forced to `none` rather than
inherited (GitHub docs, `permissions`: *"If you specify the access for any of
these permissions, all of those that are not specified are set to `none`"*).

So the rule cuts both ways: a scope the caller omits cannot be granted
downstream, **and** a scope the shared file omits cannot be granted by the
caller. Either side alone caps the token to zero.

`aw-shared-workflows@v1.1.1` was cut specifically to fix the missing
`checks: read`, added it to all three callers and to the shared file's caller
checklist, and never touched the shared file's own `permissions:` block. Net
effect was no fix at all, just a better error message. Caught by the Dev Team
reviewer on `aw-app-template#17` by resolving the pinned tag and reading the
dependency rather than the diff in front of it, and fixed in **v1.1.2**.

Two things worth taking from that, beyond the scope itself:

- **Neither canary would have caught it by running.** A public repo's gate
  works with or without the scope; a private one fails identically before and
  after. Only reading both sides finds it. v1.1.2 therefore adds
  `test_the_reusable_requests_every_scope_the_caller_checklist_demands`, which
  parses the shared file's own block and its caller checklist and fails if they
  diverge.
- **A "fix" verified only against the repo that cannot exhibit the bug is not
  verified.** I reported v1.1.1 as fixing the private-repo failure on the
  strength of a green run on the public template.

The per-caller half remains a standing obligation, exactly the kind of drift §2
otherwise tries to eliminate. The checklist plus that test is the enforcement.

**Why omitting it is invisible.** The merge gate reads the PR's checks with
`gh pr checks`, which goes through the Checks API. A **public** repo's check
rollup is readable without `checks: read`; a **private** repo's read is
*refused* — and returns an empty list rather than an error. The gate then
reports:

```
refusing to merge — required check(s) ['pytest'] did not report a pass
          on this head SHA. Saw: []
```

which reads like a CI failure, not a permissions problem.

**Measured, 2026-11-04.** Identical caller code, opposite outcomes:

| Repo | Visibility | Result |
|---|---|---|
| `tekflox/aw-app-template` | public | merged first try (PR #16) |
| `fredericowu/agents-platform-multitenant` | **private** | 4 consecutive `/aw-review` rounds failed, ~40 min after `pytest` went green |

Two agents debugged the wrong workflow before the cause was found.

**Two consequences worth carrying forward.**

1. `aw-shared-workflows@v1.1.1` hardened the reader so a *failed* read is no
   longer indistinguishable from "zero checks exist" — empty stdout with
   stderr content now retries to the 20-minute deadline. That converts the
   symptom from an instant false negative into a timeout. It is **not** a
   substitute for granting the scope — and on v1.1.1 it was in fact the *only*
   thing that landed, since the permissions half was inert until v1.1.2.
2. **A public canary cannot prove a tag for a private consumer.** §8.2's
   template-first rollout order is still right, but it has this blind spot by
   construction: the template is public. A private consumer is a *second,
   independent* canary, not a follower.

---

## 3. Test standard

**Decision: every public function reachable from a documented seam has at least
one unit test that exercises its behaviour, not just its import.**

"Public function reachable from a documented seam" rather than "every public
function", because in these repos the meaningful seams are enumerable and the
naive rule is not enforceable. For an `aw-app-*` repo the seams are:

- `plugin.py` — `activate(ctx)` / `deactivate(ctx)` and anything the manifest
  names. **This is the app's contract with the workspace and it is currently the
  least-tested file in the average app** (`template_app/plugin.py`: 0%).
- `routes.py` — every route function.
- `installer.py` — every `install_*` / `uninstall_*`.
- `mcp/` — every tool function exposed to the gateway.
- Any module a `contributes.*` entry in `aw-app.json` points at.

**Explicit exemptions — these do not require a test, and a reviewer should not
ask for one:**

1. `if __name__ == "__main__":` glue and `__main__.py` argument plumbing.
2. Getters/setters and properties with no branch and no side effect.
3. `__repr__`, `__str__`.
4. Pure re-export `__init__.py`.
5. Thin pass-through wrappers over a third-party client where the only
   assertable behaviour would be "the mock was called" — **unless** the wrapper
   does mapping, retry, or error translation, in which case that logic is the
   test.
6. Code behind a `pragma: no cover`, which must carry a comment saying why.

Exemptions 1–4 are already encoded in `aw-backend/src/tests/.coveragerc`'s
`exclude_lines`; that file is the precedent and the shared coverage config in
[§4.3](#43-the-shared-coverage-config) should start from it.

**A test that only asserts a module imports does not count.** Three apps
currently pass their whole suite while covering 0% of their package
(`aw-app-agents-platform-test-fixtures`, `aw-app-devteam`,
`aw-app-maintenance-agents`). Those suites are green and prove nothing about the
package they ship; the coverage number is what exposed it, which is an argument
for [§4](#4-coverage) independent of any threshold.

---

## 4. Coverage

### 4.1 The measured baseline

41 app packages measured (`coverage run --source=<pkg> -m pytest tests/`).
Seven are excluded: six have no Python package or no tests at all
(`blender`, `browser`, `code-server`, `home-assistant`, `kali-linux`, `signoz`
— all container/desktop wrappers), and `aw-app-crispal` could not be measured
here because its suite needs `PIL` from its own `requirements-dev.txt`, which CI
installs and this measurement did not. **`aw-app-crispal` is unmeasured, not
failing.**

| Band | Count | Apps |
|---|---|---|
| **≥ 80%** | **5** | presentations 89, mobile 83, diff-tool 82, notion 80, remote-host-cli 80 |
| 70–79% | 6 | aws 79, google-cloud 79, feed-subscriber 76, essentials 73, plaud 72, ssh 70 |
| 60–69% | 10 | git 69, mcp-tools 69, mini-browser 68, **template 68**, remote-host-terminal 68, tunnel 67, remote-screen 64, agents-platform-runners 63, portrait 63, call-agent 62 |
| 50–59% | 6 | secrets 58, codegraphcontext 57, code-agent-clis 56, android-studio 54, devctl 53, whiteboard 53 |
| < 50% | 14 | kb 48, travel 48, weather 48, google-maps 45, tasks 40, architecture 34, whatsapp 34, windows-pilot 34, google-workspace-mcp 28, proxy 27, roblox 26, test-fixtures 0, devteam 0, maintenance-agents 0 |

**Median: 62%. Mean: ~57%. At or above 80%: 5 of 41 (12%).**

### 4.2 The decision

**Decision: `pytest-cov` with a per-repo baseline and a one-way ratchet. Not a
hard 80% gate on day one. 80% is the floor's destination, not its starting
value.**

The mechanism:

1. Each repo records its measured baseline in its own `pyproject.toml`
   (`[tool.coverage.report] fail_under = <baseline>`), set to the measured
   number **rounded down to the nearest whole percent**.
2. CI runs `pytest --cov` and fails if coverage drops below `fail_under`.
   Coverage cannot go down. That alone stops the estate getting worse, and it is
   the property with by far the best cost/benefit here.
3. A repo may only raise `fail_under`, never lower it. Lowering it is a reviewed
   exception with a written reason in the commit message.
4. A **global floor** applies on top, and rises on a published schedule. It
   starts at 50%, so the 14 sub-50% apps have a named target that is not
   80%. Proposed schedule — the PO owns these dates, not the Architect:
   floor 50% at adoption, 65% at +3 months, 80% at +6 months.
5. New code has no grandfather clause: **a new `aw-app-*` repo starts at 80%**,
   enforced from its first release. This is the only way the estate converges
   rather than just stops sliding, and it costs nothing today because there are
   no new repos yet to be unfair to.

Tooling: `pytest-cov` (which wraps `coverage.py`). `aw-backend` already uses it,
so this introduces no new tool to the estate — only a threshold.

**Rejected: a hard 80% gate now, applied everywhere.** It would immediately red
the release path of 36 of 41 apps, including `aw-app-template` at 68%. A gate
that everything fails is not a gate; within a week it would be bypassed with
`[skip release]` (which `app-release.yml:37` already honours) and the standard
would be dead with no one having decided to kill it.

**Rejected: coverage as a report-only metric with no gate.** That is exactly the
state `aw-backend` is in today — full reporting configured, `fail_under = 0`,
and `--no-cov` in CI anyway. Measured and ungated converges on unmeasured. The
ratchet is the cheapest thing that is not this.

**Rejected: a single estate-wide number in the shared workflow with no per-repo
baseline.** The spread is 0%–89%. Any single number is simultaneously
unreachable for `aw-app-proxy` and a *regression licence* for
`aw-app-presentations`, which could shed 9 points and stay green.

### 4.3 The shared coverage config

`aw-backend/src/tests/.coveragerc` is the starting point — its `omit` and
`exclude_lines` blocks already encode exemptions 1–4 from [§3](#3-test-standard).
It should be lifted into the template as a `[tool.coverage.*]` section in
`pyproject.toml` (not a separate `.coveragerc` — one fewer file per repo), with
`fail_under` as the only per-repo value.

**Measure the package, not the repo.** `--source=<pkg>` (e.g.
`--source=template_app`), never `--source=.`. Pointing it at the repo root
sweeps in `tests/`, `ui/`, `scripts/` and `examples/` and produces a number that
moves when nothing about the code changed.

---

## 5. Lint

**Decision: `pylint --errors-only` is the blocking gate. Style and formatting
are `ruff`, report-only at first. Do not make two linters blocking.**

### 5.1 What the error class already found

`pylint --disable=all --enable=E` on
`agents-platform-multitenant/backend/app` (score 9.93/10) surfaced two real
defects that are in production code today:

**`E0602: Undefined variable 'run_url'` — `app/core/wakeups.py:899` and `:957`.**
Verified by reading the file, not taken from the linter: `run_url` is imported
locally inside other functions at `:196` and `:1118`, but at `:899` and `:957`
there is no import in scope. Both call sites are inside
`_notify_mark_done_rejected` (`:894`) and `_notify_mark_planned_rejected`
(`:952`) — the handlers that alert sysadmins **when a Kanban move is rejected**.
Both are wrapped in `try: ... except Exception: log.warning(...)`, so the
`NameError` is swallowed and the alert silently never sends. This is precisely
the escalation path the `aw-agents-flow` skill documents as the safety net
("a rejected/failed move just pings sysadmins on Telegram so a human notices").
It has never worked.

**`E1125: Missing mandatory keyword argument 'runner' in constructor call` —
`app/core/models/__init__.py:126`.**

Neither is a style opinion. Both are the class of defect a reviewer does not
catch and a test only catches if it happens to walk that error path — and error
paths are the least-covered code in every repo in [§4.1](#41-the-measured-baseline).

**This document does not fix them.** They are out of an Architect's lane and out
of this card's scope; see [§10](#10-handoffs).

### 5.2 Why not full pylint

Full pylint on `template_app` scores 8.80/10 with 11 messages. Ten are
`C0115`/`C0116` missing-docstring. The eleventh is
`C0415 import-outside-toplevel` at `template_app/installer.py:29` — and
lazy-importing inside the function is a pattern `app-release.yml:113-121`
explicitly documents as the *required* approach so CI need not install every
heavy dependency. A blocking check that fires on a documented convention trains
everyone to add `# pylint: disable=` without reading, which is worse than no
check.

So the initial blocking set is **`--errors-only` (E and F classes)**, with
`E0401 import-error` disabled — it fires on every optional/lazy third-party
import (12 of the 14 findings on AP-MT were `E0401` for `langchain_*`) and is
purely a function of what happens to be installed in the lint environment.

C/R/W classes run **report-only** — printed in the job summary, not failing the
build — and individual checks graduate to blocking by an explicit decision, one
at a time, recorded here.

### 5.3 Why ruff stays, and stays non-blocking

AP-MT already has `[tool.ruff]` and a `ruff>=0.7` dev dep
(`pyproject.toml:67,80-82`) that nothing invokes. Rather than delete it or
promote it, keep ruff as the fast style/format layer (it is ~100× faster than
pylint and is what the pre-commit hook in [§6](#6-pre-commit) can afford to run
on every commit), and give it the estate-wide config the template ships.

**Two linters, one blocking gate.** Pylint owns semantic errors in CI; ruff owns
style locally and reports in CI. If they ever disagree on the same rule, ruff's
config yields — pylint is the one that can fail a build, and a rule enforced by
two tools with two configs is a rule that will drift.

**Rejected: ruff only, no pylint.** Ruff's default rule set would have caught
the `run_url` bug (`F821`), but not `E1125 missing-kwoa` — ruff has no
type-inference-based checker equivalent to astroid's. The card asked for pylint;
the error class justifies it on evidence rather than on the request alone.

**Rejected: pylint at a score threshold (`--fail-under=8.5`).** A score is a
weighted average over the whole package, so adding well-documented code can mask
a new error, and deleting a docstring-heavy module can fail a build that
introduced nothing. Gate on the presence of an error, not on an aggregate.

---

## 6. pre-commit

**Decision: adopt the `pre-commit` framework (the Python package). Hooks are a
strict, fast subset of CI. The hook set does NOT run the test suite.**

`pre-commit` is the right choice and there is nothing to reconcile: **no `aw-*`
repo has a `.pre-commit-config.yaml` today**, so there is no incumbent to
displace and no conflict to manage.

The hook set, in order:

1. `ruff check --fix` and `ruff format` — changed files only.
2. `pylint --errors-only` — changed files only.
3. Whitespace/EOF/large-file/merge-conflict — the stock
   `pre-commit-hooks` set.
4. Manifest validation for `aw-app-*` — but **only** as a local hook calling the
   canonical validator, never a vendored copy of the schema. Re-vendoring the
   schema per repo is the exact failure `app-release.yml:126-132` documents.

**The test suite is deliberately not a hook, and this is the part most likely to
be argued with.** Three reasons, in descending order of force:

- **It would not work.** `aw-backend`'s suite needs a real Postgres *and* Redis;
  core's runs inside a `python:3.12-slim` container joined to a Postgres netns.
  A hook that cannot run its repo's real suite either fails constantly or
  silently runs a subset — and a hook that runs a subset while looking like it
  ran the suite is worse than no hook.
- **Concurrent agents share one working tree.** Multiple agent sessions operate
  on the same checkout and index in this workspace. A multi-minute blocking
  commit hook is paid by every one of them, on every commit.
- **CI is the gate; pre-commit is the fast feedback.** Duplicating the gate
  locally means every change to the standard has to land in two places and stay
  in sync.

**`pre-commit` must never be the only place a check runs.** Hooks are bypassable
(`git commit --no-verify`) and are not installed on a fresh clone until someone
runs `pre-commit install`. Every blocking hook here has a CI counterpart that is
the real enforcement; the hook exists to save the round trip, not to be trusted.

**Rejected: a hand-rolled `.git/hooks/pre-commit` shell script.** Not shared by
clone, not versioned, no tool pinning, and every repo's copy drifts.

**Rejected: `lefthook`/`husky`.** `husky` is Node-first and most of this estate
is Python; neither is already present. `pre-commit` pins its own tool versions
in the config, which is what keeps a hook and its CI counterpart running the
same pylint.

---

## 6a. JS lint in pre-commit (addendum, 2026-11-04)

§6's hook set is Python-only, and [§11](#11-what-i-could-not-verify) scoped
the frontend repos out entirely. This section decides JS lint for repos whose
*primary* surface is Python but which carry a small frontend — concretely
`aw-app-template`'s `ui/`: 163 lines across `ui/src/client.js`,
`ui/src/plugin.js`, `ui/src/standalone.js`, plus `ui/vite.config.js`. The
dedicated frontend repos (`aw-console`, `aw-workspace-ui`, `aw-mobile`) still
need their own standard; nothing here claims to be it.

**Decision: ESLint (v10, flat config) with `@eslint/js` recommended rules
only, config at `ui/eslint.config.js`, run as a `language: system` pre-commit
hook shelling to `npm run lint --prefix ui`, blocking from day one. No
Prettier.**

Measured on 2026-11-04: ESLint 10.14.0 with `js.configs.recommended` plus
browser globals produces **zero findings** on the template's four JS files.
That measurement is why "blocking from day one" is safe here where §5 staged
Python's rollout: the Python caution addressed ~40 repos of unmeasured lint
debt; this is four files of measured zero.

Mechanics:

- `ui/eslint.config.js` — flat config, `@eslint/js` recommended; browser
  globals for `ui/src/**`, node globals for `vite.config.js`; `ui/dist/`
  ignored (committed build output, not source).
- `ui/package.json` — `eslint`, `@eslint/js` and `globals` as
  devDependencies (ESLint 10 line: `@eslint/js@10` requires `eslint@^10` —
  measured, an `eslint@9` + `@eslint/js@*` install fails to resolve), plus a
  `"lint": "eslint ."` script. `ui/package-lock.json` is committed, so the
  version truth lives in exactly one place.
- Hook: `language: system`, `pass_filenames: false`,
  `files: ^ui/.*\.(js|jsx|mjs|cjs)$` — a commit touching no UI file never
  runs it, so Python-only work (the overwhelming majority of commits in
  these repos) never needs `node_modules`.
- Per-clone setup gains one line next to §6's pip install:
  `npm install --prefix ui` (only needed if you will touch `ui/`).
- CI counterpart (the §6 invariant that pre-commit is never the only place a
  check runs): a lint step in the template's own
  `.github/workflows/test.yml` (`npm ci --prefix ui && npm run lint --prefix
  ui`). The shared `app-release.yml` stays untouched — this is per-repo
  configuration, exactly the [§2](#2-where-the-gate-lives) split.

**Rejected: the official `mirrors-eslint` hook (`language: node`).** It is
the shape that matches §6's "pre-commit pins its own tool versions", and it
was the first draft here. It loses on a mechanical point: flat configs
*import* their plugins (`import js from '@eslint/js'`), and Node resolves
that import from the config file's location in the repo tree — not from
pre-commit's hermetic hook env where `additional_dependencies` installs
them. Making the mirror work means the repo needs `ui/node_modules` anyway,
at which point the mirror's one advantage is gone and its cost remains: the
eslint version pinned in two places (hook env vs `package.json`), drifting
silently. `language: system` plus a committed lockfile has one version truth
and matches the pylint hook's existing precedent — tool installed by a
documented per-clone setup step, hook running the same command as CI.

**Rejected: Prettier.** Three reasons, descending: (1) formatting churn —
the template's own Python measured 12 of 13 files non-format-clean the day
the formatter arrived (§6b); introducing a JS formatter to 163 readable
lines buys the same whole-file-reformat problem for nothing; (2) §6's "two
linters is a standing tax" applies to hook count too; (3) there is no JS
formatter precedent anywhere in the estate to stay consistent with. Revisit
when the real frontend-repo standard is written.

**Rejected: report-only for the first landing.** In CI, report-only output
lands in a job summary someone can read; a pre-commit hook that prints and
exits 0 is invisible noise. With a measured-clean tree there is nothing to
stage.

## 6b. Two amendments to the §6 hook set (2026-11-04)

**ruff-format: on from day one in repos that are format-clean; otherwise off
until a dedicated format commit.** The `aw-app-kb` pilot shipped with `ruff
format` deliberately disabled (`repos/aw-app-kb/.pre-commit-config.yaml:11-19`):
that codebase had never been formatted, and ruff-format rewrites whole
files, so the first 2-line fix to any file ballooned into a 336-line diff.
That reasoning is correct and stays. For `aw-app-template` the same
measurement says 12 of 13 Python files would reformat — but the template is
13 files at a verified 100% coverage (43 tests green, re-measured
2026-11-04), so the one-time `ruff format` commit the kb header asks for
costs minutes and is proven safe by the suite. Do it in the same delivery
and enable the hook. The payoff compounds: every future `aw-app-*` is born
format-clean, which retires the kb caveat for the entire future estate
instead of re-litigating it per repo.

Related: ruff's *default* rule set drifts across versions — the kb hook's
pinned `v0.9.9` and current `0.17.3` disagree on what to flag (measured:
0.17.3 defaults add ASYNC/PLW/SIM/EXE/RUF-class findings that 0.9.9 never
raised). A repo relying on defaults changes gate semantics on every hook
`rev` bump. So the template's `[tool.ruff.lint]` pins `select` explicitly
(defaults-equivalent `E4,E7,E9,F` plus `I` for import sorting), making a
rev bump a no-op until someone deliberately widens the list.

**gitleaks joins the hook set for `aw-app-*` repos.** Evidence, not
nice-to-have: (1) every `aw-app-*` repo is required to be public by the
marketplace release path, so a committed secret is published the moment it
is pushed; (2) the template is cloned, so anything committed to it
multiplies into every future app; (3) the estate already chose gitleaks —
`aw-app-template/.github/workflows/security-scan.yml` runs the OSS CLI
(v8.30.1) in CI, deliberately redundant with Trivy's secret scanner. But CI
runs post-push: on a public repo, by the time it fires the secret is an
incident, not a catch. The pre-commit hook is the only placement that is
*prevention*. Same tool as CI (the official `gitleaks` hook, pinned to the
same major) — **rejected: `detect-secrets`**, which would add a second
secrets vocabulary, a committed baseline file to curate, and nothing the CI
scanner doesn't already understand.

One hook deliberately NOT added, left as a named candidate: a **`ui/dist`
freshness check**. The dist is committed (`git ls-files ui/dist/` is
non-empty) and nothing anywhere gates its freshness — `test.yml`,
`release.yml` and the shared `app-release.yml` all have zero npm steps
(measured 2026-11-04) — and the estate has a recorded lesson that a
committed-dist repo needs exactly this gate. It clears the evidence bar but
not the lint card's scope (it is build integrity, not lint), and it needs
its own CI half; it should be its own small card.

---

## 6c. One correction to the §6/§6b hook set (2026-11-04)

**The ruff *hook* must not have `I` in its effective `select` while the
repo's import-order baseline is unclean — even though §6b correctly puts `I`
in `[tool.ruff.lint]`.** The two are not the same switch, and conflating them
is a live hazard rather than a tidiness point.

§6b pins `select = ["E4","E7","E9","F","I"]` so a hook `rev` bump can't widen
the gate. That is right, and it stays. But the §6 ruff hook also runs
`--fix`, and a pre-commit hook inherits `pyproject.toml`'s `select` — so in
any repo whose baseline still carries `I001` findings, the hook will
**silently reorder the imports of whatever file a commit happens to touch**.
Not as one reviewable sweep: one unrelated diff at a time, where it is easy
to re-stage without reading. pre-commit does rewrite the file and demand a
second `git add`, so it is not invisible — it is just not something a
developer should have to notice.

That is the same hazard CI already refuses to take. Every repo on this
standard keeps `ruff check` **report-only** in CI precisely because a blanket
`--fix` is unsafe on an unmeasured baseline — AP-MT tried it and reverted when
reordered imports broke 2 passing tests through SQLAlchemy listener
registration order. Leaving the hook unrestricted contradicts that decision
while pointing at the same rule.

So: override the hook's select to **`E4,E7,E9,F`** and leave `I` to CI's
report-only run.

```yaml
  - repo: https://github.com/astral-sh/ruff-pre-commit
    rev: v0.17.3
    hooks:
      - id: ruff
        args: [--fix, --select, "E4,E7,E9,F"]   # NOT pyproject's select: drops I
```

Verified behaviourally, not assumed: on a file with an unsorted *and* an
unused import, `--fix --select E4,E7,E9,F,I` reorders the imports and drops
the unused one; `--fix --select E4,E7,E9,F` drops only the unused one and
leaves import order alone.

**Applies to:** `aw-workspace` core (done — 50 of its 78 findings are `I001`,
concentrated in the import-order-sensitive `src/apps`). **Still outstanding
at the time of writing:** `agents-platform-multitenant` has the identical
combination (`select` includes `I`, hook is a bare `args: [--fix]`) against a
469-violation baseline including 245 import-sort findings — strictly more
exposed than core was. `aw-app-template` and any `aw-app-*` inheriting its
config need the same check. Those are not this card's to change; raised to the
Product Owner as separate work.

Lift this override per repo once that repo's import-order baseline is
genuinely zero — at which point the hook can keep it clean going forward,
which is the end state §6b wants.

---

## 7. What this makes harder later

Every decision above closes a door. These are the ones it closes.

1. **The shared workflow becomes a bigger single point of failure.**
   `app-release.yml` already gates 48 apps' releases. Adding a coverage step and
   a lint step to it means a mistake there — a bad pin, a tool that changes its
   exit codes — blocks all 48 at once. [§8.1](#81-phase-0--make-the-shared-workflow-safe-to-change)
   is the mitigation, and it is required, not optional.

2. **Per-repo `fail_under` is state that will drift from reality.** A baseline
   committed once and never revisited becomes a number nobody can justify. The
   ratchet fixes the direction, not the staleness — expect to need a periodic
   sweep that reports repos sitting far *above* their own `fail_under` (a repo
   at 89% with `fail_under = 82` has 7 points of silent regression licence).

3. **Two linters is a standing tax.** Every future rule question becomes "which
   tool owns this". [§5.3](#53-why-ruff-stays-and-stays-non-blocking) picks a
   tie-breaker, but if ruff ever grows an equivalent to astroid's inference,
   the honest move is to drop pylint and this document should be revisited
   rather than defended.

4. **Coverage percentage is a proxy, and gating on it makes the proxy the
   target.** A repo can reach 80% with tests that assert nothing (three apps
   already pass suites covering 0%; the inverse is just as reachable). The
   ratchet buys a floor against *deletion* of tests, not a guarantee of test
   quality. Mutation testing is the real answer and is explicitly out of scope
   here — but it is the successor question, and the estate's existing
   `aw-autoskill-qa-mutation-test-regression` practice is the seed for it.

5. **Adding CI steps costs wall-clock on runners that sit next to production.**
   Pylint is slow. On the largest packages the lint step may rival the test step.
   If that becomes the binding constraint, the escape hatch is running pylint
   only on changed files in CI too — which weakens the gate, and should be a
   decided trade-off rather than a quiet optimisation.

6. **`aw-app-kali-linux` and the five other package-less apps do not fit this
   standard at all.** They wrap stock images and have nothing to unit-test.
   Forcing them to comply produces ceremonial tests. They need an explicit
   `"testing": "exempt"` marker with a reason — and the moment that marker exists,
   it is something an app can quietly set to opt out.

---

## 8. Rollout

Not implementation instructions — a decomposition, in dependency order.

### 8.1 Phase 0 — make the shared workflow safe to change

**Before** any gate goes into `app-release.yml`. Two things:

- Fix the guard/run scope mismatch at `app-release.yml:135-137` so a repo with
  tests only under `tests/unit/` is not silently skipped — this standard will
  encourage exactly that layout.
- Give `aw-marketplace` a way to exercise `app-release.yml` against a real app
  repo before merging to `master`. Today the moving ref means `master` *is* the
  test.

### 8.2 Phase 1 — the template

Land in `aw-app-template`: `pyproject.toml` with `[tool.coverage.*]`,
`[tool.ruff]` and `[tool.pylint]` sections; `.pre-commit-config.yaml`;
`requirements-dev.txt` additions; README section. **Raise the template's own
coverage from 68% to ≥80% in this phase** — starting with `plugin.py` at 0%.
The reference implementation has to pass the standard it defines, or the first
question every app owner asks has no good answer.

### 8.3 Phase 2 — the shared gate

Coverage + pylint steps into `app-release.yml`, gated on the repo *having* the
config from Phase 1, so apps adopt on their own schedule rather than all breaking
on the merge.

### 8.4 Phase 3 — per-repo cards

One card per repo: measure, commit the baseline, add the config, fix what the
error-class lint finds. See [§10](#10-handoffs) for the list.

### 8.5 Phase 4 — Tier B

`aw-backend` (delete `--no-cov`, set `fail_under`), `agents-platform-multitenant`
(wire up the ruff it already configured, add pylint), `aw-workspace` core (needs
a `pyproject.toml` first — it has none).

**Progress, 2026-11-04.** Two of the three are done for *lint*; none for
coverage.

| Repo | Lint | Coverage |
|---|---|---|
| `agents-platform-multitenant` | **done** — `lint` job in `test.yml`, pylint `-E` blocking, ruff report-only (469 baseline). Also on the Tier-B hook set. | open — `--cov` is report-only, no `fail_under` |
| `aw-workspace` (core) | **done** — `pyproject.toml` (lint config only, no `[project]`), `.pre-commit-config.yaml`, `lint` job in `test.yml`. pylint `-E` blocking (tree measured clean, 1 real bug fixed), ruff report-only (**78 baseline, ruff 0.17.3 — see [§9](#9-how-to-reproduce-every-number-here) before re-measuring**). See [§1.3](#13-tier-b--three-repos-that-already-tried-and-stopped-short). | open — no baseline measured |
| `aw-backend` | not started | not started |

Both finished repos took the same shape, and it is the recommended one for
`aw-backend`: **pylint error-class blocking only after a real triage pass**
(fix what is a bug, disable what is a false positive *at the site, with the
reason*), and **ruff report-only until its baseline is cleaned by hand**. The
reverse order — gating on ruff first because it looks mechanical — is what
produced AP-MT's reverted `--fix` commit.

Unblocked by core's new `pyproject.toml`, and worth noting because the §1.3
entry listed it as the blocker: nothing else about core needed to change. The
file carries `[tool.ruff]` and `[tool.pylint.main]` and nothing else, and it
does **not** move pytest's rootdir (verified: still `src/tests`, `configfile:
pytest.ini`) — which matters, because `test.yml`'s `-k` vs `--deselect` comment
depends on node ids staying relative to `src/tests`.

---

## 9. How to reproduce every number here

```bash
# Per-app coverage (the §4.1 table)
cd /opt/aw-workspace/repos/<app>
python3 -m coverage run --source=<pkg>_app -m pytest tests/ -q
python3 -m coverage report

# The lint findings (§5.1) — pylint 4.0.8 in a throwaway venv
python3 -m venv /tmp/lintenv && /tmp/lintenv/bin/pip install pylint
cd /opt/aw-workspace/repos/agents-platform-multitenant/backend
/tmp/lintenv/bin/pylint app --disable=all --enable=E

# Estate-wide: which repos have what
cd /opt/aw-workspace/repos
for d in */; do r=${d%/}; printf "%-40s pre-commit=%s\n" "$r" \
  "$([ -f "$r/.pre-commit-config.yaml" ] && echo YES || echo no)"; done

# The core ruff baseline (the §8.5 table). PIN THE VERSION — see below.
docker run --rm -v /home/aw-remote-host/aw-workspace:/repo -w /repo \
  -e RUFF_CACHE_DIR=/tmp/rc python:3.12-slim sh -euc '
    pip install -q ruff
    python -m ruff --version
    python -m ruff check --select E4,E7,E9,F,I src/'
# -> ruff 0.17.3, 247 files: 78 = 50 I001 + 15 F401 + 6 E741 + 2 F841
#                               + 2 F541 + 2 E702 + 1 E731
```

**Run it from the repo ROOT, where `pyproject.toml` is. That is not a detail,
it changes the number.** Ruff's isort classifies first-party imports using the
project it discovers from `pyproject.toml`; with no `pyproject.toml` in scope
it classifies 4 more import blocks as unsorted and reports **82** instead of
78. Isolated by removing *only* `pyproject.toml` from an otherwise untouched
clone of this branch — `.git` and `.gitignore` left in place — and watching 78
become 82, with all four extra findings `I001`. So linting a `src/` tree copied
out on its own does **not** reproduce this baseline, and 78 is the
operationally meaningful figure because it is what CI measures: the `lint` job
runs inside the checkout, where `pyproject.toml` is present.

**Record the tool version AND the working directory next to a lint count.**
This number has now been wrong in the record three times, each time for a
different reason, and only the last one was interesting:

| Recorded | Why it was wrong |
|---|---|
| 86 | arithmetic — its own per-rule breakdown summed to 83 |
| 83 | real at the time; the single `F811` was then fixed |
| 82 | **measured against a detached `src/` copy with no `pyproject.toml`** |
| **78** | current, reproduced by the command above and independently by QA |

The per-rule breakdown is the cheap check and it must sum to the total — that
is what caught the 86. It would **not** have caught the 82, because 82 was
internally consistent; only re-running the documented command in the documented
place caught that one. Which is the actual lesson: a count is only as good as
the command recorded beside it, and a command is only reproducible if it names
its working directory.

Note the **host** path in the docker invocation. The daemon resolves binds
against the host filesystem, not this container's view, so `-v $PWD:/repo` from
`/opt/aw-workspace` silently mounts nothing and ruff reports `E902` on a
missing `src/` — which looks like a lint finding rather than a mount failure.

Caveats that will change the numbers: apps with a `requirements-dev.txt` need it
installed (`aw-app-crispal` fails collection without `PIL`); CI runs Python 3.11
for Tier A and 3.12 for core, and this measurement ran 3.12 throughout.

---

## 10. Handoffs

**Repos needing an implementation card**, with measured current state, for the
Source to open in the next phase. Coverage figures are from
[§4.1](#41-the-measured-baseline).

*Tier A — shared release workflow, has tests, no coverage/lint/pre-commit gate.*
Grouped by effort to reach a 50% floor:

| Group | Repos | State |
|---|---|---|
| **Already ≥80%** (baseline-only card) | presentations 89, mobile 83, diff-tool 82, notion 80, remote-host-cli 80 | commit baseline, add lint + pre-commit |
| **70–79%** (small lift) | aws 79, google-cloud 79, feed-subscriber 76, essentials 73, plaud 72, ssh 70 | as above |
| **50–69%** (moderate) | git 69, mcp-tools 69, mini-browser 68, **template 68**, remote-host-terminal 68, tunnel 67, remote-screen 64, agents-platform-runners 63, portrait 63, call-agent 62, secrets 58, codegraphcontext 57, code-agent-clis 56, android-studio 54, devctl 53, whiteboard 53 | template is Phase 2 and comes first |
| **Below the 50% floor** (real work) | kb 48, travel 48, weather 48, google-maps 45, tasks 40, architecture 34, whatsapp 34, windows-pilot 34, google-workspace-mcp 28, proxy 27, roblox 26 | needs tests written, not just config |
| **Green suite, 0% package coverage** | agents-platform-test-fixtures, devteam, maintenance-agents | tests pass and cover nothing — diagnose before setting a baseline |
| **Unmeasured** | crispal | needs `requirements-dev.txt` installed; measure first |
| **No package / no tests — needs an exemption decision, not a card** | blender, browser, code-server, home-assistant, kali-linux, signoz | container/desktop wrappers |

*Tier B — bespoke CI.*

| Repo | State |
|---|---|
| `aw-backend` | 251 test files; coverage fully configured, `fail_under = 0`, `--no-cov` in CI ×3. Delete the switches, set a baseline. No lint. |
| `agents-platform-multitenant` | 343 test files; `[tool.ruff]` + `ruff>=0.7` configured, **never invoked**. Wire it up, add pylint `-E`, add coverage. |
| `aw-workspace` (core) | Serious test job (ephemeral PG + Redis). Lint **done 2026-11-04** (`pyproject.toml`, pre-commit, blocking pylint `-E`, report-only ruff at a 78 baseline). Coverage still unmeasured — that is what remains of this row. |
| `aw-mcp-gateway` | 13 test files, 3 workflows. Unmeasured. |
| `aw-marketplace` | Owns `app-release.yml` — Phase 0 lands here. 5 test files. |
| `aw-console`, `aw-workspace-ui`, `aw-mobile` | Frontend; this standard is Python-shaped and does not cover them. Needs its own decision. |
| `aw-remote-host`, `aw-stack` | 0 test files each. |
| `aw-vault`, `aw-automation` | 2 and 6 test files. Unmeasured. |

**Two items that are not this card's, routed explicitly:**

- **A production bug, to the Coders/Debugger:** `run_url` is undefined at
  `agents-platform-multitenant/backend/app/core/wakeups.py:899` and `:957`,
  inside swallowed `except Exception` handlers, which means the sysadmin alert
  for a rejected Kanban move has never fired. Plus `E1125` at
  `app/core/models/__init__.py:126`. Found while grounding [§5](#5-lint);
  deliberately not fixed here.
- **A scope question, to the Product Owner:** the floor schedule in
  [§4.2](#42-the-decision) (50% → 65% → 80% over 6 months) sets delivery
  expectations across ~40 repos. Those dates are a product call.

---

## 11. What I could not verify

- **No Tier B coverage baseline.** `aw-backend`, AP-MT and core need a real
  Postgres and Redis to run their suites. Their baselines are unknown and
  measuring them is step 1 of their own cards — do not assume they resemble the
  app numbers.
- **No CI timing measurement.** [§7.5](#7-what-this-makes-harder-later) asserts
  pylint is slow enough to matter on shared runners. That is an inference from
  package sizes, not a measurement, and Phase 0 should measure it before the
  lint step goes into the shared workflow.
- **Frontend repos are out of scope entirely.** `aw-console`,
  `aw-workspace-ui` and `aw-mobile` need a separate standard (vitest/eslint);
  nothing in this document applies to them.
- **`aw-app-crispal` was never measured.** See [§4.1](#41-the-measured-baseline).
- **The pylint findings in [§5.1](#51-what-the-error-class-already-found) were
  read and confirmed in the source; the rest of pylint's output on other repos
  was not.** Expect false positives when the gate first runs — budget for a
  triage pass per repo rather than assuming a clean bill.

---

## 12. Shared-workflow drift auto-PR (addendum, 2026-11-06)

**Decision: a content-aware daily scheduled workspace task — not a GitHub
Actions workflow, not Dependabot — diffs each consumer's pinned
`fredericowu/aw-shared-workflows` file against the latest tag's content, and
auto-opens a one-line bump PR only when the §2a caller contract is unchanged.
No new secret, no new GitHub-side infra: it runs under the workspace's own
ambient admin `gh` identity, the same one every other Dev Team delivery in
this estate already uses.**

This is §2a's enforcement sibling: §2a decided *what* a caller must grant;
this section decides *how staying current with the shared file's content* is
kept from becoming either manual toil or a PR flood.

### 12.1 The decisive measurement

Measured 2026-11-06, with `aw-shared-workflows` at `v1.6.0`: **22 consumer
repos pin one of its three reusable workflows, 25 pins total, ranging
`v1.2.0`→`v1.6.0`.** Content-diffing every pinned *file* between its pin and
`v1.6.0`: `ai-review-reusable.yml` is byte-identical since `v1.2.0`,
`code-review-reusable.yml` byte-identical since `v1.5.0`. **24 of 25 pins are
tag-stale but content-current; exactly one is real drift** — `aw-stack`'s
`code-review-reusable.yml@v1.2.1`, ~1,100 lines behind.

This is the direct, unavoidable consequence of the deliberate design choice
in [the repo's own README](../../repos/aw-shared-workflows/README.md) to run
one `v*` tag line across three independent workflow files, rather than a tag
family each (a per-workflow family would be unprotected by the immutability
ruleset, which only targets `refs/tags/v*`). A fix to one workflow cuts a tag
every consumer of the *other two* is, by tag number, instantly "behind" —
with nothing to actually bump. **Any drift detector keyed on tag number
instead of content generates a mostly-no-op PR flood on every single tag
cut, forever.** Content-diffing is therefore not an optimization on top of
the design; it is the design.

### 12.2 Mechanism

Runs daily as a scheduled Agents Platform agent (`shared-workflow-drift-checker`
in `tekflox/aw-app-maintenance-agents`), the same shape as this workspace's
existing `system-analyst` / `kanban-sweeper` scheduled tasks — see the
`aw-shared-workflow-drift-checker` skill for the exact steps. Per run:

1. **Discover** the latest `v*` tag, and every consumer **live**, via `gh api
   search/code` for `uses: fredericowu/aw-shared-workflows` across both the
   `tekflox` and `fredericowu` orgs — not a committed manifest (goes stale the
   moment the fleet migration adds a consumer) and not local checkouts under
   `repos/` (misses every repo that was never cloned here).
2. **Compare by content, not tag number**: for each pin `<file>@vX`, diff the
   file between `vX` and latest inside a local `aw-shared-workflows`
   checkout. Byte-identical → skip, silently — this is the 24-of-25 case
   above and is the expected, majority outcome on every run.
3. **On real drift, classify against the §2a contract** — the shared file's
   `permissions:`, `workflow_call.inputs:` and `workflow_call.secrets:`
   blocks, the exact checklist
   `test_the_reusable_requests_every_scope_the_caller_checklist_demands`
   ([§2a](#2a-a-reusable-workflow-caller-must-grant-checks-read-addendum-2026-11-04))
   already machine-compares:
   - **Unchanged** → auto-open a mechanical bump PR (branch
     `devteam/wfbump-<tag>-<slug>`, one-line `uses:` change, body embeds the
     diffstat), opened under the admin identity so the consumer's own gates
     (test, ai-review, code-review loop) fire normally — **not** a
     `GITHUB_TOKEN` Actions-bot PR, which would not trigger them.
   - **Changed** → do not blind-bump. Open a Kanban card routed to a coder,
     citing this section, to bump the pin **and** edit the caller's own
     `permissions:`/secrets together.
4. **Never merges, never touches a tag or branch inside `aw-shared-workflows`
   itself** — merge authority stays with each consumer's own PR-first
   review/merge loop.
5. **Capped at ~10 PRs per run, staggered across days** — the self-hosted
   runner and the GitHub rate limit both argued for this after the
   mass-rollout burn on an earlier security-scan rollout (see
   [§7](#7-what-this-makes-harder-later)'s sibling concerns). At ~22
   consumers today, one tag cut drains in ≤3 days; at the full ~60-repo
   fleet, about a week. Tag cuts are rare and bumps are not urgent, so this
   is an acceptable latency, not a gap — and the checker's own report names
   whatever it deferred to the next run rather than letting a partial sweep
   read as complete.

**Does not special-case an already-open PR on the same repo.** When a repo
is already getting touched by some other PR (e.g. a fleet-migration batch),
the preferred path is for *that* PR to bump its own shared-workflow pin
inline, rather than waiting on this checker — a convention for other
dispatches to follow, not logic this checker implements. This checker is the
fallback for repos nothing else is touching, not the only path.

### 12.3 Rejected alternatives

- **Dependabot (`github-actions` ecosystem, grouped).** The standard answer,
  and the runner-up. Loses on two structural points: (1) it is tag-number
  based, so under the single `v*` tag line it would open ~24 no-op PRs
  *today* and repeat that on every future cut; (2) Dependabot-actor runs get
  restricted secrets and need org-level enablement on self-hosted runners,
  which would break the ai-review/code-review loop on exactly the PRs it
  opens. Reconsider only if the tag-line design itself ever changes to
  per-workflow families.
- **Tag-push fan-out workflow inside `aw-shared-workflows`.** Event-driven,
  nicer latency — but needs a cross-org PAT with write access to ~60 repos
  stored as a repo secret, a brand-new standing credential surface, plus
  consumer discovery from inside Actions and real tag cuts to test it. Daily
  polling from the workspace gets the same outcome with zero new secrets.
  Could be added later as a thin trigger that just pokes the same workspace
  checker, if latency ever becomes the actual complaint.
- **Moving major alias (`@v1`).** Explicitly violates the repo's own
  invariant ("not a moving major alias") and the immutability ruleset only
  protects exact `v*` tags, not an alias. Dead on arrival.

### 12.4 What this makes harder later

A second automation writing to consumer repos under the same admin identity
makes attribution muddier when diagnosing *why* a PR appeared. The §2a
checklist must stay machine-parseable forever — the existing shared-repo test
is the only thing keeping §12.2 step 3's classification honest. `gh` code
search will rate-limit as the fleet grows past ~60 repos, mitigated for now
by the per-run cap. A consumer that hand-edits its own caller file while a
bump PR from this checker is open gets a merge conflict, not a merge —
acceptable, since PRs here are cheap to re-cut.

## 13. A committed `ui/dist` needs its own CI gate (addendum, 2026-11-07)

**Decision: any `aw-app-*` repo that commits a built `ui/dist` adds a
`check-dist` job to its own `release.yml` and gates `release` on it with
`needs: check-dist`.** Without it the repo can release, marketplace-update
and pass code review while shipping a byte-identical **stale** frontend
bundle, because the shared `app-release.yml` (§2) is Python-only and never
runs `npm run build`.

This is the frontend counterpart to §2's gate: §2 decides what the shared
workflow enforces for Python; this decides what a repo must enforce locally
for a bundle the shared workflow cannot see.

### 13.1 The decisive measurement

On 2026-09-27 `aw-app-tasks` and `aw-app-presentations` each had the same
`plugin.jsx` fix released and marketplace-updated **twice**, and neither
release ever reached a browser. `apps/<slug>/aw-app.json`'s
`contributes.frontend.bundle` points at `ui/dist/*` and `routes.py` serves it
straight off disk, so the stale committed bundle is what ships. A QA session
caught it by diffing the **installed** dist against the guard string it
expected — not by reading `ui/src/plugin.jsx` (correct) and not from the
release run (green).

Exposure check for any repo:

```bash
cd repos/aw-app-<slug>
[ -d ui/dist ] && echo "dist tracked — exposed unless gated"
grep -l check-dist .github/workflows/*.yml 2>/dev/null || echo "no gate yet"
```

As of 2026-09-28 only `aw-app-tasks` and `aw-app-presentations` had the gate.
Confirmed exposed then (committed `ui/dist`, no `check-dist`), 17 repos:
`aw-app-architecture`, `aw-app-call-agent`, `aw-app-code-agent-clis`,
`aw-app-devctl`, `aw-app-diff-tool`, `aw-app-feed-subscriber`,
`aw-app-mcp-tools`, `aw-app-mini-browser`, `aw-app-mobile`, `aw-app-plaud`,
`aw-app-portrait`, `aw-app-remote-host-terminal`, `aw-app-remote-screen`,
`aw-app-research-search`, `aw-app-template`, `aw-app-uc-phd`,
`aw-app-whiteboard`. Re-measure before acting — that list is a 2026-09-28
snapshot, not a contract (§9). `aw-app-kb` also commits a `ui/dist` but is
Tier-2/container-built; check its own publish pipeline rather than assuming
this gap applies verbatim.

### 13.2 The job (verified live in `aw-app-tasks` and `aw-app-presentations`)

```yaml
  check-dist:
    # Same skip condition as app-release.yml's own `release` job — the bot's
    # own version-bump commit re-triggers this push event and never touches
    # ui/, so there is nothing to check.
    if: ${{ !contains(github.event.head_commit.message, '[skip release]') }}
    runs-on: ubuntu-24.04
    steps:
      - uses: actions/checkout@v7
      - uses: actions/setup-node@v5
        with:
          node-version: 24
      - name: Rebuild ui/dist and diff against the committed copy
        run: |
          set -euo pipefail
          cd ui
          npm ci --cache "$RUNNER_TEMP/npm-cache"
          npm run build
          cd ..
          if ! git diff --exit-code -- ui/dist; then
            echo "::error::ui/dist does not match a fresh build of ui/src — commit the rebuilt bundle (cd ui && npm run build) before pushing."
            exit 1
          fi
```

Then on the existing job:

```yaml
  release:
    needs: check-dist
```

Pins above match the two live gated repos as of 2026-11-07
(`actions/checkout@v7`, `actions/setup-node@v5`, `runs-on: ubuntu-24.04`) —
re-read one of them rather than copying this verbatim once it has aged (§9).

Simulate before pushing — `rm -rf ui/node_modules && npm ci && npm run build
&& git status --short -- ui/dist` should come back clean if source and
committed dist already agree. Watch the first real `check-dist` run
(`gh run view <id> --log | grep -A5 check-dist`) rather than assuming it
fired.

### 13.3 Verifying one specific fix shipped

Don't trust source or CI alone:

```bash
grep -o "<your guard string>" apps/<slug>/ui/dist/*.js
# or the live served bundle:
curl -s http://127.0.0.1:9030/api/apps/<slug>/ui/<bundle>.js | grep -o "<guard>"
```

Present in `ui/src/plugin.jsx` but missing from `ui/dist` means the fix never
shipped: rebuild (`cd ui && npm ci && npm run build`), commit the diff,
re-release. Edit `repos/aw-app-<slug>` — `apps/<slug>` is an installed copy,
not the source.

Added 2026-11-07 from
`aw-autoskill-app-committed-dist-needs-check-dist-gate`, card
`4115bf3b-9510-81fb-8509-de540f23bca0`, target
`aw-autoskill-registry-sync-triage`.
