"""Real routing service (CLAUDE.md §9): planogram -> 0.05 m occupancy grid inflated by ROBOT_RADIUS_M ->
skimage ``MCP_Geometric`` distance matrix + paths between stop poses -> OR-Tools orienteering (mandatory = no
disjunction, recommendations = ``AddDisjunction`` with penalty = interest x PENALTY_K_M, all in integer cm).

Heavy deps are optional (extra: routing = ortools, scipy, scikit-image) and imported lazily. Degradation ladder:
  no scikit-image -> visibility-graph shortest paths (base deps, logged)  ·  no ortools / no solution -> greedy
  nearest-neighbor + 2-opt + cheapest insertion (``core.greedy_order``)  -> ``Route.solver`` says which ran.

TODO (owner), mapped to the routing spec items in CLAUDE.md §3 / the README checklist:
  1. [P0] Path quality: line-of-sight shortcut smoothing + ~0.25 m resampling (now: collinear-pruned grid path).
  2. [P0] Measure solve time on store_ave (target < 1.5 s incl. matrix); drop to 0.10 m grid if MCP is slow.
  3. [P1] Assert every stop pose is in the entrance's connected component (scipy.ndimage.label) and emit an
     ``error`` event / skip the stop instead of a 1e6 m distance.
  4. [P1] Agree PENALTY_K_M with the recommender (interest 0.8 -> 12 m worth of detour at K = 15).
  5. [P1] Cached demo route (JSON) as the last-resort fallback.
  6. [P2] Batched shoppers: basket capacity via AddDimensionWithVehicleCapacity (merge already works).
"""

import asyncio
import hashlib
import math
import time
from collections.abc import Callable

import numpy as np

from arms.routing.core import (
    OPTIONAL_MIN_INTEREST,
    assemble_route,
    get_planogram,
    greedy_order,
    is_optional,
    merge_requests,
    node_poses,
    straight_leg,
    tour_length,
)
from arms.routing.grid import DEFAULT_RESOLUTION_M, OccupancyGrid, build_grid, make_grid_spec, nearest_free
from arms.routing.visibility import visibility_map
from contracts.common import Pose2D, StoreId
from contracts.planogram import Planogram
from contracts.routing import GridSpec, Route, RouteRequest
from contracts.services import Services
from utils.log import get_logger

log = get_logger(__name__)

SCALE = 100  # meters -> integer cm; penalties use the same unit
PENALTY_K_M = 15.0
"""Meters of detour an interest of 1.0 is worth (penalty = interest x K)."""
UNREACHABLE_M = 1e6


def geometry_hash(plan: Planogram) -> str:
    """Hash of everything that shapes the grid (not stock), for caching."""
    payload = plan.bounds.model_dump_json() + "".join(f.rect.model_dump_json() for f in plan.fixtures)
    payload += "".join(o.model_dump_json() for o in plan.obstacles)
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


class _GridPaths:
    """Distance matrix + paths on the occupancy grid via skimage MCP_Geometric (one Dijkstra per source)."""

    def __init__(self, grid: OccupancyGrid, poses: list[Pose2D]) -> None:
        from skimage.graph import MCP_Geometric  # extra: routing

        self.grid = grid
        res = grid.spec.resolution_m
        cost = np.where(grid.free, 1.0, np.inf)
        self.cells: list[tuple[int, int]] = []  # (row, col) per node
        for p in poses:
            col, row = grid.cell(p.x, p.y)
            snapped = nearest_free(grid, col, row)
            if snapped is None:
                log.warning("pose (%.2f, %.2f) has no free cell nearby; routing may fail", p.x, p.y)
                snapped = (col, row)
            elif snapped != (col, row):
                log.debug("snapped pose (%.2f, %.2f) to free cell %s", p.x, p.y, snapped)
            self.cells.append((snapped[1], snapped[0]))
        n = len(poses)
        self.matrix = np.full((n, n), UNREACHABLE_M)
        self._mcps = []
        for i, src in enumerate(self.cells):
            mcp = MCP_Geometric(cost, fully_connected=True)
            costs, _ = mcp.find_costs([src])
            self._mcps.append(mcp)
            for j, dst in enumerate(self.cells):
                c = costs[dst]
                if np.isfinite(c):
                    self.matrix[i, j] = float(c) * res
        # Symmetrize (MCP is symmetric up to float noise); unreachable stays UNREACHABLE_M.
        self.matrix = np.minimum(self.matrix, self.matrix.T)

    def dist(self, a: int, b: int) -> float:
        return float(self.matrix[a, b])

    def leg(self, a: int, b: int, pa: Pose2D, pb: Pose2D) -> list[Pose2D]:
        if a == b or self.matrix[a, b] >= UNREACHABLE_M:
            return [pa.model_copy(), pb.model_copy()]
        cells = self._mcps[a].traceback(self.cells[b])  # [(row, col), ...] from source a
        pts = [self.grid.spec.cell_to_world(c, r) for r, c in _prune_collinear(cells)]
        inner = pts[1:-1]
        out = [pa.model_copy()]
        prev = (pa.x, pa.y)
        for x, y in inner:
            out.append(Pose2D(x=x, y=y, theta=math.atan2(y - prev[1], x - prev[0])))
            prev = (x, y)
        out.append(pb.model_copy())
        return out


