import math

import pytest

from utils.geometry import (
    Pose,
    angle_diff,
    bbox,
    distance,
    heading,
    interpolate_polyline,
    point_in_rect,
    polyline_length,
    rect_corners,
    resample_polyline,
    to_local,
    to_world,
    wrap_angle,
)


class Obj:
    def __init__(self, x, y, theta=0.0):
        self.x, self.y, self.theta = x, y, theta


def test_basics():
    assert distance((0, 0), (3, 4)) == 5
    assert distance(Obj(0, 0), Pose(3, 4, 1.0)) == 5
    assert heading((0, 0), (0, 1)) == pytest.approx(math.pi / 2)
    assert wrap_angle(3 * math.pi) == pytest.approx(math.pi)
    assert wrap_angle(-math.pi) == pytest.approx(math.pi)
    assert angle_diff(0.1, 2 * math.pi - 0.1) == pytest.approx(0.2)


def test_polyline():
    pts = [(0, 0), (2, 0), (2, 2)]
    assert polyline_length(pts) == 4
    p = interpolate_polyline(pts, 3)
    assert (p.x, p.y) == pytest.approx((2, 1)) and p.theta == pytest.approx(math.pi / 2)
    assert interpolate_polyline(pts, 99)[:2] == (2, 2)
    assert interpolate_polyline(pts, -1)[:2] == (0, 0)
    rs = resample_polyline(pts, 1.0)
    assert len(rs) == 5 and rs[-1][:2] == pytest.approx((2, 2))


def test_frames_roundtrip():
    pose = Pose(1, 2, math.pi / 2)
    local = to_local(pose, (1, 3))
    assert local == pytest.approx((1, 0))
    assert to_world(pose, local) == pytest.approx((1, 3))
    assert to_local((1, 2, math.pi / 2), (1, 3)) == pytest.approx((1, 0))


def test_rects():
    assert point_in_rect((0.9, 0), 0, 0, 2, 1)
    assert not point_in_rect((0, 0.9), 0, 0, 2, 1)
    assert point_in_rect((0, 0.9), 0, 0, 2, 1, yaw=math.pi / 2)
    assert point_in_rect((1.1, 0), 0, 0, 2, 1, margin=0.2)
    assert bbox(rect_corners(0, 0, 2, 1, math.pi / 2)) == pytest.approx((-0.5, -1, 0.5, 1))
