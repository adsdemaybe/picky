"""Joint-level SO-101 control for learned policies (contracts 0.2.2 ``ArmJointsCmd``): the two policy cameras and the
"sticky grasp". Pure python (no pxr / Isaac); ``worker.WorkerCore`` uses it with the rover's ``ArmKinematics``.

Policy cameras (LeRobot ``observation.images.side`` / ``observation.images.wrist``, 640x480 RGB, raw like every sim
frame; the backend blurs before a frame leaves it). They are EXACTLY the cameras of the dataset recorder
(``arms/sim/tools/record_lerobot/plan.py``: ``side_camera_matrix`` / ``wrist_camera_matrix``), so a policy trained on
those recordings sees the same views at inference: the mount constants are READ from that module when it is present
(``CAMERA_SOURCE``; the defaults below otherwise) and ``tests/test_arm_joints.py`` pins the math to its functions.

- ``side`` rides the lift carriage (never pans): eye ``SIDE_EYE`` behind-right of the SO-101 pan axis at the reference
  height (= the lift height), looking at ``SIDE_TARGET`` in the reach workspace, 72 deg hfov. LeIsaac's tabletop
  single-arm "front" camera (0.5 m IN FRONT of the arm, looking back) would sit inside the shelf here.
- ``wrist`` is rigid on the SO-101 ``gripper`` link (it rolls with the wrist): the mount is derived once per arm from a
  level reach at the 90 deg side-grasp roll, ``WRIST_CAM_BACK_M`` behind the TCP and ``WRIST_CAM_UP_M`` above it,
  looking ``WRIST_CAM_AHEAD_M`` past the jaw tips, 78 deg hfov. (LeIsaac's wrist offset looks straight down at that
  roll and never sees the target.)

Sticky grasp (physics substitute, logged as such): the worker runs no physics, so a grasp is decided geometrically.
When the gripper joint CROSSES below ``GRIPPER_CLOSE_DEG`` (closing) and a free unit (not in a basket, not held) is
within ``STICKY_RADIUS_M`` of the jaw center (distance to the unit's box, 0 inside it), that unit is attached rigidly
to the gripper link. Crossing back above ``GRIPPER_RELEASE_DEG`` (opening) releases it: into the basket when it is
over the basket, back to its shelf spot when within ``PUT_BACK_RADIUS_M`` of it, else onto the floor below.
"""

import math
from collections.abc import Mapping, Sequence

from arms.sim.geometry import CameraPose
from arms.sim.rover_puppet import (
    GRIPPER_CLOSED_DEG,
    WRIST_ROLL_DEG,
    ArmKinematics,
    Mat,
    Vec3,
    apply,
    chain_mul,
    inverse,
    mat_mul,
    position,
    rot_z,
    rotate,
    solve_reach,
    translation,
)
from contracts.common import Point3, Pose2D
from contracts.sim import BASKET_OFFSET_LOCAL, BASKET_SIZE_M

POLICY_W, POLICY_H = 640, 480
PAN_X_M = 0.16
"""Robot-frame x of the SO-101 pan axis (contracts ARM_MOUNT_X_M + rover_puppet.ARM_PAN_FORWARD_M)."""
SIDE_EYE: Vec3 = (-0.12, -0.30, 0.30)
"""Side camera eye relative to (pan axis x, 0, reference height): behind-right of the arm, above it."""
SIDE_TARGET: Vec3 = (0.27, 0.0, -0.03)
SIDE_HFOV = math.radians(72.0)
WRIST_CAM_BACK_M = 0.045
WRIST_CAM_UP_M = 0.10
WRIST_CAM_AHEAD_M = 0.10
WRIST_HFOV = math.radians(78.0)
try:  # the dataset recorder owns the camera tuning: inference must see exactly what it recorded
    from arms.sim.tools.record_lerobot import plan as _rec

    SIDE_EYE = tuple(float(v) for v in _rec.SIDE_EYE)  # type: ignore[assignment]
    SIDE_TARGET = tuple(float(v) for v in _rec.SIDE_TARGET)  # type: ignore[assignment]
    SIDE_HFOV = math.radians(float(_rec.SIDE_HFOV_DEG))
    WRIST_CAM_BACK_M = float(_rec.WRIST_CAM_BACK_M)
    WRIST_CAM_UP_M = float(_rec.WRIST_CAM_UP_M)
    WRIST_CAM_AHEAD_M = float(_rec.WRIST_CAM_AHEAD_M)
    WRIST_HFOV = math.radians(float(_rec.WRIST_HFOV_DEG))
    CAMERA_SOURCE = "arms.sim.tools.record_lerobot.plan"
except Exception:  # noqa: BLE001 - recorder not shipped / renamed: the defaults above (same values when written)
    CAMERA_SOURCE = "arms.sim.arm_joints defaults"
