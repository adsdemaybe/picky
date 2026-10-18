"""v0.2.0 review round: event envelope + filters, request replies / budget split, goal <-> trip mapping, goal
requests, transitions, strict tool budgets, brain tool results, lenient sim parsing, new sim commands, reach."""

import json

import pytest
from pydantic import ValidationError

from contracts.api import ApprovalDecisionRequest, CreateGoalRequest, CreateTripRequest, WsChat, parse_client_message
from contracts.catalog import STAFF_STOCK_REASONS, StockAlert, StockLevel
from contracts.common import (
    ARM_MOUNT_X_M,
    ARM_REACH_M,
    BATTERY_MOTION_MIN_PCT,
    STAFF_USER_ID,
    Point3,
    Pose2D,
    slot_reachable,
)
from contracts.events import WS_FILTER_KEYS, event_matches, goal_envelope, make_event, parse_event, trip_envelope
from contracts.fleet import (
    BrainStep,
    FleetRequest,
    Goal,
    GoalItem,
    GoalSpec,
    GoalStatus,
    GoalUpdate,
    RequestDraft,
    RequestPlan,
    RobotInfo,
    StoreChoice,
    can_goal_transition,
    goal_items_from_trip,
    score_robot,
    split_budget,
)
from contracts.perception import ScanReport
from contracts.planogram import Planogram, RobotSpec, StoreCameraSpec
from contracts.routing import Route, RouteLeg, Stop
from contracts.shopping import Cart, ListItem, Recommendation, ShoppingList
from contracts.sim import (
    MOTION_COMMANDS,
    CameraFrame,
    ResetCmd,
    SetSlotStockCmd,
    SimHosted,
    UnloadBasketCmd,
    command_error_result,
    parse_gateway_to_sim,
    parse_sim_message,
    planogram_hash,
)
from contracts.tools import (
    BRAIN_STRICT_TOOLS,
    STRICT_TOOL_LIMITS,
    AskUserResult,
    BrainToolError,
    PickResult,
    RouteSummary,
    brain_tool_definitions,
    parse_brain_tool_input,
    strict_schema_budget,
    tool_definitions,
)
from contracts.trip import Trip
from contracts.trust import ApprovalKind, ApprovalRequest


def _shop_goal(**kw) -> Goal:
    base = dict(goal_id="g1", kind="shop", store_id="store_ave", robot_id="rover-ave-1", title="Tacos",
                trip_id="t1", user_id="maya", request_id="r1")  # fmt: skip
    base.update(kw)
    return Goal(**base)


# ------------------------------------------------------------------------------------------- event envelope + filters
@pytest.mark.parametrize(
    "type_, payload",
    [
        ("goal.updated", {}),
        ("brain.step", {"step": BrainStep(step_id="s", robot_id="rover-ave-1", kind="thought", text="hm")}),
        ("robot.arrived", {"pose": Pose2D(x=1, y=1), "goal_id": "g1", "stop_id": "s1"}),
        ("item.picked", {"sku": "avocado", "goal_id": "g1"}),
    ],
)
def test_shop_goal_events_carry_the_goal_trip_id(type_, payload):
    goal = _shop_goal()
    if type_ == "goal.updated":
        payload = {"goal": goal}
    ev = make_event(type_, **goal_envelope(goal), source="agent", **payload)
    assert (ev.trip_id, ev.store_id, ev.robot_id, ev.user_id, ev.request_id) == (
        "t1",
        "store_ave",
        "rover-ave-1",
        "maya",
        "r1",
    )
    assert event_matches(ev, {"trip_id": "t1"}) and event_matches(ev, {"trip_id": ["t0", "t1"], "user_id": "maya"})
    assert not event_matches(ev, {"trip_id": "t1", "robot_id": "rover-ave-2"})
    assert parse_event(ev.model_dump_json()) == ev


