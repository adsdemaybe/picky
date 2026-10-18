"""Photo check on a backend with no put-back (Isaac): a unit the shopper marks wrong is never counted or charged, and
the checkout unload still puts it on the counter (labelled 'set aside for staff, not charged')."""

from collections.abc import Callable
from typing import Any

from arms.agent.loop import PlanLoop
from arms.agent.tests.loop_world import build_loop_services
from arms.agent.tests.loop_world.helpers import run
from contracts.faults import FaultMatch, FaultRule, FaultScenario
from contracts.plan import GraspStep, PlaceStep, PlanRequest
from contracts.policy import DEFAULT_RETRY_POLICY
from contracts.trust import ApprovalRequest, ApprovalResponse

ROBOT, STORE = "rover-ave-1", "store_ave"


def _no_put_back(s: Any) -> None:
    real_caps = s.vla.capabilities

    async def caps(robot_id: str) -> Any:
        return [c.model_copy(update={"place_back": False}) for c in await real_caps(robot_id)]

    s.vla.capabilities = caps  # type: ignore[method-assign]


def _wrong_at(*answers: int) -> tuple[Callable[[ApprovalRequest], Any], list[str]]:
    """Photo-check answers: 'wrong' for the n-th photo cards in ``answers`` (1-based), 'correct' otherwise."""
    seen: list[str] = []
    rejected: list[str] = []

    async def answer(req: ApprovalRequest) -> ApprovalResponse:
        if req.kind.value == "verify_item":
            seen.append(req.approval_id)
            pick = "wrong" if len(seen) in answers else "correct"
            if pick == "wrong":
                rejected.append(req.approval_id)
            return ApprovalResponse(approval_id=req.approval_id, decision="choice", choice=pick)
        return ApprovalResponse(approval_id=req.approval_id, decision="approve")

    return answer, rejected


def _unload_by_unit(s: Any) -> tuple[int, dict[int, list[Any]]]:
    events = s.events.of("checkout.unload")
    total = max((e.total for e in events), default=0)
    return total, {n: [e for e in events if e.n == n and e.status != "done"] for n in range(1, total + 1)}


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


def test_grasp_of_another_unit_than_the_chosen_one_is_set_aside_and_the_chosen_unit_taken() -> None:
    """pick_choice pinned a unit; the grasp (as a T1 policy may) took a different unit of the SKU: no put-back, so
    that unit is set aside uncharged and the scripted grasp takes the chosen instance."""
    s = build_loop_services()
    real_caps, real_pick = s.vla.capabilities, s.robot.pick

    async def no_put_back(robot_id: str) -> Any:
        return [c.model_copy(update={"place_back": False}) for c in await real_caps(robot_id)]

    taken_wrong: list[str] = []

    async def stray_pick(robot_id: str, slot_id: str, sku: str, instance_id: str | None = None) -> Any:
        if instance_id and not taken_wrong:  # the first pinned grasp takes a neighbour of the chosen unit
            other = next(u for u in await s.robot.list_instances(robot_id, slot_id) if u.instance_id != instance_id)
            taken_wrong.append(other.instance_id)
            return await real_pick(robot_id, slot_id, sku, other.instance_id)
        return await real_pick(robot_id, slot_id, sku, instance_id)

    s.vla.capabilities = no_put_back  # type: ignore[method-assign]
    s.robot.pick = stray_pick  # type: ignore[method-assign]

    async def go() -> Any:
        plan = await s.planner.plan(PlanRequest(robot_id=ROBOT, text="1 mild salsa", store_id=STORE))
        grasp = next(st for st in plan.steps if isinstance(st, GraspStep))
        units = await s.robot.list_instances(ROBOT, grasp.target.slot_id)
        chosen = units[-1].instance_id
        grasp.target = grasp.target.model_copy(update={"instance_id": chosen})
        return chosen, await s.runner.run_plan(plan)

    chosen, out = run(go())
    assert out.status == "completed", out.summary
    assert taken_wrong and taken_wrong[0] != chosen
    lines = out.run.local_cart
    assert len(lines) == 1 and lines[0].qty == 1 and lines[0].instance_ids == [chosen]
    assert out.run.local_cart_cents == lines[0].unit_price_cents
    aside = [e for e in out.run.effects if e.kind == "goal_item_progress" and e.payload.get("set_aside")]
    picked = {e.step_id: e.payload.get("instance_id") for e in out.run.effects if e.kind == "record_pick"}
    assert len(aside) == 1 and picked[aside[0].step_id] == taken_wrong[0]
    regrab = [st for st in out.plan.steps if isinstance(st, GraspStep) and st.origin == "recovery"]
    assert len(regrab) == 1 and regrab[0].target.instance_id == chosen
    # the shopper-facing step text is in product words, not raw instance ids
    texts = [e.step.text for e in s.events.of("brain.step")]
    said = [t for t in texts if "the one you chose" in t]
    assert said and any("grabbed a different Mild Chunky Salsa" in t for t in said), texts
    assert not any(taken_wrong[0] in t or chosen in t for t in said)


