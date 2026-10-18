# contracts cheat sheet (v0.2.0)

One page per arm owner: what you implement, what flows in and out, which events, and which `services.<field>` you may
call **at call time** (never in `create()` / `create_fake()`). Full rules: `README.md`; endpoints + event producers:
`api.md`; what each builder must add this round: `CHANGES-0.2.md`; the pipeline: CLAUDE.md §2.

**Ground rules.** Import only `contracts` + `utils`. Money = `int` cents. Geometry = meters/radians in the store frame
(x right, y up, origin bottom-left, theta CCW from +x; the sim uses the same frame per store, Z-up; every wire pose is
store-local). The **gateway owns the `Trip`** (changes via `services.trips`), the **fleet owns every `Goal`** (changes
via `services.fleet.update_goal`), questions to people go through `services.trips.ask` (goals with a trip) or
`services.approvals` (goals without one). Each event type has exactly one producer (`events.EVENT_PRODUCERS`);
shop-goal events carry `trip_id` (`events.goal_envelope(goal)`). Multi-robot: every `RobotService` method takes
`robot_id` first; there is no default robot on the sim wire.

**Fixed ids.** Robots (data/fixtures/robots.yaml, kind `rover_so101`): `rover-ave-1` "Pico", `rover-ave-2` "Rhea"
(store_ave), `rover-hill-1` "Hazel" (store_hill). Cameras: `<robot_id>.front|wrist|basket`, `store_ave.ceiling-1`,
`store_ave.ceiling-2`, `store_hill.ceiling-1`. Slot ids are unique across stores (store prefix, e.g. `AVE-...`).
Robot model (contracts.common): `ROBOT_RADIUS_M`=0.3, SO-101 on a lift `ARM_MOUNT_X_M`=0.12 forward,
`ARM_REACH_M`=0.35, lift `LIFT_MIN_M`..`LIFT_MAX_M` = 0.2–1.2 m, reachable shelf items z 0.1–1.5 m, approach poses
0.35–0.40 m off the shelf face, THE reach predicate `slot_reachable` (checks the slot, never the unit); basket
`contracts.sim.BASKET_OFFSET_LOCAL`; battery model `BATTERY_*` (owned by the robot arm); front camera = pan-tilt head.

## Pipeline at a glance

```
web app text ─▶ POST /api/requests ─▶ fleet.submit_request ─▶ agent.interpret_request (RequestDraft; history on replies)
  ─▶ fleet: store choice (catalog.search: coverage + price; may split; budget via split_budget) ─▶ request.planned
  ─▶ shop: trips.create_trip(store sub-list, auto_execute=True, robot pre-assigned) per store + PENDING shop goal
        ─▶ [trip flow: sourcing ─▶ recs ─▶ route ─▶ (auto) EXECUTING]
        ─▶ fleet.execute_trip ─▶ goal_items_from_trip ─▶ choose_robot ─▶ trips.assign_robot ─▶ goal.assigned
        ─▶ ACTIVE ─▶ brain.run_goal(robot, goal)
     other kinds: fleet.create_goal (shelf_scan: categories/aisles ─▶ slot_ids) ─▶ choose_robot ─▶ brain.run_goal
  brain (tool loop / scripted): locate_items ─▶ plan_route (RouteSummary) ─▶ per item:
     [optional: ask_user (trips.ask) ─▶ yes/no ─▶ replan] ─▶ navigate_to (trips.set_stop_status, robot.arrived)
     ─▶ scan_shelf (robot.scan ─▶ perception.blur_faces/identify ─▶ catalog.apply_scan ─▶ stock.* ─▶ scan.result)
     ─▶ [OOS: update_progress unavailable ─▶ online line] ─▶ pick_item (trips.check_line on the full line first;
     needs_approval ─▶ ask_user over_threshold ─▶ pick_item(approval_id)) ─▶ [produce: ask_user pick_choice ─▶
     pick.choices] ─▶ place_in_basket per unit (catalog.record_pick(lot_id) ─▶ item.picked; once all units are in:
     trips.add_line(qty) ─▶ cart.updated; perception.verify_basket ─▶ basket.verified) ─▶ ... ─▶ navigate_to checkout
     ─▶ robot.unload_basket ─▶ finish_goal ─▶ goal.completed
  ─▶ trip flow: trips.start_checkout ─▶ trust.evaluate ─▶ checkout approval (cart_hash) ─▶ trust.checkout
  ─▶ checkout.completed ─▶ agent.compose_followup ─▶ messaging.send ─▶ followup.sent ─▶ reply
  ─▶ agent.interpret_reply ─▶ recommender.record_feedback ─▶ profile.updated   (trip 2 is visibly smarter)
cameras: robot/ceiling frames (robot.capture_camera / frames) ─▶ perception.blur_faces ─▶ snapshot / MJPEG / WS / observe
staff: restock / adjust ─▶ catalog (stock.*) ─▶ robot.sync_stock (sim shelves match) ; shelf_scan goals ─▶ apply_scan
```

