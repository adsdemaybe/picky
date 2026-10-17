"""Planogram -> occupancy grid (pure numpy). Used by the real service and by anyone drawing the walkable area.

Conventions (contracts.routing.GridSpec): arrays are indexed ``grid[row, col]`` = ``grid[y, x]``; cell (0, 0) has its
lower-left corner at ``spec.origin``. ``occupied`` = fixture/obstacle footprints (a cell is occupied when its CENTER
lies inside a rect). ``free`` = cells whose center is at least ``robot_radius_m`` from every occupied cell center and
from the store outline (the robot's center may stand there).

Inflation uses ``scipy.ndimage.distance_transform_edt`` when scipy is installed (extra: routing) and an exact numpy
disk dilation otherwise; both give the same mask.
"""

import math
from dataclasses import dataclass

import numpy as np

from contracts.common import ROBOT_RADIUS_M, Point2, Rect2D
from contracts.planogram import Planogram
from contracts.routing import GridSpec

DEFAULT_RESOLUTION_M = 0.05


@dataclass
class OccupancyGrid:
    """A rasterized store: ``occupied`` (raw footprints), ``free`` (robot-center-safe cells); bool (rows, cols)."""

    spec: GridSpec
    occupied: np.ndarray
    free: np.ndarray

    def is_free(self, x: float, y: float) -> bool:
        col, row = self.spec.world_to_cell(x, y)
        return self.spec.in_bounds(col, row) and bool(self.free[row, col])

    def cell(self, x: float, y: float) -> tuple[int, int]:
        """(col, row) of a world point, clamped into the grid."""
        col, row = self.spec.world_to_cell(x, y)
        return min(max(col, 0), self.spec.width - 1), min(max(row, 0), self.spec.height - 1)


def make_grid_spec(
    plan: Planogram, resolution_m: float = DEFAULT_RESOLUTION_M, robot_radius_m: float = ROBOT_RADIUS_M
) -> GridSpec:
    """GridSpec covering the store bounds exactly (origin = bounds' bottom-left corner)."""
    b = plan.bounds
    return GridSpec(
        store_id=plan.store_id,
        resolution_m=resolution_m,
        origin=Point2(x=b.x, y=b.y),
        width=max(1, math.ceil(b.w / resolution_m - 1e-9)),
        height=max(1, math.ceil(b.h / resolution_m - 1e-9)),
        robot_radius_m=robot_radius_m,
    )


def _cell_range(lo: float, hi: float, origin: float, res: float, n: int) -> tuple[int, int]:
    """Half-open index range [i0, i1) of cells whose centers lie in [lo, hi]."""
    i0 = math.ceil((lo - origin) / res - 0.5 - 1e-9)
    i1 = math.floor((hi - origin) / res - 0.5 + 1e-9) + 1
    return max(i0, 0), min(i1, n)


def rasterize_rects(spec: GridSpec, rects: list[Rect2D]) -> np.ndarray:
    """Bool (rows, cols) mask: True where a cell center lies inside any rect (edges inclusive)."""
    occ = np.zeros((spec.height, spec.width), dtype=bool)
    res, ox, oy = spec.resolution_m, spec.origin.x, spec.origin.y
    for r in rects:
        c0, c1 = _cell_range(r.x, r.x_max, ox, res, spec.width)
        r0, r1 = _cell_range(r.y, r.y_max, oy, res, spec.height)
        if c0 < c1 and r0 < r1:
            occ[r0:r1, c0:c1] = True
    return occ


def rasterize(plan: Planogram, spec: GridSpec | None = None) -> np.ndarray:
    """Occupied mask of every fixture footprint + extra obstacle (the store outline is handled by ``inflate``)."""
    spec = spec or make_grid_spec(plan)
    return rasterize_rects(spec, [f.rect for f in plan.fixtures] + list(plan.obstacles))


