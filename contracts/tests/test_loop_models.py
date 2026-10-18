"""0.2.1 loop contracts: model validation + the pure decision functions (conditions, fusion, diagnosis, recovery,
normalize_draft, tiers, faults, verifier helpers, policy-server shapes). Ported from the API draft's tests, without the
draft's fakes (those land in the arms)."""

import math
from typing import Any

import pytest
from pydantic import TypeAdapter, ValidationError

from contracts.common import BBox, Point3, Pose2D
from contracts.evidence import FrameRef, SlotTruth, TruthSnapshot
from contracts.faults import DEMO_SCENARIOS, FAULT_LAYERS, FaultContext, FaultMatch, FaultRule, fault_fires
from contracts.fleet import GoalItem
from contracts.grounding import GRIPPER_ROI_WRIST, HELD_ROI_MIN_IOU, Evidence, NormBox, TargetSpec, box_iou
from contracts.plan import (
    DRAFT_MACROS,
    STEP_EXECUTORS,
    VLA_STEP_KINDS,
    AlignStep,
    AllOf,
    AskUserStep,
    Condition,
    DraftStep,
    GraspStep,
    GripperEmpty,
    GripperHolding,
    GroundTargetStep,
    HandoffStep,
    LabelMatches,
    NavigateToStep,
    PlaceStep,
    PlanDraft,
    PlanRequest,
    ScanShelfStep,
    StepPlan,
    StepStatus,
    VerifyStep,
    default_conditions,
    make_exec_id,
    make_plan_id,
    normalize_draft,
    validate_plan,
)
from contracts.policy import (
    DEFAULT_RETRY_POLICY,
    RecoveryContext,
    StepFailure,
    decide_recovery,
    failure_from_verdict,
)
from contracts.sim import CommandResult, ItemInstance
from contracts.skill_server import (
    SO101_JOINT_NAMES,
    PolicyClient,
    PolicyError,
    PolicyServerMetadata,
    PolicyServerSpec,
    build_observation,
    check_action_chunk,
)
from contracts.verify import (
    AtomVerdict,
    Verdict,
    VerifyBaseline,
    combine,
    combine_verdicts,
    count_boxes_by_sku,
    evaluate_on_truth,
    match_mention_to_sku,
    parse_price_cents,
    verify_deadline,
)
from contracts.vla import (
    PolicyServerStatus,
    RunSkillCmd,
    VlaCapabilities,
    VlaRequest,
    resolve_tier,
    skill_error_text,
    t1_capability_from_status,
    vla_error_from_skill_result,
)

COND = TypeAdapter(Condition)
ROBOT = "rover-ave-1"
STORE = "store_ave"


def _plan(**kw: Any) -> StepPlan:
    base: dict[str, Any] = {"plan_id": "p", "root_plan_id": "p", "goal_id": "g", "robot_id": ROBOT, "store_id": STORE}
    base.update(kw)
    return StepPlan(**base)


def _target(sku: str = "SALSA-MILD") -> TargetSpec:
    return TargetSpec(sku=sku, slot_id="S1", description="mild salsa")