## Per arm

| Arm | Implements | Consumes | Produces | May call |
|---|---|---|---|---|
| **fleet** (new) | `FleetService` (`arms/fleet`: `create` / `create_fake`) | `FleetRequest` (+ `in_reply_to`), `RequestDraft`, `RobotSpec`s (`utils.data.load_robots`), `RobotState`s (`robot.list_robots` / `states`), `GoalSpec`, `GoalUpdate` (+ `add_items`) from the brain | `RequestPlan` (`StoreChoice` with reasons, `budget_split`), `Goal` (+ `Assignment` via `choose_robot`, reassign ≤ `MAX_GOAL_ATTEMPTS`; shop items via `goal_items_from_trip`), `RobotInfo` (`robot_info_from`; queued goals make a robot not available); **publishes** `request.*`, `goal.*`, `fleet.robot_updated` | `agent` (interpret_request), `catalog` (search/offers for store choice, get_planogram for shelf_scan slots), `routing` (distances), `robot` (states, stop), `brain` (run_goal as a task, handle_message, status), `trips` (create_trip, assign_robot, get_trip), `approvals` (pending, cancel), `events` |
| **agent** | `AgentService` (NL understanding + shopper chat) **and** `RobotBrainService` (`create_brain` / `create_brain_fake`: per-robot managing LLM) | `Intent`, `UserProfile`, read-only `Trip`, `InboundMessage` + pending approvals; brain: `Goal`, `brain_tool_definitions(strict=True)`, user messages | `RequestDraft`, `ShoppingList`, `AgentReply`, `ReplyInterpretation`, follow-up `OutboundMessage`; brain: `BrainStep` (`step_no`), `BrainReply`, `BrainStatus`, `recent_steps`, final `Goal`; **publishes** `agent.message`, `brain.step`, `robot.arrived`, `scan.result`, `pick.choices`, `item.picked`, `basket.verified` | `catalog`, `recommender`, `routing`, `trips` (add_line / replan / start_checkout / update_list / get_trip / set_stop_status / check_line / ask), `approvals` (goals without a trip; cancel), `events`; brain also `robot` (incl. unload_basket), `perception`, `cameras` (observe), `fleet` (update_goal only). Never `trust.checkout`. Real = Claude via `utils.llm` (mock the client in tests); fake = scripted, same tool sequence |
| **catalog** | `CatalogService` (+ categories + stock management) | `data/` via `utils.data` (`ProductsFile`, `Planogram`, `OnlineCatalog`, `CategoriesFile`); `ScanReport` (`robot_id`, `lot_counts`), `record_pick(..., robot_id, lot_id)`, `RestockRequest`, `StockAdjustment` | `StoreInfo`, live `Planogram`, `SearchHit`/`StoreOffer`/`ProductLocation`, `Deal`, `NearExpiryItem`, `ScanApplyResult`, `MerchantDashboard`, `CategoryInfo`, `StockLevel` (`stock_status`, unit/size/image_url), `StockAlert`, `StockMovement`, `StockChangeResult`; **publishes** `stock.updated`, `stock.alert` | `events`; `robot.sync_stock` (best-effort, after restock / adjust) |
| **perception** | `PerceptionService` **and** `CameraService` (`create_cameras` / `create_cameras_fake`) | RAW `CameraFrame` (`instances` = sim ground truth: scans list every unit of the slot; `camera_pose`); camera registry from `utils.data.load_cameras` + `sim_link.status()` | blurred `CameraFrame` (snapshot / stream, `max_width` scaling), `ScanReport` (+ `lot_counts`), `PickChoice`, `BasketCheck`; `CameraInfo`, `VisionObservation` | `catalog`, `robot` (capture_camera / frames), `sim_link` (status); `utils.llm` (claude-haiku-5-5 vision) |
| **robot** | `RobotService`, MULTI-ROBOT (real: backend PER STORE: Isaac via `sim_link` when the worker hosts the store with a matching `data_hash`, else the built-in 2D fake; Isaac loss → `disconnected` once, then continue on the fake from the last state; fake: 2D kinematic sim for every robot in robots.yaml + Pillow camera frames for robot and ceiling cameras) | `robot_id` + waypoints / slot / sku / instance; planogram + `RobotSpec` (dock, battery, cameras); `ROBOT_RADIUS_M`, `slot_reachable`, `BATTERY_*`, `BASKET_OFFSET_LOCAL` | `RobotState` (`robot_id`, battery, lift, `backend`), RAW `CameraFrame` (`camera_id`, `camera_pose`, `instances`), `ItemInstance` (`store_id`); `unload_basket`, `sync_stock`; raises `RobotError(code)` | `sim_link` (real), `catalog` (planogram/products for reset, sync_stock + fake geometry) |
| **sim** (Isaac worker on vhagar, no Service) | worker side of `contracts/sim.py`: one worker, MANY stores (translation-only `/World/Stores/<id>` Xforms) + rovers | `GatewayToSim` via `parse_gateway_to_sim` (lenient; + `set_streams`, `reset` with `robots` / `cameras`, `unload_basket`, `set_slot_stock`); scene from `ResetCmd.planogram/products/robots/cameras` (else `utils.data`) | `SimHello` (stores + `loaded` + `data_hash`, robots, cameras), `SimHosted` after hosting changes, one `CommandResult` per `cmd_id` (echo `robot_id`; unparsable → `command_error_result`), per-robot `RobotState`, RAW `CameraFrame`s, `InstanceInfo`, `SimLog` | — (wire only) |
| **routing** | `RoutingService` | `RouteRequest`; `GridSpec` inflated by `ROBOT_RADIUS_M` (0.3) | `Route` (legs start/end on stop poses, `detour_m`/`detour_s`), `GridSpec` | `catalog` |
| **recommender** | `RecommenderService` | `UserProfile`, `ShoppingList`, `Cart`, `Feedback`, `PurchaseRecord` | `Recommendation` (reason + signals, `sponsored=False`), `ProfileUpdate` | `catalog`; `utils.llm` |
| **trust** | `TrustService` | `IntentMandate`, `Cart` + line + budget, `CheckoutRequest`, `TransparencyEntry` | `MandateDecision`, `Receipt`, `TrustLedger` (`ApprovalKind.QUESTION` exists for robot questions; never payable) | nothing |
| **messaging** | `MessagingService` | `OutboundMessage`, `ApprovalRequest` (may carry `robot_id` / `store_id`: "Pico asks …") | `SendResult`, `InboundMessage` | `recommender` |
| **gateway** | `TripController` (+ `create_trip` with `auto_execute`, `assign_robot`, `set_stop_status`, `check_line`, `ask`), `ApprovalBroker` (+ `pending` filters, `cancel`), `EventPublisher`, `SimLink` (multi-worker, `status()`, `SimHosted`); REST + WS from `api.md` (incl. `/ws` filters `user_id` / `request_id` / repeated keys via `events.event_matches`, `/ws/cameras` multiplex, `X-Event-Seq`); wires `SERVICE_FACTORIES` (fleet, cameras, brain too) | everything | trip-flow events (`events.trip_envelope`), `robot.state`, live `camera.frame` (+ legacy `frame`); MJPEG/JPEG from `CameraService`; at EXECUTING calls `fleet.execute_trip` instead of its old per-stop loop (auto_execute trips get there without login); calls `fleet.start()` at boot | every service (composition root) |
| **frontend** (web app, teammate) | — (the ONLY UI: shopper, store staff, fleet views; screen → endpoint table at the end of `api.md`) | REST (`api.md`), `/ws` (filters `trip_id`/`store_id`/`robot_id`/`user_id`/`request_id`), `/ws/frames`, `/ws/cameras`, snapshot polling + ≤ 3 MJPEG `<img>`; TS types from `make gen-types` | `api.ClientMessage` (`chat` + `trip_id`, `approval`, `robot_chat`, `ping`), `FleetRequest` (+ `in_reply_to`), `CreateGoalRequest` / `GoalSpec`, `RestockRequest`, `StockAdjustment` | — |

