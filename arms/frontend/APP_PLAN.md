# Picky web app — plan and task list

> **Update (Oct 17, evening): Phia-style pivot.** Picky is now a general shopping + money assistant, with the robot as
> its in-store body. New: **Find & compare** (`/find`: 7 retailers incl. open-box + warehouse, 13-week price history,
> Good price / Wait / Skip verdicts, cheaper swaps), **Ask Picky** (`/ask` + a drawer on every page: Claude with
> deterministic tools over purchases, budget and prices; offline fallback), a **T-Mobile store** alongside the grocery
> store (own layout, catalog, SKIP/WAIT advice, mandate), and a visible **arm pick + basket cam** in the live feed.
> Sections below describe the original grocery flow, which still works unchanged.

The shopper-facing web app for the demo. It tracks grocery purchases, places orders the robot fulfills, and shows the
robot's live view while it shops. It must look finished on a projector today, even with the gateway down. It also
must not paint us into a corner, so the same screens can run on real gateway events later.

Scope note: this covers the shopper side of `README.md` (P0 + some P1). The merchant dashboard stays in the README backlog.

## 1. How the app works

### The user
**Maya** (fictional, `data/fixtures/users.yaml`). Household of 2, $40 default trip budget, likes spicy and vegetarian
food, home store **Picky Market – The Ave** (`store_ave`). No login: the app opens as Maya, and the account switcher
offers Jordan.

### The loop the app tells
```
 Track ──────────▶ Order ──────────────▶ Watch ─────────────▶ Track (again)
 what you bought,  intent → list,        robot drives the      receipt lands in history,
 what's running    in-store vs online,   route, live camera,   ratings feed the profile,
 low, monthly      recs with reasons,    asks before extras,   next order is smarter
 spend vs budget   mandate preview       you approve checkout
```

### Screens (Next.js App Router)

| Route | Name in nav | What it answers | Key pieces |
|---|---|---|---|
| `/` | Overview | "Where do I stand this month, and is anything in progress?" | Live-order strip (only while a trip runs), month spend vs budget, "running low" restock list from purchase cadence, last 3 trips |
| `/purchases` | Purchases | "What did I buy, when, for how much, and was it good?" | Weekly spend chart by category, trips ledger (expand for line items and ratings), most-bought items with average price paid |
| `/order` | New order | "Get me X under $Y." | Intent box with presets → generated list (qty, price, in-store / online badge, substitution) → recommendation cards (reason, signals, Sponsored label when set) → fulfillment choice → mandate preview → Place order |
| `/trips/[id]` | (from order or strip) | "What is the robot doing right now?" | **Live feed** (main), store map with route, dashed detours and robot dot, stop timeline, cart with basket check, budget bar, inline approval cards (detour yes/no, top-3 produce pick, checkout), event log |
| `/permissions` | Permissions | "What can the agent do without me?" | Mandate (cap, per-item cap, ask-above, blocked categories), learned taste profile with delete, approvals history, recommendation transparency |

**Fulfillment modes** (on `/order`):
1. **I'll shop with the robot.** Shows a 6-digit login code to enter on the robot in store. The trip starts when the robot logs in, and in demo mode a "Robot logged in" button starts it.
2. **Robot shops, I pick up.** The robot runs the route alone. Every optional detour becomes an approval on your phone or web, and the order ends at pickup.

### The live feed (the robot livestream)
One `<LiveFeed>` component with a source ladder, the same one as the demo-script fallback ladder:

| Rung | Source | When |
|---|---|---|
| 1 | Gateway `WS /ws/frames?trip_id=` (blurred JPEG frames → `<img>`) | `NEXT_PUBLIC_GATEWAY_URL` set and the trip is a gateway trip |
| 2 | Direct stream URL: MJPEG (`<img>`), or MP4/WebM (`<video>`) | `NEXT_PUBLIC_STREAM_URL` set, or pasted in the feed's source menu |
| 3 | Recorded backup video `public/backup-trip.mp4` | File present |
| 4 | **Simulated robot camera** (canvas): the shelf in front of the robot drawn from the planogram, with product label colors, a scan reticle on the target slot and a pick animation | Always available |

The feed header always says which rung is live ("Live · sim camera", "Recorded", "Simulated"), plus "Faces blurred"
when the source guarantees it. We never pretend a simulation is live.

### Data flow
```
data/products.yaml ─┐
data/planograms/*  ─┼─ scripts/gen-data.mjs ─▶ src/data/generated/*.json   (never hand-edit; rule 5)
data/fixtures/*    ─┘

           ┌─────────────── TripSource (interface) ───────────────┐
           │ ReplaySource: deterministic client-side trip engine   │  ← now (no backend needed)
           │ GatewaySource: POST /api/requests + WS /ws (contracts)│  ← src/lib/gateway, same store
           └──────────────────────┬────────────────────────────────┘
                                  ▼ events named after contracts.events
                      tripStore (zustand reducer) ─▶ screens
```
- The replay engine emits events with **contract event names** (`route.planned`, `robot.state`, `robot.arrived`,
  `item.picked`, `cart.updated`, `approval.requested`, `approval.resolved`, `checkout.completed`, ...). The gateway
  adapter is mostly plumbing.
