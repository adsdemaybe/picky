import pytest

from arms.routing import create
from arms.routing.grid import build_grid
from arms.routing.tests.conftest import MANDATORY, make_request
from contracts.services import RoutingService


def test_conforms_to_protocol(services):
    svc = create(services)
    assert isinstance(svc, RoutingService)
    assert services.catalog.calls == 0


async def test_plan_route_runs_with_base_deps(services, store):
    """Without the routing extra it degrades (straight lines / greedy) but still returns a valid route."""
    route = await create(services).plan_route(make_request(store))
    assert route.solver in {"ortools", "greedy"}
    assert route.order[0] == "start" and route.order[-1] == "end"
    for slot_id in MANDATORY:
        assert f"stop_{slot_id}" in route.order
    assert "stop_s_cookies" in route.skipped_optional
    assert len(route.visited_optional) <= 3
    for leg in route.legs:
        assert leg.waypoints[0] == route.stop(leg.from_stop_id).pose
        assert leg.waypoints[-1] == route.stop(leg.to_stop_id).pose
    for sid in route.visited_optional + route.skipped_optional:
        assert route.stop(sid).detour_m is not None


async def test_grid_paths_avoid_shelves(services, store):
    pytest.importorskip("skimage")
    route = await create(services).plan_route(make_request(store))
    grid = build_grid(store)
    for leg in route.legs:
        for wp in leg.waypoints[1:-1]:
            assert grid.is_free(wp.x, wp.y)
    # grid distances are never shorter than straight lines
    for leg in route.legs:
        a, b = route.stop(leg.from_stop_id).pose, route.stop(leg.to_stop_id).pose
        assert leg.distance_m >= a.distance_to(b) - 0.1


async def test_ortools_solver(services, store):
    pytest.importorskip("ortools")
    route = await create(services).plan_route(make_request(store))
    assert route.solver == "ortools"
    assert len(route.visited_optional) <= 3


async def test_grid_spec(services):
    spec = await create(services).grid_spec("store_test")
    assert spec.resolution_m == 0.05
