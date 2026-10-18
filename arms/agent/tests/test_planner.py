"""Planner (agent arm): the scripted planner, the LLM planner seam (mocked structured output only, never the network),
fallbacks (no key / timeout / invalid draft / does not compile) and the runner's planner guards."""

from __future__ import annotations

import asyncio
from collections import Counter
from typing import Any

import pytest

from arms.agent import create_planner, create_planner_fake, create_runner, create_runner_fake
from arms.agent.loop import PlanLoop
from arms.agent.planner import PLANNER_SYSTEM, LlmPlanner
from arms.agent.planner_fake import ScriptedPlanner, interpret_text
from arms.agent.tests.loop_world import build_loop_services
from arms.agent.tests.loop_world.helpers import ROBOT, STORE, TACO_TEXT, run
from contracts.faults import FaultMatch, FaultRule, FaultScenario
from contracts.fleet import Goal, GoalItem
from contracts.plan import DraftStep, GraspStep, PlanDraft, PlanRequest, StepPlan
from contracts.policy import DEFAULT_RETRY_POLICY, FailureMatch, RecoveryRule
from contracts.services import LoopRunner, PlannerService, Services
from contracts.verify import VerifyRequest, verify_deadline
from utils import llm
from utils.config import reset_settings, set_settings


@pytest.fixture(autouse=True)
def _no_llm_keys():
    set_settings(anthropic_api_key="", gemini_api_key="")
    yield
    reset_settings()


class ScriptedLlm:
    """Stands in for ``utils.llm.complete_json``: returns canned drafts / raises, records every call."""

    def __init__(self, *results: Any, delay_s: float = 0.0) -> None:
        self.results = list(results)
        self.delay_s = delay_s
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, prompt: Any, schema: type, **kw: Any) -> Any:
        self.calls.append({"prompt": prompt, "schema": schema, **kw})
        if self.delay_s:
            await asyncio.sleep(self.delay_s)
        res = self.results.pop(0) if len(self.results) > 1 else self.results[0]
        if isinstance(res, BaseException):
            raise res
        if callable(res):
            return res(prompt)
        return res


def _llm_services(fake_llm: ScriptedLlm, scenario: Any = None, **kw: Any) -> Services:
    s = build_loop_services(scenario, **kw)
    s.planner = LlmPlanner(s, complete_json=fake_llm, model="gemini-3.8-flash")
    return s


def _open_ids(prompt: str) -> list[str]:
    import json

    body = json.loads(prompt.split("\n", 1)[1])
    return [i["item_id"] for i in body["items"] if i.get("slot_id") and i["status"] not in ("online", "unavailable")]


# ---- factories ----------------------------------------------------------------------------------------------------
def test_loop_factories_satisfy_the_protocols_without_calling_services() -> None:
    empty = Services()
    assert isinstance(create_planner(empty), PlannerService) and isinstance(create_planner(empty), LlmPlanner)
    assert isinstance(create_planner_fake(empty), PlannerService)
    assert type(create_planner_fake(empty)) is ScriptedPlanner
    assert isinstance(create_runner(empty), LoopRunner) and isinstance(create_runner_fake(empty), PlanLoop)


def test_runner_factory_applies_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PICKY_VERIFY_TIMEOUT_S", "3")
    monkeypatch.setenv("PICKY_PLAN_TIMEOUT_S", "7")
    reset_settings()  # Settings re-reads the env (or the runner falls back to it before the utils fields land)
    r = create_runner(Services())
    assert r.policy.degrade.verify_timeout_s == 3.0 and r.plan_timeout_s == 7.0
    monkeypatch.delenv("PICKY_VERIFY_TIMEOUT_S")
    monkeypatch.delenv("PICKY_PLAN_TIMEOUT_S")
    reset_settings()
    r = create_runner(Services())
    assert r.policy.degrade.verify_timeout_s == DEFAULT_RETRY_POLICY.degrade.verify_timeout_s


