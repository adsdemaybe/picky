"""Visibility-graph paths: legs never cross a fixture footprint (the robot fake rejects such paths)."""

import math

import pytest

from arms.routing import create_fake
from arms.routing.tests.conftest import make_request
from arms.routing.visibility import visibility_map
from contracts.planogram import Planogram
from contracts.routing import Route, RouteRequest, Stop


def _assert_collision_free(route: Route, plan: Planogram) -> None:
    rects = [f.rect for f in plan.fixtures] + list(plan.obstacles)
    for leg in route.legs:
        for a, b in zip(leg.waypoints, leg.waypoints[1:], strict=False):
            n = max(1, int(a.distance_to(b) / 0.05))
            for k in range(n + 1):
                x = a.x + (b.x - a.x) * k / n
                y = a.y + (b.y - a.y) * k / n
                assert plan.bounds.contains(x, y), (leg.from_stop_id, leg.to_stop_id, x, y)
                for r in rects:
                    assert not r.contains(x, y), (leg.from_stop_id, leg.to_stop_id, x, y)


async def test_fake_legs_avoid_shelves(services, store):
    route = await create_fake(services).plan_route(make_request(store))
    _assert_collision_free(route, store)
    for leg in route.legs:  # path length >= straight line, and equals the leg's polyline length
        poly = sum(a.distance_to(b) for a, b in zip(leg.waypoints, leg.waypoints[1:], strict=False))
        assert leg.distance_m == pytest.approx(poly, abs=0.01)
        assert leg.distance_m >= leg.waypoints[0].distance_to(leg.waypoints[-1]) - 1e-6


def test_map_is_cached(store):
    assert visibility_map(store) is visibility_map(store)


async def test_real_store_ave_all_slots_reachable():
    from utils.data import DataError, load_planogram

    try:
        plan = load_planogram("store_ave")
    except DataError:
        pytest.skip("data/planograms/store_ave.yaml not written yet")
    stops = [
        Stop(stop_id=f"stop_{s.slot_id}", kind="mandatory", pose=s.approach_pose, slot_id=s.slot_id, skus=[s.sku])
        for s in plan.slots
    ]
    table = visibility_map(plan).paths([plan.start_pose, *(s.pose for s in stops), plan.checkout])
    assert not table.unreachable
    req = RouteRequest(store_id="store_ave", start=plan.start_pose, stops=stops[::5])
    from contracts.services import Services

    route = await create_fake(Services()).plan_route(req)
    _assert_collision_free(route, plan)
    assert all(math.isfinite(leg.distance_m) for leg in route.legs)
