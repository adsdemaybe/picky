"""PerceptionVerifier (``create_verifier``): Perceptron-style judging (ROI rule, cue agreement, mention -> SKU),
fusion with robot state, modes decide / shadow / gt_only, deadlines, fallbacks, breaker, grounding, attribution.
Providers are scripted or the deterministic sidecar provider: no network, no key."""

import time
from types import SimpleNamespace

import pytest

import arms.perception.loop_config as loop_config
from arms.perception import create_verifier
from arms.perception.evidence import EvidenceRing, FakeEvidenceCameras
from arms.perception.tests.loop_world import ROBOT, SLOT, ScriptedFaults, ScriptedProvider, answer, world
from arms.perception.verifier import PerceptionVerifier
from arms.perception.vision import PerceptronVisionProvider, SidecarVisionProvider
from contracts.grounding import TargetSpec
from contracts.plan import (
    AllOf,
    BasketDelta,
    GripperEmpty,
    GripperHolding,
    ItemOnShelf,
    LabelMatches,
    RobotAt,
    ShelfTagReads,
)
from contracts.services import VerifierService
from contracts.verify import GroundRequest, VerifierUnavailableError, VerifyRequest

HOLD_MILD = GripperHolding(sku="SALSA-MILD", note="holding the mild salsa")
MILD = "Sol de Casa Mild Chunky Salsa 16 oz"
HOT = "Fuego Ridge Habanero Fire Salsa 16 oz"
IN_JAWS = (380, 420, 620, 800)  # inside GRIPPER_ROI_WRIST
OFF_JAWS = (0, 0, 120, 120)


def _setup(provider=None, *, mode="decide", backend="fake", **kw):
    services, robot = world(backend=backend)
    services.evidence = FakeEvidenceCameras(services)  # type: ignore[assignment]
    provider = provider or SidecarVisionProvider(services)
    return services, robot, PerceptionVerifier(services, provider, mode=mode, **kw)


def _req(cond, vid="v", **kw):
    return VerifyRequest(verify_id=vid, robot_id=ROBOT, condition=cond, **kw)


def test_satisfies_the_protocol():
    _, _, v = _setup()
    assert isinstance(v, VerifierService)


# ---- decide mode with the deterministic sidecar provider -------------------------------------------------------------
async def test_decide_holding_is_decided_by_the_provider_with_attribution():
    services, robot, v = _setup()
    robot.pick("SALSA-MILD")
    verdict = await v.verify(_req(HOLD_MILD))
    assert verdict.passed and verdict.decided_by == "provider" and verdict.provider is not None
    assert verdict.provider.provider == "fake" and verdict.ground_truth_agrees is True
    atom = verdict.atoms[0]
    assert atom.decided_by == "provider" and "state agrees" in atom.reason and atom.confidence >= 0.9
    roles = {e.role for e in atom.evidence}
    assert {"held", "gripper_roi"} <= roles and all(e.frame is not None for e in atom.evidence)


async def test_decide_catches_the_false_success_drop():
    services, robot, v = _setup()  # VLA said "grasped" but nothing is attached: state + camera say empty
    verdict = await v.verify(_req(HOLD_MILD, phase="post"))
    assert verdict.outcome == "fail" and "gripper is empty" in verdict.reason


async def test_two_agreeing_camera_cues_override_robot_state():
    provider = ScriptedProvider(
        {"detect": answer(boxes=[]), "question": answer(text="Yes, nothing is between the jaws.", yes=True)}
    )
    services, robot, v = _setup(provider)
    robot.pick("SALSA-MILD")  # robot state says holding (Isaac: dropped after lift)
    verdict = await v.verify(_req(HOLD_MILD))
    assert verdict.outcome == "fail" and "overrides robot state" in verdict.reason
    assert verdict.decided_by == "provider" and verdict.provider.attribution == "Perceptron perceptron-mk1.5"
    assert verdict.ground_truth_agrees is False  # sim truth still holds the jar: logged as a disagreement


