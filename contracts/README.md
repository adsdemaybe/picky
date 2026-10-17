# contracts — the locked interfaces between arms

Every arm codes against this package and nothing else from other arms. One author per change, so the whole set stays
consistent. `CONTRACTS_VERSION` (in `contracts/__init__.py`) is currently **0.1.0**.

**Arm owners: start with [`OVERVIEW.md`](OVERVIEW.md)** — per arm: the Protocol you implement, the models you consume
and produce, the events, and the `services.<field>` you may call.

## What's here

| File | What | Main users |
|---|---|---|
| `common.py` | `Model` base (`extra="forbid"`, computed fields round-trip), `WireModel` (`extra="ignore"`, cross-machine), id aliases (`Sku`, `StoreId`, `TripId`, ...), `Cents`, `Pose2D`/`Point2`/`Point3`/`Rect2D`/`BBox`, `Timestamp` (UTC), `EMBED_DIM=384`, `ROBOT_RADIUS_M=0.6`, `ONLINE_STORE_ID`, `money()`, `angle_diff()` | everyone |
| `catalog.py` | `Product` (+ `SimAsset` hints, barcode), `Lot`, `Deal`/`DealTerms`, `ProductLocation`, `StoreOffer`, `StoreInfo` (+ online shipping), `online_shipping_cents()` (THE shipping rule), `SearchQuery`/`SearchHit`, `NearExpiryItem`, `ScanApplyResult`, `MerchantDashboard` (+ `OutOfStockItem`, `Mispricing`, `HeatmapCell`) | catalog, agent, recommender, frontend |
| `planogram.py` | Schemas of the data files: `Planogram` (`Fixture`, `Aisle`, `Slot`), `ProductsFile`, `OnlineCatalog`, `FixturesFile` (`DemoUser`). Unique-id + reference validators. **3D/sim frame rules** in the module docstring | catalog, sim, routing, data loaders |
| `profile.py` | `UserProfile`, `PurchaseRecord`, `Feedback`, `ProfileUpdate` (+ `ProfileChange`) | recommender, messaging, frontend |
| `shopping.py` | `TripState` + `TRIP_TRANSITIONS`/`can_transition`, `Intent`, `ListItem`, `ShoppingList`, `Recommendation`, `CartLine`, `Cart` (totals are computed fields) | agent, gateway, trust, frontend |
| `trip.py` | `Trip` aggregate (+ `stop_status`, `followup`; also importable as `contracts.shopping.Trip`) | gateway, agent, frontend |
| `routing.py` | `GridSpec` (the only grid wire type), `Stop` (+ `detour_m`/`detour_s`, `qty`, `item_ids`), `StopStatus`, `RouteRequest`, `RouteLeg`, `Route` | routing, robot, frontend |
| `sim.py` | Gateway ⇄ Isaac worker wire protocol (`SimCommand`, `SimMessage`, `GatewayToSim` unions; handshake, busy/cancel and reconnect rules in the docstring), `SimErrorCode` + `RobotError`, `RobotState`, `CameraFrame` (+ ground-truth `instances`), `ItemInstance`, `planogram_hash`, `SimLink` Protocol | sim, robot, gateway |
| `perception.py` | `Detection`, `ScanReport`, `FreshnessCandidate`, `PickChoice` (≤ `PICK_CHOICE_MAX`=3), `BasketCheck` (+ `BasketCheck.compare`) | perception, catalog, gateway |
| `trust.py` | `IntentMandate`, `CartMandate`, `compute_cart_hash(cart, trip_id)`, `MandateDecision` (+ `MandateVerdict`, `MandateReason`, `MandateViolation`), `ApprovalKind`/`ApprovalRequest`/`ApprovalResponse`, `CheckoutRequest`, `Receipt`, `TransparencyEntry`, `AuditEntry`, `TrustLedger` | trust, gateway, messaging, frontend |
| `messaging.py` | `InboundMessage`, `OutboundMessage` (+ `about_skus`), `SendResult`, `ReplyInterpretation`, `normalize_handle()` | messaging, agent |
| `events.py` | `Event` union on `type` (26 types), `EVENT_PRODUCERS`, `LIVE_ONLY_EVENT_TYPES`, `make_event`, `parse_event`, `with_seq`, `event_json_schema()` | gateway, frontend, agent |
| `tools.py` | LLM tool inputs, `TOOL_DEFINITIONS` (Anthropic format) + `tool_definitions(strict=True)`, `parse_tool_input`, `AgentReply`, `ToolCallRecord` | agent |
| `services.py` | One `runtime_checkable` Protocol per arm, gateway-owned `TripController`/`ApprovalBroker`/`EventPublisher`, the `Services` DI container, `ARM_NAMES`, `SERVICE_PROTOCOLS`, `GATEWAY_PROTOCOLS` | everyone |
| `api.py` / `api.md` | Gateway REST + WS request/response models (`CheckoutStatus`, `AddLineResult`, `VoiceTurnResponse`, ...), the endpoint table and the event producer table | gateway, frontend |
| `db/schema.sql` | Postgres 17 + pgvector DDL mirroring the models (money = integer cents, `vector(384)`) | catalog, recommender, trust, gateway |

