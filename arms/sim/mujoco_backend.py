"""MuJoCo renderer for the worker on a Mac / any machine without Isaac (``--backend mujoco``).

Same ``SimBackend`` seam as Isaac and Sketch: the worker core still owns every pose (navigation, lift, magic grasp),
and this backend turns the ``SceneSpec`` / ``RoverSpec`` primitives into one MJCF model and renders the cameras
offscreen with ``mujoco.Renderer`` (CGL on macOS, EGL / OSMesa on Linux via ``MUJOCO_GL``).

- Fixtures, walls, floor, signs: static geoms in the world body, store offset baked in.
- Product units and rover parts: mocap bodies, posed from the core's state right before each render (no dynamics are
  stepped, like the Isaac backend's kinematic mode), so picks and placements show exactly where the core says.
- Structural changes (a store (re)built, a rover spawned, a unit added) mark the model dirty; the next render
  recompiles it. Removed units are parked far below the floor until the next recompile.
- One camera rides its own mocap body; each render moves it to the requested ``CameraPose`` (MuJoCo cameras look
  along their local -z with +y up, so the body's axes are (right, up, -forward)) and sets the vertical fov from hfov.
"""

import io
import math
import threading
from typing import Any
from xml.sax.saxutils import quoteattr

from arms.sim.geometry import NEAR_CLIP_M, CameraPose
from arms.sim.scene_spec import PrimSpec, RoverSpec, SceneSpec, Vec3
from arms.sim.sketch import SketchBackend
from arms.sim.worker import grey_jpeg
from contracts.common import Point3

JPEG_QUALITY = 80
MAX_FRAME_W = 1280
MAX_FRAME_H = 960
PARKED_Z_M = -50.0
_CAM_BODY = "picky_cam"


class MujocoUnavailable(RuntimeError):
    """``mujoco`` is not installed (``uv sync --extra mujoco``)."""


def require_mujoco() -> Any:
    try:
        import mujoco
    except ImportError as exc:
        raise MujocoUnavailable("mujoco is not installed: run `uv sync --extra mujoco`") from exc
    return mujoco


def _rgba(color: str, alpha: float = 1.0) -> str:
    c = color.lstrip("#")
    if len(c) == 3:
        c = "".join(ch * 2 for ch in c)
    try:
        r, g, b = (int(c[i : i + 2], 16) / 255 for i in (0, 2, 4))
    except ValueError:
        r = g = b = 0.8
    return f"{r:.3f} {g:.3f} {b:.3f} {alpha:.2f}"


def _quat(yaw: float, pitch: float = 0.0) -> tuple[float, float, float, float]:
    """(w, x, y, z) of Rz(yaw) * Ry(pitch), the PrimSpec convention."""
    cz, sz = math.cos(yaw / 2), math.sin(yaw / 2)
    cy, sy = math.cos(pitch / 2), math.sin(pitch / 2)
    return (cz * cy, -sz * sy, cz * sy, sz * cy)


def _quat_from_axes(x: Vec3, y: Vec3, z: Vec3) -> tuple[float, float, float, float]:
    """(w, x, y, z) of the rotation whose columns are the orthonormal axes x, y, z."""
    m00, m01, m02 = x[0], y[0], z[0]
    m10, m11, m12 = x[1], y[1], z[1]
    m20, m21, m22 = x[2], y[2], z[2]
    tr = m00 + m11 + m22
    if tr > 0:
        s = 2.0 * math.sqrt(tr + 1.0)
        return (0.25 * s, (m21 - m12) / s, (m02 - m20) / s, (m10 - m01) / s)
    if m00 > m11 and m00 > m22:
        s = 2.0 * math.sqrt(1.0 + m00 - m11 - m22)
        return ((m21 - m12) / s, 0.25 * s, (m01 + m10) / s, (m02 + m20) / s)
    if m11 > m22:
        s = 2.0 * math.sqrt(1.0 + m11 - m00 - m22)
        return ((m02 - m20) / s, (m01 + m10) / s, 0.25 * s, (m12 + m21) / s)
    s = 2.0 * math.sqrt(1.0 + m22 - m00 - m11)
    return ((m10 - m01) / s, (m02 + m20) / s, (m12 + m21) / s, 0.25 * s)


