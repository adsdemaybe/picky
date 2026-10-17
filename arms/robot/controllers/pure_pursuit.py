"""Pure-pursuit waypoint follower. PURE Python (math + dataclasses only) so the same file runs in the backend (robot
fake) and inside Isaac Sim's Python (arms/robot/isaac drives the Ridgeback base with it).

World frame: x right, y up, theta CCW from +x (radians). Poses are anything with ``.x .y [.theta]`` or tuples
``(x, y[, theta])``; outputs are plain ``(x, y, theta)`` tuples.

Contract with NavigateCmd / RobotService.navigate:
- intermediate waypoint thetas are ignored (the base faces its travel direction while driving);
- the final waypoint's position must be reached within ``goal_tolerance_m`` and its theta within ``yaw_tolerance_rad``
  (phase ROTATE turns in place after arriving).

Usage::

    pp = PurePursuit(waypoints, PurePursuitConfig(max_speed_mps=0.8))
    while not pp.done:
        pose = pp.step(pose, dt)          # kinematic integration (fake / Isaac set_world_pose)
    # or: cmd = pp.compute(pose); apply cmd.vx, cmd.vy, cmd.wz yourself, dt later
"""

import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

PoseT = tuple[float, float, float]

TRACK = "track"
ROTATE = "rotate"
DONE = "done"


def _xy(p: Any) -> tuple[float, float]:
    if hasattr(p, "x"):
        return float(p.x), float(p.y)
    return float(p[0]), float(p[1])


def _pose(p: Any) -> PoseT:
    if hasattr(p, "x"):
        return float(p.x), float(p.y), float(getattr(p, "theta", 0.0) or 0.0)
    return float(p[0]), float(p[1]), float(p[2]) if len(p) > 2 else 0.0


def wrap_angle(a: float) -> float:
    """Wrap to (-pi, pi]."""
    w = math.atan2(math.sin(a), math.cos(a))
    return math.pi if w == -math.pi else w


def _clamp(v: float, lo: float, hi: float) -> float:
    return lo if v < lo else hi if v > hi else v


@dataclass
class PurePursuitConfig:
    max_speed_mps: float = 0.8
    min_speed_mps: float = 0.08
    lookahead_m: float = 0.5
    max_yaw_rate: float = 1.5
    """rad/s."""
    yaw_gain: float = 3.0
    """P gain on heading error (rad/s per rad)."""
    slow_radius_m: float = 0.6
    """Linear slow-down within this distance (along the path) of the goal."""
    goal_tolerance_m: float = 0.05
    yaw_tolerance_rad: float = 0.05
    holonomic: bool = True
    """Ridgeback is holonomic: drive straight at the lookahead point and turn independently. False = diff-drive
    (classic curvature law, rotate in place when the target is behind)."""
    face_travel: bool = True
    """Holonomic only: turn to face the direction of travel while driving (looks natural on camera)."""


@dataclass
class Command:
    """Velocity command in the WORLD frame (vx, vy m/s) + yaw rate (rad/s)."""

    vx: float = 0.0
    vy: float = 0.0
    wz: float = 0.0

    @property
    def speed(self) -> float:
        return math.hypot(self.vx, self.vy)


