"""The P0 scenario through the whole pipeline with every arm's fake (fleet + robot brains included): request 1 ->
feedback -> request 2, plus the concurrent staff shelf scan. Skips until the fleet / brain fakes exist."""

import importlib.util

import pytest

from arms.gateway.demo import run_demo
from arms.gateway.tests.helpers import needs_data
from contracts.fleet import GoalStatus
from contracts.shopping import TripState


def _fakes_ready() -> bool:
    if importlib.util.find_spec("arms.fleet") is None:
        return False
    import arms.agent
    import arms.fleet
    import arms.perception

    return all(
        hasattr(mod, name)
        for mod, name in (
            (arms.fleet, "create_fake"),
            (arms.agent, "create_brain_fake"),
            (arms.perception, "create_cameras_fake"),
        )
    )


@needs_data
@pytest.mark.skipif(not _fakes_ready(), reason="fleet / brain / cameras fakes not built yet")
async def test_demo_requests_staff_scan_and_smarter_second_trip() -> None:
    res = await run_demo(quiet=True, timeout_s=60)
    for trip in (*res.trips1, *res.trips2):
        assert trip.state == TripState.DONE and trip.auto_execute
        assert trip.receipt is not None and trip.receipt.status == "succeeded"
        assert trip.budget_cents is not None and trip.receipt.amount_cents <= trip.budget_cents
    t1 = res.trip1
    assert t1.followup is not None and t1.followup.about_skus
    for rec in t1.recs:
        assert rec.reason and rec.sponsored is False
    assert any(r.kind == "near_expiry_offer" and r.sku == "GUAC-KIT" for r in t1.recs), (
        "demo beat 0:50: the near-expiry guacamole kit markdown is one of trip 1's detours"
    )
    for trip in (*res.trips1, *res.trips2):  # re-plans must not re-add picked items as 'pending (added mid-trip)'
        assert "pending" not in trip.stop_status.values(), trip.stop_status
    assert any(ln.source == "online" for t in res.trips1 for ln in t.cart.active_lines()), "split sourcing: online"
    declined = {r.sku for t in res.trips1 for r in t.recs if t.stop_status.get(f"rec:{r.rec_id}") == "declined"}
    assert not declined & {r.sku for t in res.trips2 for r in t.recs}, "a declined detour must not come back"
    assert all(g.status == GoalStatus.COMPLETED for g in res.goals)
    assert res.staff_goals and res.staff_goals[0].robot_id == "rover-ave-2"
    assert res.stock_alerts > 0 and res.brain_steps > 0
    # planner -> VLA -> verifier loop (plan mode is the default once the gateway wires the loop services)
    assert res.exec_mode == "plan" and res.faults == "drop_on_first_grasp"
    assert len(res.loop.completed) == len(res.trips1) + len(res.trips2)
    assert all(e.status == "completed" for e in res.loop.completed)
    assert [f.effect for f in res.loop.firings] == ["drop_after_lift"]
    catch = res.loop.catches[0]
    assert catch.retried_ok and catch.attempt == 1 and catch.fault_ids == ["f-drop-1"], catch
    assert res.loop.dropoffs >= 1, "tacos for 6 is more than a 12-unit basket: the plan drops off at the checkout"


@needs_data
@pytest.mark.skipif(not _fakes_ready(), reason="fleet / brain / cameras fakes not built yet")
async def test_demo_direct_mode_is_the_v02_path() -> None:
    """PICKY_EXEC_MODE=direct (the kill switch) with every loop service still wired: no step plans, same trips."""
    res = await run_demo(quiet=True, timeout_s=60, staff_text=None, exec_mode="direct")
    assert res.exec_mode == "direct" and res.faults is None
    assert not res.loop.plans and not res.loop.firings
    assert all(t.state == TripState.DONE for t in (*res.trips1, *res.trips2))
