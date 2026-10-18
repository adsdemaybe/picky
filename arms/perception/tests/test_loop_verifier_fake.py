"""FakeVerifier (``create_verifier_fake``): exact, deterministic verdicts from frame ground truth over the fake
camera API, the verifier-layer demo faults, stale frames, baselines and ground-truth grounding."""

from datetime import timedelta

import pytest

from arms.perception import create_verifier_fake
from arms.perception.evidence import FakeEvidenceCameras
from arms.perception.tests.loop_world import ROBOT, SLOT, ScriptedFaults, world
from contracts.common import utcnow
from contracts.grounding import TargetSpec
from contracts.plan import AllOf, BasketDelta, GripperEmpty, GripperHolding, ItemOnShelf, RobotAt
from contracts.services import VerifierService
from contracts.verify import GroundRequest, VerifierUnavailableError, VerifyRequest

HOLD_MILD = GripperHolding(sku="SALSA-MILD", note="holding the mild salsa")


def _setup(**kw):
    services, robot = world(**kw)
    services.evidence = FakeEvidenceCameras(services)  # type: ignore[assignment]
    return services, robot, create_verifier_fake(services)


def test_fake_verifier_satisfies_the_protocol():
    services, _ = world()
    assert isinstance(create_verifier_fake(services), VerifierService)


async def test_holding_passes_and_empty_gripper_fails_with_a_ui_reason():
    services, robot, v = _setup()
    bad = await v.verify(VerifyRequest(verify_id="v-empty", robot_id=ROBOT, condition=HOLD_MILD))
    assert bad.outcome == "fail" and "gripper is empty" in bad.reason and bad.decided_by == "ground_truth"
    assert bad.frames and bad.frames[0].camera_id == f"{ROBOT}.wrist"
    assert bad.atoms[0].evidence and bad.atoms[0].evidence[0].frame is not None
    robot.pick("SALSA-MILD")
    ok = await v.verify(VerifyRequest(verify_id="v-held", robot_id=ROBOT, condition=HOLD_MILD))
    assert ok.passed and ok.provider is not None and ok.provider.attribution == "Fake verifier"
    assert ok.ground_truth_agrees is True and not ok.degraded


async def test_verify_is_idempotent_per_verify_id():
    services, robot, v = _setup()
    first = await v.verify(VerifyRequest(verify_id="same", robot_id=ROBOT, condition=HOLD_MILD))
    robot.pick("SALSA-MILD")
    again = await v.verify(VerifyRequest(verify_id="same", robot_id=ROBOT, condition=HOLD_MILD))
    assert again == first and v.calls == 1


async def test_wrong_item_fails_with_the_held_sku_observed():
    services, robot, v = _setup()
    robot.face("SALSA-HOT")
    robot.pick("SALSA-HOT")
    verdict = await v.verify(VerifyRequest(verify_id="v", robot_id=ROBOT, condition=HOLD_MILD))
    assert verdict.outcome == "fail" and verdict.atoms[0].observed["held_sku"] == "SALSA-HOT"


async def test_state_and_shelf_atoms():
    services, robot, v = _setup()
    cond = AllOf(
        of=[
            RobotAt(pose=robot.pose, note="at the shelf"),
            ItemOnShelf(slot_id=SLOT["SALSA-MILD"], sku="SALSA-MILD", min_count=2),
            GripperEmpty(),
        ]
    )
    verdict = await v.verify(VerifyRequest(verify_id="pre", robot_id=ROBOT, phase="pre", condition=cond))
    assert verdict.passed and verdict.reason.startswith("ok:")
    assert {f.camera_id for f in verdict.frames} == {f"{ROBOT}.front", f"{ROBOT}.wrist"}


