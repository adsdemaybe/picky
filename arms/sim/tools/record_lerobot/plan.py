"""Scripted shelf-pick demonstrations for the Perceptron VLA dataset: pure-python planning (no Isaac, no numpy).

One episode = the rover parked at a slot's approach pose (small pose noise), the lift fixed at the height that puts the
target unit within reach (the lift is NOT one of the 6 action values, so it must not move during an episode), and the
SO-101 playing a smooth joint-space trajectory:

    start (folded, noisy) -> pre-grasp (open, 8 cm in front of the unit) -> grasp (Cartesian line) -> close
    -> lift 3 cm -> retract -> over the basket (arm folded back over the rover) -> release (unit drops) -> hold

Frames are planned at ``FPS``. ``state[t]`` = the 6 joint values at frame t in LeRobot SO-101 units, ``action[t]`` =
``state[t + 1]`` (the next frame's joint targets; the last frame repeats its own state).

Units (Perceptron checklist = LeRobot SO-101 follower, ``use_degrees=True``): arm joints in degrees, gripper 0-100. The
rover USD is LeIsaac's ``lekiwi.usd``, whose SO-101 joint zero is the calibrated mid pose, so arm degrees are the USD
joint angles as is; the gripper maps LeIsaac's USD range (-10..100 deg) linearly onto 0..100, exactly like
``leisaac.utils.robot_utils.convert_leisaac_action_to_lerobot``.

Frames: ``robot`` = rover base frame (x forward, y left, z up, origin on the floor under the base center), ``model``
= the referenced ``/LeKiwi`` frame (``rover_puppet``). ``geo`` is the dict ``IsaacBackend._puppet_geometry`` returns
(``chain``, ``M``, ``dx``, ``z_ref``).
"""

import math
import random
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from arms.sim import rover_puppet as rp
from contracts.sim import BASKET_OFFSET_LOCAL

FPS = 30
JOINTS: tuple[str, ...] = ("shoulder_pan", "shoulder_lift", "elbow_flex", "wrist_flex", "wrist_roll", "gripper")
FEATURE_NAMES: list[str] = [f"{j}.pos" for j in JOINTS]
"""observation.state / action names, in Perceptron's required order."""
GRIPPER_USD_RANGE_DEG = (-10.0, 100.0)
"""LeIsaac SO101_FOLLOWER_USD_JOINT_LIMLITS['gripper'] (lekiwi.usd authors the same limits)."""
ARM_LIMITS_DEG: dict[str, tuple[float, float]] = {
    "shoulder_pan": (-110.0, 110.0),
    "shoulder_lift": (-100.0, 100.0),
    "elbow_flex": (-100.0, 90.0),
    "wrist_flex": (-95.0, 95.0),
    "wrist_roll": (-160.0, 160.0),
}
REST_DEG: dict[str, float] = {"shoulder_pan": 0.0, "shoulder_lift": -100.0, "elbow_flex": 90.0, "wrist_flex": 50.0}
"""LeIsaac's SO-101 rest pose (folded, gripper forward/down)."""
BASKET_DEG: dict[str, float] = {"shoulder_lift": -100.0, "elbow_flex": -85.0, "wrist_flex": -5.0}
"""Arm folded back over the rover: the TCP ends up above the basket (x ~ -0.2 m in the robot frame). The pan is set per
episode (BASKET_PAN_DEG, sign random) so the upper arm passes beside the mast column."""
BASKET_PAN_DEG = (14.0, 22.0)
GRIP_SQUEEZE_M = 0.004
"""Jaw tip gap = unit width - this when closed on a unit."""
OPEN_MARGIN_M = 0.035
"""Jaw tip gap while approaching = unit width + this."""
PREGRASP_BACK_M = 0.08
GRASP_DEPTH_M = 0.03
"""The TCP stops this far behind the unit's front face (or at its center for thin units)."""
LIFT_UP_M = 0.03
RETRACT_X_M = 0.27
"""Robot-frame x of the TCP after retracting (the shelf front is at ~0.375 m)."""
GRAVITY = 9.81