def _dilate_rows(mask: np.ndarray, half_width: int) -> np.ndarray:
    """Dilate along axis 1 by ``half_width`` cells (sliding-window OR via a cumulative sum)."""
    if half_width <= 0:
        return mask.copy()
    rows, cols = mask.shape
    csum = np.zeros((rows, cols + 1), dtype=np.int32)
    np.cumsum(mask, axis=1, out=csum[:, 1:])
    idx = np.arange(cols)
    hi = np.minimum(idx + half_width + 1, cols)
    lo = np.maximum(idx - half_width, 0)
    return (csum[:, hi] - csum[:, lo]) > 0


def _dilate_disk_numpy(mask: np.ndarray, radius_cells: float) -> np.ndarray:
    """Dilate by every offset (dr, dc) with dr^2 + dc^2 < radius_cells^2 (matches EDT ``dist < radius``)."""
    out = mask.copy()
    rows = mask.shape[0]
    r2 = radius_cells * radius_cells
    r_max = math.ceil(radius_cells)
    for dr in range(-r_max, r_max + 1):
        rem = r2 - dr * dr
        if rem <= 0:
            continue
        half = max(math.ceil(math.sqrt(rem)) - 1, 0)  # largest h with h^2 < rem (fixed up for float error below)
        while half > 0 and half * half >= rem:
            half -= 1
        while (half + 1) * (half + 1) < rem:
            half += 1
        band = _dilate_rows(mask, half)
        if dr >= 0:
            out[dr:, :] |= band[: rows - dr, :]
        else:
            out[:dr, :] |= band[-dr:, :]
    return out


def inflate(occupied: np.ndarray, radius_m: float, resolution_m: float, block_border: bool = True) -> np.ndarray:
    """Free mask: True where the cell center is >= ``radius_m`` from every occupied cell center and (with
    ``block_border``) from the grid outline (= store outline)."""
    radius_cells = radius_m / resolution_m
    if radius_cells <= 0 or not occupied.any():
        free = ~occupied
    else:
        try:
            from scipy.ndimage import distance_transform_edt
        except ImportError:
            free = ~_dilate_disk_numpy(occupied, radius_cells)
        else:
            free = distance_transform_edt(~occupied) >= radius_cells - 1e-9
    if block_border and radius_cells > 0:
        free &= _border_clearance(occupied.shape, radius_cells)
    return free


def _border_clearance(shape: tuple[int, int], radius_cells: float) -> np.ndarray:
    """True where the cell center is >= radius from the grid outline (outline = outer cell edges)."""
    rows, cols = shape
    r_idx = np.arange(rows) + 0.5
    c_idx = np.arange(cols) + 0.5
    dy = np.minimum(r_idx, rows - r_idx)
    dx = np.minimum(c_idx, cols - c_idx)
    return (dy[:, None] >= radius_cells - 1e-9) & (dx[None, :] >= radius_cells - 1e-9)


def build_grid(
    plan: Planogram, resolution_m: float = DEFAULT_RESOLUTION_M, robot_radius_m: float = ROBOT_RADIUS_M
) -> OccupancyGrid:
    spec = make_grid_spec(plan, resolution_m, robot_radius_m)
    occupied = rasterize(plan, spec)
    return OccupancyGrid(spec=spec, occupied=occupied, free=inflate(occupied, robot_radius_m, resolution_m))


def nearest_free(grid: OccupancyGrid, col: int, row: int, max_radius_cells: int = 40) -> tuple[int, int] | None:
    """Closest free cell (Euclidean in cells) to (col, row), searching a square window; None if none within it."""
    spec = grid.spec
    if spec.in_bounds(col, row) and grid.free[row, col]:
        return col, row
    r0, r1 = max(row - max_radius_cells, 0), min(row + max_radius_cells + 1, spec.height)
    c0, c1 = max(col - max_radius_cells, 0), min(col + max_radius_cells + 1, spec.width)
    window = grid.free[r0:r1, c0:c1]
    rr, cc = np.nonzero(window)
    if rr.size == 0:
        return None
    d2 = (rr + r0 - row) ** 2 + (cc + c0 - col) ** 2
    k = int(np.argmin(d2))
    return int(cc[k] + c0), int(rr[k] + r0)
