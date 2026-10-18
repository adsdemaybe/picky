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

Isaac notes (6.0, verified on vhagar):
- ``SimulationApp`` must exist before any ``omni`` / ``pxr`` import (``require_isaac`` returns the class).
- Motion is set-pose (kinematic); no physics timeline is played, so no rigid bodies / joints are needed for the
  magic grasp (the held unit's Xform follows the gripper every tick).
- Cameras: one ``UsdGeom.Camera`` + replicator render product + ``rgb`` annotator per camera id, created lazily on the
  first capture/stream (first frames via ``rep.orchestrator.step``, then plain ``app.update()``, see
  ``IsaacBackend._update``); idle render products are paused (``hydra_texture.set_updates_enabled``) to save GPU.
- Verified on vhagar (Isaac Sim 6.0.0-rc.59, RTX 5090): two stores + three rovers + all 12 cameras.
- SO-101: primitives by default. ``--so101-urdf <path to so101_new_calib.urdf>`` (from the SO-ARM100 / LeRobot repo)
  imports the real arm onto each lift carriage with the Isaac URDF importer (experimental, joints left at zero).
- Per-item grocery models (``PICKY_ASSET_ROOT`` + ``data/sim/sku_assets.yaml``, arms/sim/ASSETS.md): a resolved SKU's
  units reference ``items/<id>/item.usda`` (or a composite of ``geom.usda`` parts) instead of a box, with our label
  decal (same PNG as the primitive) on top. ``item.usda`` makes the unit root a rigid body: it is set KINEMATIC
  (never simulated; the replicator step may advance a physics timeline) so ~1,900 units stay put and a held unit
  follows its own xformOps. Anything missing -> today's primitive for that SKU.
- Rover USD (``PICKY_ROVER_USD`` -> ``picky_rover.usda``, LeKiwi + SO-101 + lift): referenced per rover under
  ``<rover>/Model/LeKiwi`` as a visual puppet (``arms.sim.rover_puppet``): joints deactivated, rigid bodies and
  colliders removed, base pose = the rover root, lift = an offset on the SO-101 links, arm = IK so the jaws meet the
  held unit. Cameras keep the geometry.py rig. Any failure -> the primitive rover.
- Joint mode (contracts 0.2.2 ``ArmJointsCmd``, learned policies): the puppet's chain is exposed as an
  ``ArmKinematics`` (``arm_kinematics``) and ``set_robot_joints`` poses the SO-101 links straight from 6 joint degrees
  (FK through the USD joint frames). The two policy cameras ('<robot>.policy_side' / '.policy_wrist', 640x480) are
  ordinary lazily created render products with a 0.01 m near clip.
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

from arms.sim import rover_puppet as puppet
from arms.sim.arm_joints import POLICY_CAMERA_PREFIX, POLICY_NEAR_CLIP_M
from arms.sim.assets import (
    ASSET_ROOT_ENV,
    ASSETS_SWITCH_ENV,
    ROVER_USD_ENV,
    SKU_ASSETS_ENV,
    LabelMesh,
    SkuAssets,
    UnitAsset,
    item_normal_to_native,
    item_to_native,
    jitter_deg,
    label_meshes,
    rover_usd_from_env,
)
from arms.sim.director import Director
from arms.sim.geometry import ARM_FULL_EXT_M, ARM_STOW_EXT_M, NEAR_CLIP_M, CameraPose
from arms.sim.labels import label_png, package_kind, package_png, sign_png
from arms.sim.scene_spec import (
    LIFT_REF_M,
    PrimSpec,
    RoverSpec,
    SceneSpec,
    SlotLayout,
    Vec3,
    camera_prim_path,
    usd_name,
)
from arms.sim.visuals import asset_usd, load_visuals
from arms.sim.worker import NullBackend, SimBackend, WorkerCore
from contracts.common import ARM_MOUNT_X_M, Point3, Pose2D
from contracts.sim import CameraFrame, SimLog, command_error_result, parse_gateway_to_sim
from utils.log import get_logger

log = get_logger(__name__)

JPEG_QUALITY = 75
LOOP_HZ = 60.0
MAX_DT_S = 0.25
RECONNECT_MAX_S = 10.0
CAMERA_IDLE_PAUSE_S = 3.0
"""Render products unused this long stop rendering (every enabled one makes each synchronous step slower)."""
RT_SUBFRAMES = 1
"""RTX subframes per synchronous replicator step."""
RT_SUBFRAMES_NEW = 2
"""RTX subframes for the first frames of a new render product."""
RT_WARMUP_STEPS = 2
"""Synchronous replicator steps for a new render product (starts the orchestrator; materials + textures load)."""
IDLE_UPDATE_S = 0.25
"""Idle ``app.update()`` period (4 Hz) when no frame was rendered this tick (frames come from synchronous steps)."""
CEILING_EMISSIVE = 0.6
AMBIENT_ENV = "PICKY_SIM_AMBIENT"
AMBIENT_DEFAULT = 0.1
"""RTX ambient term (``/rtx/sceneDb/ambientLightIntensity``): the stores are closed boxes lit by ceiling RectLights
only, so real-time RTX leaves the shelf interiors (under each board) dark grey; a small uniform ambient lifts them.
``PICKY_SIM_AMBIENT=0`` = Isaac's default (none)."""
WRIST_NEAR_CLIP_PUPPET_M = 0.01
"""Near clip of a USD-puppet rover's wrist camera: the SO-101 jaw tips sit ~5 cm ahead of / 3 cm below the eye, and
NEAR_CLIP_M (0.05) sliced them into two black cross-sections in every wrist frame. The primitive rig keeps NEAR_CLIP_M
(its wrist-camera housing prim must stay clipped)."""
EXTERNAL_LIGHT_SCALE_ENV = "PICKY_EXTERNAL_LIGHT_SCALE"
"""Multiplier on the lights of an external scene USD (``arms.sim.external``; the Genie Sim market is bright)."""
DEFAULT_LABEL_DIR = "/tmp/picky-sim-labels"
TAG_SIZE_M = (0.08, 0.06)
"""Shelf-edge price tag in front of SKUs whose model carries no label (loose produce)."""
PUPPET_DROPS_M = (0.0, 0.05, 0.10, 0.15)
"""How far below its reference height the SO-101 TCP may have to reach (lift at its bottom): ReachTable rows."""


def gf_rows(m: puppet.Mat) -> list[float]:
    """Column-vector 4x4 (rover_puppet) -> the 16 floats of the row-vector Gf.Matrix4d."""
    t = puppet.transpose(m)
    return [v for row in t for v in row]


def from_gf(g: Any) -> puppet.Mat:
    """Gf.Matrix4d (row vectors) -> column-vector 4x4."""
    return tuple(tuple(float(g[j][i]) for j in range(4)) for i in range(4))  # type: ignore[return-value]


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
        self.dirty = True
        """Pose changed (or resumed) since the last synchronous replicator step."""
        self.resumed = False


class IsaacBackend:
    """Implements worker.SimBackend on a live SimulationApp. Only call from the main thread."""

    camera_source = "isaac"

    def __init__(
        self,
        sim_app: Any,
        *,
        label_dir: str = DEFAULT_LABEL_DIR,
        so101_urdf: str | None = None,
        assets: SkuAssets | None = None,
    ) -> None:
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
        self._set_ambient()
        self.label_dir = Path(label_dir)
        self.label_dir.mkdir(parents=True, exist_ok=True)
        self.so101_urdf = so101_urdf
        self.assets = assets or SkuAssets.disabled()
        self._store_roots: dict[str, str] = {}
        self._inst_paths: dict[tuple[str, str], str] = {}
        self._units: dict[tuple[str, str], tuple[Any, Any, float, float]] = {}
        """Model units: (translate op, rotateZ op, z offset of the origin below the center, yaw offset deg)."""
        self._puppet_geo: dict[str, Any] = {}
        """model_usd -> shared puppet geometry (arm chain + reach table), or None when it failed."""
        self._robots: dict[str, dict[str, Any]] = {}
        self._cams: dict[str, _IsaacCamera] = {}
        self._materials: dict[str, Any] = {}
        self._model_bounds: dict[str, tuple[Any, Any] | None] = {}
        self._specs: dict[str, SceneSpec] = {}
        self._ext: dict[tuple[str, str], dict[str, Any]] = {}
        """External-scene units (arms.sim.external): (store, instance) -> {path, yaw, op}."""
        self._ext_base: dict[str, tuple[Any, Any, Any, float]] = {}
        """External prim path -> (store-local matrix at build, inverse parent store-local matrix, box center, yaw)."""
        self._scene_dirty = True
        self._last_update = 0.0
        self._stats_n = 0
        self._stats_s = 0.0

    @staticmethod
    def _set_ambient() -> None:
        try:
            ambient = float(os.environ.get(AMBIENT_ENV, AMBIENT_DEFAULT))
        except ValueError:
            ambient = AMBIENT_DEFAULT
        try:
            import carb.settings  # type: ignore[import-not-found]

            settings = carb.settings.get_settings()
            settings.set("/rtx/sceneDb/ambientLightIntensity", ambient)
            settings.set("/rtx/sceneDb/ambientLightColor", [1.0, 0.98, 0.95])
        except Exception as exc:  # noqa: BLE001 - cosmetic
            log.warning("cannot set the RTX ambient light (%s)", exc)

    # -- materials -----------------------------------------------------------------------------------------------------
    def _color_material(self, hex_color: str, emissive: float = 0.0) -> Any:
        from pxr import Gf, Sdf, UsdShade  # type: ignore[import-not-found]

        key = f"color_{hex_color.lstrip('#').lower()}" + (f"_e{int(emissive * 100)}" if emissive else "")
        if key not in self._materials:
            path = f"/World/Looks/{key}"
            mat = UsdShade.Material.Define(self.stage, path)
            sh = UsdShade.Shader.Define(self.stage, f"{path}/Shader")
            sh.CreateIdAttr("UsdPreviewSurface")
            sh.CreateInput("diffuseColor", Sdf.ValueTypeNames.Color3f).Set(Gf.Vec3f(*_rgb(hex_color)))
            sh.CreateInput("roughness", Sdf.ValueTypeNames.Float).Set(0.55)
            if emissive:
                rgb = _rgb(hex_color)
                sh.CreateInput("emissiveColor", Sdf.ValueTypeNames.Color3f).Set(Gf.Vec3f(*(c * emissive for c in rgb)))
            mat.CreateSurfaceOutput().ConnectToSource(sh.ConnectableAPI(), "surface")
            self._materials[key] = mat
        return self._materials[key]

    def _label_material(self, spec: SceneSpec, prim: PrimSpec, px: tuple[int, int] | None = None) -> Any:
        return self._label_material_for(
            spec, prim.sku or "unknown", prim.label_text, prim.price_cents, prim.barcode, prim.color, px
        )

    def _label_material_for(
        self,
        spec: SceneSpec,
        sku: str,
        text: str | None,
        price_cents: int | None,
        barcode: str | None,
        color: str,
        px: tuple[int, int] | None = None,
    ) -> Any:
        """Our label PNG (labels.label_png, the artwork perception matches) as a UsdPreviewSurface, per SKU + price."""

        key = f"label_{usd_name(sku)}_{price_cents or 0}"
        if key in self._materials:
            return self._materials[key]
        product = spec.products.get(sku)
        kind = package_kind(product.subcategory, product.tags) if product else None
        if product and kind:  # retail package front, pixels in the box face's aspect
            w, _, h = product.asset.dims_m
            png = package_png(
                text or sku,
                kind=kind,
                brand=product.brand,
                size=product.size,
                price_cents=price_cents,
                barcode=barcode,
                color=color,
                px=(320, round(320 * h / w)),
            )
            self._materials[key] = self._texture_material(key, png) if png else self._color_material(color)
            return self._materials[key]
        png = label_png(
            text or sku,
            brand=product.brand if product else None,
            size=product.size if product else None,
            price_cents=price_cents,
            barcode=barcode,
            color=color,
            **({"px": px} if px else {}),
        )
        if png is None:
            self._materials[key] = self._color_material(color)
            return self._materials[key]
        self._materials[key] = self._texture_material(key, png)
        return self._materials[key]

    def _texture_material(self, key: str, png: bytes, emissive: float = 0.0) -> Any:
        """UsdPreviewSurface with ``png`` as diffuse (and, when ``emissive`` > 0, emissive) texture."""
        from pxr import Gf, Sdf, UsdShade  # type: ignore[import-not-found]

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
        if emissive:
            tex.CreateInput("scale", Sdf.ValueTypeNames.Float4).Set(Gf.Vec4f(emissive, emissive, emissive, 1.0))
            sh.CreateInput("emissiveColor", Sdf.ValueTypeNames.Color3f).ConnectToSource(tex.ConnectableAPI(), "rgb")
        mat.CreateSurfaceOutput().ConnectToSource(sh.ConnectableAPI(), "surface")
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
        if spec.role in ("sign", "decor") and (spec.role == "sign" or spec.asset):
            return self._define_dressing(spec, path)
        g = self._geom(path, spec.shape)
        xf = UsdGeom.XformCommonAPI(g)
        xf.SetTranslate(Gf.Vec3d(*spec.center))
        xf.SetRotate(Gf.Vec3f(0.0, math.degrees(spec.pitch), math.degrees(spec.yaw)))
        xf.SetScale(Gf.Vec3f(*spec.size))
        g.CreateDisplayColorAttr([Gf.Vec3f(*_rgb(spec.color))])
        # The ceiling's underside only gets bounce light (the RectLights emit down) and renders black in RTX real-time,
        # right where the ceiling cameras (mounted at ceiling height) look: make it a softly glowing panel instead.
        emissive = CEILING_EMISSIVE if spec.role == "ceiling" else 0.0
        self._bind(g.GetPrim(), self._color_material(spec.color, emissive=emissive))
        if spec.collider:
            UsdPhysics.CollisionAPI.Apply(g.GetPrim())
        return g

    def _define_unit(self, spec: SceneSpec | None, prim: PrimSpec) -> str:
        """Product unit: the SKU's per-item model when it resolves (``prim.render``), else today's primitive."""
        if prim.render == "external":
            path = self._external_unit(spec, prim)
            if path is not None:
                return path
            prim = prim.model_copy(update={"render": "primitive", "external_path": None})
        ua = self.assets.resolve(prim.sku) if prim.render not in ("primitive", "external") else None
        if ua is not None and spec is not None:
            try:
                return self._define_model_unit(spec, prim, ua)
            except Exception as exc:  # noqa: BLE001 - one broken model must not kill the store: primitive instead
                log.warning("unit %s: model %s failed (%s); drawing the primitive", prim.instance_id, ua.asset_id, exc)
                if self.stage.GetPrimAtPath(prim.path):
                    self.stage.RemovePrim(prim.path)
                self._units.pop((prim.store_id or "", prim.instance_id or ""), None)
        return self._define_primitive_unit(spec, prim)

    def _tag_attrs(self, prim: PrimSpec, p: Any) -> None:
        from pxr import Sdf  # type: ignore[import-not-found]

        for key in ("sku", "instance_id", "slot_id", "lot_id", "barcode"):
            val = getattr(prim, key)
            if val is not None:
                p.CreateAttribute(f"picky:{key}", Sdf.ValueTypeNames.String).Set(str(val))
        if prim.freshness is not None:
            p.CreateAttribute("picky:freshness", Sdf.ValueTypeNames.Float).Set(prim.freshness)

    def _define_model_unit(self, spec: SceneSpec, prim: PrimSpec, ua: UnitAsset) -> str:
        """One unit = one Xform referencing the model (render=asset) or holding the composite parts (render=parts).
        xformOps translate . rotateZ . scale (scale_xyz in the model's native axes); origin = bottom center."""
        from pxr import Gf, UsdGeom, UsdPhysics  # type: ignore[import-not-found]

        key = (prim.store_id or "", prim.instance_id or "")
        root = self.stage.DefinePrim(prim.path, "Xform")
        scale: Vec3 = (1.0, 1.0, 1.0)
        if ua.render == "asset" and ua.usd:
            root.GetReferences().AddReference(ua.usd)  # default prim /Item: rigid body + instanceable /Item/Geom
            scale = ua.scale_xyz
        else:
            self._add_parts(prim, ua)
        xf = UsdGeom.Xformable(root)
        xf.ClearXformOpOrder()
        t_op = xf.AddTranslateOp(opSuffix="unit")
        r_op = xf.AddRotateZOp(opSuffix="unit")
        s_op = xf.AddScaleOp(opSuffix="unit")
        h = prim.size[2]
        yaw_off = ua.yaw_offset_deg + ua.yaw_jitter(prim.instance_id or prim.path)
        t_op.Set(Gf.Vec3d(prim.center[0], prim.center[1], prim.center[2] - h / 2))
        r_op.Set(math.degrees(prim.yaw) + yaw_off)
        s_op.Set(Gf.Vec3f(*scale))
        # never simulated: kinematic bodies follow their own xformOps (magic grasp / basket moves) and stay put
        rb = UsdPhysics.RigidBodyAPI.Apply(root)
        rb.CreateKinematicEnabledAttr(True)
        if ua.mass_kg:
            UsdPhysics.MassAPI.Apply(root).CreateMassAttr(float(ua.mass_kg))
        for mesh in label_meshes(ua.label, prim.size):
            self._label_decal(spec, prim, mesh, scale, yaw_off, ua.label.png_px)
        self._tag_attrs(prim, root)
        self._inst_paths[key] = prim.path
        self._units[key] = (t_op, r_op, h / 2, yaw_off)
        return prim.path

    def _add_parts(self, prim: PrimSpec, ua: UnitAsset) -> None:
        """Composite unit: each member references its geom.usda (no rigid body inside; instanceable child so all
        members of one model share a prototype) + opaque wrapper boxes (carrier / board)."""
        from pxr import Gf, UsdGeom, UsdPhysics  # type: ignore[import-not-found]

        root = self.stage.GetPrimAtPath(prim.path)
        UsdPhysics.RigidBodyAPI.Apply(root)
        for k, m in enumerate(ua.models):
            for i, off in enumerate(m.offsets_m):
                holder = UsdGeom.Xform.Define(self.stage, f"{prim.path}/P{k}_{i}")
                xf = UsdGeom.Xformable(holder)
                xf.AddTranslateOp().Set(Gf.Vec3d(*off))
                if m.yaw_jitter_deg:
                    xf.AddRotateZOp(opSuffix="jitter").Set(jitter_deg(f"{prim.instance_id}/{k}/{i}", m.yaw_jitter_deg))
                for j, (ax, deg) in enumerate(m.rotate):
                    getattr(xf, f"AddRotate{ax.upper()}Op")(opSuffix=f"r{j}").Set(float(deg))
                xf.AddScaleOp().Set(Gf.Vec3f(*m.scale_xyz))
                g = self.stage.DefinePrim(f"{prim.path}/P{k}_{i}/G", "Xform")
                g.GetReferences().AddReference(m.usd)
                g.SetInstanceable(True)
        for k, b in enumerate(ua.boxes):
            c = UsdGeom.Cube.Define(self.stage, f"{prim.path}/W{k}")
            c.CreateSizeAttr(1.0)
            sx, sy, sz = b.size_m
            ox, oy, oz = b.offset_m
            xf = UsdGeom.Xformable(c)
            xf.AddTranslateOp().Set(Gf.Vec3d(ox, oy, oz + sz / 2))
            xf.AddScaleOp().Set(Gf.Vec3f(sx, sy, sz))
            color = b.color or (prim.color if b.kind in ("carrier", "board", "carton", "sleeve") else "#f4f4f0")
            c.CreateDisplayColorAttr([Gf.Vec3f(*_rgb(color))])
            self._bind(c.GetPrim(), self._color_material(color))

    def _label_decal(
        self,
        spec: SceneSpec,
        prim: PrimSpec,
        mesh: LabelMesh,
        scale: Vec3,
        yaw_off_deg: float,
        px: tuple[int, int],
    ) -> None:
        """A decal child of the unit root; item-frame points mapped into the model's native (scaled) frame."""
        from pxr import Gf, Sdf, UsdGeom  # type: ignore[import-not-found]

        m = UsdGeom.Mesh.Define(self.stage, f"{prim.path}/{mesh.name}")
        m.CreatePointsAttr([Gf.Vec3f(*item_to_native(p, scale, yaw_off_deg)) for p in mesh.points])
        m.CreateFaceVertexCountsAttr(list(mesh.counts))
        m.CreateFaceVertexIndicesAttr(list(mesh.indices))
        m.CreateNormalsAttr([Gf.Vec3f(*item_normal_to_native(n, scale, yaw_off_deg)) for n in mesh.normals])
        m.SetNormalsInterpolation(UsdGeom.Tokens.vertex)
        m.CreateDoubleSidedAttr(True)
        if mesh.textured and mesh.uvs:
            st = UsdGeom.PrimvarsAPI(m).CreatePrimvar("st", Sdf.ValueTypeNames.TexCoord2fArray, UsdGeom.Tokens.vertex)
            st.Set([Gf.Vec2f(*uv) for uv in mesh.uvs])
            self._bind(m.GetPrim(), self._label_material(spec, prim, px))
        else:
            m.CreateDisplayColorAttr([Gf.Vec3f(*_rgb(prim.color))])
            self._bind(m.GetPrim(), self._color_material(prim.color))

    def _define_tag(self, spec: SceneSpec, layout: SlotLayout) -> None:
        """Shelf-edge price tag (our label PNG) in front of a slot whose model has no label (loose produce), so the
        price stays readable on camera."""
        from pxr import Gf, Sdf, UsdGeom  # type: ignore[import-not-found]

        if not layout.positions:
            return
        w, d, h = layout.dims
        lat = (math.cos(layout.yaw), math.sin(layout.yaw))
        out = (math.sin(layout.yaw), -math.cos(layout.yaw))  # item -y in the store frame (toward the aisle)
        front = max(layout.positions, key=lambda p: p[0] * out[0] + p[1] * out[1])
        z_base = min(p[2] for p in layout.positions) - h / 2
        cx, cy = front[0] + out[0] * (d / 2 + 0.01), front[1] + out[1] * (d / 2 + 0.01)
        tw, th = TAG_SIZE_M
        z0, z1 = z_base + 0.005, z_base + 0.005 + th
        pts = [
            (cx - lat[0] * tw / 2, cy - lat[1] * tw / 2, z0),
            (cx + lat[0] * tw / 2, cy + lat[1] * tw / 2, z0),
            (cx + lat[0] * tw / 2, cy + lat[1] * tw / 2, z1),
            (cx - lat[0] * tw / 2, cy - lat[1] * tw / 2, z1),
        ]
        m = UsdGeom.Mesh.Define(self.stage, f"{spec.root_path}/Tags/{usd_name(layout.slot_id)}")
        m.CreatePointsAttr([Gf.Vec3f(*p) for p in pts])
        m.CreateFaceVertexCountsAttr([4])
        m.CreateFaceVertexIndicesAttr([0, 1, 2, 3])
        m.CreateNormalsAttr([Gf.Vec3f(out[0], out[1], 0.0)] * 4)
        m.SetNormalsInterpolation(UsdGeom.Tokens.vertex)
        m.CreateDoubleSidedAttr(True)
        st = UsdGeom.PrimvarsAPI(m).CreatePrimvar("st", Sdf.ValueTypeNames.TexCoord2fArray, UsdGeom.Tokens.vertex)
        st.Set([Gf.Vec2f(0, 0), Gf.Vec2f(1, 0), Gf.Vec2f(1, 1), Gf.Vec2f(0, 1)])
        mat = self._label_material_for(
            spec, layout.sku, layout.label_text, layout.price_cents, layout.barcode, layout.color
        )
        self._bind(m.GetPrim(), mat)

    def _define_primitive_unit(self, spec: SceneSpec | None, prim: PrimSpec) -> str:
        """Product unit: Xform (translate + yaw) with a scaled body and, for boxes / cans, a textured label quad."""
        from pxr import Gf, Sdf, UsdGeom  # type: ignore[import-not-found]

        root = UsdGeom.Xform.Define(self.stage, prim.path)
        xf = UsdGeom.XformCommonAPI(root)
        xf.SetTranslate(Gf.Vec3d(*prim.center))
        xf.SetRotate(Gf.Vec3f(0.0, 0.0, math.degrees(prim.yaw)))
        if prim.asset and self._model_unit(spec, prim):
            return self._finish_unit(root, prim)
        body = self._geom(f"{prim.path}/body", prim.shape)
        UsdGeom.XformCommonAPI(body).SetScale(Gf.Vec3f(*prim.size))
        body.CreateDisplayColorAttr([Gf.Vec3f(*_rgb(prim.color))])
        self._bind(body.GetPrim(), self._color_material(prim.color))
        if prim.shape != "sphere" and spec is not None:
            w, d, h = prim.size
            product = spec.products.get(prim.sku or "")
            packaged = product is not None and package_kind(product.subcategory, product.tags) is not None
            hw, hh, y = (w * 0.5, h * 0.5, -d / 2 - 0.0008) if packaged else (w * 0.46, h * 0.42, -d / 2 - 0.0015)
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
        return self._finish_unit(root, prim)

    def _finish_unit(self, root: Any, prim: PrimSpec) -> str:
        self._tag_attrs(prim, root.GetPrim())
        self._inst_paths[(prim.store_id or "", prim.instance_id or "")] = prim.path
        return prim.path

    # -- visual models (arms/sim/visuals.yaml) -----------------------------------------------------------------------
    def _quad(self, path: str, w: float, h: float, center: Vec3, mat: Any, yaw: float = 0.0, normal_y: float = -1.0):
        """Textured w x h quad facing ``normal_y`` (-1 = local -y) at ``center`` (parent frame)."""
        from pxr import Gf, Sdf, UsdGeom  # type: ignore[import-not-found]

        hw, hh = w / 2, h / 2
        mesh = UsdGeom.Mesh.Define(self.stage, path)
        pts = [Gf.Vec3f(-hw, 0, -hh), Gf.Vec3f(hw, 0, -hh), Gf.Vec3f(hw, 0, hh), Gf.Vec3f(-hw, 0, hh)]
        mesh.CreatePointsAttr(pts)
        mesh.CreateFaceVertexCountsAttr([4])
        mesh.CreateFaceVertexIndicesAttr([0, 1, 2, 3])
        mesh.CreateNormalsAttr([Gf.Vec3f(0, normal_y, 0)] * 4)
        mesh.SetNormalsInterpolation(UsdGeom.Tokens.vertex)
        mesh.CreateDoubleSidedAttr(True)
        st = UsdGeom.PrimvarsAPI(mesh).CreatePrimvar("st", Sdf.ValueTypeNames.TexCoord2fArray, UsdGeom.Tokens.vertex)
        st.Set([Gf.Vec2f(0, 0), Gf.Vec2f(1, 0), Gf.Vec2f(1, 1), Gf.Vec2f(0, 1)])
        xf = UsdGeom.XformCommonAPI(mesh)
        xf.SetTranslate(Gf.Vec3d(*center))
        if yaw:
            xf.SetRotate(Gf.Vec3f(0.0, 0.0, math.degrees(yaw)))
        self._bind(mesh.GetPrim(), mat)
        return mesh

    def _bounds(self, asset: str) -> tuple[Any, Any] | None:
        """(min, max) of the converted model in its own frame, or None when it was not fetched (logged once)."""
        if asset not in self._model_bounds:
            from pxr import Usd, UsdGeom  # type: ignore[import-not-found]

            usd = asset_usd(asset)
            bounds = None
            if usd.exists():
                stage = Usd.Stage.Open(str(usd))
                rng = (
                    UsdGeom.BBoxCache(Usd.TimeCode.Default(), ["default", "render"])
                    .ComputeWorldBound(stage.GetPseudoRoot())
                    .ComputeAlignedRange()
                )
                if not rng.IsEmpty():
                    bounds = (rng.GetMin(), rng.GetMax())
            else:
                log.warning("visual model %s missing at %s (python -m arms.sim.fetch_visuals); box", asset, usd)
            self._model_bounds[asset] = bounds
        return self._model_bounds[asset]

    def _add_model(
        self, path: str, asset: str, size: Vec3, *, bottom_z: float, tilt_deg: float = 0.0, fit: str = "box"
    ) -> bool:
        """Reference ``asset`` at ``path`` (an Xform in the parent's frame), stood up, turned to face local -y,
        uniformly scaled to fit ``size`` (w, d, h; ``fit='height'`` matches h only), its bottom at ``bottom_z``."""
        from pxr import Gf, UsdGeom  # type: ignore[import-not-found]

        bounds = self._bounds(asset)
        src = load_visuals().assets.get(asset)
        if bounds is None or src is None:
            return False
        lo, hi = bounds
        c = (lo + hi) / 2
        ex, ey, ez = (hi - lo)[0], (hi - lo)[1], (hi - lo)[2]
        if src.up == "y":  # +90 deg about x: model +y -> +z, model +z -> -y
            ex, ey, ez = ex, ez, ey
        w, d, h = size
        scale = h / ez if fit == "height" else min(w / ex, d / ey, h / ez)
        xform = UsdGeom.Xform.Define(self.stage, path)
        xformable = UsdGeom.Xformable(xform)
        xformable.ClearXformOpOrder()
        xformable.AddTranslateOp(opSuffix="place").Set(Gf.Vec3d(0.0, 0.0, bottom_z + ez * scale / 2))
        xformable.AddRotateZOp(opSuffix="front").Set(src.yaw_deg)
        xformable.AddRotateXOp(opSuffix="tilt").Set(-tilt_deg)
        xformable.AddScaleOp().Set(Gf.Vec3f(scale, scale, scale))
        if src.up == "y":
            xformable.AddRotateXOp(opSuffix="up").Set(90.0)
        xformable.AddTranslateOp(opSuffix="center").Set(Gf.Vec3d(-c[0], -c[1], -c[2]))
        # Reference into a child: the model file's root carries its own transform (the converter's axis/unit fix),
        # which the bounds above include and which local xform ops on the referencing prim would override.
        geo = UsdGeom.Xform.Define(self.stage, f"{path}/geo")
        geo.GetPrim().GetReferences().AddReference(str(asset_usd(asset)))
        return True

    def _model_unit(self, spec: SceneSpec | None, prim: PrimSpec) -> bool:
        """Product unit drawn as its 3D model (+ display puck for phones) with a small price card in front."""
        from pxr import Gf, UsdGeom  # type: ignore[import-not-found]

        w, d, h = prim.size
        bottom = -h / 2
        model_size = prim.model_dims or prim.size
        if prim.display == "stand":
            puck = UsdGeom.Cylinder.Define(self.stage, f"{prim.path}/stand")
            puck.CreateRadiusAttr(0.045)
            puck.CreateHeightAttr(0.018)
            puck.CreateAxisAttr(UsdGeom.Tokens.z)
            UsdGeom.XformCommonAPI(puck).SetTranslate(Gf.Vec3d(0.0, 0.01, bottom + 0.009))
            self._bind(puck.GetPrim(), self._color_material("#f4f4f4"))
            bottom += 0.018
        if not self._add_model(
            f"{prim.path}/model", prim.asset or "", model_size, bottom_z=bottom, tilt_deg=prim.tilt_deg
        ):
            if prim.display == "stand":
                self.stage.RemovePrim(f"{prim.path}/stand")
            return False
        if spec is not None and prim.price_card:  # one price card per slot (front unit): name, price, barcode
            cw = max(0.06, min(0.09, w * 1.2))
            self._quad(
                f"{prim.path}/label",
                cw,
                cw * 0.75,
                # stands on the shelf / table beside the unit, in its front plane (inside the fixture edge)
                (-(max(w / 2, 0.045) + cw / 2 + 0.01), -d / 2 + 0.005, -h / 2 + cw * 0.375),
                self._label_material(spec, prim),
            )
        return True

    def _define_dressing(self, spec: PrimSpec, path: str) -> Any:
        """Decor model or sign (visuals.yaml): an Xform at ``spec.center`` turned by ``spec.yaw``."""
        from pxr import Gf, UsdGeom  # type: ignore[import-not-found]

        root = UsdGeom.Xform.Define(self.stage, path)
        xf = UsdGeom.XformCommonAPI(root)
        xf.SetTranslate(Gf.Vec3d(*spec.center))
        xf.SetRotate(Gf.Vec3f(0.0, 0.0, math.degrees(spec.yaw)))
        w, d, h = spec.size
        if spec.role == "sign":
            board = self._geom(f"{path}/board", "box")
            UsdGeom.XformCommonAPI(board).SetScale(Gf.Vec3f(w, d, h))
            self._bind(board.GetPrim(), self._color_material(spec.color))
            png = sign_png(
                spec.label_text or "", color=spec.color, text_color=spec.text_color or "#ffffff", aspect=w / h
            )
            if png:
                key = f"sign_{usd_name(spec.label_text or 'blank')}_{spec.color.lstrip('#')}_{int(w / h * 10)}"
                if key not in self._materials:
                    self._materials[key] = self._texture_material(key, png, emissive=0.6)
                self._quad(f"{path}/face", w * 0.98, h * 0.94, (0.0, -d / 2 - 0.002, 0.0), self._materials[key])
            return root
        if not self._add_model(f"{path}/model", spec.asset or "", (w, d, h), bottom_z=-h / 2, fit="height"):
            box = self._geom(f"{path}/box", "box")
            UsdGeom.XformCommonAPI(box).SetScale(Gf.Vec3f(w, d, h))
            self._bind(box.GetPrim(), self._color_material(spec.color))
        return root

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
        for key in [k for k in self._units if k[0] == spec.store_id]:
            del self._units[key]
        for key in [k for k in self._ext if k[0] == spec.store_id]:
            del self._ext[key]
        for path in [p for p in self._ext_base if p.startswith(root + "/")]:
            del self._ext_base[path]
        xform = UsdGeom.Xform.Define(self.stage, root)
        UsdGeom.XformCommonAPI(xform).SetTranslate(Gf.Vec3d(*offset))
        self._store_roots[spec.store_id] = root
        if spec.external_usd:
            self._build_external(spec)
        # (no Sdf.ChangeBlock: the Usd/UsdShade APIs used here read the stage while authoring)
        self._specs[spec.store_id] = spec
        for p in spec.prims:
            if p.role == "product":
                self._define_unit(spec, p)
            else:
                self._define(p)
        modeled = sum(1 for k in self._units if k[0] == spec.store_id)
        tags = 0
        for layout in spec.slot_layouts.values():
            ua = self.assets.resolve(layout.sku) if layout.render != "primitive" else None
            if ua is not None and ua.label.mode == "none":
                try:
                    self._define_tag(spec, layout)
                    tags += 1
                except Exception as exc:  # noqa: BLE001 - cosmetic
                    log.warning("tag for %s failed: %s", layout.slot_id, exc)
        if modeled:
            log.info("store %s: %d units on per-item models, %d shelf tags", spec.store_id, modeled, tags)
        external = sum(1 for k in self._ext if k[0] == spec.store_id)
        if spec.external_usd:
            log.info("store %s: %d units are product prims of %s", spec.store_id, external, spec.external_usd)
        for _ in range(5):
            self._update()
        self._changed()
        log.info(
            "store %s built at %s: %d prims, %d units", spec.store_id, offset, len(spec.prims), len(spec.instances)
        )

    # -- external scenes (arms.sim.external): the store's own USD draws shelves + products ---------------------------
    def _build_external(self, spec: SceneSpec) -> None:
        """Reference the store's USD under ``spec.external_root`` (translated USD -> store frame), freeze its rigid
        bodies (posed kinematically like every unit) and hide the mapped prims that have no unit."""
        from pxr import Gf, Sdf, Usd, UsdGeom, UsdLux, UsdPhysics  # type: ignore[import-not-found]

        assert spec.external_root and spec.external_usd
        scene = UsdGeom.Xform.Define(self.stage, spec.external_root)
        UsdGeom.XformCommonAPI(scene).SetTranslate(Gf.Vec3d(*spec.external_translate))
        scene.GetPrim().GetReferences().AddReference(spec.external_usd, Sdf.Path(spec.external_default_prim))
        self.stage.Load(spec.external_root)
        try:
            light_scale = float(os.environ.get(EXTERNAL_LIGHT_SCALE_ENV, "1.0"))
        except ValueError:
            light_scale = 1.0
        bodies = lights = 0
        for p in Usd.PrimRange(scene.GetPrim()):
            if p.HasAPI(UsdPhysics.RigidBodyAPI):
                UsdPhysics.RigidBodyAPI(p).CreateRigidBodyEnabledAttr(False)
                bodies += 1
            if p.IsA(UsdPhysics.Scene):
                p.SetActive(False)  # one physics scene per stage; nothing here is simulated anyway
            if light_scale != 1.0 and (p.HasAPI(UsdLux.LightAPI) or p.GetTypeName().endswith("Light")):
                a = p.GetAttribute("inputs:intensity")
                if a and a.Get() is not None:
                    a.Set(float(a.Get()) * light_scale)
                    lights += 1
        for rel in spec.external_hidden:
            self._set_visible(f"{spec.external_root}/{rel}", False)
        log.info(
            "store %s: referenced %s (%d rigid bodies frozen, %d lights x%.2f, %d spare prims hidden)",
            spec.store_id,
            spec.external_usd,
            bodies,
            lights,
            light_scale,
            len(spec.external_hidden),
        )

    def _set_visible(self, path: str, visible: bool) -> None:
        from pxr import UsdGeom  # type: ignore[import-not-found]

        prim = self.stage.GetPrimAtPath(path)
        if prim and prim.IsValid():
            im = UsdGeom.Imageable(prim)
            im.MakeVisible() if visible else im.MakeInvisible()

    def _store_matrix(self, prim: Any, root_inv: Any) -> Any:
        from pxr import Usd, UsdGeom  # type: ignore[import-not-found]

        return UsdGeom.Xformable(prim).ComputeLocalToWorldTransform(Usd.TimeCode.Default()) * root_inv

    def _external_unit(self, spec: SceneSpec | None, prim: PrimSpec) -> str | None:
        """Bind a unit to its USD product prim (no new geometry). None when the prim is missing (primitive then)."""
        from pxr import Gf, Usd, UsdGeom  # type: ignore[import-not-found]

        if spec is None or not spec.external_root or not prim.external_path:
            return None
        path = f"{spec.external_root}/{prim.external_path}"
        p = self.stage.GetPrimAtPath(path)
        if not p or not p.IsValid() or not p.IsA(UsdGeom.Xformable):
            log.warning("unit %s: external prim %s not found; drawing the primitive", prim.instance_id, path)
            return None
        if path not in self._ext_base:  # the prim's pose as authored by the scene (cached: a restock reuses it)
            root = self.stage.GetPrimAtPath(spec.root_path)
            root_inv = UsdGeom.Xformable(root).ComputeLocalToWorldTransform(Usd.TimeCode.Default()).GetInverse()
            w0 = self._store_matrix(p, root_inv)
            wp = self._store_matrix(p.GetParent(), root_inv)
            self._ext_base[path] = (w0, wp.GetInverse(), Gf.Vec3d(*prim.center), prim.yaw)
        self._set_visible(path, True)
        key = (prim.store_id or "", prim.instance_id or "")
        self._ext[key] = {"path": path, "yaw": prim.yaw, "op": None}
        self._inst_paths[key] = path
        base_center = self._ext_base[path][2]
        if any(abs(a - b) > 1e-4 for a, b in zip(base_center, prim.center, strict=True)):
            self._pose_external(key[0], key[1], Point3(x=prim.center[0], y=prim.center[1], z=prim.center[2]), prim.yaw)
        return path

    def _pose_external(self, store_id: str, instance_id: str, position: Point3, yaw: float | None) -> None:
        """Move an external prim so its box center is at ``position`` (store frame), turned by ``yaw`` - its
        build-time yaw about +z: local = W0 . T(-c0) . Rz(dyaw) . T(p) . Wparent^-1 (Gf row vectors)."""
        from pxr import Gf, UsdGeom  # type: ignore[import-not-found]

        e = self._ext[(store_id, instance_id)]
        w0, wp_inv, c0, yaw0 = self._ext_base[e["path"]]
        if yaw is not None:
            e["yaw"] = yaw
        rot = Gf.Matrix4d().SetRotate(Gf.Rotation(Gf.Vec3d(0, 0, 1), math.degrees(e["yaw"] - yaw0)))
        m = (
            w0
            * Gf.Matrix4d().SetTranslate(-c0)
            * rot
            * Gf.Matrix4d().SetTranslate(Gf.Vec3d(position.x, position.y, position.z))
        )
        if e["op"] is None:
            e["op"] = UsdGeom.Xformable(self.stage.GetPrimAtPath(e["path"])).MakeMatrixXform()
        e["op"].Set(m * wp_inv)

    def set_store_offset(self, store_id: str, offset: Vec3) -> None:
        from pxr import Gf, UsdGeom  # type: ignore[import-not-found]

        root = self._store_roots.get(store_id)
        if root:
            UsdGeom.XformCommonAPI(self.stage.GetPrimAtPath(root)).SetTranslate(Gf.Vec3d(*offset))
            self._changed()

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
        rig = self._spawn_puppet(rover) if rover.model_usd else None
        urdf_ok = rig is None and self.so101_urdf is not None and self._import_so101(f"{rover.root_path}/Lift/SO101")
        for p in rover.puppet_prims if rig is not None else rover.prims:
            if p.group == "arm" and urdf_ok:
                continue
            self._define(p)
        self._robots[rover.robot_id] = {
            "xf": UsdGeom.XformCommonAPI(root),
            "lift": lift_op,
            "scale": scale,
            "root": rover.root_path,
            "puppet": rig,
            "closed": True,
        }
        self._changed()

    # -- rover USD as a visual puppet ----------------------------------------------------------------------------------
    def _spawn_puppet(self, rover: RoverSpec) -> dict[str, Any] | None:
        """Reference the rover USD under ``rover.model_path`` and turn it into a kinematic puppet. None (and nothing
        left behind) on any failure: the caller draws the primitive rover instead."""
        from pxr import Gf, Usd, UsdGeom, UsdPhysics  # type: ignore[import-not-found]

        model_path = f"{rover.root_path}/{puppet.MODEL_PRIM_NAME}"
        rm = puppet.rover_model_for(rover.model_usd)
        try:
            model = UsdGeom.Xform.Define(self.stage, model_path)
            model.AddTranslateOp().Set(Gf.Vec3d(0.0, 0.0, rm.floor_offset_m))
            model.AddRotateZOp().Set(math.degrees(rm.heading_offset_rad))
            ref = self.stage.DefinePrim(rover.model_path or f"{model_path}/{rm.ref_name}", "Xform")
            ref.GetReferences().AddReference(rover.model_usd or "")
            names = [c.GetName() for c in ref.GetChildren()]
            if not puppet.names_ok(names, rm):
                raise RuntimeError(f"{rover.model_usd} has no SO-101 links (children: {names[:8]})")
            geo = self._puppet_geometry(rover.model_usd or "", ref, rm)
            if geo is None:
                raise RuntimeError("arm chain unavailable")
            # never simulated: no articulation, no joints, no rigid bodies, no colliders
            joints = [p.GetPath() for p in Usd.PrimRange(ref) if p.IsA(UsdPhysics.Joint)]
            bodies = [p for p in Usd.PrimRange(ref) if p.HasAPI(UsdPhysics.RigidBodyAPI)]
            for p in bodies:
                p.RemoveAPI(UsdPhysics.RigidBodyAPI)
                col = p.GetChild("collisions")
                if col:
                    col.SetActive(False)
            if ref.HasAPI(UsdPhysics.ArticulationRootAPI):
                ref.RemoveAPI(UsdPhysics.ArticulationRootAPI)
            for path in joints:
                jp = self.stage.GetPrimAtPath(path)
                if jp:
                    jp.SetActive(False)
            for rel in (*rm.hide_prims, *rm.virtual_links):
                hp = self.stage.GetPrimAtPath(f"{ref.GetPath()}/{rel}")
                if hp:
                    hp.SetActive(False)
            ops: dict[str, Any] = {}
            for name, usd_name in rm.link_map.items():
                xf = UsdGeom.Xformable(self.stage.GetPrimAtPath(f"{ref.GetPath()}/{usd_name}"))
                xf.ClearXformOpOrder()
                ops[name] = xf.AddTransformOp(opSuffix="puppet")
                ops[name].Set(Gf.Matrix4d(*gf_rows(geo["chain"].rest[name])))
            return {"geo": geo, "ops": ops, "key": None}
        except Exception as exc:  # noqa: BLE001 - visual only: fall back to the primitive rover
            log.warning("rover %s: USD puppet failed (%s); using the primitive rover", rover.robot_id, exc)
            if self.stage.GetPrimAtPath(model_path):
                self.stage.RemovePrim(model_path)
            return None

    def _puppet_geometry(
        self, model_usd: str, ref: Any, rm: puppet.RoverModel = puppet.LEKIWI
    ) -> dict[str, Any] | None:
        """Arm chain + reach table of one rover USD (read from the first rover that references it; shared)."""
        if model_usd in self._puppet_geo:
            return self._puppet_geo[model_usd]
        t0 = time.monotonic()
        try:
            geo = self._build_puppet_geometry(ref, rm)
        except Exception as exc:  # noqa: BLE001
            log.warning("rover USD %s: cannot build the SO-101 chain (%s)", model_usd, exc)
            geo = None
        self._puppet_geo[model_usd] = geo
        if geo is not None:
            log.info(
                "rover USD %s (%s): SO-101 puppet ready in %.1fs (reach table max error %.0f mm; z_ref %.3f m)",
                model_usd,
                rm.key,
                time.monotonic() - t0,
                geo["table"].max_error_m * 1000,
                geo["z_ref"],
            )
        return geo

    def _build_puppet_geometry(self, ref: Any, rm: puppet.RoverModel = puppet.LEKIWI) -> dict[str, Any]:
        from pxr import Gf, Usd, UsdGeom, UsdPhysics  # type: ignore[import-not-found]

        base_path = str(ref.GetPath())
        usd_name = rm.link_map
        rest = {
            n: from_gf(
                UsdGeom.Xformable(self.stage.GetPrimAtPath(f"{base_path}/{usd_name[n]}")).GetLocalTransformation()
            )
            for n in puppet.ARM_LINKS
        }
        found: dict[str, Any] = {}
        for p in Usd.PrimRange(ref):
            if p.IsA(UsdPhysics.Joint) and p.GetName() in (
                puppet.PAN_JOINT,
                *puppet.REACH_JOINTS,
                "wrist_roll",
                "gripper",
            ):
                found[p.GetName()] = p

        def joint(name: str) -> puppet.ArmJoint:
            p = found[name]
            j = UsdPhysics.Joint(p)
            q = j.GetLocalRot1Attr().Get() or Gf.Quatf(1.0)
            pos = j.GetLocalPos1Attr().Get() or Gf.Vec3f(0.0)
            im = q.GetImaginary()
            ax = p.GetAttribute("physics:axis")
            lo = p.GetAttribute("physics:lowerLimit")
            hi = p.GetAttribute("physics:upperLimit")
            return puppet.ArmJoint(
                name=name,
                parent=rm.canonical(j.GetBody0Rel().GetTargets()[0].name),
                child=rm.canonical(j.GetBody1Rel().GetTargets()[0].name),
                frame1=puppet.quat_pose(q.GetReal(), im[0], im[1], im[2], pos=(pos[0], pos[1], pos[2])),
                axis=str(ax.Get() if ax and ax.Get() else "X"),
                lower_deg=float(lo.Get()) if lo and lo.Get() is not None else -180.0,
                upper_deg=float(hi.Get()) if hi and hi.Get() is not None else 180.0,
            )

        chain = puppet.ArmChain(
            root="shoulder",
            rest=rest,
            joints=[joint(n) for n in ("shoulder_lift", "elbow_flex", "wrist_flex", "wrist_roll", "gripper")],
            root_joint=joint(puppet.PAN_JOINT),
        )
        # TCP between the jaw tips (gripper closed), from the visual meshes in each link's frame
        M = puppet.model_matrix(from_gf(UsdGeom.Xformable(ref).GetLocalTransformation()), rm)
        if rm.align_pan:  # robot v3: the SO-101 faces sideways at pan 0 -> turn its rest pose to face forward
            chain = puppet.align_rest_pan(chain, puppet.forward_pan(chain, M))
            rest = chain.rest
        fwd = puppet.rotate(puppet.inverse(M), (1.0, 0.0, 0.0))
        closed = {"gripper": math.radians(puppet.GRIPPER_CLOSED_DEG)}
        poses = chain.fk(closed)
        xc = UsdGeom.XformCache()

        def tip(link: str) -> puppet.Vec3:
            lp = self.stage.GetPrimAtPath(f"{base_path}/{usd_name[link]}")
            inv = xc.GetLocalToWorldTransform(lp).GetInverse()
            pts: list[puppet.Vec3] = []
            for mp in Usd.PrimRange(lp, Usd.TraverseInstanceProxies()):
                if not mp.IsA(UsdGeom.Mesh) or "collision" in str(mp.GetPath()).lower():
                    continue
                if UsdGeom.Imageable(mp).ComputePurpose() not in (UsdGeom.Tokens.default_, UsdGeom.Tokens.render):
                    continue
                to_link = xc.GetLocalToWorldTransform(mp) * inv
                raw = UsdGeom.Mesh(mp).GetPointsAttr().Get() or []
                step = max(1, len(raw) // 400)
                for v in list(raw)[::step]:
                    q = to_link.Transform(Gf.Vec3d(v))
                    pts.append(puppet.apply(poses[link], (q[0], q[1], q[2])))
            if not pts:
                return puppet.position(poses[link])
            far = max(sum(a * b for a, b in zip(p, fwd, strict=True)) for p in pts)
            sel = [p for p in pts if sum(a * b for a, b in zip(p, fwd, strict=True)) > far - 0.008]
            return (
                sum(p[0] for p in sel) / len(sel),
                sum(p[1] for p in sel) / len(sel),
                sum(p[2] for p in sel) / len(sel),
            )

        tcp_model = puppet.tcp_from_tips(tip("gripper"), tip("jaw"), puppet.position(poses["wrist"]))
        chain.tcp = puppet.apply(puppet.inverse(poses["gripper"]), tcp_model)
        fixed = {"wrist_roll": math.radians(puppet.WRIST_ROLL_DEG), **closed}
        z_ref = rm.z_ref_m if rm.z_ref_m is not None else puppet.apply(M, chain.tcp_point(chain.fk(fixed)))[2]
        dx = ARM_MOUNT_X_M + puppet.ARM_PAN_FORWARD_M - puppet.apply(M, puppet.position(rest["shoulder"]))[0]
        if rm.fixed_lift:  # robot v3: the SO-101 stays on its riser; the IK reaches the rig's gripper point from there
            dx = 0.0
        m_inv = puppet.inverse(M)

        def target(ext: float, drop: float) -> puppet.Vec3:
            return puppet.apply(m_inv, (ARM_MOUNT_X_M + ext, 0.0, z_ref - drop))

        table = puppet.ReachTable.build(
            chain,
            target,
            (ARM_STOW_EXT_M, ARM_FULL_EXT_M + puppet.TCP_LEAD_M),
            drops=rm.drops_m or PUPPET_DROPS_M,
            base=puppet.robot_delta_in_model(M, (dx, 0.0, 0.0)),
            seed=fixed,
        )
        kin = puppet.ArmKinematics(
            chain=chain, model=M, dx=dx, z_ref=z_ref, travel_m=rm.lift_travel_m, table=table, lift_mode="offset"
        )
        return {
            "chain": chain,
            "table": table,
            "M": M,
            "z_ref": z_ref,
            "dx": dx,
            "kin": kin,
            "travel": rm.lift_travel_m,
        }

    def _pose_puppet(self, rig: dict[str, Any], lift_height_m: float, arm_ext_m: float, closed: bool) -> float:
        """Pose the SO-101 links for this lift + extension; returns the carriage travel (for the Lift group)."""
        from pxr import Gf  # type: ignore[import-not-found]

        geo = rig["geo"]
        dz = puppet.lift_offset(lift_height_m, geo["z_ref"], geo.get("travel", puppet.LIFT_TRAVEL_M))
        drop = max(0.0, geo["z_ref"] - lift_height_m)
        key = (round(arm_ext_m, 4), round(drop, 4), round(dz, 4), closed)
        if key == rig["key"]:
            return dz
        rig["key"] = key
        q = geo["table"].config(arm_ext_m + (puppet.TCP_LEAD_M if closed else 0.0), drop)
        q["wrist_roll"] = math.radians(puppet.WRIST_ROLL_DEG)
        q["gripper"] = math.radians(puppet.GRIPPER_CLOSED_DEG if closed else puppet.GRIPPER_OPEN_DEG)
        poses = geo["chain"].fk(q, puppet.robot_delta_in_model(geo["M"], (geo["dx"], 0.0, dz)))
        for name, op in rig["ops"].items():
            op.Set(Gf.Matrix4d(*gf_rows(poses[name])))
        return dz

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
        self._changed()

    def set_robot_pose(self, robot_id: str, pose: Pose2D, lift_height_m: float, arm_ext_m: float) -> None:
        from pxr import Gf  # type: ignore[import-not-found]

        info = self._robots.get(robot_id)
        if info is None:
            return
        info["xf"].SetTranslate(Gf.Vec3d(pose.x, pose.y, 0.0))
        info["xf"].SetRotate(Gf.Vec3f(0.0, 0.0, math.degrees(pose.theta)))
        rig = info.get("puppet")
        if rig is not None:
            dz = self._pose_puppet(rig, lift_height_m, arm_ext_m, bool(info.get("closed", True)))
            info["lift"].Set(Gf.Vec3d(0.0, 0.0, dz))  # carriage bracket rides with the SO-101 links
        else:
            info["lift"].Set(Gf.Vec3d(0.0, 0.0, lift_height_m - LIFT_REF_M))
            info["scale"].Set(Gf.Vec3f(max(0.3, min(1.0, arm_ext_m / 0.33)), 1.0, 1.0))
        self._changed()

    def set_robot_gripper(self, robot_id: str, closed: bool) -> None:
        info = self._robots.get(robot_id)
        if info is not None:
            info["closed"] = closed

    # -- joint mode (contracts 0.2.2 ArmJointsCmd) ---------------------------------------------------------------------
    def arm_kinematics(self, robot_id: str) -> puppet.ArmKinematics | None:
        """The rover USD's SO-101 (None for a primitive rover: the core then uses the synthetic arm)."""
        rig = (self._robots.get(robot_id) or {}).get("puppet")
        return rig["geo"].get("kin") if rig is not None else None

    def set_robot_joints(self, robot_id: str, pose: Pose2D, lift_height_m: float, q_deg: list[float]) -> bool:
        """Pose the puppet's SO-101 links from 6 joint degrees (FK through the USD joint frames); False when this
        rover has no puppet (the core then poses the primitive rig)."""
        from pxr import Gf  # type: ignore[import-not-found]

        info = self._robots.get(robot_id)
        rig = info.get("puppet") if info is not None else None
        if info is None or rig is None or rig["geo"].get("kin") is None:
            return False
        kin: puppet.ArmKinematics = rig["geo"]["kin"]
        info["xf"].SetTranslate(Gf.Vec3d(pose.x, pose.y, 0.0))
        info["xf"].SetRotate(Gf.Vec3f(0.0, 0.0, math.degrees(pose.theta)))
        dz = kin.travel(lift_height_m)
        key = ("joints", tuple(round(v, 3) for v in q_deg), round(dz, 4))
        if key != rig["key"]:
            rig["key"] = key
            poses = kin.link_poses(lift_height_m, puppet.q_rad(q_deg))
            for name, op in rig["ops"].items():
                op.Set(Gf.Matrix4d(*gf_rows(poses[name])))
        info["lift"].Set(Gf.Vec3d(0.0, 0.0, dz))
        self._changed()
        return True

    def add_instance(self, prim: PrimSpec) -> None:
        spec = self._specs.get(prim.store_id or "")
        self._define_unit(spec, prim)
        self._changed()

    def remove_instance(self, store_id: str, instance_id: str) -> None:
        path = self._inst_paths.pop((store_id, instance_id), None)
        self._units.pop((store_id, instance_id), None)
        if self._ext.pop((store_id, instance_id), None) is not None:
            # a prim of the external USD cannot be removed (it lives in a referenced layer): hide it; a restock of
            # the same unit shows it again (add_instance)
            if path:
                self._set_visible(path, False)
            self._changed()
            return
        if path and self.stage.GetPrimAtPath(path):
            self.stage.RemovePrim(path)
        self._changed()

    def set_instance_pose(self, store_id: str, instance_id: str, position: Point3, yaw: float | None = None) -> None:
        from pxr import Gf, UsdGeom  # type: ignore[import-not-found]

        path = self._inst_paths.get((store_id, instance_id))
        if not path:
            return
        if (store_id, instance_id) in self._ext:
            self._pose_external(store_id, instance_id, position, yaw)
            self._changed()
            return
        unit = self._units.get((store_id, instance_id))
        if unit is not None:  # model unit: origin at the bottom center + per-SKU yaw offset
            t_op, r_op, zoff, yaw_off = unit
            t_op.Set(Gf.Vec3d(position.x, position.y, position.z - zoff))
            if yaw is not None:
                r_op.Set(math.degrees(yaw) + yaw_off)
            self._changed()
            return
        xf = UsdGeom.XformCommonAPI(self.stage.GetPrimAtPath(path))
        xf.SetTranslate(Gf.Vec3d(position.x, position.y, position.z))
        if yaw is not None:
            xf.SetRotate(Gf.Vec3f(0.0, 0.0, math.degrees(yaw)))
        self._changed()

    # -- cameras -------------------------------------------------------------------------------------------------------
    # Isaac 6.0 rendering, measured on vhagar (RTX 5090, both stores, 640x480 render products, GPU shared):
    # - the rgb annotator only fills once the replicator orchestrator has run, and plain ``app.update()`` gives NO
    #   reliable freshness: after a robot + camera move the annotator needed 2..6 updates depending on how many render
    #   products are enabled and on GPU load (a resumed render product needed 5), i.e. stale frames from the old pose.
    # - ``rep.orchestrator.step(rt_subframes=1)`` (default delta_time; 0.0 does NOT wait) renders every ENABLED render
    #   product synchronously and was exact in every trial: ~60-100 ms with 1 enabled, ~120 ms with 3, ~270 ms with 12.
    # So: one synchronous step only when the requested camera or the scene changed since the last step (one step
    # serves every enabled camera: ``prepare`` poses all stream cameras due in a tick first), and render products idle
    # for CAMERA_IDLE_PAUSE_S are paused so steps stay cheap (a resumed one gets a second step).
    def _update(self) -> None:
        self.app.update()
        self._last_update = time.monotonic()

    def _changed(self) -> None:
        self._scene_dirty = True

    def _sync(self, steps: int = 1, subframes: int = RT_SUBFRAMES) -> None:
        """Synchronous replicator frame(s): afterwards every enabled annotator holds the current scene."""
        import omni.replicator.core as rep  # type: ignore[import-not-found]

        for _ in range(steps):
            rep.orchestrator.step(rt_subframes=subframes, pause_timeline=True)
        self._scene_dirty = False
        self._last_update = time.monotonic()
        for cam in self._cams.values():
            if cam.active:
                cam.dirty = False
                cam.resumed = False

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
        # near clip = the sketch / bbox math's NEAR_CLIP_M; it also hides the camera's own housing prim (the front
        # camera eye sits inside front_cam_head, the wrist eye 1-2 cm in front of so101_wrist_cam)
        robot_id, _, kind = camera_id.rpartition(".")
        puppet_wrist = kind == "wrist" and (self._robots.get(robot_id) or {}).get("puppet") is not None
        near = WRIST_NEAR_CLIP_PUPPET_M if puppet_wrist else NEAR_CLIP_M
        if kind.startswith(POLICY_CAMERA_PREFIX):
            near = POLICY_NEAR_CLIP_M
        c.CreateClippingRangeAttr(Gf.Vec2f(near, 200.0))
        op = UsdGeom.Xformable(c).AddTransformOp()
        rows = cam.usd_matrix_rows()
        op.Set(Gf.Matrix4d(*[v for row in rows for v in row]))
        rp = rep.create.render_product(path, (cam.w, cam.h))
        annotator = rep.AnnotatorRegistry.get_annotator("rgb")
        annotator.attach([rp])
        made = _IsaacCamera(path, op, rp, annotator, cam.w, cam.h)
        made.last_rows = rows
        made.last_used = time.monotonic()
        self._cams[camera_id] = made
        t0 = time.monotonic()
        self._sync(RT_WARMUP_STEPS, RT_SUBFRAMES_NEW)  # also starts the orchestrator; materials + textures load
        log.info(
            "camera %s: render product %s ready in %.2fs (%d total)",
            camera_id,
            rp.path,
            time.monotonic() - t0,
            len(self._cams),
        )
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
            if active:
                cam.dirty = cam.resumed = True
        except Exception:  # noqa: BLE001 - older replicator: keep rendering
            cam.active = True

    def _pose_camera(self, camera_id: str, store_id: str, cam: CameraPose) -> _IsaacCamera:
        from pxr import Gf  # type: ignore[import-not-found]

        c = self._camera(camera_id, store_id, cam)
        rows = cam.usd_matrix_rows()
        if rows != c.last_rows:
            c.op.Set(Gf.Matrix4d(*[v for row in rows for v in row]))
            c.last_rows = rows
            c.dirty = True
        c.last_used = time.monotonic()
        self._set_active(c, True)
        return c

    def prepare(self, jobs: list[tuple[str, str, CameraPose]]) -> None:
        """Pose several cameras before rendering them (stream frames due in the same tick): the first ``render``
        then runs one synchronous step that serves all of them."""
        for camera_id, store_id, cam in jobs:
            try:
                self._pose_camera(camera_id, store_id, cam)
            except Exception as exc:  # noqa: BLE001 - render() reports per camera
                log.warning("camera %s: prepare failed: %s", camera_id, exc)

    def render(self, camera_id: str, store_id: str, cam: CameraPose) -> tuple[bytes, int, int] | None:
        t0 = time.monotonic()
        for other in self._cams.values():  # pause before stepping: idle render products make every step slower
            if other.active and t0 - other.last_used > CAMERA_IDLE_PAUSE_S:
                self._set_active(other, False)
        c = self._pose_camera(camera_id, store_id, cam)
        if c.dirty or self._scene_dirty:
            self._sync(2 if c.resumed else 1)
        data = c.annotator.get_data()
        if data is None or getattr(data, "size", 0) == 0:
            log.warning("camera %s: empty annotator frame", camera_id)
            return None
        jpeg = encode_jpeg(data)
        if jpeg is None:
            log.error("no JPEG encoder: pip install pillow (or opencv-python-headless) into the worker's deps dir")
            return None
        self._stats_n += 1
        self._stats_s += time.monotonic() - t0
        if self._stats_n % 200 == 0:
            active = sum(1 for x in self._cams.values() if x.active)
            log.info(
                "isaac render: %d frames, avg %.0f ms/frame over the last 200 (%d render products, %d active)",
                self._stats_n,
                5 * self._stats_s,
                len(self._cams),
                active,
            )
            self._stats_s = 0.0
        return jpeg, c.w, c.h

    def step(self) -> None:
        """Idle tick (no frame rendered this loop): pump Kit at IDLE_UPDATE_S (frames come from ``_sync``)."""
        if time.monotonic() - self._last_update >= IDLE_UPDATE_S:
            self._update()


# --------------------------------------------------------------------------------------------------------------------
# Main loop
# --------------------------------------------------------------------------------------------------------------------
def run(
    backend: SimBackend,
    link: GatewayLink | None,
    core: WorkerCore,
    is_running: Any = lambda: True,
    max_seconds: float | None = None,
    director: Director | None = None,
) -> None:
    """Drain commands, step actions, render due camera streams. Returns when ``is_running()`` is False."""
    period = 1.0 / LOOP_HZ
    last = time.monotonic()
    started = last
    step = getattr(backend, "step", None)
    prepare = getattr(backend, "prepare", None)
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
        director_jobs = director.prepare(backend, core) if director is not None and director.due() else []
        if link is not None and link.connected.is_set():
            due = core.stream_due()
            if prepare is not None and len(due) > 1:
                jobs = []
                for camera_id in due:
                    try:
                        jobs.append((camera_id, core.resolve_camera(camera_id)[1], core.camera_pose(camera_id)))
                    except Exception:  # noqa: BLE001 - core.capture below reports it
                        pass
                prepare(jobs)
            for camera_id in due:
                try:
                    frame = core.capture(camera_id, stream=True)
                except Exception as exc:  # noqa: BLE001 - a broken camera must not kill the loop
                    log.warning("stream %s failed: %s", camera_id, exc)
                    continue
                rendered = True
                if frame is not None:
                    link.offer_frame(frame)
                    if director is not None:
                        director.tee(frame)
        if director is not None and director_jobs:

            def live(camera_id: str, _store: str, cam: Any, jpeg: bytes, w: int, h: int) -> None:
                if link is not None and link.connected.is_set() and camera_id in core.streams:
                    link.offer_frame(core.director_frame(camera_id, cam, jpeg, w, h))

            rendered = director.capture(backend, director_jobs, on_frame=live) or rendered
        if step is not None and not rendered:
            step()
        spent = time.monotonic() - now
        if spent < period:
            time.sleep(period - spent)


def _quiet_kit_logging() -> None:
    """Kit sets the root logger to DEBUG, so picky's root handler would echo every library's DEBUG line (AutoNode,
    websockets frame dumps, ...) into Kit's log as ``[Error] [py stderr]``. Keep the handler at picky's level."""
    import logging

    from utils.log import log_level

    for handler in logging.getLogger().handlers:
        if getattr(handler, "name", None) == "picky":
            handler.setLevel(log_level())
    for name in ("websockets", "asyncio", "AutoNode"):
        logging.getLogger(name).setLevel(logging.WARNING)


def visual_config(args: argparse.Namespace) -> tuple[SkuAssets, str | None]:
    """(per-item models, rover USD) from the CLI flags (their defaults come from the env). Unset / missing files /
    ``--no-assets`` / ``PICKY_SIM_ASSETS=off`` -> primitives; the rover USD only for the Isaac backend (the Pillow
    sketch draws RoverSpec primitives only)."""
    assets = SkuAssets.from_env(
        {
            ASSET_ROOT_ENV: args.asset_root,
            SKU_ASSETS_ENV: args.sku_assets,
            ASSETS_SWITCH_ENV: "off" if args.no_assets else os.environ.get(ASSETS_SWITCH_ENV, ""),
        }
    )
    rover_usd = rover_usd_from_env({ROVER_USD_ENV: args.rover_usd}) if args.backend == "isaac" else None
    return assets, rover_usd


def make_backend(kind: str, args: argparse.Namespace, assets: SkuAssets | None = None) -> tuple[Any, SimBackend]:
    """(SimulationApp or None, backend). Raises IsaacUnavailable for 'isaac' without Isaac."""
    if kind == "isaac":
        SimulationApp = require_isaac()
        sim_app = SimulationApp({"headless": not args.no_headless, "width": args.width, "height": args.height})
        _quiet_kit_logging()
        return sim_app, IsaacBackend(sim_app, label_dir=args.label_dir, so101_urdf=args.so101_urdf, assets=assets)
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
    ap.add_argument(
        "--asset-root", default=os.environ.get(ASSET_ROOT_ENV, ""), help="per-item models (arms/sim/ASSETS.md)"
    )
    ap.add_argument("--sku-assets", default=os.environ.get(SKU_ASSETS_ENV, ""), help="default data/sim/sku_assets.yaml")
    ap.add_argument("--no-assets", action="store_true", help="primitive boxes for every SKU")
    ap.add_argument("--rover-usd", default=os.environ.get(ROVER_USD_ENV, ""), help="picky_rover.usda (LeKiwi + SO-101)")
    ap.add_argument("--max-seconds", type=float, default=None, help="exit after this long (smoke tests)")
    args = ap.parse_args(argv)

    assets, rover_usd = visual_config(args)
    log.info("%s; rover: %s", assets.describe, rover_usd or "primitive")
    try:
        sim_app, backend = make_backend(args.backend, args, assets)
    except IsaacUnavailable as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    link: GatewayLink | None = None
    director = Director.from_env()  # "<robot>.director" chase cameras + optional disk frames: PICKY_SIM_DIRECTOR

    def emit(msg: Any) -> None:
        if director is not None:
            director.tee(msg)
        if link is not None:
            link.send(msg)

    core = WorkerCore(
        backend,
        emit=emit,
        default_store=settings.picky_default_store,
        assets=assets,
        rover_usd=rover_usd,
    )
    core.director = director
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
            director=director,
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
