"""A full basket mid-plan (tacos for 6 = 15 units, the rover basket holds 12): the runner inserts a verified drop-off
(go to checkout -> handoff -> back to the shelf) before the next grasp, like the v0.2 brain, and the basket audit then
expects only the units placed after it."""

from arms.agent.loop import MAX_DROPOFFS
from arms.agent.tests.loop_world import build_loop_services
from arms.agent.tests.loop_world.helpers import TACO_TEXT, run
from contracts.plan import PlanRequest, StepStatus

ROBOT, STORE = "rover-ave-1", "store_ave"


def test_full_basket_drops_off_at_checkout_and_the_plan_completes() -> None:
    s = build_loop_services()

    async def go():  # noqa: ANN202
        plan = await s.planner.plan(PlanRequest(robot_id=ROBOT, text=TACO_TEXT, store_id=STORE, user_id="maya"))
        return await s.runner.run_plan(plan)

    out = run(go())
    assert out.status == "completed", out.summary
    assert all(st == "in_basket" for st in out.item_status.values())
    drop = [st for st in out.plan.steps if st.group_id == "dropoff"]
    assert [st.kind for st in drop] == ["navigate_to", "handoff", "navigate_to"]
    assert all(st.status == StepStatus.SUCCEEDED and st.origin == "recovery" for st in drop)
    grasp = next(st for st in out.plan.steps if st.step_id == drop[0].inserted_by)
    assert grasp.kind == "grasp" and out.plan.steps.index(grasp) == out.plan.steps.index(drop[-1]) + 1
    assert s.robot.calls["unload_basket"] == 2  # the drop-off + the checkout handoff
    assert s.robot.calls["pick"] == 15 and s.robot.calls["place_in_basket"] == 15
    audit = next(st for st in out.plan.steps if st.kind == "verify" and getattr(st, "purpose", "") == "audit")
    assert audit.status == StepStatus.SUCCEEDED  # expects only the units placed after the drop-off
    checks = s.events.of("basket.verified")
    assert checks and all(ev.check.ok for ev in checks)
    assert MAX_DROPOFFS >= 1