def test_step_plan_json_roundtrip_keeps_discriminated_steps() -> None:
    pose = Pose2D(x=1, y=1, theta=math.pi / 2)
    gpre, gpost = default_conditions("grasp", sku="SALSA-MILD", slot_id="S1", approach_pose=pose, expect_text="Mild")
    ppre, ppost = default_conditions("place", sku="SALSA-MILD")
    _, audit = default_conditions("verify", basket_counts={"SALSA-MILD": 1})
    steps = [
        NavigateToStep(step_id="s01", title="Go", target="slot", slot_id="S1", goal_pose=pose),
        AlignStep(step_id="s02", title="Look", slot_id="S1"),
        ScanShelfStep(step_id="s03", title="Scan", slot_id="S1", expected_sku="SALSA-MILD"),
        AskUserStep(step_id="s04", title="Ask", ask_kind="recommendation", question="Want it?", item_id="i1"),
        GroundTargetStep(step_id="s05", title="Find", target=_target(), binding="target:i1:1"),
        GraspStep(
            step_id="s06",
            title="Grab",
            target=_target(),
            binding="target:i1:1",
            instruction="pick up the mild salsa jar",
            item_id="i1",
            preconditions=gpre,
            postconditions=gpost,
        ),
        PlaceStep(step_id="s07", title="Basket", sku="SALSA-MILD", preconditions=ppre, postconditions=ppost),
        VerifyStep(step_id="s08", title="Audit", condition=audit, purpose="audit"),
        HandoffStep(step_id="s09", title="Checkout"),
    ]
    items = [GoalItem(item_id="i1", query="salsa", sku="SALSA-MILD", qty=2, price_cents=349, optional=True)]
    plan = _plan(items=items, steps=steps, budget_cents=4000)
    again = StepPlan.model_validate_json(plan.model_dump_json())
    assert [s.kind for s in again.steps] == [s.kind for s in plan.steps]
    assert {s.kind for s in again.steps} == set(STEP_EXECUTORS)
    assert again.est_total_cents == plan.est_total_cents == 698
    assert again.current_step_id == "s01" and again.progress == 0.0
    assert isinstance(again.steps[5], GraspStep) and isinstance(again.steps[5].postconditions, AllOf)
    assert validate_plan(again) == [] or all(i.severity == "warning" for i in validate_plan(again))
    assert VLA_STEP_KINDS == {"navigate_to", "align", "grasp", "place", "handoff"}
    assert DRAFT_MACROS["fetch_item"][-2:] == ("grasp", "place")


def test_ids_differ_per_fleet_attempt() -> None:
    assert make_plan_id("g1") == "plan-g1" and make_plan_id("g1", 2) == "plan-g1-a2"
    assert make_exec_id("plan-g1", "s07", 2) == "plan-g1:s07:a2"


def test_condition_language_parses_and_rejects_unknown_ops() -> None:
    cond = COND.validate_python(
        {
            "op": "all",
            "of": [{"op": "gripper_holding", "sku": "X"}, {"op": "label_matches", "sku": "X", "expect_text": "x"}],
        }
    )
    assert isinstance(cond, AllOf) and isinstance(cond.of[0], GripperHolding)
    with pytest.raises(ValidationError):
        COND.validate_python({"op": "teleport"})
    with pytest.raises(ValidationError):
        AllOf(of=[])


def test_normbox_and_frameref_validation() -> None:
    with pytest.raises(ValidationError):
        NormBox(x1=10, y1=10, x2=5, y2=20)
    b = NormBox(x1=100, y1=200, x2=300, y2=600)
    assert NormBox.from_px(b.to_px(640, 480), 640, 480) == b
    with pytest.raises(ValidationError):  # privacy: an evidence frame is always face-blurred
        FrameRef(frame_id="f", camera_id="r.front", blurred=False)  # type: ignore[arg-type]
    assert FrameRef(frame_id="f1", camera_id="r.front").url == "/api/evidence/f1.jpg"


def test_vla_request_body_hash_ignores_run_state() -> None:
    pre, post = default_conditions("grasp", sku="SALSA-MILD", slot_id="S1")
    step = GraspStep(
        step_id="s1", title="Grab", target=_target(), instruction="pick", preconditions=pre, postconditions=post
    )
    a = VlaRequest(exec_id="p:s1:a1", robot_id="r", step=step, deadline_s=5)
    step2 = step.model_copy(update={"status": StepStatus.RUNNING, "attempts": 3})
    assert VlaRequest(exec_id="p:s1:a1", robot_id="r", step=step2, deadline_s=5).body_hash == a.body_hash
    assert (
        VlaRequest(exec_id="p:s1:a1", robot_id="r", step=step, tier="t1_pretrained", deadline_s=5).body_hash
        != a.body_hash
    )


def test_plan_request_needs_goal_or_text() -> None:
    with pytest.raises(ValidationError):
        PlanRequest(robot_id="r", store_id="s")
    with pytest.raises(ValidationError):
        PlanRequest(robot_id="r", text="milk")
    assert PlanRequest(robot_id="r", text="milk", store_id="s").store_id == "s"