POLICY_NEAR_CLIP_M = 0.01
"""Near clip of both policy cameras (the Isaac backend applies it by camera id)."""
POLICY_CAMERA_PREFIX = "policy_"
"""Render-product ids of the policy cameras: '<robot_id>.policy_side' / '<robot_id>.policy_wrist' (not hosted)."""

GRIPPER_CLOSE_DEG = 10.0
"""Gripper joint (USD degrees) below which the jaws count as closed (LeIsaac SO-101: -10 = closed, ~40 = open; the
scripted rig uses 3 / 35)."""
GRIPPER_RELEASE_DEG = 20.0
"""Gripper joint above which a held unit is released (hysteresis against chatter around one threshold)."""
STICKY_RADIUS_M = 0.03
"""Jaw center to unit-box distance for the sticky grasp to attach."""
PUT_BACK_RADIUS_M = 0.10
"""A unit released this close to where it was grasped goes back to that exact spot (shelf)."""
BASKET_MARGIN_M = 0.03
BASKET_ABOVE_M = 0.35
"""'Over the basket' = inside the basket footprint (+ margin) and at most this far above its rim."""


def policy_camera_id(robot_id: str, kind: str) -> str:
    return f"{robot_id}.{POLICY_CAMERA_PREFIX}{kind}"


def store_matrix(pose: Pose2D) -> Mat:
    """Robot frame -> store frame (column vectors)."""
    return chain_mul(translation(pose.x, pose.y, 0.0), rot_z(pose.theta))


def look_at_matrix(eye: Sequence[float], target: Sequence[float], up: Vec3 = (0.0, 0.0, 1.0)) -> Mat:
    """USD camera transform at ``eye`` looking at ``target`` (columns: right, up, back, eye; looks down local -Z)."""
    f = [t - e for t, e in zip(target, eye, strict=True)]
    n = math.sqrt(sum(c * c for c in f)) or 1.0
    f = [c / n for c in f]
    r = [f[1] * up[2] - f[2] * up[1], f[2] * up[0] - f[0] * up[2], f[0] * up[1] - f[1] * up[0]]
    n = math.sqrt(sum(c * c for c in r)) or 1.0
    r = [c / n for c in r]
    u = [r[1] * f[2] - r[2] * f[1], r[2] * f[0] - r[0] * f[2], r[0] * f[1] - r[1] * f[0]]
    return (
        (r[0], u[0], -f[0], float(eye[0])),
        (r[1], u[1], -f[1], float(eye[1])),
        (r[2], u[2], -f[2], float(eye[2])),
        (0.0, 0.0, 0.0, 1.0),
    )


def camera_pose_of(usd_cam: Mat, hfov: float) -> CameraPose:
    """``CameraPose`` (store frame) of a USD camera transform (looks down its local -Z, +X = image right)."""
    return CameraPose.from_axes(
        position(usd_cam),
        rotate(usd_cam, (0.0, 0.0, -1.0)),
        rotate(usd_cam, (1.0, 0.0, 0.0)),
        hfov=hfov,
        w=POLICY_W,
        h=POLICY_H,
    )


def side_camera_matrix(kin: ArmKinematics, lift_height_m: float) -> Mat:
    """Side camera, ROBOT frame (USD convention): rides the lift with the arm, so every shelf level looks the same."""
    z0 = kin.ref_height(lift_height_m)
    eye = (PAN_X_M + SIDE_EYE[0], SIDE_EYE[1], z0 + SIDE_EYE[2])
    tgt = (PAN_X_M + SIDE_TARGET[0], SIDE_TARGET[1], z0 + SIDE_TARGET[2])
    return look_at_matrix(eye, tgt)


def wrist_mount(kin: ArmKinematics) -> Mat:
    """Wrist camera transform in the gripper LINK frame (fixed; cached on ``kin``), derived from a level reach at the
    side-grasp roll at zero lift travel (the recorder's ``plan.wrist_mount``)."""
    if kin.wrist_cam is not None:
        return kin.wrist_cam
    q0 = {"shoulder_pan": 0.0, "shoulder_lift": 0.3, "elbow_flex": -0.3, "wrist_flex": 0.0}
    q0["wrist_roll"] = math.radians(WRIST_ROLL_DEG)
    q0["gripper"] = math.radians(GRIPPER_CLOSED_DEG)
    base0 = kin.base_at_travel(0.0)
    target = apply(inverse(kin.model), (0.40, 0.0, kin.z_ref))
    q = solve_reach(kin.chain, target, q0=q0, base=base0, level_weight=0.5, iters=60).q
    poses = {k: mat_mul(kin.model, v) for k, v in kin.chain.fk(q, base0).items()}
    tcp = apply(kin.model, kin.chain.tcp_point(kin.chain.fk(q, base0)))
    wrist = position(poses["wrist"])
    f = [a - b for a, b in zip(tcp, wrist, strict=True)]
    n = math.sqrt(sum(c * c for c in f)) or 1.0
    f = [c / n for c in f]
    eye = (
        tcp[0] - WRIST_CAM_BACK_M * f[0],
        tcp[1] - WRIST_CAM_BACK_M * f[1],
        tcp[2] - WRIST_CAM_BACK_M * f[2] + WRIST_CAM_UP_M,
    )
    target_pt = (
        tcp[0] + WRIST_CAM_AHEAD_M * f[0],
        tcp[1] + WRIST_CAM_AHEAD_M * f[1],
        tcp[2] + WRIST_CAM_AHEAD_M * f[2],
    )
    kin.wrist_cam = mat_mul(inverse(poses[kin.chain.tcp_link]), look_at_matrix(eye, target_pt))
    return kin.wrist_cam


