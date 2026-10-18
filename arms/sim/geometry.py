"""Pure camera + rover geometry shared by the worker core, the Pillow sketch renderer and the Isaac backend.

Everything here is in the STORE frame (store-local meters, Z-up; contracts/planogram.py): the Isaac backend puts every
camera prim under its store's translation-only ``/World/Stores/<store_id>`` Xform, so a store-local camera pose is
also the prim's local transform and projected bboxes line up with what Isaac renders.

Camera convention (``CameraPose``): yaw CCW from +x, pitch > 0 looks up (= ``contracts.cameras.CameraMount``: pitch
< 0 = down). Pinhole intrinsics from ``hfov`` + image size. USD cameras look down local -Z with +Y up, so the Isaac
backend writes the matrix rows (right, up, -forward, eye).
"""

import math
from dataclasses import dataclass, replace
from typing import Any

from contracts.cameras import DEFAULT_ROBOT_CAMERA_MOUNTS, CameraMount
from contracts.common import ARM_MOUNT_X_M, LIFT_MAX_M, LIFT_MIN_M, BBox, Point3, Pose2D

Vec3 = tuple[float, float, float]

DEFAULT_FRAME_W = 640
DEFAULT_FRAME_H = 480
NEAR_CLIP_M = 0.05

# ---- SO-101 on the lift (robot-local, x forward, y left, z up). The lift carries the SO-101 shoulder
# (contracts.common LIFT_MIN_M..LIFT_MAX_M); the arm extends forward from the mast at ARM_MOUNT_X_M.
ARM_STOW_EXT_M = 0.12
"""Horizontal distance shoulder -> gripper TCP with the arm folded (driving / holding)."""
ARM_FULL_EXT_M = 0.33
"""Horizontal distance shoulder -> gripper TCP with the arm stretched to a shelf (SO-101 ~0.35 m reach)."""
LIFT_REST_M = 0.55
"""Lift height while driving (low center of mass, arm above the basket rim)."""
WRIST_CAM_PITCH = -0.35
"""Wrist camera pitch (looking slightly down at what the jaws approach)."""


@dataclass(frozen=True)
class CameraPose:
    """Pinhole camera in the store frame."""

    x: float
    y: float
    z: float
    yaw: float
    pitch: float = 0.0
    hfov: float = math.radians(70.0)
    w: int = DEFAULT_FRAME_W
    h: int = DEFAULT_FRAME_H

    def basis(self) -> tuple[Vec3, Vec3, Vec3]:
        """(right, up, forward) unit vectors in the store frame."""
        cy, sy, cp, sp = math.cos(self.yaw), math.sin(self.yaw), math.cos(self.pitch), math.sin(self.pitch)
        f = (cy * cp, sy * cp, sp)
        r = (sy, -cy, 0.0)
        u = (r[1] * f[2] - r[2] * f[1], r[2] * f[0] - r[0] * f[2], r[0] * f[1] - r[1] * f[0])
        return r, u, f

    @property
    def focal_px(self) -> float:
        return (self.w / 2) / math.tan(self.hfov / 2)

    def to_camera(self, p: Vec3) -> Vec3:
        """Store point -> camera coords (xc right, yc up, zc forward/depth)."""
        r, u, f = self.basis()
        v = (p[0] - self.x, p[1] - self.y, p[2] - self.z)
        return (
            v[0] * r[0] + v[1] * r[1] + v[2] * r[2],
            v[0] * u[0] + v[1] * u[1] + v[2] * u[2],
            v[0] * f[0] + v[1] * f[1] + v[2] * f[2],
        )

    def image_of(self, c: Vec3) -> tuple[float, float]:
        """Camera coords (zc > 0) -> pixel (u right, v down)."""
        fx = self.focal_px
        return self.w / 2 + fx * c[0] / c[2], self.h / 2 - fx * c[1] / c[2]

    def project(self, p: Vec3) -> tuple[float, float] | None:
        c = self.to_camera(p)
        if c[2] <= NEAR_CLIP_M:
            return None
        return self.image_of(c)

    def sized(self, w: int, h: int) -> "CameraPose":
        return replace(self, w=w, h=h)

    def to_mount(self) -> CameraMount:
        """``CameraFrame.camera_pose``: the pose actually used, frame='world' (= store frame)."""
        return CameraMount(
            frame="world",
            x=round(self.x, 4),
            y=round(self.y, 4),
            z=round(self.z, 4),
            yaw=round(self.yaw, 5),
            pitch=round(self.pitch, 5),
            hfov_deg=round(math.degrees(self.hfov), 3),
            width=self.w,
            height=self.h,
        )

    def usd_matrix_rows(self) -> tuple[tuple[float, float, float, float], ...]:
        """Row-vector 4x4 (Gf.Matrix4d order) of a USD camera at this pose: rows = right, up, -forward, eye."""
        r, u, f = self.basis()
        return (
            (r[0], r[1], r[2], 0.0),
            (u[0], u[1], u[2], 0.0),
            (-f[0], -f[1], -f[2], 0.0),
            (self.x, self.y, self.z, 1.0),
        )


