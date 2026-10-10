# aw-knowledgeable — visual parity with AP-MT (addendum to Onda U)

Architect design, 2026-10-10. Card
`design:aw-knowledgeable-ui-visual-parity-ap-mt`
(`3f55bf3b-9510-81ca-bba7-d7afe271ac87`). Addendum to
`aw-knowledgeable-ui-decoupled.md` (Onda U): §0 there scoped visual work out
("the visual half stays per-app"); Frederico reversed that on 10/10, with a
screenshot of AP-MT's live Dashboard:

> "I want to use this theme (system colors) with the menu on the right. I
> want to use the same pattern. I want a dashboard as the first/home screen,
> the existing menus on the aw-knowledgeable needs to go to its left menu.
> Clear? (we are changing aw-knowledgeable only UI)"

("menu on the right" vs "left menu": the screenshot and the explicit "left
menu" both say **left sidebar** — "right" was a slip. Resolved on the card,
not re-litigated here.)

Everything here is frontend-only, in `repos/aw-knowledgeable/frontend-react/`.
Framework, auth, Cytoscape and backend decisions from Onda U stand untouched.

---

## 0. The decision, in five sentences someone could disagree with

`frontend-react/` adopts AP-MT's GitHub-dark Tailwind tokens and its
`index.css` element/utility layer **verbatim** (hand-copied, deliberately with
no CI parity check — see §9), and becomes **dark-only**: every `dark:` variant
is deleted rather than activated, because no dark-class toggle exists anywhere
in the app today, so all 379 `neutral-*`/`dark:` occurrences across 18 `.tsx`
files are half dead code already. The top header is replaced by AP-MT's
left-sidebar shell — `NAV` array of icon+label `NavLink`s, `w-56` desktop
aside, mobile hamburger drawer — copied from AP-MT's `App.tsx` minus its
`wsManager` and `?view=telegram` embedded mode, which have no consumer here. A
new **Dashboard** route becomes `/` (Library moves to `/library`), mirroring
AP-MT's `Dashboard.tsx` layout with numbers the existing API actually serves:
bucket/document/node counts from one `GET /api/buckets` call and a
processing-queue tile from `GET /api/ingest/status` — **no links tile**,
because no endpoint serves a truthful workspace link count and fabricating one
from per-node `link_count` sums double-counts (§5). The graph canvas needs no
restyling — `cytoscapeStyle.ts` is already the target palette and
`GraphView.tsx` already hard-codes `bg-[#0d1117]`; the shell change is what
*removes* the current light-shell/dark-canvas clash, and the only graph edit is
replacing that hard-coded hex with the new `bg-bg` token.

## 1. Ground truth (read this session)

| Fact | Where |
|---|---|
| Current shell: light top header, text-only nav, `neutral-*` | `frontend-react/src/App.tsx:14-33` |
| Tailwind theme is EMPTY, `darkMode: "class"` | `frontend-react/tailwind.config.js:4-7` |
| No `class="dark"` toggle anywhere (src/ + index.html grepped) → every `dark:` variant is dead code; live UI is light-only | `frontend-react/src/`, `index.html` |
| 379 `neutral-*` occurrences across 18 `.tsx` files; accents are `blue/red/amber/emerald-*` | grep of `frontend-react/src` |
| AP-MT tokens: `bg`/`bg-1..3`, `line`, `muted`, `fg`, `accent`, `ok`, `warn`, `err`, `plum` + font stacks | `agents-platform-multitenant/frontend/tailwind.config.js:8-21` |
| AP-MT element defaults + `.btn`/`.card`/`.badge-*`/`.kbd`/`.codebox` + scrollbar live in plain CSS, not Tailwind utilities | `agents-platform-multitenant/frontend/src/index.css:5-106` |
| AP-MT sidebar shell: `NAV` array, `navLinks`, `w-56` aside, mobile drawer, `?view=telegram`, `wsManager` | `agents-platform-multitenant/frontend/src/App.tsx:37-135` |
| AP-MT page header + `StatusBadge` | `agents-platform-multitenant/frontend/src/components/Page.tsx` |
| AP-MT Dashboard: 4 `Metric` tiles + recent-runs table + quick-start card | `agents-platform-multitenant/frontend/src/routes/Dashboard.tsx:23-102` |
| Graph canvas already dark: hard-coded `bg-[#0d1117]` | `frontend-react/src/routes/GraphView.tsx:529` |
| Cytoscape style already GitHub-dark (`#0d1117` borders, `#e6edf3` labels, `#30363d` edges, `#8b949e` text) | `frontend-react/src/components/graph/cytoscapeStyle.ts:67-164` |
| Bucket list already returns `document_count` + `node_count` per bucket | `frontend-react/src/lib/api.ts:79-85`, `backend/app/api/buckets.py:52` |
| Ingest status returns `processing` status counts + extraction pause state | `backend/app/api/ingest.py:62-100` |
| No stats/links-count endpoint exists | grep of `backend/app/api/*.py` |
| Active bucket is a localStorage helper, per-call argument | `frontend-react/src/lib/activeBucket.ts` |
| `lucide-react ^1.14.0` present in BOTH repos; in aw-knowledgeable it sits in `devDependencies` | both `package.json`s (aw-k line 35) |
| Tests query by role/text, never by class | grep of `*.test.tsx` |