def test_validate_plan_rules() -> None:
    items = [GoalItem(item_id="i1", query="salsa", sku="SALSA-MILD", optional=True)]
    _, post = default_conditions("grasp", sku="SALSA-MILD")
    g1 = GraspStep(step_id="s1", title="g", target=_target(), instruction="i", item_id="i1", postconditions=post)
    g2 = GraspStep(step_id="s2", title="g", target=_target(), instruction="i", item_id="i1", postconditions=post)
    codes = {i.code for i in validate_plan(_plan(items=items, steps=[g1, g2]))}
    assert {"double_grasp", "optional_not_asked"} <= codes
    place = PlaceStep(step_id="s3", title="p", sku="SALSA-MILD", postconditions=GripperEmpty())
    assert "place_empty" in {i.code for i in validate_plan(_plan(steps=[place]))}
    ghost = GraspStep(step_id="s4", title="g", target=_target(), instruction="i", item_id="nope", postconditions=post)
    assert "unknown_item" in {i.code for i in validate_plan(_plan(items=items, steps=[ghost]))}
    ask = AskUserStep(step_id="a", title="ask", ask_kind="recommendation", question="?", item_id="i1")
    ok = [
        ask,
        g1.model_copy(update={"step_id": "s5"}),
        place.model_copy(update={"step_id": "s6"}),
        HandoffStep(step_id="h", title="h", postconditions=GripperEmpty()),
    ]
    assert {i.code for i in validate_plan(_plan(items=items, steps=ok)) if i.severity == "error"} == {"no_audit"}


def test_kleene_combine_with_advisory_atoms() -> None:
    assert combine("all", [("pass", 1.0, False), ("uncertain", 0.0, True)]) == ("pass", 1.0)
    assert combine("all", [("pass", 1.0, False), ("uncertain", 0.3, False)]) == ("uncertain", 0.3)
    assert combine("all", [("pass", 1.0, False), ("fail", 0.9, True)])[0] == "fail"
    assert combine("any", [("fail", 1.0, False), ("pass", 0.7, False)]) == ("pass", 0.7)
    assert combine("not", [("pass", 0.8, False)]) == ("fail", 0.8)


def _av(outcome: str, conf: float, by: str = "provider") -> AtomVerdict:
    return AtomVerdict(path="0", op="gripper_holding", outcome=outcome, confidence=conf, decided_by=by)  # type: ignore[arg-type]


def test_combine_verdicts_camera_overrides_state_only_when_confident() -> None:
    state = _av("pass", 1.0, "state")
    assert combine_verdicts(state, _av("fail", 0.9)).outcome == "fail"
    assert combine_verdicts(state, _av("fail", 0.7)).outcome == "uncertain"
    assert combine_verdicts(state, _av("pass", 0.5)).confidence == 0.8  # camera too weak -> state, capped
    assert combine_verdicts(None, _av("pass", 0.95)).outcome == "pass"
    with pytest.raises(ValueError):
        combine_verdicts(None, None)


def _truth(holding: ItemInstance | None = None, basket: list[ItemInstance] | None = None) -> TruthSnapshot:
    pose = Pose2D(x=1, y=1, theta=math.pi / 2)
    return TruthSnapshot(
        robot_id=ROBOT,
        store_id=STORE,
        pose=pose,
        battery_pct=80,
        holding=holding,
        basket=basket or [],
        labels={"S1#01": "Sol de Casa Mild Chunky Salsa", "S2#01": "Fuego Ridge Habanero Salsa"},
        slots={
            "S1": SlotTruth(
                slot_id="S1", sku="SALSA-MILD", approach_pose=pose, item_position=Point3(x=1, y=1.38, z=0.8)
            )
        },
    )


def _unit(iid: str, sku: str) -> ItemInstance:
    return ItemInstance(instance_id=iid, sku=sku, position=Point3(x=0, y=0, z=0))


