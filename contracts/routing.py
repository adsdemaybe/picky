"""Routing contracts: occupancy grid spec, stops, route requests and solved routes. Owned by the routing arm.

``GridSpec`` here is the only wire type for the grid; ``utils.geometry`` helpers must be built from it.
Batching: ONE path — ``plan_route`` with stops that carry ``user_ids`` (identical SKUs deduped into one stop).
``plan_batched`` only merges several RouteRequests into that form; stop_ids must be unique across all of them
(prefix with the user id, e.g. ``maya:salsa``).
"""

import math
from typing import Literal

from pydantic import Field, computed_field, model_validator

from contracts.common import (
    ROBOT_RADIUS_M,
    ItemId,
    Model,
    Point2,
    Pose2D,
    RecId,
    RouteId,
    Score01,
    Sku,
    SlotId,
    StopId,
    StoreId,
    UserId,
    ensure_unique,
)

StopKind = Literal["start", "mandatory", "optional", "end"]
StopStatus = Literal["pending", "navigating", "arrived", "done", "skipped", "declined", "failed"]
"""Execution progress of a stop (``Trip.stop_status``): skipped = solver dropped it, declined = shopper said no,
failed = out of stock / pick failed."""


class GridSpec(Model):
    """Occupancy grid geometry. Cell (0, 0) has its lower-left corner at ``origin``; row = y index, col = x index."""

    store_id: StoreId
    resolution_m: float = Field(0.05, gt=0)
    origin: Point2 = Field(default_factory=lambda: Point2(x=0.0, y=0.0))
    width: int = Field(ge=1, description="Columns (cells along +x).")
    height: int = Field(ge=1, description="Rows (cells along +y).")
    robot_radius_m: float = Field(
        ROBOT_RADIUS_M, ge=0, description="Obstacle inflation radius (contracts.common.ROBOT_RADIUS_M, no margin)."
    )

    def world_to_cell(self, x: float, y: float) -> tuple[int, int]:
        """(col, row) of the cell containing world point (x, y)."""
        return (
            math.floor((x - self.origin.x) / self.resolution_m),
            math.floor((y - self.origin.y) / self.resolution_m),
        )

    def cell_to_world(self, col: int, row: int) -> tuple[float, float]:
        """World center of cell (col, row)."""
        return (
            self.origin.x + (col + 0.5) * self.resolution_m,
            self.origin.y + (row + 0.5) * self.resolution_m,
        )

    def in_bounds(self, col: int, row: int) -> bool:
        return 0 <= col < self.width and 0 <= row < self.height


class Stop(Model):
    stop_id: StopId
    kind: StopKind
    pose: Pose2D = Field(description="Robot pose at the stop (a slot's approach_pose).")
    label: str | None = None
    slot_id: SlotId | None = None
    skus: list[Sku] = Field(default_factory=list)
    qty: int = Field(1, ge=0, description="Total units to pick here (summed over item_ids / users).")
    item_ids: list[ItemId] = Field(default_factory=list, description="ListItems served by this stop.")
    user_ids: list[UserId] = Field(default_factory=list, description="Batched trips: who this stop serves.")
    interest: Score01 = Field(1.0, description="Optional stops: predicted interest = skip penalty weight.")
    rec_id: RecId | None = None
    service_time_s: float = Field(0.0, ge=0, description="Expected dwell time (scan + pick).")
    detour_m: float | None = Field(
        None,
        description="Optional stops, filled by routing: visited = d(prev,stop)+d(stop,next)-d(prev,next); "
        "skipped = cheapest insertion cost. The gateway copies it into Recommendation.extra_distance_m.",
    )
    detour_s: float | None = Field(None, description="detour_m / robot speed + service_time_s.")


class RouteRequest(Model):
    store_id: StoreId
    start: Pose2D
    end: Pose2D | None = Field(None, description="None = the store's checkout pose.")
    stops: list[Stop] = Field(description="Mandatory + optional stops (start/end are implied by start/end poses).")
    max_optional_stops: int | None = Field(3, ge=0, description="Cap on recommendation detours (PICKY_REC_MAX_STOPS).")
    max_extra_distance_m: float | None = Field(None, ge=0, description="Detour budget vs the list-only route.")
    time_limit_s: float = Field(1.0, gt=0, description="Solver time limit.")
    robot_speed_mps: float = Field(0.5, gt=0)
    user_ids: list[UserId] = Field(default_factory=list)

    @model_validator(mode="after")
    def _unique(self) -> "RouteRequest":
        ensure_unique([s.stop_id for s in self.stops], "stop_id")
        return self


class RouteLeg(Model):
    """Path between two consecutive stops. ``waypoints[0]`` / ``waypoints[-1]`` are EXACTLY the from/to stop poses
    (incl. theta). Intermediate thetas are path headings (the follower may ignore them); the final theta must be
    reached (NavigateCmd.yaw_tolerance_rad)."""

    from_stop_id: StopId
    to_stop_id: StopId
    waypoints: list[Pose2D] = Field(description="Collision-free path including both endpoints.")
    distance_m: float = Field(ge=0)
    eta_s: float | None = None


class Route(Model):
    """A solved route. A mid-trip re-plan returns a route from the robot's current pose over the REMAINING stops;
    progress across re-plans (done / declined / failed) lives in ``Trip.stop_status``, not here."""

    route_id: RouteId
    store_id: StoreId
    stops: list[Stop] = Field(description="Every stop in the route incl. start/end (look up by stop_id).")
    order: list[StopId] = Field(description="Visit order, starting with the start stop and ending with the end stop.")
    legs: list[RouteLeg] = Field(default_factory=list, description="len(order) - 1 legs in visit order.")
    total_distance_m: float = Field(0.0, ge=0)
    est_time_s: float | None = None
    visited_optional: list[StopId] = Field(default_factory=list)
    skipped_optional: list[StopId] = Field(default_factory=list)
    baseline_distance_m: float | None = Field(None, description="List-only route distance (for '+12 m, 25 s').")
    baseline_waypoints: list[Pose2D] = Field(default_factory=list, description="List-only path for the map overlay.")
    solver: str = Field("fake", description="ortools | greedy | cached | fake")
    solve_ms: float = 0.0

    def stop(self, stop_id: StopId) -> Stop | None:
        return next((s for s in self.stops if s.stop_id == stop_id), None)

    def ordered_stops(self) -> list[Stop]:
        by_id = {s.stop_id: s for s in self.stops}
        return [by_id[i] for i in self.order if i in by_id]

    @computed_field  # type: ignore[prop-decorator]
    @property
    def extra_distance_m(self) -> float | None:
        if self.baseline_distance_m is None:
            return None
        return self.total_distance_m - self.baseline_distance_m
