"""MuJoCo backend with real physics and the real SO-ARM101 (``--backend mujoco``, physics on by default).

What is simulated (MuJoCo 3.16, implicitfast, 4 ms steps, body sleeping on):
- Product units are free rigid bodies (box collider at the unit's dims, its mass from products.yaml, the textured
  model as visual geoms). They start ASLEEP (``sleep="init"``): stable on the shelf and free until something touches
  them, which wakes them. Units that cannot start asleep (touching a neighbour) fall back to automatic sleeping.
- Fixtures (boards, back panels, bases, walls, floor, columns) are static colliders.
- Each rover is kinematic (the worker core owns navigation): its chassis / mast / basket are mocap colliders that push
  products, its four wheels spin with the distance driven, and its lift carriage is a mocap body carrying the
  SO-ARM101 from TheRobotStudio's MJCF (``so101_new_calib.xml``: real links, joint limits and STS3215 position servos).
- The arm is driven through its position actuators by damped-least-squares IK on the ``gripperframe`` site (position +
  horizontal approach axis). The core's (pose, lift, arm reach, gripper) is the command; the backend aims the jaws at
  the unit in front of the gripper, closes them, and checks the contacts.
- Grasp: when BOTH jaws touch a unit with the gripper closed, physics carries it (the core's per-tick pose for that
  unit is ignored until the gripper opens). Otherwise (too wide for the 75 mm jaw, missed) the core's scripted grasp
  pose is applied, so trips never break. ``grasp_log`` records which one happened per pick.

Collision groups (MuJoCo: two geoms touch when ``contype_a & conaffinity_b`` or ``contype_b & conaffinity_a``):
fixtures contype 1 / conaffinity 0; products 2 / 7; rover colliders 4 / 0; SO-101 (its own defaults) 1 / 1. So the
arm hits shelves and products but never its own mast, and the rover body pushes products but not the arm.
"""

import math
import re
import time
from pathlib import Path
from typing import Any
from xml.sax.saxutils import quoteattr

import numpy as np

from arms.sim.geometry import CameraPose
from arms.sim.mujoco_backend import (
    PARKED_Z_M,
    MujocoBackend,
    _geom_xml,
    _quat,
)
from arms.sim.scene_spec import LIFT_REF_M, PrimSpec, RoverSpec, Vec3
from contracts.common import ARM_MOUNT_X_M
from utils.log import get_logger

log = get_logger(__name__)

TIMESTEP_S = 0.004
MAX_STEPS_PER_SYNC = 25
DEFAULT_SO101_XML = Path(__file__).resolve().parents[1] / "robot/so101/assets/so101_new_calib.xml"
FALLBACK_SO101_XML = Path.home() / "picky/arms/robot/so101/assets/so101_new_calib.xml"
ARM_JOINTS = ("shoulder_pan", "shoulder_lift", "elbow_flex", "wrist_flex", "wrist_roll")
ARM_BASE_ON_CARRIAGE = (0.04, 0.0, -0.03)  # SO-101 base frame on the lift carriage (rover frame offsets)
GRIPPER_OPEN_RAD = 1.65
GRIPPER_CLOSED_RAD = -0.17
FIXTURE_ROLES = {"board", "panel", "base", "wall", "floor", "obstacle"}
ROVER_COLLIDERS = ("chassis", "mast", "basket_floor", "basket_front", "basket_back", "basket_left", "basket_right")
WHEEL_RADIUS_M = 0.06
STOW_EXT_M = 0.13
HOLD_RADIUS_M = 0.09  # a grasped unit whose center stays this close to the fingertips is still held


def _qmul(a, b):
    aw, ax, ay, az = a
    bw, bx, by, bz = b
    return (
        aw * bw - ax * bx - ay * by - az * bz,
        aw * bx + ax * bw + ay * bz - az * by,
        aw * by - ax * bz + ay * bw + az * bx,
        aw * bz + ax * by - ay * bx + az * bw,
    )


def _qaxis(axis: tuple[float, float, float], ang: float):
    s = math.sin(ang / 2)
    return (math.cos(ang / 2), axis[0] * s, axis[1] * s, axis[2] * s)


def so101_xml_path(explicit: str | None = None) -> Path | None:
    for p in (explicit, DEFAULT_SO101_XML, FALLBACK_SO101_XML):
        if p and Path(p).is_file():
            return Path(p)
    return None


