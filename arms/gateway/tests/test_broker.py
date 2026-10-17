import asyncio

import pytest

from arms.gateway.registry import FrameHub, GatewayApprovalBroker, GatewayPublisher, SimLinkHub
from contracts.common import Pose2D
from contracts.events import FrameEvent
from contracts.services import ApprovalBroker, EventPublisher, Services
from contracts.sim import CameraFrame, CommandResult, NavigateCmd, RobotState, SimLink
from contracts.trust import ApprovalKind, ApprovalRequest, ApprovalResponse
from utils.events import EventBus


def make_broker() -> tuple[GatewayApprovalBroker, EventBus, Services]:
    services = Services()
    bus = EventBus()
    services.events = GatewayPublisher(bus, FrameHub())
    broker = GatewayApprovalBroker(services)
    services.approvals = broker
    return broker, bus, services


def req(kind: ApprovalKind = ApprovalKind.RECOMMENDATION, **kw) -> ApprovalRequest:
    return ApprovalRequest(
        approval_id=kw.pop("approval_id", "appr_1"), trip_id="trip_1", user_id="maya", kind=kind, title="?", **kw
    )


def test_protocols() -> None:
    broker, bus, _ = make_broker()
    assert isinstance(broker, ApprovalBroker)
    assert isinstance(GatewayPublisher(bus, FrameHub()), EventPublisher)
    assert isinstance(SimLinkHub(), SimLink)


async def test_request_resolve_publishes_events() -> None:
    broker, bus, _ = make_broker()
    seen: list[str] = []
    broker.add_listener(lambda phase, r, resp: seen.append(phase))
    task = asyncio.create_task(broker.request(req()))
    await asyncio.sleep(0)
    assert [p.approval_id for p in broker.pending("trip_1")] == ["appr_1"]
    assert await broker.resolve(ApprovalResponse(approval_id="appr_1", decision="approve"))
    resp = await task
    assert resp.approved and resp.user_id == "maya"
    assert not await broker.resolve(ApprovalResponse(approval_id="appr_1", decision="reject"))  # already resolved
    assert [e.type for e in bus.recent()] == ["approval.requested", "approval.resolved"]
    assert seen == ["requested", "resolved"]
    assert broker.pending() == []


async def test_timeout() -> None:
    broker, bus, _ = make_broker()
    resp = await broker.request(req(expires_s=0.05))
    assert resp.decision == "timeout" and not resp.approved
    assert bus.recent()[-1].type == "approval.resolved"


async def test_checkout_hash_rules() -> None:
    broker, _, _ = make_broker()
    fut = await broker.open(req(ApprovalKind.CHECKOUT, cart_hash="abc"))
    bad = ApprovalResponse(approval_id="appr_1", decision="approve", channel="web", cart_hash="zzz")
    assert broker.check(bad) == "cart_hash_mismatch"
    assert not await broker.resolve(bad)
    # photon answers get the pending hash copied in
    assert await broker.resolve(ApprovalResponse(approval_id="appr_1", decision="approve", channel="photon"))
    assert (await fut).cart_hash == "abc"


async def test_unknown_approval() -> None:
    broker, _, _ = make_broker()
    assert broker.check(ApprovalResponse(approval_id="nope", decision="approve")) == "unknown"
    assert not await broker.resolve(ApprovalResponse(approval_id="nope", decision="approve"))


async def test_frames_bypass_the_bus() -> None:
    bus, hub = EventBus(), FrameHub()
    pub = GatewayPublisher(bus, hub)
    sub = hub.subscribe("trip_1")
    frame = CameraFrame(frame_id="f1", jpeg_b64="", w=1, h=1, blurred=True)
    ev = await pub.publish(FrameEvent(trip_id="trip_1", frame=frame))
    assert ev.seq == 0 and bus.recent() == []
    assert (await sub.get()).frame.frame_id == "f1"


async def test_simlink_roundtrip_and_disconnect() -> None:
    hub = SimLinkHub()
    with pytest.raises(ConnectionError):
        await hub.send(NavigateCmd(cmd_id="c0", waypoints=[Pose2D(x=0, y=0)]))
    sent: list[str] = []

    async def sender(text: str) -> None:
        sent.append(text)

    hub.attach(sender, session_id="s1")
    got: list[object] = []

    async def on_msg(msg: object) -> None:
        got.append(msg)

    unsub = hub.subscribe(on_msg)
    task = asyncio.create_task(hub.send(NavigateCmd(cmd_id="c1", waypoints=[Pose2D(x=1, y=1)]), timeout_s=2))
    await asyncio.sleep(0)
    assert '"cmd_id":"c1"' in sent[0]
    await hub.dispatch(CommandResult(cmd_id="c1", ok=True))
    assert (await task).ok
    await hub.dispatch(RobotState(pose=Pose2D(x=0, y=0)))
    assert len(got) == 1
    unsub()
    task = asyncio.create_task(hub.send(NavigateCmd(cmd_id="c2", waypoints=[Pose2D(x=1, y=1)]), timeout_s=2))
    await asyncio.sleep(0)
    hub.detach("test")
    with pytest.raises(ConnectionError):
        await task
    assert not hub.is_connected()
