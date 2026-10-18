"""Tests for the review-round additions: computed fields, wire tolerance, error codes, trip controller, helpers."""

import json
import math

import pytest
from pydantic import ValidationError

import contracts
from contracts.api import AddLineResult
from contracts.catalog import Mispricing, StoreOffer, online_shipping_cents
from contracts.common import ROBOT_RADIUS_M, BBox, Point3, Pose2D, Rect2D, angle_diff
from contracts.events import (
    EVENT_CLASSES,
    EVENT_PRODUCERS,
    EVENT_TYPES,
    LIVE_ONLY_EVENT_TYPES,
    RecsReady,
    event_json_schema,
    make_event,
    parse_event,
)
from contracts.messaging import OutboundMessage, normalize_handle
from contracts.perception import PICK_CHOICE_MAX, FreshnessCandidate, PickChoice
from contracts.planogram import Planogram
from contracts.routing import GridSpec, Route, Stop
from contracts.services import GATEWAY_PROTOCOLS, Services, TripController
from contracts.shopping import Cart, CartLine, Recommendation, ShoppingList, TripState, can_transition
from contracts.sim import (
    CameraFrame,
    CommandResult,
    ItemInstance,
    NavigateCmd,
    ResetCmd,
    RobotError,
    default_timeout_s,
    parse_gateway_to_sim,
    parse_sim_message,
    planogram_hash,
    versions_compatible,
)
from contracts.tools import TOOL_DEFINITIONS, parse_tool_input, tool_definitions
from contracts.trip import Trip
from contracts.trust import (
    ApprovalResponse,
    MandateDecision,
    MandateReason,
    MandateVerdict,
    MandateViolation,
    TransparencyEntry,
    compute_cart_hash,
)


def _cart() -> Cart:
    return Cart(
        cart_id="c",
        trip_id="t1",
        lines=[
            CartLine(line_id="l1", sku="a", qty=2, unit_price_cents=300, store_id="store_ave", slot_id="s1"),
            CartLine(line_id="l2", sku="b", qty=1, unit_price_cents=1000, source="online", store_id="online"),
        ],
        shipping_cents=599,
    )


def test_computed_fields_in_json_and_roundtrip():
    cart = _cart()
    dumped = json.loads(cart.model_dump_json())
    assert dumped["total_cents"] == 600 + 1000 + 599
    assert dumped["lines"][0]["total_cents"] == 600
    assert Cart.model_validate(dumped) == cart
    assert Cart.model_validate_json(cart.model_dump_json()) == cart
    trip = Trip(
        trip_id="t1",
        user_id="maya",
        store_id="store_ave",
        cart=cart,
        shopping_list=ShoppingList(list_id="L", user_id="maya", budget_cents=4000),
    )
    tj = json.loads(trip.model_dump_json())
    assert tj["budget_cents"] == 4000 and tj["cart"]["total_cents"] == 2199
    assert tj["shopping_list"]["est_total_cents"] == 0
    assert Trip.model_validate(tj) == trip
    route = Route(route_id="r", store_id="s", stops=[], order=[], total_distance_m=30, baseline_distance_m=18)
    assert json.loads(route.model_dump_json())["extra_distance_m"] == 12
    assert json.loads(StoreOffer(store_id="s", sku="a", price_cents=1, qty=2).model_dump_json())["in_stock"]
    mp = Mispricing(sku="a", name="A", slot_id="s1", system_price_cents=399, shelf_tag_cents=349)
    assert json.loads(mp.model_dump_json())["delta_cents"] == -50
    with pytest.raises(ValidationError):  # still strict about genuinely unknown fields
        Cart.model_validate({**cart.model_dump(), "bogus": 1})


def test_event_schema_marks_envelope_required():
    schema = event_json_schema()
    for cls in EVENT_CLASSES:
        required = schema["$defs"][cls.__name__]["required"]
        for field in ("type", "trip_id", "ts", "seq"):
            assert field in required, (cls.__name__, field)
    assert "total_cents" in schema["$defs"]["Cart"]["required"]


def test_event_registry_and_producers():
    assert set(EVENT_PRODUCERS) == set(EVENT_TYPES)
    assert EVENT_PRODUCERS["agent.message"] == "agent" and EVENT_PRODUCERS["route.planned"] == "gateway"
    assert EVENT_PRODUCERS["goal.assigned"] == "fleet" and EVENT_PRODUCERS["brain.step"] == "agent"
    assert EVENT_PRODUCERS["stock.alert"] == "catalog" and EVENT_PRODUCERS["error"] == "any"
    assert LIVE_ONLY_EVENT_TYPES == {"frame", "camera.frame"}
    recs = RecsReady(trip_id="t", recs=[], checkout_recs=[])
    assert isinstance(parse_event(recs.model_dump_json()), RecsReady)
    ev = make_event("stop.updated", trip_id="t", stop_id="s1", status="declined", rec_id="r1")
    assert parse_event(ev.model_dump()) == ev
    with pytest.raises(ValidationError):
        make_event("stop.updated", trip_id="t", stop_id="s1", status="maybe")


