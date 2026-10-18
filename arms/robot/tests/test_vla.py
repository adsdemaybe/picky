"""T0Vla (VlaService) over the real robot-arm RobotServices: the 2D fake and SimRobot on a loopback "Isaac" worker.

Ports the draft's VLA contract tests (idempotency, cancel, deadlines, stalls, the drop / hang / wrong-item / flaky-T1
scenarios) without the planner / runner / verifier arms: steps are built from ``contracts.plan`` directly and the
outcome is read back from ``RobotTruth`` (what the verifier would see)."""

import asyncio
import math
from collections import Counter
from datetime import timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from arms.robot import (
    FakeRobot,
    LoopbackSimLink,
    T0Vla,
    T1Adapter,
    T1Config,
    create,
    create_faults_fake,
    create_truth_fake,
    create_vla,
    create_vla_fake,
)
from arms.robot.tests.conftest import R1, R2, STORE, UP, fleet_opts
from arms.robot.vla import path_progress
from contracts.common import Pose2D, utcnow
from contracts.evidence import FrameRef
from contracts.faults import FaultMatch, FaultRule
from contracts.grounding import GroundedTarget, SlotLocation, TargetSpec
from contracts.plan import AlignStep, ExecContext, GraspStep, HandoffStep, NavigateToStep, PlaceStep
from contracts.services import Services, VlaService, run_vla_to_terminal
from contracts.sim import NavigateCmd, PickCmd, PlaceInBasketCmd, RobotStatus
from contracts.vla import VlaRequest, VlaStatus

AT_SALSA = Pose2D(x=3.0, y=3.62, theta=UP)
AT_AVO = Pose2D(x=5.0, y=3.62, theta=UP)
SALSA = TargetSpec(sku="salsa-mild", slot_id="S-salsa", description="Mild Salsa jar", label_hints=["Mild Salsa"])


def nav(step_id: str = "s01", goal: Pose2D = AT_SALSA, slot: str = "S-salsa", **kw: Any) -> NavigateToStep:
    return NavigateToStep(step_id=step_id, title="Drive", target="slot", slot_id=slot, goal_pose=goal, **kw)


def align(step_id: str = "s02", location: SlotLocation | None = None) -> AlignStep:
    return AlignStep(step_id=step_id, title="Look", slot_id="S-salsa", location=location)


def grasp(step_id: str = "s03", target: TargetSpec = SALSA, **kw: Any) -> GraspStep:
    return GraspStep(step_id=step_id, title="Grab", target=target, instruction="pick up the mild salsa", **kw)


def place(step_id: str = "s04", sku: str = "salsa-mild", **kw: Any) -> PlaceStep:
    return PlaceStep(step_id=step_id, title="Basket", sku=sku, **kw)


def request(step: Any, attempt: int = 1, robot_id: str = R1, **kw: Any) -> VlaRequest:
    ctx = ExecContext(plan_id="p", step_id=step.step_id, step_kind=step.kind, attempt=attempt, trip_id="trip-1")
    opts = {"deadline_s": 30.0, "heartbeat_s": 0.05, **kw}
    return VlaRequest(exec_id=f"p:{step.step_id}:a{attempt}", robot_id=robot_id, step=step, context=ctx, **opts)


async def drain(vla: Any, req: VlaRequest, **kw: Any) -> tuple[VlaStatus, list[VlaStatus]]:
    seen: list[VlaStatus] = []

    async def collect(st: VlaStatus) -> None:
        seen.append(st)

    terminal = await run_vla_to_terminal(vla, req, on_status=collect, **kw)
    return terminal, seen


class Counting:
    """RobotService proxy counting motion calls (idempotency: re-attach never moves twice)."""

    MOTIONS = ("navigate", "pick", "place_in_basket", "unload_basket", "stop", "put_back", "grab_unit")

    def __init__(self, inner: Any) -> None:
        self.inner = inner
        self.calls: Counter[str] = Counter()

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self.inner, name)
        if name not in self.MOTIONS:
            return attr

        async def wrapped(*a: Any, **k: Any) -> Any:
            self.calls[name] += 1
            return await attr(*a, **k)

        return wrapped


