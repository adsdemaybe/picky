# Gateway API (contracts v0.2.1)

Served by `arms/gateway` (FastAPI, one process) on `GATEWAY_HOST:GATEWAY_PORT` (default `0.0.0.0:8000`) for **the web
app** (one app: shopper, store-staff and fleet views; there is no iPad/native app). The gateway is the only caller of
arm services; the **owner** column is the arm whose `contracts/services.py` Protocol does the work behind the
endpoint (`fleet` = `FleetService`, `cameras` = `CameraService` in the perception arm, `brain` = `RobotBrainService`
in the agent arm). Model names are from `contracts.*` (`api` = `contracts/api.py`). All money is integer
cents; all JSON uses the models' field names as-is, and derived values (`Cart.total_cents`, `CartLine.total_cents`,
`Route.extra_distance_m`, `Trip.budget_cents`, `RequestPlan.est_total_cents`, ...) are included as read-only computed
fields. Errors: `{"detail": str}`; `KeyError` → 404, rule violation (`ValueError`, e.g. restock over capacity,
invalid goal transition) → 409, invalid body / enum → 422.

**0.2.1** adds the planner → VLA → verifier loop (plan / tools mode only; `PICKY_EXEC_MODE=direct`, the default, is
the exact v0.2 behaviour): REST routes, events and the "who writes what" table in
[the loop section](#planner--vla--verifier-loop-new-in-021) at the end. Nothing above changes.

Fixed ids: robots `rover-ave-1` (Pico), `rover-ave-2` (Rhea) in `store_ave`; `rover-hill-1` (Hazel) in
`store_hill` (data/fixtures/robots.yaml). Cameras `<robot_id>.front|wrist|basket` and `<store_id>.ceiling-<n>`
(`store_ave.ceiling-1`, `store_ave.ceiling-2`, `store_hill.ceiling-1`).

**URLs in payloads** (`CameraInfo.snapshot_url` / `stream_url`, ...) are root-relative to the gateway
(`/api/cameras/...`). The Next.js dev server runs on another origin (:3000), so the web app prefixes its API base
(`NEXT_PUBLIC_API_BASE`, e.g. `http://localhost:8000`) or proxies `/api` and `/ws` to the gateway with a
`next.config` rewrite.

## Web app: how to use it

- **Chat box.** New natural-language text (no active trip, or a new request) → `POST /api/requests`. A message
  during an active trip ("add limes", "skip the cheese") → `POST /api/trips/{trip_id}/messages` or `WsChat` with
  `trip_id`. Talking to one robot from the fleet view → `POST /api/fleet/robots/{robot_id}/messages` / `WsRobotChat`.
  When a request comes back `status: "needs_input"`, send the answer as a new `POST /api/requests` with
  `in_reply_to: <request_id>`: the SAME plan is updated (same `request_id`). Past requests: `GET /api/requests?user_id=`.
- **Request-created trips start by themselves** (`Trip.auto_execute = true`): no login step, the robot just goes
  once the route is planned. `POST /api/trips/{id}/login` is only for manual / walk-up trips created with
  `POST /api/trips` (those wait in `awaiting_login`). Show a Start/Login button only when `auto_execute` is false.
- **Follow a request on ONE socket:** `/ws?request_id=<id>` (covers a split request's several trips, its goals, the
  brains' steps, approvals, cart updates), or `/ws?trip_id=a&trip_id=b`, or `/ws?user_id=maya`.
- **Sync recipe (no missed / misordered updates):** fetch REST first and read its `X-Event-Seq` response header (the
  bus `last_seq`, sent by every GET list endpoint: requests, fleet robots, goals, stock, alerts, approvals, cameras),
  then open `/ws?...&after_seq=<that>`; dedupe by `seq`. Without the header: open `/ws` first, then fetch, and apply an
  event only if its payload's `updated_at` (Goal, RobotInfo, RequestPlan) / `last_movement_at` (StockLevel) is newer.
- **`after_seq` omitted:** sockets with a `trip_id` or `request_id` filter replay those events still in the ring (and
  send a `trip.snapshot` per `trip_id` first); all other sockets (fleet / store / robot / user views, firehose) get
  LIVE events only. `after_seq=<n>` replays everything with `seq > n` still in the 1000-event ring. The robot
  "thinking log" history comes from `GET /api/fleet/robots/{id}/steps` / `GET /api/goals/{id}/steps`, never from
  replay.
- **Approvals wait for a person.** On the dev server (`make dev`) the fake messaging does not answer approvals by
  itself, so approval cards stay until the shopper clicks (`POST /api/approvals/{id}`) or texts back;
  `PICKY_SCRIPTED_SHOPPER=1` re-enables the scripted shopper (rehearsals; `make demo` always scripts).
- **Cameras:** browsers allow ~6 HTTP/1.1 connections per host and every MJPEG `<img>` holds one open, so use live
  `stream.mjpg` for at most 2–3 cameras per page (e.g. the selected robot). Build grids from `snapshot.jpg?w=320&t=<ms>`
  polling (1–2 s; served `Cache-Control: no-store`) or ONE multiplexed `WS /ws/cameras?store_id=...&w=320` (WebSockets
  don't use the HTTP connection pool).

## REST — natural-language requests + fleet (new in v0.2)

| Method | Path | Request | Response | Owner |
|---|---|---|---|---|
| POST | `/api/requests` | `fleet.FleetRequest` `{text, user_id, channel?, store_id?, role?, in_reply_to?}` | `fleet.RequestPlan` (interpretation, `store_choices` with reasons, `goals`, `trip_ids`, `budget_split`, `reply`; execution continues async, watch `/ws?request_id=`). Shopper `status` / `chat` intents: reply only (`completed`); shopper fetch / shelf_scan / return_to_dock → `rejected`; ambiguous → `needs_input` (`reply` = the question) | fleet → agent.interpret_request |
| GET | `/api/requests` | query: `user_id?`, `limit=20` | `list[fleet.RequestPlan]` (newest first; the chat history) | fleet (`list_requests`) |
| GET | `/api/requests/{request_id}` | – | `fleet.RequestPlan` (current status) | fleet |
| GET | `/api/fleet/robots` | query: `store_id?` | `list[fleet.RobotInfo]` | fleet |
| GET | `/api/fleet/robots/{robot_id}` | – | `fleet.RobotInfo` (404 unknown) | fleet |
| GET | `/api/fleet/robots/{robot_id}/brain` | – | `fleet.BrainStatus` (mode `llm`/`scripted`, model, current tool, pending messages) | brain (`status`) |
| GET | `/api/fleet/robots/{robot_id}/steps` | query: `goal_id?`, `limit=50`, `before?` (ISO ts; page with the last row's `ts`) | `list[fleet.BrainStep]` (NEWEST first; the "what is it thinking" history) | brain (`recent_steps`) |
| POST | `/api/fleet/robots/{robot_id}/messages` | `api.RobotMessageRequest` `{text, user_id?}` | `fleet.BrainReply` (talk to that robot's brain; steps also stream as `brain.step`) | fleet → brain |
| POST | `/api/fleet/robots/{robot_id}/goals` | `api.CreateGoalRequest` (= `fleet.GoalRequest`: no store_id / robot_id needed; the path pins the robot; the gateway fills `user_id` from the caller) | `fleet.Goal` | fleet (`create_goal(body.to_spec(robot_id, user))`) |
| POST | `/api/fleet/robots/{robot_id}/stop` | `api.StopRobotRequest` (optional) | `fleet.RobotInfo` (halted; active goal cancelled, its questions withdrawn; the robot is then HELD: `available=false`, `error` says why; its queued goals move to another eligible robot in the store, or wait (pinned / no other robot); nothing starts on it until a manual goal for it or `/resume`) | fleet → robot |
| POST | `/api/fleet/robots/{robot_id}/resume` | – | `fleet.RobotInfo` (releases a stopped robot: its queue runs again) | fleet (`resume_robot`, gateway extra) |
| POST | `/api/goals` | `fleet.GoalSpec` (`store_id` and/or `robot_id`; `robot_id` omitted = the fleet picks with `choose_robot`) | `fleet.Goal` | fleet (`create_goal`) |
| GET | `/api/goals` | query: `store_id?`, `robot_id?`, `status?` (repeatable = OR), `active?` (`true` = pending/assigned/active/waiting), `kind?`, `request_id?`, `trip_id?`, `limit=50` | `list[fleet.Goal]` (newest first) | fleet |
| GET | `/api/goals/{goal_id}` | – | `fleet.Goal` (items, progress, assignments with score breakdown) | fleet |
| GET | `/api/goals/{goal_id}/steps` | query: `limit=50`, `before?` | `list[fleet.BrainStep]` (newest first; spans robots after a reassignment) | brain (`recent_steps(goal_id=)`) |
| POST | `/api/goals/{goal_id}/cancel` | `api.CancelGoalRequest` (optional) | `fleet.Goal` | fleet |
| GET | `/api/sim/status` | – | `sim.SimStatus` (Isaac connected?, hosted stores/robots/cameras, which stores/robots run on Isaac vs the 2D fake) | gateway (SimLink) + robot |

## REST — categories + stock management (new in v0.2)

| Method | Path | Request | Response | Owner |
|---|---|---|---|---|
| GET | `/api/categories` | query: `store_id?` | `list[catalog.CategoryInfo]` (flat taxonomy, counts per store) | catalog |
| GET | `/api/categories/{category_id}/products` | query: `store_id?` | `list[catalog.SearchHit]` (404 unknown category) | catalog |
| GET | `/api/stores/{store_id}/stock` | query: `status?` (`ok\|low\|out`), `category?` (matches `Product.category` OR `subcategory`), `q?` (sku / name / brand substring) | `list[catalog.StockLevel]` (worst first; `status`, `fill_ratio` computed; `unit` / `size` / `image_url` for display) | catalog |
| GET | `/api/stores/{store_id}/stock/alerts` | – | `list[catalog.StockAlert]` (active low/out, out first) | catalog |
| POST | `/api/stores/{store_id}/stock/restock` | `catalog.RestockRequest` `{sku, qty?, slot_id?, expiry?, actor?}` | `catalog.StockChangeResult` (409 if `qty` exceeds the free capacity; nothing applied). The simulated shelves are synced (`robot.sync_stock`) | catalog |
| POST | `/api/stores/{store_id}/stock/adjust` | `catalog.StockAdjustment` `{sku, delta \| set_qty, reason, actor, slot_id?, note?}` — `reason` must be in `catalog.STAFF_STOCK_REASONS` (`damage`, `manual`; else 422) | `catalog.StockChangeResult` (409: below 0 / above capacity / `set_qty` without `slot_id` on a multi-slot SKU) | catalog |
| GET | `/api/stores/{store_id}/stock/history` | query: `sku?`, `limit=100`, `before?` (ISO ts), `reason?`, `actor?` (exact or prefix like `robot:`) | `list[catalog.StockMovement]` (newest first; robot picks + scans included) | catalog |

## REST — cameras (new in v0.2; every frame is face-blurred before it is served)

| Method | Path | Request | Response | Owner |
|---|---|---|---|---|
| GET | `/api/cameras` | query: `store_id?`, `robot_id?` | `list[cameras.CameraInfo]` (incl. root-relative `snapshot_url`, `stream_url`) | cameras |
| GET | `/api/cameras/{camera_id}/snapshot.jpg` | query: `w?` (scale to that width), `t?` (cache buster, ignored) | `image/jpeg`, `Cache-Control: no-store` (blurred; 404 unknown camera, 503 no frame) | cameras (`snapshot(id, max_width=w)`) |
| GET | `/api/cameras/{camera_id}/stream.mjpg` | query: `fps=5`, `w?` | `multipart/x-mixed-replace; boundary=frame` MJPEG — works as `<img src>`; ≤ 2–3 per page | cameras (`stream`) |
| POST | `/api/cameras/{camera_id}/observe` | `api.ObserveRequest` `{question?}` | `cameras.VisionObservation` (snapshot → blur → vision) | cameras |

## REST — shopper trips, profiles, trust (v0.1, unchanged unless noted)

| Method | Path | Request | Response | Owner |
|---|---|---|---|---|
| GET | `/health` | – | `api.HealthStatus` (+ `robots`) | gateway |
| GET | `/api/stores` | – | `list[catalog.StoreInfo]` (physical stores + `online` with shipping fields) | catalog |
| GET | `/api/stores/{store_id}/planogram` | – | `planogram.Planogram` | catalog |
| GET | `/api/stores/{store_id}/grid` | – | `routing.GridSpec` | routing |
| GET | `/api/products/search` | query: `q`, `store_id?` (repeatable), `category?`, `dietary?` (repeatable), `in_stock_only?`, `limit?` → `catalog.SearchQuery` | `list[catalog.SearchHit]` | catalog |
| GET | `/api/products/{sku}` | query: `store_id?` (repeatable) | `catalog.SearchHit` (`{"product", "offers", "score"}`) | catalog |
| GET | `/api/stores/{store_id}/deals` | – | `list[catalog.Deal]` | catalog |
| GET | `/api/stores/{store_id}/near-expiry` | query: `within_days=3` | `list[catalog.NearExpiryItem]` | catalog |
| POST | `/api/scans` | `perception.ScanReport` | `catalog.ScanApplyResult` | catalog |
| GET | `/api/users/{user_id}/profile` | – | `profile.UserProfile` | recommender |
| PUT | `/api/users/{user_id}/profile` | `profile.UserProfile` | `profile.UserProfile` | recommender |
| POST | `/api/users/{user_id}/feedback` | `profile.Feedback` | `profile.ProfileUpdate` | recommender |
| GET | `/api/users/{user_id}/mandate` | – | `trust.IntentMandate` (404 if none) | trust |
| PUT | `/api/users/{user_id}/mandate` | `trust.IntentMandate` | `trust.IntentMandate` | trust |
| POST | `/api/trips` | `api.CreateTripRequest` (v0.2: optional `shopping_list`, `request_id`, `robot_id`, `auto_execute`) | `trip.Trip` (planning continues async; watch `/ws`; the in-store part runs as a fleet shop goal) | gateway → agent |
| GET | `/api/trips?user_id=` | – | `list[trip.Trip]` | gateway |
| GET | `/api/trips/{trip_id}` | – | `trip.Trip` | gateway |
| POST | `/api/trips/{trip_id}/login` | `api.RobotLoginRequest` (`robot_id?`: the robot the shopper is at) | `trip.Trip` (manual trips only: state → `executing` → `fleet.execute_trip`; a no-op for `auto_execute` trips) | gateway → fleet |
| POST | `/api/trips/{trip_id}/messages` | `api.PostMessageRequest` | `tools.AgentReply` | agent |
| POST | `/api/trips/{trip_id}/voice` | raw audio body (`Content-Type: audio/webm\|wav\|mpeg`) | `api.VoiceTurnResponse` (Deepgram STT → agent → TTS; no key → 503 and the frontend uses Web Speech + `WsChat` with `channel="voice"`) | agent |
| GET | `/api/approvals` | query: `trip_id?`, `goal_id?`, `robot_id?`, `store_id?`, `user_id?` (all optional, ANDed; staff view: `?user_id=staff&store_id=`) | `list[trust.ApprovalRequest]` (pending) | gateway (`ApprovalBroker.pending`) |
| POST | `/api/approvals/{approval_id}` | `api.ApprovalDecisionRequest` (`decision`, `choice?`, `choices?`, `cart_hash?`, `note?`) | `trust.ApprovalResponse` (409 if a checkout approval's `cart_hash` doesn't match) | gateway (ApprovalBroker) |
| POST | `/api/trips/{trip_id}/checkout` | – | `api.CheckoutStatus` (= `TripController.start_checkout`; payment runs after approval) | gateway → trust |
| POST | `/api/trips/{trip_id}/followup` | – | `messaging.OutboundMessage` (sends the post-purchase question now; demo cue) | gateway → agent + messaging |
| GET | `/api/merchant/{store_id}/dashboard` | – | `catalog.MerchantDashboard` | catalog |
| GET | `/api/trust/ledger` | query: `user_id?`, `trip_id?` | `trust.TrustLedger` | trust |
| POST | `/api/dev/inbound` | `messaging.InboundMessage` | `messaging.ReplyInterpretation` (simulates a Photon text; dev only). The text goes to `trip_id` if set, else the approval its `in_reply_to` asked, else the user's OLDEST pending texted approval across all live trips, else their newest live trip | messaging → agent |
| GET | `/api/_schema/events` | – | JSON Schema of `events.Event` (`events.event_json_schema()`, for `make gen-types`) | gateway |

## WebSockets

| Path | Direction | Messages | Owner |
|---|---|---|---|
| `WS /ws[?trip_id=&store_id=&robot_id=&user_id=&request_id=][&after_seq=<n>]` | server → client | `events.Event` (JSON, discriminated on `type`; envelope `trip_id`, `store_id`, `robot_id`, `user_id`, `request_id`, `ts`, `seq`, `source`). Filters (`events.event_matches`): each key may repeat (OR within a key), keys AND together; a null envelope field never matches; no filter = everything (fleet/staff views). With `trip_id`: one `trip.snapshot` per trip first. Replay: see "`after_seq` omitted" above. `seq` is bus-wide (gaps are normal); dedupe by `seq`. `robot.state` throttled to ≤ 2 Hz per robot here. Never carries `frame` / `camera.frame`. Every event about a shop goal carries `trip_id` (so `?trip_id=` sees the brain's `pick.choices`, `item.picked`, `brain.step`, ...). | gateway |
| `WS /ws` | client → server | `api.ClientMessage`: `{"type":"chat","text":...,"trip_id"?}` (trip_id or exactly one trip_id filter), `{"type":"approval","response":ApprovalResponse}`, `{"type":"robot_chat","robot_id":...,"text":...}` (talk to a robot brain), `{"type":"ping"}` | gateway |
| `WS /ws/frames?trip_id=\|robot_id=` | server → client | Live-only, latest-only, `seq=0`, never replayed: `camera.frame` for EVERY camera of the robot (always `blurred=true`) and full-rate `robot.state`. Legacy `frame` (front camera only) is sent only to clients that pass `?legacy=1`, never alongside a `camera.frame` of the same image. | gateway |
| `WS /ws/cameras/{camera_id}[?fps=5&w=]` | server → client | Live-only `camera.frame` events for one camera (blurred, latest-only). Same frames as the MJPEG stream. | cameras |
| `WS /ws/cameras?camera_id=a&camera_id=b \| store_id= \| robot_id=[&fps=2&w=320]` | server → client | Multiplexed live-only `camera.frame` events keyed by `camera_id` (camera grid on one socket; fps capped at 5 per camera). | gateway + cameras |
| `WS /ws/sim` | sim worker → gateway | `sim.SimMessage`, multiplexed by `robot_id` / `store_id`: first `hello` (auth = `SimHello.token`; lists `stores` (+ `loaded`, `data_hash`), `robots`, `cameras`; version checked), then `result` (one per command, echoes `robot_id`, `error_code` on failure), `robot_state` (per robot), `camera_frame` (raw; only cameras chosen by `set_streams`), `instances`, `hosted` (full hosting lists after any reset), `log`. Parse with `sim.parse_sim_message` (drops unknown keys at any depth). | gateway ↔ robot |
| `WS /ws/sim` | gateway → sim worker | `sim.GatewayToSim`: `welcome`, then `navigate`, `scan`, `pick`, `place_in_basket`, `unload_basket`, `capture` (`camera_id` for any camera incl. ceiling), `list_instances`, `reset` (one robot, or `robot_id=null` = rebuild the whole store), `stop` (`robot_id=null` = all), `set_streams`, `set_slot_stock` — each with `cmd_id`; `robot_id` per command (no wire default); one motion command at a time PER ROBOT. A command the worker cannot parse is answered with `sim.command_error_result`. | robot (via `SimLink`) |

## Event types (`contracts/events.py`)

One producer per type (`events.EVENT_PRODUCERS`): **gateway** (trip flow, `robot.state`, live frames), **fleet**
(`request.*`, `goal.*`, `fleet.robot_updated`), **agent** (`agent.message`; the robot brain: `brain.step` + the
in-store execution events `robot.arrived`, `scan.result`, `pick.choices`, `item.picked`, `basket.verified`),
**catalog** (`stock.*`), **any** (`error`); 0.2.1 loop events: **agent** (the LoopRunner: `plan.*`, `step.*`,
`vla.*`, `target.grounded`, `camera.overlay`) and **robot** (`demo.fault`, the FaultInjector). Envelopes: shop-goal
events `events.goal_envelope(goal)` (trip_id = goal.trip_id), loop events `events.plan_envelope(plan)` (same keys),
trip events `events.trip_envelope(trip)`, request events `request_id` + `user_id`.

| Event | Producer | When |
|---|---|---|
| `request.received` | fleet | NL request accepted (`request`; envelope `request_id`, `user_id`) |
| `request.planned` | fleet | interpretation + store choice + goals ready (`plan`); re-sent on every status change (needs_input answer, executing, completed / failed) |
| `goal.created` | fleet | goal created |
| `goal.assigned` | fleet | robot (re)assigned (`assignment`: score breakdown + reason + candidates; `attempt` > 1 = reassignment) |
| `goal.updated` | fleet | status (pending/assigned/active/waiting), progress note, items (incl. added ones), scanned slots |
| `goal.completed` / `goal.failed` | fleet | terminal (`goal.failed` also for cancelled) |
| `fleet.robot_updated` | fleet | robot view changed (`robot`: status, availability, battery, goal, brain state) |
| `brain.step` | agent (brain) | a robot brain thought / tool call / tool result / observation / message (`step.step_no` = per-robot counter; `seq` = bus) |
| `stock.updated` | catalog | a SKU's level changed (`level`, `movement`) |
| `stock.alert` | catalog | a SKU went low/out (`alert.active`) or was restocked (`active=false`) |
| `camera.frame` | gateway | live camera frame (`/ws/cameras*`, `/ws/frames` only; blurred) |

v0.1 events (producer gateway unless noted):

| Event | When |
|---|---|
| `trip.created` | trip created (POST /api/trips or the fleet) |
| `trip.snapshot` | first message on every `/ws?trip_id=` connect / resync |
| `trip.state_changed` | every TripState transition |
| `intent.received` | intent text accepted |
| `list.ready` | `agent.expand_intent` done (or list edited / item added) |
| `sourcing.split` | `agent.source_list` done: in-store / online / unresolved |
| `recs.ready` | `recs` (detours, near-expiry) + `checkout_recs` |
| `route.planned` | route solved (`replan=true` mid-trip) |
| `stop.updated` | a stop changed status (`Trip.stop_status`; the brain calls `TripController.set_stop_status`) |
| `robot.state` | pose/status/battery per robot (throttled on `/ws`) |
| `robot.arrived` | **agent (brain)**: reached a stop / slot (`stop_id` null for non-trip goals) |
| `scan.result` | **agent (brain)**: perception ScanReport for a slot |
| `pick.choices` | **agent (brain)**: produce top-3 for the shopper |
| `item.picked` | **agent (brain)**: a unit in the basket |
| `basket.verified` | **agent (brain)**: basket check after a drop (expected = units this robot placed) |
| `cart.updated` | any cart change (totals + budget) |
| `approval.requested` / `approval.resolved` | ApprovalBroker (also when withdrawn: decision `timeout`, channel `system`) |
| `mandate.decision` | trust evaluation shown to the shopper |
| `checkout.completed` | payment attempted: `receipt.status` succeeded / declined / error |
| `followup.sent` | post-purchase question sent (`message.about_skus`) |
| `feedback.received` / `profile.updated` | feedback → profile change |
| `frame` | legacy live view (on `/ws/frames?legacy=1` only) |
| `agent.message` | agent chat (streaming deltas) |
| `error` | degraded/failed step |

## Trip state machine (`shopping.TripState`, transitions in `shopping.TRIP_TRANSITIONS`)

`intent → list_ready → recs_ready → route_planned → [awaiting_login →] executing ⇄ awaiting_approval → checkout →
paid → followup → done` (+ `failed` from any non-terminal state). `auto_execute` trips (created by the fleet) skip
`awaiting_login` (`route_planned → executing`); manual trips wait there for `POST /login`. Pre-trip edits go back to
`list_ready` from `list_ready`, `recs_ready`, `route_planned` or `awaiting_login`; `executing → route_planned` on a
mid-trip re-plan. During `executing` the in-store part is a fleet **shop goal** run by a robot brain
(`FleetService.execute_trip`); its result decides what happens next: goal `completed` → checkout; `failed` →
checkout if the cart has active lines (plus an `error` event), else trip `failed`; `cancelled` → trip `failed` with
the reason (no checkout).

## Goal lifecycle (`fleet.GoalStatus`, transitions in `fleet.GOAL_TRANSITIONS`)

`pending → assigned → active ⇄ waiting → completed | failed | cancelled`; `active/waiting → pending/assigned` when
the robot fails and the fleet reassigns (≤ `fleet.MAX_GOAL_ATTEMPTS` attempts); same-status updates are no-ops. The
fleet sets `active` right before `run_goal`; the brain sets `waiting`, `active` (after the answer), `completed`,
`failed`. Robot choice = `fleet.choose_robot` (deterministic: idle 0.5, battery 0.3, distance 0.2; same store only;
battery < 20% only docks; a robot with a queued / pre-assigned goal is not idle). Shop goal items =
`fleet.goal_items_from_trip(trip)` (one per route stop, `item_id = stop_id`).

## Web app screens → endpoints + events

| Screen / component | REST | Live (`/ws` events unless noted) |
|---|---|---|
| Chat box + RequestPlanView | `POST /api/requests` (+ `in_reply_to`), `GET /api/requests?user_id=`, `GET /api/requests/{id}` | `/ws?request_id=`: `request.*`, `goal.*`, `trip.*`, `cart.updated`, `approval.*` |
| Trip view (map, route, cart, budget) | `GET /api/trips/{id}`, `GET /api/stores/{id}/planogram`, `/grid`, `POST /api/trips/{id}/messages`, `/checkout` | `/ws?trip_id=`: `trip.snapshot`, `route.planned`, `stop.updated`, `robot.state`, `cart.updated`, `mandate.decision`, `pick.choices`, `item.picked`, `checkout.completed` |
| Approvals / questions | `GET /api/approvals?...`, `POST /api/approvals/{id}` | `approval.requested` / `approval.resolved` (or `WsApproval`) |
| FleetOverview (robots per store) | `GET /api/fleet/robots[?store_id]`, `GET /api/sim/status` | `/ws?store_id=`: `fleet.robot_updated`, `goal.*` |
| RobotPanel + ThinkingLog | `GET /api/fleet/robots/{id}`, `/brain`, `/steps`, `POST .../messages`, `.../goals`, `.../stop` | `/ws?robot_id=`: `brain.step`, `goal.*`, `fleet.robot_updated` |
| Goals table | `GET /api/goals?active=true&store_id=`, `GET /api/goals/{id}`, `/steps`, `POST /api/goals`, `/cancel` | `goal.*` |
| CameraGrid / CameraView | `GET /api/cameras`, `snapshot.jpg?w=&t=`, `stream.mjpg` (≤ 2–3), `POST .../observe` | `WS /ws/cameras?store_id=&w=` (`camera.frame`) |
| Categories | `GET /api/categories[?store_id]`, `GET /api/categories/{id}/products` | – |
| StockTable / Alerts / Restock / History | `GET /api/stores/{id}/stock?status=&category=&q=`, `/stock/alerts`, `POST /stock/restock`, `POST /stock/adjust`, `GET /stock/history` | `/ws?store_id=`: `stock.updated`, `stock.alert` |
| Merchant dashboard | `GET /api/merchant/{store_id}/dashboard` | `stock.*`, `scan.result` |


## Planner → VLA → verifier loop (new in 0.2.1)

Additive. Every route is served by `arms/gateway` and backed by an OPTIONAL `Services` field (`runner`, `planner`,
`vla`, `verifier`, `evidence`, `truth`, `faults`; factories in `services.LOOP_SERVICE_FACTORIES`). Same error rules
(`KeyError` → 404, `ValueError` → 409, bad body → 422); money is integer cents; frames are never inlined: payloads
carry `evidence.FrameRef` (`url` = `/api/evidence/{frame_id}.jpg`, always face-blurred, retained ≥ 10 min). Design,
sequence diagram and per-arm plan: `docs/vla-loop/` (`api.md`, `IMPLEMENTING.md`); what changed: `CHANGES-0.2.1.md`.

**Kill switch.** `PICKY_EXEC_MODE` defaults to `direct`. `services.exec_mode(services, settings.picky_exec_mode)`
returns `plan` / `tools` only when that mode is configured AND planner, runner, vla and verifier are all wired.

| mode | who drives | what changes |
|---|---|---|
| `direct` | v0.2 brain tools call `RobotService` | nothing (no loop events) |
| `plan` | `RobotBrainService.run_goal` → `LoopRunner.run_goal` (planner → StepPlan → loop), then the brain finishes the goal with `bridge.goal_update_from_outcome` | full loop events; effects committed by the runner after verification |
| `tools` | v0.2 LLM tool loop; motion tools → `LoopRunner.run_tool_step` | one live plan per goal; v0.2 side effects applied by the executor after a VERIFIED outcome (`docs/vla-loop/tools.md`) |

**Providers.** Planner LLM = Gemini via `utils.llm` (default `gemini-3.8-flash`; `PICKY_LLM_PROVIDER=anthropic`
optional); verifier vision = **Perceptron** (`perceptron-mk1.5`, fallback `perceptron-mk1`) behind
`services.VisionProvider`, which rejects unblurred frames. No key → scripted planner / fake verifier (ground truth).

**Version.** 0.2.1 is a patch bump: the deployed Isaac worker (0.2.0) still passes the `/ws/sim` major.minor check.
`vla.RunSkillCmd` / `SkillProgress` / `PolicyServerStatus` (T1 on vhagar) are inert until 0.3.0 + a worker rebuild.

### REST

P0 = needed for the demo. P1 = nice to have. Dev routes exist only with `PICKY_DEV=1`.

| Pri | Method | Path | Request | Response | Owner |
|---|---|---|---|---|---|
| P0 | GET | `/api/goals/{goal_id}/plan` | – | `plan.StepPlan` (latest revision; tools mode: the live plan; 404 = direct mode / no plan yet) | runner (`current_plan`) |
| P0 | GET | `/api/plans/{plan_id}` | – | `plan.StepPlan` (any revision, incl. superseded) | runner (`get_plan`) |
| P0 | GET | `/api/plans/{plan_id}/run` | – | `policy.PlanRunState` (current step, attempts, breakers, effect ledger, `provisional_open`, local cart) | runner (`run_state`) |
| P0 | POST | `/api/plans/{plan_id}/cancel` | `{reason?}` | `policy.PlanRunState` (status `cancelled`; the running exec is cancelled, robot stopped, pending question withdrawn, `holding_instance_id` = a unit left in the gripper) | goal plans: `fleet.cancel_goal(plan.goal_id)` (the fleet owns goal status + the brain task); goal-less dev plans: runner (`cancel`) |
| P0 | GET | `/api/evidence/{frame_id}.jpg` | query `w?` | `image/jpeg` (blurred, retained ≥ 10 min; 404 expired) | evidence (`get_frame`; bytes only, never the ground-truth `instances`) |
| P1 | GET | `/api/plans/{plan_id}/attempts` | query `step_id?` | `list[policy.StepAttempt]` (pre / VLA terminal / post / failure / decision / effects / fault_ids) | runner (`attempts`) |
| P1 | GET | `/api/fleet/robots/{robot_id}/vla` | – | `list[vla.VlaCapabilities]` (robot VLA status: every tier with `available` + `reason`) | vla (`capabilities`) |
| P1 | GET | `/api/vla/execs/{exec_id}` | – | `vla.ExecRecord` (request + every status) | vla (`get_exec`) |
| P1 | POST | `/api/vla/execs/{exec_id}/cancel` | `{reason?}` | `vla.VlaStatus` (terminal; idempotent) | vla (`cancel`) |
| P1 | GET | `/api/verifier` | – | `verify.VerifierCapabilities` (mode, methods, provider, healthy) | verifier (`capabilities`) |
| P1 | PUT | `/api/verifier/mode` | `{mode: decide\|shadow\|gt_only}` | `verify.VerifierCapabilities` | verifier (`set_mode`) |
| P1 | GET | `/api/verifier/providers` | – | `list[verify.ProviderInfo]` ("Vision by Perceptron Mk1.5 · 42 calls · p50 610 ms · 97% agree with sim") | verifier |
| P1 | POST | `/api/verify` | `verify.VerifyNowRequest` `{robot_id, preset: gripper\|basket\|shelf \| condition, sku?, slot_id?, counts?}` | `verify.Verdict` (the "check the basket" judge button) | verifier |
| P1 | GET | `/api/evidence/{frame_id}/annotations` | – | `evidence.FrameAnnotations` (boxes / points / texts drawn on that frame) | gateway (from verdicts + groundings) |
| dev | GET | `/api/dev/faults` | – | `{scenario, rules: list[faults.FaultRule], firings: list[faults.FaultFiring]}` | faults |
| dev | POST | `/api/dev/faults` | `faults.FaultRule` | `faults.FaultRule` | faults (`inject`) |
| dev | POST | `/api/dev/faults/scenario` | `{name}` (a `faults.DEMO_SCENARIOS` key) or a full `faults.FaultScenario` | `faults.FaultScenario` | faults (`load_scenario`) |
| dev | DELETE | `/api/dev/faults` | – | `204` | faults (`clear_faults`) |
| dev | POST | `/api/dev/plans` | `plan.PlanRequest` `{robot_id, text \| goal, store_id?, budget_cents?}` | `plan.StepPlan` (planned and started on that robot, bypassing the fleet; for testing T0 / faults) | planner + runner |

Demo scenarios (`PICKY_FAULTS=<name>` at boot or `POST /api/dev/faults/scenario`): `drop_on_first_grasp` (THE demo
moment), `slow_vla`, `verifier_down`, `flaky_t1`, `isaac_drop`, `wrong_salsa` (2D fake only), `foggy_basket`.

### WebSockets

No new socket. The loop events go on the sequenced `/ws` bus with `events.plan_envelope(plan)` (the `goal_envelope`
keys), so `/ws?trip_id=|request_id=|robot_id=|store_id=|user_id=` filters and the replay ring work unchanged.
`camera.overlay` is live-only (`events.LIVE_ONLY_EVENT_TYPES`): `/ws/cameras/{camera_id}`, `/ws/cameras` and
`/ws/frames`, never sequenced or replayed. `vla.status` is throttled to ≤ 2 Hz per exec on `/ws`
(`events.THROTTLED_EVENT_TYPES`); phase changes (accepted / succeeded / failed / cancelled) always go out.

### Events (`contracts/events.py`, `events.LOOP_EVENT_TYPES`)

Producer: the runner (agent arm) for every type except `demo.fault` (the fault injector, robot arm). The VLA service
and the verifier publish nothing. In plan mode the v0.2 events still fire from the same runs: `robot.arrived`,
`scan.result`, `pick.choices` (produce top-3 with the `approval_id` of the choice card), `item.picked`,
`basket.verified`, `goal.updated` (fleet), `cart.updated` and the route stop statuses (the runner calls
`trips.set_stop_status` navigating → arrived → done / skipped / declined / failed per `bridge.ITEM_TO_STOP_STATUS`).
The runner also mirrors one `brain.step(kind="status")` per step transition (`bridge.mirror_brain_step`), so the
v0.2 ThinkingLog keeps working.

| Pri | type | payload | when / UI |
|---|---|---|---|
| P0 | `plan.created` | `plan: StepPlan` (~1 KB per step) | plan ready → the step list, grouped by `group_id` |
| P0 | `step.started` | `plan_id, step: PlanStep, attempt, exec_id?, tier?, inserted_by?` | row spinner + tier chip; tools mode: appends the row |
| P0 | `vla.status` | `plan_id, step_id, status: VlaStatus` | progress bar + stage ("reaching", chunk 12) |
| P0 | `step.verified` | `plan_id, step_id, attempt, phase: pre\|post\|verify\|reverify, verdict: Verdict, caught_vla_false_success` | green / red / amber check, reason, evidence overlay on `verdict.frames[*].url`, `decided_by` + `provider.attribution` badge, `degraded` = amber |
| P0 | `step.failed` | `plan_id, step_id, failure: StepFailure, decision: RecoveryDecision, attempts_left` | the moment card: `decision.reason` = "Verifier caught it: gripper is empty (expected TORT-CORN); re-finding the item and retrying (attempt 2/3)" |
| P0 | `step.completed` | `plan_id, step_id, status, attempts, effects: list[EffectRecord], note` | row done; effects ("1 × salsa added"); `provisional` = amber |
| P0 | `plan.completed` | `plan_id, status: completed\|failed\|cancelled, summary, stats: LoopMetrics` | "Done in 2:41 · 83 steps · 1 verifier catch · 1 retry" |
| P0 | `plan.revised` | `plan, previous_plan_id, reason, failure?` | replace the list from the first open step (out of stock → approved substitute) |
| P1 | `target.grounded` | `plan_id, step_id, grounding: GroundedTarget` | box + grasp point on the evidence frame |
| P1 | `vla.tier_changed` | `from_tier, to_tier, reason, breaker` | banner "switched to the scripted grasp" (right after the `step.failed` that caused it) |
| P1 | `demo.fault` | `firing: FaultFiring, scenario` | presenter / staff view only |
| P1 | `camera.overlay` | `camera_id, frame: FrameRef, evidence, label, ttl_ms, step_id` | live-only overlay on a camera tile |

### Who writes what (single writer per record)

| Record | Writer | When | Never |
|---|---|---|---|
| Plan, step status, attempts, effect ledger | runner (agent) | every transition | VLA, verifier |
| Robot motion | VLA service (robot) via `RobotService` | inside one exec | runner, verifier |
| Verdicts, groundings | verifier (perception) | returned as data | no events, no writes |
| Stock (`catalog.record_pick` / `apply_scan`) | runner, once per effect key, after a VERIFIED place / scan | `policy.COMMIT_ON_VERIFIED` | on a grasp, on a VLA claim, on a recovery place |
| Goal items + progress (`fleet.update_goal(items=…)`) | runner | after each verified place / item close | goal status |
| Goal status (completed / failed) | brain (`bridge.goal_update_from_outcome`) | after `LoopRunner.run_goal` returns | runner |
| Goal cancel | fleet (`cancel_goal`) | stop / cancel requests | runner, gateway directly |
| Cart lines (`trips.add_line`) | runner, once per item (`qty = picked`; partial on close; online for the rest) | last unit verified / item closed | per unit |
| Route stop status (`trips.set_stop_status`) | runner | navigating / arrived / item terminal | – |
| Questions (`trips.ask` / `approvals.request`) | runner; withdrawn with `approvals.cancel` on cancel | ask steps, recoveries | VLA, verifier |
| Checkout (`start_checkout`) | gateway trip flow, only when `PlanRunState.provisional_open` is false | after the goal ends | runner |

Tools mode keeps the v0.2 executor as the writer of stock / cart / goal items (after a verified `ToolStepOutcome`).
A unit in the gripper is never bought (nothing commits on a grasp): a grasp pre-check that finds the target SKU adopts
it (`adopt_held`), any other SKU is put back first (`return_held`; T0 has no place-back → rule `cannot_return` asks,
then escalates). A place accepted on an uncertain basket check commits with `provisional=true` (cart line status
`picked`), and the plan's basket audit (`VerifyStep purpose="audit"`) runs before the checkout handoff.

### Web app screens → endpoints + events (loop additions)

| Screen / component | REST | Live |
|---|---|---|
| PlanPanel (steps grouped per item, attempt chips) | `GET /api/goals/{id}/plan`, `GET /api/plans/{id}/run` | `plan.created`, `plan.revised`, `step.started`, `step.completed` |
| VLA progress bar + tier chip | `GET /api/fleet/robots/{id}/vla` | `vla.status`, `vla.tier_changed` |
| Moment card ("Verifier caught it") | `GET /api/plans/{id}/attempts?step_id=` | `step.failed` with `failure.caught_by_verifier`, `step.verified` with `caught_vla_false_success` |
| Evidence viewer | `GET /api/evidence/{frame_id}.jpg`, `/annotations` | `step.verified.verdict.evidence`, `target.grounded`, `camera.overlay` |
| Verifier badge / mode switch | `GET /api/verifier`, `PUT /api/verifier/mode`, `GET /api/verifier/providers` | `verdict.decided_by`, `verdict.provider.attribution`, `verdict.degraded` |
| Judge button "check the basket" | `POST /api/verify` (preset `basket`) | – |
| Presenter panel | `GET/POST/DELETE /api/dev/faults`, `POST /api/dev/faults/scenario` | `demo.fault` |
