"""Trip flow around the fleet: planning -> EXECUTING -> fleet.execute_trip -> checkout -> follow-up -> done.

Uses the stub fleet (tests/stubs.py) that drives the trip through the same TripController calls as the robot brain,
so these tests only depend on the gateway + the planning arms' fakes (agent, catalog, recommender, routing, trust,
messaging)."""

import asyncio
from types import SimpleNamespace

import pytest

from arms.gateway.orchestrator import OrchestratorConfig
from arms.gateway.registry import build_registry
from arms.gateway.tests.helpers import FAST_FAKES, needs_data, until
from arms.gateway.tests.stubs import StubBrain, StubCameras, StubFleet
from contracts.api import CreateTripRequest, RobotLoginRequest
from contracts.fleet import GoalItem, GoalStatus, goal_items_from_trip
from contracts.messaging import InboundMessage
from contracts.shopping import CartLine, ListItem, TripState
from contracts.trust import (
    ApprovalKind,
    ApprovalOption,
    ApprovalRequest,
    ApprovalResponse,
    MandateReason,
    MandateVerdict,
)
from utils.ids import new_id

TACOS = "tacos for 6, under $40, one vegetarian"
STUBS = {"fleet": StubFleet, "brain": StubBrain, "cameras": StubCameras}


def registry(**cfg):  # noqa: ANN003, ANN201
    cfg.setdefault("approval_channels", ("web", "photon"))
    return build_registry(
        real_arms=set(), fake_opts=FAST_FAKES, overrides=STUBS, orchestrator_config=OrchestratorConfig(**cfg)
    )


async def web_shopper(reg, trip_id: str | None = None, reject: bool = False, skip_checkout: bool = False) -> None:  # noqa: ANN001
    while True:
        for p in reg.approvals.pending(trip_id):
            if p.kind == ApprovalKind.CHECKOUT and skip_checkout:
                continue
            if p.options:
                resp = ApprovalResponse(approval_id=p.approval_id, decision="choice", choice=p.options[0].option_id)
            elif p.kind == ApprovalKind.CHECKOUT:
                resp = ApprovalResponse(approval_id=p.approval_id, decision="approve", cart_hash=p.cart_hash)
            else:
                resp = ApprovalResponse(approval_id=p.approval_id, decision="reject" if reject else "approve")
            await reg.approvals.resolve(resp)
        await asyncio.sleep(0.01)


@needs_data
async def test_auto_execute_trip_runs_through_the_fleet_to_done() -> None:
    reg = registry()
    orch = reg.orchestrator
    await orch.start()
    try:
        assert reg.services.fleet.started, "the gateway starts the fleet at boot"
        trip = await orch.create_trip(
            CreateTripRequest(user_id="maya", text=TACOS, auto_execute=True, robot_id="rover-ave-1", request_id="req_1")
        )
        assert trip.auto_execute and trip.robot_logged_in
        await until(lambda: orch.peek(trip.trip_id).state in (TripState.DONE, TripState.FAILED), 20)
        final = await orch.get_trip(trip.trip_id)
        assert final.state == TripState.DONE, final.error
        path = [h.to_state for h in final.history]
        assert TripState.AWAITING_LOGIN not in path, "auto_execute trips skip the login step"
        for s in (TripState.ROUTE_PLANNED, TripState.EXECUTING, TripState.CHECKOUT, TripState.PAID, TripState.DONE):
            assert s in path, s
        assert final.robot_id == "rover-ave-1" and final.goal_id
        assert reg.services.fleet.executed == [trip.trip_id]
        assert final.receipt is not None and final.receipt.status == "succeeded"
        assert any(v == "done" for v in final.stop_status.values())
        events = reg.bus.recent(trip_id=trip.trip_id)
        types = {e.type for e in events}
        for t in (
            "trip.created",
            "list.ready",
            "sourcing.split",
            "route.planned",
            "stop.updated",
            "cart.updated",
            "approval.requested",
            "mandate.decision",
            "checkout.completed",
            "followup.sent",
            "profile.updated",
        ):
            assert t in types, t
        for e in events:  # trip envelope on every trip-flow event (request filter + store filter work)
            if e.type in ("trip.state_changed", "cart.updated", "stop.updated", "route.planned"):
                assert e.request_id == "req_1" and e.user_id == "maya" and e.store_id == final.store_id, e.type
        appr = next(e for e in events if e.type == "approval.requested")
        assert appr.request_id == "req_1" and appr.request.store_id == final.store_id
        seqs = [e.seq for e in reg.bus.recent()]
        assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)
    finally:
        await orch.stop()
    assert not reg.services.fleet.started, "the gateway shuts the fleet down"