class StubEvidence:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def capture_refs(self, robot_id, cameras, *, not_before=None, context=None, aim_slot_id=None):  # noqa: ANN001, ANN201
        self.calls.append({"robot_id": robot_id, "cameras": list(cameras), "aim": aim_slot_id, "context": context})
        return [FrameRef(frame_id=f"{robot_id}.{c}:{len(self.calls)}", camera_id=f"{robot_id}.{c}") for c in cameras]

    async def get_frame(self, frame_id):  # noqa: ANN001, ANN201
        raise KeyError(frame_id)


def wire(services: Services, *, scenario: Any = "clean", simulate_t1: bool = False, **robot_opts: Any) -> Services:
    services.robot = FakeRobot(services, **fleet_opts(**robot_opts))
    services.faults = create_faults_fake(services, scenario=scenario)
    services.truth = create_truth_fake(services)
    services.vla = create_vla_fake(services, simulate_t1=simulate_t1)
    return services


async def on_isaac(services: Services, *, scenario: Any = "clean") -> LoopbackSimLink:
    worker = FakeRobot(services, **fleet_opts())
    link = LoopbackSimLink(worker)
    services.sim_link = link
    services.robot = create(services, **fleet_opts())
    services.faults = create_faults_fake(services, scenario=scenario)
    services.truth = create_truth_fake(services)
    services.vla = create_vla(services, t1=T1Adapter())
    await services.robot.list_robots()
    await link.connect()
    await services.robot.refresh_backends()
    return link


# ======================================================================================================================
# factories + capabilities
# ======================================================================================================================
async def test_factories_conform_and_call_nothing_at_construction(services):
    from contracts.services import FaultInjector, TruthSource

    vla, truth, faults = create_vla(services), create_truth_fake(services), create_faults_fake(services)
    assert isinstance(vla, VlaService) and isinstance(create_vla_fake(services), VlaService)
    assert isinstance(truth, TruthSource) and isinstance(faults, FaultInjector)
    assert services.catalog.calls == []


async def test_capabilities_t0_always_t1_unavailable_with_a_reason(services):
    wire(services)
    caps = {c.tier: c for c in await services.vla.capabilities(R1)}
    t0, t1, t2 = caps["t0_scripted"], caps["t1_pretrained"], caps["t2_finetuned"]
    assert t0.available and t0.backend == "fake" and t0.place_back and t0.model_id == "scripted"
    assert set(t0.step_kinds) == {"navigate_to", "align", "grasp", "place", "handoff"}
    assert not t1.available and "PICKY_VLA_TIERS" in t1.reason and not t2.available
    with pytest.raises(KeyError):
        await services.vla.capabilities("rover-nope")


def test_t1_adapter_backends():
    assert not T1Adapter().capability(R1, "isaac").available
    ws = T1Adapter(T1Config(backend="openpi_ws", model_id="lerobot/smolvla_base", policy_url="ws://vhagar:8000"))
    cap = ws.capability(R1, "isaac")
    assert not cap.available and "no policy server" in cap.reason and not ws.can_execute()
    from contracts.vla import PolicyServerStatus

    ws.on_policy_status(PolicyServerStatus(model_id="lerobot/smolvla_base", ready=True, robot_ids=[R1]))
    cap = ws.capability(R1, "isaac")
    assert not cap.available and "0.3.0" in cap.reason  # ready, but the 0.2 wire cannot run RunSkillCmd
    assert "2D fake" in ws.capability(R1, "fake").reason
    sim = T1Adapter(T1Config.simulated())
    assert sim.capability(R1, "fake").available and sim.can_execute()
    nokey = T1Adapter(T1Config(backend="perceptron_tool", model_id="perceptron-mk1.5"))
    assert not nokey.capability(R1, "fake").available and "PERCEPTRON_API_KEY" in nokey.capability(R1, "fake").reason
    key = T1Adapter(T1Config(backend="perceptron_tool", model_id="perceptron-mk1.5", api_key_set=True))
    cap = key.capability(R1, "isaac")
    assert cap.available and cap.needs_grounding and cap.step_kinds == ["grasp"] and cap.input_frames_blurred