## 2. Tokens (D-V1): copy AP-MT's, verbatim

`frontend-react/tailwind.config.js` takes AP-MT's `theme.extend` block
byte-for-byte (`colors` + `fontFamily` from
`agents-platform-multitenant/frontend/tailwind.config.js:6-21`).
`darkMode: "class"` is already set on both sides; the tokens are **literal
dark values**, not `dark:` variants — AP-MT has one theme and so will we.

`frontend-react/src/index.css` takes AP-MT's `index.css` with two edits:
**skip** the `.react-flow__*` block (`index.css:108-126`; xyflow is not used
here — Onda U kept Cytoscape) and **keep** the existing
`html, body, #root { height: 100% }` rules. Everything else — element
defaults for inputs/buttons/links, scrollbar, `.btn`/`.btn-primary`/
`.btn-danger`, `.card`, `.badge-*`, `.kbd`, `.codebox`, the `pulse`
keyframes — comes across as-is. These are the restyle's workhorses: most
per-component work in §7 is "replace bespoke neutral-* clusters with `.btn` /
`.card` / `.badge-*` / token classes".

## 3. Shell (D-V2): AP-MT's left sidebar — decided, not open

Frederico decided this on the card; the only judgement left was what to copy
and what to drop.

**Copy** from `agents-platform-multitenant/frontend/src/App.tsx`:
- the `NAV` array shape (`:37-57`) — `{path, label, icon, exact}` with
  lucide-react icons;
- the `navLinks` block (`:77-96`) including the `border-l-2` active-state
  pattern (`border-accent text-fg bg-bg-3/60` active, muted hover otherwise);
- the desktop `aside` (`:119-121`, `w-56 shrink-0 border-r border-line
  bg-bg-2`);
- the mobile top bar + hamburger drawer (`:103-134`), including the
  close-on-route-change effect (`:73-75`).

**Drop**: `wsManager` (`:67-70` — aw-knowledgeable has no ws client) and the
`?view=telegram` embedded mode (`:62-63,100` — no embedding consumer today;
re-add when one exists).

**One deliberate deviation**: a small brand block at the top of the sidebar
(`aw-knowledgeable`, `text-sm font-semibold text-accent`, matching AP-MT's
mobile top-bar styling at `:112-114`). AP-MT's desktop sidebar has no brand
header; without one, this product's name appears nowhere on screen once the
old header dies.

**NAV content** (4 items — a 19-item rail copied for 3 routes was the
original card's worry; Dashboard makes it 4, and the Onda 1.1 entity layer is
the obvious 5th):

