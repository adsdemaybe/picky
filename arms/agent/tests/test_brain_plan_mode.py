"""The brain's plan-mode hook (contracts 0.2.1): PICKY_EXEC_MODE / auto default, delegation to the loop runner, the
terminal fleet update, escalation, crash fallback to the v0.2 executor, cancellation, messages while a plan runs."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from arms.agent import create_brain
from arms.agent.brain import ScriptedBrain
from arms.agent.loop import configured_exec_mode, resolve_exec_mode
from arms.agent.tests.brain_stubs import StubFleet
from arms.agent.tests.loop_world import build_loop_services
from arms.agent.tests.loop_world.helpers import ROBOT, STORE, run
from contracts.bridge import MIRROR_MODEL
from contracts.fleet import Goal, GoalItem, GoalStatus
from contracts.services import Services
from contracts.sim import RobotError
from contracts.trust import ApprovalRequest, ApprovalResponse
from utils.config import reset_settings, set_settings

MILD_SLOT = "MINI-F1-1-02"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("PICKY_EXEC_MODE", raising=False)
    set_settings(anthropic_api_key="", gemini_api_key="")
    yield
    reset_settings()


def _set_mode(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("PICKY_EXEC_MODE", value)
    set_settings(anthropic_api_key="", gemini_api_key="")  # a fresh Settings re-reads the env


def _goal(*items: GoalItem, goal_id: str = "g-1", kind: str = "shop", **kw: Any) -> Goal:
    return Goal(
        goal_id=goal_id,
        kind=kind,  # type: ignore[arg-type]
        store_id=STORE,
        robot_id=ROBOT,
        title="salsa run",
        user_id="maya",
        status=GoalStatus.ACTIVE,
        items=list(items),
        **kw,
    )


def _salsa(**kw: Any) -> GoalItem:
    return GoalItem(item_id="i1", query="salsa", sku="SALSA-MILD", slot_id=MILD_SLOT, **kw)


def _world(scenario: Any = None, **kw: Any) -> tuple[Services, StubFleet, ScriptedBrain]:
    s = build_loop_services(scenario, **kw)
    fleet = StubFleet()
    s.fleet = fleet  # type: ignore[assignment]
    brain = ScriptedBrain(s)
    s.brain = brain
    return s, fleet, brain


class _ExplodingRunner:
    def __init__(self, exc: BaseException) -> None:
        self.exc = exc
        self.calls = 0
        self.on_brain_step = None

    async def run_goal(self, robot_id: str, goal: Goal) -> Any:
        self.calls += 1
        raise self.exc


# ---- the switch ---------------------------------------------------------------------------------------------------
def test_exec_mode_auto_default_and_the_kill_switch(monkeypatch: pytest.MonkeyPatch) -> None:
    assert configured_exec_mode(Services()) == "direct" and resolve_exec_mode(Services()) == "direct"
    s = build_loop_services()
    assert configured_exec_mode(s) == "plan" and resolve_exec_mode(s) == "plan"  # unset + VLA + verifier wired
    _set_mode(monkeypatch, "direct")
    assert resolve_exec_mode(s) == "direct"
    _set_mode(monkeypatch, "tools")
    assert resolve_exec_mode(s) == "tools"
    _set_mode(monkeypatch, "plan")
    s.verifier = None
    assert resolve_exec_mode(s) == "direct"  # contracts.exec_mode: plan needs planner + runner + vla + verifier


# ---- plan mode through the brain ------------------------------------------------------------------------------------
def test_plan_mode_runs_the_goal_and_the_brain_finishes_it() -> None:
    s, fleet, brain = _world()
    goal = fleet.add(_goal(_salsa(stop_id="stop_1"), GoalItem(item_id="i2", query="hot salsa", sku="SALSA-HOT")))
    final = run(brain.run_goal(ROBOT, goal))

    assert final.status == GoalStatus.COMPLETED and final.progress == 1.0
    assert {i.item_id: i.status for i in final.items} == {"i1": "in_basket", "i2": "in_basket"}
    assert sorted(sku for _, sku, _ in s.catalog.picks) == ["SALSA-HOT", "SALSA-MILD"]
    terminal = [u for u in fleet.updates if u.status is not None]
    assert len(terminal) == 1 and terminal[0] is fleet.updates[-1] and terminal[0].summary == final.summary
    assert any(u.items for u in fleet.updates[:-1])  # the runner patched items as they were verified

    types = s.events.types()
    assert types.index("plan.created") < types.index("plan.completed")
    steps = [e.step for e in s.events.of("brain.step")]
    assert steps[0].kind == "thought" and "verified steps" in steps[0].text
    mirrored = [st for st in steps if st.model == MIRROR_MODEL and st.kind == "status"]
    assert mirrored and all(st.goal_id == "g-1" for st in mirrored)
    assert steps[-1].kind == "message" and steps[-1].text == final.summary

    recent = run(brain.recent_steps(ROBOT, limit=1000))
    assert {st.step_id for st in mirrored} <= {st.step_id for st in recent}  # mirrored steps are in the ring
    status = run(brain.status(ROBOT))
    assert status.state == "idle" and status.goal_id is None


def test_drop_on_first_grasp_is_caught_through_the_brain() -> None:
    s, fleet, brain = _world("drop_on_first_grasp")
    goal = fleet.add(_goal(_salsa()))
    final = run(brain.run_goal(ROBOT, goal))
    assert final.status == GoalStatus.COMPLETED
    failed = s.events.of("step.failed")
    assert len(failed) == 1 and failed[0].failure.caught_by_verifier and failed[0].decision.action == "reground"
    assert failed[0].decision.reason.startswith("Verifier caught it")
    assert s.events.of("plan.completed")[-1].stats.verifier_catches == 1
    assert [sku for _, sku, _ in s.catalog.picks] == ["SALSA-MILD"]  # stock decremented once


def test_the_llm_brain_also_delegates_in_plan_mode() -> None:
    s = build_loop_services()
    brain = create_brain(s)
    final = run(brain.run_goal(ROBOT, _goal(_salsa())))  # no fleet wired: the brain applies the update locally
    assert final.status == GoalStatus.COMPLETED and final.items[0].status == "in_basket"
    assert s.events.of("plan.completed")


# ---- what stays on the v0.2 path ------------------------------------------------------------------------------------
def test_direct_kill_switch_never_touches_the_runner(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_mode(monkeypatch, "direct")
    s, fleet, brain = _world()
    runner = _ExplodingRunner(AssertionError("runner must not run in direct mode"))
    s.runner = runner  # type: ignore[assignment]
    goal = fleet.add(_goal(_salsa()))
    final = run(brain.run_goal(ROBOT, goal))
    assert runner.calls == 0 and final.terminal
    assert not s.events.of("plan.created", "step.started")


@pytest.mark.parametrize("mode", ["auto", "plan", "tools"])
def test_shelf_scan_and_dock_goals_stay_on_the_v02_executor(monkeypatch: pytest.MonkeyPatch, mode: str) -> None:
    _set_mode(monkeypatch, mode)
    s, fleet, brain = _world()
    runner = _ExplodingRunner(AssertionError("not a plan-mode goal"))
    s.runner = runner  # type: ignore[assignment]
    scan = fleet.add(_goal(kind="shelf_scan", goal_id="g-scan", slot_ids=[MILD_SLOT]))
    dock = fleet.add(_goal(kind="return_to_dock", goal_id="g-dock", end_at="dock"))
    run(brain.run_goal(ROBOT, scan))
    run(brain.run_goal(ROBOT, dock))
    assert runner.calls == 0


# ---- failures -------------------------------------------------------------------------------------------------------
def test_robot_error_from_the_runner_propagates_without_finishing_the_goal() -> None:
    s, fleet, brain = _world()
    s.runner = _ExplodingRunner(RobotError("battery_low", "battery 4%"))  # type: ignore[assignment]
    goal = fleet.add(_goal(_salsa()))
    with pytest.raises(RobotError):
        run(brain.run_goal(ROBOT, goal))
    assert all(u.status is None for u in fleet.updates)  # the fleet reassigns; the brain never failed the goal
    assert run(brain.status(ROBOT)).state == "error"
    assert any(e.step.kind == "error" and "battery_low" in e.step.text for e in s.events.of("brain.step"))


def test_a_foreign_unit_in_the_gripper_is_set_aside_and_the_goal_finishes() -> None:
    """T0 has no place-back primitive: the foreign unit goes in the basket for staff (never charged) instead of a
    put-back the robot cannot run (which used to ask 'Try again?', then hand the goal to another robot)."""
    s, fleet, brain = _world()

    async def go() -> Any:
        plano = await s.catalog.get_planogram(STORE)
        slot = next(x for x in plano.slots if x.sku == "SALSA-HOT")
        await s.robot.navigate(ROBOT, [slot.approach_pose])
        await s.robot.pick(ROBOT, slot.slot_id, "SALSA-HOT")
        return await brain.run_goal(ROBOT, fleet.add(_goal(_salsa())))

    final = run(go())
    assert final.status == GoalStatus.COMPLETED
    assert {i.item_id: (i.status, i.picked_qty) for i in final.items} == {"i1": ("in_basket", 1)}
    assert sorted(sku for _, sku, _ in s.catalog.picks) == ["SALSA-HOT", "SALSA-MILD"]  # it left its shelf
    assert not [e for e in s.events.of("brain.step") if "Try again" in e.step.text or e.step.kind == "error"]


def test_a_runner_crash_falls_back_to_the_v02_executor() -> None:
    s, fleet, brain = _world()
    runner = _ExplodingRunner(RuntimeError("planner bug"))
    s.runner = runner  # type: ignore[assignment]
    goal = fleet.add(_goal(_salsa()))
    final = run(brain.run_goal(ROBOT, goal))
    assert runner.calls == 1 and final.terminal  # the v0.2 executor finished it
    errors = [e for e in s.events.of("error") if e.code == "internal"]
    assert errors and "v0.2 executor takes over" in errors[0].message
    assert any(e.step.kind == "error" and "Plan mode failed" in e.step.text for e in s.events.of("brain.step"))


# ---- cancellation + talking to a robot mid-plan ---------------------------------------------------------------------
class _BlockingBroker:
    def __init__(self) -> None:
        self.asked: list[str] = []
        self.cancelled: list[str] = []
        self.release = asyncio.Event()

    async def request(self, req: ApprovalRequest) -> ApprovalResponse:
        self.asked.append(req.approval_id)
        await self.release.wait()
        return ApprovalResponse(approval_id=req.approval_id, decision="approve", channel="system")

    def pending(self, trip_id: Any = None, **kw: Any) -> list[Any]:
        return []

    async def cancel(self, approval_id: str, reason: str = "") -> bool:
        self.cancelled.append(approval_id)
        return True


def _optional_goal() -> Goal:
    return _goal(GoalItem(item_id="i1", query="hot salsa", sku="SALSA-HOT", optional=True))


def test_cancelling_the_brain_task_in_plan_mode_withdraws_the_question() -> None:
    s, fleet, brain = _world()
    broker = _BlockingBroker()
    s.approvals = broker  # type: ignore[assignment]

    async def go() -> None:
        task = asyncio.create_task(brain.run_goal(ROBOT, fleet.add(_optional_goal())))
        for _ in range(400):
            await asyncio.sleep(0.005)
            if broker.asked:
                break
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    run(go())
    assert broker.asked and broker.cancelled == broker.asked
    assert all(u.status is None for u in fleet.updates)  # the fleet's cancel_goal owns CANCELLED
    assert run(brain.status(ROBOT)).state == "idle"


def test_messages_while_a_plan_runs() -> None:
    s, fleet, brain = _world()
    broker = _BlockingBroker()
    s.approvals = broker  # type: ignore[assignment]

    async def go() -> tuple[str, str, Goal]:
        task = asyncio.create_task(brain.run_goal(ROBOT, fleet.add(_optional_goal())))
        for _ in range(400):
            await asyncio.sleep(0.005)
            if broker.asked:
                break
        status = await brain.handle_message(ROBOT, "status?")
        skip = await brain.handle_message(ROBOT, "skip the salsa")
        broker.release.set()
        return status.text, skip.text, await task

    status, skip, final = run(go())
    assert "verified plan" in status and "step " in status and status.startswith("Working on 'salsa run'")
    assert "can't change the list" in skip
    assert final.status == GoalStatus.COMPLETED