def test_event_filters_and_trip_envelope():
    trip = Trip(trip_id="t2", user_id="maya", store_id="store_hill", cart=Cart(cart_id="c"), request_id="r1",
                robot_id="rover-hill-1")  # fmt: skip
    ev = make_event("trip.state_changed", **trip_envelope(trip), to_state="executing")
    assert ev.request_id == "r1" and ev.robot_id == "rover-hill-1"
    assert event_matches(ev, {}) and event_matches(ev, {"request_id": ["r1"]})
    stock = make_event("error", trip_id=None, store_id="store_ave", arm="catalog", message="x")
    assert not event_matches(stock, {"trip_id": "t2"})  # null never matches
    assert event_matches(stock, {"trip_id": [], "store_id": "store_ave"})  # empty filter = no filter
    assert set(WS_FILTER_KEYS) == {"trip_id", "store_id", "robot_id", "user_id", "request_id"}
    req = make_event("request.received", trip_id=None, user_id="maya", request_id="r9",
                     request=FleetRequest(text="tacos", user_id="maya"))  # fmt: skip
    assert event_matches(req, {"request_id": "r9", "user_id": "maya"})


# -------------------------------------------------------------------------------------- requests, budgets, chat
def test_request_reply_budget_split_and_plan_totals():
    reply = FleetRequest(text="Hill", user_id="maya", in_reply_to="r1")
    assert reply.in_reply_to == "r1" and FleetRequest(text="hi", user_id="maya").in_reply_to is None
    shares = split_budget(4000, {"store_ave": 2600, "store_hill": 700})
    assert sum(shares.values()) == 4000 and shares["store_ave"] > shares["store_hill"]
    assert split_budget(1001, {"a": 0, "b": 0}) == {"a": 501, "b": 500}
    assert split_budget(500, {}) == {}
    plan = RequestPlan(
        request_id="r1",
        text="tacos",
        user_id="maya",
        interpretation=RequestDraft(intent="shop", summary="tacos",
                                    shopping_list=ShoppingList(list_id="L", user_id="maya", budget_cents=4000)),
        store_choices=[StoreChoice(store_id="store_ave", chosen=True, est_total_cents=2600),
                       StoreChoice(store_id="store_hill", chosen=True, est_total_cents=700),
                       StoreChoice(store_id="x", chosen=False, est_total_cents=9999)],
        budget_split=shares,
    )  # fmt: skip
    dumped = json.loads(plan.model_dump_json())
    assert dumped["est_total_cents"] == 3300 and dumped["budget_cents"] == 4000 and dumped["split"] is True
    assert RequestPlan.model_validate(dumped) == plan
    chat = parse_client_message({"type": "chat", "text": "add limes", "trip_id": "t2"})
    assert isinstance(chat, WsChat) and chat.trip_id == "t2"
    assert ApprovalDecisionRequest(decision="approve", note="thanks").note == "thanks"
    assert CreateTripRequest(user_id="maya", text="x").auto_execute is False
    assert Trip(trip_id="t", user_id="u", store_id="s", cart=Cart(cart_id="c")).auto_execute is False


# --------------------------------------------------------------------------------------------- goals
def test_goal_request_vs_spec_and_item_defaults():
    body = CreateGoalRequest(kind="shelf_scan", categories=["dairy"])  # no store / robot: the path pins it
    spec = body.to_spec("rover-ave-1", user_id=STAFF_USER_ID)
    assert isinstance(spec, GoalSpec) and spec.robot_id == "rover-ave-1" and spec.user_id == "staff"
    with pytest.raises(ValidationError, match="store_id or robot_id"):
        GoalSpec(kind="shelf_scan", categories=["dairy"])
    with pytest.raises(ValidationError):
        CreateGoalRequest(kind="fetch")  # still needs exactly one item
    a, b = GoalItem(query="limes"), GoalItem(query="limes")
    assert a.item_id.startswith("item_") and a.item_id != b.item_id
    fetch = CreateGoalRequest(kind="fetch", items=[{"query": "salsa"}]).to_spec("rover-ave-2")
    assert fetch.items[0].item_id.startswith("item_")
    upd = GoalUpdate(add_items=[GoalItem(query="limes", qty=2)], items=[{"item_id": "s1", "status": "failed"}])
    assert upd.add_items[0].qty == 2 and upd.items[0].status == "failed"


