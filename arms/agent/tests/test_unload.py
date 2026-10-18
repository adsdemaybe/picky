"""0.2.4 checkout unload in the runner: compiled per unit, skipped (handoff unloads at once) on a backend without the
basket -> counter primitive. (The photo-card + real unload run lives in arms/gateway/tests/test_judges_demo.py: it
needs the real arms' fakes.)"""

from arms.agent.tests.loop_world import build_loop_services
from arms.agent.tests.loop_world.helpers import run
from contracts.fleet import Goal, GoalItem
from contracts.plan import StepStatus

ROBOT, STORE = "rover-ave-1", "store_ave"
MILD_SLOT = "MINI-F1-1-02"


def _goal() -> Goal:
    return Goal(
        goal_id="g-u",
        kind="shop",
        store_id=STORE,
        robot_id=ROBOT,
        title="salsa x2",
        user_id="maya",
        items=[GoalItem(item_id="i1", query="salsa", sku="SALSA-MILD", slot_id=MILD_SLOT, qty=2)],
    )


def test_no_basket_unload_primitive_skips_the_unload_and_hands_off() -> None:
    s = build_loop_services()  # MiniRobot: no pick_from_basket -> VlaCapabilities.basket_unload False
    out = run(s.runner.run_goal(ROBOT, _goal()))
    assert out.status == "completed", out.summary
    unload = [st for st in out.plan.steps if (st.group_id or "").startswith("unload:")]
    assert len({st.group_id for st in unload}) == 2  # compiled per unit
    assert all(st.status == StepStatus.SKIPPED for st in unload)
    assert out.plan.steps[-1].kind == "handoff" and out.plan.steps[-1].status == StepStatus.SUCCEEDED
    assert not s.events.of("checkout.unload")
