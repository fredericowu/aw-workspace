# How to recover when two sessions wrote the same tree

**Verified on:** 2026-11-07 (distilled from two `aw-autoskill-*` skills, see
[Provenance](#provenance)).
**Scope:** the repair side of concurrent writers on `/opt/aw-workspace` and
the repos under it. The *prevention* side is already conduct — `AGENTS.md`'s
"Dev Team: PR-first on this repo" block says never commit on the shared live
tree and isolate one worktree per card. This document is what to do when
that discipline was not followed, or when a sibling session turned up
anyway.

---

## 1. The three signatures, and which one you have

| What you see | What it is |
|---|---|
| An assertion/string/block referencing a name that exists nowhere else in that function or test | two concurrent `Edit` calls landed on the same file and one clobbered the other mid-edit |
| Files changed in `git status` that **you never touched** | an unrelated sibling session is live in the same tree |
| A half-written file with a real syntax or reference error, no other session involved | a single session's turn died on its own usage/session limit before it could commit |

The third is a solo failure mode with an identical symptom, and it was
re-diagnosed three separate times — each costing a full test run-and-fail
cycle — before anyone traced it back. Concretely: a `NetworkingTab.jsx`
calling a `refreshMesh` prop that was never defined anywhere in the file.

**Detect it immediately, don't wait for tests to fail.** Right after any
dispatched Coder's turn ends — normal finish, error, or usage cutoff — run
`git status` / `git diff` on the files it was meant to touch. A half-finished
edit with a dangling reference looks visibly different from a normal
in-progress diff, and catching it here is far cheaper than rediscovering it
several test runs later.

## 2. Before you re-implement a fix: check whether someone already landed it

Nothing is corrupted in this case; the cost is pure wasted work plus a
deploy race.

```bash
git log --oneline -5 -- <the file(s) you are about to touch>   # beaten to it?
gh run list; gh run view <id> --json headSha                   # stale build?
```

If the CI/deploy run you are polling was built from a SHA that predates a
newer fix commit, it is building stale code and waiting it out will not
help — re-point at the new commit or trigger a fresh run from HEAD. If you
already triggered a now-redundant second deploy, `gh run cancel` it rather
than letting two deploys of the same area race.

Two sessions independently diagnosed the same unbounded-version-floor bug on
the same card within minutes of each other; the one that ran `git log` and
`gh run view --json headSha` found the other's fix already pushed and
cancelled its own duplicate trigger.

## 3. Repair, cheapest technique first

**(a) The two changes are clean, non-interleaved hunks in the same file —
stage only your own.** No worktree needed:

```bash
git diff -- <file> | grep -n "^@@"     # find hunk boundaries
git diff -- <file> > /tmp/all.patch    # extract just your hunk from this
git apply --cached /tmp/your-hunk.patch
git add <files that are entirely yours>
git diff --cached --stat               # verify scope BEFORE committing
git diff --cached
```

This let one session commit its own change to a shared `models.py` without
touching or losing the other session's still-uncommitted work.

**(b) The overlap is inside shared logic, not adjacent hunks — un-mix into a
scratch worktree.**

1. `git status --porcelain=v1 -uall` — every touched file across both
   deliveries.
2. Per contested file, `git diff -- <file>` against a saved pre-dispatch copy
   to work out which lines belong to which delivery.
3. `git worktree add /opt/aw-workspace/.worktrees/<repo>/<scope> <branch>` and
   `cp` in **only** the files for the delivery you are isolating, not the
   whole tree.
4. Run that delivery's own tests/build inside the worktree, confirm it is
   clean on its own, then commit and push only from there.

**(c) You need a verdict about one specific commit on a concurrently-active
branch.** `git worktree add /tmp/qa-review-<x> <sha>` and run the suite
there, so the verdict is about *that commit* rather than about whatever else
is unstaged in the shared tree at review time.

## 4. Four rules that come out of the incidents

- **A full `Write` to a file in a shared tree is unsafe unless you read it in
  this same turn.** A stale full-file `Write` silently deleted a sibling
  session's already-committed model columns. Prefer `Edit` for anything in a
  tree another session might be touching.
- **Re-read the whole file, not just your own diff**, after any `Edit` on a
  file you suspect is contended. Do not assume your last `Edit` call is what
  is actually on disk.
- **Before deleting "dead code" absorbed from a concurrent session, re-check
  whether that session has since landed it for real.** One cleanup dispatch
  was sent to strip ~90 absorbed lines as presumed debris, checked first, and
  found the sibling had legitimately pushed that same code with its own
  tests — so it correctly left it alone.
- **When fanning out, dispatch at most one Coder per repository at a time.**
  One day hit this collision class 4+ times despite everyone involved
  already knowing about worktree isolation (`git add` published another
  agent's in-flight files; a rebase overwrote another agent's commit).
  Worktree isolation helps within a repo; serialising per repo avoids the
  cross-repo version of the same race before it reaches a single file.

## 5. One harness note

Do not reach for the harness's own `EnterWorktree` tool for this — it refuses
to run unless the user or `CLAUDE.md` explicitly asked for a worktree. The
manual `git worktree add` form has no such gate.

---

## Provenance

Consolidated on 2026-11-07 from the `aw-autoskill` tenant store, card
`4115bf3b-9510-81fb-8509-de540f23bca0` (target
`aw-autoskill-registry-sync-triage`), WP2b-i:

- `aw-autoskill-concurrent-coder-worktree-isolation` → §1, §3, §4, §5
- `aw-autoskill-concurrent-dispatch-duplicate-bugfix-race` → §2
