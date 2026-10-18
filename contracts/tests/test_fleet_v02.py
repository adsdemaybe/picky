"""v0.2.0 additions: fleet (requests, goals, assignment rule, brain trace), cameras, categories + stock, multi-robot
sim wire protocol, brain tools, new events and Protocols."""

import json
import math

import pytest
from pydantic import ValidationError

import contracts
from contracts.api import ObserveRequest, WsRobotChat, parse_client_message
from contracts.cameras import (
    DEFAULT_ROBOT_CAMERA_MOUNTS,
    ROBOT_CAMERA_KINDS,
    CameraFrame,
    CameraInfo,
    VisionObservation,
    parse_camera_id,
    robot_camera_id,
    store_camera_id,
)
from contracts.catalog import (
    STANDARD_CATEGORY_IDS,
    StockAdjustment,
    StockLevel,
    default_low_threshold,
    stock_status,
)
from contracts.common import (
    APPROACH_DIST_MAX_M,
    APPROACH_DIST_MIN_M,
    ARM_MOUNT_X_M,
    ARM_REACH_M,
    LIFT_MAX_M,
    LIFT_MIN_M,
    REACH_Z_MAX_M,
    REACH_Z_MIN_M,
    ROBOT_RADIUS_M,
    Pose2D,
    normalize_camera,
)
from contracts.events import (
    EVENT_PRODUCERS,
    EVENT_TYPES,
    BrainStepEvent,
    CameraFrameEvent,
    GoalAssigned,
    StockAlertEvent,
    make_event,
    parse_event,
)
from contracts.fleet import (
    BrainStep,
    Goal,
    GoalItem,
    GoalSpec,
    GoalStatus,
    RequestPlan,
    RobotInfo,
    StoreChoice,
    can_goal_transition,
    choose_robot,
    robot_info_from,
    score_robot,
)
from contracts.planogram import CategoriesFile, RobotsFile, RobotSpec, StoreCameraSpec
from contracts.services import (
    EXTRA_SERVICE_PROTOCOLS,
    SERVICE_FACTORIES,
    SERVICE_PROTOCOLS,
    CameraService,
    CatalogService,
    FleetService,
    RobotBrainService,
    Services,
)
from contracts.sim import (
    BASKET_OFFSET_LOCAL,
    BASKET_SIZE_M,
    CaptureCmd,
    CommandResult,
    NavigateCmd,
    ResetCmd,
    RobotState,
    RobotStatus,
    SetStreamsCmd,
    SimHello,
    SimRobotInfo,
    SimStoreInfo,
    StopCmd,
    message_robot_id,
    parse_gateway_to_sim,
    parse_sim_message,
)
from contracts.tools import (
    BRAIN_TOOL_DEFINITIONS,
    BRAIN_TOOL_NAMES,
    TOOL_NAMES,
    brain_tool_definitions,
    parse_brain_tool_input,
)
from contracts.trust import ApprovalKind, ApprovalRequest

PICO = RobotSpec(robot_id="rover-ave-1", name="Pico", store_id="store_ave", dock=Pose2D(x=1, y=2), battery_pct=90)
RHEA = RobotSpec(robot_id="rover-ave-2", name="Rhea", store_id="store_ave", dock=Pose2D(x=2, y=2), battery_pct=60)
HAZEL = RobotSpec(robot_id="rover-hill-1", name="Hazel", store_id="store_hill", dock=Pose2D(x=1, y=1))


def _state(robot_id: str, x: float, *, status: RobotStatus = RobotStatus.IDLE, battery: float = 80) -> RobotState:
    pose = Pose2D(x=x, y=2)
    return RobotState(robot_id=robot_id, store_id="store_ave", pose=pose, status=status, battery_pct=battery)


