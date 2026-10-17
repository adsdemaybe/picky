"""TripOrchestrator: runs the trip state machine end to end (CLAUDE.md §2) using only ``services.*``.

It is the gateway's ``TripController`` (the only thing that mutates a ``Trip``) and publishes every user-visible
event. Flow per trip::

    intent -> agent.expand_intent -> list.ready -> agent.source_list -> sourcing.split (+ online lines in the cart)
      -> recommender.recommend / checkout_batch -> recs.ready (+ transparency log) -> routing.plan_route
      -> route.planned -> trust.evaluate (projected cart) -> awaiting_login
    login -> robot.reset -> per stop: [optional: approval -> yes/no -> replan] -> navigate -> scan -> blur + identify
      -> catalog.apply_scan -> scan.result -> [produce: rank_freshness -> pick.choices -> approval]
      -> trust.evaluate(line) [-> over_threshold approval] -> pick x qty -> place_in_basket -> record_pick
      -> item.picked -> add_line -> cart.updated -> verify_basket -> basket.verified
    -> checkout-batch question -> start_checkout: trust.evaluate -> checkout approval bound to cart_hash
      -> trust.checkout -> checkout.completed -> paid -> record_purchase -> agent.compose_followup
      -> messaging.send -> followup.sent -> reply -> agent.interpret_reply -> recommender.record_feedback
      -> feedback.received + profile.updated -> done

Failures never kill the trip unless planning itself is impossible: every degraded step emits an ``error`` event and
the trip continues (OOS -> re-source online; pick failed -> stop failed; sim disconnected -> robot fake; approval
timeout -> skip / freshest).
"""

