# Manifest-driven app event hooks

Status: Accepted and implemented for Kanban `4005bf3b-9510-81e0-a4d1-c266769107be`.

## Decision

Keep `NotificationManager.emit_app_event` and `/ws/notifications` as the
ephemeral transport. Apps declare shell reactions under
`contributes.event_hooks`; core validates and stamps each contribution with its
installed app id; the SPA installs one generic dispatcher from the same
`/api/apps/-/contributions` payload it already consumes.

```json
{
  "event_hooks": [{
    "event": "open_file",
    "action": {
      "type": "open_window",
      "window": "example.main",
      "url_field": "url"
    }
  }]
}
```

`open_window` is the complete v1 action vocabulary. It opens or focuses an
existing manifest window through the shell window manager. `url_field` is
optional; when present, that string payload field overrides the managed
window's iframe URL. There is no arbitrary JavaScript and unknown action
fields fail manifest validation.

## Envelope and delivery

Frames are `{"type":"app_event","version":1,"data":{"app":"<stamped>",
"event":"<declared>",...}}`. Event names are lower snake case, at most 64
characters. Payloads are JSON objects capped at 8192 encoded bytes. Delivery
is broadcast, ephemeral, and has no persistence or replay; a closed shell
receives no backlog. Existing unversioned consumers remain compatible.

## Identity and permissions

Tier-1 calls `ctx.notify.event`: the facade enforces `notifications:send` and
stamps `ctx.app_id`. Tier-2 containers receive `AW_APP_EVENT_URL` and an
`AW_APP_EVENT_TOKEN` HMAC scoped to their manifest id. The endpoint derives
identity from its path and validates that app-specific credential, the
installed grant, and the declared event. A body cannot supply or override app
identity; a token for app A fails on app B's path. Sidecars receive no token.

The HMAC derives from the durable workspace API key, so it survives container
recreation and rotates with that key. Rotation requires Tier-2 containers to
restart to receive the new token.

## Rejected

- Manifest JavaScript: marketplace-controlled code in the shell is outside
  the required vocabulary and trust boundary.
- Raw `window.open`: an async WebSocket callback has no browser user gesture
  and can be popup-blocked. The shell-managed window path is deterministic.
- Persistence/replay and per-user targeting: both contradict the existing
  app-event transport and are unnecessary for open/focus behavior.