# ---- unit conversion -----------------------------------------------------------------------------------------------
def gripper_to_lerobot(deg: float) -> float:
    lo, hi = GRIPPER_USD_RANGE_DEG
    return (deg - lo) / (hi - lo) * 100.0


def gripper_from_lerobot(value: float) -> float:
    lo, hi = GRIPPER_USD_RANGE_DEG
    return lo + value / 100.0 * (hi - lo)


def to_lerobot(q_rad: Mapping[str, float]) -> list[float]:
    """Joint angles (radians, USD) -> the 6 LeRobot values in FEATURE_NAMES order."""
    out = [math.degrees(q_rad.get(j, 0.0)) for j in JOINTS[:5]]
    out.append(gripper_to_lerobot(math.degrees(q_rad.get("gripper", 0.0))))
    return [round(v, 4) for v in out]


def from_lerobot(values: Sequence[float]) -> dict[str, float]:
    """The 6 LeRobot values -> joint angles (radians, USD)."""
    q = {j: math.radians(float(v)) for j, v in zip(JOINTS[:5], values[:5], strict=True)}
    q["gripper"] = math.radians(gripper_from_lerobot(float(values[5])))
    return q


# ---- smooth interpolation ------------------------------------------------------------------------------------------
def min_jerk(s: float) -> float:
    """Minimum-jerk time scaling: 0 -> 1 with zero velocity + acceleration at both ends."""
    s = max(0.0, min(1.0, s))
    return s * s * s * (10.0 - 15.0 * s + 6.0 * s * s)


def lerp_q(a: Mapping[str, float], b: Mapping[str, float], t: float) -> dict[str, float]:
    return {k: a.get(k, 0.0) + t * (b.get(k, 0.0) - a.get(k, 0.0)) for k in set(a) | set(b)}


def n_frames(seconds: float, fps: int = FPS) -> int:
    return max(1, round(seconds * fps))


# ---- episode spec + result -----------------------------------------------------------------------------------------
@dataclass
class EpisodeSpec:
    episode_index: int
    slot_id: str
    sku: str
    product_name: str
    instance_id: str
    unit_robot: tuple[float, float, float]
    """Unit center in the robot frame."""
    unit_dims: tuple[float, float, float]
    """(width across the jaws, depth along the reach, height)."""
    unit_yaw_rel: float = 0.0
    """Unit yaw relative to the robot heading (store yaw - robot theta)."""
    seed: int = 0
    basket_pan_deg: float = 18.0
    speed: float = 1.0
    """Duration scale (> 1 = slower)."""
    start_noise_deg: dict[str, float] = field(default_factory=dict)
    grasp_dz_m: float = 0.0
    """Grasp height offset from the unit center."""

    @property
    def task(self) -> str:
        return f"pick up the {self.product_name.lower()} and put it in the basket"


@dataclass
class EpisodePlan:
    spec: EpisodeSpec
    lift_dz: float
    """Lift carriage travel for the whole episode (rover_puppet.lift_offset)."""
    drop: float
    q: list[dict[str, float]]
    """Per frame joint angles (radians, USD)."""
    unit: list[tuple[tuple[float, float, float], float] | None]
    """Per frame unit pose in the robot frame (center, yaw_rel) once it moves; None = still on the shelf."""
    phases: list[str]
    grasp_frame: int
    release_frame: int
    max_ik_error_m: float

    @property
    def states(self) -> list[list[float]]:
        return [to_lerobot(q) for q in self.q]

    @property
    def actions(self) -> list[list[float]]:
        s = self.states
        return s[1:] + s[-1:]