def test_goal_transitions_same_status_and_reassign_from_waiting():
    for st in (GoalStatus.PENDING, GoalStatus.ASSIGNED, GoalStatus.ACTIVE, GoalStatus.WAITING):
        assert can_goal_transition(st, st)
    assert can_goal_transition(GoalStatus.WAITING, GoalStatus.PENDING)
    assert can_goal_transition(GoalStatus.WAITING, GoalStatus.ASSIGNED)
    assert not can_goal_transition(GoalStatus.CANCELLED, GoalStatus.CANCELLED)


def test_queued_robot_is_not_idle():
    base = dict(robot_id="rover-ave-1", name="Pico", store_id="store_ave", dock=Pose2D(x=1, y=1), connected=True,
                status="idle", available=True, battery_pct=90)  # fmt: skip
    free = score_robot(RobotInfo(**base), store_id="store_ave")
    queued = score_robot(RobotInfo(**base, queued_goal_ids=["g9"]), store_id="store_ave")
    assert free.breakdown["idle"] == 1.0 and queued.breakdown["idle"] == 0.25 and free.score > queued.score


def _trip_with_route() -> Trip:
    stops = [
        Stop(stop_id="start", kind="start", pose=Pose2D(x=0, y=0)),
        Stop(stop_id="s_avo", kind="mandatory", pose=Pose2D(x=2, y=1), slot_id="AVE-P-00", skus=["avocado"], qty=3,
             item_ids=["i_avo"], label="Avocado"),
        Stop(stop_id="s_salsa", kind="mandatory", pose=Pose2D(x=4, y=1), slot_id="AVE-G2-03", skus=["salsa_mild"],
             item_ids=["i_salsa"]),
        Stop(stop_id="s_rec", kind="optional", pose=Pose2D(x=5, y=2), slot_id="AVE-G3-01", skus=["hot_sauce"],
             rec_id="rec1"),
        Stop(stop_id="s_skip", kind="optional", pose=Pose2D(x=9, y=9), slot_id="AVE-G9-01", skus=["cookies"],
             rec_id="rec2"),
        Stop(stop_id="end", kind="end", pose=Pose2D(x=6, y=0)),
    ]  # fmt: skip
    leg = RouteLeg(from_stop_id="start", to_stop_id="s_salsa", waypoints=[], distance_m=4.1)
    order = ["start", "s_salsa", "s_avo", "s_rec", "end"]
    route = Route(route_id="r", store_id="store_ave", stops=stops, order=order, skipped_optional=["s_skip"], legs=[leg])
    sl = ShoppingList(list_id="L", user_id="maya", items=[
        ListItem(item_id="i_avo", query="avocados", qty=3, category="produce", est_line_cents=387),
        ListItem(item_id="i_salsa", query="mild salsa", substitutes=["salsa_medium"], est_line_cents=349),
    ])  # fmt: skip
    rec = Recommendation(rec_id="rec1", user_id="maya", sku="hot_sauce", store_id="store_ave", interest=0.7,
                         reason="you like spicy", price_cents=299, product_name="Hot Sauce")  # fmt: skip
    return Trip(trip_id="t1", user_id="maya", store_id="store_ave", cart=Cart(cart_id="c"), shopping_list=sl,
                recs=[rec], route=route, stop_status={"s_salsa": "done"})  # fmt: skip


def test_goal_items_from_trip():
    items = goal_items_from_trip(_trip_with_route())
    assert [i.item_id for i in items] == ["s_salsa", "s_avo", "s_rec", "s_skip"]
    salsa, avo, rec, skip = items
    assert salsa.status == "in_basket" and salsa.picked_qty == 1 and salsa.substitutes == ["salsa_medium"]
    assert avo.stop_id == "s_avo" and avo.qty == 3 and avo.category == "produce" and avo.price_cents == 129
    assert avo.query == "avocados" and avo.list_item_id == "i_avo" and not avo.optional
    assert rec.optional and rec.rec_id == "rec1" and rec.price_cents == 299 and rec.name == "Hot Sauce"
    assert skip.status == "skipped"
    assert goal_items_from_trip(Trip(trip_id="t", user_id="u", store_id="s", cart=Cart(cart_id="c"))) == []
    summary = RouteSummary.from_route(_trip_with_route().route)
    assert [s.stop_id for s in summary.stops] == ["start", "s_salsa", "s_avo", "s_rec", "end"]
    assert summary.stops[1].distance_from_prev_m == 4.1 and "waypoints" not in summary.model_dump_json()


