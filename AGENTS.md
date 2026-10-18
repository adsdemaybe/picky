# AGENTS.md — Picky

Picky is a personal AI shopping-assistant **robot** for physical stores, built at **DubHacks 2026 (UW)** for the
**Visa "Reimagine Shopping"** track (side tracks: Photon 2.0 messaging agent, SideShift "Best Startup Idea"). Shoppers save preferences, lists, budgets and favorites online, log in to the robot in-store, and
continue where they left off. The robot (4-wheel base + arm + basket) uses camera, voice and arm to find products,
compare prices, surface deals, manage the cart, stay within budget, and check out.

**Core value prop:** the robot plans a route that efficiently visits every list item **and** detours past recommended
items, stopping to ask the shopper if they want them.

**This is an END-TO-END PIPELINE.** Every arm is a stage in one flow (§2). An arm that works alone but not in the
pipeline is not done.

This file is the base for every decision in this repo. If something isn't covered here, decide from the spec (§3),
then `docs/research.md`, then ask.

---

## 1. Judging lens (use it to break ties)

Visa rubric: **Innovation 25% · User Experience 25% · Technical Execution 20% · Commerce Impact 15% · Trust &
Responsibility 15%.** When choosing between two approaches, prefer the one that is (a) visible in the demo, (b) reliable
under pressure, (c) stronger on the rubric. Side tracks: Photon (must be built on Photon 2.0 and live on ≥1 Photon
channel), SideShift (startup story / distribution). We are **not** entering the T-Mobile track.

## 2. The pipeline

```
 PLAN (remote, before the trip)
  shopper ─ text / web / iMessage ─▶ messaging | frontend ─▶ gateway ─▶ agent
     agent: intent expansion ("tacos for 6, <$40, 1 veg") → ShoppingList (items, qty, substitutions, budget)
          → catalog: search all stores + online → split sourcing (in-store → robot, OOS/online-only → online order)
          → recommender: optional stops (interest score + "why you'd like this"), capped per trip
          → routing: A* distance matrix + OR-Tools orienteering → Route (mandatory + optional detours)
          → trust: mandate pre-check on projected cart
 SHOP (in store, robot)
  shopper logs in to robot → gateway resumes the session → for each stop on the Route:
     robot navigate (waypoints) → perception scan/verify slot → [produce: freshness top-3 → shopper picks]
     → robot pick → place in basket → perception verifies basket → cart updated
     optional stop → agent asks shopper (voice / Photon / web): approve or skip
     scans continuously update catalog (location, stock, freshness)
 PAY
  cart (basket + online items) → trust mandate engine (deterministic) → explicit approval → Visa sandbox
  (Stripe test fallback) → receipt
 AFTER
  messaging follow-up ("how was the salsa?") → recommender updates taste profile → trip 2 is visibly smarter
 MERCHANT
  scan data → dashboard: out-of-stocks, mispricings, shelf heatmap
```

`arms/gateway` runs this as a trip state machine and is the **only** place that wires arms together. Everything
user-visible is published as a `contracts.events` event → websocket → frontend.

## 3. Spec — Things to Implement, by arm

The team's source of truth. Track progress on the arm's draft PR checklist (mirrored in `arms/<arm>/README.md`), not
here.

