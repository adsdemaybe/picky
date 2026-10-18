# Frontend ↔ backend map (web app → gateway)

How the teammate's web app (`arms/frontend` on `origin/arm/frontend` @ `3d28f73`, landed 17:09 PT today) maps
onto the gateway, so it can be wired up. Written 2026-10-17 ~17:45 PT from:
- the frontend code (read-only), `arms/frontend/README.md`, `APP_PLAN.md` ("Phia-style pivot");
- the **v0.1 gateway running on :8000** (`/health` → contracts 0.1.0, all arms fake; `/openapi.json` = 25 REST
  paths / 28 operations; `/ws` and `/ws/frames` probed with a WS client; CORS preflight checked);
- the **v0.2 spec** `contracts/api.md` + `contracts/events.py` (CONTRACTS_VERSION 0.2.0, being built now).

> **Update 18:13 PT: `6fc1c41` ("redesign with shadcn, add motion, media slots, and live feed").** Re-checked. The
> mapping below still holds (same routes plus `/live`, same replay and event names, same login code), with these
> deltas: nav is Home / Find / Spending / Robot / Live / Ask, with Permissions ("Rules and privacy") in the account
> menu and no Jordan switcher; the UI is shadcn/ui (radix) + Lucide + motion; the planning cards are "Picky's advice"
> (**Skip it** / **Worth waiting**). A new **Live** page (`/live`, `src/lib/feed.ts`) replaces the live-feed ladder
> with three sources: Simulated, Robot gateway, Direct stream (an MJPEG / MP4 URL per camera). The gateway source
> takes an optional trip id (`/ws/frames?trip_id=`), reads legacy `frame` events with `frame.camera` =
> `head|wrist|basket|overview` plus `robot.state`, and polls `GET /health`. Finding 3 still applies: v0.2 needs
> `trip_id` or `robot_id`, sends `camera.frame` (`camera_id` = `<robot>.front|wrist|basket`; `head` = `front`, no
> `overview`), and sends the legacy `frame` (front only) only with `&legacy=1`.

Status legend: **NOW** = served by v0.1 on :8000 (verified) · **v0.2** = in the v0.2 spec (being built) ·
**MISSING** = nothing in either; a proposal is given (ids `B1`–`B13`, defined in §5.2). Money is integer cents
everywhere. No frontend file was changed; the teammate owns all of `arms/frontend/**`.

## Key findings

1. **The frontend's trip store is not an event reducer yet.** The replay engine (`src/lib/trip.ts`) mutates the
   zustand store with `set()`; `emit(type, summary)` only appends `{seq, t, type, summary}` to the event log. Event
   *names* match `contracts.events` for 17 of the 19 types it emits, but no payload matches (they are summary
   strings). A `GatewaySource` therefore needs an `applyEvent(ev)` reducer plus the id/vocabulary maps in §2.2.
   It is still mostly mapping work: the store's fields (stops, route, stop status, robot pose, cart, approval, receipt)
   all have contract equivalents.
2. **Live trip, robot order, approvals, checkout, mandate, profile and trust ledger map to endpoints that exist NOW.**
   Find & compare, purchase history (Overview, Spending), Ask Picky's data, SKIP/WAIT advice and everything
   T-Mobile have **no backend** yet.
3. **v0.2 breaks the frontend's one live hookup.** `LiveFeed` opens `WS /ws/frames` with no query and draws only
   `type === "frame"`. In v0.2, `/ws/frames` requires `?trip_id=` or `?robot_id=` (otherwise it sends one `error`
   and stops) and sends `camera.frame`; legacy `frame` only comes with `&legacy=1`. The trip page passes
   `tripId={null}`.
4. **The T-Mobile store fixture (`src/data/stores/tmobile.ts`) can't be ported as is.** All 16 slots break the reach
   contract (approach 0.75–1.45 m from the item, the hard limit is 0.47 m), 2 slots sit at z = 1.6 m (limit 1.5 m),
   fixture kinds `display_table` / `counter` and deal kind `clearance` aren't in contracts, slot `aisle_id`s
   reference aisles that don't exist, the wireless categories aren't top-level category ids, and `compatible_with`,
   devices and installments have no contract fields (§3).
5. **`scripts/gen-data.mjs` still works with the current `data/`**: exit 0, 125 products, 124 `store_ave` slots,
   107 purchases. Details and the small changes it sees: §4.

---

## 1. Screens → data today → backend feed

### 1.1 App shell (`src/components/Rail.tsx`, `src/app/layout.tsx`)

| Piece | Today | Backend feed | Status |
|---|---|---|---|
| Connection chip | hard-coded "DEMO DATA · NO BACKEND" | `GET /health` → `api.HealthStatus` (`real_arms`, `sim_connected`; v0.2 adds `robots`) | NOW |
| "live" badge on Live trip | `useTrip().phase` | `GET /api/trips?user_id=maya` + `/ws?user_id=maya` (v0.1: bare `/ws` replays everything) | NOW / v0.2 filter |
| Current user | `maya` from `src/data/generated/maya.json` | `GET /api/users/maya/profile` → `profile.UserProfile` (`users.yaml` also has `jordan`, `sam`) | NOW |

### 1.2 Overview `/` (`src/app/view.tsx`)

