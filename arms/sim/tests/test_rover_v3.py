"""Robot v3 (white SO-101 rover, ``picky_so101_rover.usd``) as the worker's visual puppet: model detection, the
sideways-mounted arm turned to face forward at pan 0, the fixed riser (no lift) and no primitives next to it."""

import json
import math
from pathlib import Path

import pytest

from arms.sim import rover_puppet as puppet
from arms.sim.scene_spec import build_rover_spec
from arms.sim.tests.sim_fixtures import robots


def test_model_detection_by_name_and_sidecar(tmp_path: Path):
    assert puppet.rover_model_for(None) is puppet.LEKIWI
    assert puppet.rover_model_for("/x/picky-rover/picky_rover.usda") is puppet.LEKIWI
    assert puppet.rover_model_for("/x/picky_so101_rover.usd") is puppet.SO101_ROVER_V3
    renamed = tmp_path / "white.usd"
    renamed.write_text("")
    assert puppet.rover_model_for(str(renamed)) is puppet.LEKIWI
    (tmp_path / "white.kin.json").write_text(json.dumps({"robot": "picky_so101_rover_v3"}))
    assert puppet.rover_model_for(str(renamed)) is puppet.SO101_ROVER_V3
    (tmp_path / "white.kin.json").write_text("not json")
    assert puppet.rover_model_for(str(renamed)) is puppet.LEKIWI


def test_v3_profile_maps_every_arm_link_and_has_no_lift():
    m = puppet.SO101_ROVER_V3
    assert m.fixed_lift and not m.primitives and m.align_pan
    assert m.link_map["gripper"] == "so101_gripper" and m.canonical("so101_jaw") == "jaw"
    assert m.canonical("base_link") == "base_link"  # the rover's own base passes through
    assert puppet.names_ok(["base_link", *m.links], m) and not puppet.names_ok(list(puppet.ARM_LINKS), m)
    assert puppet.names_ok(list(puppet.ARM_LINKS))  # LeKiwi default unchanged
    assert m.drops_m is not None and m.z_ref_m is not None
    # the reach table spans every lift height the worker asks for (contracts LIFT_MIN_M..LIFT_MAX_M = 0.2..1.2 m)
    assert m.z_ref_m - max(m.drops_m) <= 0.2 + 1e-9 and m.z_ref_m >= 1.2
    assert puppet.model_matrix(model=m) == puppet.IDENTITY  # origin on the floor, x forward: no offsets


def test_align_rest_pan_turns_the_arm_forward():
    chain = puppet.synthetic_kinematics().chain
    sideways = puppet.align_rest_pan(chain, math.radians(90.0))  # an arm that faces +y at pan 0
    assert puppet.forward_pan(sideways, puppet.IDENTITY) == pytest.approx(math.radians(-90.0))
    fwd = puppet.align_rest_pan(sideways, puppet.forward_pan(sideways, puppet.IDENTITY))
    q = {"shoulder_lift": 0.3, "elbow_flex": -0.4, "wrist_flex": 0.2}
    a, b = chain.fk(q), fwd.fk(q)
    for link in chain.links:
        assert puppet.position(a[link]) == pytest.approx(puppet.position(b[link]), abs=1e-9)
    rj, rj0 = fwd.root_joint, chain.root_joint
    assert rj is not None and rj0 is not None
    assert (rj.lower_deg, rj.upper_deg) == pytest.approx((rj0.lower_deg, rj0.upper_deg))


def test_fixed_lift_kinematics_follow_the_requested_height():
    kin = puppet.synthetic_kinematics()
    fixed = puppet.ArmKinematics(chain=kin.chain, z_ref=1.2, travel_m=0.0, lift_mode="offset")
    assert fixed.travel(0.3) == 0.0 and fixed.travel(1.2) == 0.0
    assert fixed.ref_height(0.35) == 0.35  # the policy side camera frames the shelf being reached
    lekiwi = puppet.ArmKinematics(chain=kin.chain, z_ref=0.3, lift_mode="offset")
    assert lekiwi.ref_height(0.8) == pytest.approx(0.8)  # unchanged inside the travel range


def test_v3_rover_spec_draws_no_primitives_next_to_the_usd(tmp_path: Path):
    usd = tmp_path / "picky_so101_rover.usd"
    usd.write_text("")
    spec = build_rover_spec(robots()[0], model_usd=str(usd))
    assert spec.model_path is not None and spec.model_path.endswith("/Model/Picky")
    assert spec.puppet_prims == []
    assert spec.prims  # the primitive rover stays complete as the fallback
    lekiwi = build_rover_spec(robots()[0], model_usd=str(tmp_path / "picky_rover.usda"))
    assert lekiwi.model_path is not None and lekiwi.model_path.endswith("/Model/LeKiwi") and lekiwi.puppet_prims
