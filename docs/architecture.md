# Picky architecture

How the pieces fit at runtime. The rules live in [`CLAUDE.md`](../CLAUDE.md) (§2 pipeline, §4 layout rules); the
interfaces live in [`contracts/`](../contracts). This page is the map. Event names below are illustrative; the exact
set is the discriminated union in `contracts/events.py`.

## 1. The pipeline

Every arm is one stage of one flow. `arms/gateway` runs it as a trip state machine and is the only place arms are
wired together.

```mermaid
flowchart LR
  subgraph PLAN["PLAN (remote, before the trip)"]
    U([Shopper]) -->|text / iMessage| MSG[messaging]
    U -->|web| FE[frontend]
    MSG --> GW
    FE --> GW
    GW[gateway<br/>trip FSM] --> AG[agent<br/>intent → ShoppingList]
    AG --> CAT[catalog<br/>search all stores + online<br/>split sourcing]
    CAT --> REC[recommender<br/>optional stops + reasons]
    REC --> RT[routing<br/>A* matrix + orienteering]
    RT --> TR1[trust<br/>mandate pre-check]
  end
  subgraph SHOP["SHOP (in store)"]
    TR1 --> RB[robot<br/>navigate · pick · place]
    RB <-->|SimLink| SIM[sim<br/>Isaac worker]
    RB --> PER[perception<br/>verify slot · freshness top-3<br/>verify basket · face blur]
    PER -->|scan reports| CAT
    RB -->|optional stop| ASK{ask shopper}
  end
  subgraph PAY
    ASK --> TR2[trust<br/>CartMandate → decision]
    TR2 -->|needs_approval| APR[ApprovalResponse<br/>bound to cart hash]
    APR --> PAYM[VisaIntelligentCommerce adapter<br/>→ Stripe test]
    PAYM --> RCPT[Receipt]
  end
  subgraph AFTER
    RCPT --> FUP["messaging follow-up<br/>how was the salsa?"]
    FUP --> REC2[recommender<br/>profile update]
    REC2 -.->|trip 2 is smarter| AG
  end
  CAT -.->|scan data| DASH[frontend merchant dashboard]
```

### Trip state machine (`contracts` `TripState`)

```
intent → list_ready → recs_ready → route_planned → awaiting_login → executing ⇄ awaiting_approval
       → checkout → paid → followup → done          (any state → failed)
```

### One trip, across arms and events

Each arrow into **Bus** is a `contracts.events` event (envelope: `trip_id`, `ts`, `seq`, `type`) that the gateway
publishes on its in-process bus and fans out to every websocket client (`/ws`).

```mermaid
sequenceDiagram
  autonumber
  actor Maya
  participant MSG as messaging (Photon)
  participant GW as gateway (FSM)
  participant AG as agent
  participant CAT as catalog
  participant REC as recommender
  participant RT as routing
  participant TR as trust
  participant RB as robot (SimLink / 2D fake)
  participant PER as perception
  participant Bus as event bus → /ws → frontend

  Maya->>MSG: "tacos for 6, under $40, one vegetarian"
  MSG->>GW: InboundMessage
  GW->>Bus: trip.created (state=intent)
  GW->>AG: expand intent
  AG->>CAT: query_inventory (store_ave + online)
  CAT-->>AG: in-store hits, 1 OOS → online
  AG-->>GW: ShoppingList (split sourcing)
  GW->>Bus: list.ready
  GW->>REC: recommend(user, list, store)
  REC-->>GW: ≤ PICKY_REC_MAX_STOPS recs (reason, signals, sponsored=false)
  GW->>Bus: recs.ready
  GW->>RT: plan_route(mandatory + optional stops)
  RT-->>GW: Route (+ list-only baseline for comparison)
  GW->>Bus: route.planned
  GW->>TR: pre-check projected cart vs IntentMandate
  Maya->>GW: logs in to robot (awaiting_login → executing)
  loop each stop
    GW->>RB: navigate(waypoints)
    RB->>Bus: robot.state (pose stream)
    RB->>PER: camera frame (face-blurred)
    PER-->>GW: slot verified / produce top-3 by freshness
    alt optional stop
      GW->>MSG: "Hot sauce? because ..." (awaiting_approval)
      Maya-->>MSG: YES / NO
    end
    GW->>RB: pick → place in basket
    PER-->>GW: basket verified
    GW->>Bus: cart.updated
  end
  GW->>TR: CartMandate (lines, total, sha256 cart hash)
  TR-->>GW: MandateDecision (approve | needs_approval | deny + reason codes)
  GW->>MSG: approval request bound to cart hash
  Maya-->>MSG: YES
  GW->>TR: checkout(ApprovalResponse)
  TR-->>GW: Receipt (provider=stripe_test)
  GW->>Bus: checkout.paid
  GW->>MSG: "how was the salsa?" (followup)
  Maya-->>MSG: "too mild"
  GW->>REC: feedback → profile update
  GW->>Bus: profile.updated (state=done)
```