def _verdict(cond: Condition, truth: TruthSnapshot, baseline: VerifyBaseline | None = None) -> Verdict:
    outcome, conf, atoms = evaluate_on_truth(cond, truth, baseline=baseline)
    return Verdict(verify_id="v", robot_id=ROBOT, outcome=outcome, confidence=conf, reason="r", atoms=atoms)


def test_diagnosis_drop_wrong_item_and_place_not_released() -> None:
    _, post = default_conditions("grasp", sku="SALSA-MILD", slot_id="S1", expect_text="Mild Chunky Salsa")
    grasp = GraspStep(step_id="s1", title="g", target=_target(), instruction="i", postconditions=post)
    drop = failure_from_verdict(
        _verdict(post, _truth()), plan_id="p", step=grasp, attempt=1, phase="post", vla_claimed_success=True
    )
    assert drop is not None and drop.code == "atom:gripper_holding" and drop.caught_by_verifier
    wrong = failure_from_verdict(
        _verdict(post, _truth(holding=_unit("S2#01", "SALSA-HOT"))), plan_id="p", step=grasp, attempt=1, phase="post"
    )
    assert wrong is not None and wrong.code == "wrong_item_grasped"
    _, ppost = default_conditions("place", sku="SALSA-MILD")
    place = PlaceStep(step_id="s2", title="p", sku="SALSA-MILD", postconditions=ppost)
    v = _verdict(ppost, _truth(holding=_unit("S1#01", "SALSA-MILD")), VerifyBaseline(baseline_id="b", robot_id=ROBOT))
    nr = failure_from_verdict(v, plan_id="p", step=place, attempt=1, phase="post")
    assert nr is not None and nr.code == "place_not_released"
    assert decide_recovery(DEFAULT_RETRY_POLICY, RecoveryContext(failure=nr)).action == "retry"
    assert isinstance(post, AllOf) and isinstance(post.of[1], LabelMatches) and post.of[1].advisory
    ok = _verdict(post, _truth(holding=_unit("S1#01", "SALSA-MILD")))
    assert ok.passed and failure_from_verdict(ok, plan_id="p", step=grasp, attempt=1, phase="post") is None


def _failure(
    code: str, kind: str = "postcondition", step_kind: str = "grasp", tier: str | None = "t0_scripted"
) -> StepFailure:
    return StepFailure(
        plan_id="p",
        step_id="s1",
        step_kind=step_kind,  # type: ignore[arg-type]
        attempt=1,
        kind=kind,  # type: ignore[arg-type]
        code=code,
        item_id="i1",
        tier=tier,  # type: ignore[arg-type]
    )


def test_decide_recovery_ladder_is_counted_per_rule() -> None:
    first = decide_recovery(
        DEFAULT_RETRY_POLICY, RecoveryContext(failure=_failure("atom:gripper_holding", kind="verifier_uncertain"))
    )
    assert (first.rule_id, first.action) == ("grasp_unsure", "reverify")
    second = decide_recovery(
        DEFAULT_RETRY_POLICY,
        RecoveryContext(
            failure=_failure("atom:gripper_holding"), rule_failures={"grasp_unsure": 1}, failures_of_step=2
        ),
    )
    assert (second.rule_id, second.action, second.rung) == ("grasp_failed", "reground", 1)
    assert second.insert == ["ground_target"] and second.rerun_failed_step and second.next_attempt == 3


def test_decide_recovery_budgets_infra_and_fatal() -> None:
    free = decide_recovery(
        DEFAULT_RETRY_POLICY, RecoveryContext(failure=_failure("disconnected", kind="vla_error"), failures_of_step=9)
    )
    assert free.action == "retry" and not free.budget_exhausted and "free retry" in free.reason
    fatal = decide_recovery(DEFAULT_RETRY_POLICY, RecoveryContext(failure=_failure("battery_low", kind="vla_error")))
    assert fatal.action == "escalate" and not fatal.inner
    spent = decide_recovery(
        DEFAULT_RETRY_POLICY,
        RecoveryContext(
            failure=_failure("atom:gripper_holding"), failures_of_step=3, rule_failures={"grasp_failed": 2}
        ),
    )
    assert spent.action == "ask_user" and spent.budget_exhausted
    t1 = decide_recovery(
        DEFAULT_RETRY_POLICY,
        RecoveryContext(
            failure=_failure("grasp_missed", kind="vla_error", tier="t1_pretrained"),
            tier_strikes=1,
            available_tiers=["t1_pretrained", "t0_scripted"],
        ),
    )
    assert t1.open_breaker == "t1_pretrained" and t1.next_tier == "t0_scripted"
    pre = decide_recovery(DEFAULT_RETRY_POLICY, RecoveryContext(failure=_failure("atom:robot_at", kind="precondition")))
    assert pre.action == "renavigate" and pre.rule_id == "pre:atom:robot_at"


