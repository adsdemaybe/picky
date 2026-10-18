"""Hardware side of the SO-101 gateway bridge (``bridge.py``): cameras, face blur and the arm.

Everything heavy (OpenCV, ikpy, LeRobot, Perceptron) is imported lazily, so ``--dry --camera fake`` runs on a Mac with
the repo's base deps only (Pillow + numpy) and the unit tests never touch hardware.

Cameras (``open_camera_source``):
- ``fake``            synthetic Pillow frames (table, plate, a moving marker, the bridge's status line)
- ``image:<path>``    loops one image, or every .jpg/.png in a folder (recorded rig frames)
- ``<index|name>``    a real camera (OpenCV; ``SO101_CAMERA`` by default). One thread owns the device and also serves
                      ``camera.grab_frame`` for the pick routine (Windows lets only one client open a webcam).

Privacy (CLAUDE.md section 8): every REAL frame is face-blurred here, before it is streamed to the gateway or sent to
Perceptron. The gateway lists the bridge's cameras like Isaac's rendered ones and so does not face-check them itself;
the blur on the laptop is the guarantee. Detector: OpenCV's bundled Haar cascades (frontal + profile). If they cannot
load, real frames are pixelated whole (fail closed).

Arm (``DryRig`` / ``So101Rig``): ``pick(product)`` = Perceptron grounding + IK plan (``pick_routine.plan``) and the
waypoints up to the grasp + short lift; ``place()`` = carry to the plate / basket, release, retreat, home. ``stop``
raises ``MotionCancelled`` at the next interpolation step (``pick_place.ABORT_CHECK``).
"""

from __future__ import annotations

import io
import math
import os
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import numpy as np

from utils.log import get_logger

log = get_logger("arms.robot.so101.bridge_rig")

JPEG_QUALITY = 80
PICK_LAST_WAYPOINT = "lift straight up"
"""pick() runs the routine's waypoints up to and including this one (object gripped, just off the table); place()
runs the rest (lift high -> transit -> over the destination -> lower -> release -> retreat) and goes home."""
DRY_WAYPOINTS = (
    "hover over target",
    "straddle",
    "close",
    "lift straight up",
    "lift high",
    "transit",
    "over destination",
    "lower",
    "release",
    "retreat",
)


class MotionCancelled(Exception):
    """A stop (gateway StopCmd or the operator) interrupted the motion; the arm holds where it is."""


class RigError(Exception):
    """A rig failure with a ``contracts.sim.SimErrorCode``-compatible ``code``."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


# =====================================================================================================================
# face blur (real frames only)
# =====================================================================================================================
_CASCADES: list[Any] | None = None
_CASCADE_LOCK = threading.Lock()


def _cascades() -> list[Any]:
    """OpenCV's bundled Haar face cascades (frontal + profile); [] when OpenCV or the files are missing."""
    global _CASCADES
    with _CASCADE_LOCK:
        if _CASCADES is None:
            found: list[Any] = []
            try:
                import cv2

                for name in ("haarcascade_frontalface_default.xml", "haarcascade_profileface.xml"):
                    c = cv2.CascadeClassifier(os.path.join(cv2.data.haarcascades, name))
                    if not c.empty():
                        found.append(c)
            except Exception as exc:  # noqa: BLE001 - no OpenCV / no cascade data: callers fail closed
                log.warning("face detector unavailable (%s); real frames will be pixelated whole", exc)
            _CASCADES = found
        return _CASCADES


def pixelate(img: np.ndarray, block: int = 24) -> np.ndarray:
    """Coarse mosaic of a whole image (numpy only): the fail-closed privacy fallback."""
    h, w = img.shape[:2]
    block = max(1, min(block, h, w))
    bh, bw = h // block, w // block
    small = img[: bh * block, : bw * block].reshape(bh, block, bw, block, -1).mean(axis=(1, 3))
    out = np.repeat(np.repeat(small, block, axis=0), block, axis=1).astype(img.dtype)
    full = img.copy()
    full[: bh * block, : bw * block] = out.reshape(full[: bh * block, : bw * block].shape)
    return full


