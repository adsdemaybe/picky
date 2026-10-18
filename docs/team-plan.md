# Team Plan ("Aisle" master plan) + reconciliation with CLAUDE.md

> **What this file is:** the team's master plan for DubHacks 2026 (Part B, kept verbatim as written), preceded by an
> analysis (Part A) that reconciles it with [`CLAUDE.md`](../CLAUDE.md), flags contradictions, and lists the decisions
> the team still has to make.
>
> **Precedence (until the team decides otherwise):** `CLAUDE.md` governs repo rules, git workflow, architecture,
> conventions and trust rules. This plan adds product scope, track strategy, team ownership, timeline and demo script.
> Where the two conflict on *what we build* (see A.2), the conflict is **open**. Resolve it, then update `CLAUDE.md`
> (and run `make rules`) so there is one source of truth again.
>
> Date added: 2026-10-17 (Saturday, hacking day 1).

---

# PART A — ANALYSIS

## A.1 TL;DR

1. **The plan and `CLAUDE.md` describe two different robots.** `CLAUDE.md` builds an **Isaac Sim** mobile manipulator
   in a **grocery** store generated from a planogram. The plan builds a **physical** robot on a **tape-line grid with
   AprilTags** in a **T-Mobile-style wireless store**, with a 2D "store sim view" as fallback. This is the biggest
   decision to make, and it changes the sim, robot, perception, catalog and routing arms.
2. **The plan is much broader than `CLAUDE.md` §3.** It adds price comparison and history, deal finding, budget swaps,
   remote delegation, guest mode, returns, loyalty, privacy mode / local-model fallback, an audit log, and group
   shopping. `CLAUDE.md` has things the plan lacks: the merchant dashboard, produce freshness ranking, batched shoppers,
   split sourcing (in-store vs online), and the "trip 2 is visibly smarter" demo beat.
3. **The software core agrees.** One shared agent with tools, a deterministic mandate engine with explicit approval, a
   prize-collecting route with recommendation detours, reason-coded recommendations, Photon for text, a mock Visa
   checkout, and a live map UI. Build these first regardless of the robot decision.
4. **The repo layout and data shapes in the plan don't follow the repo rules.** The plan uses `brain/ agent/ robot/ web/`
   folders and JSON with float dollars. The repo uses `arms/<arm>/`, `contracts/` (pydantic, locked), `utils/`, and
   integer cents. Map the plan onto the arms (A.4). Don't create the plan's folders.
5. **Several plan claims are not true as written**, mainly "nothing leaves the store" in privacy mode (A.5). Fix them
   before they reach a slide, because Trust is 15% of the Visa score.

## A.2 Conflicts with CLAUDE.md (decisions needed)