def test_resolve_tier_floor_breaker_and_privacy_gate() -> None:
    t1 = VlaCapabilities(robot_id="r", tier="t1_pretrained", available=True, step_kinds=["grasp"])
    assert resolve_tier("auto", [t1], "grasp") == "t1_pretrained"
    assert resolve_tier("auto", [t1], "grasp", open_tiers={"t1_pretrained"}) == "t0_scripted"
    assert resolve_tier("auto", [t1.model_copy(update={"input_frames_blurred": False})], "grasp") == "t0_scripted"
    assert resolve_tier("auto", [t1], "navigate_to") == "t0_scripted"
    assert resolve_tier("t0_scripted", [], "grasp") == "t0_scripted"


def test_fault_occurrence_is_counted_over_distinct_steps() -> None:
    """The FaultInjector rule: occurrence = n-th DISTINCT step_key matching the rule (retries don't advance it)."""
    rule = FaultRule(
        fault_id="f", effect="grasp_miss", match=FaultMatch(step_kind="grasp", occurrence=2, attempt=1), times=0
    )
    seen: list[str] = []
    fired = 0
    out: list[int] = []
    for step, attempt in (("s1", 1), ("s1", 2), ("s2", 1), ("s2", 2), ("s3", 1)):
        key = f"p:{step}"
        if key not in seen:
            seen.append(key)
        ctx = FaultContext(
            robot_id="r",
            step_kind="grasp",
            step_id=step,
            step_key=key,
            attempt=attempt,
            occurrence=seen.index(key) + 1,
        )
        hit = fault_fires(rule, ctx, fired)
        fired += int(hit)
        out.append(int(hit))
    assert out == [0, 0, 1, 0, 0]
    drop = DEMO_SCENARIOS["drop_on_first_grasp"].rules[0]
    assert drop.effect == "drop_after_lift" and drop.match.occurrence == 1 and drop.match.attempt == 1
    assert DEMO_SCENARIOS["wrong_salsa"].two_d_fake_only
    assert set(FAULT_LAYERS.values()) == {"vla", "camera", "verifier", "world"}
    once = FaultRule(fault_id="o", effect="grasp_miss", times=1)
    assert fault_fires(once, FaultContext(robot_id="r"), 0) and not fault_fires(once, FaultContext(robot_id="r"), 1)


def _items() -> list[GoalItem]:
    return [
        GoalItem(item_id="a", query="salsa", sku="SALSA-MILD", slot_id="S1", stop_id="st1"),
        GoalItem(item_id="b", query="chips", sku="CHIPS", slot_id="S2", stop_id="st2", optional=True),
        GoalItem(item_id="c", query="limes", sku="LIME", slot_id="S3", qty=2, stop_id="st3"),
        GoalItem(item_id="d", query="kombucha", sku="KOMB", status="online"),
    ]


