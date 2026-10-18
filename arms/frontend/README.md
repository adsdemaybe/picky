# arms/frontend

**Purpose.** The shopper and merchant web UI: live sim view, store map with route + recommendation detours, cart,
budget, chat, approvals, trust ledger, and the merchant dashboard. It is the screen judges watch. Pipeline position:
`shopper ─ web ─▶ frontend ─▶ gateway` (PLAN) and `gateway events ─▶ /ws ─▶ frontend` (SHOP / PAY / AFTER / MERCHANT),
see CLAUDE.md §2. It talks **only** to the gateway (REST + WS), never to another arm.

Owner: TBD · Branch: `arm/frontend`

> Node app is **not scaffolded yet**. The owner runs the setup below on `arm/frontend`. No Python lives here, so this
> arm has no `__init__.py` / `fake.py` / `service.py` (the gateway's all-fake mode is the frontend's "fake backend").

## Scope (CLAUDE.md §3)
- [x] **P0** `/debug` fallback page: provided by the gateway (`arms/gateway/static/debug.html`, `GET /debug`); don't rebuild it
- [ ] **P0** `useEventSocket` hook + zustand store fed by `/ws?trip_id=` (snapshot, dedupe by `seq`, resume `after_seq`)
- [ ] **P0** Shopper: intent box → `POST /api/trips`, Cart, BudgetBar, ApprovalModal (incl. checkout with `cart_hash`)
- [ ] **P0** StoreMap SVG: fixtures from planogram, route polyline (solid mandatory, **dashed detours**), skipped recs
  as hollow markers, live robot pose, stop status colors
- [ ] **P0** Rec cards with reason + signals + "Sponsored" badge when `sponsored=true` (CLAUDE.md §8)
- [ ] **P0** LiveSimView: blurred JPEG frames from `/ws/frames` (canned JPEG in all-fake mode)
- [ ] **P0** PickChoice top-3 produce picker (freshness score + reason + thumbnail)
- [ ] **P0** ChatPanel with streaming `agent.message` deltas
- [ ] **P0** Receipt + follow-up/profile-update panel showing trip 2 is smarter
- [ ] **P1** TrustLedger: mandate, live APPROVE / NEEDS_APPROVAL / DENY decisions, transparency log, receipts, audit chain
- [ ] **P1** Merchant dashboard: OOS table, mispricing table, shelf heatmap (SVG over planogram), KPI tiles (Recharts)
- [ ] **P1** Push-to-talk voice (`POST /voice`; Web Speech + `WsChat channel="voice"` fallback on 503)
- [ ] **P1** "List-only route vs route with detours" comparison (`+12 m, 25 s` from `Stop.detour_m/detour_s`)
- [ ] **P2** Mandate editor (`PUT /mandate`), profile editor (`PUT /profile`), batched-shopper view, map pan/zoom

## Contract
- **Implements:** no Python Protocol (Node app). Consumes the gateway API in `contracts/api.md`.
- **In (server → client):** `contracts.events.Event` on `WS /ws?trip_id=<id>[&after_seq=<n>]` (first message is
  `trip.snapshot`; `seq` is bus-wide, gaps are normal, dedupe by `seq`; `robot.state` ≤ 2 Hz) and on
  `WS /ws/frames?trip_id=<id>` (`frame` events, always `blurred=true`, plus full-rate `robot.state`; live-only, `seq=0`).
  Merchant page uses `WS /ws?store_id=<id>`; `/debug` may use bare `WS /ws` (everything).
- **Out (client → server):** `api.ClientMessage` on `/ws?trip_id=`: `{"type":"chat","text","channel"}`,
  `{"type":"approval","response": ApprovalResponse}`, `{"type":"ping"}`; REST bodies `api.CreateTripRequest`,
  `api.RobotLoginRequest`, `api.PostMessageRequest`, `api.ApprovalDecisionRequest` (echo `cart_hash` for checkout
  approvals, `choices` for multi-select), `profile.Feedback`, `trust.IntentMandate`, `profile.UserProfile`.
- **Events emitted:** none (only the gateway/agent publish). **services.\*** called: none (gateway only).
- **Types:** generated, never hand-written (CLAUDE.md §7). See "Type generation".

### Component → endpoints + events

