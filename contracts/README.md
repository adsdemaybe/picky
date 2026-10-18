# contracts — the locked interfaces between arms

Every arm codes against this package and nothing else from other arms. One author per change, so the whole set stays
consistent. `CONTRACTS_VERSION` (in `contracts/__init__.py`) is currently **0.2.0**.

**v0.2.0 (fleet architecture):** natural-language requests → fleet manager (`fleet.py`, `FleetService`) → goals
assigned to robots across multiple stores (`choose_robot`) → per-robot managing LLM (`RobotBrainService`, brain tools
in `tools.py`) → multi-robot `RobotService` over a multiplexed sim wire (`sim.py`, `robot_id` everywhere). New:
cameras API (`cameras.py`, `CameraService`), categories + stock management (`catalog.py`), robots/categories data
files (`planogram.py`), SO-101 rover constants (`common.py`, `ROBOT_RADIUS_M` 0.6 → 0.3). Everything user-facing is
one web app. Review round (same version): event envelope `user_id` / `request_id` + `event_matches`, auto-executing
request trips, `goal_items_from_trip`, `split_budget`, `add_item` / result models / strict budgets for brain tools,
lenient sim parsing + `SimHosted` / `UnloadBasketCmd` / `SetSlotStockCmd`, `slot_reachable`, battery model. What each
arm must build: [`CHANGES-0.2.md`](CHANGES-0.2.md).

**Arm owners: start with [`OVERVIEW.md`](OVERVIEW.md)** — per arm: the Protocol you implement, the models you consume
and produce, the events, and the `services.<field>` you may call.

## What's here