@needs_data
async def test_manual_trip_waits_for_login_then_executes_with_that_robot() -> None:
    reg = registry(approval_channels=("web",))
    orch = reg.orchestrator
    await orch.start()
    shopper = asyncio.create_task(web_shopper(reg))
    try:
        trip = await orch.create_trip(CreateTripRequest(user_id="maya", text=TACOS))
        await until(lambda: orch.peek(trip.trip_id).state == TripState.AWAITING_LOGIN)
        assert reg.services.fleet.executed == []
        with pytest.raises(PermissionError):
            await orch.login(trip.trip_id, RobotLoginRequest(user_id="mallory"))
        await orch.login(trip.trip_id, RobotLoginRequest(user_id="maya", robot_id="rover-ave-2"))
        await until(lambda: orch.peek(trip.trip_id).state in (TripState.FOLLOWUP, TripState.DONE), 20)
        assert orch.peek(trip.trip_id).robot_id == "rover-ave-2"
    finally:
        shopper.cancel()
        await orch.stop()


@needs_data
@pytest.mark.parametrize("outcome", [GoalStatus.CANCELLED, GoalStatus.FAILED])
async def test_goal_outcomes(outcome: GoalStatus) -> None:
    reg = registry(approval_channels=("web",), checkout_batch=False)
    reg.services.fleet.outcome = outcome
    orch = reg.orchestrator
    await orch.start()
    shopper = asyncio.create_task(web_shopper(reg))
    try:
        trip = await orch.create_trip(CreateTripRequest(user_id="maya", text=TACOS, auto_execute=True))
        await until(lambda: orch.peek(trip.trip_id).state in (TripState.FAILED, TripState.FOLLOWUP, TripState.DONE), 20)
        final = await orch.get_trip(trip.trip_id)
        if outcome == GoalStatus.CANCELLED:
            assert final.state == TripState.FAILED and "cancelled" in (final.error or "")
        else:  # failed with lines in the cart -> still checks out, with an error event
            assert final.state in (TripState.FOLLOWUP, TripState.DONE)
            assert any(e.type == "error" and "ended early" in e.message for e in reg.bus.recent(trip_id=trip.trip_id))
    finally:
        shopper.cancel()
        await orch.stop()


class _BlockingFleet(StubFleet):
    """Holds the trip in EXECUTING until released (to test mid-trip TripController calls)."""

    def __init__(self, services) -> None:  # noqa: ANN001
        super().__init__(services)
        self.release = asyncio.Event()

    async def execute_trip(self, trip_id: str, robot_id: str | None = None):  # noqa: ANN201
        await self.services.trips.assign_robot(trip_id, robot_id or "rover-ave-1", "goal_x")
        await self.release.wait()
        return await super().execute_trip(trip_id, robot_id)