import asyncio
import contextlib
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from contracts.api import AddLineResult, CheckoutStatus, CreateTripRequest, RobotLoginRequest
from contracts.catalog import StoreInfo, online_shipping_cents
from contracts.common import ONLINE_STORE_ID, Pose2D, money
from contracts.events import make_event
from contracts.messaging import InboundMessage, OutboundMessage, ReplyInterpretation
from contracts.profile import Feedback, ProfileUpdate, PurchaseRecord, UserProfile
from contracts.routing import Route, RouteLeg, RouteRequest, Stop, StopStatus
from contracts.services import Services
from contracts.shopping import TRIP_TRANSITIONS, Cart, CartLine, Intent, ListItem, Recommendation, TripState
from contracts.sim import RobotError, RobotStatus
from contracts.tools import AgentReply
from contracts.trip import Trip, TripTransition
from contracts.trust import (
    ApprovalKind,
    ApprovalOption,
    ApprovalRequest,
    ApprovalResponse,
    CheckoutRequest,
    MandateDecision,
    MandateReason,
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
    """True = start executing as soon as the route is planned (headless demo / tests)."""
    auto_checkout: bool = True
    """Start checkout when the last stop is done."""
    auto_followup: bool = True
    """Send the post-purchase question right after payment (POST /api/trips/{id}/followup re-sends it)."""
    followup_timeout_s: float | None = None
    """Finish the trip (done) if no feedback arrives in time; None = wait for the reply."""
    checkout_batch: bool = True
    """Ask the batched 'want any of these?' question before checkout."""
    rec_limit: int | None = None
    """Detour recs per trip; None = PICKY_REC_MAX_STOPS."""
    robot_state_hz: float = 2.0
    """robot.state rate on the sequenced bus (/ws); full rate goes to /ws/frames."""


class TripOrchestrator:
    """``contracts.services.TripController`` + the pipeline driver. Construct freely; wiring happens in ``start()``."""

    def __init__(
        self,
        services: Services,
        config: OrchestratorConfig | None = None,
        fallback_robot: Callable[[], Any] | None = None,
    ) -> None:
        self.services = services
        self.config = config or OrchestratorConfig()
        self._fallback_robot = fallback_robot
        self._trips: dict[str, Trip] = {}
        self._fsm: dict[str, StateMachine] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._at: dict[str, str | None] = {}
        self._deferred_online: dict[str, list[CartLine]] = {}
        self._used_approvals: dict[str, set[str]] = {}
        self._declined_skus: dict[str, set[str]] = {}
        """user_id -> detour SKUs the shopper said no to (passed as exclude_skus on the next trips)."""
        self._tasks: set[asyncio.Task[Any]] = set()
        self._exec_tasks: dict[str, asyncio.Task[Any]] = {}
        self._robot_lock: asyncio.Lock | None = None
        self._online_info: StoreInfo | None = None
        self._started = False

    # ==================================================================================================================
    # lifecycle
    # ==================================================================================================================
    async def start(self) -> None:
        """Register the approval listener and the messaging inbound handler; start messaging."""
        self._wire()
        messaging = self.services.messaging
        if messaging is not None:
            try:
                await messaging.start()
            except Exception as exc:  # noqa: BLE001 - web channel still works
                log.warning("messaging.start failed: %s", exc)
                await self._error(None, "messaging", f"messaging unavailable: {exc}")

    async def stop(self) -> None:
        tasks = list(self._tasks)
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
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
        """True while the in-store run (robot loop + checkout question) is still going."""
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
        trip = Trip(
            trip_id=trip_id,
            user_id=req.user_id,
            user_ids=user_ids,
            store_id=store_id,
            intent=intent,
            cart=Cart(cart_id=new_id("cart"), trip_id=trip_id, user_id=req.user_id),
            history=[TripTransition(from_state=None, to_state=S.INTENT, reason="created")],
        )
        self._trips[trip_id] = trip
        self._fsm[trip_id] = StateMachine(TRIP_TRANSITIONS, initial=S.INTENT, any_to=[S.FAILED], terminal=TERMINAL)
        self._locks[trip_id] = asyncio.Lock()
        await self._publish("trip.created", trip_id, trip=trip.model_copy(deep=True))
        await self._publish("intent.received", trip_id, intent=intent)
        self._spawn(self._plan(trip_id), name=f"plan-{trip_id}")
        return trip.model_copy(deep=True)

    async def update_list(self, trip_id: str, shopping_list: Any) -> Trip:
        trip = self._trips[trip_id]
        shopping_list = shopping_list.model_copy(update={"trip_id": trip_id})
        trip.shopping_list = shopping_list
        if trip.state in PRE_TRIP:
            await self._transition(trip, S.LIST_READY, "list edited")
            await self._publish("list.ready", trip_id, shopping_list=shopping_list)
            self._spawn(self._source_recs_route(trip_id), name=f"replan-{trip_id}")
        else:
            await self._publish("list.ready", trip_id, shopping_list=shopping_list)
        return trip.model_copy(deep=True)

    async def add_line(self, trip_id: str, line: CartLine, approval_id: str | None = None) -> AddLineResult:
        trip = self._trips[trip_id]
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
        trip = self._trips[trip_id]
        if req.user_id not in trip.user_ids and req.user_id != trip.user_id:
            raise PermissionError(f"user {req.user_id} is not on trip {trip_id}")
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
                res = await messaging.send(msg)
                if not res.ok:
                    await self._error(trip_id, "messaging", f"follow-up not delivered: {res.error}")
            except Exception as exc:  # noqa: BLE001
                await self._error(trip_id, "messaging", f"follow-up send failed: {exc}")
        if self.config.followup_timeout_s is not None:
            self._spawn(self._followup_timeout(trip_id, self.config.followup_timeout_s))
        return msg

    async def handle_inbound(self, msg: InboundMessage) -> ReplyInterpretation:
        """Messaging inbound (and POST /api/dev/inbound): approval answers, feedback, new intents, chat."""
        trip = self.active_trip_for(msg.user_id)
        if trip is not None and msg.trip_id is None:
            msg = msg.model_copy(update={"trip_id": trip.trip_id})
        approvals = self.services.approvals
        pending = approvals.pending(trip.trip_id) if (approvals is not None and trip is not None) else []
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
            await self.create_trip(
                CreateTripRequest(user_id=msg.user_id, text=interp.intent_text or msg.text, channel=msg.channel)
            )
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
    async def _plan(self, trip_id: str) -> None:
        trip = self._trips[trip_id]
        try:
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
        if trip.robot_logged_in or self.config.auto_login:
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
        """Up to ``limit`` detours, in order, while they fit ``headroom``; plus at most one that does not fit (its ask
        says it busts the budget, so the shopper decides). Returns the kept detours and the headroom they leave."""
        if headroom is None:
            return recs[:limit], None
        kept: list[Recommendation] = []
        over_kept = False
        for rec in recs:
            if len(kept) >= limit:
                break
            price = rec.price_cents or 0
            if price <= headroom:
                kept.append(rec)
                headroom -= price
            elif not over_kept:
                kept.append(rec)
                over_kept = True
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
        self._at[trip.trip_id] = route.order[0] if route.order else None
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
    # execution
    # ==================================================================================================================
    async def _start_execution(self, trip: Trip) -> None:
        if trip.state == S.EXECUTING:
            return
        if not await self._transition(trip, S.EXECUTING, "shopper logged in to the robot"):
            return
        self._exec_tasks[trip.trip_id] = self._spawn(self._execute(trip.trip_id), name=f"exec-{trip.trip_id}")

    async def _execute(self, trip_id: str) -> None:
        trip = self._trips[trip_id]
        if self._robot_lock is None:
            self._robot_lock = asyncio.Lock()
        async with self._robot_lock:  # one physical robot
            forwarders: list[asyncio.Task[Any]] = []
            try:
                robot = self.services.robot
                if robot is None:
                    await self._error(trip_id, "robot", "no robot wired; skipping the in-store part")
                else:
                    if hasattr(robot, "set_trip"):
                        robot.set_trip(trip_id)
                    forwarders = self._start_forwarders(trip_id, robot)
                    try:
                        await self._robot_call(trip, "reset", trip.store_id)
                    except RobotError as exc:
                        await self._error(trip_id, "robot", f"reset failed: {exc}", code=exc.code)
                    forwarders = self._restart_forwarders_if_swapped(trip_id, robot, forwarders)
                    while (stop := self._next_stop(trip)) is not None:
                        await self._visit(trip, stop)
                        forwarders = self._restart_forwarders_if_swapped(trip_id, robot, forwarders)
                        robot = self.services.robot
                    await self._go_to_end(trip)
                await self._wrap_up_shopping(trip)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.exception("execution crashed for %s", trip_id)
                await self._error(trip_id, "gateway", f"execution error: {exc}", recoverable=False)
            finally:
                for t in forwarders:
                    t.cancel()
        if self.config.auto_checkout and trip.state in (S.EXECUTING,):
            await self.start_checkout(trip_id)

    def _next_stop(self, trip: Trip) -> Stop | None:
        if trip.route is None:
            return None
        for stop in trip.route.ordered_stops():
            if stop.kind in ("mandatory", "optional") and trip.stop_status.get(stop.stop_id, "pending") == "pending":
                return stop
        return None

    async def _set_stop(self, trip: Trip, stop: Stop, status: StopStatus, reason: str = "") -> None:
        trip.stop_status[stop.stop_id] = status
        await self._publish(
            "stop.updated",
            trip.trip_id,
            stop_id=stop.stop_id,
            status=status,
            reason=reason,
            rec_id=stop.rec_id,
            slot_id=stop.slot_id,
        )

    async def _visit(self, trip: Trip, stop: Stop) -> None:
        trip_id = trip.trip_id
        sku = stop.skus[0] if stop.skus else None
        rec = next((r for r in trip.recs if r.rec_id == stop.rec_id), None) if stop.rec_id else None
        if sku is None or stop.slot_id is None:
            await self._set_stop(trip, stop, "failed", "stop has no slot/sku")
            return

        # optional detour: ask first (declining saves the walk), then re-plan
        if stop.kind == "optional":
            accepted = await self._ask_detour(trip, stop, rec)
            await self._record_rec_outcome(trip, rec, "accepted" if accepted else "declined")
            if not accepted:
                self._declined_skus.setdefault(trip.user_id, set()).add(sku)
                await self._set_stop(trip, stop, "declined", "shopper said no")
                try:
                    await self.replan(trip_id)
                except Exception as exc:  # noqa: BLE001
                    await self._error(trip_id, "routing", f"re-plan failed: {exc}")
                return

        # navigate
        trip.current_stop_id = stop.stop_id
        await self._set_stop(trip, stop, "navigating")
        try:
            state = await self._navigate_to(trip, stop)
        except RobotError as exc:
            await self._set_stop(trip, stop, "failed", f"navigation failed: {exc.code}")
            await self._error(trip_id, "robot", f"could not reach {stop.label}: {exc}", code=exc.code)
            return
        self._at[trip_id] = stop.stop_id
        await self._set_stop(trip, stop, "arrived")
        await self._publish("robot.arrived", trip_id, stop_id=stop.stop_id, slot_id=stop.slot_id, pose=state.pose)

        # scan + identify (faces blurred first)
        report = None
        frame = None
        try:
            raw = await self._robot_call(trip, "scan", stop.slot_id)
            frame = await self._blur(raw)
            perception = self.services.perception
            if perception is not None and frame is not None:
                report = await perception.identify(frame, trip.store_id, stop.slot_id, sku)
                report = report.model_copy(update={"trip_id": trip_id})
                if self.services.catalog is not None:
                    with contextlib.suppress(Exception):
                        await self.services.catalog.apply_scan(report)
                await self._publish("scan.result", trip_id, report=report)
        except RobotError as exc:
            await self._error(trip_id, "robot", f"scan failed: {exc}", code=exc.code)
        except Exception as exc:  # noqa: BLE001
            await self._error(trip_id, "perception", f"scan/identify failed: {exc}")

        if report is not None and report.observed_count == 0 and (report.count_is_exact or report.mismatch):
            await self._out_of_stock(trip, stop, sku)
            return

        product = None
        if self.services.catalog is not None:
            with contextlib.suppress(Exception):
                product = await self.services.catalog.get_product(sku)
        unit = await self._unit_price(trip, sku, rec, product)
        line = CartLine(
            line_id=new_id("line"),
            sku=sku,
            name=product.name if product else (rec.product_name if rec else stop.label),
            category=product.category if product else "uncategorized",
            qty=max(stop.qty, 1),
            unit_price_cents=unit,
            source="in_store",
            store_id=trip.store_id,
            slot_id=stop.slot_id,
            status="picked",
            rec_id=stop.rec_id,
            deal_id=(rec.deal.deal_id if rec and rec.deal else None),
            list_item_id=stop.item_ids[0] if stop.item_ids else None,
            user_id=(stop.user_ids[0] if stop.user_ids else trip.user_id),
        )

        # mandate pre-check on the line before we pick anything
        approval_id = None
        decision = await self._evaluate(trip, line)
        if decision.verdict == MandateVerdict.DENY:
            await self._set_stop(trip, stop, "failed", "blocked by mandate: " + "; ".join(decision.messages))
            return
        if decision.verdict == MandateVerdict.NEEDS_APPROVAL:
            req = self._approval(
                trip,
                ApprovalKind.OVER_THRESHOLD,
                self._over_title(line.name or sku, line.total_cents, decision),
                body="; ".join(decision.messages),
                amount_cents=line.total_cents,
                sku=sku,
                rec_id=stop.rec_id,
                payload={"reasons": [str(r) for r in decision.reasons]},
            )
            resp = await self._ask(trip, req)
            if not resp.approved:
                await self._set_stop(trip, stop, "declined", "over threshold; shopper declined")
                return
            approval_id = req.approval_id

        # produce: freshness top-3 -> shopper picks
        chosen: list[str | None] = [None] * line.qty
        is_produce = product is not None and product.category == "produce"
        if is_produce and frame is not None and self.services.perception is not None:
            chosen = await self._pick_choice(trip, stop, sku, frame, line.qty)

        # pick + place
        picked_ids: list[str] = []
        for instance_id in chosen:
            item = await self._pick_one(trip, stop, sku, instance_id)
            if item is None:
                break
            picked_ids.append(item.instance_id)
            await self._publish(
                "item.picked",
                trip_id,
                sku=sku,
                slot_id=stop.slot_id,
                instance_id=item.instance_id,
                line_id=line.line_id,
                stop_id=stop.stop_id,
            )
        if not picked_ids:
            await self._set_stop(trip, stop, "failed", "pick failed")
            return
        line = line.model_copy(update={"qty": len(picked_ids), "instance_ids": picked_ids})
        res = await self.add_line(trip_id, line, approval_id=approval_id)
        if not res.added:
            await self._error(trip_id, "trust", f"{line.name} picked but not added: {res.decision.verdict}")
        await self._verify_basket(trip)
        await self._set_stop(trip, stop, "done", f"picked {len(picked_ids)}")

    @staticmethod
    def _over_title(name: str, cents: int, decision: MandateDecision) -> str:
        """Shopper-facing question for a line the mandate wants confirmed, naming the rule that fired."""
        reasons = set(decision.reasons)
        if MandateReason.OVER_BUDGET in reasons and decision.budget_cents is not None:
            over = (decision.total_cents or 0) - decision.budget_cents
            return (
                f"{name} ({money(cents)}) puts you {money(over)} over your {money(decision.budget_cents)} budget. "
                "Add it anyway?"
            )
        if MandateReason.ABOVE_ASK_THRESHOLD in reasons:
            return f"{name} is {money(cents)}, above your ask-me limit. Add it?"
        return f"{name} is {money(cents)}. Add it anyway?"

    async def _ask_detour(self, trip: Trip, stop: Stop, rec: Recommendation | None) -> bool:
        name = (rec.product_name if rec else None) or stop.label or (stop.skus[0] if stop.skus else "this")
        price = f" ({money(rec.price_cents)})" if rec and rec.price_cents is not None else ""
        extra = ""
        if stop.detour_m is not None:
            extra = f" Detour: +{stop.detour_m:.0f} m"
            if stop.detour_s is not None:
                extra += f", {stop.detour_s:.0f} s"
        over = self._budget_overage(trip, rec.price_cents if rec else None)
        title = f"Grab {name}{price}?"
        if over and trip.budget_cents is not None:  # say it up front: the shopper decides, the budget is not silent
            title += f" Heads up: it puts you {money(over)} over your {money(trip.budget_cents)} budget."
        req = self._approval(
            trip,
            ApprovalKind.RECOMMENDATION,
            title,
            body=((rec.reason if rec else "") + ("." if rec else "") + extra).strip(),
            amount_cents=rec.price_cents if rec else None,
            sku=stop.skus[0] if stop.skus else None,
            rec_id=stop.rec_id,
            payload={
                "interest": stop.interest,
                "sponsored": rec.sponsored if rec else False,
                "over_budget": over > 0,
                "over_budget_cents": over,
            },
        )
        resp = await self._ask(trip, req)
        return resp.approved

    async def _pick_choice(self, trip: Trip, stop: Stop, sku: str, frame: Any, qty: int) -> list[str | None]:
        perception = self.services.require("perception")
        try:
            choice = await perception.rank_freshness(frame, stop.slot_id, sku, k=3)
        except Exception as exc:  # noqa: BLE001
            await self._error(trip.trip_id, "perception", f"freshness ranking failed: {exc}")
            return [None] * qty
        flat = len(choice.candidates) > 1 and len({round(c.score, 3) for c in choice.candidates}) == 1
        if not choice.candidates or flat:
            return [None] * qty  # nothing to choose between (no freshness signal)
        ranked = sorted(choice.candidates, key=lambda c: c.rank)
        req = self._approval(
            trip,
            ApprovalKind.PICK_CHOICE,
            f"Which {stop.label or sku}? #1 is the freshest",
            sku=sku,
            options=[
                ApprovalOption(
                    option_id=c.instance_id,
                    label=f"#{c.rank} · freshness {c.score:.0%}",
                    detail=c.reason or None,
                    thumbnail_b64=c.thumbnail_b64,
                )
                for c in ranked
            ],
            expires_s=min(self.config.approval_timeout_s, 60.0),
        )
        await self._publish("pick.choices", trip.trip_id, choice=choice, approval_id=req.approval_id)
        resp = await self._ask(trip, req)
        first = (resp.chosen() or [ranked[0].instance_id])[0] if resp.approved else ranked[0].instance_id
        order = [first] + [c.instance_id for c in ranked if c.instance_id != first]
        out: list[str | None] = list(order[:qty])
        out += [None] * (qty - len(out))
        return out

    async def _pick_one(self, trip: Trip, stop: Stop, sku: str, instance_id: str | None) -> Any:
        for attempt in range(2):
            try:
                item = await self._robot_call(trip, "pick", stop.slot_id, sku, instance_id if attempt == 0 else None)
                await self._robot_call(trip, "place_in_basket")
            except RobotError as exc:
                if exc.code in ("grasp_failed", "busy") and attempt == 0:
                    continue
                if exc.code == "no_instance" and instance_id is not None and attempt == 0:
                    continue
                await self._error(trip.trip_id, "robot", f"pick of {sku} failed: {exc}", code=exc.code)
                return None
            if self.services.catalog is not None:
                with contextlib.suppress(Exception):
                    await self.services.catalog.record_pick(trip.store_id, stop.slot_id, sku, 1, trip.trip_id)
            return item
        return None

    async def _verify_basket(self, trip: Trip) -> None:
        perception = self.services.perception
        if perception is None or self.services.robot is None:
            return
        expected: dict[str, int] = {}
        for ln in trip.cart.active_lines():
            if ln.source == "in_store":
                expected[ln.sku] = expected.get(ln.sku, 0) + ln.qty
        try:
            raw = await self._robot_call(trip, "capture", "basket")
            frame = await self._blur(raw)
            if frame is None:
                return
            check = await perception.verify_basket(frame, expected)
            await self._publish("basket.verified", trip.trip_id, check=check)
            if not check.ok:
                await self._error(
                    trip.trip_id, "perception", f"basket mismatch: missing {check.missing} extra {check.unexpected}"
                )
        except Exception as exc:  # noqa: BLE001
            await self._error(trip.trip_id, "perception", f"basket check failed: {exc}")

    async def _out_of_stock(self, trip: Trip, stop: Stop, sku: str) -> None:
        await self._set_stop(trip, stop, "failed", "out of stock on the shelf; ordering online")
        if stop.kind == "optional":
            return
        items = [i for i in (trip.shopping_list.items if trip.shopping_list else []) if i.item_id in stop.item_ids]
        if not items:
            items = [ListItem(item_id=new_id("item"), query=stop.label or sku, sku=sku, qty=max(stop.qty, 1))]
        for item in items:
            await self._reroute_online(trip, item)

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

    async def _go_to_end(self, trip: Trip) -> None:
        if trip.route is None or not trip.route.order:
            return
        end_id = trip.route.order[-1]
        end = trip.route.stop(end_id)
        if end is None or end.kind != "end" or self._at.get(trip.trip_id) == end_id:
            return
        try:
            await self._navigate_to(trip, end)
            self._at[trip.trip_id] = end_id
        except RobotError as exc:
            await self._error(trip.trip_id, "robot", f"could not reach checkout: {exc}", code=exc.code)

    async def _wrap_up_shopping(self, trip: Trip) -> None:
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
            for rec in recs:
                await self._record_rec_outcome(trip, rec, "accepted" if rec.rec_id in chosen else "declined")
                if rec.rec_id not in chosen:
                    continue
                source = "online" if rec.store_id == ONLINE_STORE_ID else "in_store"
                line = CartLine(
                    line_id=new_id("line"),
                    sku=rec.sku,
                    name=rec.product_name,
                    category=await self._category(rec.sku),
                    qty=1,
                    unit_price_cents=rec.price_cents or await self._unit_price(trip, rec.sku, rec, None),
                    source=source,
                    store_id=ONLINE_STORE_ID if source == "online" else trip.store_id,
                    slot_id=rec.slot_id,
                    status="picked" if source == "in_store" else "planned",
                    rec_id=rec.rec_id,
                )
                # the shopper explicitly picked it in this approval, so a soft mandate ask (e.g. over budget) is met
                res = await self.add_line(trip.trip_id, line, approval_id=req.approval_id)
                if not res.added:
                    await self._error(trip.trip_id, "trust", f"{rec.product_name or rec.sku} not added (mandate)")
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
        try:
            receipt = await trust.checkout(
                CheckoutRequest(
                    trip_id=trip_id,
                    user_id=trip.user_id,
                    cart=trip.cart.model_copy(deep=True),
                    cart_hash=cart_hash,
                    budget_cents=trip.budget_cents,
                    approval_request=request,
                    approval=resp,
                    idempotency_key=f"{trip_id}:{cart_hash[:16]}",
                )
            )
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

    async def _record_purchases(self, trip: Trip) -> None:
        rec_svc = self.services.recommender
        if rec_svc is None:
            return
        records = [
            PurchaseRecord(
                purchase_id=new_id("pur"),
                user_id=ln.user_id or trip.user_id,
                sku=ln.sku,
                qty=ln.qty,
                unit_price_cents=ln.unit_price_cents,
                store_id=ln.store_id,
                trip_id=trip.trip_id,
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
        trip = self._trips.get(request.trip_id)
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
    # robot helpers
    # ==================================================================================================================
    async def _robot_call(self, trip: Trip, method: str, *args: Any) -> Any:
        robot = self.services.require("robot")
        try:
            return await getattr(robot, method)(*args)
        except RobotError as exc:
            if exc.code != "disconnected" or self._fallback_robot is None:
                raise
            fallback = self._fallback_robot()
            if fallback is None or fallback is robot:
                raise
            await self._error(trip.trip_id, "robot", "sim disconnected; switching to the 2D robot fake", code=exc.code)
            self.services.robot = fallback
            if hasattr(fallback, "set_trip"):
                fallback.set_trip(trip.trip_id)
            await fallback.reset(trip.store_id)
            if method == "reset":
                return await fallback.state()
            return await getattr(fallback, method)(*args)

    async def _navigate_to(self, trip: Trip, stop: Stop) -> Any:
        """Follow the routed leg; on path_blocked/timeout re-plan from the current pose and retry once."""
        try:
            return await self._robot_call(trip, "navigate", await self._waypoints_to(trip, stop))
        except RobotError as exc:
            if exc.code not in ("path_blocked", "timeout"):
                raise
            log.info("trip %s: %s to %s; re-planning once", trip.trip_id, exc.code, stop.stop_id)
            try:
                await self.replan(trip.trip_id)
            except Exception:  # noqa: BLE001
                raise exc from None
            return await self._robot_call(trip, "navigate", await self._waypoints_to(trip, stop))

    async def _waypoints_to(self, trip: Trip, stop: Stop) -> list[Pose2D]:
        leg = self._leg_to(trip, stop.stop_id)
        if leg is None or (self._at.get(trip.trip_id) not in (None, leg.from_stop_id)):
            with contextlib.suppress(Exception):
                await self.replan(trip.trip_id)
            leg = self._leg_to(trip, stop.stop_id)
        if leg is not None and leg.waypoints:
            return list(leg.waypoints)
        robot = self.services.robot
        here = (await robot.state()).pose if robot is not None else stop.pose
        return [here, stop.pose]

    def _leg_to(self, trip: Trip, stop_id: str) -> RouteLeg | None:
        if trip.route is None:
            return None
        return next((leg for leg in trip.route.legs if leg.to_stop_id == stop_id), None)

    async def _blur(self, frame: Any) -> Any:
        """Face-blur a RAW frame; never return an unblurred frame (None instead)."""
        if frame is None:
            return None
        perception = self.services.perception
        if perception is not None:
            try:
                return await perception.blur_faces(frame)
            except Exception as exc:  # noqa: BLE001
                log.warning("blur_faces failed: %s", exc)
        return frame if frame.blurred else None

    def _start_forwarders(self, trip_id: str, robot: Any) -> list[asyncio.Task[Any]]:
        self._forwarding_robot = robot
        return [
            self._spawn(self._forward_states(trip_id, robot), name=f"states-{trip_id}"),
            self._spawn(self._forward_frames(trip_id, robot), name=f"frames-{trip_id}"),
        ]

    def _restart_forwarders_if_swapped(
        self, trip_id: str, robot: Any, tasks: list[asyncio.Task[Any]]
    ) -> list[asyncio.Task[Any]]:
        current = self.services.robot
        if current is None or current is getattr(self, "_forwarding_robot", robot):
            return tasks
        for t in tasks:
            t.cancel()
        return self._start_forwarders(trip_id, current)

    async def _forward_states(self, trip_id: str, robot: Any) -> None:
        period = 1.0 / self.config.robot_state_hz if self.config.robot_state_hz > 0 else 0.0
        last_t = 0.0
        last_status: RobotStatus | None = None
        try:
            async for state in robot.states():
                event = make_event("robot.state", trip_id=trip_id, source="gateway", state=state)
                hub_pub = getattr(self.services.events, "frames", None)
                if hub_pub is not None:
                    hub_pub.publish(event)
                now = time.monotonic()
                if state.status != last_status or now - last_t >= period:
                    last_t, last_status = now, state.status
                    await self._emit(event)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.warning("robot state stream ended: %s", exc)

    async def _forward_frames(self, trip_id: str, robot: Any) -> None:
        try:
            async for raw in robot.frames():
                frame = await self._blur(raw)
                if frame is None or not frame.blurred:
                    continue
                await self._emit(make_event("frame", trip_id=trip_id, source="gateway", frame=frame))
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.warning("robot frame stream ended: %s", exc)

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
        robot = self.services.robot
        if trip.state in (S.EXECUTING, S.AWAITING_APPROVAL) and robot is not None:
            with contextlib.suppress(Exception):
                return (await robot.state()).pose
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
        try:
            event = make_event(type_, trip_id=trip_id, source="gateway", **payload)
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