# ---- scripted planner -----------------------------------------------------------------------------------------------
def test_interpret_text_taco_list_and_generic_fallback() -> None:
    items, budget, summary = interpret_text(TACO_TEXT)
    assert budget == 4000 and summary == "Tacos for 6, 1 vegetarian, under $40"
    assert [(q, n) for q, n, *_ in items][:3] == [("tortillas", 2), ("ground beef", 2), ("black beans", 1)]
    generic, budget, _ = interpret_text("2 salsa, limes and sour cream under $15")
    assert [(q, n) for q, n, *_ in generic] == [("salsa", 2), ("limes", 1), ("sour cream", 1)] and budget == 1500


def test_scripted_goal_plan_follows_the_route_and_asks_before_the_optional_item() -> None:
    s = build_loop_services()
    goal = Goal(
        goal_id="g-1",
        kind="shop",
        store_id=STORE,
        robot_id=ROBOT,
        title="salsa + rec",
        items=[
            GoalItem(item_id="stop_2", query="hot salsa", sku="SALSA-HOT", optional=True, stop_id="stop_2"),
            GoalItem(item_id="stop_1", query="salsa", sku="SALSA-MILD", stop_id="stop_1", qty=2),
        ],
    )
    plan = run(s.planner.plan(PlanRequest(robot_id=ROBOT, goal=goal)))
    kinds = [st.kind for st in plan.steps]
    assert kinds[0] == "ask_user" and plan.steps[0].item_id == "stop_2"  # ask BEFORE driving for the rec
    assert [st.item_id for st in plan.steps if isinstance(st, GraspStep)] == ["stop_2", "stop_1", "stop_1"]
    assert kinds[-3:] == ["verify", "navigate_to", "handoff"] and plan.planner.origin == "scripted"
    assert plan.plan_id == "plan-g-1" and all(i.severity != "error" for i in s.planner.validate(plan))


def test_validate_flags_slots_that_are_not_in_the_store() -> None:
    s = build_loop_services()
    plan = run(s.planner.plan(PlanRequest(robot_id=ROBOT, text="salsa", store_id=STORE)))
    nav = plan.steps[0].model_copy(update={"slot_id": "NOPE-1"})
    bad = plan.model_copy(update={"steps": [nav, *plan.steps[1:]]})
    assert any(i.code == "unknown_slot" and i.severity == "error" for i in s.planner.validate(bad))


# ---- LLM planner: the seam ------------------------------------------------------------------------------------------
def test_llm_draft_is_used_and_labelled() -> None:
    def draft(prompt: str) -> PlanDraft:
        ids = _open_ids(prompt)
        return PlanDraft(
            summary="Taco night for 6",
            steps=[*(DraftStep(kind="fetch_item", item_id=i) for i in reversed(ids)), DraftStep(kind="checkout")],
            rationale="produce last so it stays fresh",
        )

    fake = ScriptedLlm(draft)
    s = _llm_services(fake)

    async def go() -> tuple[StepPlan, Any]:
        plan = await s.planner.plan(PlanRequest(robot_id=ROBOT, text=TACO_TEXT, store_id=STORE))
        return plan, await s.runner.run_plan(plan)

    plan, out = run(go())
    assert plan.planner.origin == "llm" and plan.planner.model == "gemini-3.8-flash"
    assert plan.planner.rationale == "produce last so it stays fresh" and plan.title == "Taco night for 6"
    assert out.status == "completed" and len(s.catalog.picks) == 15
    call = fake.calls[0]
    assert call["schema"] is PlanDraft and call["system"] == PLANNER_SYSTEM and call["kind"] == "brain"
    assert call["effort"] == "low" and call["retries"] == 1
    assert '"item_id": "item_01"' in call["prompt"] and '"budget_cents": 4000' in call["prompt"]