| Piece | Today (file → function) | Backend feed | Status |
|---|---|---|---|
| Month spend vs budget | `usePurchases()` (`lib/usePurchases.ts`: `maya.purchases` + session `extraPurchases`) → sum; `MONTHLY_BUDGET_CENTS = 32000` constant (`lib/data.ts`) | purchases: **B1**; monthly budget: no contract field (`UserProfile.default_budget_cents` is per trip, $40) → **B10** or add `UserProfile.monthly_budget_cents` | MISSING |
| Trips / average basket / saved on deals | `tripsFrom`, `savedCents` (vs `Product.price_cents`) | **B1** + list prices from `GET /api/products/search?limit=` | MISSING (B1) |
| Probably running low | `runningLow(purchases)` (purchase cadence) | derive client-side from **B1**, or **B11** (= Cart Whisperer "replenish") | MISSING |
| Live-order strip | `useTrip()` | `GET /api/trips?user_id=` + `/ws?trip_id=` | NOW |
| Recent trips | `tripsFrom(purchases)` | **B1** (`GET /api/trips` returns only trips created in this gateway session, not the fixture history) | MISSING |
| Ask bar | `useAsk().ask` | see 1.5 | – |

### 1.3 Spending `/purchases` (`src/app/purchases/view.tsx`)

| Piece | Today | Backend feed | Status |
|---|---|---|---|
| Weekly spend by category | `weeklyByCategory` + `catOf` (5 fixed buckets) | **B1**; names/colors per top-level id from `GET /api/categories` (bucketing stays a display choice) | MISSING (B1) / v0.2 |
| Trips ledger + line items, "DEMO HISTORY" tag | `tripsFrom`, `Purchase.synthetic` | **B1** (needs `synthetic` on `PurchaseRecord`, see 3.4) | MISSING |
| Rate an item | local `useState` | `POST /api/users/{id}/feedback` `profile.Feedback{feedback_id, user_id, sku, trip_id, rating, channel:"web"}` → `profile.ProfileUpdate` | NOW |
| What you buy most | `mostBought` | **B1** | MISSING |

### 1.4 Find & compare `/find` (`src/app/find/view.tsx`, `src/lib/prices.ts`, `src/components/PriceBits.tsx`)

| Piece | Today | Backend feed | Status |
|---|---|---|---|
| Search | `searchProducts` over `allProducts` (grocery + 16 wireless SKUs) | `GET /api/products/search?q=` → `list[catalog.SearchHit]` (grocery only) | NOW (no wireless) |
| Offers table, 7 retailers | `offersFor`: `offers.json` from gen-data (store_ave, store_hill, online, synthetic `cascade` warehouse 2-pack, `errand` delivery) + `wirelessOffers` (store_tmobile, carrier_online, volt, openbox; FNV-hash prices at runtime) | **B3**. `GET /api/products/{sku}` NOW returns only the Picky offers (`StoreOffer` per store_ave / store_hill / online) | MISSING |
| 13-week sparkline | `historyFor` (gen-data seeded, wireless hashed) | **B4** | MISSING |
| Verdict badge (Good price / Wait / Typical / High / Skip it) | `verdictFor` (owned → skip, incompatible → skip, upcoming sale → wait, vs 13-week low/median) | **B5** | MISSING |
| Worth waiting for | `upcomingFor` (gen-data random 14% + `COFFEE` hook + `tmobileUpcoming`) | **B4** `.upcoming`; `GET /api/stores/{id}/deals` NOW has current deals only (`catalog.Deal` has `valid_until`, no start date) | MISSING |
| Cheaper swaps | `alternativesFor` (same subcategory, cheaper effective price) | **B9** | MISSING |
| "Add to a robot order" | `robotStoreFor` (a slot exists) → `/order?store=&add=` | `GET /api/products/{sku}`: `StoreOffer.location != null` = robot store | NOW |

### 1.5 Ask Picky `/ask` + drawer (`src/lib/assistant/store.ts` → `POST /api/chat` → `src/app/api/chat/route.ts`; tools in `src/lib/assistant/tools.ts`)

| Piece | Today | Backend feed | Status |
|---|---|---|---|
| Chat turn | Next.js server route runs Claude (`claude-opus-5-5`, low effort, 6 tools); offline responder on no key / error | This route is the interim (CLAUDE.md §7 exception; its README calls it a stand-in until the agent is reachable through the gateway). Backend has `POST /api/requests` (v0.2: shopper `status`/`chat` intents → `RequestPlan.reply`, text only, no cards, no money tools) and trip-scoped `POST /api/trips/{id}/messages` → `tools.AgentReply` (NOW) | partial |
| `spending_summary` / `budget_status` | local purchases + constant budget | **B1**, **B10**, `GET /api/users/{id}/mandate` (NOW) | MISSING |
| `price_check` / `find_savings` | `offersFor`, `historyFor`, `upcomingFor`, `verdictFor` | **B3**, **B4**, **B5** (+ **B1** for savings) | MISSING |
| `search_products` | `searchProducts` | `GET /api/products/search` | NOW |
| `can_i_afford` | purchases, budget, `mayaDevices` | **B1**, **B10**, **B8** | MISSING |

Lowest-effort path: keep the teammate's route (it works and has the offline fallback) and point its tool data reads
at gateway REST once B1/B3–B5 exist.

### 1.6 Robot order `/order` (`src/app/order/view.tsx`, `src/lib/order.ts`)

