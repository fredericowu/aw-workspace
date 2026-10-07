# How to use a browser using MCP in this workspace

**Short answer: use the `aw__kali__aw__playwright__*` tools.** In this
workspace they are the only browser automation that actually works. Measured
live 2026-10-06/07.

They drive the `kali-linux` container's own headed Chromium on `DISPLAY=:0`,
so they depend on nothing else being installed.

## The recipe

1. **Go somewhere** — `aw__kali__aw__playwright__browser_navigate` with a URL.
2. **See the page** — `browser_snapshot` returns the accessibility tree with a
   `ref` for every element; prefer it over `browser_take_screenshot`, which is
   for showing a human what the page looks like.
3. **Act** — `browser_click`, `browser_type`, `browser_fill_form`,
   `browser_press_key`, `browser_select_option`, `browser_hover`,
   `browser_drag` / `browser_drop`, `browser_file_upload`. Target elements by
   the `ref` from the snapshot, not by pixel coordinates.
4. **Read results** — `browser_evaluate` to run JS in the page,
   `browser_console_messages` for console output,
   `browser_network_requests` for what the page actually fetched.
5. **Wait** — `browser_wait_for` on text appearing or disappearing, rather
   than sleeping.
6. **Tabs and teardown** — `browser_tabs`, then `browser_close`.

The session is persistent between calls, so a login survives into the next
call. A Google session was already signed in when this was measured.

## Every other browser MCP here fails at call time

`aw-app-browser` is **not installed** in this workspace — `aw-workspace-cli
apps` lists `proxy`, `kali-linux`, `devctl`, `mini-browser`, and no `browser`.
Every other browser MCP is a CDP *client* of that missing container, so they
fail when called, not when discovered:

| Tool family | Result when called |
|---|---|
| `aw__kali__aw__playwright__*` | **works** — the Kali container's own Chromium |
| `aw__mini_browser__browser_*` | `aw-app-browser not reachable over CDP (:9223) and could not be started` |
| `aw__devctl_browser__browser_*` | same — points at the same absent container |
| `aw__playwright__*` (top level) | configured against `aw-app-browser`'s CDP endpoint; unreachable |

## The trap: a listed tool is not a working tool

The gateway's `tools/list` reports a tool whenever its MCP *server* is up. It
says nothing about whether that server's *backend* exists. `mini-browser` and
`devctl` are installed and answer fine — they just have nothing to drive. So
a populated tool list is not evidence of capability, and neither is a green
`doctor`. `aw-workspace-cli apps` is the authoritative check.

## Why searching the knowledge base did not answer this

The knowledge base documents **code**, not **reachability**. Asking it how to
use a browser over MCP returns the implementations (`devctl_browser.py`,
`mini_browser_browser.py`) and the gateway architecture — all correct, all
useless for picking a tool that answers. It is a map, not a dial tone. When a
task depends on a backend being alive, check the app list and make one cheap
live call before building on it.

See also: the `aw-kali-linux` skill (v0.17.0+) carries the same table.