def _geom_xml(prim: PrimSpec, pos: Vec3 | None = None, quat: tuple[float, ...] | None = None) -> str:
    sx, sy, sz = (max(1e-3, v) for v in prim.size)
    if prim.shape == "cylinder":
        attrs = f'type="cylinder" size="{max(sx, sy) / 2:.4f} {sz / 2:.4f}"'
    elif prim.shape == "sphere":
        attrs = f'type="ellipsoid" size="{sx / 2:.4f} {sy / 2:.4f} {sz / 2:.4f}"'
    else:
        attrs = f'type="box" size="{sx / 2:.4f} {sy / 2:.4f} {sz / 2:.4f}"'
    extra = ' material="lamp"' if prim.role == "light" else ""
    where = ""
    if pos is not None:
        where += f' pos="{pos[0]:.4f} {pos[1]:.4f} {pos[2]:.4f}"'
    if quat is not None:
        where += ' quat="{:.6f} {:.6f} {:.6f} {:.6f}"'.format(*quat)
    # contype/conaffinity 0: nothing is simulated, so skip the collision pipeline entirely
    return f'<geom {attrs}{where} rgba="{_rgba(prim.color)}" contype="0" conaffinity="0"{extra}/>'


class MujocoBackend(SketchBackend):
    """SketchBackend bookkeeping (stores, rovers, instance poses) + a MuJoCo model built from it."""

    camera_source = "isaac"

    def __init__(self, jpeg_quality: int = JPEG_QUALITY) -> None:
        super().__init__(jpeg_quality=jpeg_quality)
        self._mj = require_mujoco()
        self._lock = threading.RLock()
        self._model: Any = None
        self._data: Any = None
        self._renderers: dict[tuple[int, int], Any] = {}
        self._dirty = True
        self._instance_mocap: dict[tuple[str, str], int] = {}
        self._rover_mocap: dict[str, list[int]] = {}
        self._cam_mocap = -1
        self._cam_id = -1
        self._parked: set[tuple[str, str]] = set()
        self.builds = 0

    # ---- structural changes -> recompile on the next render
    def build_store(self, spec: SceneSpec, offset: Vec3) -> None:
        super().build_store(spec, offset)
        self._dirty = True

    def set_store_offset(self, store_id: str, offset: Vec3) -> None:
        super().set_store_offset(store_id, offset)
        self._dirty = True

    def spawn_robot(self, rover: RoverSpec) -> None:
        super().spawn_robot(rover)
        self._dirty = True

    def remove_robot(self, robot_id: str) -> None:
        super().remove_robot(robot_id)
        self._dirty = True

    def add_instance(self, prim: PrimSpec) -> None:
        super().add_instance(prim)
        key = (prim.store_id or "", prim.instance_id or "")
        self._parked.discard(key)
        if key not in self._instance_mocap:
            self._dirty = True

    def remove_instance(self, store_id: str, instance_id: str) -> None:
        super().remove_instance(store_id, instance_id)
        self._parked.add((store_id, instance_id))

    # ---- model
    def _offset(self, store_id: str | None) -> Vec3:
        return self.offsets.get(store_id or "", (0.0, 0.0, 0.0))

    def _build(self) -> None:
        mj = self._mj
        static: list[str] = []
        bodies: list[str] = []
        instance_keys: list[tuple[str, str]] = []
        rover_parts: dict[str, int] = {}
        for sid, spec in self.stores.items():
            ox, oy, oz = self._offset(sid)
            for p in self._static_prims(spec):
                c = p.center
                static.append(_geom_xml(p, (c[0] + ox, c[1] + oy, c[2] + oz), _quat(p.yaw, p.pitch)))
        for key, prim in self.instance_prims.items():
            instance_keys.append(key)
            bodies.append(f'<body name={quoteattr("i/" + "/".join(key))} mocap="true">{_geom_xml(prim)}</body>')
        for rid, rover in self.rovers.items():
            rover_parts[rid] = len(rover.prims)
            for k, p in enumerate(rover.prims):
                bodies.append(f'<body name={quoteattr(f"r/{rid}/{k}")} mocap="true">{_geom_xml(p)}</body>')
        xml = f"""<mujoco model="picky_stores">
  <compiler angle="radian"/>
  <visual>
    <global offwidth="{MAX_FRAME_W}" offheight="{MAX_FRAME_H}"/>
    <quality shadowsize="0"/>
    <headlight ambient="0.3 0.3 0.3" diffuse="0.35 0.35 0.35" specular="0.05 0.05 0.05"/>
    <map znear="{NEAR_CLIP_M / 10:.4f}" zfar="20"/>
  </visual>
  <asset><material name="lamp" emission="1" rgba="1 1 0.98 1"/></asset>
  <worldbody>
    <light directional="true" dir="0.3 0.2 -1" diffuse="0.25 0.25 0.25" castshadow="false"/>
    <light directional="true" dir="-0.3 -0.4 -1" diffuse="0.2 0.2 0.2" castshadow="false"/>
    {"".join(static)}
    {"".join(bodies)}
    <body name="{_CAM_BODY}" mocap="true"><camera name="{_CAM_BODY}" fovy="45"/></body>
  </worldbody>
</mujoco>"""
        model = mj.MjModel.from_xml_string(xml)
        # map z clip planes are fractions of the model extent; set them absolutely
        model.vis.map.znear = NEAR_CLIP_M / max(model.stat.extent, 1e-6)
        model.vis.map.zfar = 40.0 / max(model.stat.extent, 1e-6)
        data = mj.MjData(model)

        def mocap_of(name: str) -> int:
            return int(model.body_mocapid[mj.mj_name2id(model, mj.mjtObj.mjOBJ_BODY, name)])

        self._instance_mocap = {key: mocap_of("i/" + "/".join(key)) for key in instance_keys}
        self._rover_mocap = {rid: [mocap_of(f"r/{rid}/{k}") for k in range(n)] for rid, n in rover_parts.items()}
        self._cam_mocap = mocap_of(_CAM_BODY)
        self._cam_id = mj.mj_name2id(model, mj.mjtObj.mjOBJ_CAMERA, _CAM_BODY)
        for r in self._renderers.values():
            r.close()
        self._model, self._data, self._renderers = model, data, {}
        self._parked = {k for k in self._parked if k in self._instance_mocap and k not in self.instance_prims}
        self._dirty = False
        self.builds += 1

    def _sync(self) -> None:
        d = self._data
        for key, mid in self._instance_mocap.items():
            prim = self.instance_prims.get(key)
            if prim is None or key in self._parked:
                d.mocap_pos[mid] = (0.0, 0.0, PARKED_Z_M)
                continue
            pos: Point3 = self.positions.get(key, Point3(x=prim.center[0], y=prim.center[1], z=prim.center[2]))
            ox, oy, oz = self._offset(key[0])
            d.mocap_pos[mid] = (pos.x + ox, pos.y + oy, pos.z + oz)
            d.mocap_quat[mid] = _quat(self.yaws.get(key, prim.yaw))
        for rid, rover in self.rovers.items():
            ox, oy, oz = self._offset(rover.store_id)
            for mid, (prim, (wx, wy, wz), yaw) in zip(
                self._rover_mocap.get(rid, []), self._rover_prims(rover), strict=False
            ):
                d.mocap_pos[mid] = (wx + ox, wy + oy, wz + oz)
                d.mocap_quat[mid] = _quat(yaw, prim.pitch)

    def _renderer(self, w: int, h: int) -> Any:
        r = self._renderers.get((w, h))
        if r is None:
            r = self._mj.Renderer(self._model, height=h, width=w)
            self._renderers[(w, h)] = r
        return r

    def render(self, camera_id: str, store_id: str, cam: CameraPose) -> tuple[bytes, int, int] | None:
        self.renders.append((camera_id, store_id))
        if store_id not in self.stores:
            return grey_jpeg(cam.w, cam.h)
        w, h = min(cam.w, MAX_FRAME_W), min(cam.h, MAX_FRAME_H)
        with self._lock:
            if self._dirty or self._model is None:
                self._build()
            self._sync()
            ox, oy, oz = self._offset(store_id)
            right, up, fwd = cam.basis()
            d = self._data
            d.mocap_pos[self._cam_mocap] = (cam.x + ox, cam.y + oy, cam.z + oz)
            d.mocap_quat[self._cam_mocap] = _quat_from_axes(right, up, (-fwd[0], -fwd[1], -fwd[2]))
            self._model.cam_fovy[self._cam_id] = math.degrees(2 * math.atan(math.tan(cam.hfov / 2) * h / w))
            self._mj.mj_kinematics(self._model, d)
            self._mj.mj_camlight(self._model, d)
            renderer = self._renderer(w, h)
            renderer.update_scene(d, camera=self._cam_id)
            pixels = renderer.render()
        from PIL import Image

        out = io.BytesIO()
        Image.fromarray(pixels).save(out, format="JPEG", quality=self.jpeg_quality)
        return out.getvalue(), w, h