# ---- the planner ---------------------------------------------------------------------------------------------------
class PickPlanner:
    """IK + kinematics of the rover's SO-101 puppet in the ROBOT frame."""

    def __init__(self, geo: Mapping[str, Any], jaw_points: Mapping[str, Sequence[Sequence[float]]] | None = None):
        self.chain: rp.ArmChain = geo["chain"]
        self.M: rp.Mat = geo["M"]
        self.m_inv = rp.inverse(self.M)
        self.dx: float = geo["dx"]
        self.z_ref: float = geo["z_ref"]
        self._tips: tuple[rp.Vec3, rp.Vec3] | None = None
        self.wrist_mount: rp.Mat | None = None
        if jaw_points:
            self._tips = self._select_tips(jaw_points)

    def _clamp(self, name: str, q_rad: float) -> float:
        j = self.chain.joint(name)
        return j.clamp(q_rad) if j is not None else q_rad

    # -- frames
    def base(self, dz: float) -> rp.Mat:
        return rp.robot_delta_in_model(self.M, (self.dx, 0.0, dz))

    def poses(self, q: Mapping[str, float], dz: float) -> dict[str, rp.Mat]:
        """Link poses in the ROBOT frame."""
        return {k: rp.mat_mul(self.M, v) for k, v in self.chain.fk(q, self.base(dz)).items()}

    def model_poses(self, q: Mapping[str, float], dz: float) -> dict[str, rp.Mat]:
        """Link poses in the /LeKiwi (model) frame: what the puppet's xformOps get."""
        return self.chain.fk(q, self.base(dz))

    def tcp(self, q: Mapping[str, float], dz: float) -> rp.Vec3:
        return rp.apply(self.M, self.chain.tcp_point(self.chain.fk(q, self.base(dz))))

    def ik(self, target_robot: rp.Vec3, dz: float, q0: Mapping[str, float]) -> rp.ReachResult:
        res = rp.solve_reach(
            self.chain, rp.apply(self.m_inv, target_robot), q0=q0, base=self.base(dz), level_weight=0.5, iters=60
        )
        return res

    # -- jaws
    def _select_tips(self, pts: Mapping[str, Sequence[Sequence[float]]]) -> tuple[rp.Vec3, rp.Vec3]:
        """Fixed + moving jaw tip (centroid of the points within 8 mm of each finger end), in the gripper / jaw link
        frames, picked at the closed pose (same rule as IsaacBackend._build_puppet_geometry)."""
        closed = {"gripper": math.radians(rp.GRIPPER_CLOSED_DEG)}
        P = self.chain.fk(closed)
        fwd = rp.rotate(self.m_inv, (1.0, 0.0, 0.0))

        def tip(link: str) -> rp.Vec3:
            model = [rp.apply(P[link], p) for p in pts[link]]
            dots = [sum(a * b for a, b in zip(p, fwd, strict=True)) for p in model]
            far = max(dots)
            sel = [p for p, d in zip(pts[link], dots, strict=True) if d > far - 0.008]
            return tuple(sum(float(p[i]) for p in sel) / len(sel) for i in range(3))  # type: ignore[return-value]

        return tip("gripper"), tip("jaw")

    def jaw_gap(self, gripper_rad: float) -> float:
        """Distance between the two jaw tips at this gripper angle (0 when no jaw points were given)."""
        if self._tips is None:
            return 0.0
        P = self.chain.fk({"gripper": gripper_rad})
        a = rp.apply(P["gripper"], self._tips[0])
        b = rp.apply(P["jaw"], self._tips[1])
        return math.dist(a, b)

    def gripper_for_gap(self, gap: float, lo_deg: float = rp.GRIPPER_CLOSED_DEG, hi_deg: float = 95.0) -> float:
        """Gripper angle (radians) whose jaw tip gap is ``gap`` (bisection; the gap grows with the angle)."""
        if self._tips is None:
            return math.radians(lo_deg + (hi_deg - lo_deg) * max(0.0, min(1.0, gap / 0.12)))
        lo, hi = math.radians(lo_deg), math.radians(hi_deg)
        if self.jaw_gap(lo) >= gap:
            return lo
        if self.jaw_gap(hi) <= gap:
            return hi
        for _ in range(40):
            mid = (lo + hi) / 2
            if self.jaw_gap(mid) < gap:
                lo = mid
            else:
                hi = mid
        return (lo + hi) / 2

    # -- episode
    def lift_for(self, unit_z: float) -> tuple[float, float]:
        """(lift travel, drop below the reference height) that put a level TCP at ``unit_z``."""
        dz = rp.lift_offset(unit_z, self.z_ref)
        return dz, max(0.0, self.z_ref + dz - unit_z)

    def plan(self, spec: EpisodeSpec, fps: int = FPS) -> EpisodePlan:
        rng = random.Random(spec.seed)
        ux, uy, uz = spec.unit_robot
        w, d, h = spec.unit_dims
        gz = uz + spec.grasp_dz_m
        dz, drop = self.lift_for(gz)
        roll = math.radians(rp.WRIST_ROLL_DEG)
        g_open = max(self.gripper_for_gap(w + OPEN_MARGIN_M), math.radians(rp.GRIPPER_OPEN_DEG))
        g_closed = self.gripper_for_gap(max(0.01, w - GRIP_SQUEEZE_M))
        g_start = math.radians(rp.GRIPPER_CLOSED_DEG + rng.uniform(0.0, 8.0))

        start = {k: math.radians(v + spec.start_noise_deg.get(k, 0.0)) for k, v in REST_DEG.items()}
        start["wrist_roll"] = math.radians(rng.uniform(0.0, 90.0))
        start["gripper"] = g_start

        front = ux - d / 2
        grasp_x = front + min(d / 2, GRASP_DEPTH_M)
        p_grasp = (grasp_x, uy, gz)
        p_pre = (front - PREGRASP_BACK_M, uy, gz)
        p_up = (grasp_x, uy, gz + LIFT_UP_M)
        p_back = (min(RETRACT_X_M, front - 0.10), uy * 0.5, gz + LIFT_UP_M)

        errs: list[float] = []
        seed_q = {"shoulder_pan": math.atan2(uy, max(0.05, ux - 0.16)), "wrist_roll": roll, "gripper": g_open}
        # seed the IK from a stretched-forward config so it stays on the elbow-up branch
        seed_q.update({"shoulder_lift": 0.3, "elbow_flex": -0.3, "wrist_flex": 0.0})
        r_pre = self.ik(p_pre, dz, seed_q)
        errs.append(r_pre.error_m)
        q_pre = dict(r_pre.q)

        qs: list[dict[str, float]] = []
        unit: list[tuple[tuple[float, float, float], float] | None] = []
        phases: list[str] = []

        def add(q: Mapping[str, float], phase: str) -> None:
            # the puppet's FK clamps every joint to its USD limit, so the recorded state must be clamped the same way
            # (the rest pose sits AT the shoulder_lift / elbow limits and the start noise would cross them)
            qs.append({k: self._clamp(k, v) for k, v in q.items()})
            phases.append(phase)
            unit.append(None)

        def joint_seg(a: Mapping[str, float], b: Mapping[str, float], seconds: float, phase: str) -> None:
            n = n_frames(seconds * spec.speed, fps)
            for i in range(1, n + 1):
                add(lerp_q(a, b, min_jerk(i / n)), phase)

        def cart_seg(
            a: rp.Vec3, b: rp.Vec3, q0: Mapping[str, float], seconds: float, phase: str, g: float
        ) -> dict[str, float]:
            n = n_frames(seconds * spec.speed, fps)
            q = dict(q0)
            for i in range(1, n + 1):
                s = min_jerk(i / n)
                p = (a[0] + s * (b[0] - a[0]), a[1] + s * (b[1] - a[1]), a[2] + s * (b[2] - a[2]))
                q["gripper"] = g
                res = self.ik(p, dz, q)
                errs.append(res.error_m)
                q = dict(res.q)
                add(q, phase)
            return q

        # 1) hold, 2) start -> pre-grasp (joint space; roll to 90 deg, jaws open)
        for _ in range(n_frames(0.3, fps)):
            add(start, "start")
        joint_seg(start, {**q_pre, "wrist_roll": roll, "gripper": g_open}, 1.7, "approach")
        # 3) pre-grasp -> grasp (straight line)
        q = cart_seg(p_pre, p_grasp, qs[-1], 1.0, "reach", g_open)
        # 4) close
        q_close = {**q, "gripper": g_closed}
        joint_seg(q, q_close, 0.5, "close")
        grasp_frame = len(qs) - 1
        G0 = self.poses(qs[grasp_frame], dz)["gripper"]
        rel = rp.apply(rp.inverse(G0), spec.unit_robot)
        pan0 = qs[grasp_frame].get("shoulder_pan", 0.0)
        # 5) lift, 6) retract
        q = cart_seg(p_grasp, p_up, q_close, 0.45, "lift", g_closed)
        q = cart_seg(p_up, p_back, q, 0.9, "retract", g_closed)
        # 7) over the basket (joint space)
        q_basket = {k: math.radians(v + rng.uniform(-4.0, 4.0)) for k, v in BASKET_DEG.items()}
        q_basket["shoulder_pan"] = math.radians(spec.basket_pan_deg)
        q_basket["wrist_roll"] = roll
        q_basket["gripper"] = g_closed
        joint_seg(q, q_basket, 1.6, "to_basket")
        # 8) release: jaws open, the unit drops into the basket
        release_frame = len(qs)
        joint_seg(q_basket, {**q_basket, "gripper": g_open}, 0.45, "release")
        # 9) hold
        for _ in range(n_frames(0.5, fps)):
            add(qs[-1], "hold")

        # unit trajectory: rides the gripper from the grasp to the release, then falls into the basket
        for i in range(grasp_frame + 1, release_frame + 1):
            G = self.poses(qs[i], dz)["gripper"]
            unit[i] = (rp.apply(G, rel), spec.unit_yaw_rel + qs[i].get("shoulder_pan", 0.0) - pan0)
        p_rel, yaw_rel = unit[release_frame] or ((0.0, 0.0, 0.0), 0.0)
        bx, by, bz = BASKET_OFFSET_LOCAL
        target = (
            max(bx - 0.06, min(bx + 0.06, p_rel[0])),
            max(by - 0.09, min(by + 0.09, p_rel[1])),
            bz + h / 2 + 0.005,
        )
        fall = max(0.0, p_rel[2] - target[2])
        t_fall = math.sqrt(2 * fall / GRAVITY) if fall > 0 else 0.0
        for i in range(release_frame + 1, len(qs)):
            t = (i - release_frame) / fps
            s = min(1.0, t / t_fall) if t_fall > 0 else 1.0
            z = max(target[2], p_rel[2] - 0.5 * GRAVITY * t * t)
            unit[i] = (
                (p_rel[0] + s * (target[0] - p_rel[0]), p_rel[1] + s * (target[1] - p_rel[1]), z),
                yaw_rel,
            )
        return EpisodePlan(
            spec=spec,
            lift_dz=dz,
            drop=drop,
            q=qs,
            unit=unit,
            phases=phases,
            grasp_frame=grasp_frame,
            release_frame=release_frame,
            max_ik_error_m=max(errs) if errs else 0.0,
        )