def test_messy_llm_draft_still_picks_every_unit_exactly_once() -> None:
    def messy(prompt: str) -> PlanDraft:
        ids = _open_ids(prompt)  # repeats the first item, checks out early, forgets the last
        steps = [DraftStep(kind="fetch_item", item_id=ids[0]), DraftStep(kind="checkout")]
        steps += [DraftStep(kind="fetch_item", item_id=i) for i in ids[:-1]]
        steps += [DraftStep(kind="fetch_item", item_id="item_99")]  # unknown
        return PlanDraft(summary="LLM tacos", steps=steps, rationale="produce last")

    s = _llm_services(ScriptedLlm(messy))

    async def go() -> tuple[StepPlan, Any]:
        plan = await s.planner.plan(PlanRequest(robot_id=ROBOT, text=TACO_TEXT, store_id=STORE))
        return plan, await s.runner.run_plan(plan)

    plan, out = run(go())
    assert plan.planner.origin == "llm" and plan.planner.fixes
    assert out.status == "completed", out.summary
    want = Counter({i.sku: i.qty for i in plan.items if i.status != "online" and i.slot_id})
    assert Counter(sku for _, sku, _ in s.catalog.picks) == want  # no double pick, nothing forgotten
    assert len([st for st in plan.steps if isinstance(st, GraspStep)]) == sum(want.values())


@pytest.mark.parametrize(
    ("result", "needle"),
    [
        (llm.LLMOutputError("invalid PlanDraft after 2 attempts"), "invalid draft"),
        (RuntimeError("503 overloaded"), "RuntimeError: 503 overloaded"),
        (llm.LLMNotConfigured("GEMINI_API_KEY is not set"), "no GEMINI_API_KEY"),
        (TypeError("complete_json() got an unexpected keyword argument"), "TypeError"),
    ],
)
def test_llm_failures_fall_back_to_the_scripted_draft(result: BaseException, needle: str) -> None:
    fake = ScriptedLlm(result)
    s = _llm_services(fake)
    plan = run(s.planner.plan(PlanRequest(robot_id=ROBOT, text=TACO_TEXT, store_id=STORE, user_id="maya")))
    assert plan.planner.origin == "scripted" and needle in (plan.planner.fallback_reason or "")
    assert plan.planner.model == "gemini-3.8-flash" and len(fake.calls) == 1
    err = [e for e in s.events.events if e.type == "error"]
    assert len(err) == 1 and err[0].code == "llm_fallback" and err[0].recoverable and err[0].user_id == "maya"


def test_llm_timeout_falls_back_within_the_budget() -> None:
    fake = ScriptedLlm(PlanDraft(summary="late", steps=[DraftStep(kind="checkout")]), delay_s=5.0)
    s = build_loop_services()
    s.planner = LlmPlanner(s, complete_json=fake, timeout_s=0.05)
    plan = run(s.planner.plan(PlanRequest(robot_id=ROBOT, text="salsa", store_id=STORE)))
    assert plan.planner.origin == "scripted" and plan.planner.fallback_reason == "LLM timeout after 0.05 s"
    assert plan.planner.latency_ms < 2000


def test_no_key_never_calls_the_llm() -> None:
    s = build_loop_services()
    s.planner = LlmPlanner(s)  # default utils.llm path; no key configured in tests
    plan = run(s.planner.plan(PlanRequest(robot_id=ROBOT, text="salsa", store_id=STORE)))
    assert plan.planner.origin == "scripted" and (plan.planner.fallback_reason or "").startswith("no ")
    assert plan.planner.fallback_reason in ("no GEMINI_API_KEY", "no ANTHROPIC_API_KEY")


