# Standard — app↔backend WebSocket messaging

**Status:** design, written 2026-09-09. Sections 1–3 **document what already
exists**; sections 4–8 **design what does not**. No code changes in this card.
**Card:** `Architecture: app↔backend WebSocket messaging framework` —
`3d65bf3b-9510-819b-af72-c38538fadce0`, target
`aw-app-backend-ws-messaging-framework`.
**Scope:** the message-exchange contract for WebSockets between a browser (or
an agent) and this workspace's backend — core sockets and app-owned sockets
alike. Implementation lands in separate per-repo cards; see [§9](#9-rollout).

Every path, line number and message shape below was **read on 2026-09-09** in
`/opt/aw-workspace` at commit `16e988a`. How to re-derive the inventory is in
[§11](#11-how-to-reproduce-the-inventory).

---

## 0. The four findings that shaped this design

Read these first. Each contradicts an assumption the card was written on.

**0.1 — A framework *does* exist, but only below the message layer.**
The card's premise ("maybe one already exists") is half right. There is a real,
enforced, documented contract for **how a WebSocket is addressed, mounted and
authenticated** — canonical path `/api/apps/<slug>/ws/<name>`, an ASGI
`IdentityGuard` that gates the `websocket` scope exactly as it gates `http`, a
shared client-side URL builder, and a Tier-2 reverse proxy that bridges WS the
same as HTTP. That is a framework, it is live, and nothing in it needs
inventing. See [§2](#2-what-already-exists-the-transport-contract).

**0.2 — There is no contract at all for what travels *inside* the socket.**
Nineteen WebSocket surfaces, **four different discriminator keys** for "what
kind of message is this": `type` (13 surfaces), `kind` (AP-MT), `cmd` (devctl's
tab relay), `op` (remote-host-terminal). No versioning anywhere — a
`grep` for `"version"`/`"v":`/`protocol_version` in any WS payload returns
nothing. Two mutually incompatible error shapes. This is the actual gap.
See [§3](#3-what-does-not-exist-the-message-layer).

**0.3 — The naming *inside* `type` is already consistent, and nobody wrote it
down.** Across core and apps the value is `<domain>_<event>` in snake_case:
`whiteboard_init`, `whiteboard_update`, `presentation_init`,
`presentation_update`, `diff_open`, `ninja_init`, `ninja_notification`,
`app_install_status`, `github_init`, `terminal_update`. That convention is
already unanimous among the surfaces that use `type` at all. **The design below
adopts it verbatim, which means zero renames for existing message types** — the
churn is confined to the envelope around them.

**0.4 — No client anywhere inspects the close code, so the auth rejection the
framework carefully sends is thrown away.** `IdentityGuard` closes with `4401`
on a bad token (`src/apps/runtime.py:360`), and core does the same
(`src/api/terminal.py:348`). Every reconnecting client handles `onclose`
without ever reading `event.code`
(`repos/aw-workspace-ui/src/hooks/useWebSocket.js:26-30`,
`apps/whiteboard/ui/src/plugin.jsx:68`,
`apps/diff-tool/ui/src/plugin.jsx:59`,
`repos/aw-workspace-ui/src/components/TerminalWindow.jsx:370`). **An expired
session therefore produces an infinite reconnect loop against an auth wall** —
every 2s for the log hook, every 5s for the app plugins — instead of a visible
"logged out". This is a live defect, not a hypothetical; it is the single
highest-value thing in [§7](#7-client-rules).

---

## 1. Inventory — every app↔backend WebSocket, 2026-09-09

Nineteen endpoints. The card's initial KB sweep found three; it was right to
say it was not definitive.

### 1.1 Core sockets — root `/ws/*`, reserved for the control plane

| Path | Declared at | Payload |
|---|---|---|
| `/ws/terminal/{session_id}` | `src/api/terminal.py:158` | **raw bytes** both ways; inbound TEXT frame `{"type":"resize","rows","cols"}` is the one control message (`terminal.py:407`) |
| `/ws/status` | `src/api/terminal.py:159` | `{"type":"init",...}` then `{"type":"terminal_update"}` (`terminal.py:438`, `:181`) |
| `/ws/logs/{key:path}` | `src/api/components.py:164` | **raw text lines**, no envelope at all (`components.py:262-268`) |
| `/ws/notifications` | `src/api/notifications.py:182` | `{"type":"ninja_init"}` then `{"type":"ninja_notification"}` (`notifications.py:261`, `:129`) |
| `/ws/apps/install-status` | `src/apps/routes.py:1132` | `{"type":"app_install_status","job":{...}}` (`routes.py:1148`) |

### 1.2 Tier-1 in-process app sockets — mounted under `/api/apps/<slug>`

| Path | Declared at | Discriminator | Payload |
|---|---|---|---|
| `…/whiteboard/ws` | `apps/whiteboard/whiteboard_app/routes.py:298` | `type` | `whiteboard_init` / `whiteboard_update` / `whiteboard_exec`; client→server `whiteboard_viewport` (`:318`) |
| `…/presentations/ws` | `apps/presentations/presentations_app/routes.py:172` | `type` | `presentation_init` / `presentation_update` |
| `…/diff-tool/ws` | `apps/diff-tool/diff_app/routes.py:188` | `type` | `diff_open` |
| `…/devctl/ws` | `apps/devctl/devctl_app/routes.py:134` | `type` | `frame` (base64 JPEG screencast) / `error` with **`detail`** (`:141`) |
| `…/devctl/ws/tab` | `apps/devctl/devctl_app/routes.py:161` | **`cmd`** | `{"cmd":"hello"}` (`:176`) |
| `…/remote-host-terminal/ws/hosts/{id}` | `apps/remote-host-terminal/remote_host_terminal_app/routes.py:40` | **`op`** | `{"op":"status"}` out; `{"op":"input"\|"resize"}` in (`:46`, `:68`) |
| `…/remote-screen/ws/android/{id}` | `apps/remote-screen/remote_screen_app/routes.py:128` | `type` | **mixed** — inbound control frames keyed on `type` (`android.py:226`), outbound raw PNG bytes (`android.py:389`) |
| `…/remote-screen/ws/bridge/{id}` | `apps/remote-screen/remote_screen_app/routes.py:157` | — | **raw bytes**, websockify-equivalent TCP bridge |
| `…/git/github/stream` | `apps/git/git_app/plugin.py:316` | `type` | `github_init` — **note the path**, see §1.5 |
| `…/aw-app-template/ws/echo` | `apps/aw-app-template/template_app/routes.py:57` | — | raw echo, reference implementation |
| `…/mini-browser/{token}/v3/` | `apps/mini-browser/mini_browser_app/bare_server.py:234` | — | **Bare Server v3**, an external third-party spec; out of scope here |

### 1.3 Tier-2 container app sockets — bridged by the reverse proxy

Forwarded by `src/apps/proxy.py:196-234`, which pumps text and binary frames
in both directions and is otherwise protocol-blind.

| Path | Declared at | Discriminator | Payload |
|---|---|---|---|
| `…/call-agent/ws/call` | `apps/call-agent/call_agent_app/routes.py:462` | `type` | in: `message`; out: `ready` / `text_delta` / `done` / `heartbeat` / `cleared` / `agent_changed` / `error` with **`message`** (`:497`, `:501`, `:576`) |
| `…/ux-proto/ws/{slug}` | `apps/ux-proto/container/src/api/main.py:843` | `type` | `console` / `eval_result` |
| `…/mcp-gateway/link` | `apps/mcp-gateway/back/gateway/server.py:588` | `type` | `register` handshake; closes `4400`/`4401` (`remote_upstream.py:120-134`) |

### 1.4 Adjacent, not in scope

`agents-platform-multitenant` is a **separate deployed service**, not an
aw-workspace app, and reaches browsers on its own origin:

- `/api/ws` — `{"kind": "run_update"|"target_update", "data": {...}}`;
  client messages ignored (`repos/agents-platform-multitenant/backend/app/api/ws.py`).
- `/ws/agent/{run_id}` — `aw-connector` → platform CLI event stream,
  `{"type":"stdout","line":…}` / `{"type":"done","returncode":N}`
  (`backend/app/api/ws_agent.py`).

It is the **only** surface using `kind`, and it is also the only one that
already nests its payload under `data`. It is listed because the card asked for
a complete survey, and because §4 borrows its `data` nesting — but AP-MT ships
on its own release train and this standard does not bind it. See
[§9](#9-rollout).

### 1.5 Two defects the survey turned up

Both are real, both are cheap, neither is fixed by this card.

- **`aw-app-git` violates the canonical path shape.** It serves
  `/api/apps/git/github/stream` (`apps/git/git_app/plugin.py:316`), not
  `/api/apps/git/ws/<name>`. Its own docstring explains it moved off the
  monolith's `/ws/github` and stops there. The client hardcodes the same path
  (`repos/aw-workspace-ui/src/lib/githubApi.js:19`), so this is a two-line
  coordinated fix, not a design problem.
- **`aw-app-whiteboard`'s WS docstring is stale and actively misleading.** It
  states the `IdentityGuard` "has not landed" and that the route "is mounted
  unauthenticated today" (`apps/whiteboard/whiteboard_app/routes.py:302-307`).
  `IdentityGuard` landed and is applied unconditionally to every app mount,
  Tier-1 and Tier-2 alike (`src/apps/runtime.py:1113-1120`). A reader trusting
  that comment would conclude the framework is less complete than it is —
  which is close to the wrong conclusion this card exists to prevent.

---

## 2. What already exists — the transport contract

This is a genuine framework. It is documented, but **the documentation lives in
the wrong repo**: the ADR
*"Apps Own Their Front + Back Routes"* sits at
`repos/agentic-workspace/docs/knowledge_base/docs/architecture/adr-app-front-back-routes-dual-mode.md`
— inside the **monolith**, not aw-workspace. `find docs/ native-skills/ -name
"*adr*"` in this repo returns nothing. The nearest thing aw-workspace has to a
normative copy is a module docstring in the template
(`apps/aw-app-template/template_app/routes.py:20-41`). §2.1–§2.5 restate it
here so it is finally written down where the code is.

### 2.1 Path shape (ADR Decision 2)

```
/api/apps/<slug>/ws/<name>      ← the sub-app declares @app.websocket("/ws/<name>")
```

Root `/ws/*` is **reserved for core / control-plane sockets** and must never be
used for an app feature (`apps/aw-app-template/template_app/routes.py:22-26`).
A browser-facing app socket that genuinely needs a top-level edge namespace
goes under `/ws/apps/<slug>/…`. Paths declared inside the sub-app are always
**relative**, so the same string works in integrated and standalone mode.

### 2.2 Mounting

Nothing app-specific is required. `AppRuntime._attach_mount`
(`src/apps/runtime.py:1104-1121`) wraps the sub-app in `_DrainableApp` and
`IdentityGuard` and mounts it with a Starlette `Mount`, which forwards
`websocket` scopes exactly as it forwards `http`. The same guarded ASGI app is
also reachable on a per-app subdomain (`<slug>.app.…`), same guard, same
permissions.

### 2.3 Authentication

`IdentityGuard.__call__`'s `websocket` branch (`src/apps/runtime.py:324-338`)
resolves identity via `_default_verify_ws` (`:145-174`) in this order:

1. `X-Api-Key` header (workspace API key — for CLIs, MCPs, CDP automation)
2. `?token=` query parameter (identity JWT)
3. the apex `aw_id_jwt` cookie

The header path exists because a browser `new WebSocket()` **cannot set custom
headers**; the query/cookie path exists for exactly that reason. The header
check was missing until 2026-08-08 and silently `4401`ed every Playwright
session authenticated by API key alone — the docstring at `:155-160` records
it. On success the claims are stashed at `scope["aw_identity"]`, and **app
handlers read them, never re-verify** (`apps/devctl/devctl_app/routes.py:169-171`
is the reference reader).

On failure the guard drains the connect event, accepts, then closes `4401`
(`src/apps/runtime.py:351-360`) — accept-then-close rather than a bare
handshake rejection, because some clients never surface the latter as a
disconnect (`src/api/terminal.py:340-344`).

### 2.4 Client-side URL construction

Two shared builders, and app code should use neither directly:

- `wsUrl(path)` — `repos/aw-workspace-ui/src/auth.js:174`. Picks `ws:`/`wss:`
  from the page protocol and reuses the page host, so the apex cookie is sent
  automatically on a same-origin upgrade.
- `host.app.wsUrl(sub)` — `repos/aw-workspace-ui/src/apps/pluginHost.js:101`.
  Prefixes `/api/apps/<slug>` for you and routes through the SDK
  (`src/apps/sdk.js:36`) so the BYOD `apiBase` rewrite still applies.

**An app plugin must use `host.app.wsUrl('/ws/<name>')`.** Every app plugin
already does.

> `repos/aw-workspace-ui/src/hooks/useWebSocket.js` is **not** a generic
> client despite its name — it is hardcoded to `/ws/logs/${key}` at line 17.
> It is a log-stream hook. Do not reach for it for anything else; §7 replaces
> it with something that deserves the name.

### 2.5 Tier-2 bridging

`_AppProxy._ws` (`src/apps/proxy.py:196-234`) dials the container over the
`websockets` client and runs two pump tasks. It preserves frame type (text vs
binary), sets `max_size=None`, and closes `1011` if the upstream dial fails. A
Tier-2 app therefore speaks the same protocol as a Tier-1 app; the proxy adds
nothing and constrains nothing.

**Verdict on the card's question 1:** a shared transport framework exists and
is sound. What follows is the layer it stops at.

---

## 3. What does not exist — the message layer

Concretely, across the 19 surfaces:

| Concern | State today |
|---|---|
| Discriminator key | `type` ×13, `cmd` ×1, `op` ×1, none ×4 (plus `kind` ×1 in out-of-scope AP-MT) |
| Payload placement | inlined into the envelope (majority) vs. nested under `data` (AP-MT only) |
| Versioning | **absent everywhere** |
| Error shape | `{"type":"error","detail":…}` (devctl) vs `{"type":"error","message":…}` (call-agent); 17 surfaces have no error message at all |
| Close codes | `4401` unauthorized is universal; after that `4004` (core) vs `4404` (remote-screen) both mean "not found" |
| Handshake | a `<domain>_init` first frame is near-universal, but nowhere required |
| Heartbeat | `heartbeat` in call-agent only; elsewhere the server ignores inbound frames and relies on the client to keep the socket warm |
| Client reconnect | ad-hoc `setTimeout` per call site, 2s or 5s, **none inspect the close code** (§0.4) |

The inlining pattern is not merely inconsistent, it is unsafe. `aw-app-git`
builds its frame as `{"type": "github_init", **prs.get_cached()}`
(`apps/git/git_app/plugin.py:323`) — a **spread of upstream GitHub-derived keys
directly into the envelope**. One cached field named `type` and the message
silently becomes a different message. `src/apps/routes.py:1148` nests correctly
under `job`; `github_init` does not. This is the argument that decides §4.2.

---

## 4. Decision — the `aw-ws/1` envelope

One envelope, for every new app↔backend WebSocket in this workspace.

### 4.1 Frame shape

Every JSON frame, both directions:

```json
{
  "type": "whiteboard_update",
  "data": { "action": "set", "id": "board-1" },
  "id":   "5b1f…",
  "re":   "9c02…"
}
```

- **`type`** *(required, string)* — `<domain>_<event>`, snake_case. `<domain>`
  is the app slug with `-` collapsed to `_`, or the core subsystem name.
  This is §0.3's existing convention, adopted unchanged.
- **`data`** *(required, object)* — the payload. Always an object, even when
  empty (`{}`). Never a bare scalar or array.
- **`id`** *(optional, string)* — sender-assigned correlation id. Present only
  when the sender expects a reply.
- **`re`** *(optional, string)* — echoes the `id` being answered. Required on
  any frame that answers one.

**No other top-level keys.** Anything else belongs in `data`.

### 4.2 Payload goes in `data`, not inlined

This is the one place the standard **deliberately breaks with the majority
pattern**, and §3's `github_init` spread is why: inlining means every producer
that forwards a payload it did not author is one upstream key away from
corrupting its own envelope. Nesting costs one line per producer and removes
the class of bug entirely. It also matches AP-MT, the only surface that already
got this right.

### 4.3 Versioning — negotiated once, not stamped per frame

The **server's first frame** on every socket is the handshake:

```json
{ "type": "<domain>_init", "data": { "protocol": 1, ... } }
```

`<domain>_init` is already what almost every socket sends first
(`whiteboard_init`, `presentation_init`, `ninja_init`, `github_init`,
core's `init`) — this makes it mandatory and gives it a job. `data.protocol`
is an integer, `1` for this document.

A client that reads a `protocol` it does not support **must close `4426`
(upgrade required) and surface a "reload the page" state** rather than
degrade silently. A client that receives an unknown `type` **must ignore that
frame and keep the connection** — that is what makes adding a message type a
backwards-compatible change.

Per-frame version stamps were rejected; see [§10.1](#101-a-v-field-on-every-frame).

### 4.4 Errors

```json
{ "type": "error", "data": { "code": "not_configured", "message": "…" } }
```

`type` is the literal string `error` — **not** `<domain>_error`, so one client
handler catches it on any socket. `data.code` is a stable snake_case slug
clients may branch on; `data.message` is human-readable and must not be parsed.
An `error` answering a correlated request carries `re`.

This supersedes both `detail` (devctl) and the bare `message`
(call-agent).

### 4.5 Binary frames are exempt, and stay exempt

`/ws/terminal`, `/ws/logs`, `remote-screen`'s TCP bridge and mini-browser's
Bare v3 carry raw bytes or raw text by design. **This envelope does not apply
to them, and they must not be migrated.** Wrapping a PTY byte stream in JSON
would add a base64 hop to the most latency-sensitive socket in the workspace
for no benefit.

The rule for a **mixed** socket — a byte stream with occasional control
messages, which is exactly what `/ws/terminal` is — is the one it already
uses: **binary frames are data, TEXT frames are envelopes**. A text frame on a
mixed socket must be a valid `aw-ws/1` envelope. `/ws/terminal`'s existing
`{"type":"resize"}` (`src/api/terminal.py:407`) needs only its fields moved
into `data` to comply.

---

## 5. Close codes

`4000 + <the HTTP status that would have applied>`. This is already what most
of the codebase does; it makes the rest derivable instead of memorized.

| Code | Meaning | Already used at |
|---|---|---|
| `4401` | unauthorized — bad/missing/expired identity | `src/apps/runtime.py:360`, `src/api/terminal.py:348`, and 5 more |
| `4403` | authenticated, but not permitted | — |
| `4404` | target does not exist | `apps/remote-screen/…/routes.py:40` |
| `4415` | unsupported protocol for this target | `apps/remote-screen/…/routes.py:41` |
| `4426` | protocol version not supported (§4.3) | — |
| `4502` | upstream/backing service unreachable | `remote-screen:42`, `remote-host-terminal:60` |
| `4503` | this app is not configured | `remote-host-terminal:47` |

`1011` stays as-is for an internal proxy failure (`src/apps/proxy.py:206`) —
it is the correct RFC 6455 code and no client branches on it.

**The one conflict:** core uses `4004` for "not found" in two places
(`src/api/components.py:253`, `src/api/terminal.py:354`) where the table says
`4404`. `4004` predates the convention and has exactly two call sites. Change
them when the surrounding code is next touched; **do not open a card to break
them on their own** — no client reads either code today (§0.4), so the fix has
no observable effect until §7 ships.

---

## 6. Server rules

1. Send `<domain>_init` as the first frame, with `data.protocol`. Send it even
   when there is no initial state (`data: {"protocol": 1}`).
2. Read identity from `scope["aw_identity"]`. **Never re-verify** — the guard
   already did, and in standalone mode there is nothing to verify.
3. Close with a §5 code and a `reason`. Accept **then** close, never reject the
   handshake bare (§2.3).
4. Ignore unknown inbound `type`s; do not close the socket over one.
5. Never inline a payload you did not author (§4.2).
6. Long work belongs in a task, not in a socket handler. The socket reports
   progress; it does not hold it.

## 7. Client rules

These exist because of §0.4, and this section is the highest-value part of the
standard.

1. **Inspect `event.code` in `onclose`.** Reconnect on transport-level closes.
   **Do not reconnect on `4401`, `4403` or `4426`** — surface "session
   expired" / "not permitted" / "reload required" instead. Today every client
   hot-loops forever on all three.
2. Reconnect with **exponential backoff and jitter**, capped — not the fixed
   2s/5s `setTimeout` currently copy-pasted across five call sites.
3. Ignore unknown `type`s (this is what makes §4.3 work).
4. Handle the literal `type: "error"` centrally.
5. Build the URL with `host.app.wsUrl('/ws/<name>')` (§2.4).

The right home for 1–4 is a **real** shared hook next to the existing one:
`repos/aw-workspace-ui/src/hooks/` — taking a path and returning parsed
envelopes and a connection state, with `useWebSocket.js` renamed to
`useLogStream.js` to stop advertising a generality it has never had (§2.4).
That hook is the single unit of work that fixes the reconnect-loop defect
across every client at once.

## 8. Recipe for a new app socket

1. Declare `@app.websocket("/ws/<name>")` on the sub-app returned by
   `build_routes()`. Relative path, no `/api/apps/<slug>` prefix.
2. Do **not** write auth code. Read `scope["aw_identity"]` if you need to know
   who is calling.
3. First frame: `{"type": "<slug>_init", "data": {"protocol": 1, ...}}`.
4. Subsequent frames: `{"type": "<slug>_<event>", "data": {...}}`.
5. Client: `host.app.wsUrl('/ws/<name>')` plus the shared hook from §7.
6. Nothing goes in the manifest — `routes:register` already covers WebSockets.

`apps/aw-app-template/template_app/routes.py:57` is the reference
implementation of the transport half. It should be extended to demonstrate
the envelope; that is the first rollout step.

---

## 9. Rollout

Ordered by value, not by repo. Each is a separate card.

1. **Shared client hook + close-code handling** (`aw-workspace-ui`). Fixes the
   §0.4 reconnect loop for every existing client. **Do this first** — it is the
   only item with a user-visible bug behind it, and it is independent of every
   server change.
2. **Template** (`aw-app-template`). Make `/ws/echo` speak `aw-ws/1` and carry
   this document's §8 in its docstring. Every new app is born from it.
3. **New sockets only.** From here, any new WS surface conforms. No existing
   socket is migrated on a schedule.
4. **Opportunistic conformance.** `cmd`→`type` (devctl tab relay), `op`→`type`
   (remote-host-terminal), `detail`→`data.code`/`data.message` (devctl), the
   `github_init` spread (§3), the `/api/apps/git/github/stream` path (§1.5),
   `4004`→`4404` (§5) — each when its file is next touched for another reason.
   Each is a coordinated server+client change; none is urgent.
5. **AP-MT is explicitly out.** It is a separately deployed service with its
   own release train, its `kind` envelope is stable and already nests under
   `data`, and its clients are its own. Aligning it is a **Product Owner
   decision about a different codebase**, not something this standard should
   quietly assume.

---

## 10. Rejected alternatives

### 10.1 A `v` field on every frame

Rejected. A WebSocket is a long-lived connection whose peer cannot change
mid-stream, so a per-frame version is a constant repeated thousands of times —
pure overhead on `/ws/logs`-class sockets. Negotiating once in a handshake
frame that **already exists on nearly every socket** (§4.3) costs nothing and
puts the version where a client can act on it before processing anything.

### 10.2 Path versioning — `/api/apps/<slug>/ws/v1/<name>`

Rejected. It forks the mount surface, doubles what `IdentityGuard` and the
Tier-2 proxy have to route, and — worse — implies a new endpoint for every
protocol change when the overwhelmingly common change is *adding a message
type*, which §4.3 already makes backwards-compatible. Path versioning also
breaks the ADR's canonical shape (§2.1), which is the part of the framework
that actually works.

### 10.3 Standardising on `kind` instead of `type`

Rejected on arithmetic. `kind` has one user (AP-MT, §1.4) and that user is out
of scope for this standard; `type` has thirteen, including all five core
sockets. Choosing `kind` would churn thirteen surfaces to align with one that
this document does not bind.

### 10.4 Keeping payloads inlined, and merely documenting it

The runner-up, and genuinely tempting: it is the majority pattern, and it would
make this standard a pure description with zero migration. Rejected because of
`github_init` (§3) — inlining a payload you did not author is a live
correctness hazard, and a standard that blesses it has to also write down "…
unless you're forwarding someone else's object", which is a rule nobody
remembers at the moment it matters.

### 10.5 A shared `AwWebSocket` server base class / decorator

Rejected as premature. The envelope is four keys; a base class that enforces it
would have to accommodate raw-byte sockets (§4.5), mixed sockets, the Tier-2
proxy pass-through and standalone mode, and would end up mostly escape hatches.
A documented shape plus a shared **client** hook (§7) captures nearly all the
value — the client is where the duplicated logic actually is, and the server
side is one dict literal per handler. Revisit if a fifth app writes the same
handshake boilerplate.

---

## 11. What this makes harder later

Named honestly, because each one is a door this closes.

- **Binary efficiency is off the table for enveloped sockets.** JSON text
  frames are the contract. A future high-frequency socket (devctl's screencast
  already base64s JPEG frames into JSON, `apps/devctl/devctl_app/routes.py:153`)
  will want MessagePack or raw binary and will have to sit in §4.5's exemption,
  which weakens the "one envelope" claim the more often it is used.
- **`data`-nesting is a breaking change wherever it is applied.** Server and
  client must ship together for each migrated socket. In Tier-2 apps that means
  a container image and an SPA bundle in lockstep — which is exactly why §9
  makes migration opportunistic rather than scheduled.
- **The `<domain>_<event>` flat namespace has no room for sub-namespaces.**
  `whiteboard_update` cannot later become `whiteboard.canvas.update` without a
  rename, since `_` is already the separator and slugs contain `-`→`_`.
- **One protocol integer per socket, not per message type.** Bumping
  `protocol` is all-or-nothing for that socket; there is no way to version an
  individual message. That is the right trade at 19 surfaces and would be the
  wrong one at 200.
- **This standard does not bind AP-MT** (§9.5), so "how do apps talk to a
  backend over WS" will have two answers in this workspace for as long as AP-MT
  keeps `kind`. That is a deliberate scope boundary, but it is a real cost and
  the next person will trip over it.

---

## 12. Risks for the Coders

- **The whiteboard docstring lies** (§1.5). Anything you infer about the
  framework's completeness from a comment in an app should be checked against
  `src/apps/runtime.py`. The guard is real and unconditional.
- **`_default_verify_ws` has three credential paths and browsers can only use
  two** (§2.3). If you test a socket with `curl`/an MCP client using
  `X-Api-Key` and it works, that proves nothing about a browser. Test both. The
  2026-08-08 code-server outage was exactly this gap in reverse.
- **`useWebSocket` is not a WebSocket hook** (§2.4) — it is hardwired to
  `/ws/logs/${key}`. Do not "reuse" it for a new socket; you will silently
  connect to the log stream.
- **Nothing tests the envelope.** There is no WS contract test anywhere in the
  estate. A frame that violates §4 will be caught by a human noticing a broken
  panel, not by CI. If §9.1 ships the shared hook, it should ship with tests
  for the close-code branches specifically — those are the paths that only
  execute when a session expires, which no one does on purpose.
- **WS-over-tunnel has its own failure history, independent of anything here.**
  The BYOD PTY socket opened at the edge and closed `1000` without ever
  reaching the workspace (Kanban `bug:tunnel-websocket-not-bridged-to-byod`,
  fixed 2026-07-27, regression test at
  `repos/aw-remote-host/internal/tunnelproxy/tunnelproxy_test.go`), and
  `/ws/status` intermittently hangs in `pending` through the tunnel (Kanban
  `bug:tunnel-ws-status-handshake-hangs-pending`, open, `need_human` as of
  2026-09-04). **A socket that misbehaves only through
  `api.<ws>.workspace.aw.tekflox.com` is probably an edge problem, not a
  protocol problem** — reproduce against the workspace directly before
  debugging your envelope.
- **Do not confuse the ~30s tunnel cut with WS behaviour.** That limit is
  documented for HTTP requests, not established WebSocket connections, and
  this survey did not verify whether it applies to an idle socket. If you need
  to know, measure it — do not assume either way.

---

## 13. How to reproduce the inventory

From `/opt/aw-workspace`:

```bash
# every WS endpoint, core + apps — returns 20 lines for the 19 real endpoints;
# aw-app-template/template_app/routes.py:22 is a docstring, not a declaration
grep -rn '\.websocket(\|websocket_route\|add_websocket_route' \
  --include='*.py' src/ apps/ | grep -v node_modules

# discriminator keys actually in use — counts SEND SITES (27 `type`, 2 `cmd`,
# 2 `op`), not surfaces; the per-surface tally in §1 was read by hand
grep -rn 'send_json\|send_text(json' --include='*.py' src/ apps/ \
  | grep -o '"\(type\|kind\|cmd\|op\)":' | sort | uniq -c

# every browser-side WS client
grep -rn 'new WebSocket' --include='*.jsx' --include='*.js' \
  repos/aw-workspace-ui/src/ apps/*/ui/src/ | grep -v node_modules

# close codes in use
grep -rn 'close(code=' --include='*.py' src/ apps/ | grep -v node_modules

# Tier-1 vs Tier-2 for an app
python3 -c "import json;print(json.load(open('apps/<slug>/aw-app.json'))['runtime'])"
```

The ADR that §2 restates:
`repos/agentic-workspace/docs/knowledge_base/docs/architecture/adr-app-front-back-routes-dual-mode.md`
(Decisions 1 and 2). It is in the **monolith** repo, not this one.
