"""Isaac Sim worker process: MANY stores + MANY SO-101 rovers in one stage (composition root of the Isaac process,
CLAUDE.md §4 rule 2; it imports only arms.sim, contracts and utils).

Runs on the GPU host "vhagar" (arms/sim/DEPLOY.md) and connects OUTBOUND to the gateway at
``ws <PICKY_GATEWAY_URL>/ws/sim`` (contracts/sim.py):

    /data/$USER/isaacsim/python.sh -m arms.sim.app                  # Isaac 5.x/6.x, headless, every store in data/
    uv run python -m arms.sim.app --backend sketch                   # no Isaac: same protocol, Pillow-rendered frames
    uv run python -m arms.sim.app --backend null                     # no rendering at all (grey frames)

Threads: the websocket thread (``GatewayLink``) ONLY parses + enqueues commands into ``inbox`` and sends what the main
loop queues; it never touches USD. The main loop owns Isaac: drain inbox -> ``WorkerCore.submit``, ``WorkerCore.tick``
(per-robot action state machines), render the cameras ``WorkerCore.stream_due`` names -> JPEG -> ``CameraFrame``,
``app.update()``. Scenes = ``arms.sim.scene_spec`` primitives turned into USD prims with ``pxr``; each store is a
translation-only ``/World/Stores/<store>`` Xform, so every coordinate below it is store-local like the wire.

Isaac notes (written against the 5.x/6.x docs; first GPU run still to do, see README "Known limitations"):
- ``SimulationApp`` must exist before any ``omni`` / ``pxr`` import (``require_isaac`` returns the class).
- Motion is set-pose (kinematic); no physics timeline is played, so no rigid bodies / joints are needed for the
  magic grasp (the held unit's Xform follows the gripper every tick).
- Cameras: one ``UsdGeom.Camera`` + replicator render product + ``rgb`` annotator per camera id, created lazily on the
  first capture/stream; idle render products are paused (``hydra_texture.set_updates_enabled``) to save GPU.
- SO-101: primitives by default. ``--so101-urdf <path to so101_new_calib.urdf>`` (from the SO-ARM100 / LeRobot repo)
  imports the real arm onto each lift carriage with the Isaac URDF importer (experimental, joints left at zero).
"""

import argparse
import json
import math
import os
import queue
import sys
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit

from arms.sim.geometry import CameraPose
from arms.sim.labels import label_png
from arms.sim.scene_spec import LIFT_REF_M, PrimSpec, RoverSpec, SceneSpec, Vec3, camera_prim_path, usd_name
from arms.sim.worker import NullBackend, SimBackend, WorkerCore
from contracts.common import ARM_MOUNT_X_M, Point3, Pose2D
from contracts.sim import CameraFrame, SimLog, command_error_result, parse_gateway_to_sim
from utils.log import get_logger

log = get_logger(__name__)

JPEG_QUALITY = 75
LOOP_HZ = 60.0
MAX_DT_S = 0.25
RECONNECT_MAX_S = 10.0
CAMERA_IDLE_PAUSE_S = 5.0
DEFAULT_LABEL_DIR = "/tmp/picky-sim-labels"


class IsaacUnavailable(RuntimeError):
    """Isaac Sim cannot run in this Python (not installed / unsupported OS)."""


ISAAC_HELP = (
    "Isaac Sim (5.x / 6.x) is not available in this Python. It runs only on Linux or Windows with an RT-core NVIDIA "
    "GPU (RTX 4080 / 16 GB or better; A100/H100 do not work). On vhagar run the worker with "
    "/data/$USER/isaacsim/python.sh (arms/sim/DEPLOY.md). Without Isaac: `python -m arms.sim.app --backend sketch` "
    "exercises the same sim link with Pillow-rendered frames, and the pipeline uses the 2D robot fake by default."
)


def require_isaac() -> Any:
    """Return ``SimulationApp`` (Isaac 5.x/6.x ``isaacsim``, else 4.x ``omni.isaac.kit``) or raise IsaacUnavailable."""
    if sys.platform == "darwin":
        raise IsaacUnavailable(ISAAC_HELP)
    try:
        from isaacsim import SimulationApp  # type: ignore[import-not-found]

        return SimulationApp
    except ImportError:
        pass
    try:
        from omni.isaac.kit import SimulationApp  # type: ignore[import-not-found,no-redef]

        return SimulationApp
    except ImportError as exc:
        raise IsaacUnavailable(ISAAC_HELP) from exc


