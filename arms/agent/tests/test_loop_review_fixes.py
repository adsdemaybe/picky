"""Adversarial-review regressions: held units, produce choices, v0.2 stop mirroring, cancel safety, races, ownership."""

import asyncio
from typing import Any

import pytest

from arms.agent.tests.loop_world import build_loop_services
from arms.agent.tests.loop_world.helpers import run
from arms.agent.tests.loop_world.world import MiniCatalog, MiniRobot
from contracts.bridge import goal_update_from_outcome
from contracts.faults import FaultMatch, FaultRule, FaultScenario
from contracts.fleet import Goal, GoalItem, GoalStatus
from contracts.plan import ApprovalGranted, PlanRequest
from contracts.trust import ApprovalRequest, ApprovalResponse
from contracts.verify import StateFacts, VerifyRequest, evaluate_on_state

ROBOT, STORE = "rover-ave-1", "store_ave"
MILD_SLOT = "MINI-F1-1-02"


def _goal(*items: GoalItem, goal_id: str = "g-1", robot_id: str = ROBOT, trip_id: str | None = None) -> Goal:
    return Goal(
        goal_id=goal_id,
        kind="shop",
        store_id=STORE,
        robot_id=robot_id,
        title="test",
        user_id="maya",
        trip_id=trip_id,
        items=list(items),
    )


async def _hold(s: Any, sku: str) -> None:
    """Leave a unit in the gripper, as a goal cancelled mid-grasp would."""
    plano = await s.catalog.get_planogram(STORE)
    slot = next(x for x in plano.slots if x.sku == sku)
    await s.robot.navigate(ROBOT, [slot.approach_pose])
    await s.robot.pick(ROBOT, slot.slot_id, sku)


class RecordingTrips:
    """Just enough v0.2 TripController for the runner: stop statuses, lines, asks, mandate checks."""

    def __init__(self) -> None:
        self.stops: list[tuple[str, str]] = []
        self.lines: list[Any] = []

    async def set_stop_status(self, trip_id: str, stop_id: str, status: str, reason: str = "") -> None:
        self.stops.append((stop_id, status))

    async def add_line(self, trip_id: str, line: Any, approval_id: str | None = None) -> None:
        self.lines.append(line)

    async def ask(self, trip_id: str, req: ApprovalRequest) -> ApprovalResponse:
        if req.options:
            return ApprovalResponse(approval_id=req.approval_id, decision="choice", choice=req.options[0].option_id)
        return ApprovalResponse(approval_id=req.approval_id, decision="approve", channel="system")

    async def check_line(self, trip_id: str, line: Any) -> Any:
        class _D:
            verdict = "approve"

        return _D()


# ---- held units: never a second unit, never a foreign unit counted ---------------------------------------------------
def test_wrong_item_return_finds_the_slot_even_when_the_neighbour_is_on_the_list() -> None:
    s = build_loop_services("wrong_salsa")
    goal = _goal(
        GoalItem(item_id="i1", query="salsa", sku="SALSA-MILD", slot_id=MILD_SLOT),
        GoalItem(item_id="i2", query="hot salsa", sku="SALSA-HOT"),
    )
    out = run(s.runner.run_goal(ROBOT, goal))  # was AttributeError: GraspStep has no slot_id
    assert out.status == "completed" and out.item_status == {"i1": "in_basket", "i2": "in_basket"}
    back = next(e.step for e in s.events.of("step.started") if getattr(e.step, "destination", "") == "shelf")
    assert back.sku == "SALSA-HOT" and back.slot_id != MILD_SLOT  # back to the unit's OWN slot
    assert sorted(sku for _, sku, _ in s.catalog.picks) == ["SALSA-HOT", "SALSA-MILD"]