# ---------------------------------------------------------------------------------------------------- robot model
def test_robot_model_constants_are_consistent():
    assert ROBOT_RADIUS_M == 0.3
    assert ROBOT_RADIUS_M <= APPROACH_DIST_MIN_M < APPROACH_DIST_MAX_M <= ARM_MOUNT_X_M + ARM_REACH_M
    assert LIFT_MIN_M < LIFT_MAX_M and REACH_Z_MIN_M < LIFT_MIN_M and REACH_Z_MAX_M > LIFT_MAX_M
    bx, by, bz = BASKET_OFFSET_LOCAL
    sx, sy, _ = BASKET_SIZE_M
    assert math.hypot(abs(bx) + sx / 2, sy / 2) <= ROBOT_RADIUS_M + 1e-9  # basket inside the footprint
    assert math.hypot(bx - ARM_MOUNT_X_M, by) <= ARM_REACH_M  # arm reaches the basket center
    assert LIFT_MIN_M <= bz + 0.2 <= LIFT_MAX_M
    assert normalize_camera("head") == "front" and normalize_camera("wrist") == "wrist"


# ---------------------------------------------------------------------------------------------------- cameras
def test_camera_ids_and_info():
    assert robot_camera_id("rover-ave-1", "front") == "rover-ave-1.front"
    assert robot_camera_id("rover-ave-1", "head") == "rover-ave-1.front"
    assert store_camera_id("store_ave", 2) == "store_ave.ceiling-2"
    assert parse_camera_id("rover-hill-1.basket") == ("rover-hill-1", "basket")
    assert parse_camera_id("store_ave.ceiling-2") == ("store_ave", "ceiling")
    for bad in ("nodot", "rover.lens", ".front"):
        with pytest.raises(ValueError):
            parse_camera_id(bad)
    assert set(DEFAULT_ROBOT_CAMERA_MOUNTS) == set(ROBOT_CAMERA_KINDS)
    info = CameraInfo(camera_id="rover-ave-1.wrist", kind="wrist", name="Pico · wrist", robot_id="rover-ave-1")
    dumped = json.loads(info.model_dump_json())
    assert dumped["snapshot_url"] == "/api/cameras/rover-ave-1.wrist/snapshot.jpg"
    assert dumped["stream_url"].endswith("/stream.mjpg")
    assert CameraInfo.model_validate(dumped) == info
    obs = VisionObservation(observation_id="o", camera_id=info.camera_id, answer="4 jars of salsa", people=1)
    assert obs.blurred and obs.method == "fake"
    assert CameraFrame.__name__ == "CameraFrame"  # lazy re-export from contracts.sim


# ------------------------------------------------------------------------------------------------ categories + stock
def test_stock_rules_and_models():
    assert default_low_threshold(12) == 3 and default_low_threshold(0) == 1 and default_low_threshold(5) == 2
    assert stock_status(0, 3) == "out" and stock_status(3, 3) == "low" and stock_status(4, 3) == "ok"
    lvl = StockLevel(store_id="store_ave", sku="SALSA-MILD", name="Mild Salsa", category="international", qty=2,
                     capacity=12, low_threshold=3)  # fmt: skip
    dumped = json.loads(lvl.model_dump_json())
    assert dumped["status"] == "low" and dumped["fill_ratio"] == pytest.approx(0.167)
    assert StockLevel.model_validate(dumped) == lvl
    assert StockAdjustment(sku="A", delta=-2, reason="damage").delta == -2
    assert StockAdjustment(sku="A", set_qty=0, reason="scan", actor="robot:rover-ave-1").set_qty == 0
    for bad in ({"sku": "A"}, {"sku": "A", "delta": 1, "set_qty": 2}, {"sku": "A", "delta": 0}):
        with pytest.raises(ValidationError):
            StockAdjustment(**bad)
    assert {"produce", "dairy", "international", "personal_care", "alcohol"} <= set(STANDARD_CATEGORY_IDS)