def test_normalize_draft_repairs_a_messy_llm_draft() -> None:
    draft = PlanDraft(
        summary="tacos",
        steps=[
            DraftStep(kind="fetch_item", item_id="c"),
            DraftStep(kind="checkout"),  # too early
            DraftStep(kind="fetch_item", item_id="c"),  # duplicate: would pick both limes twice
            DraftStep(kind="fetch_item", item_id="b"),
            DraftStep(kind="ask_user", item_id="b", question="Want chips?"),  # after its pick
            DraftStep(kind="fetch_item", item_id="zzz"),  # unknown
            DraftStep(kind="navigate_to", target="slot"),  # no slot
        ],
    )
    out, fixes = normalize_draft(draft, _items(), end_at="checkout")
    assert [(d.kind, d.item_id) for d in out.steps] == [
        ("fetch_item", "c"),
        ("ask_user", "b"),
        ("fetch_item", "b"),
        ("fetch_item", "a"),  # the forgotten item, added
        ("checkout", None),  # exactly one, last
    ]
    text = " | ".join(fixes)
    for needle in ("duplicate fetch", "unknown item zzz", "before its pick", "missing pick of a", "moved checkout"):
        assert needle in text, (needle, fixes)


def test_normalize_draft_route_lock_and_end_at() -> None:
    draft = PlanDraft(
        summary="s",
        steps=[
            DraftStep(kind="fetch_item", item_id="c"),
            DraftStep(kind="fetch_item", item_id="a"),
            DraftStep(kind="checkout"),
        ],
    )
    locked, fixes = normalize_draft(draft, _items(), end_at="dock", lock_order=True)
    assert [d.item_id for d in locked.steps if d.kind == "fetch_item"] == ["a", "c", "b"]  # route order, b added last
    assert all(d.kind != "checkout" for d in locked.steps)
    assert any("route order" in f for f in fixes) and any("ends at the dock" in f for f in fixes)
    empty, fixes = normalize_draft(PlanDraft(summary="s", steps=[DraftStep(kind="checkout")]), [], end_at="dock")
    assert [(d.kind, d.target) for d in empty.steps] == [("navigate_to", "dock")]


def test_draft_schema_is_llm_friendly() -> None:
    """The JSON schema the planner sends to the LLM (utils.llm.complete_json) carries the field descriptions."""
    schema = PlanDraft.model_json_schema()
    step = schema["$defs"]["DraftStep"]
    assert "fetch_item" in step["properties"]["kind"]["description"]
    assert "EXACTLY" in step["properties"]["item_id"]["description"]


def test_skill_result_errors_map_to_typed_vla_errors() -> None:
    def res(ok: bool, code: Any = None, err: str | None = None) -> CommandResult:
        return CommandResult(cmd_id="c1", robot_id=ROBOT, ok=ok, error_code=code, error=err)

    assert vla_error_from_skill_result(res(True)) is None
    e = vla_error_from_skill_result(res(False, "internal", skill_error_text("grasp_missed", "closed on air")))
    assert e is not None and e.code == "grasp_missed" and e.message == "closed on air" and e.tier == "t1_pretrained"
    e = vla_error_from_skill_result(res(False, "internal", "vla:made_up_code"))
    assert e is not None and e.code == "internal" and e.details == {"worker_code": "made_up_code"}
    e = vla_error_from_skill_result(res(False, "busy", "pick running"))
    assert e is not None and e.code == "busy" and e.sim_code == "busy"
    cmd = RunSkillCmd(
        cmd_id="c1", robot_id=ROBOT, skill="grasp", tier="t1_pretrained", instruction="pick up", exec_id="x"
    )
    again = RunSkillCmd.model_validate_json(cmd.model_dump_json())
    assert again.cameras == ["front", "wrist"] and again.success_check == "held_geometry" and again.blur_faces is True


def test_t1_capability_follows_policy_server_status_and_privacy_gate() -> None:
    st = PolicyServerStatus(model_id="lerobot/smolvla_base", ready=True, robot_ids=[ROBOT], action_hz=30, chunk_size=50)
    assert not t1_capability_from_status(ROBOT, None, "isaac").available
    assert "2D fake" in t1_capability_from_status(ROBOT, st, "fake").reason
    assert not t1_capability_from_status("rover-ave-2", st, "isaac").available
    cap = t1_capability_from_status(ROBOT, st, "isaac")
    assert cap.available and cap.step_kinds == ["grasp", "place"] and cap.model_id == "lerobot/smolvla_base"
    assert resolve_tier("auto", [cap], "grasp") == "t1_pretrained"
    assert resolve_tier("auto", [cap], "navigate_to") == "t0_scripted"
    unblurred = t1_capability_from_status(ROBOT, st.model_copy(update={"input_frames_blurred": False}), "isaac")
    assert not unblurred.available and resolve_tier("auto", [unblurred], "grasp") == "t0_scripted"