def test_a_unit_already_in_the_gripper_is_adopted_not_doubled() -> None:
    s = build_loop_services()

    async def go() -> Any:
        await _hold(s, "SALSA-MILD")
        return await s.runner.run_goal(ROBOT, _goal(GoalItem(item_id="i1", query="salsa", sku="SALSA-MILD")))

    out = run(go())
    assert out.status == "completed" and [e.decision.action for e in s.events.of("step.failed")] == ["adopt_held"]
    assert out.plan.items[0].picked_qty == 1 and [sku for _, sku, _ in s.catalog.picks] == ["SALSA-MILD"]
    assert [ln.qty for ln in out.run.local_cart] == [1]


def test_a_foreign_unit_in_the_gripper_is_never_counted_as_the_item() -> None:
    s = build_loop_services()

    async def go() -> Any:
        await _hold(s, "SALSA-HOT")
        return await s.runner.run_goal(ROBOT, _goal(GoalItem(item_id="i1", query="salsa", sku="SALSA-MILD")))

    # T0 has no place-back: the foreign unit is set aside in the basket for staff (never charged), then the grasp
    out = run(go())
    assert out.status == "completed", out.summary
    failed = s.events.of("step.failed")
    assert [e.decision.action for e in failed][:2] == ["return_held", "retry"]
    assert failed[1].decision.rule_id == "no_put_back" and "not charged" in failed[1].decision.reason
    assert not any(e.decision.action in ("ask_user", "escalate") for e in failed)
    assert out.plan.items[0].picked_qty == 1 and out.plan.items[0].status == "in_basket"
    assert [(ln.sku, ln.qty) for ln in out.run.local_cart] == [("SALSA-MILD", 1)]  # the foreign unit: no line
    aside = [e for e in out.run.effects if e.kind == "goal_item_progress" and e.payload.get("set_aside")]
    assert len(aside) == 1
    assert sorted(sku for _, sku, _ in s.catalog.picks) == ["SALSA-HOT", "SALSA-MILD"]  # stock: it left its shelf


# ---- produce: the chosen units survive a re-ground; one choice per unit; v0.2 pick.choices ----------------------
def _avocado_run(qty: int, scenario: Any = None) -> tuple[Any, Any, dict[str, Any]]:
    s = build_loop_services(scenario)
    seen: dict[str, Any] = {}

    async def answer(req: ApprovalRequest) -> ApprovalResponse:
        if req.options:
            ids = [o.option_id for o in req.options]
            seen.update(options=ids, multi=req.multi)
            pick = ids[-qty:]  # deliberately NOT the default order
            return ApprovalResponse(approval_id=req.approval_id, decision="choice", choice=pick[0], choices=pick)
        return ApprovalResponse(approval_id=req.approval_id, decision="approve")

    s.runner.answer = answer
    out = run(s.runner.run_goal(ROBOT, _goal(GoalItem(item_id="i1", query="avocado", sku="AVOCADO", qty=qty))))
    return s, out, seen


def test_produce_choice_survives_a_dropped_grasp() -> None:
    drop = FaultScenario(
        name="drop",
        description="",
        rules=[FaultRule(fault_id="d", effect="drop_after_lift", match=FaultMatch(step_kind="grasp", attempt=1))],
    )
    s, out, seen = _avocado_run(1, drop)
    assert out.status == "completed" and out.metrics.verifier_catches == 1
    assert out.plan.items[0].instance_ids == seen["options"][-1:]
    pc = s.events.of("pick.choices")
    assert len(pc) == 1 and [c.instance_id for c in pc[0].choice.candidates] == seen["options"]
    assert pc[0].approval_id  # the web app links the chooser to the approval card


def test_multi_unit_produce_takes_one_chosen_unit_per_unit() -> None:
    _, out, seen = _avocado_run(2)
    assert seen["multi"] is True
    assert out.plan.items[0].instance_ids == seen["options"][-2:]


