"""Kinematic "visual puppet" of the LeKiwi + SO-101 rover USD (``PICKY_ROVER_USD`` -> ``picky_rover.usda``).

PURE python (stdlib only, no pxr / pydantic): the Isaac backend reads the rest transforms + joint frames of the SO-101
links from the composed USD once per rover, hands them to ``ArmChain`` and then only writes the matrices this module
returns. The worker runs NO physics, so the articulation is never simulated: the backend deactivates the joints, makes
every link kinematic, and each moving SO-101 link gets one ``xformOp:transform:puppet`` per pose.

Model (``picky_rover.usda`` = LeIsaac ``lekiwi.usd`` + Picky overlay): ``/LeKiwi`` is a flat articulation, every link
(``base``, ``shoulder``, ``upper_arm``, ``lower_arm``, ``wrist``, ``gripper``, ``jaw``, ...) is a direct child of
``/LeKiwi`` with its rest pose authored in the ``/LeKiwi`` frame (Z up; the arm reaches along -Y of the world, i.e.
the robot heading needs ``+pi/2``: ``customLayerData.picky.heading_offset_rad``).

Math convention: column vectors, ``Mat`` = 4 rows of 4 floats, ``p' = M . p``. ``Gf.Matrix4d`` is the transpose
(row vectors); the backend converts with ``transpose``.

Posing:
- the root SO-101 link (``shoulder``) rides the lift: ``X = T(lift, arm offset) . X_rest . F1 . R(pan) . F1^-1``;
- every further joint: ``X_child = X_parent . F0 . R(axis, q) . F1^-1`` with ``F0`` derived from the REST poses
  (``X_parent_rest^-1 . X_child_rest . F1``), so ``fk(zeros) == rest`` exactly whatever the authored localPos0 says;
- ``solve_reach`` is a small damped-least-squares IK (pan, shoulder_lift, elbow_flex, wrist_flex) that puts the TCP
  (between the jaw tips) at a target with the gripper level, so the visual jaws meet the unit the worker holds at
  ``geometry.held_position``. ``ReachTable`` precomputes it over the arm-extension range once per rover.
"""

import json
import math
import os
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field

Vec3 = tuple[float, float, float]
Mat = tuple[tuple[float, float, float, float], ...]

IDENTITY: Mat = ((1.0, 0.0, 0.0, 0.0), (0.0, 1.0, 0.0, 0.0), (0.0, 0.0, 1.0, 0.0), (0.0, 0.0, 0.0, 1.0))

# ---- picky_rover.usda conventions (compose_picky_rover.py writes them into customLayerData.picky)
MODEL_PRIM_NAME = "Model"
"""Child Xform of the rover root that carries the heading offset + floor offset; the USD is referenced below it."""
MODEL_REF_NAME = "LeKiwi"
"""Prim under ``Model`` that references the rover USD (its default prim ``/LeKiwi`` maps onto it)."""
HEADING_OFFSET_RAD = math.pi / 2
"""LeKiwi's arm points along -Y of its root frame; robot heading theta (+x forward) needs a +90 deg yaw."""
FLOOR_OFFSET_M = 0.0443
"""lekiwi.usd's wheels reach z = -0.0443 below the /LeKiwi origin: lift the model so they touch the floor."""
LIFT_TRAVEL_M = 1.08
"""Prismatic lift travel of the overlay (shoulder 0.12 m -> ~1.2 m; contracts LIFT_MIN_M..LIFT_MAX_M)."""
DECK_Z_M = 0.18
"""Robot-frame height of LeKiwi's top plate (0.138 m in /LeKiwi + FLOOR_OFFSET_M): the puppet's primitive mast and
basket post stand on it."""
MAST_X_M = 0.012
"""Puppet mast (lift column) x in the robot frame: on the LeKiwi base, between the basket's front wall (x <= -0.015)
and the folded SO-101 (upper arm tilted back). The carriage bracket spans from the mast to the shoulder."""
ARM_PAN_FORWARD_M = 0.04
"""The SO-101 pan axis sits this far ahead of ``contracts.common.ARM_MOUNT_X_M`` (the primitive carriage center)."""
SHOULDER_BASE_Z_M = 0.114
"""Robot-frame height of the bottom of the SO-101 shoulder link at zero lift travel (lekiwi.usd)."""
WRIST_ROLL_DEG = 90.0
"""Wrist roll for side grasps: jaws close across the unit's width (0 = jaws open vertically in lekiwi.usd)."""
TCP_BACK_M = 0.015
"""TCP = midpoint of the two jaw tips moved this far back toward the wrist."""
TCP_LEAD_M = 0.035
"""With the jaws closed (holding / stowed) the visual TCP leads the rig's gripper point by this much: the worker holds
a unit ``geometry.HELD_CLEARANCE_M`` ahead of the gripper point (so the wrist camera is never inside it), and the
jaws should close on it rather than float behind it. Open jaws (reaching) aim at the rig point itself."""
ARM_LINKS: tuple[str, ...] = ("shoulder", "upper_arm", "lower_arm", "wrist", "gripper", "jaw")
"""SO-101 links that ride the lift and get a puppet transform."""
PAN_JOINT = "shoulder_pan"
REACH_JOINTS: tuple[str, ...] = ("shoulder_pan", "shoulder_lift", "elbow_flex", "wrist_flex")
GRIPPER_JOINT = "gripper"
GRIPPER_OPEN_DEG = 35.0
GRIPPER_CLOSED_DEG = 3.0
HIDE_OVERLAY_PRIMS: tuple[str, ...] = ("lift_carriage", "base/picky_mast", "base/picky_basket")
"""Overlay parts the puppet replaces with the scene's own primitives (lift carriage, mast + camera head, basket at
``contracts.sim.BASKET_OFFSET_LOCAL``), relative to the referenced ``/LeKiwi``."""
VIRTUAL_LINKS: tuple[str, ...] = ("world", "base_x_link", "base_y_link", "base_theta_link")
"""lekiwi.usd's virtual planar-joint links (no visuals): deactivated in the puppet."""
PUPPET_SKIP_PRIMS: frozenset[str] = frozenset({"chassis", "bumper", "wheel_0", "wheel_1", "wheel_2", "wheel_3"})
"""Primitive rover parts the USD replaces (the arm group is replaced as a whole)."""
PUPPET_STANDS: frozenset[str] = frozenset({"mast", "basket_post"})
"""Primitive parts that stand on the deck: extended down to ``DECK_Z_M`` in puppet mode."""