@needs_data
async def test_mid_trip_ask_update_list_and_replan() -> None:
    reg = build_registry(
        real_arms=set(),
        fake_opts=FAST_FAKES,
        overrides={**STUBS, "fleet": _BlockingFleet},
        orchestrator_config=OrchestratorConfig(approval_channels=("web",)),
    )
    orch = reg.orchestrator
    await orch.start()
    try:
        trip = await orch.create_trip(CreateTripRequest(user_id="maya", text=TACOS, auto_execute=True))
        await until(lambda: orch.peek(trip.trip_id).state == TripState.EXECUTING and orch.peek(trip.trip_id).robot_id)
        live = await orch.get_trip(trip.trip_id)
        assert live.robot_id == "rover-ave-1" and live.goal_id == "goal_x"

        # set_stop_status -> stop.updated with the trip envelope
        stop = next(s for s in live.route.stops if s.kind == "mandatory")
        await orch.set_stop_status(trip.trip_id, stop.stop_id, "navigating")
        assert orch.peek(trip.trip_id).current_stop_id == stop.stop_id
        ev = [e for e in reg.bus.recent(trip_id=trip.trip_id) if e.type == "stop.updated"][-1]
        assert ev.stop_id == stop.stop_id and ev.robot_id == "rover-ave-1" and ev.slot_id == stop.slot_id

        # ask: gateway fills ids / channels / expiry, drives AWAITING_APPROVAL, flags over-budget detours
        rec = live.recs[0] if live.recs else None
        req = ApprovalRequest(
            approval_id=new_id("appr"),
            user_id="whoever",
            kind=ApprovalKind.RECOMMENDATION,
            title="Grab it?",
            rec_id=rec.rec_id if rec else None,
            sku=rec.sku if rec else None,
            amount_cents=99_999,
        )
        task = asyncio.create_task(orch.ask(trip.trip_id, req))
        await until(lambda: reg.approvals.pending(trip.trip_id))
        pending = reg.approvals.pending(trip.trip_id)[0]
        assert pending.user_id == "maya" and pending.store_id == live.store_id and pending.robot_id == "rover-ave-1"
        assert pending.goal_id == "goal_x" and pending.channels == ["web"]
        assert pending.payload["over_budget"] is True and "budget" in pending.title
        assert orch.peek(trip.trip_id).state == TripState.AWAITING_APPROVAL
        assert reg.approvals.pending(goal_id="goal_x") and not reg.approvals.pending(goal_id="other")
        await reg.approvals.resolve(ApprovalResponse(approval_id=pending.approval_id, decision="reject"))
        assert not (await task).approved
        assert orch.peek(trip.trip_id).state == TripState.EXECUTING
        if rec is not None:
            assert rec.sku in orch._declined_skus["maya"]

        # update_list during EXECUTING appends a pending stop; replan orders it
        catalog = reg.services.catalog
        plan = await catalog.get_planogram(live.store_id)
        on_route = {s for st in live.route.stops for s in st.skus}
        extra = next(sl for sl in plan.slots if sl.sku not in on_route and sl.qty > 0)
        new_item = ListItem(item_id="item_extra", query="extra thing", sku=extra.sku, qty=1, sourcing="in_store")
        lst = live.shopping_list.model_copy(update={"items": [*live.shopping_list.items, new_item]})
        after = await orch.update_list(trip.trip_id, lst)
        added = [s for s in after.route.stops if "item_extra" in s.item_ids]
        assert added and after.stop_status[added[0].stop_id] == "pending"
        route = await orch.replan(trip.trip_id)
        assert added[0].stop_id in route.order
        assert orch.peek(trip.trip_id).state == TripState.EXECUTING
    finally:
        reg.services.fleet.release.set()
        await orch.stop()


