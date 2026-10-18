# Demo script (3 minutes, judged)

One story, two trips: **trip 1** shows the whole pipeline, a **follow-up text** teaches the profile, and **trip 2** is
visibly smarter. Rubric: Innovation 25 · UX 25 · Tech 20 · Commerce 15 · Trust 15. Demo user is **Maya** (fictional,
`data/fixtures/users.yaml`), store **store_ave**.

> **The live demo store is an OPEN TEAM DECISION:** grocery `store_ave` (this script) or the T-Mobile-style wireless
> store `store_tmobile` ([variant B](#variant-b-wireless-store-t-mobile-cut)). T-Mobile judges see variant B either way,
> because their brief needs a phone-shopping store. The team plan keeps trip 2, so the two-trip grocery script stays as
> written.

## Roles

| Who | Device | Does |
|---|---|---|
| **Narrator** | none | talks, points at the screen, keeps time |
| **Maya (phone)** | demo iPhone that already texted the Photon line "ping" | sends the intent, answers YES/NO, answers the follow-up |
| **Driver** | demo laptop, frontend full-screen | shows the robot's live view and thinking log, clicks approvals in the web UI if iMessage lags, flips to the merchant view |
| **Backstop** | second laptop | watches gateway logs; owns the fallback ladder (below) |

Before going on stage: `make dev` (the catalog runs its in-memory fake: the Postgres catalog is not demo-ready, so
no `make db-up` / `make seed`), `make web`, sim worker connected (or decide the fallback rung now), browser on the trip
view, phone unlocked with the Photon thread open, Maya's profile reset to trip-1 state. Approvals wait for a person
(the web app or a real text); only for an unattended rehearsal start the gateway with `PICKY_SCRIPTED_SHOPPER=1`
(the fake texts YES/NO and the follow-up by itself; never on stage).

## Beats

| Time | Beat | Who clicks / says | On screen | Rubric callout |
|---|---|---|---|---|
| 0:00–0:15 | **Hook.** "Grocery runs eat an hour a week. Picky is a robot that shops with your preferences, your budget and your permission." | Narrator | web app Home (month spend vs budget, "probably running low"), then the store map | Commerce |
| 0:15–0:35 | **Plan by text.** Maya texts *"tacos for 6, under $40, one vegetarian"*. | Maya (phone) | chat fills; list appears: tortillas, ground beef, black beans (vegetarian), cheese, salsa, avocados, limes, onion, cilantro, sour cream... with quantities for 6 | Innovation, UX (Photon) |
| 0:35–0:50 | **Split sourcing.** One item is out of stock at store_ave → moved to the online order, one combined checkout. | Narrator points | list badges: "in store" / "online" | Commerce |
| 0:50–1:05 | **Route + detours.** Map shows the list-only route vs the route with 3 recommendation detours, each with its reason: a hot sauce she'd like, a near-expiry guacamole kit markdown, a deal. "+12 m, +25 s." | Narrator | SVG map: solid route, dashed detours, reason cards, `sponsored: no` | Innovation, Tech, Trust (transparency) |
| 1:05–1:35 | **Robot shops.** The trip starts by itself (request-created trips need no login); Pico drives the route, scans, picks, drops in the basket; camera verifies the basket. At produce it shows the **top-3 avocados by freshness** and Maya picks one. At a detour it asks "Ghost Pepper hot sauce? You rated Classic Red 5★." → Maya says YES. | Maya (YES); Driver points at the robot's thinking log | live sim view + cart + budget bar | Tech, UX |
| 1:35–1:50 | **Cart Whisperer (T-Mobile).** Live card at aisle 3: *"1 pack of 30 tortillas covers 6 people, pick 1 not 2: −$2.79"* (accept) and *"beef but no seasoning: Taco Seasoning $1.49, +0 m"* (accept); at the back wall *"swap 1 lb beef for Soy Chorizo, save $1.00"* → Maya: *no*. Before you pay: *"cilantro ships for $2.99, drop it: −$4.28"* (accept). "It used her request, her history, her budget and the store, and every card says why." | Maya (taps), Narrator | ≤3 cards with price delta, detour, "why am I seeing this"; "saved $7.07 · added $1.49 · 3 of 4 accepted" | **Commerce**, UX, Trust (T-Mobile track) |
| 1:50–2:05 | **Trust + pay.** Cart goes to the mandate engine: an item crosses "ask me above $X" → `needs_approval` with reason codes → approval request bound to the cart hash → Maya approves → Stripe test checkout via the Visa Intelligent Commerce adapter → receipt. "The LLM proposes; code decides." Flash the Permissions page: what Picky remembers (delete any of it), the mandate, the approvals ledger. | Maya (approve), Narrator | mandate panel with reason codes, receipt (`stripe_test`), Permissions page + trust ledger | **Trust**, Commerce |
| 2:05–2:20 | **Follow-up.** Phone buzzes: *"How was the salsa?"* Maya: *"too mild, loved the hot sauce"*. Profile updates. | Maya (phone) | "what changed" panel: +spicy, salsa rating | Innovation, UX |
| 2:20–2:45 | **Trip 2, visibly smarter.** Same request. A different (spicier) salsa is ranked first, spicier recs, the rejected detour is gone; route re-plans. Cart Whisperer learned too: the Soy Chorizo swap she declined is gone ("hidden because you declined it"), a chips deal takes its slot. | Driver (starts trip 2) | side-by-side trip 1 vs trip 2 recs + routes | Innovation, Tech |
| 2:45–3:00 | **Merchant flip + close.** Shelf heatmap and out-of-stock alerts from the robot's trip-1 scans. "The shopper owns the agent, the merchant gets shelf intelligence, and the mandate keeps it trustworthy. One memory, online and in the store." | Driver (merchant tab), Narrator | dashboard | Commerce, close |

The 1:35 Cart Whisperer beat is **designed, not implemented** in the backend yet
([`docs/cart-whisperer.md`](cart-whisperer.md) §11a; amounts from the current fixtures). Until it lands, skip it and
give the 15 s back to the robot. The web app already shows the planning-phase SKIP / WAIT cards ("Picky's advice") on
demo data (Robot order page). Its taco card reads "Skip hot sauce: you bought Classic Red on Oct 11" (seeded history;
the fixture says Sep 26), and its taco detours are Chipotle salsa, guac kit and chips, not Ghost Pepper. Until the
preset is aligned with this story (`docs/cart-whisperer.md` §14), narrate what's on screen.
The merchant tab is not built in the web app yet (who builds it is open); until it is, show the stock alerts on the
gateway's `/debug` page.

Route numbers: show the detour cost against the list-only optimal route ("+12 m, +25 s"). Any "% shorter" figure must
use aisle order or nearest neighbor as the baseline, and every number on screen comes from code, never hardcoded.

Say these out loud at least once: *faces are blurred on every frame*, *every recommendation and suggestion shows why and
whether it's sponsored*, *a suggestion can't skip your approval*, *it tells you when to skip or wait*, *saying nothing
is a valid answer*, *payments are deterministic code + your explicit approval, never the LLM*, *test-mode payments
only*, *synthetic data only*. Never say "privacy mode" or "nothing leaves the store": model calls go to the Claude API.

## Variant B: wireless store (T-Mobile cut)

About 45 s at `store_tmobile`. It replaces the 1:35 Cart Whisperer beat when the live store is the wireless store, and
it is the T-Mobile judges' cut either way. Numbers come from the web app's demo data (`arm/frontend`
`src/lib/order.ts`, `src/data/stores/tmobile.ts`; fictional); re-check them once `store_tmobile` moves into `data/`.
Detail: [`docs/cart-whisperer.md` §11b](cart-whisperer.md#11b-wireless-store_tmobile-the-t-mobile-cut).

| Time | Beat | Who clicks / says | On screen | Rubric callout (T-Mobile ⚠️ team's copy) |
|---|---|---|---|---|
| +0:00–0:10 | **Request.** Robot order, store `store_tmobile`: *"case and screen protector for my new Pixel 9, under $90"*. List: Pixel 9 Clear Case $29.99, Pixel 9 Glass 2-pk $23.99 (20% off with a Pixel 9 case). Detours: USB-C braided cable $19.99 (+0 m, same wall) kept; power bank $34.99 not kept (over $90). | Driver, Narrator | list with badges; detour cards with reason + "Not sponsored" | Use of signals |
| +0:10–0:25 | **SKIP + WAIT ("Picky's advice").** *"Skip the charger: you bought a 20W USB-C charger in March and it works with the Pixel 9"* (saves $24.99). *"Your Pixel 7 has 4 installments left ($66.00); a $200-off promo is expected Nov 27. I'll remind you."* "It tells you when to skip or wait." | Narrator | "Skip it" and "Worth waiting" cards with "Saves $X" | **Customer value**, Explainability |
| +0:25–0:35 | **Robot shops + checkout.** Total $73.97. The tech mandate (cap $150, ask above $50) lets every line through, so only the checkout approval asks. In-trip (designed, backend v0.3): "grab the $9.99 clearance case too" → SKIP, "that's a Pixel 7 case; the Clear Case in your basket fits the Pixel 9". | Maya (approve) | live trip, cart, checkout approval | Feasibility & demo, Trust |
| +0:35–0:45 | **Zero suggestions.** Second request *"earbuds for running, under $100"*: SportFit $79.99 + WAIT (*"$59.99 from Fri Oct 23, save $20. I'll text you Friday"*) and *"Nothing worth a stop today, so the robot goes straight down your list."* "Saying nothing is a valid answer." (The $79.99 line is above the $50 ask threshold, so it asks too.) | Driver, Narrator | WAIT card; empty detour panel with the zero copy | **Customer value** |

Story check before rehearsal (team call): if Maya takes the WAIT on the phone, the case and glass are for a phone she
doesn't own yet. Either phrase the request as "I'm picking up my Pixel 9 today" and make the WAIT about the second
line, or show the phone WAIT only when the phone is on the list.

## Per-track cuts

| Track | Cut |
|---|---|
| **T-Mobile** | Say "Cart Whisperer". Run variant B (SKIP, WAIT, upgrade timing, the zero case). Show a "synthetic data only" slide. |
| **Photon** | Narrate the request, the robot's questions, the approval and the follow-up from the phone screen; let a judge text the number. |
| **SideShift** | Hook → robot shops → merchant flip → business slide (free for shoppers; stores pay robot-as-a-service + shelf intelligence + a share of measured basket lift; the "says wait" recommender is the moat). |
| **Visa (optional 15 s)** | Find & compare: *"Is coffee a good price?"* → **Wait** (weekly ad $2 off Fri Oct 23, $7.99), the decision stage of the journey. |

## Fallback ladder

Decide the rung **before** walking up; never debug on stage. On rungs 1–3 everything except the live view stays real.
Rungs 1–3 show in the web app only once it is wired to the gateway (`GatewaySource`, not built yet); until then the
gateway's `/debug` page is the screen for them.

1. **Isaac live** — sim worker on the GPU box connected to `/ws/sim`; robot arm real (`PICKY_REAL_ARMS` includes
   `robot`).
2. **2D sim** — drop `robot` from `PICKY_REAL_ARMS` and restart the gateway: the robot fake drives the same trip on
   the top-down map. Say: "this is our 2D planner view; the Isaac run is on the GPU box."
3. **Recorded video** — play the backup recording of a full Isaac trip in the web app's camera panel (Live → Direct
   stream with the MP4's URL) while agent, routing, mandate, payment and messaging run for real. Say so honestly.
4. **Web app replay** — if the gateway itself is down, the web app's own client-side trip replay and simulated cameras
   still run the whole trip on demo data (its taco preset differs from this script; see the note under Beats). The
   feed is labeled "Simulated"; say so out loud.

Other per-arm fallbacks (env only): any flaky arm → remove it from `PICKY_REAL_ARMS`. iMessage lag → Driver clicks
YES/approve in the web UI. Stripe down → mock receipt (`provider=mock`). LLM slow → agent fake's canned taco list.

## 30-second version

> "Text Picky *tacos for 6, under $40, one vegetarian*." (list appears, one item goes online) "It plans a route through
> the store with three detours it thinks Maya will like, each with a reason." (map) "The robot shops, picks the freshest
> avocado, and asks before every extra." (live view) "Checkout runs through a deterministic mandate and her explicit
> approval, then Stripe test via a Visa adapter." (receipt) "She says the salsa was too mild, and trip 2 already ranks a
> spicier one first." (side-by-side)

Wireless version (T-Mobile): "Case and glass for her new Pixel 9, under $90. Picky says skip the charger, she bought
one in March, and wait on the phone, $200 off is expected Nov 27. For running earbuds: wait for Friday's sale, and
nothing is worth a detour today."
