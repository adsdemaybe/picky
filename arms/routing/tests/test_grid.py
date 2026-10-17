import math

import numpy as np
import pytest

from arms.routing.grid import (
    _dilate_disk_numpy,
    build_grid,
    inflate,
    make_grid_spec,
    nearest_free,
    rasterize,
)
from contracts.common import ROBOT_RADIUS_M


def test_spec_covers_bounds(store):
    spec = make_grid_spec(store)
    assert (spec.width, spec.height) == (280, 440)
    assert spec.resolution_m == 0.05 and spec.robot_radius_m == ROBOT_RADIUS_M
    assert spec.store_id == store.store_id


def test_rasterize_marks_fixture_centers(store):
    spec = make_grid_spec(store)
    occ = rasterize(store, spec)
    assert occ.shape == (spec.height, spec.width)
    c, r = spec.world_to_cell(3.0, 10.0)  # inside shelf A
    assert occ[r, c]
    c, r = spec.world_to_cell(4.25, 10.0)  # aisle
    assert not occ[r, c]
    # shelf A spans x 2.5..3.5 -> cell centers 2.525..3.475 -> 20 columns, 240 rows
    assert occ[:, : spec.world_to_cell(4.0, 0)[0]].sum() == 20 * 240


def test_inflation_and_outline(store):
    grid = build_grid(store)
    for slot in store.slots:  # aisles are 1.5 m: approach poses (0.75 m clearance) stay free
        assert grid.is_free(slot.approach_pose.x, slot.approach_pose.y), slot.slot_id
    assert grid.is_free(store.entrance.x, store.entrance.y)
    assert grid.is_free(store.checkout.x, store.checkout.y)
    assert not grid.is_free(3.7, 10.0)  # 0.2 m from shelf A
    assert not grid.is_free(0.3, 10.0)  # 0.3 m from the outline
    assert not grid.is_free(12.75, 17.8)  # next to the pillar
    assert grid.is_free(1.25, 10.0)


def test_numpy_dilation_matches_distance_definition():
    occ = np.zeros((41, 41), dtype=bool)
    occ[20, 20] = True
    blocked = _dilate_disk_numpy(occ, 5.0)
    rr, cc = np.mgrid[0:41, 0:41]
    expected = (rr - 20) ** 2 + (cc - 20) ** 2 < 25
    assert np.array_equal(blocked, expected)
    blocked = _dilate_disk_numpy(occ, 4.5)
    assert np.array_equal(blocked, (rr - 20) ** 2 + (cc - 20) ** 2 < 20.25)


def test_scipy_and_numpy_inflation_agree(store):
    pytest.importorskip("scipy")
    import arms.routing.grid as g

    spec = make_grid_spec(store)
    occ = rasterize(store, spec)
    with_scipy = inflate(occ, ROBOT_RADIUS_M, spec.resolution_m)
    numpy_only = ~g._dilate_disk_numpy(occ, ROBOT_RADIUS_M / spec.resolution_m) & g._border_clearance(
        occ.shape, ROBOT_RADIUS_M / spec.resolution_m
    )
    assert np.array_equal(with_scipy, numpy_only)


def test_nearest_free(store):
    grid = build_grid(store)
    col, row = grid.spec.world_to_cell(3.0, 10.0)  # inside a shelf
    found = nearest_free(grid, col, row)
    assert found is not None
    x, _ = grid.spec.cell_to_world(*found)
    assert grid.free[found[1], found[0]]
    # occupied centers of shelf A span 2.525..3.475 -> first free columns are 0.6 m beyond them
    assert any(math.isclose(x, v, abs_tol=1e-6) for v in (2.525 - ROBOT_RADIUS_M, 3.475 + ROBOT_RADIUS_M))
