"""TripOrchestrator: runs the trip state machine end to end (CLAUDE.md §2) using only ``services.*``.

It is the gateway's ``TripController`` (the only thing that mutates a ``Trip``) and publishes every trip-flow event
(envelope ``events.trip_envelope(trip)``) plus ``robot.state`` for every robot. Flow per trip::

    intent -> agent.expand_intent (skipped when the fleet passes a per-store shopping_list) -> list.ready
      -> agent.source_list -> sourcing.split (+ online lines in the cart) -> recommender.recommend / checkout_batch
      -> recs.ready (+ transparency log) -> routing.plan_route (from the assigned robot's pose) -> route.planned
      -> trust.evaluate (projected cart)
      -> auto_execute (fleet / request trips): EXECUTING right away | manual trips: awaiting_login -> POST /login
    EXECUTING: goal = fleet.execute_trip(trip_id, robot_id)  (the robot brain does the in-store part through
      trips.assign_robot / set_stop_status / check_line / ask / add_line / replan / update_list)
      completed -> checkout-batch question (accepted in-store picks: one more fleet run picks them; billed only once
      picked) -> start_checkout; failed -> checkout if the cart has lines (+ error) else FAILED; cancelled -> FAILED
    start_checkout: trust.evaluate -> checkout approval bound to cart_hash -> trust.checkout -> checkout.completed
      -> paid -> record_purchase -> agent.compose_followup -> messaging.send -> followup.sent -> reply
      -> agent.interpret_reply -> recommender.record_feedback -> feedback.received + profile.updated -> done

Failures never kill the trip unless planning itself is impossible: every degraded step emits an ``error`` event.
"""

import asyncio
import contextlib
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from contracts.api import AddLineResult, CheckoutStatus, CreateTripRequest, RobotLoginRequest
from contracts.catalog import StoreInfo, online_shipping_cents
from contracts.common import ONLINE_STORE_ID, Pose2D, money, utcnow
from contracts.events import make_event, trip_envelope
from contracts.fleet import FleetRequest, GoalStatus
from contracts.messaging import InboundMessage, OutboundMessage, ReplyInterpretation
from contracts.profile import Feedback, ProfileUpdate, PurchaseRecord, UserProfile
from contracts.routing import Route, RouteRequest, Stop, StopStatus
from contracts.services import Services
from contracts.shopping import TRIP_TRANSITIONS, Cart, CartLine, Intent, ListItem, Recommendation, TripState
from contracts.tools import AgentReply
from contracts.trip import Trip, TripTransition
from contracts.trust import (
    ApprovalKind,
    ApprovalOption,
    ApprovalRequest,
    ApprovalResponse,
    CheckoutRequest,
    MandateDecision,
    MandateVerdict,
    TransparencyEntry,
    compute_cart_hash,
)
from utils.config import get_settings
from utils.fsm import StateMachine
from utils.ids import new_id
from utils.log import get_logger

log = get_logger(__name__)

S = TripState
TERMINAL = (S.DONE, S.FAILED)
PRE_TRIP = (S.LIST_READY, S.RECS_READY, S.ROUTE_PLANNED, S.AWAITING_LOGIN)
FINISHED_STOP: tuple[StopStatus, ...] = ("done", "declined", "failed")
CHECKOUT_BATCH_SIZE = 3
BUDGET_CANDIDATE_POOL = 3
"""With a trip budget, ask the recommender for this many times the slots, then keep the ones that fit."""


@dataclass
class OrchestratorConfig:
    approval_channels: tuple[str, ...] = ("web", "photon")
    """Where approvals are asked (photon = messaging.send_approval_request)."""
    approval_timeout_s: float = 120.0
    checkout_timeout_s: float = 300.0
    auto_login: bool = False
    """True = manual trips also start executing as soon as the route is planned (tests). Fleet-created trips
    (``auto_execute``) always do."""
    auto_checkout: bool = True
    """Start checkout when the in-store goal ends."""
    auto_followup: bool = True
    """Send the post-purchase question right after payment (POST /api/trips/{id}/followup re-sends it)."""
    followup_timeout_s: float | None = None
    """Finish the trip (done) if no feedback arrives in time; None = wait for the reply."""
    checkout_batch: bool = True
    """Ask the batched 'want any of these?' question before checkout."""
    fetch_checkout_suggestions: bool = True
    """An in-store suggestion accepted in that batch sends the robot back to pick it (billed only once picked).
    False = it is not added at all (the bill never carries an in-store line the robot did not pick either way)."""
    rec_limit: int | None = None
    """Detour recs per trip; None = PICKY_REC_MAX_STOPS."""
    robot_state_hz: float = 2.0
    """robot.state rate on the sequenced bus (/ws); full rate goes to /ws/frames."""
    payment_agent: bool | None = None
    """Checkout through the Visa payment agent (payment.agent_step timeline); None = PICKY_PAYMENT_AGENT."""
    payment_step_pause_s: float = 0.35
    """Beat between payment agent steps (the web app's timeline fills in visibly); tests use 0."""


def _ask_expiry(request: ApprovalRequest, timeout: float) -> float:
    """Approval expiry per kind: photo verification cards keep their own auto-accept window (0.2.4); produce reviews
    time out after PICKY_PRODUCE_REVIEW_TIMEOUT_S (the robot then takes its best guess); the rest the config's."""
    if request.kind == ApprovalKind.VERIFY_ITEM:
        return request.expires_s
    if request.kind == ApprovalKind.PICK_CHOICE:
        try:
            produce = float(get_settings().picky_produce_review_timeout_s)
        except Exception:  # noqa: BLE001
            produce = 60.0
        return max(1.0, min(timeout, produce))
    return timeout