# --------------------------------------------------------------------------------------------------------------------
# Rover models: which USD the puppet drives (detected from the file, no extra env var)
# --------------------------------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class RoverModel:
    """How one rover USD maps onto the puppet. ``links`` = the USD prim names of the canonical ``ARM_LINKS`` (same
    order); every pose the puppet computes is keyed by the canonical names, so the rest of the worker never sees them.

    ``lift_travel_m == 0`` = no lift: the SO-101 sits on a fixed riser and the arm itself reaches every shelf height
    (``z_ref_m`` = the height of the scripted TCP at zero drop, ``drops_m`` = how far below it the reach table goes).
    ``align_pan``: the arm faces sideways at pan 0 in the USD; the puppet rotates its rest pose about the pan axis so
    pan 0 reaches FORWARD like LeKiwi's SO-101 (same joint degrees on the wire for policies, IK seeds and home poses).
    ``primitives``: draw the primitive mast / camera head / basket next to the USD (LeKiwi has none of its own)."""

    key: str
    ref_name: str
    heading_offset_rad: float
    floor_offset_m: float
    links: tuple[str, ...] = ARM_LINKS
    hide_prims: tuple[str, ...] = HIDE_OVERLAY_PRIMS
    virtual_links: tuple[str, ...] = VIRTUAL_LINKS
    lift_travel_m: float = LIFT_TRAVEL_M
    z_ref_m: float | None = None
    drops_m: tuple[float, ...] | None = None
    align_pan: bool = False
    primitives: bool = True

    @property
    def link_map(self) -> dict[str, str]:
        """Canonical link name -> USD prim name."""
        return dict(zip(ARM_LINKS, self.links, strict=True))

    def canonical(self, usd_name: str) -> str:
        """USD prim name -> canonical link name (unknown names, e.g. the rover's own base link, pass through)."""
        return {u: c for c, u in self.link_map.items()}.get(usd_name, usd_name)

    @property
    def fixed_lift(self) -> bool:
        return self.lift_travel_m <= 0.0


LEKIWI = RoverModel(
    key="lekiwi", ref_name=MODEL_REF_NAME, heading_offset_rad=HEADING_OFFSET_RAD, floor_offset_m=FLOOR_OFFSET_M
)
"""``picky_rover.usda`` (LeKiwi + SO-101 + lift overlay): the default and the fallback."""
SO101_ROVER_V3 = RoverModel(
    key="so101_rover_v3",
    ref_name="Picky",
    heading_offset_rad=0.0,
    floor_offset_m=0.0,
    links=("so101_shoulder", "so101_upper_arm", "so101_lower_arm", "so101_wrist", "so101_gripper", "so101_jaw"),
    hide_prims=(),
    virtual_links=("base_anchor", "base_x_link", "base_y_link"),
    lift_travel_m=0.0,
    z_ref_m=1.2,
    drops_m=tuple(round(0.1 * i, 2) for i in range(11)),
    align_pan=True,
    primitives=False,
)
"""``picky_so101_rover.usd`` (arms/sim/supermarket/robot/build_so101_rover.py, robot v3): round white PMB2-style base,
fixed riser (shoulder at 0.75 m), long-reach SO-101 (+0.25 / +0.275 m tubes), round wire basket, its own camera
housings. Flat ``/Picky`` links, robot frame = x forward / z up / origin on the floor. ``z_ref_m`` = LIFT_MAX_M: the
reach table spans every lift height (0.2..1.2 m) as a drop below it."""
V3_ROBOT_PREFIX = "picky_so101_rover"