## Rules of use

- **Import from `contracts`, never from another arm.** Reach other arms through `services.<field>` at call time.
- **Fakes return real contract models** with deterministic data, so the pipeline always runs.
- **Money is `int` cents; geometry is meters/radians in the store world frame** (x right, y up, origin bottom-left,
  theta CCW from +x). Fixture `rect` is axis-aligned; `yaw` is the direction the pick face points.
- **The gateway owns the Trip.** Arms never mutate a `Trip` they're handed; trip changes go through `services.trips`
  (`TripController`), questions to the shopper through `services.approvals`.
- **Events:** the gateway publishes everything except `agent.message` (agent) and `error` (any arm). Build with
  `make_event("route.planned", trip_id=..., route=...)`; the publisher sets the bus-wide `seq`. `frame` events reject
  unblurred frames and travel only on `/ws/frames`.
- **Checkout:** `compute_cart_hash(cart, trip_id)` is what the user approves; `TrustService.checkout` must refuse if
  the approval's `cart_hash` differs. The LLM `checkout` tool calls `services.trips.start_checkout` (same path as
  `POST /api/trips/{id}/checkout`).
- **Robot failures** raise `contracts.sim.RobotError` with a `SimErrorCode`; branch on `.code`, never on strings.
- **Data files** are loaded with `utils.data` (`load_products`, `load_planogram`, `list_store_ids`,
  `load_online_catalog`, `load_demo_fixtures`). Check the data dir with `uv run python -m utils.data` (robot
  clearance `ROBOT_RADIUS_M`, approach headings, shelf heights, EAN-13 check digits, mandate expiry, ...).
- TS types for the frontend are generated (`make gen-types`), never hand-written: OpenAPI for REST,
  `event_json_schema()` (`GET /api/_schema/events`) for websocket events.

## How to change a contract (versioning rule)

`contracts/` is locked (CLAUDE.md §4 rule 4, §5 "Contracts change"). To change it:

1. Branch `contracts/<slug>` off `main` — never change contracts inside an arm PR.
2. Make the change **and bump `CONTRACTS_VERSION`** in `contracts/__init__.py` (and the version in `api.md`):
   patch = additive optional field/docs, minor = new model/endpoint/event or new required field, major = rename/removal.
3. Update every affected arm's `fake.py` in the same PR so `main` stays green, plus `db/schema.sql` / `api.md` /
   `OVERVIEW.md` if the shape they mirror changed. Sim wire: patch changes are safe (`WireModel` ignores unknown
   fields); a minor/major bump makes the gateway refuse an old Isaac worker (`SimHello.contracts_version`), so
   redeploy the worker on the GPU box too.
4. `make check` (tests in `contracts/tests/` must pass), list the affected arms in the PR description, get a review
   from one affected arm owner, and announce it in team chat. Arm branches then `git merge origin/main`.