# ------------------------------------------------------------------------------------- brain tools: strict budgets
def test_strict_tool_budgets_fit_the_api_limits():
    for defs in (brain_tool_definitions(strict=True), tool_definitions(strict=True)):
        budget = strict_schema_budget(defs)
        for key, limit in STRICT_TOOL_LIMITS.items():
            assert budget[key] <= limit, (key, budget)
    brain = brain_tool_definitions(strict=True)
    assert {t["name"] for t in brain if t.get("strict")} == set(BRAIN_STRICT_TOOLS)
    assert all("strict" not in t for t in brain_tool_definitions())


def test_brain_tool_inputs_and_results():
    pick = parse_brain_tool_input("pick_item", {"slot_id": "s", "sku": "k", "item_id": "s_avo", "approval_id": "a1"})
    assert pick.approval_id == "a1"
    ask = parse_brain_tool_input("ask_user", {"kind": "over_threshold", "question": "OK?", "item_id": "s_avo"})
    assert ask.item_id == "s_avo"
    add = parse_brain_tool_input("add_item", {"query": "limes", "qty": 2})
    assert add.qty == 2 and add.sku is None
    with pytest.raises(ValidationError):
        parse_brain_tool_input("add_item", {"query": "limes", "qty": 0})
    assert parse_brain_tool_input("update_progress", {"note": "x", "item_status": "failed"}).item_status == "failed"
    assert PickResult(picked=False, needs_approval=True).needs_approval
    res = AskUserResult(approval_id="a", decision="choice", approved=True, choice="AVE-P-00#03",
                        options=[{"option_id": "AVE-P-00#03", "label": "Freshest"}])  # fmt: skip
    assert res.options[0].option_id == res.choice
    err = BrainToolError(error_code="path_blocked", error="rover in the way", retryable=True)
    assert json.loads(err.model_dump_json())["ok"] is False


# ------------------------------------------------------------------------------------- sim wire
def test_lenient_parse_drops_nested_unknown_fields():
    pico = RobotSpec(robot_id="rover-ave-1", name="Pico", store_id="store_ave", dock=Pose2D(x=1, y=1))
    cam = StoreCameraSpec(camera_id="store_ave.ceiling-1", store_id="store_ave", name="c", mount={"frame": "world"})
    raw = ResetCmd(cmd_id="c1", store_id="store_ave", robots=[pico], cameras=[cam]).model_dump(mode="json")
    raw["robots"][0]["future_field"] = 1
    raw["robots"][0]["dock"]["z"] = 0.0
    raw["cameras"][0]["mount"]["roll"] = 0.1
    parsed = parse_gateway_to_sim(json.dumps(raw))
    assert isinstance(parsed, ResetCmd) and parsed.robots[0].name == "Pico" and parsed.cameras[0].mount.frame == "world"
    msg = {"type": "result", "cmd_id": "c2", "ok": True,
           "frame": {"type": "camera_frame", "frame_id": "f", "jpeg_b64": "AA", "w": 1, "h": 1,
                     "pose": {"x": 1, "y": 2, "yaw_rate": 0}, "instances": [
                         {"instance_id": "AVE-P-00#00", "sku": "avocado", "position": {"x": 1, "y": 1, "z": 1,
                                                                                       "w": 9}}]}}  # fmt: skip
    res = parse_sim_message(msg)
    assert res.frame.pose.y == 2 and res.frame.instances[0].position.z == 1
    with pytest.raises(ValidationError):  # real errors still raise
        parse_gateway_to_sim({"type": "navigate", "cmd_id": "c3", "waypoints": []})
    err = command_error_result('{"type": "navigate", "cmd_id": "c3", "robot_id": "rover-ave-1"}', ValueError("bad"))
    assert err.cmd_id == "c3" and not err.ok and err.error_code == "internal" and err.robot_id == "rover-ave-1"
    assert command_error_result("not json", ValueError("x")) is None


