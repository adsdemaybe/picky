"""Isaac Sim 6.2 standalone sim worker (composition root of the Isaac process, CLAUDE.md §4 rule 2).

Runs on the GPU box and connects OUTBOUND to the gateway at ``ws <PICKY_GATEWAY_URL>/ws/sim`` (contracts/sim.py):

    python -m arms.sim.app                          # Isaac, headless, store from PICKY_DEFAULT_STORE
    python -m arms.sim.app --backend kinematic      # no Isaac: same protocol + kinematics, placeholder frames (Mac OK)

Threads: the websocket thread (``GatewayLink``) ONLY enqueues parsed commands into ``inbox`` and sends what the main
loop puts in its outbox; it never touches USD. The main loop owns Isaac: drain inbox -> ``WorkerCore.submit``,
``WorkerCore.tick(dt)`` (non-blocking action state machines), ``app.update()``, every 1/fps capture the head camera ->
JPEG -> ``CameraFrame``. Scene = ``arms.sim.scene_spec`` primitives turned into USD prims with ``pxr``.

TODO (numbered, maps to the README scope checklist):
  1. [P0] Verify on the GPU box: stage units/up-axis, camera orientation vs ``CameraPose`` bboxes, JPEG colors.
  2. [P0] Host arms.robot's Isaac robot: ``create_isaac_robot`` loads the Ridgeback-Franka under /World/Robot/Body
     (proxy robot if that raises). The robot arm owns the asset, base nav, magic grasp and basket drop
     (arms/robot/isaac); the sim arm owns the scene, this worker and the bridge.
  3. [P1] Readable label textures: render label PNG per SKU (name + price + EAN-13) with PIL, bind UsdPreviewSurface.
  4. [P1] Step the robot arm's ``NavigateAction`` / ``MagicGraspAction`` / ``PlaceAction`` from ``WorkerCore`` in
     place of its kinematic placeholders (the robot owner animates the Franka; the sim owner adds the hook).
  5. [P1] Rigid bodies (low mass, CCD) on items + FixedJoint attach on pick, instead of pure set-pose.
  6. [P2] cuMotion RMPflow pick-place; WebRTC livestream view; real-to-sim scan.
"""

import argparse
import json
import math
import queue
import sys
import threading
import time
from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit

from arms.sim.scene_spec import BASKET_OFFSET_M, BASKET_SIZE_M, PrimSpec, SceneSpec
from arms.sim.worker import FRAME_H, FRAME_W, CameraPose, NullBackend, SimBackend, WorkerCore
from contracts.common import CameraName, Point3, Pose2D
from contracts.sim import CameraFrame, InstanceInfo, SimLog, parse_gateway_to_sim
from utils.log import get_logger

log = get_logger(__name__)

ISAAC_VERSION = "6.2.0"
ROBOT_BODY_PRIM = "/World/Robot/Body"
JPEG_QUALITY = 70
DEFAULT_FPS = 10.0
RECONNECT_MAX_S = 10.0


class IsaacUnavailable(RuntimeError):
    """Isaac Sim cannot run in this Python (not installed / unsupported OS)."""


ISAAC_HELP = (
    "Isaac Sim 6.2 is not available in this Python. It runs only on Linux (Ubuntu 24.04) or Windows 11 with an "
    "RT-core NVIDIA GPU (RTX 4080 / 16 GB or better; A100/H100 do not work) and Python 3.12. Set it up on the GPU host "
    "(arms/sim/README.md 'GPU host setup'). On a Mac: the pipeline uses the robot fake (2D kinematic sim) by default, "
    "or run `python -m arms.sim.app --backend kinematic` to exercise the sim link without Isaac."
)


def require_isaac() -> Any:
    """Return ``isaacsim.SimulationApp`` or raise IsaacUnavailable with setup help. Import it before omni/pxr."""
    if sys.platform == "darwin":
        raise IsaacUnavailable(ISAAC_HELP)
    try:
        from isaacsim import SimulationApp  # type: ignore[import-not-found]
    except ImportError as exc:
        raise IsaacUnavailable(ISAAC_HELP) from exc
    return SimulationApp