def test_t1_config_from_settings():
    from utils.config import reset_settings, set_settings

    base = {"picky_vla_policy_protocol": "", "perceptron_api_key": ""}
    set_settings(picky_vla_tiers="t0_scripted,t1_pretrained", **base)
    assert T1Config.from_settings().backend == "openpi_ws"
    set_settings(
        picky_vla_tiers="t0_scripted,t1_pretrained", picky_vla_policy_protocol="perceptron_tool", perceptron_api_key=""
    )
    cfg = T1Config.from_settings()
    assert cfg.backend == "perceptron_tool" and not cfg.api_key_set
    set_settings(picky_vla_tiers="t1_sim", **base)
    assert T1Config.from_settings().backend == "simulated"
    assert create_vla_fake(Services()).t1.backend == "simulated"
    set_settings(picky_vla_tiers="", **base)
    assert T1Config.from_settings().backend == "none" and create_vla_fake(Services()).t1.backend == "none"
    reset_settings()


# ======================================================================================================================
# the T0 sequence + stream contract
# ======================================================================================================================
async def test_t0_pick_sequence_on_the_2d_fake(services):
    wire(services)
    vla, truth = services.vla, services.truth
    terminal, seen = await drain(vla, request(nav()))
    assert terminal.phase == "succeeded" and terminal.tier == "t0_scripted" and terminal.backend == "fake"
    assert [s.seq for s in seen] == list(range(1, len(seen) + 1)) and seen[0].phase == "accepted"
    assert [s.stage for s in seen if s.phase == "running"][:2] == ["planning_path", "driving"]
    out = terminal.outcome
    assert out is not None and out.ended_at is not None and out.final_state.pose.distance_to(AT_SALSA) < 1e-6

    terminal, _ = await drain(vla, request(align()))
    assert terminal.phase == "succeeded" and "aligned" in terminal.message

    terminal, seen = await drain(vla, request(grasp()))
    assert terminal.phase == "succeeded" and terminal.outcome.holding_instance_id == "S-salsa#00"
    assert [s.stage for s in seen if s.phase == "running"] == ["reaching", "closing", "lifting"]
    t = await truth.truth(R1, ["S-salsa"])
    assert t.holding is not None and t.holding.sku == "salsa-mild" and len(t.slots["S-salsa"].instances) == 3

    terminal, _ = await drain(vla, request(place()))
    assert terminal.phase == "succeeded" and terminal.outcome.final_state.basket == ["S-salsa#00"]
    t = await truth.truth(R1)
    assert t.holding is None and t.basket_counts() == {"salsa-mild": 1}

    terminal, _ = await drain(vla, request(HandoffStep(step_id="s09", title="Checkout")))
    assert terminal.phase == "succeeded" and terminal.message == "unloaded 1 units at checkout"
    assert (await services.robot.state(R1)).basket == []


async def test_execute_is_idempotent_and_rejects_body_conflicts(services):
    wire(services)
    counting = Counting(services.robot)
    services.robot = counting
    req = request(nav())
    first = [s.phase async for s in services.vla.execute(req)]
    again = [s.phase async for s in services.vla.execute(req)]  # re-attach: replay, no second motion
    tail = [s.seq async for s in services.vla.execute(req, after_seq=2)]
    conflict = [s async for s in services.vla.execute(req.model_copy(update={"tier": "t1_pretrained"}))]
    assert first == again and first[0] == "accepted" and first[-1] == "succeeded"
    assert counting.calls["navigate"] == 1 and tail[0] == 3
    assert len(conflict) == 1 and conflict[0].error.code == "idempotency_conflict"
    rec = await services.vla.get_exec(req.exec_id)
    assert rec.terminal is not None and rec.body_hash == req.body_hash
    with pytest.raises(KeyError):
        await services.vla.get_exec("nope")