def gateway_ws_url(gateway_url: str, token: str | None = None) -> str:
    """http(s)://host[/prefix] -> ws(s)://host[/prefix]/ws/sim[?token=...] (the query token is only an early reject)."""
    parts = urlsplit(gateway_url)
    scheme = {"https": "wss", "wss": "wss"}.get(parts.scheme, "ws")
    path = parts.path.rstrip("/") + "/ws/sim"
    query = f"token={quote(token)}" if token else ""
    return urlunsplit((scheme, parts.netloc, path, query, ""))


# --------------------------------------------------------------------------------------------------------------------
# Websocket thread (only parses + enqueues; never touches USD)
# --------------------------------------------------------------------------------------------------------------------
class GatewayLink(threading.Thread):
    """Outbound websocket client with reconnect. Main loop reads ``inbox`` items: ("connected", SimWelcome),
    ("disconnected", reason), ("cmd", GatewayToSim). Main loop sends with ``send(msg)`` / ``offer_frame(frame)``."""

    def __init__(self, url: str, hello: Any, recv_poll_s: float = 0.01) -> None:
        super().__init__(name="picky-sim-link", daemon=True)
        self.url = url
        self.hello = hello  # callable -> SimHello (hosting changes after resets)
        self.recv_poll_s = recv_poll_s
        self.inbox: queue.Queue[tuple[str, Any]] = queue.Queue()
        self._out: queue.Queue[str] = queue.Queue()
        self._frames: dict[str, str] = {}
        self._frame_lock = threading.Lock()
        self._stop = threading.Event()
        self.connected = threading.Event()
        self.rejected: str | None = None

    def send(self, msg: Any) -> None:
        """Queue a SimMessage (dropped while disconnected; the gateway resyncs from RobotState on welcome)."""
        if not self.connected.is_set():
            return
        if isinstance(msg, CameraFrame):
            self.offer_frame(msg)
            return
        self._out.put(msg.model_dump_json() if hasattr(msg, "model_dump_json") else json.dumps(msg))

    def offer_frame(self, frame: CameraFrame) -> None:
        """Unsolicited stream frames: keep only the latest per camera (never back up the socket)."""
        if self.connected.is_set():
            with self._frame_lock:
                self._frames[frame.camera_id or frame.frame_id] = frame.model_dump_json()

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        from websockets.exceptions import ConnectionClosed
        from websockets.sync.client import connect

        backoff = 1.0
        while not self._stop.is_set():
            try:
                with connect(self.url, open_timeout=10, max_size=None, close_timeout=2) as ws:
                    ws.send(self.hello().model_dump_json())
                    welcome = parse_gateway_to_sim(ws.recv(timeout=15))
                    if getattr(welcome, "type", None) != "welcome" or not welcome.ok:
                        err = getattr(welcome, "error", None) or f"unexpected {getattr(welcome, 'type', welcome)}"
                        self.rejected = err
                        log.error("gateway rejected sim worker: %s", err)
                        backoff = RECONNECT_MAX_S  # contracts / token mismatch: no point hammering
                        raise ConnectionError(err)
                    self.rejected = None
                    log.info("sim link up: %s (session %s)", self.url.split("?")[0], welcome.session_id)
                    backoff = 1.0
                    self._drain_out()
                    self.connected.set()
                    self.inbox.put(("connected", welcome))
                    self._pump(ws)
            except (ConnectionClosed, OSError, TimeoutError, ConnectionError) as exc:
                log.warning("sim link down (%s); retry in %.0fs", exc, backoff)
            except Exception:  # noqa: BLE001 - keep the worker alive
                log.exception("sim link crashed; retry in %.0fs", backoff)
            if self.connected.is_set():
                self.connected.clear()
                self.inbox.put(("disconnected", "link lost"))
            self._stop.wait(backoff)
            backoff = min(backoff * 2, RECONNECT_MAX_S)

    def _drain_out(self) -> None:
        while not self._out.empty():
            self._out.get_nowait()
        with self._frame_lock:
            self._frames = {}

    def _pump(self, ws: Any) -> None:
        while not self._stop.is_set():
            while True:
                try:
                    ws.send(self._out.get_nowait())
                except queue.Empty:
                    break
            with self._frame_lock:
                frames, self._frames = list(self._frames.values()), {}
            for frame in frames:
                ws.send(frame)
            try:
                raw = ws.recv(timeout=self.recv_poll_s)
            except TimeoutError:
                continue
            try:
                self.inbox.put(("cmd", parse_gateway_to_sim(raw)))
            except Exception as exc:  # noqa: BLE001 - bad message: answer it if it is a command, keep going
                reply = command_error_result(raw, exc)
                msg = reply if reply is not None else SimLog(level="error", message=f"unparsable message: {exc}"[:500])
                self._out.put(msg.model_dump_json())