def test_default_path_goes_through_utils_llm_complete_json(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[Any] = []

    async def fake_complete_json(prompt: Any, schema: type, **kw: Any) -> PlanDraft:
        seen.append(kw)
        return PlanDraft(summary="Salsa", steps=[DraftStep(kind="fetch_item", item_id="item_01")], rationale="one")

    monkeypatch.setattr(llm, "complete_json", fake_complete_json)
    monkeypatch.setattr(llm, "is_configured", lambda: True)
    s = build_loop_services()
    s.planner = LlmPlanner(s)
    plan = run(s.planner.plan(PlanRequest(robot_id=ROBOT, text="salsa", store_id=STORE)))
    assert plan.planner.origin == "llm" and seen and seen[0]["kind"] == "brain"
    assert [st.kind for st in plan.steps][-1] == "handoff"  # normalize_draft added the checkout


def test_nothing_to_pick_skips_the_llm() -> None:
    fake = ScriptedLlm(RuntimeError("should not be called"))
    s = _llm_services(fake)
    goal = Goal(
        goal_id="g-2",
        kind="shop",
        store_id=STORE,
        robot_id=ROBOT,
        title="all online",
        items=[GoalItem(item_id="i1", query="kombucha", sku="SALSA-HOT", status="online")],
    )
    plan = run(s.planner.plan(PlanRequest(robot_id=ROBOT, goal=goal)))
    assert not fake.calls and plan.planner.origin == "scripted" and plan.planner.fallback_reason is None


class _UncompilableLlm(LlmPlanner):
    async def compile(self, draft: PlanDraft, request: PlanRequest, **kw: Any) -> Any:
        if kw.get("info") is not None and kw["info"].origin == "llm":
            raise ValueError("invalid plan: synthetic")
        return await super().compile(draft, request, **kw)


def test_llm_draft_that_does_not_compile_falls_back_to_the_scripted_draft() -> None:
    s = build_loop_services()
    s.planner = _UncompilableLlm(
        s,
        complete_json=ScriptedLlm(PlanDraft(summary="x", steps=[DraftStep(kind="checkout")])),
        model="gemini-3.8-flash",
    )
    plan = run(s.planner.plan(PlanRequest(robot_id=ROBOT, text=TACO_TEXT, store_id=STORE)))
    assert plan.planner.origin == "scripted" and "did not compile" in (plan.planner.fallback_reason or "")
    assert plan.planner.model == "gemini-3.8-flash"


# ---- the runner's planner guards ------------------------------------------------------------------------------------
class _BrokenReplanPlanner(ScriptedPlanner):
    async def replan(self, request: Any) -> Any:
        raise RuntimeError("LLM re-plan exploded")


def test_replan_that_raises_ends_only_that_item() -> None:
    policy = DEFAULT_RETRY_POLICY.model_copy(deep=True)
    policy.rules.insert(
        0,
        RecoveryRule(rule_id="r", match=FailureMatch(step_kinds=["grasp"], codes=["grasp_missed"]), ladder=["replan"]),
    )
    scenario = FaultScenario(
        name="miss",
        description="",
        rules=[FaultRule(fault_id="m", effect="grasp_miss", match=FaultMatch(sku="TORT-CORN", step_kind="grasp"))],
    )
    s = build_loop_services(scenario, policy=policy, planner_cls=_BrokenReplanPlanner)

    async def go() -> Any:
        plan = await s.planner.plan(PlanRequest(robot_id=ROBOT, text=TACO_TEXT, store_id=STORE))
        return plan, await s.runner.run_plan(plan)

    plan, out = run(go())  # never raises
    tort = next(i.item_id for i in plan.items if i.sku == "TORT-CORN")
    assert out.item_status[tort] == "failed"
    picks = [sku for _, sku, _ in s.catalog.picks]
    assert "TORT-CORN" not in picks and len(picks) == 15 - 2  # every other unit still picked


def test_runner_gives_the_verifier_a_deadline_inside_its_own_timeout() -> None:
    s = build_loop_services()
    seen: list[VerifyRequest] = []
    real = s.verifier.verify

    async def spy(req: VerifyRequest) -> Any:
        seen.append(req)
        return await real(req)

    s.verifier.verify = spy  # type: ignore[method-assign]

    async def go() -> Any:
        plan = await s.planner.plan(PlanRequest(robot_id=ROBOT, text="salsa", store_id=STORE))
        return await s.runner.run_plan(plan)

    out = run(go())
    assert out.status == "completed"
    timeout = DEFAULT_RETRY_POLICY.degrade.verify_timeout_s
    assert seen and all(r.deadline_s == verify_deadline(timeout) < timeout for r in seen)


def test_llm_seam_signature_matches_the_scripted_planner() -> None:
    import inspect

    a = inspect.signature(ScriptedPlanner.draft)
    b = inspect.signature(LlmPlanner.draft)
    assert list(a.parameters) == list(b.parameters)