@needs_data
async def test_text_replies_find_their_approval_across_live_trips() -> None:
    """One user, two live trips (a split request): a texted YES/NO must resolve the approval it answers, not just
    whatever the newest trip has pending."""
    reg = build_registry(
        real_arms=set(),
        fake_opts={**FAST_FAKES, "messaging": {"reply_mode": "off", "reply_delay_s": 0.01}},
        overrides={**STUBS, "fleet": _BlockingFleet},
        orchestrator_config=OrchestratorConfig(approval_channels=("web", "photon")),
    )
    orch = reg.orchestrator
    await orch.start()
    try:
        t1 = await orch.create_trip(CreateTripRequest(user_id="maya", text=TACOS, auto_execute=True))
        t2 = await orch.create_trip(CreateTripRequest(user_id="maya", text="pretzels and dish soap", auto_execute=True))
        await until(lambda: all(orch.peek(t.trip_id).state == TripState.EXECUTING for t in (t1, t2)))
        assert orch.active_trip_for("maya").trip_id == t2.trip_id  # the newest live trip

        def ask(trip_id: str, title: str) -> ApprovalRequest:
            return ApprovalRequest(
                approval_id=new_id("appr"),
                trip_id=trip_id,
                user_id="maya",
                kind=ApprovalKind.SUBSTITUTION,
                title=title,
                channels=["web", "photon"],
            )

        a1, a2, a3 = ask(t1.trip_id, "Swap salsa?"), ask(t2.trip_id, "Swap pretzels?"), ask(t1.trip_id, "Swap lime?")
        futs = {}
        for a in (a1, a2, a3):
            futs[a.approval_id] = await reg.approvals.open(a)
            await asyncio.sleep(0.005)  # distinct created_at order
        messaging = reg.services.messaging
        await until(lambda: len([e for e in messaging.transcript if e.approval_id]) == 3)
        texted = {e.approval_id: e.message_id for e in messaging.transcript if e.approval_id}
        assert reg.approvals.approval_for_message(texted[a2.approval_id]).approval_id == a2.approval_id

        # 1) a threaded reply (in_reply_to = the approval text) resolves exactly that approval, in the OLDER trip too
        reply = await messaging.inject("YES", user_id="maya", in_reply_to=texted[a3.approval_id])
        assert reply.in_reply_to == texted[a3.approval_id]
        resp = await asyncio.wait_for(futs[a3.approval_id], 2)
        assert resp.approved and resp.channel == "photon"
        assert {p.approval_id for p in reg.approvals.pending(user_id="maya")} == {a1.approval_id, a2.approval_id}

        # 2) an unthreaded NO answers the user's OLDEST pending texted approval (trip 1), not the newest trip's
        await messaging.inject("no", user_id="maya")
        resp = await asyncio.wait_for(futs[a1.approval_id], 2)
        assert resp.decision == "reject"
        assert [p.approval_id for p in reg.approvals.pending(user_id="maya")] == [a2.approval_id]

        # 3) a late reply to an already-answered text resolves nothing else
        await messaging.inject("YES", user_id="maya", in_reply_to=texted[a1.approval_id])
        assert [p.approval_id for p in reg.approvals.pending(user_id="maya")] == [a2.approval_id]

        # 4) an explicit trip_id wins
        interp = await orch.handle_inbound(
            InboundMessage(message_id="in_x", channel="photon", user_id="maya", text="yes", trip_id=t2.trip_id)
        )
        assert interp.kind == "approval" and (await asyncio.wait_for(futs[a2.approval_id], 2)).approved
    finally:
        reg.services.fleet.release.set()
        await orch.stop()


@needs_data
async def test_web_checkout_requires_matching_cart_hash() -> None:
    reg = registry(approval_channels=("web",), checkout_batch=False, auto_checkout=False)
    orch = reg.orchestrator
    await orch.start()
    shopper = asyncio.create_task(web_shopper(reg, skip_checkout=True))
    try:
        trip = await orch.create_trip(CreateTripRequest(user_id="maya", text=TACOS, auto_execute=True))
        await until(lambda: reg.services.fleet.executed and not orch.is_executing(trip.trip_id))
        status = await orch.start_checkout(trip.trip_id)
        assert status.approval is not None and status.approval.cart_hash == status.cart_hash
        bad = ApprovalResponse(
            approval_id=status.approval.approval_id, decision="approve", channel="web", cart_hash="0" * 64
        )
        assert not await reg.approvals.resolve(bad)
        good = bad.model_copy(update={"cart_hash": status.cart_hash})
        assert await reg.approvals.resolve(good)
        await until(lambda: orch.peek(trip.trip_id).state in (TripState.FOLLOWUP, TripState.DONE), 10)
        assert (await orch.get_trip(trip.trip_id)).receipt.status == "succeeded"
    finally:
        shopper.cancel()
        await orch.stop()


class _ProvisionalRunner:
    """Loop runner stub: the goal's plan still has a provisional line the basket audit has not confirmed."""

    def __init__(self, services) -> None:  # noqa: ANN001
        self.provisional_open = True
        self.goals: list[str] = []

    async def current_plan(self, goal_id: str) -> SimpleNamespace:
        self.goals.append(goal_id)
        return SimpleNamespace(plan_id=f"plan-{goal_id}")

    async def run_state(self, plan_id: str) -> SimpleNamespace:
        return SimpleNamespace(plan_id=plan_id, provisional_open=self.provisional_open)