# --------------------------------------------------------------------------------------------------------------------
# Isaac backend (pxr scene from SceneSpec / RoverSpec, set-pose rovers, replicator cameras)
# --------------------------------------------------------------------------------------------------------------------
def _rgb(hex_color: str) -> tuple[float, float, float]:
    h = hex_color.lstrip("#")
    return int(h[0:2], 16) / 255, int(h[2:4], 16) / 255, int(h[4:6], 16) / 255


def encode_jpeg(rgba: Any, quality: int = JPEG_QUALITY) -> bytes | None:
    """RGBA/RGB uint8 array -> JPEG bytes via cv2 (preferred) or Pillow; None when neither is installed."""
    import numpy as np

    arr = np.ascontiguousarray(np.asarray(rgba)[..., :3]).astype("uint8")
    try:
        import cv2  # type: ignore[import-not-found]

        ok, buf = cv2.imencode(".jpg", arr[..., ::-1], [int(cv2.IMWRITE_JPEG_QUALITY), quality])
        return bytes(buf) if ok else None
    except ImportError:
        pass
    try:
        import io

        from PIL import Image

        out = io.BytesIO()
        Image.fromarray(arr).save(out, format="JPEG", quality=quality)
        return out.getvalue()
    except ImportError:
        return None


class _IsaacCamera:
    def __init__(self, path: str, op: Any, rp: Any, annotator: Any, w: int, h: int) -> None:
        self.path, self.op, self.rp, self.annotator, self.w, self.h = path, op, rp, annotator, w, h
        self.last_rows: Any = None
        self.last_used = 0.0
        self.active = True