async def test_a_second_exec_on_a_busy_robot_fails_at_once(services):
    wire(services, realtime_factor=2.0)
    it = services.vla.execute(request(nav())).__aiter__()
    assert (await it.__anext__()).phase == "accepted"
    other = [s async for s in services.vla.execute(request(grasp()))]
    assert len(other) == 1 and other[0].phase == "failed" and other[0].error.code == "busy"
    assert other[0].error.retryable  # busy is a free retry (retryable, counts_attempt=False)
    await services.vla.cancel_robot(R1, "test over")


async def test_unknown_robot_and_unsupported_store_are_typed(services):
    wire(services)
    terminal, _ = await drain(services.vla, request(nav(), robot_id="rover-nope"))
    assert terminal.phase == "failed" and terminal.error.code == "unknown_robot"
    terminal, _ = await drain(services.vla, request(nav("s06"), store_id="store_elsewhere"))
    assert terminal.phase == "failed" and terminal.error.code == "precondition_failed"


async def test_navigate_follows_routed_legs_and_skips_when_already_there(services):
    wire(services)
    routed: list[Any] = []

    async def plan_route(req: Any) -> Any:
        routed.append(req)
        mid = Pose2D(x=3.0, y=1.0, theta=UP)
        return SimpleNamespace(legs=[SimpleNamespace(waypoints=[req.start, mid, req.end])])

    services.routing = SimpleNamespace(plan_route=plan_route)  # type: ignore[assignment]
    terminal, _ = await drain(services.vla, request(nav()))
    assert terminal.phase == "succeeded" and len(routed) == 1 and routed[0].end == AT_SALSA
    counting = Counting(services.robot)
    services.robot = counting
    terminal, _ = await drain(services.vla, request(nav("s05")))
    assert terminal.phase == "succeeded" and terminal.message.startswith("already at") and not counting.calls


async def test_heartbeats_report_path_progress_while_driving(services):
    wire(services, realtime_factor=4.0)
    terminal, seen = await drain(services.vla, request(nav(), heartbeat_s=0.05))
    beats = [s for s in seen if s.message == "heartbeat" and s.stage == "driving"]
    assert terminal.phase == "succeeded" and len(beats) >= 2
    assert beats[-1].progress >= beats[0].progress and beats[0].eta_s is not None and beats[0].pose is not None


def test_path_progress():
    path = [Pose2D(x=0, y=0), Pose2D(x=2, y=0), Pose2D(x=2, y=2)]
    assert path_progress(path, Pose2D(x=1, y=0.1)) == pytest.approx((0.25, 3.0))
    assert path_progress(path, Pose2D(x=2, y=2)) == pytest.approx((1.0, 0.0))
    assert path_progress([Pose2D(x=1, y=1)], Pose2D(x=0, y=0)) == (1.0, 0.0)


# ======================================================================================================================
# cancel / deadlines / stalls
# ======================================================================================================================
async def test_cancel_mid_navigation_is_idempotent_and_stops_the_robot(services):
    wire(services, realtime_factor=1.0)
    counting = Counting(services.robot)
    services.robot = counting
    req = request(nav())
    it = services.vla.execute(req).__aiter__()
    assert (await it.__anext__()).phase == "accepted"
    while (await it.__anext__()).stage != "driving":
        pass
    a = await services.vla.cancel(req.exec_id, "shopper pressed stop")
    b = await services.vla.cancel(req.exec_id, "again")
    assert a.phase == b.phase == "cancelled" and a.seq == b.seq and a.error.code == "cancelled"
    assert a.error.message == "shopper pressed stop" and a.outcome.final_state is not None
    st = await counting.inner.state(R1)
    assert counting.calls["stop"] >= 1 and st.status != RobotStatus.NAVIGATING and st.pose.distance_to(AT_SALSA) > 0.1
    assert await services.vla.cancel_robot(R1) == []  # nothing left running