def test_new_sim_messages():
    hosted = parse_sim_message({"type": "hosted", "stores": [{"store_id": "store_ave", "data_hash": "h"}],
                                "robots": [{"robot_id": "rover-ave-1", "store_id": "store_ave"}]})  # fmt: skip
    assert isinstance(hosted, SimHosted) and hosted.store_ids() == ["store_ave"] and hosted.robot_ids()
    unload = parse_gateway_to_sim({"type": "unload_basket", "cmd_id": "u", "robot_id": "rover-ave-1"})
    assert isinstance(unload, UnloadBasketCmd) and "unload_basket" in MOTION_COMMANDS
    sss = parse_gateway_to_sim({"type": "set_slot_stock", "cmd_id": "s", "store_id": "store_ave", "slot_id": "AVE-1",
                                "sku": "avocado", "qty": 6, "lots": [{"lot_id": "L1", "qty": 6}]})  # fmt: skip
    assert isinstance(sss, SetSlotStockCmd) and "set_slot_stock" not in MOTION_COMMANDS
    frame = CameraFrame(frame_id="f", jpeg_b64="AA", w=1, h=1, camera_pose={"frame": "world", "z": 1.45})
    assert parse_sim_message(frame.model_dump_json()).camera_pose.z == 1.45


def test_planogram_hash_ignores_low_threshold():
    def plan(low: int | None) -> Planogram:
        return Planogram.model_validate({
            "store_id": "s", "name": "S", "bounds": {"x": 0, "y": 0, "w": 10, "h": 10},
            "entrance": {"x": 1, "y": 1}, "checkout": {"x": 2, "y": 1},
            "fixtures": [{"fixture_id": "f", "rect": {"x": 4, "y": 4, "w": 1, "h": 2}}],
            "slots": [{"slot_id": "s1", "fixture_id": "f", "sku": "a", "item_position": {"x": 5, "y": 5},
                       "approach_pose": {"x": 5.38, "y": 5, "theta": 3.14159}, "low_threshold": low}],
        })  # fmt: skip

    assert planogram_hash(plan(None)) == planogram_hash(plan(4))


# ------------------------------------------------------------------------------------- reach, stock, approvals
def test_slot_reachable():
    approach = Pose2D(x=1.0, y=1.0, theta=0.0)
    item = Point3(x=1.38, y=1.0, z=0.8)
    assert slot_reachable(approach, approach, item)
    assert slot_reachable(Pose2D(x=1.05, y=1.0), approach, item)  # within nav tolerance
    assert not slot_reachable(Pose2D(x=1.5, y=1.0), approach, item)  # robot not at the approach pose
    far = Point3(x=1.0 + ARM_MOUNT_X_M + ARM_REACH_M + 0.05, y=1.0, z=0.8)
    assert not slot_reachable(approach, approach, far)
    assert not slot_reachable(approach, approach, Point3(x=1.38, y=1.0, z=1.7))
    assert not slot_reachable(approach, approach, Point3(x=1.38, y=1.0, z=0.05))
    assert 0 < BATTERY_MOTION_MIN_PCT < 20


def test_stock_display_fields_lot_counts_and_approvals():
    lvl = StockLevel(store_id="s", sku="a", name="A", category="dairy", qty=1, capacity=8, low_threshold=2,
                     size="16 oz")  # fmt: skip
    assert lvl.unit == "each" and lvl.size == "16 oz" and lvl.image_url is None
    alert = StockAlert(alert_id="s:a", store_id="s", sku="a", name="A", status="low", qty=1, low_threshold=2,
                       capacity=8, unit="pack")  # fmt: skip
    assert alert.unit == "pack"
    assert set(STAFF_STOCK_REASONS) == {"damage", "manual"}
    rep = ScanReport(scan_id="x", store_id="s", observed_count=5, count_is_exact=True, lot_counts={"L1": 2, "L2": 3})
    assert sum(rep.lot_counts.values()) == rep.observed_count
    ask = ApprovalRequest(approval_id="a", user_id=STAFF_USER_ID, kind=ApprovalKind.QUESTION, title="Restock dairy?",
                          goal_id="g1", robot_id="rover-ave-1", store_id="store_ave")  # fmt: skip
    assert ask.store_id == "store_ave" and ask.trip_id is None
