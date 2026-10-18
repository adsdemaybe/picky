"""Third-person "director" (chase) camera ``<robot_id>.director``: the livestream's main view (contracts 0.2.3).
Off unless ``PICKY_SIM_DIRECTOR`` is set on the worker.

    PICKY_SIM_DIRECTOR=all | rover-ave-1[,rover-ave-2]   robots to follow ('all' = every rover hosted here)
    PICKY_SIM_DIRECTOR_FPS=10                            frames per second per followed robot (0.5-15)
    PICKY_SIM_DIRECTOR_SIZE=640x480                      frame size
    PICKY_SIM_DIRECTOR_VIEW=back,side,z                  optional: eye offsets in m (default 1.15,0.5,2.1)
    PICKY_SIM_DIRECTOR_DIR=/data/.../director            optional: ALSO write frames to disk for the video tools

A followed robot's director camera is a REGULAR camera: listed in ``SimHello.cameras`` (kind ``director``), answered
by ``capture`` (snapshots), and streamed over ``/ws/sim`` as unsolicited ``CameraFrame``s at the director's own fps
while the gateway has it in its ``set_streams`` set (the web app opened ``/api/cameras/<robot>.director/stream.mjpg``).
Nothing is rendered for it while nobody watches and no disk folder is set.

With ``PICKY_SIM_DIRECTOR_DIR`` the frames also land in ``<dir>/<robot_id>/director/<epoch_ms>.jpg`` and every frame
the worker renders for a followed robot is TEED into ``<dir>/<robot_id>/<camera>/<epoch_ms>.jpg`` (scan / capture
results, stream frames, the T1 policy pictures taken while the arm moves) for ``arms/sim/tools/director`` (the video
compositor).

The camera rides behind, to the right of and above the rover (a smoothed heading, so turns don't whip it around) and
looks at a point just ahead of the rover at arm height, so the frame shows the rover, its SO-101, its basket and the
shelf it faces. Isaac store scenes are synthetic renders with no people in them (the gateway still runs every frame
through the cameras service's face blur before it is served).

Cost: one extra render product per followed robot, only while it is wanted. A frame due in the same loop tick as a
stream frame shares its replicator step (``Director.prepare`` poses the camera before the streams render).
"""

from __future__ import annotations

import base64
import math
import os
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from arms.sim.geometry import CameraPose
from contracts.cameras import CameraInfo, CameraMount
from contracts.common import Pose2D
from contracts.sim import CameraFrame
from utils.log import get_logger

log = get_logger(__name__)

DIRECTOR_ENV = "PICKY_SIM_DIRECTOR"
DIRECTOR_DIR_ENV = "PICKY_SIM_DIRECTOR_DIR"
DIRECTOR_FPS_ENV = "PICKY_SIM_DIRECTOR_FPS"
DIRECTOR_VIEW_ENV = "PICKY_SIM_DIRECTOR_VIEW"
DIRECTOR_SIZE_ENV = "PICKY_SIM_DIRECTOR_SIZE"
CAMERA_SUFFIX = "director"
ALL = "all"
DEFAULT_FPS = 10.0
"""Livestream target (>= 8 fps on Isaac); the replicator step is shared with the streams due in the same tick."""
DEFAULT_SIZE = (640, 480)
BACK_M = 1.15
"""Eye distance behind the rover (along its smoothed heading)."""
SIDE_M = 0.5
"""Eye offset to the rover's right (a 3/4 view: the arm and the shelf it faces are both in frame)."""
EYE_Z_M = 2.1
"""Above the 1.8 m gondolas, below the 3.0 m ceiling. PICKY_SIM_DIRECTOR_VIEW='back,side,z' overrides all three."""
LOOK_AHEAD_M = 0.35
LOOK_Z_M = 0.75
HFOV_DEG = 74.0
HEADING_SMOOTH_S = 0.9
"""Time constant of the heading filter (s)."""
WALL_MARGIN_M = 0.35


def _wrap(a: float) -> float:
    return (a + math.pi) % (2 * math.pi) - math.pi


def chase_pose(
    pose: Pose2D,
    heading: float,
    *,
    bounds: tuple[float, float, float, float] | None = None,
    w: int = 640,
    h: int = 480,
    view: tuple[float, float, float] | None = None,
) -> CameraPose:
    """Chase camera for a rover at ``pose`` with the (smoothed) ``heading``; ``bounds`` = (x0, y0, x1, y1) of the
    store floor in the store frame (the eye is clamped inside the walls)."""
    back, side, eye_z = view or (BACK_M, SIDE_M, EYE_Z_M)
    c, s = math.cos(heading), math.sin(heading)
    ex = pose.x - back * c + side * s
    ey = pose.y - back * s - side * c
    if bounds is not None:
        x0, y0, x1, y1 = bounds
        ex = min(max(ex, x0 + WALL_MARGIN_M), x1 - WALL_MARGIN_M)
        ey = min(max(ey, y0 + WALL_MARGIN_M), y1 - WALL_MARGIN_M)
    tx = pose.x + LOOK_AHEAD_M * math.cos(pose.theta)
    ty = pose.y + LOOK_AHEAD_M * math.sin(pose.theta)
    dx, dy, dz = tx - ex, ty - ey, LOOK_Z_M - eye_z
    yaw = math.atan2(dy, dx)
    pitch = math.atan2(dz, max(1e-6, math.hypot(dx, dy)))
    return CameraPose(x=ex, y=ey, z=eye_z, yaw=yaw, pitch=pitch, hfov=math.radians(HFOV_DEG), w=w, h=h)