async def test_one_disagreeing_cue_cannot_override_state():
    provider = ScriptedProvider({"detect": answer(boxes=[]), "question": answer(error="timeout")})
    services, robot, v = _setup(provider)
    robot.pick("SALSA-MILD")
    verdict = await v.verify(_req(HOLD_MILD))
    assert verdict.outcome == "uncertain" and "camera (" in verdict.reason


async def test_conflicting_cues_are_uncertain_and_state_decides():
    provider = ScriptedProvider(
        {"detect": answer(boxes=[(MILD, IN_JAWS)]), "question": answer(text="Yes, it is empty.", yes=True)}
    )
    services, robot, v = _setup(provider)
    robot.pick("SALSA-MILD")
    verdict = await v.verify(_req(HOLD_MILD))
    assert verdict.passed and "camera inconclusive" in verdict.atoms[0].reason


async def test_wrong_item_named_by_the_held_box():
    provider = ScriptedProvider(
        {"detect": answer(boxes=[(HOT, IN_JAWS)]), "question": answer(text="No, a jar is held.", yes=False)}
    )
    services, robot, v = _setup(provider)
    robot.face("SALSA-HOT")
    robot.pick("SALSA-HOT")
    verdict = await v.verify(_req(HOLD_MILD))
    assert verdict.outcome == "fail" and verdict.atoms[0].observed["held_sku"] == "SALSA-HOT"
    detect_prompt = next(p for k, p in provider.calls if k == "detect")
    assert MILD in detect_prompt and HOT in detect_prompt  # planogram neighbours are detect classes


async def test_box_outside_the_jaws_does_not_count_as_held():
    provider = ScriptedProvider({"detect": answer(boxes=[(MILD, OFF_JAWS)]), "question": answer(text="Yes.", yes=True)})
    services, robot, v = _setup(provider)
    verdict = await v.verify(_req(GripperEmpty()))
    assert verdict.passed and verdict.atoms[0].confidence >= 0.9


async def test_label_shelf_tag_and_shelf_count_judges():
    services, robot, v = _setup()
    robot.pick("SALSA-MILD")
    label = await v.verify(_req(LabelMatches(sku="SALSA-MILD", expect_text="mild chunky"), "l1"))
    assert label.passed and label.atoms[0].evidence[0].text == MILD
    wrong = await v.verify(_req(LabelMatches(sku="SALSA-MILD", expect_text="medium"), "l2"))
    assert wrong.outcome == "fail" and "expected 'medium'" in wrong.reason
    robot.face("SALSA-HOT")
    tag = await v.verify(_req(ShelfTagReads(slot_id=SLOT["SALSA-HOT"], cents=399), "t1"))
    assert tag.passed
    mispriced = await v.verify(_req(ShelfTagReads(slot_id=SLOT["SALSA-HOT"], cents=499), "t2"))
    assert mispriced.outcome == "fail" and mispriced.atoms[0].observed["cents"] == 399
    few = await v.verify(_req(ItemOnShelf(slot_id=SLOT["SALSA-HOT"], sku="SALSA-HOT", min_count=9), "c1"))
    assert few.outcome == "uncertain"  # fewer visible is never 'fail' (back rows hidden)


async def test_basket_delta_counts_before_and_after_frames():
    services, robot, v = _setup()
    cond = BasketDelta(sku="SALSA-MILD", delta=1)
    base = await v.baseline(ROBOT, cond, baseline_id="b")
    assert base.frames and base.frames[0].camera_id == f"{ROBOT}.basket"
    robot.pick("SALSA-MILD")
    robot.place()
    verdict = await v.verify(_req(cond, baseline=base))
    assert verdict.passed and "camera before/after" in verdict.atoms[0].reason


# ---- fallbacks / degrade -----------------------------------------------------------------------------------------
async def test_provider_errors_fall_back_to_ground_truth_and_say_so():
    provider = ScriptedProvider({"detect": answer(error="timeout"), "question": answer(error="timeout")})
    services, robot, v = _setup(provider)
    robot.pick("SALSA-MILD")
    verdict = await v.verify(_req(HOLD_MILD))
    assert verdict.passed and verdict.degraded and verdict.decided_by == "ground_truth"
    assert verdict.methods_tried == ["perceptron", "ground_truth"] and "vision timeout" in verdict.reason