# ---- cameras -------------------------------------------------------------------------------------------------------
WRIST_CAM_BACK_M = 0.045
WRIST_CAM_UP_M = 0.10
WRIST_CAM_AHEAD_M = 0.10
"""Wrist camera: rigidly mounted on the gripper link, above the fixed jaw (jaw-plane normal side at the 90 deg
side-grasp roll), WRIST_CAM_BACK_M behind the TCP and just above the gripper servo housing (which stands ~8.3 cm
above the TCP 3-6 cm behind it), looking at a point WRIST_CAM_AHEAD_M past the TCP (~35 deg down). It stays outside
the target unit at the grasp (the TCP is GRASP_DEPTH_M inside the front face). LeIsaac's LeKiwi offset
(pos (-0.001, 0.1, -0.04)) looks straight down at the 90 deg roll our side grasps use, so it never saw the target;
this mount keeps the jaw tips at the bottom edge of the frame and the unit ahead of them."""
WRIST_HFOV_DEG = 78.0
WRIST_H_APERTURE_MM = 20.955
WRIST_FOCAL_MM = WRIST_H_APERTURE_MM / (2 * math.tan(math.radians(WRIST_HFOV_DEG) / 2))
SIDE_EYE = (-0.12, -0.30, 0.30)
"""Side camera eye relative to the SO-101 pan axis at the reference height (rides the lift carriage): behind-right of
the arm, above it. LeIsaac's tabletop "front" camera (0.5 m in front of the arm) would sit inside the shelf here."""
SIDE_TARGET = (0.27, 0.0, -0.03)
SIDE_HFOV_DEG = 72.0


