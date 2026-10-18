# contracts v0.1.0 → v0.2.0: what each builder must implement

`CONTRACTS_VERSION = "0.2.0"`. Read `OVERVIEW.md` (per-arm cheat sheet) and `api.md` (endpoints, envelopes, screens)
first. Everything below is in `contracts/`; signatures are copied from `contracts/services.py`. "New" = not in
v0.1; "changed" = signature or semantics changed. No exported name was removed, so existing imports keep working.

**State of `main` after this PR:** `uv run pytest contracts utils` passes; `make demo` prints `DEMO OK` (trip 2
switches to the hot salsa). `make check` fails exactly these 7 Protocol-conformance tests until the arms add the new
methods (each test turns green when its arm's object satisfies the Protocol below):
`arms/agent/tests/test_agent.py::test_factories_satisfy_protocol_without_calling_services` (AgentService),
`arms/catalog/tests/test_catalog_fake.py::test_protocol_conformance_and_lazy_construction` (CatalogService),
`arms/gateway/tests/test_broker.py::test_protocols` (ApprovalBroker, SimLink),
`arms/gateway/tests/test_registry.py::test_all_fakes_by_default` and `::test_real_arm_without_prereqs_falls_back_to_fake`
(TripController + every fake), `arms/robot/tests/test_fake.py::test_conforms_to_protocol` and
`arms/robot/tests/test_service.py::test_conforms_to_protocol` (RobotService).

Cross-cutting rules every producer must follow:
- **Event envelopes** (`contracts/events.py`): every event about a SHOP goal is built with
  `make_event(type, **goal_envelope(goal), ...)` so `trip_id = goal.trip_id` (+ store_id, robot_id, user_id,
  request_id); trip-flow events with `**trip_envelope(trip)`; `request.*` with `request_id` + `user_id`; store-level
  events (stock, robots, non-shop goals) with `store_id` (+ `robot_id`). New envelope fields: `user_id`, `request_id`.
- **One producer per event type** (`EVENT_PRODUCERS`): gateway = trip flow + `robot.state` + live frames; fleet =
  `request.*`, `goal.*`, `fleet.robot_updated`; agent = `agent.message` + `brain.step` + `robot.arrived`,
  `scan.result`, `pick.choices`, `item.picked`, `basket.verified`; catalog = `stock.*`; any = `error`.
- **Ids**: robots `rover-ave-1` Pico / `rover-ave-2` Rhea (store_ave), `rover-hill-1` Hazel (store_hill); cameras
  `<robot_id>.front|wrist|basket`, `<store_id>.ceiling-<n>`; slot ids unique across stores (`AVE-...`, `HILL-...`).
- **Robot model** (`contracts.common`): `ROBOT_RADIUS_M=0.3`, `ARM_MOUNT_X_M=0.12`, `ARM_REACH_M=0.35`, lift
  0.2–1.2 m, items z 0.1–1.5 m, approach 0.35–0.40 m; reach = `slot_reachable(robot_pose, approach_pose,
  item_position)` (the slot, never the unit); battery `BATTERY_DRAIN_PCT_PER_M=0.2`,
  `BATTERY_IDLE_DRAIN_PCT_PER_MIN=0.05`, `BATTERY_CHARGE_PCT_PER_MIN=5`, `BATTERY_MOTION_MIN_PCT=5`,
  `DOCK_TOLERANCE_M=0.15` (CHARGING = idle on the dock); `STAFF_USER_ID="staff"` (who robot questions go to on goals
  without a user).

---

## fleet (NEW arm: `arms/fleet`, exports `create(services)` / `create_fake(services)` → `FleetService`)

Implement every method (fake = deterministic, same semantics; real may add smarter store choice):
- `async start() -> None` (idempotent; subscribe to `robot.states()`, publish `fleet.robot_updated` ≤ 1 Hz per robot
  for pose-only changes; an idle robot below `MIN_BATTERY_PCT` gets a `return_to_dock` goal) · `async shutdown()`.
- `async submit_request(request: FleetRequest) -> RequestPlan` — `agent.interpret_request(text, user_id, store_id,
  role=, profile=, history=)`; shop: store choice by catalog coverage + price (`StoreChoice` with `reason`), split
  when it helps, `RequestPlan.budget_split = split_budget(budget, {store: est_total_cents})`, per chosen store
  `trips.create_trip(CreateTripRequest(user_id, text, store_id, shopping_list=<sub-list with budget share>,
  request_id, robot_id=<pre-assigned>, auto_execute=True))` + one PENDING shop `Goal` (trip_id, robot pre-assigned:
  `Goal.robot_id` set, robot lists it in `queued_goal_ids`, `available=False`, items empty). Shopper status / chat →
  reply only, `completed`; shopper fetch / shelf_scan / return_to_dock → `rejected`; ambiguous → `needs_input`.
  `in_reply_to` → re-interpret with `history=[original text, question, ...]`, update THAT plan, append to `replies`.
- `async get_request(request_id) -> RequestPlan` (KeyError) · `async list_requests(user_id=None, limit=20) ->
  list[RequestPlan]` (newest first).
- `async list_robots(store_id=None) -> list[RobotInfo]` · `async get_robot(robot_id) -> RobotInfo` (KeyError) — build
  with `robot_info_from(spec, state, ...)`.
- `async create_goal(spec: GoalSpec) -> Goal` — pin `spec.robot_id` or `choose_robot(robots, store_id=, target=,
  kind=)`; shelf_scan: expand categories / aisle_ids (empty = whole store) into `slot_ids` from
  `catalog.get_planogram` (category matches Product.category or subcategory); `user_id` None → `STAFF_USER_ID`; shop
  without trip_id → create an auto_execute trip. Start in the background (queue if the robot is busy).
- `async list_goals(store_id=None, robot_id=None, status: GoalStatus | Sequence[GoalStatus] | None = None, *,
  active=None, kind=None, request_id=None, trip_id=None, limit=50) -> list[Goal]` · `async get_goal(goal_id) -> Goal`.
- `async update_goal(goal_id, update: GoalUpdate) -> Goal` — the brain's only write path: patch `items`, append
  `add_items` and `scanned_slot_ids` (progress = scanned / slot_ids for shelf_scan), status via
  `can_goal_transition` (same status = no-op; invalid → ValueError). Publishes `goal.updated` / `goal.completed` /
  `goal.failed`.
- `async cancel_goal(goal_id, reason="") -> Goal` — stop the robot, cancel the brain task, withdraw pending questions
  (`approvals.pending(goal_id=...)` + `approvals.cancel`).
- `async execute_trip(trip_id, robot_id=None) -> Goal` — get-or-create the trip's shop goal, `items =
  goal_items_from_trip(trip)`, keep the pre-assigned robot if still eligible else `choose_robot`,
  `trips.assign_robot(trip_id, robot_id, goal_id)`, set ACTIVE, `brain.run_goal`; on `RobotError` reassign
  (`MAX_GOAL_ATTEMPTS=2`, `goal.assigned` with `attempt` > 1, rebuild items from the trip); return the TERMINAL goal;
  no eligible robot → failed, `failure_code="no_robot"`. If `run_goal` returns non-terminal, finalize it.
- `async message_robot(robot_id, text, user_id=None) -> BrainReply` (→ `brain.handle_message`) ·
  `async stop_robot(robot_id, reason="") -> RobotInfo` (→ `robot.stop` + cancel the active goal).
- Events: `request.received`, `request.planned` (re-sent on status changes), `goal.created`, `goal.assigned`,
  `goal.updated`, `goal.completed`, `goal.failed`, `fleet.robot_updated`.
- Fake must keep `make demo` green: one robot per goal in store_ave (Pico), scripted store choice.

## agent (AgentService changed + NEW RobotBrainService via `create_brain` / `create_brain_fake`)

AgentService:
- NEW `async interpret_request(text, user_id, store_id=None, *, role="shopper", profile=None, history=None) ->
  RequestDraft` (fake: keyword rules; the taco list must equal `expand_intent`'s; "Hazel, go charge" →
  return_to_dock + robot_hint; "scan dairy at Hill" → shelf_scan; questions → status / chat).

RobotBrainService (NEW):
- `async run_goal(robot_id, goal) -> Goal` — tool loop over `brain_tool_definitions(strict=True)` (only
  `BRAIN_STRICT_TOOLS` are strict; the request fits the strict limits, tested). Fake = scripted executor running the
  SAME tool sequence and reproducing the old orchestrator per-stop loop exactly (stop order, approvals, pick choices,
  OOS → online, mispricing via scan, near-expiry). Executor mapping is in the comments of `contracts/tools.py`; key
  rules: `check_line` on the FULL line before the first unit; `add_line` ONCE per GoalItem with qty = picked units and
  the approval_id; ask via `trips.ask` for goals with a trip (`approvals.request` otherwise, `user_id` =
  goal.user_id or `STAFF_USER_ID`), executor fills sku / rec_id / amount_cents (qty × price) and payload keys;
  mirror item status onto stops with `trips.set_stop_status`; basket check expects only the units THIS robot
  placed; at end_at='checkout' navigate to checkout, `robot.unload_basket`, then finish. Raises only `RobotError`
  for `BRAIN_FATAL_CODES` (`battery_low`, `unknown_robot`) or a robot in ERROR, never after update_goal(failed);
  `disconnected` is retryable. LLM failures → error event + `brain.step(kind="error")`, `BrainStatus.mode =
  "scripted"`, finish with the scripted executor. On task cancellation withdraw the pending question
  (`approvals.cancel`).
- `async handle_message(robot_id, text, user_id=None) -> BrainReply` — 'also grab limes' → `add_item`; 'skip X' →
  item skipped; idle → answer (status / what it sees).
- `async status(robot_id) -> BrainStatus` · NEW `async recent_steps(robot_id=None, goal_id=None, limit=50,
  before=None) -> list[BrainStep]` (newest first; keep a ring ≥ 500 per robot).
- Tool results: `LocateItemsResult` (`LocatedItem.category`), `RouteSummary.from_route(route)` for plan_route,
  `PickResult`, `PlaceResult`, `AskUserResult` (pick_choice options in freshness rank order), new `add_item` →
  `GoalItem`, errors as `BrainToolError` (`retryable` from `BRAIN_RETRYABLE_CODES`).
- Events: `brain.step` (with `step.step_no`, per robot, 1-based), `robot.arrived`, `scan.result`, `pick.choices`,
  `item.picked`, `basket.verified` — all with `goal_envelope(goal, robot_id)`. Real brain: claude-sonnet-5-5 via
  `utils.llm`, unit-tested with a mocked client.

## catalog (CatalogService changed)

- NEW `async list_categories(store_id=None) -> list[CategoryInfo]` · `async category_products(category_id,
  store_id=None) -> list[SearchHit]` (KeyError).
- NEW `async stock_levels(store_id, status=None, category=None, q=None) -> list[StockLevel]` (fill `unit`, `size`,
  `image_url`; category matches category OR subcategory) · `async stock_alerts(store_id) -> list[StockAlert]`.
- NEW `async restock(store_id, request: RestockRequest) -> StockChangeResult` (over free capacity → ValueError, nothing
  applied) · `async adjust_stock(store_id, adjustment: StockAdjustment) -> StockChangeResult` (rules in
  `contracts/catalog.py`) · `async stock_history(store_id, sku=None, limit=100, before=None, reason=None, actor=None)
  -> list[StockMovement]`. After restock / adjust: best-effort `services.robot.sync_stock(store_id, slot_ids)`.
- CHANGED `async record_pick(store_id, slot_id, sku, qty=1, trip_id=None, robot_id=None, lot_id=None)` — decrement
  that lot (else earliest expiry), StockMovement reason 'pick', actor `robot:<id>`.
- CHANGED `apply_scan` — StockMovement reason 'scan'; exact → qty = observed_count (lots from `lot_counts`); not exact →
  0 when nothing seen, else `max(qty, observed)`. Keep `sum(lots) == qty`.
- Events: `stock.updated` (`level`, `movement`), `stock.alert` (raise on low/out, clear with `active=False`), envelope
  `store_id` (+ `robot_id` for robot-caused changes).

## perception (PerceptionService unchanged + NEW CameraService via `create_cameras` / `create_cameras_fake`)

- `identify` / `ScanReport`: fill NEW `lot_counts` (units per lot from the frame's instances) when `count_is_exact`;
  scan frames now list EVERY unit of the slot (bbox None when off-image), so the ground-truth count is exact.
- CameraService: `async list_cameras(store_id=None, robot_id=None) -> list[CameraInfo]` · `async get_camera(camera_id)
  -> CameraInfo` · `async snapshot(camera_id, max_width=None) -> CameraFrame` (blurred, Pillow downscale) ·
  `stream(camera_id, fps=5.0, max_width=None) -> AsyncIterator[CameraFrame]` (from `robot.frames(camera_id)`; poll
  `robot.capture_camera` if nothing arrives within 1 s) · `async observe(camera_id, question=None, *, goal_id=None) ->
  VisionObservation` (real: claude-haiku-5-5 via `utils.llm`; fake: from ground-truth instances). Every frame leaving
  the service has `blurred=True`.

## robot (RobotService changed: multi-robot)

- Every method takes `robot_id` first: `list_robots()`, `state(robot_id)`, `reset(robot_id, store_id=None)` (store_id
  must be None or the robot's own store, else `RobotError('unknown_robot')`), `navigate`, `stop`, `scan` (aim the
  pan-tilt front camera; `frame.instances` = every unit of the slot; set `frame.camera_pose`), `capture(robot_id,
  camera="front")`, `capture_camera(camera_id)`, `list_instances(robot_id, slot_id=None)`, `pick` (reach =
  `slot_reachable` on the slot; any unit incl. back rows), `place_in_basket`.
- NEW `async unload_basket(robot_id) -> list[ItemInstance]` · NEW `async sync_stock(store_id, slot_ids=None) -> None`
  (read `catalog.get_planogram`; Isaac: `SetSlotStockCmd` per slot; fake: respawn / remove units deterministically).
- CHANGED `frames(camera_id=None, robot_id=None)`, `states(robot_id=None)` — fan out to every subscriber; opening a
  frames iterator for a camera makes it stream (ref-counted `SetStreamsCmd` union on Isaac, fps = max requested ≤ 15);
  stream frames carry `instances=[]`.
- Backend PER STORE (contracts/sim.py): Isaac iff a connected worker hosts the store `loaded` with `data_hash ==
  planogram_hash(live planogram)`, else send the store-level `ResetCmd(robot_id=None, store_id, planogram, products,
  robots, cameras)` first; never mix Isaac and fake robots in one store; switch back to Isaac only when the store's
  robots are idle with empty gripper + basket. Isaac loss → in-flight command raises `RobotError('disconnected')`
  once, robot continues on the fake from its last state (no reset). Own `battery_pct` with the battery model; refuse
  motion below `BATTERY_MOTION_MIN_PCT` except to the dock. Always send `robot_id` on motion commands.
- Fake: all robots in data/fixtures/robots.yaml across stores, Pillow frames for robot + ceiling cameras,
  ItemInstance `store_id` set, `instance_id(slot_id, n)` ids.

## sim (Isaac worker on vhagar; no Service; code now, deploy later)

- Parse everything with `parse_gateway_to_sim` (lenient: drops unknown keys at any depth); answer an unparsable
  command that has a `cmd_id` with `command_error_result(raw, exc)`.
- Host MANY stores (`/World/Stores/<store_id>` translation-only Xforms, ≥ largest store + 20 m apart; wire poses are
  store-local) and rovers; `SimHello` with `stores` (`loaded`, `data_hash`), `robots`, `cameras`; send `SimHosted`
  (full lists) after any reset that changes hosting.
- `robot_id` semantics per command (no default): required for navigate / scan / pick / place_in_basket /
  unload_basket (missing → `unknown_robot`); stop None = all; reset None = rebuild `store_id`'s scene (from
  `planogram` / `products` / `robots` / NEW `cameras`); capture by `camera_id` or robot + camera.
- NEW `UnloadBasketCmd` (motion) and `SetSlotStockCmd` (store-level: spawn / remove units deterministically, back rows
  first). Magic grasp uses `slot_reachable`. Scan: aim the pan-tilt front camera at `item_position` + half the item
  height, list every non-basket unit of the slot (bbox None off-image), set `frame.camera_pose`. Stream only
  `SetStreamsCmd` cameras, with `instances=[]`.

## gateway (TripController / ApprovalBroker / SimLink changed; new endpoints)

- TripController NEW: `async create_trip(request: CreateTripRequest) -> Trip` (honor `auto_execute`: set
  `robot_logged_in=True`, go `ROUTE_PLANNED → EXECUTING`, call `fleet.execute_trip(trip_id, request.robot_id)`),
  `async assign_robot(trip_id, robot_id, goal_id=None) -> Trip`, `async set_stop_status(trip_id, stop_id, status,
  reason="") -> Trip`, `async check_line(trip_id, line) -> MandateDecision`, `async ask(trip_id, request:
  ApprovalRequest) -> ApprovalResponse` (fill trip/user/store ids, channels + expiry from config, EXECUTING ⇄
  AWAITING_APPROVAL, transparency outcome for recommendations). CHANGED `replan` starts from the trip robot's pose and
  includes new list items; `update_list` during EXECUTING appends a pending stop for added items.
- EXECUTING: replace the per-stop loop with `goal = await fleet.execute_trip(trip_id, robot_id)`; then completed →
  `start_checkout`; failed → checkout if the cart has active lines (+ error event) else trip FAILED; cancelled → trip
  FAILED with the reason.
- ApprovalBroker: CHANGED `pending(trip_id=None, *, goal_id=None, robot_id=None, store_id=None, user_id=None)`; NEW
  `async cancel(approval_id, reason="") -> bool` (resolve `timeout` / channel `system` / note, publish
  `approval.resolved`). `ApprovalDecisionRequest.note` → `ApprovalResponse.note`.
- SimLink: `is_connected(robot_id=None)`, NEW `status() -> SimStatus` (fill `isaac_*` / `fake_*` from the robot arm),
  subscribers get accepted `SimHello` and every `SimHosted`; parse with `parse_sim_message`.
- Events: build trip events with `trip_envelope(trip)`; `/ws` filters `trip_id`, `store_id`, `robot_id`, `user_id`,
  `request_id`, repeatable, via `event_matches`; replay rules and `X-Event-Seq` header as in `api.md`; `/ws/frames`
  sends `camera.frame` for every camera of the robot (`frame` only with `?legacy=1`); NEW multiplexed `WS
  /ws/cameras?camera_id=..|store_id=|robot_id=&fps=&w=`.
- NEW endpoints (api.md): `POST /api/requests`, `GET /api/requests`, `GET /api/requests/{id}`, `GET
  /api/fleet/robots[/{id}]`, `GET /api/fleet/robots/{id}/brain`, `GET /api/fleet/robots/{id}/steps`, `POST
  /api/fleet/robots/{id}/messages`, `POST /api/fleet/robots/{id}/goals` (body `api.CreateGoalRequest`, then
  `body.to_spec(robot_id, caller)`), `POST /api/fleet/robots/{id}/stop`, `POST /api/goals`, `GET /api/goals`, `GET
  /api/goals/{id}`, `GET /api/goals/{id}/steps`, `POST /api/goals/{id}/cancel`, `GET /api/categories`, `GET
  /api/categories/{id}/products`, `GET /api/stores/{id}/stock[/alerts|/history]`, `POST
  /api/stores/{id}/stock/restock|adjust` (adjust: 422 unless reason in `STAFF_STOCK_REASONS`), `GET /api/cameras`,
  `GET /api/cameras/{id}/snapshot.jpg` (`w`, `no-store`), `GET /api/cameras/{id}/stream.mjpg` (`fps`, `w`), `POST
  /api/cameras/{id}/observe`, `GET /api/sim/status`; CHANGED `GET /api/approvals` filters. Error mapping: KeyError
  404, ValueError 409, validation 422.
- Registry: wire `fleet`, `cameras`, `brain` from `SERVICE_FACTORIES`; call `fleet.start()` at boot.

## routing · recommender · trust · messaging

- routing: `GridSpec` inflation is now 0.3 m (`ROBOT_RADIUS_M`); nothing else.
- trust: `ApprovalKind.QUESTION` exists (never payable); `ApprovalRequest.trip_id` optional, new `goal_id`,
  `robot_id`, `store_id`.
- messaging: show robot questions as "Pico asks: …" (`ApprovalRequest.robot_id`); questions may have no trip.
- recommender: unchanged.

## data (data agent)

- `data/fixtures/robots.yaml` (`RobotsFile`: the three robots + ceiling cameras `store_ave.ceiling-1/2`,
  `store_hill.ceiling-1`), `data/categories.yaml` (`CategoriesFile`), ~120 SKUs in `products.yaml` (top-level
  categories from `STANDARD_CATEGORY_IDS`), taco demo intact.
- Planograms: slot ids unique across stores (store prefix), approach poses 0.35–0.40 m from `item_position`,
  item z 0.1–1.5 m, `qty ≤ capacity`, `low_threshold ≤ capacity`, `sum(lots) == qty`; docks ≥ 0.7 m apart and
  ≥ 0.3 m from every approach pose. Check with `uv run python -m utils.data` (currently 127 problems on the old data:
  SO-101 reach, 18 cross-store slot-id collisions, missing robots.yaml / categories.yaml).

## frontend (teammate)

- `arms/frontend/README.md` still describes v0.1. Use the "Web app screens → endpoints + events" table at the end of
  `contracts/api.md` as the source of truth (chat via `/api/requests`, fleet robots + goals, ThinkingLog from
  `/steps` + `brain.step`, CameraGrid via snapshot polling / `/ws/cameras`, categories, stock). Prefix root-relative
  URLs with the API base. Show Start/Login only for trips with `auto_execute=false`.