async def test_service_side_deadline_is_typed_and_stops_the_robot(services):
    wire(services, realtime_factor=1.0)
    counting = Counting(services.robot)
    services.robot = counting
    terminal, _ = await drain(services.vla, request(nav(), deadline_s=0.2), stall_factor=100.0)
    assert terminal.phase == "failed" and terminal.error.code == "deadline_exceeded"
    assert not terminal.error.details.get("stalled") and counting.calls["stop"] >= 1
    assert (await counting.inner.state(R1)).status != RobotStatus.NAVIGATING


async def test_slow_vla_hangs_silently_and_the_runner_detects_the_stall(services):
    wire(services, scenario="slow_vla")
    await services.robot.navigate(R1, [AT_SALSA])
    terminal, seen = await drain(services.vla, request(grasp(), heartbeat_s=0.05))
    assert terminal.phase == "failed" and terminal.error.code == "deadline_exceeded"
    assert terminal.error.details.get("stalled") is True and [s.phase for s in seen][0] == "accepted"
    rec = await services.vla.get_exec(request(grasp()).exec_id)
    assert rec.terminal is not None and rec.terminal.phase == "cancelled"  # the runner cancelled the hung exec
    terminal, _ = await drain(services.vla, request(grasp(), attempt=2))  # occurrence 1 / attempt 1 only
    assert terminal.phase == "succeeded" and terminal.outcome.holding_instance_id == "S-salsa#00"


async def test_service_deadline_on_a_hung_exec(services):
    wire(services, scenario="slow_vla")
    await services.robot.navigate(R1, [AT_SALSA])
    terminal, _ = await drain(services.vla, request(grasp(), deadline_s=0.2), stall_factor=100.0)
    assert terminal.phase == "failed" and terminal.error.code == "deadline_exceeded"
    assert not terminal.error.details.get("stalled")


# ======================================================================================================================
# faults -> the real outcome in the ground truth (what the verifier sees)
# ======================================================================================================================
async def test_drop_on_first_grasp_claims_success_but_the_gripper_is_empty(services):
    wire(services, scenario="drop_on_first_grasp")
    await services.robot.navigate(R1, [AT_SALSA])
    terminal, _ = await drain(services.vla, request(grasp()))
    assert terminal.phase == "succeeded" and terminal.outcome.self_report == "success"  # the CLAIM
    assert terminal.outcome.holding_instance_id is None and terminal.outcome.ended_at is not None
    t = await services.truth.truth(R1, ["S-salsa"])
    assert t.holding is None and len(t.slots["S-salsa"].instances) == 4  # the jar never left the shelf
    fired = [e for e in services.events.events if e.type == "demo.fault"]
    assert len(fired) == 1 and fired[0].source == "robot" and fired[0].firing.effect == "drop_after_lift"
    assert fired[0].trip_id == "trip-1" and fired[0].store_id == STORE and fired[0].scenario == "drop_on_first_grasp"
    terminal, _ = await drain(services.vla, request(grasp(), attempt=2))
    t = await services.truth.truth(R1)
    assert terminal.phase == "succeeded" and t.holding is not None and t.holding.sku == "salsa-mild"
    assert len(await services.faults.firings()) == 1


async def test_grasp_miss_is_an_honest_typed_failure(services):
    wire(services)
    await services.faults.inject(FaultRule(fault_id="f-miss", effect="grasp_miss", match=FaultMatch(step_kind="grasp")))
    await services.robot.navigate(R1, [AT_SALSA])
    terminal, _ = await drain(services.vla, request(grasp()))
    assert (
        terminal.phase == "failed"
        and terminal.error.code == "grasp_missed"
        and terminal.error.sim_code == "grasp_failed"
    )
    assert terminal.error.retryable and terminal.outcome.self_report == "failure"
    assert (await services.truth.truth(R1)).holding is None