def gateway_ws_url(gateway_url: str, token: str | None = None) -> str:
    """http(s)://host[/prefix] -> ws(s)://host[/prefix]/ws/sim[?token=...] (the query token is only an early reject)."""
    parts = urlsplit(gateway_url)
    scheme = {"https": "wss", "wss": "wss"}.get(parts.scheme, "ws")
    path = parts.path.rstrip("/") + "/ws/sim"
    query = f"token={quote(token)}" if token else ""
    return urlunsplit((scheme, parts.netloc, path, query, ""))


# --------------------------------------------------------------------------------------------------------------------
# Websocket thread (only enqueues; never touches USD)
# --------------------------------------------------------------------------------------------------------------------
class GatewayLink(threading.Thread):
    """Outbound websocket client with reconnect. Main loop reads ``inbox`` items: ("connected", None),
    ("disconnected", reason), ("cmd", GatewayToSim). Main loop sends with ``send(msg)`` / ``offer_frame(frame)``."""

    def __init__(self, url: str, hello: Any, recv_poll_s: float = 0.02) -> None:
        super().__init__(name="picky-sim-link", daemon=True)
        self.url = url
        self.hello = hello  # callable -> SimHello (data_hash may change after a reset)
        self.recv_poll_s = recv_poll_s
        self.inbox: queue.Queue[tuple[str, Any]] = queue.Queue()
        self._out: queue.Queue[str] = queue.Queue()
        self._frame: str | None = None
        self._frame_lock = threading.Lock()
        self._stop = threading.Event()
        self.connected = threading.Event()

    def send(self, msg: Any) -> None:
        """Queue a SimMessage (dropped while disconnected; the gateway resyncs from RobotState on welcome)."""
        if not self.connected.is_set():
            return
        if isinstance(msg, CameraFrame) and msg.slot_id is None:
            self.offer_frame(msg)
            return
        self._out.put(msg.model_dump_json() if hasattr(msg, "model_dump_json") else json.dumps(msg))

    def offer_frame(self, frame: CameraFrame) -> None:
        """Live-view frames: keep only the latest (never back up the socket)."""
        if self.connected.is_set():
            with self._frame_lock:
                self._frame = frame.model_dump_json()

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
                        log.error("gateway rejected sim worker: %s", err)
                        backoff = RECONNECT_MAX_S  # contracts/data mismatch: no point hammering
                        raise ConnectionError(err)
                    log.info("sim link up: %s (session %s)", self.url, welcome.session_id)
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
            self._frame = None

    def _pump(self, ws: Any) -> None:
        while not self._stop.is_set():
            while True:
                try:
                    ws.send(self._out.get_nowait())
                except queue.Empty:
                    break
            with self._frame_lock:
                frame, self._frame = self._frame, None
            if frame is not None:
                ws.send(frame)
            try:
                raw = ws.recv(timeout=self.recv_poll_s)
            except TimeoutError:
                continue
            try:
                self.inbox.put(("cmd", parse_gateway_to_sim(raw)))
            except Exception as exc:  # noqa: BLE001 - bad message: tell the gateway, keep going
                self._out.put(SimLog(level="error", message=f"unparsable command: {exc}").model_dump_json())


# --------------------------------------------------------------------------------------------------------------------
# Isaac backend (pxr scene from SceneSpec, kinematic robot, replicator camera)
# --------------------------------------------------------------------------------------------------------------------
def _rgb(hex_color: str) -> tuple[float, float, float]:
    h = hex_color.lstrip("#")
    return int(h[0:2], 16) / 255, int(h[2:4], 16) / 255, int(h[4:6], 16) / 255


def encode_jpeg(rgba: Any, quality: int = JPEG_QUALITY) -> bytes | None:
    """RGBA/RGB uint8 array -> JPEG bytes via cv2 (preferred) or PIL; None when neither is installed."""
    import numpy as np

    arr = np.asarray(rgba)[..., :3].astype("uint8")
    try:
        import cv2  # type: ignore[import-not-found]

        ok, buf = cv2.imencode(".jpg", arr[..., ::-1], [int(cv2.IMWRITE_JPEG_QUALITY), quality])
        return bytes(buf) if ok else None
    except ImportError:
        pass
    try:
        import io

        from PIL import Image  # type: ignore[import-not-found]

        out = io.BytesIO()
        Image.fromarray(arr).save(out, format="JPEG", quality=quality)
        return out.getvalue()
    except ImportError:
        return None


