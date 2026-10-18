"""Solver-agnostic route assembly shared by the fake and the real service (base deps only).

- ``merge_requests``: plan_batched -> one RouteRequest (one SKU -> one stop, qty summed, user_ids unioned).
- ``greedy_order``: nearest-neighbor over mandatory stops + cheapest insertion of optional stops (the fake's solver
  and the real service's fallback when OR-Tools is missing or fails).
- ``assemble_route``: turn a visit order + distance matrix + leg builder into a contracts ``Route`` with detours,
  baseline, visited/skipped and legs whose endpoints are exactly the stop poses.

Node indexing used everywhere here: 0 = start, 1..n = request.stops (same order), n + 1 = end.
"""

import hashlib
import math
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING

from contracts.common import Pose2D, StopId, StoreId
from contracts.planogram import Planogram
from contracts.routing import Route, RouteLeg, RouteRequest, Stop

if TYPE_CHECKING:
    from contracts.services import Services

START_ID = "start"
END_ID = "end"
OPTIONAL_MIN_INTEREST = 0.5
"""Optional stops below this interest are never visited by the greedy solver."""

DistFn = Callable[[int, int], float]
LegFn = Callable[[int, int, Pose2D, Pose2D], list[Pose2D]]
"""(from_node, to_node, from_pose, to_pose) -> waypoints (must start/end exactly on the two poses)."""


def endpoint_ids(request: RouteRequest) -> tuple[StopId, StopId]:
    """Ids for the implied start/end stops that don't collide with request stop ids."""
    taken = {s.stop_id for s in request.stops}
    start, end = START_ID, END_ID
    while start in taken:
        start = f"_{start}"
    while end in taken:
        end = f"_{end}"
    return start, end


def node_poses(request: RouteRequest, end_pose: Pose2D) -> list[Pose2D]:
    return [request.start, *(s.pose for s in request.stops), end_pose]


def euclidean_matrix(poses: Sequence[Pose2D]) -> list[list[float]]:
    return [[math.hypot(b.x - a.x, b.y - a.y) for b in poses] for a in poses]


def is_optional(stop: Stop) -> bool:
    return stop.kind == "optional"


def tour_length(tour: Sequence[int], dist: DistFn) -> float:
    return sum(dist(tour[i], tour[i + 1]) for i in range(len(tour) - 1))


def cheapest_insertion(tour: Sequence[int], node: int, dist: DistFn) -> tuple[float, int]:
    """(extra distance, insert position) for inserting ``node`` between tour[i-1] and tour[i]; ties -> earliest."""
    best = (math.inf, 1)
    for i in range(1, len(tour)):
        a, b = tour[i - 1], tour[i]
        cost = dist(a, node) + dist(node, b) - dist(a, b)
        if cost < best[0] - 1e-9:
            best = (cost, i)
    return best


def nearest_neighbor_tour(start: int, end: int, nodes: Sequence[int], dist: DistFn) -> list[int]:
    """start -> nearest unvisited (ties: lowest node index) -> ... -> end."""
    tour = [start]
    remaining = list(nodes)
    cur = start
    while remaining:
        nxt = min(remaining, key=lambda n: (dist(cur, n), n))
        remaining.remove(nxt)
        tour.append(nxt)
        cur = nxt
    tour.append(end)
    return tour


def two_opt(tour: list[int], dist: DistFn, max_rounds: int = 20) -> list[int]:
    """Classic 2-opt on the interior of an open path with fixed endpoints (asymmetric-safe: re-evaluates fully)."""
    best = list(tour)
    best_len = tour_length(best, dist)
    for _ in range(max_rounds):
        improved = False
        for i in range(1, len(best) - 2):
            for j in range(i + 1, len(best) - 1):
                cand = best[:i] + best[i : j + 1][::-1] + best[j + 1 :]
                cand_len = tour_length(cand, dist)
                if cand_len < best_len - 1e-9:
                    best, best_len, improved = cand, cand_len, True
        if not improved:
            break
    return best