class MujocoPhysicsBackend(MujocoBackend):
    def __init__(self, *args: Any, so101_xml: str | None = None, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.so101_xml = so101_xml_path(so101_xml)
        if self.so101_xml is None:
            log.warning("SO-101 MJCF not found (arms/robot/so101/get_assets.sh): rovers get no arm in physics mode")
        self._qadr: dict[tuple[str, str], int] = {}
        self._dadr: dict[tuple[str, str], int] = {}
        self._body: dict[tuple[str, str], int] = {}
        self._body_key: dict[int, tuple[str, str]] = {}
        self._pending: set[tuple[str, str]] = set()
        self._grasped: dict[str, tuple[str, str]] = {}
        self._closed: dict[str, bool] = {}
        self._odo: dict[str, tuple[float, float, float]] = {}
        self._rmocap: dict[str, dict[str, Any]] = {}
        self._arm: dict[str, dict[str, Any]] = {}
        self._ik_cache: dict[str, tuple[tuple[float, ...], np.ndarray]] = {}
        self._last_step: float | None = None
        self._ikd: Any = None
        self.grasp_log: list[dict[str, Any]] = []

    # ---- core -> backend state
    def set_instance_pose(self, store_id: str, instance_id: str, position, yaw: float | None = None) -> None:
        super().set_instance_pose(store_id, instance_id, position, yaw)
        self._pending.add((store_id, instance_id))

    def add_instance(self, prim: PrimSpec) -> None:
        super().add_instance(prim)
        self._pending.add((prim.store_id or "", prim.instance_id or ""))

    def remove_instance(self, store_id: str, instance_id: str) -> None:
        super().remove_instance(store_id, instance_id)
        self._pending.add((store_id, instance_id))

    def set_robot_gripper(self, robot_id: str, closed: bool) -> None:
        super().set_robot_gripper(robot_id, closed)
        was = self._closed.get(robot_id, False)
        self._closed[robot_id] = closed
        if was and not closed and robot_id in self._grasped:
            key = self._grasped.pop(robot_id)
            self._pending.add(key)  # released: the core's pose for it (basket / shelf) wins again

    # ---- build hooks
    def _static_geom(self, prim: PrimSpec, pos: Vec3, material: str) -> str:
        g = super()._static_geom(prim, pos, material)
        if prim.role in FIXTURE_ROLES:
            g = g.replace('contype="0" conaffinity="0"', 'contype="1" conaffinity="0" friction="0.8 0.01 0.001"')
        return g

    def _instance_body(self, key: tuple[str, str], prim: PrimSpec, inner: str) -> str:
        ox, oy, oz = self._offset(key[0])
        c = prim.center
        q = _quat(prim.yaw)
        sx, sy, sz = (max(0.006, v) for v in prim.size)
        mass = max(0.02, float(prim.mass_kg or 0.3))
        collider = (
            f'<geom type="box" size="{sx * 0.48:.4f} {sy * 0.48:.4f} {sz / 2 - 0.001:.4f}" mass="{mass:.3f}" '
            f'contype="2" conaffinity="7" friction="1.0 0.02 0.002" group="3" rgba="0 0 0 0"/>'
        )
        return (
            f"<body name={quoteattr('i/' + '/'.join(key))} "
            f'pos="{c[0] + ox:.4f} {c[1] + oy:.4f} {c[2] + oz + 0.0015:.4f}" '
            f'quat="{q[0]:.6f} {q[1]:.6f} {q[2]:.6f} {q[3]:.6f}" sleep="init"><freejoint/>{collider}{inner}</body>'
        )

    def _rover_bodies(self, rid: str, rover: RoverSpec) -> str:
        base, lift, wheels = [], [], []
        for p in rover.prims:
            name = p.path.rsplit("/", 1)[-1]
            if p.group == "arm" and self.so101_xml is not None:
                continue  # the real SO-101 replaces the primitive arm
            if name.startswith("wheel_"):
                wheels.append(p)
                continue
            target = lift if p.group in ("lift", "arm") else base
            local = p.model_copy(
                update={"center": (p.center[0], p.center[1], p.center[2] - (LIFT_REF_M if target is lift else 0.0))}
            )
            g = _geom_xml(local, local.center, _quat(p.yaw, p.pitch))
            if name in ROVER_COLLIDERS:
                g = g.replace('contype="0" conaffinity="0"', 'contype="4" conaffinity="0"')
            target.append(g)
        lift_xml = "".join(lift) or '<geom size="0.01" contype="0" conaffinity="0"/>'
        out = [
            f'<body name="r/{rid}/base" mocap="true">{"".join(base)}</body>',
            f'<body name="r/{rid}/lift" mocap="true">{lift_xml}</body>',
        ]
        for k, w in enumerate(wheels):
            out.append(
                f'<body name="r/{rid}/w{k}" mocap="true">'
                f'<geom type="cylinder" size="{w.size[0] / 2:.4f} {w.size[1] / 2:.4f}" '
                f'rgba="0.10 0.10 0.11 1" contype="0" conaffinity="0"/>'
                f'<geom type="box" size="{w.size[0] * 0.3:.4f} {w.size[1] * 0.52:.4f} 0.008" rgba="0.55 0.56 0.58 1" '
                f'contype="0" conaffinity="0"/></body>'
            )
        self._wheel_specs = getattr(self, "_wheel_specs", {})
        self._wheel_specs[rid] = [w.center for w in wheels]
        return "".join(out)

    def _compile(self, xml: str) -> Any:
        mj = self._mj
        xml = xml.replace(
            '<compiler angle="radian"/>',
            '<compiler angle="radian"/>'
            f'<option timestep="{TIMESTEP_S}" integrator="implicitfast" cone="elliptic" impratio="10">'
            '<flag sleep="enable"/></option><default><geom density="0"/></default>',
        )
        park = (
            f'<geom type="box" pos="0 0 {PARKED_Z_M - 0.5}" size="200 200 0.5" '
            'contype="1" conaffinity="0" rgba="0 0 0 0"/>'
        )
        xml = xml.replace("<worldbody>", "<worldbody>" + park, 1)
        spec = mj.MjSpec.from_string(xml)
        if self.so101_xml is not None:
            for rid in self.rovers:
                arm = mj.MjSpec.from_file(str(self.so101_xml))
                arm.meshdir = str(self.so101_xml.parent / "assets")
                frame = spec.body(f"r/{rid}/lift").add_frame(pos=list(ARM_BASE_ON_CARRIAGE))
                frame.attach_body(arm.body("base"), f"{rid}/", "")
        bodies = spec.worldbody.find_all(mj.mjtObj.mjOBJ_BODY)
        for _attempt in range(64):
            try:
                return spec.compile()
            except ValueError as exc:
                m = re.search(r"\(id=(\d+)\) is the root of the first tree that could not be slept", str(exc))
                if not m:
                    raise
                b = bodies[int(m.group(1)) - 1]
                b.sleep = mj.mjtSleepPolicy.mjSLEEP_AUTO  # touching a neighbour: settle, then sleep on its own
        return spec.compile()

    def _index(self, model: Any, instance_keys: list, rover_parts: dict[str, int], mocap_of: Any) -> None:
        mj = self._mj
        self._instance_mocap = {key: -1 for key in instance_keys}  # "known unit" for add_instance (no recompile)
        self._rover_mocap = {}
        self._qadr, self._dadr, self._body, self._body_key = {}, {}, {}, {}
        for key in instance_keys:
            bid = mj.mj_name2id(model, mj.mjtObj.mjOBJ_BODY, "i/" + "/".join(key))
            jid = model.body_jntadr[bid]
            self._body[key], self._body_key[bid] = bid, key
            self._qadr[key], self._dadr[key] = int(model.jnt_qposadr[jid]), int(model.jnt_dofadr[jid])
        self._rmocap = {}
        for rid in rover_parts:
            n_w = len(getattr(self, "_wheel_specs", {}).get(rid, []))
            self._rmocap[rid] = {
                "base": mocap_of(f"r/{rid}/base"),
                "lift": mocap_of(f"r/{rid}/lift"),
                "wheels": [mocap_of(f"r/{rid}/w{k}") for k in range(n_w)],
            }
        self._arm = {}
        if self.so101_xml is not None:
            for rid in rover_parts:
                jids = [mj.mj_name2id(model, mj.mjtObj.mjOBJ_JOINT, f"{rid}/{j}") for j in ARM_JOINTS]
                gid = mj.mj_name2id(model, mj.mjtObj.mjOBJ_JOINT, f"{rid}/gripper")
                self._arm[rid] = {
                    "qadr": [int(model.jnt_qposadr[j]) for j in jids],
                    "dadr": [int(model.jnt_dofadr[j]) for j in jids],
                    "lo": np.array([model.jnt_range[j][0] for j in jids]),
                    "hi": np.array([model.jnt_range[j][1] for j in jids]),
                    "act": [mj.mj_name2id(model, mj.mjtObj.mjOBJ_ACTUATOR, f"{rid}/{j}") for j in ARM_JOINTS],
                    "grip_act": mj.mj_name2id(model, mj.mjtObj.mjOBJ_ACTUATOR, f"{rid}/gripper"),
                    "grip_qadr": int(model.jnt_qposadr[gid]),
                    "site": mj.mj_name2id(model, mj.mjtObj.mjOBJ_SITE, f"{rid}/gripperframe"),
                    "jaw_moving": mj.mj_name2id(model, mj.mjtObj.mjOBJ_BODY, f"{rid}/moving_jaw_so101_v1"),
                    "jaw_fixed": mj.mj_name2id(model, mj.mjtObj.mjOBJ_BODY, f"{rid}/gripper"),
                }
        self._ikd = mj.MjData(model)
        self._pending = set()
        self._grasped = {}
        self._ik_cache = {}
        self._last_step = None
        self._parked = set()

    # ---- per-tick sync: mocaps, teleports, arm control, physics steps
    def _rover_world(self, rid: str, local: Vec3) -> tuple[float, float, float]:
        rover = self.rovers[rid]
        pose, lift, _ext = self.robot_poses.get(rid, (rover.dock, LIFT_REF_M, 0.12))
        ox, oy, oz = self._offset(rover.store_id)
        c, s = math.cos(pose.theta), math.sin(pose.theta)
        return (pose.x + c * local[0] - s * local[1] + ox, pose.y + s * local[0] + c * local[1] + oy, local[2] + oz)

    def _sync(self) -> None:
        d, _m = self._data, self._model
        for rid, rover in self.rovers.items():
            ids = self._rmocap.get(rid)
            if not ids:
                continue
            pose, lift, _ext = self.robot_poses.get(rid, (rover.dock, LIFT_REF_M, 0.12))
            q = _quat(pose.theta)
            d.mocap_pos[ids["base"]] = self._rover_world(rid, (0.0, 0.0, 0.0))
            d.mocap_quat[ids["base"]] = q
            d.mocap_pos[ids["lift"]] = self._rover_world(rid, (0.0, 0.0, lift))
            d.mocap_quat[ids["lift"]] = q
            # wheel spin from odometry (signed distance along the heading)
            last = self._odo.get(rid)
            dist = 0.0 if last is None else last[2]
            if last is not None:
                dx, dy = pose.x - last[0], pose.y - last[1]
                dist += dx * math.cos(pose.theta) + dy * math.sin(pose.theta)
            self._odo[rid] = (pose.x, pose.y, dist)
            spin = _qaxis((0.0, 1.0, 0.0), dist / WHEEL_RADIUS_M)
            for mid, wc in zip(ids["wheels"], self._wheel_specs.get(rid, []), strict=False):
                d.mocap_pos[mid] = self._rover_world(rid, wc)
                d.mocap_quat[mid] = _qmul(_qmul(q, spin), (0.7071068, 0.7071068, 0.0, 0.0))
        held = set(self._grasped.values())
        for key in self._pending:
            if key in held or key not in self._qadr:
                continue
            qa, da = self._qadr[key], self._dadr[key]
            prim = self.instance_prims.get(key)
            if prim is None:  # removed: park it out of sight on the hidden floor
                h = hash(key) % 4000
                d.qpos[qa : qa + 3] = (h % 63 * 0.6, h // 63 * 0.6, PARKED_Z_M + 0.2)
            else:
                pos = self.positions.get(key)
                ox, oy, oz = self._offset(key[0])
                p = (pos.x, pos.y, pos.z) if pos is not None else prim.center
                d.qpos[qa : qa + 3] = (p[0] + ox, p[1] + oy, p[2] + oz + 0.0015)
                d.qpos[qa + 3 : qa + 7] = _quat(self.yaws.get(key, prim.yaw))
            d.qvel[da : da + 6] = 0.0
        self._pending = set()
        for rid in self._arm:
            self._control_arm(rid)
        self._advance()

    def _advance(self) -> None:
        now = time.monotonic()
        if self._last_step is None:
            self._last_step = now
            return
        n = min(MAX_STEPS_PER_SYNC, int((now - self._last_step) / TIMESTEP_S))
        if n <= 0:
            return
        self._last_step += n * TIMESTEP_S
        if now - self._last_step > 0.5:
            self._last_step = now  # can't keep up: run slower than real time instead of spiralling
        for _ in range(n):
            self._mj.mj_step(self._model, self._data)
        self._detect_grasps()

    def _forward_for_render(self) -> None:
        # the render camera's mocap was just moved: refresh kinematics (cheap, leaves the dynamics state alone)
        self._mj.mj_kinematics(self._model, self._data)
        self._mj.mj_camlight(self._model, self._data)

    def step(self) -> None:
        with self._lock:
            if self._model is None or self._dirty:
                if not self.stores:
                    return
                self._build()
            self._sync()
            self._sync_viewer()

    def _sync_viewer(self) -> None:
        v = self._viewer
        if v is None or time.monotonic() - self._viewer_synced < 1 / 30:
            return
        self._viewer_synced = time.monotonic()
        if not v.is_running():
            self._viewer, self._want_viewer = None, False
            return
        v.sync()

    # ---- arm
    def _target(self, rid: str) -> tuple[np.ndarray, bool]:
        """World target for the gripperframe + whether it is a grasp target (else a stow pose)."""
        rover = self.rovers[rid]
        pose, lift, ext = self.robot_poses.get(rid, (rover.dock, LIFT_REF_M, 0.12))
        c, s = math.cos(pose.theta), math.sin(pose.theta)
        if ext <= STOW_EXT_M:
            # stowed (open) or carrying (closed): hand up and in front of the arm base, clear of the shelf edge
            if self._closed.get(rid, False):
                return np.array(self._rover_world(rid, (ARM_MOUNT_X_M + 0.26, 0.0, lift + 0.06))), True
            return np.array(self._rover_world(rid, (ARM_MOUNT_X_M + 0.18, 0.0, lift + 0.02))), True
        tcp = np.array(self._rover_world(rid, (ARM_MOUNT_X_M + ext, 0.0, lift)))
        if rid in self._grasped:
            return tcp, True
        best, best_d = None, 0.16
        held = set(self._grasped.values())
        for key, prim in self.instance_prims.items():
            if key[0] != rover.store_id or key in held:
                continue
            p = self._data.xpos[self._body[key]] if key in self._body else None
            if p is None:
                continue
            rel = p - tcp
            along = rel[0] * c + rel[1] * s
            if -0.02 < along < best_d and abs(-rel[0] * s + rel[1] * c) < 0.06 and abs(rel[2]) < 0.08:
                best, best_d = (key, p, prim), along
        if best is None:
            return tcp, True
        key, p, prim = best
        depth = min(prim.size[0], prim.size[1])
        reach = min(best_d, depth / 2 + 0.03)  # never past the unit's center
        return tcp + reach * np.array([c, s, 0.0]) + np.array([0.0, 0.0, p[2] - tcp[2]]), True

    def _control_arm(self, rid: str) -> None:
        a, _m, d = self._arm[rid], self._model, self._data
        target, grasp = self._target(rid)
        pose = self.robot_poses.get(rid, (self.rovers[rid].dock, LIFT_REF_M, 0.12))[0]
        approach = np.array([math.cos(pose.theta), math.sin(pose.theta), 0.0 if grasp else -0.6])
        approach /= np.linalg.norm(approach)
        key = tuple(np.round(np.r_[target, approach], 3))
        cached = self._ik_cache.get(rid)
        if cached is None or cached[0] != key:
            q = self._ik(rid, target, approach)
            self._ik_cache[rid] = (key, q)
        q = self._ik_cache[rid][1]
        for act, qi in zip(a["act"], q, strict=True):
            d.ctrl[act] = qi
        d.ctrl[a["grip_act"]] = GRIPPER_CLOSED_RAD if self._closed.get(rid, False) else GRIPPER_OPEN_RAD

    def _ik(self, rid: str, target: np.ndarray, approach: np.ndarray, iters: int = 60) -> np.ndarray:
        mj, m, a = self._mj, self._model, self._arm[rid]
        k = self._ikd
        k.qpos[:] = self._data.qpos
        k.mocap_pos[:] = self._data.mocap_pos
        k.mocap_quat[:] = self._data.mocap_quat
        qa, da, site = a["qadr"], a["dadr"], a["site"]
        jp, jr = np.zeros((3, m.nv)), np.zeros((3, m.nv))
        for _ in range(iters):
            mj.mj_kinematics(m, k)
            mj.mj_comPos(m, k)
            pos = k.site_xpos[site]
            z = k.site_xmat[site].reshape(3, 3)[:, 2]
            ep = target - pos
            er = np.cross(z, approach)
            if np.linalg.norm(ep) < 0.002 and np.linalg.norm(er) < 0.02:
                break
            mj.mj_jacSite(m, k, jp, jr, site)
            J = np.vstack([jp[:, da], 0.3 * jr[:, da]])
            e = np.r_[ep, 0.3 * er]
            dq = J.T @ np.linalg.solve(J @ J.T + 2e-3 * np.eye(6), e)
            k.qpos[qa] = np.clip(k.qpos[qa] + 0.7 * dq, a["lo"], a["hi"])
        return np.array(k.qpos[qa])

    def _detect_grasps(self) -> None:
        d, m = self._data, self._model
        for rid, a in self._arm.items():
            if not self._closed.get(rid, False):
                continue
            touch_m, touch_f = set(), set()
            for i in range(d.ncon):
                c = d.contact[i]
                b1, b2 = int(m.geom_bodyid[c.geom1]), int(m.geom_bodyid[c.geom2])
                for jb, other in ((b1, b2), (b2, b1)):
                    key = self._body_key.get(other)
                    if key is None:
                        continue
                    if jb == a["jaw_moving"]:
                        touch_m.add(key)
                    elif jb == a["jaw_fixed"]:
                        touch_f.add(key)
            both = touch_m & touch_f
            if rid in self._grasped:
                key = self._grasped[rid]
                tip = d.site_xpos[a["site"]]
                if key in both or np.linalg.norm(d.xpos[self._body[key]] - tip) < HOLD_RADIUS_M:
                    continue  # still in the hand (contacts flicker step to step)
            if both and rid not in self._grasped:
                key = sorted(both)[0]
                self._grasped[rid] = key
                prim = self.instance_prims.get(key)
                self.grasp_log.append(
                    {
                        "robot_id": rid,
                        "instance_id": key[1],
                        "sku": prim.sku if prim else None,
                        "mode": "physical",
                        "t": time.time(),
                    }
                )
                log.info("%s: physical grasp of %s (%s)", rid, key[1], prim.sku if prim else "?")
            elif rid in self._grasped and not both:
                key = self._grasped.pop(rid)  # slipped out: the core's scripted pose takes over
                self._pending.add(key)
                self.grasp_log.append({"robot_id": rid, "instance_id": key[1], "mode": "slipped", "t": time.time()})
                log.info("%s: %s slipped from the jaws; scripted grasp takes over", rid, key[1])

    # ---- cameras: the wrist camera rides the real gripper
    def _camera_override(self, camera_id: str, store_id: str, cam: CameraPose) -> CameraPose:
        rid, _, kind = camera_id.rpartition(".")
        a = self._arm.get(rid)
        if kind != "wrist" or a is None:
            return cam
        d = self._data
        x = d.site_xmat[a["site"]].reshape(3, 3)
        ox, oy, oz = self._offset(store_id)
        eye = d.site_xpos[a["site"]] - x[:, 2] * 0.07 + x[:, 0] * 0.03
        return CameraPose.from_axes(
            (float(eye[0] - ox), float(eye[1] - oy), float(eye[2] - oz)),
            tuple(float(v) for v in x[:, 2]),
            tuple(float(v) for v in x[:, 1]),
            hfov=cam.hfov,
            w=cam.w,
            h=cam.h,
        )
