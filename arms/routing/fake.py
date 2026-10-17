"""Deterministic routing fake (base deps only): obstacle-aware shortest paths on a visibility graph of the
planogram (straight lines only when no planogram is available), nearest-neighbor mandatory order, optional stops with
interest >= 0.5 inserted at their cheapest position (up to ``max_optional_stops``)."""

from arms.routing.core import (
    assemble_route,
    euclidean_matrix,
    get_planogram,
    greedy_order,
    merge_requests,
    node_poses,
    straight_leg,
)
from arms.routing.grid import make_grid_spec
from arms.routing.visibility import visibility_map
from contracts.common import StoreId
from contracts.planogram import Planogram
from contracts.routing import GridSpec, Route, RouteRequest
from contracts.services import Services
from utils.log import get_logger

log = get_logger(__name__)


class FakeRoutingService:
    """Satisfies ``contracts.services.RoutingService``. Touches ``services.catalog`` only at call time."""

    def __init__(self, services: Services | None = None) -> None:
        self._services = services

    async def _planogram(self, store_id: StoreId) -> Planogram | None:
        try:
            return await get_planogram(self._services, store_id)
        except Exception as exc:  # no catalog/data: straight lines, end at the start pose
            log.warning("no planogram for %s (%s); straight-line routing", store_id, exc)
            return None

    async def plan_route(self, request: RouteRequest) -> Route:
        plan = await self._planogram(request.store_id)
        end_pose = request.end or (plan.checkout if plan is not None else request.start)
        poses = node_poses(request, end_pose)
        leg_fn = straight_leg
        if plan is not None:
            table = visibility_map(plan).paths(poses)
            if table.unreachable:
                log.warning(
                    "%d stop pairs unreachable in %s; straight legs for those", len(table.unreachable), plan.store_id
                )
            dist, leg_fn = table.dist, table.leg
        else:
            matrix = euclidean_matrix(poses)

            def dist(a: int, b: int) -> float:
                return matrix[a][b]

        baseline, tour = greedy_order(request, dist)
        return assemble_route(request, end_pose, tour, baseline, dist, leg_fn, solver="fake")

    async def plan_batched(self, requests: list[RouteRequest]) -> Route:
        return await self.plan_route(merge_requests(requests))

    async def grid_spec(self, store_id: StoreId) -> GridSpec:
        return make_grid_spec(await get_planogram(self._services, store_id))