@needs_data
async def test_checkout_waits_for_the_basket_audit() -> None:
    """api.md 'Who writes what': checkout starts only when PlanRunState.provisional_open is false."""
    reg = build_registry(
        real_arms=set(),
        fake_opts=FAST_FAKES,
        overrides={**STUBS, "runner": _ProvisionalRunner},
        orchestrator_config=OrchestratorConfig(approval_channels=("web",), checkout_batch=False, auto_checkout=False),
    )
    orch = reg.orchestrator
    runner = reg.services.runner
    await orch.start()
    shopper = asyncio.create_task(web_shopper(reg, skip_checkout=True))
    try:
        trip = await orch.create_trip(CreateTripRequest(user_id="maya", text=TACOS, auto_execute=True))
        await until(lambda: reg.services.fleet.executed and not orch.is_executing(trip.trip_id))
        blocked = await orch.start_checkout(trip.trip_id)
        assert blocked.approval is None and orch.peek(trip.trip_id).state == TripState.EXECUTING
        assert runner.goals == [orch.peek(trip.trip_id).goal_id]
        errors = [e for e in reg.bus.recent(trip_id=trip.trip_id) if e.type == "error"]
        assert errors and errors[-1].code == "basket_audit_pending"
        runner.provisional_open = False  # the audit passed
        status = await orch.start_checkout(trip.trip_id)
        assert status.approval is not None and status.approval.kind == ApprovalKind.CHECKOUT
    finally:
        shopper.cancel()
        await orch.stop()


@needs_data
async def test_line_approvals_are_bound_to_the_line_and_single_use() -> None:
    """§8 'ask me above $X always asks': an approval id only unlocks the exact line it was asked for, once."""
    reg = registry(approval_channels=("web",), checkout_batch=False, auto_checkout=False)
    orch = reg.orchestrator
    await orch.start()
    try:
        trip = await orch.create_trip(CreateTripRequest(user_id="maya", text=TACOS))
        await until(lambda: orch.peek(trip.trip_id).state == TripState.AWAITING_LOGIN)
        live = await orch.get_trip(trip.trip_id)
        mandate = await reg.services.trust.get_mandate("maya")
        assert mandate is not None and mandate.ask_above_cents is not None

        def coffee() -> CartLine:
            return CartLine(
                line_id=new_id("line"),
                sku="COFFEE",
                name="Coffee",
                category="beverages",
                qty=1,
                unit_price_cents=mandate.ask_above_cents + 100,
                source="in_store",
                store_id=live.store_id,
                status="planned",
            )

        bare = await orch.add_line(trip.trip_id, coffee())
        assert not bare.added and bare.decision.verdict == MandateVerdict.NEEDS_APPROVAL, bare.decision.reasons
        assert MandateReason.ABOVE_ASK_THRESHOLD in bare.decision.reasons
        pick = ApprovalRequest(
            approval_id=new_id("appr"),
            trip_id=trip.trip_id,
            user_id="maya",
            kind=ApprovalKind.PICK_CHOICE,
            title="Which one?",
            sku="COFFEE",
        )
        fut = await reg.approvals.open(pick)
        await reg.approvals.resolve(ApprovalResponse(approval_id=pick.approval_id, decision="approve"))
        await fut
        reused = await orch.add_line(trip.trip_id, coffee(), approval_id=pick.approval_id)
        assert not reused.added, "a pick_choice approval must not unlock an above-threshold line"

        line = coffee()
        ask = ApprovalRequest(
            approval_id=new_id("appr"),
            trip_id=trip.trip_id,
            user_id="maya",
            kind=ApprovalKind.OVER_THRESHOLD,
            title="Coffee is over your ask-me limit. Add it?",
            amount_cents=line.total_cents,
            sku="COFFEE",
        )
        fut = await reg.approvals.open(ask)
        assert await reg.approvals.resolve(ApprovalResponse(approval_id=ask.approval_id, decision="approve"))
        await fut
        other_sku = line.model_copy(update={"line_id": new_id("line"), "sku": "COFFEE-2"})
        assert not (await orch.add_line(trip.trip_id, other_sku, approval_id=ask.approval_id)).added
        pricier = line.model_copy(update={"line_id": new_id("line"), "qty": 2})
        assert not (await orch.add_line(trip.trip_id, pricier, approval_id=ask.approval_id)).added
        assert (await orch.add_line(trip.trip_id, line, approval_id=ask.approval_id)).added
        again = await orch.add_line(trip.trip_id, coffee(), approval_id=ask.approval_id)
        assert not again.added, "one approval covers one add"
    finally:
        await orch.stop()