def test_categories_file_tree():
    cats = CategoriesFile.model_validate(
        {
            "categories": [
                {"category_id": "international", "name": "International & Mexican", "sort": 6},
                {"category_id": "produce", "name": "Produce", "sort": 0, "aliases": ["Fruit", "Veg"]},
                {"category_id": "salsa", "name": "Salsa", "parent_id": "international"},
            ]
        }
    )
    assert [c.category_id for c in cats.top_level()] == ["produce", "international"]
    assert [c.category_id for c in cats.children("international")] == ["salsa"]
    assert cats.get("produce").aliases == ["fruit", "veg"]
    with pytest.raises(ValidationError, match="unknown parent_id"):
        CategoriesFile.model_validate({"categories": [{"category_id": "x", "name": "X", "parent_id": "nope"}]})
    with pytest.raises(ValidationError, match="max depth"):
        CategoriesFile.model_validate(
            {
                "categories": [
                    {"category_id": "a", "name": "A"},
                    {"category_id": "b", "name": "B", "parent_id": "a"},
                    {"category_id": "c", "name": "C", "parent_id": "b"},
                ]
            }
        )
    with pytest.raises(ValidationError, match="snake_case"):
        CategoriesFile.model_validate({"categories": [{"category_id": "Dairy & Eggs", "name": "D"}]})


def test_robots_file():
    cam = StoreCameraSpec(
        camera_id="store_ave.ceiling-1", store_id="store_ave", name="Ave 1", mount={"frame": "world", "z": 3.2}
    )
    rf = RobotsFile(robots=[PICO, RHEA, HAZEL], store_cameras=[cam])
    assert [r.name for r in rf.robots_in("store_ave")] == ["Pico", "Rhea"]
    hill = rf.robot("rover-hill-1")
    assert hill.kind == "rover_so101" and hill.cameras == list(ROBOT_CAMERA_KINDS)
    assert rf.cameras_in("store_ave") == [cam]
    with pytest.raises(ValidationError, match="duplicate robot_id"):
        RobotsFile(robots=[PICO, PICO])
    with pytest.raises(ValidationError, match="no dots"):
        RobotSpec(robot_id="rover.1", name="X", store_id="s", dock=Pose2D(x=0, y=0))
    with pytest.raises(ValidationError, match="ceiling"):
        StoreCameraSpec(camera_id="store_hill.ceiling-1", store_id="store_ave", name="x", mount={"frame": "world"})
    with pytest.raises(ValidationError, match="world"):
        StoreCameraSpec(camera_id="store_ave.ceiling-1", store_id="store_ave", name="x", mount={"frame": "gripper"})


# ---------------------------------------------------------------------------------------------------- fleet
def test_robot_info_and_assignment_rule():
    pico = robot_info_from(PICO, _state("rover-ave-1", 3.0), available=True)
    rhea = robot_info_from(RHEA, _state("rover-ave-2", 9.0, battery=60), available=True)
    hazel = robot_info_from(HAZEL, None)
    assert pico.connected and pico.cameras == ["rover-ave-1.front", "rover-ave-1.wrist", "rover-ave-1.basket"]
    assert not hazel.connected and hazel.status == RobotStatus.DISCONNECTED and hazel.battery_pct == 100
    assert json.loads(pico.model_dump_json())["basket_count"] == 0

    target = Pose2D(x=3.5, y=2)
    best, cands = choose_robot([rhea, hazel, pico], store_id="store_ave", target=target)
    assert best is not None and best.robot_id == "rover-ave-1"
    assert [c.robot_id for c in cands] == ["rover-ave-1", "rover-ave-2", "rover-hill-1"]
    assert not cands[-1].eligible and "another store" in cands[-1].reason
    assert set(best.breakdown) == {"idle", "battery", "distance"} and "Pico: idle" in best.reason

    busy = pico.model_copy(update={"available": False, "current_goal_id": "g1"})
    best2, _ = choose_robot([busy, rhea], store_id="store_ave", target=target)
    assert best2.robot_id == "rover-ave-2"  # idle beats close-but-busy
    flat = rhea.model_copy(update={"battery_pct": 10.0})
    assert not score_robot(flat, store_id="store_ave").eligible
    assert score_robot(flat, store_id="store_ave", kind="return_to_dock").eligible
    assert choose_robot([hazel], store_id="store_hill")[0] is None  # disconnected


