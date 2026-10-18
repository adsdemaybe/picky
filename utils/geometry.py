"""2D geometry in the store world frame (meters, radians; x right, y up, theta CCW from +x).

Functions accept plain ``(x, y)`` tuples, :class:`Pose` tuples, or any object with ``.x``/``.y`` attributes
(e.g. ``contracts`` ``Pose2D``), so callers never have to convert.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from typing import Any, NamedTuple

TAU = 2.0 * math.pi


class Point(NamedTuple):
    x: float
    y: float


class Pose(NamedTuple):
    x: float
    y: float
    theta: float = 0.0


def xy(p: Any) -> tuple[float, float]:
    """Extract ``(x, y)`` from a tuple/list or an object with ``.x``/``.y``."""
    if hasattr(p, "x") and hasattr(p, "y"):
        return float(p.x), float(p.y)
    return float(p[0]), float(p[1])


def theta_of(p: Any) -> float:
    """Heading of a pose-like value (``.theta`` attr or 3rd tuple element); 0 for plain points."""
    if hasattr(p, "theta"):
        return float(p.theta)
    if not hasattr(p, "x") and len(p) > 2:
        return float(p[2])
    return 0.0


def wrap_angle(a: float) -> float:
    """Wrap to ``(-pi, pi]``."""
    w = math.fmod(a + math.pi, TAU)
    if w <= 0:
        w += TAU
    return w - math.pi


def angle_diff(a: float, b: float) -> float:
    """Signed smallest difference ``a - b`` in ``(-pi, pi]``."""
    return wrap_angle(a - b)


def distance(a: Any, b: Any) -> float:
    ax, ay = xy(a)
    bx, by = xy(b)
    return math.hypot(bx - ax, by - ay)


def heading(a: Any, b: Any) -> float:
    """Heading (rad, CCW from +x) of the vector from ``a`` to ``b``."""
    ax, ay = xy(a)
    bx, by = xy(b)
    return math.atan2(by - ay, bx - ax)


def lerp(a: Any, b: Any, t: float) -> Point:
    ax, ay = xy(a)
    bx, by = xy(b)
    return Point(ax + (bx - ax) * t, ay + (by - ay) * t)


def polyline_length(points: Sequence[Any]) -> float:
    return sum(distance(points[i], points[i + 1]) for i in range(len(points) - 1))


def interpolate_polyline(points: Sequence[Any], s: float) -> Pose:
    """Pose at arc length ``s`` along the polyline (clamped to its ends); theta = heading of that segment."""
    if not points:
        raise ValueError("empty polyline")
    if len(points) == 1:
        x, y = xy(points[0])
        return Pose(x, y, 0.0)
    s = max(0.0, s)
    last_theta = 0.0
    for i in range(len(points) - 1):
        a, b = points[i], points[i + 1]
        seg = distance(a, b)
        if seg <= 1e-12:
            continue
        last_theta = heading(a, b)
        if s <= seg:
            p = lerp(a, b, s / seg)
            return Pose(p.x, p.y, last_theta)
        s -= seg
    x, y = xy(points[-1])
    return Pose(x, y, last_theta)


def resample_polyline(points: Sequence[Any], step: float) -> list[Pose]:
    """Poses every ``step`` meters along the polyline, always including both endpoints."""
    if step <= 0:
        raise ValueError("step must be > 0")
    total = polyline_length(points)
    n = max(1, math.ceil(total / step))
    return [interpolate_polyline(points, total * i / n) for i in range(n + 1)]


def rotate(p: Any, theta: float, about: Any = (0.0, 0.0)) -> Point:
    px, py = xy(p)
    cx, cy = xy(about)
    c, s = math.cos(theta), math.sin(theta)
    dx, dy = px - cx, py - cy
    return Point(cx + c * dx - s * dy, cy + s * dx + c * dy)


def to_local(pose: Any, p: Any) -> Point:
    """World point -> frame of ``pose`` (x forward, y left)."""
    ox, oy = xy(pose)
    th = theta_of(pose)
    px, py = xy(p)
    c, s = math.cos(-th), math.sin(-th)
    dx, dy = px - ox, py - oy
    return Point(c * dx - s * dy, s * dx + c * dy)


def to_world(pose: Any, p: Any) -> Point:
    """Point in the frame of ``pose`` -> world."""
    ox, oy = xy(pose)
    th = theta_of(pose)
    r = rotate(p, th)
    return Point(ox + r.x, oy + r.y)


def rect_corners(cx: float, cy: float, w: float, h: float, yaw: float = 0.0) -> list[Point]:
    """Corners (CCW) of a ``w`` (along local x) by ``h`` rectangle centred at (cx, cy) rotated by ``yaw``."""
    hw, hh = w / 2.0, h / 2.0
    return [rotate((cx + dx, cy + dy), yaw, (cx, cy)) for dx, dy in ((-hw, -hh), (hw, -hh), (hw, hh), (-hw, hh))]


def point_in_rect(p: Any, cx: float, cy: float, w: float, h: float, yaw: float = 0.0, margin: float = 0.0) -> bool:
    """True if ``p`` lies inside the (optionally inflated by ``margin``) rotated rectangle."""
    lx, ly = to_local(Pose(cx, cy, yaw), p)
    return abs(lx) <= w / 2.0 + margin and abs(ly) <= h / 2.0 + margin


def bbox(points: Iterable[Any]) -> tuple[float, float, float, float]:
    """``(min_x, min_y, max_x, max_y)``."""
    pts = [xy(p) for p in points]
    if not pts:
        raise ValueError("no points")
    xs, ys = zip(*pts, strict=True)
    return min(xs), min(ys), max(xs), max(ys)