# ---- v0.2 compatibility: stops mirrored, goal finished by the brain --------------------------------------------------
def test_item_status_is_mirrored_onto_trip_route_stops() -> None:
    s = build_loop_services()
    s.trips = RecordingTrips()  # type: ignore[assignment]
    goal = _goal(
        GoalItem(item_id="stop_1", query="salsa", sku="SALSA-MILD", slot_id=MILD_SLOT, stop_id="stop_1"),
        GoalItem(item_id="stop_2", query="hot salsa", sku="SALSA-HOT", optional=True, stop_id="stop_2"),
        trip_id="trip-1",
    )

    async def no_recs(req: ApprovalRequest) -> ApprovalResponse:
        ok = req.kind.value != "recommendation"
        return ApprovalResponse(approval_id=req.approval_id, decision="approve" if ok else "reject")

    s.trips.ask = lambda trip_id, req: no_recs(req)  # type: ignore[method-assign,attr-defined]
    out = run(s.runner.run_goal(ROBOT, goal))
    assert out.status == "completed"
    stops = s.trips.stops  # type: ignore[attr-defined]
    assert ("stop_1", "navigating") in stops and ("stop_1", "arrived") in stops and ("stop_1", "done") in stops
    assert ("stop_2", "declined") in stops
    assert len(stops) == len(set(stops))  # once per stop + status
    upd = goal_update_from_outcome(out)
    assert upd.status == GoalStatus.COMPLETED and upd.summary == out.summary


def test_goal_update_from_a_cancelled_outcome_leaves_status_to_the_fleet() -> None:
    s = build_loop_services()
    out = run(s.runner.run_goal(ROBOT, _goal(GoalItem(item_id="i1", query="salsa", sku="SALSA-MILD"))))
    cancelled = out.model_copy(update={"status": "cancelled"})
    assert goal_update_from_outcome(cancelled).status is None


# ---- partial picks: a substitute never re-prices units already in the basket ----------------------------------------
def test_shelf_runs_out_mid_item_orders_the_rest_online_instead_of_substituting() -> None:
    s = build_loop_services()
    s.trips = RecordingTrips()  # type: ignore[assignment]
    slot = s.catalog.planogram.slot(MILD_SLOT)
    slot.qty, slot.lots[0].qty = 2, 2
    goal = _goal(
        GoalItem(item_id="i1", query="salsa", sku="SALSA-MILD", slot_id=MILD_SLOT, qty=3, substitutes=["SALSA-HOT"]),
        trip_id="trip-1",
    )
    out = run(s.runner.run_goal(ROBOT, goal))
    lines = s.trips.lines  # type: ignore[attr-defined]
    got = [(ln.sku, ln.qty, ln.source) for ln in lines]
    assert got == [("SALSA-MILD", 2, "in_store"), ("SALSA-MILD", 1, "online")]
    assert out.item_status == {"i1": "online"} and "SALSA-HOT" not in {sku for _, sku, _ in s.catalog.picks}


# ---- cancellation safety ---------------------------------------------------------------------------------------------
def test_cancel_while_asking_withdraws_the_pending_question() -> None:
    s = build_loop_services()

    class Broker:
        def __init__(self) -> None:
            self.asked: list[str] = []
            self.cancelled: list[str] = []

        async def request(self, req: ApprovalRequest) -> ApprovalResponse:
            self.asked.append(req.approval_id)
            await asyncio.Event().wait()  # the shopper never answers
            raise AssertionError

        async def cancel(self, approval_id: str, reason: str = "") -> bool:
            self.cancelled.append(approval_id)
            return True

    s.approvals = Broker()  # type: ignore[assignment]

    async def go() -> None:
        goal = _goal(GoalItem(item_id="i1", query="hot salsa", sku="SALSA-HOT", optional=True))
        task = asyncio.create_task(s.runner.run_goal(ROBOT, goal))
        for _ in range(200):
            await asyncio.sleep(0.005)
            if s.approvals.asked:  # type: ignore[attr-defined]
                break
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    run(go())
    assert s.approvals.cancelled == s.approvals.asked and s.approvals.asked  # type: ignore[attr-defined]