@needs_data
async def test_no_fleet_means_error_not_crash() -> None:
    reg = build_registry(
        real_arms=set(),
        fake_opts=FAST_FAKES,
        overrides={"fleet": lambda s: None, "brain": StubBrain, "cameras": StubCameras},
        orchestrator_config=OrchestratorConfig(approval_channels=("web",)),
    )
    orch = reg.orchestrator
    await orch.start()
    shopper = asyncio.create_task(web_shopper(reg))
    try:
        trip = await orch.create_trip(CreateTripRequest(user_id="maya", text=TACOS, auto_execute=True))
        await until(lambda: orch.peek(trip.trip_id).state in (TripState.FAILED, TripState.FOLLOWUP, TripState.DONE), 20)
        assert any(e.type == "error" and e.arm == "fleet" for e in reg.bus.recent(trip_id=trip.trip_id))
    finally:
        shopper.cancel()
        await orch.stop()


def test_fit_detours_keeps_a_near_expiry_markdown_on_a_tight_budget() -> None:
    from arms.gateway.orchestrator import TripOrchestrator
    from contracts.shopping import Recommendation

    def rec(sku: str, price: int, kind: str = "detour") -> Recommendation:
        return Recommendation(
            rec_id=f"rec_{sku}", user_id="maya", sku=sku, store_id="store_ave", interest=0.9, reason="why",
            kind=kind, price_cents=price,
        )  # fmt: skip

    recs = [rec("HOT", 299), rec("GUAC", 399, "near_expiry_offer"), rec("JAL", 129), rec("CHIPS", 349)]
    kept, left = TripOrchestrator._fit_detours(recs, 3, 244)
    assert [r.sku for r in kept] == ["HOT", "GUAC", "JAL"] and left == 115
    kept, _ = TripOrchestrator._fit_detours([rec("HOT", 299), rec("CHIPS", 349), rec("JAL", 129)], 3, 244)
    assert [r.sku for r in kept] == ["HOT", "JAL"], "still at most one plain over-budget detour"
    assert [r.sku for r in TripOrchestrator._fit_detours(recs, 3, None)[0]] == ["HOT", "GUAC", "JAL"]


# ======================================================================================================================
# checkout-phase suggestions: billed in store only once a robot has picked them (CLAUDE.md §8)
# ======================================================================================================================
CHIPS = "tortilla chips and salsa, under $40"
"""Leaves budget headroom, so the checkout batch has in-store suggestions."""


async def checkout_shopper(reg, *, take_batch: bool, take_detours: bool = False) -> None:  # noqa: ANN001
    """Web shopper: says no to detours (unless ``take_detours``), takes every checkout-batch card (or none), answers
    the rest with yes / the first option, approves the checkout with its cart hash."""
    while True:
        for p in reg.approvals.pending():
            if p.kind == ApprovalKind.CHECKOUT:
                resp = ApprovalResponse(approval_id=p.approval_id, decision="approve", cart_hash=p.cart_hash)
            elif p.payload.get("kind") == "checkout_batch":
                resp = (
                    ApprovalResponse(
                        approval_id=p.approval_id, decision="choice", choices=[o.option_id for o in p.options]
                    )
                    if take_batch
                    else ApprovalResponse(approval_id=p.approval_id, decision="reject")
                )
            elif p.options:
                resp = ApprovalResponse(approval_id=p.approval_id, decision="choice", choice=p.options[0].option_id)
            elif p.kind == ApprovalKind.RECOMMENDATION:
                resp = ApprovalResponse(approval_id=p.approval_id, decision="approve" if take_detours else "reject")
            else:
                resp = ApprovalResponse(approval_id=p.approval_id, decision="approve")
            await reg.approvals.resolve(resp)
        await asyncio.sleep(0.01)