def rover_model_for(model_usd: str | None) -> RoverModel:
    """The ``RoverModel`` of a rover USD: robot v3 when the file (or its ``<stem>.kin.json`` sidecar) says so, else
    LeKiwi (``picky_rover.usda``, unchanged behaviour)."""
    if not model_usd:
        return LEKIWI
    base = os.path.basename(model_usd)
    if base.startswith(V3_ROBOT_PREFIX):
        return SO101_ROVER_V3
    side = os.path.splitext(model_usd)[0] + ".kin.json"
    try:
        with open(side, encoding="utf-8") as fh:
            if str(json.load(fh).get("robot", "")).startswith(V3_ROBOT_PREFIX):
                return SO101_ROVER_V3
    except (OSError, ValueError, AttributeError):
        pass
    return LEKIWI


# --------------------------------------------------------------------------------------------------------------------
# 4x4 helpers (column vectors)
# --------------------------------------------------------------------------------------------------------------------
def mat_mul(a: Mat, b: Mat) -> Mat:
    return tuple(tuple(sum(a[i][k] * b[k][j] for k in range(4)) for j in range(4)) for i in range(4))  # type: ignore[return-value]


def chain_mul(*ms: Mat) -> Mat:
    out = IDENTITY
    for m in ms:
        out = mat_mul(out, m)
    return out


def transpose(m: Sequence[Sequence[float]]) -> Mat:
    return tuple(tuple(float(m[j][i]) for j in range(4)) for i in range(4))  # type: ignore[return-value]


def apply(m: Mat, p: Sequence[float]) -> Vec3:
    """Transform a point."""
    return (
        m[0][0] * p[0] + m[0][1] * p[1] + m[0][2] * p[2] + m[0][3],
        m[1][0] * p[0] + m[1][1] * p[1] + m[1][2] * p[2] + m[1][3],
        m[2][0] * p[0] + m[2][1] * p[1] + m[2][2] * p[2] + m[2][3],
    )


def rotate(m: Mat, v: Sequence[float]) -> Vec3:
    """Transform a direction (no translation)."""
    return (
        m[0][0] * v[0] + m[0][1] * v[1] + m[0][2] * v[2],
        m[1][0] * v[0] + m[1][1] * v[1] + m[1][2] * v[2],
        m[2][0] * v[0] + m[2][1] * v[1] + m[2][2] * v[2],
    )


def translation(x: float, y: float, z: float) -> Mat:
    return ((1.0, 0.0, 0.0, x), (0.0, 1.0, 0.0, y), (0.0, 0.0, 1.0, z), (0.0, 0.0, 0.0, 1.0))


def position(m: Mat) -> Vec3:
    return (m[0][3], m[1][3], m[2][3])


def axis_rotation(axis: str, angle_rad: float) -> Mat:
    """Rotation about the local X / Y / Z axis (an empty / unknown token means X, the UsdPhysics default)."""
    c, s = math.cos(angle_rad), math.sin(angle_rad)
    ax = (axis or "X").upper()
    if ax == "Y":
        return ((c, 0.0, s, 0.0), (0.0, 1.0, 0.0, 0.0), (-s, 0.0, c, 0.0), (0.0, 0.0, 0.0, 1.0))
    if ax == "Z":
        return ((c, -s, 0.0, 0.0), (s, c, 0.0, 0.0), (0.0, 0.0, 1.0, 0.0), (0.0, 0.0, 0.0, 1.0))
    return ((1.0, 0.0, 0.0, 0.0), (0.0, c, -s, 0.0), (0.0, s, c, 0.0), (0.0, 0.0, 0.0, 1.0))


def quat_pose(w: float, x: float, y: float, z: float, pos: Sequence[float] = (0.0, 0.0, 0.0)) -> Mat:
    """Rigid transform from a (real-first) quaternion + translation (UsdPhysics localRot / localPos)."""
    n = math.sqrt(w * w + x * x + y * y + z * z) or 1.0
    w, x, y, z = w / n, x / n, y / n, z / n
    return (
        (1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w), float(pos[0])),
        (2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w), float(pos[1])),
        (2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y), float(pos[2])),
        (0.0, 0.0, 0.0, 1.0),
    )