def wrist_mount(planner: PickPlanner) -> rp.Mat:
    """Camera transform in the gripper LINK frame (fixed), derived once from a level reach at the side-grasp roll."""
    q0 = {"shoulder_pan": 0.0, "shoulder_lift": 0.3, "elbow_flex": -0.3, "wrist_flex": 0.0}
    q0["wrist_roll"] = math.radians(rp.WRIST_ROLL_DEG)
    q0["gripper"] = math.radians(rp.GRIPPER_CLOSED_DEG)
    q = planner.ik((0.40, 0.0, planner.z_ref), 0.0, q0).q
    poses = planner.poses(q, 0.0)
    tcp = planner.tcp(q, 0.0)
    wrist = rp.position(poses["wrist"])
    f = [a - b for a, b in zip(tcp, wrist, strict=True)]
    n = math.sqrt(sum(c * c for c in f)) or 1.0
    f = [c / n for c in f]
    eye = (
        tcp[0] - WRIST_CAM_BACK_M * f[0],
        tcp[1] - WRIST_CAM_BACK_M * f[1],
        tcp[2] - WRIST_CAM_BACK_M * f[2] + WRIST_CAM_UP_M,
    )
    target = (tcp[0] + WRIST_CAM_AHEAD_M * f[0], tcp[1] + WRIST_CAM_AHEAD_M * f[1], tcp[2] + WRIST_CAM_AHEAD_M * f[2])
    return rp.mat_mul(rp.inverse(poses["gripper"]), look_at_matrix(eye, target))