## Brain tools (`contracts/tools.py`, executor mapping in its comments)

`locate_items` · `plan_route` (→ `RouteSummary`) · `navigate_to` · `look` (camera → `VisionObservation`) ·
`scan_shelf` · `pick_item` (→ `PickResult`, `approval_id` input) · `place_in_basket` (→ `PlaceResult`) · `ask_user`
(→ `AskUserResult`, `item_id` input; executor fills money fields) · `add_item` (→ `GoalItem`) · `update_progress` ·
`finish_goal`. Inputs: `BRAIN_TOOL_INPUT_MODELS`; definitions: `BRAIN_TOOL_DEFINITIONS` /
`brain_tool_definitions(strict=True)` (only `BRAIN_STRICT_TOOLS` strict, to fit the API's strict limits). Robot
failures come back as `BrainToolError` (ok=False, `retryable`); `disconnected` is retryable (the robot continues on the
2D fake); only `BRAIN_FATAL_CODES` (`battery_low`, `unknown_robot`) or a robot in ERROR make `run_goal` raise (the fleet
reassigns). LLM failures fall back to the scripted executor mid-goal.

## Events

gateway: `trip.created` · `trip.snapshot` · `trip.state_changed` · `intent.received` · `list.ready` ·
`sourcing.split` · `recs.ready` · `route.planned` · `stop.updated` · `robot.state` · `cart.updated` ·
`approval.requested` · `approval.resolved` · `mandate.decision` · `checkout.completed` · `followup.sent` ·
`feedback.received` · `profile.updated` · `frame` + `camera.frame` (live-only).
fleet: `request.received` · `request.planned` · `goal.created` · `goal.assigned` · `goal.updated` · `goal.completed` ·
`goal.failed` · `fleet.robot_updated`. agent: `agent.message` · `brain.step` · `robot.arrived` · `scan.result` ·
`pick.choices` · `item.picked` · `basket.verified`. catalog: `stock.updated` · `stock.alert`. any: `error`.
Envelope: `trip_id`, `store_id`, `robot_id`, `user_id`, `request_id`, `ts`, `seq`, `source` (`goal_envelope`,
`trip_envelope`; filter rule `event_matches`).

## Failure codes to branch on

`RobotError.code` / `CommandResult.error_code`: `unknown_slot`, `no_instance` → re-source online · `grasp_failed` →
retry / skip (out of reach; item `failed`) · `path_blocked`, `timeout` → re-plan or skip · `cancelled` · `busy` (per
robot) · `not_ready` · `disconnected` → raised once on Isaac loss; the robot continues on the 2D fake, re-plan ·
`unknown_robot` (missing / unknown robot_id) · `battery_low` → return_to_dock · `internal` (incl. an unparsable
command). Goals: `Goal.failure_code` (+ `no_robot`). Mandate: `MandateReason` (+ `MandateViolation.line_id`).