class IsaacBackend:
    """Implements worker.SimBackend on a live SimulationApp. Only call from the main thread."""

    camera_source = "isaac"

    def __init__(self, sim_app: Any, *, label_dir: str = DEFAULT_LABEL_DIR, so101_urdf: str | None = None) -> None:
        import omni.usd  # type: ignore[import-not-found]
        from pxr import UsdGeom, UsdLux  # type: ignore[import-not-found]

        self.app = sim_app
        ctx = omni.usd.get_context()
        ctx.new_stage()
        self.stage = ctx.get_stage()
        UsdGeom.SetStageUpAxis(self.stage, UsdGeom.Tokens.z)
        UsdGeom.SetStageMetersPerUnit(self.stage, 1.0)
        UsdGeom.Xform.Define(self.stage, "/World")
        UsdGeom.Xform.Define(self.stage, "/World/Stores")
        dome = UsdLux.DomeLight.Define(self.stage, "/World/Lights/Dome")
        dome.CreateIntensityAttr(600.0)
        self.label_dir = Path(label_dir)
        self.label_dir.mkdir(parents=True, exist_ok=True)
        self.so101_urdf = so101_urdf
        self._store_roots: dict[str, str] = {}
        self._inst_paths: dict[tuple[str, str], str] = {}
        self._robots: dict[str, dict[str, Any]] = {}
        self._cams: dict[str, _IsaacCamera] = {}
        self._materials: dict[str, Any] = {}
        self._specs: dict[str, SceneSpec] = {}
        self._dirty = True

    # -- materials -----------------------------------------------------------------------------------------------------
    def _color_material(self, hex_color: str) -> Any:
        from pxr import Gf, Sdf, UsdShade  # type: ignore[import-not-found]

        key = f"color_{hex_color.lstrip('#').lower()}"
        if key not in self._materials:
            path = f"/World/Looks/{key}"
            mat = UsdShade.Material.Define(self.stage, path)
            sh = UsdShade.Shader.Define(self.stage, f"{path}/Shader")
            sh.CreateIdAttr("UsdPreviewSurface")
            sh.CreateInput("diffuseColor", Sdf.ValueTypeNames.Color3f).Set(Gf.Vec3f(*_rgb(hex_color)))
            sh.CreateInput("roughness", Sdf.ValueTypeNames.Float).Set(0.55)
            mat.CreateSurfaceOutput().ConnectToSource(sh.ConnectableAPI(), "surface")
            self._materials[key] = mat
        return self._materials[key]

    def _label_material(self, spec: SceneSpec, prim: PrimSpec) -> Any:
        from pxr import Sdf, UsdShade  # type: ignore[import-not-found]

        sku = prim.sku or "unknown"
        key = f"label_{usd_name(sku)}_{prim.price_cents or 0}"
        if key in self._materials:
            return self._materials[key]
        product = spec.products.get(sku)
        png = label_png(
            prim.label_text or sku,
            brand=product.brand if product else None,
            size=product.size if product else None,
            price_cents=prim.price_cents,
            barcode=prim.barcode,
            color=prim.color,
        )
        if png is None:
            self._materials[key] = self._color_material(prim.color)
            return self._materials[key]
        file = self.label_dir / f"{key}.png"
        file.write_bytes(png)
        path = f"/World/Looks/{key}"
        mat = UsdShade.Material.Define(self.stage, path)
        sh = UsdShade.Shader.Define(self.stage, f"{path}/Shader")
        sh.CreateIdAttr("UsdPreviewSurface")
        sh.CreateInput("roughness", Sdf.ValueTypeNames.Float).Set(0.5)
        reader = UsdShade.Shader.Define(self.stage, f"{path}/stReader")
        reader.CreateIdAttr("UsdPrimvarReader_float2")
        reader.CreateInput("varname", Sdf.ValueTypeNames.String).Set("st")
        tex = UsdShade.Shader.Define(self.stage, f"{path}/Texture")
        tex.CreateIdAttr("UsdUVTexture")
        tex.CreateInput("file", Sdf.ValueTypeNames.Asset).Set(str(file))
        tex.CreateInput("st", Sdf.ValueTypeNames.Float2).ConnectToSource(reader.ConnectableAPI(), "result")
        tex.CreateOutput("rgb", Sdf.ValueTypeNames.Float3)
        sh.CreateInput("diffuseColor", Sdf.ValueTypeNames.Color3f).ConnectToSource(tex.ConnectableAPI(), "rgb")
        mat.CreateSurfaceOutput().ConnectToSource(sh.ConnectableAPI(), "surface")
        self._materials[key] = mat
        return mat

    @staticmethod
    def _bind(prim: Any, mat: Any) -> None:
        from pxr import UsdShade  # type: ignore[import-not-found]

        UsdShade.MaterialBindingAPI.Apply(prim).Bind(mat)

    # -- prims ---------------------------------------------------------------------------------------------------------
    def _geom(self, path: str, shape: str) -> Any:
        from pxr import UsdGeom  # type: ignore[import-not-found]

        if shape == "cylinder":
            g = UsdGeom.Cylinder.Define(self.stage, path)
            g.CreateRadiusAttr(0.5)
            g.CreateHeightAttr(1.0)
            g.CreateAxisAttr(UsdGeom.Tokens.z)
        elif shape == "sphere":
            g = UsdGeom.Sphere.Define(self.stage, path)
            g.CreateRadiusAttr(0.5)
        else:
            g = UsdGeom.Cube.Define(self.stage, path)
            g.CreateSizeAttr(1.0)
        return g

    def _define(self, spec: PrimSpec, path: str | None = None) -> Any:
        """One scaled primitive (fixtures, walls, rover parts)."""
        from pxr import Gf, UsdGeom, UsdLux, UsdPhysics  # type: ignore[import-not-found]

        path = path or spec.path
        if spec.role == "light":
            light = UsdLux.RectLight.Define(self.stage, path)
            light.CreateWidthAttr(spec.size[0])
            light.CreateHeightAttr(spec.size[1])
            light.CreateIntensityAttr(9000.0)
            UsdGeom.XformCommonAPI(light).SetTranslate(Gf.Vec3d(*spec.center))  # RectLight emits along -Z (down)
            return light
        g = self._geom(path, spec.shape)
        xf = UsdGeom.XformCommonAPI(g)
        xf.SetTranslate(Gf.Vec3d(*spec.center))
        xf.SetRotate(Gf.Vec3f(0.0, math.degrees(spec.pitch), math.degrees(spec.yaw)))
        xf.SetScale(Gf.Vec3f(*spec.size))
        g.CreateDisplayColorAttr([Gf.Vec3f(*_rgb(spec.color))])
        self._bind(g.GetPrim(), self._color_material(spec.color))
        if spec.collider:
            UsdPhysics.CollisionAPI.Apply(g.GetPrim())
        return g

    def _define_unit(self, spec: SceneSpec | None, prim: PrimSpec) -> str:
        """Product unit: Xform (translate + yaw) with a scaled body and, for boxes / cans, a textured label quad."""
        from pxr import Gf, Sdf, UsdGeom  # type: ignore[import-not-found]

        root = UsdGeom.Xform.Define(self.stage, prim.path)
        xf = UsdGeom.XformCommonAPI(root)
        xf.SetTranslate(Gf.Vec3d(*prim.center))
        xf.SetRotate(Gf.Vec3f(0.0, 0.0, math.degrees(prim.yaw)))
        body = self._geom(f"{prim.path}/body", prim.shape)
        UsdGeom.XformCommonAPI(body).SetScale(Gf.Vec3f(*prim.size))
        body.CreateDisplayColorAttr([Gf.Vec3f(*_rgb(prim.color))])
        self._bind(body.GetPrim(), self._color_material(prim.color))
        if prim.shape != "sphere" and spec is not None:
            w, d, h = prim.size
            hw, hh, y = w * 0.46, h * 0.42, -d / 2 - 0.0015
            mesh = UsdGeom.Mesh.Define(self.stage, f"{prim.path}/label")
            mesh.CreatePointsAttr(
                [Gf.Vec3f(-hw, y, -hh), Gf.Vec3f(hw, y, -hh), Gf.Vec3f(hw, y, hh), Gf.Vec3f(-hw, y, hh)]
            )
            mesh.CreateFaceVertexCountsAttr([4])
            mesh.CreateFaceVertexIndicesAttr([0, 1, 2, 3])
            mesh.CreateNormalsAttr([Gf.Vec3f(0, -1, 0)] * 4)
            mesh.SetNormalsInterpolation(UsdGeom.Tokens.vertex)
            mesh.CreateDoubleSidedAttr(True)
            st = UsdGeom.PrimvarsAPI(mesh).CreatePrimvar(
                "st", Sdf.ValueTypeNames.TexCoord2fArray, UsdGeom.Tokens.vertex
            )
            st.Set([Gf.Vec2f(0, 0), Gf.Vec2f(1, 0), Gf.Vec2f(1, 1), Gf.Vec2f(0, 1)])
            self._bind(mesh.GetPrim(), self._label_material(spec, prim))
        p = root.GetPrim()
        for key in ("sku", "instance_id", "slot_id", "lot_id", "barcode"):
            val = getattr(prim, key)
            if val is not None:
                p.CreateAttribute(f"picky:{key}", Sdf.ValueTypeNames.String).Set(str(val))
        if prim.freshness is not None:
            p.CreateAttribute("picky:freshness", Sdf.ValueTypeNames.Float).Set(prim.freshness)
        self._inst_paths[(prim.store_id or "", prim.instance_id or "")] = prim.path
        return prim.path

    # -- SimBackend ----------------------------------------------------------------------------------------------------
    def build_store(self, spec: SceneSpec, offset: Vec3) -> None:
        from pxr import Gf, UsdGeom  # type: ignore[import-not-found]

        root = spec.root_path
        if self.stage.GetPrimAtPath(root):
            self.stage.RemovePrim(root)
        for cid in [c for c, cam in self._cams.items() if cam.path.startswith(root + "/")]:
            self._drop_camera(cid)
        for key in [k for k in self._inst_paths if k[0] == spec.store_id]:
            del self._inst_paths[key]
        xform = UsdGeom.Xform.Define(self.stage, root)
        UsdGeom.XformCommonAPI(xform).SetTranslate(Gf.Vec3d(*offset))
        self._store_roots[spec.store_id] = root
        # (no Sdf.ChangeBlock: the Usd/UsdShade APIs used here read the stage while authoring)
        self._specs[spec.store_id] = spec
        for p in spec.prims:
            if p.role == "product":
                self._define_unit(spec, p)
            else:
                self._define(p)
        for _ in range(5):
            self.app.update()
        self._dirty = True
        log.info(
            "store %s built at %s: %d prims, %d units", spec.store_id, offset, len(spec.prims), len(spec.instances)
        )

    def set_store_offset(self, store_id: str, offset: Vec3) -> None:
        from pxr import Gf, UsdGeom  # type: ignore[import-not-found]

        root = self._store_roots.get(store_id)
        if root:
            UsdGeom.XformCommonAPI(self.stage.GetPrimAtPath(root)).SetTranslate(Gf.Vec3d(*offset))
            self._dirty = True

    def spawn_robot(self, rover: RoverSpec) -> None:
        from pxr import Gf, UsdGeom  # type: ignore[import-not-found]

        if self.stage.GetPrimAtPath(rover.root_path):
            self.stage.RemovePrim(rover.root_path)
        root = UsdGeom.Xform.Define(self.stage, rover.root_path)
        UsdGeom.Xform.Define(self.stage, f"{rover.root_path}/Base")
        lift = UsdGeom.Xform.Define(self.stage, f"{rover.root_path}/Lift")
        lift_op = lift.AddTranslateOp()
        arm = UsdGeom.Xform.Define(self.stage, f"{rover.root_path}/Lift/Arm")
        pivot = arm.AddTranslateOp(opSuffix="pivot")
        scale = arm.AddScaleOp()
        unpivot = arm.AddTranslateOp(opSuffix="unpivot")
        pivot.Set(Gf.Vec3d(ARM_MOUNT_X_M, 0, 0))
        unpivot.Set(Gf.Vec3d(-ARM_MOUNT_X_M, 0, 0))
        urdf_ok = self.so101_urdf is not None and self._import_so101(f"{rover.root_path}/Lift/SO101")
        for p in rover.prims:
            if p.group == "arm" and urdf_ok:
                continue
            self._define(p)
        self._robots[rover.robot_id] = {
            "xf": UsdGeom.XformCommonAPI(root),
            "lift": lift_op,
            "scale": scale,
            "root": rover.root_path,
        }
        self._dirty = True

    def _import_so101(self, dest: str) -> bool:
        """Import the SO-101 URDF under ``dest`` (Isaac URDF importer). False (primitives used) on any failure."""
        try:
            import omni.kit.commands  # type: ignore[import-not-found]

            ok, cfg = omni.kit.commands.execute("URDFCreateImportConfig")
            cfg.merge_fixed_joints = False
            cfg.fix_base = True
            cfg.make_default_prim = False
            cfg.create_physics_scene = False
            ok, path = omni.kit.commands.execute(
                "URDFParseAndImportFile", urdf_path=self.so101_urdf, import_config=cfg, dest_path=""
            )
            if not ok or not path:
                return False
            from pxr import Sdf  # type: ignore[import-not-found]

            Sdf.CopySpec(self.stage.GetRootLayer(), path, self.stage.GetRootLayer(), dest)
            self.stage.RemovePrim(path)
            return True
        except Exception as exc:  # noqa: BLE001 - fall back to the primitive arm
            log.warning("SO-101 URDF import failed (%s); using primitives", exc)
            return False

    def remove_robot(self, robot_id: str) -> None:
        info = self._robots.pop(robot_id, None)
        if info and self.stage.GetPrimAtPath(info["root"]):
            self.stage.RemovePrim(info["root"])
        self._dirty = True

    def set_robot_pose(self, robot_id: str, pose: Pose2D, lift_height_m: float, arm_ext_m: float) -> None:
        from pxr import Gf  # type: ignore[import-not-found]

        info = self._robots.get(robot_id)
        if info is None:
            return
        info["xf"].SetTranslate(Gf.Vec3d(pose.x, pose.y, 0.0))
        info["xf"].SetRotate(Gf.Vec3f(0.0, 0.0, math.degrees(pose.theta)))
        info["lift"].Set(Gf.Vec3d(0.0, 0.0, lift_height_m - LIFT_REF_M))
        info["scale"].Set(Gf.Vec3f(max(0.3, min(1.0, arm_ext_m / 0.33)), 1.0, 1.0))
        self._dirty = True

    def add_instance(self, prim: PrimSpec) -> None:
        spec = self._specs.get(prim.store_id or "")
        self._define_unit(spec, prim)
        self._dirty = True

    def remove_instance(self, store_id: str, instance_id: str) -> None:
        path = self._inst_paths.pop((store_id, instance_id), None)
        if path and self.stage.GetPrimAtPath(path):
            self.stage.RemovePrim(path)
        self._dirty = True

    def set_instance_pose(self, store_id: str, instance_id: str, position: Point3, yaw: float | None = None) -> None:
        from pxr import Gf, UsdGeom  # type: ignore[import-not-found]

        path = self._inst_paths.get((store_id, instance_id))
        if not path:
            return
        xf = UsdGeom.XformCommonAPI(self.stage.GetPrimAtPath(path))
        xf.SetTranslate(Gf.Vec3d(position.x, position.y, position.z))
        if yaw is not None:
            xf.SetRotate(Gf.Vec3f(0.0, 0.0, math.degrees(yaw)))
        self._dirty = True

    # -- cameras -------------------------------------------------------------------------------------------------------
    def _camera(self, camera_id: str, store_id: str, cam: CameraPose) -> _IsaacCamera:
        existing = self._cams.get(camera_id)
        if existing is not None and (existing.w, existing.h) == (cam.w, cam.h):
            return existing
        if existing is not None:
            self._drop_camera(camera_id)
        import omni.replicator.core as rep  # type: ignore[import-not-found]
        from pxr import Gf, UsdGeom  # type: ignore[import-not-found]

        path = camera_prim_path(store_id, camera_id)
        c = UsdGeom.Camera.Define(self.stage, path)
        h_ap = 20.955
        c.CreateHorizontalApertureAttr(h_ap)
        c.CreateVerticalApertureAttr(h_ap * cam.h / cam.w)
        c.CreateFocalLengthAttr(h_ap / (2 * math.tan(cam.hfov / 2)))
        c.CreateClippingRangeAttr(Gf.Vec2f(0.03, 200.0))
        op = UsdGeom.Xformable(c).AddTransformOp()
        rp = rep.create.render_product(path, (cam.w, cam.h))
        annotator = rep.AnnotatorRegistry.get_annotator("rgb")
        annotator.attach([rp])
        made = _IsaacCamera(path, op, rp, annotator, cam.w, cam.h)
        self._cams[camera_id] = made
        return made

    def _drop_camera(self, camera_id: str) -> None:
        cam = self._cams.pop(camera_id, None)
        if cam is None:
            return
        try:
            cam.annotator.detach([cam.rp])
            cam.rp.destroy()
        except Exception:  # noqa: BLE001 - best effort
            pass

    def _set_active(self, cam: _IsaacCamera, active: bool) -> None:
        if cam.active == active:
            return
        try:
            cam.rp.hydra_texture.set_updates_enabled(active)
            cam.active = active
        except Exception:  # noqa: BLE001 - older replicator: keep rendering
            cam.active = True

    def render(self, camera_id: str, store_id: str, cam: CameraPose) -> tuple[bytes, int, int] | None:
        from pxr import Gf  # type: ignore[import-not-found]

        c = self._camera(camera_id, store_id, cam)
        fresh = c.last_rows is None
        rows = cam.usd_matrix_rows()
        moved = rows != c.last_rows
        c.op.Set(Gf.Matrix4d(*[v for row in rows for v in row]))
        c.last_rows = rows
        now = time.monotonic()
        c.last_used = now
        self._set_active(c, True)
        for other in self._cams.values():
            if other is not c and now - other.last_used > CAMERA_IDLE_PAUSE_S:
                self._set_active(other, False)
        # replicator annotators lag a frame behind the stage: update twice when the view changed
        for _ in range(3 if fresh else (2 if (moved or self._dirty) else 1)):
            self.app.update()
        self._dirty = False
        data = c.annotator.get_data()
        if data is None or getattr(data, "size", 0) == 0:
            return None
        jpeg = encode_jpeg(data)
        if jpeg is None:
            log.error("no JPEG encoder: pip install pillow (or opencv-python-headless) into the worker's deps dir")
            return None
        return jpeg, c.w, c.h

    def step(self) -> None:
        self.app.update()
        self._dirty = False