def _run_wrong_unit_with_unload(text: str, *wrong: int) -> tuple[Any, Any, list[str]]:
    s = build_loop_services(basket_unload=True)  # the 0.2.4 basket -> counter primitive: the arm unloads each unit
    _no_put_back(s)
    answer, rejected = _wrong_at(*wrong)
    s.runner = PlanLoop(s, policy=DEFAULT_RETRY_POLICY, answer=answer, verify_items=True, heartbeat_s=0.05)

    async def go() -> Any:
        plan = await s.planner.plan(PlanRequest(robot_id=ROBOT, text=text, store_id=STORE))
        return await s.runner.run_plan(plan)

    return s, run(go()), rejected


def test_set_aside_unit_is_unloaded_at_the_counter_like_the_others_but_never_charged() -> None:
    """2 x salsa, the first photo answered 'wrong' with no put-back: 3 units ride in the basket, so 3 units are
    unloaded (started -> placed -> verified each, in basket order), the cart still says 2 x price, and the set-aside
    unit's counter card says it is not charged."""
    s, out, rejected = _run_wrong_unit_with_unload("2 mild salsa", 1)
    assert out.status == "completed", out.summary
    assert len(rejected) == 1
    aside = [e for e in out.run.effects if e.kind == "goal_item_progress" and e.payload.get("set_aside")]
    picked = {e.step_id: e.payload.get("instance_id") for e in out.run.effects if e.kind == "record_pick"}
    wrong_unit = picked[aside[0].step_id]
    # N + 1 units unloaded at the counter, each started -> placed -> verified, numbered in order
    total, by_unit = _unload_by_unit(s)
    assert total == 3
    for n, evs in by_unit.items():
        assert [e.status for e in evs] == ["started", "placed", "verified"], (n, [e.text for e in evs])
    starts = [e.n for e in s.events.of("checkout.unload") if e.status == "started"]
    assert starts == [1, 2, 3]
    assert len(s.robot.counter) == 3 and all(u.sku == "SALSA-MILD" for u in s.robot.counter)
    assert not run(s.robot.state(ROBOT)).basket
    # basket order: the wrong unit went in first, so it comes out first, labelled as set aside / not charged
    on_counter = [e.payload.get("instance_id") for e in out.run.effects if e.kind == "unload_to_counter"]
    assert on_counter[0] == wrong_unit and len(set(on_counter)) == 3
    assert all("not charged" in e.text for e in by_unit[1])
    assert not any("not charged" in e.text for n in (2, 3) for e in by_unit[n])
    done = [e for e in s.events.of("checkout.unload") if e.status == "done"]
    assert len(done) == 1 and "3 items on the counter (1 set aside for staff, not charged)" in done[0].text
    # the counter cards: one 'not charged' (kept off the purchase photos), two ordinary auto-accepted ones
    cards = [e.verification for e in s.events.of("item.verification") if "on the counter" in e.verification.question]
    assert len(cards) == 3
    flagged = [v for v in cards if "not charged" in v.question]
    assert len(flagged) == 1 and flagged[0].status == "rejected"
    assert sum(v.status == "auto_accepted" for v in cards) == 2
    # billing: exactly N units, N x price, the set-aside unit on no line
    lines = out.run.local_cart
    assert len(lines) == 1 and lines[0].sku == "SALSA-MILD" and lines[0].qty == 2
    assert wrong_unit not in lines[0].instance_ids
    assert out.run.local_cart_cents == 2 * lines[0].unit_price_cents
    assert [e.payload["qty"] for e in out.run.effects if e.kind == "add_cart_line"] == [2]
    aside_steps = {st.step_id for st in out.plan.steps if "not charged" in (st.group_label or "")}
    assert len(aside_steps) == 4  # ground in the basket -> grasp -> place on the counter -> verify
    assert {e.kind for e in out.run.effects if e.step_id in aside_steps} == {"unload_to_counter"}


def test_set_aside_second_unit_unloads_between_its_neighbours_in_basket_order() -> None:
    """The 2nd unit is answered 'wrong': basket = unit 1, the wrong unit, its replacement -> unloaded in that order."""
    s, out, rejected = _run_wrong_unit_with_unload("2 mild salsa", 2)
    assert out.status == "completed", out.summary
    assert len(rejected) == 1
    aside = [e for e in out.run.effects if e.kind == "goal_item_progress" and e.payload.get("set_aside")]
    picked = {e.step_id: e.payload.get("instance_id") for e in out.run.effects if e.kind == "record_pick"}
    wrong_unit = picked[aside[0].step_id]
    total, by_unit = _unload_by_unit(s)
    assert total == 3 and all([e.status for e in evs] == ["started", "placed", "verified"] for evs in by_unit.values())
    on_counter = [e.payload.get("instance_id") for e in out.run.effects if e.kind == "unload_to_counter"]
    assert on_counter[1] == wrong_unit
    assert [any("not charged" in e.text for e in by_unit[n]) for n in (1, 2, 3)] == [False, True, False]
    lines = out.run.local_cart
    assert lines[0].qty == 2 and out.run.local_cart_cents == 2 * lines[0].unit_price_cents
    assert wrong_unit not in lines[0].instance_ids