async def test_wrong_item_really_grabs_the_neighbour_and_put_back_returns_it(services):
    rule = FaultRule(
        fault_id="f-wrong", effect="wrong_item", match=FaultMatch(sku="salsa-mild", step_kind="grasp", attempt=1)
    )
    wire(services)
    await services.faults.inject(rule)
    await services.robot.navigate(R1, [AT_SALSA])
    terminal, _ = await drain(services.vla, request(grasp()))
    assert terminal.phase == "succeeded" and terminal.message == "grasped salsa-mild"  # the CLAIM
    t = await services.truth.truth(R1, ["S-avocado"])
    assert t.holding is not None and t.holding.sku == "avocado" and t.labels[t.holding.instance_id] == "Avocado"
    assert terminal.outcome.holding_instance_id == t.holding.instance_id
    assert len(t.slots["S-avocado"].instances) == 4 and services.faults.overlay.virtual_holding == {}
    caps = {c.tier: c for c in await services.vla.capabilities(R1)}
    assert caps["t0_scripted"].place_back
    back = place("s03.r1.1", sku="avocado", destination="shelf", slot_id="S-avocado")
    terminal, _ = await drain(services.vla, request(back))
    t = await services.truth.truth(R1, ["S-avocado"])
    assert terminal.phase == "succeeded" and t.holding is None and len(t.slots["S-avocado"].instances) == 5
    assert [u.instance_id for u in t.slots["S-avocado"].instances] == sorted(
        u.instance_id for u in t.slots["S-avocado"].instances
    )
    terminal, _ = await drain(services.vla, request(grasp(), attempt=2))
    assert terminal.phase == "succeeded" and (await services.truth.truth(R1)).holding.sku == "salsa-mild"


async def test_place_miss_claims_a_place_but_the_unit_stays_in_the_gripper(services):
    wire(services)
    await services.faults.inject(FaultRule(fault_id="f-pm", effect="place_miss", match=FaultMatch(step_kind="place")))
    await services.robot.navigate(R1, [AT_SALSA])
    await drain(services.vla, request(grasp()))
    terminal, _ = await drain(services.vla, request(place()))
    t = await services.truth.truth(R1)
    assert terminal.phase == "succeeded" and t.holding is not None and t.basket == []
    terminal, _ = await drain(services.vla, request(place(), attempt=2))
    assert terminal.phase == "succeeded" and (await services.truth.truth(R1)).basket_counts() == {"salsa-mild": 1}


async def test_place_with_an_empty_gripper_is_place_failed(services):
    wire(services)
    terminal, _ = await drain(services.vla, request(place()))
    assert terminal.phase == "failed" and terminal.error.code == "place_failed"


async def test_nav_blocked_and_disconnect_faults(services):
    wire(services)
    await services.faults.inject(
        FaultRule(fault_id="f-nb", effect="nav_blocked", match=FaultMatch(step_kind="navigate_to"))
    )
    await services.faults.inject(FaultRule(fault_id="f-dc", effect="disconnect", match=FaultMatch(step_kind="align")))
    terminal, _ = await drain(services.vla, request(nav()))
    assert terminal.phase == "failed" and terminal.error.code == "path_blocked"
    terminal, _ = await drain(services.vla, request(align(location=None)))
    assert terminal.phase == "failed" and terminal.error.code == "disconnected"
    from contracts.vla import ERROR_TRAITS

    assert ERROR_TRAITS["disconnected"].counts_attempt is False


async def test_mislabel_fault_changes_the_label_the_truth_reports(services):
    wire(services)
    rule = FaultRule(
        fault_id="f-ml", effect="mislabel", match=FaultMatch(step_kind="grasp"), params={"text": "Hot Salsa"}
    )
    await services.faults.inject(rule)
    await services.robot.navigate(R1, [AT_SALSA])
    terminal, _ = await drain(services.vla, request(grasp()))
    t = await services.truth.truth(R1)
    assert terminal.phase == "succeeded" and t.labels[t.holding.instance_id] == "Hot Salsa"