- Approvals **pause** the replay until the user answers, so the demo is driven by clicks, not timers. An "auto-answer"
  toggle exists for unattended loops on a second screen.
- Money stays **integer cents** end to end. It's formatted only at render.
- Purchase history beyond the two fixture trips is **generated deterministically** (seeded) by the generator script,
  flagged `synthetic: true`, and labeled "demo history" in the UI.

### Trust surfaces (15% of the score; visible on every relevant screen)
- Every recommendation shows its reason, its signals, and "Not sponsored" or "Sponsored".
- The mandate verdict shows before you place an order, and again at checkout with reason codes.
- Checkout always asks. The receipt says **"Test mode, no real charge"**.
- The live feed says "Faces blurred" only when the source guarantees blurring.

## 2. Visual direction (per the "AI-generated look" guide)

- **Replace the Tailwind palette, don't extend it.** `@theme { --color-*: initial; }`, then our own tokens, so
  `bg-indigo-600` can't compile. Tokens: `paper` (warm off-white), `ink` (green-black), `line`, `muted`, `tomato`
  (accent, actions), `basil` (in-store, success), `turmeric` (detours, warnings), `plum` (online orders).
- **Type:** a display face with character for headings and big numbers (Bricolage Grotesque), IBM Plex Sans for
  body, IBM Plex Mono for prices, codes and timestamps (tabular figures).
- **Components:** 1px borders and color contrast, not radius plus shadow. Radius ≤ 3px. No drop shadows.
  One card vocabulary: `default` (bordered), `inset` (left accent rule), `flat` (tinted, no border).
- **Layout:** narrow left rail for nav. Asymmetric grids: the live page is about 8/4 with the feed overlapping the
  map's top edge, and Overview leads with one big number rather than a row of identical stat tiles.
- **Copy:** specific and numeric ("$31.40 of $40", "Limes: you buy them every 9 days, last bought 9 days ago").
  No "Unlock / Empower / Transform", and no two-abstract-noun headings.
- **Icons:** a few, inline SVG, stroke 1.5. No emoji in UI chrome.

## 3. Task list

Legend: **P0** = needed for the demo, **P1** = makes it look finished, **P2** = later.

### Foundation
- [x] P0 Scaffold Next.js 16 + React 19 + Tailwind 4 + TypeScript in `arms/frontend` (keep `README.md`)
- [x] P0 Theme tokens, fonts and base primitives (`Card`, `Button`, `Badge`, `Money`, `Meter`)
- [x] P0 App shell: left rail nav, user switcher, store and connection chip ("Demo data" / "Gateway connected")
- [x] P0 `scripts/gen-data.mjs`: YAML → `src/data/generated/*.json` (products, store_ave planogram, users, purchases + seeded history)

### Track
- [x] P0 Overview: month spend vs budget, live-order strip, running-low list, recent trips
- [x] P0 Purchases: weekly spend by category chart, trips ledger with line items and ratings, most-bought table
- [x] P1 Rating an item from the ledger (local state) → "profile will learn" note

### Order
- [x] P0 Intent box with presets ("tacos for 6, under $40, one vegetarian", "weekly basics")
- [x] P0 Deterministic intent → list expansion for the presets (qty scaled to party size; out of stock → online)
- [x] P0 Recommendation cards (reason, signals, sponsored label) with include/skip toggles
- [x] P0 Fulfillment choice (shop with robot + login code / robot picks for pickup), mandate preview, Place order

### Watch (live trip)
- [x] P0 Replay engine: route from the planogram (stops → approach poses → corridor waypoints), timed events, pause on approvals
- [x] P0 Trip store (zustand reducer over contract-named events)
- [x] P0 Store map SVG: fixtures, route (solid), detours (dashed turmeric), stops by status, robot pose
- [x] P0 LiveFeed with the 4-rung source ladder + simulated canvas camera
- [x] P0 Stop timeline, cart with basket check, budget bar
- [x] P0 Approval cards: detour yes/no, top-3 avocado pick by freshness, checkout approval with mandate reasons
- [x] P0 Receipt (test mode) → "Added to purchases" link back to Track
- [x] P1 Event log drawer (raw events, good for the Tech Execution score)
- [x] P1 Playback controls: speed 1×/2×/4×, auto-answer toggle, restart

### Trust
- [x] P1 Permissions page: mandate view/edit (local), taste profile chips with delete, approvals history

### Later (P2, not in this pass)
- [x] GatewaySource (`src/lib/gateway/tripSource.ts`): `POST /api/requests`, `GET /api/trips/{id}` + `WS /ws?trip_id=`
  (snapshot, seq dedupe, `after_seq` resume), `POST /api/approvals/{id}`, brain thinking log, MJPEG/snapshot cameras;
  `/ws/frames` source fixed for v0.2 (`camera.frame`, `?trip_id=|robot_id=`)
- [x] Generated TS types from `contracts` (`pnpm gen-types` → `src/lib/gateway/{api-types,events}.ts`)
- [ ] Merchant dashboard, voice, trip-2 side-by-side

### Done when
- `pnpm build` passes and `pnpm tsc --noEmit` is clean
- Order → live trip → approvals → receipt → shows in Purchases works with no backend
- Readable at 1440×900 and 1080p; no default Tailwind palette in the bundle