# ---- verifier-caught wrong grasp (live vhagar, goal_bu7gy5gm): never strand the trip on a put-back it cannot run ----
_NIKE = "Brand: Nike\nProduct Name: Air Max 270 React\nSize: 10"  # what the Isaac label check read on the live trip


def _mislabel_first_grasp(sku: str = "SALSA-MILD") -> FaultScenario:
    rule = FaultRule(
        fault_id="f-mislabel",
        effect="mislabel",
        match=FaultMatch(step_kind="grasp", sku=sku, attempt=1),
        params={"text": _NIKE},
    )
    return FaultScenario(name="mislabel_first_grasp", description="label check fails on the first grasp", rules=[rule])


def _verifier_caught_run(*, put_back: bool | None, basket_unload: bool = False) -> tuple[Any, Any, list[Any]]:
    """1 x mild salsa; the first grasp's label check reads a Nike shoebox. ``put_back`` False = T0 says it has no
    place-back primitive (Isaac); None = the capability claims one but the backend has none (the live failure)."""
    s = build_loop_services(_mislabel_first_grasp(), basket_unload=basket_unload)
    if put_back is False:
        _no_put_back(s)
    asked: list[ApprovalRequest] = []

    async def answer(req: ApprovalRequest) -> ApprovalResponse:
        asked.append(req)
        if req.kind.value == "verify_item":
            return ApprovalResponse(approval_id=req.approval_id, decision="choice", choice="correct")
        return ApprovalResponse(approval_id=req.approval_id, decision="approve")

    s.runner = PlanLoop(s, policy=DEFAULT_RETRY_POLICY, answer=answer, verify_items=True, heartbeat_s=0.05)

    async def go() -> Any:
        plan = await s.planner.plan(PlanRequest(robot_id=ROBOT, text="1 mild salsa", store_id=STORE))
        return await s.runner.run_plan(plan)

    return s, run(go()), asked


def _assert_set_aside_and_regrabbed(s: Any, out: Any, asked: list[Any]) -> str:
    assert out.status == "completed", out.summary
    caught = s.events.of("step.failed")[0]
    assert caught.failure.step_kind == "grasp" and caught.failure.caught_by_verifier
    assert caught.decision.action == "return_item"
    # never 'Try again?' / a hand-off for a step the robot cannot run
    assert not [r for r in asked if r.kind.value not in ("verify_item",)], [r.title for r in asked]
    assert not any(e.decision.action in ("ask_user", "escalate") for e in s.events.of("step.failed"))
    texts = [e.step.text for e in s.events.of("brain.step")]
    assert not any("Try again" in t or "another robot" in t for t in texts), texts
    # the mislabelled unit rode in the basket uncounted; a fresh unit was grabbed and is the one charged
    aside = [e for e in out.run.effects if e.kind == "goal_item_progress" and e.payload.get("set_aside")]
    assert len(aside) == 1
    picked = {e.step_id: e.payload.get("instance_id") for e in out.run.effects if e.kind == "record_pick"}
    wrong_unit = picked[aside[0].step_id]
    assert wrong_unit and len(picked) == 2
    aside_step = next(st for st in out.plan.steps if st.step_id == aside[0].step_id)
    assert isinstance(aside_step, PlaceStep) and aside_step.destination == "basket"
    assert "set aside for staff, not charged" in aside_step.title and "SALSA" not in aside_step.title
    lines = out.run.local_cart
    assert [(ln.sku, ln.qty) for ln in lines] == [("SALSA-MILD", 1)]
    assert wrong_unit not in lines[0].instance_ids and len(lines[0].instance_ids) == 1
    assert out.run.local_cart_cents == lines[0].unit_price_cents
    item = next(i for i in out.plan.items if i.sku == "SALSA-MILD")
    assert item.picked_qty == 1 and item.status == "in_basket" and wrong_unit not in item.instance_ids
    return wrong_unit