def inverse(m: Mat) -> Mat:
    """General 4x4 inverse (Gauss-Jordan with partial pivoting)."""
    a = [list(m[i]) + [1.0 if i == j else 0.0 for j in range(4)] for i in range(4)]
    for col in range(4):
        piv = max(range(col, 4), key=lambda r: abs(a[r][col]))
        if abs(a[piv][col]) < 1e-12:
            raise ValueError("singular matrix")
        a[col], a[piv] = a[piv], a[col]
        p = a[col][col]
        a[col] = [v / p for v in a[col]]
        for r in range(4):
            if r != col and a[r][col] != 0.0:
                f = a[r][col]
                a[r] = [vr - f * vc for vr, vc in zip(a[r], a[col], strict=True)]
    return tuple(tuple(row[4:]) for row in a)  # type: ignore[return-value]


def rot_z(angle_rad: float) -> Mat:
    return axis_rotation("Z", angle_rad)


# --------------------------------------------------------------------------------------------------------------------
# Arm chain
# --------------------------------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class ArmJoint:
    """One revolute joint: ``frame1`` = joint frame in the child link (UsdPhysics localPos1 / localRot1)."""

    name: str
    parent: str
    child: str
    frame1: Mat = IDENTITY
    axis: str = "X"
    lower_deg: float = -180.0
    upper_deg: float = 180.0

    def clamp(self, q_rad: float) -> float:
        return max(math.radians(self.lower_deg), min(math.radians(self.upper_deg), q_rad))


@dataclass
class ArmChain:
    """SO-101 links + joints in the model (``/LeKiwi``) frame, rest poses from the USD.

    ``root_joint`` rotates the ``root`` link (the pan joint, whose USD parent is the lift carriage); ``joints`` are
    ordered proximal -> distal. ``tcp`` is the tool point in the ``tcp_link`` frame."""

    root: str
    rest: dict[str, Mat]
    joints: list[ArmJoint]
    root_joint: ArmJoint | None = None
    tcp_link: str = "gripper"
    tcp: Vec3 = (0.0, 0.0, 0.0)
    _f0: dict[str, Mat] = field(default_factory=dict, repr=False)
    _f1_inv: dict[str, Mat] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        if self.root_joint is not None:
            self._f1_inv[self.root_joint.name] = inverse(self.root_joint.frame1)
        for j in self.joints:
            self._f1_inv[j.name] = inverse(j.frame1)
            if j.parent not in self.rest or j.child not in self.rest:
                raise ValueError(f"joint {j.name}: links {j.parent!r} / {j.child!r} have no rest pose")
            # F0 from the rest poses: fk(zeros) reproduces the authored link poses exactly
            self._f0[j.name] = chain_mul(inverse(self.rest[j.parent]), self.rest[j.child], j.frame1)

    @property
    def links(self) -> list[str]:
        return [self.root, *[j.child for j in self.joints]]

    def joint(self, name: str) -> ArmJoint | None:
        if self.root_joint is not None and self.root_joint.name == name:
            return self.root_joint
        return next((j for j in self.joints if j.name == name), None)

    def fk(self, q: Mapping[str, float] | None = None, base: Mat = IDENTITY) -> dict[str, Mat]:
        """Link poses (model frame) for joint angles ``q`` (radians; missing = 0). ``base`` is left-multiplied onto
        the whole arm (lift + arm offset translation)."""
        q = q or {}
        out: dict[str, Mat] = {}
        root = mat_mul(base, self.rest[self.root])
        rj = self.root_joint
        if rj is not None:
            root = chain_mul(
                root, rj.frame1, axis_rotation(rj.axis, rj.clamp(q.get(rj.name, 0.0))), self._f1_inv[rj.name]
            )
        out[self.root] = root
        for j in self.joints:
            parent = out.get(j.parent)
            if parent is None:  # a joint whose parent is outside the chain: keep its rest pose under base
                parent = mat_mul(base, self.rest[j.parent])
            out[j.child] = chain_mul(
                parent, self._f0[j.name], axis_rotation(j.axis, j.clamp(q.get(j.name, 0.0))), self._f1_inv[j.name]
            )
        return out

    def tcp_point(self, poses: Mapping[str, Mat]) -> Vec3:
        return apply(poses[self.tcp_link], self.tcp)


@dataclass(frozen=True)
class ReachResult:
    q: dict[str, float]
    error_m: float


