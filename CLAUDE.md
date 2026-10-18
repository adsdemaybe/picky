# CLAUDE.md — Picky

Picky is a fleet of personal AI shopping-assistant **robots** for physical stores, built at **DubHacks 2026 (UW)** for
the **Visa "Reimagine Shopping"** track (side tracks: T-Mobile "Cart Whisperer: checkout suggestions that actually
help", Photon 2.0 messaging agent, SideShift "Best Startup Idea").
Shoppers save preferences, lists, budgets and favorites online and just say what they want in natural language
("tacos for 6, under $40, one vegetarian"). A **fleet manager** picks the store(s), turns the request into goals and
assigns them to robots; each robot is a small **rover carrying a vertical lift with an SO-101 arm** and a basket, run
by its own **managing LLM** that sees through its cameras, finds products, compares prices, surfaces deals, asks the
shopper, fills the cart within budget, and hands off to checkout. Store staff use the same system for shelf scans,
stock and restocking.

**One web app, nothing else:** there is no iPad/native app and no screen on the robot; every UI is the web app
(shopper, store-staff and fleet views), built by a teammate against the gateway's REST + WS APIs (`contracts/api.md`).
Online, the same assistant tracks spend, flags what's running low, compares prices and answers money questions; the
robots are its in-store body (one memory). Everything runs in sim: the Isaac Sim worker (multi-store, multi-rover) or
the always-available 2D robot fake. Stores: grocery `store_ave` + `store_hill`, a planned T-Mobile-style wireless store
`store_tmobile` (web-app demo data today; `data/` after v0.2), and a mock online store.

**Core value prop:** the robot plans a route that efficiently visits every list item **and** detours past recommended
items, stopping to ask the shopper if they want them; the fleet splits work across robots and stores by stock and price.

**This is an END-TO-END PIPELINE.** Every arm is a stage in one flow (§2). An arm that works alone but not in the
pipeline is not done.

This file is the base for every decision in this repo. If something isn't covered here, decide from the spec (§3),
then `docs/research.md`, then ask.

---

## 1. Judging lens (use it to break ties)

Visa rubric: **Innovation 25% · User Experience 25% · Technical Execution 20% · Commerce Impact 15% · Trust &
Responsibility 15%.** When choosing between two approaches, prefer the one that is (a) visible in the demo, (b) reliable
under pressure, (c) stronger on the rubric. Side tracks: Photon (must be built on Photon 2.0 and live on ≥1 Photon
channel), SideShift (startup story / distribution). We **are** entering T-Mobile with **Cart Whisperer** (+ the
brief's "bring the one that says wait"; recommender arm; design: `docs/cart-whisperer.md`): ≤3 explained cards per
phase, zero is valid, explicit SKIP and WAIT, using commerce, customer and real-time physical signals; deterministic
score + reason codes, the LLM only phrases; mandate-gated; demo store for T-Mobile judging = the T-Mobile-style
wireless store. ⚠️ Rubric per the team's copy of the brief: Customer value 30 · Use of signals 25 · Explainability &
trust 20 · Feasibility & demo 25. The backend needs contracts v0.3 (after v0.2). If the sponsor-track cap forces a
cut: Visa → T-Mobile → Photon → SideShift (team plan; cap unconfirmed).
**Open team decisions** (never decide them in code or docs): live demo store (`store_ave` vs `store_tmobile`), name
(Picky vs Aisle), sponsor-track cap, owner per arm, who builds the staff/fleet views, local-model fallback. Details and the
open technical questions: `docs/frontend-backend-map.md` §6.

## 2. The pipeline

```
 REQUEST (natural language, web app chat or text)
  user ─ web app / iMessage ─▶ gateway  POST /api/requests ─▶ fleet
 FLEET MANAGER (arms/fleet: all robots, all stores)
  agent.interpret_request: NL → draft (shop | fetch | shelf_scan | return_to_dock; list, budget, store/robot hints)
   → choose store(s) by catalog stock coverage + price (may split one list across stores; OOS everywhere → online)
   → Goal(s) → assign a robot IN THAT STORE (deterministic score: idle, battery, distance; contracts.fleet)
   → monitor → reassign on robot failure → goal completed / failed
 SHOP GOALS wrap a Trip (gateway trip flow; request-created trips auto-execute: no login step)
  fulfillment: shop with the robot | robot shops, I pick up (every extra still asks by web/Photon; ends at pickup)
  PLAN: agent: list (items, qty, substitutions, budget) → catalog: split sourcing (in-store vs online)
        → recommender: optional stops (interest + "why you'd like this") → routing: A* + OR-Tools orienteering
        → trust: mandate pre-check on the projected cart → recommender: Cart Whisperer planning pass (SKIP / WAIT)
 PER-ROBOT MANAGING LLM (arms/agent brain; video input from front / wrist / basket cameras, faces blurred)
  tools: locate items → plan route → navigate → LOOK (camera → vision) → scan shelf → pick (lift + SO-101)
         → place in basket → ask the user / request approval → update cart + progress → finish goal
  optional stop → ask the shopper (web / Photon) → yes/no → re-plan; scans + picks update stock (alerts)
  cart changes / item picked → Cart Whisperer mid-trip cards incl. SKIP / WAIT (accepted add → optional stop → re-plan)
  emits brain.step (thought / tool call / observation / message) so the web app shows what each robot is thinking
 ROBOT SKILLS IN SIM
  RobotService (multi-robot): Isaac worker on "vhagar" (many stores + rovers over /ws/sim, by robot_id)
  or the 2D fake (always available), chosen PER STORE; picking = scripted "magic grasp" for now
 PAY
  Cart Whisperer (T-Mobile): cart + full shopper context → ≤3 explained cards (complete / replenish / swap-save /
  qty / deal / remove / skip / wait; zero is valid) → accept (normal cart change + mandate; WAIT = reminder) /
  decline (feeds the profile)
  cart (basket + online items) → trust mandate engine (deterministic) → explicit approval → Visa sandbox
  (Stripe test fallback) → receipt
 AFTER
  messaging follow-up ("how was the salsa?") → recommender updates taste profile → trip 2 is visibly smarter
 STORE STAFF / MERCHANT
  stock levels, low/out alerts, restock, adjustments, movement history; cameras; dashboard: out-of-stocks,
  mispricings, shelf heatmap
```

`arms/gateway` is the composition root (REST + WS for the web app, the trip state machine, the event bus, the sim
link) and the **only** place that wires arms together. `arms/fleet` owns goals and robot assignment; each robot's
brain changes goals only through the fleet and trips only through the gateway. Everything user-visible is published
as a `contracts.events` event → websocket → web app (one producer per event type).

## 3. Spec — Things to Implement, by arm

The team's source of truth. Track progress on the arm's draft PR checklist (mirrored in `arms/<arm>/README.md`), not
here.

| Arm | Owns |
|---|---|
| **fleet** | Fleet manager for ALL robots across MULTIPLE stores. NL request → `agent.interpret_request` → choose store(s) by stock coverage + price (split when it helps) → Goals (shop / fetch / shelf_scan / return_to_dock) → assign a robot in that store with the deterministic score (idle, battery, distance) → run it on that robot's brain → monitor, reassign on failure, low battery → dock. REST: requests, robots, goals, robot messages, stop. Publishes request.* / goal.* / fleet.robot_updated. |
| **agent** | Natural-language understanding (`interpret_request`, intent expansion → items + quantities + substitutions, split sourcing, shopper chat with tools `query_inventory`, `plan_route`, `recommend`, `add_to_cart`, `request_approval`, `checkout`, reply interpretation, follow-ups) **and** the per-robot managing LLM ("robot brain"): given a goal it decides what to pick, where, in what order and handles exceptions via brain tools (locate, route, navigate, look, scan, pick, place, ask user, progress, finish); emits brain.step. Fake brain = deterministic scripted executor (same tool sequence, reproduces the demo story). Ask Picky's tools (spending summary, budget status, price check, search, find savings, can I afford) join shopper chat once the gateway exposes it (today a web-app `/api/chat` route, §7). |
| **catalog** | Planogram file (product → aisle/shelf/slot) is the single source of truth → generates sim scene AND DB seed. Mock ERP (~120 realistic SKUs across real categories, realistic US prices/pack sizes, made-up brands). Store API: search, location, stock/price/deals, near-expiry. Multi-store catalog (2 sim stores + mock online store) queried as one. **Categories API** (taxonomy in `data/categories.yaml`, counts per store) and **stock management API** (levels ok/low/out, alerts, restock, adjustments, movement history) fed by robot scans and picks; restocks/adjustments re-sync the sim shelves (`robot.sync_stock`). Planned after v0.2 (data + contracts v0.3 PRs): the T-Mobile-style wireless store `store_tmobile` (planogram, wireless SKUs, `compatible_with`), and for the web app's Find & compare: offers across fictional retailers, 13-week price history, a promo calendar and the price-only verdict (Good price / Wait / Typical / High), all synthetic. |
| **perception** | **Cameras API** (list, blurred JPEG snapshot, MJPEG stream for `<img>`, WS, observe → vision description) over robot front / wrist / basket cameras and store ceiling cameras. Scan → CV identify (barcode/label with VLM, or CLIP/SigLIP vs catalog) → scan reports that update stock. "Pick the exact item": produce freshness top 3. Basket verified by camera. **Face blurring on every frame before it is served, stored or sent to an LLM.** |
| **sim** | Isaac Sim worker on the GPU host "vhagar": MULTIPLE stores and MULTIPLE rovers in one process, scenes built from the planogram/DB, textured product props with readable labels, connects outbound to the gateway `/ws/sim`, multiplexed by `robot_id`; robot + ceiling cameras rendered and streamed on demand. |
| **robot** | The robot model: small rover + vertical lift (0.2–1.2 m) + SO-101 arm (reach ~0.35 m, shelf items 0.1–1.5 m), basket on the back. MULTI-ROBOT `RobotService`: waypoint follower, scripted "magic grasp" pick (attach, `contracts.common.slot_reachable`) + place in basket + unload at checkout, per-robot battery (`BATTERY_*`); routes each STORE to Isaac when the worker hosts it with a matching data hash, else to the 2D fake (all robots across stores, synthetic Pillow camera frames); on Isaac loss robots continue on the fake from their last state. |
| **routing** | Occupancy grid of the store (from planogram, inflated by the 0.3 m rover radius). A* on the grid; distance matrix between product approach poses. OR-Tools orienteering: list items = mandatory stops, recommendations = optional stops with skip penalty = predicted interest. Batched shoppers. |
| **recommender** | Profile store: preferences, dislikes, budget, purchase history, ratings. Product embeddings → similarity to profile → LLM reranks top few + writes "why you'd like this". Cap rec stops per trip. Near-expiry markdowns only to users likely to want them. Post-purchase feedback updates the profile. **Cart Whisperer (T-Mobile; design `docs/cart-whisperer.md`)**, replacing the old checkout batch: a server-side `ShopperContext` (commerce, customer incl. owned items / devices / installments left, and physical signals) → deterministic candidates (complete / replenish / swap-save / qty fix / deal match / remove-warn / skip / wait) → hard gates in code (stock, compatibility, allergens/dislikes, mandate + budget, not declined, max detour) → deterministic score + reason codes (below threshold → nothing) → the LLM only phrases the why → ≤3 cards per phase (planning, in-trip, checkout), zero is valid; accept/decline feeds the profile; a WAIT accept schedules a reminder text; both store types; metrics: accepted rate, $ saved, avoided spend, $ added, basket completeness. Also the user-aware "Skip it" verdict for Find & compare. |
| **trust** | Mandate engine (deterministic code, NOT the LLM): spending cap, allowed categories, "ask me above $X". Explicit approval before checkout. Visa sandbox checkout (Stripe test mode fallback). Recommendation transparency log (why each item was suggested, no hidden paid placement). The web app's Permissions page is the approved-memory + mandate + audit view (profile, mandate, ledger); pending-memory approval (`remember(fact)`) is P2 / v0.3. In pickup mode every off-list item asks; checkout never auto-approves. |
| **messaging** | Photon 2.0 agent: text your request before the trip; mid-trip suggestions + approval requests (incl. robot questions); post-purchase follow-up → updates taste profile; Cart Whisperer WAIT reminders on the promo date; "ready at pickup" in pickup mode. Must be live on ≥1 Photon channel. |
| **frontend** | THE web app (owned by a teammate; the only UI; Next.js on `arm/frontend`, PR #12). Built on demo data (`scripts/gen-data.mjs` from `data/*.yaml`) with a client-side trip replay that emits `contracts.events` names: Home (month spend, running low), Ask Picky, Find & compare, Spending, Robot order (both stores, SKIP/WAIT advice cards, both fulfillment modes), Live trip (map with detours, approvals, receipt), Live (camera sources: simulated / gateway / direct stream), Permissions. Next: wire it to the gateway REST + WS (GatewaySource). Still to build: store-staff views (stock, alerts, restock, cameras, merchant dashboard) and the fleet view (robots per store, goals, what each robot is thinking). P2: guest mode = the web app without a profile, opened from a store QR (find items, price checks, store help). |
| **gateway** | FastAPI backend in the middle: REST + WS for the web app (`contracts/api.md`), trip orchestrator (shop goals' plan + checkout), event bus, arm registry (real vs fake), sim link to the Isaac worker. |

**Later (not now):** the physical SO-101 robot on a real rover and VLA fine-tuning (LeRobot / LeIsaac) for real
grasping. Keep the rover + SO-101 model in `contracts.common` consistent so that work can drop in; don't build it now.
The team plan's physical-build items (tape-grid / AprilTag navigation, firmware under `arms/robot/firmware/`, any
robot-mounted screen, mic or speaker) come only with the physical robot; the web app's simulated camera is a view of the
sim, not a hardware claim. Also later / pitch-only: returns and exchanges; group chat shopping (P2, build at most one of
it and batched shoppers).

Demo safety (everyone): thin end-to-end loop working by hour 8–10; feature freeze ~6 h before judging; backup video
of a full trip; a second trip that is visibly smarter because of post-purchase feedback.

**Timeline (PT, DubHacks Oct 17–18 2026):** Sat ~12:30 PM hacking starts · **Sat ~9–10 PM thin E2E loop on `main`** ·
**Sun ~5:30 AM feature freeze** (fix PRs only) + record backup video · **Sun 8:00 AM code freeze**, rehearse ×3 ·
**Sun 10:00 AM submit** (hard deadline 10:30 AM) · Sun 11:30 AM–2:00 PM judging. Prioritize P0 (the demo trip) over
P1/P2 polish every time.

## 4. Repo layout & architecture rules

```
CLAUDE.md            this file (AI + human rules). Cursor mirror: .cursor/rules/*.mdc — generated, run `make rules`
contracts/           LOCKED interfaces: pydantic models, service Protocols, events, LLM tool schemas, DB schema, API list
utils/               central shared helpers (config, logging, db, llm client, money, geometry, ids, event bus, data loaders)
arms/<arm>/          one folder per feature arm (see §3): catalog, perception, sim, robot, routing, agent, recommender,
                     trust, messaging, fleet, gateway, frontend
  README.md            scope, contract, how to run, demo-ready checklist, known limitations
  __init__.py          public API: exports create() and create_fake()
  service.py           real implementation of the arm's contracts Protocol → create(services)
  fake.py              deterministic fake of the same Protocol → create_fake(services)
  tests/               pytest; must pass with base deps (use pytest.importorskip for extras)
data/products.yaml   product master (sku, name, price, tags, shape/label for sim assets, barcode, shelf life)
data/planograms/     one YAML per physical store: layout, fixtures, slots (sku → aisle/shelf/slot), stock lots
data/online_store.yaml   mock online store catalog
data/categories.yaml category taxonomy (Product.category / subcategory ids, display names)
data/fixtures/       demo users, profiles, mandates, purchase history (users.yaml); fleet robots per store + store
                     ceiling cameras (robots.yaml: rover-ave-1 Pico, rover-ave-2 Rhea, rover-hill-1 Hazel)
scripts/             repo tooling only (e.g. sync_agent_rules.py). Run arm entrypoints with `python -m arms.<arm>.<module>`
docs/                research.md, architecture.md, demo-script.md, cart-whisperer.md, frontend-backend-map.md, pitch/
                     (planned: slides, Devpost text); team-plan.md (team master plan) is on arm/frontend until PR #12 merges
```

Rules:
1. **An arm's code lives only in `arms/<arm>/`.** Arms import `contracts` and `utils` — never another arm.
   Cross-arm calls go through the `contracts` service Protocols and are wired by the gateway.
2. **Composition roots are the only exception:** `arms/gateway` (backend process) and `arms/sim/app.py` (Isaac Sim
   process) may import other arms, and only what that arm's `__init__.py` exports.
3. **`utils/` is feature-agnostic.** Something used by one arm stays in that arm. Something two+ arms need and that
   has no business logic moves to `utils/` via its own PR.
4. **`contracts/` is locked.** Change it only in a `contracts/<slug>` PR that bumps `CONTRACTS_VERSION` and names the
   affected arms. Never change contracts inside an arm PR.
5. **Planogram is the source of truth.** Sim scene, DB seed and occupancy grid are generated from
   `data/products.yaml` + `data/planograms/*` (+ `data/fixtures/robots.yaml` for robots and cameras). Never hand-edit
   a generated artifact; fix the data or the generator.
6. **Every arm ships a fake.** `arms/<arm>/fake.py` implements the same Protocol with deterministic data so the
   pipeline always runs. The gateway uses the real implementation only for arms listed in `PICKY_REAL_ARMS`
   (comma-separated, e.g. `PICKY_REAL_ARMS=catalog,routing`); every other arm runs its fake.
7. **Rules files stay in sync.** After editing `CLAUDE.md` or any `arms/<arm>/README.md`, run `make rules` and commit
   the regenerated `.cursor/rules/`. CI fails if they drift.

## 5. How to contribute

### Rules (mandatory)
- `main` is always demo-able. **Never commit or push directly to `main`.** Every change lands through a PR.
- **A new arm = one branch `arm/<arm>`** off `main`. The whole arm is built there and merged by one PR when it is
  demo-ready (§6).
- After an arm has merged, every further change gets its own short-lived branch + PR:
  `feat/<arm>-<slug>` (new capability) · `fix/<arm>-<slug>` · `contracts/<slug>` · `utils/<slug>` · `chore/<slug>`.
- **One owner per arm = the assignee on the arm's draft PR.** That PR is the claim and the progress checklist; the
  scope in `arms/<arm>/README.md` is its in-repo copy (keep both ticked the same). There are no per-arm issues; open
  an issue only for a bug or a new idea. Touch another arm's folder only through a PR its owner reviews.
- Checkboxes mean the same everywhere: `[x]` = works in the pipeline (`make demo`) with the arm's **real** impl;
  `[x] … (fake)` = works only in the fake so far; `[ ]` = not done.
- **Pull constantly.** 12 arms move at once; stale branches are how merges break. Sync with
  `git pull && git fetch && git merge origin/main` at the start of every session, before starting each new task,
  before every push, after any PR merges to `main`, and at least every ~30 min while working. Fix conflicts right away
  on your branch, then re-run `make check`.
- Commit messages: `<arm>: <imperative summary>` (e.g. `routing: add OR-Tools orienteering with skip penalties`).

### Step by step
1. **Set up once**
   ```bash
   gh repo clone adsdemaybe/picky && cd picky
   make setup                 # uv sync --locked: Python 3.12 env + dev tools (base deps only)
   uv sync --extra <arm>      # your arm's heavy deps (pyproject extras); `uv sync --all-extras` for everything
   cp .env.example .env       # add only the keys your arm needs; .env is git-ignored
   make check && make demo    # both must pass on a fresh clone (all arms fake)
   ```
   `make setup` / plain `uv sync` is an exact sync and **removes extras**: re-run `uv sync --extra <arm>` after it.
   Frontend owner: `cd arms/frontend && pnpm i`, then `make gen-types` (TS types from contracts) and `make web`.
2. **Claim your arm** on its **draft PR** (from `arm/<arm>`; its checklist is the scope, mirroring §3):
   `gh pr list --label arm:<arm>` → `gh pr edit <n> --add-assignee @me`. Tick boxes in the PR and in
   `arms/<arm>/README.md` as items land (checkbox meaning: Rules above).
3. **Check out the branch** (it already exists).
   ```bash
   git fetch && git switch arm/<arm> && git merge origin/main
   # new work after the arm has merged: git switch main && git pull && git switch -c feat/<arm>-<slug>
   ```
4. **Build inside `arms/<arm>/` only.** Implement `service.py` against your Protocol in `contracts/services.py`; keep
   `fake.py` working. Run your arm inside the full pipeline as you go: `PICKY_REAL_ARMS=<arm> make demo` (needs
   `uv sync --extra <arm>` + your keys; the arm README's "How to run" lists them). If your real `create()` can't run
   (extra or key missing), make it raise so the gateway boots the fake and `/health` says why. While your arm still
   degrades on purpose (e.g. robot with no sim worker): `PICKY_REAL_ARMS=<arm> make demo ARGS=--allow-errors`.
5. **Push early, stay current.**
   ```bash
   git push                                     # the draft PR picks it up: CI + visibility, NOT a request to merge
   git fetch && git merge origin/main           # often, and always after a contracts change
   ```
   Resolve conflicts on your branch, never on `main`.
6. **Get it demo-ready (§6).** Then run the pre-PR checks:
   ```bash
   make fmt                                     # auto-format + auto-fix (fixes a failing fmt-check / lint)
   uv run pytest arms/<arm>                     # your tests, fast
   make rules                                   # if you edited CLAUDE.md or any arm README
   make check                                   # lint, format, rules sync, all tests (what CI runs)
   PICKY_REAL_ARMS=<arm> make demo              # full trip with your arm real (CI runs it all-fake)
   ```
   Update `arms/<arm>/README.md`: what works, known limitations, how to run, fallback flag.
7. **Mark it ready:** tick the PR's scope + demo-ready checklists, add demo evidence (screenshot/GIF/log of the pipeline
   with your arm real), fallback flag and known limitations to the description, then `gh pr ready <n>`. New `feat/`/
   `fix/` branches open their own PR: `gh pr create --base main --fill --label arm:<arm>` (fill the template).
   **Arm PRs must not change `contracts/`.**
8. **Review.** One reviewer from a different arm: `gh pr checkout <n> && make check && PICKY_REAL_ARMS=<arm> make demo`,
   then approve or request changes. Author squash-merges: `gh pr merge --squash --delete-branch`.
9. **Next change** → back to step 3 with a `feat/`/`fix/` branch.

### Special cases
- **Contracts change:** separate `contracts/<slug>` PR. Bump `CONTRACTS_VERSION`, list the affected arms in the PR, update
  their fakes in the same PR so `main` stays green, and get a review from one affected arm owner. Announce in team chat.
- **Shared helper:** `utils/<slug>` PR; keep it feature-agnostic (§4 rule 3).
- **After feature freeze (~T-6 h):** only `fix/` PRs, still through a PR; any one teammate can approve.
- **Foundation PR #1** is merged with a **merge commit** (not squash), because the arm PRs are stacked on it and
  GitHub retargets them to `main` automatically when its branch is deleted.

### For AI assistants (Claude Code, Cursor)
Follow the same flow. Before writing code, check the branch (`git branch --show-current`); if it is `main`, switch to
the right `arm/`/`feat/`/`fix/` branch first. Never push to `main`, never force-push a branch you didn't create, and
never merge a PR or mark a draft ready unless the user asks. Open PRs with `gh pr create` and fill the template.

## 6. Definition of demo-ready (the PR bar)

"Somewhat production ready": it does **not** need to handle every real-world case, but it must hold up in the demo.
- Implements its `contracts` Protocol fully; no `NotImplementedError` on demo paths.
- Works in the pipeline: `make demo` completes a trip with this arm real (`PICKY_REAL_ARMS=<arm>`) and others
  real-or-fake.
- Handles the predictable failures without killing the trip (empty search, out of stock, timeouts, malformed LLM
  output, sim disconnect, payment decline): degrade, log, emit an `error` event.
- Can be switched back to its fake by env flag alone.
- Tests for its core logic pass; CI is green. No secrets committed; new env vars added to `.env.example`.
- README updated: what works, known limitations, how to run it.
Not required: every edge case, scale beyond the demo, perfect polish.

## 7. Engineering conventions

- **Python 3.12 for dev** (Isaac Sim 6.x runs Python 3.12; vhagar runs a preinstalled 6.0 build);
  `requires-python >= 3.11`, so avoid 3.12-only syntax (PEP 695 `type`/generics). `uv` for envs, `ruff` for
  lint/format, `pytest`, pydantic v2, `asyncio` services.
  Frontend: pnpm in `arms/frontend`; TS types are generated from contracts (`make gen-types`), never hand-written.
- Commands: `make setup` (uv sync --locked), `make check` (lint + format check + rules check + tests), `make fmt`,
  `make dev` (gateway API, host/port from `.env`, default :8000), `make demo` (full trip through the pipeline, fakes
  unless `PICKY_REAL_ARMS` is set; extra flags via `make demo ARGS=--allow-errors`), `make db-up` + `make seed` (DB
  from planograms; needs the catalog extra), `make rules` (regenerate Cursor rules). Heavy per-arm deps go in that
  arm's optional extra in `pyproject.toml` (`uv sync --extra routing`); base deps must be enough for contracts, utils,
  fakes and tests.
- **Money is integer cents** (`int`), never float. **Units:** meters, radians, store world frame defined by the
  planogram origin. Poses are `contracts` `Pose2D`.
- Config only via `utils.config.settings` (env/.env). Logging only via `utils.log.get_logger(__name__)`. Two tooling
  knobs are process-env only (not in Settings, not read from `.env`): `PICKY_LOG_LEVEL` and `PICKY_ID_SEED`. DB access
  only via `utils.db` (one shared pool).
  Secrets via `utils.secrets` / `make secret NAME=...` (OS keychain, team bundle `make secrets-pull`; `docs/secrets.md`);
  `settings` resolves env > vault > `.env`. Never commit keys or paste them in chat, PRs or issues.
  LLM calls go through `utils.llm` (one place for model ids, retries, JSON/tool helpers). Real LLM code is
  unit-tested with a mocked client (`client=` / `utils.llm.set_client`); the demo must run on fakes with no
  `ANTHROPIC_API_KEY`. One interim exception: the web app's Ask Picky route (`arms/frontend` `/api/chat`: Claude over
  deterministic tools, offline fallback, key in `arms/frontend/.env.local`) until the gateway exposes the agent; then
  it proxies.
- **Deterministic vs LLM:** money math, mandate checks, approval gating and payments are deterministic code. The LLM
  proposes; code decides. Nothing reaches checkout without the mandate engine and an explicit user approval.
- Async I/O at service boundaries; no blocking calls inside the gateway event loop (use `asyncio.to_thread`).

## 8. Trust & privacy rules (15% of the score; non-negotiable)

- Faces are blurred before any camera frame is stored, sent to an LLM, or streamed to the frontend.
- Every recommendation carries its reason and signals; no hidden paid placement (sponsored items must be labeled).
- Mandates are enforced in code; explicit approval before checkout; "ask me above $X" always asks.
- Cart Whisperer suggestions never bypass mandates (an accept is a normal cart change: mandate check, approval when
  needed) and always show their reason + signals ("why am I seeing this"); the raw shopper context stays server-side.
  SKIP/WAIT reasons come from deterministic reason codes; the LLM only phrases them.
- Picky remembers only what the shopper can see and delete (the web app's Permissions page).
- Never claim a "privacy mode" or "nothing leaves the store": model calls go to the Claude API. Claim only what is
  true: faces blurred before any frame is stored, streamed or sent to an LLM; derived signals only to the LLM.
- Test/sandbox payments only, tokenized credentials only; never store card numbers. Synthetic data only: demo users,
  stores, retailers, devices and prices are fictional.

## 9. Stack decisions

Defaults from `docs/research.md` (options, versions, setup steps, sources, ⚠️ unverified flags). Use the pick; switch
to the fallback when the time-box in §10 is hit. Changing a pick needs a one-line note in the arm README.

| Arm | Pick | Fallback |
|---|---|---|
| catalog | `data/` YAML → seed local Postgres (Docker `pgvector/pgvector:pg17`) via SQLAlchemy async + psycopg 3; ~60 hand-written SKUs first, Open Food Facts enrichment later | Supabase via the same `DATABASE_URL`; SQLite + numpy if Docker fails |
| perception | Cascade: zxing-cpp barcode → open_clip SigLIP 2 (`ViT-B-16-SigLIP2`) match vs our label textures → `claude-haiku-5-5` vision pick-from-candidates. Face blur: OpenCV YuNet (`opencv-python-headless>=4.10,<5`). Runs in the backend, not Isaac's Python | Ground-truth bypass (sim reports prim→SKU); CLIP ViT-B-32; MediaPipe faces |
| sim | Isaac Sim 6.x (vhagar runs a preinstalled 6.0 build; pip pick elsewhere `isaacsim[all,extscache]==6.2.0.0`; Python 3.12) headless worker on "vhagar": one process, several store scenes side by side + several rovers, generated from the planogram with `pxr`; cameras → JPEG over WS on demand (`set_streams`); connects **outbound** to the gateway `/ws/sim`, multiplexed by `robot_id` | **2D top-down sim (robot fake, all robots + Pillow camera frames) is the always-available fallback**; recorded Isaac video |
| robot | Rover (kinematic diff/holonomic base, radius 0.3 m) + prismatic lift + SO-101 arm (URDF from the SO-ARM100 repo); pure-pursuit follower; scripted "magic grasp" + FixedJoint attach → reparent into the basket. Avoid deprecated `wheeled_robots`/`lula` APIs in 6.x | Box rover + kinematic arm pose; physical SO-101 + LeRobot/LeIsaac VLA later |
| fleet | Pure-Python asyncio manager in the backend: store choice by stock coverage + price, `contracts.fleet.choose_robot` scoring, one background task per running goal, reassignment on failure | Single robot per store, first-free assignment |
| routing | Planogram → 0.05 m grid (inflated by robot radius) → skimage `MCP_Geometric` distance matrix between aisle approach poses → OR-Tools 9.15 (mandatory = no disjunction, recs = `AddDisjunction` penalty in the same integer cm units) PATH_CHEAPEST_ARC + GLS, 1–2 s limit; re-solve after each yes/no | Greedy insertion on a 2-opt tour; cached demo route |
| agent | NL + shopper chat: `claude-sonnet-5-5`, structured output into `RequestDraft` / `ShoppingList`, hand-written async tool loop (anthropic SDK via `utils.llm`), ≤8 iterations, prompt caching. Robot brain: one `claude-sonnet-5-5` tool loop per robot over the brain tools, `claude-haiku-5-5` for vision (`look`) | Deterministic scripted brain (the fake, same tool sequence); keyword NL rules |
| recommender | `BAAI/bge-small-en-v1.5` (384-dim, local) in pgvector; profile vector from likes/purchases; Sonnet rerank + "why"; 3–5 rec stops per trip; Cart Whisperer ≤3 per phase (zero allowed) incl. SKIP/WAIT: deterministic score + reason codes, Sonnet only phrases the why (template fallback) | numpy cosine scan |
| trust | Deterministic AP2-style mandates (intent/cart mandates, signed, reason codes, audit log); approval bound to the cart hash; **Stripe test mode on the critical path** behind a `VisaIntelligentCommerce` adapter (Visa sandbox access is unlikely this weekend) | Mock signed receipt |
| messaging | Photon 2.0 via `photonhq-api` 0.5.0 (Python), poll + idempotent send on the beta shared iMessage line; demo phones text the number first; YES/NO approvals | spectrum-ts sidecar; Telegram; web chat |
| frontend | The web app (teammate; shipped on demo data): Next.js 16 + React 19 + Tailwind 4 + shadcn/ui (radix) + Lucide + motion + zustand (own palette, no Recharts); hand-drawn SVG charts and store map (world meters); `@anthropic-ai/sdk` for Ask Picky; live view sources, always labeled: simulated cameras, gateway `/ws/frames`, direct stream (MJPEG / MP4 URL) | Recorded video as a direct-stream MP4; the simulated cameras |
| gateway | FastAPI single process, async; `/ws` per client; in-process event bus; explicit trip state machine; `PICKY_REAL_ARMS` picks real vs fake | — |

Hour-0 accounts (lead times, owners): see the checklist at the top of `docs/research.md`.

## 10. How Claude works in this repo

- **Pull first, pull often (§5).** Before reading or editing code, and again before each new task and each push:
  `git fetch && git status`, then `git pull` and `git merge origin/main` into the current branch. Never work on top of
  a stale branch; if the merge conflicts, resolve it (or ask) before doing anything else.
- Before coding: read the arm's `README.md` and its contract in `contracts/`. Check the branch
  (`git branch --show-current`); if on `main`, switch to `arm/<arm>` (or the right `feat/`/`fix/` branch) first.
- Stay inside the arm folder. Need a contract change → stop and propose a `contracts/` PR. Need a shared helper →
  `utils/` PR.
- Time-box blockers: if a dependency (GPU host, sandbox approval, SDK) blocks you for ~45 min, switch to the documented
  fallback and record it in the arm README.
- Scope is §3. Don't build things outside it without asking; do suggest cheap additions that clearly raise a rubric
  score.
- Before opening a PR: run the pre-PR checks in §5 step 6.
