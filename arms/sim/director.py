"""Director camera for the demo video (PURE: no Isaac, no I/O): a smoothed third-person view of one rover.

Not a wire camera (contracts camera ids are unchanged): the worker's recorder (``app.py --record-control``) renders
it straight through the backend and writes the frames to disk (arms/sim/tools/demo_video assembles the video).

Shots (``Shot.kind``):
- ``chase``: behind and above the rover while it drives; while it works (scan / pick / place) the camera moves in over
  its left shoulder (the basket side the arm swings to) and lower; while it unloads at the checkout it pulls out to a
  side view of the counter. Eye and target follow with a lag (exponential smoothing), so mode changes and turns glide.
- ``orbit``: a slow circle around a point (intro / outro).
- ``path``: keyframed (t, eye, target) moves, eased (crane / dolly shots).

The eye never sits inside a fixture: over a fixture footprint it rises above the fixture's top, and it stays inside
the store walls and below the ceiling lights.
"""

import math
from collections.abc import Sequence
from dataclasses import dataclass, field

from arms.sim.geometry import CameraPose, look_at
from contracts.common import Pose2D
from contracts.planogram import Fixture
from contracts.sim import RobotStatus

Vec3 = tuple[float, float, float]

CHASE_BACK_M = 2.3
CHASE_UP_M = 2.15
CHASE_LOOK_AHEAD_M = 0.7
WORK_BACK_M = 1.15
WORK_SIDE_M = 0.8
WORK_UP_M = 1.95
UNLOAD_BACK_M = 1.5
UNLOAD_SIDE_M = 1.7
UNLOAD_UP_M = 1.75
EYE_LAG_S = 0.9
TARGET_LAG_S = 0.4
FIXTURE_CLEAR_M = 0.3
"""Over a fixture footprint the eye stays this far above its top."""
WALL_INSET_M = 0.25
EYE_MAX_Z_M = 3.05
DIRECTOR_HFOV_DEG = 62.0

WORK_STATUSES = {RobotStatus.SCANNING, RobotStatus.PICKING, RobotStatus.PLACING}


@dataclass
class Shot:
    kind: str = "chase"
    center: Vec3 = (0.0, 0.0, 0.6)
    radius: float = 3.0
    height: float = 2.2
    period_s: float = 24.0
    start_deg: float = 0.0
    keys: list[tuple[float, Vec3, Vec3]] = field(default_factory=list)
    """``path``: (t seconds from the shot start, eye, target), sorted by t."""

    @classmethod
    def from_dict(cls, d: dict | None) -> "Shot":
        d = dict(d or {})
        keys = [(float(k[0]), tuple(k[1]), tuple(k[2])) for k in d.pop("keys", [])]
        known = {k: d[k] for k in ("kind", "radius", "height", "period_s", "start_deg") if k in d}
        if "center" in d:
            known["center"] = tuple(d["center"])  # type: ignore[assignment]
        return cls(keys=keys, **known)  # type: ignore[arg-type]


@dataclass
class World:
    """What the director needs from the store: walls + fixture footprints (store frame)."""

    x0: float
    y0: float
    x1: float
    y1: float
    fixtures: list[tuple[float, float, float, float, float]] = field(default_factory=list)
    """(x0, y0, x1, y1, height_m) per fixture."""

    @classmethod
    def from_planogram(cls, bounds: object, fixtures: Sequence[Fixture]) -> "World":
        b = bounds
        return cls(
            x0=b.x,  # type: ignore[attr-defined]
            y0=b.y,  # type: ignore[attr-defined]
            x1=b.x + b.w,  # type: ignore[attr-defined]
            y1=b.y + b.h,  # type: ignore[attr-defined]
            fixtures=[(f.rect.x, f.rect.y, f.rect.x_max, f.rect.y_max, f.height_m) for f in fixtures],
        )

    def safe_eye(self, eye: Vec3) -> Vec3:
        x = min(self.x1 - WALL_INSET_M, max(self.x0 + WALL_INSET_M, eye[0]))
        y = min(self.y1 - WALL_INSET_M, max(self.y0 + WALL_INSET_M, eye[1]))
        z = eye[2]
        pad = 0.15
        for fx0, fy0, fx1, fy1, h in self.fixtures:
            if fx0 - pad <= x <= fx1 + pad and fy0 - pad <= y <= fy1 + pad:
                z = max(z, h + FIXTURE_CLEAR_M)
        return (x, y, min(EYE_MAX_Z_M, z))