def test_wire_models_ignore_unknown_fields():
    msg = parse_sim_message({"type": "robot_state", "pose": {"x": 1, "y": 2}, "from_newer_worker": 3})
    assert msg.pose.x == 1
    cmd = parse_gateway_to_sim({"type": "navigate", "cmd_id": "c", "waypoints": [{"x": 0, "y": 0}], "new": True})
    assert isinstance(cmd, NavigateCmd) and cmd.yaw_tolerance_rad == pytest.approx(0.05)
    assert versions_compatible("0.2.0", "0.2.7") and not versions_compatible("0.1.0", "0.2.0")
    assert not versions_compatible(None, "0.1.0")


def test_frames_carry_ground_truth_and_errors_are_coded():
    inst = ItemInstance(
        instance_id="i1", sku="avocado", position=Point3(x=1, y=2, z=0.9), lot_id="A1", bbox=BBox(x=1, y=2, w=3, h=4)
    )
    frame = CameraFrame(frame_id="f", jpeg_b64="AAAA", w=640, h=480, instances=[inst])
    res = CommandResult(cmd_id="c1", ok=True, frame=frame)
    again = parse_sim_message(res.model_dump_json())
    assert again.frame.instances[0].bbox.w == 3 and again.frame.instances[0].lot_id == "A1"
    failed = CommandResult(cmd_id="c2", ok=False, error_code="busy")
    assert parse_sim_message(failed.model_dump())
    with pytest.raises(ValidationError):
        CommandResult(cmd_id="c3", ok=False, error_code="oops")
    err = RobotError("grasp_failed", "slipped")
    assert err.code == "grasp_failed" and "slipped" in str(err)
    assert default_timeout_s(ResetCmd(cmd_id="r")) == 120.0
    assert default_timeout_s(NavigateCmd(cmd_id="n", waypoints=[Pose2D(x=0, y=0)])) == 90.0


def _plan(qty: int) -> Planogram:
    return Planogram.model_validate(
        {
            "store_id": "s",
            "name": "S",
            "bounds": {"x": 0, "y": 0, "w": 10, "h": 10},
            "entrance": {"x": 1, "y": 1},
            "checkout": {"x": 2, "y": 1},
            "fixtures": [{"fixture_id": "f", "rect": {"x": 4, "y": 4, "w": 1, "h": 2}}],
            "slots": [
                {
                    "slot_id": "s1",
                    "fixture_id": "f",
                    "sku": "a",
                    "item_position": {"x": 5, "y": 5},
                    "approach_pose": {"x": 5.8, "y": 5, "theta": math.pi},
                    "qty": qty,
                }
            ],
        }
    )


def test_planogram_hash_ignores_stock_and_reset_can_push_it():
    assert planogram_hash(_plan(3)) == planogram_hash(_plan(0))
    moved = _plan(3)
    moved.slots[0].approach_pose.x = 6.0
    assert planogram_hash(moved) != planogram_hash(_plan(3))
    reset = ResetCmd(cmd_id="r", store_id="s", planogram=_plan(3))
    assert parse_gateway_to_sim(reset.model_dump_json()).planogram.slot("s1").qty == 3


def test_geometry_helpers_and_robot_radius():
    assert GridSpec(store_id="s", width=1, height=1).robot_radius_m == ROBOT_RADIUS_M == 0.3
    r = Rect2D(x=0, y=0, w=2, h=1)
    assert r.distance_to(1, 0.5) == 0 and r.distance_to(3, 1) == pytest.approx(1.0)
    assert r.inflated(0.5).contains(-0.4, 1.4)
    assert angle_diff(math.pi - 0.1, -math.pi + 0.1) == pytest.approx(-0.2)


def test_stop_detour_fields_and_recommendation_copy():
    stop = Stop(stop_id="s", kind="optional", pose=Pose2D(x=1, y=1), detour_m=12.0, detour_s=25.0, qty=2)
    rec = Recommendation(
        rec_id="r", user_id="maya", trip_id="t", sku="hot_sauce", store_id="store_ave", interest=0.8, reason="spicy"
    )
    rec = rec.model_copy(update={"extra_distance_m": stop.detour_m, "extra_time_s": stop.detour_s})
    assert rec.extra_distance_m == 12.0
    entry = TransparencyEntry.from_recommendation(rec)
    assert entry.entry_id == "r" and entry.trip_id == "t" and not entry.sponsored