def solve_reach(
    chain: ArmChain,
    target: Vec3,
    *,
    level_link: str = "wrist",
    joints: Sequence[str] = REACH_JOINTS,
    q0: Mapping[str, float] | None = None,
    base: Mat = IDENTITY,
    up: Vec3 = (0.0, 0.0, 1.0),
    iters: int = 80,
    level_weight: float = 0.5,
) -> ReachResult:
    """Damped least squares: TCP at ``target`` (model frame) with the gripper level (TCP at the same height as the
    ``level_link`` origin, i.e. the wrist_flex joint). Finite-difference Jacobian, joint limits clamped."""
    names = [n for n in joints if chain.joint(n) is not None]
    q = dict(q0 or {})  # joints not in ``joints`` (wrist_roll, gripper) stay fixed at their q0 value
    for n in names:
        q.setdefault(n, 0.0)

    def residual(qq: Mapping[str, float]) -> list[float]:
        poses = chain.fk(qq, base)
        tcp = chain.tcp_point(poses)
        lvl = position(poses[level_link]) if level_link in poses else tcp
        dz = sum((tcp[i] - lvl[i]) * up[i] for i in range(3))
        return [tcp[0] - target[0], tcp[1] - target[1], tcp[2] - target[2], level_weight * dz]

    lam = 1e-3
    r = residual(q)
    cost = sum(v * v for v in r)
    eps = 1e-5
    for _ in range(iters):
        if cost < 1e-10:
            break
        cols: list[list[float]] = []
        for n in names:
            qp = dict(q)
            qp[n] += eps
            rp = residual(qp)
            cols.append([(rp[i] - r[i]) / eps for i in range(len(r))])
        # normal equations (J^T J + lam I) dq = -J^T r, tiny dense solve
        k = len(names)
        jtj = [
            [sum(cols[a][i] * cols[b][i] for i in range(len(r))) + (lam if a == b else 0.0) for b in range(k)]
            for a in range(k)
        ]
        jtr = [-sum(cols[a][i] * r[i] for i in range(len(r))) for a in range(k)]
        dq = _solve(jtj, jtr)
        trial = dict(q)
        for n, d in zip(names, dq, strict=True):
            j = chain.joint(n)
            trial[n] = j.clamp(trial[n] + max(-0.3, min(0.3, d))) if j else trial[n] + d
        rt = residual(trial)
        ct = sum(v * v for v in rt)
        if ct < cost:
            q, r, cost = trial, rt, ct
            lam = max(1e-6, lam * 0.3)
        else:
            lam *= 10.0
            if lam > 1e4:
                break
    poses = chain.fk(q, base)
    tcp = chain.tcp_point(poses)
    err = math.dist(tcp, target)
    return ReachResult(q=q, error_m=err)


def _solve(a: list[list[float]], b: list[float]) -> list[float]:
    n = len(b)
    m = [row[:] + [b[i]] for i, row in enumerate(a)]
    for col in range(n):
        piv = max(range(col, n), key=lambda r: abs(m[r][col]))
        if abs(m[piv][col]) < 1e-15:
            return [0.0] * n
        m[col], m[piv] = m[piv], m[col]
        for r in range(col + 1, n):
            f = m[r][col] / m[col][col]
            m[r] = [vr - f * vc for vr, vc in zip(m[r], m[col], strict=True)]
    x = [0.0] * n
    for i in range(n - 1, -1, -1):
        x[i] = (m[i][n] - sum(m[i][j] * x[j] for j in range(i + 1, n))) / m[i][i]
    return x


@dataclass
class ReachTable:
    """Precomputed IK over arm extension x drop (how far the TCP must sit BELOW the arm's reference height because
    the lift is already at its bottom). Bilinear interpolation; built once per rover USD (all rovers share it)."""

    ext: list[float]
    drops: list[float]
    q: list[list[dict[str, float]]]
    """q[drop_index][ext_index]."""
    error_m: list[list[float]]

    @classmethod
    def build(
        cls,
        chain: ArmChain,
        target: Callable[[float, float], Vec3],
        ext_range: tuple[float, float],
        drops: Sequence[float] = (0.0,),
        *,
        samples: int = 12,
        base: Mat = IDENTITY,
        seed: Mapping[str, float] | None = None,
        level_weight: float = 0.15,
    ) -> "ReachTable":
        """``target(ext, drop)`` = TCP target in the model frame (before any lift travel)."""
        lo, hi = ext_range
        exts = [lo + (hi - lo) * i / max(1, samples - 1) for i in range(samples)]
        qs: list[list[dict[str, float]]] = []
        errs: list[list[float]] = []
        row_seed = dict(seed or {})
        for drop in drops:
            # solve from the stretched end (closest to the rest pose) toward the folded end: continuation keeps one
            # elbow branch and avoids flips between neighbouring samples
            row: list[dict[str, float]] = [{} for _ in exts]
            err = [0.0] * len(exts)
            prev = dict(row_seed)
            for i in reversed(range(len(exts))):
                res = solve_reach(chain, target(exts[i], drop), q0=prev, base=base, level_weight=level_weight)
                row[i], err[i], prev = res.q, res.error_m, res.q
            row_seed = dict(row[-1])
            qs.append(row)
            errs.append(err)
        return cls(ext=exts, drops=list(drops), q=qs, error_m=errs)

    @staticmethod
    def _bracket(xs: Sequence[float], x: float) -> tuple[int, int, float]:
        if len(xs) == 1 or x <= xs[0]:
            return 0, 0, 0.0
        if x >= xs[-1]:
            return len(xs) - 1, len(xs) - 1, 0.0
        for i in range(len(xs) - 1):
            if xs[i] <= x <= xs[i + 1]:
                span = xs[i + 1] - xs[i]
                return i, i + 1, (x - xs[i]) / span if span > 0 else 0.0
        return len(xs) - 1, len(xs) - 1, 0.0

    def config(self, ext: float, drop: float = 0.0) -> dict[str, float]:
        if not self.ext or not self.q:
            return {}
        e0, e1, te = self._bracket(self.ext, ext)
        d0, d1, td = self._bracket(self.drops, drop)

        def lerp(a: Mapping[str, float], b: Mapping[str, float], t: float) -> dict[str, float]:
            return {k: a.get(k, 0.0) + t * (b.get(k, 0.0) - a.get(k, 0.0)) for k in set(a) | set(b)}

        return lerp(lerp(self.q[d0][e0], self.q[d0][e1], te), lerp(self.q[d1][e0], self.q[d1][e1], te), td)

    @property
    def max_error_m(self) -> float:
        return max((e for row in self.error_m for e in row), default=0.0)