def test_verifier_caught_wrong_grasp_without_put_back_is_set_aside_and_regrabbed() -> None:
    """T0 says it cannot put back (Isaac): the label catch sets the held unit aside in the basket (never charged) and
    grabs another; no put-back step is ever planned."""
    s, out, asked = _verifier_caught_run(put_back=False)
    _assert_set_aside_and_regrabbed(s, out, asked)
    caught = s.events.of("step.failed")[0]
    assert "not charged" in caught.decision.reason and "putting it back" not in caught.decision.reason
    started = [e.step for e in s.events.of("step.started")]
    assert not any(isinstance(st, PlaceStep) and st.destination == "shelf" for st in started)
    assert len(s.events.of("step.failed")) == 1  # nothing else failed on the way
    regrab = [st for st in out.plan.steps if isinstance(st, GraspStep) and st.status.value == "succeeded"]
    assert len(regrab) == 1 and regrab[0].attempts == 2  # the same grasp step, re-run on a fresh unit


def test_put_back_the_backend_cannot_run_becomes_a_set_aside_not_a_question() -> None:
    """The live failure: the capability claimed a put-back but T0 has no primitive ('unsupported_step'). The put-back
    step turns into a set-aside place (no 'Try again?', no hand-off to another robot) and the trip completes."""
    s, out, asked = _verifier_caught_run(put_back=None)
    _assert_set_aside_and_regrabbed(s, out, asked)
    failed = s.events.of("step.failed")
    assert [e.failure.code for e in failed][1] == "unsupported_step"
    assert failed[1].decision.action == "retry" and failed[1].decision.rule_id == "no_put_back"
    assert "not charged" in failed[1].decision.reason
    converted = next(st for st in out.plan.steps if st.step_id == failed[1].step_id)
    assert isinstance(converted, PlaceStep) and converted.destination == "basket" and converted.slot_id is None
    assert converted.status.value == "succeeded" and converted.attempts == 2


def test_no_put_back_is_remembered_for_the_rest_of_the_run() -> None:
    """After one 'unsupported_step' put-back, a later wrong unit is set aside straight away (no second failed
    put-back attempt)."""
    s = build_loop_services(
        FaultScenario(
            name="two_mislabels",
            description="both units' first grasps read a wrong label",
            rules=[
                FaultRule(
                    fault_id="f-m",
                    effect="mislabel",
                    match=FaultMatch(step_kind="grasp", sku="SALSA-MILD", attempt=1),
                    params={"text": _NIKE},
                    times=2,
                )
            ],
        )
    )

    async def go() -> Any:
        plan = await s.planner.plan(PlanRequest(robot_id=ROBOT, text="2 mild salsa", store_id=STORE))
        return await s.runner.run_plan(plan)

    out = run(go())
    assert out.status == "completed", out.summary
    codes = [e.failure.code for e in s.events.of("step.failed")]
    assert codes.count("unsupported_step") == 1, codes
    assert sum(1 for e in out.run.effects if e.kind == "goal_item_progress" and e.payload.get("set_aside")) == 2
    lines = out.run.local_cart
    assert [(ln.sku, ln.qty) for ln in lines] == [("SALSA-MILD", 2)]


def test_verifier_caught_set_aside_unit_is_unloaded_at_the_counter_not_charged() -> None:
    s, out, asked = _verifier_caught_run(put_back=False, basket_unload=True)
    wrong_unit = _assert_set_aside_and_regrabbed(s, out, asked)
    total, by_unit = _unload_by_unit(s)
    assert total == 2 and all([e.status for e in evs] == ["started", "placed", "verified"] for evs in by_unit.values())
    on_counter = [e.payload.get("instance_id") for e in out.run.effects if e.kind == "unload_to_counter"]
    assert on_counter[0] == wrong_unit and all("not charged" in e.text for e in by_unit[1])
    assert "Mild Chunky Salsa" in by_unit[1][0].text and "SALSA" not in by_unit[1][0].text
    done = [e for e in s.events.of("checkout.unload") if e.status == "done"]
    assert "(1 set aside for staff, not charged)" in done[0].text


def test_wrong_unit_is_still_put_back_where_the_backend_can() -> None:
    """Put-back supported (the 2D fake's overlay): the wrong jar goes back on its shelf as before, nothing set aside."""
    s = build_loop_services("wrong_salsa")

    async def go() -> Any:
        plan = await s.planner.plan(PlanRequest(robot_id=ROBOT, text="1 mild salsa", store_id=STORE))
        return await s.runner.run_plan(plan)

    out = run(go())
    assert out.status == "completed", out.summary
    caught = s.events.of("step.failed")[0]
    assert caught.decision.action == "return_item" and "putting it back" in caught.decision.reason
    back = [st for st in out.plan.steps if isinstance(st, PlaceStep) and st.destination == "shelf"]
    assert len(back) == 1 and back[0].status.value == "succeeded"
    assert not out.run.effects or not any(e.payload.get("set_aside") for e in out.run.effects)
    assert not any("not charged" in e.step.text for e in s.events.of("brain.step"))
    assert [(ln.sku, ln.qty) for ln in out.run.local_cart] == [("SALSA-MILD", 1)]