def test_cancel_during_commit_finishes_the_effect_set() -> None:
    entered = asyncio.Event()

    class SlowCatalog(MiniCatalog):
        async def record_pick(self, *a: Any, **kw: Any) -> Any:
            entered.set()
            await asyncio.sleep(0.05)
            return await super().record_pick(*a, **kw)

    s = build_loop_services()
    s.catalog = SlowCatalog(s)

    async def go() -> Any:
        plan = await s.planner.plan(PlanRequest(robot_id=ROBOT, text="salsa", store_id=STORE))
        task = asyncio.create_task(s.runner.run_plan(plan))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return await s.runner.run_state(plan.plan_id)

    state = run(go())
    kinds = [e.kind for e in state.effects]
    assert kinds == ["stop_arrived", "apply_scan", "record_pick", "goal_item_progress", "add_cart_line"]
    assert [ln.qty for ln in state.local_cart] == [1] and state.status.value == "cancelled"


# ---- races + uncheckable atoms ---------------------------------------------------------------------------------------
def test_two_robots_racing_for_the_last_unit_degrade_cleanly() -> None:
    s = build_loop_services()
    s.robot = MiniRobot(s, robot_ids=("rover-ave-1", "rover-ave-2"))
    slot = s.catalog.planogram.slot(MILD_SLOT)
    slot.qty, slot.lots[0].qty = 1, 1

    async def go() -> list[Any]:
        goals = [
            _goal(GoalItem(item_id="i1", query="salsa", sku="SALSA-MILD", slot_id=MILD_SLOT), goal_id=g, robot_id=r)
            for g, r in (("g-a", "rover-ave-1"), ("g-b", "rover-ave-2"))
        ]
        return list(await asyncio.gather(*(s.runner.run_goal(g.robot_id or "", g) for g in goals)))

    outs = run(go())
    statuses = sorted(o.item_status["i1"] for o in outs)
    assert statuses == ["in_basket", "online"]  # one got the jar, the other re-sourced online: no crash, no double sale
    assert [sku for _, sku, _ in s.catalog.picks] == ["SALSA-MILD"]


def test_approval_granted_is_checkable_once_the_runner_passes_answers() -> None:
    s = build_loop_services()
    cond = ApprovalGranted(approval_id="apr-1")

    async def go(answers: dict[str, bool]) -> str:
        req = VerifyRequest(verify_id=f"v-{len(answers)}", robot_id=ROBOT, condition=cond, approvals=answers)
        return (await s.verifier.verify(req)).outcome

    assert run(go({})) == "uncertain"
    assert run(go({"apr-1": True})) == "pass"
    facts = StateFacts(robot_id=ROBOT, approvals={"apr-1": False})
    assert evaluate_on_state(cond, facts)[0] == "fail"


def test_a_reassigned_goal_never_reuses_plan_exec_or_verify_ids() -> None:
    from contracts.fleet import Assignment

    s = build_loop_services()
    s.robot = MiniRobot(s, robot_ids=("rover-ave-1", "rover-ave-2"))
    item = GoalItem(item_id="i1", query="salsa", sku="SALSA-MILD", slot_id=MILD_SLOT)

    def asg(robot_id: str, attempt: int) -> Assignment:
        return Assignment(
            assignment_id=f"asg-{attempt}",
            goal_id="g-1",
            robot_id=robot_id,
            store_id=STORE,
            attempt=attempt,
            score=1.0,
            reason="test",
        )

    first = _goal(item, robot_id="rover-ave-1").model_copy(update={"assignments": [asg("rover-ave-1", 1)]})
    second = first.model_copy(
        update={"robot_id": "rover-ave-2", "assignments": [*first.assignments, asg("rover-ave-2", 2)]}
    )

    async def go() -> tuple[Any, Any]:
        a = await s.runner.run_goal("rover-ave-1", first)
        return a, await s.runner.run_goal("rover-ave-2", second)  # the fleet reassigned the goal (attempt 2)

    a, b = run(go())
    assert a.plan.plan_id == "plan-g-1" and b.plan.plan_id == "plan-g-1-a2"
    assert b.status == "completed" and not s.events.of("step.failed")  # was: idempotency_conflict on every VLA step