| Piece | Today | Backend feed | Status |
|---|---|---|---|
| Store switch | `STORES = {store_ave, store_tmobile}` | `GET /api/stores` → `store_ave`, `store_hill`, `online` (no `store_tmobile`, §3.1) | NOW (no T-Mobile) |
| "Plan it" (intent → list) | `expandIntent` (4 deterministic presets: tacos, basics, Pixel 9, earbuds) | `POST /api/trips {user_id, text, store_id}` → `trip.Trip`; then `list.ready`, `sourcing.split`, `recs.ready`, `route.planned` on `/ws?trip_id=`; a manual trip waits in `awaiting_login` = the preview. (`POST /api/requests`, v0.2, creates `auto_execute` trips: the robot leaves right away, no preview) | NOW |
| List lines | `ListLine{line_id, sku, qty, unit_cents, sourcing, note, vegetarian}` | `ShoppingList.items: ListItem{item_id, sku, qty, sourcing, store_id, est_line_cents (LINE total), notes, dietary}` | NOW |
| "Picky's advice" SKIP / WAIT cards | `Draft.advice: {kind: "skip"\|"wait", sku, text, save_cents}` | **B2** (Cart Whisperer suggestions, v0.3; `skip` / `wait` kinds are in the `docs/cart-whisperer.md` §7 proposal) | MISSING |
| "Worth a detour?" rec cards | `Rec{rec_id, sku, unit_cents, original_cents, interest, reason, signals, sponsored, detour_m, detour_s}` | `recs.ready` → `Recommendation{price_cents, deal.original_cents, interest, reason, signals, sponsored, extra_distance_m, extra_time_s}` | NOW |
| Keep / skip a rec before departure | local `accepted` set | **B6** | MISSING |
| Detour metres per rec, "on your route" | client `planRoute` delta | `Route.stops[].detour_m` / `Recommendation.extra_distance_m` (v0.1 fake routing returns 0 / null, so no detours show) | NOW (fake = 0) |
| Totals + delivery | `draftTotal` (waives delivery for online add-ons on a robot order) | `ShoppingList.est_total_cents`, `Cart.shipping_cents` (v0.1 charges $2.99 on a $1.29 online add-on). Rule mismatch, §6 | NOW (rule differs) |
| Mandate preview ("checked in code") | `mandateCheck` + `WIRELESS_MANDATE` (frontend-only 2nd mandate) | `mandate.decision` events during planning (scope `line`/`cart`); an explicit pre-order check is **B7**; a 2nd mandate per user has no contract support | NOW / MISSING |
| Fulfillment: shop with me / robot shops for pickup | `Mode = "with_me" \| "pickup"` | `Trip.auto_execute` false / true. v0.1 `CreateTripRequest` has no `auto_execute`, so pickup = v0.2 | NOW / v0.2 |
| Login code "482 913" | fixed string, "Simulate robot login" button | No contract field, and there is no robot screen to type it on (lead decision: one web app, no kiosk). Use a web "I'm at the robot" / "Start" button → `POST /api/trips/{id}/login {user_id, robot_id?}` (`api.RobotLoginRequest`) | NOW |
| "Send to robot" | `useTrip().placeOrder` | `POST /api/trips/{id}/login` (manual) | NOW |
| `?add=SKU` from Find | prepended `ListLine` | v0.2 `CreateTripRequest.shopping_list`, or put the item in `text` | v0.2 / NOW |

### 1.7 Live trip `/trip` (`src/app/trip/view.tsx`, `src/lib/trip.ts`, `src/components/StoreMap.tsx`, `src/components/LiveFeed.tsx`)