class IsaacBackend:
    """Implements worker.SimBackend on a live SimulationApp. Only call from the main thread."""

    def __init__(self, sim_app: Any, width: int = FRAME_W, height: int = FRAME_H) -> None:
        import omni.usd  # type: ignore[import-not-found]
        from pxr import UsdGeom  # type: ignore[import-not-found]

        self.app, self.w, self.h = sim_app, width, height
        ctx = omni.usd.get_context()
        ctx.new_stage()
        self.stage = ctx.get_stage()
        UsdGeom.SetStageUpAxis(self.stage, UsdGeom.Tokens.z)
        UsdGeom.SetStageMetersPerUnit(self.stage, 1.0)
        self._paths: dict[str, str] = {}
        self.robot: Any = None
        """arms.robot RidgebackFranka handle when the asset loaded (None = proxy body)."""
        self._robot_xf: Any = None
        self._cam_op: Any = None
        self._annotator: Any = None
        self._lights()
        self._robot()
        self._camera()

    # -- setup ---------------------------------------------------------------------------------------------------------
    def _lights(self) -> None:
        from pxr import UsdLux  # type: ignore[import-not-found]

        dome = UsdLux.DomeLight.Define(self.stage, "/World/Lights/Dome")
        dome.CreateIntensityAttr(800.0)
        sun = UsdLux.DistantLight.Define(self.stage, "/World/Lights/Sun")
        sun.CreateIntensityAttr(2500.0)
        sun.CreateAngleAttr(1.0)

    def _robot(self) -> None:
        """/World/Robot root xform (we set its pose; base + basket follow). The robot arm's Ridgeback-Franka goes
        under /World/Robot/Body (``arms.robot.create_isaac_robot``); if it can't load, a proxy body is drawn."""
        from pxr import UsdGeom  # type: ignore[import-not-found]

        from arms.robot import create_isaac_robot  # composition root: the robot arm owns the Isaac robot

        root = UsdGeom.Xform.Define(self.stage, "/World/Robot")
        self._robot_xf = UsdGeom.XformCommonAPI(root)
        loaded = False
        try:
            self.robot = create_isaac_robot(ROBOT_BODY_PRIM)
            loaded = True
        except Exception as exc:  # noqa: BLE001 - asset root is cloud-hosted; fall back to the proxy
            log.warning("Ridgeback-Franka unavailable (%s); using proxy robot", exc)
        if not loaded:
            self._define(
                PrimSpec(
                    path="/World/Robot/Body/base",
                    role="base",
                    center=(0, 0, 0.15),
                    size=(0.96, 0.79, 0.3),
                    color="#2d6cdf",
                    collider=False,
                )
            )
            self._define(
                PrimSpec(
                    path="/World/Robot/Body/mast",
                    role="base",
                    shape="cylinder",
                    center=(0.2, 0, 0.8),
                    size=(0.12, 0.12, 1.0),
                    color="#d0d4da",
                    collider=False,
                )
            )
        bx, by, bz = BASKET_SIZE_M
        ox, oy, oz = BASKET_OFFSET_M
        self._define(
            PrimSpec(
                path="/World/Robot/Basket",
                role="base",
                center=(ox, oy, oz - 0.01),
                size=(bx, by, 0.02),
                color="#f2a33a",
                collider=False,
            )
        )

    def _camera(self) -> None:
        import omni.replicator.core as rep  # type: ignore[import-not-found]
        from pxr import UsdGeom  # type: ignore[import-not-found]

        cam = UsdGeom.Camera.Define(self.stage, "/World/HeadCam")
        h_ap = 20.955
        cam.CreateHorizontalApertureAttr(h_ap)
        cam.CreateVerticalApertureAttr(h_ap * self.h / self.w)
        cam.CreateFocalLengthAttr(h_ap / (2 * math.tan(CameraPose(0, 0, 0, 0).hfov / 2)))
        cam.CreateClippingRangeAttr((0.05, 100.0))
        self._cam_op = UsdGeom.Xformable(cam).AddTransformOp()
        rp = rep.create.render_product("/World/HeadCam", (self.w, self.h))
        self._annotator = rep.AnnotatorRegistry.get_annotator("rgb")
        self._annotator.attach([rp])

    def _define(self, spec: PrimSpec) -> str:
        from pxr import Gf, Sdf, UsdGeom, UsdPhysics  # type: ignore[import-not-found]

        if spec.shape == "cylinder":
            geom = UsdGeom.Cylinder.Define(self.stage, spec.path)
            geom.CreateRadiusAttr(0.5)
            geom.CreateHeightAttr(1.0)
            geom.CreateAxisAttr(UsdGeom.Tokens.z)
        elif spec.shape == "sphere":
            geom = UsdGeom.Sphere.Define(self.stage, spec.path)
            geom.CreateRadiusAttr(0.5)
        else:
            geom = UsdGeom.Cube.Define(self.stage, spec.path)
            geom.CreateSizeAttr(1.0)
        xf = UsdGeom.XformCommonAPI(geom)
        xf.SetTranslate(Gf.Vec3d(*spec.center))
        xf.SetRotate(Gf.Vec3f(0.0, 0.0, math.degrees(spec.yaw)))
        xf.SetScale(Gf.Vec3f(*spec.size))
        geom.CreateDisplayColorAttr([Gf.Vec3f(*_rgb(spec.color))])
        prim = geom.GetPrim()
        if spec.collider:
            UsdPhysics.CollisionAPI.Apply(prim)
        for key in ("sku", "instance_id", "slot_id", "lot_id", "fixture_id", "label_text", "barcode"):
            val = getattr(spec, key)
            if val is not None:
                prim.CreateAttribute(f"picky:{key}", Sdf.ValueTypeNames.String).Set(str(val))
        if spec.price_cents is not None:
            prim.CreateAttribute("picky:price_cents", Sdf.ValueTypeNames.Int).Set(spec.price_cents)
        if spec.freshness is not None:
            prim.CreateAttribute("picky:freshness", Sdf.ValueTypeNames.Float).Set(spec.freshness)
        return spec.path

    # -- SimBackend ----------------------------------------------------------------------------------------------------
    def build(self, spec: SceneSpec) -> None:
        from pxr import Sdf  # type: ignore[import-not-found]

        if self.stage.GetPrimAtPath("/World/Store"):
            self.stage.RemovePrim("/World/Store")
        self._paths = {}
        with Sdf.ChangeBlock():
            for p in spec.prims:
                path = self._define(p)
                if p.instance_id:
                    self._paths[p.instance_id] = path
        for _ in range(10):  # warm-up: first replicator frames are black
            self.app.update()
        log.info("scene built: %s, %d prims, %d instances", spec.store_id, len(spec.prims), len(spec.instances))

    def set_robot_pose(self, pose: Pose2D) -> None:
        from pxr import Gf  # type: ignore[import-not-found]

        self._robot_xf.SetTranslate(Gf.Vec3d(pose.x, pose.y, 0.0))
        self._robot_xf.SetRotate(Gf.Vec3f(0.0, 0.0, math.degrees(pose.theta)))

    def set_instance_position(self, instance_id: str, position: Point3) -> None:
        from pxr import Gf, UsdGeom  # type: ignore[import-not-found]

        path = self._paths.get(instance_id)
        if path:
            UsdGeom.XformCommonAPI(self.stage.GetPrimAtPath(path)).SetTranslate(
                Gf.Vec3d(position.x, position.y, position.z)
            )

    def render(self, camera: CameraName, cam: CameraPose) -> tuple[bytes, int, int] | None:
        from pxr import Gf  # type: ignore[import-not-found]

        r, u, f = cam.basis()  # USD camera: local +X = right, +Y = up, -Z = forward (row-vector matrices)
        self._cam_op.Set(
            Gf.Matrix4d(r[0], r[1], r[2], 0, u[0], u[1], u[2], 0, -f[0], -f[1], -f[2], 0, cam.x, cam.y, cam.z, 1)
        )
        self.app.update()
        data = self._annotator.get_data()
        if data is None or getattr(data, "size", 0) == 0:
            return None
        jpeg = encode_jpeg(data)
        if jpeg is None:
            log.error("no JPEG encoder: pip install opencv-python-headless (or pillow) on the GPU host")
            return None
        return jpeg, self.w, self.h

    def step(self) -> None:
        self.app.update()


