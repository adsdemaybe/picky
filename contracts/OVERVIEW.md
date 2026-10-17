# contracts cheat sheet (v0.1.0)

One page per arm owner: what you implement, what flows in and out, which events, and which `services.<field>` you may
call **at call time** (never in `create()` / `create_fake()`). Full rules: `README.md`; endpoints + event producers:
`api.md`; the pipeline: CLAUDE.md §2.

**Ground rules.** Import only `contracts` + `utils`. Money = `int` cents. Geometry = meters/radians in the store frame
(x right, y up, origin bottom-left, theta CCW from +x; the sim uses the same frame, Z-up). The **gateway owns the
`Trip`**: nobody else mutates it. Trip changes go through `services.trips`, and questions to the shopper go through
`services.approvals`. The **gateway publishes every event** except `agent.message` (agent) and `error` (any arm), so
most arms publish nothing.

## Pipeline at a glance (gateway drives it)

```
intent ─▶ agent.expand_intent ─▶ list.ready ─▶ agent.source_list ─▶ sourcing.split ─▶ recommender.recommend
  ─▶ recs.ready ─▶ routing.plan_route ─▶ route.planned ─▶ [login] ─▶ per stop: robot.navigate ─▶ robot.scan
  ─▶ perception.blur_faces/identify ─▶ catalog.apply_scan ─▶ scan.result ─▶ (produce: perception.rank_freshness
  ─▶ pick.choices ─▶ approval) ─▶ robot.pick/place_in_basket ─▶ catalog.record_pick ─▶ item.picked ─▶ cart.updated
  optional stop ─▶ approvals.request ─▶ yes/no ─▶ trips.replan  ...  ─▶ trips.start_checkout ─▶ trust.evaluate
  ─▶ checkout approval (cart_hash) ─▶ trust.checkout ─▶ checkout.completed ─▶ agent.compose_followup
  ─▶ messaging.send ─▶ followup.sent ─▶ reply ─▶ agent.interpret_reply ─▶ recommender.record_feedback ─▶ profile.updated
```

## Per arm

