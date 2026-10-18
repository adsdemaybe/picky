"""Showcase scenarios: wrong item, T1 breaker, over-threshold decline, replan, stale frames, optional asks."""

from typing import Any

from arms.agent.tests.loop_world import build_loop_services
from arms.agent.tests.loop_world.helpers import TACO_TEXT, run
from contracts.faults import FaultMatch, FaultRule, FaultScenario
from contracts.plan import PlaceStep, PlanRequest
from contracts.policy import DEFAULT_RETRY_POLICY, FailureMatch, RecoveryRule
from contracts.trust import ApprovalRequest, ApprovalResponse

ROBOT, STORE = "rover-ave-1", "store_ave"


def _go(s: Any, text: str = TACO_TEXT) -> Any:
    async def go() -> Any:
        plan = await s.planner.plan(PlanRequest(robot_id=ROBOT, text=text, store_id=STORE))
        return plan, await s.runner.run_plan(plan)

    return run(go())


def test_wrong_salsa_is_put_back_and_the_right_one_taken() -> None:
    s = build_loop_services("wrong_salsa")
    plan, out = _go(s)
    assert out.status == "completed"
    f = s.events.of("step.failed")[0]
    assert f.failure.code == "wrong_item_grasped" and f.failure.caught_by_verifier
    assert f.decision.action == "return_item" and "holding SALSA-HOT" in f.decision.reason
    back = [
        e.step for e in s.events.of("step.started") if isinstance(e.step, PlaceStep) and e.step.destination == "shelf"
    ]
    assert len(back) == 1 and back[0].origin == "recovery"
    assert back[0].title == "Put the wrong item back"  # words, never the raw SKU ('Put the SALSA-HOT back')
    skus = [sku for _, sku, _ in s.catalog.picks]
    assert skus.count("SALSA-MILD") == 1 and "SALSA-HOT" not in skus
    done = s.events.of("plan.completed")[-1]
    n = done.stats.verifier_catches
    assert n >= 1 and f" · {n} verifier {'catch' if n == 1 else 'catches'} · " in done.summary
    assert "catch(es)" not in done.summary


def test_target_description_says_the_size_once() -> None:
    """Live Isaac trip: 'Casa Verde Black Beans 15 oz, 15 oz' (the product name already carries the size)."""
    from types import SimpleNamespace

    from arms.agent.planner_fake import target_description

    beans = SimpleNamespace(brand="Casa Verde", name="Black Beans 15 oz", size="15 oz")
    assert target_description(beans, "Black Beans") == "Casa Verde Black Beans 15 oz"
    salsa = SimpleNamespace(brand="Sol de Casa", name="Mild Chunky Salsa", size="16 oz")
    assert target_description(salsa, "salsa") == "Sol de Casa Mild Chunky Salsa, 16 oz"
    assert target_description(None, "limes") == "limes"


def test_flaky_t1_opens_the_breaker_and_finishes_on_t0() -> None:
    s = build_loop_services("flaky_t1", simulate_t1=True)
    plan, out = _go(s)
    assert out.status == "completed" and out.metrics.tier_fallbacks >= 1
    types = s.events.types()
    i_change = types.index("vla.tier_changed")
    assert types[i_change - 1] == "step.failed"  # the banner follows the failure that caused it
    change = s.events.events[i_change]
    assert (change.from_tier, change.to_tier) == ("t1_pretrained", "t0_scripted") and change.breaker.open
    accepted = [e.status for e in s.events.of("vla.status") if e.status.phase == "accepted"]
    later = accepted[[a.exec_id for a in accepted].index(next(a.exec_id for a in accepted if ":a3" in a.exec_id)) :]
    assert all(a.tier == "t0_scripted" for a in later)


def test_over_threshold_decline_skips_the_item_and_never_moves_for_it() -> None:
    async def decline_threshold(req: ApprovalRequest) -> ApprovalResponse:
        if req.kind.value == "over_threshold":
            return ApprovalResponse(approval_id=req.approval_id, decision="reject", channel="system")
        if req.options:
            return ApprovalResponse(approval_id=req.approval_id, decision="choice", choice=req.options[0].option_id)
        return ApprovalResponse(approval_id=req.approval_id, decision="approve")

    s = build_loop_services()
    s.runner.answer = decline_threshold
    plan, out = _go(s, "tacos for 6, under $10, one vegetarian")
    skipped = [i for i in out.plan.items if i.status == "skipped"]
    assert skipped and out.status == "completed"
    picked_skus = {sku for _, sku, _ in s.catalog.picks}
    assert not picked_skus & {i.sku for i in skipped}
    assert out.total_cents <= 1000