# --------------------------------------------------------------------------------------------------------------------
# Main loop
# --------------------------------------------------------------------------------------------------------------------
def run(
    backend: SimBackend,
    link: GatewayLink | None,
    core: WorkerCore,
    fps: float = DEFAULT_FPS,
    is_running: Any = lambda: True,
    max_seconds: float | None = None,
) -> None:
    """Drain commands, step actions, render, publish frames. Returns when ``is_running()`` is False."""
    period = 1.0 / fps
    last = time.monotonic()
    next_frame = last
    started = last
    step = getattr(backend, "step", None)
    while is_running():
        now = time.monotonic()
        if max_seconds is not None and now - started > max_seconds:
            break
        dt, last = now - last, now
        if link is not None:
            while True:
                try:
                    kind, item = link.inbox.get_nowait()
                except queue.Empty:
                    break
                if kind == "cmd":
                    if getattr(item, "type", None) != "welcome":
                        core.submit(item)
                elif kind == "connected":
                    core.publish_state(force=True)
                    core.emit(InstanceInfo(instances=list(core.instances.values())))
                elif kind == "disconnected":
                    core.abort(str(item))
        core.tick(dt)
        rendered = False
        if link is not None and link.connected.is_set() and now >= next_frame:
            frame = core.capture_frame("head")
            rendered = True
            if frame is not None:
                link.offer_frame(frame)
            next_frame = now + period
        if step is not None and not rendered:
            step()
        elif step is None:
            time.sleep(max(0.0, min(period, 0.02)))