class _ScriptedPolicy:
    """Minimal PolicyClient (the draft's ScriptedPolicyClient moves to the sim arm's tests)."""

    def __init__(self, spec: PolicyServerSpec, fail_on: dict[int, str] | None = None) -> None:
        self.spec, self.fail_on, self.calls, self._ready = spec, fail_on or {}, 0, False

    @property
    def ready(self) -> bool:
        return self._ready

    def connect(self) -> PolicyServerMetadata:
        self._ready = True
        return PolicyServerMetadata(
            model_id=self.spec.model_id,
            action_dim=self.spec.action_dim,
            chunk_size=self.spec.chunk_size,
            joint_names=list(SO101_JOINT_NAMES),
        )

    def infer(self, observation: Any) -> list[list[float]]:
        self.calls += 1
        if self.calls in self.fail_on:
            raise PolicyError(self.fail_on[self.calls], "scripted")  # type: ignore[arg-type]
        return [[0.0] * self.spec.action_dim for _ in range(self.spec.chunk_size)]

    def reset(self) -> None:
        self.calls = 0

    def close(self) -> None:
        self._ready = False


def test_policy_server_adapter_shapes() -> None:
    spec = PolicyServerSpec()
    obs = build_observation(spec, {"front": "IMG_F", "wrist": "IMG_W"}, [0.0] * 6, "pick up the mild salsa jar")
    assert obs == {
        "observation.images.front": "IMG_F",
        "observation.images.wrist": "IMG_W",
        "observation.state": [0.0] * 6,
        "task": "pick up the mild salsa jar",
    }
    with pytest.raises(ValueError):
        build_observation(spec, {}, [0.0] * 5, "x")
    assert len(SO101_JOINT_NAMES) == spec.action_dim
    assert check_action_chunk(spec, []) == "empty action chunk"
    assert "values" in (check_action_chunk(spec, [[0.0] * 5]) or "")
    assert "out of range" in (check_action_chunk(spec, [[float("nan")] * 6]) or "")
    client = _ScriptedPolicy(spec, fail_on={2: "policy_timeout"})
    assert isinstance(client, PolicyClient)
    meta = client.connect()
    assert meta.action_dim == 6 and client.ready
    assert check_action_chunk(spec, client.infer(obs)) is None
    with pytest.raises(PolicyError) as err:
        client.infer(obs)
    assert err.value.code == "policy_timeout"


def test_verifier_rule_helpers() -> None:
    held = NormBox.from_px(BBox(x=640 * 0.35, y=480 * 0.35, w=640 * 0.3, h=480 * 0.3), 640, 480)  # fake wrist box
    assert box_iou(held, GRIPPER_ROI_WRIST) > HELD_ROI_MIN_IOU
    assert box_iou(NormBox(x1=0, y1=0, x2=100, y2=100), GRIPPER_ROI_WRIST) == 0.0
    assert [parse_price_cents(t) for t in ("$3.49", "3,49", "$5", "99¢", "SALE 2 for $5.00", "4 oz", "")] == [
        349,
        349,
        500,
        99,
        500,
        None,
        None,
    ]
    names = {"SALSA-MILD": "Sol de Casa Mild Chunky Salsa 16 oz", "SALSA-HOT": "Sol de Casa Hot Chunky Salsa 16 oz"}
    assert match_mention_to_sku("mild salsa jar", names) == "SALSA-MILD"
    assert match_mention_to_sku("salsa", names) is None  # ambiguous never matches
    boxes = [
        Evidence(kind="box", mention=m) for m in ("mild salsa", "Sol de Casa Hot Chunky Salsa", "a hand", "mild salsa")
    ]
    assert count_boxes_by_sku(boxes, names) == {"SALSA-MILD": 2, "SALSA-HOT": 1}
    assert verify_deadline(8.0) == 7.0 and verify_deadline(0.2) == 0.5
