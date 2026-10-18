"""Kinematic pure-pursuit follower for the rover (differential drive, set-pose in the sim; no physics controller).

Pure python and deterministic: ``PurePursuit.step(pose, dt)`` returns the next pose. The path is the robot's start
position followed by the ``NavigateCmd`` waypoints (intermediate thetas ignored, final theta reached in place).

Control law: find the closest point on the polyline (progress never goes backwards), walk ``lookahead_m`` ahead along
the path, steer with curvature ``k = 2 sin(alpha) / Ld`` toward it (alpha = bearing error, Ld = distance to the
lookahead point). Large bearing errors (> ``rotate_in_place_rad``) turn in place first (the rover is a diff drive).
Speed ramps down near the goal; within ``tolerance_m`` of the final waypoint the rover stops and turns to the final
theta at ``max_yaw_rate``. Routing supplies collision-free waypoints around fixtures; lookahead corner-cutting stays
well inside the 0.3 m inflation for a 0.45 m lookahead at aisle corners.
"""

import math
from dataclasses import dataclass

from contracts.common import ROBOT_MAX_SPEED_MPS, Pose2D, angle_diff, normalize_angle


@dataclass(frozen=True)
class PurePursuitParams:
    lookahead_m: float = 0.45
    max_speed_mps: float = ROBOT_MAX_SPEED_MPS
    min_speed_mps: float = 0.08
    max_yaw_rate: float = 2.0
    rotate_in_place_rad: float = 1.0
    slow_radius_m: float = 0.6


class PurePursuit:
    """Follows ``waypoints`` from ``start``. ``done`` once the final position is within ``tolerance_m`` and the final
    theta within ``yaw_tolerance_rad``."""

    def __init__(
        self,
        start: Pose2D,
        waypoints: list[Pose2D],
        *,
        speed_mps: float | None = None,
        tolerance_m: float = 0.05,
        yaw_tolerance_rad: float = 0.05,
        params: PurePursuitParams | None = None,
    ) -> None:
        if not waypoints:
            raise ValueError("navigate needs at least one waypoint")
        self.params = params or PurePursuitParams()
        self.speed = min(speed_mps or self.params.max_speed_mps, self.params.max_speed_mps)
        self.tolerance = max(1e-3, tolerance_m)
        self.yaw_tolerance = max(1e-3, yaw_tolerance_rad)
        self.final = waypoints[-1]
        pts = [(start.x, start.y)] + [(w.x, w.y) for w in waypoints]
        # drop zero-length segments
        self.path: list[tuple[float, float]] = [pts[0]]
        for p in pts[1:]:
            if math.hypot(p[0] - self.path[-1][0], p[1] - self.path[-1][1]) > 1e-6:
                self.path.append(p)
        self.seg = 0
        self.aligning = False
        self.done = False

    @property
    def length_m(self) -> float:
        return sum(math.hypot(b[0] - a[0], b[1] - a[1]) for a, b in zip(self.path, self.path[1:], strict=False))

    def remaining_m(self, pose: Pose2D) -> float:
        _, seg, t = self._project(pose)
        if len(self.path) < 2:
            return 0.0
        a, b = self.path[seg], self.path[seg + 1]
        rem = math.hypot(b[0] - a[0], b[1] - a[1]) * (1 - t)
        for p, q in zip(self.path[seg + 1 :], self.path[seg + 2 :], strict=False):
            rem += math.hypot(q[0] - p[0], q[1] - p[1])
        return rem

    def _project(self, pose: Pose2D) -> tuple[tuple[float, float], int, float]:
        """Closest point on the path at or after the current segment: (point, segment index, t in [0, 1])."""
        if len(self.path) < 2:
            return self.path[0], 0, 1.0
        best: tuple[float, tuple[float, float], int, float] | None = None
        # look at the current segment and the next two (progress is monotonic)
        for i in range(self.seg, min(self.seg + 3, len(self.path) - 1)):
            a, b = self.path[i], self.path[i + 1]
            dx, dy = b[0] - a[0], b[1] - a[1]
            ll = dx * dx + dy * dy
            t = 0.0 if ll == 0 else max(0.0, min(1.0, ((pose.x - a[0]) * dx + (pose.y - a[1]) * dy) / ll))
            p = (a[0] + t * dx, a[1] + t * dy)
            d = math.hypot(pose.x - p[0], pose.y - p[1])
            if best is None or d < best[0] - 1e-9:
                best = (d, p, i, t)
        assert best is not None
        return best[1], best[2], best[3]

    def _lookahead(self, start: tuple[float, float], seg: int) -> tuple[float, float]:
        remaining = self.params.lookahead_m
        p = start
        for i in range(seg, len(self.path) - 1):
            b = self.path[i + 1]
            d = math.hypot(b[0] - p[0], b[1] - p[1])
            if d >= remaining:
                return (p[0] + (b[0] - p[0]) * remaining / d, p[1] + (b[1] - p[1]) * remaining / d)
            remaining -= d
            p = b
        return self.path[-1]

    def step(self, pose: Pose2D, dt: float) -> Pose2D:
        """Advance ``dt`` seconds from ``pose``; sets ``done`` when the goal pose is reached."""
        if self.done or dt <= 0:
            return pose
        p = self.params
        gx, gy = self.final.x, self.final.y
        dist_goal = math.hypot(gx - pose.x, gy - pose.y)
        if self.aligning or dist_goal <= self.tolerance:
            self.aligning = True
            err = angle_diff(self.final.theta, pose.theta)
            if abs(err) <= self.yaw_tolerance:
                self.done = True
                return pose
            turn = max(-p.max_yaw_rate * dt, min(p.max_yaw_rate * dt, err))
            new = Pose2D(x=pose.x, y=pose.y, theta=normalize_angle(pose.theta + turn))
            if abs(angle_diff(self.final.theta, new.theta)) <= self.yaw_tolerance:
                self.done = True
            return new

        proj, seg, _ = self._project(pose)
        self.seg = seg
        target = self._lookahead(proj, seg)
        if math.hypot(target[0] - gx, target[1] - gy) < 1e-9 or dist_goal < p.lookahead_m:
            target = (gx, gy)
        tx, ty = target[0] - pose.x, target[1] - pose.y
        ld = math.hypot(tx, ty)
        if ld < 1e-9:
            self.aligning = True
            return pose
        alpha = angle_diff(math.atan2(ty, tx), pose.theta)
        if abs(alpha) > p.rotate_in_place_rad:
            turn = max(-p.max_yaw_rate * dt, min(p.max_yaw_rate * dt, alpha))
            return Pose2D(x=pose.x, y=pose.y, theta=normalize_angle(pose.theta + turn))

        v = self.speed
        if dist_goal < p.slow_radius_m:
            v = max(p.min_speed_mps, min(v, v * dist_goal / p.slow_radius_m + p.min_speed_mps))
        curvature = 2.0 * math.sin(alpha) / ld
        omega = v * curvature
        if abs(omega) > p.max_yaw_rate:
            omega = math.copysign(p.max_yaw_rate, omega)
            v = abs(omega / curvature) if curvature else v
        step = v * dt
        if target == (gx, gy) and step >= dist_goal and abs(alpha) < 0.3:
            # last stretch: snap onto the goal position (kinematic), keep the heading
            return Pose2D(x=gx, y=gy, theta=normalize_angle(pose.theta + omega * dt))
        theta_mid = pose.theta + omega * dt / 2
        return Pose2D(
            x=pose.x + step * math.cos(theta_mid),
            y=pose.y + step * math.sin(theta_mid),
            theta=normalize_angle(pose.theta + omega * dt),
        )