def tcp_from_tips(fixed_tip: Vec3, moving_tip: Vec3, toward: Vec3, back_m: float = TCP_BACK_M) -> Vec3:
    """Tool point between the two jaw tips, ``back_m`` toward ``toward`` (e.g. the wrist)."""
    mid = tuple((a + b) / 2 for a, b in zip(fixed_tip, moving_tip, strict=True))
    v = tuple(t - m for t, m in zip(toward, mid, strict=True))
    n = math.sqrt(sum(c * c for c in v)) or 1.0
    return (mid[0] + v[0] / n * back_m, mid[1] + v[1] / n * back_m, mid[2] + v[2] / n * back_m)


# --------------------------------------------------------------------------------------------------------------------
# Rover-level mapping (robot frame <-> model frame)
# --------------------------------------------------------------------------------------------------------------------
def model_matrix(lekiwi_local: Mat = IDENTITY, model: RoverModel = LEKIWI) -> Mat:
    """Robot frame <- /LeKiwi frame: ``Model`` (floor offset + heading offset) times /LeKiwi's own authored ops."""
    return chain_mul(translation(0.0, 0.0, model.floor_offset_m), rot_z(model.heading_offset_rad), lekiwi_local)


def align_rest_pan(chain: "ArmChain", pan_rad: float) -> "ArmChain":
    """The same arm with its rest pose turned by ``pan_rad`` about the pan axis and the pan limits shifted to match:
    the new chain at pan 0 is the old one at ``pan_rad`` (robot v3 mounts the SO-101 facing sideways)."""
    rj = chain.root_joint
    if rj is None:
        return chain
    rest = dict(chain.fk({rj.name: pan_rad}))
    rest = {k: rest.get(k, v) for k, v in chain.rest.items()}
    deg = math.degrees(pan_rad)
    root_joint = ArmJoint(
        name=rj.name,
        parent=rj.parent,
        child=rj.child,
        frame1=rj.frame1,
        axis=rj.axis,
        lower_deg=rj.lower_deg - deg,
        upper_deg=rj.upper_deg - deg,
    )
    return ArmChain(
        root=chain.root,
        rest=rest,
        joints=list(chain.joints),
        root_joint=root_joint,
        tcp_link=chain.tcp_link,
        tcp=chain.tcp,
    )


def forward_pan(chain: "ArmChain", model: Mat, candidates_deg: Sequence[float] = (-90.0, 90.0)) -> float:
    """Pan angle (radians) among ``candidates_deg`` that points the arm's wrist farthest along robot +x."""
    rj = chain.root_joint
    if rj is None:
        return 0.0
    best, best_x = 0.0, -1e9
    for d in candidates_deg:
        poses = chain.fk({rj.name: math.radians(d)})
        x = apply(model, position(poses["wrist"]))[0]
        if x > best_x:
            best, best_x = math.radians(d), x
    return best


def lift_offset(lift_height_m: float, shoulder_ref_z_m: float, travel_m: float = LIFT_TRAVEL_M) -> float:
    """Carriage travel that puts the shoulder-lift joint (and a level TCP) at ``lift_height_m`` (robot frame)."""
    return max(0.0, min(travel_m, lift_height_m - shoulder_ref_z_m))


def robot_delta_in_model(model: Mat, delta_robot: Vec3) -> Mat:
    """Translation in the robot frame expressed as a translation in the model frame (rotation part only)."""
    inv = inverse(model)
    d = rotate(inv, delta_robot)
    return translation(*d)


def joint_angles_deg(q: Mapping[str, float]) -> dict[str, float]:
    return {k: round(math.degrees(v), 2) for k, v in q.items()}