def blur_faces_bgr(img: np.ndarray) -> tuple[np.ndarray, int]:
    """Blur every detected face (box grown 25%, strong mosaic). Returns (image, faces); faces = -1 means no detector
    was available and the whole frame was pixelated instead (fail closed)."""
    cascades = _cascades()
    if not cascades:
        return pixelate(img), -1
    import cv2

    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    boxes: list[tuple[int, int, int, int]] = []
    for c in cascades:
        for x, y, w, h in c.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=5, minSize=(24, 24)):
            boxes.append((int(x), int(y), int(w), int(h)))
    if not boxes:
        return img, 0
    out = img.copy()
    H, W = out.shape[:2]
    for x, y, w, h in boxes:
        gx, gy = int(w * 0.25), int(h * 0.25)
        x0, y0, x1, y1 = max(0, x - gx), max(0, y - gy), min(W, x + w + gx), min(H, y + h + gy)
        roi = out[y0:y1, x0:x1]
        if roi.size:
            small = cv2.resize(roi, (max(1, (x1 - x0) // 16), max(1, (y1 - y0) // 16)), interpolation=cv2.INTER_LINEAR)
            out[y0:y1, x0:x1] = cv2.resize(small, (x1 - x0, y1 - y0), interpolation=cv2.INTER_NEAREST)
    return out, len(boxes)


# =====================================================================================================================
# camera sources
# =====================================================================================================================
def _encode_pil(img: Any) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=JPEG_QUALITY)
    return buf.getvalue()


class FrameSource:
    """A camera: ``read_jpeg()`` -> (JPEG bytes, w, h) for the app; ``read_bgr()`` -> image for grounding (or None)."""

    real = False
    name = "camera"
    hud: Callable[[], str] | None = None

    def read_jpeg(self) -> tuple[bytes, int, int]:  # pragma: no cover - interface
        raise NotImplementedError

    def read_bgr(self) -> np.ndarray | None:
        return None

    def close(self) -> None:
        return None


class FakeCamera(FrameSource):
    """Synthetic overhead view of the bench (Pillow): table, plate, a marker that moves (so a live stream visibly
    updates) and the bridge's status line. No faces, so no blur."""

    name = "fake"

    def __init__(self, w: int = 640, h: int = 480, title: str = "SO-101 bench (synthetic camera)") -> None:
        self.w, self.h, self.title = w, h, title
        self._n = 0

    def _image(self) -> Any:
        from PIL import Image, ImageDraw

        self._n += 1
        img = Image.new("RGB", (self.w, self.h), (196, 164, 120))  # wooden table
        d = ImageDraw.Draw(img)
        cx, cy, r = int(self.w * 0.72), int(self.h * 0.55), int(min(self.w, self.h) * 0.16)
        d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=(245, 245, 240), outline=(120, 120, 120), width=3)  # plate
        d.rectangle(
            (int(self.w * 0.22), int(self.h * 0.45), int(self.w * 0.30), int(self.h * 0.62)), fill=(40, 150, 70)
        )
        bx, by = int(self.w * 0.12), int(self.h * 0.82)
        d.ellipse((bx - 30, by - 30, bx + 30, by + 30), fill=(60, 60, 60))  # arm base
        t = self._n / 6.0
        mx, my = int(self.w * 0.45 + 90 * math.cos(t)), int(self.h * 0.42 + 50 * math.sin(t))
        d.line((bx, by, mx, my), fill=(30, 30, 30), width=8)
        d.ellipse((mx - 9, my - 9, mx + 9, my + 9), fill=(230, 80, 40))
        d.rectangle((0, 0, self.w, 26), fill=(0, 0, 0))
        d.text((8, 7), f"{self.title}  #{self._n}", fill=(255, 255, 255))
        status = self.hud() if self.hud else ""
        if status:
            d.rectangle((0, self.h - 26, self.w, self.h), fill=(0, 0, 0))
            d.text((8, self.h - 19), status[:90], fill=(120, 255, 140))
        return img

    def read_jpeg(self) -> tuple[bytes, int, int]:
        return _encode_pil(self._image()), self.w, self.h

    def read_bgr(self) -> np.ndarray | None:
        return np.asarray(self._image())[:, :, ::-1].copy()


class ImageLoopCamera(FrameSource):
    """Loops recorded frames (one image or every .jpg / .png in a folder). Treated as REAL footage: face-blurred."""

    real = True
    name = "image"

    def __init__(self, path: str, period_s: float = 1.0) -> None:
        p = Path(path).expanduser()
        files = sorted(f for f in p.iterdir() if f.suffix.lower() in (".jpg", ".jpeg", ".png")) if p.is_dir() else [p]
        if not files or not files[0].exists():
            raise FileNotFoundError(f"no images at {path}")
        self.files, self.period_s, self._t0 = files, period_s, time.monotonic()

    def read_bgr(self) -> np.ndarray | None:
        from PIL import Image

        i = int((time.monotonic() - self._t0) / self.period_s) % len(self.files)
        with Image.open(self.files[i]) as im:
            rgb = np.asarray(im.convert("RGB"))
        return blur_faces_bgr(rgb[:, :, ::-1].copy())[0]

    def read_jpeg(self) -> tuple[bytes, int, int]:
        from PIL import Image

        bgr = self.read_bgr()
        assert bgr is not None
        img = Image.fromarray(bgr[:, :, ::-1])
        return _encode_pil(img), img.width, img.height


class LiveCamera(FrameSource):
    """A real camera owned by one capture thread (OpenCV). ``latest`` is the newest frame, FACE-BLURRED, so it can
    stand in for ``camera._ACTIVE`` and the pick routine's ``grab_frame`` gets the same (blurred) frames."""

    real = True
    name = "live"

    def __init__(self, index: str, w: int = 640, h: int = 480, register: bool = True) -> None:
        import cv2

        from . import camera as rig_camera

        self.index = index
        self.cap = rig_camera.open_camera(index)
        self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, w)
        self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, h)
        ok, frame = self.cap.read()
        if not ok or frame is None:
            self.cap.release()
            raise RuntimeError(f"camera {index} returned no frame (open in another app? check SO101_CAMERA)")
        self._raw = frame
        self._seq = 0
        self._blur_seq = -1
        self._blurred = frame
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name=f"so101-cam-{index}", daemon=True)
        self._thread.start()
        if register:  # share the device with pick_routine.grab_frame (it reads _ACTIVE.latest when the index matches)
            rig_camera._ACTIVE = self  # noqa: SLF001 - the rig's documented sharing hook (see camera.Recorder)

    def _run(self) -> None:
        while not self._stop.is_set():
            ok, frame = self.cap.read()
            if ok and frame is not None:
                with self._lock:
                    self._raw, self._seq = frame, self._seq + 1
            else:
                time.sleep(0.02)

    @property
    def latest(self) -> np.ndarray:
        """Newest frame, faces blurred (blurred once per new frame)."""
        with self._lock:
            raw, seq = self._raw, self._seq
        if seq != self._blur_seq:
            out, n = blur_faces_bgr(raw)
            if n > 0:
                log.info("blurred %d face(s) on camera %s", n, self.index)
            self._blurred, self._blur_seq = out, seq
        return self._blurred

    def read_bgr(self) -> np.ndarray | None:
        return self.latest.copy()

    def read_jpeg(self) -> tuple[bytes, int, int]:
        import cv2

        img = self.latest
        status = self.hud() if self.hud else ""
        if status:
            img = img.copy()
            y = img.shape[0]
            cv2.rectangle(img, (0, y - 28), (img.shape[1], y), (0, 0, 0), -1)
            cv2.putText(img, status[:80], (8, y - 9), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (120, 255, 140), 1, cv2.LINE_AA)
        ok, jpg = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY])
        if not ok:
            raise RuntimeError("JPEG encode failed")
        return jpg.tobytes(), img.shape[1], img.shape[0]

    def close(self) -> None:
        from . import camera as rig_camera

        self._stop.set()
        self._thread.join(timeout=2)
        self.cap.release()
        if getattr(rig_camera, "_ACTIVE", None) is self:
            rig_camera._ACTIVE = None  # noqa: SLF001


