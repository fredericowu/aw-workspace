# aw-knowledgeable — the decoupled UI (Onda U)

Architect design, 2026-09-28. Card `design:aw-knowledgeable-decoupled-react-ui`
(`3e95bf3b-9510-810e-be55-fcba3c4274ac`). This reverses D1
(`aw-knowledgeable-infra.md` §5a) on the product owner's explicit instruction —
see the dated amendment in that file for why this is D1's own invoice coming
due, not a contradiction of its reasoning.

The request, verbatim (Frederico, Telegram, 28/09):

> "a UI tem que está totalmente desacoplada, vai levar o JWT token e usar APIs
> pra falar com o back, react com python, se inspira no design do
> agents-platform-multitenant"

**Amendment 2026-10-10:** §0's "the visual half stays per-app" clause is
superseded — Frederico asked for AP-MT's actual look (dark theme + sidebar
shell), and, in a same-day follow-up, a Dashboard home route at `/` (Library
moves to `/library`). See `aw-knowledgeable-ui-visual-parity.md` (card
`3f55bf3b-9510-81ca-bba7-d7afe271ac87`). Everything else here stands.

---

## 0. The decision, in five sentences someone could disagree with

The frontend is rewritten in **React 19 + TypeScript + Vite + Tailwind 3**,
mirroring AP-MT's `frontend/package.json` pins, built next to the vanilla UI in
`frontend-react/` and swapped in behind a server flag once the parity checklist
is green. The graph view **keeps Cytoscape** — measured head-to-head against
`@xyflow/react` at the node counts the entity layer will produce, xyflow is
unusable at 3 000 nodes (13 fps pan) where Cytoscape holds 29–60 fps — wrapped
in exactly one React component that owns the instance lifecycle. The auth
gate's **subtle half becomes a byte-identical copy** of an extracted AP-MT
module with a CI parity check, ending the hand-translation defect D1's point 6
documented; the visual half stays per-app. **Decoupling is contract-level, not
deploy-level**: the UI stays a static artifact in the same container and the
same CI/CD pipeline, talking only HTTP with the identity JWT — no template
rendering, no server session — and is CDN-liftable later without touching the
backend. The V5a/V5b behaviour set is carried as a **parity checklist** (spec,
not code), absorbing the two open UI bugs.

---

## 1. Stack — copy AP-MT's pins, not its vibes

Read from `repos/agents-platform-multitenant/frontend/package.json` on
2026-09-28:

| dep | version | note |
|---|---|---|
| react / react-dom | ^19.2 | |
| typescript | ~6.0 | `"build": "tsc -b && vite build"` — restores the build-time contract check D1 gave up (its cost #7) |
| vite | ^8.0 | rolldown-based; `@vitejs/plugin-react` ^6 |
| tailwindcss | ^3.4 | **not v4** — parity with AP-MT's postcss/autoprefixer setup |
| react-router-dom | ^7 | |
| zustand | ^5 | app state: active bucket, view, inspector selection |
| lucide-react | (devDep in AP-MT) | icons |
| cytoscape | ^3.30 | kept — §2 |

`@xyflow/react` is **not** added (§2). Dev server keeps AP-MT's
`server.proxy["/api"]` shape (`agents-platform-multitenant/frontend/vite.config.ts`).

Visual direction: AP-MT's neutral Tailwind palette (`bg-neutral-50/-950`,
bordered cards, `rounded-lg`, dark-mode variants — see its `AuthGate.tsx:33-52`
for the vocabulary). The prototype's hand-written custom properties
(`style.css:6-33`) are retired with the vanilla code; their *information
design* (legend, inspector layout, four-step upload) survives via §6.

## 2. The graph view: Cytoscape stays — measured, not opined

### The measurement

Benchmark harness at `.tmp/aw-knowledgeable/ui-bench/` (kept, with
`results.json`): identical synthetic graphs — seeded RNG, clustered positions
**precomputed** so layout cost is excluded (both libs would run the same
external layout; the comparison is rendering architecture), labels on every
node, two edge kinds styled differently, ~2.5 edges/node. Chromium headless
shell (Playwright 1.55 build 1234, SwiftShader, 1440×900), `cytoscape@3.30.2`
from this repo's lockfile, `@xyflow/react@12.10.2` + React 19.2.6 bundled from
AP-MT's lockfile. 90-frame programmatic pan and zoom, rAF-timed.