def test_replan_keeps_progress_and_never_over_picks() -> None:
    policy = DEFAULT_RETRY_POLICY.model_copy(deep=True)
    policy.rules.insert(
        0,
        RecoveryRule(
            rule_id="replan_grasps", match=FailureMatch(step_kinds=["grasp"], codes=["grasp_missed"]), ladder=["replan"]
        ),
    )
    scenario = FaultScenario(
        name="miss_2nd_tortilla",
        description="",
        rules=[
            FaultRule(
                fault_id="m2",
                effect="grasp_miss",
                match=FaultMatch(sku="TORT-CORN", step_kind="grasp", occurrence=2, attempt=1),
            )
        ],
    )
    s = build_loop_services(scenario, policy=policy)
    plan, out = _go(s)
    assert out.status == "completed", out.summary
    rev = s.events.of("plan.revised")
    assert len(rev) == 1 and rev[0].plan.revision == 2 and rev[0].plan.root_plan_id == plan.plan_id
    assert out.plan.revision == 2
    skus = [sku for _, sku, _ in s.catalog.picks]
    assert skus.count("TORT-CORN") == 2 and len(skus) == 15  # unit 1 was kept, only unit 2 re-planned
    assert len({r.effect_key for r in out.run.effects}) == len(out.run.effects)


def test_stale_frame_is_rejected_by_not_before() -> None:
    scenario = FaultScenario(
        name="stale",
        description="",
        rules=[
            # 1st wrist capture of the grasp = its pre-check (nothing older to re-serve); 2nd = the VLA's capture_after,
            # which re-serves the pre-check frame (empty gripper): not_before must reject it, else a false failure
            FaultRule(
                fault_id="st",
                effect="stale_frame",
                times=2,
                match=FaultMatch(camera="wrist", step_kind="grasp", occurrence=1),
            )
        ],
    )
    s = build_loop_services(scenario)
    plan, out = _go(s, "salsa")
    assert out.status == "completed" and not s.events.of("step.failed")
    claimed = next(
        e.status for e in s.events.of("vla.status") if e.status.phase == "succeeded" and ":s05:" in e.status.exec_id
    )
    stale_ids = {f.frame_id for f in claimed.outcome.frames if f.camera_id.endswith(".wrist")}
    post = next(e.verdict for e in s.events.of("step.verified") if e.step_id == "s05" and e.phase == "post")
    used = {f.frame_id for f in post.frames if f.camera_id.endswith(".wrist")}
    assert stale_ids and used and not (stale_ids & used)


def test_run_goal_from_a_fleet_goal_asks_before_the_optional_item() -> None:
    from contracts.fleet import Goal, GoalItem

    async def no_recs(req: ApprovalRequest) -> ApprovalResponse:
        decision = "reject" if req.kind.value == "recommendation" else "approve"
        return ApprovalResponse(approval_id=req.approval_id, decision=decision, channel="system")  # type: ignore[arg-type]

    s = build_loop_services()
    s.runner.answer = no_recs
    goal = Goal(
        goal_id="g-1",
        kind="shop",
        store_id=STORE,
        robot_id=ROBOT,
        title="salsa + rec",
        user_id="maya",
        items=[
            GoalItem(item_id="stop_1", query="salsa", sku="SALSA-MILD", slot_id="MINI-F1-1-02", stop_id="stop_1"),
            GoalItem(item_id="stop_2", query="hot salsa", sku="SALSA-HOT", optional=True, rec_id="rec_9"),
        ],
    )
    out = run(s.runner.run_goal(ROBOT, goal))
    assert out.status == "completed" and out.plan.goal_id == "g-1"
    assert out.item_status == {"stop_1": "in_basket", "stop_2": "declined"}
    asks = [e.step for e in s.events.of("step.started") if e.step.kind == "ask_user"]
    assert [a.ask_kind for a in asks] == ["recommendation"] and asks[0].item_id == "stop_2"
    assert [sku for _, sku, _ in s.catalog.picks] == ["SALSA-MILD"]
    nav = next(e.step for e in s.events.of("step.started") if e.step.kind == "navigate_to")
    assert nav.target == "stop" and nav.stop_id == "stop_1"  # shop goals navigate to trip route stops