def _prune_collinear(cells: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Keep endpoints + cells where the step direction changes (TODO 1: replace by line-of-sight smoothing)."""
    if len(cells) <= 2:
        return list(cells)
    out = [cells[0]]
    for prev, cur, nxt in zip(cells, cells[1:], cells[2:], strict=False):
        if (cur[0] - prev[0], cur[1] - prev[1]) != (nxt[0] - cur[0], nxt[1] - cur[1]):
            out.append(cur)
    out.append(cells[-1])
    return out


def solve_ortools(
    request: RouteRequest, dist: Callable[[int, int], float], candidates: list[int], time_limit_s: float
) -> list[int] | None:
    """Orienteering with OR-Tools over start + mandatory + ``candidates`` + end. Returns the node tour or None."""
    from ortools.constraint_solver import pywrapcp
    from ortools.constraint_solver import routing_enums_pb2 as enums

    n = len(request.stops)
    mandatory = [i + 1 for i, s in enumerate(request.stops) if not is_optional(s)]
    nodes = [0, *mandatory, *candidates, n + 1]  # sub-problem index -> global node
    di = [[int(round(min(dist(a, b), UNREACHABLE_M) * SCALE)) for b in nodes] for a in nodes]
    mgr = pywrapcp.RoutingIndexManager(len(nodes), 1, [0], [len(nodes) - 1])
    model = pywrapcp.RoutingModel(mgr)
    cb = model.RegisterTransitCallback(lambda i, j: di[mgr.IndexToNode(i)][mgr.IndexToNode(j)])
    model.SetArcCostEvaluatorOfAllVehicles(cb)
    if request.max_extra_distance_m is not None:
        base = tour_length(greedy_order(request.model_copy(update={"max_optional_stops": 0}), dist)[0], dist)
        model.AddDimension(cb, 0, int((base + request.max_extra_distance_m) * SCALE), True, "Dist")
    for k, node in enumerate(nodes):
        if node in candidates:
            penalty = int(round(request.stops[node - 1].interest * PENALTY_K_M * SCALE))
            model.AddDisjunction([mgr.NodeToIndex(k)], penalty)
    params = pywrapcp.DefaultRoutingSearchParameters()
    params.first_solution_strategy = enums.FirstSolutionStrategy.PATH_CHEAPEST_ARC
    params.local_search_metaheuristic = enums.LocalSearchMetaheuristic.GUIDED_LOCAL_SEARCH
    params.time_limit.FromMilliseconds(int(time_limit_s * 1000))
    sol = model.SolveWithParameters(params)
    if not sol:
        return None
    tour, idx = [], model.Start(0)
    while not model.IsEnd(idx):
        tour.append(nodes[mgr.IndexToNode(idx)])
        idx = sol.Value(model.NextVar(idx))
    tour.append(nodes[mgr.IndexToNode(idx)])
    return tour


class GridRoutingService:
    """Satisfies ``contracts.services.RoutingService``. Calls ``services.catalog.get_planogram`` at call time."""

    def __init__(self, services: Services | None = None, resolution_m: float = DEFAULT_RESOLUTION_M) -> None:
        self._services = services
        self._resolution_m = resolution_m
        self._grids: dict[tuple[StoreId, str], OccupancyGrid] = {}

    def grid_for(self, plan: Planogram) -> OccupancyGrid:
        key = (plan.store_id, geometry_hash(plan))
        if key not in self._grids:
            self._grids[key] = build_grid(plan, self._resolution_m)
        return self._grids[key]

    async def grid_spec(self, store_id: StoreId) -> GridSpec:
        return make_grid_spec(await get_planogram(self._services, store_id), self._resolution_m)

    async def plan_route(self, request: RouteRequest) -> Route:
        plan = await get_planogram(self._services, request.store_id)
        return await asyncio.to_thread(self._solve, request, plan)

    async def plan_batched(self, requests: list[RouteRequest]) -> Route:
        return await self.plan_route(merge_requests(requests))

    def _solve(self, request: RouteRequest, plan: Planogram) -> Route:
        t0 = time.perf_counter()
        end_pose = request.end or plan.checkout
        poses = node_poses(request, end_pose)

        leg_fn = straight_leg
        try:
            paths = _GridPaths(self.grid_for(plan), poses)
            dist, leg_fn = paths.dist, paths.leg
        except ImportError:
            log.warning("scikit-image missing (uv sync --extra routing): visibility-graph paths")
            table = visibility_map(plan).paths(poses)
            dist, leg_fn = table.dist, table.leg

        baseline, greedy_tour = greedy_order(request, dist, improve=True)
        tour, solver = greedy_tour, "greedy"
        cap = request.max_optional_stops
        candidates = sorted(
            (i + 1 for i, s in enumerate(request.stops) if is_optional(s) and s.interest >= OPTIONAL_MIN_INTEREST),
            key=lambda node: (-request.stops[node - 1].interest, node),
        )
        if cap is not None:
            candidates = candidates[:cap]
        try:
            solved = solve_ortools(request, dist, candidates, request.time_limit_s)
        except ImportError:
            log.warning("ortools missing (uv sync --extra routing): greedy solver")
            solved = None
        except Exception:  # never kill the trip over the solver
            log.exception("ortools failed; greedy fallback")
            solved = None
        if solved is not None:
            tour, solver = solved, "ortools"
            mandatory_only = [n for n in solved if n == 0 or n == len(poses) - 1 or not is_optional_node(request, n)]
            if tour_length(mandatory_only, dist) < tour_length(baseline, dist):
                baseline = mandatory_only
        ms = (time.perf_counter() - t0) * 1000
        return assemble_route(request, end_pose, tour, baseline, dist, leg_fn, solver=solver, solve_ms=round(ms, 1))


def is_optional_node(request: RouteRequest, node: int) -> bool:
    return 1 <= node <= len(request.stops) and is_optional(request.stops[node - 1])