| File | What | Main users |
|---|---|---|
| `common.py` | `Model` base (`extra="forbid"`, computed fields round-trip), `WireModel` (`extra="ignore"`, cross-machine), id aliases (`Sku`, `StoreId`, `TripId`, `RobotId`, `GoalId`, `CameraId`, `CategoryId`, ...), `Cents`, `Pose2D`/`Point2`/`Point3`/`Rect2D`/`BBox`, `Timestamp` (UTC), `EMBED_DIM=384`, **THE robot model** (`ROBOT_KIND`, `ROBOT_RADIUS_M=0.3`, `ARM_MOUNT_X_M`, `ARM_REACH_M`, `LIFT_MIN_M`/`LIFT_MAX_M`, `REACH_Z_*`, `APPROACH_DIST_*`), `DEFAULT_ROBOT_ID`, `ONLINE_STORE_ID`, `money()`, `angle_diff()` | everyone |
| `catalog.py` | `Product` (+ `SimAsset` hints, barcode, `category` = taxonomy id), `Lot`, `Deal`/`DealTerms`, `ProductLocation`, `StoreOffer`, `StoreInfo` (+ online shipping), `online_shipping_cents()` (THE shipping rule), `SearchQuery`/`SearchHit`, `NearExpiryItem`, `ScanApplyResult`, `MerchantDashboard`; **categories** (`STANDARD_CATEGORY_IDS`, `CategoryInfo`) and **stock management** (`StockLevel`, `StockAlert`, `RestockRequest`, `StockAdjustment`, `StockMovement`, `StockChangeResult`, `stock_status()` / `default_low_threshold()`) | catalog, agent, fleet, frontend |
| `planogram.py` | Schemas of the data files: `Planogram` (`Fixture`, `Aisle`, `Slot` + `low_threshold`), `ProductsFile`, `OnlineCatalog`, `FixturesFile` (`DemoUser`), `RobotsFile` (`RobotSpec`, `StoreCameraSpec`; data/fixtures/robots.yaml), `CategoriesFile` (`CategoryDef`; data/categories.yaml). Unique-id + reference validators. **3D/sim frame + reach rules** and YAML examples in the module docstring | catalog, sim, routing, fleet, data loaders |
| `fleet.py` | `FleetRequest`, `RequestDraft`, `StoreChoice`, `RequestPlan`, `GoalKind`/`GoalStatus` (+ `GOAL_TRANSITIONS`), `GoalItem`, `Goal`, `GoalSpec`, `GoalUpdate`, `Assignment` (+ candidates), `RobotInfo` (+ `robot_info_from`), `BrainStep`/`BrainReply`/`BrainStatus`, THE assignment rule `score_robot`/`choose_robot`, `GoalRequest` (manual goal body), `goal_items_from_trip` (shop goal ↔ trip route), `split_budget` | fleet, agent (brain), gateway, frontend |
| `cameras.py` | `CameraKind`, `CameraMount` (+ `DEFAULT_ROBOT_CAMERA_MOUNTS`), `CameraInfo` (+ `snapshot_url`/`stream_url`), `VisionObservation`/`ObservedItem`, camera id helpers (`robot_camera_id`, `store_camera_id`, `parse_camera_id`); frames are `sim.CameraFrame` | perception (cameras), robot, sim, frontend |
| `profile.py` | `UserProfile`, `PurchaseRecord`, `Feedback`, `ProfileUpdate` (+ `ProfileChange`) | recommender, messaging, frontend |
| `shopping.py` | `TripState` + `TRIP_TRANSITIONS`/`can_transition`, `Intent`, `ListItem`, `ShoppingList`, `Recommendation`, `CartLine`, `Cart` (totals are computed fields) | agent, gateway, trust, frontend |
| `trip.py` | `Trip` aggregate (+ `stop_status`, `followup`; also importable as `contracts.shopping.Trip`) | gateway, agent, frontend |
| `routing.py` | `GridSpec` (the only grid wire type), `Stop` (+ `detour_m`/`detour_s`, `qty`, `item_ids`), `StopStatus`, `RouteRequest`, `RouteLeg`, `Route` | routing, robot, frontend |
| `sim.py` | Gateway ⇄ Isaac worker wire protocol, **multi-store + multi-robot, multiplexed by `robot_id`** (`SimCommand` incl. `SetStreamsCmd` / `UnloadBasketCmd` / `SetSlotStockCmd`, `SimMessage` incl. `SimHosted`, `GatewayToSim` unions; lenient `parse_*` helpers + `command_error_result`; `SimHello` with `stores`/`robots`/`cameras`; handshake, per-store backend choice, per-robot busy/cancel and reconnect rules in the docstring), `SimErrorCode` (+ `unknown_robot`, `battery_low`) + `RobotError`, `RobotState` (+ battery, lift, backend), `CameraFrame` (+ `camera_id`, ground-truth `instances`), `ItemInstance`, `BASKET_*`, `planogram_hash`, `SimStatus`, `SimLink` Protocol | sim, robot, gateway |
| `perception.py` | `Detection`, `ScanReport`, `FreshnessCandidate`, `PickChoice` (≤ `PICK_CHOICE_MAX`=3), `BasketCheck` (+ `BasketCheck.compare`) | perception, catalog, gateway |
| `trust.py` | `IntentMandate`, `CartMandate`, `compute_cart_hash(cart, trip_id)`, `MandateDecision` (+ `MandateVerdict`, `MandateReason`, `MandateViolation`), `ApprovalKind`/`ApprovalRequest`/`ApprovalResponse`, `CheckoutRequest`, `Receipt`, `TransparencyEntry`, `AuditEntry`, `TrustLedger` | trust, gateway, messaging, frontend |
| `messaging.py` | `InboundMessage`, `OutboundMessage` (+ `about_skus`), `SendResult`, `ReplyInterpretation`, `normalize_handle()` | messaging, agent |
| `events.py` | `Event` union on `type` (38 types; envelope `trip_id`/`store_id`/`robot_id`/`user_id`/`request_id`), `EVENT_PRODUCERS` (gateway / fleet / agent / catalog / any), `LIVE_ONLY_EVENT_TYPES`, `make_event`, `goal_envelope` / `trip_envelope`, `event_matches` (THE `/ws` filter rule), `parse_event`, `with_seq`, `event_json_schema()` | gateway, fleet, agent, catalog, frontend |
| `tools.py` | Shopper LLM tools (`TOOL_DEFINITIONS`, `parse_tool_input`, `AgentReply`, `ToolCallRecord`) and **robot brain tools** (`BRAIN_TOOL_NAMES` incl. `add_item`, `BRAIN_TOOL_DEFINITIONS`, `BRAIN_STRICT_TOOLS`, `strict_schema_budget`, `parse_brain_tool_input`, result models `PickResult` / `AskUserResult` / `RouteSummary` / `PlaceResult` / `BrainToolError`, executor mapping) | agent |
| `services.py` | One `runtime_checkable` Protocol per service (incl. `FleetService`, `CameraService`, `RobotBrainService`), gateway-owned `TripController`/`ApprovalBroker`/`EventPublisher`, the `Services` DI container, `ARM_NAMES`, `SERVICE_PROTOCOLS`, `EXTRA_SERVICE_PROTOCOLS`, `SERVICE_FACTORIES`, `GATEWAY_PROTOCOLS` | everyone |
| `api.py` / `api.md` | Gateway REST + WS request/response models (`CheckoutStatus`, `AddLineResult`, `RobotMessageRequest`, `ObserveRequest`, `WsRobotChat`, ...), the endpoint table and the event producer table | gateway, frontend |
| `db/schema.sql` | Postgres 17 + pgvector DDL mirroring the models (money = integer cents, `vector(384)`; v0.2: robots, cameras, requests, goals, brain_steps, stock_movements, stock_alerts, categories) | catalog, recommender, trust, gateway, fleet |