async def test_no_truth_falls_back_to_robot_state():
    provider = ScriptedProvider({"detect": answer(error="rate_limit"), "question": answer(error="rate_limit")})
    services, robot, v = _setup(provider)
    services.evidence = EvidenceRing(services)  # type: ignore[assignment]  # real frames need no truth

    class NoTruth:
        async def truth(self, robot_id, slot_ids=()):
            raise RuntimeError("isaac truth unavailable")

    services.truth = NoTruth()  # type: ignore[assignment]
    robot.pick("SALSA-MILD")
    verdict = await v.verify(_req(HOLD_MILD))
    assert verdict.passed and verdict.degraded and verdict.methods_tried == ["perceptron", "state"]
    assert "vision rate_limit; fell back to robot state" in verdict.reason
    camera_only = await v.verify(_req(LabelMatches(sku="SALSA-MILD", expect_text="mild"), "l"))
    assert camera_only.outcome == "uncertain"


async def test_provider_down_fault_makes_no_calls():
    provider = ScriptedProvider({})
    services, robot, v = _setup(provider)
    services.faults = ScriptedFaults({"provider_down": {}})  # type: ignore[assignment]
    verdict = await v.verify(_req(HOLD_MILD))
    assert provider.calls == [] and verdict.degraded and "provider_down" in verdict.reason


async def test_verifier_unavailable_only_without_state_and_truth():
    services, robot, v = _setup()
    services.robot = None
    with pytest.raises(VerifierUnavailableError):
        await v.verify(_req(HOLD_MILD))


async def test_slow_provider_never_overruns_the_deadline():
    provider = ScriptedProvider({"detect": answer(), "question": answer(yes=True)}, delay_s=5.0)
    services, robot, v = _setup(provider)
    t0 = time.monotonic()
    verdict = await v.verify(_req(GripperEmpty(), deadline_s=1.0))
    assert time.monotonic() - t0 < 1.5
    assert verdict.passed and verdict.degraded and "deadline" in verdict.reason


async def test_breaker_opens_after_repeated_failures():
    provider = ScriptedProvider({"detect": answer(error="server_error"), "question": answer(error="server_error")})
    services, robot, v = _setup(provider, breaker_failures=3)
    await v.verify(_req(GripperEmpty(), "a"))
    await v.verify(_req(GripperEmpty(), "b"))
    n = len(provider.calls)
    verdict = await v.verify(_req(GripperEmpty(), "c"))
    assert len(provider.calls) == n and "breaker_open" in verdict.reason
    caps = await v.capabilities()
    assert not caps.healthy and "circuit open" in caps.reason and "perceptron" not in caps.methods


# ---- modes ---------------------------------------------------------------------------------------------------------
async def test_shadow_mode_ground_truth_decides_and_the_provider_rides_along():
    provider = ScriptedProvider({"detect": answer(boxes=[]), "question": answer(text="Yes, it is empty.", yes=True)})
    services, robot, v = _setup(provider, mode="shadow")
    robot.pick("SALSA-MILD")
    verdict = await v.verify(_req(HOLD_MILD))
    assert verdict.passed and verdict.decided_by == "ground_truth" and not verdict.degraded
    [op] = verdict.shadow
    assert op.outcome == "fail" and op.provider.attribution == "Perceptron perceptron-mk1.5"
    assert verdict.ground_truth_agrees is False and verdict.methods_tried == ["perceptron", "ground_truth"]
    [info] = await v.providers()
    assert info.agreement_with_ground_truth == 0.0 and info.calls == 2 and info.provider == "perceptron"


async def test_default_mode_follows_the_backend_and_gt_only_makes_no_calls():
    provider = ScriptedProvider({"detect": answer(), "question": answer(yes=True)})
    services, robot, v = _setup(provider, mode=None)
    assert v.resolve_mode(None, "fake") == "shadow" and v.resolve_mode(None, "isaac") == "decide"
    gt = await v.verify(_req(GripperEmpty(), mode="gt_only"))
    assert gt.passed and provider.calls == [] and gt.methods_tried == ["ground_truth"]
    caps = await v.set_mode("decide")
    assert caps.mode == "decide" and caps.healthy


