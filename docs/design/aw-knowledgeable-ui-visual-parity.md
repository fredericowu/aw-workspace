# aw-knowledgeable — visual parity with AP-MT (addendum to Onda U)

Architect design, 2026-10-10. Card `3f55bf3b-9510-81ca-bba7-d7afe271ac87`.
Addendum to `aw-knowledgeable-ui-decoupled.md` (Onda U), whose §0 deliberately
scoped visual work OUT ("the visual half stays per-app"). This card reverses
that one clause on the product owner's explicit instruction — everything else
Onda U decided (React/Vite/Tailwind stack, Cytoscape, AuthGate logic,
contract-level decoupling, UI_V2 flag) stands untouched.

The request, verbatim (Frederico, Telegram, 10/10, with a screenshot of the
live light-themed UI):

> "we need to work on the knowledgeable UI only for now. We have to plan to
> look like the agents-platforms-multitenant."

Everything cited below was read live on 2026-10-10.

---

## 0. The decision, in four sentences someone could disagree with

`frontend-react/` adopts AP-MT's Tailwind tokens, font stack and `index.css`
base layer **verbatim** (`repos/agents-platform-multitenant/frontend/tailwind.config.js:6-21`,
`index.css:1-106`), and goes **dark-only** — every `dark:` variant is deleted
along with the light styles, because the dark variants are dead code today
anyway (`darkMode: "class"` is set but nothing ever adds the `dark` class;
verified by grep across `src/` and `index.html`). The shell becomes AP-MT's
**left sidebar**, copied structurally from
`repos/agents-platform-multitenant/frontend/src/App.tsx:77-136` (icon+label
NavLinks with the `border-l-2` accent active state, desktop `w-56` rail,
mobile hamburger drawer) minus the two things aw-knowledgeable doesn't have
(`wsManager`, `?view=telegram` embedded mode). The graph canvas needs **no
Cytoscape work**: `cytoscapeStyle.ts` is already GitHub-dark end to end and
`GraphView.tsx:529` already hard-codes `bg-[#0d1117]` — the dark shell
*resolves* today's dark-island-in-light-shell inconsistency rather than
creating one; the only change there is swapping the hard-coded hex for the
new `bg-bg` token. The work is pure class-level restyling across 18 files
(379 `neutral-*` occurrences counted), split into 4 cards for
`ux-coder-sonnet` in §8.

---

## 1. Tokens — copy AP-MT's, verbatim

`frontend-react/tailwind.config.js:5-7` currently has an **empty**
`theme.extend`. Replace with AP-MT's block, byte-for-byte
(`repos/agents-platform-multitenant/frontend/tailwind.config.js:7-21`):

```js
colors: {
  bg:    { DEFAULT: "#0d1117", 1: "#0a0f17", 2: "#161b22", 3: "#21262d" },
  line:  "#30363d",
  muted: "#8b949e",
  fg:    "#c9d1d9",
  accent:"#58a6ff",
  ok:    "#2dd4bf",
  warn:  "#f0c000",
  err:   "#f87171",
  plum:  "#b794f4",
},
fontFamily: {
  sans: ['-apple-system', 'BlinkMacSystemFont', '"Segoe UI"', 'Roboto', 'sans-serif'],
  mono: ['"SF Mono"', 'Menlo', 'Consolas', 'monospace'],
},
```

`darkMode: "class"` is already set in both configs — keep it (harmless, and
keeps the configs diffable), but nothing toggles it; see §3.