@dataclass
class _Follow:
    heading: float | None = None
    last_t: float = 0.0
    frames: int = 0


def _parse_size(text: str | None) -> tuple[int, int]:
    try:
        w, h = (int(v) for v in (text or "").lower().replace(" ", "").split("x"))
        if 160 <= w <= 1920 and 120 <= h <= 1080:
            return w, h
    except ValueError:
        pass
    return DEFAULT_SIZE


FrameSink = Callable[[str, str, CameraPose, bytes, int, int], None]
"""on_frame(camera_id, store_id, pose, jpeg, w, h): a director frame was rendered."""


@dataclass
class Director:
    robot_ids: list[str]
    out_dir: Path | None = None
    fps: float = DEFAULT_FPS
    size: tuple[int, int] = DEFAULT_SIZE
    view: tuple[float, float, float] | None = None
    _next_t: float = 0.0
    _follow: dict[str, _Follow] = field(default_factory=dict)
    _warned: set[str] = field(default_factory=set)

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> Director | None:
        env = dict(os.environ) if env is None else env
        ids = [r.strip() for r in env.get(DIRECTOR_ENV, "").split(",") if r.strip()]
        if not ids:
            return None
        if any(r.lower() in (ALL, "*") for r in ids):
            ids = [ALL]
        try:
            fps = max(0.5, min(15.0, float(env.get(DIRECTOR_FPS_ENV, "") or DEFAULT_FPS)))
        except ValueError:
            fps = DEFAULT_FPS
        raw_dir = env.get(DIRECTOR_DIR_ENV, "").strip()
        out = Path(raw_dir) if raw_dir else None
        if out is not None and ids != [ALL]:
            for rid in ids:
                (out / rid / CAMERA_SUFFIX).mkdir(parents=True, exist_ok=True)
        size = _parse_size(env.get(DIRECTOR_SIZE_ENV))
        view = None
        try:
            parts = [float(v) for v in env.get(DIRECTOR_VIEW_ENV, "").split(",") if v.strip()]
            view = (parts[0], parts[1], parts[2]) if len(parts) == 3 else None
        except ValueError:
            view = None
        log.info(
            "director camera: following %s at %.1f fps, %dx%d%s",
            ", ".join(ids),
            fps,
            size[0],
            size[1],
            f", frames + tee -> {out}" if out is not None else " (live only)",
        )
        return cls(robot_ids=ids, out_dir=out, fps=fps, size=size, view=view)

    # ---- who ---------------------------------------------------------------------------------------------------
    def follows(self, robot_id: str) -> bool:
        return self.robot_ids == [ALL] or robot_id in self.robot_ids

    def followed(self, core: Any) -> list[str]:
        """Followed robots hosted by ``core`` (sorted)."""
        rovers = getattr(core, "rovers", {})
        if self.robot_ids == [ALL]:
            return sorted(rovers)
        out = []
        for rid in self.robot_ids:
            if rid in rovers:
                out.append(rid)
            elif rid not in self._warned:
                self._warned.add(rid)
                log.warning("director camera: robot %s is not hosted here", rid)
        return out

    def camera_infos(self, core: Any, source: str = "isaac") -> list[CameraInfo]:
        """``SimHello.cameras`` entries (kind 'director') for the followed robots hosted here."""
        out = []
        for rid in self.followed(core):
            rover = core.rovers[rid]
            spec = getattr(rover, "robot", None)
            name = getattr(spec, "name", None) or rid
            out.append(
                CameraInfo(
                    camera_id=self.camera_id(rid),
                    kind="director",
                    name=f"{name} · director",
                    store_id=rover.store_id,
                    robot_id=rid,
                    mount=CameraMount(
                        frame="world", z=EYE_Z_M, pitch=-0.6, hfov_deg=HFOV_DEG, width=self.size[0], height=self.size[1]
                    ),
                    source=source,  # type: ignore[arg-type]
                )
            )
        return out

    def tee(self, msg: Any) -> None:
        """Copy a frame the worker sends (CameraFrame, or a result carrying ``frame``) for a followed robot to disk
        (only with PICKY_SIM_DIRECTOR_DIR)."""
        if self.out_dir is None:
            return
        frame = msg if isinstance(msg, CameraFrame) else getattr(msg, "frame", None)
        if not isinstance(frame, CameraFrame) or not frame.robot_id or not frame.jpeg_b64:
            return
        if not self.follows(frame.robot_id):
            return
        cam = (frame.camera_id or frame.camera or "camera").rpartition(".")[2]
        if cam == CAMERA_SUFFIX:  # director frames are written by capture()
            return
        folder = self.out_dir / frame.robot_id / cam
        try:
            folder.mkdir(parents=True, exist_ok=True)
            path = folder / f"{int(time.time() * 1000)}.jpg"
            while path.exists():  # two frames of one camera in the same ms (policy pairs): keep both
                path = path.with_name(f"{int(path.stem) + 1}.jpg")
            path.write_bytes(base64.b64decode(frame.jpeg_b64))
        except (OSError, ValueError) as exc:
            if "tee" not in self._warned:
                self._warned.add("tee")
                log.warning("director camera: cannot tee %s: %s", frame.camera_id, exc)

    @staticmethod
    def camera_id(robot_id: str) -> str:
        return f"{robot_id}.{CAMERA_SUFFIX}"

    def due(self, now: float | None = None) -> bool:
        return (time.monotonic() if now is None else now) >= self._next_t

    def _bounds(self, core: Any, store_id: str) -> tuple[float, float, float, float] | None:
        scene = getattr(core, "stores", {}).get(store_id)
        b = getattr(getattr(scene, "planogram", None), "bounds", None)
        if b is None:
            return None
        return (b.x, b.y, b.x + b.w, b.y + b.h)

    def pose_for(self, core: Any, robot_id: str) -> CameraPose:
        """The chase pose right now (current smoothed heading; snapshots / captures)."""
        rover = core.rovers[robot_id]
        f = self._follow.get(robot_id)
        heading = f.heading if f is not None and f.heading is not None else rover.pose.theta
        w, h = self.size
        return chase_pose(rover.pose, heading, bounds=self._bounds(core, rover.store_id), w=w, h=h, view=self.view)

    def wanted(self, core: Any, camera_id: str) -> bool:
        """Render it this tick? Only while the gateway streams it or frames go to disk."""
        return self.out_dir is not None or camera_id in getattr(core, "streams", {})

    def jobs(self, core: Any, now: float | None = None) -> list[tuple[str, str, CameraPose]]:
        """(camera_id, store_id, pose) per followed robot hosted here and wanted; advances the heading filters."""
        now = time.monotonic() if now is None else now
        out: list[tuple[str, str, CameraPose]] = []
        w, h = self.size
        for rid in self.followed(core):
            rover = core.rovers[rid]
            f = self._follow.setdefault(rid, _Follow())
            theta = rover.pose.theta
            if f.heading is None:
                f.heading = theta
            else:
                alpha = 1.0 - math.exp(-max(0.0, now - f.last_t) / HEADING_SMOOTH_S)
                f.heading = _wrap(f.heading + alpha * _wrap(theta - f.heading))
            f.last_t = now
            if not self.wanted(core, self.camera_id(rid)):
                continue
            cam = chase_pose(rover.pose, f.heading, bounds=self._bounds(core, rover.store_id), w=w, h=h, view=self.view)
            out.append((self.camera_id(rid), rover.store_id, cam))
        return out

    def prepare(self, backend: Any, core: Any) -> list[tuple[str, str, CameraPose]]:
        """Pose this tick's director cameras before the streams render (they then share one replicator step)."""
        jobs = self.jobs(core)
        prep = getattr(backend, "prepare", None)
        if prep is not None and jobs:
            prep(jobs)
        return jobs

    def capture(self, backend: Any, jobs: list[tuple[str, str, CameraPose]], on_frame: FrameSink | None = None) -> bool:
        """Render this tick's frames; each goes to ``on_frame`` (the live stream) and, with a folder, to disk. True
        when anything rendered."""
        self._next_t = time.monotonic() + 1.0 / self.fps
        rendered = False
        for camera_id, store_id, cam in jobs:
            try:
                out = backend.render(camera_id, store_id, cam)
            except Exception as exc:  # noqa: BLE001 - a demo camera must never break the worker
                log.warning("director camera %s failed: %s", camera_id, exc)
                continue
            rendered = True
            if out is None:
                continue
            jpeg, w, h = out
            rid = camera_id.rpartition(".")[0]
            if on_frame is not None:
                try:
                    on_frame(camera_id, store_id, cam.sized(w, h), jpeg, w, h)
                except Exception as exc:  # noqa: BLE001
                    log.warning("director camera %s: frame sink failed: %s", camera_id, exc)
            if self.out_dir is not None:
                path = self.out_dir / rid / CAMERA_SUFFIX / f"{int(time.time() * 1000)}.jpg"
                try:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    tmp = path.with_suffix(".tmp")
                    tmp.write_bytes(jpeg)
                    tmp.replace(path)
                except OSError as exc:
                    log.warning("director camera: cannot write %s: %s", path, exc)
            f = self._follow.get(rid)
            if f is not None:
                f.frames += 1
                if f.frames % 500 == 0:
                    log.info("director camera %s: %d frames", camera_id, f.frames)
        return rendered


__all__ = ["Director", "chase_pose"]
