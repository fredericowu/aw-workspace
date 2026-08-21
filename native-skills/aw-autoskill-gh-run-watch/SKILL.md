---
name: aw-autoskill-gh-run-watch
description: Waiting on a GitHub Actions run (e.g. after `gh workflow run "Deploy aw-backend"`) — use `gh run watch <run-id> --exit-status`, not a hand-rolled `until [ "$(gh run view ... --jq .status)" = completed ]; sleep N; done` loop.
auto_generated: true
generated_at: 2026-08-21T03:02:55Z
evidence_sessions: [6d3c6cd5-6fed-40f5-a5e9-064f6e962a57]
---

# gh run watch, not a hand-rolled polling loop

Every deploy in this workspace that goes through `gh workflow run "Deploy
<app>"` (aw-backend's deploy is `workflow_dispatch`-only — see the
`aw-backend-deploy-is-manual` memory) needs the caller to then wait for that
run to finish before declaring success. The same six-line polling loop shows
up verbatim, repeated **6 times in a single session** in this scan and in
at least 16 sessions historically:

```bash
# DON'T — reinvents what gh already does, burns a full 30s+ sleep tick every poll
until [ "$(gh run view $RUN_ID --json status --jq .status 2>/dev/null)" = "completed" ]; do
  sleep 30
done
gh run view $RUN_ID --json status,conclusion --jq '"\(.status)/\(.conclusion)"'
```

`gh` already ships the blocking wait as a first-class command:

```bash
# DO — blocks, streams live step progress, and gh run watch's own exit
# code IS the pass/fail signal (no separate conclusion check needed)
gh run watch "$RUN_ID" --exit-status && echo DEPLOY_OK || echo DEPLOY_FAILED
```

`--compact` trims the output to just the failed/relevant steps if the full
step-by-step log is too noisy to read.

## The one wrinkle: getting `$RUN_ID` right after triggering

`gh workflow run` doesn't print the run id it just created — the API needs a
moment to register the dispatch, so grabbing it via `gh run list` can race.
Retry briefly instead of trusting the first read:

```bash
gh workflow run "Deploy aw-backend"

RUN_ID=""
for i in $(seq 1 10); do
  RUN_ID=$(gh run list --workflow "Deploy aw-backend" --limit 1 --json databaseId,createdAt --jq '.[0].databaseId')
  [ -n "$RUN_ID" ] && break
  sleep 2
done

gh run watch "$RUN_ID" --exit-status && echo DEPLOY_OK || echo DEPLOY_FAILED
```

If you already have the run id from `gh run list`/`gh api` output (e.g. you
triggered it earlier in the same session and it's in scrollback), skip
straight to `gh run watch`.

## When NOT to use this

`gh run watch` blocks the whole tool call until the run finishes — for a
long deploy (aw-backend's has run 5-8+ minutes in observed sessions) that's
fine, it's what you want. If you specifically need to keep working on
something else while a run is in flight and check back later without
blocking, use `gh run view $RUN_ID --json status --jq .status` as a single
non-blocking poll — just don't wrap it in your own sleep loop when a blocking
wait is what you actually want.