**`index.css`:** aw-knowledgeable's is 8 lines (`@tailwind` directives + the
`html, body, #root { height: 100% }` rule). Adopt AP-MT's
`frontend/src/index.css` wholesale — the base element styling (body
background/color/font, inputs, links, scrollbars) and the utility classes
`.btn` / `.btn-primary` / `.btn-danger` / `.card` / `.kbd` / `.badge` family /
`.codebox` (lines 1-106) — **except** the `.react-flow__*` block (lines
108-126, AP-MT's xyflow flow-editor styling; aw-knowledgeable uses Cytoscape,
which doesn't read CSS at all). Keep the existing `height: 100%` rule merged
into the body block. Adopting the `.btn`/`.card` utilities is what makes the
per-component work in §4 small: most buttons/panels become a class swap, not
a redesign.

## 2. Shell — left sidebar, despite only 3 routes

**Decision: adopt the sidebar.** The honest counter-argument is route count:
aw-knowledgeable has 3 routes (`App.tsx:6-10` — Library/Graph/Playground) vs
AP-MT's 19 (`App.tsx:37-57`), and a 224px rail for 3 items spends horizontal
space the Graph view in particular would like to keep. Rejected the
restyled-top-bar alternative anyway, for three reasons: (a) the ask is
*parity*, and the persistent left sidebar is the single most recognizable
element of AP-MT's shell — a dark top bar would read "dark theme", not
"looks like AP-MT"; (b) the route list is about to grow — the v2 retrieval
work (`aw-knowledgeable-v2-retrieval.md`, entity layer / Onda 1.1+) plausibly
adds an entity browser and a settings surface, and a sidebar absorbs new
entries for free where a top bar starts truncating; (c) it lets the nav be a
structural copy of AP-MT's `navLinks` block rather than a reinvention, which
is this card's instruction. What would change this call: Frederico reacting
to the first screenshot with "too much chrome for 3 links" — the slim
fallback (same tokens on the existing top bar) loses only the shell card
(§8 VP1), nothing downstream.

Copy from `repos/agents-platform-multitenant/frontend/src/App.tsx`:

- the `navLinks` block (`:77-96`): `NavLink` with
  `flex items-center gap-3 px-4 py-3 md:py-2 text-sm border-l-2`, active =
  `border-accent text-fg bg-bg-3/60`, inactive =
  `border-transparent text-muted hover:text-fg hover:bg-bg-3/40`, lucide icon
  `size={16}` + label.
- the responsive skeleton (`:98-137`): mobile top bar with `Menu`/`X`
  hamburger (`:103-116`), desktop `<aside className="hidden md:flex w-56
  shrink-0 border-r border-line bg-bg-2 flex-col">` (`:119-121`), mobile
  drawer overlay with backdrop (`:124-134`), `close-drawer-on-route-change`
  effect (`:73-75`).

Drop: the `wsManager` effect (`:67-70` — aw-knowledgeable has no websocket
layer) and the `?view=telegram` embedded mode (`:62-63,100` — no embedder
exists; do not carry dead props "just in case").

NAV entries (lucide icons, already a dependency at `^1.14.0` — same pin as
AP-MT): `Library` → `LibraryBig` (or `FolderOpen`), `Graph` → `Share2`,
`Playground` → `MessageCircle` (AP-MT uses `MessageCircle` for its own
Playground, `App.tsx:47` — reuse it for recognizability). Icon choice is the
coder's within lucide 1.x.

**One deliberate deviation:** AP-MT's desktop sidebar has no brand block (the
product name only appears in the mobile top bar, `App.tsx:112-114`). Keep
aw-knowledgeable's product name as a small header at the top of the sidebar
(`text-sm font-semibold text-accent`, matching AP-MT's mobile-bar styling) —
two apps now share one look, and the name is how you tell which one you're
in. `ReadOnlyBadge` (shown for `aw::` buckets) moves next to it.

**`lucide-react` sits in `devDependencies`** in frontend-react's
`package.json:35` (scaffolded for future use, never yet imported). The shell
card moves it to `dependencies` — Vite bundles it either way, but shipped
imports don't belong in devDependencies.

## 3. Dark-only — delete the `dark:` variants, don't keep a light theme

Today every component carries paired light + `dark:` classes
(e.g. `Modal.tsx:25`, `AuthGate.tsx:35-44`), and the dark half **never
renders**: `darkMode: "class"` requires someone to put `dark` on the root,
and nothing in `src/` or `index.html` does. AP-MT has no light theme — its
tokens are literal dark values styled directly (`bg-bg-2`, not
`dark:bg-bg-2`). Parity therefore means **single-theme dark**: each paired
light/`dark:` class collapses to one token class. Rejected keeping a dual
theme behind a real toggle: nobody asked for it, AP-MT doesn't have it, and
it would double the restyle surface to preserve a mode that has never been
seen. Acceptance grep (VP4): `grep -rn 'neutral-\|dark:' src/` → zero hits.

Mapping table (the mechanical 90% of the work):

| current (light + dead dark pair) | becomes |
|---|---|
| `bg-neutral-50` / `bg-white` page+panel bgs | `bg-bg` (page), `bg-bg-2` (panels/cards/modals) |
| `bg-neutral-100/800` hover/inset bgs | `bg-bg-3` (or `.btn` hover for buttons) |
| `text-neutral-900/100` primary text | `text-fg` (often deletable — body default) |
| `text-neutral-500/400` secondary text | `text-muted` |
| `border-neutral-200/300/700/800` | `border-line` |
| `bg-black text-white` primary buttons (`GraphView.tsx:487`, `AuthGate.tsx:44`) | `.btn-primary` |
| `blue-*` (accents, spinners, focus) | `accent` |
| `red-*` (errors, destructive) | `err` / `.btn-danger` / `.badge-error` |
| `amber-*` (warnings, overflow banner `GraphView.tsx:544`) | `warn` / `.badge-warn` |
| `emerald-*` (success) | `ok` / `.badge-success` |

Counted live: 379 `neutral-*` occurrences across 18 `.tsx` files, plus 16
`border-blue-500`, ~50 red/amber/emerald accents. No test couples to a CSS
class (grepped the 7 `*.test.tsx` files) — restyling cannot break the vitest
suite except through accessible-name/text changes, so don't change any text.

## 4. Component inventory

Every file that restyles, grouped as the §8 cards slice them:

**Shell + entry (VP1):** `App.tsx` (full rewrite per §2), `index.css`,
`tailwind.config.js`, `AuthGate.tsx` (visual half only — the gate screen at
`:35-44` becomes a `.card` on `bg-bg`; `src/lib/authRedirect.ts` is
byte-locked to AP-MT by the CI parity check and is **not touched**).

**Shared primitives + Library (VP2):** `Modal.tsx` (base for 5 other modals
— `border-line bg-bg-2`, one file fixes six dialogs' chrome), `Toast.tsx`,
`SearchBar.tsx`, `NotFoundState.tsx`, `ReadOnlyBadge.tsx` (→ `.badge`),
`BucketGrid.tsx`, `LibraryGrid.tsx`, `UploadModal.tsx`,
`CreateBucketModal.tsx`, `routes/Library.tsx`. Consider adopting AP-MT's
`Page.tsx` header shape (`frontend/src/components/Page.tsx` — `text-2xl
text-accent` title, `border-b border-line`, actions slot) for Library and
Playground; Graph keeps its own toolbar.

**Graph (VP3):** `routes/GraphView.tsx` (toolbar `:469-527` is the densest
single spot — segmented expand control `:493-513`, icon buttons, overlays
`:544-581`; swap `bg-[#0d1117]` at `:529` and the three `bg-[#0d1117]/75`
overlay scrims to token classes), `graph/Legend.tsx`, `graph/Inspector.tsx`,
`graph/Breadcrumbs.tsx`, `graph/AddLinkModal.tsx`,
`graph/ConfirmDeleteModal.tsx`, `graph/DocumentViewerModal.tsx`.
`GraphCanvas.tsx` needs **nothing**: its container is bare
(`GraphCanvas.tsx:284`) and `cytoscapeStyle.ts` already speaks the AP-MT
palette (`#0d1117` node borders `:81`, `#e6edf3` labels `:73`, `#30363d`
edges `:104`, `#8b949e` edge text `:120`) — it was ported from the old
dark-canvas prototype and has been waiting for the shell to catch up.
`cytoscapeStyle.ts` keeps its hex literals: Cytoscape styles are JS objects,
not CSS, and can't read Tailwind tokens (duplication cost named in §6).

**Playground + sweep (VP4):** `routes/Playground.tsx` (largest route file —
chat column, provenance panel, its own `buildTraversalGraph` canvas overlays),
plus the repo-wide acceptance grep from §3 and a visual pass.

## 5. What does NOT change

- Routes, router shape, `react-router-dom` NavLink pattern (already matches
  AP-MT's).
- `src/lib/api.ts`, all API contracts, the backend (`backend/app/main.py`'s
  `UI_V2` flag stays as-is — the restyle ships behind the same always-on
  default).
- AuthGate **logic** and `src/lib/authRedirect.ts` (CI-enforced byte parity
  with AP-MT — any diff there is a red build, by design).
- Cytoscape-vs-xyflow (Onda U §0's measured decision), `cytoscapeStyle.ts`'s
  node/edge vocabulary, `NODE_BUDGET`, legend semantics.
- All component props, state, tests' queried roles/text. This is Tailwind
  classes and className strings only — if a card finds itself editing a hook
  or an API call, it has left its scope.

## 6. What this makes harder later

- **Second hand-copied AP-MT artifact, no parity check this time.**
  `authRedirect.ts` got a CI byte-parity check because drift there is a
  security bug. The tokens get no such check — deliberately: AP-MT retuning
  its `warn` yellow must not break aw-knowledgeable's build. Cost: the two
  palettes WILL drift silently. The day a third consumer appears is the day
  a shared design-token package pays for itself; until then this doc is the
  only record that the copy was made (from AP-MT `tailwind.config.js` as of
  2026-10-10).
- **The same hexes now live in three places** in this one repo:
  `tailwind.config.js`, `cytoscapeStyle.ts` (unavoidable — Cytoscape can't
  read Tailwind), and AP-MT upstream. A future "rebrand the dark theme" is
  three edits, not one.
- **Light theme becomes a rebuild, not a toggle.** Deleting the `dark:`
  pairs removes the (never-functional) scaffolding; re-adding light support
  later means re-authoring tokens as CSS variables, not flipping a switch.
- **Shell divergence from AP-MT resumes immediately.** The sidebar is a
  structural copy, not a shared component; AP-MT shell improvements (e.g.
  collapse-to-icons) won't propagate.

## 7. Risks for the coders

1. **`GraphView.tsx` toolbar is load-bearing UI, not chrome.** The expand
   segmented control (`:493-513`) and tree-mode toggles carry state-dependent
   conditional classes — restyle the class strings without touching the
   conditions; a wrong `isActive`-style ternary silently breaks mode
   feedback, and no test catches it (tests query text/roles, not styles).
2. **The overlay scrims double as loading gates.** The `bg-[#0d1117]/75`
   overlays (`GraphView.tsx:550,557,563,575`) sit over the canvas; replacing
   them with `bg-bg/75` must keep the opacity suffix or the spinner becomes
   unreadable over a busy graph.
3. **`.btn`/`.card` adoption changes box metrics.** AP-MT's `.btn` is
   `padding: 6px 12px; font-size: 13px` — slightly different from current
   `px-3 py-1.5 text-sm` buttons. Fine everywhere except the Graph toolbar's
   `h-8 w-8` icon buttons (`:515,523`): keep those explicit squares.
4. **AP-MT's `index.css` styles raw `input`/`select`/`textarea` globally**
   (`:24-32`, full-width, `bg-bg-2`, line border). Every existing form
   control in modals/SearchBar inherits this the moment the file lands —
   VP1 merging it before VP2 restyles the components means one deploy window
   where library modals are half-themed. Acceptable for an internal tool;
   sequence the cards promptly rather than engineering a flag.
5. **Playground renders provisional/pruned graph nodes** via
   `cytoscapeStyle.ts:152-158` data-selectors — those are canvas styles and
   must not be "cleaned up" during the VP4 sweep; the sweep's grep targets
   `neutral-`/`dark:` in `.tsx` only.

## 8. Cards, in dependency order (all `ux-coder-sonnet`)

Same pattern as Onda U §10. Each card's acceptance includes `npm run build`
(tsc + vite) and `npm test` green.

| id | card | depends on | scope |
|---|---|---|---|
| VP1 | tokens + shell: tailwind config, index.css base layer, sidebar App.tsx, AuthGate visual, lucide→dependencies | — | §1, §2; acceptance: sidebar renders all 3 routes, active state matches AP-MT's border-l-2 pattern, screenshot for Frederico |
| VP2 | library surfaces: Modal, Toast, SearchBar, NotFoundState, ReadOnlyBadge, BucketGrid, LibraryGrid, UploadModal, CreateBucketModal, Library route (+ optional Page.tsx adoption) | VP1 | §3 mapping, §4; acceptance: no `neutral-` under components/ (graph/ excluded), modals read as AP-MT cards |
| VP3 | graph surfaces: GraphView toolbar/overlays (incl. `bg-[#0d1117]`→`bg-bg`), Legend, Inspector, Breadcrumbs, 3 graph modals | VP1 (Modal restyle lands in VP2 but graph modals compose it — take VP2 as done or restyle compose-level classes only) | §4, risks 1-2; acceptance: canvas visually continuous with shell, legend swatches unchanged |
| VP4 | playground + sweep: Playground route, repo-wide `grep -rn 'neutral-\|dark:' src/` → 0, full visual pass, screenshot set | VP2, VP3 | §4; acceptance: grep clean, all tests green, before/after screenshots on the card |

---

*Cross-links: `aw-knowledgeable-ui-decoupled.md` (Onda U — stack, auth,
parity checklist; its §0 visual-scope clause is superseded by this doc),
`aw-knowledgeable-infra.md` (D1 history), AP-MT sources:
`repos/agents-platform-multitenant/frontend/{tailwind.config.js,src/index.css,src/App.tsx,src/components/Page.tsx}`.*
