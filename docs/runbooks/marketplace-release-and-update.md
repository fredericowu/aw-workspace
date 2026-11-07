# How to release an app version and actually get it installed

**Verified on:** 2026-11-07 (distilled from
`aw-autoskill-marketplace-release-cdn-lag`, see
[Provenance](#provenance)).
**Scope:** the gap between "my app's release workflow went green" and
"`aw-workspace-cli marketplace install <app> --update` picks up the new
version". Two separate causes live in that gap, they look identical from the
install side, and only one of them is fixed by waiting.

**The headline:** `raw.githubusercontent.com` serves `tekflox/aw-marketplace`'s
`apps.json` from a CDN edge with roughly a **5-minute TTL**. The new version
is genuinely in the repo; it is not visible at that URL yet. Two independent
sessions on 2026-09-04 both lost real time to this — one spent ~17 tool
calls — suspecting the release or the install command.

---

## 1. Don't debug the install path. Ask what the CDN is serving.

```bash
curl -s https://raw.githubusercontent.com/tekflox/aw-marketplace/master/apps.json \
  | python3 -c "import json,sys; d=json.load(sys.stdin); print(next(a['version'] for a in d['apps'] if a['id']=='<app-slug>'))"
```

Two details that cost two more sessions several tool calls each:

| Thing | Value | Not |
|---|---|---|
| branch | `master` | `main` |
| per-app key | `id` (e.g. `"id": "git"`) | `slug` — it does not exist anywhere in this JSON, and a `next(...)` filter on it raises a bare `StopIteration` |

If that still prints the **old** version after your release reported success,
confirm the release itself actually landed before you start waiting on the
CDN:

```bash
gh api repos/tekflox/aw-marketplace/contents/apps.json   # or a local git log
```

## 2. Poll the catalog, don't re-run `--update` blindly

```bash
for i in $(seq 1 20); do
  v=$(curl -s https://raw.githubusercontent.com/tekflox/aw-marketplace/master/apps.json \
    | python3 -c "import json,sys; d=json.load(sys.stdin); print(next(a['version'] for a in d['apps'] if a['id']=='<app-slug>'))")
  echo "attempt $i: $v"
  [ "$v" = "<expected-version>" ] && break
  sleep 15
done
```

Only once the raw catalog reflects the new version does `install --update`
have anything new to pull. Running it in a tight loop before that
re-confirms the stale version and changes nothing.

## 3. After install, verify the local manifest changed

```bash
grep -m1 '"version"' apps/<app-slug>/aw-app.json
```

If this still shows the old number **after** the catalog check passed, *then*
something in the install path is genuinely broken and worth debugging — but
not before, because CDN lag accounts for the overwhelming majority of
"`--update` did nothing" reports.

Restart the app if it does not auto-restart:

```bash
aw-workspace-cli restart <app-slug>
```

## 4. The sibling failure: the catalog-sync PR itself can 500

Different cause, identical symptom from the install side. Seen releasing
`aw-app-kali-linux` v0.13.0: the app's own CI reported build + test + tag
green, but the step that opens the PR against `tekflox/aw-marketplace` to
bump the catalog entry **failed silently with a 500**. The new version never
landed in the marketplace at all, so no amount of polling helps.

The §1 check still catches it — the version string simply never changes, even
after minutes — but the fix is different:

```bash
gh pr list --repo tekflox/aw-marketplace
```

- PR exists but was not auto-merged → merge it manually, then re-run
  `marketplace install <app-slug> --update`.
- **No PR at all** → the release workflow's catalog-sync step needs
  re-triggering. Do **not** just re-release the app's own repo: build and tag
  already succeeded there, and re-running them will not touch the broken
  catalog-sync step.

## 5. Which cause do I have?

| Catalog version after ~5 min | Diagnosis |
|---|---|
| updated | it was CDN lag; proceed to §3 |
| still old, **and** a marketplace PR exists | §4 — merge it |
| still old, **and** no PR exists | §4 — re-trigger catalog sync |
| updated, but `apps/<slug>/aw-app.json` still old | genuine install-path bug; now debug it |

---

## Provenance

Consolidated on 2026-11-07 from the `aw-autoskill` tenant store, card
`4115bf3b-9510-81fb-8509-de540f23bca0` (target
`aw-autoskill-registry-sync-triage`), WP2b-i:

- `aw-autoskill-marketplace-release-cdn-lag` → all sections
