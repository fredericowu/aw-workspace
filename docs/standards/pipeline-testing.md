# Standard — pipeline testing, coverage, lint and pre-commit for `aw-*` repos

**Status:** design, written 2026-09-09. Nothing here is implemented yet.
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

**`aw-workspace` (core) — a serious test job, no quality gates.**
`.github/workflows/test.yml` stands up an ephemeral Postgres *and* Redis, joins
a `python:3.12-slim` container into the Postgres netns, and runs `pytest src/tests`.
The comments in that file are the best documentation in the estate of *why a
skipped test is a failure* ("a skipped test is how a card's whole VERIFY step
quietly stops running"). It has no coverage and no lint step, and the repo has
no `pyproject.toml` at all.

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
```

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
| `aw-workspace` (core) | Serious test job (ephemeral PG + Redis), no coverage, no lint, **no `pyproject.toml`**. |
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
