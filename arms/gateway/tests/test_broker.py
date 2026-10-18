import asyncio

import pytest

from arms.gateway.registry import FrameHub, GatewayApprovalBroker, GatewayPublisher, SimLinkHub
from contracts.common import Pose2D
from contracts.events import FrameEvent
from contracts.services import ApprovalBroker, EventPublisher, Services
from contracts.sim import (
    CameraFrame,
    CaptureCmd,
    CommandResult,
    NavigateCmd,
    ResetCmd,
    RobotState,
    SetStreamsCmd,
    SimHello,
    SimHosted,
    SimLink,
    SimRobotInfo,
    SimStoreInfo,
    StopCmd,
)
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


async def test_pending_filters_and_cancel() -> None:
    broker, bus, _ = make_broker()
    t1 = asyncio.create_task(broker.request(req(approval_id="a1", goal_id="g1", robot_id="rover-ave-1", store_id="s1")))
    t2 = asyncio.create_task(
        broker.request(
            ApprovalRequest(
                approval_id="a2",
                user_id="staff",
                kind=ApprovalKind.QUESTION,
                title="?",
                goal_id="g2",
                robot_id="rover-ave-2",
                store_id="s1",
            )
        )
    )
    await asyncio.sleep(0)
    assert {p.approval_id for p in broker.pending()} == {"a1", "a2"}
    assert [p.approval_id for p in broker.pending(goal_id="g2")] == ["a2"]
    assert [p.approval_id for p in broker.pending(user_id="staff", store_id="s1")] == ["a2"]
    assert [p.approval_id for p in broker.pending("trip_1", robot_id="rover-ave-1")] == ["a1"]
    assert broker.pending(robot_id="rover-ave-1", user_id="staff") == []
    assert await broker.cancel("a2", "goal cancelled")
    resp = await t2
    assert resp.decision == "timeout" and resp.channel == "system" and resp.note == "goal cancelled"
    assert not await broker.cancel("a2")
    resolved = [e for e in bus.recent() if e.type == "approval.resolved"]
    assert resolved and resolved[-1].robot_id == "rover-ave-2" and resolved[-1].trip_id is None
    requested = next(e for e in bus.recent() if e.type == "approval.requested" and e.request.approval_id == "a1")
    assert requested.trip_id == "trip_1" and requested.store_id == "s1" and requested.user_id == "maya"
    await broker.cancel("a1")
    await t1


async def test_envelope_hook_fills_request_id() -> None:
    broker, bus, _ = make_broker()
    broker.envelope_hook = lambda r: {"request_id": "req_9"}
    task = asyncio.create_task(broker.request(req()))
    await asyncio.sleep(0)
    await broker.resolve(ApprovalResponse(approval_id="appr_1", decision="approve"))
    await task
    assert all(e.request_id == "req_9" for e in bus.recent())


def _hello(worker_id: str, store: str, robots: list[str]) -> SimHello:
    return SimHello(
        token="t",
        worker_id=worker_id,
        stores=[SimStoreInfo(store_id=store, data_hash="h", camera_ids=[f"{store}.ceiling-1"])],
        robots=[SimRobotInfo(robot_id=r, store_id=store, camera_ids=[f"{r}.front"]) for r in robots],
    )


