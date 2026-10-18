"""End to end on fakes: 'tacos for 6, under $40, one vegetarian' -> plan -> VLA -> verifier -> recovery -> events."""

from typing import Any

from arms.agent.tests.loop_world import build_loop_services
from arms.agent.tests.loop_world.helpers import TACO_TEXT, run
from contracts.events import EVENT_ADAPTER
from contracts.plan import GraspStep, PlanRequest, StepPlan
from contracts.policy import PlanOutcome
from contracts.vla import VlaRequest

ROBOT, STORE = "rover-ave-1", "store_ave"


def _run_tacos(scenario: Any = None, **kw: Any) -> tuple[Any, StepPlan, PlanOutcome]:
    s = build_loop_services(scenario, **kw)

    async def go() -> tuple[StepPlan, PlanOutcome]:
        plan = await s.planner.plan(PlanRequest(robot_id=ROBOT, text=TACO_TEXT, store_id=STORE, user_id="maya"))
        return plan, await s.runner.run_plan(plan)

    plan, out = run(go())
    return s, plan, out


def _subsequence(seq: list[str], wanted: list[str]) -> bool:
    it = iter(seq)
    return all(any(x == w for x in it) for w in wanted)


def test_taco_plan_matches_the_v02_list() -> None:
    s, plan, out = _run_tacos()
    qty = {i.sku: i.qty for i in plan.items}
    assert qty == {
        "TORT-CORN": 2,
        "BEEF-GROUND": 2,
        "BEANS-BLACK": 1,
        "CHEESE-MEX": 1,
        "SALSA-MILD": 1,
        "AVOCADO": 3,
        "LIMES": 2,
        "ONION-WHITE": 1,
        "CILANTRO": 1,
        "SOUR-CREAM": 1,
    }
    assert plan.budget_cents == 4000 and plan.est_total_cents == 3427 <= plan.budget_cents
    assert out.status == "completed" and out.metrics.verifier_catches == 0 and out.metrics.retries == 0
    assert all(st == "in_basket" for st in out.item_status.values())


def test_drop_on_first_grasp_is_caught_by_the_verifier_and_retried() -> None:
    s, plan, out = _run_tacos("drop_on_first_grasp")
    bus = s.events
    first_grasp = next(st for st in plan.steps if isinstance(st, GraspStep))
    gid = first_grasp.step_id

    # outcome + effects: everything picked once, stock decremented once per unit, cart within budget
    assert out.status == "completed", out.summary
    assert out.metrics.verifier_catches == 1 and out.metrics.retries == 1
    assert s.robot.calls["pick"] == 15  # the faulted grasp never attached anything
    assert len(s.catalog.picks) == 15 and len(bus.of("item.picked")) == 15
    assert out.total_cents == out.run.local_cart_cents == 3427 <= 4000
    assert len({r.effect_key for r in out.run.effects}) == len(out.run.effects)  # ledger: each effect once

    # the demo moment, in order, for the first grasp
    mine = [
        e
        for e in bus.events
        if getattr(e, "step_id", None) == gid or getattr(getattr(e, "step", None), "step_id", None) == gid
    ]
    seq = [(e.type, getattr(e, "phase", None) or getattr(e, "attempt", None)) for e in mine if e.type != "vla.status"]
    assert seq == [
        ("step.started", 1),
        ("step.verified", "pre"),
        ("step.verified", "post"),
        ("step.failed", None),
        ("step.started", 2),
        ("step.verified", "pre"),
        ("step.verified", "post"),
        ("step.completed", None),
    ]
    post1 = next(e for e in mine if e.type == "step.verified" and e.phase == "post")
    assert post1.verdict.outcome == "fail" and post1.caught_vla_false_success
    assert "gripper is empty" in post1.verdict.reason and post1.verdict.decided_by == "ground_truth"
    assert post1.verdict.frames and post1.verdict.frames[0].camera_id == f"{ROBOT}.wrist"
    failed = next(e for e in mine if e.type == "step.failed")
    assert failed.failure.caught_by_verifier and failed.failure.vla_claimed_success
    assert failed.failure.code == "atom:gripper_holding"
    assert (failed.decision.rule_id, failed.decision.action) == ("grasp_failed", "reground")
    assert (
        failed.decision.reason.startswith("Verifier caught it: gripper is empty")
        and "(attempt 2/3)" in failed.decision.reason
    )
    vla1 = [e.status.phase for e in bus.of("vla.status") if e.status.exec_id == f"{plan.plan_id}:{gid}:a1"]
    assert vla1[0] == "accepted" and vla1[-1] == "succeeded"  # the VLA CLAIMED success

    # the inserted re-ground step runs between the failure and attempt 2
    types = bus.types()
    i_fail = next(i for i, e in enumerate(bus.events) if e.type == "step.failed")
    i_retry = next(
        i for i, e in enumerate(bus.events) if e.type == "step.started" and e.step.step_id == gid and e.attempt == 2
    )
    between = bus.events[i_fail:i_retry]
    assert [e.step.step_id for e in between if e.type == "step.started"] == [f"{gid}.r1.1"]
    assert any(e.type == "target.grounded" for e in between)
    assert any(e.type == "demo.fault" and e.firing.effect == "drop_after_lift" for e in bus.events[:i_fail])

    # whole-run envelope + ordering
    assert types[0] == "plan.created" and types[-1] == "plan.completed"
    assert bus.events[-1].status == "completed" and bus.events[-1].stats.verifier_catches == 1
    assert _subsequence(
        types,
        [
            "plan.created",
            "step.started",
            "vla.status",
            "step.verified",
            "step.completed",
            "step.failed",
            "target.grounded",
            "item.picked",
            "basket.verified",
            "plan.completed",
        ],
    )
    seqs = [e.seq for e in bus.events]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)
    for e in bus.events:  # every event survives the websocket JSON round trip
        assert EVENT_ADAPTER.validate_python(e.model_dump(mode="json")).type == e.type
    assert all(e.user_id == "maya" and e.robot_id == ROBOT for e in bus.of("step.failed", "plan.completed"))


def test_vla_execute_is_idempotent_and_rejects_body_conflicts() -> None:
    s = build_loop_services()

    async def go() -> tuple[list[str], list[str], str]:
        plan = await s.planner.plan(PlanRequest(robot_id=ROBOT, text="salsa", store_id=STORE))
        nav = plan.steps[0]
        req = VlaRequest(exec_id="x:s1:a1", robot_id=ROBOT, step=nav, deadline_s=5, heartbeat_s=0.05)
        a = [st.phase async for st in s.vla.execute(req)]
        b = [st.phase async for st in s.vla.execute(req)]  # re-attach: replay, no second motion
        other = req.model_copy(update={"tier": "t1_pretrained"})
        c = [st.error.code async for st in s.vla.execute(other) if st.error]
        return a, b, c[0]

    first, again, conflict = run(go())
    assert first == again and first[0] == "accepted" and first[-1] == "succeeded"
    assert s.robot.calls["navigate"] == 1
    assert conflict == "idempotency_conflict"