| Component | REST | WS events consumed (`/ws` unless noted) | Sends |
|---|---|---|---|
| `useEventSocket` + `tripStore` (zustand) | `GET /api/trips/{id}` (cold load) | **all**; reduce into `Trip`; `trip.snapshot` replaces state; `trip.state_changed`; `error` → toast | `ping` every 20 s |
| IntentBox (home) | `POST /api/trips` (`CreateTripRequest`, `user_id="maya"`), `GET /api/trips?user_id=` | `trip.created`, `intent.received`, `list.ready`, `sourcing.split` | – |
| ShoppingList / SourcingSplit | – | `list.ready`, `sourcing.split` (in-store / online / unresolved badges) | – |
| RecCards | – | `recs.ready` (`recs` + `checkout_recs`), `stop.updated` (declined/skipped) | – |
| **StoreMap** (SVG) | `GET /api/stores/{store_id}/planogram` (fixtures, aisles, slots, entrance/checkout), `GET /api/stores/{store_id}/grid` (optional overlay) | `route.planned` (legs → polylines; `kind="optional"` legs dashed amber; `skipped_optional` → hollow markers; `replan=true` replaces), `stop.updated`, `robot.state`, `robot.arrived`, `scan.result` (slot flash) | – |
| **LiveSimView** | – | `/ws/frames`: `frame` (`<img src="data:image/jpeg;base64,…">`), full-rate `robot.state` | – |
| RobotLogin | `POST /api/trips/{id}/login` (`RobotLoginRequest`) | `trip.state_changed` → `executing` | – |
| **Cart** | – | `cart.updated`, `item.picked`, `basket.verified` (✓/✗ per line) | – |
| **BudgetBar** | – | `cart.updated` (`total_cents`, `budget_cents`, `remaining_cents` < 0 = red), `mandate.decision` (`remaining_cents`) | – |
| **ChatPanel** | `POST /api/trips/{id}/messages` (fallback), `POST /api/trips/{id}/voice` (P1) | `agent.message` (append `delta` by `message_id`; `done` ends stream; `role="user"` echoes) | `chat` |
| **ApprovalModal** | `GET /api/approvals?trip_id=` (on reconnect), `POST /api/approvals/{id}` (`ApprovalDecisionRequest`; 409 = cart changed → refetch) | `approval.requested`, `approval.resolved`, `mandate.decision` (show verdict + reasons) | `approval` (WS path) |
| **PickChoice** (top-3) | `POST /api/approvals/{id}` with `decision="choice", choice=<instance_id>` | `pick.choices` (`choice.candidates` rank/score/reason/thumbnail; `approval_id`) | `approval` |
| CheckoutPanel / Receipt | `POST /api/trips/{id}/checkout` → `CheckoutStatus` (`cart_hash`, `decision`, `approval`) | `mandate.decision`, `approval.requested` (kind `checkout`), `checkout.completed` (`receipt.status`, `provider` → "Sandbox" label) | – |
| Follow-up / ProfileDiff | `POST /api/trips/{id}/followup` (demo cue), `POST /api/users/{id}/feedback`, `GET /api/users/{id}/profile` | `followup.sent`, `feedback.received`, `profile.updated` (show `ProfileChange`s) | – |
| **TrustLedger** | `GET /api/trust/ledger?user_id=&trip_id=`, `GET /api/users/{id}/mandate` | `mandate.decision`, `approval.resolved`, `checkout.completed`, `recs.ready` (refetch) | – |
| Merchant: **OOS table**, **Mispricing table**, **Shelf heatmap**, KPIs | `GET /api/merchant/{store_id}/dashboard` (`out_of_stock`, `mispricings[].delta_cents`, `heatmap[].rect/value`, `near_expiry`, `kpis`), `GET /api/stores/{store_id}/planogram` (heatmap base), `GET /api/stores/{store_id}/near-expiry` | `/ws?store_id=`: `scan.result` → refetch dashboard (debounced 1 s) | – |
| Header / StoreSelect | `GET /health` (`real_arms`, `sim_connected` badge), `GET /api/stores` | – | – |
| **/debug** | `GET /health`, `POST /api/trips`, `GET /api/trips/{id}`, `GET /api/approvals?trip_id=`, `POST /api/approvals/{id}`, `POST /api/trips/{id}/login`, `/checkout`, `/followup`, `POST /api/dev/inbound` | bare `/ws` or `/ws?trip_id=`: every event as raw JSON | `chat`, `approval` |

Map rules: SVG `viewBox` in store meters from `planogram` bounds, content inside `<g transform="scale(1,-1)">`
(world frame: x right, y up, origin bottom-left, theta CCW from +x); put `<text>` **outside** the flipped group.
Look up stops by `stop_id` in `route.stops`; progress lives in `Trip.stop_status` / `stop.updated`, not the route.
Money is integer cents; format only at render (`(c/100).toFixed(2)`).

## Stack (CLAUDE.md §9, docs/research.md §9.10)
- **Pick:** Next.js 16.4 (App Router, Turbopack) + React 19 + Tailwind 4.3 + shadcn/ui (CLI 4.22) + zustand +
  Recharts 3.10; hand-written SVG store map; live view = JPEG frames over WS into `<img>`. Node 22 LTS (`.nvmrc`), pnpm.
