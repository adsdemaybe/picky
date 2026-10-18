"""Dev-only third-person "director" (chase) camera for demo videos. Off unless ``PICKY_SIM_DIRECTOR`` is set.

    PICKY_SIM_DIRECTOR=rover-ave-1[,rover-ave-2]   robots to follow
    PICKY_SIM_DIRECTOR_DIR=/data/.../director       frames land in <dir>/<robot_id>/director/<epoch_ms>.jpg
    PICKY_SIM_DIRECTOR_FPS=4                         frames per second per robot

It also TEES every frame the worker already renders for a followed robot (scan / capture results, stream frames,
the T1 policy pictures taken while the arm moves) into ``<dir>/<robot_id>/<camera>/<epoch_ms>.jpg``, so a video can
show the live cameras without asking the gateway for extra streams (each extra stream is another render product in
every replicator step: on a shared GPU that slows the whole simulation down).

The camera rides behind, to the right of and above the rover (a smoothed heading, so turns don't whip it around) and
looks at a point just ahead of the rover at arm height, so the frame shows the rover, its SO-101 and the shelf it
faces. The frames are written to the worker's disk for ``arms/sim/tools/director`` (the video compositor); they never
go over ``/ws/sim``, so nothing in ``contracts`` changes and the gateway never sees them. Isaac store scenes are
synthetic renders with no people in them (nothing to blur).

Cost: one extra render product. A frame due in the same loop tick as a stream frame shares its replicator step
(``Director.prepare`` poses the camera before the streams render).
"""

from __future__ import annotations

import base64
import math
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from arms.sim.geometry import CameraPose
from contracts.common import Pose2D
from contracts.sim import CameraFrame
from utils.log import get_logger

log = get_logger(__name__)

DIRECTOR_ENV = "PICKY_SIM_DIRECTOR"
DIRECTOR_DIR_ENV = "PICKY_SIM_DIRECTOR_DIR"
DIRECTOR_FPS_ENV = "PICKY_SIM_DIRECTOR_FPS"
CAMERA_SUFFIX = "director"
BACK_M = 1.55
"""Eye distance behind the rover (along its smoothed heading)."""
SIDE_M = 0.75
"""Eye offset to the rover's right (a 3/4 view: the arm and the shelf it faces are both in frame)."""
EYE_Z_M = 2.35
"""Above the 1.8 m gondolas, below the 3.0 m ceiling."""
LOOK_AHEAD_M = 0.45
LOOK_Z_M = 0.65
HFOV_DEG = 78.0
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
) -> CameraPose:
    """Chase camera for a rover at ``pose`` with the (smoothed) ``heading``; ``bounds`` = (x0, y0, x1, y1) of the
    store floor in the store frame (the eye is clamped inside the walls)."""
    c, s = math.cos(heading), math.sin(heading)
    ex = pose.x - BACK_M * c + SIDE_M * s
    ey = pose.y - BACK_M * s - SIDE_M * c
    if bounds is not None:
        x0, y0, x1, y1 = bounds
        ex = min(max(ex, x0 + WALL_MARGIN_M), x1 - WALL_MARGIN_M)
        ey = min(max(ey, y0 + WALL_MARGIN_M), y1 - WALL_MARGIN_M)
    tx = pose.x + LOOK_AHEAD_M * math.cos(pose.theta)
    ty = pose.y + LOOK_AHEAD_M * math.sin(pose.theta)
    dx, dy, dz = tx - ex, ty - ey, LOOK_Z_M - EYE_Z_M
    yaw = math.atan2(dy, dx)
    pitch = math.atan2(dz, max(1e-6, math.hypot(dx, dy)))
    return CameraPose(x=ex, y=ey, z=EYE_Z_M, yaw=yaw, pitch=pitch, hfov=math.radians(HFOV_DEG), w=w, h=h)


@dataclass
class _Follow:
    heading: float | None = None
    last_t: float = 0.0
    frames: int = 0


@dataclass
class Director:
    robot_ids: list[str]
    out_dir: Path
    fps: float = 4.0
    _next_t: float = 0.0
    _follow: dict[str, _Follow] = field(default_factory=dict)
    _warned: set[str] = field(default_factory=set)

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> Director | None:
        env = dict(os.environ) if env is None else env
        ids = [r.strip() for r in env.get(DIRECTOR_ENV, "").split(",") if r.strip()]
        if not ids:
            return None
        try:
            fps = max(0.5, min(15.0, float(env.get(DIRECTOR_FPS_ENV, "") or 4.0)))
        except ValueError:
            fps = 4.0
        out = Path(env.get(DIRECTOR_DIR_ENV, "") or "director")
        for rid in ids:
            (out / rid / CAMERA_SUFFIX).mkdir(parents=True, exist_ok=True)
        log.info("director camera: following %s at %.1f fps -> %s", ", ".join(ids), fps, out)
        return cls(robot_ids=ids, out_dir=out, fps=fps)

    def tee(self, msg: Any) -> None:
        """Copy a frame the worker sends (CameraFrame, or a result carrying ``frame``) for a followed robot to disk."""
        frame = msg if isinstance(msg, CameraFrame) else getattr(msg, "frame", None)
        if not isinstance(frame, CameraFrame) or frame.robot_id not in self.robot_ids or not frame.jpeg_b64:
            return
        cam = (frame.camera_id or frame.camera or "camera").rpartition(".")[2]
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

    def jobs(self, core: Any, now: float | None = None) -> list[tuple[str, str, CameraPose]]:
        """(camera_id, store_id, pose) per followed robot hosted here; advances the heading filters."""
        now = time.monotonic() if now is None else now
        out: list[tuple[str, str, CameraPose]] = []
        for rid in self.robot_ids:
            rover = getattr(core, "rovers", {}).get(rid)
            if rover is None:
                if rid not in self._warned:
                    self._warned.add(rid)
                    log.warning("director camera: robot %s is not hosted here", rid)
                continue
            f = self._follow.setdefault(rid, _Follow())
            theta = rover.pose.theta
            if f.heading is None:
                f.heading = theta
            else:
                alpha = 1.0 - math.exp(-max(0.0, now - f.last_t) / HEADING_SMOOTH_S)
                f.heading = _wrap(f.heading + alpha * _wrap(theta - f.heading))
            f.last_t = now
            cam = chase_pose(rover.pose, f.heading, bounds=self._bounds(core, rover.store_id))
            out.append((self.camera_id(rid), rover.store_id, cam))
        return out

    def prepare(self, backend: Any, core: Any) -> list[tuple[str, str, CameraPose]]:
        """Pose this tick's director cameras before the streams render (they then share one replicator step)."""
        jobs = self.jobs(core)
        prep = getattr(backend, "prepare", None)
        if prep is not None and jobs:
            prep(jobs)
        return jobs

    def capture(self, backend: Any, jobs: list[tuple[str, str, CameraPose]]) -> bool:
        """Render + write this tick's frames. True when anything rendered."""
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
            jpeg = out[0]
            rid = camera_id.rpartition(".")[0]
            path = self.out_dir / rid / CAMERA_SUFFIX / f"{int(time.time() * 1000)}.jpg"
            try:
                tmp = path.with_suffix(".tmp")
                tmp.write_bytes(jpeg)
                tmp.replace(path)
            except OSError as exc:
                log.warning("director camera: cannot write %s: %s", path, exc)
                continue
            f = self._follow.get(rid)
            if f is not None:
                f.frames += 1
                if f.frames % 200 == 0:
                    log.info("director camera %s: %d frames", camera_id, f.frames)
        return rendered


__all__ = ["Director", "chase_pose"]
