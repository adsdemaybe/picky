# Gateway API (contracts v0.1.0)

Served by `arms/gateway` (FastAPI, one process) on `GATEWAY_HOST:GATEWAY_PORT` (default `0.0.0.0:8000`). The gateway
is the only caller of arm services; the **owner** column is the arm whose `contracts/services.py` Protocol does the
work behind the endpoint. Model names are from `contracts.*` (`api` = `contracts/api.py`). All money is integer
cents; all JSON uses the models' field names as-is, and derived values (`Cart.total_cents`, `CartLine.total_cents`,
`Route.extra_distance_m`, `Trip.budget_cents`, ...) are included as read-only computed fields. Errors:
`{"detail": str}` with 4xx/5xx (FastAPI default).

## REST

| Method | Path | Request | Response | Owner |
|---|---|---|---|---|
| GET | `/health` | – | `api.HealthStatus` | gateway |
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
| POST | `/api/trips` | `api.CreateTripRequest` | `trip.Trip` (planning continues async; watch `/ws`) | gateway → agent |
| GET | `/api/trips?user_id=` | – | `list[trip.Trip]` | gateway |
| GET | `/api/trips/{trip_id}` | – | `trip.Trip` | gateway |
| POST | `/api/trips/{trip_id}/login` | `api.RobotLoginRequest` | `trip.Trip` (state → `executing`) | gateway → robot |
| POST | `/api/trips/{trip_id}/messages` | `api.PostMessageRequest` | `tools.AgentReply` | agent |
| POST | `/api/trips/{trip_id}/voice` | raw audio body (`Content-Type: audio/webm\|wav\|mpeg`) | `api.VoiceTurnResponse` (Deepgram STT → agent → TTS; no key → 503 and the frontend uses Web Speech + `WsChat` with `channel="voice"`) | agent |
| GET | `/api/approvals?trip_id=` | – | `list[trust.ApprovalRequest]` (pending) | gateway |
| POST | `/api/approvals/{approval_id}` | `api.ApprovalDecisionRequest` | `trust.ApprovalResponse` (409 if a checkout approval's `cart_hash` doesn't match) | gateway (ApprovalBroker) |
| POST | `/api/trips/{trip_id}/checkout` | – | `api.CheckoutStatus` (= `TripController.start_checkout`; payment runs after approval) | gateway → trust |
| POST | `/api/trips/{trip_id}/followup` | – | `messaging.OutboundMessage` (sends the post-purchase question now; demo cue) | gateway → agent + messaging |
| GET | `/api/merchant/{store_id}/dashboard` | – | `catalog.MerchantDashboard` | catalog |
| GET | `/api/trust/ledger` | query: `user_id?`, `trip_id?` | `trust.TrustLedger` | trust |
| POST | `/api/dev/inbound` | `messaging.InboundMessage` | `messaging.ReplyInterpretation` (simulates a Photon text; dev only) | messaging → agent |
| GET | `/api/_schema/events` | – | JSON Schema of `events.Event` (`events.event_json_schema()`, for `make gen-types`) | gateway |

## WebSockets

| Path | Direction | Messages | Owner |
|---|---|---|---|
| `WS /ws?trip_id=<id>[&after_seq=<n>]` | server → client | `events.Event` (JSON, discriminated on `type`; envelope `trip_id`, `ts`, `seq`). On connect: a `trip.snapshot` of the current trip, then replay of events with `seq > after_seq`, then live events. `seq` is bus-wide (gaps are normal); dedupe by `seq`. `robot.state` is throttled to ≤ 2 Hz here. Never carries `frame`. | gateway |
| `WS /ws?store_id=<id>` / `WS /ws` | server → client | Same events, filtered to trips in that store plus store-level events (`trip_id=null`, e.g. merchant scans); no filter = everything (merchant dashboard / debug). | gateway |
| `WS /ws?trip_id=<id>` | client → server | `api.ClientMessage`: `{"type":"chat","text":...}`, `{"type":"approval","response":ApprovalResponse}`, `{"type":"ping"}` | gateway |
| `WS /ws/frames?trip_id=<id>` | server → client | Live-only, latest-only, `seq=0`, never replayed: `frame` events (always `blurred=true`) and full-rate `robot.state`. In all-fake mode the robot fake sends a canned JPEG; draw `robot.state` on the SVG map. | gateway |
| `WS /ws/sim` | sim worker → gateway | `sim.SimMessage`: first `hello` (auth = `SimHello.token`; optional `?token=` only for early reject; version + `data_hash` checked), then `result` (one per command, `error_code` on failure), `robot_state`, `camera_frame` (raw), `instances`, `log` | gateway ↔ sim/robot |
| `WS /ws/sim` | gateway → sim worker | `sim.GatewayToSim`: `welcome`, then `navigate`, `scan`, `pick`, `place_in_basket`, `capture`, `list_instances`, `reset`, `stop` (each with `cmd_id`; one motion command at a time) | robot (via `SimLink`) |

## Event types (`contracts/events.py`)

Producer: **gateway** for all of these except `agent.message` (agent) and `error` (any arm) — `events.EVENT_PRODUCERS`.

| Event | When |
|---|---|
| `trip.created` | trip created (POST /api/trips) |
| `trip.snapshot` | first message on every `/ws` connect / resync |
| `trip.state_changed` | every TripState transition |
| `intent.received` | intent text accepted |
| `list.ready` | `agent.expand_intent` done (or list edited) |
| `sourcing.split` | `agent.source_list` done: in-store / online / unresolved |
| `recs.ready` | `recs` (detours, near-expiry) + `checkout_recs` |
| `route.planned` | route solved (`replan=true` mid-trip) |
| `stop.updated` | a stop changed status (`Trip.stop_status`) |
| `robot.state` | pose/status (throttled on `/ws`) |
| `robot.arrived` | reached a stop |
| `scan.result` | perception ScanReport for a slot |
| `pick.choices` | produce top-3 for the shopper |
| `item.picked` | item in the gripper/basket |
| `basket.verified` | basket check after a drop |
| `cart.updated` | any cart change (totals + budget) |
| `approval.requested` / `approval.resolved` | ApprovalBroker |
| `mandate.decision` | trust evaluation shown to the shopper |
| `checkout.completed` | payment attempted: `receipt.status` succeeded / declined / error |
| `followup.sent` | post-purchase question sent (`message.about_skus`) |
| `feedback.received` / `profile.updated` | feedback → profile change |
| `frame` | live view (on `/ws/frames` only) |
| `agent.message` | agent chat (streaming deltas) |
| `error` | degraded/failed step |

## Trip state machine (`shopping.TripState`, transitions in `shopping.TRIP_TRANSITIONS`)

`intent → list_ready → recs_ready → route_planned → awaiting_login → executing ⇄ awaiting_approval → checkout → paid →
followup → done` (+ `failed` from any non-terminal state). Pre-trip edits go back to `list_ready` from `list_ready`,
`recs_ready`, `route_planned` or `awaiting_login`; `executing → route_planned` on a mid-trip re-plan.
