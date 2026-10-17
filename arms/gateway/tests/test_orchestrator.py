"""The P0 scenario through the orchestrator with all fakes (trip 1 -> feedback -> trip 2)."""

import asyncio

import pytest

from arms.gateway.demo import run_demo
from arms.gateway.orchestrator import OrchestratorConfig
from arms.gateway.registry import build_registry
from arms.gateway.tests.helpers import FAST_FAKES, needs_data, until
from contracts.api import CreateTripRequest, RobotLoginRequest
from contracts.shopping import CartLine, TripState
from contracts.trust import ApprovalKind, ApprovalRequest, ApprovalResponse, MandateReason, MandateVerdict
from utils.ids import new_id


@needs_data
async def test_demo_two_trips_done_and_smarter(capsys: pytest.CaptureFixture[str]) -> None:
    trip1, trip2 = await run_demo(quiet=True, timeout_s=30)
    assert trip1.state == trip2.state == TripState.DONE
    assert trip1.receipt is not None and trip1.receipt.status == "succeeded"
    assert trip1.followup is not None and trip1.followup.about_skus
    assert [r.sku for r in trip1.recs] != [r.sku for r in trip2.recs]
    for rec in trip1.recs:
        assert rec.reason and rec.sponsored is False
    assert any(ln.source == "online" for ln in trip1.cart.active_lines()), "split sourcing: one online line"
    for trip in (trip1, trip2):  # "stays within budget": extras are fitted to the headroom, overages are declined
        assert trip.budget_cents is not None and trip.receipt is not None
        assert trip.receipt.amount_cents <= trip.budget_cents
    declined = {r.sku for r in trip1.recs if trip1.stop_status.get(f"rec:{r.rec_id}") == "declined"}
    assert declined, "trip 1 should show one declined detour"
    assert not declined & {r.sku for r in trip2.recs}, "a declined detour must not come back in trip 2"


@needs_data
async def test_events_cover_the_pipeline() -> None:
    reg = build_registry(
        real_arms=set(),
        fake_opts=FAST_FAKES,
        orchestrator_config=OrchestratorConfig(auto_login=True, approval_channels=("web", "photon")),
    )
    orch = reg.orchestrator
    await orch.start()
    try:
        trip = await orch.create_trip(CreateTripRequest(user_id="maya", text="tacos for 6, under $40, one vegetarian"))
        await until(lambda: orch.peek(trip.trip_id).state in (TripState.DONE, TripState.FAILED))
        final = await orch.get_trip(trip.trip_id)
        assert final.state == TripState.DONE, final.error
        types = {e.type for e in reg.bus.recent(trip_id=trip.trip_id)}
        for t in (
            "trip.created",
            "intent.received",
            "list.ready",
            "sourcing.split",
            "recs.ready",
            "route.planned",
            "stop.updated",
            "robot.arrived",
            "scan.result",
            "item.picked",
            "cart.updated",
            "approval.requested",
            "approval.resolved",
            "mandate.decision",
            "checkout.completed",
            "followup.sent",
            "feedback.received",
            "profile.updated",
            "trip.state_changed",
        ):
            assert t in types, t
        seqs = [e.seq for e in reg.bus.recent()]
        assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)
        path = [h.to_state for h in final.history]
        for s in (
            TripState.LIST_READY,
            TripState.RECS_READY,
            TripState.ROUTE_PLANNED,
            TripState.EXECUTING,
            TripState.CHECKOUT,
            TripState.PAID,
            TripState.FOLLOWUP,
            TripState.DONE,
        ):
            assert s in path, s
        assert final.mandate_decision is not None
        assert all(ln.unit_price_cents >= 0 for ln in final.cart.lines)
    finally:
        await orch.stop()


@needs_data
async def test_web_checkout_requires_matching_cart_hash() -> None:
    reg = build_registry(
        real_arms=set(),
        fake_opts=FAST_FAKES,
        orchestrator_config=OrchestratorConfig(approval_channels=("web",), checkout_batch=False, auto_checkout=False),
    )
    orch = reg.orchestrator
    await orch.start()
    try:
        trip = await orch.create_trip(CreateTripRequest(user_id="maya", text="tacos for 6, under $40, one vegetarian"))
        await until(lambda: orch.peek(trip.trip_id).state == TripState.AWAITING_LOGIN)
        await orch.login(trip.trip_id, RobotLoginRequest(user_id="maya"))

        async def web_shopper() -> None:  # approve everything except checkout
            while True:
                for p in reg.approvals.pending(trip.trip_id):
                    if p.kind == ApprovalKind.CHECKOUT:
                        continue
                    if p.options:
                        resp = ApprovalResponse(
                            approval_id=p.approval_id, decision="choice", choice=p.options[0].option_id
                        )
                    else:
                        resp = ApprovalResponse(approval_id=p.approval_id, decision="approve")
                    await reg.approvals.resolve(resp)
                await asyncio.sleep(0.01)

        shopper = asyncio.create_task(web_shopper())
        await until(lambda: not orch.is_executing(trip.trip_id) and orch.peek(trip.trip_id).stop_status)
        shopper.cancel()
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
        await orch.stop()


@needs_data
async def test_line_approvals_are_bound_to_the_line_and_single_use() -> None:
    """§8 'ask me above $X always asks': an approval id only unlocks the exact line it was asked for, once."""
    reg = build_registry(
        real_arms=set(),
        fake_opts=FAST_FAKES,
        orchestrator_config=OrchestratorConfig(approval_channels=("web",), checkout_batch=False, auto_checkout=False),
    )
    orch = reg.orchestrator
    await orch.start()
    try:
        trip = await orch.create_trip(CreateTripRequest(user_id="maya", text="tacos for 6, under $40, one vegetarian"))
        await until(lambda: orch.peek(trip.trip_id).state == TripState.AWAITING_LOGIN)
        await orch.login(trip.trip_id, RobotLoginRequest(user_id="maya"))

        async def shopper() -> None:  # pick #1 on choices, say no to yes/no asks (small cart), leave checkout alone
            while True:
                for p in reg.approvals.pending(trip.trip_id):
                    if p.kind == ApprovalKind.CHECKOUT:
                        continue
                    if p.options:
                        resp = ApprovalResponse(
                            approval_id=p.approval_id, decision="choice", choice=p.options[0].option_id
                        )
                    else:
                        resp = ApprovalResponse(approval_id=p.approval_id, decision="reject")
                    await reg.approvals.resolve(resp)
                await asyncio.sleep(0.01)

        task = asyncio.create_task(shopper())
        await until(lambda: not orch.is_executing(trip.trip_id) and orch.peek(trip.trip_id).stop_status)
        task.cancel()
        live = await orch.get_trip(trip.trip_id)
        pick = next(a for a in live.approvals if a.kind == ApprovalKind.PICK_CHOICE)
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