class PurePursuit:
    """Stateful follower for one path. Progress along the path is monotonic (never snaps back to earlier segments)."""

    def __init__(self, waypoints: Sequence[Any], config: PurePursuitConfig | None = None) -> None:
        if not waypoints:
            raise ValueError("pure pursuit needs at least one waypoint")
        self.cfg = config or PurePursuitConfig()
        pts = [_xy(w) for w in waypoints]
        # drop consecutive duplicates (zero-length segments)
        self.points: list[tuple[float, float]] = [pts[0]]
        for p in pts[1:]:
            if math.hypot(p[0] - self.points[-1][0], p[1] - self.points[-1][1]) > 1e-9:
                self.points.append(p)
        self.goal: PoseT = _pose(waypoints[-1])
        self._cum = [0.0]
        for a, b in zip(self.points, self.points[1:], strict=False):
            self._cum.append(self._cum[-1] + math.hypot(b[0] - a[0], b[1] - a[1]))
        self.length = self._cum[-1]
        self._seg = 0
        self._s = 0.0
        self.phase = TRACK

    # ---------------------------------------------------------------------------------------------------------------
    @property
    def done(self) -> bool:
        return self.phase == DONE

    def remaining_m(self, pose: Any) -> float:
        """Distance still to drive along the path (from the projection of ``pose``)."""
        self._project(pose)
        x, y = _xy(pose)
        gx, gy = self.goal[0], self.goal[1]
        return max(self.length - self._s, 0.0) if self.length > 0 else math.hypot(gx - x, gy - y)

    def _project(self, pose: Any) -> None:
        """Advance (monotonically) the closest-point projection of ``pose`` onto the path."""
        if len(self.points) < 2:
            return
        x, y = _xy(pose)
        best_s, best_d, best_seg = self._s, math.inf, self._seg
        # search the current segment and a few ahead
        for i in range(self._seg, min(self._seg + 4, len(self.points) - 1)):
            ax, ay = self.points[i]
            bx, by = self.points[i + 1]
            dx, dy = bx - ax, by - ay
            seg_len2 = dx * dx + dy * dy
            t = _clamp(((x - ax) * dx + (y - ay) * dy) / seg_len2, 0.0, 1.0) if seg_len2 > 0 else 0.0
            px, py = ax + t * dx, ay + t * dy
            d = math.hypot(x - px, y - py)
            s = self._cum[i] + t * math.sqrt(seg_len2)
            if d < best_d - 1e-9 and s >= self._s - 1e-9:
                best_d, best_s, best_seg = d, s, i
        self._s, self._seg = best_s, best_seg

    def _point_at(self, s: float) -> tuple[float, float]:
        if len(self.points) < 2:
            return self.points[0]
        s = _clamp(s, 0.0, self.length)
        i = self._seg
        while i < len(self.points) - 2 and self._cum[i + 1] < s:
            i += 1
        seg_len = self._cum[i + 1] - self._cum[i]
        t = (s - self._cum[i]) / seg_len if seg_len > 0 else 1.0
        ax, ay = self.points[i]
        bx, by = self.points[i + 1]
        return ax + t * (bx - ax), ay + t * (by - ay)

    # ---------------------------------------------------------------------------------------------------------------
    def compute(self, pose: Any) -> Command:
        """Velocity command for the current pose (also advances the phase TRACK -> ROTATE -> DONE)."""
        cfg = self.cfg
        x, y, th = _pose(pose)
        gx, gy, gth = self.goal
        if self.phase == TRACK:
            dist_goal = math.hypot(gx - x, gy - y)
            remaining = max(self.remaining_m(pose), dist_goal)
            if dist_goal <= cfg.goal_tolerance_m:
                self.phase = ROTATE
            else:
                tx, ty = self._point_at(self._s + cfg.lookahead_m)
                if math.hypot(tx - x, ty - y) < 1e-6:
                    tx, ty = gx, gy
                speed = cfg.max_speed_mps * min(1.0, remaining / cfg.slow_radius_m) if cfg.slow_radius_m > 0 else 0.0
                speed = _clamp(speed, cfg.min_speed_mps, cfg.max_speed_mps)
                bearing = math.atan2(ty - y, tx - x)
                if cfg.holonomic:
                    wz = 0.0
                    if cfg.face_travel and dist_goal > cfg.slow_radius_m:
                        wz = _clamp(cfg.yaw_gain * wrap_angle(bearing - th), -cfg.max_yaw_rate, cfg.max_yaw_rate)
                    return Command(speed * math.cos(bearing), speed * math.sin(bearing), wz)
                # differential drive: curvature law in the robot frame
                alpha = wrap_angle(bearing - th)
                if abs(alpha) > math.pi / 2:
                    return Command(0.0, 0.0, math.copysign(cfg.max_yaw_rate, alpha))
                ld = max(math.hypot(tx - x, ty - y), 1e-6)
                wz = _clamp(speed * 2.0 * math.sin(alpha) / ld, -cfg.max_yaw_rate, cfg.max_yaw_rate)
                return Command(speed * math.cos(th), speed * math.sin(th), wz)
        if self.phase == ROTATE:
            err = wrap_angle(gth - th)
            if abs(err) <= cfg.yaw_tolerance_rad:
                self.phase = DONE
                return Command()
            wz = _clamp(cfg.yaw_gain * err, -cfg.max_yaw_rate, cfg.max_yaw_rate)
            if abs(wz) < 0.1:
                wz = math.copysign(0.1, err)
            return Command(0.0, 0.0, wz)
        return Command()

    def step(self, pose: Any, dt: float) -> PoseT:
        """Integrate one kinematic step of ``dt`` seconds; never overshoots the goal position or goal yaw."""
        x, y, th = _pose(pose)
        cmd = self.compute((x, y, th))
        gx, gy, gth = self.goal
        if self.phase == TRACK:
            nx, ny = x + cmd.vx * dt, y + cmd.vy * dt
            # final approach: if this step would reach/pass the goal, land on it
            if math.hypot(gx - x, gy - y) <= cmd.speed * dt + 1e-9:
                nx, ny = gx, gy
            nth = wrap_angle(th + cmd.wz * dt)
            return nx, ny, nth
        if self.phase == ROTATE:
            err = wrap_angle(gth - th)
            turn = cmd.wz * dt
            if abs(turn) >= abs(err):
                return gx, gy, gth
            return x, y, wrap_angle(th + turn)
        return x, y, th


def simulate(
    waypoints: Sequence[Any],
    start: Any,
    config: PurePursuitConfig | None = None,
    dt: float = 0.05,
    max_time_s: float = 600.0,
) -> list[PoseT]:
    """Run the follower kinematically from ``start`` until DONE (or ``max_time_s``); returns the pose trace."""
    pp = PurePursuit(waypoints, config)
    pose = _pose(start)
    trace = [pose]
    t = 0.0
    while not pp.done and t < max_time_s:
        pose = pp.step(pose, dt)
        trace.append(pose)
        t += dt
    return trace


__all__ = ["DONE", "ROTATE", "TRACK", "Command", "PurePursuit", "PurePursuitConfig", "simulate", "wrap_angle"]
