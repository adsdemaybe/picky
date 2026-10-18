import math

import pytest

from arms.sim.geometry import (
    ARM_FULL_EXT_M,
    CameraPose,
    gripper_position,
    look_at,
    mount_camera,
    project_box,
    robot_camera_pose,
)
from arms.sim.labels import ean13_modules, label_image, label_lines, label_png
from arms.sim.navigation import PurePursuit
from contracts.cameras import DEFAULT_ROBOT_CAMERA_MOUNTS, CameraMount
from contracts.common import Point3, Pose2D


def follow(pp: PurePursuit, pose: Pose2D, dt: float = 0.05, max_steps: int = 20000) -> tuple[Pose2D, list[Pose2D]]:
    trace = [pose]
    for _ in range(max_steps):
        if pp.done:
            break
        pose = pp.step(pose, dt)
        trace.append(pose)
    return pose, trace


@pytest.mark.parametrize(
    "start,waypoints",
    [
        (Pose2D(x=0, y=0, theta=0), [Pose2D(x=5, y=0, theta=math.pi / 2)]),
        (Pose2D(x=0, y=0, theta=math.pi), [Pose2D(x=3, y=0, theta=0)]),  # starts facing away
        (Pose2D(x=1, y=1, theta=0), [Pose2D(x=1, y=6), Pose2D(x=4, y=6), Pose2D(x=4, y=2, theta=-math.pi / 2)]),
        (Pose2D(x=2, y=2, theta=1.0), [Pose2D(x=2.05, y=2.0, theta=3.0)]),  # basically a turn in place
    ],
)
def test_pure_pursuit_reaches_goal(start, waypoints):
    pp = PurePursuit(start, waypoints, speed_mps=0.8, tolerance_m=0.05, yaw_tolerance_rad=0.05)
    end, trace = follow(pp, start)
    goal = waypoints[-1]
    assert pp.done
    assert end.distance_to(goal) <= 0.05
    assert abs(math.remainder(end.theta - goal.theta, 2 * math.pi)) <= 0.05
    # speed limit respected
    for a, b in zip(trace, trace[1:], strict=False):
        assert a.distance_to(b) <= 0.8 * 0.05 + 1e-6


def test_pure_pursuit_tracks_corners_closely():
    start = Pose2D(x=1, y=1, theta=math.pi / 2)
    pts = [Pose2D(x=1, y=6), Pose2D(x=4, y=6)]
    pp = PurePursuit(start, pts)
    _, trace = follow(pp, start)

    def dist_to_path(p: Pose2D) -> float:
        d1 = abs(p.x - 1) if 1 <= p.y <= 6 else math.inf
        d2 = abs(p.y - 6) if 1 <= p.x <= 4 else math.inf
        return min(d1, d2, math.hypot(p.x - 1, p.y - 6))

    assert max(dist_to_path(p) for p in trace) < 0.3  # inside the 0.3 m inflation routing keeps free
    assert pp.length_m == pytest.approx(8.0)


def test_project_box_centered():
    cam = CameraPose(0, 0, 1, 0, 0)
    bbox = project_box(cam, Point3(x=2, y=0, z=1), (0.1, 0.1, 0.1), 0.0)
    assert bbox is not None
    assert bbox.x + bbox.w / 2 == pytest.approx(cam.w / 2, abs=1)
    assert bbox.y + bbox.h / 2 == pytest.approx(cam.h / 2, abs=1)
    assert project_box(cam, Point3(x=-2, y=0, z=1), (0.1, 0.1, 0.1), 0.0) is None  # behind


def test_look_at_and_usd_rows():
    cam = look_at((0, 0, 1.45), (0.4, 0, 0.1))
    assert cam.pitch < -1.0
    assert cam.project((0.4, 0, 0.1)) == pytest.approx((cam.w / 2, cam.h / 2), abs=1e-6)
    rows = cam.usd_matrix_rows()
    r, u, f = cam.basis()
    assert rows[2][:3] == tuple(-v for v in f) and rows[3][:3] == (cam.x, cam.y, cam.z)
    assert sum(a * b for a, b in zip(r, u, strict=True)) == pytest.approx(0)


def test_robot_cameras_follow_the_robot():
    pose = Pose2D(x=5, y=5, theta=math.pi / 2)
    front = robot_camera_pose("front", pose)
    m = DEFAULT_ROBOT_CAMERA_MOUNTS["front"]
    assert front.x == pytest.approx(5.0) and front.y == pytest.approx(5 + m.x) and front.z == pytest.approx(m.z)
    assert front.yaw == pytest.approx(math.pi / 2) and front.pitch == pytest.approx(m.pitch)
    basket = robot_camera_pose("basket", pose)
    assert basket.y < 5 and basket.pitch == pytest.approx(-1.5708)
    wrist = robot_camera_pose("wrist", pose, lift_height_m=1.0, arm_ext_m=ARM_FULL_EXT_M)
    g = gripper_position(pose, 1.0, ARM_FULL_EXT_M)
    assert wrist.x == pytest.approx(g.x) and wrist.y == pytest.approx(g.y) and wrist.z > 1.0
    aimed = robot_camera_pose("front", pose, aim_at=Point3(x=5, y=5.5, z=0.15))
    assert aimed.project((5, 5.5, 0.15)) == pytest.approx((aimed.w / 2, aimed.h / 2), abs=1e-6)
    ceiling = mount_camera(CameraMount(frame="world", x=1, y=2, z=3, yaw=0.5, pitch=-0.4))
    assert (ceiling.x, ceiling.y, ceiling.z, ceiling.yaw) == (1, 2, 3, 0.5)
    assert ceiling.to_mount().frame == "world"
    with pytest.raises(ValueError):
        mount_camera(DEFAULT_ROBOT_CAMERA_MOUNTS["front"])


def test_ean13_and_label_png():
    bits = ean13_modules("4006381333931")
    assert len(bits) == 95 and bits.startswith("101") and bits.endswith("101") and bits[45:50] == "01010"
    with pytest.raises(ValueError):
        ean13_modules("123")
    assert label_lines("Hot Salsa", "Casa Verde", "16 oz", 399) == ["CASA VERDE", "Hot Salsa", "16 oz", "$3.99"]
    img = label_image("Hot Salsa", brand="Casa Verde", size="16 oz", price_cents=399, barcode="4006381333931")
    assert img.size == (512, 384)
    png = label_png("Hot Salsa", barcode="4006381333931", color="#aa2222")
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