def open_camera_source(spec: str, *, register: bool = True) -> FrameSource:
    """'fake' | 'image:<path>' | a camera index / name (``SO101_CAMERA``)."""
    if spec == "fake":
        return FakeCamera()
    if spec.startswith("image:"):
        return ImageLoopCamera(spec.split(":", 1)[1])
    return LiveCamera(spec, register=register)


# =====================================================================================================================
# the arm
# =====================================================================================================================
@dataclass
class PickOutcome:
    product: str
    target_xy: tuple[float, float] | None = None
    """Object position on the table, robot base frame (x forward, y left), meters; None when simulated."""
    waypoints: list[str] = field(default_factory=list)
    planned: bool = False
    """True when a real perception + IK plan was made (camera, calibration and grounding available)."""
    note: str = ""


Confirm = Callable[[str], bool]
Say = Callable[[str], None]


def _always(_: str) -> bool:
    return True


class RoutinePlanner:
    """``pick_routine.plan`` on a (blurred) frame: Perceptron grounding of the product + the destination, the
    calibrated camera model (calibrate_camera.py) and ikpy IK, steered by the learned Q-policy when one exists."""

    def __init__(self, dest: str = "plate", object_height: float = 0.05) -> None:
        from .config import RigConfig
        from .kinematics import SO101Kinematics
        from .scene import CameraModel

        self.cfg = RigConfig()
        self.kin = SO101Kinematics(self.cfg)
        self.cam = CameraModel.load()  # FileNotFoundError until calibrate_camera has run
        self.dest = dest
        self.object_height = object_height

    def steer(self) -> dict[str, float] | None:
        try:
            from .train_loop import QPolicy

            policy = QPolicy.load()
            return policy.steer(policy.greedy())
        except Exception as exc:  # noqa: BLE001 - no policy yet: the base routine
            log.debug("no learned steering (%s)", exc)
            return None

    def plan(self, product: str, frame: np.ndarray, seed: dict[str, float] | None = None) -> Any:
        from . import pick_routine

        command = f"pick up the {product} and put it on the {self.dest}"
        return pick_routine.plan(
            self.cfg,
            self.kin,
            self.cam,
            command,
            dict(seed or self.cfg.home_deg),
            self.object_height,
            frame=frame.copy(),
            steer=self.steer(),
        )