class _RecordingFleet(StubFleet):
    """Remembers, per in-store run, the cart before it and the goal items it was handed."""

    def __init__(self, services) -> None:  # noqa: ANN001
        super().__init__(services)
        self.runs: list[tuple[list[CartLine], list[GoalItem]]] = []

    async def execute_trip(self, trip_id: str, robot_id: str | None = None):  # noqa: ANN201
        trip = await self.services.trips.get_trip(trip_id)
        self.runs.append((list(trip.cart.lines), goal_items_from_trip(trip)))
        return await super().execute_trip(trip_id, robot_id)


async def _paid_trip(reg, text: str, **shopper):  # noqa: ANN001, ANN003, ANN202
    orch = reg.orchestrator
    await orch.start()
    task = asyncio.create_task(checkout_shopper(reg, **shopper))
    try:
        trip = await orch.create_trip(CreateTripRequest(user_id="maya", text=text, auto_execute=True))
        await until(lambda: orch.peek(trip.trip_id).state in (TripState.FOLLOWUP, TripState.DONE, TripState.FAILED))
        final = await orch.get_trip(trip.trip_id)
        assert final.receipt is not None and final.receipt.status == "succeeded", final.error
        bought = await reg.services.recommender.purchase_history("maya")
        return final, [p for p in bought if p.trip_id == trip.trip_id]
    finally:
        task.cancel()
        await orch.stop()


def _recording_registry(**cfg):  # noqa: ANN003, ANN202
    return build_registry(
        real_arms=set(),
        fake_opts=FAST_FAKES,
        overrides={**STUBS, "fleet": _RecordingFleet},
        orchestrator_config=OrchestratorConfig(approval_channels=("web",), payment_step_pause_s=0.0, **cfg),
    )


@needs_data
async def test_checkout_suggestion_accepted_after_picking_is_billed_only_once_the_robot_picks_it() -> None:
    """The trip_j2x2ci4p bug: a suggestion accepted at checkout, after the robot had finished, was billed as an
    in-store 'picked' line nobody picked. Now the robot goes back for it first."""
    reg = _recording_registry()
    final, bought = await _paid_trip(reg, CHIPS, take_batch=True)
    accepted = {r.rec_id: r for r in final.checkout_recs if r.store_id == final.store_id}
    assert accepted, "precondition: the checkout batch has in-store suggestions"
    runs = reg.services.fleet.runs
    assert len(runs) == 2, "the shop run, then one run back to the shelves for the accepted suggestions"
    cart_before, items = runs[1]
    assert not any(ln.rec_id in accepted for ln in cart_before), "nothing is billed before the robot goes back"
    todo = [i for i in items if i.status == "pending"]
    assert {i.rec_id for i in todo} == set(accepted)
    assert not any(i.optional for i in todo), "the shopper already said yes: no second ask"
    billed = [ln for ln in final.receipt.lines if ln.rec_id in accepted]
    assert {ln.rec_id for ln in billed} == set(accepted)
    for ln in billed:  # the stub robot's pick (status 'picked' after arriving at the stop), at the card's price
        assert ln.source == "in_store" and ln.status == "picked"
        assert final.stop_status[f"rec:{ln.rec_id}"] == "done"
        assert ln.unit_price_cents == accepted[ln.rec_id].price_cents
    assert final.receipt.amount_cents == final.cart.total_cents
    assert {r.sku for r in accepted.values()} <= {p.sku for p in bought}
    assert not [e for e in reg.bus.recent(trip_id=final.trip_id) if e.type == "error"]


@needs_data
@pytest.mark.parametrize("why", ["fetch_off", "goal_failed"])
async def test_checkout_suggestion_is_not_billed_when_no_robot_can_pick_it(why: str) -> None:
    reg = _recording_registry(fetch_checkout_suggestions=why != "fetch_off")
    if why == "goal_failed":
        reg.services.fleet.outcome = GoalStatus.FAILED  # the in-store run ended early: nobody goes back
    final, bought = await _paid_trip(reg, CHIPS, take_batch=True)
    accepted = {r.rec_id: r for r in final.checkout_recs if r.store_id == final.store_id}
    assert accepted, "precondition: the checkout batch has in-store suggestions"
    assert len(reg.services.fleet.runs) == 1
    assert not any(ln.rec_id in accepted for ln in final.cart.lines)
    assert not any(ln.rec_id in accepted for ln in final.receipt.lines)
    assert not {r.sku for r in accepted.values()} & {p.sku for p in bought}, "never in the purchase history"
    assert all(ln.status in ("picked", "verified") for ln in final.receipt.lines if ln.source == "in_store")
    errors = [
        e for e in reg.bus.recent(trip_id=final.trip_id) if e.type == "error" and e.code == "suggestion_not_picked"
    ]
    assert len(errors) == len(accepted) and all("not in your cart or on your bill" in e.message for e in errors)


