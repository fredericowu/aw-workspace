# Standard — an app's public / `auth_required` exposure

**Status:** shipped. Distilled 2026-11-07 from
`aw-autoskill-app-auth-required-routing` for Kanban
`4115bf3b-9510-81fb-8509-de540f23bca0`, target
`aw-autoskill-registry-sync-triage`.
**Scope:** any installed app whose HTTP surface needs to be reachable
without a workspace login — a login page that bounces to auth, a webhook, an
ingest endpoint — and anyone changing that setting. Sibling to
[`app-shared-state.md`](./app-shared-state.md) and
[`app-backend-websocket-messaging.md`](./app-backend-websocket-messaging.md)
in the app-framework family.

---

## 1. The rule

**An app's HTTP exposure is one flag on the whole app, not per route. If only
some of an app's routes should be public, do not touch the workspace-level
flag at all — push the auth decision down into the app's own front door.**

Flipping `public` / `auth_required` at the workspace-routing layer to fix one
route strips auth from **every** route that app exposes, including ones that
must never be open. Recorded instance: fixing an `aw-app-signoz` login bounce
that way exposed the unauthenticated OTLP ingest endpoints (`/v1/traces`,
`/v1/metrics`, `/v1/logs`) to the open internet. It was caught live with a
direct `curl -X POST https://<app>.app.aw.workspace.*/v1/traces` returning
200 with no credentials, and reverted.

SigNoz's actual fix is the shape to copy: an `envsubst`-templated
`nginx.conf` **inside the app's own container**, checking an API-key header
per `location` block — the UI path left open (SigNoz has its own login),
everything else gated.

### 1a. The one framework exception: `routes:public` + `public_paths`

For a path that a third party calls with **no workspace credential at all**
(a Shopify webhook: its only credential is an HMAC header the app itself
verifies), an app can open exactly that path and nothing else:

```json
"permissions": ["routes:register", "routes:public"],
"contributes": {"routes": [{
  "prefix": "/api/apps/shopify",
  "public_paths": ["/api/apps/shopify/webhooks"]
}]}
```

A path opens only when **all three** hold, checked at both enforcement
points (aw-workspace's `IdentityGuard`, aw-backend's tunnel edge):

1. `routes:public` is **granted** (declaring it is not enough);
2. the manifest names the **exact full path** — validated to sit strictly
   below the app's own `/api/apps/<id>/`, no wildcard, no `..`, no trailing
   slash, no mount root. Exact match, HTTP only (never a WebSocket), and only
   on the `api.<ws>` path form — the per-app subdomain stays gated;
3. the owner set the framework config flag **`public_paths_approved: true`**
   for that install (it appears in the app's config modal only when the app
   declares paths, default off). Revoking it closes the paths on save.

The edge never learns the paths from the request: the workspace publishes the
effective set to the registry row (`PUT /api/workspaces/<ws>/app-installs/<id>/public-paths`)
on every load, and the edge reads that row plus `config.public_paths_approved`.
The app's own handler is then the **only** auth on that path — it must verify
its own signature/token and fail closed. Every other route of the app keeps
the normal login.

## 2. Where the decision is resolved — the file chain

Two sessions about an hour apart each burned 12-15 greps hunting for this
while debugging the same bug, and both landed on the same files. Skip the
search.

| File | Role |
|---|---|
| `src/apps/manifest.py` | where an app declares `public` / `auth_required` in its manifest |
| `src/apps/proxy.py` | reads that flag when proxying a request into the app's container |
| `src/apps/runtime.py` | `_attach_mount` / container mount wiring; also the subdomain-adjacent logic |
| `repos/aw-backend/src/api/app.py` | `AppSubdomainMiddleware` — the ASGI-level piece deciding enforcement from the `Host:` header for subdomain-mounted apps (`<app>.app.aw.workspace.*`). **Not in this repo**: `src/api/app.py` here carries only `_ScopedCORSMiddleware` and `_ConfigGenerationMiddleware` |
| `repos/aw-backend/src/api/routes/app_auth_gate.py` | the gate aw-backend actually runs in front of a proxied app |
| `repos/aw-backend/src/libs/caddy_template.py`, `…/workspace_caddy_template.py` | generate the Caddy config fronting the app's hostname; per-hostname auth policy is baked in here too |
| `repos/aw-backend/src/api/routes/workspace_tunnel_proxy.py` | the path taken when the app is reached through the workspace tunnel domain instead of a direct subdomain |

For one app's own config, read `apps/<name>/aw-app.json` (materialized) or
`repos/aw-app-<name>/aw-app.json` (source) for the `public` /
`auth_required` field — plus the app's own `container/nginx/nginx.conf` if it
fronts itself with nginx, as SigNoz does.

## 3. Verifying a container config change with no local docker

The agent container here has no docker/podman socket, so you cannot
`docker run` the app's container locally to test a config change. Use the
linked BYOD host via `aw_app_remote_host_cli`:

```
remote_host_status                       # confirm the host is connected
remote_host_exec_run command="which docker podman 2>&1"
# base64 the config out to avoid quoting hell:
remote_host_exec_run command="mkdir -p /tmp/<scope> && echo '<base64>' | base64 -d > /tmp/<scope>/nginx.conf"
remote_host_exec_run command="podman run --rm -e AW_SIGNOZ_API_KEY=testkey123 \
  -v /tmp/<scope>/nginx.conf:/etc/nginx/templates/... nginx:alpine nginx -t"
```

`nginx -t` is a syntax check only. For a functional check, stand up a tiny
stub upstream on the same host and drive real requests at it — **no key →
401, wrong key → 401, correct key → 200, unauthenticated UI path still
200** — before touching the live deploy. Clean up `/tmp/<scope>` afterwards.

---

## Provenance

- `aw-autoskill-app-auth-required-routing` → all sections