## Rules of use

- **Import from `contracts`, never from another arm.** Reach other arms through `services.<field>` at call time.
- **Fakes return real contract models** with deterministic data, so the pipeline always runs.
- **Money is `int` cents; geometry is meters/radians in the store world frame** (x right, y up, origin bottom-left,
  theta CCW from +x). Fixture `rect` is axis-aligned; `yaw` is the direction the pick face points.
- **The gateway owns the Trip; the fleet owns every Goal.** Arms never mutate a `Trip` they're handed; trip changes go
  through `services.trips` (`TripController`), goal changes through `services.fleet.update_goal`, questions to people
  through `services.approvals`.
- **Events:** exactly one producer per type (`EVENT_PRODUCERS`): gateway (trip flow, robot.state, live frames), fleet
  (request/goal/fleet), agent (agent.message, brain.step + in-store execution events of the goal its brain runs),
  catalog (stock), any (error). Build with `make_event("goal.assigned", trip_id=None, store_id=..., robot_id=...,
  goal=..., assignment=...)`; the publisher sets the bus-wide `seq`. `frame` / `camera.frame` events reject unblurred
  frames and never enter the sequenced bus.
- **Multi-robot:** every `RobotService` method takes `robot_id` first; sim wire commands carry `robot_id` with a
  per-command meaning for None (stop = all robots, reset = whole store; required for motion; no wire default). The
  robot arm picks Isaac vs the 2D fake PER STORE. Assign robots only with `fleet.choose_robot` (deterministic).
- **Envelopes:** events about a shop goal carry `trip_id = goal.trip_id` (`events.goal_envelope`); trip events use
  `events.trip_envelope`; `/ws` filtering is `events.event_matches`.
- **Privacy:** every frame leaving `CameraService` (snapshot, MJPEG, WS, vision) is face-blurred (`blurred=True`).
- **Checkout:** `compute_cart_hash(cart, trip_id)` is what the user approves; `TrustService.checkout` must refuse if
  the approval's `cart_hash` differs. The LLM `checkout` tool calls `services.trips.start_checkout` (same path as
  `POST /api/trips/{id}/checkout`).
- **Robot failures** raise `contracts.sim.RobotError` with a `SimErrorCode`; branch on `.code`, never on strings.
- **Data files** are loaded with `utils.data` (`load_products`, `load_planogram`, `list_store_ids`,
  `load_online_catalog`, `load_demo_fixtures`, `load_robots` / `load_robot` / `robots_for_store`, `load_cameras` /
  `load_store_cameras`, `load_categories` / `load_categories_file`). Check the data dir with
  `uv run python -m utils.data` (robot clearance `ROBOT_RADIUS_M`, SO-101 reach + shelf heights, approach headings,
  docks, ceiling cameras, category ids, EAN-13 check digits, mandate expiry, ...).
- TS types for the frontend are generated (`make gen-types`), never hand-written: OpenAPI for REST,
  `event_json_schema()` (`GET /api/_schema/events`) for websocket events.

## How to change a contract (versioning rule)

`contracts/` is locked (CLAUDE.md §4 rule 4, §5 "Contracts change"). To change it:

1. Branch `contracts/<slug>` off `main` — never change contracts inside an arm PR.
2. Make the change **and bump `CONTRACTS_VERSION`** in `contracts/__init__.py` (and the version in `api.md`):
   patch = additive optional field/docs, minor = new model/endpoint/event or new required field, major = rename/removal.
3. Update every affected arm's `fake.py` in the same PR so `main` stays green, plus `db/schema.sql` / `api.md` /
   `OVERVIEW.md` if the shape they mirror changed. Sim wire: patch changes are safe because both ends parse with
   `sim.parse_sim_message` / `parse_gateway_to_sim`, which drop unknown keys at any depth (nested models are
   `extra="forbid"`, so never `model_validate` wire JSON directly); a minor/major bump makes the gateway refuse an old
   Isaac worker (`SimHello.contracts_version`), so redeploy the worker on the GPU box too.
4. `make check` (tests in `contracts/tests/` must pass), list the affected arms in the PR description, get a review
   from one affected arm owner, and announce it in team chat. Arm branches then `git merge origin/main`.