@needs_data
async def test_mid_trip_detour_accept_is_still_picked_and_billed() -> None:
    reg = _recording_registry()
    final, bought = await _paid_trip(reg, CHIPS, take_batch=False, take_detours=True)
    picked = [r for r in final.recs if final.stop_status.get(f"rec:{r.rec_id}") == "done"]
    assert picked, "precondition: the shopper said yes to a detour"
    assert len(reg.services.fleet.runs) == 1, "a declined checkout batch sends nobody back"
    lines = {ln.rec_id: ln for ln in final.receipt.lines if ln.rec_id}
    for rec in picked:
        ln = lines[rec.rec_id]
        assert ln.source == "in_store" and ln.status == "picked" and ln.sku == rec.sku
    assert final.receipt.amount_cents == final.cart.total_cents
    assert {r.sku for r in picked} <= {p.sku for p in bought}


@needs_data
async def test_pick_of_a_checkout_choice_uses_the_card_price_and_that_choice_once() -> None:
    """The robot's line for a suggestion chosen at checkout: billed at the price on the card, and the batch choice is
    its line approval (one use per chosen rec), so an above-threshold pick is not dropped after the robot picked it."""
    reg = build_registry(
        real_arms=set(),
        fake_opts=FAST_FAKES,
        overrides={**STUBS, "fleet": _BlockingFleet},
        orchestrator_config=OrchestratorConfig(approval_channels=("web",)),
    )
    orch = reg.orchestrator
    await orch.start()
    try:
        trip = await orch.create_trip(CreateTripRequest(user_id="maya", text=TACOS, auto_execute=True))
        await until(lambda: orch.peek(trip.trip_id).state == TripState.EXECUTING and orch.peek(trip.trip_id).robot_id)
        live = await orch.get_trip(trip.trip_id)
        mandate = await reg.services.trust.get_mandate("maya")
        card = mandate.ask_above_cents + 200
        batch = ApprovalRequest(
            approval_id=new_id("appr"),
            trip_id=trip.trip_id,
            user_id="maya",
            kind=ApprovalKind.RECOMMENDATION,
            title="Want any of these before checkout?",
            options=[ApprovalOption(option_id="rec_x", label="Coffee"), ApprovalOption(option_id="rec_y", label="Tea")],
            multi=True,
            payload={"kind": "checkout_batch"},
        )
        fut = await reg.approvals.open(batch)
        await reg.approvals.resolve(ApprovalResponse(approval_id=batch.approval_id, decision="choice", choice="rec_x"))
        await fut
        orch._checkout_picks[trip.trip_id] = {"rec_x": (batch.approval_id, card), "rec_y": (batch.approval_id, card)}

        def picked(rec_id: str) -> CartLine:  # the robot's own price lookup disagrees with the card
            return CartLine(
                line_id=new_id("line"),
                sku="COFFEE",
                name="Coffee",
                category="beverages",
                unit_price_cents=card - 100,
                store_id=live.store_id,
                status="picked",
                rec_id=rec_id,
                instance_ids=["X#00"],
            )

        res = await orch.add_line(trip.trip_id, picked("rec_x"))
        assert res.added and res.decision.verdict == MandateVerdict.NEEDS_APPROVAL
        line = next(ln for ln in res.cart.lines if ln.line_id == res.line_id)
        assert line.unit_price_cents == card and line.status == "picked"
        assert not (await orch.add_line(trip.trip_id, picked("rec_x"))).added, "one use per chosen rec"
        assert not (await orch.add_line(trip.trip_id, picked("rec_y"))).added, "rec_y was not chosen"
    finally:
        reg.services.fleet.release.set()
        await orch.stop()