def test_goal_lifecycle_models():
    assert can_goal_transition(GoalStatus.PENDING, GoalStatus.ASSIGNED)
    assert can_goal_transition(GoalStatus.ACTIVE, GoalStatus.PENDING)  # reassign after robot failure
    assert not can_goal_transition(GoalStatus.COMPLETED, GoalStatus.ACTIVE)
    goal = Goal(goal_id="g1", kind="fetch", store_id="store_ave", title="Fetch salsa",
                items=[GoalItem(item_id="i1", query="mild salsa", sku="SALSA-MILD")])  # fmt: skip
    assert not goal.terminal and goal.item("i1").status == "pending" and goal.assignment is None
    assert json.loads(goal.model_dump_json())["attempts"] == 0
    assert Goal.model_validate_json(goal.model_dump_json()) == goal
    assert GoalSpec(kind="return_to_dock", robot_id="rover-hill-1").resolved_end_at() == "dock"
    assert GoalSpec(kind="shelf_scan", store_id="store_ave", categories=["dairy"]).resolved_end_at() == "stay"
    for bad in (
        {"kind": "return_to_dock"},
        {"kind": "shop", "store_id": "store_ave"},
        {"kind": "fetch", "store_id": "store_ave"},
    ):
        with pytest.raises(ValidationError):
            GoalSpec(**bad)
    plan = RequestPlan(
        request_id="r1",
        text="tacos",
        user_id="maya",
        store_choices=[StoreChoice(store_id="store_ave", chosen=True, in_stock_count=7, total_items=8),
                       StoreChoice(store_id="store_hill", chosen=True, in_stock_count=1, total_items=8)],
    )  # fmt: skip
    dumped = json.loads(plan.model_dump_json())
    assert dumped["split"] is True and dumped["store_choices"][0]["coverage"] == 0.875
    assert RequestPlan.model_validate(dumped) == plan


# ---------------------------------------------------------------------------------------------------- sim wire
def test_multiplexed_sim_wire():
    hello = SimHello(
        token="t",
        stores=[SimStoreInfo(store_id="store_ave", camera_ids=["store_ave.ceiling-1"])],
        robots=[SimRobotInfo(robot_id="rover-ave-1", store_id="store_ave", camera_ids=["rover-ave-1.front"])],
        cameras=[CameraInfo(camera_id="rover-ave-1.front", kind="front", name="Pico", source="isaac")],
    )
    again = parse_sim_message(hello.model_dump_json())
    assert again.robot_ids() == ["rover-ave-1"] and again.store_ids() == ["store_ave"]
    legacy = parse_sim_message({"type": "hello", "token": "t", "store_id": "store_hill", "data_hash": "abc"})
    assert legacy.store_ids() == ["store_hill"] and legacy.robots == []

    nav = NavigateCmd(cmd_id="c1", robot_id="rover-ave-2", waypoints=[Pose2D(x=0, y=0)])
    assert parse_gateway_to_sim(nav.model_dump_json()).robot_id == "rover-ave-2"
    cap = parse_gateway_to_sim({"type": "capture", "cmd_id": "c2", "camera_id": "store_ave.ceiling-1"})
    assert isinstance(cap, CaptureCmd) and cap.robot_id is None and cap.camera == "front"
    assert isinstance(parse_gateway_to_sim({"type": "set_streams", "cmd_id": "c3", "camera_ids": []}), SetStreamsCmd)
    assert isinstance(parse_gateway_to_sim({"type": "stop", "cmd_id": "c4"}), StopCmd)
    reset = ResetCmd(cmd_id="c5", robot_id="rover-ave-1", store_id="store_ave", robots=[PICO])
    assert parse_gateway_to_sim(reset.model_dump_json()).robots[0].name == "Pico"
    res = parse_sim_message(CommandResult(cmd_id="c1", robot_id="rover-ave-2", ok=False, error_code="unknown_robot")
                            .model_dump_json())  # fmt: skip
    assert message_robot_id(res) == "rover-ave-2"
    st = parse_sim_message({"type": "robot_state", "robot_id": "rover-ave-1", "pose": {"x": 1, "y": 1},
                            "status": "charging", "battery_pct": 40, "backend": "isaac"})  # fmt: skip
    assert st.status == RobotStatus.CHARGING and st.backend == "isaac"
    frame = parse_sim_message({"type": "camera_frame", "frame_id": "f", "camera": "head", "jpeg_b64": "AA", "w": 1,
                               "h": 1, "camera_id": "rover-ave-1.front", "robot_id": "rover-ave-1"})  # fmt: skip
    assert frame.camera == "head" and frame.camera_id == "rover-ave-1.front"  # v0.1 'head' still accepted


