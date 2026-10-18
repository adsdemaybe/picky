"""Discriminated unions (events, sim wire protocol), LLM tool definitions, Protocol conformance."""

import json

import pytest
from pydantic import ValidationError

from contracts.common import Pose2D
from contracts.events import (
    EVENT_TYPES,
    AgentMessage,
    ErrorEvent,
    FrameEvent,
    RoutePlanned,
    TripStateChanged,
    event_json_schema,
    make_event,
    parse_event,
    with_seq,
)
from contracts.routing import Route, RouteRequest
from contracts.services import (
    ARM_NAMES,
    SERVICE_ARMS,
    SERVICE_PROTOCOLS,
    ApprovalBroker,
    EventPublisher,
    RoutingService,
    Services,
)
from contracts.shopping import TripState
from contracts.sim import (
    CameraFrame,
    CommandResult,
    NavigateCmd,
    PickCmd,
    RobotState,
    SimHello,
    SimLink,
    SimWelcome,
    parse_gateway_to_sim,
    parse_sim_message,
)
from contracts.tools import TOOL_DEFINITIONS, TOOL_NAMES, parse_tool_input
from contracts.trust import ApprovalRequest, ApprovalResponse

EXPECTED_EVENT_TYPES = {
    "trip.created",
    "trip.snapshot",
    "trip.state_changed",
    "intent.received",
    "list.ready",
    "sourcing.split",
    "recs.ready",
    "route.planned",
    "stop.updated",
    "robot.state",
    "robot.arrived",
    "scan.result",
    "pick.choices",
    "item.picked",
    "basket.verified",
    "cart.updated",
    "approval.requested",
    "approval.resolved",
    "mandate.decision",
    "checkout.completed",
    "followup.sent",
    "feedback.received",
    "profile.updated",
    "frame",
    "camera.frame",
    "request.received",
    "request.planned",
    "goal.created",
    "goal.assigned",
    "goal.updated",
    "goal.completed",
    "goal.failed",
    "fleet.robot_updated",
    "brain.step",
    "stock.updated",
    "stock.alert",
    "agent.message",
    "error",
}


def _frame(blurred: bool) -> CameraFrame:
    return CameraFrame(frame_id="f1", jpeg_b64="AAAA", w=640, h=480, blurred=blurred)


def test_event_types_complete():
    assert set(EVENT_TYPES) == EXPECTED_EVENT_TYPES


def test_events_parse_by_type():
    ev = make_event("trip.state_changed", trip_id="t1", to_state=TripState.EXECUTING, source="gateway")
    assert isinstance(ev, TripStateChanged)
    parsed = parse_event(ev.model_dump_json())
    assert isinstance(parsed, TripStateChanged) and parsed == ev
    route = Route(route_id="r", store_id="store_ave", stops=[], order=[])
    parsed2 = parse_event(json.loads(RoutePlanned(trip_id="t1", route=route).model_dump_json()))
    assert isinstance(parsed2, RoutePlanned)
    assert isinstance(parse_event({"type": "error", "arm": "robot", "message": "boom"}), ErrorEvent)
    assert isinstance(parse_event({"type": "agent.message", "message_id": "m", "text": "hi"}), AgentMessage)
    with pytest.raises(ValidationError):
        parse_event({"type": "nope"})
    with pytest.raises(ValueError):
        make_event("nope", trip_id=None)


def test_with_seq_and_envelope():
    ev = make_event("error", trip_id="t1", arm="x", message="m")
    assert ev.seq == 0
    ev2 = with_seq(ev, 7)
    assert ev2.seq == 7 and ev2.trip_id == "t1" and ev2.ts == ev.ts


def test_frame_event_requires_blur():
    assert FrameEvent(trip_id="t", frame=_frame(True)).frame.blurred
    with pytest.raises(ValidationError, match="blurred"):
        FrameEvent(trip_id="t", frame=_frame(False))


def test_event_json_schema_has_all_types():
    schema = json.dumps(event_json_schema())
    for t in EXPECTED_EVENT_TYPES:
        assert f'"{t}"' in schema


def test_sim_messages_parse_by_type():
    state = RobotState(pose=Pose2D(x=1, y=2))
    assert isinstance(parse_sim_message(state.model_dump_json()), RobotState)
    assert isinstance(parse_sim_message(_frame(False).model_dump()), CameraFrame)
    hello = SimHello(token="dev-sim-token", store_id="store_ave")
    assert isinstance(parse_sim_message(hello.model_dump_json()), SimHello)
    res = CommandResult(cmd_id="c1", ok=True, state=state)
    assert parse_sim_message(res.model_dump_json()) == res
    nav = NavigateCmd(cmd_id="c2", waypoints=[Pose2D(x=0, y=0)])
    assert isinstance(parse_gateway_to_sim(nav.model_dump_json()), NavigateCmd)
    assert isinstance(parse_gateway_to_sim({"type": "pick", "cmd_id": "c3", "slot_id": "s", "sku": "k"}), PickCmd)
    assert isinstance(parse_gateway_to_sim({"type": "welcome", "ok": True}), SimWelcome)
    with pytest.raises(ValidationError):
        parse_gateway_to_sim({"type": "navigate", "cmd_id": "c", "waypoints": []})