def plan_error_code(error: str) -> str:
    """Map a pick_routine plan error to a SimErrorCode: not seen -> no_instance, everything else -> grasp_failed."""
    return "no_instance" if "not found" in error else "grasp_failed"


def split_plan(p: Any) -> tuple[Any, Any]:
    """(pick part, place part) of a ScenePlan, split after PICK_LAST_WAYPOINT."""
    names = [w.name for w in p.waypoints]
    k = names.index(PICK_LAST_WAYPOINT) + 1 if PICK_LAST_WAYPOINT in names else len(names)
    return replace(p, waypoints=p.waypoints[:k]), replace(p, waypoints=p.waypoints[k:])


class DryRig:
    """No serial port: arm motion is simulated (logged waypoints, ``step_s`` per waypoint, stop honoured between
    steps). With a real camera, calibration and a grounding backend it also makes the REAL perception + IK plan (so
    ``--dry`` on the bench checks everything except the motors); otherwise the plan is simulated too."""

    name = "dry"

    def __init__(
        self,
        camera: FrameSource | None = None,
        *,
        step_s: float = 0.4,
        planner: RoutinePlanner | None = None,
        say: Say | None = None,
    ) -> None:
        self.camera = camera
        self.step_s = step_s
        self.planner = planner
        self.say = say or (lambda m: log.info("%s", m))
        self.held: str | None = None
        self.fail_next: tuple[str, str] | None = None
        """Tests: (code, message) makes the next pick fail with that RigError."""

    def _walk(self, names: list[str], stop: threading.Event) -> None:
        for n in names:
            if stop.is_set():
                raise MotionCancelled(f"stopped before '{n}'")
            self.say(f"  (dry) {n}")
            end = time.monotonic() + self.step_s
            while time.monotonic() < end:
                if stop.is_set():
                    raise MotionCancelled(f"stopped during '{n}'")
                time.sleep(min(0.02, self.step_s))

    def pick(self, product: str, stop: threading.Event, confirm: Confirm = _always) -> PickOutcome:
        if self.fail_next is not None:
            code, msg = self.fail_next
            self.fail_next = None
            raise RigError(code, msg)
        out = PickOutcome(product=product)
        frame = self.camera.read_bgr() if (self.planner is not None and self.camera is not None) else None
        if self.planner is not None and frame is not None and self.camera is not None and self.camera.real:
            p = self.planner.plan(product, frame)
            if p.error:
                raise RigError(plan_error_code(p.error), p.error)
            out.planned, out.target_xy = True, p.target_xy
            names = [w.name for w in split_plan(p)[0].waypoints]
            self.say(f"(dry) plan for {product!r}: target at {p.target_xy}, roll {p.roll}")
        else:
            names = list(DRY_WAYPOINTS[: DRY_WAYPOINTS.index(PICK_LAST_WAYPOINT) + 1])
            out.note = "simulated plan (no camera calibration / grounding in dry mode)"
        if not confirm(f"pick {product!r} ({len(names)} waypoints, DRY)"):
            raise MotionCancelled("operator declined")
        self._walk(names, stop)
        out.waypoints = names
        self.held = product
        return out

    def place(self, stop: threading.Event, confirm: Confirm = _always) -> list[str]:
        if self.held is None:
            raise RigError("no_instance", "the gripper is empty")
        names = [*DRY_WAYPOINTS[DRY_WAYPOINTS.index(PICK_LAST_WAYPOINT) + 1 :], "home"]
        if not confirm(f"place {self.held!r} ({len(names)} waypoints, DRY)"):
            raise MotionCancelled("operator declined")
        self._walk(names, stop)
        self.held = None
        return names

    def home(self, stop: threading.Event, confirm: Confirm = _always) -> None:
        self._walk(["home"], stop)

    def close(self) -> None:
        return None