def stand_prim_bounds(center: Vec3, size: Vec3, deck_z: float = DECK_Z_M) -> tuple[Vec3, Vec3]:
    """(center, size) of a deck-standing primitive (mast / basket post) extended down to ``deck_z``."""
    top = center[2] + size[2] / 2
    bottom = min(center[2] - size[2] / 2, deck_z)
    return (center[0], center[1], (top + bottom) / 2), (size[0], size[1], top - bottom)


def names_ok(names: Iterable[str], model: RoverModel = LEKIWI) -> bool:
    """True when every SO-101 link the puppet poses is present (else the backend keeps the primitive arm)."""
    have = set(names)
    return all(n in have for n in model.links)


# --------------------------------------------------------------------------------------------------------------------
# Joint-level kinematics (learned-policy arm control, contracts 0.2.2 ArmJointsCmd)
# --------------------------------------------------------------------------------------------------------------------
JOINT_ORDER: tuple[str, ...] = ("shoulder_pan", "shoulder_lift", "elbow_flex", "wrist_flex", "wrist_roll", "gripper")
"""= contracts.skill_server.SO101_JOINT_NAMES (kept stdlib-only here): the order of every joint vector on the wire."""


def q_rad(q_deg: Sequence[float]) -> dict[str, float]:
    """6 joint degrees (JOINT_ORDER) -> the radians dict ``ArmChain.fk`` takes."""
    return {n: math.radians(float(v)) for n, v in zip(JOINT_ORDER, q_deg, strict=False)}


def q_deg(q: Mapping[str, float]) -> list[float]:
    """Radians dict -> 6 joint degrees in JOINT_ORDER (missing joints = 0)."""
    return [math.degrees(q.get(n, 0.0)) for n in JOINT_ORDER]


@dataclass
class ArmKinematics:
    """The SO-101 of one rover model, in the ROBOT frame (x forward, y left, z up; the base center on the floor).

    Built by the Isaac backend from the rover USD (``chain`` read from the stage, ``model`` = robot <- /LeKiwi,
    ``lift_mode='offset'``: lift travel = lift height - ``z_ref``, the scripted pose comes from ``table``), or
    ``synthetic_kinematics()`` for primitive rovers / tests (``lift_mode='absolute'``: the shoulder-lift joint sits AT
    the lift height). The worker core uses it for joint mode: link poses (visual puppet), the TCP (sticky grasp) and
    the policy cameras (``arms.sim.arm_joints``; ``wrist_cam`` caches the wrist camera mount in the gripper frame)."""

    chain: ArmChain
    model: Mat = IDENTITY
    dx: float = 0.0
    z_ref: float = 0.0
    travel_m: float = LIFT_TRAVEL_M
    table: ReachTable | None = None
    lift_mode: str = "offset"
    wrist_cam: Mat | None = None
    stow_seed: dict[str, float] = field(default_factory=dict)
    _stow_cache: dict[tuple[float, float, bool], dict[str, float]] = field(default_factory=dict, repr=False)

    # ---- frames ----------------------------------------------------------------------------------------------------
    def travel(self, lift_height_m: float) -> float:
        """Carriage travel of the Lift group (visuals) for this lift height."""
        if self.lift_mode == "absolute":
            return lift_height_m
        return lift_offset(lift_height_m, self.z_ref, self.travel_m)

    def base_at_travel(self, travel_m: float) -> Mat:
        """Model-frame matrix left-multiplied onto the arm (``ArmChain.fk(base=...)``) for a carriage travel."""
        return robot_delta_in_model(self.model, (self.dx, 0.0, travel_m))

    def base(self, lift_height_m: float) -> Mat:
        return self.base_at_travel(self.travel(lift_height_m))

    def ref_height(self, lift_height_m: float) -> float:
        """Robot-frame height of the arm's reference point (the scripted level TCP) for this lift height: = the lift
        height inside the travel range (the policy side camera rides it)."""
        if self.lift_mode == "absolute" or self.travel_m <= 0.0:
            return lift_height_m  # no lift (robot v3): the side camera still frames the shelf height being reached
        return self.z_ref + self.travel(lift_height_m)

    def link_poses(self, lift_height_m: float, q: Mapping[str, float]) -> dict[str, Mat]:
        """Link poses in the MODEL frame (what the puppet's xformOps take) for joint radians ``q``."""
        return self.chain.fk(q, self.base(lift_height_m))

    def robot_poses(self, lift_height_m: float, q: Mapping[str, float]) -> dict[str, Mat]:
        """Link poses in the ROBOT frame."""
        return {k: mat_mul(self.model, v) for k, v in self.link_poses(lift_height_m, q).items()}

    def tcp(self, lift_height_m: float, q: Mapping[str, float]) -> Vec3:
        """Jaw center (TCP) in the robot frame."""
        return apply(self.model, self.chain.tcp_point(self.link_poses(lift_height_m, q)))

    def clamp_deg(self, q_deg_in: Sequence[float]) -> list[float]:
        """Joint degrees clamped to the chain's joint limits (what fk will actually pose)."""
        out = []
        for n, v in zip(JOINT_ORDER, q_deg_in, strict=False):
            j = self.chain.joint(n)
            out.append(math.degrees(j.clamp(math.radians(float(v)))) if j is not None else float(v))
        return out

    # ---- the scripted (T0) pose ------------------------------------------------------------------------------------
    def scripted_q(self, lift_height_m: float, arm_ext_m: float, closed: bool) -> dict[str, float]:
        """Joint radians of the scripted rig for (lift, arm extension, jaws): the puppet pose of the magic grasp, so
        entering joint mode starts exactly where the scripted arm stands (no visual jump)."""
        grip = math.radians(GRIPPER_CLOSED_DEG if closed else GRIPPER_OPEN_DEG)
        if self.table is not None:
            drop = max(0.0, self.z_ref - lift_height_m)
            q = self.table.config(arm_ext_m + (TCP_LEAD_M if closed else 0.0), drop)
            q["wrist_roll"] = math.radians(WRIST_ROLL_DEG)
            q["gripper"] = grip
            return q
        key = (round(lift_height_m, 3), round(arm_ext_m, 3), closed)
        if key not in self._stow_cache:
            # no reach table (synthetic arm): IK for a level TCP at the rig's gripper point
            target_robot = (ARM_MOUNT_X_M_RIG + arm_ext_m, 0.0, lift_height_m)
            target = apply(inverse(self.model), target_robot)
            res = solve_reach(self.chain, target, q0=dict(self.stow_seed), base=self.base(lift_height_m))
            self._stow_cache[key] = res.q
        q = dict(self._stow_cache[key])
        q["gripper"] = grip
        return q


