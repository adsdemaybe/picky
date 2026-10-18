"""Photo check on a backend with no put-back (Isaac): a unit the shopper marks wrong is never counted or charged."""

from typing import Any

from arms.agent.loop import PlanLoop
from arms.agent.tests.loop_world import build_loop_services
from arms.agent.tests.loop_world.helpers import run
from contracts.plan import GraspStep, PlaceStep, PlanRequest
from contracts.policy import DEFAULT_RETRY_POLICY
from contracts.trust import ApprovalRequest, ApprovalResponse

ROBOT, STORE = "rover-ave-1", "store_ave"


def test_wrong_unit_without_put_back_is_set_aside_uncharged_and_regrabbed() -> None:
    s = build_loop_services()
    real_caps = s.vla.capabilities

    async def no_put_back(robot_id: str) -> Any:
        return [c.model_copy(update={"place_back": False}) for c in await real_caps(robot_id)]

    s.vla.capabilities = no_put_back  # type: ignore[method-assign]
    wrong_once = {"left": 1}
    rejected: list[Any] = []

    async def answer(req: ApprovalRequest) -> ApprovalResponse:
        if req.kind.value == "verify_item":
            pick = "correct"
            if wrong_once["left"]:
                wrong_once["left"] -= 1
                pick = "wrong"
                rejected.append(req.approval_id)
            return ApprovalResponse(approval_id=req.approval_id, decision="choice", choice=pick)
        return ApprovalResponse(approval_id=req.approval_id, decision="approve")

    s.runner = PlanLoop(s, policy=DEFAULT_RETRY_POLICY, answer=answer, verify_items=True, heartbeat_s=0.05)

    async def go() -> Any:
        plan = await s.planner.plan(PlanRequest(robot_id=ROBOT, text="2 mild salsa", store_id=STORE))
        return await s.runner.run_plan(plan)

    out = run(go())
    assert out.status == "completed", out.summary
    assert len(rejected) == 1
    aside = [e for e in out.run.effects if e.kind == "goal_item_progress" and e.payload.get("set_aside")]
    assert len(aside) == 1
    picked = {e.step_id: e.payload.get("instance_id") for e in out.run.effects if e.kind == "record_pick"}
    wrong_unit = picked[aside[0].step_id]
    assert wrong_unit and len(picked) == 3  # the wrong unit is physically in the basket (the basket audit counts it)
    # no put-back step was attempted: the wrong unit went to the basket for staff and a fresh unit was grabbed
    places = [st for st in out.plan.steps if isinstance(st, PlaceStep) and st.status.value == "succeeded"]
    assert all(st.destination == "basket" for st in places) and len(places) == 3
    assert sum(isinstance(st, GraspStep) and st.origin == "recovery" for st in out.plan.steps) == 1
    # charged: exactly the list qty, and the rejected unit is not on the line
    lines = out.run.local_cart
    assert len(lines) == 1 and lines[0].sku == "SALSA-MILD" and lines[0].qty == 2
    assert wrong_unit not in lines[0].instance_ids and len(lines[0].instance_ids) == 2
    assert out.run.local_cart_cents == 2 * lines[0].unit_price_cents
    item = out.plan.item(lines[0].list_item_id) or next(i for i in out.plan.items if i.sku == "SALSA-MILD")
    assert item.picked_qty == 2 and wrong_unit not in item.instance_ids