| # | Topic | CLAUDE.md says | Plan says | Recommendation |
|---|---|---|---|---|
| D1 | **Robot embodiment** | Isaac Sim (Franka on mobile base), RMPflow picking, sim bridge | Physical 4-wheel robot, line following, AprilTags, replayed arm trajectories; 2D sim view as Tier 0 | **Decide in the first hour, based on hardware inventory (plan §2.5).** If real hardware exists and someone owns it full time, go physical. It's far more memorable live, and the plan's Tier 0 makes it safe. If not, keep Isaac Sim. Either way, the gateway talks to the robot through one `RobotService` Protocol, so the 2D sim, Isaac Sim and the real robot are interchangeable implementations (real/fake swap, `CLAUDE.md` rule 6). |
| D2 | **Store type** | Grocery ("tacos for 6", produce freshness, near-expiry markdowns) | T-Mobile wireless store (cases, chargers, earbuds, upgrade timing) | The wireless store fits the T-Mobile track (which is about phone shopping) and makes "says wait" lines concrete. Grocery fits Visa's everyday-commerce story and the freshness feature. **Cheapest path: one store as the live demo, the other as a second planogram/config** (the plan already pitches "swap the config"). Pick the live one by which side track the team values more. |
| D3 | **Project name** | Picky | Aisle | Pick one before Devpost and slides. Repo, README and CLAUDE.md say Picky. |
| D4 | **Store map / source of truth** | Planogram → sim scene, DB seed, occupancy grid → A* | `store_map.json` node graph (tape grid) + `catalog.json` | Compatible. Keep the planogram as the source of truth and **generate** both the node graph (for a tape grid) and the occupancy grid from it. Product → node/slot/tag lives in the planogram. |
| D5 | **Repo layout** | `arms/<arm>/`, `contracts/`, `utils/` | `brain/ agent/ infra/ perception/ robot/ firmware/ web/ photon/ pitch/` | Follow `CLAUDE.md`. Mapping in A.4. Firmware and pitch are non-Python and need homes: `arms/robot/firmware/` and `docs/pitch/`. |
| D6 | **Money** | Integer cents | Float dollars (`29.99`) in every JSON sample | Integer cents in `contracts`. Fixtures store `2999`. Format only at the UI or text edge. |
| D7 | **Data store** | "Postgres/Supabase" for mock ERP | Supabase with auth + row-level security on 8 tables | Supabase is fine. **Skip RLS for the hackathon**: the backend uses the service role, and the browser never queries tables directly. Have a SQLite/in-memory fallback in `utils.db` for when venue Wi-Fi drops (the plan's own risk register rates that High). |
| D8 | **LLM access** | Everything through `utils.llm` | Everything through a LiteLLM proxy with aliases (`agent`, `vision`, `explain`) | Compatible: `utils.llm` calls the LiteLLM proxy (or the `litellm` library) by alias. Default to the latest Claude models for the API tier (`claude-opus-5-5` for the agent, `claude-haiku-5-5` for `explain`). |
| D9 | **Photon SDK language** | Python ≥3.11 everywhere | `spectrum-ts` (Node/Bun) | The messaging arm becomes a small Node sidecar in `arms/messaging/`, talking to the gateway over HTTP/websocket. Alternative: Photon's REST API directly from Python. Prefer REST if it supports inbound webhooks for iMessage, because it keeps one language. Verify that in the first 30 minutes. |
| D10 | **Event/command protocol** | `contracts.events` → websocket → frontend | `{"cmd":"goto"}` / `{"evt":"arrived"}` JSON | Put the plan's commands and events into `contracts` as pydantic models. Same content, typed. |
| D11 | **Perception approach** | VLM label read or CLIP/SigLIP embedding match; produce freshness top-3; face blur on *every* frame | AprilTag on every product for identity; VLM only for "is this the right one?"; face blur on *stored* frames | Tags for identity is the right reliability call. **Keep `CLAUDE.md`'s stricter rule: blur every frame** before storage, any LLM call, or streaming. Freshness top-3 applies only if the live store is grocery. |
| D12 | **Team structure** | 11 arms, each with a branch `arm/<arm>` and an issue | 4 owners (A–D) with end-to-end slices | Compatible. Each owner owns several arms (A.4). Branch per arm still applies. |

## A.3 Scope reconciliation

**In both (core; build first):** account/profile, list + budget, agent with tools, recommender with reasons, route
optimizer with detours, mandate engine + explicit approval, mock Visa checkout, Photon text flows, live map UI with
cart/budget/approvals, basket verification, face blurring.

**Only in the plan (add to `CLAUDE.md` §3 if kept):**

| Feature | Plan scope | Rubric value | Cost | Keep? |
|---|---|---|---|---|
| Remote delegation ("grab my usual") | BUILD | High (Innovation, Commerce) | Medium (needs a robot that runs without a shopper) | **Yes**. It's the strongest "why a robot" answer. |
| "Says wait" SKIP/WAIT cards, "nothing today" | BUILD | High (T-Mobile 30%+20%, Visa Trust) | Low (rules + templates) | **Yes** |
| Cross-retailer compare / good-price / price history | BUILD | Medium (Visa Decision stage) | Low with synthetic data | Yes, as a catalog-arm feature |
| Audit log + memory approval panel | BUILD | High (Trust 15%) | Low | **Yes** |
| Local-model fallback / privacy mode / cloud-on-prem chip | BUILD | Medium (Tech Exec, Trust) | **High** (GB10 + 5090 serving, prompt testing on 2 models) | Keep the fallback chain. Cut the "privacy mode" claim unless it's made true (A.5). |
| Walk-up guest mode | BUILD | Medium (UX) | Low (an agent prompt without a profile) | Yes |
| Returns with label + weight check | DEMO-LIGHT | Low–Medium | Medium (needs a scale) | Bonus beat only |
| Loyalty points | DEMO-LIGHT | Low (Visa Loyalty stage) | Very low | Yes, a receipt line |
| Group/household shopping | STRETCH | Low | Medium | Overlaps with `CLAUDE.md`'s "batched shoppers". Do one, not both. |

**Only in `CLAUDE.md` (the plan drops these; decide explicitly):**

| Feature | Recommendation |
|---|---|
| Merchant dashboard (out-of-stocks, mispricings, shelf heatmap) | **Keep, small.** It's the SideShift B2B revenue story ("stores pay for the robot") and adds Commerce Impact. Data comes free from the scans. |
| Trip 2 visibly smarter after post-purchase feedback | **Keep.** It's the clearest proof the memory works, and the plan's demo has no beat for it. It can replace the returns bonus beat. |
| Split sourcing (in-store vs online, one checkout) | The plan's cross-retailer compare already covers half of it. Keep it if the catalog is multi-store anyway. |
| Produce freshness top-3 | Only if the live store is grocery (D2). |
| Batched shoppers in one trip | Cut, or merge with group shopping. |

## A.4 Owner → arm mapping

| Owner | Arms | Plan folders that map here |
|---|---|---|
| **A — Robot** | `robot`, `sim` (Isaac Sim, or the physical store build + 2D sim, per D1) | `robot/`, `firmware/` → `arms/robot/` (+ `arms/robot/firmware/`) |
| **B — AI/Infra** | `agent`, `perception`, plus `utils/llm` (via a `utils/` PR) | `agent/`, `infra/`, `perception/` |
| **C — Commerce** | `catalog`, `routing`, `recommender`, `trust`, `gateway` (orchestrator) | `contracts/`*, `brain/` |
| **D — Product** | `frontend`, `messaging` | `web/`, `photon/`, `pitch/` → `docs/pitch/` |

\* In this repo `contracts/` means locked pydantic interfaces, not data. The plan's `store_map.json`, `catalog.json`
and `users/*.json` go in `data/planograms/` and `data/fixtures/`.

**Load concern:** C owns five arms including the gateway, which is the integration bottleneck. **Move `gateway` to B
or split it**: B takes the gateway/orchestrator, and C keeps the domain logic. Otherwise C is the critical path for
everyone from 9pm on.

## A.5 Problems inside the plan

Claims that are false or risky as written:

1. **"Privacy mode: nothing leaves the store / no frame leaves the building"** is contradicted by the plan itself. Photon
   (cloud) delivers order **photos** to the shopper, Supabase is cloud-hosted, and remote-order texts go out over the
   internet. Either privacy mode also disables photo texts and uses a local DB, or the claim becomes **"no model
   inference leaves the building."** Judges on the Trust criterion will catch this.
2. **"Face blurring on any stored frame"** is weaker than `CLAUDE.md` §8. Frames sent to an API vision model or streamed
   to the kiosk/web must be blurred too, so blur at capture.
3. **Mandate sample vs engine:** `requires_yes` lists `"purchase"`, so *every* purchase asks, which makes
   `auto_approve_under: 20` dead config. Decide which wins. Suggested: auto-approve under the threshold only in remote
   mode for list items, and always ask for off-list items. `CLAUDE.md`: "ask me above $X" **always** asks.
4. **Demo beat 4 (remote: "the robot picks the item")** depends on Tier 2 (arm = stretch). Script the fallback line
   now ("a store associate drops it in, and the robot verifies the basket") so the beat survives a no-arm outcome.
5. **The basket camera reads tags** of items piled in a basket. Tags will be occluded or face down. Mitigate: put tags
   on several faces, use a shallow basket, verify **at each drop** (camera over the drop zone) rather than scanning the
   whole basket, and keep a running cart instead of re-reading everything.
6. **"X% shorter than shopping in list order"** is easy to inflate by ordering the list badly. Compare against a
   naive baseline the judges would accept (aisle order or nearest-neighbor) to keep the metric honest.
7. **Detour caps vs penalty:** "at most 2 detours and at most 15% extra distance" are hard constraints layered on top of
   OR-Tools disjunction penalties. 12 items is small, so brute-force or OR-Tools with a dimension cap both work. Make
   sure the "nothing worth a stop today" case gives **zero** detours without special-casing.
8. **Demo timing:** a 3-minute demo with three modes and a physical robot driving a 3×4 m store is tight. Robot travel
   time between stops is the hidden cost. Measure one route's wall-clock time early, and keep beat 3's route to 3–4
   stops.
9. **GB10 memory:** gpt-oss-120b (~65 GB MXFP4) plus Qwen3-VL-30B-A3B (~60 GB bf16) don't both fit in 128 GB with KV
   cache. Use FP8/quantized VL weights or serve one at a time, and check what's already on disk.
10. **Supabase RLS on 8 tables "in the first hour"** isn't realistic with everything else C owns in that hour. See D7.
11. **Floats for money** in every sample (D6). The order cap check `amount > 100` with floats is the kind of bug that
    shows up live.
12. **The deadline is assumed (10am Sunday).** The whole timeline depends on it. Confirm it first.

Things the plan gets right (keep them):
- BUILD / DEMO-LIGHT / PITCH scoping, tiers, and a cut line per owner.
- Deterministic core (route, recommender, mandates, checkout) that works with zero models.
- Reason codes produced by code and phrased by the LLM, never invented by it.
- Own router, local fallback, and one rehearsal with the API key removed.
- Avoiding US SMS (A2P 10DLC) and using iMessage/Telegram for Photon.
- Sleep rotation that never takes A and B off together.

## A.6 Recommended first-hour actions (in order)

1. Hardware inventory → **decide D1 (sim vs physical)** and **D2 (live store)**. Confirm the deadline and the Devpost
   track cap.
2. Update `CLAUDE.md` §3 with the decided scope (A.3), and the name (D3). Run `make rules`.
3. `contracts/` PR: pydantic models for user/profile, mandate, product/offer (cents), planogram/store graph, session,
   cart, route, recommendation (with reason codes), approval, robot commands/events, and the `RobotService` Protocol
   that covers sim, physical and 2D-fake.
4. `data/` PR: planogram for the live store plus 3 synthetic users, orders and promos (from the plan's §4.1 samples,
   converted to cents).
5. Each owner branches `arm/<arm>` for their first arm, and the fakes land first so `make demo` runs end to end on
   fakes by the 9pm checkpoint.

## A.7 Rubric check (Visa weights)

| Criterion | Strongest evidence after reconciliation | Gap to watch |
|---|---|---|
| Innovation 25% | Detours priced by walking cost; robot picks for you remotely; one memory across phone and robot | Make sure the route optimizer is *visible* (show the detour on the map with its value) |
| UX 25% | Login code → robot already has your list; text approvals | Voice in a loud hall; push-to-talk + transcript |
| Tech Exec 20% | Orienteering, mandate engine, real/fake arm swap, model fallback | Integration reliability; rehearse full runs |
| Commerce 15% | Metrics panel, merchant dashboard, basket lift | Metrics must be computed, not hardcoded |
| Trust 15% | Mandates, approvals, audit log, memory panel, reason-coded recs, face blur, no paid placement | Fix the privacy-mode claim (A.5 #1) |

---

# PART B — ORIGINAL PLAN (verbatim)

> Kept as written by the team. Money in these samples is in float dollars; in code it's integer cents (D6).
> Folder names here map to arms per A.4.

# Aisle — Personal AI Shopping Agent + Physical Shopping Robot
### DubHacks 2026 · Master Team Plan

> **Event:** DubHacks, Oct 17–18, 2026, HUB North Ballroom, UW
> **Team:** fill in names: **A** ______ · **B** ______ · **C** ______ · **D** ______
> **How to use this doc:** Parts 1–6 explain *what* we're building and *why*. Part 7 says *who does what*, with checklists. Part 8 is the clock. Tick boxes as you go: `[ ]` → `[x]`.

---

## Table of contents

- **Part 1:** The idea (core idea, Layer 1, Layer 2, how they connect, the three ways to shop)
- **Part 2:** Feasibility and scope (what we build vs. demo vs. pitch)
- **Part 3:** Track alignment and requirement checklists (Visa, T-Mobile, Photon, SideShift)
- **Part 4:** System architecture and shared data
- **Part 5:** Hardware plan and API → local model fallback
- **Part 6:** Core algorithms
- **Part 7:** Team split: four owners with checklists
- **Part 8:** Timeline and checkpoint checklists
- **Part 9:** Demo script
- **Part 10:** Submission checklists
- **Part 11:** Risk register
- **Part 12:** Repo layout, tools and sources

---

# PART 1 — THE IDEA

## 1.1 The core idea

**Build the personal AI shopping brain first, then connect it to a physical robot that can act in the real world.** Together they form one shopping platform that helps customers **online, in-store, or remotely**, without the customer having to be physically present for every task.

**The problem:**
- Physical shopping is slow. You hunt for items, checkout glitches, and you walk past things you'd actually want because nobody mentioned them.
- Online shopping is scattered across tabs, retailers and forgotten carts.
- Nothing connects the two. The store has no idea what you planned at home, and your phone has no idea what's on the shelf in front of you.

**Our answer:** one agent, one account and one memory, working through two bodies. One is a screen or text thread (Layer 1). The other is a robot with wheels, a camera and an arm (Layer 2).

## 1.2 Layer 1: Personal AI Shopping Agent (online)

A **Phia-like personal shopping brain** for each customer. It has a user account and remembers **only what the customer approves**:

| It remembers | It helps you |
|---|---|
| Approved preferences (sizes, brands, dietary choices, "never suggest X") | **Discover** products |
| Budgets (per trip, per month, per category) | **Compare** prices across retailers, including open-box and resale |
| Shopping lists | **Find deals** and judge whether a price is actually good, using price history |
| Saved product comparisons | **Optimize spending** with swaps that save money |
| Purchase history and items you own | **Plan purchases**, e.g. "wait, this goes on sale Friday" |
| Loyalty rewards and points | **Manage orders** (status, pickup, delivery) |
| Household or group members (opt-in) | **Handle post-purchase tasks**: returns, exchanges, price-drop refunds |

It can also **shop remotely on the customer's behalf**, within permissions the customer sets, so they don't have to visit the store. Example: "Buy my usual groceries, under $60, text me before paying anything over $20."

It lives in two places: a **web app** and a **text thread** (Photon: iMessage and others).

## 1.3 Layer 2: Physical AI Shopping Robot (in-store)

The **same intelligence in the physical world.** The robot connects to the customer's account and continues shopping **from where the online agent left off.**

**Robot hardware:** a 4-wheel drivetrain, a robotic arm, a basket, a camera, a screen, plus a mic and speaker.

**What it does:**
- **Identifies and locates products** using computer vision and the store map
- **Verifies items on shelves**: is it in stock, and is it the right variant?
- **Navigates** an efficient route that **passes recommended items**, and stops to ask if you want them
- **Collects products** with the arm where supported
- **Prepares orders**, including picking a remote customer's order on its own
- **Verifies the cart** with a basket camera that checks what's in it against the list
- **Assists checkout** and payment, which needs your approval
- **Handles physical returns and exchanges** by checking a returned item's label and weight

**Two ways to work with it:**
- **In person.** Anyone can walk up and talk to it or tap its screen. **Guest mode** handles "where are the chargers?" and "is this a good price?" **Logged-in mode** loads your list, budget and memory. You log in by scanning a QR code or texting a code.
- **Remote.** The robot executes supported tasks independently while you're elsewhere. It picks your order, verifies items, sends photos, and texts "ready for pickup."

## 1.4 How the two layers work together

Both layers share **one** of each: AI agent, customer account, shopping memory, product catalog, budget, shopping list and cart.

```
        PLAN ONLINE                 CONTINUE IN STORE              OR DELEGATE
   ┌───────────────────┐        ┌────────────────────┐       ┌──────────────────┐
   │ Layer 1 (web/text)│ ─────► │ Layer 2 (robot,    │       │ Layer 2 (robot,  │
   │ list, budget,     │ login  │ customer present)  │       │ customer remote) │
   │ compare, deals    │        │ route, detours,    │       │ picks order,     │
   └─────────┬─────────┘        │ "says wait", cart, │       │ verifies, texts  │
             │                  │ checkout           │       │ "ready"          │
             └──────────────────┴─────────┬──────────┴───────┴────────┬─────────┘
                                          ▼                           ▼
                      SHARED BRAIN: one agent · account · memory · catalog ·
                      budget · list · cart · permissions · order history
```

**The full shopping lifecycle it covers:** discovery, product finding, in-store assistance, remote shopping, budget optimization, fast checkout, loyalty rewards, post-purchase support, group shopping, and returns and exchanges.

**Adapts to different stores** by swapping the store map and catalog: grocery, general retail, and **T-Mobile stores** (our demo store).

**Safety rule built in from the start:** the customer's information is protected, and **every purchase and other consequential action requires the right approval** (Part 6.4).

## 1.5 The three ways to shop (our demo backbone)

| Mode | Customer is… | What happens |
|---|---|---|
| **① Online** | At home, on phone or web | Layer 1 builds the list, compares prices, finds deals and plans within budget |
| **② In-store with the robot** | Standing next to the robot | Login → the robot continues the plan, routes past worthwhile items, says "skip this" when it should, verifies the cart, checks out |
| **③ Remote delegation** | Anywhere | "Grab my usual." The robot picks the order alone, texts photos and asks for approval, and the order waits at pickup |

## 1.6 The thing judges should remember

> **Everyone else built a shopping chatbot. Ours carries your shopping memory into the real world, drives the route itself, and is willing to tell you "don't buy that."**

The robot earns its place in **mode ③**, because picking orders is real physical labor and a bottleneck for stores. The memory earns its place in **mode ②**, because nothing else connects your online plan to the aisle you're standing in.

---

# PART 2 — FEASIBILITY AND SCOPE

## 2.1 Honest verdict

The full vision is a company, not a weekend. It **can be demoed convincingly in the time left** if we split every feature into **BUILD** (works live), **DEMO-LIGHT** (works on a happy path with mock data) and **PITCH** (a slide only). Judges reward a working slice of a big vision. They punish a broken attempt at the whole thing.

## 2.2 Feature scope table

| Feature | Layer | Scope | Notes |
|---|---|---|---|
| Account and approved memory panel | 1 | **BUILD** | Supabase auth; memory items with approve/delete toggles |
| Shopping list and budget | 1 | **BUILD** | Shared by both layers |
| Price comparison across retailers | 1 | **BUILD** | **Synthetic** multi-retailer catalog, including open-box. No live scraping. |
| "Good price?" plus deal finder | 1 | **BUILD** | Synthetic price history and promo calendar |
| Budget optimizer (swaps) | 1 | DEMO-LIGHT | Rule-based swaps |
| Text agent (Photon) | 1 | **BUILD** | List edits, login code, approvals, receipts |
| Remote shopping with permissions | 1+2 | **BUILD** | The mandate engine (6.4) and the robot picking alone |
| Robot navigation on a route with detours | 2 | **BUILD** | Tape-line grid with AprilTags at the nodes. No SLAM. |
| "Says wait" recommender | 1+2 | **BUILD** | Reason codes plus explanation |
| Walk-up guest mode | 2 | **BUILD** | Touch screen plus push-to-talk |
| Shelf verification (vision) | 2 | DEMO-LIGHT | Tag gives identity; the vision model answers "is this the right one?" |
| Arm picking | 2 | DEMO-LIGHT → stretch | 1–2 known shelf slots with replayed trajectories; human-drop fallback |
| Basket cart verification | 2 | **BUILD** | Tags read by the basket camera |
| Checkout and payment | 1+2 | **BUILD** (mock) | Visa test card; approval by text |
| Loyalty points | 1 | DEMO-LIGHT | Points on the receipt |
| Returns and exchanges | 1+2 | DEMO-LIGHT | Agent starts the return; robot checks label and weight; instant refund (mock) |
| Group or household shopping | 1 | STRETCH | Photon group chat edits a shared list |
| Live "robot eyes" video for remote shoppers | 2 | PITCH | Slide only |
| Multiple store types | both | PITCH + config | Demo the T-Mobile store; show grocery as a swapped config file |

## 2.3 Build tiers (protect the demo)

- [ ] **Tier 0, the software spine (must work by the 9pm checkpoint).** Layer 1 web app and Photon, the shared brain, and a **store sim view** with a 2D map and a robot dot following the route. This is a complete submission for every track even if all hardware dies.
- [ ] **Tier 1, the robot drives (by the 1am checkpoint).** The real robot follows the route, stops, detours, talks, and verifies the basket.
- [ ] **Tier 2, the arm and remote picking (stretch, after 1am).** The robot picks 1–2 items alone for a remote order.

## 2.4 Key simplifications (and why)

| Instead of… | We do… | Because… |
|---|---|---|
| SLAM / free navigation | Tape-line grid with AprilTags at the intersections | Deterministic, buildable in hours, and the map *is* the route graph |
| Recognizing any product | AprilTag/QR on every product, with the vision model only for "is this the right one?" questions | Reliable identity under bad lighting |
| Grasping anything | Fixed shelf slots with recorded trajectories | It's what makes an arm demo work in 18 hours |
| Scraping real retailers | Synthetic multi-retailer catalog | Legal, reliable, and the tracks require synthetic data |
| Real payments | Mock Visa-shaped payment service with test cards | Visa sandboxes need credentials we may not get |
| Voice-only in a loud hall | Push-to-talk, screen transcript, text fallback | Ballroom noise |

## 2.5 Unknowns to resolve in the first 15 minutes

- [ ] What robot hardware exists: chassis, motors and motor driver, microcontroller, **which arm**, webcams, onboard laptop?
- [ ] Floor space for a mock store (about 3 × 4 m). Ask the organizers.
- [ ] The real submission deadline (the timeline in Part 8 assumes about 10am Sunday).
- [ ] Visa booth: any sandbox credentials? Photon beta access working?
- [ ] Devpost: is there a cap on how many tracks one project can enter? If so, the priority order is Visa → T-Mobile → Photon → SideShift.

---

# PART 3 — TRACK ALIGNMENT AND REQUIREMENT CHECKLISTS

## 3.1 Visa: Reimagine Shopping (main track)

**Stage coverage.** The Visa track lists seven journey stages, and we hit all of them:

| Visa stage | Where in Aisle |
|---|---|
| Discovery | Robot detours past worthwhile items; Layer 1 deal finder |
| Decision-making | Cross-retailer comparison, "good price?", side-by-side on the robot screen |
| Personalization | Approved memory follows you from phone to robot |
| Checkout | Basket verification and a one-tap or one-text checkout |
| Payments | **Agentic payments within a customer-set mandate**, with approval on your own phone |
| Loyalty and rewards | Points and "use points?" at checkout |
| Post-purchase | Order tracking, returns and exchanges, price-drop alerts |

**How we score against the Visa rubric:**

| Criterion (weight) | How we earn it |
|---|---|
| Innovation (25%) | One brain with two bodies; the route optimizer prices a recommendation by its detour cost; the robot shops for you remotely |
| User Experience (25%) | No restart between phone and robot; walk-up guest mode; texting as the universal interface |
| Technical Execution (20%) | Tool-calling agent, route optimizer, vision, robot control, permissions engine, API → local fallback |
| Commerce Impact (15%) | Trip time saved, suggestion acceptance, basket accuracy, staff picking time saved, budget adherence |
| Trust & Responsibility (15%) | Approved-only memory, spending mandates, approval for consequential actions, an audit log, privacy mode, synthetic data |

**Visa requirement checklist:**
- [ ] Generative AI is central (agent, explanations, comparisons, vision)
- [ ] Shows one or more journey stages (we show all seven)
- [ ] Trust story is on a slide *and* in the live demo
- [ ] Commerce-impact metrics are visible on screen
- [ ] Payment uses test cards only, and the mock is labeled as a mock

## 3.2 T-Mobile: Cart Whisperer (side track)

**Alignment decision:** our **demo store is a T-Mobile-style wireless store**, with phones on display, cases, screen protectors, chargers, earbuds, a watch and a home-internet gateway. The T-Mobile track is specifically about **phone shopping**, and the platform adapts to any store anyway.

**Direction picked: Cart Whisperer**, meaning checkout suggestions that actually help. We also include the brief's "bring the one that says wait" spirit.

| T-Mobile brief rubric (weight) | How we earn it |
|---|---|
| Customer value (30%) | At most 3 suggestions, which can be **zero**; explicit SKIP and WAIT cards |
| Use of signals (25%) | **Commerce** (cart, catalog, promos), **customer** (household lines, owned accessories, device age, installments left) and **real-time physical context**: where the robot is and what you just picked up |
| Explainability and trust (20%) | A visible "why" on every suggestion |
| Feasibility and demo (25%) | Judges can literally shop the store with the robot |

"Says wait" lines to script:
- "You already own a 20W USB-C charger (bought March), so skip it."
- "That case doesn't fit the Pixel you're buying. This one does."
- "Your phone has 4 installment months left. Upgrading today costs about $X more than waiting. I'd wait, and I'll text you then."
- "The earbuds put you $34 over budget. They're on sale Friday. Want a reminder?"

**T-Mobile requirement checklist:**
- [ ] One direction clearly named (Cart Whisperer)
- [ ] Phone-shopping store in the demo
- [ ] At least one SKIP or WAIT moment in the live demo
- [ ] Synthetic data only, stated on a slide

## 3.3 Photon: messaging agent (side track)

The requirements are to be **built on Photon 2.0** and **live on at least one Photon channel.**

Layer 1's text presence *is* the Photon agent:
- Edit your list or budget by text
- "I'm at the store" → a login code for the robot
- **Approvals:** "Robot found your order, $47.10. Reply YES to pay with Visa ••1111"
- **Remote delegation:** "Grab my usual" → the robot starts → photos and "ready for pickup"
- Receipts, price-drop alerts, the "sale starts Friday" reminder, and return status
- **Stretch:** a household group chat where anyone adds to the shared list and the robot reroutes live

Notes from Photon's docs:
- The SDK is `spectrum-ts` (Node or Bun), with cloud credentials `SPECTRUM_PROJECT_ID` / `SPECTRUM_PROJECT_SECRET`. There's also a REST API (Users → Spaces → Messages). The public beta includes $100 of free credit.
- **Use iMessage or Telegram, not US SMS.** US SMS needs A2P 10DLC registration, which won't clear in a weekend.
- Read the guidelines in a browser: https://somethingreat.notion.site/Hackathon-Guidelines-for-Photon-2-0-3f8e443f36bb81f9893fed0d08a4c01d

**Photon requirement checklist:**
- [ ] Built on Photon 2.0 (`spectrum-ts` or REST)
- [ ] Live on at least one channel (iMessage) during judging
- [ ] Demo shows real texts arriving on a real phone

## 3.4 SideShift: Best Startup Idea (side track)

- **Company:** a personal shopping agent (consumer, free) plus a robot that stores pay for (B2B).
- **Wedge:** Layer 1 ships first as software with no hardware. It builds the user base and the memory.
- **Store side:** the robot handles remote-order picking and in-store assistance. Revenue comes from robot-as-a-service per store plus a share of measured basket lift.
- **Moat:** the honest "says wait" recommender earns trust, and trust earns accepted suggestions.
- **Distribution:** a shopping robot is inherently filmable, and creators can publish shareable lists ("dorm starter kit," "new-phone essentials") that load into the agent in one tap, with an affiliate split. SideShift-style creator distribution fits here, but it's optional.

**SideShift checklist:**
- [ ] One slide each for business model, wedge and distribution
- [ ] A one-paragraph startup pitch in the Devpost entry

## 3.5 Trust and responsibility (applies everywhere)

- [ ] **Approved memory only:** every remembered fact is visible in a memory panel with delete
- [ ] **Spending mandates:** per-order cap, monthly budget, allowed categories, and an auto-approve threshold
- [ ] **Consequential actions need approval:** purchases above the threshold, returns, adding non-list items in remote mode, and changing the mandate
- [ ] **An audit log** of every agent and robot action, visible to the customer
- [ ] **Explanations come from reason codes.** The LLM phrases them and never invents them.
- [ ] **The camera faces shelves and the basket.** No face storage, and faces are blurred.
- [ ] **Privacy mode:** all AI runs on our local GPUs, and no frame leaves the building
- [ ] Synthetic data and test cards only

---

# PART 4 — SYSTEM ARCHITECTURE

```
  ┌──── Layer 1 surfaces ────┐                          ┌──── Layer 2 ─────────────┐
  │ Web app (Next.js)        │                          │ Robot onboard laptop     │
  │ Photon agent (iMessage)  │                          │ drivetrain · arm · cams  │
  └────────────┬─────────────┘                          │ kiosk screen · mic/spkr  │
               │                                        └────────────┬─────────────┘
               ▼                                                     ▼
  ┌──────────────────────── SHARED BRAIN (FastAPI on RTX 5090 #1) ───────────────────┐
  │ Agent (tool calling) · Memory service · Mandate/approval engine · Audit log      │
  │ Catalog + price compare · Route optimizer · Recommender ("says wait")            │
  │ Cart · Checkout/payment (mock Visa) · Orders · Returns · WebSocket event bus      │
  └───────┬───────────────────────┬────────────────────────────┬─────────────────────┘
          ▼                       ▼                            ▼
   LLM gateway (LiteLLM)    Vision + voice (5090/GB10)   Supabase (auth, users,
   API → GB10 → 5090        tags, VLM, ASR, TTS          memory, lists, orders)
```

**Bring our own Wi-Fi router.** All machines sit on one local network. Only Photon, Supabase and API models need the internet, and each has a fallback.

## 4.1 Shared data contracts (lock in the first 30 minutes)

```jsonc
// user + approved memory
{ "user_id":"u1", "name":"Sam", "budget":{"trip":200,"month":600},
  "memory":[{"id":"m1","fact":"owns 20W USB-C charger","approved":true,"source":"purchase"}],
  "household":{"lines":3}, "device":{"model":"PX6","age_months":34,"installments_left":4},
  "loyalty_points":1240 }

// mandate (permissions for remote/agent actions)
{ "user_id":"u1", "auto_approve_under":20, "order_cap":100,
  "categories_allowed":["accessories","grocery"], "requires_yes":["purchase","return","off_list_item"] }

// catalog.json (synthetic, multi-retailer)
{ "sku":"CASE-PX9-BLK", "name":"Pixel 9 case", "category":"case", "compatible_with":["PX9"],
  "offers":[{"retailer":"our_store","price":29.99,"node":"N4","slot":"N4-L2","tag":21},
            {"retailer":"RetailerB","price":24.99,"ship_days":3},
            {"retailer":"OpenBox","price":17.50,"condition":"like_new"}],
  "price_history":[34.99,29.99,29.99], "promo":{"pct":20,"starts":"2026-10-23"}, "weight_g":85 }

// store_map.json (the tape grid is the graph)
{ "nodes":[{"id":"N4","x":1.0,"y":2.0,"tag":4}], "edges":[["N3","N4",1.0]],
  "entrance":"N0", "checkout":"N9", "pickup":"N10" }

// session (shared between layers; this is "continue where you left off")
{ "session_id":"s1", "user_id":"u1", "mode":"online|in_store|remote",
  "list":["CASE-PX9-BLK","EARBUDS-X"], "cart":[], "robot_id":"r1" }

// robot commands / events
{"cmd":"goto","node":"N4"} {"cmd":"pick","slot":"N4-L2"} {"cmd":"say","text":"..."} {"cmd":"show","view":"compare"}
{"evt":"arrived","node":"N4"} {"evt":"basket","tags":[21,35]} {"evt":"user_input","text":"..."} {"evt":"error","msg":"..."}
```

## 4.2 Agent tools (one agent, used by web, text and robot)

`search_products` · `compare_prices(sku)` · `is_good_price(sku)` · `add_to_list` / `remove_from_list` · `check_budget` · `suggest_swaps` · `plan_route` · `get_recommendations` · `request_approval(action)` · `start_checkout` · `start_remote_order` · `start_return(order_id, sku)` · `remember(fact)` (needs user approval) · `explain(reason_codes)`

Each tool is the same function no matter which surface calls it. **That's how "continue where you left off" works.**

---

# PART 5 — HARDWARE PLAN AND MODEL FALLBACK

## 5.1 What runs where

| Machine | Role | Runs |
|---|---|---|
| **GB10 (DGX Spark, 128 GB unified memory)** | Local "big brain" and the fallback when API credits run out | Local agent LLM (gpt-oss-120b or Qwen3-30B-A3B) and vision model (Qwen3-VL-30B-A3B) via vLLM or Ollama, both OpenAI-compatible. **Prefer MoE models**, since the GB10's ~273 GB/s memory bandwidth makes big dense models slow. |
| **RTX 5090 PC #1 (32 GB)** | Shared brain server and real-time perception | FastAPI brain, LiteLLM gateway, faster-whisper or Parakeet (ASR), Kokoro (TTS), AprilTag detection, and a small fast LLM (gpt-oss-20b or Qwen3-8B) as the last resort |
| **RTX 5090 PC #2** | Frontend dev, backup serving, demo video recording, 2D store sim | |
| **Robot onboard laptop** + ESP32/Arduino | Thin client | Cameras, line following, motor serial, arm driver, kiosk screen. **No models on the robot.** |

**Gotchas:**
- [ ] The GB10 is ARM64, so use NGC containers or Ollama's ARM build.
- [ ] The RTX 5090 needs PyTorch built for CUDA 12.8+ (sm_120).
- [ ] Use weights already on disk. Don't pull big models over venue Wi-Fi.

## 5.2 API → local fallback

**Rule: no code calls a model provider directly.** Everything goes through LiteLLM role aliases.

| Alias | Primary (API) | Fallback 1 (GB10) | Fallback 2 (5090) |
|---|---|---|---|
| `agent` | API LLM | gpt-oss-120b / Qwen3-30B-A3B | gpt-oss-20b / Qwen3-8B |
| `vision` | API vision model | Qwen3-VL (local) | smaller local vision model |
| `explain` | API small model | local | **template strings** |
| `asr` / `tts` | always local | — | browser Web Speech API |

```yaml
model_list:
  - model_name: agent
    litellm_params: { model: anthropic/<api-model>, api_key: os.environ/ANTHROPIC_API_KEY, timeout: 8 }
  - model_name: agent-gb10
    litellm_params: { model: openai/<local-llm>, api_base: http://gb10.lan:8000/v1, api_key: "none" }
  - model_name: agent-5090
    litellm_params: { model: openai/<small-llm>, api_base: http://pc1.lan:8001/v1, api_key: "none" }
  - model_name: vision
    litellm_params: { model: <api-vlm>, api_key: os.environ/..., timeout: 10 }
  - model_name: vision-gb10
    litellm_params: { model: openai/<local-vlm>, api_base: http://gb10.lan:8002/v1, api_key: "none" }
litellm_settings:
  fallbacks: [{ "agent": ["agent-gb10", "agent-5090"] }, { "vision": ["vision-gb10"] }]
  num_retries: 1
router_settings:
  allowed_fails: 2      # out-of-credit (402/429) twice → bench the API model
  cooldown_time: 600    # for 10 minutes, so we stop paying the latency
```

**Fallback checklist:**
- [ ] Every model call goes through the gateway (grep the code for direct SDK calls)
- [ ] `FORCE_LOCAL=1` kill switch routes everything local, and it doubles as **privacy mode**
- [ ] LiteLLM spend tracking with a max budget per key
- [ ] Tool schemas are small and flat; vLLM tool-call parsing is enabled
- [ ] Every prompt is tested on the local model too
- [ ] **One full rehearsal runs with the API key removed**
- [ ] A "Cloud AI / On-prem AI" chip shows in the UI
- [ ] Route optimizer, recommender, mandates and checkout are plain code, so the robot still works with zero models

---

# PART 6 — CORE ALGORITHMS

## 6.1 Route optimizer (the in-store value proposition)
1. Compute all-pairs shortest paths on the store graph with Dijkstra.
2. **Required stops** are the list items. **Optional stops** are recommended items, each with a value score.
3. Solve a **prize-collecting TSP** with OR-Tools routing, from the entrance to checkout (or to pickup in remote mode). Each optional node gets `AddDisjunction([node], penalty = value × K)`, so a detour happens only when its value beats the extra walking.
4. Caps: **at most 2 detours** and **at most 15% extra distance**. **Remote mode turns detours off**, since nobody is there to ask.
5. Output the ordered stops, the detours with reasons, and "**X% shorter than shopping in list order.**"

## 6.2 Recommender with "says wait"
```
score = complement + preference_match + promo_value − budget_pressure − already_owned − incompatible×∞
```
- Below a threshold, the result is **no suggestion** ("Nothing worth a stop today"). Show this in the demo.
- Negative advice cards are **SKIP** (already owned or incompatible) and **WAIT** (promo soon, over budget, or an upgrade that's too early).
- The output is reason codes, and the LLM only phrases them.

## 6.3 Layer 1 price intelligence
- **Compare:** rank offers by total cost (price + shipping + wait time), with open-box as an option.
- **Good price?:** compare the current price to the synthetic price history and upcoming promos, and answer BUY NOW, WAIT or GOOD DEAL.
- **Budget optimizer:** suggest swaps (cheaper retailer, open-box, similar item) that keep you under budget.

## 6.4 Mandate and approval engine (trust core)
```
action → check mandate:
  outside allowed categories or above order_cap → BLOCK and tell the user
  in requires_yes, or amount > auto_approve_under → send approval request (Photon/robot screen) → wait for YES
  else → execute
every action → audit log (who, what, when, which surface, approval id)
```

## 6.5 Cart verification and returns
- The basket camera reads tags, and the result is reconciled against the list and cart. The robot reports "missing earbuds" or "extra item, add or remove?"
- **Returns (demo-light):** the agent starts a return from order history → at the robot, the camera reads the label and the scale checks weight against the catalog → a match gives an instant refund (mock) after approval, and a mismatch goes to human review. **Nothing is ever auto-denied.**

---

# PART 7 — TEAM SPLIT (4 owners)

Each person **owns a slice end to end** and makes the decisions for it. The interfaces between people are the contracts in Part 4. Pair-integration partners are listed for each person.

## 👤 A — Robot Lead (Layer 2 hardware and motion)
**Owns:** everything physical. That's the robot, mock store, navigation, arm and basket camera.
**Integrates with:** C (routes), B (vision/voice), D (kiosk screen)

**Setup**
- [ ] Hardware inventory posted to the team chat (first 15 min): chassis, motors, driver, MCU, arm model, webcams, laptop
- [ ] Go/no-go call on Tier 1 and Tier 2
- [ ] Floor space claimed; mock store laid out (tape grid, AprilTags at every node, 2 "aisles", checkout, pickup)
- [ ] About 12 product boxes labeled with tags (C provides the list)

**Motion**
- [ ] Motor control on the ESP32/Arduino over serial
- [ ] Line following between nodes
- [ ] Node detection with AprilTags, and turns at intersections
- [ ] `goto(node)` reliable 5/5 times on the demo route

**Robot client (`robot/`)**
- [ ] WebSocket client implementing `goto`, `pick`, `say`, `show`
- [ ] Emits `arrived`, `basket`, `user_input`, `error`
- [ ] Basket camera emits the tag list
- [ ] Kiosk screen mounted, with mic and speaker working

**Arm (Tier 2)**
- [ ] Arm mounted and powered
- [ ] Trajectories recorded for 1–2 shelf slots into the basket
- [ ] 4/5 success on the demo slot, otherwise the human-drop fallback is locked

**Cut line:** if the robot isn't driving reliably by 1am, stop adding features. Lock in the human-pushed robot with a live screen, and spend the remaining time on reliability.

## 👤 B — AI Brain and Infrastructure Lead
**Owns:** the shared agent, memory, models, fallback, voice and vision.
**Integrates with:** C (tools), D (web, Photon, kiosk), A (cameras)

**Infra**
- [ ] GB10 serving the local LLM and vision model (OpenAI-compatible) on the local network
- [ ] 5090 #1 serving the small LLM, ASR and TTS
- [ ] LiteLLM gateway with all aliases and fallbacks (Part 5.2)
- [ ] `FORCE_LOCAL` kill switch, plus the cloud/on-prem status endpoint for the UI chip
- [ ] Spend tracking turned on

**Agent (`agent/`)**
- [ ] Tool-calling agent wired to all of Part 4.2's tools (C implements them)
- [ ] Same agent session reachable from web, Photon and robot (session id)
- [ ] `remember(fact)` creates a *pending* memory that the user approves
- [ ] Reason-code → plain-language phrasing, with template fallback
- [ ] Prompts tested on the API model **and** the local model

**Voice and vision**
- [ ] Push-to-talk → ASR → agent → TTS in under 3 s on the local network
- [ ] Guest mode prompt (no account: find item, price check, store help)
- [ ] Shelf verification: "is this the right one?" with the vision model on a camera frame, using tags as ground truth
- [ ] Face blurring on any stored frame

**Cut line:** if the vision model is flaky, shelf verification becomes tag-only. Never let vision block checkout.

## 👤 C — Commerce Core Lead
**Owns:** data, shopping logic, the route, recommendations, mandates, payments, orders and returns.
**Integrates with:** B (tools), A (routes, tags), D (UI data)

**Data (first hour, which unblocks everyone)**
- [ ] `store_map.json` matching A's tape grid
- [ ] `catalog.json`: about 12 wireless-store items, 2–3 offers each, price history, promos, weights, compatibility
- [ ] 3 synthetic users (student single line, family of 3, upgrader with installments left) with approved memory
- [ ] Synthetic order history (for "already owned" and returns)
- [ ] Supabase tables with row-level security: users, memory, mandates, lists, sessions, carts, orders, audit_log

**Logic (`brain/`)**
- [ ] `/route` prize-collecting TSP with detour caps; remote mode has no detours
- [ ] `/recommend` scoring, SKIP and WAIT cards, and the "nothing today" case
- [ ] `/compare`, `/good-price`, `/swaps` (Layer 1 price intelligence)
- [ ] Mandate and approval engine plus the audit log
- [ ] Cart reconciliation from basket tags
- [ ] Checkout → mock Visa payment (test card) → receipt and loyalty points
- [ ] Remote order flow: create → robot picks → approval → ready at pickup
- [ ] Returns: start → label and weight check → refund (mock) or review
- [ ] `/metrics`: % route saved, suggestion acceptance, basket accuracy, budget adherence
- [ ] Unit tests for all 3 users on the demo path

**Cut line:** if OR-Tools fights you, brute force works for 12 items. Use the Visa sandbox only if the booth gives credentials by 8pm; otherwise ship a labeled mock.

## 👤 D — Product, Channels and Pitch Lead
**Owns:** every screen and text a human sees, the Photon integration and all submissions.
**Integrates with:** B (agent), C (data), A (kiosk hardware)

**Layer 1 web app (`web/`)**
- [ ] Sign up and log in (Supabase auth)
- [ ] List and budget editor
- [ ] Price comparison and "good price?" views
- [ ] **Memory panel** (approve/delete) and **mandate settings**
- [ ] Orders, returns and audit log views
- [ ] Chat with the agent

**Photon (`photon/`)**
- [ ] Photon beta access and an iMessage line working
- [ ] Text → agent → reply round trip
- [ ] Login code flow ("I'm at the store")
- [ ] Approval texts (YES/NO) wired to the mandate engine
- [ ] Remote order texts (started, photo, approve, ready)
- [ ] Receipts and alerts
- [ ] Stretch: household group chat

**Robot kiosk UI (`web/kiosk`)**
- [ ] Walk-up guest screen plus a login QR/code
- [ ] Logged-in view: 2D map with the route and robot dot, current stop, suggestion cards with "why", cart and budget bar, cloud/on-prem chip
- [ ] **Store sim mode** (simulated `arrived` events), our Tier 0 insurance

**Pitch and submissions**
- [ ] Demo script (Part 9) rehearsed 3+ times
- [ ] Slides: problem, two layers, demo, trust, impact, business
- [ ] Backup video recorded at feature freeze
- [ ] Devpost entry with a section per track (Part 10)

**Cut line:** the minimum for Photon is login code, approvals and receipt. The group chat is stretch.

---

# PART 8 — TIMELINE AND CHECKPOINTS

> It's Saturday afternoon. This assumes submissions close **around 10am Sunday**, so **shift every row to the real deadline.**

| Time | A Robot | B AI/Infra | C Commerce | D Product |
|---|---|---|---|---|
| **3:15–3:45pm** | Inventory, floor space | Check model servers | Draft data files | Repo, Supabase, Photon signup |
| 3:45–9:00pm | Store build, line following | Gateway, agent, voice | Data, route, recommender, mandates | Web app, kiosk UI, Photon round trip |
| 9:00pm–1:00am | Robot drives C's routes | Agent ↔ Photon ↔ robot | Checkout, remote order, returns | Approvals, sim mode, map |
| 1:00–5:00am | Arm, basket camera | Shelf verification, privacy mode | Metrics, tests | Polish, slides |
| 5:00am– | Reliability only | Local-only rehearsal | Bug fixes | Video, Devpost, rehearsals |

Sleep in shifts, two people at a time, **never A and B together.**

### ✅ Checkpoint 0 — 3:45pm (contracts)
- [ ] Hardware inventory posted; Tier 1/2 go/no-go made
- [ ] Data contracts (Part 4.1) committed
- [ ] Repo, Supabase and router all up

### ✅ Checkpoint 1 — 9:00pm (Tier 0 spine)
- [ ] Web app: log in → list → compare → budget
- [ ] Photon: text in → agent → reply
- [ ] Brain: `/route` and `/recommend` correct on all 3 users
- [ ] Sim mode: robot dot follows the route on the 2D map
- [ ] Robot drives node to node on a hardcoded path
- [ ] Gateway fallback demonstrated (API key removed → local answers)

### ✅ Checkpoint 2 — 1:00am (Tier 1, real robot end to end)
- [ ] "I'm at the store" → login code → robot loads the list
- [ ] Robot drives the optimized route with 1 detour plus a SKIP moment
- [ ] Basket verified → checkout → approval text → receipt
- [ ] Walk-up guest mode answers "where are the chargers?"

### ✅ Checkpoint 3 — 5:00am (feature freeze)
- [ ] Remote order: robot picks (arm or fallback) → approval → pickup text
- [ ] Return happy path works (demo-light)
- [ ] Full rehearsal with `FORCE_LOCAL=1`
- [ ] Backup video recorded

### ✅ Checkpoint 4 — submission
- [ ] All Part 10 checklists ticked
- [ ] 3 rehearsals timed under 3:00

---

# PART 9 — DEMO SCRIPT (3 minutes)

| # | Time | Beat | Shows |
|---|---|---|---|
| 1 | 0:00–0:20 | **Hook.** "Shopping online and shopping in a store are two different worlds. Your plan at home never makes it to the aisle." | Problem |
| 2 | 0:20–0:50 | **① Online (Layer 1).** Text: "need a case for my new Pixel 9 and earbuds, $120 max." The agent compares retailers, flags the earbuds as "on sale Friday," and builds the list. | Discovery, comparison, budget |
| 3 | 0:50–1:50 | **② In-store (Layer 2).** "I'm at the store" → code → the robot greets you with *your* list. It plans the route on screen ("31% shorter, 1 detour"), drives, detours to a screen protector ("fits your phone, 20% off"), then **"Skip the charger, you bought one in March."** The basket is verified and the approval text arrives. Reply YES, and the receipt comes with points. | Continuity, route, says wait, checkout, payments |
| 4 | 1:50–2:25 | **③ Remote.** A teammate texts "grab my usual." The robot drives off **alone**, picks the item (arm or fallback), and texts a photo plus "Approve $24.99?" → YES → "Ready at pickup." | Remote shopping, mandates, real labor value |
| 5 | 2:25–2:45 | **Trust.** Show the memory panel ("approve what I remember"), the mandate settings and the audit log. Flip privacy mode and the chip shows On-prem AI. "It works with zero API credits, and nothing leaves the store." | Trust and responsibility |
| 6 | 2:45–3:00 | **Close.** Metrics panel, then: "One brain, two bodies: your shopping agent online, in the store, or on your behalf." | Impact |

**Per-track cuts:**
- **T-Mobile:** expand beat 3 (SKIP, WAIT, the upgrade-timing card); say "Cart Whisperer."
- **Photon:** narrate beats 2–4 from the phone screen; let a judge text the number.
- **SideShift:** beats 1, 3, 4, then the business slide.
- **Optional bonus beat (if time):** return an item at the robot → label and weight check → instant refund text.

---

# PART 10 — SUBMISSION CHECKLISTS

**Devpost (D owns; everyone reviews)**
- [ ] Project name, tagline, and the two-layer diagram
- [ ] "What we built vs. vision" table (honest scoping impresses judges)
- [ ] Tech stack and the local-fallback explanation
- [ ] "Synthetic data only" statement
- [ ] Repo link, demo video link
- [ ] All eligible tracks selected (check the cap)

**Per-track sections in the write-up**
- [ ] **Visa:** stage coverage table, trust features, impact metrics
- [ ] **T-Mobile:** "Direction: Cart Whisperer," signals used, SKIP and WAIT examples
- [ ] **Photon:** "Built on Photon 2.0, live on iMessage," list of flows
- [ ] **SideShift:** business model, wedge, distribution paragraph

**Demo table**
- [ ] Mock store set up and tested 30 minutes before judging
- [ ] Robot charged, plus a spare battery
- [ ] Router on; all machines on the local network
- [ ] Phones charged with Photon threads open
- [ ] Backup video queued on a laptop

---

# PART 11 — RISK REGISTER

| Risk | Likelihood | Mitigation | Owner |
|---|---|---|---|
| Robot hardware missing or broken | Med | Store sim mode (Tier 0), human-pushed robot | A / D |
| Arm unreliable | High | Replayed trajectories, human-drop fallback, stretch only | A |
| Venue Wi-Fi dies | High | Own router, local models, `FORCE_LOCAL` | B |
| API credits run out | Med | LiteLLM fallback chain plus budget alerts | B |
| Local model is bad at tool calling | Med | Small flat schemas, test early, deterministic core | B / C |
| Photon setup delay | Med | Sign up now; REST API as a fallback to the SDK; minimum flows only | D |
| No Visa sandbox | High | Labeled mock with the same flow | C |
| Voice fails in the noise | High | Push-to-talk, transcript, text fallback | B |
| Scope creep | **Very high** | Part 2 scope table is law; cut lines per owner | All |
| Integration hell late at night | Med | Contracts at 3:45pm, pair integration from 9pm, freeze at 5am | All |

---

# PART 12 — REPO, TOOLS AND SOURCES

## 12.1 Repo layout
```
aisle/
  contracts/   store_map.json, catalog.json, users/*.json, orders.json, api.md   (C)
  brain/       FastAPI: route, recommend, compare, mandates, cart, checkout,
               remote orders, returns, audit, metrics, ws bus                    (C)
  agent/       tool-calling agent, prompts, reason-code templates, memory svc    (B)
  infra/       litellm_config.yaml, serve_gb10.sh, serve_5090.sh                 (B)
  perception/  ASR/TTS, tag detection, VLM shelf check, face blur                (B)
  robot/       robot client, line follow, arm, basket cam                        (A)
  firmware/    ESP32/Arduino motor + sensor code                                 (A)
  web/         Layer 1 web app, kiosk UI, sim mode, map                          (D)
  photon/      spectrum-ts integration                                           (D)
  pitch/       slides, video, Devpost text                                       (D)
```

## 12.2 Tools

| Area | Tool |
|---|---|
| Model gateway and fallback | LiteLLM proxy |
| Local serving | vLLM or Ollama; NGC containers on the GB10 |
| Local models | gpt-oss-120b / -20b, Qwen3-30B-A3B / Qwen3-8B, Qwen3-VL (or newer equivalents already on disk) |
| Voice | faster-whisper or Parakeet (ASR), Kokoro (TTS), Web Speech API fallback |
| Vision | `pupil-apriltags` or OpenCV ArUco, OpenCV, local/API vision model |
| Routing | Google OR-Tools (routing with disjunctions), NetworkX |
| Backend | Python FastAPI, WebSockets |
| Data and auth | Supabase (Postgres, Auth, Realtime, row-level security) |
| Frontend | Next.js + Tailwind, SVG/canvas store map |
| Messaging | Photon 2.0 (`spectrum-ts` / REST), iMessage |
| Payments | Mock Visa-shaped service with test cards; Visa Acceptance Agent Toolkit or Visa Direct sandbox only if credentials are available |
| Robot | ESP32/Arduino with a motor driver, USB webcams, LeRobot (if the arm is SO-100/101) |

## 12.3 Sources
- T-Mobile challenge brief (UW_Hackathon_Challenge_Brief.docx, provided)
- [Introducing Photon 2.0](https://photon.codes/blog/introducing-photon-2.0)
- [Photon spectrum-ts iMessage provider docs](https://photon.codes/docs/spectrum-ts/providers/imessage)
- [Photon hackathon guidelines (Notion, open in a browser)](https://somethingreat.notion.site/Hackathon-Guidelines-for-Photon-2-0-3f8e443f36bb81f9893fed0d08a4c01d)
- [Visa MCP Server and Acceptance Agent Toolkit](https://corporate.review.visa.com/en/sites/visa-perspectives/innovation/visa-mcp-server-agent-acceptance-toolkit.html)
- [Visa Acceptance Agent Toolkit quick start](https://developer.visaacceptance.com/docs/vas/en-us/agent-toolkit/quick-start/all/na/agent-toolkit/agent-toolkit-intro.html)
- [Visa Direct developer docs](https://developer.visa.com/docs/visa_direct)
- [NRF 2025 retail returns ($849.9B returned, 9% fraud)](https://nrf.com/media-center/press-releases/consumers-expected-to-return-nearly-850-billion-in-merchandise-in-2025)
- [DubHacks listing (GeekWire)](https://www.geekwire.com/calendar-event/dubhacks-hackathon/)