class So101Rig:
    """The physical SO-101 (LeRobot over ``SO101_PORT``): camera-guided pick_routine, split into pick and place."""

    name = "so101"

    def __init__(
        self, camera: FrameSource, *, dest: str = "plate", say: Say | None = None, home_on_connect: bool = True
    ) -> None:
        from . import pick_place, pick_routine
        from .calibrate_camera import set_limits_from_calibration

        if not camera.real:
            raise RigError("not_ready", "the real arm needs the real camera (drop --camera fake or add --dry)")
        self.camera = camera
        self.say = say or (lambda m: log.info("%s", m))
        self.planner = RoutinePlanner(dest=dest)
        self.cfg, self.kin = self.planner.cfg, self.planner.kin
        self._pp, self._pr = pick_place, pick_routine
        self.say(f"connecting to the SO-101 on {self.cfg.port} ...")
        self.robot = pick_place.connect_robot(self.cfg)
        set_limits_from_calibration(self.kin, self.robot)
        self.home_pose = dict(self.cfg.home_deg)
        self._held_plan: Any = None
        self.held: str | None = None
        self._lock = threading.Lock()
        if home_on_connect:  # connect_robot holds the present pose; homing is the first motion
            self._run(lambda: pick_routine.go_home(self.robot, self.home_pose, pick_routine.GRIP_OPEN), None)

    def _run(self, fn: Callable[[], Any], stop: threading.Event | None) -> Any:
        """Run one blocking arm routine with the stop flag wired into pick_place.move (between interpolation steps)."""
        with self._lock:
            self._pp.ABORT_CHECK = (lambda: stop.is_set()) if stop is not None else None
            try:
                return fn()
            except self._pp.Interrupted as exc:
                raise MotionCancelled("stopped mid-move; the arm holds its pose") from exc
            finally:
                self._pp.ABORT_CHECK = None

    def pick(self, product: str, stop: threading.Event, confirm: Confirm = _always) -> PickOutcome:
        if self.held is not None:
            raise RigError("grasp_failed", f"already holding {self.held!r}: place it first")
        frame = self.camera.read_bgr()
        if frame is None:
            raise RigError("not_ready", "no camera frame")
        t0 = time.monotonic()
        p = self.planner.plan(product, frame, seed=self._pr.read_pose(self.robot)[0])
        self.say(self._pr.describe(p) + f"\n  (planned in {time.monotonic() - t0:.1f}s, frame: {p.frame_path})")
        if p.error:
            raise RigError(plan_error_code(p.error), p.error)
        pick_part, place_part = split_plan(p)
        if stop.is_set():
            raise MotionCancelled("stopped before the first move")
        if not confirm(f"pick {product!r} at {p.target_xy} (real arm moves)"):
            raise MotionCancelled("operator declined")
        self._run(lambda: self._pr.execute(self.robot, self.kin, pick_part, cfg=None, log=self.say), stop)
        self._held_plan, self.held = place_part, product
        return PickOutcome(
            product=product, target_xy=p.target_xy, waypoints=[w.name for w in pick_part.waypoints], planned=True
        )

    def place(self, stop: threading.Event, confirm: Confirm = _always) -> list[str]:
        if self._held_plan is None:
            raise RigError("no_instance", "the gripper is empty")
        names = [w.name for w in self._held_plan.waypoints]
        if not confirm(f"place {self.held!r} on the {self.planner.dest} (real arm moves)"):
            raise MotionCancelled("operator declined")
        plan = self._held_plan

        def go() -> None:
            self._pr.execute(self.robot, self.kin, plan, cfg=None, log=self.say)
            self._pr.go_home(self.robot, self.home_pose, self._pr.GRIP_OPEN)

        self._run(go, stop)
        self._held_plan, self.held = None, None
        return [*names, "home"]

    def home(self, stop: threading.Event, confirm: Confirm = _always) -> None:
        if not confirm("move the arm home (real arm moves)"):
            raise MotionCancelled("operator declined")
        self._run(lambda: self._pr.go_home(self.robot, self.home_pose, self._pr.GRIP_OPEN), stop)

    def close(self) -> None:
        try:
            self._run(lambda: self._pr.go_home(self.robot, self.home_pose, self._pr.GRIP_OPEN), None)
        except Exception as exc:  # noqa: BLE001 - always release the bus
            log.warning("could not home the arm on exit: %s", exc)
        self.robot.disconnect()


__all__ = [
    "DRY_WAYPOINTS",
    "PICK_LAST_WAYPOINT",
    "DryRig",
    "FakeCamera",
    "FrameSource",
    "ImageLoopCamera",
    "LiveCamera",
    "MotionCancelled",
    "PickOutcome",
    "RigError",
    "RoutinePlanner",
    "So101Rig",
    "blur_faces_bgr",
    "open_camera_source",
    "pixelate",
    "plan_error_code",
    "split_plan",
]