| Arm | Implements | Consumes | Produces | May call |
|---|---|---|---|---|
| **catalog** | `CatalogService` | `data/` via `utils.data` (`ProductsFile`, `Planogram`, `OnlineCatalog`); `ScanReport` (`count_is_exact`, `lot_freshness`, `shelf_tag_cents`, `pose`); `record_pick` after each pick | `StoreInfo` (online shipping fields), live `Planogram`, `SearchHit`/`StoreOffer`/`ProductLocation`, `Deal` (slot deal id = `store:slot`), `NearExpiryItem`, `ScanApplyResult`, `MerchantDashboard` (heatmap `picks` from `record_pick`) | nothing (leaf) |
| **perception** | `PerceptionService` | RAW `CameraFrame` whose `instances` hold sim ground truth (sku, instance_id, lot_id, freshness, pixel `bbox`) | blurred `CameraFrame` (`blurred=True`), `ScanReport`, `PickChoice` (≤ `PICK_CHOICE_MAX`=3, each with `instance_id` for `robot.pick`, bbox, face-free thumbnail), `BasketCheck` | `catalog` (product master, planogram); `utils.llm` for the VLM step |
| **sim** (Isaac worker, no Service) | worker side of `contracts/sim.py` over `ws /ws/sim` (`SimHello` with token, version and `data_hash`) | `GatewayToSim` commands; build the scene from `ResetCmd.planogram/products` (else `utils.data`); frame and placement rules in `planogram.py` docstring; ground truth shared with the robot fake via `contracts.sim` `instance_id` / `unit_freshness` / `BASKET_OFFSET_LOCAL` | exactly one `CommandResult` per `cmd_id` (`error_code` on failure; `busy` when a motion is running; `cancelled` on stop), `RobotState`, RAW `CameraFrame` with `instances`, `InstanceInfo`, `SimLog` | — (talks only over the wire) |
| **robot** | `RobotService` (real: drives `sim_link`; fake: 2D kinematic sim, canned JPEG, ground-truth `instances`); same `instance_id` / `unit_freshness` / `BASKET_OFFSET_LOCAL` (contracts.sim) as the Isaac scene | `RouteLeg.waypoints` (final theta must be reached), `slot_id`/`sku`/`instance_id`, planogram geometry, `ROBOT_RADIUS_M` | `RobotState`, RAW `CameraFrame` (with `instances`), `ItemInstance`; raises `RobotError(code)` | `sim_link` (real), `catalog` (`get_planogram`/`get_product` for reset and fake geometry) |
| **routing** | `RoutingService` | `RouteRequest` (mandatory stops from in-store `ListItem`s with `qty`/`item_ids`; optional stops from recs with `interest`, `rec_id`); `GridSpec` inflated by `ROBOT_RADIUS_M` | `Route`: legs whose first/last waypoints equal the stop poses; `detour_m`/`detour_s` on every optional stop; `visited_optional`/`skipped_optional`; baseline. Also `GridSpec` | `catalog` (`get_planogram`) |
| **agent** | `AgentService` | `Intent`, `UserProfile`, read-only `Trip`, `InboundMessage` + pending `ApprovalRequest`s (+ `trip.followup.about_skus`) | `ShoppingList` (`expand_intent` unresolved, then `source_list` sets sku/sourcing/store_id/est_line_cents), `AgentReply`, `ReplyInterpretation` (feedback `sku`), follow-up `OutboundMessage` (`about_skus`) | `catalog`, `recommender`, `trips` (add_line / replan / start_checkout / update_list / get_trip), `approvals`, `events` (**publishes `agent.message`**). Never `trust.checkout` |
| **recommender** | `RecommenderService` | `UserProfile` (fixtures via `utils.data`), `ShoppingList`, `Cart`, `focus`, `exclude_skus`, `Feedback` (sku), `PurchaseRecord` | `Recommendation` (reason + signals + `sponsored=False` + `trip_id`; `price_cents` = unit price), `ProfileUpdate`, `find_user_by_handle` | `catalog` (search, deals, near-expiry, products); `utils.llm` for the rerank and the "why" |
| **trust** | `TrustService` | `IntentMandate`, `Cart` + proposed `CartLine` + `budget_cents` + `trip_id`, `CheckoutRequest` (with `approval_request`), `TransparencyEntry` (upsert on `rec_id`) | `MandateDecision` (verdict, reasons, `violations`; over cap → deny, over budget → needs_approval), `Receipt` (`stripe_test`/`visa_sandbox`/`mock`, mock `online_order_id`), `TrustLedger`, `CartMandate` | nothing (Stripe via the VIC adapter) |
| **messaging** | `MessagingService` | `OutboundMessage`, `ApprovalRequest` | `SendResult`; `InboundMessage` (with `user_id` resolved) to the `on_inbound` handler | `recommender` (`find_user_by_handle(normalize_handle(h))`, `get_profile` for `to_handle`) |
| **gateway** | provides `TripController`, `ApprovalBroker`, `EventPublisher`, `SimLink`; REST + WS from `api.md` | everything | every event except `agent.message`/`error`; `Cart.shipping_cents` via `online_shipping_cents`; `Trip.stop_status`; `TransparencyEntry.from_recommendation`; copies `Stop.detour_m` → `Recommendation.extra_distance_m`; fills `cart_hash` on photon/voice checkout answers | every service (it is the composition root) |
| **frontend** | — (Node app) | REST (`api.md`), `/ws?trip_id=` (`trip.snapshot` first, bus-wide `seq`, dedupe), `/ws/frames` (frames + full-rate `robot.state`); TS types from `make gen-types` | `api.ClientMessage` (`chat`, `approval`, `ping`); `ApprovalDecisionRequest` (echo `cart_hash` for checkout; `choices` for multi-select) | — |

## Events (all published by the gateway unless noted)

`trip.created` · `trip.snapshot` (first on connect) · `trip.state_changed` · `intent.received` · `list.ready` ·
`sourcing.split` · `recs.ready` (`recs`, `checkout_recs`) · `route.planned` · `stop.updated` · `robot.state` ·
`robot.arrived` · `scan.result` · `pick.choices` · `item.picked` · `basket.verified` · `cart.updated` ·
`approval.requested` · `approval.resolved` · `mandate.decision` · `checkout.completed` · `followup.sent` ·
`feedback.received` · `profile.updated` · `frame` (`/ws/frames` only) · `agent.message` (**agent**) · `error` (**any**).

## Failure codes to branch on

`RobotError.code` / `CommandResult.error_code`: `unknown_slot`, `no_instance` → re-source online · `grasp_failed` →
retry / attach fallback · `path_blocked`, `timeout` → re-plan or skip (`stop.updated failed`) · `cancelled` · `busy` ·
`not_ready` · `disconnected` → switch to the robot fake · `internal`. Mandate: `MandateReason` (+ `MandateViolation.line_id`).