def greedy_order(
    request: RouteRequest,
    dist: DistFn,
    min_interest: float = OPTIONAL_MIN_INTEREST,
    improve: bool = False,
) -> tuple[list[int], list[int]]:
    """Return (mandatory-only baseline tour, full tour) as node lists.

    Mandatory stops: nearest-neighbor from the start (optionally 2-opt). Optional stops with interest >=
    ``min_interest``, highest interest first (ties: stop order), are inserted at their cheapest position until
    ``max_optional_stops`` are in or the ``max_extra_distance_m`` budget would be exceeded.
    """
    n = len(request.stops)
    start, end = 0, n + 1
    mandatory = [i + 1 for i, s in enumerate(request.stops) if not is_optional(s)]
    baseline = nearest_neighbor_tour(start, end, mandatory, dist)
    if improve:
        baseline = two_opt(baseline, dist)
    tour = list(baseline)
    cap = request.max_optional_stops
    budget = request.max_extra_distance_m
    extra = 0.0
    candidates = sorted(
        (i + 1 for i, s in enumerate(request.stops) if is_optional(s) and s.interest >= min_interest),
        key=lambda node: (-request.stops[node - 1].interest, node),
    )
    added = 0
    for node in candidates:
        if cap is not None and added >= cap:
            break
        cost, pos = cheapest_insertion(tour, node, dist)
        if budget is not None and extra + cost > budget + 1e-9:
            continue
        tour.insert(pos, node)
        extra += cost
        added += 1
    return baseline, tour


def assemble_route(
    request: RouteRequest,
    end_pose: Pose2D,
    tour: list[int],
    baseline: list[int],
    dist: DistFn,
    leg_fn: LegFn,
    solver: str,
    solve_ms: float = 0.0,
    route_id: str | None = None,
) -> Route:
    """Build the contracts Route for a solved node tour (see module docstring for node indexing)."""
    n = len(request.stops)
    start_id, end_id = endpoint_ids(request)
    poses = node_poses(request, end_pose)
    speed = request.robot_speed_mps

    start_stop = Stop(stop_id=start_id, kind="start", pose=request.start, label="Start", qty=0)
    end_stop = Stop(stop_id=end_id, kind="end", pose=end_pose, label="Checkout", qty=0)
    stops_out: list[Stop] = [start_stop]
    in_tour = set(tour)
    visited_optional: list[StopId] = []
    skipped_optional: list[StopId] = []
    for i, stop in enumerate(request.stops):
        node = i + 1
        update: dict[str, float] = {}
        if is_optional(stop):
            if node in in_tour:
                k = tour.index(node)
                a, b = tour[k - 1], tour[k + 1]
                detour = dist(a, node) + dist(node, b) - dist(a, b)
                visited_optional.append(stop.stop_id)
            else:
                detour, _ = cheapest_insertion(tour, node, dist)
                skipped_optional.append(stop.stop_id)
            detour = max(0.0, detour)
            update = {"detour_m": round(detour, 3), "detour_s": round(detour / speed + stop.service_time_s, 2)}
        stops_out.append(stop.model_copy(update=update, deep=True))
    stops_out.append(end_stop)

    def node_id(node: int) -> StopId:
        if node == 0:
            return start_id
        if node == n + 1:
            return end_id
        return request.stops[node - 1].stop_id

    legs: list[RouteLeg] = []
    for a, b in zip(tour, tour[1:], strict=False):
        d = dist(a, b)
        wps = leg_fn(a, b, poses[a], poses[b])
        legs.append(
            RouteLeg(
                from_stop_id=node_id(a),
                to_stop_id=node_id(b),
                waypoints=wps,
                distance_m=round(d, 3),
                eta_s=round(d / speed, 2),
            )
        )
    total = tour_length(tour, dist)
    service = sum(request.stops[node - 1].service_time_s for node in tour[1:-1])
    baseline_wps: list[Pose2D] = []
    for a, b in zip(baseline, baseline[1:], strict=False):
        seg = leg_fn(a, b, poses[a], poses[b])
        baseline_wps.extend(seg if not baseline_wps else seg[1:])
    return Route(
        route_id=route_id or deterministic_route_id(request, tour),
        store_id=request.store_id,
        stops=stops_out,
        order=[node_id(node) for node in tour],
        legs=legs,
        total_distance_m=round(total, 3),
        est_time_s=round(total / speed + service, 2),
        visited_optional=visited_optional,
        skipped_optional=skipped_optional,
        baseline_distance_m=round(tour_length(baseline, dist), 3),
        baseline_waypoints=baseline_wps,
        solver=solver,
        solve_ms=solve_ms,
    )


