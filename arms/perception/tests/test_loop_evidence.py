"""Evidence cameras (contracts 0.2.1 ``EvidenceCameras``): fake synthetic frames + sidecars, the real retention ring
(aimed scans, CameraService snapshots, blur, gateway-clock re-stamp), camera faults and retention limits."""

import pytest

from arms.perception import create_evidence, create_evidence_fake
from arms.perception.evidence import EvidenceRing, FakeEvidenceCameras
from arms.perception.tests.loop_world import ROBOT, SLOT, ScriptedFaults, world
from contracts.common import utcnow
from contracts.evidence import EvidenceCameras
from contracts.sim import RobotStatus


def test_factories_satisfy_the_protocol():
    services, _ = world()
    assert isinstance(create_evidence(services), EvidenceCameras)
    assert isinstance(create_evidence_fake(services), EvidenceCameras)


async def test_fake_frames_are_blurred_retained_and_carry_exact_sidecars():
    services, robot = world()
    ev = FakeEvidenceCameras(services)
    robot.pick("SALSA-MILD")
    wrist, basket = await ev.capture_refs(ROBOT, ["wrist", "basket"])
    assert wrist.camera_id == f"{ROBOT}.wrist" and wrist.blurred is True
    assert wrist.url == f"/api/evidence/{wrist.frame_id}.jpg"
    frame = await ev.get_frame(wrist.frame_id)
    assert frame.blurred and frame.jpeg_b64
    sc = await ev.ground_truth(wrist.frame_id)
    assert sc.truth.holding is not None and sc.truth.holding.sku == "SALSA-MILD"
    assert sc.truth.holding.instance_id in sc.in_view and sc.synthetic
    assert sc.truth.labels[sc.truth.holding.instance_id] == "Sol de Casa Mild Chunky Salsa 16 oz"
    assert (await ev.ground_truth(basket.frame_id)).truth.basket == []
    with pytest.raises(KeyError):
        await ev.get_frame("nope")


async def test_fake_frame_ids_are_deterministic_per_truth():
    s1, _ = world()
    s2, _ = world()
    a = await FakeEvidenceCameras(s1).capture_refs(ROBOT, ["front"])
    b = await FakeEvidenceCameras(s2).capture_refs(ROBOT, ["front"])
    assert a[0].frame_id == b[0].frame_id


async def test_camera_faults_offline_occluded_stale():
    services, _ = world()
    ev = FakeEvidenceCameras(services)
    first = (await ev.capture_refs(ROBOT, ["basket"]))[0]
    services.faults = ScriptedFaults({"camera_offline": {}}, times=1)  # type: ignore[assignment]
    assert await ev.capture_refs(ROBOT, ["basket"]) == []
    services.faults = ScriptedFaults({"camera_occluded": {}}, times=1)  # type: ignore[assignment]
    occ = (await ev.capture_refs(ROBOT, ["basket"]))[0]
    sc = await ev.ground_truth(occ.frame_id)
    assert sc.occluded and f"{ROBOT}.basket" in sc.truth.occluded and sc.in_view == {}
    services.faults = ScriptedFaults({"stale_frame": {}}, times=1)  # type: ignore[assignment]
    stale = (await ev.capture_refs(ROBOT, ["basket"]))[0]
    assert stale.frame_id == occ.frame_id and stale.ts == occ.ts and stale.ts >= first.ts


async def test_ring_aims_front_with_a_scan_blurs_and_restamps_on_the_gateway_clock():
    services, robot = world(worker_clock_skew_s=5.0)  # the worker clock runs 5 s behind
    ev = EvidenceRing(services)
    before = utcnow()
    [ref] = await ev.capture_refs(ROBOT, ["front"], aim_slot_id=SLOT["SALSA-HOT"])
    assert robot.calls["scan"] == 1
    assert ref.ts >= before  # re-stamped: a skewed worker clock never makes fresh frames look stale
    frame = await ev.get_frame(ref.frame_id)
    assert frame.blurred and frame.slot_id == SLOT["SALSA-HOT"] and len(frame.instances) == 4  # kept WITH instances
    assert ref.frame_id.startswith(f"{ROBOT}.front:")
    sc = await ev.ground_truth(ref.frame_id)
    assert not sc.synthetic and set(sc.in_view) == {u.instance_id for u in frame.instances}
    assert SLOT["SALSA-HOT"] in sc.truth.slots


async def test_ring_falls_back_to_a_plain_capture_while_the_robot_moves():
    services, robot = world()
    robot.status = RobotStatus.NAVIGATING
    ev = EvidenceRing(services)
    [ref] = await ev.capture_refs(ROBOT, ["front"], aim_slot_id=SLOT["SALSA-HOT"])
    assert robot.calls["scan"] == 0 and robot.calls["capture:front"] == 1
    assert (await ev.get_frame(ref.frame_id)).blurred


async def test_ring_uses_camera_service_snapshots_and_drops_offline_placeholders():
    from arms.perception.cameras import FakeCameras

    services, robot = world()
    services.cameras = FakeCameras(services)  # type: ignore[assignment]
    ev = EvidenceRing(services)
    robot.pick("LIMES")
    [ref] = await ev.capture_refs(ROBOT, ["wrist"])
    frame = await ev.get_frame(ref.frame_id)
    assert frame.blurred and frame.instances and frame.instances[0].sku == "LIMES"
    services.robot = None  # camera offline -> CameraService placeholder -> left out
    assert await ev.capture_refs(ROBOT, ["wrist"]) == []


async def test_ring_pixelates_a_frame_nobody_can_certify():
    services, robot = world(backend="fake")
    ev = EvidenceRing(services)
    robot.backend = "unknown"  # not a rendered sim backend and no instances on a basket frame -> fail closed
    [ref] = await ev.capture_refs(ROBOT, ["basket"])
    frame = await ev.get_frame(ref.frame_id)
    assert frame.blurred  # pixelated whole frame (obscure_frame), never an unverified claim


async def test_retention_prunes_by_count_and_age():
    services, _ = world()
    ev = FakeEvidenceCameras(services, ring=3)
    refs = [(await ev.capture_refs(ROBOT, ["basket"]))[0] for _ in range(5)]
    assert ev.retained() == 3
    with pytest.raises(KeyError):
        await ev.get_frame(refs[0].frame_id)
    ev.max_age_s = 0.0
    with pytest.raises(KeyError):
        await ev.get_frame(refs[-1].frame_id)


async def test_capture_never_raises_when_the_robot_is_missing():
    services, _ = world()
    services.robot = None
    assert await EvidenceRing(services).capture_refs(ROBOT, ["front", "wrist"]) == []
    assert await FakeEvidenceCameras(services).capture_refs(ROBOT, ["front"]) == []
