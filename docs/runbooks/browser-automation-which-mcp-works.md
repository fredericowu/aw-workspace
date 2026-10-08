# How to use a browser using MCP in this workspace

**All four browser MCP families work.** Verified live 2026-10-08 09:16 UTC by
calling each one:

| Tool family | Drives | Verified |
|---|---|---|
| `aw__playwright__*` | the shared `aw-app-browser` Chromium over CDP | navigated, returned a snapshot |
| `aw__devctl_browser__*` | the same shared Chromium | returned title + URL |
| `aw__mini_browser__browser_*` | the same shared Chromium | navigated, `ok: true` |
| `aw__kali__aw__playwright__*` | the `kali-linux` container's OWN headed Chromium | works independently of `aw-app-browser` |

`aw-app-browser` **is** installed (`browser`, 0.28.0, Tier-2 container) and
serves CDP on `aw-app-browser:9223` (measured: `Chrome/154.0.8037.57`).

## Which to pick

- **`aw__playwright__*`** for most automation — it returns an accessibility
  snapshot with a `ref` per element, so you target elements by `ref` instead
  of pixel coordinates.
- **`aw__kali__aw__playwright__*`** when you need isolation from whatever
  else is using the shared browser, or a session that persists independently.
  It has its own Chromium, so it is unaffected by `aw-app-browser`'s state.
- **`aw__devctl_browser__*` / `aw__mini_browser__*`** for quick coordinate
  clicks, JS eval/inject, and screenshots against the shared browser.

Note the three shared-browser families all pilot the **same** Chromium — a
navigation through one is visible to the others. That is a feature when you
want it and a surprise when you don't.

## The recipe

1. **Go somewhere** — `browser_navigate` with a URL.
2. **See the page** — `browser_snapshot` (accessibility tree with `ref`s) for
   acting on; `browser_take_screenshot` for showing a human.
3. **Act** — `browser_click`, `browser_type`, `browser_fill_form`,
   `browser_press_key`, `browser_select_option`, `browser_hover`,
   `browser_drag` / `browser_drop`, `browser_file_upload`.
4. **Read results** — `browser_evaluate` to run JS,
   `browser_console_messages`, `browser_network_requests`.
5. **Wait** — `browser_wait_for` on text appearing or disappearing, not sleeps.
6. **Tabs and teardown** — `browser_tabs`, then `browser_close`.

## The real trap: a CDP error does NOT mean the app is missing

If a browser tool fails with

```
aw-app-browser not reachable over CDP (:9223) and could not be started
```

the overwhelmingly likely cause is a **stale gateway upstream**, not a
missing app. The MCP gateway holds long-lived upstream connections; when one
goes bad it keeps serving the tool name while every call fails. `doctor` says
so, in these words:

```
an upstream the gateway failed to connect to serves zero tools
until a reload; the runtime re-checks every 60s
```

The fix is `aw-workspace-cli restart mcp-gateway`. Be aware it blinds every
agent session's MCP client for ~1 minute, so say so before doing it.

**Diagnose in this order, cheapest first:**

1. `aw-workspace-cli apps` — read the whole list, not a grep.
2. `docker ps | grep aw-app-browser` — is the container actually up?
3. From inside the gateway container, hit the CDP endpoint directly:
   `urllib.request.urlopen("http://aw-app-browser:9223/json/version")`.
4. Only then conclude anything about the app being absent.

## A cautionary tale — this document used to say the opposite

On 2026-10-07 these three shared-browser families were measured **failing**
with exactly that CDP error, and this runbook (plus the `aw-kali-linux`
skill, v0.17.0) concluded that `aw-app-browser` was **not installed** and
that the Kali tools were the only ones that worked.

That was wrong. The app was installed and its container had been `Up` for
16 hours — spanning the entire measurement. The failures were the stale
gateway upstream above, and a `restart mcp-gateway` on 2026-10-08 brought all
three back. The false conclusion came from inferring "not installed" from a
tool error plus a misread of `aw-workspace-cli apps` — while that same
document asserted `apps` was "the authoritative check".

The transferable lesson is not about browsers:

- A tool erroring is evidence about **one call path**, not about what exists.
- `tools/list` showing a tool proves its MCP server is up, nothing more.
- But the inverse trap is just as real: a tool **failing** does not prove its
  backend is absent. Check the container and the endpoint before writing
  anything down.