class TripOrchestrator:
    """``contracts.services.TripController`` + the pipeline driver. Construct freely; wiring happens in ``start()``."""

    def __init__(
        self,
        services: Services,
        config: OrchestratorConfig | None = None,
        **_ignored: Any,
    ) -> None:
        self.services = services
        self.config = config or OrchestratorConfig()
        self.photo_lookup: Any = None
        """(trip_id, sku) -> verification photo urls for the purchase rows (set by the registry: JudgesIndex)."""
        self._trips: dict[str, Trip] = {}
        self._fsm: dict[str, StateMachine] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._robot_trip: dict[str, str] = {}
        """robot_id -> trip it executes (robot.state envelope)."""
        self._deferred_online: dict[str, list[CartLine]] = {}
        self._checkout_picks: dict[str, dict[str, tuple[str, int | None]]] = {}
        """trip_id -> rec_id -> (checkout-batch approval_id, card price) of the in-store suggestions the robot goes
        back for: their pick is billed at the price on the card the shopper chose, and that choice is its approval."""
        self._used_approvals: dict[str, set[str]] = {}
        self._sent_trip: dict[str, str] = {}
        self._routed_items: dict[str, set[str]] = {}
        """trip_id -> list item ids that have EVER had a route stop (a re-plan installs only the remaining stops, so
        finished / in-progress items are not in Trip.route any more and must not be re-added as 'missing')."""
        """Outbound follow-up message_id -> trip_id (a text reply with ``in_reply_to`` finds its trip)."""
        self._declined_skus: dict[str, set[str]] = {}
        """user_id -> detour SKUs the shopper said no to (passed as exclude_skus on the next trips)."""
        self._tasks: set[asyncio.Task[Any]] = set()
        self._exec_tasks: dict[str, asyncio.Task[Any]] = {}
        self._online_info: StoreInfo | None = None
        self._started = False
        self._state_task: asyncio.Task[Any] | None = None

    # ==================================================================================================================
    # lifecycle
    # ==================================================================================================================
    async def start(self) -> None:
        """Register the approval listener and the messaging inbound handler; start the fleet (robot monitoring),
        the robot.state forwarder and messaging."""
        self._wire()
        if self._state_task is None and self.services.robot is not None:
            self._state_task = self._spawn(self._forward_states(), name="robot-states")
        fleet = self.services.fleet
        if fleet is not None:
            try:
                await fleet.start()
            except Exception as exc:  # noqa: BLE001 - trips still plan; execution reports the problem
                log.warning("fleet.start failed: %s", exc)
                await self._error(None, "fleet", f"fleet manager failed to start: {exc}")
        messaging = self.services.messaging
        if messaging is not None:
            try:
                await messaging.start()
            except Exception as exc:  # noqa: BLE001 - web channel still works
                log.warning("messaging.start failed: %s", exc)
                await self._error(None, "messaging", f"messaging unavailable: {exc}")

    async def stop(self) -> None:
        fleet = self.services.fleet
        if fleet is not None:
            with contextlib.suppress(Exception):
                await fleet.shutdown()
        tasks = list(self._tasks)
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._state_task = None
        messaging = self.services.messaging
        if messaging is not None:
            with contextlib.suppress(Exception):
                await messaging.stop()

    def _wire(self) -> None:
        if self._started:
            return
        self._started = True
        approvals = self.services.approvals
        if approvals is not None and hasattr(approvals, "add_listener"):
            approvals.add_listener(self._on_approval)
        if approvals is not None and getattr(approvals, "envelope_hook", False) is None:
            approvals.envelope_hook = self.approval_envelope
        messaging = self.services.messaging
        if messaging is not None:
            messaging.on_inbound(self.handle_inbound)

    def _spawn(self, coro: Any, name: str | None = None) -> asyncio.Task[Any]:
        task = asyncio.get_running_loop().create_task(coro, name=name)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    # ==================================================================================================================
    # queries
    # ==================================================================================================================
    def list_trips(self, user_id: str | None = None) -> list[Trip]:
        trips = [t for t in self._trips.values() if user_id is None or t.user_id == user_id or user_id in t.user_ids]
        return [t.model_copy(deep=True) for t in trips]

    def is_executing(self, trip_id: str) -> bool:
        """True while the in-store run (fleet shop goal + checkout question) is still going."""
        task = self._exec_tasks.get(trip_id)
        return task is not None and not task.done()

    def peek(self, trip_id: str) -> Trip:
        """The live Trip (no copy; gateway-internal)."""
        return self._trips[trip_id]

    def active_trip_for(self, user_id: str | None) -> Trip | None:
        if user_id is None:
            return None
        mine = [t for t in self._trips.values() if t.user_id == user_id or user_id in t.user_ids]
        if not mine:
            return None
        live = [t for t in mine if t.state not in TERMINAL]
        return (live or mine)[-1]

    # ==================================================================================================================
    # TripController
    # ==================================================================================================================
    async def get_trip(self, trip_id: str) -> Trip:
        return self._trips[trip_id].model_copy(deep=True)

    async def create_trip(self, req: CreateTripRequest) -> Trip:
        """POST /api/trips: create the trip and start planning in the background."""
        self._wire()
        trip_id = new_id("trip")
        store_id = req.store_id or await self._home_store(req.user_id)
        intent = Intent(
            intent_id=new_id("intent"),
            user_id=req.user_id,
            text=req.text,
            channel=req.channel,
            store_id=store_id,
            trip_id=trip_id,
        )
        user_ids = [req.user_id, *[u for u in req.user_ids if u != req.user_id]]
        preset = None
        if req.shopping_list is not None:
            preset = req.shopping_list.model_copy(update={"trip_id": trip_id, "store_id": store_id}, deep=True)
        trip = Trip(
            trip_id=trip_id,
            user_id=req.user_id,
            user_ids=user_ids,
            store_id=store_id,
            intent=intent,
            shopping_list=preset,
            cart=Cart(cart_id=new_id("cart"), trip_id=trip_id, user_id=req.user_id),
            history=[TripTransition(from_state=None, to_state=S.INTENT, reason="created")],
            auto_execute=req.auto_execute,
            robot_logged_in=req.auto_execute,
            robot_id=req.robot_id,
            request_id=req.request_id,
        )
        self._trips[trip_id] = trip
        self._fsm[trip_id] = StateMachine(TRIP_TRANSITIONS, initial=S.INTENT, any_to=[S.FAILED], terminal=TERMINAL)
        self._locks[trip_id] = asyncio.Lock()
        await self._publish("trip.created", trip_id, trip=trip.model_copy(deep=True))
        await self._publish("intent.received", trip_id, intent=intent)
        self._spawn(self._plan(trip_id, preset is not None), name=f"plan-{trip_id}")
        return trip.model_copy(deep=True)

    async def assign_robot(self, trip_id: str, robot_id: str, goal_id: str | None = None) -> Trip:
        """Fleet -> trip: which robot / goal executes the trip (robot.state for that robot carries this trip_id)."""
        trip = self._trips[trip_id]
        old = trip.robot_id
        if old and old != robot_id and self._robot_trip.get(old) == trip_id:
            self._robot_trip.pop(old, None)
        trip.robot_id = robot_id
        if goal_id is not None:
            trip.goal_id = goal_id
        if trip.state not in TERMINAL:
            self._robot_trip[robot_id] = trip_id
        trip.updated_at = utcnow()
        return trip.model_copy(deep=True)

    async def set_stop_status(self, trip_id: str, stop_id: str, status: StopStatus, reason: str = "") -> Trip:
        """Robot brain -> trip: route stop progress; publishes stop.updated."""
        trip = self._trips[trip_id]
        stop = trip.route.stop(stop_id) if trip.route is not None else None
        if trip.stop_status.get(stop_id) != status or reason:
            trip.stop_status[stop_id] = status
            if status in ("navigating", "arrived"):
                trip.current_stop_id = stop_id
            elif trip.current_stop_id == stop_id and status in (*FINISHED_STOP, "skipped"):
                trip.current_stop_id = None
            await self._publish(
                "stop.updated",
                trip_id,
                stop_id=stop_id,
                status=status,
                reason=reason,
                rec_id=stop.rec_id if stop else None,
                slot_id=stop.slot_id if stop else None,
            )
        return trip.model_copy(deep=True)

    async def check_line(self, trip_id: str, line: CartLine) -> MandateDecision:
        """Mandate pre-check of cart + the FULL proposed line with the trip budget; publishes mandate.decision."""
        trip = self._trips[trip_id]
        return await self._evaluate(trip, line)

    async def ask(self, trip_id: str, request: ApprovalRequest) -> ApprovalResponse:
        """The brain's ask_user for goals with a trip: fill ids, channels + expiry from config, flag detours that bust
        the budget, drive EXECUTING <-> AWAITING_APPROVAL, record the transparency outcome."""
        trip = self._trips[trip_id]
        timeout = self.config.approval_timeout_s
        update: dict[str, Any] = {
            "trip_id": trip_id,
            "user_id": trip.user_id,
            "store_id": request.store_id or trip.store_id,
            "robot_id": request.robot_id or trip.robot_id,
            "goal_id": request.goal_id or trip.goal_id,
            "channels": list(self.config.approval_channels),
            "expires_s": _ask_expiry(request, timeout),
        }
        rec = self._find_rec(trip, request.rec_id)
        if request.kind == ApprovalKind.RECOMMENDATION and not request.multi:
            payload = dict(request.payload)
            price = request.amount_cents if request.amount_cents is not None else (rec.price_cents if rec else None)
            if "over_budget" not in payload:
                over = self._budget_overage(trip, price)
                payload.update(over_budget=over > 0, over_budget_cents=over)
            payload.setdefault("sponsored", rec.sponsored if rec else False)
            if rec is not None:
                payload.setdefault("interest", rec.interest)
            update["payload"] = payload
            over_cents = int(payload.get("over_budget_cents") or 0)
            if payload.get("over_budget") and trip.budget_cents is not None and "budget" not in request.title.lower():
                update["title"] = (
                    f"{request.title} Heads up: it puts you {money(over_cents)} over your "
                    f"{money(trip.budget_cents)} budget."
                )
        request = request.model_copy(update=update)
        resp = await self._ask(trip, request)
        if request.kind == ApprovalKind.RECOMMENDATION:
            if request.multi:
                chosen = set(resp.chosen()) if resp.approved else set()
                for opt in request.options:
                    opt_rec = self._find_rec(trip, opt.option_id)
                    await self._record_rec_outcome(trip, opt_rec, "accepted" if opt.option_id in chosen else "declined")
            elif rec is not None:
                await self._record_rec_outcome(trip, rec, "accepted" if resp.approved else "declined")
                if not resp.approved:
                    self._declined_skus.setdefault(trip.user_id, set()).add(rec.sku)
        return resp

    async def approval_envelope(self, request: ApprovalRequest) -> dict[str, Any]:
        """Envelope for approval.* events: the trip's (robot = the asking robot), else the goal's (fleet)."""
        trip = self._trips.get(request.trip_id) if request.trip_id else None
        if trip is not None:
            env = trip_envelope(trip)
            if request.robot_id:
                env["robot_id"] = request.robot_id
            return env
        fleet = self.services.fleet
        if request.goal_id and fleet is not None:
            with contextlib.suppress(Exception):
                goal = await fleet.get_goal(request.goal_id)
                return {
                    "trip_id": goal.trip_id,
                    "store_id": goal.store_id,
                    "robot_id": request.robot_id or goal.robot_id,
                    "user_id": request.user_id,
                    "request_id": goal.request_id,
                }
        return {}

    def _find_rec(self, trip: Trip, rec_id: str | None) -> Recommendation | None:
        if not rec_id:
            return None
        return next((r for r in [*trip.recs, *trip.checkout_recs] if r.rec_id == rec_id), None)

    async def update_list(self, trip_id: str, shopping_list: Any) -> Trip:
        """Pre-trip: replace the list and re-run sourcing / recs / routing. During EXECUTING (the brain's add_item):
        new items are sourced and appended to the route as PENDING stops (``trips.replan`` then orders them)."""
        trip = self._trips[trip_id]
        shopping_list = shopping_list.model_copy(update={"trip_id": trip_id})
        old_ids = {i.item_id for i in (trip.shopping_list.items if trip.shopping_list else [])}
        trip.shopping_list = shopping_list
        if trip.state in PRE_TRIP:
            await self._transition(trip, S.LIST_READY, "list edited")
            await self._publish("list.ready", trip_id, shopping_list=shopping_list)
            self._spawn(self._source_recs_route(trip_id), name=f"replan-{trip_id}")
            return trip.model_copy(deep=True)
        new_items = [i for i in shopping_list.items if i.item_id not in old_ids]
        if new_items and trip.state in (S.EXECUTING, S.AWAITING_APPROVAL, S.ROUTE_PLANNED):
            try:
                await self._append_items(trip, new_items)
            except Exception as exc:  # noqa: BLE001
                await self._error(trip_id, "gateway", f"could not add {', '.join(i.query for i in new_items)}: {exc}")
        await self._publish("list.ready", trip_id, shopping_list=trip.shopping_list)
        return trip.model_copy(deep=True)

    async def _append_items(self, trip: Trip, items: list[ListItem]) -> None:
        """Source items added mid-trip and append them as pending route stops (online ones go to the cart)."""
        assert trip.shopping_list is not None
        need = [i for i in items if i.sku is None or i.sourcing == "unresolved"]
        sourced: dict[str, ListItem] = {}
        agent = self.services.agent
        if need and agent is not None:
            with contextlib.suppress(Exception):
                sub = trip.shopping_list.model_copy(update={"items": need})
                res = await agent.source_list(sub, trip.store_id)
                sourced = {i.item_id: i for i in res.items}
        if sourced:
            trip.shopping_list = trip.shopping_list.model_copy(
                update={"items": [sourced.get(i.item_id, i) for i in trip.shopping_list.items]}
            )
        added = [sourced.get(i.item_id, i) for i in items]
        for item in added:
            if item.sourcing == "online":
                await self._reroute_online(trip, item)
        await self._add_missing_stops(trip, [i for i in added if i.sourcing == "in_store"])

    async def _add_missing_stops(self, trip: Trip, items: list[ListItem] | None = None) -> list[Stop]:
        """Pending mandatory stops for in-store list items no route stop covers yet."""
        if trip.route is None or trip.shopping_list is None:
            return []
        covered = {iid for st in trip.route.stops for iid in st.item_ids} | self._routed_items.get(trip.trip_id, set())
        candidates = items if items is not None else trip.shopping_list.by_sourcing("in_store")
        catalog = self.services.catalog
        new_stops: list[Stop] = []
        existing = {st.stop_id for st in trip.route.stops}
        for item in candidates:
            if item.item_id in covered or item.sku is None or catalog is None:
                continue
            locs = []
            with contextlib.suppress(Exception):
                locs = await catalog.get_locations(item.sku, trip.store_id)
            if not locs:
                await self._reroute_online(trip, item)
                continue
            loc = next((x for x in locs if x.qty > 0), locs[0])
            stop_id = f"item:{item.sku}"
            if stop_id in existing:
                stop_id = f"item:{item.sku}:{item.item_id}"
            existing.add(stop_id)
            new_stops.append(
                Stop(
                    stop_id=stop_id,
                    kind="mandatory",
                    pose=loc.approach_pose,
                    label=item.query,
                    slot_id=loc.slot_id,
                    skus=[item.sku],
                    qty=item.qty,
                    item_ids=[item.item_id],
                    user_ids=[item.for_user_id or trip.user_id],
                    service_time_s=8.0,
                )
            )
        if new_stops:
            trip.route = trip.route.model_copy(update={"stops": [*trip.route.stops, *new_stops]})
            self._routed_items.setdefault(trip.trip_id, set()).update(i for st in new_stops for i in st.item_ids)
            for st in new_stops:
                await self.set_stop_status(trip.trip_id, st.stop_id, "pending", "added mid-trip")
        return new_stops

    async def add_line(self, trip_id: str, line: CartLine, approval_id: str | None = None) -> AddLineResult:
        trip = self._trips[trip_id]
        chosen = self._checkout_picks.get(trip_id, {}).get(line.rec_id or "")
        if chosen is not None and line.source == "in_store":  # the pick of a suggestion chosen at checkout
            approval_id = approval_id or chosen[0]
            if chosen[1] and line.unit_price_cents != chosen[1]:
                line = line.model_copy(update={"unit_price_cents": chosen[1]})
        decision = await self._evaluate(trip, line)
        added = False
        if decision.verdict == MandateVerdict.APPROVE:
            added = True
        elif (
            decision.verdict == MandateVerdict.NEEDS_APPROVAL
            and approval_id
            and self._approved(trip, approval_id, line)
        ):
            added = True
            self._used_approvals.setdefault(trip_id, set()).add(self._approval_key(trip, approval_id, line))
        if added:
            line = line.model_copy(update={"user_id": line.user_id or trip.user_id})
            trip.cart.lines.append(line)
            await self._cart_changed(trip)
        return AddLineResult(
            added=added,
            line_id=line.line_id if added else None,
            decision=decision,
            cart=trip.cart.model_copy(deep=True),
            approval_id=approval_id if added else None,
        )

    async def remove_line(self, trip_id: str, line_id: str) -> Cart:
        trip = self._trips[trip_id]
        trip.cart.lines = [ln for ln in trip.cart.lines if ln.line_id != line_id]
        await self._cart_changed(trip)
        return trip.cart.model_copy(deep=True)

    async def replan(
        self, trip_id: str, include_recommendations: bool = True, max_optional_stops: int | None = None
    ) -> Route:
        trip = self._trips[trip_id]
        if trip.route is None:
            route = await self._plan_route(trip)
            return route
        await self._add_missing_stops(trip)
        remaining = [
            s.model_copy(update={"detour_m": None, "detour_s": None})
            for s in trip.route.stops
            if s.kind in ("mandatory", "optional")
            and trip.stop_status.get(s.stop_id, "pending") in ("pending", "skipped")
        ]
        if not include_recommendations:
            remaining = [s for s in remaining if s.kind == "mandatory"]
        start = await self._start_pose(trip)
        request = RouteRequest(
            store_id=trip.store_id,
            start=start,
            stops=remaining,
            max_optional_stops=max_optional_stops if max_optional_stops is not None else self._rec_limit(),
            user_ids=list(trip.user_ids),
        )
        route = await self.services.require("routing").plan_route(request)
        await self._install_route(trip, route, replan=True)
        return route

    async def start_checkout(self, trip_id: str, note: str | None = None) -> CheckoutStatus:
        trip = self._trips[trip_id]
        approvals = self.services.require("approvals")
        pending = [a for a in approvals.pending(trip_id) if a.kind == ApprovalKind.CHECKOUT]
        if pending:
            return self._checkout_status(trip, approval=pending[0])
        if trip.state not in (S.EXECUTING, S.AWAITING_APPROVAL, S.CHECKOUT):
            if trip.state not in (S.PAID, S.FOLLOWUP, S.DONE):
                await self._error(trip_id, "gateway", f"checkout is not available in state {trip.state.value}")
            return self._checkout_status(trip)
        unaudited = await self._unaudited_basket(trip)
        if unaudited:
            await self._error(trip_id, "gateway", unaudited, code="basket_audit_pending")
            return self._checkout_status(trip)
        await self._transition(trip, S.CHECKOUT, note or "checkout started")
        cart_hash = compute_cart_hash(trip.cart, trip_id)
        decision = await self._evaluate(trip, None)
        trip.mandate_decision = decision
        if decision.verdict == MandateVerdict.DENY:
            await self._error(trip_id, "trust", "checkout blocked by mandate: " + "; ".join(decision.messages or []))
            await self._transition(trip, S.EXECUTING, "checkout denied by mandate")
            return self._checkout_status(trip, decision=decision)
        lines = ", ".join(f"{ln.qty}x {ln.name or ln.sku}" for ln in trip.cart.active_lines())
        body = f"{lines}. In store {money(trip.cart.in_store_cents)}, online {money(trip.cart.online_cents)}"
        if trip.cart.shipping_cents and trip.cart.online_cents:
            body += f" + {money(trip.cart.shipping_cents)} shipping"
        if decision.verdict == MandateVerdict.NEEDS_APPROVAL and decision.messages:
            body += ". Heads up: " + "; ".join(decision.messages)
        if note:
            body += f". {note}"
        request = self._approval(
            trip,
            ApprovalKind.CHECKOUT,
            f"Pay {money(trip.cart.total_cents)} with Visa?",
            body=body,
            amount_cents=trip.cart.total_cents,
            cart_hash=cart_hash,
            expires_s=self.config.checkout_timeout_s,
        )
        if hasattr(approvals, "open"):
            fut = await approvals.open(request)
        else:  # generic broker
            fut = self._spawn(approvals.request(request))
            await asyncio.sleep(0)
        await self._transition(trip, S.AWAITING_APPROVAL, "waiting for checkout approval")
        self._spawn(self._finish_checkout(trip_id, request, fut), name=f"checkout-{trip_id}")
        return self._checkout_status(trip, decision=decision, approval=request)

    # ==================================================================================================================
    # extra gateway entry points (REST / WS / messaging)
    # ==================================================================================================================
    async def login(self, trip_id: str, req: RobotLoginRequest) -> Trip:
        """Manual trips: the shopper is at a robot -> EXECUTING -> fleet.execute_trip. No-op for auto_execute trips."""
        trip = self._trips[trip_id]
        if req.user_id not in trip.user_ids and req.user_id != trip.user_id:
            raise PermissionError(f"user {req.user_id} is not on trip {trip_id}")
        if trip.auto_execute:
            return trip.model_copy(deep=True)
        if req.robot_id:
            trip.robot_id = req.robot_id
        trip.robot_logged_in = True
        if trip.state in (S.ROUTE_PLANNED, S.AWAITING_LOGIN):
            await self._start_execution(trip)
        return trip.model_copy(deep=True)

    async def chat(self, trip_id: str, text: str, channel: str = "web") -> AgentReply:
        trip = self._trips[trip_id]
        agent = self.services.agent
        if agent is None:
            return AgentReply(trip_id=trip_id, text="(agent unavailable)")
        try:
            return await agent.respond(trip.model_copy(deep=True), text, channel)  # type: ignore[arg-type]
        except Exception as exc:  # noqa: BLE001
            await self._error(trip_id, "agent", f"chat failed: {exc}")
            return AgentReply(trip_id=trip_id, text="Sorry, I hit a snag. Try again?")

    async def send_followup(self, trip_id: str) -> OutboundMessage:
        trip = self._trips[trip_id]
        agent = self.services.agent
        if agent is not None:
            try:
                msg = await agent.compose_followup(trip.model_copy(deep=True))
            except Exception as exc:  # noqa: BLE001
                await self._error(trip_id, "agent", f"compose_followup failed: {exc}")
                msg = self._default_followup(trip)
        else:
            msg = self._default_followup(trip)
        msg = msg.model_copy(
            update={
                "trip_id": trip_id,
                "user_id": msg.user_id or trip.user_id,
                "message_id": msg.message_id or new_id("msg"),
            }
        )
        trip.followup = msg
        if trip.state == S.PAID:
            await self._transition(trip, S.FOLLOWUP, "follow-up sent")
        await self._publish("followup.sent", trip_id, message=msg)
        messaging = self.services.messaging
        if messaging is not None:
            try:
                if msg.message_id:
                    self._sent_trip[msg.message_id] = trip_id
                res = await messaging.send(msg)
                if res.message_id:
                    self._sent_trip[res.message_id] = trip_id
                if not res.ok:
                    await self._error(trip_id, "messaging", f"follow-up not delivered: {res.error}")
            except Exception as exc:  # noqa: BLE001
                await self._error(trip_id, "messaging", f"follow-up send failed: {exc}")
        if self.config.followup_timeout_s is not None:
            self._spawn(self._followup_timeout(trip_id, self.config.followup_timeout_s))
        return msg

    def _inbound_target(self, msg: InboundMessage) -> tuple[Trip | None, list[ApprovalRequest]]:
        """Which trip and pending approvals an inbound text is about. A user can have several live trips (a split
        request, or an older trip still running), so: explicit ``msg.trip_id`` > a reply to an approval text
        (``in_reply_to`` -> broker map) or to a follow-up > the user's OLDEST pending texted approval across all live
        trips (texts go out in that order; a numeric reply prefers the oldest one with options) > newest live trip."""
        approvals = self.services.approvals
        replied_trip = self._sent_trip.get(msg.in_reply_to) if msg.in_reply_to else None
        explicit = self._trips.get(msg.trip_id or replied_trip or "")
        if approvals is None:
            return explicit or self.active_trip_for(msg.user_id), []
        lookup = getattr(approvals, "approval_for_message", None)
        replied = lookup(msg.in_reply_to) if (msg.in_reply_to and callable(lookup)) else None
        if replied is not None and explicit is None:
            explicit = self._trips.get(replied.trip_id or "")
        if replied is not None:
            live = [p for p in approvals.pending(user_id=replied.user_id) if p.approval_id == replied.approval_id]
            return explicit or self.active_trip_for(msg.user_id), live  # answered already -> nothing to resolve
        if explicit is not None:
            return explicit, approvals.pending(explicit.trip_id)
        mine = approvals.pending(user_id=msg.user_id) if msg.user_id else []
        texted = sorted([p for p in mine if "photon" in p.channels] or mine, key=lambda p: p.created_at)
        if texted:
            target = texted[0]
            if any(ch.isdigit() for ch in msg.text):
                target = next((p for p in texted if p.options), target)
            trip = self._trips.get(target.trip_id or "")
            return trip or self.active_trip_for(msg.user_id), [target]
        trip = self.active_trip_for(msg.user_id)
        return trip, (approvals.pending(trip.trip_id) if trip is not None else [])

    async def handle_inbound(self, msg: InboundMessage) -> ReplyInterpretation:
        """Messaging inbound (and POST /api/dev/inbound): approval answers, feedback, new intents, chat."""
        trip, pending = self._inbound_target(msg)
        if trip is not None and msg.trip_id is None:
            msg = msg.model_copy(update={"trip_id": trip.trip_id})
        approvals = self.services.approvals
        agent = self.services.agent
        try:
            if agent is None:
                raise RuntimeError("agent not wired")
            interp = await agent.interpret_reply(msg, pending, trip.model_copy(deep=True) if trip else None)
        except Exception as exc:  # noqa: BLE001
            await self._error(msg.trip_id, "agent", f"could not interpret reply: {exc}")
            return ReplyInterpretation(kind="chat", confidence=0.0)

        if interp.kind == "approval" and approvals is not None:
            target = interp.approval_id or (pending[0].approval_id if pending else None)
            if target is not None:
                req = next((p for p in pending if p.approval_id == target), None)
                decision: Any = "choice" if (interp.choice or interp.choices) else (interp.decision or "approve")
                await approvals.resolve(
                    ApprovalResponse(
                        approval_id=target,
                        decision=decision,
                        choice=interp.choice,
                        choices=list(interp.choices),
                        channel="voice" if msg.channel == "voice" else ("web" if msg.channel == "web" else "photon"),
                        user_id=msg.user_id,
                        cart_hash=req.cart_hash if req is not None else None,
                    )
                )
        elif interp.kind == "feedback" and trip is not None:
            await self._feedback_from_reply(trip, msg, interp)
        elif interp.kind == "intent" and msg.user_id is not None and (trip is None or trip.state in TERMINAL):
            text = interp.intent_text or msg.text
            fleet = self.services.fleet
            if fleet is not None:  # new NL requests go through the fleet (store choice, goals, robots)
                try:
                    await fleet.submit_request(FleetRequest(text=text, user_id=msg.user_id, channel=msg.channel))
                except Exception as exc:  # noqa: BLE001
                    await self._error(None, "fleet", f"could not start that request: {exc}")
            else:
                await self.create_trip(CreateTripRequest(user_id=msg.user_id, text=text, channel=msg.channel))
        elif trip is not None:
            reply = await self.chat(trip.trip_id, msg.text, msg.channel)
            if msg.channel == "photon" and self.services.messaging is not None and reply.text:
                with contextlib.suppress(Exception):
                    await self.services.messaging.send(
                        OutboundMessage(channel="photon", user_id=msg.user_id, trip_id=trip.trip_id, text=reply.text)
                    )
        if interp.reply_text and msg.channel == "photon" and self.services.messaging is not None:
            with contextlib.suppress(Exception):
                await self.services.messaging.send(
                    OutboundMessage(
                        channel="photon",
                        user_id=msg.user_id,
                        trip_id=msg.trip_id,
                        text=interp.reply_text,
                        in_reply_to=msg.message_id,
                    )
                )
        return interp

    async def submit_feedback(self, feedback: Feedback) -> ProfileUpdate:
        """POST /api/users/{id}/feedback (web) or a messaging reply: recommender + events (+ trip done)."""
        trip = self._trips.get(feedback.trip_id) if feedback.trip_id else self.active_trip_for(feedback.user_id)
        trip_id = trip.trip_id if trip else feedback.trip_id
        if trip is not None and feedback.trip_id is None:
            feedback = feedback.model_copy(update={"trip_id": trip_id})
        await self._publish("feedback.received", trip_id, feedback=feedback)
        update = await self.services.require("recommender").record_feedback(feedback)
        await self._publish("profile.updated", trip_id, update=update)
        if trip is not None and trip.state in (S.PAID, S.FOLLOWUP):
            await self._transition(trip, S.DONE, "feedback received")
        return update

    # ==================================================================================================================
    # planning
    # ==================================================================================================================
    async def _plan(self, trip_id: str, preset: bool = False) -> None:
        trip = self._trips[trip_id]
        try:
            if preset and trip.shopping_list is not None:  # the fleet's per-store sub-list: no expand_intent
                shopping_list = trip.shopping_list
            else:
                agent = self.services.require("agent")
                profile = await self._profile(trip.user_id)
                assert trip.intent is not None
                shopping_list = await agent.expand_intent(trip.intent, profile)
            shopping_list = shopping_list.model_copy(update={"trip_id": trip_id})
            trip.shopping_list = shopping_list
            await self._transition(trip, S.LIST_READY, "intent expanded")
            await self._publish("list.ready", trip_id, shopping_list=shopping_list)
        except Exception as exc:  # noqa: BLE001
            log.exception("planning failed for %s", trip_id)
            await self._fail(trip, "agent", f"could not understand the request: {exc}")
            return
        await self._source_recs_route(trip_id)

    async def _source_recs_route(self, trip_id: str) -> None:
        trip = self._trips[trip_id]
        lock = self._locks[trip_id]
        async with lock:
            try:
                await self._source(trip)
                await self._recommend(trip)
                await self._plan_route(trip)
                await self._precheck(trip)
            except Exception as exc:  # noqa: BLE001
                log.exception("planning failed for %s", trip_id)
                await self._fail(trip, "gateway", f"planning failed: {exc}")
                return
        if trip.auto_execute or trip.robot_logged_in or self.config.auto_login:
            trip.robot_logged_in = True
            await self._start_execution(trip)
        else:
            await self._transition(trip, S.AWAITING_LOGIN, "route ready; log in to the robot to start")

    async def _source(self, trip: Trip) -> None:
        assert trip.shopping_list is not None
        agent = self.services.require("agent")
        try:
            sourced = await agent.source_list(trip.shopping_list, trip.store_id)
            sourced = sourced.model_copy(update={"trip_id": trip.trip_id, "store_id": trip.store_id})
        except Exception as exc:  # noqa: BLE001
            await self._error(trip.trip_id, "agent", f"sourcing failed, using the unsourced list: {exc}")
            sourced = trip.shopping_list
        trip.shopping_list = sourced
        await self._publish(
            "sourcing.split",
            trip.trip_id,
            store_id=trip.store_id,
            in_store=sourced.by_sourcing("in_store"),
            online=sourced.by_sourcing("online"),
            unresolved=sourced.by_sourcing("unresolved"),
        )
        # online order lines (re-sourcing replaces the previous ones)
        list_ids = {i.item_id for i in sourced.items}
        trip.cart.lines = [ln for ln in trip.cart.lines if not (ln.source == "online" and ln.list_item_id in list_ids)]
        self._deferred_online[trip.trip_id] = []
        added_any = False
        for item in sourced.by_sourcing("online"):
            line = await self._online_line(trip, item)
            if line is None:
                continue
            res = await self.add_line(trip.trip_id, line)
            added_any = added_any or res.added
            if not res.added:
                if res.decision.verdict == MandateVerdict.NEEDS_APPROVAL:
                    self._deferred_online[trip.trip_id].append(line)
                else:
                    await self._error(trip.trip_id, "trust", f"online item {line.name or line.sku} blocked by mandate")
        if not added_any:
            await self._cart_changed(trip)

    async def _online_line(self, trip: Trip, item: ListItem, reason: str | None = None) -> CartLine | None:
        if item.sku is None:
            return None
        catalog = self.services.catalog
        unit = None
        name, category = item.query, item.category or "uncategorized"
        if catalog is not None:
            with contextlib.suppress(Exception):
                offers = await catalog.get_offers(item.sku, [ONLINE_STORE_ID])
                if offers:
                    unit = offers[0].price_cents
            with contextlib.suppress(Exception):
                product = await catalog.get_product(item.sku)
                if product is not None:
                    name, category = product.name, product.category
                    unit = unit if unit is not None else product.price_cents
        if unit is None:
            unit = (item.est_line_cents or 0) // max(item.qty, 1)
        return CartLine(
            line_id=new_id("line"),
            sku=item.sku,
            name=name,
            category=category,
            qty=item.qty,
            unit_price_cents=unit,
            source="online",
            store_id=ONLINE_STORE_ID,
            status="planned",
            list_item_id=item.item_id,
            user_id=item.for_user_id or trip.user_id,
        )

    async def _recommend(self, trip: Trip) -> None:
        assert trip.shopping_list is not None
        shopping_list = trip.shopping_list
        rec_svc = self.services.recommender
        recs: list[Recommendation] = []
        checkout_recs: list[Recommendation] = []
        limit = self._rec_limit()
        declined = sorted(self._declined_skus.get(trip.user_id, set()))
        # "stays within budget" (CLAUDE.md §1): extras must fit budget - projected list cart (None = no budget). The
        # recommender Protocol has no budget parameter, so the fitting lives here.
        headroom = None if trip.budget_cents is None else trip.budget_cents - self._projected_cart(trip).total_cents
        if rec_svc is not None:

            async def detours(n: int) -> list[Recommendation]:
                return await rec_svc.recommend(
                    trip.user_id, trip.store_id, shopping_list, limit=n, exclude_skus=declined
                )

            try:
                recs = await self._candidates(detours, limit, headroom)
            except Exception as exc:  # noqa: BLE001
                await self._error(trip.trip_id, "recommender", f"recommendations unavailable: {exc}")
            recs, headroom = self._fit_detours(recs, limit, headroom)
            if self.config.checkout_batch:
                projected = self._projected_cart(trip)
                exclude = [*declined, *(r.sku for r in recs)]

                async def batch(n: int) -> list[Recommendation]:
                    return await rec_svc.checkout_batch(
                        trip.user_id, trip.store_id, projected, limit=n, exclude_skus=exclude
                    )

                try:
                    checkout_recs = await self._candidates(batch, CHECKOUT_BATCH_SIZE, headroom)
                except Exception as exc:  # noqa: BLE001
                    await self._error(trip.trip_id, "recommender", f"checkout suggestions unavailable: {exc}")
                checkout_recs = self._fit_batch(checkout_recs, headroom)
        recs = [await self._locate_rec(trip, r.model_copy(update={"trip_id": trip.trip_id})) for r in recs]
        checkout_recs = [r.model_copy(update={"trip_id": trip.trip_id}) for r in checkout_recs]
        trip.recs, trip.checkout_recs = recs, checkout_recs
        trust = self.services.trust
        if trust is not None:
            for rec in [*recs, *checkout_recs]:
                with contextlib.suppress(Exception):
                    await trust.record_transparency(TransparencyEntry.from_recommendation(rec, trip.trip_id))
        await self._transition(trip, S.RECS_READY, f"{len(recs)} detours, {len(checkout_recs)} checkout suggestions")
        await self._publish("recs.ready", trip.trip_id, recs=recs, checkout_recs=checkout_recs)

    @staticmethod
    async def _candidates(fetch: Callable[[int], Any], n: int, headroom: int | None) -> list[Recommendation]:
        """The recommender's own top ``n``; if they don't all fit the budget headroom, append spare candidates (a larger
        pool, recommender order) so cheaper ones can fill the slots. A roomy budget changes nothing."""
        first: list[Recommendation] = await fetch(n)
        if headroom is None or sum(r.price_cents or 0 for r in first) <= headroom:
            return first
        pool: list[Recommendation] = await fetch(n * BUDGET_CANDIDATE_POOL)
        seen = {r.sku for r in first}
        return first + [r for r in pool if r.sku not in seen]

    @staticmethod
    def _fit_detours(
        recs: list[Recommendation], limit: int, headroom: int | None
    ) -> tuple[list[Recommendation], int | None]:
        """Up to ``limit`` detours, in order, while they fit ``headroom``; plus at most one plain detour and one
        near-expiry markdown that do not fit (their asks say they bust the budget, so the shopper decides; the markdown
        gets its own slot so a tight budget never hides it behind a pricier detour). Returns the kept detours and the
        headroom they leave."""
        if headroom is None:
            return recs[:limit], None
        kept: list[Recommendation] = []
        over_kept: set[bool] = set()  # True = the near-expiry over-budget slot is used, False = the plain one
        for rec in recs:
            if len(kept) >= limit:
                break
            price = rec.price_cents or 0
            markdown = rec.kind == "near_expiry_offer"
            if price <= headroom:
                kept.append(rec)
                headroom -= price
            elif markdown not in over_kept:
                kept.append(rec)
                over_kept.add(markdown)
        return kept, headroom

    @staticmethod
    def _fit_batch(recs: list[Recommendation], headroom: int | None) -> list[Recommendation]:
        """Checkout 'want any of these?' suggestions that fit what the detours leave (max CHECKOUT_BATCH_SIZE)."""
        if headroom is None:
            return recs[:CHECKOUT_BATCH_SIZE]
        out: list[Recommendation] = []
        for rec in recs:
            price = rec.price_cents or 0
            if price <= headroom and len(out) < CHECKOUT_BATCH_SIZE:
                out.append(rec)
                headroom -= price
        return out

    def _budget_overage(self, trip: Trip, extra_cents: int | None) -> int:
        """Cents by which adding ``extra_cents`` would push the projected trip (cart + unpicked list) over budget."""
        if trip.budget_cents is None or not extra_cents:
            return 0
        return max(0, self._projected_cart(trip).total_cents + extra_cents - trip.budget_cents)

    async def _locate_rec(self, trip: Trip, rec: Recommendation) -> Recommendation:
        if rec.slot_id is not None or rec.store_id != trip.store_id or self.services.catalog is None:
            return rec
        with contextlib.suppress(Exception):
            locs = await self.services.catalog.get_locations(rec.sku, trip.store_id)
            stocked = [loc for loc in locs if loc.qty > 0] or locs
            if stocked:
                return rec.model_copy(update={"slot_id": stocked[0].slot_id})
        return rec

    async def _plan_route(self, trip: Trip) -> Route:
        assert trip.shopping_list is not None
        catalog = self.services.require("catalog")
        stops: list[Stop] = []
        by_sku: dict[str, list[ListItem]] = {}
        for item in trip.shopping_list.by_sourcing("in_store"):
            if item.sku:
                by_sku.setdefault(item.sku, []).append(item)
        for sku, items in by_sku.items():
            locs = []
            with contextlib.suppress(Exception):
                locs = await catalog.get_locations(sku, trip.store_id)
            if not locs:
                await self._error(trip.trip_id, "catalog", f"no shelf location for {sku}; ordering online")
                for item in items:
                    await self._reroute_online(trip, item)
                continue
            loc = next((x for x in locs if x.qty > 0), locs[0])
            stops.append(
                Stop(
                    stop_id=f"item:{sku}",
                    kind="mandatory",
                    pose=loc.approach_pose,
                    label=items[0].query,
                    slot_id=loc.slot_id,
                    skus=[sku],
                    qty=sum(i.qty for i in items),
                    item_ids=[i.item_id for i in items],
                    user_ids=sorted({i.for_user_id or trip.user_id for i in items}),
                    service_time_s=8.0,
                )
            )
        for rec in trip.recs:
            if rec.store_id != trip.store_id or rec.slot_id is None:
                continue
            loc = None
            with contextlib.suppress(Exception):
                loc = next(
                    (x for x in await catalog.get_locations(rec.sku, trip.store_id) if x.slot_id == rec.slot_id), None
                )
            if loc is None:
                continue
            stops.append(
                Stop(
                    stop_id=f"rec:{rec.rec_id}",
                    kind="optional",
                    pose=loc.approach_pose,
                    label=rec.product_name or rec.sku,
                    slot_id=rec.slot_id,
                    skus=[rec.sku],
                    qty=1,
                    interest=rec.interest,
                    rec_id=rec.rec_id,
                    user_ids=[trip.user_id],
                    service_time_s=8.0,
                )
            )
        request = RouteRequest(
            store_id=trip.store_id,
            start=await self._start_pose(trip),
            stops=stops,
            max_optional_stops=self._rec_limit(),
            user_ids=list(trip.user_ids),
        )
        route = await self.services.require("routing").plan_route(request)
        trip.stop_status = {}
        await self._install_route(trip, route, replan=False)
        return route

    async def _install_route(self, trip: Trip, route: Route, replan: bool) -> None:
        trip.route = route
        self._routed_items.setdefault(trip.trip_id, set()).update(i for st in route.stops for i in st.item_ids)
        changed: list[tuple[str, StopStatus, str]] = []
        for stop in route.stops:
            if stop.kind not in ("mandatory", "optional"):
                continue
            old = trip.stop_status.get(stop.stop_id)
            if old in FINISHED_STOP:
                continue
            new: StopStatus = "skipped" if stop.stop_id in route.skipped_optional else "pending"
            if old != new:
                trip.stop_status[stop.stop_id] = new
                if replan or new == "skipped":
                    changed.append(
                        (stop.stop_id, new, "solver dropped this detour" if new == "skipped" else "re-added")
                    )
        # copy detour costs onto the recommendations
        detours = {s.rec_id: s for s in route.stops if s.rec_id}
        trip.recs = [
            r.model_copy(
                update={"extra_distance_m": detours[r.rec_id].detour_m, "extra_time_s": detours[r.rec_id].detour_s}
            )
            if r.rec_id in detours
            else r
            for r in trip.recs
        ]
        if replan and trip.state == S.EXECUTING:
            await self._transition(trip, S.ROUTE_PLANNED, "re-plan")
            await self._publish("route.planned", trip.trip_id, route=route, replan=True)
            await self._transition(trip, S.EXECUTING, "resume")
        elif trip.state in (S.LIST_READY, S.RECS_READY, S.ROUTE_PLANNED):
            await self._transition(trip, S.ROUTE_PLANNED, f"{len(route.order)} stops, {route.total_distance_m:.0f} m")
            await self._publish("route.planned", trip.trip_id, route=route, replan=replan)
        else:
            await self._publish("route.planned", trip.trip_id, route=route, replan=replan)
        for stop_id, status, reason in changed:
            stop = route.stop(stop_id)
            await self._publish(
                "stop.updated",
                trip.trip_id,
                stop_id=stop_id,
                status=status,
                reason=reason,
                rec_id=stop.rec_id if stop else None,
                slot_id=stop.slot_id if stop else None,
            )
        if trip.recs and not replan:
            await self._publish("recs.ready", trip.trip_id, recs=trip.recs, checkout_recs=trip.checkout_recs)

    async def _precheck(self, trip: Trip) -> None:
        decision = await self._evaluate(trip, None, cart=self._projected_cart(trip))
        trip.mandate_decision = decision

    # ==================================================================================================================
    # execution: the in-store part is a fleet shop goal run by a robot brain
    # ==================================================================================================================
    async def _start_execution(self, trip: Trip) -> None:
        if trip.state == S.EXECUTING or self.is_executing(trip.trip_id):
            return
        reason = "auto-execute: the fleet runs it" if trip.auto_execute else "shopper logged in to the robot"
        if not await self._transition(trip, S.EXECUTING, reason):
            return
        self._exec_tasks[trip.trip_id] = self._spawn(self._execute(trip.trip_id), name=f"exec-{trip.trip_id}")

    async def _execute(self, trip_id: str) -> None:
        """EXECUTING: ``goal = fleet.execute_trip(trip_id, robot_id)``; then completed -> (checkout batch; the robot
        goes back for accepted in-store picks) -> checkout; failed -> checkout if the cart has active lines (+ error)
        else FAILED; cancelled -> FAILED with the reason."""
        trip = self._trips[trip_id]
        goal = await self._run_goal(trip)
        if trip.state in TERMINAL:
            return
        status = goal.status if goal is not None else None
        if status == GoalStatus.CANCELLED:
            why = (goal.error or goal.summary or "cancelled") if goal is not None else "cancelled"
            await self._fail(trip, "fleet", f"the in-store goal was cancelled: {why}")
            return
        if status != GoalStatus.COMPLETED:
            why = "no robot available" if goal is not None and goal.failure_code == "no_robot" else ""
            if goal is not None:
                why = why or goal.error or goal.summary or f"goal {status}"
            why = why or "in-store part did not run"
            if not trip.cart.active_lines():
                await self._fail(trip, "fleet", f"in-store shopping failed ({why}) and the cart is empty")
                return
            await self._error(
                trip_id, "fleet", f"in-store shopping ended early ({why}); checking out what is in the cart"
            )
        try:
            await self._wrap_up_shopping(trip, robot_done=status == GoalStatus.COMPLETED)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            await self._error(trip_id, "gateway", f"checkout suggestions failed: {exc}")
        if self.config.auto_checkout and trip.state == S.EXECUTING:
            await self.start_checkout(trip_id)

    async def _run_goal(self, trip: Trip, *, fetch: bool = False) -> Any:
        """One in-store run: ``fleet.execute_trip`` (a shop goal over the trip's still-pending route stops). Returns the
        terminal goal, or None when no fleet is wired or the run crashed (an ``error`` event either way). ``fetch`` =
        the extra run for suggestions accepted at checkout: a crash there is recoverable (checkout goes on without
        them)."""
        trip_id = trip.trip_id
        goal = None
        try:
            fleet = self.services.fleet
            if fleet is None:
                await self._error(trip_id, "fleet", "no fleet wired; skipping the in-store part")
            else:
                goal = await fleet.execute_trip(trip_id, trip.robot_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            what = "picking the checkout suggestions" if fetch else "in-store execution"
            log.exception("%s crashed for %s", what, trip_id)
            await self._error(trip_id, "fleet", f"{what} failed: {exc}", recoverable=fetch)
        finally:
            trip.current_stop_id = None
            if trip.robot_id and self._robot_trip.get(trip.robot_id) == trip_id:
                self._robot_trip.pop(trip.robot_id, None)
        return goal

    async def _reroute_online(self, trip: Trip, item: ListItem) -> None:
        if item.sku is None:
            return
        offers = []
        if self.services.catalog is not None:
            with contextlib.suppress(Exception):
                offers = await self.services.catalog.get_offers(item.sku, [ONLINE_STORE_ID])
        if not offers or not any(o.in_stock for o in offers):
            await self._error(trip.trip_id, "catalog", f"{item.query} is unavailable online too")
            return
        line = await self._online_line(trip, item)
        if line is None:
            return
        res = await self.add_line(trip.trip_id, line)
        if not res.added and res.decision.verdict == MandateVerdict.NEEDS_APPROVAL:
            self._deferred_online.setdefault(trip.trip_id, []).append(line)

    async def _wrap_up_shopping(self, trip: Trip, robot_done: bool = True) -> None:
        """After the in-store goal: the checkout batch (asked once), then the online lines that need an explicit OK.

        An accepted ONLINE suggestion becomes a planned online line. An accepted IN-STORE suggestion is never put in the
        cart here: only a robot pick does that (``_fetch_accepted``), so the bill never has an in-store line nobody
        picked. ``robot_done`` = the in-store goal completed (the robot is free at checkout and can go back)."""
        trip.current_stop_id = None
        # 'want any of these?' batch (asked once)
        if self.config.checkout_batch and trip.checkout_recs:
            recs = trip.checkout_recs
            req = self._approval(
                trip,
                ApprovalKind.RECOMMENDATION,
                "Want any of these before checkout?",
                body=" · ".join(f"{r.product_name or r.sku}: {r.reason}" for r in recs),
                options=[
                    ApprovalOption(
                        option_id=r.rec_id,
                        label=f"{r.product_name or r.sku}" + (f" {money(r.price_cents)}" if r.price_cents else ""),
                        detail=r.reason,
                    )
                    for r in recs
                ],
                multi=True,
                payload={"kind": "checkout_batch"},
            )
            resp = await self._ask(trip, req)
            chosen = set(resp.chosen()) if resp.approved else set()
            fetch: list[Recommendation] = []
            for rec in recs:
                await self._record_rec_outcome(trip, rec, "accepted" if rec.rec_id in chosen else "declined")
                if rec.rec_id not in chosen:
                    continue
                if rec.store_id != ONLINE_STORE_ID:
                    fetch.append(rec)  # in store: on the bill only once a robot has picked it
                    continue
                line = await self._suggestion_line(trip, rec, source="online")
                # the shopper explicitly picked it in this approval, so a soft mandate ask (e.g. over budget) is met
                res = await self.add_line(trip.trip_id, line, approval_id=req.approval_id)
                if not res.added:
                    await self._error(trip.trip_id, "trust", f"{rec.product_name or rec.sku} not added (mandate)")
            if fetch:
                await self._fetch_accepted(trip, fetch, robot_done, req.approval_id)
        # online lines that needed an explicit OK
        for line in self._deferred_online.pop(trip.trip_id, []):
            req = self._approval(
                trip,
                ApprovalKind.ONLINE_ORDER,
                f"Order {line.name or line.sku} online for {money(line.total_cents)}?",
                body="Out of stock in store; ships with your online order.",
                amount_cents=line.total_cents,
                sku=line.sku,
            )
            resp = await self._ask(trip, req)
            if resp.approved:
                await self.add_line(trip.trip_id, line, approval_id=req.approval_id)

    async def _suggestion_line(self, trip: Trip, rec: Recommendation, source: str) -> CartLine:
        """The cart line an accepted checkout suggestion would become (status 'planned': nothing picked yet)."""
        online = source == "online"
        return CartLine(
            line_id=new_id("line"),
            sku=rec.sku,
            name=rec.product_name,
            category=await self._category(rec.sku),
            qty=1,
            unit_price_cents=rec.price_cents or await self._unit_price(trip, rec.sku, rec, None),
            source="online" if online else "in_store",
            store_id=ONLINE_STORE_ID if online else trip.store_id,
            slot_id=None if online else rec.slot_id,
            status="planned",
            rec_id=rec.rec_id,
        )

    def _fetch_blocker(self, trip: Trip, robot_done: bool) -> str | None:
        """Why no robot can go back for in-store suggestions accepted at checkout (None = one can)."""
        if not self.config.fetch_checkout_suggestions:
            return "the robot has already finished shopping"
        if self.services.fleet is None:
            return "no robot is available to pick it"
        if not robot_done:
            return "the robot could not finish shopping"
        if trip.route is None:
            return "there is no store route to go back on"
        return None

    async def _fetch_stop(self, trip: Trip, rec: Recommendation) -> Stop | None:
        """A MANDATORY route stop for an accepted in-store suggestion (the shopper already said yes: no second ask),
        at a stocked shelf location in the trip's store; None when there is none."""
        catalog = self.services.catalog
        if rec.store_id != trip.store_id or catalog is None:
            return None
        locs = []
        with contextlib.suppress(Exception):
            locs = await catalog.get_locations(rec.sku, trip.store_id)
        stocked = [x for x in locs if x.qty > 0]
        loc = next((x for x in stocked if x.slot_id == rec.slot_id), stocked[0] if stocked else None)
        if loc is None:
            return None
        stop_id = f"rec:{rec.rec_id}"
        if trip.route is not None and trip.route.stop(stop_id) is not None:
            stop_id = f"rec:{rec.rec_id}:checkout"
        return Stop(
            stop_id=stop_id,
            kind="mandatory",
            pose=loc.approach_pose,
            label=rec.product_name or rec.sku,
            slot_id=loc.slot_id,
            skus=[rec.sku],
            qty=1,
            rec_id=rec.rec_id,
            user_ids=[trip.user_id],
            service_time_s=8.0,
        )

    async def _fetch_accepted(self, trip: Trip, recs: list[Recommendation], robot_done: bool, approval_id: str) -> None:
        """In-store suggestions accepted at checkout. The robot already left those shelves, so the gateway must not
        bill them as picked (Trust, CLAUDE.md §8: the bill is what is in the basket). When the in-store goal completed,
        the robot goes back for them: they become mandatory route stops, the route is re-planned from the robot's
        pose, and one more fleet run (a shop goal over just those stops, ending at checkout) picks them; the brain
        adds each line itself after the pick, mandate-checked like any other (``add_line`` bills it at the card's price
        and takes the batch choice as its approval, ``_checkout_picks``). Otherwise, or when a stop has no stocked
        shelf, the mandate denies it, or the pick fails, it stays out of the cart and an ``error`` event says so."""
        trip_id = trip.trip_id

        async def not_added(rec: Recommendation, why: str, arm: str = "gateway") -> None:
            await self._error(
                trip_id,
                arm,
                f"{rec.product_name or rec.sku} not added: {why}. It is not in your cart or on your bill.",
                code="suggestion_not_picked",
            )

        blocker = self._fetch_blocker(trip, robot_done)
        stops: list[tuple[Recommendation, Stop]] = []
        for rec in recs:
            if blocker is not None:
                await not_added(rec, blocker)
                continue
            stop = await self._fetch_stop(trip, rec)
            if stop is None:
                await not_added(rec, "no stocked shelf for it in this store")
                continue
            decision = await self._evaluate(trip, await self._suggestion_line(trip, rec, source="in_store"))
            if decision.verdict == MandateVerdict.DENY:
                await not_added(rec, "your spending rules do not allow it (mandate)", arm="trust")
                continue
            stops.append((rec, stop))
        if not stops or trip.route is None:
            return
        picks = self._checkout_picks.setdefault(trip_id, {})
        for rec, _ in stops:
            picks[rec.rec_id] = (approval_id, rec.price_cents)
        trip.route = trip.route.model_copy(update={"stops": [*trip.route.stops, *(s for _, s in stops)]})
        for _, stop in stops:
            await self.set_stop_status(trip_id, stop.stop_id, "pending", "accepted at checkout: going back for it")
        try:
            await self.replan(trip_id, include_recommendations=False)
        except Exception as exc:  # noqa: BLE001 - the brain still has the stop's slot; it routes itself
            await self._error(trip_id, "routing", f"re-plan for the checkout suggestions failed: {exc}")
        try:
            await self._run_goal(trip, fetch=True)
        finally:
            for rec, _ in stops:
                picks.pop(rec.rec_id, None)
        for rec, stop in stops:
            if any(ln.active and ln.source == "in_store" and ln.rec_id == rec.rec_id for ln in trip.cart.lines):
                continue
            status = trip.stop_status.get(stop.stop_id)
            if status not in FINISHED_STOP:
                await self.set_stop_status(trip_id, stop.stop_id, "failed", "not picked")
            await not_added(
                rec, "your spending rules kept it off the bill" if status == "done" else "the robot could not pick it"
            )

    # ==================================================================================================================
    # checkout -> paid -> followup
    # ==================================================================================================================
    async def _finish_checkout(self, trip_id: str, request: ApprovalRequest, fut: Any) -> None:
        trip = self._trips[trip_id]
        resp: ApprovalResponse = await fut
        if not resp.approved:
            await self._transition(trip, S.EXECUTING, f"checkout {resp.decision}")
            if resp.decision == "timeout":
                await self._error(trip_id, "gateway", "checkout approval timed out; ask again when ready")
            return
        await self._transition(trip, S.CHECKOUT, "checkout approved")
        cart_hash = request.cart_hash or compute_cart_hash(trip.cart, trip_id)
        if resp.cart_hash is None:
            resp = resp.model_copy(update={"cart_hash": cart_hash})
        trust = self.services.require("trust")
        checkout_req = CheckoutRequest(
            trip_id=trip_id,
            user_id=trip.user_id,
            cart=trip.cart.model_copy(deep=True),
            cart_hash=cart_hash,
            budget_cents=trip.budget_cents,
            approval_request=request,
            approval=resp,
            idempotency_key=f"{trip_id}:{cart_hash[:16]}",
        )
        try:
            if self._payment_agent_on(trust):
                from arms.trust import run_payment_agent

                async def on_step(step: Any) -> None:
                    await self._publish("payment.agent_step", trip_id, step=step)

                receipt = await run_payment_agent(
                    trust, checkout_req, on_step, pause_s=self.config.payment_step_pause_s
                )
            else:
                receipt = await trust.checkout(checkout_req)
        except Exception as exc:  # noqa: BLE001
            await self._error(trip_id, "trust", f"payment error: {exc}")
            await self._transition(trip, S.EXECUTING, "payment error")
            return
        trip.receipt = receipt
        await self._publish("checkout.completed", trip_id, receipt=receipt)
        if receipt.status != "succeeded":
            await self._error(trip_id, "trust", f"payment {receipt.status}: {receipt.error or ''}".strip())
            await self._transition(trip, S.EXECUTING, f"payment {receipt.status}")
            return
        await self._transition(trip, S.PAID, f"paid {money(receipt.amount_cents)} via {receipt.provider}")
        await self._record_purchases(trip)
        if self.config.auto_followup:
            await self.send_followup(trip_id)

    def _payment_agent_on(self, trust: Any) -> bool:
        """The Visa payment agent needs a TrustCore (mandates + adapter + audit chain): the fake and the real trust."""
        on = self.config.payment_agent
        if on is None:
            on = bool(get_settings().picky_payment_agent)
        return bool(on) and hasattr(trust, "audit") and hasattr(trust, "get_mandate")

    async def _record_purchases(self, trip: Trip) -> None:
        rec_svc = self.services.recommender
        if rec_svc is None:
            return
        photos = self.photo_lookup or (lambda _trip_id, _sku: [])
        records = [
            PurchaseRecord(
                purchase_id=new_id("pur"),
                user_id=ln.user_id or trip.user_id,
                sku=ln.sku,
                name=ln.name,
                qty=ln.qty,
                unit_price_cents=ln.unit_price_cents,
                store_id=ln.store_id,
                trip_id=trip.trip_id,
                photo_urls=photos(trip.trip_id, ln.sku),
            )
            for ln in trip.cart.active_lines()
        ]
        try:
            await rec_svc.record_purchase(records)
        except Exception as exc:  # noqa: BLE001
            await self._error(trip.trip_id, "recommender", f"record_purchase failed: {exc}")

    async def _feedback_from_reply(self, trip: Trip, msg: InboundMessage, interp: ReplyInterpretation) -> None:
        sku = interp.sku or (trip.followup.about_skus[0] if trip.followup and trip.followup.about_skus else None)
        fb = Feedback(
            feedback_id=new_id("fb"),
            user_id=msg.user_id or trip.user_id,
            sku=sku,
            trip_id=trip.trip_id,
            rating=interp.rating,
            text=msg.text,
            channel=msg.channel,
            tags=list(interp.feedback_tags),
        )
        try:
            await self.submit_feedback(fb)
        except Exception as exc:  # noqa: BLE001
            await self._error(trip.trip_id, "recommender", f"feedback not recorded: {exc}")
            if trip.state in (S.PAID, S.FOLLOWUP):
                await self._transition(trip, S.DONE, "feedback (not recorded)")

    async def _followup_timeout(self, trip_id: str, delay: float) -> None:
        await asyncio.sleep(delay)
        trip = self._trips[trip_id]
        if trip.state in (S.PAID, S.FOLLOWUP):
            await self._transition(trip, S.DONE, "no follow-up reply")

    def _default_followup(self, trip: Trip) -> OutboundMessage:
        lines = trip.receipt.lines if trip.receipt else trip.cart.active_lines()
        line = lines[0] if lines else None
        return OutboundMessage(
            channel="photon",
            user_id=trip.user_id,
            trip_id=trip.trip_id,
            text=f"How was the {line.name or line.sku}? Reply 1-5." if line else "How was your trip? Reply 1-5.",
            about_skus=[line.sku] if line else [],
            quick_replies=["1", "2", "3", "4", "5"],
        )

    async def _unaudited_basket(self, trip: Trip) -> str | None:
        """contracts/api.md "Who writes what": checkout starts only when the loop's basket audit has closed every
        provisional line (``PlanRunState.provisional_open`` is false). None = go ahead (also in direct mode, without a
        runner or plan, or when the run state can't be read: the guard never breaks a v0.2 checkout)."""
        runner = getattr(self.services, "runner", None)
        if runner is None or not trip.goal_id:
            return None
        try:
            plan = await runner.current_plan(trip.goal_id)
            if plan is None:
                return None
            state = await runner.run_state(plan.plan_id)
        except Exception as exc:  # noqa: BLE001 - a lookup error must not block a v0.2 checkout
            log.debug("checkout guard: no run state for goal %s (%s)", trip.goal_id, exc)
            return None
        if not getattr(state, "provisional_open", False):
            return None
        return (
            "checkout blocked: the basket camera could not confirm some items and the robot's basket audit has not "
            "passed yet"
        )

    def _checkout_status(
        self, trip: Trip, decision: MandateDecision | None = None, approval: ApprovalRequest | None = None
    ) -> CheckoutStatus:
        return CheckoutStatus(
            trip_id=trip.trip_id,
            state=trip.state,
            cart_hash=compute_cart_hash(trip.cart, trip.trip_id),
            total_cents=trip.cart.total_cents,
            decision=decision or trip.mandate_decision,
            approval=approval,
            receipt=trip.receipt,
        )

    # ==================================================================================================================
    # approvals
    # ==================================================================================================================
    def _approval(self, trip: Trip, kind: ApprovalKind, title: str, **kw: Any) -> ApprovalRequest:
        kw.setdefault("expires_s", self.config.approval_timeout_s)
        return ApprovalRequest(
            approval_id=new_id("appr"),
            trip_id=trip.trip_id,
            user_id=trip.user_id,
            store_id=trip.store_id,
            robot_id=trip.robot_id,
            goal_id=trip.goal_id,
            kind=kind,
            title=title,
            channels=list(self.config.approval_channels),  # type: ignore[arg-type]
            **kw,
        )

    async def _ask(self, trip: Trip, request: ApprovalRequest) -> ApprovalResponse:
        approvals = self.services.approvals
        if approvals is None:  # nobody to ask: deterministic default (never used for checkout)
            return ApprovalResponse(approval_id=request.approval_id, decision="timeout", channel="system")
        entered = False
        if trip.state == S.EXECUTING:
            entered = await self._transition(trip, S.AWAITING_APPROVAL, request.title)
        try:
            return await approvals.request(request)
        finally:
            if entered and trip.state == S.AWAITING_APPROVAL:
                await self._transition(trip, S.EXECUTING, "answered")

    async def _on_approval(self, phase: str, request: ApprovalRequest, response: ApprovalResponse | None) -> None:
        trip = self._trips.get(request.trip_id) if request.trip_id else None
        if trip is None:
            return
        if phase == "requested" and all(a.approval_id != request.approval_id for a in trip.approvals):
            trip.approvals.append(request)
        elif phase == "resolved" and response is not None:
            trip.approval_responses.append(response)

    # approval kinds that can stand in for a line-level mandate ask (never pick_choice / checkout)
    LINE_APPROVAL_KINDS = frozenset(
        {ApprovalKind.OVER_THRESHOLD, ApprovalKind.RECOMMENDATION, ApprovalKind.ONLINE_ORDER, ApprovalKind.SUBSTITUTION}
    )

    @staticmethod
    def _approval_key(trip: Trip, approval_id: str, line: CartLine) -> str:
        """One use per approval; a multi-select batch is one use per chosen rec."""
        req = next((a for a in trip.approvals if a.approval_id == approval_id), None)
        return f"{approval_id}:{line.rec_id}" if req is not None and req.multi else approval_id

    def _approved(self, trip: Trip, approval_id: str, line: CartLine) -> bool:
        """True only if ``approval_id`` is an unused, granted approval for exactly this line (CLAUDE.md §8).

        The LLM can pass any approval_id to add_to_cart, so the id alone proves nothing: the request must be a
        line-level kind, name this sku (or, for a multi-select batch, list this rec as chosen), cover the line's
        total, and not have been spent on another line already.
        """
        req = next((a for a in trip.approvals if a.approval_id == approval_id), None)
        resp = next((r for r in trip.approval_responses if r.approval_id == approval_id), None)
        if req is None or resp is None or not resp.approved or req.kind not in self.LINE_APPROVAL_KINDS:
            return False
        if self._approval_key(trip, approval_id, line) in self._used_approvals.get(trip.trip_id, set()):
            return False
        if req.multi:
            return line.rec_id is not None and line.rec_id in resp.chosen() and req.sku in (None, line.sku)
        if req.sku != line.sku:
            return False
        return req.amount_cents is not None and req.amount_cents >= line.total_cents

    async def _record_rec_outcome(self, trip: Trip, rec: Recommendation | None, outcome: str) -> None:
        if rec is None or self.services.trust is None:
            return
        with contextlib.suppress(Exception):
            entry = TransparencyEntry.from_recommendation(rec, trip.trip_id).model_copy(update={"outcome": outcome})
            await self.services.trust.record_transparency(entry)

    # ==================================================================================================================
    # robots: live robot.state (all robots) + poses
    # ==================================================================================================================
    async def _forward_states(self) -> None:
        """Every robot's state -> full rate on the frame hub (/ws/frames), <= robot_state_hz per robot (+ every status
        change) on the sequenced bus (/ws). Envelope: store_id, robot_id (+ the trip it executes)."""
        robot = self.services.robot
        if robot is None:
            return
        period = 1.0 / self.config.robot_state_hz if self.config.robot_state_hz > 0 else 0.0
        last: dict[str, tuple[float, Any]] = {}
        try:
            async for state in robot.states():
                rid = getattr(state, "robot_id", None)
                if rid is None:
                    continue
                trip_id = self._robot_trip.get(rid)
                trip = self._trips.get(trip_id) if trip_id else None
                env: dict[str, Any] = trip_envelope(trip) if trip is not None else {"trip_id": None}
                env.update(store_id=state.store_id or env.get("store_id"), robot_id=rid)
                event = make_event("robot.state", source="gateway", state=state, **env)
                hub_pub = getattr(self.services.events, "frames", None)
                if hub_pub is not None:
                    hub_pub.publish(event)
                now = time.monotonic()
                prev_t, prev_status = last.get(rid, (0.0, None))
                if state.status != prev_status or now - prev_t >= period:
                    last[rid] = (now, state.status)
                    await self._emit(event)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.warning("robot state stream ended: %s", exc)

    async def _robot_pose(self, robot_id: str | None) -> Pose2D | None:
        robot = self.services.robot
        if robot is None or robot_id is None:
            return None
        with contextlib.suppress(Exception):
            return (await robot.state(robot_id)).pose
        return None

    # ==================================================================================================================
    # misc helpers
    # ==================================================================================================================
    def _rec_limit(self) -> int:
        return self.config.rec_limit if self.config.rec_limit is not None else get_settings().picky_rec_max_stops

    async def _home_store(self, user_id: str) -> str:
        profile = await self._profile(user_id)
        if profile is not None and profile.home_store_id:
            return profile.home_store_id
        return get_settings().picky_default_store

    async def _profile(self, user_id: str) -> UserProfile | None:
        rec = self.services.recommender
        if rec is None:
            return None
        try:
            return await rec.get_profile(user_id)
        except Exception:  # noqa: BLE001 - unknown user -> no personalization
            return None

    async def _start_pose(self, trip: Trip) -> Pose2D:
        """Route start: the trip robot's current pose (re-plans, and pre-assigned robots at their docks), else the
        store entrance."""
        pose = await self._robot_pose(trip.robot_id)
        if pose is not None:
            return pose
        plan = await self.services.require("catalog").get_planogram(trip.store_id)
        return plan.start_pose

    async def _unit_price(self, trip: Trip, sku: str, rec: Recommendation | None, product: Any) -> int:
        if rec is not None and rec.price_cents is not None:
            return rec.price_cents
        catalog = self.services.catalog
        if catalog is not None:
            with contextlib.suppress(Exception):
                offers = await catalog.get_offers(sku, [trip.store_id])
                if offers:
                    return offers[0].price_cents
        if product is not None:
            return product.price_cents
        return 0

    async def _category(self, sku: str) -> str:
        catalog = self.services.catalog
        if catalog is not None:
            with contextlib.suppress(Exception):
                product = await catalog.get_product(sku)
                if product is not None:
                    return product.category
        return "uncategorized"

    def _projected_cart(self, trip: Trip) -> Cart:
        """Current cart + the in-store list items at their estimated prices (for the pre-check / checkout batch)."""
        lines = list(trip.cart.lines)
        have = {ln.list_item_id for ln in lines if ln.list_item_id}
        for item in trip.shopping_list.items if trip.shopping_list else []:
            if item.sourcing != "in_store" or item.sku is None or item.item_id in have:
                continue
            lines.append(
                CartLine(
                    line_id=f"proj_{item.item_id}",
                    sku=item.sku,
                    name=item.query,
                    category=item.category or "uncategorized",
                    qty=item.qty,
                    unit_price_cents=(item.est_line_cents or 0) // max(item.qty, 1),
                    source="in_store",
                    store_id=trip.store_id,
                    list_item_id=item.item_id,
                )
            )
        return trip.cart.model_copy(update={"lines": lines})

    async def _evaluate(self, trip: Trip, line: CartLine | None, cart: Cart | None = None) -> MandateDecision:
        cart = cart or trip.cart
        trust = self.services.trust
        if trust is None:
            decision = MandateDecision(
                decision_id=new_id("dec"),
                user_id=trip.user_id,
                trip_id=trip.trip_id,
                verdict=MandateVerdict.APPROVE,
                messages=["trust arm not wired (no mandate enforced)"],
                scope="line" if line else "cart",
                sku=line.sku if line else None,
            )
        else:
            try:
                decision = await trust.evaluate(trip.user_id, cart, line, trip.budget_cents, trip.trip_id)
            except Exception as exc:  # noqa: BLE001 - fail closed: ask the shopper
                await self._error(trip.trip_id, "trust", f"mandate check failed: {exc}")
                decision = MandateDecision(
                    decision_id=new_id("dec"),
                    user_id=trip.user_id,
                    trip_id=trip.trip_id,
                    verdict=MandateVerdict.NEEDS_APPROVAL,
                    messages=["mandate engine unavailable; asking you to confirm"],
                    scope="line" if line else "cart",
                    sku=line.sku if line else None,
                )
        await self._publish("mandate.decision", trip.trip_id, decision=decision)
        return decision

    async def _cart_changed(self, trip: Trip) -> None:
        info = await self._online_store_info()
        if info is not None:
            trip.cart.shipping_cents = online_shipping_cents(
                trip.cart.online_cents, info.shipping_cents or 0, info.free_shipping_over_cents
            )
        else:
            trip.cart.shipping_cents = 0
        budget = trip.budget_cents
        await self._publish(
            "cart.updated",
            trip.trip_id,
            cart=trip.cart.model_copy(deep=True),
            total_cents=trip.cart.total_cents,
            budget_cents=budget,
            remaining_cents=(budget - trip.cart.total_cents) if budget is not None else None,
        )

    async def _online_store_info(self) -> StoreInfo | None:
        if self._online_info is None and self.services.catalog is not None:
            with contextlib.suppress(Exception):
                stores = await self.services.catalog.list_stores()
                self._online_info = next((s for s in stores if s.store_id == ONLINE_STORE_ID), None)
        return self._online_info

    async def _transition(self, trip: Trip, dst: TripState, reason: str = "") -> bool:
        fsm = self._fsm[trip.trip_id]
        src = fsm.state
        if src == dst and dst not in TRIP_TRANSITIONS.get(src, frozenset()):
            return False
        if not fsm.can(dst):
            log.warning("trip %s: illegal transition %s -> %s (%s)", trip.trip_id, src, dst, reason)
            return False
        fsm.transition(dst, reason)
        trip.state = dst
        trip.history.append(TripTransition(from_state=src, to_state=dst, reason=reason))
        trip.updated_at = trip.history[-1].ts
        await self._publish("trip.state_changed", trip.trip_id, from_state=src, to_state=dst, reason=reason)
        return True

    async def _fail(self, trip: Trip, arm: str, message: str) -> None:
        trip.error = message
        await self._error(trip.trip_id, arm, message, recoverable=False)
        await self._transition(trip, S.FAILED, message)

    async def _publish(self, type_: str, trip_id: str | None, **payload: Any) -> None:
        trip = self._trips.get(trip_id) if trip_id else None
        env: dict[str, Any] = trip_envelope(trip) if trip is not None else {"trip_id": trip_id}
        try:
            event = make_event(type_, source="gateway", **{**env, **payload})
        except Exception as exc:  # noqa: BLE001
            log.warning("bad %s event: %s", type_, exc)
            return
        await self._emit(event)

    async def _emit(self, event: Any) -> None:
        events = self.services.events
        if events is None:
            return
        try:
            await events.publish(event)
        except Exception as exc:  # noqa: BLE001 - never kill a trip over a UI event
            log.warning("publish %s failed: %s", getattr(event, "type", "?"), exc)

    async def _error(
        self, trip_id: str | None, arm: str, message: str, recoverable: bool = True, code: str | None = None
    ) -> None:
        log.warning("[%s] %s: %s", trip_id or "-", arm, message)
        await self._publish("error", trip_id, arm=arm, message=message, recoverable=recoverable, code=code)


__all__ = ["OrchestratorConfig", "TripOrchestrator"]