def wrist_camera_matrix(planner: PickPlanner, q: Mapping[str, float], dz: float) -> rp.Mat:
    """USD camera transform (column vectors; camera looks down local -Z) in the ROBOT frame."""
    if planner.wrist_mount is None:
        planner.wrist_mount = wrist_mount(planner)
    return rp.mat_mul(planner.poses(q, dz)["gripper"], planner.wrist_mount)


def look_at_matrix(eye: rp.Vec3, target: rp.Vec3, up: rp.Vec3 = (0.0, 0.0, 1.0)) -> rp.Mat:
    """USD camera transform at ``eye`` looking at ``target`` (columns: right, up, back, eye)."""
    f = [t - e for t, e in zip(target, eye, strict=True)]
    n = math.sqrt(sum(c * c for c in f)) or 1.0
    f = [c / n for c in f]
    r = [f[1] * up[2] - f[2] * up[1], f[2] * up[0] - f[0] * up[2], f[0] * up[1] - f[1] * up[0]]
    n = math.sqrt(sum(c * c for c in r)) or 1.0
    r = [c / n for c in r]
    u = [r[1] * f[2] - r[2] * f[1], r[2] * f[0] - r[0] * f[2], r[0] * f[1] - r[1] * f[0]]
    b = [-c for c in f]
    return (
        (r[0], u[0], b[0], eye[0]),
        (r[1], u[1], b[1], eye[1]),
        (r[2], u[2], b[2], eye[2]),
        (0.0, 0.0, 0.0, 1.0),
    )


def side_camera_matrix(planner: PickPlanner, dz: float) -> rp.Mat:
    """Side camera (robot frame): rides the lift with the arm, so every shelf level is framed the same way."""
    pan_x = 0.16  # contracts ARM_MOUNT_X_M + rover_puppet.ARM_PAN_FORWARD_M
    z0 = planner.z_ref + dz
    eye = (pan_x + SIDE_EYE[0], SIDE_EYE[1], z0 + SIDE_EYE[2])
    tgt = (pan_x + SIDE_TARGET[0], SIDE_TARGET[1], z0 + SIDE_TARGET[2])
    return look_at_matrix(eye, tgt)


def robot_to_store(x: float, y: float, theta: float) -> rp.Mat:
    return rp.mat_mul(rp.translation(x, y, 0.0), rp.rot_z(theta))