def test_trip_transitions_allow_pre_trip_edits():
    for src in (TripState.LIST_READY, TripState.RECS_READY, TripState.ROUTE_PLANNED, TripState.AWAITING_LOGIN):
        assert can_transition(src, TripState.LIST_READY)
    assert can_transition(TripState.AWAITING_LOGIN, TripState.ROUTE_PLANNED)
    assert not can_transition(TripState.EXECUTING, TripState.LIST_READY)


def test_mandate_violations_and_multi_choice():
    d = MandateDecision(
        decision_id="d",
        user_id="maya",
        verdict=MandateVerdict.NEEDS_APPROVAL,
        reasons=[MandateReason.OVER_BUDGET],
        violations=[MandateViolation(reason=MandateReason.OVER_BUDGET, message="over your $40 trip budget")],
        budget_cents=4000,
    )
    assert json.loads(d.model_dump_json())["violations"][0]["reason"] == "over_budget"
    resp = ApprovalResponse(approval_id="a", decision="choice", choice="r1", choices=["r1", "r3"])
    assert resp.approved and resp.chosen() == ["r1", "r3"]
    assert ApprovalResponse.model_validate_json(resp.model_dump_json()) == resp
    assert AddLineResult(added=False, decision=d, cart=_cart()).cart.total_cents == 2199


def test_tools_inputs_and_strict_definitions():
    add = parse_tool_input("add_to_cart", {"sku": "sour_cream", "source": "online"})
    assert add.store_id == "online"
    with pytest.raises(ValidationError):
        parse_tool_input("add_to_cart", {"sku": "x", "store_id": "online"})
    assert parse_tool_input("query_inventory", {"query": "protein", "dietary": ["vegetarian"]}).dietary
    assert parse_tool_input("request_approval", {"kind": "recommendation", "title": "Add?", "rec_id": "r1"}).rec_id
    with pytest.raises(ValidationError):
        parse_tool_input("request_approval", {"kind": "checkout", "title": "Pay?"})
    strict = tool_definitions(strict=True)
    assert [t["name"] for t in strict] == [t["name"] for t in TOOL_DEFINITIONS]
    blob = json.dumps(strict)
    assert '"minimum"' not in blob and '"maximum"' not in blob
    assert all(t["strict"] is True and "title" not in t["input_schema"] for t in strict)


def test_messaging_helpers():
    assert normalize_handle("(555) 555-0100") == "+15555550100"
    assert normalize_handle("+1 555 555 0100") == "+15555550100"
    assert normalize_handle(" Maya@Example.com ") == "maya@example.com"
    assert normalize_handle("15555550100") == "+15555550100"
    with pytest.raises(ValidationError, match="does not match store_id"):
        CartLine(line_id="x", sku="a", unit_price_cents=1, source="online", store_id="store_ave")
    msg = OutboundMessage(channel="photon", text="How was the salsa?", about_skus=["salsa_mild"])
    assert OutboundMessage.model_validate_json(msg.model_dump_json()).about_skus == ["salsa_mild"]


def test_shipping_rule():
    assert online_shipping_cents(0, 599, 3500) == 0
    assert online_shipping_cents(1000, 599, 3500) == 599
    assert online_shipping_cents(3500, 599, 3500) == 0
    assert online_shipping_cents(1000, 599, None) == 599


def test_pick_choice_cap():
    cands = [FreshnessCandidate(instance_id=f"i{i}", sku="a", score=0.5, rank=i + 1) for i in range(4)]
    assert PickChoice(choice_id="p", slot_id="s", sku="a", candidates=cands[:PICK_CHOICE_MAX])
    with pytest.raises(ValidationError):
        PickChoice(choice_id="p", slot_id="s", sku="a", candidates=cands)


class DummyTrips:
    async def get_trip(self, trip_id): ...
    async def create_trip(self, request): ...
    async def assign_robot(self, trip_id, robot_id, goal_id=None): ...
    async def set_stop_status(self, trip_id, stop_id, status, reason=""): ...
    async def check_line(self, trip_id, line): ...
    async def ask(self, trip_id, request): ...
    async def update_list(self, trip_id, shopping_list): ...
    async def add_line(self, trip_id, line, approval_id=None): ...
    async def remove_line(self, trip_id, line_id): ...
    async def replan(self, trip_id, include_recommendations=True, max_optional_stops=None): ...
    async def start_checkout(self, trip_id, note=None): ...


def test_trip_controller_wiring_and_exports():
    assert isinstance(DummyTrips(), TripController)
    services = Services(trips=DummyTrips())
    assert services.require("trips") is services.trips
    assert set(GATEWAY_PROTOCOLS) <= set(Services.field_names())
    for name in ("TrustLedger", "AuditEntry", "SERVICE_PROTOCOLS", "TripController", "RobotError", "WireModel"):
        assert hasattr(contracts, name) and name in contracts.__all__
    assert compute_cart_hash(_cart(), "t1") == compute_cart_hash(
        Cart.model_validate_json(_cart().model_dump_json()), "t1"
    )
