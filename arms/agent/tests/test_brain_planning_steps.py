"""Plan mode keeps the robot view talking: who is planning (before the LLM call) and 'Plan ready' when it lands."""

from __future__ import annotations

from typing import Any

from arms.agent.brain import _llm_planner_label
from arms.agent.loop import plan_ready_text
from arms.agent.planner import LlmPlanner
from arms.agent.tests.loop_world.helpers import run
from arms.agent.tests.test_brain_plan_mode import _clean_env, _goal, _salsa, _world  # noqa: F401 - fixture
from contracts.fleet import GoalItem
from contracts.plan import PlannerInfo, StepPlan


def test_planning_step_comes_first_and_plan_ready_follows_before_any_step() -> None:
    _s, fleet, brain = _world()
    goal = fleet.add(_goal(_salsa(stop_id="stop_1"), GoalItem(item_id="i2", query="hot salsa", sku="SALSA-HOT")))
    run(brain.run_goal("rover-ave-1", goal))
    steps = [e.step for e in _s.events.of("brain.step")]
    assert steps[0].kind == "thought" and steps[0].text.startswith("Planning 2 stops for 'salsa run'")
    assert steps[1].kind == "thought" and steps[1].text.startswith("Plan ready: ")
    assert "by the scripted planner" in steps[1].text and steps[1].goal_id == "g-1"
    assert steps[2].kind == "status"  # the first mirrored plan step comes after
    ring = run(brain.recent_steps("rover-ave-1", limit=1000))
    assert steps[1].step_id in {st.step_id for st in ring}


def _plan(info: PlannerInfo, n: int) -> Any:
    return StepPlan.model_construct(planner=info, steps=[object()] * n)


def test_plan_ready_text_names_the_model_and_the_time() -> None:
    llm = PlannerInfo(origin="llm", mode="llm", model="gemini-3.5-flash", latency_ms=16448.8)
    assert plan_ready_text(_plan(llm, 73)) == "Plan ready: 73 steps in 16.4 s by gemini-3.5-flash."
    fb = PlannerInfo(origin="scripted", latency_ms=20012.0, fallback_reason="LLM timeout after 20 s")
    assert plan_ready_text(_plan(fb, 9)) == (
        "Plan ready: 9 steps in 20.0 s by the scripted planner (LLM timeout after 20 s)."
    )
    assert plan_ready_text(_plan(PlannerInfo(), 5)) == "Plan ready: 5 steps by the scripted planner."


async def _never(*_a: Any, **_k: Any) -> Any:
    raise AssertionError("not called")


def test_llm_planner_label() -> None:
    s, _fleet, _brain = _world()
    assert _llm_planner_label(None) is None
    assert _llm_planner_label(LlmPlanner(s, complete_json=_never, model="gemini-3.5-flash")) == "Gemini"
    assert _llm_planner_label(LlmPlanner(s, complete_json=_never, model="claude-sonnet-5-5")) == "Claude"
    assert _llm_planner_label(LlmPlanner(s)) is None  # no key: the scripted draft runs
