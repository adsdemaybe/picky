"""Verifier unavailable / slow / provider down / camera blocked -> the degrade policy keeps the trip going."""

import asyncio
from typing import Any

from arms.agent.tests.loop_world import build_loop_services
from arms.agent.tests.loop_world.helpers import TACO_TEXT, run
from contracts.faults import FaultMatch, FaultRule, FaultScenario
from contracts.plan import PlanRequest, StepStatus
from contracts.policy import DEFAULT_RETRY_POLICY, VerifierDegradePolicy
from contracts.verify import VerifyRequest

ROBOT, STORE = "rover-ave-1", "store_ave"


def _go(s: Any, text: str = TACO_TEXT) -> Any:
    async def go() -> Any:
        plan = await s.planner.plan(PlanRequest(robot_id=ROBOT, text=text, store_id=STORE))
        return plan, await s.runner.run_plan(plan)

    return run(go())


def test_verifier_unavailable_degrades_to_state_only_checks() -> None:
    s = build_loop_services("verifier_down")
    _, out = _go(s)
    assert out.status == "completed"
    verdicts = [e.verdict for e in s.events.of("step.verified")]
    assert verdicts and all(v.degraded and v.decided_by == "state" for v in verdicts)
    assert out.metrics.degraded_checks >= len(verdicts)
    assert all(a.degraded for a in out.run.attempts if a.post is not None)
    assert len(s.catalog.picks) == 15


def test_state_only_checks_still_catch_the_false_grasp() -> None:
    scenario = FaultScenario(
        name="down_and_drop",
        description="verifier down + first grasp dropped",
        rules=[
            FaultRule(fault_id="v", effect="verifier_unavailable", times=0),
            FaultRule(
                fault_id="d", effect="drop_after_lift", match=FaultMatch(step_kind="grasp", occurrence=1, attempt=1)
            ),
        ],
    )
    s = build_loop_services(scenario)
    _, out = _go(s)
    assert out.status == "completed" and out.metrics.verifier_catches == 1
    f = s.events.of("step.failed")[0]
    assert f.failure.caught_by_verifier and f.failure.verdict.decided_by == "state" and f.failure.verdict.degraded
    assert f.decision.action == "reground"


class _SlowVerifier:
    """Wraps the fake verifier but takes longer than the runner's verify timeout."""

    def __init__(self, inner: Any) -> None:
        self.inner = inner

    async def verify(self, request: VerifyRequest) -> Any:
        await asyncio.sleep(1.0)
        return await self.inner.verify(request)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)


def test_verifier_timeout_degrades() -> None:
    policy = DEFAULT_RETRY_POLICY.model_copy(update={"degrade": VerifierDegradePolicy(verify_timeout_s=0.02)})
    s = build_loop_services(policy=policy)
    s.verifier = _SlowVerifier(s.verifier)  # type: ignore[assignment]
    _, out = _go(s, "salsa")
    assert out.status == "completed"
    v = s.events.of("step.verified")[0].verdict
    assert v.degraded and "TimeoutError" in v.reason


def test_degrade_policy_uncertain_asks_or_accepts() -> None:
    policy = DEFAULT_RETRY_POLICY.model_copy(update={"degrade": VerifierDegradePolicy(on_unavailable="uncertain")})
    s = build_loop_services("verifier_down", policy=policy)
    _, out = _go(s, "salsa")
    actions = [e.decision.action for e in s.events.of("step.failed")]
    assert actions and actions[0] == "reverify"
    assert out.metrics.asks >= 1  # uncertain everywhere -> the shopper is asked (auto-approve answers)


def test_provider_down_is_labelled_degraded_but_decided_by_ground_truth() -> None:
    scenario = FaultScenario(
        name="pdown", description="", rules=[FaultRule(fault_id="p", effect="provider_down", times=0)]
    )
    s = build_loop_services(scenario)
    _, out = _go(s, "salsa")
    assert out.status == "completed"
    v = s.events.of("step.verified")[0].verdict
    assert v.degraded and v.decided_by == "ground_truth" and v.methods_tried == ["perceptron", "ground_truth"]


def test_foggy_basket_is_accepted_provisionally_then_audited() -> None:
    s = build_loop_services("foggy_basket")
    plan, out = _go(s)
    assert out.status == "completed"
    actions = [e.decision.action for e in s.events.of("step.failed")]
    assert actions == ["reverify", "accept_provisional"]
    prov = [e for e in s.events.of("step.completed") if e.status == StepStatus.PROVISIONAL]
    assert len(prov) == 1 and all(r.provisional for r in prov[0].effects) and prov[0].effects
    audit = [e for e in s.events.of("step.verified") if e.phase == "verify"]
    assert audit and audit[-1].verdict.passed  # the pre-checkout audit confirmed the provisional place
    assert not out.run.provisional_open
    statuses = {line.sku: line.status for line in out.run.local_cart}
    prov_sku = next(r.payload["sku"] for r in prov[0].effects if r.kind == "record_pick")
    assert statuses[prov_sku] == "picked" and list(statuses.values()).count("verified") == len(statuses) - 1