def test_tool_definitions_shape():
    assert [t["name"] for t in TOOL_DEFINITIONS] == list(TOOL_NAMES)
    assert set(TOOL_NAMES) == {
        "query_inventory",
        "plan_route",
        "recommend",
        "add_to_cart",
        "request_approval",
        "checkout",
    }
    for tool in TOOL_DEFINITIONS:
        assert set(tool) == {"name", "description", "input_schema"}
        assert tool["description"]
        schema = tool["input_schema"]
        assert schema["type"] == "object"
        assert "properties" in schema
        assert "title" not in schema
        json.dumps(schema)
    approval = next(t for t in TOOL_DEFINITIONS if t["name"] == "request_approval")
    assert "title" in approval["input_schema"]["properties"]  # a real field named 'title' must survive
    assert approval["input_schema"]["required"] == ["kind", "title"]
    add = parse_tool_input("add_to_cart", {"sku": "salsa_hot", "qty": 2})
    assert add.qty == 2
    with pytest.raises(ValidationError):
        parse_tool_input("add_to_cart", {"sku": "x", "qty": 0})


# --------------------------------------------------------------------------------------------------------------------
# Protocol conformance with tiny dummies
# --------------------------------------------------------------------------------------------------------------------
class DummyRouting:
    async def plan_route(self, request):
        return Route(route_id="r", store_id=request.store_id, stops=[], order=[])

    async def plan_batched(self, requests):
        return await self.plan_route(requests[0])

    async def grid_spec(self, store_id):
        raise NotImplementedError


class DummyBroker:
    def __init__(self) -> None:
        self._pending: dict[str, ApprovalRequest] = {}

    async def request(self, request):
        return ApprovalResponse(approval_id=request.approval_id, decision="approve")

    async def resolve(self, response):
        return self._pending.pop(response.approval_id, None) is not None

    def pending(self, trip_id=None, *, goal_id=None, robot_id=None, store_id=None, user_id=None):
        return list(self._pending.values())

    async def cancel(self, approval_id, reason=""):
        return self._pending.pop(approval_id, None) is not None


class DummyPublisher:
    def __init__(self) -> None:
        self.seq = 0

    async def publish(self, event):
        self.seq += 1
        return with_seq(event, self.seq)


class DummySimLink:
    def is_connected(self, robot_id=None) -> bool:
        return True

    def status(self):
        from contracts.sim import SimStatus

        return SimStatus(connected=True)

    async def send(self, cmd, timeout_s=None):
        return CommandResult(cmd_id=cmd.cmd_id, ok=True)

    def subscribe(self, handler):
        return lambda: None


class Incomplete:
    async def plan_route(self, request): ...


async def test_protocol_conformance():
    assert isinstance(DummyRouting(), RoutingService)
    assert not isinstance(Incomplete(), RoutingService)
    assert isinstance(DummyBroker(), ApprovalBroker)
    assert isinstance(DummyPublisher(), EventPublisher)
    assert isinstance(DummySimLink(), SimLink)

    services = Services(routing=DummyRouting(), events=DummyPublisher(), sim_link=DummySimLink())
    with pytest.raises(RuntimeError, match="catalog"):
        services.require("catalog")
    route = await services.require("routing").plan_route(
        RouteRequest(store_id="store_ave", start=Pose2D(x=0, y=0), stops=[])
    )
    assert route.store_id == "store_ave"
    published = await services.events.publish(make_event("error", trip_id="t", arm="a", message="m"))
    assert published.seq == 1
    result = await services.sim_link.send(NavigateCmd(cmd_id="c9", waypoints=[Pose2D(x=0, y=0)]))
    assert result.cmd_id == "c9"


def test_registry_constants():
    assert set(SERVICE_ARMS) <= set(ARM_NAMES)
    assert set(SERVICE_ARMS) <= set(Services.field_names())
    assert len(ARM_NAMES) == 12 and "fleet" in ARM_NAMES
    for proto in SERVICE_PROTOCOLS.values():
        assert getattr(proto, "_is_runtime_protocol", False)


def test_robot_protocol_accepts_async_generators():
    from contracts.services import RobotService

    class R:
        async def list_robots(self): ...
        async def state(self, robot_id): ...
        async def reset(self, robot_id, store_id=None): ...
        async def navigate(self, robot_id, waypoints): ...
        async def stop(self, robot_id): ...
        async def scan(self, robot_id, slot_id): ...
        async def capture(self, robot_id, camera="front"): ...
        async def capture_camera(self, camera_id): ...
        async def list_instances(self, robot_id, slot_id=None): ...
        async def pick(self, robot_id, slot_id, sku, instance_id=None): ...
        async def place_in_basket(self, robot_id): ...
        async def unload_basket(self, robot_id): ...
        async def sync_stock(self, store_id, slot_ids=None): ...

        async def frames(self, camera_id=None, robot_id=None):
            yield _frame(False)

        async def states(self, robot_id=None):
            yield RobotState(robot_id="rover-ave-1", pose=Pose2D(x=0, y=0))

    assert isinstance(R(), RobotService)

    class OldSingleRobot(R):  # v0.1 shape (no list_robots / capture_camera) no longer conforms
        list_robots = None  # type: ignore[assignment]

    assert not isinstance(OldSingleRobot(), RobotService)


def test_client_messages_parse_by_type():
    from contracts.api import WsApproval, WsChat, WsPing, parse_client_message

    assert isinstance(parse_client_message({"type": "chat", "text": "add limes"}), WsChat)
    assert isinstance(parse_client_message('{"type": "ping"}'), WsPing)
    msg = parse_client_message(
        {"type": "approval", "response": {"approval_id": "ap1", "decision": "choice", "choice": "inst_2"}}
    )
    assert isinstance(msg, WsApproval) and msg.response.approved
    with pytest.raises(ValidationError):
        parse_client_message({"type": "approval", "response": {"approval_id": "ap1", "decision": "maybe"}})