async def test_simlink_multiplexes_workers_by_robot_store_and_camera() -> None:
    hub = SimLinkHub()
    sent: dict[str, list[str]] = {"A": [], "B": []}

    def sender(name: str):  # noqa: ANN202
        async def _send(text: str) -> None:
            sent[name].append(text)

        return _send

    seen: list[str] = []

    async def on_msg(msg: object) -> None:
        seen.append(getattr(msg, "type", "?"))

    hub.subscribe(on_msg)
    await hub.accept(_hello("isaac-a", "store_ave", ["rover-ave-1", "rover-ave-2"]), sender("A"), "sA")
    await hub.accept(_hello("isaac-b", "store_hill", ["rover-hill-1"]), sender("B"), "sB")
    assert seen == ["hello", "hello"]
    assert hub.is_connected() and hub.is_connected("rover-hill-1") and not hub.is_connected("rover-x")

    async def answer(session: str, cmd_id: str) -> None:
        await asyncio.sleep(0.01)
        await hub.dispatch(CommandResult(cmd_id=cmd_id, ok=True), session)

    # robot_id -> the worker hosting that robot
    task = asyncio.create_task(
        hub.send(NavigateCmd(cmd_id="n1", robot_id="rover-hill-1", waypoints=[Pose2D(x=1, y=1)]))
    )
    await asyncio.sleep(0.01)
    assert '"n1"' in sent["B"][-1] and not sent["A"]
    await answer("sB", "n1")
    assert (await task).ok
    # camera_id -> the worker hosting the camera (ceiling cam of store_ave)
    task = asyncio.create_task(hub.send(CaptureCmd(cmd_id="c1", camera_id="store_ave.ceiling-1")))
    await asyncio.sleep(0.01)
    assert '"c1"' in sent["A"][-1]
    await answer("sA", "c1")
    await task
    # unknown robot with two workers -> ConnectionError
    with pytest.raises(ConnectionError):
        await hub.send(NavigateCmd(cmd_id="n2", robot_id="rover-x", waypoints=[Pose2D(x=0, y=0)]))
    # store-level reset of a store nobody hosts -> least loaded worker; emergency stop -> every worker
    task = asyncio.create_task(hub.send(ResetCmd(cmd_id="r1", store_id="store_new")))
    await asyncio.sleep(0.01)
    assert '"r1"' in sent["A"][-1] or '"r1"' in sent["B"][-1]
    await hub.dispatch(CommandResult(cmd_id="r1", ok=True), "sA" if '"r1"' in sent["A"][-1] else "sB")
    await task
    task = asyncio.create_task(hub.send(StopCmd(cmd_id="s1")))
    await asyncio.sleep(0.01)
    assert '"s1.0"' in sent["A"][-1] and '"s1.1"' in sent["B"][-1]
    await hub.dispatch(CommandResult(cmd_id="s1.0", ok=True), "sA")
    await hub.dispatch(CommandResult(cmd_id="s1.1", ok=True), "sB")
    assert (await task).cmd_id == "s1"
    # set_streams: each worker only gets its own cameras
    task = asyncio.create_task(
        hub.send(SetStreamsCmd(cmd_id="v1", camera_ids=["rover-ave-1.front", "rover-hill-1.front"]))
    )
    await asyncio.sleep(0.01)
    assert "rover-hill-1.front" not in sent["A"][-1] and "rover-hill-1.front" in sent["B"][-1]
    await hub.dispatch(CommandResult(cmd_id="v1.0", ok=True), "sA")
    await hub.dispatch(CommandResult(cmd_id="v1.1", ok=True), "sB")
    await task
    # hosted updates + status
    await hub.dispatch(SimHosted(worker_id="isaac-b", stores=[SimStoreInfo(store_id="store_hill")], robots=[]), "sB")
    assert not hub.is_connected("rover-hill-1") and seen[-1] == "hosted"
    st = hub.status()
    assert st.connected and len(st.workers) == 2 and "store_ave" in st.isaac_store_ids
    # one worker drops: only its in-flight commands fail
    task = asyncio.create_task(hub.send(NavigateCmd(cmd_id="n3", robot_id="rover-ave-1", waypoints=[Pose2D(x=1, y=1)])))
    await asyncio.sleep(0.01)
    hub.detach("gone", session_id="sB")
    assert hub.is_connected("rover-ave-1")
    await answer("sA", "n3")
    assert (await task).ok
    hub.detach("gone", session_id="sA")
    assert not hub.is_connected() and not hub.status().connected


async def test_simlink_detach_tells_subscribers_what_is_still_hosted() -> None:
    """A worker leaving fans out a SimHosted with the remaining stores, so the robot arm moves the lost stores onto
    the 2D fake right away (not at their next command)."""
    hub = SimLinkHub()
    seen: list[SimHosted] = []

    async def on_msg(msg: object) -> None:
        if isinstance(msg, SimHosted):
            seen.append(msg)

    async def _send(text: str) -> None:
        return None

    hub.subscribe(on_msg)
    await hub.accept(_hello("isaac-a", "store_ave", ["rover-ave-1"]), _send, "sA")
    await hub.accept(_hello("isaac-b", "store_hill", ["rover-hill-1"]), _send, "sB")
    hub.detach("gone", session_id="sB")
    await asyncio.sleep(0.01)
    assert seen and seen[-1].store_ids() == ["store_ave"] and seen[-1].robot_ids() == ["rover-ave-1"]
    hub.detach("gone", session_id="sA")
    await asyncio.sleep(0.01)
    assert seen[-1].store_ids() == [] and seen[-1].robot_ids() == []