| nodes (edges) | metric | Cytoscape | @xyflow/react | xyflow + `onlyRenderVisibleElements` |
|---|---|---|---|---|
| 100 (250) | init ms / pan fps / zoom fps | 116 / 59 / 60 | 225 / 60 / 59 | 204 / 60 / 59 |
| 300 (750) | | 226 / 56 / 60 | 430 / 58 / 60 | 419 / 51 / 36 |
| 1 000 (2 500) | | 580 / **41** / **60** | 941 / 34 / 41 | 1 073 / 19 / 18 |
| 3 000 (7 499) | | 1 174 / **29** / **60** | 2 599 / **13** / **15** | 2 275 / **6** / **5** |

Three findings that decide it:

1. **Below ~300 nodes the libraries are equivalent.** If the graph view were
   AP-MT's flow editor (dozens of rich nodes), xyflow would be the right call —
   which is why AP-MT using it is not evidence for us.
2. **At 1 000+ nodes, DOM-per-node loses to canvas** and the gap compounds:
   3 000 nodes is 29 fps vs 13 pan, 60 vs 15 zoom, and 2.2× the mount time.
3. **The canonical xyflow mitigation makes the dense case worse.**
   `onlyRenderVisibleElements` costs more in visibility bookkeeping than it
   saves when most nodes are in-viewport — 6/5 fps at 3 000 — and an overview
   fit is precisely the all-in-viewport case.

### Why the 1 000–3 000 band is the design point, not paranoia

Today the counts are small (the MCP connector tenant: 7 documents, every one
`link_count: 0` — verified live today). But the frame budget must survive the
entity layer being designed in parallel
(`design:aw-knowledgeable-entity-layer-incremental`):

- Measured corpus density: **1 232 chunks over 9 workspace documents**
  (v2-retrieval §5 Amendment 1) — ~137 chunks per hand-written doc.
- Entity extraction runs **per chunk** (plano Onda 1.2). Even at a handful of
  entities per chunk with per-bucket dedup, a long document plausibly touches
  50–200 distinct entities via `MENTIONS`.
- Derived doc-doc edges are bounded (≤ 8 per doc per `via`, §5 Amendment 2),
  but `MENTIONS` is not, and a depth-2 neighbourhood through one hub entity
  fans out to every document mentioning it.

So a depth-2 frame after Onda 1 realistically lands in the hundreds-to-low
-thousands of nodes. The bench brackets that: xyflow is already degraded at
1 000 and unusable at 3 000; Cytoscape's one weak number (29 fps pan at 3 000,
p95 187 ms) is workable and improvable with its own knobs (`textureOnViewport`,
label hiding below a zoom threshold).

### Decision

**Cytoscape, wrapped in exactly one React component.** Contract:

- `frontend-react/src/components/GraphCanvas.tsx` owns the only
  `cytoscape()` instance. Created in a `useEffect` on mount against a
  `ref` div, `cy.destroy()` on unmount. No third-party React-cytoscape
  wrapper — the maintained ones re-diff the full elements JSON per render,
  which is the worst of both worlds; we own the diff.
- Props are **data + callbacks**, never the cy instance: `elements` diffs
  imperatively (`cy.batch` + `add`/`remove`), `focusId` pans/fits, events
  (`tap`, expand) surface as `onSelect`/`onExpand` callbacks. The instance
  never escapes the component; everything else in the app is ordinary React.
- **Node budget**: the component caps a rendered frame at ~1 500 nodes and
  surfaces the overflow through the same "N links hidden" affordance §5 of
  this doc specifies for scope-hidden links (distinct wording: "N more not
  shown — narrow the focus"). The API already can't return the whole graph
  (no such endpoint, by §5 of infra.md); this is the client-side half of the
  same discipline.

### Rejected

- **`@xyflow/react`** — the stack-consistency candidate, rejected on the
  numbers above. What would change it: the graph view becoming an *editor*
  (drag-to-connect, rich HTML nodes) at AP-MT-editor scale, or the node
  budget being permanently capped below ~500. Neither is the product: the
  screen is a dense read-mostly neighbourhood explorer.
- **Cytoscape via `react-cytoscapejs`** — unmaintained (last release 2023,
  React 19 peer conflict) and diff-by-JSON per render.
- **Re-benchmark deferral** ("decide when entity counts are real") — the
  shell's central screen can't be provisional; a later flip would be a second
  rewrite of the main view. The bench brackets any plausible outcome of the
  entity design.

## 3. The auth gate: one subtle half, byte-identical, parity-checked

D1's point 6 named the defect: the gate exists twice in two languages, and the
subtle half — the `/api/me` exemption preventing the redirect loop, the
never-resolving promise during navigation — is what silently drifts. React+TS
removes the two-languages excuse. What remains is keeping two copies honest.

### Decision

Split the gate at the subtle/visual seam:

- **Subtle half — `src/lib/authRedirect.ts`**, extracted in AP-MT first (card
  U0) from `api.ts:505-523` + the `CONSOLE_*` env reads: `redirectToLogin()`,
  the 401 interceptor with the `path !== "/api/me"` exemption, the
  never-resolving promise. aw-knowledgeable's copy is **byte-identical**, and
  a CI job diffs the two files (via `gh api` on the AP-MT repo) — drift fails
  the build. The header comment in both names the counterpart path and the
  rule: *edit both or neither*.
- **Visual half — `AuthGate.tsx` + `LoggedOutScreen`** — per-app. Same
  behaviour contract as AP-MT's `AuthGate.tsx:10-27` (check `GET /api/me`
  directly, 401 → logged-out screen with an explicit **Log in** anchor to
  `${CONSOLE_BASE_URL}/login?return=<encoded href>`, explicit click, no
  bounce), but the markup and branding are this product's own. In React the
  gate returning `null` until `/api/me` answers means the shell is never
  mounted — D1's risk 11 (hide `#app` by hand) dissolves structurally.