def _smooth_toward(cur: Vec3 | None, goal: Vec3, dt: float, lag_s: float) -> Vec3:
    if cur is None or lag_s <= 0:
        return goal
    k = 1.0 - math.exp(-max(0.0, dt) / lag_s)
    return (cur[0] + (goal[0] - cur[0]) * k, cur[1] + (goal[1] - cur[1]) * k, cur[2] + (goal[2] - cur[2]) * k)


def _ease(t: float) -> float:
    t = max(0.0, min(1.0, t))
    return t * t * t * (t * (6 * t - 15) + 10)


@dataclass
class Director:
    """One director camera; ``update`` once per recorded frame (sim dt) returns its CameraPose."""

    world: World
    w: int = 1920
    h: int = 1080
    hfov_deg: float = DIRECTOR_HFOV_DEG
    shot: Shot = field(default_factory=Shot)
    eye: Vec3 | None = None
    target: Vec3 | None = None
    shot_t: float = 0.0

    def set_shot(self, shot: Shot, cut: bool = False) -> None:
        """Switch shots (``cut`` = jump, else the chase lag blends into it)."""
        self.shot, self.shot_t = shot, 0.0
        if cut:
            self.eye = self.target = None

    def chase_goal(self, pose: Pose2D, status: RobotStatus, lift_m: float) -> tuple[Vec3, Vec3]:
        c, s = math.cos(pose.theta), math.sin(pose.theta)
        left = (-s, c)
        if status in WORK_STATUSES:
            back, side, up = WORK_BACK_M, WORK_SIDE_M, WORK_UP_M
            tz = max(0.55, min(1.2, lift_m * 0.85))
            ahead = 0.3
        elif status == "unloading":
            back, side, up = UNLOAD_BACK_M, UNLOAD_SIDE_M, UNLOAD_UP_M
            tz, ahead = 0.8, 0.45
        else:
            back, side, up = CHASE_BACK_M, 0.0, CHASE_UP_M
            tz, ahead = 0.6, CHASE_LOOK_AHEAD_M
        eye = (pose.x - c * back + left[0] * side, pose.y - s * back + left[1] * side, up)
        target = (pose.x + c * ahead, pose.y + s * ahead, tz)
        return eye, target

    def update(
        self, dt: float, pose: Pose2D | None = None, status: RobotStatus | str = RobotStatus.IDLE, lift_m: float = 0.55
    ) -> CameraPose:
        self.shot_t += dt
        shot = self.shot
        if shot.kind == "orbit":
            a = math.radians(shot.start_deg) + 2 * math.pi * self.shot_t / max(1.0, shot.period_s)
            cx, cy, cz = shot.center
            goal_eye = (cx + shot.radius * math.cos(a), cy + shot.radius * math.sin(a), shot.height)
            goal_target: Vec3 = (cx, cy, cz)
            eye_lag, target_lag = 0.0, 0.0
        elif shot.kind == "path" and shot.keys:
            goal_eye, goal_target = self._path_at(self.shot_t)
            eye_lag, target_lag = 0.0, 0.0
        else:
            if pose is None:
                pose = Pose2D(x=(self.world.x0 + self.world.x1) / 2, y=(self.world.y0 + self.world.y1) / 2, theta=0.0)
            goal_eye, goal_target = self.chase_goal(pose, status, lift_m)  # type: ignore[arg-type]
            eye_lag, target_lag = EYE_LAG_S, TARGET_LAG_S
        goal_eye = self.world.safe_eye(goal_eye)
        self.eye = self.world.safe_eye(_smooth_toward(self.eye, goal_eye, dt, eye_lag))
        self.target = _smooth_toward(self.target, goal_target, dt, target_lag)
        return look_at(self.eye, self.target, hfov=math.radians(self.hfov_deg), w=self.w, h=self.h)

    def _path_at(self, t: float) -> tuple[Vec3, Vec3]:
        keys = self.shot.keys
        if t <= keys[0][0]:
            return keys[0][1], keys[0][2]
        for (t0, e0, g0), (t1, e1, g1) in zip(keys, keys[1:], strict=False):
            if t0 <= t <= t1:
                u = _ease((t - t0) / max(1e-6, t1 - t0))
                return (
                    tuple(a + (b - a) * u for a, b in zip(e0, e1, strict=True)),  # type: ignore[return-value]
                    tuple(a + (b - a) * u for a, b in zip(g0, g1, strict=True)),
                )
        return keys[-1][1], keys[-1][2]
