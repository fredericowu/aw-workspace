# How to wait for something that takes longer than one turn

**Verified on:** 2026-11-07 (distilled from four `aw-autoskill-*` skills, see
[Provenance](#provenance)).
**Scope:** any wait an agent session has to survive — a CI run, a deploy, a
multi-hour ingest, a backgrounded shell command. Covers which mechanism to
pick, the four ways a wait silently dies here, and how to recover one that
already did.

The failure this document exists to prevent is always the same shape: the
wait looks armed, nothing reports an error, and the thing you were waiting
for finished (or died) with nobody listening.

---

## 1. Pick the mechanism

| You are… | Waiting on… | Use |
|---|---|---|
| an Agents Platform agent | a CI run / deploy, ≤10 min | `run_monitor_sync` — blocks in the same tool call, so there is no gap for the next turn to narrate instead of act on |
| an Agents Platform agent | a long external command, >10 min | `run_monitor_async` (wakes you on the exit code) |
| an Agents Platform agent | nothing in particular, just later | `schedule_wakeup` — **with a non-empty `prompt`** (see §4) |
| a Claude Code CLI session | a long command | `Bash` with `run_in_background: true`, or the `Monitor` tool pointed at the command itself |
| anyone | a GitHub Actions run | `gh run watch <id> --exit-status` as the command (§2) |
| anyone | a job that must outlive the session | `docker exec -d` into a persistent container, then poll (§5) |

Two rules cut across the whole table:

- **Point the backgrounding mechanism at a blocking command, never at a
  polling loop you wrote.** Wrapping `while true; do gh run view …; sleep N;
  done` in `Monitor` is the same anti-pattern as hand-rolling the loop, just
  moved to a different tool; one live session produced 8+ near-identical
  `status=queued jobs=[]` notification turns that way.
- **A sentence describing a background wait, with no tool call behind it, is
  not a wait.** The Agents Platform executor has no hook into the harness's
  own native `Monitor` tool or a backgrounded `Bash` child; a turn that ends
  on "I'll wait for the watcher to notify me" strands the run until a human
  or a supervisor notices.

## 2. Waiting on GitHub Actions

`gh` already ships the blocking wait, and its own exit code **is** the
pass/fail signal:

```bash
gh run watch "$RUN_ID" --exit-status && echo OK || echo FAILED
```

- `--compact` trims the output to the failed/relevant steps.
- `--interval 30` on a long run: the default polling interval can trip
  GitHub's secondary rate limit.
- `gh workflow run` does **not** print the run id it created, and the API
  needs a moment to register the dispatch — retry the `gh run list` read a
  few times rather than trusting the first one:

```bash
gh workflow run "Deploy aw-backend"
for i in $(seq 1 10); do
  RUN_ID=$(gh run list --workflow "Deploy aw-backend" --limit 1 \
             --json databaseId --jq '.[0].databaseId')
  [ -n "$RUN_ID" ] && break
  sleep 2
done
gh run watch "$RUN_ID" --exit-status
```

**The Bash tool's hard 600s cap.** Even with an explicit `timeout: 600000`,
a `gh run watch` still blocking at 10 minutes is **auto-backgrounded**, not
errored — you get a background task id and an output file instead of a
pass/fail signal. For a workflow known to run long, background it from the
start. If it got auto-backgrounded anyway, wait on that background task or
re-check with `gh run view`; do not fall back to a hand-rolled poll.

**A run stuck `queued` with an idle runner is often GitHub's outage, not
yours.** Check before restarting a runner or re-dispatching:

```bash
curl -s https://www.githubstatus.com/api/v2/status.json \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["status"]["description"])'
```

A genuine Actions incident on 2026-08-26 stalled queued runs workspace-wide;
one session checked the status page and moved on, another restarted the
runner service twice and re-dispatched three times first.

## 3. Chaining two waits into one wake-up

`run_monitor_async` takes a shell command, so two sequential watches fit in
one call and wake you once, after both finish:

```
run_monitor_async(command='gh run watch <ci_id> --exit-status; ci=$?; \
  gh run watch <release_id> --exit-status; exit $((ci || $?))')
```

## 4. The four ways a wait dies quietly

| Symptom | Cause | What to do |
|---|---|---|
| `run_monitor_async` fails in ~0.2s with `401 unauthorized`, while `run_agent_async` keeps working | a runner-lookup collision at the dispatch layer | retry **once**; on a second instant-401 fall back to a synchronous `gh run watch` or a `schedule_wakeup` self-timer. Do not keep hammering the same path, and do not read the 401 as your own work having failed |
| `run_monitor_async` armed, no callback ever arrives, even on success | an internal cap around ~1h of polling | after ~1h of silence, check the underlying run directly (`gh run view <id>`). "No notification yet" is not "still running" |
| `run_monitor_async` returns `exit_code=None` with no output while the process is demonstrably alive | a third, distinct failure mode on the same tool | verify with `docker exec … ps` / a log tail. An empty result is not proof the job is done or dead |
| a backgrounded `Bash` watcher simply vanishes, no completion notification | the agent's own sandbox container was recreated (§5) | `gh run view` the run directly, then re-arm — ideally via `run_monitor_async` rather than another detached child |
| `schedule_wakeup` appears to arm but nothing fires | called with `stop=false` and **no `prompt`** — rejected client-side in-turn (`prompt is required when stop is not true`) | always pass a non-empty `prompt`, and handle the rejection rather than ending the turn on it |

`run_monitor_async`'s runner-monitor image is also **thinner than the agent
image** — a command importing `httpx` failed there with
`ModuleNotFoundError`. Treat it as safe for plain shell commands only.

## 5. Launching a job that must outlive the turn

**The agent's own sandbox container is recreated between turns**, and it
takes every detached child with it. A `setsid nohup` job launched there died
at 28 minutes with no `done:` line; the tell was `ps -p 1 -o pid,etime`
showing a PID 1 only 50 seconds old.

```bash
# check before trusting a previously-detached job: near-zero uptime means
# the container was just recreated and that job is already gone
ps -p 1 -o pid,etime
```

Launch instead into a persistent container, logging somewhere both sides can
read:

```bash
docker exec -d aw-remote-host-workspace sh -lc \
  'nohup python3 … > /opt/aw-workspace/.tmp/<job>.log 2>&1 &'
```

One such job ran 4h54m unattended to completion. **But no launch target here
is immune**: a ~12h harvest and a pgvector ingest, both launched exactly this
way, died anyway when `aw-remote-host-workspace` itself was recreated around
01:00 and rebuilt the app's venv with a dangling `python3` symlink — every
subsequent `docker exec` into that venv failed silently. Diagnose by
comparing the symlink's target against a working interpreter, and repoint it
rather than rebuilding the venv if major.minor still matches:

```bash
docker exec aw-remote-host-workspace sh -c "ls -la /path/to/venv/bin/python3"
docker exec aw-remote-host-workspace sh -c "ln -sf /usr/bin/python3.12 /path/to/venv/bin/python3"
```

So: a multi-hour job needs a periodic `schedule_wakeup` + `docker exec … ps`
/ log-tail check, not a single fire-and-forget launch. When a job you
expected to be running has gone quiet, check container uptime and the venv's
python symlink **before** suspecting the job's own logic.

## 6. Confirming a long job actually finished

1. `ps -p <pid> -o pid,etime,cmd` — liveness, **and** whether more than one
   matching process exists. Two concurrent ingests against the same target
   have happened (an accidental duplicate), risking racing writes. Inspect
   each (log tail, start time) before killing one.
2. Compare a row/chunk count across **two checks a few seconds apart** and
   only declare done when they match — "stable at 5,526 across two checks 5s
   apart". A single snapshot can catch a job mid-write and look finished.
3. If `remote_host_exec_run`'s blocking variant starts returning empty or
   hanging mid-session (distinct from the known >60s hang), switch the rest
   of the loop to `remote_host_exec_start` + `remote_host_exec_status`
   instead of retrying the same blocking call. A one-line `echo ping` probe
   tells you which side is flaky.
4. **Anything unattended wraps every external call in its own `timeout N`.**
   A nightly disk-cleanup cron that shelled out to docker/podman without one
   stalled 3h25m on a single hung call, eating the whole night instead of
   finishing in its expected ~1 minute. There is no caller to lose patience
   on a cron's behalf.

## 7. Recovering a session already stranded on a dead wait

Fetch the result yourself (`gh run view` / `gh run watch`), then resume the
stranded session with `run_agent_async`, handing it the answer — don't wait
on its dead watch. **Quoting the stranded session's own last sentence back
to it** as the resume prompt orients it immediately without re-explaining
the situation.

Prioritise by what it was holding: a session stranded **with uncommitted
changes in a shared tree** is more urgent than one with a clean tree, because
that state is exactly what a concurrent writer's edits collide with — see
[`concurrent-writers-on-the-shared-tree.md`](./concurrent-writers-on-the-shared-tree.md).

---

## Provenance

Consolidated on 2026-11-07 from the `aw-autoskill` tenant store, card
`4115bf3b-9510-81fb-8509-de540f23bca0` (target
`aw-autoskill-registry-sync-triage`), WP2b-i:

- `aw-autoskill-agent-container-recycle-kills-background-job` → §5
- `aw-autoskill-babysit-long-running-background-job` → §6
- `aw-autoskill-ap-mt-native-monitor-strands-session` → §4, §7
- `aw-autoskill-gh-run-watch` → §2, §3

The conduct halves of the last two — "never end a turn on an unfinished
action", "point the mechanism at `gh run watch`, not a loop" — already live
in the `aw-agent-coder` role contract. This document is the operational
residue, kept here because it is looked up when a wait misbehaves rather
than read on every run.