async def test_state_only_atoms_never_call_the_provider():
    provider = ScriptedProvider({})
    services, robot, v = _setup(provider)
    verdict = await v.verify(_req(AllOf(of=[RobotAt(pose=robot.pose)])))
    assert verdict.passed and provider.calls == [] and verdict.decided_by == "state"


# ---- grounding -----------------------------------------------------------------------------------------------------
TARGET = TargetSpec(sku="SALSA-HOT", slot_id=SLOT["SALSA-HOT"], description=HOT)


async def test_decide_grounding_maps_the_grasp_point_to_a_unit():
    services, robot, v = _setup()
    robot.face("SALSA-HOT")
    res = await v.ground(GroundRequest(grounding_id="g", robot_id=ROBOT, target=TARGET))
    assert res.found and res.target is not None and res.target.decided_by == "provider"
    assert res.target.instance_id == f"{SLOT['SALSA-HOT']}#01" and res.target.grasp_point is not None


async def test_shadow_grounding_keeps_ground_truth_and_attaches_the_provider_box():
    provider = ScriptedProvider(
        {"detect": answer(boxes=[(HOT, (290, 410, 420, 800))]), "point": answer(points=[(HOT, (350, 600))])}
    )
    services, robot, v = _setup(provider, mode="shadow")
    robot.face("SALSA-HOT")
    res = await v.ground(GroundRequest(grounding_id="g", robot_id=ROBOT, target=TARGET))
    assert res.found and res.target.decided_by == "ground_truth"
    opinion = res.target.candidates[0]
    assert "Perceptron" in opinion.mention and opinion.point is not None
    assert res.methods_tried == ["ground_truth", "perceptron"]


async def test_grounding_falls_back_when_the_provider_fails():
    provider = ScriptedProvider({"detect": answer(error="timeout"), "point": answer(error="timeout")})
    services, robot, v = _setup(provider)
    robot.face("SALSA-HOT")
    res = await v.ground(GroundRequest(grounding_id="g", robot_id=ROBOT, target=TARGET))
    assert res.found and res.degraded and res.methods_tried == ["perceptron", "ground_truth"]


async def test_provider_sees_nothing_in_decide_mode():
    provider = ScriptedProvider({"detect": answer(boxes=[]), "point": answer(points=[])})
    services, robot, v = _setup(provider)
    res = await v.ground(GroundRequest(grounding_id="g", robot_id=ROBOT, target=TARGET))
    assert not res.found and "Perceptron" in res.reason


# ---- factory -------------------------------------------------------------------------------------------------------
def _settings(monkeypatch, **values):
    monkeypatch.setattr(loop_config, "get_settings", lambda: SimpleNamespace(**values))


def test_create_verifier_raises_without_a_key(monkeypatch):
    services, _ = world()
    _settings(monkeypatch)
    with pytest.raises(RuntimeError, match="PERCEPTRON_API_KEY"):
        create_verifier(services)
    _settings(monkeypatch, picky_vision_provider="gemini")
    with pytest.raises(RuntimeError, match="not built"):
        create_verifier(services)


def test_create_verifier_with_the_fake_provider_and_settings(monkeypatch):
    services, _ = world()
    _settings(monkeypatch, picky_vision_provider="fake", picky_verifier_mode="shadow", picky_vision_call_timeout_s="2")
    v = create_verifier(services)
    assert isinstance(v.provider, SidecarVisionProvider) and v.resolve_mode(None, "isaac") == "shadow"
    assert v.call_timeout_s == 2.0


def test_create_verifier_with_a_key_builds_perceptron(monkeypatch):
    services, _ = world()
    _settings(monkeypatch, perceptron_api_key="pk-test", perceptron_model="perceptron-mk1", picky_verifier_mode="bogus")
    monkeypatch.setattr(PerceptronVisionProvider, "sdk_available", staticmethod(lambda: True))
    v = create_verifier(services)
    assert isinstance(v.provider, PerceptronVisionProvider) and v.provider.model == "perceptron-mk1"
    assert v.provider.base_url == "https://api.perceptron.inc/v1" and v._mode_override is None
