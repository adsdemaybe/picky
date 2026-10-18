"""Pure helpers of the Perceptron VLA dataset recorder (arms/sim/tools/record_lerobot/plan.py)."""

import math

import pytest

from arms.sim.tools.record_lerobot import plan as P


def test_feature_names_match_perceptron_order():
    assert P.FEATURE_NAMES == [
        "shoulder_pan.pos",
        "shoulder_lift.pos",
        "elbow_flex.pos",
        "wrist_flex.pos",
        "wrist_roll.pos",
        "gripper.pos",
    ]


@pytest.mark.parametrize("deg", [-10.0, 3.0, 35.0, 100.0])
def test_gripper_maps_leisaac_usd_range_to_0_100(deg):
    v = P.gripper_to_lerobot(deg)
    assert 0.0 <= v <= 100.0
    assert P.gripper_from_lerobot(v) == pytest.approx(deg)
    assert P.gripper_to_lerobot(-10.0) == 0.0 and P.gripper_to_lerobot(100.0) == 100.0


def test_lerobot_round_trip_arm_in_degrees():
    q = {"shoulder_pan": 0.2, "shoulder_lift": -1.2, "elbow_flex": 1.0, "wrist_flex": 0.3, "wrist_roll": 1.5708}
    q["gripper"] = math.radians(35.0)
    vals = P.to_lerobot(q)
    assert vals[0] == pytest.approx(math.degrees(0.2), abs=1e-3)
    assert vals[4] == pytest.approx(90.0, abs=1e-2)
    back = P.from_lerobot(vals)
    for k, v in q.items():
        assert back[k] == pytest.approx(v, abs=1e-4)


def test_min_jerk_is_smooth_and_bounded():
    assert P.min_jerk(0.0) == 0.0 and P.min_jerk(1.0) == 1.0
    assert P.min_jerk(0.5) == pytest.approx(0.5)
    eps = 1e-4
    assert (P.min_jerk(eps) - P.min_jerk(0.0)) / eps < 1e-3  # zero start velocity
    vals = [P.min_jerk(i / 50) for i in range(51)]
    assert all(b >= a for a, b in zip(vals, vals[1:], strict=False))


def test_task_string_and_frame_count():
    spec = P.EpisodeSpec(0, "S", "SALSA-MILD", "Mild Salsa", "i", (0.4, 0.0, 0.7), (0.08, 0.08, 0.13))
    assert spec.task == "pick up the mild salsa and put it in the basket"
    assert P.n_frames(1.0) == 30


def test_look_at_matrix_points_the_camera_down_minus_z():
    m = P.look_at_matrix((0.0, 0.0, 1.0), (1.0, 0.0, 1.0))
    fwd = (-m[0][2], -m[1][2], -m[2][2])
    up = (m[0][1], m[1][1], m[2][1])
    assert fwd == pytest.approx((1.0, 0.0, 0.0), abs=1e-9)
    assert up == pytest.approx((0.0, 0.0, 1.0), abs=1e-9)