- **Types:** openapi-typescript 7.13 (REST) + json-schema-to-typescript (WS events).
- **Fallback:** Vite + React; recorded `<video autoplay loop>` as the live view; Leaflet `L.CRS.Simple` only if pan/zoom
  is needed. The `/debug` page is the always-working fallback UI. (Streamlit hurts the UX score.)
- **Gotchas:** connect the browser **directly** to FastAPI (`NEXT_PUBLIC_GATEWAY_URL`, gateway allows CORS for :3000);
  do **not** proxy websockets through Next and no Server Actions for robot traffic. All live components are
  `"use client"` (Recharts too). Discriminated unions come out as `oneOf` + discriminator; narrow on `event.type`.
  Stale routes: apply a `route.planned` only if its `seq` is higher than the last one applied.
  Never render an unblurred frame (the contract rejects them; don't add a bypass). **≤ 2 h on map polish before P0 works.**

## Type generation
```
contracts/*.py ──make gen-types──▶ uv run python -m arms.gateway.export_schemas
                                   ├─ arms/frontend/schemas/openapi.json        (FastAPI app.openapi())
                                   └─ arms/frontend/schemas/events.schema.json  (events.event_json_schema())
               ──pnpm gen-types──▶ src/lib/api-types.ts  (openapi-typescript)   → components["schemas"]["Trip"] ...
                                   src/lib/events.ts     (json-schema-to-typescript) → Event union on "type"
```
`package.json` script:
`"gen-types": "openapi-typescript schemas/openapi.json -o src/lib/api-types.ts && json2ts -i schemas/events.schema.json -o src/lib/events.ts"`.
Commit `schemas/` and `src/lib/*types*.ts`; on merge conflicts regenerate, never hand-merge. Re-run after every
`contracts/` change (`git fetch && git merge origin/main && make gen-types`).

## Files (planned)
| Path | What |
|---|---|
| `schemas/` | JSON schemas exported by the gateway (`make gen-types`) |
| `src/lib/api-types.ts`, `src/lib/events.ts` | generated TS types (do not edit) |
| `src/lib/api.ts` | typed `fetch` wrappers over `NEXT_PUBLIC_GATEWAY_URL` |
| `src/lib/useEventSocket.ts`, `src/lib/useFrames.ts` | `/ws` (reconnect with `after_seq`, seq dedupe) and `/ws/frames` hooks |
| `src/store/trip.ts` | zustand store: event reducer → `Trip`, chat buffers, pending approvals, robot pose |
| `src/app/page.tsx` | shopper view (IntentBox → StoreMap + LiveSimView + Cart/BudgetBar + ChatPanel + modals) |
| `src/app/merchant/[storeId]/page.tsx` | merchant dashboard |
| `src/app/debug/page.tsx` | raw fallback UI |
| `src/components/` | `StoreMap`, `LiveSimView`, `Cart`, `BudgetBar`, `ChatPanel`, `ApprovalModal`, `PickChoice`, `RecCards`, `TrustLedger`, `Receipt`, `ProfileDiff`, `merchant/{OosTable,MispricingTable,ShelfHeatmap}` |

## How to run

### Demo app (works today, no backend)
UI: shadcn/ui (radix) + Lucide on a custom token set; design direction and rules in [`DESIGN.md`](DESIGN.md).
The shopper app in `src/` runs entirely on generated demo data plus a client-side trip replay. See
[`APP_PLAN.md`](APP_PLAN.md) for the screens, data flow and task list.
```bash
cd arms/frontend
npx pnpm@10.18.3 install         # or `pnpm install` if pnpm is on PATH
npx pnpm@10.18.3 gen-data        # data/*.yaml → src/data/generated/*.json (gitignored; also runs before dev/build)
npx pnpm@10.18.3 dev             # http://localhost:3000 : Overview, Purchases, New order, Live trip, Permissions
```
- **Live feed (`/live`, connecting the real robot):** one connection layer in `src/lib/feed.ts`, three sources picked
  on the Live page (saved per browser): **Simulated** (in-browser trip, no backend), **Robot gateway**
  (`WS /ws/frames?trip_id=` → `frame` events per camera `head|wrist|basket|overview`, only `blurred=true` drawn, plus
  `robot.state` for pose/status/basket; `GET /health` for the status box; auto-reconnect with backoff), and **Direct
  streams** (an MJPEG/MP4 URL per camera). Preset with `NEXT_PUBLIC_GATEWAY_URL` / `NEXT_PUBLIC_STREAM_URL`. The
  gateway must allow CORS from the app origin. The trip page's camera panel uses the same source.
- **Ask Picky (Claude):** `cp .env.example .env.local` and set `ANTHROPIC_API_KEY`. The `/api/chat` route runs Claude
  (`claude-opus-5-5`, low effort, server-side refusal fallback) over deterministic tools in `src/lib/assistant/tools.ts`;
  with no key or on any API error it answers offline from the same tools. Stand-in until the agent arm is reachable
  through the gateway.
- **Stores:** grocery (`store_ave`, generated from `data/`) and a demo T-Mobile store (`src/data/stores/tmobile.ts`,
  frontend-only fixture until the catalog arm adds a planogram for it).
- **Not wired yet:** REST/WS trips through the gateway (the replay emits the same `contracts.events` names, so this is
  a source swap), generated types, merchant dashboard.

### Full stack (planned)
```bash
# one-time scaffold (owner, on arm/frontend)
nvm use 22 && corepack enable
pnpm create next-app@latest arms/frontend --ts --tailwind --app --turbopack --use-pnpm   # keep this README
cd arms/frontend && pnpm dlx shadcn@latest init && pnpm dlx shadcn@latest add button card dialog badge progress table sonner
pnpm add zustand recharts && pnpm add -D openapi-typescript@7.13.0 json-schema-to-typescript
echo "NEXT_PUBLIC_GATEWAY_URL=http://localhost:8000" > .env.local

# every time (repo root)
make dev            # gateway on :8000, all arms fake (= the frontend's fake backend)
make gen-types      # after any contracts change
make web            # pnpm dev on :3000 → http://localhost:3000, /debug, /merchant/store_ave
PICKY_REAL_ARMS=robot,routing make dev   # same UI against real arms
cd arms/frontend && pnpm tsc --noEmit && pnpm lint   # "tests" for this arm (CI: Node 22 + pnpm)
```
Demo cue without a phone: `POST /api/dev/inbound` from `/debug` simulates maya's text.

## How to contribute to this arm
Follow CLAUDE.md §5 exactly; frontend specifics:
1. Claim the draft PR: `gh pr list --label arm:frontend` → `gh pr edit <n> --add-assignee @me`.
2. `git fetch && git switch arm/frontend && git merge origin/main`, then `make gen-types`.
3. Work only in `arms/frontend/`. Never hand-edit generated types; if a field is missing, propose a `contracts/<slug>` PR.
4. Before pushing: `pnpm tsc --noEmit && pnpm lint`, `make check`, and a full fake trip in the browser (`make dev` + `make web`).
5. After editing this README run `make rules` and commit `.cursor/rules/`.
6. Ready = demo-ready checklist below ticked + a screenshot/GIF of the taco trip in the PR, then `gh pr ready <n>`.
   Later changes: `feat/frontend-<slug>` / `fix/frontend-<slug>` branches, one PR each.

## Demo-ready checklist (CLAUDE.md §6)
- [ ] `/debug` drives maya's whole taco trip against all-fake gateway (create → login → approvals → checkout → follow-up)
- [ ] Shopper page shows the full P0 flow with no manual refresh: list + split sourcing → 3 rec cards with reasons →
  route with dashed detours → robot moving on map + live view → top-3 produce pick → cart/budget update → mandate
  verdict → checkout approval (cart-hash echoed) → receipt labelled "Sandbox / test mode" → follow-up → profile diff
- [ ] Trip 2 visibly smarter (different salsa first / spicier recs) on the same screen
- [ ] Survives gateway restart / WS drop: reconnect with `after_seq`, snapshot resync, no duplicate cart lines
- [ ] Predictable failures degrade: `error` events → toast; `/voice` 503 → Web Speech; no frames → map-only + "sim offline";
  409 on checkout approval → refetch and re-ask
- [ ] Works with any mix of `PICKY_REAL_ARMS` (frontend has no flag of its own; all-fake gateway is its fallback)
- [ ] Merchant page shows OOS, mispricing and heatmap for `store_ave` updating from `scan.result`
- [ ] Sponsored badge rendered when set; every rec shows its reason; only blurred frames rendered
- [ ] `pnpm tsc --noEmit` + lint green; generated types up to date; no secrets in the client bundle (only `NEXT_PUBLIC_GATEWAY_URL`)
- [ ] Projector check: readable at 1080p, works on a 1440×900 laptop

## Known limitations
- Not scaffolded yet; depends on `arms.gateway.export_schemas` (gateway arm) for `make gen-types`.
- No auth: `user_id` is picked from the demo users (fictional, `data/fixtures/users.yaml`).
- Map is static SVG (no pan/zoom) sized for `store_ave` (~14 m × 22 m) and `store_hill`; `online` has no map.
