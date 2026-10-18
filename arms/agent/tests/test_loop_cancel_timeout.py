"""Cancellation (plan and exec level) and timeouts -> typed error codes (the runner over the loop stubs)."""

import asyncio
from typing import Any

import pytest

from arms.agent.tests.loop_world import build_loop_services
from arms.agent.tests.loop_world.helpers import TACO_TEXT, run
from contracts.plan import GraspStep, NavigateToStep, PlanRequest
from contracts.services import run_vla_to_terminal
from contracts.vla import VlaRequest, VlaStatus

ROBOT, STORE = "rover-ave-1", "store_ave"


def test_cancel_mid_navigation_stops_robot_and_publishes_cancelled() -> None:
    s = build_loop_services(motion_s=5.0)

    async def go() -> tuple[str, Any]:
        plan = await s.planner.plan(PlanRequest(robot_id=ROBOT, text=TACO_TEXT, store_id=STORE))
        task = asyncio.create_task(s.runner.run_plan(plan))
        for _ in range(200):  # wait until the first navigate is running
            await asyncio.sleep(0.01)
            if any(e.type == "vla.status" and e.status.phase == "accepted" for e in s.events.events):
                break
        state = await s.runner.cancel(plan.plan_id, "shopper pressed stop")
        with pytest.raises(asyncio.CancelledError):
            await task
        nav = plan.steps[0]
        rec = await s.vla.get_exec(f"{plan.plan_id}:{nav.step_id}:a1")
        return state.status.value, rec.terminal

    status, terminal = run(go())
    assert status == "cancelled"
    assert terminal is not None and terminal.phase == "cancelled" and terminal.error.code == "cancelled"
    assert s.robot.calls["stop"] >= 1 and s.robot.calls["pick"] == 0
    last = s.events.events[-1]
    assert last.type == "plan.completed" and last.status == "cancelled"


def test_vla_cancel_is_idempotent() -> None:
    s = build_loop_services(motion_s=5.0)

    async def go() -> tuple[VlaStatus, VlaStatus]:
        plan = await s.planner.plan(PlanRequest(robot_id=ROBOT, text="salsa", store_id=STORE))
        req = VlaRequest(exec_id="c:s1:a1", robot_id=ROBOT, step=plan.steps[0], deadline_s=30, heartbeat_s=0.05)
        it = s.vla.execute(req).__aiter__()
        assert (await it.__anext__()).phase == "accepted"
        a = await s.vla.cancel(req.exec_id, "stop")
        b = await s.vla.cancel(req.exec_id, "stop again")
        return a, b

    a, b = run(go())
    assert a.phase == b.phase == "cancelled" and a.seq == b.seq


def test_hung_grasp_times_out_with_deadline_exceeded_then_retries() -> None:
    s = build_loop_services("slow_vla", heartbeat_s=0.05)

    async def go() -> Any:
        plan = await s.planner.plan(PlanRequest(robot_id=ROBOT, text=TACO_TEXT, store_id=STORE))
        return plan, await s.runner.run_plan(plan)

    plan, out = run(go())
    assert out.status == "completed"
    failed = s.events.of("step.failed")
    assert len(failed) == 1
    f = failed[0]
    gid = next(st.step_id for st in plan.steps if isinstance(st, GraspStep))
    assert f.step_id == gid and f.failure.code == "deadline_exceeded" and f.failure.kind == "stalled"
    assert f.decision.rule_id == "deadline" and f.decision.action == "retry"  # downgrade on T0 = plain retry
    exec1 = [e.status for e in s.events.of("vla.status") if e.status.exec_id == f"{plan.plan_id}:{gid}:a1"]
    assert exec1[0].phase == "accepted" and exec1[-1].phase == "failed"
    assert exec1[-1].error.code == "deadline_exceeded" and exec1[-1].error.details.get("stalled") is True


def test_service_side_deadline_is_a_typed_error() -> None:
    s = build_loop_services("slow_vla")

    async def go() -> VlaStatus:
        plan = await s.planner.plan(PlanRequest(robot_id=ROBOT, text="salsa", store_id=STORE))
        grasp = next(st for st in plan.steps if isinstance(st, GraspStep))
        ctx = {"plan_id": plan.plan_id, "step_id": grasp.step_id, "step_kind": "grasp", "attempt": 1}
        req = VlaRequest(exec_id="d:g:a1", robot_id=ROBOT, step=grasp, deadline_s=0.2, heartbeat_s=0.05, context=ctx)  # type: ignore[arg-type]
        return await run_vla_to_terminal(s.vla, req, stall_factor=100.0)  # stall detection off: the service decides

    terminal = run(go())
    assert terminal.phase == "failed" and terminal.error is not None
    assert terminal.error.code == "deadline_exceeded" and not terminal.error.details.get("stalled")


def test_navigate_step_kind_is_a_vla_step() -> None:
    s = build_loop_services()

    async def go() -> Any:
        return await s.planner.plan(PlanRequest(robot_id=ROBOT, text="salsa", store_id=STORE))

    plan = run(go())
    assert isinstance(plan.steps[0], NavigateToStep) and plan.steps[0].postconditions is not None