## 2. Process topology

```mermaid
flowchart LR
  subgraph MAC["Mac (demo laptop)"]
    FE["frontend<br/>Next.js :3000"]
    GW["gateway process<br/>FastAPI :8000<br/>all arms in-process (real or fake)"]
    PG[("Postgres + pgvector<br/>docker compose :5432")]
    FE <-->|"HTTP + ws /ws"| GW
    GW <--> PG
  end
  subgraph GPU["GPU box (Linux, Brev / RunPod)"]
    SIMW["sim worker<br/>python -m arms.sim.app<br/>Isaac Sim 6.2 headless"]
  end
  subgraph CLOUD["Cloud APIs"]
    PH["Photon 2.0<br/>(iMessage line)"]
    AN["Anthropic API"]
    ST["Stripe test mode"]
    DG["Deepgram"]
  end
  SIMW -->|"outbound ws /ws/sim<br/>PICKY_SIM_TOKEN"| GW
  GW -->|"poll ~2 s + send"| PH
  GW --> AN
  GW --> ST
  FE --> DG
```

- **One backend process, one worker.** The event bus is in-process; never run uvicorn with `--workers > 1`.
  Blocking work (OR-Tools, CV, sync DB) goes through `asyncio.to_thread`.
- **The sim worker dials out** to `PICKY_GATEWAY_URL` + `/ws/sim` with `PICKY_SIM_TOKEN`, so the GPU box needs no
  inbound ports. It exchanges `contracts/sim.py` messages: commands with `cmd_id` → results, robot state, camera frames
  (JPEG base64), ground-truth instance info. The gateway exposes this connection to the robot arm as a `SimLink`.
- **Photon is polled**, not webhooked: no public URL needed for the Mac. The user texts the shared line first.
- If the GPU box is down, nothing waits on it: the robot fake (2D kinematic sim) drives the same Protocol.

## 3. Real vs fake

Every arm exports `create(services)` (real, `service.py`) and `create_fake(services)` (deterministic, `fake.py`,
base deps only). The gateway is the composition root:

1. Build an empty `contracts.services.Services` container.
2. For each arm: `real = arm in PICKY_REAL_ARMS` → `services.<field> = arm.create(services)` else
   `arm.create_fake(services)`. Default (blank) = all fakes, so `make demo` runs with no keys and no GPU.
3. Add the gateway-owned `ApprovalBroker`, `EventPublisher` and (robot only) `SimLink`.

Factories never call other services while constructing (only at call time), so construction order doesn't matter.
Arms never import each other; they reach each other only through `services.<field>`.

```bash
make demo                                    # all fakes
PICKY_REAL_ARMS=routing make demo            # real routing, everything else fake
PICKY_REAL_ARMS=catalog,routing,trust make demo
```

Switching back to a fake is an env change only, never a code change. That is the per-arm fallback during the demo.

## 4. Data flow

The planogram is the single source of truth. Nothing generated is hand-edited: fix the data or the generator.

```mermaid
flowchart LR
  P["data/products.yaml<br/>sku, price, tags, shelf life,<br/>shape, dims_m, label_color, EAN-13"]
  G["data/planograms/&lt;store_id&gt;.yaml<br/>bounds, entrance, checkout,<br/>fixtures, aisles, slots, lots"]
  O["data/online_store.yaml"]
  U["data/fixtures/users.yaml<br/>profile, mandate, history"]
  L["utils/data.py<br/>loaders → contracts models"]
  P --> L
  G --> L
  O --> L
  U --> L
  L -->|"make seed (arms.catalog.seed)"| DB[("Postgres + pgvector")]
  L -->|"arms.sim scene builder (pxr)"| USD["USD scene<br/>(GPU box)"]
  L -->|"routing: rasterize fixtures"| GRID["occupancy grid<br/>→ A* distance matrix"]
  L -->|"fakes read YAML directly"| FAKES["arm fakes"]
  L --> FE2["frontend store map<br/>(via gateway API)"]
  PER2["perception scan reports"] -->|"location / stock / freshness"| DB
```

Stores: `store_ave` (main demo store, ~14 m × 22 m, 5–6 aisles + produce + checkout), `store_hill` (smaller second
store), `online` (mock online store, no planogram). Coordinates: meters, world frame with x right, y up, origin at the
store's bottom-left, theta CCW from +x. Money: integer cents everywhere.