def main(argv: list[str] | None = None) -> int:
    from utils.config import get_settings

    settings = get_settings()
    ap = argparse.ArgumentParser(prog="python -m arms.sim.app", description="Picky Isaac Sim worker")
    ap.add_argument("--store", default=settings.picky_default_store)
    ap.add_argument("--gateway-url", default=settings.picky_gateway_url)
    ap.add_argument("--token", default=settings.picky_sim_token)
    ap.add_argument("--backend", choices=["isaac", "kinematic"], default="isaac")
    ap.add_argument("--fps", type=float, default=DEFAULT_FPS)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--width", type=int, default=FRAME_W)
    ap.add_argument("--height", type=int, default=FRAME_H)
    ap.add_argument("--no-headless", action="store_true", help="open the Isaac UI (needs a display)")
    ap.add_argument("--worker-id", default="isaac-0")
    args = ap.parse_args(argv)

    sim_app: Any = None
    backend: SimBackend
    if args.backend == "isaac":
        try:
            SimulationApp = require_isaac()
        except IsaacUnavailable as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        sim_app = SimulationApp({"headless": not args.no_headless, "width": args.width, "height": args.height})
        backend = IsaacBackend(sim_app, args.width, args.height)  # omni/pxr imports happen after SimulationApp
    else:
        backend = NullBackend()

    url = gateway_ws_url(args.gateway_url, args.token)
    link: GatewayLink | None = None
    core = WorkerCore(backend, emit=lambda msg: link.send(msg) if link else None, store_id=args.store)
    try:
        core.load_scene(args.store, args.seed)
    except Exception as exc:  # noqa: BLE001 - start anyway; the gateway's ResetCmd can push the planogram
        log.warning("no initial scene for %s (%s); waiting for reset", args.store, exc)

    sim_version = f"isaacsim-{ISAAC_VERSION}" if args.backend == "isaac" else "kinematic"
    link = GatewayLink(url, lambda: core.hello(args.token, args.worker_id, sim_version))
    link.start()
    log.info("sim worker (%s) store=%s -> %s", args.backend, args.store, url.split("?")[0])
    try:
        run(backend, link, core, fps=args.fps, is_running=(sim_app.is_running if sim_app else lambda: True))
    except KeyboardInterrupt:
        pass
    finally:
        link.stop()
        if sim_app is not None:
            sim_app.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