| Arm | Owns |
|---|---|
| **catalog** | Planogram file (product → aisle/shelf/slot) is the single source of truth → generates sim scene AND DB seed. Mock ERP in Postgres/Supabase (SKU, name, price, tags, expiry). Store API: search products, get location, stock/price/deals, near-expiry items. Multi-store catalog: 2–3 sim stores + mock online store, queried as one catalog. Applies scan updates (location, stock, freshness). |
| **perception** | Robot scanning pass → CV identify object (barcode/label read with VLM, or CLIP/SigLIP embedding match vs catalog) → scan reports that update the DB. "Pick the exact item": rank produce by freshness, show top 3. Basket contents verified by camera at drop. **Face blurring on every camera frame.** |
| **sim** | Isaac Sim environment with objects (built from the planogram/DB). Real-to-sim (CleanRoom3D scan only if it runs in hours, else hand-built USD aisles/shelves). Product assets: textured boxes/cylinders with readable labels. Basket on robot with fixed drop pose. Sim bridge (commands in, state + camera frames out). Live sim view source. |
| **robot** | Existing Isaac Sim mobile manipulator (e.g. Franka on mobile base). Waypoint follower. Picking with ground-truth poses + RMPflow/Lula, attach-on-contact fallback. Place in basket. |
| **routing** | Occupancy grid of the store (from planogram). A* on the grid; distance matrix between all product approach poses. OR-Tools orienteering: list items = mandatory stops, recommendations = optional stops with skip penalty = predicted interest. Batched shoppers: multiple users' lists in one trip. |
| **agent** | LLM brain with tools: `query_inventory`, `plan_route`, `recommend`, `add_to_cart`, `request_approval`, `checkout`. Intent expansion → items + quantities + substitutions. Split sourcing (in-store vs online, one combined checkout). Voice interface in store, text remote. |
| **recommender** | Profile store: preferences, dislikes, budget, purchase history, ratings. Product embeddings → similarity to profile → LLM reranks top few + writes "why you'd like this". Cap rec stops per trip; batch the rest into "want any of these?" at checkout. Near-expiry markdowns only to users likely to want them. Post-purchase feedback updates the profile. |
| **trust** | Mandate engine (deterministic code, NOT the LLM): spending cap, allowed categories, "ask me above $X". Explicit approval before checkout. Visa sandbox checkout (Stripe test mode fallback). Recommendation transparency log (why each item was suggested, no hidden paid placement). |
| **messaging** | Photon 2.0 agent: text your list/intent before the trip; mid-trip suggestions + approval requests; post-purchase follow-up → updates taste profile. Must be live on ≥1 Photon channel. |
| **frontend** | Live sim view. Store map with route + recommendation detours drawn. Cart, budget bar, chat, approvals. Merchant dashboard: out-of-stocks, mispricings, shelf heatmap from scan data. |
| **gateway** | FastAPI backend in the middle. Websockets to frontend. Trip orchestrator (the pipeline state machine). Arm registry (real vs fake). Talks to Isaac Sim through the sim bridge. |

Demo safety (everyone): thin end-to-end loop working by hour 8–10; feature freeze ~6 h before judging; backup video
of a full trip; a second trip that is visibly smarter because of post-purchase feedback.

**Timeline (PT, DubHacks Oct 17–18 2026):** Sat ~12:30 PM hacking starts · **Sat ~9–10 PM thin E2E loop on `main`** ·
**Sun ~5:30 AM feature freeze** (fix PRs only) + record backup video · **Sun 8:00 AM code freeze**, rehearse ×3 ·
**Sun 10:00 AM submit** (hard deadline 10:30 AM) · Sun 11:30 AM–2:00 PM judging. Prioritize P0 (the demo trip) over
P1/P2 polish every time.

## 4. Repo layout & architecture rules