# --------------------------------------------------------------------------------------------------------------------
# Main loop
# --------------------------------------------------------------------------------------------------------------------
def run(
    backend: SimBackend,
    link: GatewayLink | None,
    core: WorkerCore,
    is_running: Any = lambda: True,
    max_seconds: float | None = None,
) -> None:
    """Drain commands, step actions, render due camera streams. Returns when ``is_running()`` is False."""
    period = 1.0 / LOOP_HZ
    last = time.monotonic()
    started = last
    step = getattr(backend, "step", None)
    while is_running():
        now = time.monotonic()
        if max_seconds is not None and now - started > max_seconds:
            break
        dt, last = min(MAX_DT_S, now - last), now
        if link is not None:
            while True:
                try:
                    kind, item = link.inbox.get_nowait()
                except queue.Empty:
                    break
                if kind == "cmd":
                    core.submit(item)
                elif kind == "connected":
                    core.on_connected()
                elif kind == "disconnected":
                    core.on_disconnected(str(item))
        core.tick(dt)
        rendered = False
        if link is not None and link.connected.is_set():
            for camera_id in core.stream_due():
                try:
                    frame = core.capture(camera_id, stream=True)
                except Exception as exc:  # noqa: BLE001 - a broken camera must not kill the loop
                    log.warning("stream %s failed: %s", camera_id, exc)
                    continue
                rendered = True
                if frame is not None:
                    link.offer_frame(frame)
        if step is not None and not rendered:
            step()
        spent = time.monotonic() - now
        if spent < period:
            time.sleep(period - spent)