def wrist_camera_matrix(kin: ArmKinematics, lift_height_m: float, q: Mapping[str, float]) -> Mat:
    """Wrist camera, ROBOT frame (USD convention)."""
    return mat_mul(kin.robot_poses(lift_height_m, q)[kin.chain.tcp_link], wrist_mount(kin))


def side_camera_pose(kin: ArmKinematics, robot_pose: Pose2D, lift_height_m: float) -> CameraPose:
    return camera_pose_of(mat_mul(store_matrix(robot_pose), side_camera_matrix(kin, lift_height_m)), SIDE_HFOV)


def wrist_camera_pose(
    kin: ArmKinematics, robot_pose: Pose2D, lift_height_m: float, q: Mapping[str, float]
) -> CameraPose:
    return camera_pose_of(mat_mul(store_matrix(robot_pose), wrist_camera_matrix(kin, lift_height_m, q)), WRIST_HFOV)


def gripper_store_matrix(kin: ArmKinematics, robot_pose: Pose2D, lift_height_m: float, q: Mapping[str, float]) -> Mat:
    """Pose of the ``gripper`` link (the sticky grasp's carrier) in the store frame."""
    return chain_mul(store_matrix(robot_pose), kin.robot_poses(lift_height_m, q)[kin.chain.tcp_link])


def tcp_store(kin: ArmKinematics, robot_pose: Pose2D, lift_height_m: float, q: Mapping[str, float]) -> Point3:
    p = kin.tcp(lift_height_m, q)
    s = apply(store_matrix(robot_pose), p)
    return Point3(x=s[0], y=s[1], z=s[2])


def point_box_distance(p: Sequence[float], center: Sequence[float], dims: Sequence[float], yaw: float) -> float:
    """Distance from point ``p`` to an upright box (center, dims x/y/z, yaw about z); 0 inside it."""
    dx, dy, dz = p[0] - center[0], p[1] - center[1], p[2] - center[2]
    c, s = math.cos(-yaw), math.sin(-yaw)
    lx, ly = c * dx - s * dy, s * dx + c * dy
    ex = max(0.0, abs(lx) - dims[0] / 2)
    ey = max(0.0, abs(ly) - dims[1] / 2)
    ez = max(0.0, abs(dz) - dims[2] / 2)
    return math.sqrt(ex * ex + ey * ey + ez * ez)


def over_basket(robot_pose: Pose2D, point: Point3) -> bool:
    """True when ``point`` (store frame) is above the basket of the rover at ``robot_pose``."""
    c, s = math.cos(robot_pose.theta), math.sin(robot_pose.theta)
    dx, dy = point.x - robot_pose.x, point.y - robot_pose.y
    lx, ly = c * dx + s * dy, -s * dx + c * dy
    bx, by, bz = BASKET_OFFSET_LOCAL
    sx, sy, sz = BASKET_SIZE_M
    return (
        abs(lx - bx) <= sx / 2 + BASKET_MARGIN_M
        and abs(ly - by) <= sy / 2 + BASKET_MARGIN_M
        and bz <= point.z <= bz + sz + BASKET_ABOVE_M
    )


def crossed_closed(prev_deg: float, now_deg: float) -> bool:
    return prev_deg >= GRIPPER_CLOSE_DEG > now_deg


def crossed_open(prev_deg: float, now_deg: float) -> bool:
    return prev_deg <= GRIPPER_RELEASE_DEG < now_deg


__all__ = [
    "GRIPPER_CLOSE_DEG",
    "GRIPPER_RELEASE_DEG",
    "POLICY_H",
    "POLICY_NEAR_CLIP_M",
    "POLICY_W",
    "SIDE_HFOV",
    "STICKY_RADIUS_M",
    "WRIST_HFOV",
    "crossed_closed",
    "crossed_open",
    "gripper_store_matrix",
    "over_basket",
    "look_at_matrix",
    "point_box_distance",
    "policy_camera_id",
    "side_camera_matrix",
    "side_camera_pose",
    "store_matrix",
    "tcp_store",
    "wrist_camera_matrix",
    "wrist_camera_pose",
    "wrist_mount",
]