### Rejected

- **A shared npm package** (`@aw/auth-gate`). The right shape at three-plus
  consumers or when a design system is extracted — today there are two
  consumers, no private registry in the estate, and a package adds a publish
  step to every fix. The byte-identical-plus-CI-check contract buys the same
  "a fix propagates or the build screams" property for one CI job. Revisit at
  the third consumer.
- **Keep hand translation, add comments** — that is D1's mitigation, already
  judged insufficient by this card's existence.
- **One gate lives, the other dies** — both products need gates; there is
  nothing to kill.

## 4. Decoupling: contract-level now, deploy-level when something needs it

### Decision

- **What "decoupled" binds**: the UI is a static artifact (`dist/`), zero
  Python-rendered templates, zero server session; every byte of data arrives
  over `/api/*` with the identity JWT. Nothing in the backend knows the UI
  exists except the static mount + SPA fallback
  (`backend/app/main.py`, mirroring
  `agents-platform-multitenant/backend/app/main.py:466-484`).
- **Token transport**: in the browser the JWT travels as the HttpOnly
  `aw_id_jwt` cookie — that cookie *is* the identity JWT, minted by
  aw-console/aw-backend; same-origin means no CORS and no token in
  JS-readable storage. Programmatic callers use `Authorization: Bearer` with
  the same token — `backend/app/core/identity.py:158-162` already accepts
  both. "Leva o JWT e usa APIs" is satisfied by contract; putting the token
  in `localStorage` to make the header literal for browsers too would be an
  XSS downgrade with no functional gain, and is explicitly not done.
- **Packaging**: same repo, same container, two-stage Dockerfile — stage 1
  builds `frontend-react/` (node:22-slim, `npm ci`, `tsc -b && vite build`),
  unchanged CI/CD (`aw-knowledgeable-cicd.md` §1: the dedicated bare-metal
  runner). During the transition both frontends build; `UI_V2=1` on the
  backend picks which `dist/` the SPA fallback serves (default old). The
  closing card (U5) flips the default and **deletes** `frontend/`.

### Rejected

- **Separate deploy (CDN / second container) now.** It buys nothing a user
  can see and costs: a second pipeline on a host whose deploy mechanism is a
  single dedicated runner, CORS-with-credentials config, and cookie scope
  work. The static artifact is liftable later precisely *because* the
  contract half is done now; do it when a real need (edge caching, backend
  redeploy decoupling) shows up, and pay only the ops half then.
- **Big-bang replace of `frontend/`** (no flag, no parallel dir). Master is
  the working branch here (house rule) and V5a/V5b behaviour is approved and
  live; a half-rewritten UI on master would be deployed by the next push.
  The flag keeps every intermediate commit shippable. The cost — two
  frontends in-repo for some weeks — is bounded by U5 being an explicit card
  whose deliverable is the deletion.

## 5. Scope-aware UI: 403 and 404 are different screens, and the token may be old

From v2-retrieval §7bis, which the UI must render rather than flatten:

- **404 on a bucket** = "this token has no business knowing it exists". The
  UI shows its ordinary not-found state — indistinguishable from a typo'd
  URL, *by design*. No "you lack permission" copy: that copy would leak
  existence, undoing §7bis Decision 1 in the presentation layer.
- **403 on write, read granted** = "you know it exists; you may not do that".
  The UI renders the bucket **read-only**: a badge on the bucket header,
  write affordances (upload, add-link, create-bucket-scoped actions)
  disabled with a "read-only — ask a tenant admin for write" hint. Never a
  dead-end error page.