# ---------------------------------------------------------------------------------------------------- events
def test_new_events_roundtrip_and_envelope():
    for t in ("request.received", "goal.assigned", "brain.step", "stock.alert", "camera.frame", "fleet.robot_updated"):
        assert t in EVENT_TYPES and t in EVENT_PRODUCERS
    goal = Goal(goal_id="g1", kind="return_to_dock", store_id="store_hill", title="Dock", end_at="dock")
    from contracts.fleet import Assignment

    asg = Assignment(assignment_id="a1", goal_id="g1", robot_id="rover-hill-1", store_id="store_hill", score=0.9,
                     reason="Hazel: idle")  # fmt: skip
    ev = make_event("goal.assigned", trip_id=None, store_id="store_hill", robot_id="rover-hill-1", goal=goal,
                    assignment=asg, source="fleet")  # fmt: skip
    assert isinstance(ev, GoalAssigned) and ev.store_id == "store_hill" and ev.robot_id == "rover-hill-1"
    assert parse_event(ev.model_dump_json()) == ev
    step = BrainStep(step_id="s1", robot_id="rover-ave-1", kind="tool_call", tool="navigate_to",
                     input={"target": "slot", "slot_id": "G2-E1-03"}, model="scripted")  # fmt: skip
    assert isinstance(parse_event(BrainStepEvent(robot_id="rover-ave-1", step=step).model_dump()), BrainStepEvent)
    alert = {"alert_id": "store_ave:CILANTRO", "store_id": "store_ave", "sku": "CILANTRO", "name": "Cilantro",
             "status": "out", "qty": 0, "low_threshold": 2, "capacity": 8}  # fmt: skip
    assert isinstance(parse_event({"type": "stock.alert", "alert": alert}), StockAlertEvent)
    raw = CameraFrame(frame_id="f", jpeg_b64="AA", w=1, h=1)
    with pytest.raises(ValidationError, match="blurred"):
        CameraFrameEvent(camera_id="rover-ave-1.front", frame=raw)
    blurred = raw.model_copy(update={"blurred": True})
    assert CameraFrameEvent(camera_id="rover-ave-1.front", frame=blurred).frame.blurred
    # robot.arrived for non-trip goals has no stop
    assert parse_event({"type": "robot.arrived", "pose": {"x": 1, "y": 1}, "goal_id": "g1"}).stop_id is None


# ---------------------------------------------------------------------------------------------------- tools + api
def test_brain_tools():
    assert [t["name"] for t in BRAIN_TOOL_DEFINITIONS] == list(BRAIN_TOOL_NAMES)
    assert len(BRAIN_TOOL_NAMES) == 11 and len(TOOL_NAMES) == 6
    for tool in BRAIN_TOOL_DEFINITIONS:
        assert tool["description"] and tool["input_schema"]["type"] == "object" and "title" not in tool["input_schema"]
    assert parse_brain_tool_input("navigate_to", {"target": "dock"}).target == "dock"
    with pytest.raises(ValidationError):
        parse_brain_tool_input("navigate_to", {"target": "slot"})
    with pytest.raises(ValidationError):
        parse_brain_tool_input("locate_items", {})
    with pytest.raises(ValidationError):
        parse_brain_tool_input("look", {"camera": "ceiling"})
    with pytest.raises(ValidationError):
        parse_brain_tool_input("ask_user", {"kind": "checkout", "question": "Pay?"})
    assert parse_brain_tool_input("update_progress", {"note": "x", "item_status": "unavailable"}).item_status
    assert parse_brain_tool_input("finish_goal", {"summary": "done"}).status == "completed"
    strict = json.dumps(brain_tool_definitions(strict=True))
    assert '"minimum"' not in strict and '"maximum"' not in strict
    ask = ApprovalRequest(approval_id="a", user_id="staff", kind=ApprovalKind.QUESTION, title="Restock?",
                          goal_id="g1", robot_id="rover-ave-1")  # fmt: skip
    assert ask.trip_id is None
    chat = parse_client_message({"type": "robot_chat", "robot_id": "rover-ave-1", "text": "hi"})
    assert isinstance(chat, WsRobotChat)
    assert ObserveRequest().question is None


