import pytest

from arms.routing import create_fake
from arms.routing.core import merge_requests
from arms.routing.fake import FakeRoutingService
from arms.routing.tests.conftest import MANDATORY, make_request
from contracts.routing import RouteRequest
from contracts.services import RoutingService, Services


def test_conforms_to_protocol(services):
    svc = create_fake(services)
    assert isinstance(svc, RoutingService)
    assert isinstance(FakeRoutingService(Services()), RoutingService)


def test_factory_does_not_call_services(services):
    create_fake(services)
    assert services.catalog.calls == 0


async def test_demo_route(services, store):
    route = await create_fake(services).plan_route(make_request(store))
    assert route.solver == "fake"
    assert route.order[0] == "start" and route.order[-1] == "end"
    # every mandatory stop visited once
    for slot_id in MANDATORY:
        assert route.order.count(f"stop_{slot_id}") == 1
    # top-3 interest optional stops visited, low-interest one skipped
    assert sorted(route.visited_optional) == ["stop_s_chips", "stop_s_guac", "stop_s_hot"]
    assert route.skipped_optional == ["stop_s_cookies"]
    for sid in route.visited_optional + route.skipped_optional:
        stop = route.stop(sid)
        assert stop.detour_m is not None and stop.detour_m >= 0
        assert stop.detour_s == pytest.approx(stop.detour_m / 0.5 + 10.0, abs=0.02)
    # end defaults to the store's checkout pose
    assert route.stop("end").pose == store.checkout
    # legs chain the order and start/end exactly on the stop poses
    assert len(route.legs) == len(route.order) - 1
    for leg, (a, b) in zip(route.legs, zip(route.order, route.order[1:], strict=False), strict=True):
        assert (leg.from_stop_id, leg.to_stop_id) == (a, b)
        assert leg.waypoints[0] == route.stop(a).pose
        assert leg.waypoints[-1] == route.stop(b).pose
    assert route.total_distance_m == pytest.approx(sum(leg.distance_m for leg in route.legs), abs=0.01)
    assert route.baseline_distance_m is not None and route.extra_distance_m >= 0
    assert route.baseline_waypoints[0] == store.entrance and route.baseline_waypoints[-1] == store.checkout


async def test_deterministic(services, store):
    svc = create_fake(services)
    a = await svc.plan_route(make_request(store))
    b = await svc.plan_route(make_request(store))
    assert a.model_dump() == b.model_dump()


async def test_caps_and_budget(services, store):
    svc = create_fake(services)
    none = await svc.plan_route(make_request(store, max_optional_stops=0))
    assert none.visited_optional == [] and len(none.skipped_optional) == 4
    assert none.total_distance_m == pytest.approx(none.baseline_distance_m)
    tight = await svc.plan_route(make_request(store, max_optional_stops=None, max_extra_distance_m=0.0))
    assert tight.extra_distance_m <= 1e-6


async def test_works_without_catalog(store):
    svc = create_fake(Services())
    route = await svc.plan_route(make_request(store).model_copy(update={"end": store.checkout}))
    assert route.order[-1] == "end"


async def test_batched_merges_shared_skus(services, store):
    maya = make_request(store)
    sam_stops = [
        s.model_copy(update={"stop_id": f"sam:{s.stop_id}", "user_ids": ["sam"], "item_ids": [f"sam_{s.stop_id}"]})
        for s in maya.stops
        if s.slot_id in ("s_salsa", "s_cookies")
    ]
    sam = RouteRequest(store_id=store.store_id, start=store.entrance, stops=sam_stops, user_ids=["sam"])
    merged = merge_requests([maya, sam])
    salsa = next(s for s in merged.stops if s.slot_id == "s_salsa")
    assert salsa.qty == 2 and salsa.user_ids == ["maya", "sam"]
    cookies = next(s for s in merged.stops if s.slot_id == "s_cookies")
    assert cookies.kind == "optional" and cookies.interest == pytest.approx(0.6)
    route = await create_fake(services).plan_batched([maya, sam])
    assert "stop_s_cookies" in route.visited_optional or "stop_s_cookies" in route.skipped_optional
    assert merged.user_ids == ["maya", "sam"]


async def test_grid_spec(services):
    spec = await create_fake(services).grid_spec("store_test")
    assert (spec.width, spec.height) == (280, 440)
    with pytest.raises(KeyError):
        await create_fake(services).grid_spec("online")


async def test_real_data_store_ave():
    """Demo store from data/ (skipped until the data agent has written it)."""
    from utils.data import DataError, load_planogram

    try:
        plan = load_planogram("store_ave")
    except DataError:
        pytest.skip("data/planograms/store_ave.yaml not written yet")
    from contracts.routing import Stop

    stops = [
        Stop(stop_id=f"stop_{s.slot_id}", kind="mandatory", pose=s.approach_pose, slot_id=s.slot_id, skus=[s.sku])
        for s in plan.slots[:8]
    ]
    req = RouteRequest(store_id="store_ave", start=plan.start_pose, stops=stops)
    route = await create_fake(Services()).plan_route(req)
    assert len(route.order) == len(stops) + 2
    assert route.stop("end").pose == plan.checkout