def look_at(eye: Vec3, target: Vec3, **kw: Any) -> CameraPose:
    dx, dy, dz = target[0] - eye[0], target[1] - eye[1], target[2] - eye[2]
    return CameraPose(eye[0], eye[1], eye[2], math.atan2(dy, dx), math.atan2(dz, math.hypot(dx, dy)), **kw)


def to_store(pose: Pose2D, local: Vec3) -> Point3:
    """Robot-local offset (x forward, y left, z up) -> store-frame point."""
    c, s = math.cos(pose.theta), math.sin(pose.theta)
    return Point3(x=pose.x + c * local[0] - s * local[1], y=pose.y + s * local[0] + c * local[1], z=local[2])


def clamp_lift(z: float) -> float:
    return max(LIFT_MIN_M, min(LIFT_MAX_M, z))


def gripper_local(lift_height_m: float, arm_ext_m: float) -> Vec3:
    """Gripper TCP in the robot frame for a lift height + arm extension."""
    return (ARM_MOUNT_X_M + arm_ext_m, 0.0, lift_height_m)


def gripper_position(pose: Pose2D, lift_height_m: float = LIFT_REST_M, arm_ext_m: float = ARM_STOW_EXT_M) -> Point3:
    return to_store(pose, gripper_local(lift_height_m, arm_ext_m))


def mount_camera(
    mount: CameraMount,
    robot_pose: Pose2D | None = None,
    *,
    lift_height_m: float = LIFT_REST_M,
    arm_ext_m: float = ARM_STOW_EXT_M,
    aim_at: Point3 | None = None,
) -> CameraPose:
    """CameraPose of a mounted camera.

    ``frame='world'``: the mount as is (ceiling). ``'robot_base'``: rides the rover (``robot_pose`` required).
    ``'gripper'``: rides the SO-101 TCP (lift height + arm extension). ``aim_at`` turns the camera into a pan-tilt head
    looking at that point (the front camera for scans), keeping the mount position.
    """
    hfov = math.radians(mount.hfov_deg)
    if mount.frame == "world":
        eye: Vec3 = (mount.x, mount.y, mount.z)
        yaw, pitch = mount.yaw, mount.pitch
    else:
        if robot_pose is None:
            raise ValueError(f"camera mount frame {mount.frame!r} needs the robot pose")
        if mount.frame == "gripper":
            gx, gy, gz = gripper_local(lift_height_m, arm_ext_m)
            local = (gx + mount.x, gy + mount.y, gz + mount.z + 0.03)  # camera sits just above the jaws
            pitch = mount.pitch + WRIST_CAM_PITCH
        else:
            local = (mount.x, mount.y, mount.z)
            pitch = mount.pitch
        p = to_store(robot_pose, local)
        eye = (p.x, p.y, p.z)
        yaw = robot_pose.theta + mount.yaw
    if aim_at is not None:
        return look_at(eye, (aim_at.x, aim_at.y, aim_at.z), hfov=hfov, w=mount.width, h=mount.height)
    return CameraPose(eye[0], eye[1], eye[2], yaw, pitch, hfov, mount.width, mount.height)


def robot_camera_pose(
    kind: str,
    robot_pose: Pose2D,
    *,
    lift_height_m: float = LIFT_REST_M,
    arm_ext_m: float = ARM_STOW_EXT_M,
    aim_at: Point3 | None = None,
) -> CameraPose:
    """Pose of a robot camera ('front' / 'wrist' / 'basket') from DEFAULT_ROBOT_CAMERA_MOUNTS."""
    kind = "front" if kind == "head" else kind
    mount = DEFAULT_ROBOT_CAMERA_MOUNTS[kind]
    return mount_camera(mount, robot_pose, lift_height_m=lift_height_m, arm_ext_m=arm_ext_m, aim_at=aim_at)


def box_corners(center: Point3 | Vec3, dims: Vec3, yaw: float) -> list[Vec3]:
    cx, cy, cz = (center.x, center.y, center.z) if isinstance(center, Point3) else center
    c, s = math.cos(yaw), math.sin(yaw)
    out: list[Vec3] = []
    for sx in (-0.5, 0.5):
        for sy in (-0.5, 0.5):
            for sz in (-0.5, 0.5):
                lx, ly = sx * dims[0], sy * dims[1]
                out.append((cx + c * lx - s * ly, cy + s * lx + c * ly, cz + sz * dims[2]))
    return out


def project_box(cam: CameraPose, center: Point3 | Vec3, dims: Vec3, yaw: float) -> BBox | None:
    """Pixel bbox of an oriented box clipped to the image, or None when (partly) behind the camera / off-image."""
    px = [q for q in (cam.project(p) for p in box_corners(center, dims, yaw)) if q is not None]
    if len(px) < 8:
        return None
    x0, x1 = max(0.0, min(q[0] for q in px)), min(float(cam.w), max(q[0] for q in px))
    y0, y1 = max(0.0, min(q[1] for q in px)), min(float(cam.h), max(q[1] for q in px))
    if x1 - x0 < 1 or y1 - y0 < 1:
        return None
    return BBox(x=round(x0, 1), y=round(y0, 1), w=round(x1 - x0, 1), h=round(y1 - y0, 1))