def deterministic_route_id(request: RouteRequest, tour: Sequence[int]) -> str:
    digest = hashlib.sha256((request.model_dump_json() + repr(list(tour))).encode()).hexdigest()
    return f"route_{digest[:10]}"


def straight_leg(_a: int, _b: int, pa: Pose2D, pb: Pose2D) -> list[Pose2D]:
    """Straight segment; both endpoints are the exact stop poses."""
    return [pa.model_copy(), pb.model_copy()]


# --------------------------------------------------------------------------------------------------------------------
# Batching
# --------------------------------------------------------------------------------------------------------------------
def _merge_key(stop: Stop) -> tuple[str, ...]:
    if stop.slot_id:
        return ("slot", stop.slot_id)
    if stop.skus:
        return ("skus", *sorted(stop.skus))
    return ("id", stop.stop_id)


def merge_requests(requests: list[RouteRequest]) -> RouteRequest:
    """Union several shoppers' requests (same store) into one: identical slot/SKU stops collapse into one stop with
    qty summed and item_ids/user_ids unioned; mandatory if any shopper needs it; optional interest = min(1, sum).
    Start/end/speed/time limit come from the first request; caps take the max; budgets the min."""
    if not requests:
        raise ValueError("plan_batched needs at least one RouteRequest")
    stores = {r.store_id for r in requests}
    if len(stores) != 1:
        raise ValueError(f"plan_batched: requests span several stores {sorted(stores)}")
    first = requests[0]
    merged: dict[tuple[str, ...], Stop] = {}
    users: list[str] = []
    for req in requests:
        for u in req.user_ids:
            if u not in users:
                users.append(u)
        for stop in req.stops:
            stop_users = stop.user_ids or list(req.user_ids)
            key = _merge_key(stop)
            cur = merged.get(key)
            if cur is None:
                merged[key] = stop.model_copy(update={"user_ids": list(stop_users)}, deep=True)
                continue
            kind = "mandatory" if (not is_optional(cur) or not is_optional(stop)) else "optional"
            merged[key] = cur.model_copy(
                update={
                    "kind": kind,
                    "qty": cur.qty + stop.qty,
                    "item_ids": cur.item_ids + [i for i in stop.item_ids if i not in cur.item_ids],
                    "user_ids": cur.user_ids + [u for u in stop_users if u not in cur.user_ids],
                    "skus": cur.skus + [s for s in stop.skus if s not in cur.skus],
                    "interest": 1.0 if kind == "mandatory" else min(1.0, cur.interest + stop.interest),
                    "service_time_s": max(cur.service_time_s, stop.service_time_s),
                    "rec_id": cur.rec_id or stop.rec_id,
                }
            )
    caps = [r.max_optional_stops for r in requests]
    budgets = [r.max_extra_distance_m for r in requests if r.max_extra_distance_m is not None]
    return RouteRequest(
        store_id=first.store_id,
        start=first.start,
        end=first.end,
        stops=list(merged.values()),
        max_optional_stops=None if any(c is None for c in caps) else max(c for c in caps if c is not None),
        max_extra_distance_m=min(budgets) if budgets else None,
        time_limit_s=max(r.time_limit_s for r in requests),
        robot_speed_mps=first.robot_speed_mps,
        user_ids=users,
    )


# --------------------------------------------------------------------------------------------------------------------
# Planogram access (call time only)
# --------------------------------------------------------------------------------------------------------------------
async def get_planogram(services: "Services | None", store_id: StoreId) -> Planogram:
    """Live planogram via ``services.catalog`` when wired, else straight from ``data/`` (utils.data).

    Raises KeyError for unknown / online stores (same as CatalogService.get_planogram).
    """
    catalog = services.catalog if services is not None else None
    if catalog is not None:
        return await catalog.get_planogram(store_id)
    from utils.data import DataError, load_planogram

    try:
        return load_planogram(store_id)
    except DataError as exc:
        raise KeyError(store_id) from exc