def make_backend(kind: str, args: argparse.Namespace) -> tuple[Any, SimBackend]:
    """(SimulationApp or None, backend). Raises IsaacUnavailable for 'isaac' without Isaac."""
    if kind == "isaac":
        SimulationApp = require_isaac()
        sim_app = SimulationApp({"headless": not args.no_headless, "width": args.width, "height": args.height})
        return sim_app, IsaacBackend(sim_app, label_dir=args.label_dir, so101_urdf=args.so101_urdf)
    if kind in ("sketch", "kinematic"):
        from arms.sim.sketch import SketchBackend

        return None, SketchBackend()
    return None, NullBackend()


def main(argv: list[str] | None = None) -> int:
    from utils.config import get_settings

    settings = get_settings()
    ap = argparse.ArgumentParser(prog="python -m arms.sim.app", description="Picky Isaac Sim worker (multi-store)")
    ap.add_argument("--stores", default=os.environ.get("PICKY_SIM_STORES", ""), help="comma list (default: all)")
    ap.add_argument("--store", default=None, help="legacy single store (same as --stores <id>)")
    ap.add_argument("--gateway-url", default=settings.picky_gateway_url)
    ap.add_argument("--token", default=settings.picky_sim_token)
    ap.add_argument("--backend", choices=["isaac", "sketch", "kinematic", "null"], default="isaac")
    ap.add_argument("--width", type=int, default=1280, help="Isaac app window (cameras use their mount size)")
    ap.add_argument("--height", type=int, default=720)
    ap.add_argument("--no-headless", action="store_true", help="open the Isaac UI (needs a display)")
    ap.add_argument("--worker-id", default=os.environ.get("PICKY_SIM_WORKER_ID", "isaac-0"))
    ap.add_argument("--label-dir", default=os.environ.get("PICKY_SIM_LABEL_DIR", DEFAULT_LABEL_DIR))
    ap.add_argument("--so101-urdf", default=os.environ.get("PICKY_SO101_URDF") or None)
    ap.add_argument("--max-seconds", type=float, default=None, help="exit after this long (smoke tests)")
    args = ap.parse_args(argv)

    try:
        sim_app, backend = make_backend(args.backend, args)
    except IsaacUnavailable as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    link: GatewayLink | None = None
    core = WorkerCore(
        backend, emit=lambda msg: link.send(msg) if link else None, default_store=settings.picky_default_store
    )
    if args.store:
        store_ids = [args.store]
    elif args.stores:
        store_ids = [s.strip() for s in args.stores.split(",") if s.strip()]
    else:
        try:
            from utils.data import list_store_ids

            store_ids = list_store_ids()
        except Exception as exc:  # noqa: BLE001 - the gateway's ResetCmd can push stores later
            log.warning("cannot list stores in data/ (%s); waiting for reset", exc)
            store_ids = []
    loaded = core.load_stores(store_ids)
    log.info("hosting stores %s, robots %s", loaded, sorted(core.rovers))

    sim_version = {"isaac": "isaacsim", "null": "null"}.get(args.backend, "sketch")
    url = gateway_ws_url(args.gateway_url, args.token)
    link = GatewayLink(url, lambda: core.hello(args.token, args.worker_id, sim_version))
    link.start()
    log.info("sim worker (%s) -> %s", args.backend, url.split("?")[0])
    try:
        run(
            backend,
            link,
            core,
            is_running=(sim_app.is_running if sim_app else lambda: True),
            max_seconds=args.max_seconds,
        )
    except KeyboardInterrupt:
        pass
    finally:
        link.stop()
        if sim_app is not None:
            sim_app.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