async def test_basket_delta_against_a_baseline():
    services, robot, v = _setup()
    cond = BasketDelta(sku="SALSA-MILD", delta=1)
    base = await v.baseline(ROBOT, cond, baseline_id="b1")
    robot.pick("SALSA-MILD")
    robot.place()
    verdict = await v.verify(VerifyRequest(verify_id="post", robot_id=ROBOT, condition=cond, baseline=base))
    assert verdict.passed and verdict.atoms[0].observed["delta"] == 1
    none = await v.verify(VerifyRequest(verify_id="nobase", robot_id=ROBOT, condition=cond))
    assert none.outcome == "uncertain"


async def test_verifier_faults():
    services, robot, v = _setup()
    services.faults = ScriptedFaults({"verifier_unavailable": {}})  # type: ignore[assignment]
    with pytest.raises(VerifierUnavailableError):
        await v.verify(VerifyRequest(verify_id="f1", robot_id=ROBOT, condition=HOLD_MILD))
    with pytest.raises(VerifierUnavailableError):
        await v.baseline(ROBOT, HOLD_MILD, baseline_id="b")
    robot.pick("SALSA-MILD")
    services.faults = ScriptedFaults({"provider_down": {}})  # type: ignore[assignment]
    down = await v.verify(VerifyRequest(verify_id="f2", robot_id=ROBOT, condition=HOLD_MILD))
    assert down.passed and down.degraded and down.methods_tried == ["perceptron", "ground_truth"]
    services.faults = ScriptedFaults({"low_confidence": {"confidence": 0.3}})  # type: ignore[assignment]
    low = await v.verify(VerifyRequest(verify_id="f3", robot_id=ROBOT, condition=HOLD_MILD))
    assert low.outcome == "uncertain"


async def test_stale_frames_are_recaptured_and_never_pass():
    services, robot, v = _setup()
    robot.pick("SALSA-MILD")
    ev = services.evidence
    [old] = await ev.capture_refs(ROBOT, ["wrist"])  # type: ignore[union-attr]
    robot.place()  # the item left the gripper after that frame
    later = utcnow() + timedelta(seconds=1)
    # the given frame is older than not_before: re-captured (fresh frame shows the empty gripper)
    verdict = await v.verify(
        VerifyRequest(verify_id="s1", robot_id=ROBOT, condition=GripperEmpty(), frames=[old], not_before=later)
    )
    assert verdict.outcome == "uncertain" and verdict.reason.startswith("stale frame")
    fresh = await v.verify(VerifyRequest(verify_id="s2", robot_id=ROBOT, condition=GripperEmpty(), frames=[old]))
    assert fresh.outcome == "fail"  # judged on what THAT frame showed (still holding), not on truth now


async def test_ground_truth_grounding():
    services, robot, v = _setup()
    target = TargetSpec(sku="SALSA-MILD", slot_id=SLOT["SALSA-MILD"], description="Sol de Casa Mild Chunky Salsa")
    res = await v.ground(GroundRequest(grounding_id="g1", robot_id=ROBOT, target=target))
    assert res.found and res.target is not None and res.target.instance_id == f"{SLOT['SALSA-MILD']}#01"
    assert res.target.frame is not None and res.target.box is not None and res.target.decided_by == "ground_truth"
    empty = TargetSpec(sku="SALSA-MILD", slot_id=SLOT["LIMES"], description="salsa")
    miss = await v.ground(GroundRequest(grounding_id="g2", robot_id=ROBOT, target=empty))
    assert not miss.found and "no SALSA-MILD" in miss.reason


async def test_no_robot_no_truth_is_unavailable():
    services, _, v = _setup()
    services.robot = None
    with pytest.raises(VerifierUnavailableError):
        await v.verify(VerifyRequest(verify_id="x", robot_id=ROBOT, condition=HOLD_MILD))


async def test_capabilities_mode_and_providers():
    _, _, v = _setup()
    caps = await v.set_mode("gt_only")
    assert caps.mode == "gt_only" and caps.provider == "fake"
    [info] = await v.providers()
    assert info.provider == "fake" and info.available
