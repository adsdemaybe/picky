# Demo script (3 minutes, judged)

One story, two trips: **trip 1** shows the whole pipeline, a **follow-up text** teaches the profile, and **trip 2** is
visibly smarter. Rubric: Innovation 25 · UX 25 · Tech 20 · Commerce 15 · Trust 15. Demo user is **Maya** (fictional,
`data/fixtures/users.yaml`), store **store_ave**.

## Roles

| Who | Device | Does |
|---|---|---|
| **Narrator** | none | talks, points at the screen, keeps time |
| **Maya (phone)** | demo iPhone that already texted the Photon line "ping" | sends the intent, answers YES/NO, answers the follow-up |
| **Driver** | demo laptop, frontend full-screen | logs Maya in to the robot, clicks approvals in the web UI if iMessage lags, flips to the merchant view |
| **Backstop** | second laptop | watches gateway logs; owns the fallback ladder (below) |

Before going on stage: `make db-up && make seed`, `make dev`, `make web`, sim worker connected (or decide the
fallback rung now), browser on the trip view, phone unlocked with the Photon thread open, Maya's profile reset to
trip-1 state.

## Beats

| Time | Beat | Who clicks / says | On screen | Rubric callout |
|---|---|---|---|---|
| 0:00–0:15 | **Hook.** "Grocery runs eat an hour a week. Picky is a robot that shops with your preferences, your budget and your permission." | Narrator | landing / store map | Commerce |
| 0:15–0:35 | **Plan by text.** Maya texts *"tacos for 6, under $40, one vegetarian"*. | Maya (phone) | chat fills; list appears: tortillas, ground beef, black beans (vegetarian), cheese, salsa, avocados, limes, onion, cilantro, sour cream... with quantities for 6 | Innovation, UX (Photon) |
| 0:35–0:50 | **Split sourcing.** One item is out of stock at store_ave → moved to the online order, one combined checkout. | Narrator points | list badges: "in store" / "online" | Commerce |
| 0:50–1:05 | **Route + detours.** Map shows the list-only route vs the route with 3 recommendation detours, each with its reason: a hot sauce she'd like, a near-expiry guacamole kit markdown, a deal. "+12 m, +25 s." | Narrator | SVG map: solid route, dashed detours, reason cards, `sponsored: no` | Innovation, Tech, Trust (transparency) |
| 1:05–1:40 | **Robot shops.** Driver logs Maya in; robot drives the route, scans, picks, drops in the basket; camera verifies the basket. At produce it shows the **top-3 avocados by freshness** and Maya picks one. At a detour it asks "Hot sauce? You rated chipotle 5★." → Maya says YES. | Driver (login), Maya (YES) | live sim view + cart + budget bar | Tech, UX |
| 1:40–2:05 | **Trust + pay.** Cart goes to the mandate engine: an item crosses "ask me above $X" → `needs_approval` with reason codes → approval request bound to the cart hash → Maya approves → Stripe test checkout via the Visa Intelligent Commerce adapter → receipt. "The LLM proposes; code decides." | Maya (approve), Narrator | mandate panel with reason codes, receipt (`stripe_test`) | **Trust**, Commerce |
| 2:05–2:20 | **Follow-up.** Phone buzzes: *"How was the salsa?"* Maya: *"too mild, loved the hot sauce"*. Profile updates. | Maya (phone) | "what changed" panel: +spicy, salsa rating | Innovation, UX |
| 2:20–2:45 | **Trip 2, visibly smarter.** Same request. A different (spicier) salsa is ranked first, spicier recs, the rejected detour is gone; route re-plans. | Driver (starts trip 2) | side-by-side trip 1 vs trip 2 recs + routes | Innovation, Tech |
| 2:45–3:00 | **Merchant flip + close.** Shelf heatmap and out-of-stock alerts from the robot's trip-1 scans. "The shopper owns the agent, the merchant gets shelf intelligence, and the mandate keeps it trustworthy." | Driver (merchant tab), Narrator | dashboard | Commerce, close |

Say these out loud at least once: *faces are blurred on every frame*, *every recommendation shows why and whether it's
sponsored*, *payments are deterministic code + your explicit approval, never the LLM*, *test-mode payments only*.

## Fallback ladder

Decide the rung **before** walking up; never debug on stage. Everything except the live view stays real on every rung.

1. **Isaac live** — sim worker on the GPU box connected to `/ws/sim`; robot arm real (`PICKY_REAL_ARMS` includes
   `robot`).
2. **2D sim** — drop `robot` from `PICKY_REAL_ARMS` and restart the gateway: the robot fake drives the same trip on
   the top-down map. Say: "this is our 2D planner view; the Isaac run is on the GPU box."
3. **Recorded video** — play the backup recording of a full Isaac trip in the live-view panel while agent, routing,
   mandate, payment and messaging run for real. Say so honestly.

Other per-arm fallbacks (env only): any flaky arm → remove it from `PICKY_REAL_ARMS`. iMessage lag → Driver clicks
YES/approve in the web UI. Stripe down → mock receipt (`provider=mock`). LLM slow → agent fake's canned taco list.

## 30-second version

> "Text Picky *tacos for 6, under $40, one vegetarian*." (list appears, one item goes online) "It plans a route through
> the store with three detours it thinks Maya will like, each with a reason." (map) "The robot shops, picks the freshest
> avocado, and asks before every extra." (live view) "Checkout runs through a deterministic mandate and her explicit
> approval, then Stripe test via a Visa adapter." (receipt) "She says the salsa was too mild, and trip 2 already ranks a
> spicier one first." (side-by-side)