| path | label | icon (suggestion — coder's pick within lucide) |
|---|---|---|
| `/` | Dashboard | `LayoutDashboard` (exact) |
| `/library` | Library | `Library` or `BookOpen` |
| `/graph` | Graph | `Network` or `Share2` |
| `/playground` | Playground | `MessageCircle` (AP-MT's own) |

**Route change**: Library moves `/` → `/library`; `/` becomes Dashboard; the
`*` fallback still navigates to `/` (`frontend-react/src/App.tsx:39`). This
supersedes the card body's "routes do not change" for exactly this one move —
it is Frederico's instruction ("dashboard as the first/home screen"), not
architectural drift. No redirect shim: an old `/` bookmark now lands on
Dashboard, which is the requested behaviour.

## 4. Dark-only (D-V3): delete `dark:` variants, don't activate them

The tempting-but-wrong move is adding `class="dark"` to `<html>` so the
existing 379 `dark:` variants light up. That keeps two styling sources per
element forever and keeps `neutral-*` alive. Instead: **every component drops
both halves** and uses token classes directly. Mapping:

| today | becomes |
|---|---|
| `bg-neutral-50` / `dark:bg-neutral-950` (page) | `bg-bg` |
| `bg-white` / `dark:bg-neutral-900` (surfaces, modals, headers) | `bg-bg-2` (via `.card` where it fits) |
| `bg-neutral-100` / `dark:bg-neutral-800` (hovers, wells) | `bg-bg-3` |
| `border-neutral-200/300` / `dark:border-neutral-700/800` | `border-line` |
| `text-neutral-900` / `dark:text-neutral-100` | `text-fg` |
| `text-neutral-400/500` / `dark:text-neutral-400` | `text-muted` |
| `blue-*` accents (16× `border-blue-500`, 9× `bg-blue-500`, …) | `accent` |
| `red-*` (errors, danger buttons) | `err` / `.btn-danger` / `.badge-error` |
| `amber-*` (warnings, overflow banner `GraphView.tsx:544`) | `warn` / `.badge-warn` |
| `emerald-*` (success) | `ok` / `.badge-success` |
| `bg-black text-white dark:bg-white dark:text-black` CTAs (`GraphView.tsx:487`, `AuthGate.tsx:44`) | `.btn-primary` |

Translucent fills follow AP-MT's own rgba badge pattern
(`index.css:82-90`), not Tailwind `/10` opacity ad-hockery.

Acceptance is mechanical: `grep -rn 'neutral-\|dark:' frontend-react/src`
returns **zero hits** when done.

## 5. Dashboard (new scope) — AP-MT's layout, this product's truth

Mirror `agents-platform-multitenant/frontend/src/routes/Dashboard.tsx`:
`Page` title/subtitle, a 4-tile `Metric` row (`:95-102` — `.card
hover:border-accent`, big number, uppercase muted label, each tile links to
its route), then two-column cards. Copy AP-MT's `Page.tsx` into
`frontend-react/src/components/Page.tsx` (Dashboard needs it; Library/Graph
keep their own toolbars).

**Tiles — all from existing endpoints, two calls total:**

| tile | source | links to |
|---|---|---|
| Buckets | `GET /api/buckets` → `buckets.length` (`api.ts:349`, `buckets.py:52`) | `/library` |
| Documents | same call → Σ `document_count` (`api.ts:83`) | `/library` |
| Nodes | same call → Σ `node_count` (`api.ts:84`) | `/graph` |
| Processing | `GET /api/ingest/status` → `processing` counts (`ingest.py:71` — docs not yet `ready`: pending+extracting+embedding, failed called out) | `/library` |

When `extraction.paused_reason` is non-null, the Processing tile carries a
`.badge-warn` with `paused_explanation` (`ingest.py:84-90`) — a real operator
signal the backend already built precisely to be seen.

**No Links tile.** No endpoint serves a workspace link total, and the two
ways to fake one are both wrong: summing `KnowledgeNode.link_count` over
`listDocuments` double-counts document↔document edges and misses
entity↔entity edges entirely (`api.ts:67-69`), and a new `GET /api/stats`
violates both the card's "backend contract untouched" and the follow-up's
"existing API" constraint. If Frederico wants the tile, it's a one-route
backend follow-up card — a PO call, flagged in §10, not smuggled in here.

**"Don't fake zeros" means loading ≠ empty**: tiles render a skeleton/`—`
until the fetch resolves; a real `0` from a 200 renders as `0`. AP-MT's own
Dashboard gets this subtly wrong (`useState([])` renders 0 pre-fetch) — copy
the layout, not that.

**Recent activity** (AP-MT's recent-runs slot): recent **documents** —
`listDocuments(bucket)` per visible bucket in parallel (bucket counts are
single-digit today; the moment that's false this becomes the first real
backend ask, §9), merged, sorted `uploaded_at` desc, top 10. Row: label,
bucket name, processing badge (`ready→badge-success`, `failed→badge-error`,
`extracting/embedding→badge-running`, `pending→badge-pending` — a
`StatusBadge` sibling over the `ProcessingStatus` vocabulary,
`api.ts:60`), relative `uploaded_at`. Click → `setActiveBucket(bucket)`
(`activeBucket.ts`) then `/library`. Not a graph deep-link: `/graph?focus=`
is not an existing contract and this card doesn't invent one.

**Quick-start card** (AP-MT `:53-62`): upload a document → `/library`,
explore the graph → `/graph`, ask a question → `/playground`.

## 6. Graph view: the canvas is already done

`cytoscapeStyle.ts` needs **zero changes** — it already renders the exact
target palette (`:67-164`). The shell going dark is what fixes today's
light-chrome/dark-canvas clash. Two real edits:

1. `GraphView.tsx:529` (and the `bg-[#0d1117]/75` overlays at
   `:550,557,563,575`) swap hard-coded hex for `bg-bg` / `bg-bg/75` — same
   pixels, one source of truth.
2. The toolbar/overlay/modal chrome around the canvas (`GraphView.tsx:
   469-527` toolbar, Legend, Inspector, Breadcrumbs, AddLink/ConfirmDelete/
   DocumentViewer modals) restyles per §4's mapping like everything else.

**Accepted duplication**: `#0d1117`/`#30363d`/`#8b949e` now exist in both
`tailwind.config.js` and `cytoscapeStyle.ts` — Cytoscape style objects can't
read Tailwind classes. Do NOT bridge with `resolveConfig` (drags the full
config into the bundle); a comment in each file pointing at the other is the
whole fix.

## 7. Component inventory (all 18 files with `neutral-*`)

| file | work |
|---|---|
| `src/App.tsx` | replaced by sidebar shell (§3) |
| `src/components/AuthGate.tsx` | visual only (`:35-44` → `bg-bg`, `.card`, `.btn-primary`); `lib/authRedirect.ts` logic + CI byte-parity check (UI-U1) UNTOUCHED |
| `src/components/Modal.tsx` | `:18-34` → `bg-bg-2 border-line`; keep `z-40`, escape/overlay-click behaviour |
| `src/components/Toast.tsx` | token colors; align with `.badge-*` semantics |
| `src/components/SearchBar.tsx` | inputs inherit `index.css` element defaults; strip per-element border classes that now double up |
| `src/components/BucketGrid.tsx`, `LibraryGrid.tsx` | `.card` surfaces, token text/borders, `.badge-*` for processing status |
| `src/components/UploadModal.tsx`, `CreateBucketModal.tsx` | via restyled `Modal` + `.btn`/`.btn-primary` |
| `src/components/NotFoundState.tsx`, `ReadOnlyBadge.tsx` | tokens; `ReadOnlyBadge` → `.badge-warn` |
| `src/routes/Library.tsx` | toolbar/header tokens |
| `src/routes/GraphView.tsx` | §6 |
| `src/components/graph/Legend.tsx`, `Inspector.tsx`, `Breadcrumbs.tsx` | panel chrome → `bg-bg-2 border-line`; swatch colors stay (they mirror `cytoscapeStyle.ts`) |
| `src/components/graph/AddLinkModal.tsx`, `ConfirmDeleteModal.tsx`, `DocumentViewerModal.tsx` | via restyled `Modal`; internal tokens |
| `src/routes/Playground.tsx` | tokens; answer/code panels → `.codebox`; keep the 502/503 degraded-response panels VISIBLE (`api.ts:323-341` renders retrieval on failure by contract) |
| `src/main.tsx`, `src/index.css`, `tailwind.config.js` | §2 |

New files: `src/components/Page.tsx` (copied from AP-MT),
`src/routes/Dashboard.tsx`.

## 8. What does NOT change

- **Routes** except the single `/`→Dashboard, `/library` move (§3, Frederico's
  instruction). `/graph`, `/playground` URLs untouched.
- **`src/lib/api.ts`** — zero edits. The Dashboard consumes existing methods.
- **AuthGate logic / `authRedirect.ts`** — byte-identical copy + CI parity
  check from UI-U1 stays exactly as is; only AuthGate's visual shell restyles.
- **Cytoscape-vs-xyflow, `cytoscapeStyle.ts`, `GraphCanvas.tsx`** (`:284` has
  no visual classes to change).
- **Backend contract** — zero new endpoints, zero changed responses. The
  no-links-tile decision (§5) exists to keep this true.
- **`UI_V2` flag semantics** (`backend/app/main.py:131`) and the old vanilla
  `frontend/` (its deletion is card U5's job, not this one's).
- **Test strategy** — tests query roles/text (verified), restyle doesn't touch
  them except Dashboard's own new tests.

## 9. What this makes harder later

- **Token drift vs AP-MT.** This is the second hand-copy of AP-MT material
  into this repo (after `authRedirect.ts`). The first got a CI parity check
  because it's a *contract* (auth behaviour). Tokens deliberately get **no
  parity check** — they're a fork, not a contract; aw-knowledgeable may
  legitimately diverge (e.g. a graph-specific accent). Cost: when AP-MT
  improves its theme, nothing propagates. Two consumers of one palette is the
  threshold where a shared design-token package starts paying for itself;
  this doc is the second data point, cited for whoever proposes it.
- **Light theme becomes a rebuild, not a toggle.** Deleting `dark:` variants
  removes the dual-theme skeleton. It was dead code (no toggle existed), but
  resurrecting light mode later means re-touching every component rather than
  flipping a class.
- **Dashboard's client-side aggregation.** Σ over `/api/buckets` and N
  parallel `listDocuments` calls for recent activity are fine at today's
  single-digit bucket counts and wrong at 100 buckets. The first scaling
  complaint lands on a new `GET /api/stats` + recent-documents endpoint —
  named now so it arrives as a known invoice, not a surprise.
- **Verbatim shell copy.** Future AP-MT sidebar improvements (collapsible
  rail, grouping) don't propagate; same drift as tokens, same remedy.

## 10. Risks for the Coders

1. **Don't "preserve dark mode".** `darkMode: "class"` + no toggle means
   every `dark:` class is currently inert. Adding `class="dark"` instead of
   deleting variants would ship 379 dead strings and two styling sources per
   element. Delete both halves, write tokens.
2. **`.btn`/`.card`/`.badge-*` are plain CSS, not Tailwind.** They only exist
   if `index.css` is copied (§2). Forgetting it produces a built, test-green,
   visually broken app — no compiler error anywhere.
3. **`lucide-react` is in `devDependencies`** (`package.json:35`). Vite
   bundles it regardless so nothing breaks, but move it to `dependencies`
   when the sidebar starts importing it.
4. **Loading vs zero on Dashboard tiles** (§5) — "don't fake zeros" is part
   of the ask; `useState([])`-renders-0 is the AP-MT bug not to copy.
5. **Playground degraded shapes**: `askPlayground` deliberately returns a
   renderable body on 502/503 (`api.ts:337-339`). A restyle that hides the
   retrieval panel behind a new error layout breaks §12's
   declared-degradation contract silently — no test catches a visually
   hidden panel.
6. **Z-order**: existing `Modal` is `z-40` (`Modal.tsx:18`), AP-MT's mobile
   drawer is `z-50` (`App.tsx:125`). Copied as-is, an open drawer sits above
   a modal. Decide consciously (drawer `z-30` is the cheap fix); don't
   discover it on a phone.
7. **Acceptance sweep**: `grep -rn 'neutral-\|dark:' frontend-react/src`
   must return nothing; `npm run build` and `npm test` green
   (`frontend-react/package.json` scripts); screenshot of every route
   attached to the QA trail.

## 11. Cards, in dependency order (for `ux-coder-sonnet`)

| # | card | depends on | scope |
|---|---|---|---|
| VP1 | tokens + sidebar shell | — | §2 `tailwind.config.js` + `index.css`; §3 `App.tsx` sidebar (incl. `/library` move, Dashboard placeholder route rendering an empty `Page` so nav lands somewhere); copy `Page.tsx`; AuthGate visual; `lucide-react` → `dependencies` |
| VP2 | Dashboard route | VP1 | §5 in full: tiles (2 API calls), processing badge + paused warn, recent documents, quick-start; tests for tile math + loading-vs-zero |
| VP3 | Library family restyle | VP1 | `Library.tsx`, `BucketGrid`, `LibraryGrid`, `SearchBar`, `UploadModal`, `CreateBucketModal`, `Modal`, `Toast`, `NotFoundState`, `ReadOnlyBadge` per §4/§7 |
| VP4 | Graph family restyle | VP1 (not VP3 — but do NOT re-edit `Modal.tsx`, it's VP3's) | §6: `GraphView` chrome + overlays + `bg-bg` cleanup, `Legend`, `Inspector`, `Breadcrumbs`, 3 graph modals |
| VP5 | Playground + final sweep | VP1 (sweep: all) | `Playground.tsx` restyle; repo-wide §10.7 sweep: grep zero-hit, build, tests, per-route screenshots |

VP2–VP4 can run in parallel after VP1. VP5's restyle can too, but its sweep
is the finish line and runs last.

---

*Cross-references: `aw-knowledgeable-ui-decoupled.md` (Onda U — stack, auth,
parity checklist; its §0 visual-scope carve-out is superseded by this doc),
`aw-knowledgeable-infra.md` (D1 and its amendments),
`aw-knowledgeable-v2-retrieval.md` §12 (playground contract the restyle must
not break).*