```
AGENTS.md            this file (AI + human rules). Cursor mirror: .cursor/rules/*.mdc — generated, run `make rules`
contracts/           LOCKED interfaces: pydantic models, service Protocols, events, LLM tool schemas, DB schema, API list
utils/               central shared helpers (config, logging, db, llm client, money, geometry, ids, event bus, data loaders)
arms/<arm>/          one folder per feature arm (see §3)
  README.md            scope, contract, how to run, demo-ready checklist, known limitations
  __init__.py          public API: exports create() and create_fake()
  service.py           real implementation of the arm's contracts Protocol → create(services)
  fake.py              deterministic fake of the same Protocol → create_fake(services)
  tests/               pytest; must pass with base deps (use pytest.importorskip for extras)
data/products.yaml   product master (sku, name, price, tags, shape/label for sim assets, barcode, shelf life)
data/planograms/     one YAML per physical store: layout, fixtures, slots (sku → aisle/shelf/slot), stock lots
data/online_store.yaml   mock online store catalog
data/fixtures/       demo users, profiles, mandates, purchase history
scripts/             repo tooling only (e.g. sync_agent_rules.py). Run arm entrypoints with `python -m arms.<arm>.<module>`
docs/                research.md, architecture notes, demo script
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
   `data/products.yaml` + `data/planograms/*`. Never hand-edit a generated artifact; fix the data or the generator.
6. **Every arm ships a fake.** `arms/<arm>/fake.py` implements the same Protocol with deterministic data so the
   pipeline always runs. The gateway uses the real implementation only for arms listed in `PICKY_REAL_ARMS`
   (comma-separated, e.g. `PICKY_REAL_ARMS=catalog,routing`); every other arm runs its fake.
7. **Rules files stay in sync.** After editing `AGENTS.md` or any `arms/<arm>/README.md`, run `make rules` and commit
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
- **Pull constantly.** 11 arms move at once; stale branches are how merges break. Sync with
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
   make rules                                   # if you edited AGENTS.md or any arm README
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

### For AI assistants (Codex, Cursor)
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

- **Python 3.12 for dev** (Isaac Sim 6.2 runs Python 3.12); `requires-python >= 3.11`, so avoid 3.12-only syntax (PEP 695
  `type`/generics). `uv` for envs, `ruff` for lint/format, `pytest`, pydantic v2, `asyncio` services.
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
  LLM calls go through `utils.llm` (one place for model ids, retries, JSON/tool helpers).
- **Deterministic vs LLM:** money math, mandate checks, approval gating and payments are deterministic code. The LLM
  proposes; code decides. Nothing reaches checkout without the mandate engine and an explicit user approval.
- Async I/O at service boundaries; no blocking calls inside the gateway event loop (use `asyncio.to_thread`).

## 8. Trust & privacy rules (15% of the score; non-negotiable)

- Faces are blurred before any camera frame is stored, sent to an LLM, or streamed to the frontend.
- Every recommendation carries its reason and signals; no hidden paid placement (sponsored items must be labeled).
- Mandates are enforced in code; explicit approval before checkout; "ask me above $X" always asks.
- Test/sandbox payments only, tokenized credentials only; never store card numbers. Demo users are fictional.

## 9. Stack decisions

Defaults from `docs/research.md` (options, versions, setup steps, sources, ⚠️ unverified flags). Use the pick; switch
to the fallback when the time-box in §10 is hit. Changing a pick needs a one-line note in the arm README.

| Arm | Pick | Fallback |
|---|---|---|
| catalog | `data/` YAML → seed local Postgres (Docker `pgvector/pgvector:pg17`) via SQLAlchemy async + psycopg 3; ~60 hand-written SKUs first, Open Food Facts enrichment later | Supabase via the same `DATABASE_URL`; SQLite + numpy if Docker fails |
| perception | Cascade: zxing-cpp barcode → open_clip SigLIP 2 (`ViT-B-16-SigLIP2`) match vs our label textures → `Codex-haiku-5-5` vision pick-from-candidates. Face blur: OpenCV YuNet (`opencv-python-headless>=4.10,<5`). Runs in the backend, not Isaac's Python | Ground-truth bypass (sim reports prim→SKU); CLIP ViT-B-32; MediaPipe faces |
| sim | Isaac Sim 6.2 (pip `isaacsim[all,extscache]==6.2.0.0`, Python 3.12) headless worker; scene generated from the planogram with `pxr`; camera → JPEG over WS. Worker connects **outbound** to the gateway | **2D top-down sim (robot fake) is built first and is the demo fallback**; recorded Isaac video |
| robot | Ridgeback-Franka; pure-pursuit on a kinematic base; scripted "magic grasp" + FixedJoint attach → reparent into basket. Avoid deprecated `wheeled_robots`/`lula` APIs in 6.x | Franka on a box base; cuMotion RMPflow pick-place as stretch |
| routing | Planogram → 0.05 m grid (inflated by robot radius) → skimage `MCP_Geometric` distance matrix between aisle approach poses → OR-Tools 9.15 (mandatory = no disjunction, recs = `AddDisjunction` penalty in the same integer cm units) PATH_CHEAPEST_ARC + GLS, 1–2 s limit; re-solve after each yes/no | Greedy insertion on a 2-opt tour; cached demo route |
| agent | `Codex-sonnet-5-5` brain, `Codex-haiku-5-5` for voice/vision/classification, hand-written async tool loop (anthropic SDK), ≤8 iterations, prompt caching; intent expansion via structured output into `ShoppingList`. Voice: Deepgram STT/TTS, push-to-talk | Browser Web Speech API; env-var model fallback |
| recommender | `BAAI/bge-small-en-v1.5` (384-dim, local) in pgvector; profile vector from likes/purchases; Sonnet rerank + "why"; 3–5 rec stops per trip, rest batched at checkout (max 3) | numpy cosine scan |
| trust | Deterministic AP2-style mandates (intent/cart mandates, signed, reason codes, audit log); approval bound to the cart hash; **Stripe test mode on the critical path** behind a `VisaIntelligentCommerce` adapter (Visa sandbox access is unlikely this weekend) | Mock signed receipt |
| messaging | Photon 2.0 via `photonhq-api` 0.5.0 (Python), poll + idempotent send on the beta shared iMessage line; demo phones text the number first; YES/NO approvals | spectrum-ts sidecar; Telegram; web chat |
| frontend | Next.js 16 + React 19 + Tailwind 4 + shadcn/ui + zustand + Recharts; hand-written SVG store map (world meters); live view = JPEG frames over WS | Vite + React; recorded video loop |
| gateway | FastAPI single process, async; `/ws` per client; in-process event bus; explicit trip state machine; `PICKY_REAL_ARMS` picks real vs fake | — |

Hour-0 accounts (lead times, owners): see the checklist at the top of `docs/research.md`.

## 10. How Codex works in this repo

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