- The API client (`src/lib/api.ts`) turns these into a typed
  `ApiError{status, bucket?}` so views branch on status, not on message
  strings; K1's two expect-classes (`404-no-bucket-scope`,
  `403-bucket-read-only`) are the backend contract the types mirror.
- **`GET /api/buckets` already returns only visible buckets** (§7bis
  Decision 2) — the bucket grid renders what arrives and never infers "there
  might be more".
- **Claim tail**: tokens minted before aw-backend T2 deploy carry no
  `tenant`/`aw_claims_v` claim for up to 30 days (`JWT_EXPIRY_HOURS` =
  24×30). The UI never parses the JWT — it reads `/api/me` and treats every
  scope question as an API answer. That one rule makes the tail invisible to
  the frontend.

## 6. Entity-ready shell — designed in, not retrofitted

Coordinated with `design:aw-knowledgeable-entity-layer-incremental` (in
flight; its §5 amendment lands in `aw-knowledgeable-v2-retrieval.md`, which
this doc deliberately does not edit). The component contracts below let its
output land as *data*, not as UI refactor:

1. **Edges carry a reason.** The edge model in the store is
   `{id, source, target, via?: "entity"|"topic"|"embedding", kind:
   "manual"|"derived", reason?: string}`. The inspector's edge panel renders
   `reason` (the LLM-written relation description) when present; absent =
   render the `via` channel only. Nothing crashes on absence.
2. **`via` is a first-class visual channel**: legend entries and per-channel
   edge styles (today: `LINKS_TO` solid / `RELATED_TO` dashed; `via:
   "entity"` gets its own style when it exists), plus a legend-side toggle to
   filter channels — the D1-era legend (`index.html:102`) becomes
   interactive.
3. **`links_manual` vs `links_derived` are separate inspector sections**
   (Onda 0.1 splits the counter; the UI splits the list the same way).
4. **Hidden-links count is nullable.** The "N links hidden by scope" badge
   renders only when the field arrives (it is per-bucket configurable and off
   for sensitive buckets) — absence of the field is not zero, it is
   *unknown*, and renders as nothing.
5. **`(:Event)` nodes with dates (Onda 2)**: node model carries optional
   `occurred_from/occurred_to/precision`; `GraphCanvas` styles unknown node
   kinds by a lookup with a default, so a new kind renders acceptably before
   it renders specially.

## 7. Parity checklist — V5a/V5b behaviour is spec, not code

The rewrite ships when all of these hold (each is an existing approved
behaviour; file refs are the vanilla implementation being replaced):

- [ ] Bucket view: grid with document counts, create-bucket modal with slug
      preview (`app.js:97-198`)
- [ ] Active-bucket switcher; bucket scoped into every API call
- [ ] Library grid with processing-state badges (`renderProcessingBadge`,
      `app.js:232`)
- [ ] Four-step upload flow; **absorbs** the open "Title field does nothing"
      bug — the field is wired end-to-end or removed, either closes the card
- [ ] Semantic search in the top bar showing `topic_path` + snippet
      (`renderSemanticResults`, `app.js:730`); link-picker search stays
      lexical substring with `exclude=` (`app.js:612`)
- [ ] Topic-tree overview as the graph entry point (`openTreeOverview`,
      `app.js:494`); drill topic → documents → focus/depth neighbourhood
- [ ] Focus/depth neighbourhood with expand-neighbours; **absorbs** the open
      "expand pushes nodes off-screen" bug — expanded nodes are brought into
      view (animated fit to the added collection), acceptance of the graph
      card, not a follow-up
- [ ] `RELATED_TO` vs `LINKS_TO` rendered distinctly + legend
- [ ] Inspector: node details, links list with delete, tabbed
      (`renderInspector`, `app.js:634`)
- [ ] Breadcrumbs (`renderBreadcrumbs`, `app.js:421`)
- [ ] AuthGate behaviour per §3 (explicit login click, no shell flash —
      structural in React)
- [ ] 403/404 rendered per §5

## 8. What this makes harder later

1. **A React rewrite is a one-way door for the "reuse the approved UI"
   argument.** D1's cheapest asset — the prototype's code verbatim — is
   spent. If the React UI stalls mid-transition, the fallback is the flag
   (old dist), but every new feature from Onda 0/1 lands twice or only in
   the new one. Mitigation: U2–U4 are scoped to reach parity before any new
   feature work lands in the UI, and U5 deletes the fallback.