async def test_grasp_reach_precheck_fails_before_any_motion(services):
    wire(services)
    counting = Counting(services.robot)
    services.robot = counting
    terminal, _ = await drain(services.vla, request(grasp()))  # still on the dock
    assert terminal.phase == "failed" and terminal.error.code == "not_at_pose" and counting.calls["pick"] == 0
    await counting.inner.navigate(R1, [Pose2D(x=3.0, y=3.4, theta=UP)])  # 0.22 m short of the approach pose
    terminal, _ = await drain(services.vla, request(grasp(), attempt=2))
    assert terminal.phase == "failed" and terminal.error.code == "out_of_reach" and counting.calls["pick"] == 0


async def test_capture_after_uses_evidence_with_the_front_camera_aimed_at_the_slot(services):
    wire(services)
    services.evidence = StubEvidence()  # type: ignore[assignment]
    await services.robot.navigate(R1, [AT_SALSA])
    terminal, _ = await drain(services.vla, request(grasp(), capture_after=["wrist", "front"]))
    call = services.evidence.calls[0]
    assert (
        call["aim"] == "S-salsa" and call["cameras"] == ["wrist", "front"] and call["context"]["step_kind"] == "grasp"
    )
    assert call["context"]["step_key"] == "p:s03" and call["context"]["slot_ids"] == ["S-salsa"]
    frames = terminal.outcome.frames
    assert [f.camera_id for f in frames] == [f"{R1}.wrist", f"{R1}.front"]
    assert all(f.ts >= terminal.outcome.ended_at for f in frames)  # not_before = ended_at keeps them


# ======================================================================================================================
# tiers
# ======================================================================================================================
async def test_requested_t1_falls_back_to_t0_at_accept_or_fails_typed(services):
    wire(services)
    await services.robot.navigate(R1, [AT_SALSA])
    terminal, seen = await drain(services.vla, request(grasp(), tier="t1_pretrained"))
    assert seen[0].fell_back_from == "t1_pretrained" and seen[0].tier == "t0_scripted" and terminal.phase == "succeeded"
    req = request(grasp("s07"), tier="t1_pretrained", allow_fallback=False)
    terminal, _ = await drain(services.vla, req)
    assert terminal.phase == "failed" and terminal.error.code == "policy_unavailable"


async def test_simulated_t1_counts_chunks_and_flaky_t1_misses_then_t0_works(services):
    wire(services, scenario="flaky_t1", simulate_t1=True)
    await services.robot.navigate(R1, [AT_SALSA])
    terminal, seen = await drain(services.vla, request(grasp()))  # auto -> t1 (available)
    assert seen[0].tier == "t1_pretrained" and terminal.phase == "failed" and terminal.error.code == "grasp_missed"
    assert terminal.error.tier == "t1_pretrained" and any(s.chunk_no for s in seen)
    terminal, _ = await drain(services.vla, request(grasp(), attempt=2, tier="t0_scripted"))
    assert terminal.phase == "succeeded" and terminal.tier == "t0_scripted"


async def test_policy_unavailable_fault_falls_back_inside_the_exec(services):
    wire(services, simulate_t1=True)
    await services.faults.inject(FaultRule(fault_id="f-pu", effect="policy_unavailable"))
    await services.robot.navigate(R1, [AT_SALSA])
    terminal, seen = await drain(services.vla, request(grasp(), tier="t1_pretrained"))
    assert (
        seen[0].fell_back_from == "t1_pretrained" and terminal.tier == "t0_scripted" and terminal.phase == "succeeded"
    )


async def test_perceptron_tool_t1_grasps_the_grounded_unit_only(services):
    wire(services)
    services.vla = T0Vla(services, t1=T1Adapter(T1Config(backend="perceptron_tool", api_key_set=True)))
    await services.robot.navigate(R1, [AT_SALSA])
    terminal, _ = await drain(services.vla, request(grasp(), tier="t1_pretrained"))
    assert terminal.phase == "failed" and terminal.error.code == "target_not_grounded"
    g = GroundedTarget(grounding_id="g1", target=SALSA, confidence=0.9, instance_id="S-salsa#02")
    terminal, _ = await drain(services.vla, request(grasp(), attempt=2, tier="t1_pretrained", grounding=g))
    assert terminal.phase == "succeeded" and terminal.tier == "t1_pretrained"
    assert terminal.outcome.holding_instance_id == "S-salsa#02"