| Piece | Today | Backend feed | Status |
|---|---|---|---|
| Cold load / resync | – | `GET /api/trips/{id}` + `/ws?trip_id=` (first message `trip.snapshot`) | NOW |
| Header stats (stops, route m, +detour m, basket) | `route.total_m`, `baseline_m`, `stopStatus`, `cartTotal` | `Route.total_distance_m`, `baseline_distance_m`, `extra_distance_m`; `Trip.stop_status`; `Cart.total_cents` | NOW |
| Playback (1×/2×/4×, pause, auto-answer, restart) | replay only | none; hide in gateway mode (`POST /api/fleet/robots/{id}/stop` halts and cancels, it doesn't pause) | n/a |
| Live feed, gateway rung | `WS /ws/frames` (no query), draws `frame` with `frame.blurred === true` | v0.1: legacy `frame` (NOW). v0.2: `/ws/frames?trip_id=<id>` → `camera.frame{camera_id, frame}` for front/wrist/basket, or `&legacy=1`. Simplest: the existing URL rung with `<img src="{GW}/api/cameras/{robot_id}.front/stream.mjpg">` | NOW / v0.2 |
| Basket cam overlay, arm-pick view | drawn on the canvas | `{robot_id}.basket`, `{robot_id}.wrist` cameras | v0.2 |
| Approval cards (detour, top-3 pick, checkout) | `Approval` union, `answer(value)` | `approval.requested` (+ `pick.choices`) → `POST /api/approvals/{id}` (`api.ApprovalDecisionRequest`); mapping in §2.3 | NOW |
| Receipt | `Receipt{receipt_id, total_cents, in_store_cents, online_cents, shipping_cents, card, provider, ts}` | `checkout.completed.receipt: trust.Receipt{amount_cents, in_store_cents, online_cents, provider: mock\|stripe_test\|visa_sandbox, status, created_at}`; shipping = amount − in_store − online; no card label field | NOW |
| Stop timeline | `route.order` + `stopStatus` | `Route.order` + `stop.updated` / `Trip.stop_status` | NOW |
| Basket panel (✓ cam) | `CartLine{verified}` | `cart.updated` (`CartLine.status == "verified"`) + `basket.verified` | NOW |
| Event log | `log` | every `/ws` event | NOW |
| Store map | `STORES[storeId]` (generated planogram), `route.legs[].path`, `baselinePath`, `offRoute`, `robot` | `GET /api/stores/{id}/planogram`; `route.planned` `legs[].waypoints`, `baseline_waypoints`; dashed legs = legs touching `visited_optional` with `detour_m > 0.5`; pose from `robot.state` | NOW |
| Robot "thinking" | none | `brain.step` + `GET /api/fleet/robots/{id}/steps` | v0.2 |
| Follow-up, "trip 2 is smarter" | receipt sentence only | `POST /api/trips/{id}/followup`, `followup.sent`, `feedback.received`, `profile.updated` | NOW (no screen) |

### 1.8 Permissions `/permissions` (`src/app/permissions/view.tsx`)

| Piece | Today | Backend feed | Status |
|---|---|---|---|
| Spending rules (cap, ask above, per-item, online, blocked) | local state from `maya.mandate` | `GET` / `PUT /api/users/{id}/mandate` → `trust.IntentMandate` | NOW |
| Learned taste chips + delete | local state from `maya.profile.prefs` | `GET` / `PUT /api/users/{id}/profile` (`prefs`) | NOW |
| Approval history | session `approvalsLog` | `GET /api/trust/ledger?user_id=` (`audit` rows with `action="approval.resolved"`, `transparency`, `receipts`) or `Trip.approval_responses` | NOW |

### 1.9 Backend features with no screen yet (the lead's one web app also has staff + fleet views)

| View | REST | Live | Status |
|---|---|---|---|
| NL chat box → plan, store choice, split | `POST /api/requests` (+ `in_reply_to`), `GET /api/requests[/{id}]` | `/ws?request_id=` | v0.2 |
| Fleet overview, robot panel, goals | `/api/fleet/robots[/{id}]`, `/brain`, `/steps`, `POST .../messages`, `/goals`, `/stop`; `/api/goals*`; `/api/sim/status` | `fleet.robot_updated`, `goal.*`, `brain.step` | v0.2 |
| Camera grid | `/api/cameras`, `snapshot.jpg?w=&t=`, `stream.mjpg` | `/ws/cameras?store_id=&w=` | v0.2 |
| Stock, alerts, restock, history, categories | `/api/stores/{id}/stock*`, `/api/categories*` | `stock.updated`, `stock.alert` | v0.2 |
| Merchant dashboard | `/api/merchant/{store_id}/dashboard` | `scan.result`, `stock.*` | NOW |

---

## 2. Event compatibility (frontend replay vs `contracts/events.py` v0.2)

### 2.1 Event types

Every replay event is `{seq, t, type, summary: string}` (`trip.ts` `emit`), so **every payload mismatches**; the
table lists what a gateway reducer has to read instead.

| Type the replay emits | In contracts? | Contract payload to read | Notes |
|---|---|---|---|
| `trip.created` | yes | `trip: Trip` | |
| `list.ready` | yes | `shopping_list: ShoppingList` | |
| `sourcing.split` | yes | `store_id, in_store[], online[], unresolved[]` (`ListItem`) | frontend has no `unresolved` |
| `recs.ready` | yes | `recs[], checkout_recs[]` (`Recommendation`) | frontend has no `checkout_recs` |
| `route.planned` | yes | `route: Route, replan` | apply only if `seq` > last applied |
| `trip.state_changed` | yes | `from_state, to_state: TripState, reason` | phase map in 2.2 |
| **`robot.command`** | **NO** | – | nearest: `stop.updated status="navigating"` or `brain.step` (tool call `navigate`, v0.2) |
| `robot.arrived` | yes | `stop_id, slot_id, goal_id, pose` | |
| `approval.requested` | yes | `request: ApprovalRequest` | detour = kind `recommendation`, checkout = `checkout` (carries `cart_hash`) |
| `pick.choices` | yes | `choice: PickChoice, approval_id` | replay emits it **instead of** `approval.requested`; the gateway sends **both** (kind `pick_choice`), so dedupe by `approval_id` |
| `stop.updated` | yes | `stop_id, status, reason, rec_id, slot_id` | replay emits it only for declines; gateway on every transition |
| `scan.result` | yes | `report: ScanReport` | |
| `item.picked` | yes | `sku, slot_id, instance_id, line_id, stop_id, goal_id` | frontend tracks `lot`; contract has `instance_id` (lot via `ScanReport` detections) |
| `basket.verified` | yes | `check: BasketCheck{expected, observed, ok, missing, unexpected}` | per SKU, not per line |
| `cart.updated` | yes | `cart: Cart, total_cents, budget_cents, remaining_cents` | |
| `mandate.decision` | yes | `decision: MandateDecision` | codes map in 2.2 |
| `approval.resolved` | yes | `kind, response: ApprovalResponse` | |
| `checkout.completed` | yes | `receipt: Receipt` | |
| **`robot.login`** | **NO** | – | nearest: `trip.state_changed awaiting_login → executing` after `POST /login` |

Frontend consumes from the gateway today: only `frame` on `/ws/frames` (`ev.frame.blurred`, `ev.frame.jpeg_b64`).
v0.2 sends `camera.frame` (`camera_id`, `frame: CameraFrame` with the same `jpeg_b64` / `blurred`) unless
`&legacy=1`, and needs `?trip_id=` / `?robot_id=`.

In contracts but not handled by the frontend (the reducer must at least ignore them): `trip.snapshot`,
`intent.received`, `robot.state` (the replay moves the robot without an event), `followup.sent`,
`feedback.received`, `profile.updated`, `agent.message`, `error`, `camera.frame`, and v0.2 `request.*`, `goal.*`,
`fleet.robot_updated`, `brain.step`, `stock.updated`, `stock.alert`.

### 2.2 Ids and vocabularies (verified against a live v0.1 event stream)

| Thing | Frontend | Contract / v0.1 |
|---|---|---|
| Stop ids | `start`, `s_<SKU>`, `o_<SKU>`, `end` | `start`, `item:<SKU>`, `rec:<rec_id>`, `end` |
| Stop kinds | `start`, `list`, `optional`, `end` | `start`, `mandatory`, `optional`, `end` |
| Stop status | `pending`, `active`, `done`, `declined`, `skipped` | `pending`, `navigating`, `arrived`, `done`, `skipped`, `declined`, `failed` (`navigating`/`arrived` → `active`) |
| Trip phase | `idle`, `waiting_login`, `shopping`, `checkout`, `done` | `TripState`: `intent … route_planned` → idle/plan, `awaiting_login` → waiting_login, `executing`/`awaiting_approval` → shopping, `checkout` → checkout, `paid`/`followup`/`done` → done, `failed` (no frontend state) |
| Robot status | `idle`, `driving`, `scanning`, `picking`, `waiting`, `done` | `RobotStatus`: `idle`, `navigating`, `scanning`, `picking`, `placing`, `charging`, `error`, `disconnected`; `waiting` = trip `awaiting_approval`, `done` = trip `paid`/`done` |
| Route | `{order, legs[{from, to, path, length}], total_m}` + `baseline_m`, `baselinePath`, `offRoute` | `Route{stops, order, legs[{from_stop_id, to_stop_id, waypoints, distance_m, eta_s}], total_distance_m, baseline_distance_m, baseline_waypoints, visited_optional, skipped_optional}` |
| Cart line | `{sku, qty, unit_cents, source, verified, lot}` | `CartLine{line_id, sku, name, category, qty, unit_price_cents, source, store_id, slot_id, status, rec_id, instance_ids}` |
| Pick candidates | per **lot**: `{instance_id, lot_id, freshness, expiry, note}` | per **unit**: `FreshnessCandidate{instance_id, sku, score, rank, reason, bbox, thumbnail_b64}`; options `option_id = instance_id` |
| Mandate reasons | `{code, text, level: ok\|ask\|block}`; codes `WITHIN_CAP`, `OVER_CAP`, `BLOCKED_CATEGORY`, `ASK_ABOVE`, `CHECKOUT_APPROVAL` | `MandateDecision.reasons[]` (lowercase enum) + parallel `messages[]`, `verdict` `approve\|needs_approval\|deny`: `ok`, `over_cap`, `category_blocked`, `above_ask_threshold`, `checkout_requires_approval`; the frontend has no `over_item_cap` / `over_budget`. Semantics differ: the frontend asks when the **unit** price > ask-above, the contract when the **line total** does |

### 2.3 Approval answers → `POST /api/approvals/{approval_id}`

| Frontend card / answer | `ApprovalRequest.kind` | Body (`api.ApprovalDecisionRequest`) |
|---|---|---|
| detour "yes" / "no" | `recommendation` | `{"decision":"approve"}` / `{"decision":"reject"}` |
| pick `inst_<lot>` | `pick_choice` | `{"decision":"choice","choice":"<option_id>"}` |
| checkout "approve" / "decline" | `checkout` | `{"decision":"approve","cart_hash":"<request.cart_hash>"}` (409 = cart changed → refetch, re-ask) / `{"decision":"reject"}` |

---

## 3. Data parity: one source of truth (CLAUDE.md rule 5)

### 3.1 T-Mobile store (`src/data/stores/tmobile.ts`, frontend-only today)

| Frontend has | Backend / `data/` has | Backend needs |
|---|---|---|
| `tmobileStore`: 10 × 8 m, 6 fixtures, 1 aisle, 16 slots, 2 deals | nothing (planograms: `store_ave`, `store_hill`) | `data/planograms/store_tmobile.yaml` (`contracts.planogram.Planogram`), slot ids prefixed `TMO-` (v0.2 convention: `AVE-`, `HILL-`), `capacity`, `price_cents`, `deal` |
| `tmobileProducts`: 16 SKUs (phones, cases, glass, chargers, cable, earbuds, watch, 5G gateway, power bank) | none | rows in `data/products.yaml` with EAN-13 `barcode`, `unit`, `size`, `asset` |
| top-level categories `phone`, `case`, `screen`, `charger`, `audio`, `wearable`, `home_internet` (used in `prices.ts`, `tools.ts`, `data.ts catOf`) | `STANDARD_CATEGORY_IDS` (13 grocery ids) + `data/categories.yaml`; `utils.data` rejects a `category` that isn't top-level there | a contracts PR adding a top-level id (e.g. `electronics`) + subcategories, then the frontend switches to `subcategory`; or the same 7 ids as top-level. Open (§6) |
| `compatible_with: Sku[]` on wireless products (drives "doesn't fit your phone") | not on `catalog.Product` | `Product.compatible_with` (contracts), or tags such as `fits-px9-128` as a no-contract stopgap |
| no robot for the store | `data/fixtures/robots.yaml`: robots only in `store_ave` / `store_hill` | one rover + one ceiling camera in `store_tmobile` (fleet assigns robots within a store only) |
| `mayaDevices` (Pixel 7, 34 months, 4 installments × $16.50, 2 lines; owns `CHG-20W` 2026-03-14, `CASE-PX7-BLK` 2023-12-02) | nothing (`UserProfile` has no devices / lines) | owned accessories as `PurchaseRecord`s with `store_id: store_tmobile` in `users.yaml`; devices + lines via **B8** (contracts) |
| `WIRELESS_MANDATE` (cap $150, per item $100, ask above $50) | one `IntentMandate` per user (cap $60) | either a 2nd mandate scoped by store/category (contracts) or one mandate with per-category caps. Open (§6) |
| `tmobileUpcoming` (`BUDS-RUN` $59.99 from 10-23, `PX9-128` $599.99 from 11-27) | none | upcoming-price fixture (3.3) |

`tmobile.ts` slots checked against `contracts.common` (`APPROACH_DIST` 0.35–0.40 m, reach ≤ 0.47 m, item z 0.1–1.5 m)
and `contracts.planogram`:

| Problem | Slots |
|---|---|
| approach → item distance 0.75 m (> 0.47 reach) | all 13 wall / endcap slots (`ACC-WALL-*`, `AUDIO-WALL-*`, `WEAR-CASE-*`) |
| approach → item distance 1.45 m (item at table centre) | `TABLE-A-N0-00` `PX9-128`, `TABLE-A-N0-01` `IP16-128`, `TABLE-B-N0-00` `GS25-128` |
| item z 1.60 m (> 1.5 m), from `z = 0.4 + 0.4 × level` | `ACC-WALL-E3-00` `CASE-PX9-CLR` (the demo case), `ACC-WALL-E3-01` `CASE-PX9-RUG` |
| `FixtureKind` not in `shelf\|fridge\|freezer\|produce_bin\|endcap\|checkout` | `display_table` (TABLE-A/B), `counter` (COUNTER → `checkout`) |
| `DealKind` not in `markdown\|percent_off\|bogo\|near_expiry` | `clearance` on `CASE-PX7-BLK` (→ `markdown`); "20% off glass with any Pixel 9 case" is a bundle condition contracts can't express |
| slot `aisle_id` = `fixture_id.toLowerCase()` (`acc-wall`, ...) but only aisle `floor` exists → `Planogram` validator error | all 16 |

### 3.2 Grocery stores

`store_ave` / `store_hill` / `online` come from `data/` through gen-data, so they're already one source. The
frontend reads `slot.price_cents` and `slot.deal` the same way the catalog arm does.

### 3.3 Prices, retailers, price history

| Frontend has | Where | Backend has | Backend needs |
|---|---|---|---|
| 2 synthetic retailers (`cascade` warehouse: 0.78–0.86 × price, 2-pack, $60/yr; `errand` delivery: ×1.14 + $3.99) | `gen-data.mjs`, `mulberry32(4242)` consumed in `products.yaml` order | none | `data/retailers.yaml` (id, name, kind, fee, pack, note, pricing rule) |
| 4 wireless retailers (`store_tmobile`, `carrier_online`, `volt`, `openbox` "Second Shelf", condition "Like new") | `prices.ts wirelessOffers`, FNV hash at runtime | none | same file |
| 13 weekly prices per SKU at The Ave | gen-data (grocery), `historyFor` hash (wireless) | none | price-history fixture |
| upcoming sales (random 14% + `COFFEE` $7.99 from 10-23 + `tmobileUpcoming`) | gen-data + `tmobile.ts` | `catalog.Deal` has no start date | upcoming-price fixture (or `Deal.starts_at`) |

Recommendation: put offers, history and upcoming prices in data YAML (e.g. `data/retailers.yaml` +
`data/prices.yaml`, written once by a seeded generator in `scripts/` and committed as data) that both the catalog
arm (serves B3–B5) and gen-data read. Today the numbers come from two RNGs keyed on file order and on SKU hashes, so
they drift as soon as `products.yaml` changes (§4).

### 3.4 Purchase history

The frontend has 107 lines: 9 fixture purchases from `users.yaml` and 98 synthesized by gen-data (`mulberry32(20261017)`,
20 staples with cadences, `synthetic: true`). The backend has `PurchaseRecord`, `DemoUser.purchases` (the 9 fixture
lines) and `RecommenderService.record_purchase`, but **no read endpoint**. Needed: **B1** serving fixture + seeded
history + purchases recorded at checkout. Make one generator own the seeded history (e.g. `data/fixtures/purchase_history.yaml`
from a seeded script; gen-data reads it instead of synthesizing). Add `PurchaseRecord.synthetic: bool = False` so
the "DEMO HISTORY" label survives.

---

## 4. gen-data.mjs against the current `data/` (snapshot 17:44 PT)

Method: copied `arms/frontend` (without `node_modules`/`.next`, plus the `yaml` package) twice under the scratchpad,
once next to the frontend branch's `data/` (baseline) and once next to a copy of the current repo `data/`
(gen-data resolves `data/` at `../../../` from `scripts/`), then ran `node scripts/gen-data.mjs` in each.

| | Baseline (frontend branch data) | Current repo data |
|---|---|---|
| Exit | 0 | **0** |
| Products / `store_ave` slots | 73 / 72 | 125 / 124 |
| Purchases (synthetic) | 107 (98) | 107 (98); `maya.json` byte-identical |
| Offers / upcoming sales | 73 / 9 | 125 / 16 |

- **Every field gen-data reads is unchanged:** products (`sku, name, brand, category, subcategory, tags, dietary,
  price_cents, unit, size, perishable, asset.label_color/shape/dims_m`), planogram (`store_id, name, address, bounds,
  entrance, checkout, robot_home, obstacles, fixtures, aisles, slots[].lots{lot_id, qty, expiry, freshness},
  slots[].deal{kind, price_cents, original_cents, percent_off, reason}`), `store_hill` slots, `online_store.yaml`,
  `users.yaml` maya.
- **Every SKU the frontend hard-codes still exists** with an in-stock `store_ave` slot: the 20 gen-data staples, the
  taco and basics presets and their recs (`CILANTRO` has qty 0 in both, so it routes online, as before). `AVOCADO`
  still has 4 lots (freshness 0.95 / 0.82 / 0.58 / 0.31), so the top-3 pick card works. Bounds, checkout,
  `robot_home` (= Pico's dock) and the aisle ids (`aisle_1..5`, used for map labels) are unchanged.
- **Changes it sees:**
  1. Slot ids gained the `AVE-` prefix (`PRODUCE-A-S0-00` → `AVE-PRODUCE-A-S0-00`); fixture ids are unchanged.
     `LiveFeed`'s face parser (second-to-last `-` part) still works. No frontend code hard-codes slot ids.
  2. New top-level categories `international` (MASA-HARINA, SOY-SAUCE, RAMEN, COCONUT-MILK) and `personal_care`
     (TOOTHPASTE, SHAMPOO, BODY-WASH, DEODORANT) fall into the "Home & tech" chart bucket (`catOf` default). None
     are in Maya's purchases, so charts don't change.
  3. 4 subcategory renames change `alternativesFor` for those SKUs: TACO-SHELLS `tortillas`→`taco_shells`, PICO-FRESH
     `salsa`→`fresh_salsa`, QUESO-DIP `dips`→`cheese_dip`, CORN-FROZEN `vegetables`→`frozen_vegetables`.
  4. The offer RNG runs in file order, so 3 online-only SKUs that moved to the end of `products.yaml` (MOLCAJETE,
     OREGANO-MEX, CHILE-LIME-SEASONING) got new synthetic offers and history; the other 70 shared SKUs are identical,
     and the 9 baseline upcoming sales (incl. the `COFFEE` hook) are all still there.
- Ignored by gen-data: `data/categories.yaml`, `data/fixtures/robots.yaml`, new product fields (`barcode`,
  `description`, `allergens`, `spice_level`, `shelf_life_days`, `image_url`), new slot fields (`capacity`,
  `low_threshold`, `shelf_tag_cents`), and users `jordan` / `sam`.
- Frontend-side nit (for the teammate): `purchases/view.tsx` badges `trip_id === "trip_maya_1017"`, but
  `trip.ts` writes `trip_maya_1017_<storeId>`, so the "today · robot" badge never shows.

---

## 5. Wiring plan

### 5.1 Minimal `GatewaySource` (teammate's code; listed by demo value)

Env: `NEXT_PUBLIC_GATEWAY_URL=http://localhost:8000` (the name the frontend already uses; `contracts/api.md`
says `NEXT_PUBLIC_API_BASE`, so one of the two should be renamed). CORS: verified on v0.1 (`access-control-allow-origin: *`,
POST preflight with `content-type` passes); v0.2 `app.py` keeps `allow_origins=["*"]` and exposes `X-Event-Seq`.
WebSockets and MJPEG `<img>` need no CORS. Connect the browser straight to the gateway, no Next proxy (the frontend
README says the same).

1. **`lib/gateway.ts`**: `api<T>(path, init)` fetch wrapper; `GET /health` drives the Rail chip ("Gateway
   connected" vs "Demo data"); when there's no URL or health fails, everything stays on the replay (today's behaviour).
2. **Live trip over `/ws?trip_id=`** (`lib/trip.ts`): add `source: "replay" | "gateway"` and
   `applyEvent(ev)` that maps the payloads in §2.1 with the id maps in §2.2 onto the existing store fields (stops
   from `route.stops`, `stopStatus` from `stop.updated`, `robot` from `robot.state`, `cart` from `cart.updated`,
   `approval` from `approval.requested`, `receipt` from `checkout.completed`, `log` from everything). Dedupe by `seq`;
   reconnect with `after_seq`; `trip.snapshot` replaces state. `answer()` → `POST /api/approvals/{id}` (§2.3).
   Hide playback controls in gateway mode.
3. **Order** (`order/view.tsx`): "Plan it" → `POST /api/trips {user_id:"maya", text, store_id}` and render the list
   and recs from `list.ready` / `sourcing.split` / `recs.ready` (keep `expandIntent` for offline and for
   `store_tmobile` until it exists). "Send to robot" / LoginPanel "Start" → `POST /api/trips/{id}/login`.
   Pickup mode → v0.2 `auto_execute` (or `POST /api/requests`).
4. **Live feed** (`LiveFeed.tsx`): pass the trip id; on v0.2 open `/ws/frames?trip_id=<id>&legacy=1` (or accept
   `camera.frame` with `camera_id` ending `.front`), or feed the URL rung `{GW}/api/cameras/{robot_id}.front/stream.mjpg`.
5. **Permissions + trip 2**: mandate and profile via `GET/PUT`, history via `/api/trust/ledger`; purchases ratings
   → `POST /api/users/{id}/feedback`; a small follow-up / `profile.updated` panel after the receipt.
6. **Find, Spending, Overview, Ask Picky data**: switch to B1/B3–B5/B9/B10 as they land, falling back to the
   generated JSON when the gateway is down.

### 5.2 Backend endpoints to add (by demo value)

New models need a `contracts/<slug>` PR (v0.3; the v0.2 build owns `contracts/` now). Shapes in contracts terms:

| # | Endpoint | Response (cents ints) | Feeds | Notes |
|---|---|---|---|---|
| **B1** | `GET /api/users/{user_id}/purchases?since=&until=&store_id=&limit=` | `list[profile.PurchaseRecord]` newest first (+ `synthetic: bool`) | Overview, Spending, running low, Ask Picky, Cart Whisperer | data exists (`users.yaml`); add seeded history (3.4) + records from checkout |
| **B2** | `GET /api/trips/{trip_id}/suggestions`, `POST .../suggestions/{suggestion_id}` `{decision: accept\|decline}`; event `suggestions.updated` | `list[Suggestion]` / `SuggestionResult` as in `docs/cart-whisperer.md` §7 (kinds incl. `skip` (action `none`, or `remove` if in the cart) and `wait` (action `remind`), `reason_codes`, `avoided_cents` = the web app's `save_cents`, `remind_on`) | "Picky's advice" SKIP/WAIT, T-Mobile track | zero suggestions is a valid answer ("nothing worth adding today") |
| **B3** | `GET /api/products/{sku}/offers?user_id=` | `list[RetailerOffer{retailer_id, retailer_name, kind: store\|online\|warehouse\|delivery\|resale\|carrier, price_cents, regular_cents, pack, fee_cents, fee_note, eta_text, in_stock, note, condition, effective_unit_cents, robot_store: bool}]` sorted by `effective_unit_cents` (= ⌈(price + fee) / pack⌉) | Find offers table, `price_check` | from `data/retailers.yaml` (3.3); include the Picky `StoreOffer`s |
| **B4** | `GET /api/products/{sku}/price-history?store_id=&weeks=13` | `PriceHistory{sku, store_id, points: [{week_start: date, price_cents}], low_cents, median_cents, high_cents, now_cents, upcoming: {starts: date, price_cents, reason} \| null}` | sparkline, "worth waiting for", WAIT | |
| **B5** | `GET /api/products/{sku}/verdict?user_id=` | `PriceVerdict{sku, label: good_price\|wait\|typical\|high\|skip, reason_code, why, now_cents, median_cents, low_cents, upcoming?, owned_since?: date, incompatible_with?: Sku}` | verdict badge, Ask Picky | port `verdictFor`: deterministic; the LLM only phrases |
| **B6** | `POST /api/trips/{trip_id}/recs/{rec_id}` `{decision: "keep"\|"skip"}` | `trip.Trip` (re-route: `stop.updated declined` + `route.planned replan=true`) | keep/skip before departure | allowed `list_ready`…`awaiting_login` |
| **B7** | `POST /api/users/{user_id}/mandate/check` `{store_id, lines: list[trust.MandateLine], budget_cents?}` | `trust.MandateDecision` | order-page mandate preview | trust engine, same code as checkout |
| **B8** | `GET /api/users/{user_id}/devices` | `UserDevices{lines: int, devices: [OwnedDevice{model, name, age_months, installments_left, monthly_installment_cents}], owned: list[PurchaseRecord]}` | T-Mobile signals, `can_i_afford`, B5 skip | fixture in `users.yaml` (needs `DemoUser` field) |
| **B9** | `GET /api/products/{sku}/alternatives?user_id=&limit=3` | `list[{product: catalog.Product, best: RetailerOffer, save_cents}]` | cheaper swaps | same subcategory, compatible, not disliked |
| **B10** | `GET /api/users/{user_id}/spending?period=this_month\|last_month\|last_30_days\|last_12_weeks` | `SpendingSummary{period, start, end, total_cents, by_category: dict[CategoryId, int], trips, top: [{sku, qty, spend_cents}], budget_cents, pace_cents, projected_cents}` | Overview, Ask Picky, Photon "how am I doing" | needs `UserProfile.monthly_budget_cents` (frontend uses 32000) |
| B11 | `GET /api/users/{user_id}/replenish` | `list[{sku, every_days, since_days, due_in_days, last_qty, last_bought_at}]` | running low | P2, the client can derive it from B1 |
| B12 | `GET /api/products?store_id=` (no 100 cap) | `list[catalog.Product]` | product lookups for any SKU in events | P2: `SearchQuery.limit ≤ 100`, but there are 125 grocery + 16 wireless SKUs; for now, page by `/api/categories/{id}/products` |
| B13 | `POST /api/assistant/chat` `{user_id, messages}` | `{text, cards, source}` | Ask Picky | for when Ask Picky moves behind the gateway agent (CLAUDE.md §7) |

Data work that unlocks existing endpoints (no new endpoint): `store_tmobile` planogram + products + robot + camera
(3.1, needs the geometry fixes); `data/retailers.yaml` + `data/prices.yaml` (3.3); seeded purchase history (3.4).

---

## 6. Decisions

**Open team decisions (not decided here; the canonical list, mirrored in CLAUDE.md §1):**
- Which store is the live demo store: `store_ave` grocery (taco trip; CLAUDE.md demo) or `store_tmobile` wireless
  (team plan §3.2). The frontend ships both. T-Mobile judges see the wireless store either way.
- Name: Picky vs Aisle.
- How many sponsor tracks we may enter (Visa + T-Mobile + Photon + SideShift; last year's guide allowed two
  sponsored tracks plus the title track). If capped, the team plan's default cut order is Visa → T-Mobile → Photon →
  SideShift.
- Owner per arm (team plan A.4 maps 4 owners onto the arms; no draft PR has an assignee yet).
- Who builds the store-staff and fleet views (the web app has neither yet).
- Local-model fallback (team plan §5: LiteLLM + local models, `FORCE_LOCAL`) vs today's fallback (deterministic fakes,
  CLAUDE.md §7).

**Open technical questions (arm owners; most land in the v0.3 contracts PR):**
- Delivery fee on online add-ons in a robot order: the frontend waives it, the backend charges catalog shipping.
- Wireless taxonomy: a new top-level id (e.g. `electronics`) with subcategories vs the frontend's 7 top-level ids
  (needs a contracts change either way).
- Tech purchases: a separate mandate (frontend `WIRELESS_MANDATE`) vs one mandate per user.
- Which ask-above semantics win: unit price (frontend) or line total (contract).

**Already decided (applied above):** the lead's calls today: robots are rovers + lift + SO-101, simulated now
(Isaac on vhagar or the 2D fake), physical robot + VLA later (team plan D1's physical tape grid is out); one web app,
no iPad and no robot kiosk screen (so the login-code entry becomes a web "Start" button); NL request → fleet
manager across stores → per-robot managing LLM with camera input; T-Mobile track is entered. The teammate's
shipped choices also count: verdict labels "Good price / Wait / Typical price / High right now / Skip it" (not the
team plan's BUY NOW / WAIT / GOOD DEAL), env var `NEXT_PUBLIC_GATEWAY_URL`, the labeled live-feed sources (now the
Live page's Simulated / Robot gateway / Direct stream), detours in pickup mode become approvals (not "detours off" as
in team plan §6.1), and Ask Picky's Next.js route as the interim until the gateway exposes the agent (CLAUDE.md §7).