2. **The byte-identical gate couples our CI to AP-MT's repo layout.** If
   AP-MT moves or rewrites `authRedirect.ts`, our parity job goes red
   through no local change. That is the designed behaviour (drift must be
   loud), but it makes AP-MT refactors slightly more expensive — their coder
   now owns telling us. At a third consumer, promote to a package.
3. **Tailwind utility classes bind the UI to AP-MT's visual generation.**
   When a real design system is extracted estate-wide, this codebase now
   *can* adopt it (that is the point) — but until then, "inspired by AP-MT"
   drifts as AP-MT drifts, with no token source of truth. Accepted; the
   alternative was inventing a third visual language.
4. **Two frontends in-repo during the transition** double `npm ci` time in
   stage 1 and invite edits to the dying one. Bounded by U5; any fix needed
   in `frontend/` during the window must be mirrored in the parity
   checklist.
5. **The node budget (~1 500) becomes an invisible product ceiling** if the
   affordance copy is vague. The "N more not shown" surface must always name
   the narrowing action, or dense-graph users will read it as a bug — the
   same silent-downgrade rule §5 Amendment 2 applies to edges.

## 9. Risks for the Coders

1. **Do not port the vanilla API paths blindly.** `app.js` calls
   `api/nodes/{id}`, `api/search?q=`, JSON upload — D1's risk 10 records the
   drift against the spec'd paths. The React client is written against the
   **live backend routes** (read `backend/app/api/*.py`, not `app.js`), and
   TypeScript types are transcribed from the actual response models.
2. **`tsc -b` will surface real backend/frontend mismatches on day one** —
   that is it working. Fix the type or the backend, never `any` it away; the
   unchecked-contract era is what this rewrite ends.
3. **Cytoscape instance leaks.** StrictMode double-mounts effects in dev:
   `GraphCanvas`'s effect must be idempotent (`destroy()` in cleanup, guard
   against double-init) or dev shows two canvases and phantom event
   handlers.
4. **The parity CI job needs cross-repo read access** (`gh api` on
   `fredericowu/agents-platform-multitenant` from the aw-knowledgeable
   workflow). Verify a token with that scope exists on the runner before
   building the job; if none does, that is a blocker to raise, not a reason
   to silently drop the check (memory: security-scan runs hit gh rate limits
   — use a conditional job on push to master, not per-PR).
5. **Vite 8 is rolldown-based** — plugin behaviour differs from Vite 5 lore.
   Copy AP-MT's `vite.config.ts` and `tsconfig*.json` as the starting point
   rather than generating fresh ones.
6. **The backend flag touches `backend/app/main.py`** while Onda 0 coders
   are in `core/graph.py` and `ingest/worker.py` — different files, same
   tree. Commit only your paths (memory:
   `concurrent-agents-share-one-working-tree`).
7. **`GET /api/me` is the one path that must never redirect** — the
   exemption lives in the copied `authRedirect.ts`; if you find yourself
   editing that file locally, stop: the edit belongs in AP-MT first.
8. **v2-retrieval §6 still says "vanilla JS, no framework".** Its five UI
   behaviours remain the spec (they are §7's checklist); its stack sentence
   is superseded by this doc. Do not "fix" that file in a UI card — the
   entity-layer Architect owns it right now (file collision).

## 10. Cards (dependency order)

```
U0 (AP-MT) ──► U1 (scaffold+gate+flag) ──► U2 (library/buckets/search)
                                      ├──► U3 (GraphCanvas/tree/inspector)
                                      └──► U4 (scope-aware states)
U2+U3+U4 ──► U5 (parity audit, flip UI_V2, delete frontend/)
```

- **U0** — AP-MT: extract `authRedirect.ts` (no behaviour change) —
  coder-sonnet, AP-MT repo.
- **U1** — scaffold `frontend-react/` (stack §1), AuthGate (§3), typed API
  client (§5), router shell, Dockerfile stage + `UI_V2` flag, parity CI job —
  coder-sonnet.
- **U2** — library + buckets + upload + search views to parity — ux-coder-
  sonnet.
- **U3** — `GraphCanvas` + topic tree + inspector + legend (§2, §6) —
  ux-coder-sonnet.
- **U4** — 403/404/read-only/claim-tail states (§5) — coder-sonnet.
- **U5** — parity checklist audit (§7), flip default, delete `frontend/`,
  update `cicd.md` build refs — coder-sonnet, QA gate before the deletion.

U2/U3/U4 may run in parallel **only if** they stay in disjoint view
directories; they all touch `frontend-react/src/lib` types — U1 lands the
shared types first precisely so they don't.