# ======================================================================================================================
# Isaac (SimRobot over a loopback worker): the same T0 code
# ======================================================================================================================
async def test_t0_runs_unchanged_on_isaac(services):
    link = await on_isaac(services)
    terminal, _ = await drain(services.vla, request(nav()))
    assert terminal.phase == "succeeded" and terminal.backend == "isaac"
    terminal, _ = await drain(services.vla, request(grasp()))
    assert terminal.phase == "succeeded" and terminal.outcome.final_state.backend == "isaac"
    terminal, _ = await drain(services.vla, request(place()))
    assert terminal.phase == "succeeded"
    kinds = [type(c) for c in link.sent]
    assert NavigateCmd in kinds and PickCmd in kinds and PlaceInBasketCmd in kinds
    t = await services.truth.truth(R1)
    assert t.source == "isaac" and t.basket_counts() == {"salsa-mild": 1}
    caps = {c.tier: c for c in await services.vla.capabilities(R1)}
    assert caps["t0_scripted"].backend == "isaac" and not caps["t0_scripted"].place_back
    assert caps["t1_pretrained"].reason  # unavailable, with the reason


async def test_put_back_is_unsupported_on_isaac_for_a_real_unit(services):
    await on_isaac(services)
    await services.robot.navigate(R1, [AT_SALSA])
    await drain(services.vla, request(grasp()))
    back = place("s03.r1.1", destination="shelf", slot_id="S-salsa")
    terminal, _ = await drain(services.vla, request(back))
    assert terminal.phase == "failed" and terminal.error.code == "unsupported_step"


async def test_wrong_item_on_isaac_uses_the_overlay_and_can_be_put_back(services):
    await on_isaac(services)
    rule = FaultRule(fault_id="f-wrong", effect="wrong_item", match=FaultMatch(step_kind="grasp", attempt=1))
    await services.faults.inject(rule)
    await services.robot.navigate(R1, [AT_SALSA])
    terminal, _ = await drain(services.vla, request(grasp()))
    t = await services.truth.truth(R1, ["S-avocado"])
    assert terminal.phase == "succeeded" and t.holding is not None and t.holding.sku == "avocado"
    assert terminal.outcome.holding_instance_id == t.holding.instance_id
    assert t.holding.instance_id not in {u.instance_id for u in t.slots["S-avocado"].instances}
    assert (await services.robot.state(R1)).holding is None  # nothing really attached on Isaac
    assert {c.tier: c for c in await services.vla.capabilities(R1)}["t0_scripted"].place_back
    terminal, _ = await drain(services.vla, request(place("s03.r1.1", sku="avocado", destination="shelf")))
    assert terminal.phase == "succeeded" and (await services.truth.truth(R1)).holding is None


# ======================================================================================================================
# housekeeping
# ======================================================================================================================
async def test_exec_records_are_pruned_after_an_hour(services):
    wire(services)
    await drain(services.vla, request(nav()))
    rec = services.vla._execs["p:s01:a1"].record
    rec.created_at = utcnow() - timedelta(hours=2)
    await drain(services.vla, request(nav("s05")))
    assert "p:s01:a1" not in services.vla._execs and "p:s05:a1" in services.vla._execs


async def test_two_robots_run_concurrently(services):
    wire(services, realtime_factor=math.inf)
    a, b = await asyncio.gather(
        drain(services.vla, request(nav())),
        drain(services.vla, request(nav("s11", goal=AT_AVO, slot="S-avocado"), robot_id=R2)),
    )
    assert a[0].phase == b[0].phase == "succeeded"
