import math

import pytest

from arms.robot.controllers.pure_pursuit import DONE, PurePursuit, PurePursuitConfig, simulate, wrap_angle


def _cross_track(p, a, b) -> float:
    (ax, ay), (bx, by) = a, b
    dx, dy = bx - ax, by - ay
    t = max(0.0, min(1.0, ((p[0] - ax) * dx + (p[1] - ay) * dy) / (dx * dx + dy * dy)))
    return math.hypot(p[0] - (ax + t * dx), p[1] - (ay + t * dy))


def test_straight_line_reaches_goal_and_final_yaw():
    cfg = PurePursuitConfig(max_speed_mps=0.8)
    trace = simulate([(0, 0, 0), (8, 0, math.pi / 2)], (0, 0, 0), cfg, dt=0.05)
    x, y, th = trace[-1]
    assert math.hypot(x - 8, y) <= cfg.goal_tolerance_m
    assert abs(wrap_angle(th - math.pi / 2)) <= cfg.yaw_tolerance_rad
    duration = (len(trace) - 1) * 0.05
    assert 8 / 0.8 <= duration < 8 / 0.8 + 6  # cruise at max speed + slow-down + final turn


def test_l_path_tracks_corridor_and_never_overspeeds():
    path = [(1, 1), (1, 6), (6, 6, 0.0)]
    cfg = PurePursuitConfig(max_speed_mps=0.8, lookahead_m=0.4)
    trace = simulate(path, (1, 1, math.pi / 2), cfg, dt=0.05)
    assert math.hypot(trace[-1][0] - 6, trace[-1][1] - 6) <= cfg.goal_tolerance_m
    for p, q in zip(trace, trace[1:], strict=False):
        assert math.hypot(q[0] - p[0], q[1] - p[1]) <= 0.8 * 0.05 + 1e-9
    worst = max(min(_cross_track(p, (1, 1), (1, 6)), _cross_track(p, (1, 6), (6, 6))) for p in trace)
    assert worst < 0.3  # corner cut bounded by the lookahead


def test_diff_drive_mode_reaches_goal():
    cfg = PurePursuitConfig(holonomic=False, max_speed_mps=0.8)
    trace = simulate([(0, 0), (3, 3), (6, 0, -math.pi / 2)], (0, 0, math.pi), cfg, dt=0.05)
    x, y, th = trace[-1]
    assert math.hypot(x - 6, y) <= cfg.goal_tolerance_m
    assert abs(wrap_angle(th + math.pi / 2)) <= cfg.yaw_tolerance_rad


def test_single_waypoint_rotate_in_place():
    pp = PurePursuit([(2, 2, math.pi)])
    pose = (2.0, 2.0, 0.0)
    for _ in range(400):
        pose = pp.step(pose, 0.05)
        if pp.done:
            break
    assert pp.phase == DONE
    assert pose[:2] == (2.0, 2.0)
    assert abs(wrap_angle(pose[2] - math.pi)) <= 0.05


def test_accepts_pose_objects_and_rejects_empty():
    from contracts.common import Pose2D

    trace = simulate([Pose2D(x=0, y=0), Pose2D(x=1, y=0, theta=0.0)], Pose2D(x=0, y=0))
    assert math.hypot(trace[-1][0] - 1, trace[-1][1]) <= 0.05
    with pytest.raises(ValueError):
        PurePursuit([])


def test_progress_is_monotonic_on_a_hairpin():
    # out and back along nearly the same line: projection must not jump to the return leg early
    path = [(0, 0), (5, 0), (5, 0.5), (0, 0.5, math.pi)]
    trace = simulate(path, (0, 0, 0), PurePursuitConfig(lookahead_m=0.3), dt=0.05)
    assert max(p[0] for p in trace) > 4.5
    assert math.hypot(trace[-1][0], trace[-1][1] - 0.5) <= 0.05