# ---------------------------------------------------------------------------------------------------- protocols
class DummyFleet:
    async def start(self): ...
    async def shutdown(self): ...
    async def submit_request(self, request): ...
    async def get_request(self, request_id): ...
    async def list_robots(self, store_id=None): ...
    async def get_robot(self, robot_id): ...
    async def create_goal(self, spec): ...
    async def list_requests(self, user_id=None, limit=20): ...
    async def list_goals(self, store_id=None, robot_id=None, status=None, *, active=None, kind=None, request_id=None,
                         trip_id=None, limit=50): ...  # fmt: skip
    async def get_goal(self, goal_id): ...
    async def update_goal(self, goal_id, update): ...
    async def cancel_goal(self, goal_id, reason=""): ...
    async def execute_trip(self, trip_id, robot_id=None): ...
    async def message_robot(self, robot_id, text, user_id=None): ...
    async def stop_robot(self, robot_id, reason=""): ...


class DummyCameras:
    async def list_cameras(self, store_id=None, robot_id=None): ...
    async def get_camera(self, camera_id): ...
    async def snapshot(self, camera_id, max_width=None): ...

    async def stream(self, camera_id, fps=5.0, max_width=None):
        yield CameraFrame(frame_id="f", jpeg_b64="AA", w=1, h=1, blurred=True)

    async def observe(self, camera_id, question=None, *, goal_id=None): ...


class DummyBrain:
    async def run_goal(self, robot_id, goal): ...
    async def handle_message(self, robot_id, text, user_id=None): ...
    async def status(self, robot_id): ...
    async def recent_steps(self, robot_id=None, goal_id=None, limit=50, before=None): ...


def test_new_protocols_and_factories():
    assert isinstance(DummyFleet(), FleetService)
    assert isinstance(DummyCameras(), CameraService)
    assert isinstance(DummyBrain(), RobotBrainService)
    services = Services(fleet=DummyFleet(), cameras=DummyCameras(), brain=DummyBrain())
    assert services.require("brain") is services.brain
    assert set(SERVICE_FACTORIES) == set(SERVICE_PROTOCOLS) | set(EXTRA_SERVICE_PROTOCOLS)
    assert set(SERVICE_FACTORIES) <= set(Services.field_names())
    assert SERVICE_FACTORIES["cameras"] == ("perception", "create_cameras", "create_cameras_fake")
    assert SERVICE_FACTORIES["brain"][0] == "agent" and SERVICE_FACTORIES["fleet"] == ("fleet", "create", "create_fake")
    for name in ("list_categories", "category_products", "stock_levels", "stock_alerts", "restock", "adjust_stock",
                 "stock_history"):  # fmt: skip
        assert hasattr(CatalogService, name)
    for name in ("FleetService", "Goal", "RobotInfo", "BrainStep", "CameraInfo", "StockLevel", "RobotsFile",
                 "BRAIN_TOOL_DEFINITIONS", "choose_robot", "SimStatus"):  # fmt: skip
        assert hasattr(contracts, name) and name in contracts.__all__


def test_robot_info_rejects_unknown_fields():
    with pytest.raises(ValidationError):
        RobotInfo(robot_id="r", name="R", store_id="s", dock=Pose2D(x=0, y=0), mystery=1)
