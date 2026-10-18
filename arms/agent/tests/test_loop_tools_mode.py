"""Tools mode: the v0.2 LLM brain's motion tools become verified steps on ONE live plan; inner recoveries only."""

from typing import Any

from arms.agent.tests.loop_world import build_loop_services
from arms.agent.tests.loop_world.helpers import run
from contracts.bridge import pick_result_from, place_result_from
from contracts.faults import FaultMatch, FaultRule, FaultScenario
from contracts.fleet import Goal, GoalItem

ROBOT, STORE = "rover-ave-1", "store_ave"
SLOT = "MINI-F1-1-02"  # SALSA-MILD in the mini world


def _goal() -> Goal:
    return Goal(
        goal_id="g-tools",
        kind="shop",
        store_id=STORE,
        robot_id=ROBOT,
        title="salsa run",
        user_id="maya",
        items=[GoalItem(item_id="i1", query="salsa", sku="SALSA-MILD", slot_id=SLOT)],
    )


def test_tools_mode_drop_is_fixed_inside_the_tool_call() -> None:
    s = build_loop_services("drop_on_first_grasp")
    goal = _goal()

    async def go() -> tuple[Any, Any, Any, Any]:
        nav = await s.runner.run_tool_step(ROBOT, goal, "navigate_to", {"target": "slot", "slot_id": SLOT})
        pick = await s.runner.run_tool_step(
            ROBOT, goal, "pick_item", {"slot_id": SLOT, "sku": "SALSA-MILD", "item_id": "i1"}
        )
        place = await s.runner.run_tool_step(ROBOT, goal, "place_in_basket", {"item_id": "i1"})
        live = await s.runner.current_plan(goal.goal_id)
        return nav, pick, place, live

    nav, pick, place, live = run(go())
    assert nav.ok and pick.ok and place.ok
    pr = pick_result_from(pick)
    assert pr.picked and pr.instance is not None and pr.instance.sku == "SALSA-MILD"  # VERIFIED holding
    assert place_result_from(place, sku="SALSA-MILD", item_id="i1", picked_qty=1).basket_ok is True
    assert live is not None and live.mode == "tools" and live.planner.origin == "tools"
    assert [st.origin for st in live.steps].count("tool") >= 5
    assert any(st.origin == "recovery" for st in live.steps)  # the inner re-ground
    failed = s.events.of("step.failed")
    assert len(failed) == 1 and failed[0].decision.action == "reground" and failed[0].decision.inner
    assert len(s.events.of("plan.created")) == 1
    assert not s.events.of("item.picked")  # tools mode: the v0.2 executor commits v0.2 side effects itself
    assert s.catalog.picks == []


def test_tools_mode_outer_failure_returns_a_v02_brain_tool_error() -> None:
    scenario = FaultScenario(
        name="miss",
        description="every grasp misses",
        rules=[FaultRule(fault_id="m", effect="grasp_miss", match=FaultMatch(step_kind="grasp"), times=0)],
    )
    s = build_loop_services(scenario)
    goal = _goal()

    async def go() -> Any:
        await s.runner.run_tool_step(ROBOT, goal, "navigate_to", {"target": "slot", "slot_id": SLOT})
        return await s.runner.run_tool_step(
            ROBOT, goal, "pick_item", {"slot_id": SLOT, "sku": "SALSA-MILD", "item_id": "i1"}
        )

    out = run(go())
    assert not out.ok and out.error is not None
    assert out.error.error_code == "grasp_failed" and out.error.retryable
    assert out.decision is not None and not out.decision.inner and out.decision.action == "ask_user"
    assert not pick_result_from(out).picked
    actions = [e.decision.action for e in s.events.of("step.failed")]
    assert actions == ["reground", "realign", "ask_user"]  # 3rd charged attempt exhausts the budget -> outer ask