ARM_MOUNT_X_M_RIG = 0.12
"""= contracts.common.ARM_MOUNT_X_M (the rig's gripper point is ARM_MOUNT_X_M + arm extension ahead of the base)."""
SYNTH_UPPER_ARM_M = 0.116
SYNTH_FOREARM_M = 0.135
SYNTH_GRIPPER_M = 0.10
SYNTH_PAN_BASE_M = 0.06
SYNTH_LIMITS_DEG: dict[str, tuple[float, float]] = {
    "shoulder_pan": (-110.0, 110.0),
    "shoulder_lift": (-100.0, 100.0),
    "elbow_flex": (-100.0, 90.0),
    "wrist_flex": (-95.0, 95.0),
    "wrist_roll": (-160.0, 160.0),
    "gripper": (-10.0, 100.0),
}
"""LeIsaac's SO-101 USD joint limits (degrees), reused for the synthetic arm."""


def synthetic_kinematics() -> ArmKinematics:
    """An SO-101-sized arm for primitive rovers, the sketch / null backends and tests (NOT the rover USD's joint
    conventions). Zero pose: upper arm vertical, forearm + gripper forward (+x); pan about z, the three flex joints
    about y (positive = forward / down), roll about x. The shoulder-lift joint sits at the lift height, the pan axis at
    ``ARM_MOUNT_X_M + ARM_PAN_FORWARD_M``."""
    lo, up = SYNTH_UPPER_ARM_M, SYNTH_FOREARM_M
    rest = {
        "shoulder": translation(0.0, 0.0, -SYNTH_PAN_BASE_M),
        "upper_arm": IDENTITY,
        "lower_arm": translation(0.0, 0.0, lo),
        "wrist": translation(up, 0.0, lo),
        "gripper": translation(up, 0.0, lo),
        "jaw": translation(up + 0.02, -0.01, lo),
    }

    def j(name: str, parent: str, child: str, axis: str) -> ArmJoint:
        lo_deg, hi_deg = SYNTH_LIMITS_DEG[name]
        return ArmJoint(name=name, parent=parent, child=child, axis=axis, lower_deg=lo_deg, upper_deg=hi_deg)

    chain = ArmChain(
        root="shoulder",
        rest=rest,
        joints=[
            j("shoulder_lift", "shoulder", "upper_arm", "Y"),
            j("elbow_flex", "upper_arm", "lower_arm", "Y"),
            j("wrist_flex", "lower_arm", "wrist", "Y"),
            j("wrist_roll", "wrist", "gripper", "X"),
            j("gripper", "gripper", "jaw", "Z"),
        ],
        root_joint=j("shoulder_pan", "shoulder", "shoulder", "Z"),
        tcp_link="gripper",
        tcp=(SYNTH_GRIPPER_M, 0.0, 0.0),
    )
    return ArmKinematics(
        chain=chain,
        model=IDENTITY,
        dx=ARM_MOUNT_X_M_RIG + ARM_PAN_FORWARD_M,
        lift_mode="absolute",
        stow_seed={"shoulder_lift": math.radians(-20.0), "elbow_flex": math.radians(60.0)},
    )
